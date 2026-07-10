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
from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

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
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def load_memory(state: GraphState) -> GraphState:
    """Memory & Constraints: load the generic profile, split hard/soft.

    TODO(v2-M1):
    - memory.store.load_family_profile() -> state["memory"].
    - profile["hard"] is the safety policy (allergens, medical, dietary,
      budget_cap, quiet_hours): kept as DATA for verbatim injection.
    - profile["soft"] + members + context: planning bias only.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def build_contract(state: GraphState) -> GraphState:
    """Assemble + validate the generic Task Contract (models.contract.Dispatch).

    TODO(v2-M1):
    - Merge intent (LLM) with constraints.hard copied VERBATIM from memory.
    - constraints.soft from soft prefs; scope/context stay domain-flexible.
    - autonomy = "tiered"; mint goal_id + correlation_id.
    - Validate via Dispatch(**frame); stash the frame in state["contract"].
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def dispatch_to_device(state: GraphState) -> GraphState:
    """Hand the dispatch frame to the WS hub (the hub owns socket IO).

    TODO(v2-M1): mark task_status="planning"; the hub sends the frame and
    relays the device's agent_event stream while the graph waits.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def collect_plan(state: GraphState) -> GraphState:
    """Resume point: the hub feeds the device's plan_ready payload in.

    TODO(v2-M1): record payload into state["plan"]; extract proposals with
    requires_approval into state["pending_approvals"]; task_status="checking".
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


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
    _ = interrupt  # design anchor: this node is the interrupt() site
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def relay_decisions(state: GraphState) -> GraphState:
    """Forward the approval decisions to the device via the hub.

    TODO(v2-M1): build the Approval frame from state["decisions"];
    task_status="executing" then "monitoring".
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def monitor(state: GraphState) -> GraphState:
    """Monitor & Adapt: track device status/proposals for material changes.

    TODO(v2-M1): fed by the hub on status/proposal frames; a MATERIAL change
    (or an adaptation proposal) populates pending_approvals for the adapt
    loop; task_status="monitoring"|"adapting"|"done".
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def explain_block(state: GraphState) -> GraphState:
    """Trace/Explain: safety-blocked plan or LLM error -> user-facing why.

    TODO(v2-M1): compose the explanation from safety.violations or
    state["error"]; never silently retry or fake a plan.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def finalize(state: GraphState) -> GraphState:
    """Close out the goal; emit the final trace; task_status="done".

    TODO(v2-M1): summarize event_log for the Trace/Explain surface.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


# ---------------------------------------------------------------------------
# Conditional-edge routers
# ---------------------------------------------------------------------------


def route_after_interpret(state: GraphState) -> str:
    """error -> explain_block (LLM-only, fail loudly); else -> load_memory."""
    # TODO(v2-M1): return "explain_block" if state.get("error") else "load_memory"
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def route_on_safety(state: GraphState) -> str:
    """After collect_plan: blocked -> explain_block; approvals pending ->
    hitl_approval; auto-tier only -> relay_decisions."""
    # TODO(v2-M1): inspect state["plan"]["safety"]["gate"] + pending_approvals.
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def route_on_monitor(state: GraphState) -> str:
    """After monitor: material change -> hitl_approval (adapt loop);
    done -> finalize; else keep monitoring."""
    # TODO(v2-M1): inspect task_status / pending_approvals / material flag.
    raise NotImplementedError("v2 design stub — implemented in the build pass")


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

    TODO(v2-M1): graph.invoke({"goal_text": goal_text, "goal_id": goal_id},
    config={"configurable": {"thread_id": goal_id}}) and pull
    state["contract"] out (the run pauses at the device hand-off).
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def resume_goal(graph: Any, goal_id: str, resume_value: Any) -> dict[str, Any]:
    """Resume a paused run (plan_ready arrival, approval, adapt decision).

    TODO(v2-M1): graph.invoke(Command(resume=resume_value),
    config={"configurable": {"thread_id": goal_id}}).
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")
