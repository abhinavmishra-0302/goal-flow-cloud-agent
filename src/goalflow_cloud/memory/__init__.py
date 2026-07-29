"""Household constraint store (v6).

Mocked as data/memory/family_profile.json: "fake the world; make the mechanism
real." The real mechanism is the hard/soft split plus PER-GOAL RESOLUTION — every
constraint carries its own source, scope and expiry, and ``store.resolve_constraints``
picks the set for the goal in hand: the hard block is assembled by code (list kinds
unioned across the whole store, cap/window kinds domain-picked) and injected into
dispatch.constraints.hard; soft preferences and household context only bias planning.

Serves ANY goal domain — and, unlike the flat block it replaced, stops handing a
vacation goal the weekly grocery cap.
"""
