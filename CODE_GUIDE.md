# Code Guide — goal-flow-cloud-agent (v2)

The **cloud agent** is the hub tier of GoalFlow v2, a general goal-based agent. It owns
the conversation and family memory, LLM-interprets the user's fuzzy goal into a **generic
Task Contract**, drives an advanced **LangGraph StateGraph** (conditional edges,
`interrupt()`-based HITL, checkpointer), and relays every frame between the UI and the
on-device agent. Domain-agnostic by design: meal planning and guest dinner prep are just
`domain` strings — no meal-specific fields exist in the protocol or the code paths.

See `README.md` for run steps, `CONTRACT.md` for the canonical wire protocol, and
`docs/ARCHITECTURE.md` for the full design (node table, Mermaid state diagram).

## File map

```
CONTRACT.md                       # canonical CONTRACT v2 (source of truth; generic)
scripts/run_graph_demo.py         # run the graph on any goal text, print the contract
data/memory/family_profile.json   # generic family memory (hard + soft + context)
src/goalflow_cloud/
  config.py                       # Settings dataclass from env (OPENROUTER_*, WS_*, LOG_LEVEL)
  server.py                       # FastAPI WS hub: registry, routing, relays, graph driving  ← start here
  models/contract.py              # Pydantic mirror of every CONTRACT v2 message (lenient extras)
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
`task_status` (the CONTRACT v2 lifecycle), `event_log` (append-only,
`Annotated[..., add]` reducer), `goal_id`, `correlation_id`, `error`, plus
`approval_frame` / `monitor_frame` / `explanation` outputs.

**Nodes and edges:**

```
interpret_goal ─(error? → explain_block)→ load_memory → present_understanding
  ─(route_after_understanding)→ build_contract | goal_declined
build_contract → dispatch_to_device → collect_plan ─(route_on_safety)→ hitl_approval | relay_decisions | explain_block
hitl_approval → relay_decisions → monitor ─(route_on_monitor)→ hitl_approval (adapt loop) | monitor | finalize
explain_block → finalize → END
goal_declined → END
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
- **`explain_block` / `finalize`** — the Trace/Explain surface: user-facing explanation
  for safety-blocked plans or LLM errors, then close-out.

**Hub-facing entry points:** `start_goal(graph, goal_text, goal_id)` invokes the graph
until it pauses at `present_understanding` (or errors) and returns the state (with
`understanding` or `error`); `resume_goal(graph, goal_id, resume_value)` resumes any
paused interrupt with `Command(resume=...)` on the same thread — first the
understanding gate (`{"confirmed": bool}`), later `hitl_approval` and `monitor`.

## The WebSocket hub (`server.py`)

FastAPI, single `/ws` endpoint. First frame must be `hello` (`role: ui|device`); the
`ConnectionRegistry` keeps one active socket per role (a reconnect replaces and closes
the old one) and replies `hello_ack` with a `session_id`. Routing is on
**`type` + sender role** (`route_message`):

| Incoming `type` | From   | Cloud action |
|-----------------|--------|--------------|
| `user_goal`     | ui     | `start_goal` runs the graph (in a thread) to the `present_understanding` gate → send the `understanding` frame to the UI; on graph `error`, send a terminal `status` frame to the UI instead. |
| `understanding_response` | ui | `resume_goal` resumes `present_understanding` with `{"confirmed": bool}`; confirmed sends the `dispatch` contract to the device, declined ends the goal (`goal_declined`). |
| `approval`      | ui     | `resume_goal` resumes the `hitl_approval` interrupt with the decisions; forward the frame to the device. |
| `control`       | ui     | Forward to the device (generic clock: `advance_day` / `reset` / `set_date`; plus `trigger_event` for the event-driven demo — cloud makes no logic changes, pure passthrough). |
| `capabilities`  | device | Cache the module registry; relay to the UI (also replayed to late-joining UIs on `hello`). |
| `agent_event`   | device | **Passthrough relay** to the UI; best-effort append into the graph's `event_log` via `graph.update_state`. The stream never blocks the graph — streaming lives at the hub layer while the graph waits at its interrupts. |
| `plan_ready`    | device | `resume_goal` (resumes `collect_plan`); re-wrap as `present_plan` with `payload.knew` added; send to UI. If the run auto-advanced past approval (auto-tier only), forward the resulting `approval_frame` to the device. |
| `proposal` / `status` | device | Relay to the UI, then feed the graph's `monitor` interrupt (best-effort). |

Device frames are **deduped** per goal on `correlation_id` (plus `seq` for
`agent_event`) so reconnect replays are dropped.

**`build_knew(contract)`** produces the UI's "what it knew" personalization from the
dispatched contract — generically: it surfaces `constraints.hard` (allergens, dietary,
medical, budget, quiet hours), `constraints.soft` (dislikes, prefer), and `context`
notes as flat display-ready chips. No domain-specific logic.

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
