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
    text: str
    #: What moment this speaks for — "understanding" today. Carried so a UI can decide
    #: whether it still wants to hear it (a gate answered before the audio arrives).
    cue: str
    goal_id: str = ""
    #: The complete mp3, populated after the first successful synthesis. Empty until
    #: then, and left empty if synthesis fails — a partial body is never cached.
    audio: bytes = field(default=b"", repr=False)


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


def mint_utterance(goal_id: str, cue: str, text: str) -> Utterance:
    """Register ``text`` as speakable and return it. Re-minting is idempotent.

    Re-minting the same (goal, cue) with the SAME text keeps the cached audio; with
    different text it replaces the entry, because the sentence has changed and the old
    bytes now say something untrue. That happens for real: confirming a captured rule
    at the gate re-resolves the constraints behind it.
    """
    key = utterance_id_for(goal_id, cue)
    existing = _utterances.get(key)
    if existing is not None and existing.text == text:
        _utterances.move_to_end(key)
        return existing
    utterance = Utterance(id=key, text=text, cue=cue, goal_id=goal_id)
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
