"""v6-M1 gate — constraints resolve PER GOAL, and the enforced set is never narrowed.

Run:  python scripts/verify_constraints.py    (no API key needed — resolution has no LLM)

WHY THIS EXISTS. Through v5 the safety block was one flat, hand-seeded, meal-shaped
object: every goal — a vacation, a party, an energy week — was dispatched the same
$120 WEEKLY GROCERY cap as its ceiling and the same dinner-preference bias. Nothing
caught it, because a wrong cap is not a crash: the plan still comes back, it is just
planned against the wrong household.

So this gate asserts the two halves that are easy to get backwards:

  * the DOMAIN-PICKED half really does differ per goal (travel cap and away window on
    a vacation; peak tariff on an energy goal; the party cap on a party), and
  * the ENFORCED-ALWAYS half really is identical on every goal, including a domain
    slug nobody has ever tagged. Relevance may pick a cap. It may NEVER drop an
    allergen — that is the one failure mode the whole design exists to prevent.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.memory.store import (  # noqa: E402
    load_family_profile,
    resolve_constraints,
    soft_candidates,
)

TODAY = date(2026, 7, 29)
DOMAINS = ("meal_plan", "guest_dinner", "vacation_prep", "birthday_party", "grocery_cost", "energy_saving")


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    profile = load_family_profile()
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

    # 2. The domain-picked half differs, and differs correctly.
    check(resolved["meal_plan"]["hard"]["budget_cap"] == 120.0,
          f"meal_plan keeps the weekly household cap, got {resolved['meal_plan']['hard'].get('budget_cap')}")
    check(resolved["vacation_prep"]["hard"]["budget_cap"] == 1500.0,
          f"vacation_prep must carry the TRAVEL cap, got {resolved['vacation_prep']['hard'].get('budget_cap')} "
          "(the v5 bug: it inherited the $120 grocery cap)")
    check(resolved["birthday_party"]["hard"]["budget_cap"] == 200.0,
          f"birthday_party must carry the party cap, got {resolved['birthday_party']['hard'].get('budget_cap')}")
    check(coined["hard"]["budget_cap"] == 120.0,
          f"a coined domain falls back to the household cap, got {coined['hard'].get('budget_cap')}")

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
    for domain, r in resolved.items():
        check(r["hard"].get("quiet_hours") == {"start": "21:30", "end": "07:00"},
              f"quiet hours are household-wide, missing on {domain}")

    # 4. The v5 contract is preserved where it was already right: a meal goal's hard
    #    block must be exactly what v5 dispatched — plus the household envelope, which
    #    every goal draws from and which the DEVICE (not this resolver) narrows the
    #    goal's own cap against.
    check(
        resolved["meal_plan"]["hard"] == {
            "allergens": ["peanuts"],
            "medical": ["rohan_low_sodium"],
            "dietary": ["no_pork"],
            "budget_cap": 120.0,
            "quiet_hours": {"start": "21:30", "end": "07:00"},
            "budget_envelope": {"cap": 600.0, "period": "monthly"},
        },
        f"meal_plan hard block drifted: {resolved['meal_plan']['hard']}",
    )

    # 4b. The envelope is household-wide: EVERY goal must carry it, or two goals can
    #     spend the same money while each stays inside its own cap.
    for domain, r in list(resolved.items()) + [("plant_care", coined)]:
        check(r["hard"].get("budget_envelope") == {"cap": 600.0, "period": "monthly"},
              f"{domain} must carry the household envelope, got {r['hard'].get('budget_envelope')}")

    # 5. Soft bias is domain-shaped — the visible half of the fix.
    veg_prefs = resolved["meal_plan"]["soft"].get("prefer", [])
    check("more_vegetables" in veg_prefs, f"meal_plan keeps its meal bias, got {veg_prefs}")
    check("mushrooms" in resolved["meal_plan"]["soft"].get("dislikes", []),
          "meal_plan keeps the household dislike")
    vac_soft = resolved["vacation_prep"]["soft"]
    check("mushrooms" not in str(vac_soft),
          f"a vacation goal must not be told about mushrooms, got {vac_soft}")
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

    # 7. The relevance path: ids select, and garbage falls back to tags rather than
    #    resolving to an empty bias.
    picked = resolve_constraints(profile, "meal_plan", today=TODAY, soft_ids=["s-dislikes-mushrooms"])
    check(picked["soft"].get("dislikes") == ["mushrooms"] and "prefer" not in picked["soft"],
          f"soft_ids selects exactly what it names, got {picked['soft']}")
    check(picked["hard"] == resolved["meal_plan"]["hard"],
          "the relevance pass must not change the hard block")
    junk = resolve_constraints(profile, "meal_plan", today=TODAY, soft_ids=["nope-not-a-real-id"])
    check(junk["soft"] == resolved["meal_plan"]["soft"],
          f"unknown ids fall back to tag matching, got {junk['soft']}")

    # 8. Provenance rides along: every applied row can say where it came from.
    rows = resolved["vacation_prep"]["applied"]
    check(all(row.get("source") in {"account", "derived", "chat"} for row in rows),
          f"every applied row carries a source, got {[r.get('source') for r in rows]}")
    check(any(row["id"] == "c-cap-travel" and row["source"] == "account" for row in rows),
          "the travel cap is traceable to the account")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 15 (constraint resolution): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
