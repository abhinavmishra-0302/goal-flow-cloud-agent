# goal-flow-cloud-agent

Cloud agent for the **GoalFlow** POC — a two-tier, goal-based agent orchestration
demo (Samsung Tizen Family Hub). The cloud agent owns **conversation + memory**,
resolves ambiguity, decomposes the user's goal into a **Task Contract**, and
relays messages between the UI and the on-device agent.

> POC ethos: *"fake the world; make the mechanism real."*

## Role in the system

```
UI (tablet chat)  <--WS-->  CLOUD (this repo, hub)  <--WS-->  DEVICE agent
```

- The cloud is the **WebSocket hub/server**. The UI and the device each open
  one outbound WS connection to it and register a role via a `hello` frame.
- **The UI and the device NEVER talk directly** — everything routes through here.
- Cloud owns talk/memory; device owns local truth.
- Two distinct gates: the **safety gate** is deterministic *code on the device*
  (blocks); the **approval gate** is the *user via the cloud* (waits).
  Slogan: **"LLM plans, code checks."**

The shared protocol is frozen as **Contract v0** — see [`CONTRACT.md`](CONTRACT.md)
(this file is the canonical copy; the UI repo mirrors it as TypeScript types).

## Scope by milestone

### M1 — thin vertical slice (current)
- FastAPI + WebSocket hub (`/ws`), connection registry keyed by role (`ui`, `device`).
- UI sends `user_goal` → cloud dispatches a **HARDCODED** Task Contract to the
  device → device returns a **CANNED** `plan_ready` → cloud relays `present_plan`
  to the UI, which renders it.
- Family memory is read only to inject `constraints.hard` and
  `constraints.soft` into the fake dispatch.
- No LLM, no LangGraph in M1.

### M2+ — the real mechanism
- LangGraph pipeline: `ambiguity → memory → decompose → relay` nodes
  (see `src/goalflow_cloud/graph/`).
- Family memory (`data/memory/family_profile.json`): **hard constraints** are
  injected verbatim into the contract's `constraints.hard` (never left to
  LLM semantics); **soft preferences** only bias planning.
- LLM via **OpenRouter** (OpenAI-compatible), default model
  `anthropic/claude-sonnet-5` (configurable via `OPENROUTER_MODEL`), with a
  scripted/mock fallback behind the same interface.
- Approval routing (`approval` → device), adaptation (`proposal` → UI), status relay.

## How to run

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # fill in OPENROUTER_API_KEY (not needed for M1)
./run.sh                      # uvicorn goalflow_cloud.server:app
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

## Environment variables

| Variable              | Default                          | Notes                       |
|-----------------------|----------------------------------|-----------------------------|
| `OPENROUTER_API_KEY`  | —                                | M2+; not needed for M1      |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1`   | OpenAI-compatible endpoint  |
| `OPENROUTER_MODEL`    | `anthropic/claude-sonnet-5`      | Any OpenRouter model id     |
| `WS_HOST`             | `0.0.0.0`                        |                             |
| `WS_PORT`             | `8000`                           |                             |

## Repo layout

```
CONTRACT.md                     # canonical frozen Contract v0
docs/ARCHITECTURE.md            # cloud agent architecture
docs/diagrams.md                # Mermaid sequence + component diagrams
src/goalflow_cloud/
  config.py                     # env-backed settings (stub)
  server.py                     # FastAPI app + WS hub (M1)
  models/contract.py            # Pydantic mirror of Contract v0
  graph/nodes.py                # LangGraph node signatures (M2)
  memory/store.py               # family profile loader signature (M2)
data/memory/family_profile.json # mocked family memory
run.sh
```
