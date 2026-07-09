# Code Guide — goal-flow-cloud-agent

The **cloud agent** is the hub of GoalFlow: a FastAPI WebSocket server that owns the conversation
and memory, turns the user's fuzzy goal into a **Task Contract** via a LangGraph + LLM pipeline,
and relays every message between the UI and the device. See the repo `README.md` for run steps and
`../goal-flow-agents/docs/SYSTEM_OVERVIEW.md` for the whole system.

> **Note:** `README.md` describes M1 scope; this guide reflects the finished M1–M4 build.

## File map

```
CONTRACT.md                       # canonical frozen wire protocol (source of truth)
scripts/run_graph_demo.py         # runs the graph on the sample goal, prints the contract
data/memory/family_profile.json   # mocked family memory (hard + soft + context)
src/goalflow_cloud/
  config.py                       # env-backed settings (OPENROUTER_*, WS_HOST/PORT)
  server.py                       # FastAPI app + WS hub: registry, routing, relays  ← start here
  models/contract.py              # Pydantic models mirroring every CONTRACT.md message
  memory/store.py                 # loads family_profile.json
  graph/nodes.py                  # LangGraph nodes: ambiguity → memory → decompose
docs/ARCHITECTURE.md, docs/diagrams.md
```

## How it works (request flow)

1. **`server.py`** hosts the WS endpoint `/ws`. Each client connects and sends a `hello` frame
   (`role: ui | device`); the server keeps an in-memory **connection registry** keyed by role
   (one active connection per role) and replies `hello_ack`. It **routes purely on the `type`
   field** and logs every frame (this log feeds the UI presenter mode).
2. On **`user_goal`** (from the UI), the server calls **`graph.nodes.build_dispatch_frame(text)`**,
   which runs the LangGraph graph:
   - **ambiguity node** — normalizes the fuzzy goal into a concrete intent (weekday dinners, this week).
   - **memory node** — loads `family_profile.json` (hard block + soft block + context).
   - **decompose node** — calls the LLM (OpenRouter) to fill objective/scope/optimization/context
     and produce the Task Contract.
   - **Deterministic guardrails:** `constraints.hard` is copied **verbatim** from memory (never
     LLM-sourced — a missed allergen is a health incident); `autonomy` is hard-set to `propose_all`.
   - The server then sends the contract to the device as a **`dispatch`** frame.
3. On **`plan_ready`** (from the device), the server attaches a **`knew`** summary (the "what it
   knew" personalization, built from the contract it dispatched for that `goal_id`), passes the
   device's **`impact`** metrics through untouched, and relays it to the UI as **`present_plan`**.
4. **`approval`** and **`control`** frames from the UI are forwarded to the device; **`status`** and
   **`proposal`** frames from the device are forwarded to the UI. The cloud is a pure relay for
   these — it never touches device state.

## Key design points

- **Hard vs soft memory split.** `family_profile.json` has a `hard` block (safety-critical, injected
  verbatim) and a `soft` block (preferences, only bias the LLM). This split is enforced in
  `graph/nodes.py`.
- **Demo-safe LLM.** The LLM call is wrapped: on 429 / timeout / empty content / JSON parse failure,
  the graph falls back to a scripted contract and logs it — the demo never hangs on an LLM hiccup.
- **Reasoning model.** `OPENROUTER_MODEL=openai/gpt-oss-120b` spends tokens reasoning; the graph uses
  a generous token budget and reads the final `content`.

## Run & verify

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
python scripts/run_graph_demo.py                          # prints the real LLM-built contract
uvicorn goalflow_cloud.server:app --host 127.0.0.1 --port 8000
```

## Extending it

- **New message type:** add a Pydantic model in `models/contract.py`, add a route branch in
  `server.py`, and update `CONTRACT.md` (+ the UI/device mirrors).
- **Smarter clarification:** flesh out the ambiguity node in `graph/nodes.py` into a real multi-turn
  loop (it's currently a single scripted pass).
- **Real memory:** replace `memory/store.py`'s JSON read with a vector store for soft prefs — but
  keep the hard block a deterministic, non-semantic lookup.
