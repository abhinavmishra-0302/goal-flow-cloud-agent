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
    #
    # The explanation here is a REAL one, verbatim in shape from a live run: the
    # planner writes a rationale paragraph, not a caption. An earlier version of this
    # gate used a tidy one-liner fixture and so passed while the rendered card read
    # "The 7-day vegetarian dinner plan leverages existing inven…". A fixture prettier
    # than production tests nothing — the field must be fed what it will really get.
    board.on_plan_ready(DEV, "g1", {
        "plan": [{"id": "s1"}], "safety": {"gate": "passed"},
        "proposals": [{"proposal_id": "p1", "requires_approval": True}],
        "explanation": (
            "This plan orders the cake early to secure the Sunday slot, then blocks "
            "the afternoon and confirms the guest count before decorations are bought, "
            "so nothing is purchased against a number that may still change."
        ),
    })
    s = board.snapshot(DEV)[1][0]
    check(s.state == "waiting", f"a plan awaiting approval is Waiting, got {s.state!r}")
    check(bool(board.cached_plan("g1")), "the plan is cached for drill-in after a reload")
    check(s.activity == [], f"a plan's rationale is NOT activity — nothing has happened yet, got {s.activity}")

    # 3b. A COMPLETED task is what activity means.
    board.on_task_update(DEV, "g1", {
        "task_id": "t1", "title": "Grocery delivery confirmed", "state": "completed",
        "progress_pct": 43, "pending_tasks": 4, "next_step": "Buy party decorations",
    })
    s = board.snapshot(DEV)[1][0]
    check(s.activity == ["Grocery delivery confirmed"], f"activity is what finished, got {s.activity}")
    check(all(len(a) <= 60 for a in s.activity), "an activity line must fit the card")

    # 4. An adaptation -> an alert, and the board says what it wants.
    board.on_proposal(DEV, "g1", {"payload": {"action": "Swap the nut cake for a fruit platter"}})
    s = board.snapshot(DEV)[1][0]
    check(s.alerts.count == 1 and s.alerts.severity == "danger", f"an adaptation alerts, got {s.alerts}")
    check(s.state == "waiting", "an adaptation waits on the user")

    # 5. A deferred effect: the WORLD's fault, not the plan's -> warn, and it holds the goal.
    #
    # SEMANTICS CHANGED in v3.6.1: alerts are what is OUTSTANDING, not a tally of
    # everything that ever needed attention. This tick both ANSWERS the adaptation from
    # step 4 (task_status leaves "adapting") and reports a NEW deferral, so the count is
    # 1 — the deferral — not 2. The old cumulative reading is what left a card saying
    # "1 alert — tap to review" forever after the user had already approved it, and
    # pinned the card to At Risk off a danger that no longer existed.
    board.on_status(DEV, "g1", {
        "task_status": "monitoring",
        "payload": {"executed": [{"result": "deferred_precheck"}], "note": "The oven is offline"},
    })
    s = board.snapshot(DEV)[1][0]
    check(s.alerts.count == 1, f"the answered adaptation clears; the new deferral stands, got {s.alerts.count}")
    check(s.alerts.severity == "warn", f"a deferral is the world's fault -> warn, got {s.alerts.severity!r}")
    check(s.state == "waiting", f"a deferred effect waits on the WORLD, got {s.state!r}")

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

    # 10. NEGATIVE-PATH HONESTY (M8). A goal the WORLD blocked must read Waiting, not
    # On Track — the bug this closes rendered a precheck-blocked goal GREEN because a
    # downstream ternary recomputed the state the precheck had set.
    board.on_goal_created(DEV, "g4", CONTRACT, None)
    board.on_plan_ready(DEV, "g4", {
        "plan": [], "proposals": [],
        "safety": {"gate": "passed"},
        "precheck": {"ok": False, "results": [
            {"id": "smartthings_connected", "status": "fail", "detail": "SmartThings is disconnected — reconnect and this will resume"},
        ]},
    })
    s4 = next(g for g in board.snapshot(DEV)[1] if g.goal_id == "g4")
    check(s4.state == "waiting", f"a precheck-blocked goal is Waiting, not On Track — got {s4.state!r}")
    check(s4.alerts.severity == "warn", f"the block is a warn (world's fault, recoverable), got {s4.alerts.severity!r}")
    check(s4.next_step is not None and "SmartThings" in s4.next_step,
          f"the card shows the fix as its next step, got {s4.next_step!r}")

    # A deferred effect during execution, with no prior danger, also holds the goal on
    # the WORLD — Waiting, not the false green _state_for would have returned for a
    # warn-only alert.
    board.on_status(DEV, "g4", {"task_status": "monitoring", "payload": {"executed": [{"result": "deferred_precheck"}]}})
    s4 = next(g for g in board.snapshot(DEV)[1] if g.goal_id == "g4")
    check(s4.state == "waiting", f"a deferred effect keeps the goal Waiting, got {s4.state!r}")

    # --- v3.6.1 regressions: three bugs a user hit on a live board ---

    # 1. An approved adaptation must CLEAR its alert. _bump only ever counted upward and
    #    nothing cleared it, so the card kept "1 alert — tap to review" forever, and
    #    _state_for pinned it to At Risk off that stale danger.
    board.on_goal_created(DEV, "g5", CONTRACT, None)
    board.on_proposal(DEV, "g5", {"payload": {"action": "notify about a delivery arriving to an empty house"}})
    s5 = next(g for g in board.snapshot(DEV)[1] if g.goal_id == "g5")
    check(s5.alerts.count == 1, f"a pending adaptation raises one alert, got {s5.alerts.count}")

    # 2. A PENDING proposal is not activity — it has not happened. Logging it as activity
    #    made the card print the same sentence twice: once as "done", once as "next".
    check("notify about a delivery arriving to an empty house" not in s5.activity,
          f"a pending proposal must not be logged as something that happened, got {s5.activity!r}")
    check(s5.next_step == "notify about a delivery arriving to an empty house",
          f"the pending action IS the next step, got {s5.next_step!r}")

    board.on_status(DEV, "g5", {"task_status": "monitoring",
                                "payload": {"executed": [{"result": "ok"}]}})
    s5 = next(g for g in board.snapshot(DEV)[1] if g.goal_id == "g5")
    check(s5.alerts.count == 0, f"an executed adaptation clears its alert, got {s5.alerts.count}")
    check(s5.state != "at_risk", f"and the card stops reading At Risk off it, got {s5.state!r}")

    # 2b. A DECLINE resolves the adaptation too. It executes nothing, so a clear-rule
    #     keyed on `executed` leaves the alert up for the case the user notices most:
    #     they said no, and the card went on insisting it needed them.
    board.on_goal_created(DEV, "g7", CONTRACT, None)
    board.on_proposal(DEV, "g7", {"payload": {"action": "swap the paneer for tofu"}})
    board.on_status(DEV, "g7", {"task_status": "monitoring", "payload": {"executed": []}})
    s7 = next(g for g in board.snapshot(DEV)[1] if g.goal_id == "g7")
    check(s7.alerts.count == 0, f"a DECLINED adaptation clears its alert too, got {s7.alerts.count}")
    check(s7.task_status != "adapting", f"and the goal stops being 'adapting', got {s7.task_status!r}")

    # 3. Progress spans the GOAL's deadline, not the plan's item spread. v3.5 made plan
    #    days real dates, so an all-on-one-evening checklist has span 1 — and one Advance
    #    day drove the card to 100% with the deadline still a week out.
    # A LIVE contract — its deadline is ahead of today, as a real goal's is. (CONTRACT's
    # fixed dates are historical, which would make the plan span the later bound and hide
    # the very regression this checks.)
    from datetime import date as _date, timedelta as _td
    deadline = (_date.today() + _td(days=7)).isoformat()
    live = dict(CONTRACT, time_window={"start": _date.today().isoformat(), "end": deadline})
    board.on_goal_created(DEV, "g6", live, None)
    board.on_plan_ready(DEV, "g6", {
        "plan": [{"id": "s1", "day": 1, "title": "Lock up"}, {"id": "s2", "day": 1, "title": "Arm alarm"}],
        "proposals": [], "safety": {"gate": "passed"}, "precheck": {"ok": True},
    })
    window = board._windows["g6"]
    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    check(window["end"] > tomorrow,
          f"a same-day plan must not give a 1-day window when the goal has a later deadline — got {window!r}")

    board.on_status(DEV, "g6", {"task_status": "monitoring",
                                "payload": {"sim_date": (date.today() + timedelta(days=1)).isoformat()}})
    s6 = next(g for g in board.snapshot(DEV)[1] if g.goal_id == "g6")
    check(s6.progress_pct < 100,
          f"one advanced day must not complete a goal whose deadline is days away — got {s6.progress_pct}%")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 13 (board fold): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
