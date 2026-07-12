"""v2 LangGraph pipeline (design skeletons).

StateGraph: interpret_goal -> load_memory -> present_understanding
(interrupt()) -> build_contract -> dispatch_to_device -> [device plans] ->
collect_plan -> hitl_approval (interrupt()) -> relay_decisions -> monitor
(adapt loop) -> finalize.
Conditional edges route safety-blocked plans (and LLM errors — LLM-only,
no fallback) to explain_block. Compiled with a checkpointer
(thread_id = goal_id) so the approval pause is durable.
See graph/nodes.py and docs/ARCHITECTURE.md.
"""
