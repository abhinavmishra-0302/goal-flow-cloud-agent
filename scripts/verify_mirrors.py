"""M6 gate — the contract mirrors have not drifted.

Run:  python scripts/verify_mirrors.py     (no API key needed)

WHY THIS EXISTS. CONTRACT.md is canonical and four repos mirror it, but there is no
cross-repo atomic commit — so a half-mirrored change breaks at RUNTIME, not at build
time, and the failure is silent:

  * the Python mirror types `event` as a Literal. An unlisted value fails validation
    and the frame is DROPPED. That really happened: `task_update` was added to
    CONTRACT.md and the C# mirror but not here, and every one of the device's 35
    task_update frames was rejected. The board sat at 0% with no error anywhere.
  * the chat UI's ws.ts has an INBOUND_TYPES allowlist that drops unknown frames
    just as quietly.

Neither the compiler, the type checker, nor any per-repo test can see this: each
repo is internally consistent while the SYSTEM is broken. Only comparing the mirrors
to the canonical file can.

This checks the machine-readable parts — frame `type` values and agent_event kinds.
It is a floor, not a proof of full agreement.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIBLINGS = ROOT.parent
CONTRACT = ROOT / "CONTRACT.md"
PY_MIRROR = ROOT / "src/goalflow_cloud/models/contract.py"
CS_MIRRORS = sorted((SIBLINGS / "goal-flow-device-agent-ubuntu/src/GoalFlow.Device/Contracts").glob("*.cs"))
TS_MIRRORS = [
    SIBLINGS / "goal-flow-agent-chat-ui/src/types/contract.ts",
    SIBLINGS / "goal-flow-agent-board-ui/src/types/contract.ts",
]
WS_ALLOWLIST = SIBLINGS / "goal-flow-agent-chat-ui/src/lib/ws.ts"

#: Frames the DEVICE neither sends nor receives — it has no reason to know them.
#: The board is a cloud↔ui concern; the device is upstream of it and never sees one.
DEVICE_EXEMPT = {
    "board_snapshot", "board_update", "board_get", "goal_state_get", "goal_accepted",
    "devices", "select_device", "user_goal", "understanding", "understanding_response",
    "present_plan", "notice",
}

#: Frames a UI never handles inbound (ui→cloud only, or device↔cloud only).
UI_INBOUND_EXEMPT = {
    "hello", "user_goal", "understanding_response", "approval", "control",
    "select_device", "board_get", "goal_state_get", "dispatch", "plan_ready",
}


def contract_types() -> set[str]:
    """Every frame `type` the canonical file defines, from its JSON examples."""
    return set(re.findall(r'"type"\s*:\s*"([a-z_]+)"', CONTRACT.read_text()))


def contract_event_kinds() -> set[str]:
    """agent_event kinds, from the canonical `"event": a | b | c` line."""
    text = CONTRACT.read_text()
    m = re.search(r'"event"\s*:\s*((?:"[a-z_]+"\s*\|?\s*)+)', text)
    return set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()


def main() -> int:
    failures: list[str] = []
    types = contract_types()
    kinds = contract_event_kinds()
    print(f"  CONTRACT.md defines {len(types)} frame types, {len(kinds)} agent_event kinds")

    # --- Python ---
    py = PY_MIRROR.read_text()
    for t in sorted(types):
        if f'"{t}"' not in py:
            failures.append(f"python mirror is missing frame type {t!r}")
    for k in sorted(kinds):
        if f'"{k}"' not in py:
            failures.append(f"python AgentEventKind is missing {k!r} — frames WILL be dropped at validation")

    # --- C# (device) ---
    cs = "\n".join(p.read_text() for p in CS_MIRRORS)
    for t in sorted(types - DEVICE_EXEMPT):
        if f'"{t}"' not in cs:
            failures.append(f"C# mirror is missing frame type {t!r}")
    for k in sorted(kinds):
        if f'"{k}"' not in cs:
            failures.append(f"C# is missing agent_event kind {k!r}")

    # --- TypeScript ---
    for ts in TS_MIRRORS:
        if not ts.exists():
            print(f"  (skipped {ts.parent.parent.parent.name} — not created yet)")
            continue
        text = ts.read_text()
        for t in sorted(types):
            if f'"{t}"' not in text:
                failures.append(f"{ts.parent.parent.parent.name} contract.ts is missing {t!r}")

    # --- the silent dropper ---
    if WS_ALLOWLIST.exists():
        allow = WS_ALLOWLIST.read_text()
        m = re.search(r"INBOUND_TYPES[^=]*=\s*(?:new Set\()?\[([^\]]+)\]", allow)
        if m:
            listed = set(re.findall(r'"([a-z_]+)"', m.group(1)))
            for t in sorted(types - UI_INBOUND_EXEMPT - listed):
                failures.append(f"chat-ui ws.ts INBOUND_TYPES is missing {t!r} — it will be SILENTLY DROPPED")
        else:
            failures.append("could not read INBOUND_TYPES out of ws.ts — the allowlist is unchecked")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 14 (contract mirrors): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
