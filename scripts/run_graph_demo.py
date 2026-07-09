"""Run the M2 graph once and validate its dispatch output."""

from __future__ import annotations

import json

from goalflow_cloud.graph.nodes import build_graph
from goalflow_cloud.memory.store import load_family_profile
from goalflow_cloud.models.contract import Dispatch


GOAL_TEXT = "help my family eat healthier this week and reduce food waste"


def dump_model(model: object) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)  # type: ignore[attr-defined]
    return model.dict(exclude_none=True)  # type: ignore[attr-defined]


def main() -> None:
    result = build_graph().invoke({"user_goal_text": GOAL_TEXT})
    frame = result["dispatch_frame"]
    dispatch = Dispatch(**frame)
    profile = load_family_profile()
    hard = profile["hard"]
    parsed_hard = dump_model(dispatch.constraints.hard)

    assert parsed_hard == hard, f"hard constraints drifted: {parsed_hard} != {hard}"

    print(json.dumps(frame, indent=2, sort_keys=True))
    print(f"fallback_used={result.get('fallback_used', False)}")
    if result.get("fallback_reason"):
        print(f"fallback_reason={result['fallback_reason']}")


if __name__ == "__main__":
    main()
