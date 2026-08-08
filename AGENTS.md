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
- **`OPENROUTER_PROVIDER_ORDER=cerebras` + `OPENROUTER_PROVIDER_ALLOW_FALLBACKS=false` (v8) is what decides how fast this
  is.** Unset, OpenRouter load-balances across nineteen endpoints spanning 39x in
  throughput and lands on the slow ones: interpretation measured 19.7s unpinned vs 2.3s
  pinned. Fallbacks are OFF deliberately — the next-best provider measured 203-234s on the
  real pipeline, slower than no pinning at all, so a fallback is a stall rather than a
  degrade. Every LLM client is built by `graph/nodes.py:build_chat()`; both it and the
  startup `llm_routing` log line are the places to look. `OPENROUTER_REASONING_EFFORT`
  exists and ships OFF — read the comment in `config.py` before setting it, `low` was
  measured to break the output outright.
- Each LLM call logs `llm_call site=... elapsed_ms=...`. **THREE** run per goal creation:
  `interpret`, `detect_constraints`, `soft_select`. (v11.2 removed a fourth, `thought`.
  It ran TWICE per goal — LangGraph re-executes a node from the top when resuming from
  `interrupt()` — for a sentence no surface has rendered since v9, and at `max_tokens=60`
  on a reasoning model it mostly came back `finish_reason: length` and fell back to the
  deterministic version anyway. Two wasted round trips inside the interpretation window,
  and two more chances to draw a provider 429.)

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
  `guest_dinner`, `vacation_prep`, `birthday_party`.
  **The dispatch `time_window` always STARTS at the device's today** (monitoring begins now,
  for every goal); the END is the interpreter's, falling back to today+6 only when it gives
  none or one at/before the start. There is NO meal-plan carve-out — this line used to claim
  the meal window was "pinned today..today+6" and it is not, which sent a v11.2 bug hunt to
  the wrong file. It matters: the interpreter reads "this week" as ending Sunday about half
  the time, so a 7-day meal plan routinely runs under a 3-4 day window. The DEVICE reconciles
  that (`ResolveLastDay` takes the later of window and plan), not the cloud.
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
- `src/goalflow_cloud/speech/` — **v11, the voice.** The understanding gate said out loud
  via fish.audio. `graph/nodes.py:_understanding_speech()` composes the sentence
  DETERMINISTICALLY (no LLM call — it sits at the end of the interpretation window v8
  spent a milestone shortening); the hub sends a `speech` frame carrying a URL, and
  synthesis happens when a UI GETs `/speech/<id>.mp3` — the hub's only HTTP route. So
  nothing about the gate waits on fish.audio, and an unplayed utterance is never billed.
  **This is the one feature here that fails SILENTLY**: no `FISH_API_KEY` ⇒ no frame ⇒
  every surface renders exactly as it did in v10. That is deliberate and it is the
  opposite of the LLM-only rule — read the package docstring before making a failure here
  loud. Gate: `scripts/verify_speech.py` (31).
  **`SPEECH_ENABLED=false` silences it while leaving the key in place** — the dev
  switch, because iterating on the UI with a key set means the fridge talks on every
  reload. The startup `speech_routing` line names WHICH reason it is quiet (no key vs
  switched off), so a silent run is never a mystery.
  **Autoplay is the UI's problem and it is real**: a browser refuses `audio.play()`
  without a user gesture, so the chat UI degrades to a "Hear this" tap
  (`goal-flow-agent-chat-ui/src/lib/speech.ts`). Do not "fix" that by assuming autoplay.
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
`board_get`, `goal_state_get`, `select_device`.
Receives from device: `capabilities`, `agent_event` (stream), `plan_ready`,
`proposal`, `status`. Sends to UI: `understanding`, `present_plan` (adds `knew`,
relays `demo_events`), `agent_event`, `proposal`, `status`, `board_snapshot`,
`board_update`, `day_advanced`. Sends to device:
`dispatch` (the generic Task Contract), `approval`, `control`. See `CONTRACT.md` for
exact shapes — it is authoritative and current (includes `understanding`,
`trigger_event`, `demo_events`, `event_id`, `updated_plan`/`changed_ids`).

## Current state

Fully built and verified end-to-end (headless + live browser): interpret → memory →
**confirm-understanding gate** → contract → device plan (streamed) → present → tiered
HITL approval → monitor. Both `meal_plan` and `guest_dinner` domains. The cloud made
NO logic change for the event-driven meal demo — `trigger_event` control + device
`demo_events`/`updated_plan` flow through by pass-through.

## v12.2 — the cloud's 429 policy (gate 37)

Every cloud LLM call goes through `invoke_llm(site, call)` in `graph/nodes.py`. It carries
**two separate retry counters**, and separate is the point: a 429 is waited out (6 retries
at 2s, 4s, 8s… capped at 20s, with jitter) and costs no ordinary attempt; a dropped socket
is retried fast (2 retries at 0.4s, 0.8s) and costs no rate-limit attempt. Anything else —
a bad key, a broken prompt — raises at once, because the LLM-only rule is to fail loudly.

The numbers are copied from the device's v11.2 policy on purpose, so one set of
measurements explains both tiers. Before this, `interpret_goal` had a single sub-second
retry and the other two call sites had none; the provider's recovery window is about three
seconds, so one 429 killed the goal before the confirmation card was drawn. A user hit
exactly that in a pre-demo run.

**`max_retries=1` at `interpret_goal` stays, and that was measured, not reasoned.** Setting
it to 0 — so that only one layer owned retry — took `scripts/verify_dates.py` from 3 passes
in 3 runs to 3 in 6, and restoring it returned 3 in 3. The two layers do different jobs at
different time scales and both are wanted. Gate 37 asserts it. Re-run `verify_dates.py`
several times before touching it; **one run of an LLM gate proves nothing**.

Gate: `scripts/verify_rate_limit.py` (37) — offline, no key, no network.

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

`verify_refusal_replay.py` (gate 27) pins the THIRD failure of that same bracket, and the
subtlest: a refusal is the only create phase with no round-trip in it, so the `notice` was
broadcast while Bixby was still mounting the webview it was meant for. Nobody was
connected, and the user watched a blank panel appear and close 4.5s later. Terminal
notices now join the create-phase replay cache; `updating_goals` deliberately does not.

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
