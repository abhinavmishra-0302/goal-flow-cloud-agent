# goal-flow-cloud-agent

Cloud agent for **GoalFlow v2** — a two-tier, **general goal-based agent** for the
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

The shared protocol is **CONTRACT v2** — see [`CONTRACT.md`](CONTRACT.md) (this file
is the canonical copy; the UI and device repos mirror it as typed definitions). The
protocol is **generic and domain-agnostic**: no meal-specific fields anywhere. A
`domain` string (`"meal_plan"`, `"guest_dinner"`, ...) names the use case; domain
specifics live in the device's capability modules plus the free-form
`scope` / `context` objects.

## What the cloud does per goal

1. **Interprets** the natural-language goal via a real LLM structured-output call
   (OpenRouter) into `{domain, objective, success_criteria, scope, time_window}` —
   the time window computed **relative to real today**, never hardcoded.
2. **Loads memory** (`data/memory/family_profile.json`) with a strict split:
   the **hard** block (allergens, medical, dietary, budget_cap, quiet_hours) is
   injected **verbatim** into `constraints.hard` as pure data — the LLM never
   generates, edits, or paraphrases the safety policy; **soft** preferences and
   family context only bias planning.
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
CONTRACT.md                     # canonical CONTRACT v2 (generic wire protocol)
docs/ARCHITECTURE.md            # cloud architecture: graph, memory, hub, logging
docs/diagrams.md                # Mermaid sequence + component diagrams
scripts/run_graph_demo.py       # run the graph on a goal, print the contract
src/goalflow_cloud/
  config.py                     # env-backed settings (OPENROUTER_*, WS_*, LOG_LEVEL)
  server.py                     # FastAPI WS hub: registry, routing, relays, graph driving
  models/contract.py            # Pydantic mirror of every CONTRACT v2 message
  graph/nodes.py                # the LangGraph StateGraph: nodes, routers, interrupts
  memory/store.py               # family profile loader + hard/soft split
data/memory/family_profile.json # generic family memory (hard + soft + context)
run.sh
```

See [`CODE_GUIDE.md`](CODE_GUIDE.md) for the code walkthrough and
`docs/ARCHITECTURE.md` for the full design. The system-level v2 framing (the 11
harness modules, demo pair, decisions) lives in
`../goal-flow-agents/docs/V2_DESIGN_PROPOSAL.md`.
