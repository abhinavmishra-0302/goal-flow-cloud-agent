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
from goalflow_cloud.server import _window_already_household, _window_words  # noqa: E402

TODAY = date(2026, 7, 28)
AWAY_START = (TODAY + timedelta(days=2)).isoformat()
AWAY_END = (TODAY + timedelta(days=3)).isoformat()
#: A second, LONGER trip stated after the first — same start, so only the end moves and
#: the two entries cannot be told apart by anything but which was said later.
LATER_END = (TODAY + timedelta(days=5)).isoformat()


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

        # 5b. PROVENANCE. The window is now live household-wide, so EVERY other goal
        #     resolves it into its own `constraints.hard` — and the fan-out reads the
        #     approved goal's resolved hard block. Without a guard, approving that meal
        #     week re-promoted the window it had just read, under a fresh id and a fresh
        #     expiry, and the next goal inherited THAT. Found in a live demo: four
        #     captured away windows had stacked up, one of them seven days long, and
        #     because `append_constraints` is idempotent the real away goal's approval
        #     then wrote nothing and the cross-goal re-plan silently stopped firing.
        #
        #     The distinction is authorship: a goal whose window equals what the
        #     household already holds for everyone READ it and has nothing to promote.
        inherited_by_meal = after["hard"]["away_window"]
        check(
            _window_already_household(load_family_profile(store), "away_window",
                                      inherited_by_meal, today=TODAY),
            "a goal carrying the window it inherited must not re-promote it",
        )
        check(
            not _window_already_household(
                load_family_profile(store), "away_window",
                {"start": "2026-09-10", "end": "2026-09-12"}, today=TODAY),
            "a genuinely different trip must still be promotable",
        )
        # And the household must hold exactly what was written — the guard is a
        # provenance test, not a blanket "never write twice".
        check(
            resolve_constraints(load_family_profile(store), "", today=TODAY)["hard"]
            .get("away_window") == {"start": AWAY_START, "end": AWAY_END},
            "the household-wide resolution is the promoted window itself",
        )

        # 6. THE SEED IS UNTOUCHED — this gate writes to a copy, like the capture gate.
        seed_rows = [c for c in json.loads(seed.read_text())["constraints"] if c.get("source") == "chat"]
        check(not any(c["kind"] == "away_window" for c in seed_rows),
              "the committed seed must not have gained a captured away window")

    # 7. A NEWER WINDOW WINS — the failure that silently broke the demo twice.
    #
    #    Approving a second trip while an older household window is still live WROTE the
    #    new window, logged it, and left it INVISIBLE: two entries at the same
    #    specificity, and resolution kept whichever came first in the store. So every
    #    other goal went on resolving the old dates, `fan_out_household_change` compared
    #    each goal's enforced set before and after, found them identical, and correctly
    #    concluded nothing had moved. Nothing had. No error was raised at any step,
    #    because at every individual step the code was right.
    #
    #    Both entries carry the SAME captured_on — a demo states every one of these rules
    #    on one simulated day — which is why store ORDER, not the date, is what decides.
    #
    #    Its own store: this writes a second window that outlives the first, and the
    #    expiry and provenance checks above are asserted against a store with exactly one.
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "family_profile.json"
        shutil.copy(seed, store)
        first = {
            "kind": "away_window",
            "value": {"start": AWAY_START, "end": AWAY_END},
            "enforcement": "hard", "scope": "household", "applies_to": ["*"],
            "label": "away window", "expires_on": AWAY_END,
        }
        append_constraints([dict(first)], path=store, today=TODAY)
        settled = resolve_constraints(load_family_profile(store), "meal_plan", today=TODAY)

        # Same start, later end — so nothing but "which was said second" tells them apart.
        second = {**first, "value": {"start": AWAY_START, "end": LATER_END}, "expires_on": LATER_END}
        written = append_constraints([dict(second)], path=store, today=TODAY)
        check(len(written) == 1, f"a window with different dates must still be writable, got {written}")

        moved = resolve_constraints(load_family_profile(store), "meal_plan", today=TODAY)
        check(
            moved["hard"].get("away_window") == {"start": AWAY_START, "end": LATER_END},
            f"the NEWEST household window must be what other goals resolve, got {moved['hard'].get('away_window')}",
        )
        check(
            moved["hard"] != settled["hard"],
            "the enforced set must actually move, or the fan-out sees no change and re-plans nothing",
        )

    # 8. THE CARD HAS ONE LINE, so the window has to read as words, not a date range.
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
