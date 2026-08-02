"""v8.1 gate — a named weekday means that weekday.

Run:  python scripts/verify_dates.py
      (the calendar half needs no API key; the interpreter half runs only with one)

WHY THIS EXISTS. Told only "Real today is 2026-08-02 (Sunday)", the interpreter resolved
every named weekday exactly ONE DAY EARLY — "Tuesday and Wednesday" came back as Mon–Tue,
"Thursday and Friday" as Wed–Thu, "Monday" as Sunday. Four for four, temperature 0,
reproducible on demand. It was not misreading the calendar; it was not consulting one. It
counted ordinals from today and labelled the result Tuesday.

That is not a cosmetic slip, because of where the number lands. ``_align_away_window``
takes ``intent.time_window`` VERBATIM as the away window; the away window is what empties
days out of every OTHER goal's plan through the cross-goal fan-out. So a one-day error
deletes the wrong two dinners from a plan the user already approved, and does it with
enough confidence to look deliberate. It was found exactly that way — a home-away goal for
Tuesday and Wednesday emptied Monday and Tuesday.

The fix is to stop asking for arithmetic: ``_calendar_block`` hands the model the next
fortnight, dated and named, and a weekday becomes a LOOKUP. So this gate has two halves:

  * the calendar we hand over is itself correct — deterministic, always runs;
  * and the interpreter actually uses it — live, and skipped without a key rather than
    failing, because a gate that cannot run must not be able to pass either.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import goalflow_cloud.graph.nodes as nodes  # noqa: E402
from goalflow_cloud.config import get_settings  # noqa: E402
from goalflow_cloud.graph.nodes import CALENDAR_DAYS, _calendar_block  # noqa: E402

#: Anchors chosen to sit on different weekdays — the failure was weekday-dependent, so a
#: single anchor could pass while the demo's own day of the week was broken.
ANCHORS = [date(2026, 8, 2), date(2026, 8, 3), date(2026, 8, 5), date(2026, 8, 7)]

#: Enough capability for the interpreter to judge the goal actionable. An empty digest
#: short-circuits the node before any date is resolved, which would make this gate pass
#: while testing nothing — that mistake cost a full sweep the first time.
CAPS = {
    "domains": [
        {"id": "meal_plan", "hint": "planning the week's dinners"},
        {"id": "vacation_prep", "hint": "a trip or being away from home"},
    ],
    "modules": [
        {"kind": "capability", "name": "Calendar", "description": "read the family calendar"},
        {"kind": "capability", "name": "Appliance", "description": "run appliances"},
    ],
}

CASES = [
    ("We will be out on Tuesday and Wednesday. Prepare our home.", ("Tuesday", "Wednesday")),
    ("We are away Thursday and Friday. Get the house ready.", ("Thursday", "Friday")),
    ("We're out Monday. Prepare the home.", ("Monday",)),
    ("We'll be gone Saturday and Sunday. Prepare our home.", ("Saturday", "Sunday")),
]


def expected(today: date, names: tuple[str, ...]) -> tuple[str, str]:
    """The dates a person means. Each name resolves forward from the PREVIOUS one, so a
    span stays consecutive — "Saturday and Sunday" is a weekend, not a Saturday and the
    Sunday behind it. Today counts: "out Monday" said on a Monday means today."""
    picked: list[date] = []
    cursor = today
    for name in names:
        for offset in range(8):
            day = cursor + timedelta(days=offset)
            if day.strftime("%A") == name:
                picked.append(day)
                cursor = day
                break
    return picked[0].isoformat(), picked[-1].isoformat()


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    # 1. THE CALENDAR WE HAND OVER. If this is wrong the model is being lied to, and no
    #    amount of prompt discipline saves it.
    for anchor in ANCHORS:
        rows = _calendar_block(anchor).splitlines()
        check(len(rows) == CALENDAR_DAYS, f"{anchor}: calendar must list {CALENDAR_DAYS} days, got {len(rows)}")
        for i, row in enumerate(rows):
            want_day = anchor + timedelta(days=i)
            iso, name = row.split()[0], row.split()[1]
            check(iso == want_day.isoformat(), f"{anchor}: row {i} is {iso}, expected {want_day}")
            check(
                name == want_day.strftime("%A"),
                f"{anchor}: {iso} is labelled {name}, but it is a {want_day.strftime('%A')}",
            )
        check("<- today" in rows[0], f"{anchor}: the first row must be marked as today")
        check(
            not any("<- today" in row for row in rows[1:]),
            f"{anchor}: only one row may be marked today",
        )

    # 2. THE INTERPRETER USES IT. The half that actually broke.
    if not (get_settings().openrouter_api_key or "").strip():
        print("  SKIP interpreter sweep — no API key configured")
        for f in failures:
            print(f"  FAIL {f}")
        print("gate 28 (a named weekday means that weekday): "
              + ("PASS (calendar only)" if not failures else f"FAIL: {len(failures)}"))
        return 0 if not failures else 1

    for anchor in ANCHORS:
        with patch.object(nodes, "date", wraps=date) as stub:
            stub.today.return_value = anchor
            for text, names in CASES:
                out = nodes.interpret_goal({"goal_text": text, "device_capabilities": CAPS})
                window = (out.get("intent") or {}).get("time_window") or {}
                got = (window.get("start"), window.get("end"))
                want = expected(anchor, names)
                check(
                    got == want,
                    f"{anchor} ({anchor.strftime('%a')}) {text[:40]!r}: got {got[0]}..{got[1]}, expected {want[0]}..{want[1]}",
                )

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 28 (a named weekday means that weekday): "
          + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
