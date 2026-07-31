"""v7 gate — the create-phase webview outlasts the work it claims to be doing.

Run:  python scripts/verify_close_hold.py    (no API key needed — no LLM on this path)

WHY THIS EXISTS. The chat surface says "Saving your plan to the device AI board", and then
"Saving this goal, and updating your other goals…". Both sentences are promises about
something that has not finished yet, and for the whole of v7 the cloud closed the webview
within a round-trip of the approval — a few tens of milliseconds. The screen existed in the
code and never existed on screen. The user's report was simply "it seems the chat UI closes
abruptly", which is precisely what a promise with no dwell looks like.

The dwell CANNOT live in the chat UI, and that is the subtle part this gate pins down.
Bixby hosts the chat in a webview and unmounts it the instant `chat_ui_close` arrives, so a
hold implemented inside the iframe is a hold nobody sees — the component politely keeps
rendering a screen that is no longer on the display. The cloud owns the bracket, so the
cloud owns the dwell.

Three properties, and the third is the one with teeth:

  * an ordinary approval (nothing else to update) still holds for the dwell floor, so the
    hand-off is a moment rather than a flicker;
  * an approval that moved the household holds until the OTHER goal reports back — the
    user watches the sentence naming their meal plan, then finds the meal plan already
    changed when they reach the board;
  * a device that never answers does not strand them: the wait is bounded, the webview
    closes anyway, and the change still lands on the board whenever it arrives.

Timings are scaled down by monkeypatching the module's constants — the gate asserts the
ORDERING and the BOUND, never the wall-clock numbers, which are a presentation choice.
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


class Recorder:
    """Stands in for the registry: records when the close actually went out."""

    def __init__(self) -> None:
        self.closed_at: float | None = None

    async def close(self, device_id: str, goal_id: str) -> None:
        self.closed_at = asyncio.get_running_loop().time()


async def run() -> None:
    recorder = Recorder()
    server.emit_chat_ui_close = recorder.close  # type: ignore[assignment]
    # Scaled down so the gate runs in under a second. The gate asserts orderings against
    # these, never against the shipped values.
    server.SAVE_DWELL_S = 0.30
    server.CROSS_GOAL_WAIT_S = 0.60

    # 1. NOTHING TO WAIT FOR — the floor still applies.
    server.crossgoal_waiters.clear()
    start = asyncio.get_running_loop().time()
    await server._close_when_saved("dev", "goal-1", [])
    held = (recorder.closed_at or start) - start
    check(held >= 0.30 * 0.9,
          f"an approval with nothing to update must still hold for the dwell floor, held {held:.3f}s")
    check(held < 0.60,
          f"...and must not wait on a cross-goal that was never pushed, held {held:.3f}s")

    # 2. THE CROSS-GOAL CASE — the close waits for the other goal to report back.
    #    The other goal answers at 0.40s, comfortably past the floor, so a close at ~0.40s
    #    can only mean it waited for the answer rather than for the floor.
    recorder.closed_at = None
    server.crossgoal_waiters.clear()
    answered = asyncio.Event()
    server.crossgoal_waiters["goal-2"] = answered

    async def answer_late() -> None:
        # Held by reference, not looked up at fire time: a gate that raises KeyError when
        # the code under test drops the waiter is reporting its own bug, not the code's.
        await asyncio.sleep(0.40)
        answered.set()

    start = asyncio.get_running_loop().time()
    await asyncio.gather(server._close_when_saved("dev", "goal-1", ["goal-2"]), answer_late())
    held = (recorder.closed_at or start) - start
    check(held >= 0.40 * 0.9,
          f"the webview must stay open until the other goal has actually re-planned, held {held:.3f}s")
    check(held < 0.60,
          f"...and must close as soon as it has, not sit out the timeout, held {held:.3f}s")
    check("goal-2" not in server.crossgoal_waiters,
          "a satisfied waiter must be cleaned up, or the next fan-out inherits a set event "
          "and closes the webview instantly")

    # 3. A DEVICE THAT NEVER ANSWERS. The bound is the whole point: a spinner with no exit
    #    is worse than a board that catches up a minute late.
    recorder.closed_at = None
    server.crossgoal_waiters.clear()
    server.crossgoal_waiters["goal-3"] = asyncio.Event()  # never set
    start = asyncio.get_running_loop().time()
    await server._close_when_saved("dev", "goal-1", ["goal-3"])
    held = (recorder.closed_at or start) - start
    check(recorder.closed_at is not None,
          "a device that never answers must not leave the webview open forever")
    check(0.60 * 0.9 <= held < 0.60 * 2,
          f"the wait must be bounded by CROSS_GOAL_WAIT_S, held {held:.3f}s")
    check("goal-3" not in server.crossgoal_waiters,
          "a timed-out waiter must be cleaned up too — it is the same leak")


def main() -> int:
    asyncio.run(run())
    for f in failures:
        print(f"  FAIL {f}")
    print("gate 18 (the webview outlasts the save): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
