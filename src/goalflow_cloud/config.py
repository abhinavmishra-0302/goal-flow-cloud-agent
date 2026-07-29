"""Environment-backed configuration for the v2 cloud agent.

LLM-ONLY design: the OPENROUTER_* settings are required at runtime for goal
interpretation (OpenRouter's OpenAI-compatible API); there is no mock or
scripted fallback behind them. LOG_LEVEL feeds the structured-logging setup
(server.setup_logging) — a first-class v2 requirement.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Runtime settings, resolved once from the environment."""

    # --- WebSocket hub ---
    ws_host: str = field(default_factory=lambda: os.getenv("WS_HOST", "0.0.0.0"))
    ws_port: int = field(default_factory=lambda: int(os.getenv("WS_PORT", "8000")))

    # --- LLM via OpenRouter (OpenAI-compatible; LLM-only, no fallback) ---
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    openrouter_base_url: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    #: Any OpenRouter model id.
    openrouter_model: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b")
    )
    #: Cap the per-request token reservation. Left uncapped, OpenRouter reserves
    #: the model max (~65k) and a low-credit key hits HTTP 402. 2500 is plenty
    #: to interpret a goal into a small structured intent.
    openrouter_max_tokens: int = field(
        default_factory=lambda: int(os.getenv("OPENROUTER_MAX_TOKENS", "2500"))
    )

    # --- Household constraint store ---
    #: Where the household constraint store lives (v6). Empty = the repo's seed,
    #: `data/memory/family_profile.json`.
    #:
    #: THE DEVICE'S `--data` EQUIVALENT. Capture (v6-M4) WRITES to this file when a
    #: user confirms a rule, so a demo run dirties the repo's seed — while the device
    #: has run against a scratch world for milestones. Point this at a copy and the
    #: seed stays pristine; a path that does not exist yet is seeded FROM the seed on
    #: first use, exactly like `ProgramHelpers.EnsureDataDir` on the device.
    profile_path: str = field(default_factory=lambda: os.getenv("GOALFLOW_PROFILE_PATH", ""))

    # --- Structured logging ---
    #: Standard logging level name (DEBUG/INFO/WARNING/ERROR).
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))


def get_settings() -> Settings:
    """Return process-wide settings.

    TODO(v2-M1): cache (functools.lru_cache) once server startup wiring lands.
    """
    return Settings()
