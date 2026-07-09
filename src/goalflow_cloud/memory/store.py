"""Loader for the mocked family memory.

*** M2 SCOPE — DESIGN STUB ONLY. ***

Source of truth: data/memory/family_profile.json. Shape:
  { "family_id", "members": [...],
    "hard": { "allergens": [], "dietary": [], "medical": [] },
    "soft": { "dislikes": [], "prefer": [], "notes": [] },
    "context": [...] }

Design rule: the ``hard`` block maps field-for-field onto
models.contract.HardConstraints and is copied verbatim into
dispatch.constraints.hard — never paraphrased by the LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Default profile location, relative to the repo root.
DEFAULT_PROFILE_PATH = Path("data/memory/family_profile.json")


def load_family_profile(path: Path = DEFAULT_PROFILE_PATH) -> dict[str, Any]:
    """Read and return the family profile JSON.

    M1 keeps this intentionally small: the hub needs a real data path from
    mocked family memory into dispatch.constraints.
    """
    profile_path = path
    if not profile_path.is_absolute():
        repo_root = Path(__file__).resolve().parents[3]
        profile_path = repo_root / profile_path

    with profile_path.open(encoding="utf-8") as profile_file:
        return json.load(profile_file)
