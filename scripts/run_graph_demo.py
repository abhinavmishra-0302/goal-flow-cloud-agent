"""Run the v2 graph once for a goal and print the resulting Task Contract.

DESIGN STUB — runnable once the build pass implements the node bodies.
Demonstrates generality: pass any goal text (meal week, guest dinner, ...).
"""

from __future__ import annotations

import json
import sys

from goalflow_cloud.graph.nodes import build_graph, start_goal
from goalflow_cloud.memory.store import load_family_profile
from goalflow_cloud.models.contract import Dispatch

DEFAULT_GOAL = "we've got 6 people over Saturday for dinner - sort it"


def main() -> None:
    goal_text = " ".join(sys.argv[1:]) or DEFAULT_GOAL

    # TODO(v2-M1): start_goal runs interpret_goal -> load_memory ->
    # build_contract -> dispatch_to_device and pauses at the device hand-off.
    frame = start_goal(build_graph(), goal_text, goal_id="demo-goal")
    dispatch = Dispatch(**frame)

    # Invariant check: constraints.hard must equal the profile's hard block
    # VERBATIM (the LLM never touches the safety policy).
    profile_hard = load_family_profile()["hard"]
    contract_hard = dispatch.constraints.hard.model_dump(exclude_none=True)
    assert contract_hard == {k: v for k, v in profile_hard.items() if v is not None}, (
        f"hard constraints drifted: {contract_hard} != {profile_hard}"
    )

    print(json.dumps(frame, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
