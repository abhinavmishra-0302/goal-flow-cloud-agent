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
#: Frames the DEVICE neither sends nor receives — it has no reason to know them.
#: The board is a cloud↔ui concern; the device is upstream of it and never sees one.
DEVICE_EXEMPT = {
    "board_snapshot", "board_update", "board_get", "goal_state_get", "goal_accepted",
    "devices", "select_device", "user_goal", "understanding", "understanding_response",
    "present_plan", "notice",
}

#: Frames NO ui ever handles inbound (ui→cloud only, or device↔cloud only).
UI_INBOUND_EXEMPT = {
    "hello", "user_goal", "understanding_response", "approval", "control",
    "select_device", "board_get", "goal_state_get", "dispatch", "plan_ready",
}

#: The two UIs are NOT the same shape, and the gate must not pretend they are.
#:
#: The chat UI is the full protocol surface. The board is a deliberate SLICE: a
#: read-mostly projection that renders the cloud's derived summaries. `types_exempt`
#: for the board is therefore the "board is read-mostly" decision written down as a
#: check — if someone adds `approval` to the board's mirror, this gate fails and asks
#: them to change the decision on purpose rather than by drift.
#:
#: Both allowlists are checked. The board's ws.ts drops unlisted frames exactly like
#: the chat UI's does, and it is the newer file — leaving it unchecked would reopen
#: the very hole this gate was written for.
UIS = [
    {
        "name": "chat-ui",
        "contract": SIBLINGS / "goal-flow-agent-chat-ui/src/types/contract.ts",
        "ws": SIBLINGS / "goal-flow-agent-chat-ui/src/lib/ws.ts",
        "types_exempt": set(),
        "inbound_exempt": UI_INBOUND_EXEMPT,
    },
    {
        "name": "board-ui",
        "contract": SIBLINGS / "goal-flow-agent-board-ui/src/types/contract.ts",
        "ws": SIBLINGS / "goal-flow-agent-board-ui/src/lib/ws.ts",
        # device↔cloud frames, plus the two WRITES the board must never send.
        "types_exempt": {"dispatch", "plan_ready", "control", "approval", "understanding_response"},
        # The board renders derived summaries, not raw per-goal frames. It receives
        # these (the cloud broadcasts to every ui) and deliberately ignores them.
        "inbound_exempt": UI_INBOUND_EXEMPT | {
            "capabilities", "agent_event", "understanding", "present_plan",
            "proposal", "status",
        },
    },
]


def contract_types() -> set[str]:
    """Every frame `type` the canonical file defines, from its JSON examples."""
    return set(re.findall(r'"type"\s*:\s*"([a-z_]+)"', CONTRACT.read_text()))


def contract_event_kinds() -> set[str]:
    """agent_event kinds, from the canonical `"event": a | b | c` line."""
    text = CONTRACT.read_text()
    m = re.search(r'"event"\s*:\s*((?:"[a-z_]+"\s*\|?\s*)+)', text)
    return set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()


def contract_task_states() -> set[str]:
    """The documented `task_update.state` enumeration."""
    text = CONTRACT.read_text()
    m = re.search(r"`state` is one of.*?```\n(.*?)```", text, re.S)
    return set(re.findall(r"[a-z_]+", m.group(1))) if m else set()


def check_task_states(failures: list[str]) -> None:
    """The device's TaskState enum must serialise to the documented wire values.

    Compares the C# enum MEMBERS against the contract's list, converting the way
    Trace.ToWire does. This is the check that was missing when the device shipped
    `awaitingapproval` against a contract that (silently) meant `awaiting_approval`:
    gate 14 only ever compared frame `type` values and agent_event kinds, so a
    field's own enumeration could drift freely.
    """
    documented = contract_task_states()
    if not documented:
        failures.append("CONTRACT.md no longer documents the task_update.state enum — it is unpinned")
        return

    record = SIBLINGS / "goal-flow-device-agent-ubuntu/src/GoalFlow.Device/Harness/TaskManager/TaskRecord.cs"
    if not record.exists():
        failures.append("could not find TaskRecord.cs — task_update.state is unchecked")
        return
    m = re.search(r"enum TaskState\s*\{(.*?)\n\}", record.read_text(), re.S)
    if not m:
        failures.append("could not read the TaskState enum — task_update.state is unchecked")
        return

    body = re.sub(r"///.*", "", m.group(1))
    members = re.findall(r"^\s*([A-Z][A-Za-z]*)\s*,", body, re.M)
    on_wire = {re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower() for name in members}

    for missing in sorted(on_wire - documented):
        failures.append(f"device emits task state {missing!r}, which CONTRACT.md does not list")
    for extra in sorted(documented - on_wire):
        failures.append(f"CONTRACT.md lists task state {extra!r}, which the device cannot emit")

    trace = SIBLINGS / "goal-flow-device-agent-ubuntu/src/GoalFlow.Device/Harness/Trace/Trace.cs"
    if trace.exists() and 'task.State.ToString().ToLowerInvariant()' in trace.read_text():
        failures.append(
            "Trace emits task.State via ToString().ToLowerInvariant() — that ships "
            "'awaitingapproval', not 'awaiting_approval'. Use ToWire()."
        )


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

    # --- TypeScript + each UI's silent dropper ---
    for ui in UIS:
        name, ts, ws = ui["name"], ui["contract"], ui["ws"]
        if not ts.exists():
            print(f"  (skipped {name} — not created yet)")
            continue

        text = ts.read_text()
        for t in sorted(types - ui["types_exempt"]):
            if f'"{t}"' not in text:
                failures.append(f"{name} contract.ts is missing {t!r}")
        # An exemption must stay a DECISION, not a stale list: a frame declared here
        # that the mirror also carries means the two disagree about what this surface
        # is for.
        for t in sorted(ui["types_exempt"]):
            if f'"{t}"' in text:
                failures.append(
                    f"{name} contract.ts declares {t!r}, which it is exempt from — "
                    f"either it is no longer read-mostly, or the mirror over-reaches"
                )

        if not ws.exists():
            failures.append(f"{name} has a contract mirror but no ws.ts — the allowlist is unchecked")
            continue
        m = re.search(r"INBOUND_TYPES[^=]*=\s*(?:new Set\()?\[([^\]]+)\]", ws.read_text())
        if not m:
            failures.append(f"could not read INBOUND_TYPES out of {name} ws.ts — the allowlist is unchecked")
            continue
        listed = set(re.findall(r'"([a-z_]+)"', m.group(1)))
        for t in sorted(types - ui["inbound_exempt"] - listed):
            failures.append(f"{name} ws.ts INBOUND_TYPES is missing {t!r} — it will be SILENTLY DROPPED")

    # --- field-level enums (not just frame types) ---
    check_task_states(failures)

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 14 (contract mirrors): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
