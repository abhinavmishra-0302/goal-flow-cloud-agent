"""gate 36 — one voice, however many surfaces are watching.

Run:  .venv/bin/python scripts/verify_speech_fanout.py     (no browser, no network)

WHY THIS EXISTS. UI sockets are deliberately never evicted: one-socket-per-role once
caused a mutual-eviction storm between any two ui clients, and mirroring the same plan
to a board and a chat at once is a feature, not an accident.

Audio is the one frame where that generosity is wrong. There is a single speaker in the
kitchen, so a second surface playing the same utterance is not a second view of it — it
is an echo, half a second behind.

This was REPORTED FROM A TIZEN HUB and was not reproducible on Ubuntu, which is exactly
what a surface-lifetime bug looks like: the dev surrogate keys its iframe on goal_id and
tears the old document down, so only one chat webview is ever alive. On the Hub the
webview belongs to native Bixby, a backgrounded EWK webview can stay alive and connected,
and a new one binds beside it. The chat UI cannot fix this from inside — it dedupes
utterance ids within a DOCUMENT, and these are two documents.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud import server  # noqa: E402

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    if not ok:
        failures.append(what)


class FakeSocket:
    """A ui socket that just remembers what it was sent, in order."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.client_state = None
        self.closed: tuple[int, str] | None = None

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    def types(self) -> list[str]:
        # `devices` is bind-time chatter every ui gets; it says nothing about routing.
        return [f.get("type") for f in self.sent if f.get("type") != "devices"]


SPEECH = {
    "type": "speech",
    "goal_id": "g1",
    "payload": {"utterance_id": "u1", "cue": "understanding", "text": "hi", "url": "/speech/u1.mp3"},
}
PLAN = {"type": "present_plan", "goal_id": "g1", "payload": {}}


async def run() -> None:
    registry = server.ConnectionRegistry()
    dev = "hub-a"

    old_chat = FakeSocket()   # the Hub's previous webview, still alive and still bound
    new_chat = FakeSocket()   # the one the user is actually looking at
    board = FakeSocket()      # a board UI in the same home

    await registry._bind_ui(old_chat, dev, ack=False, surface="chat")
    await registry._bind_ui(new_chat, dev, ack=False, surface="chat")
    await registry._bind_ui(board, dev, ack=False, surface="board")

    await registry.send_to_uis(dev, SPEECH)

    # v11.9 — the second chat bind EVICTS the first. This is the fix for the real bug:
    # a Hub keeps the previous webview alive and bound, the log said chat_surfaces=2 on
    # every tap, and a stale surface holding its own audio element is not made safe by
    # being ignored. 1012 is the code the chat UI already understands as "you have been
    # replaced" and deliberately does not reconnect from.
    check(old_chat.closed is not None and old_chat.closed[0] == 1012,
          f"binding a second chat webview must CLOSE the first with 1012 — got "
          f"{old_chat.closed}")
    check(old_chat not in registry._session(dev).uis,
          "and drop it from the session, so nothing is routed to it at all")

    check(new_chat.types() == ["speech"],
          f"the NEWEST chat surface speaks — got {new_chat.types()}")
    check(old_chat.types() == [],
          f"the evicted webview receives nothing — got {old_chat.types()}")
    check(board.types() == [],
          f"a board surface has no business speaking — got {board.types()}")

    # The eviction is narrow ON PURPOSE: same device, same `chat` surface. Ui sockets
    # being un-evictable is a rule worth keeping — it exists because evicting by ROLE
    # made a board and a chat fight each other — so a board in the same home must be
    # untouched and must still receive everything.
    check(board.closed is None, "a board surface is never evicted by a chat binding")
    await registry.send_to_uis(dev, PLAN)
    check("present_plan" in board.types(),
          "the board still mirrors non-speech frames — that is the feature the "
          "never-evict rule exists for")
    check("present_plan" in new_chat.types(), "and so does the live chat")

    # A home with a board and no chat: speech has nowhere to go, and that is correct
    # rather than an error. It must not fall back to "send it to whoever is left".
    solo = server.ConnectionRegistry()
    only_board = FakeSocket()
    await solo._bind_ui(only_board, dev, ack=False, surface="board")
    await solo.send_to_uis(dev, SPEECH)
    check(only_board.types() == [],
          f"with no chat surface, speech is not sent at all — got {only_board.types()}")


async def replay_policy() -> None:
    """v11.8 — only a QUESTION is worth replaying to a surface that binds mid-phase.

    Reported from a Tizen Hub: the approvals screen speaks once, the user presses
    Approve, and the approvals line plays again. Whatever replaced the document on that
    tap landed on a create phase with a cached plan, and the replay set handed it the
    plan and approvals audio a second time.

    The asymmetry is the whole rule. `understanding` stops the run and will not move
    until it is answered — a surface that binds while it is up must hear it. `plan` and
    `approvals` describe a screen that is fully rendered and readable; re-narrating them
    to someone already looking at them is noise at best, and at worst a voice that seems
    to restart itself when a button is pressed.
    """
    check(server.REPLAYABLE_CUES == frozenset({"understanding"}),
          f"only the QUESTION is replayable — got {sorted(server.REPLAYABLE_CUES)}. plan "
          f"and approvals describe a screen the binding surface can already read, and "
          f"replaying them is the reported Tizen repeat")
    for cue in ("working_start", "working_plan", "saved"):
        check(cue not in server.REPLAYABLE_CUES,
              f"{cue!r} must never be replayable — progress replayed is a voice "
              f"describing the past, and `saved` replayed is a promise about a surface "
              f"that is closing")


asyncio.run(run())
asyncio.run(replay_policy())
for f in failures:
    print(f"  FAIL {f}")
print(f"gate 36 (speech fan-out): {'FAIL: %d' % len(failures) if failures else 'PASS'}")
sys.exit(1 if failures else 0)
