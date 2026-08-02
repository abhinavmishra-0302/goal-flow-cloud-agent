"""v8.1 gate — the cloud reasons in the DEVICE's day, not the machine's.

Run:  python scripts/verify_worldclock.py    (no API key needed — no LLM on this path)

WHY THIS EXISTS. The device runs a ``SimulatedClock``: it anchors at real today when the
process starts and ``control advance_day`` steps it. Everything the user can see is dated
by that clock — the plan's rows, the world's events, the board's "today". The cloud called
``date.today()``, which is the date on whatever machine the hub happens to be running on.

The two agree exactly until someone presses Advance day, which is the second minute of the
demo. After that the cloud resolves "tomorrow", "this week", every goal horizon and every
captured rule's expiry against a day the world has already left — and the further the demo
runs, the wider the gap. It is the kind of bug that never throws: every date is a perfectly
valid date, just the wrong one.

So the assertions are about WHERE THE DAY COMES FROM:

  * the hub learns the device's day from frames it was already sending, and never has to
    be told twice or asked;
  * a nonsense date cannot poison the clock — it is refused, and the last good day stands;
  * a home that has said nothing falls back to real today, because a device that has not
    reported has not simulated either;
  * the day is per HOME, since two devices can sit on different simulated days;
  * and a goal run reasons about the day it was STAMPED with, so a world tick landing
    mid-run cannot move a goal's dates under it.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import goalflow_cloud.graph.nodes as nodes  # noqa: E402
from goalflow_cloud.server import ConnectionRegistry  # noqa: E402

REAL_TODAY = date.today()
SIM = date(2026, 8, 3)


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    registry = ConnectionRegistry()

    # 1. SILENCE MEANS REAL TODAY. A device that has not reported has not simulated.
    check(
        registry.world_today("hub-a") == REAL_TODAY,
        f"an unheard-from home must fall back to real today, got {registry.world_today('hub-a')}",
    )

    # 2. THE HUB LEARNS IT. This is the whole fix: the day arrives on frames the device
    #    was already sending (`status.payload.sim_date`, `day_advanced.sim_date`).
    registry.set_world_today("hub-a", SIM.isoformat())
    check(registry.world_today("hub-a") == SIM, f"the reported day must stick, got {registry.world_today('hub-a')}")

    # 3. ADVANCE DAY MOVES IT. The frame that says the world moved is the one that
    #    matters most — a goal created straight after a tick, with no status in
    #    between, would otherwise be interpreted against yesterday.
    registry.set_world_today("hub-a", (SIM + timedelta(days=1)).isoformat())
    check(
        registry.world_today("hub-a") == SIM + timedelta(days=1),
        "a later report must move the day forward",
    )

    # 4. RUBBISH CANNOT POISON THE CLOCK. A malformed or absent date leaves the last
    #    good day standing — falling back to real today HERE would be worse than the
    #    bug, because it would silently re-introduce the drift mid-demo.
    for junk in ("", None, "not-a-date", "2026-13-45"):
        registry.set_world_today("hub-a", junk)
        check(
            registry.world_today("hub-a") == SIM + timedelta(days=1),
            f"{junk!r} must be refused and the last good day kept",
        )

    # 5. PER HOME. Two devices can sit on different simulated days, so a single global
    #    clock would hand one home the other's calendar.
    check(
        registry.world_today("hub-b") == REAL_TODAY,
        "a second home must not inherit the first home's simulated day",
    )

    # 6. THE GRAPH REASONS IN THAT DAY. `_today` is what every node now calls; if it
    #    ignores the stamp, all of the above is bookkeeping nobody reads.
    check(nodes._today({"world_today": SIM.isoformat()}) == SIM, "a stamped run must use the world's day")
    check(nodes._today({}) == REAL_TODAY, "an unstamped run must use real today")
    check(nodes._today({"world_today": ""}) == REAL_TODAY, "an empty stamp must use real today")
    check(nodes._today({"world_today": "nonsense"}) == REAL_TODAY, "a bad stamp must not raise")

    # 7. AND THE CALENDAR IT HANDS THE INTERPRETER STARTS THERE. The date fix (gate 28)
    #    and the clock fix meet at exactly this line: a correct calendar anchored on the
    #    wrong day resolves "Tuesday" to the wrong Tuesday, and nothing downstream can
    #    tell. Belongs to this gate because only the clock decides the anchor.
    first = nodes._calendar_block(nodes._today({"world_today": SIM.isoformat()})).splitlines()[0]
    check(
        first.split()[0] == SIM.isoformat() and "<- today" in first,
        f"the interpreter's calendar must start on the world's day, got {first!r}",
    )

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 29 (the cloud reasons in the device's day): "
          + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
