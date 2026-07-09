"""LangGraph nodes for M2 goal decomposition.

Pipeline: user_goal -> ambiguity -> memory -> decompose -> relay -> dispatch.

Key invariant: hard constraints flow through the MEMORY node as data, straight
into contract.constraints.hard. The LLM never generates, edits, or paraphrases
allergens/dietary/medical constraints. "LLM plans, code checks."
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from goalflow_cloud.config import get_settings
from goalflow_cloud.memory.store import load_family_profile
from goalflow_cloud.models.contract import (
    Dispatch,
    DispatchConstraints,
    DispatchScope,
    HardConstraints,
    SoftConstraints,
    TimeWindow,
)

logger = logging.getLogger(__name__)


class GraphState(TypedDict, total=False):
    user_goal_text: str
    normalized_intent: dict[str, Any]
    family_profile: dict[str, Any]
    hard_constraints: dict[str, list[str]]
    soft_preferences: dict[str, Any]
    contract: Dispatch
    dispatch_frame: dict[str, Any]
    fallback_used: bool
    fallback_reason: str


WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]
CORRELATION_ID = "disp-001"


def _dump_model(model: object) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)  # type: ignore[attr-defined]
    return model.dict(exclude_none=True)  # type: ignore[attr-defined]


def _next_weekday_dinner_window(today: date | None = None) -> dict[str, str]:
    """Return the next complete Mon-Fri planning window.

    For the demo goal "this week", a Thursday request plans the next full
    weekday dinner block rather than a partial week.
    """
    anchor = today or date.today()
    days_until_monday = (7 - anchor.weekday()) % 7
    start = anchor + timedelta(days=days_until_monday)
    end = start + timedelta(days=4)
    return {"start": start.isoformat(), "end": end.isoformat()}


def _goal_id_from_time_window(time_window: dict[str, str]) -> str:
    start = date.fromisoformat(time_window["start"])
    iso_year, iso_week, _ = start.isocalendar()
    return f"meal-{iso_year}-w{iso_week:02d}"


def _normalize_hard_constraints(hard: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "allergens": list(hard.get("allergens", [])),
        "dietary": list(hard.get("dietary", [])),
        "medical": list(hard.get("medical", [])),
    }


def _normalize_soft_constraints(soft: dict[str, Any]) -> dict[str, list[str]]:
    return {
        "dislikes": list(soft.get("dislikes", [])),
        "prefer": list(soft.get("prefer", [])),
    }


def _build_dispatch(
    *,
    llm_fields: dict[str, Any],
    normalized_intent: dict[str, Any],
    hard_constraints: dict[str, list[str]],
    soft_preferences: dict[str, Any],
) -> Dispatch:
    time_window = llm_fields.get("time_window")
    if not isinstance(time_window, dict):
        time_window = normalized_intent["time_window"]

    start = str(time_window.get("start") or normalized_intent["time_window"]["start"])
    end = str(time_window.get("end") or normalized_intent["time_window"]["end"])
    normalized_window = {"start": start, "end": end}
    goal_id = _goal_id_from_time_window(normalized_window)

    scope = llm_fields.get("scope") if isinstance(llm_fields.get("scope"), dict) else {}
    days = scope.get("days") if isinstance(scope.get("days"), list) else normalized_intent["days"]
    meal = scope.get("meal") or normalized_intent["meal"]

    optimization = llm_fields.get("optimization")
    if not isinstance(optimization, list) or not optimization:
        optimization = ["reduce_processed", "reduce_waste"]

    context_hints = llm_fields.get("context_hints")
    if isinstance(context_hints, dict):
        notes = str(context_hints.get("notes") or "")
    else:
        notes = str(context_hints or "")
    if not notes:
        notes = "; ".join(soft_preferences.get("context", [])) or "son has sports Wednesday"

    return Dispatch(
        goal_id=goal_id,
        objective=str(llm_fields.get("objective") or "healthier family dinners, less food waste"),
        scope=DispatchScope(meal=str(meal), days=[str(day) for day in days]),
        time_window=TimeWindow(start=start, end=end),
        constraints=DispatchConstraints(
            # HARD MEMORY CHANNEL: deterministic copy from family_profile["hard"].
            # Do not source these fields from LLM output.
            hard=HardConstraints(**hard_constraints),
            # SOFT MEMORY CHANNEL: planning preferences only; not safety enforced.
            soft=SoftConstraints(**_normalize_soft_constraints(soft_preferences.get("soft", {}))),
        ),
        optimization=[str(item) for item in optimization],
        # AUTONOMY is a control-vocabulary field, not an LLM decision. For the POC
        # every side-effect is proposed for approval, so it is deterministically
        # "propose_all" (ignore any LLM-suggested value).
        autonomy="propose_all",
        context_hints={"notes": notes},
        reply_to=f"kb/device/{goal_id}",
    )


def _scripted_dispatch(state: GraphState, reason: str) -> Dispatch:
    logger.warning("using scripted dispatch fallback: %s", reason)
    normalized_intent = state["normalized_intent"]
    return _build_dispatch(
        llm_fields={
            "objective": "healthier family dinners, less food waste",
            "scope": {"meal": "dinner", "days": WEEKDAY_NAMES},
            "time_window": normalized_intent["time_window"],
            "optimization": ["reduce_processed", "reduce_waste"],
            "autonomy": "propose_all",
            "context_hints": {
                "notes": "; ".join(state.get("soft_preferences", {}).get("context", []))
                or "son Aarav has football practice Wednesday 18:00"
            },
        },
        normalized_intent=normalized_intent,
        hard_constraints=state["hard_constraints"],
        soft_preferences=state["soft_preferences"],
    )


def _extract_json_object(content: str) -> dict[str, Any]:
    if not content or not content.strip():
        raise ValueError("empty LLM content")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if match is None:
            raise
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not a JSON object")
    return parsed


def _call_llm_for_contract_fields(state: GraphState) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=settings.openrouter_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        temperature=0,
        max_tokens=1600,
        timeout=20,
        model_kwargs={"response_format": {"type": "json_object"}},
    )

    normalized = state["normalized_intent"]
    soft_preferences = state["soft_preferences"]
    prompt = {
        "role": "user",
        "content": (
            "Return STRICT JSON only. Build the non-safety fields for a GoalFlow dispatch "
            "contract from this family goal. Do not include constraints.hard and do not invent "
            "allergens, dietary rules, or medical constraints. Reflect soft preferences in the "
            "objective, optimization, and context notes.\n\n"
            "Required JSON keys: objective, scope, time_window, optimization, autonomy, "
            "context_hints.\n"
            "scope must be {\"meal\": string, \"days\": [\"Mon\", ...]}.\n"
            "time_window must be {\"start\": ISO_DATE, \"end\": ISO_DATE}.\n"
            "context_hints must be {\"notes\": string}.\n\n"
            f"User goal: {state['user_goal_text']}\n"
            f"Normalized intent defaults: {json.dumps(normalized, sort_keys=True)}\n"
            f"Soft preferences and context: {json.dumps(soft_preferences, sort_keys=True)}\n"
        ),
    }
    response = llm.invoke([prompt])
    return _extract_json_object(str(response.content))


def ambiguity_node(state: GraphState) -> GraphState:
    """Normalize the user's fuzzy goal into demo-sensible defaults."""
    text = state["user_goal_text"].strip()
    time_window = _next_weekday_dinner_window()
    return {
        **state,
        "normalized_intent": {
            "raw_goal": text,
            "meal": "dinner",
            "days": WEEKDAY_NAMES,
            "time_window": time_window,
            "assumptions": ["weekday dinners", "next complete Mon-Fri planning window"],
        },
    }


def memory_node(state: GraphState) -> GraphState:
    """Load the family profile and split it into the two constraint channels.

    M2: via memory.store.load_family_profile():
      - profile["hard"]  -> state["hard_constraints"]  (injected VERBATIM into
        the contract's constraints.hard — a data path, never LLM semantics;
        the device safety gate reads exactly this block).
      - profile["soft"] + members + context -> state["soft_preferences"]
        (bias planning only, as prompt context for decompose).
    """
    profile = load_family_profile()
    hard_constraints = _normalize_hard_constraints(profile.get("hard", {}))
    soft_preferences = {
        "members": profile.get("members", []),
        "soft": profile.get("soft", {}),
        "context": profile.get("context", []),
    }
    return {
        **state,
        "family_profile": profile,
        # HARD MEMORY CHANNEL: copied verbatim into Dispatch.constraints.hard.
        "hard_constraints": hard_constraints,
        # SOFT MEMORY CHANNEL: prompt/context only; never safety-gated.
        "soft_preferences": soft_preferences,
    }


def decompose_node(state: GraphState) -> GraphState:
    """LLM call that fills the rest of the Task Contract.

    M2: produce objective, scope, time_window, optimization, autonomy and
    context_hints from the goal text + soft preferences. Merge with the
    memory-injected hard constraints into a models.contract.Dispatch.
    Falls back to a scripted contract when no OPENROUTER_API_KEY is set.
    """
    try:
        llm_fields = _call_llm_for_contract_fields(state)
        dispatch = _build_dispatch(
            llm_fields=llm_fields,
            normalized_intent=state["normalized_intent"],
            hard_constraints=state["hard_constraints"],
            soft_preferences=state["soft_preferences"],
        )
        return {**state, "contract": dispatch, "fallback_used": False}
    except Exception as exc:
        dispatch = _scripted_dispatch(state, str(exc))
        return {
            **state,
            "contract": dispatch,
            "fallback_used": True,
            "fallback_reason": str(exc),
        }


def relay_node(state: GraphState) -> GraphState:
    """Validate the assembled contract and hand it to the WS hub.

    M2: validate against models.contract.Dispatch, then prepare the outbound
    wire frame. The server owns actual WebSocket IO.
    """
    dispatch = Dispatch(**_dump_model(state["contract"]))
    frame = _dump_model(dispatch)
    frame["correlation_id"] = CORRELATION_ID
    return {**state, "contract": dispatch, "dispatch_frame": frame}


def build_graph() -> Any:
    """Assemble and compile the LangGraph StateGraph.

    M2: StateGraph(GraphState); edges ambiguity -> memory -> decompose -> relay.
    """
    graph = StateGraph(GraphState)
    graph.add_node("ambiguity", ambiguity_node)
    graph.add_node("memory", memory_node)
    graph.add_node("decompose", decompose_node)
    graph.add_node("relay", relay_node)

    graph.set_entry_point("ambiguity")
    graph.add_edge("ambiguity", "memory")
    graph.add_edge("memory", "decompose")
    graph.add_edge("decompose", "relay")
    graph.add_edge("relay", END)
    return graph.compile()


def build_dispatch_frame(user_goal_text: str) -> dict[str, Any]:
    """Run the graph and return the dispatch wire frame for the WS hub."""
    graph = build_graph()
    result = graph.invoke({"user_goal_text": user_goal_text})
    return result["dispatch_frame"]
