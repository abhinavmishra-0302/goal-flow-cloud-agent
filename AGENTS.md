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

Sibling repos (all under `~/ashu/git/`): `goal-flow-agent-bixby-ui` (where the user types),
`goal-flow-agent-chat-ui` (the create-phase surface), `goal-flow-agent-board-ui` (home),
`goal-flow-device-agent-ubuntu` (.NET/SK device brain — source of truth for device code),
`goal-flow-device-agent-tizen` (the port, kept in sync), `goal-flow-agents` (docs — the system
design is `../goal-flow-agents/docs/DESIGN.md`). The **canonical wire contract lives HERE**:
`CONTRACT.md`, mirrored as `types/contract.ts` in three UIs and `Contracts/*.cs` in the device.
Change `CONTRACT.md` first when the protocol moves.

## Stack & run

- Python 3.11+, FastAPI + `uvicorn`, **LangGraph** (StateGraph + `interrupt()` HITL +
  a **`SqliteSaver`** checkpointer at `data/goalflow.db`, `thread_id = goal_id` — a goal
  survives a cloud restart still waiting at its gate). **LLM-only** via OpenRouter —
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
  `interpret_goal → detect_constraints → load_memory → present_understanding [interrupt]
  → build_contract → dispatch_to_device → collect_plan → hitl_approval [interrupt] →
  relay_decisions → monitor → finalize`, with branches `capture_gate` (a stated rule, no
  goal — ends at the notice), `goal_declined`, `decline_out_of_scope`, `explain_block` and
  `precheck_wait`. Routers: `route_after_interpret_or_capture`, `route_after_understanding`,
  `route_on_safety`, `route_on_monitor`.
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
- `src/goalflow_cloud/memory/store.py` + `data/memory/family_profile.json` — the
  **household constraint store** (v6): a library of entries, each with `kind`, `value`,
  `enforcement`, `source` (account/derived/chat), `scope`, `applies_to`, expiry.
  `resolve_constraints()` resolves it PER GOAL — hard list kinds unioned across the
  store regardless of domain (the enforced set is never narrowed), cap/window kinds
  domain-picked, soft picked by a small LLM relevance pass with tag matching as the
  fallback. `constraints.hard` is built by code and the device's safety gate reads it
  and nothing else. Gate: `scripts/verify_constraints.py`.
  `GOALFLOW_PROFILE_PATH` points the store at a scratch copy (the cloud's `--data`);
  an absent path is seeded from the repo's copy on first use. Without it, a demo's
  captures write into the committed seed.
  **v6-M4:** `detect_constraints` proposes rules the user states in chat; only the ids
  returned in `understanding_response.accepted_constraint_ids` are written, via
  `append_constraints` (tighten-only, append-only). A pure statement routes to
  `capture_gate` — the same understanding wire with `capture_only: true`, no board card,
  ending in a `notice` of kind `captured`. Gate: `scripts/verify_capture.py`.
- `src/goalflow_cloud/board.py` — **`BoardService`**, the Agent Board fold: it folds every
  goal's frames (`understanding`/`plan_ready`/`task_update`/`status`/`proposal`) into one
  `GoalSummary` per goal and broadcasts `board_snapshot`/`board_update`. Deterministic, no
  LLM. Every number on a board card is DERIVED here. Key rules (v3.6.1/6.2): `alerts` are
  **outstanding**, not ever-seen — they clear when an adaptation is resolved (approved *or*
  declined); `activity` is only what has occurred (a pending proposal is not activity); and
  day-based progress spans the **goal's own deadline**, so one Advance day can't falsely
  finish it. Regression-covered by `scripts/verify_board.py` (gate 13).

## Contract touchpoints (what the cloud sends/receives)

Receives from UI: `user_goal`, `understanding_response`, `approval`, `control`,
`board_get`, `goal_state_get`, `select_device`, `suggestion_action`.
Receives from device: `capabilities`, `agent_event` (stream), `plan_ready`,
`proposal`, `status`. Sends to UI: `understanding`, `present_plan` (adds `knew`,
relays `demo_events`), `agent_event`, `proposal`, `status`, `board_snapshot`,
`board_update`, `day_advanced`, `suggestions`. Sends to device:
`dispatch` (the generic Task Contract), `approval`, `control`. See `CONTRACT.md` for
exact shapes — it is authoritative and current (includes `understanding`,
`trigger_event`, `demo_events`, `event_id`, `updated_plan`/`changed_ids`).

## Current state

Fully built and verified end-to-end (headless + live browser): interpret → memory →
**confirm-understanding gate** → contract → device plan (streamed) → present → tiered
HITL approval → monitor. Both `meal_plan` and `guest_dinner` domains. The cloud made
NO logic change for the event-driven meal demo — `trigger_event` control + device
`demo_events`/`updated_plan` flow through by pass-through.

## v7 gates

`verify_crossgoal.py` (gate 17) is the newest and the one to read first if you touch the
fan-out: it pins BLAST RADIUS and IDEMPOTENCE for the only path that changes a plan
without asking. It found a real bug on its first run — `append_constraints` was not
idempotent for an identical entry, so a re-sent approval wrote a second away window and
re-planned a goal the user had already watched change.

`verify_close_hold.py` (gate 18) pins the OTHER half of that moment: the create-phase
webview must outlast the work it claims to be doing. The dwell lives in the cloud and
cannot live in the chat UI — Bixby unmounts the webview the instant `chat_ui_close`
arrives, so a hold inside the iframe is a hold nobody sees. Also found in the field: the
close followed the approval within a round-trip, and the saving screen existed only in the
code.

`verify_mirrors.py` (gate 14) now also checks the **`control.command` enumeration**, added
after `constraints_changed` reached the device and CONTRACT.md but not the Python Literal —
a Literal is a hard gate, so the frame failed validation on the SENDER's side and the
demo's headline moment silently did not happen.

## Conventions & gotchas

- **Commit identity:** author as `ashuksingh11`
  (`31301999+ashuksingh11@users.noreply.github.com`). **Push only when explicitly asked.**
- **Workflow (per the human):** plan=Opus · design/architecture=Fable · coding=Opus
  · browsing=Sonnet. Confirm before moving between phases.
- LLM-only by design — do NOT add scripted/rules fallbacks.
- There is a known benign log-noise TODO: `receive_json` on an already-closed socket
  can raise; it's caught/logged, not fatal.
- The device streams ~950 thinking frames per run; the UI coalesces them.
- **Known limitation:** `device_id` scopes message *delivery* (multi-session) and
  `goal_id` scopes each graph run (already isolated), but `memory/store.py` /
  `family_profile.json` is still GLOBAL — shared across every session, not per-home.
- **Known risk:** the checkpointer + the `asyncio.to_thread` fan-out in `server.py` isn't
  proven thread-safe under truly-parallel graph writes (concurrent sessions hitting the
  graph at once). If races surface, wrap the graph invoke/resume/`update_state` calls in
  one `asyncio.Lock` — cloud graph steps are fast; the LLM work happens on the device.
