"""The fish.audio TTS client — the one place this process talks to fish.audio.

``POST https://api.fish.audio/v1/tts`` with a bearer key and the model in a HEADER
(not the body — that is easy to get wrong, and a wrong model header is a 4xx, not a
default). The response is a chunked audio stream, which we hand straight to the client
socket: the browser starts playing on the first chunk rather than after the last, so
time-to-first-audio is fish's, not fish's plus the whole file.

Nothing here raises into the request path by accident — see ``SpeechUnavailable`` and
rule 1 in the package docstring.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from goalflow_cloud.config import get_settings

logger = logging.getLogger(__name__)


class SpeechUnavailable(RuntimeError):
    """Synthesis could not be done. Callers turn this into silence, never a crash."""


#: One shared HTTP client for every fish.audio call, for the same reason
#: ``graph.nodes`` keeps one for OpenRouter: a per-call client pays a fresh TCP+TLS
#: handshake to a host we are about to talk to again. Created lazily so importing this
#: module never opens a socket — the offline gates import it constantly.
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            # Generous: a long sentence on a busy account can take a few seconds, and a
            # timeout here costs the demo its voice rather than saving it anything.
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
    return _http_client


async def aclose() -> None:
    """Close the shared client. Called from the hub's shutdown hook."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


#: What counts as "off" in SPEECH_ENABLED. Spelled out rather than `== "false"` because
#: the value is typed by a human into a .env at speed, and being pedantic about which of
#: `0` / `no` / `off` / `False` they picked would just be a silently-still-talking demo.
_OFF_WORDS = frozenset({"false", "0", "off", "no"})


def speech_off_reason() -> str:
    """Why the voice is silent, or "" when it is not.

    Two reasons, and they are DIFFERENT — which is the whole point of returning a string
    rather than a bool. "no key" is an environment that was never set up; "switched off"
    is a deliberate choice someone made and may have forgotten. A single "off" would
    leave the developer who set SPEECH_ENABLED=false last week hunting a key that is
    sitting right there.
    """
    settings = get_settings()
    if settings.speech_enabled.strip().lower() in _OFF_WORDS:
        return "switched off (SPEECH_ENABLED)"
    if not settings.fish_api_key.strip():
        return "no FISH_API_KEY"
    return ""


def speech_enabled() -> bool:
    """Can this process speak? Off ⇒ no ``speech`` frame is ever sent ⇒ every UI renders
    exactly as it did in v10."""
    return not speech_off_reason()


def describe_speech() -> str:
    """One line for startup logging: what the voice is, or WHY there isn't one.

    The "why" carries the weight. Silence is a legal state here, so the log is the only
    thing standing between a deliberately quiet run and twenty minutes of debugging a
    feature that is working exactly as configured.
    """
    settings = get_settings()
    reason = speech_off_reason()
    if reason:
        return f"off — {reason}"
    voice = settings.fish_reference_id.strip() or "default voice"
    return f"model={settings.fish_model} voice={voice} format={settings.fish_format}"


def _request_body(text: str) -> dict[str, Any]:
    settings = get_settings()
    body: dict[str, Any] = {
        "text": text,
        "format": settings.fish_format,
        # low | normal | balanced. `normal` buys quality at the cost of first-chunk
        # latency; `balanced` is the voice-agent setting and this IS one.
        "latency": settings.fish_latency,
        # Expand numbers/dates into words before synthesis. Our text carries dates and
        # counts ("3 household rules", "Aug 4"), and unnormalized they are read as
        # digits or spelled out character by character depending on the model's mood.
        "normalize": True,
    }
    if settings.fish_format == "mp3":
        body["mp3_bitrate"] = settings.fish_mp3_bitrate
    reference_id = settings.fish_reference_id.strip()
    if reference_id:
        # Absent, fish uses its own default voice — which is a fine demo voice and one
        # less thing to configure. Only sent when chosen.
        body["reference_id"] = reference_id
    return body


async def stream_utterance(text: str) -> AsyncIterator[bytes]:
    """Synthesize ``text`` and yield audio chunks as they arrive.

    Raises ``SpeechUnavailable`` before yielding anything if there is no key or fish
    answers with an error status. Once the first chunk is yielded the response is
    committed, so a mid-stream failure truncates the audio rather than reporting — the
    listener hears a sentence cut short, which is the honest outcome of a dropped
    connection and is not worth a second code path.
    """
    settings = get_settings()
    # Checked HERE too, not just at the frame: a UI holding a URL from before the switch
    # was flipped would otherwise still be able to make this process synthesize.
    off = speech_off_reason()
    if off:
        raise SpeechUnavailable(off)
    key = settings.fish_api_key.strip()

    url = f"{settings.fish_base_url.rstrip('/')}/v1/tts"
    headers = {
        "Authorization": f"Bearer {key}",
        # THE MODEL GOES IN A HEADER. It is not a body field, and putting it in the
        # body silently gets you the account default instead of the model you asked for.
        "model": settings.fish_model,
        "Content-Type": "application/json",
    }

    started = time.perf_counter()
    first_chunk_at: float | None = None
    total = 0
    try:
        async with _client().stream(
            "POST", url, headers=headers, json=_request_body(text)
        ) as response:
            if response.status_code >= 400:
                # Read the body for the log — a fish 4xx says WHY (bad key, unknown
                # reference_id, quota), and without it every failure looks the same.
                detail = (await response.aread()).decode("utf-8", "replace")[:400]
                raise SpeechUnavailable(f"fish.audio HTTP {response.status_code}: {detail}")
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                if first_chunk_at is None:
                    first_chunk_at = time.perf_counter()
                total += len(chunk)
                yield chunk
    except httpx.HTTPError as exc:
        raise SpeechUnavailable(f"fish.audio transport error: {exc}") from exc
    finally:
        # TTFA is the number that decides whether this feels like a voice or a buffer,
        # so it is logged separately from total elapsed — exactly the split that made
        # the v8 latency work possible on the LLM side.
        ttfa = int((first_chunk_at - started) * 1000) if first_chunk_at else -1
        logger.info(
            "tts_call site=fish ttfa_ms=%d elapsed_ms=%d bytes=%d chars=%d",
            ttfa,
            (time.perf_counter() - started) * 1000,
            total,
            len(text),
        )
