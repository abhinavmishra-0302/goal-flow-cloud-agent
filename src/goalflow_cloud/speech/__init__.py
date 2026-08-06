"""Speech (v11) — the cloud's voice, via fish.audio.

The cloud composes WHAT to say (``graph.nodes``), mints an utterance for it here, and
serves the audio over HTTP; a UI plays it. The API key never leaves this process.

Two rules govern everything in this package:

1. **Speech is a decoration on a working flow, and fails SILENTLY.** Everywhere else in
   this repo the design is LLM-only and fails loudly — an interpretation that cannot run
   must not pretend. A voice-over is the opposite: the understanding gate is complete,
   legible and actionable with no audio at all, so a missing key, a fish.audio outage or
   a 429 must cost the demo nothing. No key ⇒ the feature is simply off and no ``speech``
   frame is ever sent. A failed synthesis ⇒ the HTTP fetch fails and the UI stays quiet.
2. **Synthesis happens on the GET, not on the frame.** The frame carries a URL, not
   bytes. Nothing about the understanding gate waits on fish.audio, so v8's latency work
   is untouched: the card renders exactly as fast as it did before, and the audio catches
   up. It also means an utterance nobody plays is never paid for.
"""

from goalflow_cloud.speech.client import (
    SpeechUnavailable,
    speech_enabled,
    stream_utterance,
)
from goalflow_cloud.speech.utterances import (
    Utterance,
    lookup_utterance,
    mint_utterance,
    utterance_id_for,
)

__all__ = [
    "SpeechUnavailable",
    "Utterance",
    "lookup_utterance",
    "mint_utterance",
    "speech_enabled",
    "stream_utterance",
    "utterance_id_for",
]
