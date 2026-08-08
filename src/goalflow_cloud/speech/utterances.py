"""The utterance registry — what a ``/speech/<id>.mp3`` URL actually resolves to.

A ``speech`` frame carries an id and a URL, never the text-to-synthesize as a query
parameter. Two reasons, and the second is the real one:

- a URL is short, and a spoken paragraph in a query string is not;
- **the URL must not be a synthesis oracle.** Anything that can reach the hub's HTTP
  port could otherwise spend the account's credits on text of its own choosing. Only
  text the cloud itself minted is reachable, and only by an id it handed out.

Bytes are cached against the id once a synthesis completes, so the re-GET a webview
remount produces (the surrogate re-keys its iframe per goal) is free rather than a
second billed call.

Bounded and process-local. A create phase is minutes long and an utterance is dead the
moment its gate is answered, so there is nothing here worth persisting across a restart
— unlike the graph checkpoint, which is exactly why THAT is on SQLite and this is not.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: How many utterances stay resolvable. Each create phase mints one; the cap is a
#: backstop against an unbounded dict in a long-lived process, not a tuning knob.
MAX_UTTERANCES = 64


@dataclass
class Utterance:
    """One thing the cloud is prepared to say, and the audio for it once made."""

    id: str
    #: The WORDS, clean — no emotion tags. This is what goes on the wire as
    #: `speech.payload.text`: the caption, the screen-reader string, and the only thing
    #: left when synthesis fails. "[warm] Here's what I understood" is none of those.
    text: str
    #: What moment this speaks for. Carried so a UI can decide whether it still wants to
    #: hear it (a gate answered before the audio arrives).
    cue: str
    goal_id: str = ""
    #: v11.1: what is actually SENT TO fish.audio — `text` with this cue's emotion cue
    #: prefixed. Kept separate rather than stripped on the way out because the split is
    #: the invariant: two fields that must differ are safer than one field two callers
    #: disagree about. Empty falls back to `text`.
    spoken: str = ""
    #: The complete mp3, populated after the first successful synthesis. Empty until
    #: then, and left empty if synthesis fails — a partial body is never cached.
    audio: bytes = field(default=b"", repr=False)
    #: Set while a synthesis for THIS utterance is in flight, and fired when it ends.
    #:
    #: v11.2, and it exists to stop us paying twice. Chunks are warmed in the background
    #: the moment a cue is emitted, and the UI fetches the first one a round trip later —
    #: so without this the warm and the fetch synthesize the same sentence concurrently,
    #: billing two calls and racing to fill the same field. The fetch now waits on the
    #: warm instead.
    inflight: asyncio.Event | None = field(default=None, repr=False)

    def to_synthesize(self) -> str:
        """What fish.audio should receive."""
        return self.spoken or self.text


#: id -> Utterance, oldest first.
_utterances: OrderedDict[str, Utterance] = OrderedDict()


def utterance_id_for(goal_id: str, cue: str) -> str:
    """The id for a (goal, cue) pair — DETERMINISTIC, deliberately.

    A create-phase replay re-sends the cached ``speech`` frame to a webview that bound
    late, and a goal re-entering its gate must resolve to the same audio it was already
    promised. A random id would mint a second utterance for the same sentence and pay
    for it twice.
    """
    return f"u-{goal_id}-{cue}"


def mint_utterance(goal_id: str, cue: str, text: str, spoken: str = "") -> Utterance:
    """Register ``text`` as speakable and return it. Re-minting is idempotent.

    Re-minting the same (goal, cue) with the SAME text keeps the cached audio; with
    different text it replaces the entry, because the sentence has changed and the old
    bytes now say something untrue. That happens for real: confirming a captured rule
    at the gate re-resolves the constraints behind it.

    ``text`` is the clean caption; ``spoken`` is the tagged variant for fish.audio.
    """
    key = utterance_id_for(goal_id, cue)
    existing = _utterances.get(key)
    if existing is not None and existing.text == text:
        _utterances.move_to_end(key)
        return existing
    utterance = Utterance(id=key, text=text, cue=cue, goal_id=goal_id, spoken=spoken)
    _utterances[key] = utterance
    _utterances.move_to_end(key)
    while len(_utterances) > MAX_UTTERANCES:
        dropped, _ = _utterances.popitem(last=False)
        logger.debug("utterance_evicted id=%s", dropped)
    return utterance


def lookup_utterance(utterance_id: str) -> Utterance | None:
    """Resolve an id from a URL. ``None`` is a 404 — see the module docstring."""
    return _utterances.get(utterance_id)


def reset_utterances() -> None:
    """Drop everything. For gates; nothing in the running hub calls this."""
    _utterances.clear()
