# GoalFlow Contract v0 (FROZEN)

**This file is the CANONICAL copy of the shared protocol.** The UI repo
(`goal-flow-agent-chat-ui/src/types/contract.ts`) and the device repo mirror it
as typed definitions; any change here is a contract version bump.

## Transport

- **WebSocket.** The cloud is the hub/server. The UI and the Device each open
  **ONE outbound WS** connection to the cloud.
- On connect, a client sends a `hello` frame to register its **role**.
- All messages are JSON objects; the field **`type`** discriminates the message.
- Every task-related message carries **`goal_id`**.
- Device↔cloud messages carry **`correlation_id`** — a dedupe key that also
  correlates an approval back to its proposal.
- The cloud routes on `type` + role.
- On drop: **reconnect**; dedupe on `correlation_id`.

## Handshake

`hello` (UI → cloud):

```json
{ "type": "hello", "role": "ui" }
```

`hello` (device → cloud):

```json
{ "type": "hello", "role": "device" }
```

`hello_ack` (cloud → client):

```json
{ "type": "hello_ack", "role": "ui|device", "session_id": "..." }
```

## Messages

### 1) `user_goal` (UI → cloud)

```json
{ "type": "user_goal", "text": "help my family eat healthier this week and reduce food waste" }
```

### 2) `dispatch` (cloud → device) — the Task Contract

`constraints.hard` is the **ONLY** thing the safety gate reads.

```json
{
  "type": "dispatch",
  "goal_id": "meal-2026-w29",
  "objective": "healthier family dinners, less food waste",
  "scope": { "meal": "dinner", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"] },
  "time_window": { "start": "2026-07-13", "end": "2026-07-17" },
  "constraints": {
    "hard": { "allergens": [], "dietary": ["no_pork"], "medical": [] },
    "soft": { "dislikes": ["mushrooms"], "prefer": ["more_vegetables", "more_protein"] }
  },
  "optimization": ["reduce_processed", "reduce_waste"],
  "autonomy": "propose_all",
  "context_hints": { "notes": "son has sports Wednesday" },
  "reply_to": "kb/device/meal-2026-w29"
}
```

### 3) `plan_ready` (device → cloud)

```json
{
  "type": "plan_ready",
  "goal_id": "meal-2026-w29",
  "correlation_id": "disp-001",
  "task_status": "awaiting_approval",
  "payload": {
    "plan": [
      { "day": "Mon", "dish": "spinach dal rice bowl", "why": ["more_vegetables", "uses_inventory"] }
    ],
    "proposals": [
      {
        "proposal_id": "p1",
        "action": "add_to_shopping_list",
        "items": ["bell peppers", "lentils", "yogurt"],
        "reason": "needed for Tue & Thu dishes",
        "requires_approval": true
      }
    ],
    "safety": { "gate": "passed", "hard_violations": [] }
  }
}
```

### 4) `present_plan` (cloud → UI)

Same payload as `plan_ready`, relayed for rendering (the cloud may add display
hints).

### 5) `proposal` (device → cloud) — adaptation

```json
{
  "type": "proposal",
  "goal_id": "meal-2026-w29",
  "correlation_id": "evt-014",
  "task_status": "adapting",
  "payload": {
    "proposal_id": "p7",
    "action": "add_prep_task",
    "detail": "marinate Wed's chicken on Tue night",
    "trigger": "calendar: son football Wed 18:00 — prep window shrinks",
    "requires_approval": true
  }
}
```

### 6) `approval` (cloud → device)

```json
{
  "type": "approval",
  "goal_id": "meal-2026-w29",
  "correlation_id": "evt-014",
  "payload": { "decisions": [{ "proposal_id": "p7", "approved": true }] }
}
```

### 7) `status` (device → cloud)

```json
{
  "type": "status",
  "goal_id": "meal-2026-w29",
  "correlation_id": "disp-001",
  "task_status": "executing",
  "payload": { "note": "added 3 items to shopping list" }
}
```

## Task-status lifecycle

```
created → planning → awaiting_approval → executing → adapting → done
```

## Invariants

- **Proposals are proposals, not actions** — the device executes nothing until
  an `approval` returns.
- **The UI and the device never talk directly** — everything routes through the
  cloud hub.
- **The safety gate (deterministic device code) is separate from the approval
  gate (the user, via the cloud).** The safety gate blocks; the approval gate
  waits. *"LLM plans, code checks."*
