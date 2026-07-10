# GoalFlow CONTRACT v2 — generic goal-agent WebSocket protocol

**This file is the CANONICAL copy of the shared protocol (the anchor — obey exactly).**
The Python mirror is `src/goalflow_cloud/models/contract.py`; the UI and device repos
mirror it as typed definitions. Any change here is a contract version bump.

## Transport

- **WebSocket, JSON text frames.** The cloud is the hub/server; the UI and the Device
  each open **ONE outbound WS** to the cloud and register via `hello`.
- The field **`type`** discriminates every message.
- Task messages carry **`goal_id`**; device↔cloud messages carry **`correlation_id`**.
- Route on `type` + role; **dedupe on `correlation_id`**; on drop: **reconnect**.

## Generic & domain-agnostic

**NO meal-specific fields in the protocol.** A `domain` string carries the use case
(`"meal_plan"`, `"guest_dinner"`, ...); domain specifics live in **capability modules**
plus the free-form `scope` / `context` objects. The same protocol must serve any goal.

## Messages

### Handshake

`hello` (client → cloud):

```json
{ "type": "hello", "role": "ui" }
```

```json
{ "type": "hello", "role": "device" }
```

`hello_ack` (cloud → client):

```json
{ "type": "hello_ack", "role": "ui|device", "session_id": "..." }
```

### `capabilities` (device → cloud → ui)

The device advertises its **MODULE REGISTRY** — the extensibility/discovery surface.
Modules are either `capability` (tools the planner may call) or `steering` (harness
modules that guard/steer, e.g. the deterministic Safety filter).

```json
{ "type": "capabilities", "modules": [
    { "name": "Inventory", "kind": "capability",
      "functions": [
        { "name": "GetExpiringItems", "description": "...", "side_effecting": false },
        { "name": "Add", "description": "...", "side_effecting": true, "tier": "light" }
      ] },
    { "name": "Safety", "kind": "steering",
      "description": "deterministic hard-constraint filter" }
] }
```

### `user_goal` (ui → cloud)

The raw natural-language goal.

```json
{ "type": "user_goal", "text": "..." }
```

### `dispatch` (cloud → device) — the GENERIC Task Contract

```json
{ "type": "dispatch", "goal_id": "...", "domain": "meal_plan",
  "objective": "...",
  "success_criteria": ["..."],
  "constraints": {
    "hard": { "allergens": [], "medical": [], "dietary": [],
              "budget_cap": null, "quiet_hours": null },
    "soft": { }
  },
  "scope": { },
  "time_window": { "start": "<ISO>", "end": "<ISO>" },
  "autonomy": "tiered",
  "context": { "notes": "..." } }
```

- `constraints.hard` is a **safety policy** object (allergens, medical, dietary,
  budget_cap, quiet_hours, ...). It is the **ONLY** thing the Safety filter enforces.
- `constraints.soft` holds preferences: they bias planning, never gate it.
- `scope` is a **domain-flexible** object (whatever the domain needs — no fixed shape).
- `time_window` is **RELATIVE to real today** (or the control-set clock) — never a
  hardcoded date.
- `autonomy` is `"tiered"`: side-effects are proposed with tiers (see invariants).

### `agent_event` (device → cloud → ui) — the live stream

**STREAMED as the device works**; drives the wow UI (progress rail, tool-call chips,
"watch it think"). The cloud relays these **passthrough** to the UI.

```json
{ "type": "agent_event", "goal_id": "...", "correlation_id": "...", "seq": 1,
  "event": "phase" | "thinking" | "tool_call" | "tool_result" | "plan_progress",
  "payload": { } }
```

Payload shapes by `event`:

| `event`         | `payload`                                                 |
|-----------------|-----------------------------------------------------------|
| `phase`         | `{ "phase": "grounding" \| "planning" \| "checking" \| "awaiting_approval" }` |
| `thinking`      | `{ "text": "..." }`                                       |
| `tool_call`     | `{ "module": "...", "function": "...", "args": { } }`     |
| `tool_result`   | `{ "module": "...", "function": "...", "summary": "..." }`|
| `plan_progress` | `{ "item": { } }`                                         |

### `plan_ready` (device → cloud) — generic plan + TIERED proposals

```json
{ "type": "plan_ready", "goal_id": "...", "correlation_id": "...",
  "task_status": "awaiting_approval",
  "payload": {
    "plan": [
      { "id": "s1", "title": "...", "detail": "...", "when": "<ISO?>",
        "why": ["..."], "tags": ["..."] }
    ],
    "proposals": [
      { "proposal_id": "p1", "action": "...", "module": "ShoppingList",
        "function": "Add", "args": { }, "tier": "auto" | "light" | "firm",
        "reason": "...", "requires_approval": true }
    ],
    "safety": { "gate": "passed" | "blocked", "violations": [] },
    "impact": [ { "label": "...", "value": "..." } ],
    "explanation": "..."
  } }
```

### `present_plan` (cloud → ui)

`plan_ready` relayed, **plus** `payload.knew` — the personalization "what it knew"
(what memory/constraints the cloud injected).

### `approval` (ui → cloud → device)

```json
{ "type": "approval", "goal_id": "...", "correlation_id": "...",
  "payload": { "decisions": [ { "proposal_id": "p1", "approved": true } ] } }
```

### `proposal` (device → cloud → ui) — adaptation (generic)

```json
{ "type": "proposal", "goal_id": "...", "correlation_id": "...",
  "task_status": "adapting",
  "payload": { "proposal_id": "a1", "action": "...", "detail": "...",
               "trigger": "...", "tier": "...", "requires_approval": true } }
```

### `status` (device → cloud → ui)

```json
{ "type": "status", "goal_id": "...", "correlation_id": "...",
  "task_status": "...",
  "payload": { "day": "?", "sim_date": "?", "material": false,
               "executed": [], "note": "..." } }
```

### `control` (ui → cloud → device)

```json
{ "type": "control", "goal_id": "...",
  "command": "advance_day" | "reset" | "set_date",
  "payload": { "date": "<ISO?>" } }
```

## Task-status lifecycle

```
created -> interpreting -> grounding -> planning -> checking ->
awaiting_approval -> executing -> monitoring -> adapting -> done
```

## Invariants

1. **Tiered proposals:** side-effecting tool calls are PROPOSED with a tier
   (`auto` / `light` / `firm`); **nothing firm executes until approval**.
2. **Hub-only:** UI and device NEVER talk directly; all traffic goes via the cloud.
3. **"LLM plans, code checks":** the Safety filter is deterministic code SEPARATE
   from the LLM planner; it enforces `constraints.hard` and nothing else.
4. **Generic clock:** the device reads a GENERIC clock (real today, or set via
   `control set_date` / `advance_day`) — NEVER a hardcoded date.
