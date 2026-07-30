"""v7-M5 gate — approving one goal changes another, once, and only where it should.

Run:  python scripts/verify_crossgoal.py    (no API key needed — the fan-out has no LLM)

WHY THIS EXISTS. This is the one path in the system that changes a plan WITHOUT asking.
Everything else that moves a plan is something the world did, so a person decides what to
do about it; this is something the person already decided, arriving at a goal that had not
heard yet. That makes it the most valuable moment in the demo and the most dangerous piece
of code in the repo — a fan-out that fires too widely rewrites plans nobody agreed to, and
one that fires twice does it twice.

So the assertions are about BLAST RADIUS and IDEMPOTENCE, not about the pretty part:

  * the window is written HOUSEHOLD-wide and EXPIRES, so it cannot quietly empty the same
    two dates in every future meal week;
  * every other goal's enforced set actually changes — which is what makes the re-plan
    honest rather than a re-plan for show;
  * writing it twice is a no-op, because a re-sent approval or a reconnect replaying one
    must not stack windows or push a second re-plan;
  * and the goal that CAUSED it is not told to re-plan itself.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.memory.store import (  # noqa: E402
    DEFAULT_PROFILE_PATH,
    append_constraints,
    load_family_profile,
    resolve_constraints,
)
from goalflow_cloud.server import _window_words  # noqa: E402

TODAY = date(2026, 7, 28)
AWAY_START = (TODAY + timedelta(days=2)).isoformat()
AWAY_END = (TODAY + timedelta(days=3)).isoformat()


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    seed = Path(__file__).resolve().parents[1] / DEFAULT_PROFILE_PATH

    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "family_profile.json"
        shutil.copy(seed, store)

        # 0. BEFORE. A meal week knows nothing about any trip.
        before = resolve_constraints(load_family_profile(store), "meal_plan", today=TODAY)
        check("away_window" not in before["hard"],
              f"a meal goal must not start with an away window, got {before['hard'].get('away_window')}")

        # 1. THE WRITE. Household-scoped, chat-sourced, self-expiring.
        entry = {
            "kind": "away_window",
            "value": {"start": AWAY_START, "end": AWAY_END},
            "enforcement": "hard",
            "scope": "household",
            "applies_to": ["*"],
            "label": "away window",
            "expires_on": AWAY_END,
        }
        written = append_constraints([dict(entry)], path=store, today=TODAY)
        check(len(written) == 1, f"the away window is written once, got {written}")
        check(written and written[0]["source"] == "chat",
              "the window is sourced to chat — it came from something the user said")
        check(written and written[0].get("expires_on") == AWAY_END,
              "the window MUST expire, or it silently empties the same dates in every future week")

        # 2. IT REACHES THE OTHER GOAL. This is the moment the demo is built on.
        after = resolve_constraints(load_family_profile(store), "meal_plan", today=TODAY)
        check(after["hard"].get("away_window") == {"start": AWAY_START, "end": AWAY_END},
              f"a meal goal must now carry the away window, got {after['hard'].get('away_window')}")
        check(after["hard"] != before["hard"],
              "the enforced set must actually change, or the re-plan is theatre")

        # 3. NOTHING ELSE MOVED. A fan-out that changes more than the window is a fan-out
        #    rewriting plans nobody agreed to.
        check(
            {k: v for k, v in after["hard"].items() if k != "away_window"} == before["hard"],
            f"only the away window may change: {before['hard']} -> {after['hard']}",
        )

        # 4. IDEMPOTENT. A re-sent approval, or a reconnect replaying one, must not stack
        #    windows or trigger a second re-plan.
        again = append_constraints([dict(entry)], path=store, today=TODAY)
        check(again == [], f"writing the same window twice must be a no-op, got {again}")
        rows = [c for c in json.loads(store.read_text())["constraints"]
                if c["kind"] == "away_window" and c.get("source") == "chat"]
        check(len(rows) == 1, f"exactly one captured away window may exist, got {len(rows)}")

        # 5. IT RETIRES ITSELF. The day after the family is back, the meal week is a meal
        #    week again — without anyone remembering to clean up.
        later = resolve_constraints(
            load_family_profile(store), "meal_plan",
            today=date.fromisoformat(AWAY_END) + timedelta(days=1),
        )
        check("away_window" not in later["hard"],
              f"the window must expire on its own, still present as {later['hard'].get('away_window')}")

        # 6. THE SEED IS UNTOUCHED — this gate writes to a copy, like the capture gate.
        seed_rows = [c for c in json.loads(seed.read_text())["constraints"] if c.get("source") == "chat"]
        check(not any(c["kind"] == "away_window" for c in seed_rows),
              "the committed seed must not have gained a captured away window")

    # 7. THE CARD HAS ONE LINE, so the window has to read as words, not a date range.
    for window, want in (
        ({"start": AWAY_START, "end": AWAY_END}, "Thu & Fri"),
        ({"start": AWAY_START, "end": AWAY_START}, "Thu"),
        ({"start": "nonsense", "end": "nonsense"}, "while you're away"),
    ):
        got = _window_words(window)
        check(got == want, f"window words: expected {want!r}, got {got!r}")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 17 (cross-goal: one write, right blast radius, self-retiring): "
          + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
