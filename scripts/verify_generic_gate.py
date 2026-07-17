"""M4 gate — actionability follows the DEVICE's capabilities, not a list in our code.

Run:  python scripts/verify_generic_gate.py     (needs OPENROUTER_API_KEY)

The point of the milestone is a goal that v2 would have DECLINED — "get the house
ready, we're away next week" — now being accepted, because the device advertises
appliances and reminders. So the vacation case is the real assertion; the meal case
is the regression canary, and trivia is the guard that "generic" didn't become
"accepts anything".

The last case is the subtle one: a goal is actionable if the product COULD address
it, even when the device would then refuse to do it. The cloud answers "is this in
this product's world?"; the device answers "may it happen?" — and the device's
refusal ("I never unlock doors") is a far better answer than the cloud pretending
not to understand the request.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.graph import nodes  # noqa: E402

# What the Family Hub device actually advertises today (its `capabilities` frame,
# trimmed to what the digest reads).
FAMILY_HUB = {
    # The goal shapes the device routes on. Its observers answer to these ids by
    # name, so the interpreter must use them rather than invent a plausible slug.
    "domains": [
        {"id": "meal_plan", "hint": "planning the week's dinners — healthy eating, using up what is in the fridge"},
        {"id": "guest_dinner", "hint": "hosting a dinner for guests — menu, prep timeline, dietary constraints, RSVPs"},
    ],
    "modules": [
        {"name": "Inventory", "kind": "capability", "description": "What food is currently in the fridge/pantry, including expiry.",
         "functions": [{"name": "ListItems"}, {"name": "GetExpiringItems"}]},
        {"name": "Calendar", "kind": "capability", "description": "The shared family calendar — who is busy when.",
         "functions": [{"name": "GetEvents"}, {"name": "AddEvent"}]},
        {"name": "Recipes", "kind": "capability", "description": "Recipe search and details: ingredients, allergen tags, prep time.",
         "functions": [{"name": "FindRecipes"}]},
        {"name": "ShoppingList", "kind": "capability", "description": "The family shopping list and grocery ordering.",
         "functions": [{"name": "Add"}, {"name": "PlaceOrder"}]},
        {"name": "Reminders", "kind": "capability", "description": "Family reminders and notes shown on the Hub.",
         "functions": [{"name": "Create"}]},
        {"name": "Appliance", "kind": "capability", "description": "Controls SmartThings appliances: oven, dishwasher, vacuum, lights.",
         "functions": [{"name": "ListAppliances"}, {"name": "PreheatOven"}, {"name": "RunProgram"}]},
        {"name": "Safety", "kind": "steering", "description": "deterministic hard-constraint filter"},
    ]
}

CASES = [
    # (goal, expected_actionable, expected_domain | None, why it matters)
    ("plan our dinners this week, healthy and using up what we have", True, "meal_plan",
     "REGRESSION: the v2 flagship must still work"),
    ("we've got 6 people over Saturday for dinner — sort it", True, "guest_dinner",
     "REGRESSION: the guest demo ROUTES on this domain — meal_plan silently loses RSVP watching"),
    ("get the house ready, we're away all next week", True, None,
     "THE POINT: v2 DECLINED this. Appliances + reminders + calendar can advance it"),
    ("run the dishwasher tonight after everyone's in bed", True, None,
     "appliance control is advertised, so this is in the product's world"),
    ("what's the capital of France?", False, None,
     "GUARD: generic must not mean 'accepts anything'"),
    ("write me a poem about summer", False, None,
     "GUARD: nothing advertised relates to this"),
]


def main() -> int:
    failures = []
    for goal, expected, want_domain, why in CASES:
        state = {"goal_text": goal, "goal_id": "verify", "device_capabilities": FAMILY_HUB}
        result = nodes.interpret_goal(state)
        intent = result.get("intent") or {}
        actual = bool(intent.get("actionable"))
        mark = "ok  " if actual == expected else "FAIL"
        if actual != expected:
            failures.append(goal)
        domain = intent.get("domain", "")
        if want_domain and domain != want_domain:
            failures.append(f"{goal}: domain {domain!r} != {want_domain!r}")
            mark = "FAIL"
        print(f"  {mark} actionable={actual!s:5} domain={domain!r:18} {goal[:46]!r}")
        print(f"       {why}")
        if not actual and intent.get("decline_reason"):
            print(f"       declined: {intent['decline_reason']}")

    # No device connected: we must NOT guess. Guessing is how this got hardcoded.
    no_device = nodes.interpret_goal({"goal_text": "plan our dinners", "goal_id": "v", "device_capabilities": {}})
    if (no_device.get("intent") or {}).get("actionable") is not False:
        failures.append("no-device must decline, not guess")
        print("  FAIL with no device connected the gate must decline, not guess")
    else:
        print("  ok   no device connected -> declines honestly rather than guessing")

    print("gate 10 (generic actionability): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
