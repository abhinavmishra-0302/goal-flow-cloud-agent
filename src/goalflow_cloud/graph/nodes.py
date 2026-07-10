"""GoalFlow v2 LangGraph — node signatures + graph skeleton (DESIGN PASS).

Advanced LangGraph StateGraph: conditional edges, ``interrupt()``-based HITL,
and a checkpointer so the approval pause (and the adapt loop) survive across
frames/reconnects. One graph run per goal; ``thread_id = goal_id``.

Pipeline (see docs/ARCHITECTURE.md for the full design + Mermaid diagram):

    interpret_goal -> load_memory -> build_contract -> dispatch_to_device
        -> [device plans; agent_events stream through the hub]
        -> collect_plan -> (safety route) -> hitl_approval [interrupt()]
        -> relay_decisions -> monitor -> (adapt loop | finalize)

Key invariants:
- LLM-ONLY: interpret_goal is a real structured-output LLM call via OpenRouter;
  there is NO scripted fallback. Failure sets state["error"] and routes to
  explain_block.
- Hard constraints flow through load_memory as DATA, verbatim into
  contract.constraints.hard. The LLM never generates, edits, or paraphrases
  the safety policy. "LLM plans, code checks."
- The device does the actual planning (SK function calling); the cloud graph
  pauses at its hand-off points and is resumed by the WS hub.
"""

from __future__ import annotations

import logging
from datetime import date
from operator import add
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError

from goalflow_cloud.config import get_settings
from goalflow_cloud.memory.store import hard_safety_block, load_family_profile, soft_bias_block
from goalflow_cloud.models.contract import Dispatch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------


class GraphState(TypedDict, total=False):
    """Durable per-goal state, checkpointed across interrupts."""

    #: Raw user_goal text from the UI.
    goal_text: str
    #: Normalized intent (LLM structured output): domain, objective,
    #: success_criteria, scope, time_window (relative to real today).
    intent: dict[str, Any]
    #: Loaded memory profile: hard safety block + soft prefs + family context.
    memory: dict[str, Any]
    #: The assembled generic Dispatch frame (models.contract.Dispatch shape).
    contract: dict[str, Any]
    #: plan_ready payload from the device (plan + tiered proposals + safety).
    plan: dict[str, Any]
    #: Proposals awaiting user decisions (requires_approval == True).
    pending_approvals: list[dict[str, Any]]
    #: Approval decisions returned by the interrupt() resume.
    decisions: list[dict[str, Any]]
    #: CONTRACT v2 lifecycle value (created ... done).
    task_status: str
    #: Append-only trace of agent_events / node transitions (reducer: add).
    event_log: Annotated[list[dict[str, Any]], add]
    goal_id: str
    correlation_id: str
    #: Structured failure — LLM-only design: errors surface, never faked.
    error: str
    approval_frame: dict[str, Any]
    monitor_frame: dict[str, Any]
    explanation: dict[str, Any]


class InterpretedIntent(BaseModel):
    """Structured LLM output for the generic goal interpreter."""

    domain: str = Field(description="Short generic domain id, e.g. meal_plan, chores, errands.")
    objective: str = Field(description="A concise normalized objective.")
    success_criteria: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    time_window: dict[str, str] = Field(
        description="ISO start/end dates or datetimes, relative to the supplied real today."
    )


def _event(state: GraphState, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "event": event,
        "goal_id": state.get("goal_id"),
        "correlation_id": state.get("correlation_id"),
        "payload": payload or {},
    }


# ---------------------------------------------------------------------------
# Nodes (harness modules) — signatures + TODO stubs
# ---------------------------------------------------------------------------


def interpret_goal(state: GraphState) -> GraphState:
    """Goal Interpreter: fuzzy goal text -> structured intent (LLM ONLY).

    TODO(v2-M1):
    - ChatOpenAI (OpenRouter base_url/model from config) with structured
      output: {domain, objective, success_criteria, scope, time_window}.
    - time_window computed RELATIVE to real today (never hardcoded).
    - On LLM failure: set state["error"] (routes to explain_block). NO
      scripted fallback.
    """
    goal_text = state.get("goal_text", "").strip()
    logger.info("graph_node_enter node=interpret_goal")
    if not goal_text:
        return {
            "error": "user_goal.text is required",
            "task_status": "interpreting",
            "event_log": [_event(state, "interpret_error", {"error": "empty_goal"})],
        }

    settings = get_settings()
    today = date.today()
    try:
        llm = ChatOpenAI(
            model=settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            temperature=0,
            # Cap the token reservation. Interpreting a goal into a small
            # structured intent needs little; leaving this unset makes OpenRouter
            # reserve the model max (~65k), which a low-credit key can't afford
            # (HTTP 402). Configurable via OPENROUTER_MAX_TOKENS.
            max_tokens=settings.openrouter_max_tokens,
            timeout=45,
            max_retries=1,
        )
        structured_llm = llm.with_structured_output(InterpretedIntent, method="function_calling")
        intent = structured_llm.invoke(
            [
                (
                    "system",
                    "You are the GoalFlow cloud goal interpreter. Convert the user's natural-language "
                    "goal into a generic, domain-agnostic task intent. Do not invent safety constraints. "
                    f"Real today is {today.isoformat()}; resolve phrases like this week, today, "
                    "tomorrow, weekend, next week into time_window.start/end ISO dates relative to it. "
                    "For actionable goals, the start date must be real today or later; interpret "
                    "'this week' as the remaining week starting today. "
                    "Keep scope flexible and generic for the device planner.",
                ),
                ("human", goal_text),
            ]
        )
        intent_dict = intent.model_dump(mode="json")
        if not intent_dict.get("time_window", {}).get("start") or not intent_dict.get("time_window", {}).get("end"):
            return {
                "error": "LLM goal interpretation failed: missing time_window.start/end",
                "task_status": "interpreting",
                "event_log": [_event(state, "interpret_error", {"error": "missing_time_window"})],
            }
        logger.info("graph_node_exit node=interpret_goal domain=%s", intent_dict.get("domain"))
        return {
            "intent": intent_dict,
            "task_status": "interpreting",
            "event_log": [_event(state, "intent_interpreted", {"domain": intent_dict.get("domain")})],
        }
    except Exception as exc:  # LLM-only: fail loudly, never synthesize a fallback intent.
        logger.exception("graph_node_error node=interpret_goal")
        return {
            "error": f"LLM goal interpretation failed: {type(exc).__name__}: {exc}",
            "task_status": "interpreting",
            "event_log": [_event(state, "interpret_error", {"error": str(exc)})],
        }


def load_memory(state: GraphState) -> GraphState:
    """Memory & Constraints: load the generic profile, split hard/soft.

    TODO(v2-M1):
    - memory.store.load_family_profile() -> state["memory"].
    - profile["hard"] is the safety policy (allergens, medical, dietary,
      budget_cap, quiet_hours): kept as DATA for verbatim injection.
    - profile["soft"] + members + context: planning bias only.
    """
    logger.info("graph_node_enter node=load_memory")
    profile = load_family_profile()
    memory = {
        "family_id": profile.get("family_id"),
        "hard": hard_safety_block(profile),
        "bias": soft_bias_block(profile),
    }
    logger.info("graph_node_exit node=load_memory family_id=%s", memory.get("family_id"))
    return {
        "memory": memory,
        "task_status": "grounding",
        "event_log": [_event(state, "memory_loaded", {"family_id": memory.get("family_id")})],
    }


def build_contract(state: GraphState) -> GraphState:
    """Assemble + validate the generic Task Contract (models.contract.Dispatch).

    TODO(v2-M1):
    - Merge intent (LLM) with constraints.hard copied VERBATIM from memory.
    - constraints.soft from soft prefs; scope/context stay domain-flexible.
    - autonomy = "tiered"; mint goal_id + correlation_id.
    - Validate via Dispatch(**frame); stash the frame in state["contract"].
    """
    logger.info("graph_node_enter node=build_contract")
    intent = state["intent"]
    memory = state["memory"]
    goal_id = state.get("goal_id") or str(uuid4())
    correlation_id = state.get("correlation_id") or str(uuid4())

    context = {
        "family_id": memory.get("family_id"),
        **memory.get("bias", {}),
    }
    frame = {
        "type": "dispatch",
        "goal_id": goal_id,
        "correlation_id": correlation_id,
        "domain": intent["domain"],
        "objective": intent["objective"],
        "success_criteria": intent.get("success_criteria", []),
        "constraints": {
            "hard": memory.get("hard", {}),
            "soft": memory.get("bias", {}).get("soft", {}),
        },
        "scope": intent.get("scope", {}),
        "time_window": intent.get("time_window"),
        "autonomy": "tiered",
        "context": context,
    }
    try:
        dispatch = Dispatch(**frame)
    except ValidationError as exc:
        logger.exception("graph_node_error node=build_contract")
        return {
            "error": f"Dispatch validation failed: {exc}",
            "task_status": "checking",
            "event_log": [_event(state, "dispatch_validation_error", {"error": str(exc)})],
        }

    contract = dispatch.model_dump(mode="json", exclude_none=True)
    logger.info("graph_node_exit node=build_contract goal_id=%s", goal_id)
    return {
        "goal_id": goal_id,
        "correlation_id": correlation_id,
        "contract": contract,
        "task_status": "planning",
        "event_log": [_event(state, "dispatch_built", {"domain": contract.get("domain")})],
    }


def dispatch_to_device(state: GraphState) -> GraphState:
    """Hand the dispatch frame to the WS hub (the hub owns socket IO).

    TODO(v2-M1): mark task_status="planning"; the hub sends the frame and
    relays the device's agent_event stream while the graph waits.
    """
    logger.info("graph_node_enter node=dispatch_to_device")
    return {
        "task_status": "planning",
        "event_log": [_event(state, "dispatch_ready", {"goal_id": state.get("goal_id")})],
    }


def collect_plan(state: GraphState) -> GraphState:
    """Resume point: the hub feeds the device's plan_ready payload in.

    TODO(v2-M1): record payload into state["plan"]; extract proposals with
    requires_approval into state["pending_approvals"]; task_status="checking".
    """
    logger.info("graph_node_enter node=collect_plan")
    incoming = interrupt(
        {
            "kind": "await_plan_ready",
            "goal_id": state.get("goal_id"),
            "correlation_id": state.get("correlation_id"),
        }
    )
    payload = incoming.get("payload", incoming) if isinstance(incoming, dict) else {}
    proposals = payload.get("proposals", []) if isinstance(payload, dict) else []
    pending = [proposal for proposal in proposals if proposal.get("requires_approval", True)]
    logger.info("graph_node_exit node=collect_plan pending_approvals=%s", len(pending))
    return {
        "plan": payload,
        "pending_approvals": pending,
        "task_status": "checking",
        "event_log": [_event(state, "plan_collected", {"pending_approvals": len(pending)})],
    }


def hitl_approval(state: GraphState) -> GraphState:
    """Approval/Consent (HITL): pause on the tiered proposals via interrupt().

    TODO(v2-M1):
        decisions = interrupt({
            "pending_approvals": state.get("pending_approvals", []),
        })
    The checkpointer persists the paused state; the hub resumes with the
    user's approval decisions (Command(resume=...)). Nothing firm executes
    until this returns. ``interrupt`` imported above is the real primitive.
    """
    logger.info("graph_node_enter node=hitl_approval pending_approvals=%s", len(state.get("pending_approvals", [])))
    decisions = interrupt(
        {
            "kind": "approval_required",
            "goal_id": state.get("goal_id"),
            "correlation_id": state.get("correlation_id"),
            "pending_approvals": state.get("pending_approvals", []),
            "plan": state.get("plan", {}),
        }
    )
    if isinstance(decisions, dict) and "payload" in decisions:
        decisions = decisions.get("payload", {}).get("decisions", [])
    elif isinstance(decisions, dict) and "decisions" in decisions:
        decisions = decisions["decisions"]
    decisions = decisions or []
    logger.info("graph_node_exit node=hitl_approval decisions=%s", len(decisions))
    return {
        "decisions": decisions,
        "task_status": "awaiting_approval",
        "event_log": [_event(state, "approval_received", {"decisions": len(decisions)})],
    }


def relay_decisions(state: GraphState) -> GraphState:
    """Forward the approval decisions to the device via the hub.

    TODO(v2-M1): build the Approval frame from state["decisions"];
    task_status="executing" then "monitoring".
    """
    logger.info("graph_node_enter node=relay_decisions")
    decisions = state.get("decisions")
    if decisions is None:
        decisions = [
            {"proposal_id": proposal["proposal_id"], "approved": True}
            for proposal in state.get("plan", {}).get("proposals", [])
            if proposal.get("tier") == "auto"
        ]
    frame = {
        "type": "approval",
        "goal_id": state["goal_id"],
        "correlation_id": state["correlation_id"],
        "payload": {"decisions": decisions},
    }
    logger.info("graph_node_exit node=relay_decisions decisions=%s", len(decisions))
    return {
        "approval_frame": frame,
        "task_status": "executing",
        "event_log": [_event(state, "decisions_relay_ready", {"decisions": len(decisions)})],
    }


def monitor(state: GraphState) -> GraphState:
    """Monitor & Adapt: track device status/proposals for material changes.

    TODO(v2-M1): fed by the hub on status/proposal frames; a MATERIAL change
    (or an adaptation proposal) populates pending_approvals for the adapt
    loop; task_status="monitoring"|"adapting"|"done".
    """
    logger.info("graph_node_enter node=monitor")
    incoming = interrupt(
        {
            "kind": "await_monitor_frame",
            "goal_id": state.get("goal_id"),
            "correlation_id": state.get("correlation_id"),
        }
    )
    if not isinstance(incoming, dict):
        return {"task_status": "monitoring"}

    frame_type = incoming.get("type")
    payload = incoming.get("payload", {})
    update: GraphState = {
        "monitor_frame": incoming,
        "task_status": incoming.get("task_status", "monitoring"),
        "event_log": [_event(state, "monitor_frame", {"type": frame_type})],
    }
    if frame_type == "proposal" and payload.get("requires_approval", True):
        update["pending_approvals"] = [payload]
        update["task_status"] = "adapting"
    elif frame_type == "status":
        if payload.get("material"):
            update["task_status"] = "adapting"
        if incoming.get("task_status") == "done":
            update["task_status"] = "done"
    logger.info("graph_node_exit node=monitor frame_type=%s task_status=%s", frame_type, update.get("task_status"))
    return update


def explain_block(state: GraphState) -> GraphState:
    """Trace/Explain: safety-blocked plan or LLM error -> user-facing why.

    TODO(v2-M1): compose the explanation from safety.violations or
    state["error"]; never silently retry or fake a plan.
    """
    logger.info("graph_node_enter node=explain_block")
    safety = state.get("plan", {}).get("safety", {})
    violations = safety.get("violations", [])
    message = state.get("error") or state.get("plan", {}).get("explanation") or "Plan blocked by safety policy."
    explanation = {
        "type": "blocked",
        "message": message,
        "violations": violations,
    }
    return {
        "explanation": explanation,
        "task_status": "done",
        "event_log": [_event(state, "explain_block", explanation)],
    }


def finalize(state: GraphState) -> GraphState:
    """Close out the goal; emit the final trace; task_status="done".

    TODO(v2-M1): summarize event_log for the Trace/Explain surface.
    """
    logger.info("graph_node_enter node=finalize")
    return {
        "task_status": "done",
        "event_log": [_event(state, "finalized", {"events": len(state.get("event_log", []))})],
    }


# ---------------------------------------------------------------------------
# Conditional-edge routers
# ---------------------------------------------------------------------------


def route_after_interpret(state: GraphState) -> str:
    """error -> explain_block (LLM-only, fail loudly); else -> load_memory."""
    return "explain_block" if state.get("error") else "load_memory"


def route_on_safety(state: GraphState) -> str:
    """After collect_plan: blocked -> explain_block; approvals pending ->
    hitl_approval; auto-tier only -> relay_decisions."""
    if state.get("plan", {}).get("safety", {}).get("gate") == "blocked":
        return "explain_block"
    if state.get("pending_approvals"):
        return "hitl_approval"
    return "relay_decisions"


def route_on_monitor(state: GraphState) -> str:
    """After monitor: material change -> hitl_approval (adapt loop);
    done -> finalize; else keep monitoring."""
    if state.get("pending_approvals") and state.get("task_status") == "adapting":
        return "hitl_approval"
    if state.get("task_status") == "done":
        return "finalize"
    return "monitor"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------


def build_graph(checkpointer: Any | None = None) -> Any:
    """Assemble and compile the v2 StateGraph.

    The checkpointer (default: in-process MemorySaver) makes the interrupt()
    pause durable; every invoke/resume must pass
    ``config={"configurable": {"thread_id": goal_id}}``.
    """
    graph = StateGraph(GraphState)

    graph.add_node("interpret_goal", interpret_goal)
    graph.add_node("load_memory", load_memory)
    graph.add_node("build_contract", build_contract)
    graph.add_node("dispatch_to_device", dispatch_to_device)
    graph.add_node("collect_plan", collect_plan)
    graph.add_node("hitl_approval", hitl_approval)
    graph.add_node("relay_decisions", relay_decisions)
    graph.add_node("monitor", monitor)
    graph.add_node("explain_block", explain_block)
    graph.add_node("finalize", finalize)

    graph.set_entry_point("interpret_goal")
    graph.add_conditional_edges(
        "interpret_goal",
        route_after_interpret,
        {"load_memory": "load_memory", "explain_block": "explain_block"},
    )
    graph.add_edge("load_memory", "build_contract")
    graph.add_edge("build_contract", "dispatch_to_device")
    # Device plans here (SK function calling); the hub streams agent_events
    # and resumes the graph at collect_plan when plan_ready arrives.
    graph.add_edge("dispatch_to_device", "collect_plan")
    graph.add_conditional_edges(
        "collect_plan",
        route_on_safety,
        {
            "hitl_approval": "hitl_approval",
            "relay_decisions": "relay_decisions",
            "explain_block": "explain_block",
        },
    )
    graph.add_edge("hitl_approval", "relay_decisions")
    graph.add_edge("relay_decisions", "monitor")
    graph.add_conditional_edges(
        "monitor",
        route_on_monitor,
        {
            "hitl_approval": "hitl_approval",  # the adapt loop
            "monitor": "monitor",
            "finalize": "finalize",
        },
    )
    graph.add_edge("explain_block", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())


# ---------------------------------------------------------------------------
# Hub-facing entry points (called by server.py)
# ---------------------------------------------------------------------------


def start_goal(graph: Any, goal_text: str, goal_id: str) -> dict[str, Any]:
    """Kick off a goal run; returns the dispatch frame for the hub to send.

    graph.invoke({"goal_text": goal_text, "goal_id": goal_id},
    config={"configurable": {"thread_id": goal_id}}) and pull state["contract"]
    out after the run pauses at the device hand-off.
    """
    config = {"configurable": {"thread_id": goal_id}}
    graph.invoke(
        {"goal_text": goal_text, "goal_id": goal_id, "task_status": "created", "event_log": []},
        config=config,
    )
    state = graph.get_state(config).values
    if state.get("error"):
        return dict(state)
    if not state.get("contract"):
        raise RuntimeError("graph did not produce a dispatch contract")
    return dict(state)


def resume_goal(graph: Any, goal_id: str, resume_value: Any) -> dict[str, Any]:
    """Resume a paused run (plan_ready arrival, approval, adapt decision).

    Resume with Command(resume=resume_value), preserving the thread_id.
    """
    config = {"configurable": {"thread_id": goal_id}}
    graph.invoke(Command(resume=resume_value), config=config)
    return dict(graph.get_state(config).values)
