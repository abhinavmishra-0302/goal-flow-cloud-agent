"""LangGraph nodes for goal decomposition.

*** M2 SCOPE — DESIGN STUBS ONLY. Not wired in M1. ***

Pipeline:  user_goal -> [ambiguity] -> [memory] -> [decompose] -> [relay] -> dispatch

State (design intent): a TypedDict/BaseModel carrying at least
  user_goal_text, clarification_needed / question, family_profile,
  hard_constraints, soft_preferences, contract (models.contract.Dispatch).

LLM access (decompose node): OpenRouter via langchain-openai
(ChatOpenAI(base_url=settings.openrouter_base_url, model=settings.openrouter_model)),
default model "anthropic/claude-sonnet-5" — configurable. A scripted/mock
fallback sits behind the same interface so the graph runs offline.

Key invariant: hard constraints flow through the MEMORY node as data, straight
into contract.constraints.hard — the LLM never generates them. "LLM plans,
code checks."
"""

from __future__ import annotations

from typing import Any

# TODO(M2): replace `dict[str, Any]` with a proper GraphState TypedDict.
GraphState = dict[str, Any]


def ambiguity_node(state: GraphState) -> GraphState:
    """Decide whether the user goal is actionable.

    M2: if the goal is too vague, set a clarifying question on the state and
    short-circuit back to the UI instead of proceeding to decompose.
    """
    raise NotImplementedError("M2")


def memory_node(state: GraphState) -> GraphState:
    """Load the family profile and split it into the two constraint channels.

    M2: via memory.store.load_family_profile():
      - profile["hard"]  -> state["hard_constraints"]  (injected VERBATIM into
        the contract's constraints.hard — a data path, never LLM semantics;
        the device safety gate reads exactly this block).
      - profile["soft"] + members + context -> state["soft_preferences"]
        (bias planning only, as prompt context for decompose).
    """
    raise NotImplementedError("M2")


def decompose_node(state: GraphState) -> GraphState:
    """LLM call that fills the rest of the Task Contract.

    M2: produce objective, scope, time_window, optimization, autonomy and
    context_hints from the goal text + soft preferences. Merge with the
    memory-injected hard constraints into a models.contract.Dispatch.
    Falls back to a scripted contract when no OPENROUTER_API_KEY is set.
    """
    raise NotImplementedError("M2")


def relay_node(state: GraphState) -> GraphState:
    """Validate the assembled contract and hand it to the WS hub.

    M2: validate against models.contract.Dispatch, then
    registry.send_to("device", contract).
    """
    raise NotImplementedError("M2")


def build_graph() -> Any:
    """Assemble and compile the LangGraph StateGraph.

    M2: StateGraph(GraphState); add the four nodes; edges
    ambiguity -> memory -> decompose -> relay, with a conditional edge from
    ambiguity back to the UI when clarification is needed.
    """
    raise NotImplementedError("M2")
