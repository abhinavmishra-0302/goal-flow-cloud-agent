# GoalFlow CONTRACT — generic goal-agent WebSocket protocol

**This file is the CANONICAL copy of the shared protocol (the anchor — obey exactly).**
The mirrors are `src/goalflow_cloud/models/contract.py` (Python),
`goal-flow-agent-chat-ui/src/types/contract.ts`,
`goal-flow-agent-board-ui/src/types/contract.ts` and
`goal-flow-agent-bixby-ui/src/types/contract.ts` (TS), and
`goal-flow-device-agent-ubuntu/src/GoalFlow.Device/Contracts/*.cs` (C#).
**All of them move in ONE pass** — there is no cross-repo atomic commit, so a
half-mirrored change breaks at runtime rather than at build time. The chat UI's
`ws.ts` has an `INBOUND_TYPES` allowlist too: a frame type missing from it is
**silently dropped**, which is the worst possible failure mode. Any change here is a
contract version bump.

## Version history

Every change here has been **additive** — an older client keeps working, and an unknown field
is ignored rather than fatal. That is the only property that made five surfaces and two device
ports survivable. The frame sections below are the truth; this table is just the shape of the
past.

| Version | Added |
|---|---|
| v2 | the base protocol: `hello`, `user_goal`, `dispatch`, `agent_event`, `plan_ready`, `present_plan`, `approval`, `proposal`, `status`, `control` |
| v3 | `capabilities.domains[]`, `agent_event: task_update`, `phase: "queued"`, `plan_ready.precheck`, `status.executed[].result: "deferred_precheck"`, the board frames (`board_snapshot`/`board_update`/`board_get`), `goal_state_get`, `goal_accepted` + `user_goal.client_ref` |
| v3.2 | a **goal-less** `control` = one world tick fanning out over every goal, plus `day_advanced` |
| v4.1 | `hello.surface`, the input-surface delivery fork, `chat_ui_open`/`chat_ui_close`, create-phase replay on bind |
| v5.1 | `agent_event: harness`, `plan_progress.total` |
| v6 | `constraints.hard`: `peak_hours`, `away_window`, `budget_envelope`; `understanding.constraints` (provenance) and `proposed_constraints`/`capture_only`; `understanding_response.accepted_constraint_ids` |
| v7 | `control: constraints_changed` (+ `payload.hard`/`steer`/`note`) — the one adaptation path that does not ask; `status.plan_changed_note`; `GoalSummary.plan_changed_note`; `plan[].status`/`status_reason`; `notice.kind: updating_goals` (non-terminal); `understanding.preferences` (the soft half, one row per entry); `understanding.constraints[].kind`; store-side `display_to` narrows `knew`/`constraints` on both the gate and `present_plan` without touching what is dispatched or enforced; `agent_event: thinking` gains `kind`/`step`/`detail`; `plan_ready.payload` gains `considered`/`rejected` |
| v11.1 | `speech` gains five cues (`understanding`, `working_start`, `working_plan`, `plan`, `approvals`, `saved`); `plan_ready.payload.narration` — the plan written to be spoken, by the same compose call that writes the plan |
| v11 | `speech` (cloud → ui) + the hub's `GET /speech/{utterance_id}.mp3` — the understanding gate said out loud. Additive and ignorable; absent entirely when the cloud has no TTS key |

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
{ "type": "hello", "role": "ui", "device_id": "hub-a", "surface": "input" }
```

```json
{ "type": "hello", "role": "device", "device_id": "9f3c...", "device_name": "ashu@boxA" }
```

- `device_id` (both roles, optional) — the pairing key. **device:** its own stable id;
  empty ⇒ `"default"`. **ui:** the device it wants to watch (from `?device=<id>`); empty
  ⇒ unbound, await auto-bind or `devices`/`select_device`.
- `device_name` (device only, optional) — human label for the UI's picker; defaults to
  a label that is UNIQUE per agent (it ends with a short slice of the device_id, so a
  picker never shows two identical entries).
- `surface` (ui only, optional, v4.1) — `"input" | "chat" | "board"`: what this socket
  is FOR, declared once at handshake and immutable for the socket's lifetime. Absent or
  empty ⇒ the socket receives the full session broadcast, exactly like every v3 client
  (which is what makes this additive). Only `"input"` changes delivery today — see
  *Surface-aware delivery (v4.1)*. `"chat"` additionally opts in to the create-phase
  replay on bind. Ignored on a `role:"device"` hello. The cloud stores it PER SOCKET
  alongside `(role, device_id)` in its connection metadata — a session's `uis` list
  stays flat; surface is a property of the socket, not of the session.

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

- The list is **online-only** — a device with no live agent is **omitted**, not sent with
  `online: false`. A UI can bind (via `hello.device_id` or `select_device`) to a
  `device_id` that has NO device connected; the cloud binds it to an (empty) session and
  acks it, so goals then decline with `no_capabilities`. Therefore a UI **must treat "my
  bound `device_id` is absent from this list" as "my device is offline"** and self-heal:
  auto-rebind (`select_device`) when exactly one device is online, else show the picker
  (v4.1 fix — chat-ui `8752f5f`, board-ui `ade4869`, bixby-ui `8e0dafc`).

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
    "knew": { "allergens": ["peanuts"], "dietary": ["no_pork"] },
    "constraints": [
      { "id": "c-allergen-peanuts", "kind": "allergens", "label": "peanut allergy",
        "value": "peanuts", "enforcement": "hard", "source": "account",
        "why": "always enforced" }
    ],
    "preferences": [
      { "id": "s-prefer-white-meat", "label": "prefers white meat",
        "value": "prefer white meat, chicken turkey fish over red meat",
        "source": "account", "why": "tagged" }
    ],
    "thought": "I'll shape a meal plan around your constraints before planning.",
    "time_window": { "start": "<ISO>", "end": "<ISO>" }
  } }
```

`constraints` (v6, **additive**) is the provenance list behind the `knew` chips: one
row per applied HARD constraint, with `source` ∈ `account | derived | chat` and `why`
saying whether it was enforced always, picked for this domain, or captured. `knew` is
unchanged, so a UI that ignores `constraints` keeps working.

**v7 — `preferences` (additive), and display ≠ enforcement.** `preferences` carries the
SOFT half: one row per store entry, `{id, label, value, source, why}`, with no
`enforcement` field because there is nothing to enforce. It is a separate field rather
than more `knew` chips because a preference shapes a plan and can never block one, and
a UI that renders the two alike teaches the reader that a chip is just a chip.

Also in v7, a store entry may carry `display_to` — the domains its chip is worth
**showing** on. It has no bearing on resolution: the hard list kinds still union with
`applies_to` ignored, and `dispatch.constraints.hard` still carries the full enforced
set. It only narrows `knew` and `constraints`, so a home-prep goal shows no food chips
while still being dispatched — and still being blocked by — every allergen the
household holds. **Hiding a chip never hides a rule.** `present_plan.knew` applies the
same filter, so the gate and the plan card cannot disagree.

**v6-M4 — capture (additive).** When the message STATES a household rule, the payload
also carries `proposed_constraints` — `[{id, kind, value, enforcement, label, quote,
expires_on}]` — and the response answers with `accepted_constraint_ids`. Only the ids
sent back are ever written: the model proposes, the user disposes, and a confirmed
GOAL never implies a confirmed RULE. When the message is *only* a statement
("we've gone vegan"), `capture_only: true` says there is no plan coming and the gate
is asking about the rules alone; confirming it ends with a `notice` of kind
`captured`, and no board card is ever created.

### `speech` (cloud → ui) — v11

The cloud asking a surface to **say something out loud**. Sent immediately AFTER the
frame it speaks for (today, only `understanding`), so a UI always has the card on
screen before the voice describes it.

```json
{ "type": "speech", "goal_id": "...",
  "payload": {
    "utterance_id": "u-<goal_id>-understanding",
    "cue": "understanding",
    "text": "Here's what I understood. Plan a week of healthy family dinners that cut food waste. That covers August 4 through August 9. I'll hold 2 household rules: peanut allergy and low sodium. Shall I go ahead and plan it?",
    "url": "/speech/u-<goal_id>-understanding.mp3"
  } }
```

**Additive and ignorable.** A UI that has never heard of this frame drops it and
renders exactly as it did in v10 — the understanding gate is complete, legible and
answerable in silence. That is also the failure mode: no `FISH_API_KEY` on the cloud
means the frame is **never sent at all**, so "no voice" is a normal state, not an error.

- `url` is a **PATH, not an absolute URL**. The cloud does not know which `host:port` a
  UI reached it on (a tablet, the Hub browser and a dev laptop all differ); the UI
  resolves it against the origin of the socket it is already connected on.
- `text` is the spoken words verbatim — the caption, the accessibility fallback, and
  the only thing left when synthesis fails.
- `utterance_id` is **deterministic** per (`goal_id`, `cue`): create-phase replay
  re-sends this frame, and the same sentence must not be synthesized (or billed) twice.
- `cue` names the moment. `understanding` is the only one in v11; the field exists so
  the next moment is a string, not another frame type.

**`GET /speech/{utterance_id}.mp3`** — the hub's only HTTP route. Synthesis happens on
this fetch, not when the frame was sent, so the gate never waits on the TTS provider and
an utterance nobody plays is never paid for. An unknown id is **404** (only text the
cloud itself minted is reachable — the URL is not a synthesis oracle); no key is
**503**; a provider failure closes the body early, which surfaces as the `<audio>`
element's error handler. Every one of those is a UI that stays quiet.

**Autoplay is the UI's problem, and it is a real one.** A browser rejects
`audio.play()` with `NotAllowedError` unless the document has user activation, and a
cross-origin webview additionally needs `allow="autoplay"` on its `<iframe>`. A UI that
handles this frame MUST degrade to a tap-to-play affordance rather than assume it can
speak. See the chat UI's `src/lib/speech.ts`.

### `understanding_response` (ui → cloud)

```json
{ "type": "understanding_response", "goal_id": "...",
  "payload": { "confirmed": true, "accepted_constraint_ids": ["proposed-1"] } }
```

If `confirmed` is `true`, the graph resumes to `dispatch`; if `false`, it ends
gracefully with no device dispatch.

### Actionability gate (interpret)

When the cloud interprets `user_goal`, the LLM also returns an **actionability
verdict**. As of v3-M4 this is judged against **whatever the connected device
advertises** (`capabilities.domains` + capability modules), NOT a fixed list of
topics — "what this assistant can do is a fact about the device that is plugged in,
not a fact about the cloud." So the actionable set grows with the device: M7's device
advertises `meal_plan`, `guest_dinner`, `vacation_prep`, and `birthday_party`, and
"get the house ready, we're away next week" — which v2 declined — is now actionable.
A goal nothing advertised relates to (trivia, general questions, unrelated tasks) is
judged `actionable: false`; the graph then ends at `decline_out_of_scope` **before any
device dispatch** and the UI receives a `notice` (below) instead of an
`understanding`/`present_plan`.

### `notice` (cloud → ui)

A terminal, non-plan message. Emitted when the graph ends before any device
dispatch — today, when the interpreter declines an out-of-scope goal.

```json
{ "type": "notice", "goal_id": "...", "kind": "out_of_scope",
  "message": "That's outside what I do. I'm your home goal assistant — I can help with <the device's advertised capabilities>. Try one of those and I'll get going." }
```

`kind` is `"out_of_scope"` (declined by the interpreter) or `"declined"` (v4.1,
active). A `"declined"` notice is emitted when the create phase is **cancelled at the
understanding gate** (`understanding_response { confirmed: false }`, or an aborted
create flow) — sent ALONGSIDE `chat_ui_close { goal_id }` so the input (Bixby) surface
can SPEAK the cancellation while the webview closes.

### `chat_ui_open` / `chat_ui_close` (cloud → ui, v4.1) — the create-phase bracket

The chat UI is EPHEMERAL on the real device: Bixby (a native app) opens a webview
hosting it when a goal's create phase begins and closes it when the create phase ends.
These two frames are that bracket. The device never sees either.

```json
{ "type": "chat_ui_open",  "goal_id": "...", "goal_text": "Plan my weekly meal." }
{ "type": "chat_ui_close", "goal_id": "..." }
```

**`chat_ui_open`** is emitted the INSTANT `user_goal` arrives — **before** interpretation
runs (v7.4), not after it. Interpretation is a 10-60s LLM round-trip, and emitting the
open afterwards meant the whole of the slowest wait in the product happened with no
webview on screen at all: the user spoke to the fridge, the fridge showed nothing, and
then the understanding card appeared as though the work had been instant. Nothing about
the open depends on interpretation — it is a RESET keyed to a `goal_id`, which is minted
on arrival.

It carries **`goal_text`**, verbatim, for the same reason: during interpretation that
frame is the only thing the chat surface knows, and a panel that cannot say what it is
working on has nothing to show but a spinner. Optional, so a pre-v7.4 client is unaffected.

Because the bracket is now open before either early exit, the **error** path closes it,
and the **out-of-scope** path does NOT re-open it — a second open would broadcast a reset
that wipes the panel the user has been watching. An out-of-scope goal still brackets (v7):
the already-open webview receives the `notice`, then a cloud-side `chat_ui_close` ~4.5s
later. A refusal is an answer, and it is shown where every other answer is shown. The
notice is also cached for replay — see *Create-phase replay cache*.

It has a DUAL role, one per surface:

- **`input` (Bixby):** open the webview (or ensure it is open) pointed at the chat UI.
- **`chat` (the webview):** a **HARD RESET** keyed to `goal_id` — drop ALL state from
  any prior goal, render the fresh "listening" stage for this `goal_id`, and from now
  on ignore goal-scoped frames whose `goal_id` differs. The reset is IDEMPOTENT per
  goal: a `chat_ui_open` for the goal the UI is already keyed to is a no-op (the
  bind-time replay re-sends it, and resetting then would throw away the very state
  the replay is about to restore).

**`chat_ui_close`** is emitted when the create phase TERMINATES, on any of:

1. the initial `approval` frame arrives for the create-phase goal (the normal path —
   see below);
2. `understanding_response { confirmed: false }` — the goal was cancelled at the gate;
3. a terminal error `status` ends the create phase after `chat_ui_open` was sent
   (e.g. the dispatch contract failed to build after confirmation).

Bixby closes the webview **only if `goal_id` matches the goal it currently has open**;
the chat UI returns to its idle "waiting" state. The board owns everything after.

**Pairing invariant:** `chat_ui_close(g)` is emitted only if `chat_ui_open(g)` was,
and only while `g` is still the session's current create-phase goal — with ONE
exception: a `chat_ui_open` for a NEW goal **supersedes** the previous create phase
*without* an intervening close. (Bixby's rule — close only on matching `goal_id`,
open = ensure-open-and-retarget — makes the supersede a retarget instead of a
close/reopen flicker.) Adaptation-time `approval` frames from the board never trigger
a close: by then the goal is no longer the create-phase goal, so trigger (1) cannot
match.

**Why close on `approval` and not on execution-started:** (a) the webview should
vanish at the user's final tap — snappiness is the point of the bracket; (b) there is
no single "execution started" frame to hook: after approval the device may stream
`agent_event phase: "executing"`, or defer everything (`deferred_precheck`), or be
OFFLINE entirely (`send_to_device` returning false already produces a terminal
status) — closing on approval is correct in every one of those futures, while waiting
on the device leaves a dead webview open exactly when things go wrong; (c) the v3.1
handoff is already defined as "approved on chat ⇒ the board is the primary surface" —
this frame just makes that handoff physical.

### `dispatch` (cloud → device) — the GENERIC Task Contract

```json
{ "type": "dispatch", "goal_id": "...", "domain": "meal_plan",
  "objective": "...",
  "success_criteria": ["..."],
  "constraints": {
    "hard": { "allergens": [], "medical": [], "dietary": [],
              "budget_cap": null, "quiet_hours": null,
              "peak_hours": null, "away_window": null,
              "budget_envelope": null },
    "soft": { }
  },
  "scope": { },
  "time_window": { "start": "<ISO>", "end": "<ISO>" },
  "autonomy": "tiered",
  "context": { "notes": "..." } }
```

- `constraints.hard` is a **safety policy** object (allergens, medical, dietary,
  budget_cap, quiet_hours, peak_hours, away_window, ...). It is the **ONLY** thing the
  Safety filter enforces. **v6:** the cloud RESOLVES it per goal from the household
  constraint store, by code — the list kinds (allergens/dietary/medical) are unioned
  across the whole store regardless of domain, while the cap and window kinds are
  domain-picked, so a `vacation_prep` goal carries a travel cap and an away window
  where a `meal_plan` goal carries the weekly grocery cap. `peak_hours` (peak
  electricity tariff, HH:mm) and `away_window` (the house is empty, ISO **dates**) are
  new in v6; enforcement of them lands with the device rules in v6-M2.
- **v6-M3 — `budget_envelope`** (`{"cap": 600.0, "period": "monthly"}`) is the shared
  pool EVERY goal draws from. Per-goal caps alone cannot stop two goals spending the
  same money: a $200 party and a $120 grocery week each fit their own ceiling and
  together blow a month. The **device** resolves this goal's effective ceiling as
  `min(budget_cap, cap − spent)` when it arms the policy, and again on approval and
  each day tick — the cap is policy from the account, the spend is world state the
  device owns. The rules themselves still read `constraints.hard` and nothing else;
  the arithmetic happens in a resolution step BEFORE arming.
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
  "event": "phase" | "thinking" | "tool_call" | "tool_result" | "plan_progress" | "task_update" | "harness",
  "payload": { } }
```

`seq` is **monotonic per goal**, and a consumer MUST drop any frame whose `seq` is not
greater than the last it saw for that `goal_id`. (Which is why the device's trace scope
is per-goal: a shared counter sent one goal's frames out under another's id with a seq
that had gone backwards, and they were silently discarded.)

Payload shapes by `event`:

| `event`         | `payload`                                                 |
|-----------------|-----------------------------------------------------------|
| `phase`         | `{ "phase": "queued" \| "grounding" \| "planning" \| "checking" \| "awaiting_approval" \| "executing" \| "monitoring" \| "adapting" }` |
| `thinking`      | `{ "text": "...", "kind"?: "narration" \| "step" \| "notice", "step"?: "...", "detail"?: "..." }` |
| `tool_call`     | `{ "module": "...", "function": "...", "args": { } }`     |
| `tool_result`   | `{ "module": "...", "function": "...", "summary": "..." }`|
| `plan_progress` | `{ "item": { }, "total": 7 }`                             |
| `task_update`   | `{ "task_id": "t2", "title": "find recipes", "state": "monitoring", "depends_on": ["t1"], "progress_pct": 43, "pending_tasks": 4, "next_step": "build the shopping list", "retry_count": 0, "failure_reason": null }` |
| `harness`       | `{ "module": "safety", "status": "block", "note": "blocks \"peanut sauce\"", "verdict": "1 blocked", "grade": "A1" }` |

**`thinking.kind`** (v7, optional) — `narration` | `step` | `notice`; absent means
`narration`, which is every thinking event emitted before v7. A **`step`** carries `step`
(headline) and `detail` (sub-line) and is **whole on arrival, never fragmented**, so a
client renders it immediately instead of accumulating chunks and guessing where one
thought ends. `text` still holds `"step — detail"`, so a client that ignores the new
fields is unaffected. This exists because the compose call is not streamed and keeps its
plan JSON off this channel deliberately: through v6 the planner emitted **nothing** on a
healthy run, and a silent engine is indistinguishable from a broken one.

**`plan_progress.total`** (v5.1, optional) — how many items the finished plan has. The
device composes a plan in ONE non-streaming call and then emits every item in a single
loop, so all N frames land together and a UI cannot tell how many are still coming. With
`total` a surface can reserve exactly N rows before filling any of them, for a goal of any
shape — seven dinners, eleven vacation steps — instead of guessing a horizon. Omitted by
pre-v5.1 devices; a consumer MUST treat it as unknown, not as zero.

**`task_update` (v3)** — the device emits one every time a task changes state. The goal's
task DAG lives on the DEVICE (only it can ground a decomposition), so this is how the
cloud learns what a goal is made of and how far along it is. `progress_pct`,
`pending_tasks` and `next_step` are the goal-level rollup as of that transition; they are
DERIVED from task state, never from the clock.

`state` is one of — **snake_case, like every other enum on this wire**:

```
created | ready | planning | awaiting_approval | executing
monitoring | adapting | paused | retrying | completed | failed
```

**`harness` (v5)** — names the specific HARNESS ENGINE at work, where `phase` is coarse.
It is what the UI renders as the "harness pipeline" lighting up engine-by-engine — the
whole point of v5 is that the harness (the star of the system) is finally *visible*.

- `module` ∈ `precheck | capability_manager | grounding | planner | safety | task_manager | approval | monitor_adapt`
- `status` ∈ `enter | active | pass | block | done | skip` — `active` lights the engine up
  ("now X is working"), `pass`/`done` resolve it green, `block` resolves it red, `skip` greys it.
- `note` is the engine's live one-line sub-text; `verdict` a short badge ("12 tools", "ready",
  "3 steps"); `grade` the safety grade (A0/A1/A2/AX) on `safety` beats.

Additive, like `phase`: an unknown `module`/`status` is ignored, never fatal. Under **presenter
mode** the device holds each `active` beat for `HARNESS_DWELL_MS` so the pipeline is watchable on
stage; with the dwell off (default) the beats stream at real, honest speed.

These were unlisted until v3-M6, and the drift that followed is the reason they are
written down now: the device serialised its enum with `ToString().ToLowerInvariant()`
and shipped `awaitingapproval` while `task_status` and `phase` carried
`awaiting_approval` — the same idea spelled two ways. Every value except
`awaiting_approval` is a single word, so it round-tripped by accident and no consumer
noticed. An example alone (`"state": "monitoring"`) does not pin an enum; the list does.

`phase: "queued"` (v3) means another goal holds the single planning slot; this one starts
next. It exists so a waiting goal is visible rather than appearing stalled.

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
    "considered": 17,
    "rejected": [ { "option": "pork belly stir-fry", "reason": "no pork" } ],
    "narration": "Chicken three nights, fish on Thursday, and everything that would have spoiled gets used up before you go away.",
    "explanation": "..."
  } }
```

- `narration` (v11.1, optional) is the plan **written to be spoken aloud** — two short
  sentences, ≤40 words. It is NOT a shorter `explanation`: that one is read on a screen
  and may be a paragraph; this one is heard once, by someone who may not be looking.
  Both are written by the SAME compose call, which is the point — a narration produced
  by a second model reading the plan afterwards could contradict it, and would cost a
  round trip at the one moment the user is waiting for good news. Absent or empty means
  the voice says nothing about the plan; there is deliberately **no deterministic
  fallback**, because code can only count rows and "seven items, three needing approval"
  describes a data structure rather than a week of dinners. The cloud drops it silently
  if it exceeds 260 characters — a sentence cut mid-clause is worse than no sentence.
- `considered` / `rejected` (v7, optional) are **model-authored and display-only**.
  Nothing downstream reads them: a wrong rejection reason costs a wrong sentence, which
  is the right price for the clearest evidence a person can be given that something
  reasoned rather than looked up — a lookup table cannot reject. Absent is normal, and a
  model that weighed nothing is told to omit them rather than invent a number.
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

- **`decisions` is the COMPLETE set** for the initial plan — every approval-required
  proposal (`tier != "auto" && requires_approval`), approved OR declined, in ONE frame.
  The device RESUMES the whole plan on the FIRST `approval` frame it receives (it lifts
  its `awaiting_approval` interrupt and executes the approved side-effects, skipping the
  declined/unlisted ones). A UI that instead sent one frame per proposal click would
  therefore drop every later proposal and — because the cloud closes the create-phase
  webview on the first `approval` (see `chat_ui_close`) — close it early. So the UI
  MUST accumulate its per-proposal clicks and emit a single `approval` once all
  approval-required proposals are decided (v4.1 fix, chat-ui `6b39ea8`).

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
{ "type": "control", "goal_id": "...?",
  "command": "advance_day" | "reset" | "set_date" | "trigger_event" | "constraints_changed",
  "payload": { "date": "<ISO?>", "event_id": "day3-football" } }
```

- **`goal_id` is OPTIONAL (v3.2).** The sim clock is GLOBAL (one, device-wide), so a
  clock command with **no `goal_id`** is a **WORLD-level tick**: the device advances the
  clock ONCE and fans out to **every active goal** — emitting a `status` (+ a `proposal`
  when a goal newly catches a material change) per goal, plus one `day_advanced` summary
  (below). This is how the board's main-page **Advance day** works. A `goal_id` scopes the
  older per-goal path.
- `trigger_event` fires one presenter demo event by `event_id` (from
  `plan_ready.demo_events`) for a specific goal — the per-goal path (retained, but the
  board no longer sends it; the world tick supersedes it).
- **`constraints_changed` (v7, cloud → device, goal-scoped)** is the one adaptation path
  that does **not** ask. The account re-resolved THIS goal's constraints because ANOTHER
  goal was approved — the family said they are away, so a meal week has days it should not
  be planning dinners for. Payload carries `hard` (the account's new
  `constraints.hard`, verbatim — the device re-arms from it and authors nothing), `steer`
  (how to re-plan) and `note` (one sentence for the board). The device applies the patch
  immediately and reports it with `status.plan_changed_note`; it never opens an approval,
  because the user already approved this when they approved the other goal, and asking
  twice about one decision implies the first answer did not count.

### `day_advanced` (device → cloud → ui)

```json
{ "type": "day_advanced", "sim_date": "2026-07-16", "day": 3,
  "events": [ { "id": "daily:ev-football", "title": "Day 3 - football practice added Wed",
                "kind": "calendar.event_overlap", "summary": "...",
                "goal_ids": ["<goal_id>"] } ] }
```

Emitted once per **world-level** control tick. It summarises the world events that
happened on the new day and which goals each was material to — the board's "what happened
today" card. Events with empty `goal_ids` happened but touched no active goal; a quiet day
sends an empty `events` list. The per-goal `status`/`proposal` frames that ride alongside
update each goal card. The **chat UI never renders it** (a board surface).

### Agent Board (v3) — `board_snapshot` / `board_update` (cloud → ui), `board_get` (ui → cloud)

The board watches EVERY goal at once, where every other frame is about one goal. The
cloud DERIVES these — it already routes every frame a goal produces (`dispatch`,
`plan_ready`, `task_update`, `status`, `proposal`, `approval`), so it can fold them
into a summary without asking anyone. The device is not involved.

```json
{ "type": "board_snapshot", "board_seq": 42,
  "goals": [ GoalSummary, ... ] }

{ "type": "board_update", "board_seq": 43, "goal": GoalSummary }

{ "type": "board_get" }
```

`GoalSummary`:

```json
{ "goal_id": "uuid", "client_ref": "g-17",
  "title": "Birthday Party Preparation",
  "subtitle": "Sun, Jun 22 • 20 Guests",
  "domain": "guest_dinner",
  "state": "on_track" | "at_risk" | "waiting" | "completed",
  "task_status": "monitoring",
  "progress_pct": 68,
  "next_step": "Buy party decorations",
  "eta": "2026-06-22",
  "pending_tasks": 3,
  "alerts": { "count": 2, "severity": "danger" | "warn" | null },
  "activity": ["Grocery delivery confirmed", "Decor ideas ready"],
  "updated_at": "<ISO>" }
```

**Semantics:**

- A **full `board_snapshot`** on UI bind and in reply to `board_get`. **`board_update`
  carries a WHOLE `GoalSummary`**, replace-by-`goal_id` — deltas exist to avoid
  re-sending N goals, not to save bytes within one. Whole-object replacement is
  idempotent, so a duplicate or out-of-order update cannot corrupt a card.
- **`board_seq` is monotonic per session.** A UI that sees a gap sends `board_get` and
  heals. Without it a dropped `board_update` leaves a card permanently stale with no way
  to notice.
- `state` is the board's four chips. `waiting` covers *anything* waiting on a human or
  on the world (an open gate, an approval, a queued plan, a failed precheck) —
  deliberately: from the board, "someone needs to do something" is one idea.
- `alerts.count` is what is **OUTSTANDING right now, not a running tally of everything
  that ever needed attention** (v3.6.1). An adaptation raises an alert; approving it —
  *or declining it* — clears that alert (leaving "adapting" is the test, not having
  executed something). A tick that resolves one alert and raises another nets to the new
  one, so the count tracks open items, matching the card's own "N alerts — tap to review".
- `activity` is the log of things that have **actually occurred** — completed task
  titles. A pending proposal is NOT activity (it is waiting on a person and belongs in
  `next_step`); writing one here made the card print the same sentence as both done and
  next (fixed v3.6.2).

### `goal_state_get` (ui → cloud)

```json
{ "type": "goal_state_get", "goal_id": "..." }
```

Re-sends the cached `present_plan` for a goal, then its latest `status`. This is what
makes drilling into a day-old goal show a plan instead of an empty stage after a reload.
The `agent_event` stream is deliberately NOT replayed: a rejoined view shows the plan and
its ticks, not a re-run of the thinking.

### `goal_accepted` (cloud → ui) + `user_goal.client_ref`

```json
{ "type": "user_goal", "text": "...", "client_ref": "g-17" }
{ "type": "goal_accepted", "goal_id": "uuid", "client_ref": "g-17" }
```

With two goals in flight the UI **cannot tell which inbound `goal_id` is which
submission** — it would have to adopt whichever arrives first, and mis-key the card.
`client_ref` is UI-minted and echoed straight back, so an optimistic card re-keys to the
real `goal_id`. Optional: a v2 client that omits it still works.

**v3.1 — the board is no longer read-mostly.** Once a goal's initial plan is approved
on the chat UI, the board becomes the goal's primary surface: it renders the raw device
stream on a per-goal detail page (`present_plan`, `agent_event`, `status`, `proposal`)
and it SENDS `control` (world-event / demo-clock commands) and `approval` (world-event
adaptation decisions). The chat UI keeps goal CREATION — the understanding gate and the
initial tiered approval; the board keeps everything after. Both are still just `role:ui`
sockets to the hub, distinguished only by which frames each renders. *"The cloud does
not route by surface"* held absolutely through v3; as of v4.1 it holds for every
surface EXCEPT `"input"`, which is forked at delivery (see *Surface-aware delivery*
below) — `chat` and `board` stay on full broadcast, their split temporal, not
type-based. `goal_state_get` (above) is what refills the board's detail page for a goal
it never saw start.

## Surface-aware delivery (v4.1)

One fork, in ONE place. Every cloud→ui frame for a session goes through a single
fan-out point (`ConnectionRegistry.send_to_uis`); v4.1 adds a per-surface **interest
predicate** consulted there, in the send loop, per target socket — no per-call-site
routing table, no new send paths. The same predicate gates the bind-time pushes
(`capabilities` replay, `board_snapshot`) in `_bind_ui`.

| surface (from `hello`) | receives via session fan-out |
|---|---|
| *(absent / empty — every v3 client)* | **everything** (unchanged) |
| `"chat"` | **everything** (unchanged — its client-side `INBOUND_TYPES` allowlist keeps doing the filtering) |
| `"board"` | **everything** (unchanged — same) |
| `"input"` | ONLY: `hello_ack`, `goal_accepted`, `chat_ui_open`, `chat_ui_close`, `notice` |

Anything not in the `input` row is simply NOT DELIVERED to an input socket — no
`agent_event`/`status`/board firehose for a native client that would only drop it.
Handshake/discovery frames sent point-to-point to a specific socket (`hello_ack`,
`devices`) are outside the fork and always delivered — an input surface still needs the
device picker to bind. Forking is deliberately NOT extended to `chat`/`board`: their
allowlists already work, the webview lifecycle already scopes the chat UI in time, and
a server-side per-type table would reintroduce the "forked to nobody = silently
dropped" failure mode this contract warns about at the top.

**Create-phase replay cache.** The cloud keeps, per session, the CURRENT create-phase
goal's state: `{ goal_id, goal_text, understanding?, present_plan?, notice? }` — the exact frames
it broadcast (the `present_plan` including `payload.knew`), captured as they are sent.
Lifecycle:

- **created** when `chat_ui_open` is emitted (the goal becomes the session's
  create-phase goal); `understanding` is captured at emission; `present_plan` at
  `plan_ready` handling; a TERMINAL `notice` (`out_of_scope` / `declined`, never the
  mid-save `updating_goals`) at emission — for the create-phase goal only;
- **cleared** when `chat_ui_close` is emitted (any of its three triggers);
- **replaced** wholesale by a superseding `chat_ui_open` for a new goal.

On bind of a socket whose surface is `"chat"` (and only `"chat"` — a legacy
absent-surface client keeps its exact v3 behaviour), after the existing bind-time
pushes the cloud REPLAYS: `chat_ui_open { goal_id }`, then the cached `notice` if there
is one (alone — it is terminal, and the chat UI clears the stage on it), else the cached
`understanding` (if the plan hasn't arrived yet) or the cached `present_plan` (if it
has). This
mirrors how the board rehydrates via `board_snapshot` on bind, and it closes the race
between the webview connecting and `understanding` being computed: connect early and
the frames arrive by broadcast (the `chat_ui_open`-before-`understanding` ordering
guarantees the reset lands first); connect late and the replay delivers the same
sequence. The `notice` half exists for the same race in its sharpest form: a REFUSAL has
no round-trip in it at all — the cloud opens the bracket and broadcasts the notice in the
same breath, while Bixby is still mounting the iframe off that open, so the frame reached
a socket that had not connected. Uncached, the user watched a blank webview appear and
close 4.5s later. Either way the webview paints the current goal — and ONLY the current goal,
because the `chat_ui_open` reset discarded everything else. No cache (create phase
over or never started) ⇒ no replay ⇒ the webview shows its idle state, and Bixby has
already closed it anyway.

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
