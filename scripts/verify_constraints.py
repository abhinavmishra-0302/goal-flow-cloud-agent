"""v6-M1 gate — constraints resolve PER GOAL, and the enforced set is never narrowed.

Run:  python scripts/verify_constraints.py    (no API key needed — resolution has no LLM)

WHY THIS EXISTS. Through v5 the safety block was one flat, hand-seeded, meal-shaped
object: every goal — a vacation, a party, an energy week — was dispatched the same
$120 WEEKLY GROCERY cap as its ceiling and the same dinner-preference bias. Nothing
caught it, because a wrong cap is not a crash: the plan still comes back, it is just
planned against the wrong household.

So this gate asserts the two halves that are easy to get backwards:

  * the DOMAIN-PICKED half really does differ per goal (the away window on a vacation,
    peak tariff on an energy goal), and
  * the ENFORCED-ALWAYS half really is identical on every goal, including a domain
    slug nobody has ever tagged. Relevance may pick a window. It may NEVER drop an
    allergen — that is the one failure mode the whole design exists to prevent.

v7 ADDS THE THIRD HALF, which is the one this version could plausibly get wrong.
``display_to`` hides a chip; it must never hide a RULE. So every display assertion
below is paired with an enforcement assertion on the same domain: vacation_prep shows
no food chips AND still resolves all three food constraints into its dispatch. If
those two ever agree, the feature has eaten the invariant it was built beside.

v7 also emptied the store of budget_cap, budget_envelope and quiet_hours. The old
assertions about $120 / $200 / $1500 / $600 are gone with them — but "gone" is itself
asserted, because a cap that quietly comes back would re-introduce the v5 bug this
gate was written for.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.memory.store import (  # noqa: E402
    DEFAULT_PROFILE_PATH,
    load_family_profile,
    resolve_constraints,
    soft_candidates,
)

#: THE COMMITTED SEED, named explicitly — never `load_family_profile()` with no argument.
#: That reads whatever `GOALFLOW_PROFILE_PATH` points at, and a demo run points it at a
#: scratch copy that chat capture then WRITES to. This gate's assertions are about the
#: seed's contents ("birthday_party carries the $200 party cap"), so reading the scratch
#: file makes the verdict depend on whoever last rehearsed the demo — it failed here on a
#: captured vegan rule and a tightened $150 cap, and it could just as easily have passed
#: for an equally wrong reason.
SEED = Path(__file__).resolve().parents[1] / DEFAULT_PROFILE_PATH

TODAY = date(2026, 7, 29)
DOMAINS = ("meal_plan", "guest_dinner", "vacation_prep", "birthday_party", "grocery_cost", "energy_saving")


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    profile = load_family_profile(SEED)
    resolved = {d: resolve_constraints(profile, d, today=TODAY) for d in DOMAINS}
    # A slug the interpreter coined for a goal nobody anticipated. It is tagged
    # nowhere, so it exercises the household-default path end to end.
    coined = resolve_constraints(profile, "plant_care", today=TODAY)

    # 1. The enforced-always half is IDENTICAL everywhere, coined domains included.
    for kind in ("allergens", "dietary", "medical"):
        baseline = resolved["meal_plan"]["hard"][kind]
        check(bool(baseline), f"{kind} must be non-empty in the store")
        for domain, r in list(resolved.items()) + [("plant_care", coined)]:
            check(
                r["hard"][kind] == baseline,
                f"{kind} differs on {domain}: {r['hard'][kind]} != {baseline} — the enforced set was narrowed",
            )

    # 2. v7: the money and quiet-hours entries are GONE, everywhere. Asserted rather
    #    than assumed — a re-seeded cap is exactly the v5 bug coming back, and it would
    #    show up as a $120 chip on a vacation goal long before anyone read the store.
    for domain, r in list(resolved.items()) + [("plant_care", coined)]:
        for kind in ("budget_cap", "budget_envelope", "quiet_hours"):
            check(kind not in r["hard"],
                  f"{domain} must resolve NO {kind} — v7 emptied the store of it, got {r['hard'].get(kind)}")

    # 3. Windows are scoped, not universal.
    check(resolved["energy_saving"]["hard"].get("peak_hours") == {"start": "17:00", "end": "21:00"},
          f"energy_saving must carry the peak tariff window, got {resolved['energy_saving']['hard'].get('peak_hours')}")
    check(not resolved["guest_dinner"]["hard"].get("peak_hours"),
          "guest_dinner must NOT carry peak hours — it would block the 21:00 dishwasher cleanup beat")
    away = resolved["vacation_prep"]["hard"].get("away_window")
    check(away == {"start": (TODAY + timedelta(days=1)).isoformat(), "end": (TODAY + timedelta(days=8)).isoformat()},
          f"vacation_prep away window resolves day offsets to ISO dates, got {away}")
    check(not resolved["meal_plan"]["hard"].get("away_window"),
          "meal_plan must not carry the away window while the seeded world has it eating at home those days")

    # 4. A meal goal's hard block, exactly.
    check(
        resolved["meal_plan"]["hard"] == {
            "allergens": ["peanuts"],
            "medical": ["rohan_low_sodium"],
            "dietary": ["no_pork"],
        },
        f"meal_plan hard block drifted: {resolved['meal_plan']['hard']}",
    )

    # 5. v7 DISPLAY vs ENFORCEMENT. The pairs below are the whole point: each display
    #    assertion sits next to the enforcement assertion it must not have broken.
    vac = resolved["vacation_prep"]
    for kind in ("allergens", "dietary", "medical"):
        check(kind not in vac["hard_display"],
              f"a home-prep goal shows no {kind} chip, got {vac['hard_display'].get(kind)}")
        check(vac["hard"][kind] == resolved["meal_plan"]["hard"][kind],
              f"...but it is STILL ENFORCED: {kind} was {vac['hard'][kind]}, expected "
              f"{resolved['meal_plan']['hard'][kind]} — hiding a chip must never hide a rule")
    check(vac["hard_display"].get("away_window") == away,
          f"the away window IS shown on a vacation goal, got {vac['hard_display'].get('away_window')}")
    for kind in ("allergens", "dietary", "medical"):
        check(resolved["meal_plan"]["hard_display"].get(kind) == resolved["meal_plan"]["hard"][kind],
              f"a meal goal shows every food rule it enforces, {kind} differs")
    check(resolved["energy_saving"]["hard_display"] == {"peak_hours": {"start": "17:00", "end": "21:00"}},
          f"an energy goal shows only its tariff window, got {resolved['energy_saving']['hard_display']}")
    #    Provenance rows must agree with the chips, or the caption under a chip belongs
    #    to a different rule than the chip does.
    shown_ids = {row["id"] for row in vac["applied"] if row["enforcement"] == "hard" and row["display"]}
    check(shown_ids == {"c-away-window"},
          f"vacation_prep displays exactly the away window row, got {shown_ids}")
    applied_ids = {row["id"] for row in vac["applied"] if row["enforcement"] == "hard"}
    check("c-allergen-peanuts" in applied_ids,
          "the allergen row is still APPLIED on a vacation goal — undisplayed is not unapplied")

    # 6. Soft bias is domain-shaped, and every soft entry is LABELLED (the card renders
    #    one row per entry, so an unlabelled preference reads as its kind).
    meal_prefs = resolved["meal_plan"]["soft"].get("prefer", [])
    check("prefer_white_meat" in meal_prefs, f"meal_plan carries the white-meat bias, got {meal_prefs}")
    check("match_protein_to_activity_load" in meal_prefs,
          f"meal_plan carries the workout bias, got {meal_prefs}")
    meal_soft_rows = [r for r in resolved["meal_plan"]["applied"]
                      if r["enforcement"] == "soft" and r["kind"] != "context"]
    check(len(meal_soft_rows) == 2,
          f"the meal demo shows exactly two preferences, got {[r['id'] for r in meal_soft_rows]}")
    vac_soft_rows = [r for r in vac["applied"] if r["enforcement"] == "soft" and r["kind"] != "context"]
    check(len(vac_soft_rows) == 3,
          f"the home-away demo shows exactly three preferences, got {[r['id'] for r in vac_soft_rows]}")
    for row in meal_soft_rows + vac_soft_rows:
        check(bool(row["label"]) and row["label"] != row["kind"].replace("_", " "),
              f"soft entry {row['id']} needs a real label, got {row['label']!r}")
    vac_soft = vac["soft"]
    check("prefer_white_meat" not in str(vac_soft),
          f"a home-prep goal must not be told about meat preferences, got {vac_soft}")
    check("hold_deliveries_while_away" in vac_soft.get("prefer", []),
          f"vacation_prep carries departure bias, got {vac_soft}")
    check(any("neighbour" in c for c in resolved["vacation_prep"]["context"]),
          f"vacation context reaches the dispatch, got {resolved['vacation_prep']['context']}")
    check(not any("football" in c for c in resolved["energy_saving"]["context"]),
          f"energy goals do not need the football run, got {resolved['energy_saving']['context']}")

    # 6. Expiry retires an entry before anything else looks at it.
    every_id = {row["id"] for r in resolved.values() for row in r["applied"]}
    check("s-guest-visiting" not in every_id, "an expired entry (s-guest-visiting) was still applied")
    check("s-guest-visiting" not in {c["id"] for c in soft_candidates(profile, TODAY)},
          "an expired entry was offered to the relevance pass")

    # 8. The relevance path: ids select, and garbage falls back to tags rather than
    #    resolving to an empty bias.
    picked = resolve_constraints(profile, "meal_plan", today=TODAY, soft_ids=["s-prefer-white-meat"])
    check(picked["soft"].get("prefer") == ["prefer_white_meat", "chicken_turkey_fish_over_red_meat"],
          f"soft_ids selects exactly what it names, got {picked['soft']}")
    check(picked["hard"] == resolved["meal_plan"]["hard"],
          "the relevance pass must not change the hard block")
    check(picked["hard_display"] == resolved["meal_plan"]["hard_display"],
          "nor the display block — relevance picks preferences, not chips")
    junk = resolve_constraints(profile, "meal_plan", today=TODAY, soft_ids=["nope-not-a-real-id"])
    check(junk["soft"] == resolved["meal_plan"]["soft"],
          f"unknown ids fall back to tag matching, got {junk['soft']}")

    # 9. Provenance rides along: every applied row can say where it came from.
    rows = resolved["vacation_prep"]["applied"]
    check(all(row.get("source") in {"account", "derived", "chat"} for row in rows),
          f"every applied row carries a source, got {[r.get('source') for r in rows]}")
    check(any(row["id"] == "c-away-window" and row["source"] == "derived" for row in rows),
          "the away window is traceable to the calendar it was derived from")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 15 (constraint resolution): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
