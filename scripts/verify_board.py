"""M6 gate — the board's numbers are derived, and they add up.

Run:  python scripts/verify_board.py      (no API key needed — the fold has no LLM)

Replays a realistic frame sequence through BoardService and checks the card a person
would actually read. The point is that every field traces to something the device
SAID: v2 had no task model, so a board built then could only have inferred progress
from plan-day vs the clock — a number that looks authoritative and is fiction.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.board import BoardService  # noqa: E402

DEV = "hub-a"
CONTRACT = {
    # The interpreter produces both: a sentence to plan from, and a short card
    # headline. Truncating the sentence would give "Prepare my son's birthday party
    # next Sund…" — which is what a card must never look like.
    "title": "Birthday Party Preparation",
    "objective": "Prepare my son's birthday party next Sunday",
    "domain": "guest_dinner",
    "time_window": {"start": "2026-06-15", "end": "2026-06-22"},
    "scope": {"guests": 20},
}


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    board = BoardService()

    # 1. A goal is dispatched.
    s = board.on_goal_created(DEV, "g1", CONTRACT, client_ref="g-17")
    check(s.title == "Birthday Party Preparation", f"the card headline is the interpreter's short title, got {s.title!r}")
    check(s.subtitle == "Mon, Jun 22 • 20 Guests", f"subtitle assembled from the contract, got {s.subtitle!r}")
    check(s.eta == "2026-06-22", "eta is the contract's own window end, not a guess")
    check(s.client_ref == "g-17", "client_ref rides along so the UI can re-key its card")
    check(s.state == "waiting", "a goal being planned is waiting, not on_track")

    # 2. The device's task ledger reports progress. THIS is where the numbers come from.
    board.on_task_update(DEV, "g1", {
        "task_id": "t1", "state": "monitoring", "progress_pct": 43,
        "pending_tasks": 4, "next_step": "Buy party decorations",
    })
    s = board.snapshot(DEV)[1][0]
    check(s.progress_pct == 43, f"progress comes from the device, got {s.progress_pct}")
    check(s.next_step == "Buy party decorations", f"next step comes from the DAG frontier, got {s.next_step!r}")
    check(s.pending_tasks == 4, "pending count comes from the device")

    # 3. A plan needing approval -> Waiting (the board's third chip).
    board.on_plan_ready(DEV, "g1", {
        "plan": [{"id": "s1"}], "safety": {"gate": "passed"},
        "proposals": [{"proposal_id": "p1", "requires_approval": True}],
        "explanation": "Ordered the cake and blocked out Sunday afternoon.",
    })
    s = board.snapshot(DEV)[1][0]
    check(s.state == "waiting", f"a plan awaiting approval is Waiting, got {s.state!r}")
    check(bool(board.cached_plan("g1")), "the plan is cached for drill-in after a reload")
    check(s.activity and "cake" in s.activity[0], f"activity shows what happened, got {s.activity}")

    # 4. An adaptation -> an alert, and the board says what it wants.
    board.on_proposal(DEV, "g1", {"payload": {"action": "Swap the nut cake for a fruit platter"}})
    s = board.snapshot(DEV)[1][0]
    check(s.alerts.count == 1 and s.alerts.severity == "danger", f"an adaptation alerts, got {s.alerts}")
    check(s.state == "waiting", "an adaptation waits on the user")

    # 5. A deferred effect: the WORLD's fault, not the plan's -> warn, and danger must stick.
    board.on_status(DEV, "g1", {
        "task_status": "monitoring",
        "payload": {"executed": [{"result": "deferred_precheck"}], "note": "The oven is offline"},
    })
    s = board.snapshot(DEV)[1][0]
    check(s.alerts.count == 2, f"alerts accumulate, got {s.alerts.count}")
    check(s.alerts.severity == "danger", "a warn must NOT downgrade an existing danger")
    check(s.state == "at_risk", f"outstanding danger reads as At Risk, got {s.state!r}")

    # 6. Completion wins over everything.
    board.on_status(DEV, "g1", {"task_status": "done", "payload": {"note": "All set for Sunday"}})
    s = board.snapshot(DEV)[1][0]
    check(s.state == "completed" and s.progress_pct == 100, f"done -> completed/100, got {s.state} {s.progress_pct}")
    check(s.pending_tasks == 0 and s.next_step is None, "a finished goal has no next step")

    # 7. board_seq is monotonic — a UI that sees a gap heals with board_get.
    check(board.seq(DEV) >= 6, f"board_seq advanced per change, got {board.seq(DEV)}")

    # 8. Several goals, and the device dies.
    board.on_goal_created(DEV, "g2", CONTRACT, None)
    board.on_goal_created(DEV, "g3", CONTRACT, None)
    changed = board.on_device_offline(DEV)
    states = {g.goal_id: g.state for g in board.snapshot(DEV)[1]}
    check(len(changed) == 2, f"offline touches only unfinished goals, got {len(changed)}")
    check(states["g1"] == "completed", "a COMPLETED goal is not made at_risk by the device leaving")
    check(states["g2"] == "at_risk" and states["g3"] == "at_risk", "unfinished goals go at_risk — nothing will answer for them")

    # 9. A frame for a goal we never saw start must not fabricate a card.
    check(board.on_task_update(DEV, "ghost", {"progress_pct": 50}) is None, "an unknown goal is ignored, not invented")
    check(len(board.snapshot(DEV)[1]) == 3, "the ghost created no card")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 13 (board fold): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
