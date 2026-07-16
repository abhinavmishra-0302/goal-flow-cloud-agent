# GoalFlow CONTRACT v2 — generic goal-agent WebSocket protocol

**This file is the CANONICAL copy of the shared protocol (the anchor — obey exactly).**
The Python mirror is `src/goalflow_cloud/models/contract.py`; the UI and device repos
mirror it as typed definitions. Any change here is a contract version bump.

## Transport

- **WebSocket, JSON text frames.** The cloud is the hub/server; the UI and the Device
  each open **ONE outbound WS** to the cloud and register via `hello`.
- The field **`type`** discriminates every message.
- Task messages carry **`goal_id`**; device↔cloud messages carry **`correlation_id`**.
- Route on `type` + **session** (see below); **dedupe on `correlation_id`**; on drop: **reconnect**.

## Sessions — `device_id` is the pairing key

The hub is **multi-session**: many UIs and many device agents may be connected at once.
A **session** is one **home**: exactly **one device agent + N UIs**, keyed by `device_id`.

- **Every frame routes only within its session**, chosen from the SENDER socket's
  `device_id` — never by role alone, never broadcast across sessions. A UI's `user_goal`
  / `approval` / `control` go to *its* device; a device's `agent_event` / `plan_ready` /
  `proposal` / `status` / `capabilities` go to *its* UIs.
- **Device agents own their `device_id`** — a stable, self-generated persistent UUID
  (overridable). A device reconnect replaces (1012-closes) only the previous socket of the
  **same** `device_id`; other homes are untouched. **UI sockets are never evicted.**
- **A UI must be BOUND before it can send.** It binds by (a) sending `device_id` in
  `hello`, (b) the cloud auto-binding it when exactly **one** device is connected, or
  (c) answering the `devices` list with `select_device`. Frames from an unbound UI are
  dropped.
- Absent `device_id` on a **device** `hello` ⇒ `"default"` (zero-config single-pair).
- `goal_id` still scopes the graph run (one checkpointer thread per goal); `device_id`
  scopes *delivery*. They are independent.

## Generic & domain-agnostic

**NO meal-specific fields in the protocol.** A `domain` string carries the use case
(`"meal_plan"`, `"guest_dinner"`, ...); domain specifics live in **capability modules**
plus the free-form `scope` / `context` objects. The same protocol must serve any goal.

## Messages

### Handshake

`hello` (client → cloud):

```json
{ "type": "hello", "role": "ui", "device_id": "hub-a" }
```

```json
{ "type": "hello", "role": "device", "device_id": "9f3c...", "device_name": "ashu@boxA" }
```

- `device_id` (both roles, optional) — the pairing key. **device:** its own stable id;
  empty ⇒ `"default"`. **ui:** the device it wants to watch (from `?device=<id>`); empty
  ⇒ unbound, await auto-bind or `devices`/`select_device`.
- `device_name` (device only, optional) — human label for the UI's picker; defaults to
  `user@machine`.

`hello_ack` (cloud → client):

```json
{ "type": "hello_ack", "role": "ui|device", "session_id": "...", "device_id": "hub-a" }
```

- `device_id` — the session this socket is bound to; `""` for a still-unbound UI. Also
  sent again in reply to a successful `select_device`.

### `devices` (cloud → ui)

Sent to an **unbound** UI (no `device_id` and the cloud could not auto-bind because there
isn't exactly one device), and again whenever the connected set changes.

```json
{
  "type": "devices",
  "devices": [
    { "device_id": "9f3c...", "device_name": "ashu@boxA", "online": true },
    { "device_id": "1a7d...", "device_name": "bob@boxB",  "online": true }
  ]
}
```

### `select_device` (ui → cloud)

Binds this UI socket to a device (the picker's answer). The cloud replies `hello_ack`
with the bound `device_id`.

```json
{ "type": "select_device", "device_id": "9f3c..." }
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

### `understanding` (cloud → ui)

Emitted after the cloud interprets the goal and loads memory, before any device
dispatch. The graph is paused until the UI answers with `understanding_response`.

```json
{ "type": "understanding", "goal_id": "...", "task_status": "grounding",
  "payload": {
    "objective": "...",
    "domain": "meal_plan",
    "knew": { "allergens": ["peanuts"], "budget": "$120" },
    "thought": "I'll shape a meal plan around your constraints before planning.",
    "time_window": { "start": "<ISO>", "end": "<ISO>" }
  } }
```

### `understanding_response` (ui → cloud)

```json
{ "type": "understanding_response", "goal_id": "...",
  "payload": { "confirmed": true } }
```

If `confirmed` is `true`, the graph resumes to `dispatch`; if `false`, it ends
gracefully with no device dispatch.

### Actionability gate (interpret)

When the cloud interprets `user_goal`, the LLM also returns an **actionability
verdict**. GoalFlow only acts on two kinds of goals: **weekly meal/dinner planning**
and **hosting a guest dinner**. Any other goal (trivia, general questions, unrelated
tasks) is judged `actionable: false`; the graph then ends at `decline_out_of_scope`
**before any device dispatch** and the UI receives a `notice` (below) instead of an
`understanding`/`present_plan`. This is why an off-topic prompt no longer produces a
meal plan.

### `notice` (cloud → ui)

A terminal, non-plan message. Emitted when the graph ends before any device
dispatch — today, when the interpreter declines an out-of-scope goal.

```json
{ "type": "notice", "goal_id": "...", "kind": "out_of_scope",
  "message": "That's outside what I do. I'm your Family Hub goal assistant — I can plan the week's meals or help you host a dinner." }
```

`kind` is `"out_of_scope"` (declined by the interpreter) or `"declined"` (reserved).

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
      { "id": "s1", "day": 1, "title": "...", "detail": "...", "when": "<ISO?>",
        "why": ["..."], "tags": ["..."] }
    ],
    "proposals": [
      { "proposal_id": "p1", "action": "...", "module": "ShoppingList",
        "function": "Add", "args": { }, "tier": "auto" | "light" | "firm",
        "reason": "...", "requires_approval": true }
    ],
    "safety": { "gate": "passed" | "blocked", "violations": [] },
    "impact": [ { "label": "...", "value": "..." } ],
    "demo_events": [
      { "id": "day3-football", "day": 3, "label": "Thu",
        "title": "Football practice", "kind": "calendar.event_overlap", "order": 3 }
    ],
    "explanation": "..."
  } }
```

- `plan[].day` is the **1-based plan-day index** — the source of truth for
  meal-week ordering (the UI renders "Day N"; events target a plan item by `day`).
- `demo_events` (optional) is a display catalog of **presenter-fired** demo events.
  The UI renders one chip per entry; firing a chip sends `control trigger_event`
  (below). Present only for demos that expose the event strip (e.g. the meal week).

### `present_plan` (cloud → ui)

`plan_ready` relayed, **plus** `payload.knew` — the personalization "what it knew"
(what memory/constraints the cloud injected). `payload.demo_events` is relayed
through unchanged when present.

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
               "trigger": "...", "tier": "..." | "adapt", "requires_approval": true,
               "event_id": "day3-football",
               "patch": { "upsert": [ { "id": "s3", "day": 3, "title": "...", "detail": "..." } ],
                          "remove": [], "impact_delta": [], "rationale": "..." } } }
```

- `event_id` echoes the presenter-fired event that triggered this adaptation
  (present only for `trigger_event`-driven proposals).
- `patch` (optional) is the scoped plan diff a daily/event adaptation applies:
  `upsert` (rows to add/replace, each with its `day`), `remove` (ids), plus
  `impact_delta` and a short `rationale`. The `adapt` tier marks these proactive
  adaptations.

### `status` (device → cloud → ui)

```json
{ "type": "status", "goal_id": "...", "correlation_id": "...",
  "task_status": "...",
  "payload": { "day": "?", "sim_date": "?", "material": false,
               "executed": [], "note": "...",
               "event_id": "day3-football",
               "updated_plan": [ { "id": "s3", "day": 3, "title": "...", "...": "..." } ],
               "changed_ids": ["s3"],
               "impact_delta": [ { "label": "...", "value": "..." } ] } }
```

- On an applied adaptation the status carries the **new full plan** in
  `updated_plan` (the UI replaces the plan card in place), `changed_ids` (rows to
  highlight/morph), `impact_delta` (badges to merge), and the `event_id` that
  drove it. Quiet ticks omit these (just `material: false` + a "on track" note).

### `control` (ui → cloud → device)

```json
{ "type": "control", "goal_id": "...",
  "command": "advance_day" | "reset" | "set_date" | "trigger_event",
  "payload": { "date": "<ISO?>", "event_id": "day3-football" } }
```

- `trigger_event` fires one presenter demo event by `event_id` (from
  `plan_ready.demo_events`): the device runs ONE scoped-LLM adaptation for that
  event's context, **clock frozen**, deduped once per event id. This is the
  event-driven meal-week demo path — it replaces `advance_day` for that demo.

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
