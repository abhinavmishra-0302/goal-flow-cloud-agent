"""v7.1 gate — a refusal reaches the webview it was opened for.

Run:  python scripts/verify_refusal_replay.py   (no API key needed — no LLM on this path)

WHY THIS EXISTS. v7 gave the refusal a surface: ask the fridge for a new apartment and the
chat opens, says it cannot help, and closes itself a few seconds later. It shipped, and
what the user actually saw was a BLANK panel appear and vanish.

The bug is a race that no single-frame reading of the code can show. A refusal is the one
create phase with no round-trip in it — the cloud emits `chat_ui_open` and broadcasts the
`notice` in the same breath. But `chat_ui_open` is the frame that makes Bixby MOUNT the
iframe; the chat UI's socket connects hundreds of milliseconds later, by which time the
only frame the phase will ever have has been broadcast to nobody. Every other create phase
survives this because it has an LLM call in it: by the time an `understanding` is computed
the webview has long since bound.

So the notice joins the create-phase replay cache, and this gate is the difference between
the two orderings — connect BEFORE the refusal, and connect AFTER it. Both must paint it.
The third case is the one that keeps the cache honest: `updating_goals` is a caption on a
screen that is already up, and replaying it to a socket that binds later would caption
nothing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud import server  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


class FakeSocket:
    """A ui socket that just remembers what it was sent, in order."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.client_state = None

    async def send_json(self, frame: dict) -> None:
        self.sent.append(frame)

    def types(self) -> list[str]:
        return [f.get("type") for f in self.sent]


REFUSAL = {
    "type": "notice",
    "goal_id": "g-apartment",
    "kind": "out_of_scope",
    "message": "That's outside what I do. I'm your home goal assistant.",
}


async def run() -> None:
    registry = server.ConnectionRegistry()
    dev = "hub-a"

    # --- 1. CONNECT LATE: the webview binds AFTER the refusal was broadcast. ---
    #
    # This is the real ordering on a Hub. Nobody was listening when the notice went out.
    registry.open_create_phase(dev, "g-apartment")
    registry.capture_notice(dev, "g-apartment", REFUSAL)

    late = FakeSocket()
    await registry._replay_create_phase(late, registry._session(dev).create_phase)
    check("notice" in late.types(),
          f"a webview that binds after the refusal is replayed the refusal — got {late.types()}")
    check(late.types()[0] == "chat_ui_open",
          f"the reset still lands first, or the notice is torn down by it — got {late.types()}")
    check(late.sent[-1].get("message") == REFUSAL["message"],
          "the replayed notice carries the refusal's own words, not a placeholder")

    # --- 2. A TERMINAL NOTICE IS SENT ALONE. ---
    #
    # The chat UI's reducer clears the stage on a terminal notice, so replaying an
    # understanding underneath one only paints a screen the next frame tears down.
    registry.capture_understanding(dev, "g-apartment", {"type": "understanding", "goal_id": "g-apartment"})
    both = FakeSocket()
    await registry._replay_create_phase(both, registry._session(dev).create_phase)
    check(both.types() == ["chat_ui_open", "notice"],
          f"a cached refusal wins outright over anything else cached — got {both.types()}")

    # --- 3. `updating_goals` IS NOT TERMINAL and must never be cached. ---
    #
    # It captions the saving screen while the cloud rewrites the user's OTHER goals. A
    # socket that binds afterwards would be handed a caption with nothing under it — and
    # worse, the chat UI would treat the replayed frame as the whole create phase.
    registry.open_create_phase(dev, "g-away")
    registry.capture_notice(dev, "g-away", {
        "type": "notice", "goal_id": "g-away", "kind": "updating_goals",
        "message": "Updating Weekly meal plan — you're away Thu & Fri.",
    })
    check(registry._session(dev).create_phase.get("notice") is None,
          "a mid-save updating_goals notice is NOT cached — it captions a screen, it is not one")

    # --- 4. THE CACHE IS STILL GOAL-SCOPED. ---
    #
    # A notice for a goal that is no longer the create-phase goal must not overwrite the
    # phase that superseded it, or a stale refusal reappears on the next goal's webview.
    registry.capture_notice(dev, "g-apartment", REFUSAL)
    check(registry._session(dev).create_phase.get("notice") is None,
          "a notice for a superseded goal does not land in the current phase's cache")

    # --- 4b. AN ANSWERED GATE IS NEVER REPLAYED. ---
    #
    # The bug this pins: the understanding stayed cached from the moment it was sent
    # until a plan replaced it, and confirming the gate did not count as replacing it.
    # Planning is the longest stretch of the run, so any webview that reconnected during
    # it was handed back the confirmation card the user had already answered — the
    # surface jumping from "working" to a settled gate. Seen on a Tizen Hub, where the
    # webview drops far more readily than on a dev box; the code did it everywhere.
    registry.open_create_phase(dev, "g-meal", "Plan my weekly meal.")
    understanding = {"type": "understanding", "goal_id": "g-meal", "payload": {"objective": "…"}}
    registry.capture_understanding(dev, "g-meal", understanding)

    before = FakeSocket()
    await registry._replay_create_phase(before, registry._session(dev).create_phase)
    check("understanding" in before.types(),
          f"BEFORE the gate is answered, a reconnect still gets it — got {before.types()}")

    registry.resolve_understanding(dev, "g-meal")
    during = FakeSocket()
    await registry._replay_create_phase(during, registry._session(dev).create_phase)
    check("understanding" not in during.types(),
          f"once ANSWERED it is never replayed — got {during.types()}")
    check(during.types() == ["chat_ui_open"],
          f"a mid-planning reconnect rejoins the WORK, nothing else — got {during.types()}")

    # ...and the plan still reaches a socket that binds later, or the fix would have
    # traded a stale gate for a webview that never catches up at all.
    plan = {"type": "present_plan", "goal_id": "g-meal", "payload": {"plan": []}}
    registry.capture_present_plan(dev, "g-meal", plan)
    after = FakeSocket()
    await registry._replay_create_phase(after, registry._session(dev).create_phase)
    check(after.types() == ["chat_ui_open", "present_plan"],
          f"the finished plan is still replayed — got {after.types()}")

    # Resolving a goal that is NOT the create-phase goal must not blank the live one.
    registry.open_create_phase(dev, "g-other", "Something else.")
    registry.capture_understanding(dev, "g-other", {"type": "understanding", "goal_id": "g-other"})
    registry.resolve_understanding(dev, "g-meal")
    check(registry._session(dev).create_phase.get("understanding") is not None,
          "resolving a superseded goal's gate leaves the CURRENT goal's gate cached")

    # --- 5. CLOSING THE BRACKET DROPS IT. ---
    #
    # Otherwise a webview that binds later — for any reason — is shown a refusal to a
    # question nobody just asked.
    registry.open_create_phase(dev, "g-apartment")
    registry.capture_notice(dev, "g-apartment", REFUSAL)
    check(registry.clear_create_phase(dev, "g-apartment"), "the close matched its own goal")
    check(registry._session(dev).create_phase is None,
          "the cached refusal is gone once the bracket closes")


def main() -> int:
    asyncio.run(run())
    for f in failures:
        print(f"  FAIL {f}")
    print("gate 27 (a refusal reaches its webview): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
