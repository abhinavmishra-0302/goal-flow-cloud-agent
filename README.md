# goal-flow-cloud-agent

The **cloud tier** of GoalFlow — a two-tier,
general goal-based agent for the Samsung Family Hub.

GoalFlow is not a meal application. Meal planning and guest-dinner preparation are just *domains*
riding the same domain-agnostic harness.

This tier owns the **goal**: it interprets fuzzy text into a generic Task Contract, resolves
household policy, drives a **LangGraph** state graph with durable human gates, dispatches to the
on-device agent, and relays its live stream to the surfaces.

**It is also the hub.** Every frame in the system routes through here.

---

## Role in the system

```
Bixby · Chat UI · Board UI   <--WS-->   CLOUD (this repo)   <--WS-->   DEVICE agent
```

- The cloud is the **WebSocket server**. Every other process opens **one** outbound socket to it
  and registers with a `hello` frame.
- **A surface and the device never talk directly.**
- The cloud owns the goal; the device owns the plan, local truth and the actuators.

Two gates, and they are different in kind — **"LLM plans, code checks"**:

| Gate | Where | Behaviour |
|---|---|---|
| **Safety** | Deterministic code, on the device | It **blocks**. It enforces `constraints.hard` and nothing else |
| **Approval** | The user, through a durable LangGraph `interrupt()` | It **waits** |

The shared protocol is **[`CONTRACT.md`](CONTRACT.md)**, and **this is the canonical copy**. The
surfaces and the device mirror it as typed definitions. It is generic: a `domain` string names
the use case, and domain specifics live in the device's capability modules plus the free-form
`scope` and `context` objects.

---

## What the cloud does, per goal

1. **Interprets** the natural-language goal through a structured-output LLM call into
   `{domain, objective, title, success_criteria, scope, time_window}`, with the window relative
   to the day **the device is on** — never hardcoded, and never the hub machine's date.
   It also returns an **actionability verdict**, judged against the capabilities the connected
   device advertises rather than against a fixed list of topics.
2. **Detects a stated household rule.** *"We've gone vegan"* is a statement, not a goal. It is
   **proposed** for confirmation, never written on the model's say-so.
3. **Resolves the household constraint store for this goal.** Every entry carries its source,
   scope and expiry. The **hard** block is assembled **by code**: allergens, dietary and medical
   rules unioned across the whole store and never narrowed by relevance; caps and windows picked
   per domain. So a vacation goal carries a travel cap and an away window rather than the weekly
   grocery cap. The model's only say is which **soft** preferences are relevant, and a soft
   preference can never block anything.
4. **Presents its understanding, and waits.** The graph parks at a durable `interrupt()`, and
   the surface shows what it understood, which rules it will hold, and which preferences will
   shape the plan. Nothing reaches the device until the user answers.
5. **Builds and validates** the generic `dispatch` Task Contract and sends it to the device,
   which does the actual planning.
6. **Relays the live stream** — the device's `agent_event` frames pass through untouched.
7. **Presents the plan** and **holds the approval pause**. Nothing firm executes before it.
8. **Monitors and adapts.** A material change re-enters the approval loop.

**LLM-only, no fallbacks.** There is no scripted planner behind the LLM call. A failure surfaces
as a structured error. It is never faked.

---

## The board fold

Alongside the per-goal graph and the household memory, the cloud runs a third tier: the **board
fold** (`board.py`).

Every other frame is about *one* goal. The board is the session-level view of *all* of them. The
hub already sees every frame a goal produces, so it folds them into **one `GoalSummary` per
goal** and broadcasts a snapshot on bind and a delta on change.

> **The fold is deterministic — no LLM, no I/O.** Every number on a board card is *derived* here
> from something the device actually said, never guessed.

---

## Run it

Requires Python 3.11 or later.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # set OPENROUTER_API_KEY — required, LLM-only
./run.sh                      # uvicorn on 0.0.0.0:8000, loads .env
```

Clients connect to `ws://localhost:8000/ws`, and the first frame must be a `hello`:

```json
{ "type": "hello", "role": "ui",     "surface": "board" }
{ "type": "hello", "role": "device", "device_id": "9f3c…" }
```

Sanity-check the graph without the hub — it runs interpret, memory and contract, then prints the
dispatched Task Contract for any goal text:

```bash
python scripts/run_graph_demo.py "we've got 6 people over Saturday for dinner - sort it"
```

**For the full five-process demonstration**, follow
`../goal-flow-agents/docs/FINAL_DEMO.md`
— the single source of truth for run commands.

### Environment

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — | **Required.** Interpretation is LLM-only |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | An OpenAI-compatible endpoint |
| `OPENROUTER_MODEL` | `openai/gpt-oss-120b` | The `:free` variants are throttled and unusable |
| `OPENROUTER_PROVIDER_ORDER` | — | **Pin it.** See below |
| `OPENROUTER_PROVIDER_ALLOW_FALLBACKS` | — | `false`, deliberately |
| `GOALFLOW_PROFILE_PATH` | — | Point the constraint store at a scratch copy |
| `FISH_API_KEY` | — | Text to speech. Absent means the voice is simply never sent |
| `SPEECH_ENABLED` | `true` | Silence the voice while leaving the key in place |
| `WS_HOST` / `WS_PORT` | `0.0.0.0` / `8000` | |
| `LOG_LEVEL` | `INFO` | Structured, correlation-id tagged |

> **Pinning the provider is the whole difference.** Unpinned, OpenRouter load-balances this model
> across nineteen endpoints spanning 39 times in throughput and lands on the slow ones —
> interpretation measured **19.7 s unpinned against 2.3 s pinned**. Fallbacks are off because the
> next-best provider measured 203 to 234 s on the real pipeline, *slower than sending no
> preference at all*: a fallback here is a stall, not a degrade.

---

## Layout

```
CONTRACT.md                       # THE canonical wire protocol
data/memory/family_profile.json   # the household constraint store — one entry per fact
src/goalflow_cloud/
  server.py                       # the WS hub: sessions, routing, the create bracket, speech
  graph/nodes.py                  # the StateGraph: nodes, routers, four interrupts, retries
  board.py                        # BoardService — the deterministic fold
  models/contract.py              # the Pydantic mirror of every frame
  memory/store.py                 # load, resolve per goal, append captures
  config.py                       # env-backed settings
  speech/                         # the cues, the client, and the utterance registry
scripts/verify_*.py               # the gates — one script each
scripts/e2e_two_goals.py          # NOT a gate: the real two-goal demo, headless
run.sh
```

---

## Verify

There is no test framework here, by choice. The gates are scripts that pin observable behaviour.

```bash
python scripts/verify_mirrors.py       # gate 14 — no mirror or allowlist has drifted
python scripts/verify_constraints.py   # gate 15 — resolution per goal
python scripts/verify_crossgoal.py     # gate 17 — the only path that changes a plan without asking
```

Everything except gates 10 and 12 runs with **no API key**. Gate 28's interpreter half needs one
and **skips** without it, rather than passing — a gate that cannot run must not be able to pass.

> **A gate you have not broken is a gate you do not trust.** Reintroduce the bug, watch it fail,
> restore.

---

## Read more

| Document | What it is |
|---|---|
| [`CONTRACT.md`](CONTRACT.md) | The canonical protocol. Change it **first** |
| [`AGENTS.md`](AGENTS.md) | The coding-session guide: the run sheet, the traps, the conventions |
| [`CODE_GUIDE.md`](CODE_GUIDE.md) | The code walkthrough |

The system-level explanation lives in the GoalFlow wiki —
04 — The cloud graph (`../goal-flow-agents/wiki/04-cloud-graph.md`),
03 — The wire (`../goal-flow-agents/wiki/03-the-wire.md`),
05 — Constraints (`../goal-flow-agents/wiki/05-constraints.md`).
