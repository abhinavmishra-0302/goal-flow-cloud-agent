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
    #: v8 — ordered OpenRouter provider preference, e.g. "cerebras". Empty = send no
    #: `provider` field at all, which is what every offline gate assumes.
    #:
    #: WHY THIS EXISTS (measured, v8-M0): with no `provider` field OpenRouter load-balances
    #: across nineteen endpoints whose throughput spans 39x, and it kept landing us on the
    #: slowest tier — CoreWeave at 52 tok/s and Novita at 76, against Cerebras at 1523. The
    #: same compose-shaped task took 50.1s unpinned and 1.5s pinned. The interpretation
    #: window was never really a prompt problem; it was a routing default nobody had set.
    openrouter_provider_order: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_PROVIDER_ORDER", "")
    )
    #: Only read when the order is set. The MECHANISM defaults true, but the demo ships it
    #: FALSE, and the measurement is why: on the real pipeline Cerebras plans a goal in
    #: 8-10s and the next-best provider takes 203-234s — slower than sending no preference
    #: at all. A fallback here is not graceful degradation, it is a silent four-minute
    #: stall, so a run that cannot have Cerebras should fail visibly and be re-run.
    openrouter_provider_allow_fallbacks: bool = field(
        default_factory=lambda: os.getenv("OPENROUTER_PROVIDER_ALLOW_FALLBACKS", "").strip().lower()
        != "false"
    )
    #: v8 — `reasoning_effort` for every cloud call. Empty = never send it, which is the
    #: shipped default.
    #:
    #: DO NOT SET THIS TO "low" WITHOUT RE-MEASURING. v8-M0 benchmarked it on every
    #: provider: reasoning tokens collapse from ~1400 to 26-89 and the model stops being
    #: able to do the job — every `low` run returned an unusable result. And there is
    #: nothing to win, because `medium` and the provider default are within 0.2s of each
    #: other once the provider is fast. The knob is here for a future model, not this one.
    openrouter_reasoning_effort: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_REASONING_EFFORT", "")
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
    """Return process-wide settings, re-read from the environment on every call.

    NOT CACHED, AND THE TODO THAT ASKED FOR IT IS WRONG. v8 tried ``lru_cache`` here — the
    dataclass is rebuilt at every LLM call site, four times per goal — and it broke
    ``verify_capture`` immediately: gates and tests set ``GOALFLOW_PROFILE_PATH`` (and the
    OPENROUTER_* vars) *after* import and expect the next read to see them, which is also how
    the demo's scratch-profile trick works. The saving is twelve ``os.getenv`` calls against a
    round-trip measured in seconds; the cost is a whole class of "why is it still using the
    seed" bugs. Cache this only behind explicit startup wiring that re-reads on change.
    """
    return Settings()
