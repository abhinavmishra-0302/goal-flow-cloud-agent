"""M5 gate — a paused goal survives a cloud restart.

Run:  python scripts/verify_persistence.py     (needs OPENROUTER_API_KEY)

THE CLAIM: the interrupts ARE the product. A goal sits at its understanding or
approval gate waiting for a person, and people take hours; a board goal spans days.
With MemorySaver every restart silently dropped every paused goal — the card stays
on the board and nothing can ever resume it, which is worse than losing it visibly.

So this does not test SqliteSaver (that is someone else's library). It tests the
thing we actually promise: interpret a goal to its first interrupt, THROW THE GRAPH
AWAY, build a completely new one over the same file, and resume. If the state did
not survive, the resume has nothing to resume and the goal is gone.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.graph import nodes  # noqa: E402

CAPABILITIES = {
    "domains": [{"id": "meal_plan", "hint": "planning the week's dinners"}],
    "modules": [
        {"name": "Inventory", "kind": "capability", "description": "What food is in the fridge.",
         "functions": [{"name": "ListItems"}]},
        {"name": "Recipes", "kind": "capability", "description": "Recipe search.",
         "functions": [{"name": "FindRecipes"}]},
    ],
}


def main() -> int:
    failures: list[str] = []
    db = str(Path(tempfile.mkdtemp()) / "goalflow.db")
    goal_id = "persist-me"

    # --- process 1: run to the understanding gate, then "crash" ---
    graph_a = nodes.build_graph(nodes.default_checkpointer(db))
    state_a = nodes.start_goal(graph_a, "plan our dinners this week", goal_id, CAPABILITIES)
    interrupt_a = state_a.get("_interrupt")
    if not interrupt_a:
        print("  FAIL the goal did not reach an interrupt — nothing to persist")
        return 1
    print(f"  ok   paused at: {interrupt_a.get('kind')}")

    del graph_a  # the cloud goes away

    # --- process 2: a brand-new graph over the same file ---
    graph_b = nodes.build_graph(nodes.default_checkpointer(db))
    snapshot = graph_b.get_state({"configurable": {"thread_id": goal_id}})
    if not snapshot.values:
        failures.append("a NEW graph sees nothing — the paused goal did not survive the restart")
    else:
        print(f"  ok   a new graph re-reads the goal: {snapshot.values.get('goal_text')!r}")

    # The real proof: RESUME it. Reading state back is not the same as being able
    # to continue — the interrupt has to still be there to answer.
    resumed = nodes.resume_goal(graph_b, goal_id, {"confirmed": True})
    contract = resumed.get("contract") or {}
    if not contract:
        failures.append("resume after restart produced no contract — the goal is unusable, just visible")
    else:
        print(f"  ok   resumed after restart -> dispatch built, domain={contract.get('domain')!r}")

    # An unknown goal must be empty, not an error — the board asks about goals the
    # user may have already finished.
    unknown = graph_b.get_state({"configurable": {"thread_id": "never-existed"}})
    if unknown.values:
        failures.append("an unknown goal_id returned state")
    else:
        print("  ok   an unknown goal is empty, not an error")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 12 (persistence across restart): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
