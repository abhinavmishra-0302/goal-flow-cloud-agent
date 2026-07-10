"""Generic family memory store (v2) — the hard-vs-soft split.

Source of truth: data/memory/family_profile.json. GENERIC shape (serves any
goal domain — meals, guest prep, chores, appliances, budget...):

  {
    "family_id": "...",
    "members": [ { "name", "role", ... } ],
    "hard": {                      # SAFETY POLICY — enforced, never LLM-touched
      "allergens": [], "medical": [], "dietary": [],
      "budget_cap": null, "quiet_hours": null
    },
    "soft": { ... },               # free-form preferences — planning bias only
    "context": [ "..." ]           # family routines/notes — grounds dispatch.context
  }

Design rules:
- ``hard`` maps field-for-field onto models.contract.HardConstraints and is
  copied VERBATIM into dispatch.constraints.hard — a pure data path; the LLM
  never generates, edits, or paraphrases it. The device Safety filter
  enforces exactly this block ("LLM plans, code checks").
- ``soft`` + ``members`` + ``context`` are prompt context for the Goal
  Interpreter: they bias planning, never gate it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Default profile location, relative to the repo root.
DEFAULT_PROFILE_PATH = Path("data/memory/family_profile.json")


def load_family_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    """Read and return the family profile JSON (resolved from the repo root)."""
    profile_path = path
    if not profile_path.is_absolute():
        repo_root = Path(__file__).resolve().parents[3]
        profile_path = repo_root / profile_path

    with profile_path.open(encoding="utf-8") as profile_file:
        return json.load(profile_file)


def hard_safety_block(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the verbatim hard safety policy for constraints.hard.

    TODO(v2-M1): shallow-copy profile["hard"] (defaulting missing keys per
    models.contract.HardConstraints) WITHOUT any transformation of values —
    this is the verbatim injection channel.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def soft_bias_block(profile: dict[str, Any]) -> dict[str, Any]:
    """Return the soft-preference + family-context bundle for planning bias.

    TODO(v2-M1): {"members": ..., "soft": ..., "context": ...} — fed to the
    LLM as context; never safety-enforced.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")
