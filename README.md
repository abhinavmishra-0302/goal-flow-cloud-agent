# goal-flow-cloud-agent

Cloud agent for **GoalFlow v3** — a two-tier, **general goal-based agent** for the
Samsung Family Hub. GoalFlow is not a meal app: meal planning and guest dinner prep
are just *domains* riding the same domain-agnostic harness. The cloud tier owns
**conversation + memory**, LLM-interprets the user's fuzzy goal into a **generic
Task Contract**, drives an advanced **LangGraph StateGraph** (conditional edges,
`interrupt()`-based HITL, checkpointer), dispatches to the on-device agent, and
relays its live `agent_event` stream to the UI.

## Role in the system

```
UI (tablet chat)  <--WS-->  CLOUD (this repo, hub)  <--WS-->  DEVICE agent (SK planner)
```

- The cloud is the **WebSocket hub/server**. The UI and the device each open one
  outbound WS to it and register a role via a `hello` frame.
- **The UI and the device NEVER talk directly** — everything routes through here.
- Cloud owns talk/memory/HITL; device owns local truth, the SK function-calling
  planner, capability modules, and the deterministic Safety filter.
- Two distinct gates — **"LLM plans, code checks"**:
  - **Safety gate**: deterministic *code on the device*; enforces only
    `constraints.hard`; *blocks*.
  - **Approval gate**: the *user via the cloud* (a durable LangGraph
    `interrupt()`); *waits*.

The shared protocol is **`CONTRACT.md`** — see [`CONTRACT.md`](CONTRACT.md) (this file
is the canonical copy; the UI and device repos mirror it as typed definitions). The
protocol is **generic and domain-agnostic**: no meal-specific fields anywhere. A
`domain` string (`"meal_plan"`, `"guest_dinner"`, ...) names the use case; domain
specifics live in the device's capability modules plus the free-form
`scope` / `context` objects.

## What the cloud does per goal

1. **Interprets** the natural-language goal via a real LLM structured-output call
   (OpenRouter) into `{domain, objective, success_criteria, scope, time_window}` —
   the time window computed **relative to real today**, never hardcoded.
2. **Resolves the household constraint store** (`data/memory/family_profile.json`)
   **for this goal**: every constraint carries its source, scope and expiry, and the
   **hard** block is assembled by code — allergens/medical/dietary unioned across the
   whole store (never narrowed), caps and windows picked per domain, so a vacation
   goal carries a travel cap and an away window instead of the weekly grocery cap.
   The LLM never generates, edits, or paraphrases the safety policy; its only say is
   which **soft** preferences are relevant, and those only bias planning.
3. **Presents its understanding and waits**: the confirm-understanding gate. The
   graph parks at a durable `interrupt()` (`present_understanding`) and sends the
   UI an `understanding` frame — a short LLM-authored summary plus the `knew`
   hard-constraint chips. Nothing is dispatched to the device until the user's
   `understanding_response` resumes it; a decline ends the goal (`goal_declined`)
   before any planning happens.
4. **Builds + validates** the generic `dispatch` Task Contract and sends it to the
   device, which does the actual planning (SK auto function calling).
5. **Relays the live stream**: the device's `agent_event` frames (thinking,
   tool calls, plan progress) pass through to the UI untouched.
6. **Presents the plan**: on `plan_ready`, resumes the graph, adds the
   personalization `knew` block ("what it knew"), and sends `present_plan` to the UI.
7. **Holds the HITL pause**: the graph parks at `interrupt()` with the tiered
   proposals; the user's `approval` resumes it and is forwarded to the device.
   Nothing firm executes until approval.
8. **Monitors + adapts**: `status` / `proposal` frames relay to the UI and feed the
   graph's monitor node; a material change re-enters the approval loop.

## The Agent Board (the board fold)

Alongside the per-goal conversation and the family memory, the cloud runs a **third
tier**: the **board fold** (`src/goalflow_cloud/board.py`, `BoardService`). Every
other frame is about *one* goal; the board is the *session-level* view of *all* goals
at once. The hub already sees every frame a goal produces, so `BoardService` folds
each goal's frames (`understanding` / `dispatch` / `plan_ready` / `task_update` /
`status` / `proposal`) into **one `GoalSummary` per goal**, and broadcasts a
`board_snapshot` (every goal, on UI bind or `board_get`) plus a `board_update` (one
changed goal) to the Agent Board UI. The fold is **deterministic — no LLM, no I/O** —
so every number on a board card (`state`, `progress_pct`, `alerts`, `activity`,
`next_step`) is *derived* here from something the device actually said, never guessed.
See `CODE_GUIDE.md` § "The board fold" for the derivation rules.

**LLM-only, no fallbacks.** There is no scripted/mock planner behind the LLM call.
If the LLM fails, the goal fails loudly with a structured error surfaced to the UI —
it is never faked.

## How to run

Requires Python 3.11+. For the **full three-service demo** (cloud + device + UI),
follow `goal-flow-agents/docs/FINAL_DEMO.md` — the single source of truth for run
commands. To run just the cloud hub:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # set OPENROUTER_API_KEY (required — LLM-only)
./run.sh                      # canonical launcher: uvicorn on 0.0.0.0:8000, loads .env
```

Or directly:

```bash
uvicorn goalflow_cloud.server:app --host 0.0.0.0 --port 8000
```

Connect clients to `ws://localhost:8000/ws`. The first frame must be one of:

```json
{ "type": "hello", "role": "ui" }
```

```json
{ "type": "hello", "role": "device" }
```

Sanity-check the graph without the hub (runs interpret → memory → contract and
prints the dispatched Task Contract for any goal text):

```bash
python scripts/run_graph_demo.py "we've got 6 people over Saturday for dinner - sort it"
```

## Environment variables

| Variable              | Default                        | Notes                                        |
|-----------------------|--------------------------------|----------------------------------------------|
| `OPENROUTER_API_KEY`  | —                              | **Required** — goal interpretation is LLM-only |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible endpoint                   |
| `OPENROUTER_MODEL`    | `openai/gpt-oss-120b`          | Any OpenRouter model id                      |
| `WS_HOST`             | `0.0.0.0`                      |                                              |
| `WS_PORT`             | `8000`                         |                                              |
| `LOG_LEVEL`           | `INFO`                         | Structured, correlation-id-tagged logging    |

## Repo layout

```
CONTRACT.md                     # canonical wire protocol (generic)
scripts/run_graph_demo.py       # run the graph on a goal, print the contract
scripts/verify_board.py         # gate 13: the board fold's numbers are derived and add up
scripts/verify_mirrors.py       # gate 14: the contract mirrors have not drifted
scripts/verify_constraints.py   # gate 15: constraints resolve per goal; the enforced set is never narrowed
scripts/verify_capture.py       # gate 16: a household rule is captured only when the user says yes
scripts/verify_speech.py        # gate 31: the voice says the right thing, and its absence costs nothing
src/goalflow_cloud/
  config.py                     # env-backed settings (OPENROUTER_*, FISH_*, WS_*, LOG_LEVEL)
  server.py                     # FastAPI WS hub: multi-session registry, routing, relays, graph driving, board pushes
  board.py                      # BoardService: folds every goal's frames into one GoalSummary (deterministic, no LLM)
  models/contract.py            # Pydantic mirror of every contract message
  graph/nodes.py                # the LangGraph StateGraph: nodes, routers, interrupts
  memory/store.py               # constraint store loader + per-goal resolution
  speech/                       # v11: fish.audio TTS client + the utterance registry behind /speech/<id>.mp3
data/memory/family_profile.json # household constraint store (sourced, scoped, expiring)
run.sh
```

See [`CODE_GUIDE.md`](CODE_GUIDE.md) for the code walkthrough. The system-level design — the
two-tier split, the harness, the constraint model, the surfaces — lives in
`../goal-flow-agents/docs/DESIGN.md`.
