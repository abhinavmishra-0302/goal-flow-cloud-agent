"""GoalFlow v2 LangGraph — node signatures + graph skeleton (DESIGN PASS).

Advanced LangGraph StateGraph: conditional edges, ``interrupt()``-based HITL,
and a checkpointer so the approval pause (and the adapt loop) survive across
frames/reconnects. One graph run per goal; ``thread_id = goal_id``.

Pipeline (see docs/ARCHITECTURE.md for the full design + Mermaid diagram):

    interpret_goal -> load_memory -> present_understanding [interrupt()]
        -> build_contract -> dispatch_to_device
        -> [device plans; agent_events stream through the hub]
        -> collect_plan -> (safety route) -> hitl_approval [interrupt()]
        -> relay_decisions -> monitor -> (adapt loop | finalize)

Key invariants:
- LLM-ONLY: interpret_goal is a real structured-output LLM call via OpenRouter;
  there is NO scripted fallback. Failure sets state["error"] and routes to
  explain_block.
- Hard constraints flow through load_memory as DATA — resolved from the
  household constraint store BY CODE (memory.store.resolve_constraints), then
  copied into contract.constraints.hard. The LLM never generates, edits, or
  paraphrases the safety policy; its only say over constraints is which SOFT
  preferences are relevant. "LLM plans, code checks."
- The device does the actual planning (SK function calling); the cloud graph
  pauses at its hand-off points and is resumed by the WS hub.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from operator import add
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError

from goalflow_cloud.config import get_settings
from goalflow_cloud.memory.store import (
    append_constraints,
    load_family_profile,
    resolve_constraints,
    soft_candidates,
)
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
    #: v6-M4: household rules the user STATED in this message, awaiting confirmation.
    #: Proposals only — nothing here is policy until the user says yes at the gate.
    proposed_constraints: list[dict[str, Any]]
    #: Pre-planning understanding shown to the user before dispatch.
    understanding: dict[str, Any]
    #: User response to the understanding gate.
    understanding_confirmed: bool
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
    # What the connected device says it can do (its `capabilities` frame, cached
    # per session by the hub). The interpreter judges actionability against THIS
    # rather than a hardcoded topic list — see capability_digest.
    device_capabilities: dict[str, Any]


class InterpretedIntent(BaseModel):
    """Structured LLM output for the generic goal interpreter."""

    domain: str = Field(description="Short slug naming the KIND of goal — set per the DOMAIN rule in the system prompt (match the advertised goal-shape whose hint fits the goal, else coin one). NOT a default.")
    objective: str = Field(description="A concise normalized objective.")
    title: str = Field(
        default="",
        description=(
            "A SHORT title-case noun phrase naming the goal, max 4 words — "
            "'Birthday Party Preparation', 'Weekly Meal Plan', 'Vacation Prep'. "
            "This is a card headline, not a sentence: no verbs, no dates, no detail."
        ),
    )
    success_criteria: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    time_window: dict[str, str] = Field(
        default_factory=dict,
        description="ISO start/end dates or datetimes, relative to the supplied real today. "
        "Leave empty when the goal is not actionable.",
    )
    actionable: bool = Field(
        default=True,
        description=(
            "True if the goal can plausibly be advanced using the capabilities the "
            "connected device advertises; False when nothing it can do relates to the "
            "goal (trivia, general questions, unrelated tasks)."
        ),
    )
    decline_reason: str = Field(
        default="",
        description="If not actionable, one short plain-language reason why (used internally).",
    )


def _event(state: GraphState, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "event": event,
        "goal_id": state.get("goal_id"),
        "correlation_id": state.get("correlation_id"),
        "payload": payload or {},
    }


def capability_digest(capabilities: dict[str, Any] | None) -> str:
    """Render the device's advertised toolbox for the interpreter's prompt.

    THE POINT OF v3-M4: what this assistant can do is a fact about the DEVICE that
    is plugged in, not a fact about the cloud. The device already tells us — the
    ``capabilities`` frame lands right after ``hello_ack`` and the hub has been
    caching it since v2 and never showing it to anyone who could act on it. So the
    gate stops being a hardcoded list of two domains and starts being a reading of
    what is actually there.

    Only capability modules, and only their descriptions: the steering modules are
    the harness's own plumbing (Safety, Trace, …) and say nothing about what a
    family can ask for.
    """
    domains = (capabilities or {}).get("domains") or []
    modules = [
        m for m in ((capabilities or {}).get("modules") or [])
        if m.get("kind") == "capability"
    ]
    if not modules and not domains:
        # Empty means "we don't know", and the caller must NOT proceed on a guess —
        # returning a bare header here would read as a device that advertises
        # nothing, which is a different (and wrong) claim.
        return ""

    lines: list[str] = []
    if domains:
        lines.append("Goal shapes this device understands and can sustain:")
        lines.extend(f"- {d.get('id')}: {d.get('hint')}" for d in domains)
        lines.append("")
    lines.append("Capability modules:")
    for module in modules:
        name = module.get("name", "")
        description = module.get("description") or ""
        functions = ", ".join(
            fn.get("name", "") for fn in (module.get("functions") or [])
        )
        lines.append(f"- {name}: {description} (functions: {functions})")
    return "\n".join(lines)


def _capability_summary(capabilities: dict[str, Any] | None) -> str:
    """A human phrase for what this device offers — for the redirect message.

    Uses each capability module's own description, which the device already writes
    for humans ("The shared family calendar — who is busy when"), so a new plugin
    describes itself in the refusal without anyone editing this file.
    """
    modules = (capabilities or {}).get("modules") or []
    topics = [
        (m.get("description") or m.get("name", "")).split("—")[0].strip().rstrip(".").lower()
        for m in modules
        if m.get("kind") == "capability"
    ]
    topics = [t for t in topics if t][:4]
    if not topics:
        return ""
    if len(topics) == 1:
        return topics[0]
    return ", ".join(topics[:-1]) + f" and {topics[-1]}"


def _hard_knew(hard: dict[str, Any] | None) -> dict[str, Any]:
    """Display-ready hard-constraint chips shared by the gate and plan card."""
    hard = hard or {}
    knew: dict[str, Any] = {}

    def add(label: str, value: Any) -> None:
        # Only surface flat, display-ready values (str / list[str]); never raw
        # nested objects except quiet_hours, which is intentionally stringified.
        if isinstance(value, list):
            items = [str(v) for v in value if str(v).strip()]
            if items:
                knew[label] = items
        elif isinstance(value, str) and value.strip():
            knew[label] = value
        elif isinstance(value, (int, float)) and value:
            knew[label] = str(value)

    add("allergens", hard.get("allergens"))
    add("dietary", hard.get("dietary"))
    add("medical", hard.get("medical"))
    if hard.get("budget_cap"):
        # ":g" so a whole-number cap reads "$120", not "$120.0". Coerced through
        # float() first and guarded: this value comes from an LLM, so "120" as a
        # STRING is a real shape, and formatting a str with :g raises — which would
        # kill the goal at the understanding gate to tidy a decimal point.
        try:
            knew["budget"] = f"${float(hard['budget_cap']):g}"
        except (TypeError, ValueError):
            knew["budget"] = f"${hard['budget_cap']}"
    # NOT str(dict) — that renders "{'start': '21:30', 'end': '07:00'}" into a
    # user-facing chip: a Python literal, quotes and braces included, on a fridge
    # door. These chips are the agent proving it listened, so they have to read
    # like a person wrote them.
    for label, key in (("quiet hours", "quiet_hours"), ("peak tariff", "peak_hours"), ("away", "away_window")):
        window = hard.get(key)
        if not window:
            continue
        if isinstance(window, dict) and (window.get("start") or window.get("end")):
            knew[label] = f"{window.get('start', '?')}–{window.get('end', '?')}"
        elif isinstance(window, str) and window.strip():
            knew[label] = window.strip()

    envelope = hard.get("budget_envelope")
    if isinstance(envelope, dict) and envelope.get("cap"):
        # The pool, not this goal's slice — the device narrows the goal's own cap to
        # whatever is left of it, so the chip says what the household has, not what
        # the goal may spend.
        period = str(envelope.get("period") or "").strip()
        try:
            knew["envelope"] = f"${float(envelope['cap']):g}" + (f" {period}" if period else "")
        except (TypeError, ValueError):
            knew["envelope"] = f"${envelope['cap']}"
    return knew


def _applied_constraints(applied: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Display-ready provenance rows: what was applied, and where it came from.

    Values are rendered here, not in the UI, for the same reason the ``knew`` chips
    are: a raw ``{'start': ..., 'end': ...}`` on a fridge door reads like a bug.
    """
    rows: list[dict[str, Any]] = []
    for entry in applied or []:
        rows.append(
            {
                "id": entry.get("id", ""),
                "label": entry.get("label", ""),
                "value": _constraint_display(entry.get("kind", ""), entry.get("value")),
                "enforcement": entry.get("enforcement", "soft"),
                "source": entry.get("source", "account"),
                "why": entry.get("why", ""),
            }
        )
    return rows


def _constraint_display(kind: str, value: Any) -> str:
    """One constraint value as a person would write it."""
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
        if start or end:
            return f"{start or '?'}–{end or '?'}"
        if value.get("cap") is not None:
            period = str(value.get("period") or "").strip()
            return f"${value['cap']:g}" + (f" {period}" if period else "")
        return ""
    if isinstance(value, list):
        return ", ".join(str(item).replace("_", " ") for item in value)
    if isinstance(value, bool) or value is None:
        return ""
    if kind == "budget_cap" and isinstance(value, (int, float)):
        return f"${value:g}"
    return str(value).replace("_", " ")


def _understanding_thought_fallback(intent: dict[str, Any], hard: dict[str, Any], domain: str) -> str:
    tw = intent.get("time_window") or {}
    # Counted off the resolved block rather than a fixed list of five keys, so a new
    # hard kind (peak_hours, away_window, …) is included the day it is added.
    constraint_count = sum(
        len(value) if isinstance(value, list) else (1 if value else 0) for value in hard.values()
    )
    label = domain.replace("_", " ")
    start = tw.get("start", "")
    end = tw.get("end", "")
    window = f"{start} to {end}" if start and end else start or end
    window_part = f" for {window}" if window else ""
    guard = ""
    if constraint_count:
        guard = f" while honoring {constraint_count} household constraint"
        if constraint_count != 1:
            guard += "s"
    return f"I'll shape a {label}{window_part}{guard}, then have your Family Hub build the plan."


def _understanding_thought(intent: dict[str, Any], hard: dict[str, Any], domain: str) -> str:
    """Tiny LLM one-liner for the understanding gate, with a deterministic fallback."""
    fallback = _understanding_thought_fallback(intent, hard, domain)
    settings = get_settings()
    max_tokens = settings.openrouter_max_tokens
    thought_tokens = min(max_tokens, 60) if isinstance(max_tokens, int) and max_tokens > 0 else 60
    try:
        llm = ChatOpenAI(
            model=settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            temperature=0.2,
            max_tokens=thought_tokens,
            timeout=15,
            max_retries=0,
        )
        response = llm.invoke(
            [
                (
                    "system",
                    "Write one short sentence describing how GoalFlow will approach the user's goal. "
                    "Keep it under 22 words. Do not mention internal systems or uncertainty.",
                ),
                (
                    "human",
                    "Objective: {objective}\nDomain: {domain}\nTime window: {time_window}\n"
                    "Hard constraints: {hard}".format(
                        objective=intent.get("objective", ""),
                        domain=domain,
                        time_window=intent.get("time_window") or {},
                        hard=hard,
                    ),
                ),
            ]
        )
        thought = " ".join(str(getattr(response, "content", "") or "").split())
        if len(thought) > 180:
            thought = thought[:177].rstrip() + "..."
        return thought if thought else fallback
    except Exception:
        logger.exception("understanding_thought_llm_failed")
        return fallback


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

    digest = capability_digest(state.get("device_capabilities"))
    if not digest:
        # No device, or one that advertised nothing. We have no basis for judging
        # what is actionable, so we do NOT guess: guessing here is how the gate got
        # hardcoded to two domains in the first place. Decline honestly instead.
        logger.info("graph_node_exit node=interpret_goal actionable=false reason=no_capabilities")
        return {
            "intent": {
                "actionable": False,
                "decline_reason": "no device is connected, so I don't know what I can do yet",
                "domain": "",
                "objective": goal_text,
                "time_window": {},
            },
            "task_status": "interpreting",
            "event_log": [_event(state, "intent_interpreted", {"actionable": False, "reason": "no_capabilities"})],
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
                    "Keep scope flexible and generic for the device planner.\n\n"
                    "The connected device advertises exactly these capabilities:\n"
                    f"{digest}\n\n"
                    "Set actionable=true if the goal can plausibly be ADVANCED using them, "
                    "and fill time_window. Judge the goal against the capability list, not "
                    "against any fixed list of topics. Whether an action is PERMITTED is not "
                    "your call — the device decides that and gives a better answer than you "
                    "could; your question is only whether this is the kind of thing this "
                    "product is for. If nothing it can do relates to the goal — general "
                    "questions, trivia, facts, chit-chat, unrelated tasks — set "
                    "actionable=false, put one short reason in decline_reason, and you may "
                    "leave time_window empty.\n\n"
                    "A BARE STATEMENT OF FACT IS NOT A GOAL. \"We've gone vegan\", \"Aarav is "
                    "allergic to peanuts\", \"we're away next week\" on their own tell you "
                    "something about the household; they do not ask for anything. Set "
                    "actionable=false with decline_reason \"a statement, not a goal\" — do NOT "
                    "invent the plan you imagine they meant. Something downstream remembers "
                    "the fact; inventing a week of dinners nobody asked for does not. A "
                    "statement that ALSO asks for something (\"plan our dinners — we've gone "
                    "vegan\") is still a goal.\n\n"
                    "ALWAYS respond by calling the structured "
                    "function — never answer the user's question directly in prose, even "
                    "for out-of-scope goals (call it with actionable=false instead).\n\n"
                    "DOMAIN: set `domain` to the advertised goal-shape id whose HINT best "
                    "matches the KIND of goal — read what the goal is ABOUT, not which id "
                    "is listed first. E.g. a birthday/party → the party shape; a trip or "
                    "being away from home → the vacation/away shape; hosting guests for a "
                    "dinner → the guest-dinner shape; planning the week's dinners → the "
                    "meal shape; cutting the electricity/power bill or shifting appliance "
                    "usage → the energy shape; keeping the kitchen stocked or spending less "
                    "on groceries → the grocery shape. Do NOT default to meal planning — use "
                    "the meal shape ONLY when the goal is genuinely about planning meals; a "
                    "goal about the grocery BILL is the grocery shape, not the meal shape. "
                    "The device ROUTES on this value, so a mismatched shape loses its "
                    "handling. Coin a new short slug only when the goal is a KIND none of "
                    "the advertised hints covers.",
                ),
                ("human", goal_text),
            ]
        )
        if intent is None:
            # The model replied in free text instead of returning structured intent —
            # it treated the input as a question/chit-chat, not an actionable goal.
            # Decline gracefully (redirect) rather than erroring.
            logger.info("graph_node_exit node=interpret_goal actionable=false reason=no_structured_intent")
            return {
                "intent": {
                    "actionable": False,
                    "decline_reason": "not an actionable meal or guest-dinner goal",
                    "domain": "",
                    "objective": goal_text,
                    "time_window": {},
                },
                "task_status": "interpreting",
                "event_log": [_event(state, "intent_interpreted", {"actionable": False})],
            }
        intent_dict = intent.model_dump(mode="json")
        # Out-of-scope goals need no time window and never reach the device — the
        # graph routes them to decline_out_of_scope. Only actionable goals must
        # carry a resolved planning window.
        if intent_dict.get("actionable") is False:
            logger.info(
                "graph_node_exit node=interpret_goal actionable=false reason=%s",
                intent_dict.get("decline_reason"),
            )
            return {
                "intent": intent_dict,
                "task_status": "interpreting",
                "event_log": [_event(state, "intent_interpreted", {"actionable": False})],
            }
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
    """Memory & Constraints: resolve the household constraint store FOR THIS GOAL.

    v6: the store is a library of sourced, scoped, expiring entries, and this node
    resolves it against the goal's domain — so a vacation goal carries a travel cap
    and an away window instead of the $120 weekly grocery cap that every goal used
    to inherit.

    The split that matters: ``resolve_constraints`` builds the HARD block from store
    data by code alone. The LLM's only say is ``_relevant_soft_ids`` — which SOFT
    preferences to send — and that call is allowed to fail, because tag matching
    inside the resolver is a complete fallback.
    """
    logger.info("graph_node_enter node=load_memory")
    profile = load_family_profile()
    domain = (state.get("intent") or {}).get("domain", "")
    today = date.today()

    soft_ids = _relevant_soft_ids(state, profile, domain, today)
    resolved = resolve_constraints(profile, domain, today=today, soft_ids=soft_ids)

    memory = {
        "family_id": profile.get("family_id"),
        "hard": resolved["hard"],
        "bias": {
            "members": list(profile.get("members", [])),
            "soft": resolved["soft"],
            "context": resolved["context"],
        },
        # Provenance: which entries were picked, from where, and why. Rides into the
        # understanding card and the logs — a block the user cannot trace is a block
        # they will not trust.
        "applied": resolved["applied"],
    }
    logger.info(
        "graph_node_exit node=load_memory family_id=%s domain=%s applied=%d picked=%s",
        memory.get("family_id"),
        domain,
        len(resolved["applied"]),
        "relevance" if soft_ids else "tagged",
    )
    return {
        "memory": memory,
        "task_status": "grounding",
        "event_log": [
            _event(
                state,
                "memory_loaded",
                {
                    "family_id": memory.get("family_id"),
                    "domain": domain,
                    "constraints": [entry["id"] for entry in resolved["applied"]],
                },
            )
        ],
    }


class _SoftSelection(BaseModel):
    """Structured LLM output for the soft-preference relevance pass."""

    ids: list[str] = Field(
        default_factory=list,
        description="Ids of the soft entries worth sending with THIS goal. Omit the rest.",
    )


def _relevant_soft_ids(
    state: GraphState,
    profile: dict[str, Any],
    domain: str,
    today: date,
) -> list[str] | None:
    """Ask a small LLM call which SOFT preferences this goal should carry.

    Why the model is allowed near this at all: ``applies_to`` tags only know the
    domains someone thought to write down, and the interpreter is free to COIN a
    domain slug for a goal nobody anticipated — that goal would fall back to the
    household-wide entries alone. Relevance covers what tagging cannot.

    Why it is allowed to fail: this is SOFT bias only. Returning None hands the
    resolver back to tag matching, which is a complete answer on its own. Nothing
    here touches the hard block.
    """
    candidates = soft_candidates(profile, today)
    if not candidates:
        return None

    intent = state.get("intent") or {}
    settings = get_settings()
    max_tokens = settings.openrouter_max_tokens
    # 1200, not the ~40 an id list actually needs: the default model is a REASONING
    # model, and its reasoning tokens are drawn from this same budget. At 200 the
    # thinking consumed the allowance and the function call never landed — the call
    # returned None, no exception, and every goal quietly fell back to tag matching.
    # A relevance pass that silently never runs is worse than not having one.
    selection_tokens = min(max_tokens, 1200) if isinstance(max_tokens, int) and max_tokens > 0 else 1200
    try:
        llm = ChatOpenAI(
            model=settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            temperature=0,
            max_tokens=selection_tokens,
            timeout=20,
            max_retries=0,
        )
        structured_llm = llm.with_structured_output(_SoftSelection, method="function_calling")
        selection = structured_llm.invoke(
            [
                (
                    "system",
                    "You pick which household PREFERENCES are worth sending with a goal. "
                    "These are soft biases, never safety rules — you are not deciding what is "
                    "allowed, only what is relevant.\n\n"
                    "Return the ids that would make a planner's output better for THIS goal, "
                    "and leave out the ones that would only add noise: a vacation checklist "
                    "does not need the family's dinner preferences, and a meal plan does not "
                    "need the departure routine. Household notes about who is busy when are "
                    "usually worth keeping. Prefer a few good ones over all of them; return "
                    "no ids at all rather than padding the list.",
                ),
                (
                    "human",
                    "Goal: {objective}\nDomain: {domain}\n\nCandidates (JSON):\n{candidates}".format(
                        objective=intent.get("objective") or state.get("goal_text", ""),
                        domain=domain or "(none)",
                        candidates=candidates,
                    ),
                ),
            ]
        )
        ids = [str(i) for i in (getattr(selection, "ids", None) or []) if str(i).strip()]
        known = {entry["id"] for entry in candidates}
        # Drop hallucinated ids rather than letting them silently select nothing —
        # an empty result after filtering falls back to tags, which is correct.
        picked = [i for i in ids if i in known]
        if len(picked) != len(ids):
            logger.warning("soft_relevance_unknown_ids dropped=%s", sorted(set(ids) - known))
        if not picked:
            # Not an error — tags are a complete answer — but say so out loud. This
            # is the only signal that the pass ran and chose nothing.
            logger.info("soft_relevance_empty domain=%s — using applies_to tags", domain)
        return picked or None
    except Exception:
        logger.exception("soft_relevance_llm_failed — falling back to applies_to tags")
        return None


class _ProposedConstraint(BaseModel):
    """One household rule the user just STATED, proposed for confirmation."""

    kind: str = Field(description="allergens | dietary | medical | budget_cap | dislikes | prefer | habits")
    value: Any = Field(
        description=(
            "A list of short avoid-slugs (['no_dairy', 'no_meat']), a number for a cap, "
            "or a short string. For a diet, list what must be AVOIDED — never the diet's "
            "name: 'vegan' names a philosophy, 'no_dairy' names a thing to keep out of a "
            "recipe, and only the second one can be checked."
        )
    )
    enforcement: str = Field(default="soft", description="'hard' only for allergens/dietary/medical/budget_cap.")
    scope: str = Field(
        default="household",
        description="'goal' if the rule is about THIS goal only (a spend limit for this party); else 'household'.",
    )
    applies_to: list[str] = Field(
        default_factory=lambda: ["*"],
        description="['*'] for a standing household rule; [this goal's domain] when scope is 'goal'.",
    )
    label: str = Field(default="", description="Two or three words a person would recognise: 'no dairy'.")
    quote: str = Field(default="", description="The user's own words this came from, verbatim and short.")
    expires_on: str = Field(default="", description="ISO date, ONLY if the user time-boxed it ('for two weeks').")


class _CaptureResult(BaseModel):
    constraints: list[_ProposedConstraint] = Field(default_factory=list)


def detect_constraints(state: GraphState) -> GraphState:
    """Spot household rules the user STATED, and propose them — never apply them.

    "We've gone vegan" is not a goal; it is a standing fact about the household, and
    a home assistant that makes you re-say it every week is not remembering anything.
    But a model must not write the policy it is then checked against (R2), so this
    node only ever PROPOSES: the confirmation happens at the gate, and
    ``memory.store.append_constraints`` is the single write path.

    It runs before the actionability router because a message can be pure statement
    ("we've gone vegan", no goal attached) — which the interpreter correctly judges
    un-actionable, and which must be captured rather than declined.
    """
    logger.info("graph_node_enter node=detect_constraints")
    goal_text = state.get("goal_text", "").strip()
    if not goal_text:
        return {"proposed_constraints": []}

    settings = get_settings()
    max_tokens = settings.openrouter_max_tokens
    # Same reasoning-token trap as the relevance pass: too small a budget and the
    # structured call never lands, silently.
    capture_tokens = min(max_tokens, 1200) if isinstance(max_tokens, int) and max_tokens > 0 else 1200
    today = date.today()
    # A limit stated WITH a goal usually belongs to that goal ("keep the party under
    # $150"), not to the household forever. Telling the model which goal it is looking
    # at is what lets it say so — and a household-wide $150 would be resolved away by
    # the more specific standing cap, which is a silent way to ignore the user.
    intent = state.get("intent") or {}
    # The interpreter has already judged this message. When it says "a statement, not
    # a goal", that is a strong prior that a household rule IS in there — and without
    # passing it on, the same sentence was found half the time and missed the other
    # half, leaving the user with an out-of-scope redirect for something they told us.
    statement_hint = (
        "\n\nThe goal interpreter judged this message to be a STATEMENT rather than a request, "
        "so it very probably states a household rule. Read it again before returning nothing."
        if intent.get("actionable") is False
        else ""
    )
    domain = intent.get("domain") or ""
    scope_hint = (
        f"This message also asks for a '{domain}' goal. A rule that is really about THAT goal — a "
        f"spend limit for this party, a rule for this trip — takes scope='goal' and applies_to=['{domain}']. "
        "A standing household rule takes scope='household' and applies_to=['*']."
        if domain
        else "There is no goal here, only a statement: use scope='household' and applies_to=['*']."
    )
    try:
        llm = ChatOpenAI(
            model=settings.openrouter_model,
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
            temperature=0,
            max_tokens=capture_tokens,
            timeout=20,
            max_retries=0,
        )
        structured_llm = llm.with_structured_output(_CaptureResult, method="function_calling")
        result = structured_llm.invoke(
            [
                (
                    "system",
                    "You spot HOUSEHOLD RULES a person states in passing, so a home assistant can "
                    "remember them instead of asking again next week.\n\n"
                    f"Real today is {today.isoformat()}.\n\n"
                    "Return a constraint ONLY for a standing fact or rule about the household: a diet "
                    "('we've gone vegan', 'no dairy for a month'), an allergy, a medical restriction, a "
                    "spending limit ('keep the party under $150'), a strong preference or routine.\n\n"
                    "Return NOTHING for an ordinary goal or request. 'Plan our dinners this week' states "
                    "no rule — it is just the job. Most messages produce no constraints, and that is the "
                    "correct answer; inventing one puts words in the family's mouth.\n\n"
                    "NEVER propose removing, relaxing or raising an existing rule. 'We can eat pork "
                    "again' and 'raise the budget to $900' are not constraints — return nothing for "
                    "them. You may only ever propose something MORE restrictive.\n\n"
                    "enforcement='hard' only for allergens, dietary, medical and budget_cap — things a "
                    "plan must be BLOCKED for violating. Preferences, dislikes and routines are 'soft'.\n"
                    "Set expires_on only when the user time-boxed it, resolved to an ISO date.\n"
                    "quote must be the user's own words, short and verbatim.\n\n"
                    f"{scope_hint}{statement_hint}",
                ),
                ("human", goal_text),
            ]
        )
        proposed = [c.model_dump(mode="json") for c in (getattr(result, "constraints", None) or [])]
        proposed = [c for c in proposed if _capture_is_sane(c)]
        for index, entry in enumerate(proposed, start=1):
            # A proposal id is only needed to answer with ("I accept #2"); the STORED
            # id is minted at write time, so these never reach the file.
            entry["id"] = f"proposed-{index}"
            entry["label"] = entry.get("label") or str(entry.get("kind", "")).replace("_", " ")
        logger.info("graph_node_exit node=detect_constraints proposed=%d", len(proposed))
        return {
            "proposed_constraints": proposed,
            "event_log": [_event(state, "constraints_proposed", {"count": len(proposed)})],
        }
    except Exception:
        # Capture is a convenience, not a gate. Losing it costs the user a repeat;
        # failing the goal over it would cost them the goal.
        logger.exception("constraint_capture_failed — continuing without capture")
        return {"proposed_constraints": []}


def _capture_is_sane(entry: dict[str, Any]) -> bool:
    """Drop or repair proposals the prompt asked for but the model may still get wrong."""
    kind = _KIND_SYNONYMS.get(str(entry.get("kind") or "").strip().lower())
    if not kind or entry.get("value") in (None, "", [], {}):
        # An unrecognised kind is NOT stored as-is. It would resolve to nothing —
        # neither unioned into the hard block nor picked as bias — so the user would
        # be told their rule was remembered while it silently did nothing at all.
        if entry.get("kind"):
            logger.warning("constraint_capture_unknown_kind kind=%s — dropped", entry.get("kind"))
        return False
    entry["kind"] = kind
    if entry.get("enforcement") == "hard" and kind not in _CAPTURABLE_HARD_KINDS:
        # A "hard" anything-else would ride into constraints.hard where no device rule
        # enforces it — a constraint that reads as a guarantee and blocks nothing.
        logger.warning("constraint_capture_downgraded kind=%s — no rule enforces it", kind)
        entry["enforcement"] = "soft"
    return True


#: The hard kinds a person can state in chat AND the device actually enforces.
#: quiet_hours / peak_hours / away_window are windows the account or the calendar
#: owns; capturing one from a sentence would be guessing at a schedule.
_CAPTURABLE_HARD_KINDS = {"allergens", "dietary", "medical", "budget_cap"}

#: Model wording → the store's kind. The model reaches for "diet" and "allergy" as
#: readily as the canonical names, and a kind the resolver does not recognise is a
#: constraint that resolves to nothing — so map what is obviously the same thing and
#: drop the rest rather than storing a rule that cannot act.
_KIND_SYNONYMS = {
    kind: kind
    for kind in ("allergens", "dietary", "medical", "budget_cap", "dislikes", "prefer", "habits", "context")
} | {
    "allergy": "allergens",
    "allergies": "allergens",
    "allergen": "allergens",
    "diet": "dietary",
    "diets": "dietary",
    "dietary_restriction": "dietary",
    "dietary_restrictions": "dietary",
    "medical_condition": "medical",
    "health": "medical",
    "budget": "budget_cap",
    "budget_limit": "budget_cap",
    "spend_cap": "budget_cap",
    "cap": "budget_cap",
    "dislike": "dislikes",
    "preference": "prefer",
    "preferences": "prefer",
    "prefers": "prefer",
    "habit": "habits",
    "routine": "habits",
    "routines": "habits",
}


def route_after_interpret_or_capture(state: GraphState) -> str:
    """Actionable goals plan; un-actionable ones with a captured rule still land somewhere.

    Without this branch "we've gone vegan" gets the out-of-scope redirect — technically
    correct (it is not a goal) and exactly the wrong answer, because the user just told
    the assistant something it should remember.
    """
    if state.get("error"):
        return "explain_block"
    if (state.get("intent") or {}).get("actionable") is False:
        return "capture_gate" if state.get("proposed_constraints") else "decline_out_of_scope"
    return "load_memory"


def capture_gate(state: GraphState) -> GraphState:
    """The user stated a rule and no goal: confirm it, then remember it.

    Rides the EXISTING understanding gate wire rather than minting a frame kind —
    that card is already "here is what I understood, yes or no?", and this is the
    same question about a smaller thing. ``capture_only`` tells the UI there is no
    plan coming.
    """
    logger.info("graph_node_enter node=capture_gate")
    proposed = state.get("proposed_constraints") or []
    understanding = {
        "objective": state.get("goal_text", ""),
        "title": "",
        "domain": "",
        "time_window": {},
        "hard": {},
        "knew": {},
        "constraints": [],
        "capture_only": True,
        "proposed_constraints": proposed,
        "thought": _capture_thought(proposed),
    }
    incoming = interrupt(
        {
            "kind": "understanding_confirmation",
            "goal_id": state.get("goal_id"),
            "understanding": understanding,
        }
    )
    if isinstance(incoming, dict) and "payload" in incoming:
        incoming = incoming["payload"]
    accepted = _accepted_constraints(incoming, proposed)
    written = append_constraints(accepted) if accepted else []

    message = (
        "Noted — I'll remember that: " + ", ".join(entry.get("label", "") for entry in written) + "."
        if written
        else "Nothing saved."
    )
    logger.info("graph_node_exit node=capture_gate written=%d", len(written))
    return {
        "understanding": understanding,
        "task_status": "done",
        "explanation": {"type": "captured", "message": message},
        "event_log": [_event(state, "constraints_captured", {"ids": [e["id"] for e in written]})],
    }


def _capture_thought(proposed: list[dict[str, Any]]) -> str:
    labels = [str(entry.get("label") or entry.get("kind", "")) for entry in proposed]
    if not labels:
        return "I didn't catch a household rule in that."
    joined = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" and {labels[-1]}"
    return f"That sounds like a standing rule — {joined}. Want me to remember it for every goal?"


def _accepted_constraints(
    incoming: Any,
    proposed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The proposals the user actually said yes to.

    Silence is a NO. An answer that confirms the goal but names no constraint ids
    accepts no constraints — a household rule needs its own yes, not one inherited
    from clicking Confirm on something else.
    """
    if not isinstance(incoming, dict) or not incoming.get("confirmed"):
        return []
    accepted_ids = incoming.get("accepted_constraint_ids")
    if not isinstance(accepted_ids, list):
        return []
    wanted = {str(i) for i in accepted_ids}
    return [
        {k: v for k, v in entry.items() if k != "id" and v not in ("", None)}
        for entry in proposed
        if entry.get("id") in wanted
    ]


def present_understanding(state: GraphState) -> GraphState:
    """Confirm-understanding gate: pause before contract build and dispatch."""
    logger.info("graph_node_enter node=present_understanding")
    intent = state["intent"]
    memory = state["memory"]
    hard = memory.get("hard") or {}
    domain = intent["domain"]
    understanding = {
        "objective": intent["objective"],
        # A short card headline for the board. Additive on the dispatch: the device
        # ignores it, but the hub caches the contract and the board reads it from
        # there, so it rides along rather than needing its own channel.
        "title": intent.get("title") or "",
        "domain": domain,
        "time_window": intent.get("time_window") or {},
        "hard": hard,
        "knew": _hard_knew(hard),
        # v6 provenance, additive: WHERE each constraint came from and why it was
        # picked for this goal. `knew` stays exactly as it was, so no UI has to
        # change to ship this — the chips keep working and this rides alongside.
        "constraints": _applied_constraints(memory.get("applied")),
        # v6-M4, additive: rules the user stated in the same breath as the goal
        # ("plan our dinners — we've gone vegan"). Proposed here, applied only if
        # they come back accepted.
        "proposed_constraints": state.get("proposed_constraints") or [],
        "thought": _understanding_thought(intent, hard, domain),
    }
    incoming = interrupt(
        {
            "kind": "understanding_confirmation",
            "goal_id": state.get("goal_id"),
            "understanding": understanding,
        }
    )
    if isinstance(incoming, dict) and "payload" in incoming:
        incoming = incoming["payload"]
    confirmed = bool(isinstance(incoming, dict) and incoming.get("confirmed"))

    # A rule accepted here binds THIS goal, not just the next one. The user said it
    # while asking for this plan; honouring it a week later would be a strange kind
    # of remembering. Persist, then re-resolve so the dispatch carries it.
    accepted = _accepted_constraints(incoming, understanding["proposed_constraints"])
    if accepted:
        # A goal-scoped rule MUST expire. "Keep the party under $150" is true of this
        # party; left permanent it would quietly cap every birthday from now on, and
        # nobody would remember why. The goal's own horizon is the natural end date.
        window_end = (intent.get("time_window") or {}).get("end")
        for entry in accepted:
            if entry.get("scope") == "goal" and not entry.get("expires_on") and window_end:
                entry["expires_on"] = window_end
        written = append_constraints(accepted)
        if written:
            today = date.today()
            resolved = resolve_constraints(load_family_profile(), domain, today=today)
            memory = {
                **memory,
                "hard": resolved["hard"],
                "bias": {**memory.get("bias", {}), "soft": resolved["soft"], "context": resolved["context"]},
                "applied": resolved["applied"],
            }
            understanding["hard"] = resolved["hard"]
            understanding["knew"] = _hard_knew(resolved["hard"])
            understanding["constraints"] = _applied_constraints(resolved["applied"])
            logger.info("captured_constraints_applied goal=%s ids=%s",
                        state.get("goal_id"), [entry["id"] for entry in written])
    logger.info("graph_node_exit node=present_understanding confirmed=%s", confirmed)
    return {
        "understanding": understanding,
        "understanding_confirmed": confirmed,
        # Re-resolved when a rule was captured above — build_contract reads this, so
        # returning the local would be the difference between "we've gone vegan"
        # binding this goal and being silently deferred to the next one.
        "memory": memory,
        "task_status": "grounding",
        "event_log": [
            _event(
                state,
                "understanding_confirmed" if confirmed else "understanding_declined",
                {},
            )
        ],
    }


def build_contract(state: GraphState) -> GraphState:
    """Assemble + validate the generic Task Contract (models.contract.Dispatch).

    Merges the LLM's intent with the constraints ``load_memory`` already RESOLVED
    for this goal's domain: ``constraints.hard`` is copied straight out of
    ``state["memory"]["hard"]`` — no edit, no paraphrase, nothing derived from the
    model — while ``soft``/``context`` carry the picked bias. ``autonomy`` is
    "tiered"; goal_id + correlation_id are minted here; ``Dispatch(**frame)``
    validates before the frame leaves the cloud.
    """
    logger.info("graph_node_enter node=build_contract")
    intent = state["intent"]
    memory = state["memory"]
    goal_id = state.get("goal_id") or str(uuid4())
    correlation_id = state.get("correlation_id") or str(uuid4())
    domain = intent["domain"]
    today = date.today()
    # Monitoring/prep begins NOW, so the window START is real today for EVERY goal — NOT
    # the LLM's start, which for an event goal ("next Sunday") is the EVENT date and would
    # peg progress at 0% until then. The LLM's end is the goal's horizon (the card's ETA).
    # The board's day-by-day progress is anchored to the PLAN's own day span from this
    # start (BoardService.on_plan_ready).
    start = today.isoformat()
    end = (intent.get("time_window") or {}).get("end")
    if not end or end <= start:
        end = (today + timedelta(days=6)).isoformat()
    time_window = {"start": start, "end": end}

    context = {
        "family_id": memory.get("family_id"),
        **memory.get("bias", {}),
    }
    frame = {
        "type": "dispatch",
        "goal_id": goal_id,
        "correlation_id": correlation_id,
        "domain": domain,
        "objective": intent["objective"],
        # A short card headline for the board. Additive on the dispatch: the device
        # ignores it, but the hub caches the contract and the board reads it from
        # there, so it rides along rather than needing its own channel.
        "title": intent.get("title") or "",
        "success_criteria": intent.get("success_criteria", []),
        "constraints": {
            "hard": memory.get("hard", {}),
            "soft": memory.get("bias", {}).get("soft", {}),
        },
        "scope": intent.get("scope", {}),
        "time_window": time_window,
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


def goal_declined(state: GraphState) -> GraphState:
    """User declined the understanding before planning; end without dispatch."""
    logger.info("graph_node_enter node=goal_declined")
    return {
        "task_status": "done",
        "explanation": {"type": "declined", "message": "Goal cancelled before planning."},
        "event_log": [_event(state, "goal_declined", {})],
    }


def decline_out_of_scope(state: GraphState) -> GraphState:
    """Interpreter judged the goal out of scope; end without any device dispatch.

    Nothing the connected device can do relates to the goal, so it is politely
    declined and redirected — the device is never touched.

    The redirect names what this device ACTUALLY offers (v3-M4). It used to promise
    "the week's meals or help you host a dinner" from a string literal here, which
    would have become a lie the moment the device grew a capability — the assistant
    telling the user it can't do something it just learned to do.
    """
    logger.info("graph_node_enter node=decline_out_of_scope")
    intent = state.get("intent") or {}
    reason = intent.get("decline_reason") or ""
    can_do = _capability_summary(state.get("device_capabilities"))
    message = (
        f"That's outside what I do. I'm your home goal assistant — I can help with {can_do}. "
        "Try one of those and I'll get going."
        if can_do
        else "That's outside what I do, and right now I can't see a device to check what I can help with."
    )
    explanation = {"type": "out_of_scope", "message": message, "reason": reason}
    return {
        "task_status": "done",
        "explanation": explanation,
        "event_log": [_event(state, "out_of_scope_declined", {"reason": reason})],
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
    """error -> explain_block (LLM-only, fail loudly); out-of-scope -> decline;
    else -> load_memory."""
    if state.get("error"):
        return "explain_block"
    if (state.get("intent") or {}).get("actionable") is False:
        return "decline_out_of_scope"
    return "load_memory"


def route_after_understanding(state: GraphState) -> str:
    """confirmed -> build_contract; declined -> graceful terminal node."""
    return "build_contract" if state.get("understanding_confirmed") else "goal_declined"


def precheck_wait(state: GraphState) -> GraphState:
    """A precheck-blocked plan HOLDS; it does not complete.

    A safety block is "never" (→ explain_block, done). A precheck block is "not yet" —
    the world isn't ready (signed out, appliance offline), and the plan should run when
    it recovers. The empty plan must therefore NOT flow to relay_decisions: that sends
    an empty approval, the device answers status(done), and the goal falsely reads
    Completed. Ending here instead leaves the board on the Waiting state on_plan_ready
    set, with the precheck reason. (Auto-retry on recovery is future work; for now it
    honestly waits rather than lying that it finished.)
    """
    logger.info("graph_node_enter node=precheck_wait")
    precheck = state.get("plan", {}).get("precheck") or {}
    return {
        "task_status": "monitoring",
        "event_log": [_event(state, "precheck_wait", {"reason": precheck.get("reason")})],
    }


def route_on_safety(state: GraphState) -> str:
    """After collect_plan: safety-blocked -> explain_block; precheck-blocked ->
    precheck_wait (holds, doesn't complete); approvals pending -> hitl_approval;
    auto-tier only -> relay_decisions."""
    plan = state.get("plan", {})
    if plan.get("safety", {}).get("gate") == "blocked":
        return "explain_block"
    if (plan.get("precheck") or {}).get("ok") is False:
        return "precheck_wait"
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


def default_checkpointer(db_path: str | None = None) -> Any:
    """A SQLite-backed checkpointer at ``data/goalflow.db`` (v3-M5).

    WHY NOT MemorySaver: the interrupts ARE the product. A goal sits at its
    approval gate waiting for a person, and people take hours — a board goal spans
    days. MemorySaver spans one process, so every restart silently dropped every
    paused goal: the card stays on the board and nothing can ever resume it, which
    is worse than losing it visibly.

    ``check_same_thread=False`` because LangGraph is synchronous and every call
    hops to a worker thread via ``asyncio.to_thread`` — so the connection is used
    from several threads. That is safe HERE because the hub holds a per-goal lock
    around every invoke (server.goal_lock), and a thread_id is a goal_id: two
    threads never touch the same checkpoint at once.

    One file, stdlib sqlite3 under the hood. No Redis, no Postgres, no ORM.
    """
    settings = get_settings()
    path = db_path or getattr(settings, "checkpoint_db", None) or "data/goalflow.db"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    logger.info("checkpointer=sqlite path=%s", path)
    return SqliteSaver(connection)


def build_graph(checkpointer: Any | None = None) -> Any:
    """Assemble and compile the v2 StateGraph.

    The checkpointer (default: in-process MemorySaver) makes the interrupt()
    pause durable; every invoke/resume must pass
    ``config={"configurable": {"thread_id": goal_id}}``.
    """
    graph = StateGraph(GraphState)

    graph.add_node("interpret_goal", interpret_goal)
    graph.add_node("detect_constraints", detect_constraints)
    graph.add_node("capture_gate", capture_gate)
    graph.add_node("load_memory", load_memory)
    graph.add_node("present_understanding", present_understanding)
    graph.add_node("build_contract", build_contract)
    graph.add_node("dispatch_to_device", dispatch_to_device)
    graph.add_node("collect_plan", collect_plan)
    graph.add_node("hitl_approval", hitl_approval)
    graph.add_node("relay_decisions", relay_decisions)
    graph.add_node("goal_declined", goal_declined)
    graph.add_node("decline_out_of_scope", decline_out_of_scope)
    graph.add_node("monitor", monitor)
    graph.add_node("explain_block", explain_block)
    graph.add_node("precheck_wait", precheck_wait)
    graph.add_node("finalize", finalize)

    graph.set_entry_point("interpret_goal")
    # Capture runs between interpreting and routing: a message can be a pure
    # STATEMENT ("we've gone vegan"), which is correctly un-actionable and must
    # still be remembered rather than redirected.
    graph.add_edge("interpret_goal", "detect_constraints")
    graph.add_conditional_edges(
        "detect_constraints",
        route_after_interpret_or_capture,
        {
            "load_memory": "load_memory",
            "capture_gate": "capture_gate",
            "decline_out_of_scope": "decline_out_of_scope",
            "explain_block": "explain_block",
        },
    )
    graph.add_edge("capture_gate", END)
    graph.add_edge("load_memory", "present_understanding")
    graph.add_conditional_edges(
        "present_understanding",
        route_after_understanding,
        {"build_contract": "build_contract", "goal_declined": "goal_declined"},
    )
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
            "precheck_wait": "precheck_wait",
        },
    )
    graph.add_edge("precheck_wait", END)
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
    graph.add_edge("goal_declined", END)
    graph.add_edge("decline_out_of_scope", END)
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer or default_checkpointer())


# ---------------------------------------------------------------------------
# Hub-facing entry points (called by server.py)
# ---------------------------------------------------------------------------


def _interrupt_value(interrupt_obj: Any) -> Any:
    if hasattr(interrupt_obj, "value"):
        return interrupt_obj.value
    if isinstance(interrupt_obj, dict):
        return interrupt_obj.get("value", interrupt_obj)
    return interrupt_obj


def _with_interrupt(result: Any, state_snapshot: Any) -> dict[str, Any]:
    """Return checkpointed state plus the pending interrupt payload, if any."""
    state = dict(getattr(state_snapshot, "values", state_snapshot) or {})
    interrupts = []
    if isinstance(result, dict):
        interrupts = list(result.get("__interrupt__") or [])
    if not interrupts:
        for task in getattr(state_snapshot, "tasks", ()) or ():
            task_interrupts = getattr(task, "interrupts", None) or ()
            interrupts.extend(task_interrupts)
    state["_interrupt"] = _interrupt_value(interrupts[0]) if interrupts else None
    return state


def start_goal(
    graph: Any,
    goal_text: str,
    goal_id: str,
    device_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Kick off a goal run and return checkpointed state plus any interrupt.

    ``device_capabilities`` is the connected device's ``capabilities`` frame. The
    interpreter judges actionability against it, so what the assistant can do
    follows the hardware that is plugged in rather than a list in this file.
    """
    config = {"configurable": {"thread_id": goal_id}}
    result = graph.invoke(
        {
            "goal_text": goal_text,
            "goal_id": goal_id,
            "task_status": "created",
            "event_log": [],
            "device_capabilities": device_capabilities or {},
        },
        config=config,
    )
    return _with_interrupt(result, graph.get_state(config))


def resume_goal(graph: Any, goal_id: str, resume_value: Any) -> dict[str, Any]:
    """Resume a paused run (plan_ready arrival, approval, adapt decision).

    Resume with Command(resume=resume_value), preserving the thread_id.
    """
    config = {"configurable": {"thread_id": goal_id}}
    result = graph.invoke(Command(resume=resume_value), config=config)
    return _with_interrupt(result, graph.get_state(config))
