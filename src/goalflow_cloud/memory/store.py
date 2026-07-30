"""Household constraint store (v6) — sourced, scoped, expiring, resolved per goal.

Source of truth: data/memory/family_profile.json. This is the ACCOUNT's copy of
household policy; it pushes DOWN to the device on every dispatch. The device owns
world state (inventory, spend, appliance draw) and never sources policy.

The file is a LIBRARY OF ENTRIES, one per fact rather than one per kind, so each
carries its own provenance and lifetime::

    { "id": "c-cap-travel", "kind": "budget_cap", "value": 1500.0,
      "enforcement": "hard", "source": "account", "scope": "domain",
      "applies_to": ["vacation_prep"], "expires_day_offset": 8 }

WHY THIS REPLACED A FLAT hard/soft BLOCK (v2–v5). The old block was seeded once, in
the shape of a meal: every goal — a vacation, a party, an energy-saving week — was
dispatched the same $120 WEEKLY GROCERY cap as its ceiling and the same "prefer more
vegetables, dislikes mushrooms" bias. v3.5 made the planner generic per domain; the
constraints it planned against never followed.

RESOLUTION RULES (see resolve_constraints) — deterministic, with no LLM anywhere near
the hard block:

- hard LIST kinds (allergens, dietary, medical) are UNIONED across every entry with
  ``applies_to`` IGNORED. The enforced set is NEVER narrowed by relevance: a wrong
  relevance pick must cost a noisy plan, never a safety miss. Allergens cost nothing
  on a vacation goal; dropping them could cost a child.
- hard SCALAR kinds (budget_cap, quiet_hours, peak_hours, away_window) ARE
  domain-picked — most specific ``applies_to`` wins, ties go to the STRICTER value.
  This is what makes a vacation goal carry a travel cap instead of the grocery cap.
- soft entries are picked by RELEVANCE. The caller may pass ``soft_ids`` (the
  graph's small LLM relevance pass); with none, tag matching is the fallback. Soft
  can be wrong for free, which is exactly why only soft goes near the model.

v7 SPLIT ENFORCEMENT FROM DISPLAY. An entry may carry ``display_to`` — the domains its
chip is worth SHOWING on. It changes nothing about resolution: the list kinds still
union with ``applies_to`` ignored, and ``hard`` is still what gets dispatched. It only
produces a second block, ``hard_display``, for the cards. The reason is that a home-prep
goal enforcing "peanuts · no pork · low sodium" is telling the truth and saying nothing:
three chips that cannot bite on any step of that plan teach the reader to skip the chip
row, and the row is where a chip that DOES bite has to be noticed. Hiding a chip must
never hide a rule, which is why these are two separate blocks rather than one filtered
one.

v7 ALSO EMPTIED THE SEED OF MONEY AND QUIET HOURS. ``budget_cap``, ``budget_envelope``
and ``quiet_hours`` remain first-class kinds here and remain enforced by the device's
rule instances — the mechanism is intact and gated. There is simply no entry of those
kinds in the household any more, so nothing resolves and nothing is dispatched. A kind
with no entry is not dead code; it is an unused capability.

Dates are DAY OFFSETS resolved against today at load — the same generic-clock rule
the device's data files follow, so a seeded world never goes stale between demos.
Chat-captured constraints (M4) write an absolute ``expires_on`` instead.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from goalflow_cloud.config import get_settings

logger = logging.getLogger(__name__)

#: Default profile location, relative to the repo root.
DEFAULT_PROFILE_PATH = Path("data/memory/family_profile.json")

#: Hard kinds that UNION across every entry — never domain-filtered (see module docs).
HARD_LIST_KINDS: tuple[str, ...] = ("allergens", "dietary", "medical")

#: Hard kinds that hold ONE value per goal, and so are domain-picked.
HARD_SCALAR_KINDS: tuple[str, ...] = (
    "budget_cap",
    "quiet_hours",
    "peak_hours",
    "away_window",
    #: The shared pool every goal draws from. Household-wide by nature, but resolved
    #: through the same path so a domain could override it if one ever needs to.
    "budget_envelope",
)

#: The soft kind whose entries are free-text household notes, not a preference list.
CONTEXT_KIND = "context"

#: v7: an OPTIONAL per-entry list of domains the entry is worth SHOWING on. It has no
#: bearing whatsoever on enforcement — `applies_to` is still ignored for the list kinds,
#: so the enforced set is never narrowed (R7). This exists because a vacation goal that
#: displays "peanuts · no pork · low sodium" is telling the truth about what is enforced
#: while saying nothing about what is being planned, and three irrelevant safety chips
#: teach the reader to stop reading them. Absent means "show everywhere", so every entry
#: that does not opt in behaves exactly as it did before.
DISPLAY_TO_KEY = "display_to"


def profile_path(path: Path | None = None) -> Path:
    """Resolve which store file to use — the caller's, the configured one, or the seed.

    ``GOALFLOW_PROFILE_PATH`` is the device's ``--data`` for household policy: point it
    at a scratch copy and a demo's captures stop dirtying the repo's seed. A configured
    path that does not exist yet is SEEDED from the repo's copy on first use, so a
    demo is one env var rather than a setup step someone forgets.
    """
    resolved = path
    if resolved is None:
        configured = get_settings().profile_path.strip()
        resolved = Path(configured) if configured else DEFAULT_PROFILE_PATH

    if not resolved.is_absolute():
        resolved = Path(__file__).resolve().parents[3] / resolved

    if not resolved.exists():
        seed = Path(__file__).resolve().parents[3] / DEFAULT_PROFILE_PATH
        if resolved == seed:
            return resolved
        resolved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, resolved)
        logger.info("profile_seeded path=%s from=%s", resolved, seed)
    return resolved


def load_family_profile(path: Path | None = None) -> dict[str, Any]:
    """Read and return the constraint store JSON."""
    with profile_path(path).open(encoding="utf-8") as profile_file:
        return json.load(profile_file)


def active_constraints(profile: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """Every non-expired entry, with day-offset values resolved to ISO dates.

    Expiry is checked BEFORE anything else, so a retired constraint can never be
    unioned into the hard block by the "never narrow" rule.
    """
    today = today or date.today()
    live: list[dict[str, Any]] = []
    for entry in profile.get("constraints", []):
        if not isinstance(entry, dict) or not entry.get("kind"):
            continue
        if _is_expired(entry, today):
            logger.debug("constraint_expired id=%s kind=%s", entry.get("id"), entry.get("kind"))
            continue
        resolved = dict(entry)
        resolved["value"] = _resolve_offsets(entry.get("value"), today)
        live.append(resolved)
    return live


def resolve_constraints(
    profile: dict[str, Any],
    domain: str,
    today: date | None = None,
    soft_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve the store for ONE goal.

    Returns ``{"hard", "hard_display", "soft", "context", "applied"}``:

    - ``hard``  — the safety policy for ``dispatch.constraints.hard``. Assembled by
      code from store data only; no LLM output reaches it.
    - ``hard_display`` — v7: the same block minus whatever this domain should not be
      SHOWN (``display_to``). Display only. ``hard`` is what is dispatched and enforced,
      and the two must never be confused: hiding a chip must never hide a rule.
    - ``soft``  — preference bias, grouped by kind, for ``dispatch.constraints.soft``.
    - ``context`` — free-text household notes for ``dispatch.context``.
    - ``applied`` — provenance records (id / kind / label / source / scope / display)
      for the understanding card and the logs: what was picked, and where it came from.

    ``soft_ids`` selects soft entries by id (the graph's relevance pass). When it is
    None or selects nothing, tag matching on ``applies_to`` is the fallback — the
    resolution must still work with the LLM unavailable.
    """
    today = today or date.today()
    entries = active_constraints(profile, today)

    hard: dict[str, Any] = {kind: [] for kind in HARD_LIST_KINDS}
    hard_display: dict[str, Any] = {kind: [] for kind in HARD_LIST_KINDS}
    applied: list[dict[str, Any]] = []
    scalar_best: dict[str, tuple[int, dict[str, Any]]] = {}

    for entry in entries:
        if entry.get("enforcement") != "hard":
            continue
        kind = entry["kind"]
        if kind in HARD_LIST_KINDS:
            # UNIONED regardless of domain — the enforced set is never narrowed.
            shown = _displays(entry, domain)
            for item in _as_list(entry.get("value")):
                if item not in hard[kind]:
                    hard[kind].append(item)
                if shown and item not in hard_display[kind]:
                    hard_display[kind].append(item)
            applied.append(_provenance(entry, why="always enforced", display=shown))
        elif kind in HARD_SCALAR_KINDS:
            specificity = _specificity(entry, domain)
            if specificity < 0:
                continue
            incumbent = scalar_best.get(kind)
            if incumbent is None or specificity > incumbent[0]:
                scalar_best[kind] = (specificity, entry)
            elif specificity == incumbent[0]:
                scalar_best[kind] = (specificity, _stricter(kind, incumbent[1], entry))
        else:
            logger.warning(
                "constraint_unknown_hard_kind id=%s kind=%s — no device rule enforces it",
                entry.get("id"),
                kind,
            )

    for kind, (specificity, entry) in scalar_best.items():
        hard[kind] = entry["value"]
        shown = _displays(entry, domain)
        if shown:
            hard_display[kind] = entry["value"]
        applied.append(
            _provenance(entry, why="domain" if specificity else "household default", display=shown)
        )

    # An empty list kind is dropped from the display block entirely, so the chip row
    # simply is not rendered rather than rendering an empty chip.
    hard_display = {kind: value for kind, value in hard_display.items() if value}

    soft, context, soft_applied = _resolve_soft(entries, domain, soft_ids)
    applied.extend(soft_applied)

    return {
        "hard": hard,
        "hard_display": hard_display,
        "soft": soft,
        "context": context,
        "applied": applied,
    }


def append_constraints(
    entries: list[dict[str, Any]],
    path: Path | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Persist user-confirmed constraints into the store; return what was written.

    This is the ONLY write path into household policy, and it exists because the
    user said yes — never because a model proposed something (see R2 in
    docs/V6_CONSTRAINTS.md). Two rules are enforced here rather than trusted to the
    caller:

    - **Tighten only.** A captured cap may lower an existing ceiling, never raise or
      remove one. "Keep the party under $150" is a constraint; "we can spend $900 on
      the party now" is a policy change, and a chat message is not where that
      happens. Anything looser is dropped and logged.
    - **Nothing is removed.** Entries are appended. Relaxing a household rule is a
      deliberate act somewhere with more ceremony than a sentence typed at a fridge.

    Ids are stamped here so two captures of the same thing cannot collide.
    """
    today = today or date.today()
    target = profile_path(path)

    with target.open(encoding="utf-8") as profile_file:
        profile = json.load(profile_file)

    existing_ids = {entry.get("id") for entry in profile.get("constraints", [])}
    written: list[dict[str, Any]] = []
    for entry in entries:
        candidate = dict(entry)
        candidate["source"] = "chat"
        candidate.setdefault("scope", "household")
        candidate.setdefault("applies_to", ["*"])
        candidate.setdefault("enforcement", "soft")
        candidate["captured_on"] = today.isoformat()

        if not _tightens(profile, candidate, today):
            logger.warning(
                "constraint_capture_rejected kind=%s value=%s — a captured constraint may only tighten",
                candidate.get("kind"),
                candidate.get("value"),
            )
            continue

        candidate["id"] = _unique_id(candidate, existing_ids, today)
        existing_ids.add(candidate["id"])
        profile.setdefault("constraints", []).append(candidate)
        written.append(candidate)

    if written:
        with target.open("w", encoding="utf-8") as profile_file:
            json.dump(profile, profile_file, indent=2, ensure_ascii=False)
            profile_file.write("\n")
        logger.info("constraints_captured path=%s ids=%s", target, [entry["id"] for entry in written])
    return written


def _tightens(profile: dict[str, Any], candidate: dict[str, Any], today: date) -> bool:
    """True when this capture only ever narrows what is already allowed.

    Numeric ceilings must come DOWN. Everything else is additive by nature — a new
    allergen or preference restricts, it cannot permit — so it passes.
    """
    value = candidate.get("value")
    if not _is_number(value):
        return True

    domains = [d for d in candidate.get("applies_to", ["*"]) if d != "*"] or [""]
    for domain in domains:
        current = resolve_constraints(profile, domain, today=today)["hard"].get(candidate.get("kind", ""))
        if _is_number(current) and value >= current:
            return False
    return True


def _unique_id(candidate: dict[str, Any], taken: set[str | None], today: date) -> str:
    """A stable, readable id: chat-<kind>-<date>, suffixed if that is already taken."""
    base = f"chat-{candidate.get('kind', 'constraint')}-{today.isoformat()}"
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


def soft_candidates(profile: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """The soft entries a relevance pass may choose from (id + kind + value + tags).

    Deliberately small and flat: this is what gets rendered into a prompt, so it
    carries no notes, no provenance, and no hard entries.
    """
    return [
        {
            "id": entry.get("id", ""),
            "kind": entry["kind"],
            "value": entry.get("value"),
            "applies_to": entry.get("applies_to", []),
        }
        for entry in active_constraints(profile, today)
        if entry.get("enforcement") == "soft"
    ]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_soft(
    entries: list[dict[str, Any]],
    domain: str,
    soft_ids: list[str] | None,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """Pick soft entries (by id when given, else by tag) and group them by kind."""
    soft_entries = [entry for entry in entries if entry.get("enforcement") == "soft"]

    picked: list[dict[str, Any]] = []
    why = "relevance"
    if soft_ids:
        wanted = set(soft_ids)
        picked = [entry for entry in soft_entries if entry.get("id") in wanted]
    if not picked:
        # Fallback — the LLM said nothing usable, or was never asked. Tag matching
        # must stand on its own: resolution cannot depend on a model being up.
        why = "tagged"
        picked = [entry for entry in soft_entries if _specificity(entry, domain) >= 0]

    soft: dict[str, Any] = {}
    context: list[str] = []
    applied: list[dict[str, Any]] = []
    for entry in picked:
        kind = entry["kind"]
        value = entry.get("value")
        if kind == CONTEXT_KIND:
            for item in _as_list(value):
                if item not in context:
                    context.append(item)
        elif isinstance(value, list):
            bucket = soft.setdefault(kind, [])
            for item in value:
                if item not in bucket:
                    bucket.append(item)
        else:
            soft.setdefault(kind, value)
        applied.append(_provenance(entry, why=why, display=_displays(entry, domain)))
    return soft, context, applied


def _displays(entry: dict[str, Any], domain: str) -> bool:
    """Whether this entry is worth SHOWING on this domain's card. Never about enforcement.

    Three cases, and the difference between the first two is the point:

    - **absent** — show everywhere. An entry that does not opt in behaves as it always
      did, which is what makes this additive.
    - **``[]``** — show NOWHERE. Explicitly empty is a decision, not a default, so it
      is honoured rather than falling through to "absent". This is how a rule that is
      enforced on every goal can be kept off every card.
    - **a list** — show on these domains (``"*"`` for all).
    """
    if DISPLAY_TO_KEY not in entry:
        return True
    display_to = entry.get(DISPLAY_TO_KEY) or []
    return "*" in display_to or (bool(domain) and domain in display_to)


def _provenance(entry: dict[str, Any], why: str, display: bool = True) -> dict[str, Any]:
    """One "where did this come from" record, for the gate card and the logs."""
    return {
        "id": entry.get("id", ""),
        "kind": entry["kind"],
        "label": entry.get("label") or entry["kind"].replace("_", " "),
        "value": entry.get("value"),
        "enforcement": entry.get("enforcement", "soft"),
        "source": entry.get("source", "account"),
        "scope": entry.get("scope", "household"),
        "why": why,
        #: v7, display only: this row is still APPLIED and still enforced when it is
        #: hard — it is simply not worth a chip on this domain's card.
        "display": display,
    }


def _specificity(entry: dict[str, Any], domain: str) -> int:
    """How specifically an entry claims this goal: -1 no match, 0 household, 1 domain, 2 goal.

    Goal-scoped entries (M4's chat capture) outrank domain ones, which outrank the
    household default — so a cap set on THIS goal wins over the standing one.
    """
    applies_to = entry.get("applies_to") or ["*"]
    if entry.get("scope") == "goal" and domain in applies_to:
        return 2
    if domain and domain in applies_to:
        return 1
    if "*" in applies_to:
        return 0
    return -1


def _stricter(kind: str, current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Break a specificity tie toward the tighter constraint.

    Numbers compare (a lower cap is stricter). Windows do not order meaningfully, so
    the incumbent stands and the collision is logged rather than silently resolved —
    two equally-specific quiet-hour windows is a store-authoring bug, not an input.
    """
    current_value, candidate_value = current.get("value"), candidate.get("value")
    if isinstance(current_value, (int, float)) and isinstance(candidate_value, (int, float)):
        return candidate if candidate_value < current_value else current
    logger.warning(
        "constraint_tie kind=%s ids=%s,%s — keeping the first; give one a narrower applies_to",
        kind,
        current.get("id"),
        candidate.get("id"),
    )
    return current


def _is_expired(entry: dict[str, Any], today: date) -> bool:
    """Absolute ``expires_on`` wins; ``expires_day_offset`` is the seeded-world form."""
    expires_on = entry.get("expires_on")
    if isinstance(expires_on, str) and expires_on.strip():
        try:
            return date.fromisoformat(expires_on.strip()) < today
        except ValueError:
            logger.warning("constraint_bad_expiry id=%s expires_on=%s", entry.get("id"), expires_on)
            return False
    offset = entry.get("expires_day_offset")
    return _is_number(offset) and offset < 0


def _resolve_offsets(value: Any, today: date) -> Any:
    """Turn ``{start_day_offset, end_day_offset}`` into ISO ``{start, end}`` dates."""
    if not isinstance(value, dict):
        return value
    if "start_day_offset" not in value and "end_day_offset" not in value:
        return value
    resolved = {k: v for k, v in value.items() if not k.endswith("_day_offset")}
    for bound in ("start", "end"):
        offset = value.get(f"{bound}_day_offset")
        if _is_number(offset):
            resolved[bound] = (today + timedelta(days=int(offset))).isoformat()
    return resolved


def _is_number(value: Any) -> bool:
    """True for a real number — bools are ints in Python and must not count."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None else [value]
