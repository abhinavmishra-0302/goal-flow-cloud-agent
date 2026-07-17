# AGENTS.md — goal-flow-cloud-agent (coding-session guide)

Context for an AI/coding session working in this repo. Read this first; it is the
fast path to being productive without re-deriving the architecture.

## What this repo is

The **cloud agent** of GoalFlow — a two-tier, goal-based agent POC for the Samsung
Tizen Family Hub. This tier owns **conversation, memory, and human-in-the-loop
orchestration**. It is a FastAPI WebSocket **hub**: the UI and the device each open
one outbound WS to it; the UI and device NEVER talk directly. Driving use case:
*"help my family eat healthier this week and reduce food waste"* → an adaptive,
approval-gated weekly dinner plan (also a `guest_dinner` domain).

Sibling repos (all under `~/ashu/git/`): `goal-flow-agent-chat-ui` (React UI),
`goal-flow-device-agent-ubuntu` (.NET/SK device brain), `goal-flow-device-agent-tizen`
(frozen port), `goal-flow-agents` (cross-cutting docs). The **canonical wire
contract lives HERE**: `CONTRACT.md` (mirrored as `types/contract.ts` in the UI and
`Contracts/*.cs` in the device). Change `CONTRACT.md` first when the protocol moves.

## Stack & run

- Python 3.11+, FastAPI + `uvicorn`, **LangGraph** (StateGraph + `interrupt()` HITL +
  `MemorySaver` checkpointer, `thread_id = goal_id`). **LLM-only** via OpenRouter —
  no scripted/rules fallback; it fails loudly.
- Run the hub: `./run.sh` (sources `.env`, runs `uvicorn goalflow_cloud.server:app`
  on `WS_HOST:WS_PORT`, default `0.0.0.0:8000`, WS endpoint `/ws`).
- Headless graph sanity check (no hub/sockets): `python scripts/run_graph_demo.py "<goal>"`.
- Env (`.env`, see `.env.example`): `OPENROUTER_API_KEY` (required),
  `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL` (default `openai/gpt-oss-120b` — the
  `:free` variants are 429-throttled/unusable), `OPENROUTER_MAX_TOKENS`,
  `WS_HOST`/`WS_PORT`, `LOG_LEVEL`.

## Architecture / key files

- `src/goalflow_cloud/server.py` — the `/ws` hub. `ConnectionRegistry` is **multi-session**:
  a `Session` (one device agent + N UIs) is keyed by `device_id`, so multiple homes can be
  connected at once. A device reconnect 1012-evicts only the prior socket of the SAME
  `device_id`; other homes are untouched. UI sockets are never evicted. A UI binds via
  `hello.device_id`, auto-binds when exactly one device is online, or is held UNBOUND
  (gets a `devices` list, binds later via `select_device`) — frames from an unbound UI
  are dropped. Dispatch table routes inbound frames by `type` within the sender's
  session; every frame is logged with a correlation id (this log is the presenter
  "Show agent flow" feed).
- `src/goalflow_cloud/graph/nodes.py` — the LangGraph StateGraph. Node flow:
  `interpret_goal → load_memory → present_understanding [interrupt] → build_contract
  → dispatch_to_device → collect_plan [interrupt] → hitl_approval [interrupt] →
  relay_decisions → monitor → finalize`, with branches `goal_declined` (user declined
  the understanding) and `explain_block`. Router: `route_after_understanding`.
  The interpreter picks `domain` by preferring one of the device's advertised
  `capabilities.domains[].id` values (the device routes on the EXACT string), coining
  a new slug only when none fit — there is NO `_canonical_domain()` normalizer (that
  was the pre-M4 keyword hack; removed). M7's device advertises `meal_plan`,
  `guest_dinner`, `vacation_prep`, `birthday_party`. The meal-plan `time_window` is
  pinned today..today+6 (the one remaining domain-specific carve-out in cloud code).
- `src/goalflow_cloud/models/contract.py` — Pydantic mirror of every wire message.
  `_ContractModel` uses `extra="allow"` so new nested device fields (e.g.
  `demo_events`, `updated_plan`) pass through without cloud changes.
  `Control.command` Literal includes `"trigger_event"`.
- `src/goalflow_cloud/memory/store.py` + `data/memory/family_profile.json` — memory.
  **Hard constraints** (allergies, medical) are injected deterministically into the
  contract's `constraints.hard`; **soft prefs** only bias. The safety gate on the
  device reads `constraints.hard` and nothing else.

## Contract touchpoints (what the cloud sends/receives)

Receives from UI: `user_goal`, `understanding_response`, `approval`, `control`.
Receives from device: `capabilities`, `agent_event` (stream), `plan_ready`,
`proposal`, `status`. Sends to UI: `understanding`, `present_plan` (adds `knew`,
relays `demo_events`), `agent_event`, `proposal`, `status`. Sends to device:
`dispatch` (the generic Task Contract), `approval`, `control`. See `CONTRACT.md` for
exact shapes — it is authoritative and current (includes `understanding`,
`trigger_event`, `demo_events`, `event_id`, `updated_plan`/`changed_ids`).

## Current state

Fully built and verified end-to-end (headless + live browser): interpret → memory →
**confirm-understanding gate** → contract → device plan (streamed) → present → tiered
HITL approval → monitor. Both `meal_plan` and `guest_dinner` domains. The cloud made
NO logic change for the event-driven meal demo — `trigger_event` control + device
`demo_events`/`updated_plan` flow through by pass-through.

## Conventions & gotchas

- **Commit identity:** author as `ashuksingh11`
  (`31301999+ashuksingh11@users.noreply.github.com`). **Push only when explicitly asked.**
- **Workflow (per the human):** plan=Opus · design/architecture=Fable · coding=Codex CLI
  · browsing=Sonnet. Confirm before moving between phases.
- LLM-only by design — do NOT add scripted/rules fallbacks.
- There is a known benign log-noise TODO: `receive_json` on an already-closed socket
  can raise; it's caught/logged, not fatal.
- The device streams ~950 thinking frames per run; the UI coalesces them.
- **Known limitation:** `device_id` scopes message *delivery* (multi-session) and
  `goal_id` scopes each graph run (already isolated), but `memory/store.py` /
  `family_profile.json` is still GLOBAL — shared across every session, not per-home.
- **Known risk:** `MemorySaver` + the `asyncio.to_thread` fan-out in `server.py` isn't
  proven thread-safe under truly-parallel graph writes (concurrent sessions hitting the
  graph at once). If races surface, wrap the graph invoke/resume/`update_state` calls in
  one `asyncio.Lock` — cloud graph steps are fast; the LLM work happens on the device.
