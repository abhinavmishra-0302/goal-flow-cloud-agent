# Cloud Agent Architecture

## Role

The cloud agent is one of three tiers in GoalFlow:

| Tier   | Owns                                   | Never does                          |
|--------|----------------------------------------|-------------------------------------|
| UI     | Conversation surface, approval clicks  | Talks to the device directly        |
| Cloud  | **Talk + memory**, goal decomposition, routing | Touches actuators / local state |
| Device | **Local truth**, actuators, safety gate | Talks to the UI directly           |

The cloud:

1. Owns the **conversation** with the human and the **family memory**.
2. Resolves ambiguity in the user's goal (M2).
3. Decomposes the goal into a frozen-shape **Task Contract** (`dispatch`, see
   [`../CONTRACT.md`](../CONTRACT.md)).
4. **Relays**: routes device output (`plan_ready`, `proposal`, `status`) to the
   UI and user decisions (`approval`) back to the device.

Two gates, deliberately distinct:

- **Safety gate** — deterministic **code on the device**; reads only
  `constraints.hard`; *blocks*.
- **Approval gate** — the **user via the cloud**; *waits*.

Slogan: **"LLM plans, code checks."**

## WebSocket hub (M1)

`src/goalflow_cloud/server.py` — FastAPI app exposing a single WS endpoint
(`/ws`).

- **Connection registry keyed by role.** On connect, a client's first frame
  must be `hello` with `role: "ui" | "device"`. The hub replies `hello_ack`
  with a `session_id` and stores the socket in `registry[role]` (one active
  connection per role for the POC; a reconnect replaces the entry).
- **Routing rules** (on `type` + sender role):

  | Incoming `type` | From   | Cloud action                                             |
  |-----------------|--------|----------------------------------------------------------|
  | `user_goal`     | ui     | M1: build **hardcoded** `dispatch`, send to device. M2: run LangGraph pipeline first. |
  | `plan_ready`    | device | Re-wrap as `present_plan`, send to UI (may add display hints). |
  | `proposal`      | device | Relay to UI for a user decision.                         |
  | `status`        | device | Relay to UI.                                             |
  | `approval`      | ui     | Relay to device (correlated by `correlation_id`).        |

- **Reliability:** clients reconnect on drop; the hub dedupes device messages
  on `correlation_id` (a seen-set per `goal_id` is enough for the POC).
- The UI and device **never** talk directly; the hub is the only path.

## LangGraph pipeline (M2 — signatures only today)

`src/goalflow_cloud/graph/nodes.py` defines the node chain that turns a raw
`user_goal` into a `dispatch`:

```
user_goal → [ambiguity] → [memory] → [decompose] → [relay] → dispatch
```

- **ambiguity** — decide whether the goal is actionable; if not, formulate a
  clarifying question back to the UI instead of proceeding.
- **memory** — load the family profile and split it:
  - **hard constraints** (allergens, dietary, medical) are **injected verbatim**
    into `constraints.hard` of the contract — *never* left to LLM semantics,
    because the device safety gate reads exactly this block;
  - **soft preferences** (dislikes, prefer, notes) go into `constraints.soft` /
    `context_hints` and only **bias** planning.
- **decompose** — LLM call (OpenRouter) that fills the rest of the Task
  Contract: objective, scope, time_window, optimization, autonomy.
- **relay** — validate against the Pydantic contract models and hand off to the
  hub for dispatch to the device.

### LLM access

Via **OpenRouter** (OpenAI-compatible; `base_url` from `OPENROUTER_BASE_URL`,
default `https://openrouter.ai/api/v1`) using `langchain-openai`. Default model
`anthropic/claude-sonnet-5` — configurable via `OPENROUTER_MODEL`. A
**scripted/mock fallback** implements the same interface so the pipeline runs
without a key or network.

## Memory (M2 — loader signature only today)

`src/goalflow_cloud/memory/store.py` reads
`data/memory/family_profile.json` (mocked for the POC — "fake the world").

The split is the important design decision:

- `hard` → copied field-for-field into `dispatch.constraints.hard`. This is a
  **data path**, not a prompt path.
- `soft` + members + context → prompt context for the decompose node only.

## Module map

```
src/goalflow_cloud/
  config.py            # Settings from env (.env.example documents them)
  server.py            # FastAPI + WS hub, connection registry, router   [M1]
  models/contract.py   # Pydantic mirror of Contract v0 (canonical: CONTRACT.md)
  graph/nodes.py       # ambiguity / memory / decompose / relay nodes    [M2]
  memory/store.py      # family_profile.json loader                      [M2]
data/memory/family_profile.json
```
