"""Environment-backed configuration for the cloud agent.

DESIGN STUB — no logic beyond trivial env reads. See .env.example for the
documented variable set.

M1 needs only WS_HOST / WS_PORT. The OPENROUTER_* settings are M2 (LLM via
OpenRouter's OpenAI-compatible API, with a mock fallback behind the same
interface).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Runtime settings, resolved once from the environment."""

    # --- WebSocket hub (M1) ---
    ws_host: str = field(default_factory=lambda: os.getenv("WS_HOST", "0.0.0.0"))
    ws_port: int = field(default_factory=lambda: int(os.getenv("WS_PORT", "8000")))

    # --- LLM via OpenRouter (M2) ---
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    openrouter_base_url: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    )
    # Any OpenRouter model id; configurable, defaults to Claude Sonnet.
    openrouter_model: str = field(
        default_factory=lambda: os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
    )


def get_settings() -> Settings:
    """Return process-wide settings.

    TODO(M1): decide whether to cache (functools.lru_cache) once server
    startup wiring is implemented.
    """
    return Settings()
