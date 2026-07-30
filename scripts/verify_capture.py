"""v6-M4 gate — a household rule is captured only when the user says yes.

Run:  python scripts/verify_capture.py     (no API key needed — the write path has no LLM)

WHAT THIS PROTECTS. Capture is the one place a sentence typed at a fridge turns into
policy the safety filter will enforce, so the interesting assertions are all about
what must NOT happen:

  * silence must not capture — an un-answered or unticked proposal writes nothing,
    and confirming the GOAL must not smuggle in a rule that rode along with it;
  * a capture may only ever TIGHTEN — "raise the party budget to $900" is a policy
    change, and a chat message is not where that happens;
  * a captured rule must actually reach constraints.hard on the NEXT goal, with its
    provenance intact, or the whole feature is theatre.

The store is written for real against a TEMP COPY of the profile, because the bug
this guards is a write — asserting on an in-memory dict would prove nothing about
the file the next goal reads.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.graph.nodes import _accepted_constraints, _capture_is_sane  # noqa: E402
from goalflow_cloud.memory.store import (  # noqa: E402
    DEFAULT_PROFILE_PATH,
    append_constraints,
    load_family_profile,
    profile_path,
    resolve_constraints,
)

TODAY = date(2026, 7, 29)
VEGAN = {
    "id": "proposed-1",
    "kind": "dietary",
    "value": ["no_dairy"],
    "enforcement": "hard",
    "applies_to": ["*"],
    "label": "no dairy",
    "quote": "we've gone vegan",
}
LIGHTER = {"id": "proposed-2", "kind": "prefer", "value": ["lighter_dinners"], "enforcement": "soft"}


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    proposed = [VEGAN, LIGHTER]

    # 1. SILENCE IS A NO. Every shape of "the user didn't tick it" writes nothing.
    check(_accepted_constraints({"confirmed": False, "accepted_constraint_ids": ["proposed-1"]}, proposed) == [],
          "declining the gate must accept no constraints, whatever ids came back")
    check(_accepted_constraints({"confirmed": True}, proposed) == [],
          "confirming the GOAL must not accept a rule that rode along with it")
    check(_accepted_constraints({"confirmed": True, "accepted_constraint_ids": []}, proposed) == [],
          "an empty acceptance list captures nothing")
    check(_accepted_constraints(None, proposed) == [], "no answer at all captures nothing")

    accepted = _accepted_constraints({"confirmed": True, "accepted_constraint_ids": ["proposed-1"]}, proposed)
    check(len(accepted) == 1 and accepted[0]["kind"] == "dietary",
          f"ticking one proposal accepts exactly that one, got {accepted}")
    check(all("id" not in entry for entry in accepted),
          "the proposal id must not be persisted — the stored id is minted at write time")

    # 2. A kind the resolver does not know is DROPPED, not stored. Stored, it would
    #    resolve to nothing — neither enforced nor used as bias — so the user would be
    #    told their rule was remembered while it did precisely nothing.
    check(not _capture_is_sane({"kind": "vibes", "value": ["calm"], "enforcement": "hard"}),
          "an unrecognised kind must be dropped, not stored")
    check(not _capture_is_sane({"kind": "dietary", "value": []}), "an empty value is not a constraint")

    # The model reaches for "diet" and "allergy" as readily as the canonical names, so
    # what is obviously the same thing is mapped rather than thrown away.
    synonym = {"kind": "diet", "value": ["no_dairy"], "enforcement": "hard"}
    check(_capture_is_sane(synonym) and synonym["kind"] == "dietary",
          f"'diet' must normalise to the store's kind, got {synonym}")

    # A REAL kind marked hard that no device rule enforces is downgraded rather than
    # shipped: in constraints.hard it would read as a guarantee and block nothing.
    soft_kind = {"kind": "habits", "value": ["shop_on_saturday"], "enforcement": "hard"}
    _capture_is_sane(soft_kind)
    check(soft_kind["enforcement"] == "soft", f"an unenforceable hard kind must be downgraded, got {soft_kind}")

    # 3. THE WRITE PATH, against a real copy of the store.
    with tempfile.TemporaryDirectory() as tmp:
        store_file = Path(tmp) / "family_profile.json"
        shutil.copy(Path(__file__).resolve().parents[1] / DEFAULT_PROFILE_PATH, store_file)

        before = load_family_profile(store_file)
        written = append_constraints(accepted, path=store_file, today=TODAY)
        check(len(written) == 1, f"the accepted rule is written once, got {written}")
        check(written[0]["source"] == "chat", f"a captured rule is sourced to chat, got {written[0].get('source')}")
        check(written[0]["captured_on"] == TODAY.isoformat(), "a captured rule records when it was said")
        check(written[0]["id"].startswith("chat-dietary-"), f"stored id is minted here, got {written[0]['id']}")

        after = load_family_profile(store_file)
        check(len(after["constraints"]) == len(before["constraints"]) + 1,
              "capture APPENDS — nothing in the store is replaced or removed")

        # 4. IT REACHES THE NEXT GOAL. The point of remembering.
        resolved = resolve_constraints(after, "meal_plan", today=TODAY)
        check("no_dairy" in resolved["hard"]["dietary"],
              f"the captured rule must be enforced on the next goal, got {resolved['hard']['dietary']}")
        row = next((r for r in resolved["applied"] if r["id"] == written[0]["id"]), None)
        check(row is not None and row["source"] == "chat",
              "the applied row must say the rule came from chat, or a block cannot cite it")

        # 5. TIGHTEN ONLY. A cap may come down from chat; it may never go up.
        #
        #    v7 SEEDS ITS OWN INCUMBENT. This used to lean on the $200 party cap in the
        #    committed store, which v7 removed along with every other money entry — and
        #    the failure mode was quiet: with nothing to loosen, `_tightens` has nothing
        #    to compare against and returns True, so "raising a cap must be refused"
        #    would have gone on passing while testing nothing at all. A gate for a
        #    ratchet has to own the thing it ratchets against.
        standing = [{"kind": "budget_cap", "value": 200.0, "enforcement": "hard", "applies_to": ["birthday_party"]}]
        check(len(append_constraints(standing, path=store_file, today=TODAY)) == 1,
              "fixture: a standing cap is written when the household has none")
        loosen = [{"kind": "budget_cap", "value": 900.0, "enforcement": "hard", "applies_to": ["birthday_party"]}]
        check(append_constraints(loosen, path=store_file, today=TODAY) == [],
              "raising a $200 party cap to $900 from chat must be refused")
        tighten = [{"kind": "budget_cap", "value": 150.0, "enforcement": "hard", "applies_to": ["birthday_party"]}]
        check(len(append_constraints(tighten, path=store_file, today=TODAY)) == 1,
              "lowering the party cap to $150 from chat is a real constraint and must stick")
        party = resolve_constraints(load_family_profile(store_file), "birthday_party", today=TODAY)
        check(party["hard"]["budget_cap"] == 150.0,
              f"the tightened cap must win over the standing $200, got {party['hard']['budget_cap']}")

        # 6. Two captures of the same kind on the same day must not collide.
        again = append_constraints([{"kind": "dietary", "value": ["no_shellfish"], "enforcement": "hard"}],
                                   path=store_file, today=TODAY)
        check(again and again[0]["id"] != written[0]["id"], "a second capture gets its own id")

        # The seed profile must be untouched by all of the above.
        check(json.loads((Path(__file__).resolve().parents[1] / DEFAULT_PROFILE_PATH).read_text())
              == before, "the real store must not be written by this gate")

    # 7. GOALFLOW_PROFILE_PATH — the device's `--data` for household policy. Without
    #    it, demoing capture writes into the repo's seed and someone has to remember
    #    to `git checkout` afterwards; the device has had a scratch world for
    #    milestones. A configured path that does not exist yet must SEED itself, or
    #    the override is one more setup step to forget.
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "nested" / "profile.json"
        os.environ["GOALFLOW_PROFILE_PATH"] = str(scratch)
        try:
            check(not scratch.exists(), "fixture starts with no scratch profile")
            resolved = profile_path()
            check(resolved == scratch and scratch.exists(),
                  f"a configured path is created and seeded on first use, got {resolved}")
            check(load_family_profile()["family_id"] == before["family_id"],
                  "the scratch copy starts as a copy of the seed")

            # Guarded: if the override is ignored, this write lands in the REAL seed.
            # The gate must say so and stop, not crash on a missing scratch file — a
            # gate that dies is a gate whose verdict nobody can read.
            if not scratch.exists():
                check(False, "the override was ignored — a capture here would write to the repo's seed")
            else:
                written = append_constraints(
                    [{"kind": "allergens", "value": ["shellfish"], "enforcement": "hard"}], today=TODAY
                )
                check(len(written) == 1, "a capture writes to the configured path")
                check("shellfish" in json.loads(scratch.read_text())["constraints"][-1]["value"],
                      "the capture landed in the scratch copy")
            check(json.loads((Path(__file__).resolve().parents[1] / DEFAULT_PROFILE_PATH).read_text()) == before,
                  "THE SEED IS UNTOUCHED — that is the whole point of the override")
        finally:
            os.environ.pop("GOALFLOW_PROFILE_PATH", None)

    check(profile_path().name == "family_profile.json" and "data/memory" in str(profile_path()),
          "with the env var unset, the seed is used again")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 16 (chat capture): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
