# Code Guide — goal-flow-cloud-agent (v3)

The **cloud agent** is the hub tier of GoalFlow v3, a general goal-based agent. It owns
the conversation and family memory, LLM-interprets the user's fuzzy goal into a **generic
Task Contract**, drives an advanced **LangGraph StateGraph** (conditional edges,
`interrupt()`-based HITL, checkpointer), and relays every frame between the UI and the
on-device agent. Domain-agnostic by design: meal planning and guest dinner prep are just
`domain` strings — no meal-specific fields exist in the protocol or the code paths.

See `README.md` for run steps, `CONTRACT.md` for the canonical wire protocol, and
`docs/ARCHITECTURE.md` for the full design (node table, Mermaid state diagram).

## File map

```
CONTRACT.md                       # canonical CONTRACT v3 (source of truth; generic)
scripts/run_graph_demo.py         # run the graph on any goal text, print the contract
scripts/verify_board.py           # gate 13: the board fold's numbers are derived and add up
scripts/verify_mirrors.py         # gate 14: the contract mirrors have not drifted
data/memory/family_profile.json   # generic family memory (hard + soft + context)
src/goalflow_cloud/
  config.py                       # Settings dataclass from env (OPENROUTER_*, WS_*, LOG_LEVEL)
  server.py                       # FastAPI WS hub: multi-session registry, routing, relays, graph driving, board pushes  ← start here
  board.py                        # BoardService: folds a goal's frames into one GoalSummary per goal (deterministic, no LLM)
  models/contract.py              # Pydantic mirror of every CONTRACT v3 message (lenient extras)
  memory/store.py                 # profile loader + hard_safety_block / soft_bias_block
  graph/nodes.py                  # the StateGraph: nodes, routers, interrupts, checkpointer
docs/ARCHITECTURE.md, docs/diagrams.md
```

## The LangGraph StateGraph (`graph/nodes.py`)

One graph run per goal, `thread_id = goal_id`, compiled with a **`MemorySaver`
checkpointer** (`build_graph()`), so every `interrupt()` pause is durable and resumable
(swap in a persistent checkpointer without touching the graph).

**State** (`GraphState`, a `TypedDict`): `goal_text`, `intent`, `memory`, `understanding`,
`understanding_confirmed`, `contract`, `plan`, `pending_approvals`, `decisions`,
`task_status` (the CONTRACT v3 lifecycle), `event_log` (append-only,
`Annotated[..., add]` reducer), `goal_id`, `correlation_id`, `error`, plus
`approval_frame` / `monitor_frame` / `explanation` outputs.

**Nodes and edges:**

```
interpret_goal ─(route_after_interpret)→ load_memory | decline_out_of_scope | explain_block
load_memory → present_understanding ─(route_after_understanding)→ build_contract | goal_declined
build_contract → dispatch_to_device → collect_plan
  ─(route_on_safety)→ hitl_approval | relay_decisions | explain_block | precheck_wait
hitl_approval → relay_decisions → monitor ─(route_on_monitor)→ hitl_approval (adapt loop) | monitor | finalize
explain_block → finalize → END
goal_declined | decline_out_of_scope | precheck_wait → END
```

- **`interpret_goal`** — the only LLM call in this repo: `ChatOpenAI` (OpenRouter
  base_url/model from `config.py`) with `with_structured_output(InterpretedIntent)`,
  producing `{domain, objective, success_criteria, scope, time_window}`. The prompt
  passes **real today** so `time_window` is always relative, never hardcoded.
  **LLM-only:** any failure (exception, missing time_window) sets `state["error"]` and
  `route_after_interpret` sends the run to `explain_block` — there is no scripted
  fallback anywhere.
- **`load_memory`** — loads the profile via `memory/store.py`; keeps the `hard` block as
  data and bundles `soft` + members + context as planning bias.
- **`present_understanding`** — the **confirm-understanding gate**: second `interrupt()`.
  Builds a short LLM-authored `thought` one-liner plus the `knew` hard-constraint chips
  and pauses (`kind: "understanding_confirmation"`) until the hub resumes it with the
  user's `understanding_response`. `route_after_understanding` sends a confirmed
  response to `build_contract`; a decline routes to `goal_declined`.
- **`goal_declined`** — graceful terminal: the user declined the understanding before
  any planning happened, so the goal ends (`task_status="done"`) without ever
  dispatching to the device.
- **`decline_out_of_scope`** — graceful terminal reached straight from
  `interpret_goal` (`route_after_interpret`) when the interpreter judges the goal
  outside what the agent can act on: the run ends with an out-of-scope explanation
  instead of loading memory or presenting understanding.
- **`build_contract`** — merges intent + memory into a `dispatch` frame:
  `constraints.hard` copied **verbatim** from memory (never LLM output),
  `constraints.soft` from soft prefs, `autonomy = "tiered"`, mints
  `goal_id`/`correlation_id`, and validates via `Dispatch(**frame)`.
- **`collect_plan`** — second `interrupt()`: the graph parks until the hub resumes it
  with the device's `plan_ready` payload; extracts proposals with
  `requires_approval` into `pending_approvals`.
- **`hitl_approval`** — the HITL pause: third `interrupt()`, with the tiered proposals;
  the checkpointer persists the paused state; the hub resumes with the user's decisions
  (`Command(resume=...)`). Nothing firm executes until this returns.
- **`relay_decisions`** — builds the `approval` frame for the device (defaults to
  approving auto-tier proposals when no explicit decisions exist).
- **`monitor`** — fourth `interrupt()`: fed by the hub on each device `status`/`proposal`
  frame. An adaptation `proposal` (or a `material` status) sets `task_status="adapting"`
  and `route_on_monitor` loops back into `hitl_approval` — the **adapt loop**;
  `task_status == "done"` routes to `finalize`.
- **`precheck_wait`** — terminal hold reached from `collect_plan` when the device's
  Pre-check Engine reports the world isn't ready (`precheck.ok is False`, e.g. signed
  out, appliance offline). Unlike a safety block ("never" → `explain_block`), a
  precheck block is "not yet": the run parks so the goal can resume when the world
  recovers, rather than falsely completing.
- **`explain_block` / `finalize`** — the Trace/Explain surface: user-facing explanation
  for safety-blocked plans or LLM errors, then close-out.

**Hub-facing entry points:** `start_goal(graph, goal_text, goal_id)` invokes the graph
until it pauses at `present_understanding` (or errors) and returns the state (with
`understanding` or `error`); `resume_goal(graph, goal_id, resume_value)` resumes any
paused interrupt with `Command(resume=...)` on the same thread — first the
understanding gate (`{"confirmed": bool}`), later `hitl_approval` and `monitor`.

## The WebSocket hub (`server.py`)

FastAPI, single `/ws` endpoint. First frame must be `hello` (`role: ui|device`); the
`ConnectionRegistry` is **multi-session**: a `Session` (one device + N uis) is keyed by
`device_id`, so multiple homes run concurrently without cross-talk. A device reconnect
1012-evicts only the PRIOR socket of the SAME `device_id` (other sessions untouched); ui
sockets are never evicted. A ui binds via `hello.device_id`, auto-binds when exactly one
device is online, or is held UNBOUND (sent a `devices` list, binds later via a
`select_device` frame, ACKed with `hello_ack.device_id`) — frames from an unbound ui are
dropped. A device hello with no `device_id` defaults to `"default"` (zero-config
single-pair back-compat). Every send replies `hello_ack` with a `session_id`. Routing is
on **`type` + sender role, within the sender's session** (`route_message` derives
`device_id` from the sender socket's cached `(role, device_id)` meta):

| Incoming `type` | From   | Cloud action |
|-----------------|--------|--------------|
| `user_goal`     | ui     | `start_goal` runs the graph (in a thread) to the `present_understanding` gate → send the `understanding` frame to the UI; also folds the goal onto the board (`board.on_understanding`); on graph `error`, send a terminal `status` frame to the UI instead. |
| `understanding_response` | ui | `resume_goal` resumes `present_understanding` with `{"confirmed": bool}`; confirmed sends the `dispatch` contract to the device and folds the goal (`board.on_goal_created`), declined ends the goal (`goal_declined`) and re-snapshots the board so the card drops. |
| `approval`      | ui     | `resume_goal` resumes the `hitl_approval` interrupt with the decisions; forward the frame to the device. |
| `control`       | ui     | Forward to the device (generic clock: `advance_day` / `reset` / `set_date`; plus `trigger_event` for the event-driven demo — cloud makes no logic changes, pure passthrough). |
| `board_get`     | ui     | Send this session's `board_snapshot` (every goal, folded) — first paint, or to heal a `board_seq` gap. |
| `goal_state_get`| ui     | Reply from the board's cache with the goal's last `present_plan` / `status` / pending `understanding` (drill-in after a reload); a miss is logged. |
| `suggestion_action` | ui | Accept or dismiss one proactive suggestion (`BoardService.take_suggestion`); an accept starts a real goal, then re-send `suggestions`. |
| `capabilities`  | device | Cache the module registry **per session**; relay to the UI (also replayed to a UI when it binds into the session). |
| `agent_event`   | device | **Passthrough relay** to the UI; best-effort append into the graph's `event_log` via `graph.update_state`. The stream never blocks the graph — streaming lives at the hub layer while the graph waits at its interrupts. A `task_update` event also folds onto the board (`board.on_task_update`). |
| `plan_ready`    | device | `resume_goal` (resumes `collect_plan`); re-wrap as `present_plan` with `payload.knew` added; send to UI; fold onto the board (`board.on_plan_ready`). If the run auto-advanced past approval (auto-tier only), forward the resulting `approval_frame` to the device. |
| `proposal` / `status` | device | Relay to the UI, feed the graph's `monitor` interrupt (best-effort), and fold onto the board (`board.on_proposal` / `board.on_status`). |
| `suggestions`   | device | Replace this session's proactive-suggestion list (`board.on_suggestions`); re-broadcast `suggestions` to the boards. |
| `day_advanced`  | device | Relay the global world-tick summary (v3.2) straight through to the boards; the per-goal `status`/`proposal` frames that ride alongside it already moved the cards. |

Device frames are **deduped** per goal on `correlation_id` (plus `seq` for
`agent_event`) so reconnect replays are dropped.

`send_to_device` reports whether the frame was delivered; both dispatch sites
(`handle_user_goal`, `handle_understanding_response`) surface an undelivered dispatch
as a terminal `status` frame to that session's uis ("Device agent '<id>' isn't
connected — start it and try again") via `send_device_offline`, instead of leaving the
UI stuck on "planning".

**`build_knew(contract)`** produces the UI's "what it knew" personalization from the
dispatched contract — generically: it surfaces `constraints.hard` (allergens, dietary,
medical, budget, quiet hours), `constraints.soft` (dislikes, prefer), and `context`
notes as flat display-ready chips. No domain-specific logic.

## The board fold (`board.py`)

`BoardService` is the hub's **third tier**, next to the conversation/graph and the
family memory. Every other frame is about *one* goal; the board is the *session-level*
view of *all* goals. It holds per-session state (`device_id → goal_id → GoalSummary`,
plus a monotonic `board_seq`) and folds each goal's frames — `understanding`,
`dispatch`, `plan_ready`, `task_update`, `status`, `proposal` — into **one
`GoalSummary` per goal** (`models/contract.py`). Each `on_*` method returns the updated
summary; `server.py`'s `push_board` broadcasts it as a `board_update`, and
`send_board_snapshot` sends the whole set as a `board_snapshot`. The fold is
**deterministic — no LLM, no I/O** (`scripts/verify_board.py`, gate 13, replays a frame
sequence and checks the card a person would read). The card is born at
`on_understanding` (so a goal blocked behind a human is visible), not at dispatch.

Three derivation rules are worth knowing before touching this file (these are
cloud-internal, *not* wire-contract semantics):

- **`alerts.count` is what is OUTSTANDING, not a running tally.** `_bump` only ever
  counted upward; `on_status` clears the alert (`count=0`) once the thing that raised
  it is resolved. An adaptation's alert clears when the goal *leaves* `"adapting"` —
  the test is `task_status != "adapting"`, **not** whether something executed, because
  a **decline** executes nothing yet still answers the alert. A `done` goal carries no
  open alerts. `danger` never downgrades to `warn`.
- **`activity` lists only things that OCCURRED.** A completed `task_update` title and a
  `status` note get `_push`ed (last two, "a • b"); a pending `proposal` is **not** —
  it is what the agent *wants* to do and is waiting on a person, so it belongs in
  `next_step` alone. Recording it as activity used to make the card show the same
  sentence twice (✓ done and ➡ next).
- **Day-based progress (v3.2).** Once a goal is running, `progress_pct` is where the
  sim date (`status.payload.sim_date`) sits in the goal's window (`_day_progress`), so
  one Advance-day moves every card; it falls back to the task-DAG `progress_pct` during
  planning. The window (`on_plan_ready`) spans `max(` the plan's own day-span `,` the
  goal's `eta` `)` — because since v3.5 plan days are real dates, a checklist that all
  happens on one evening has span 1, and without the `eta` floor a single Advance-day
  would drive that card to 100% with the deadline still days out.

`goal_state_get` (drill-in) and the proactive **suggestions** list (M8:
`on_suggestions` / `take_suggestion`) are also served from here; a device going offline
marks every unfinished card at-risk (`on_device_offline`).

## Memory: the hard-vs-soft split (`memory/store.py`)

`data/memory/family_profile.json` is generic (serves any domain):

- **`hard`** — the safety policy (`allergens`, `medical`, `dietary`, `budget_cap`,
  `quiet_hours`). `hard_safety_block()` returns it **verbatim** and `build_contract`
  copies it as data into `dispatch.constraints.hard` — a pure data path the LLM never
  generates, edits, or paraphrases. The device's deterministic Safety filter enforces
  exactly this block and nothing else ("LLM plans, code checks").
  `scripts/run_graph_demo.py` asserts this invariant on every run.
- **`soft` + `members` + `context`** — `soft_bias_block()` bundles these as planning
  bias only; `soft` lands in `constraints.soft`, the rest grounds `dispatch.context`.
  Never safety-enforced.

## Structured logging (first-class)

Configured in `server.setup_logging()` from `LOG_LEVEL`:

- Every record is stamped with `correlation_id` and `goal_id` via a
  `contextvars`-backed `CorrelationIdFilter`, so one goal's trail is grep-able across
  hub, graph, and LLM calls.
- `log_frame()` emits one INFO line per inbound/outbound frame
  (`direction`, `role`, `type`); full frame bodies at DEBUG.
- Graph nodes log enter/exit (`graph_node_enter` / `graph_node_exit` / `graph_node_error`)
  — this is the Trace/Explain harness surface and the debugging story.

## Config (`config.py`)

Frozen `Settings` dataclass from env (`.env` via python-dotenv): `OPENROUTER_API_KEY`
(required — LLM-only, no fallback), `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`
(default `openai/gpt-oss-120b`), `WS_HOST`/`WS_PORT`, `LOG_LEVEL`.

## Extending it

- **New graph node:** add the node function in `graph/nodes.py` (return partial state;
  append to `event_log` via `_event`), register it in `build_graph()` with its edges or
  a router function. If it pauses for external input, use `interrupt()` and have the
  hub resume it via `resume_goal` — the checkpointer makes the pause durable for free.
- **New message type:** add the Pydantic model in `models/contract.py` (extend
  `ContractMessage`), add a `route_message` branch in `server.py`, and update
  `CONTRACT.md` first (it is the canonical anchor; the UI/device repos mirror it —
  any change is a contract version bump). Models allow extra fields, so purely
  additive payload keys don't break older peers.
- **New domain:** nothing to change here — the contract is generic. The device
  advertises new capability modules via `capabilities`; the LLM interpreter emits the
  new `domain` string and a free-form `scope`.
- **Real memory:** replace `memory/store.py`'s JSON read with a vector store for soft
  prefs — but keep the hard block a deterministic, non-semantic lookup.
- **Durable restarts:** swap `MemorySaver` for a persistent LangGraph checkpointer in
  `build_graph()` — the interrupt/resume contract is unchanged.
