# Code Guide — goal-flow-cloud-agent

The **cloud agent** is GoalFlow's hub tier. It does four jobs, and they are worth separating in
your head before you open a file:

| Job | Where | What it means |
|---|---|---|
| **The hub** | `server.py` | Every frame between every surface and the device routes here. Nothing else routes |
| **The goal graph** | `graph/nodes.py` | One LangGraph run per goal: interpret, resolve, gate, dispatch, monitor |
| **Household policy** | `memory/store.py` | The account's copy of the rules, resolved per goal by code |
| **The board fold** | `board.py` | Every goal's frames folded into one card per goal |

It is domain-agnostic by design. A domain is a `domain` string, and no meal-specific field exists
in the protocol or in any code path here.

**Read first:** `CONTRACT.md` (canonical wire protocol) · `README.md` (run steps) ·
`AGENTS.md` (session guide, gotchas).

**The system-level explanation** lives in the wiki:
04 — The cloud graph (`../goal-flow-agents/wiki/04-cloud-graph.md`) ·
03 — The wire (`../goal-flow-agents/wiki/03-the-wire.md`) ·
05 — Constraints (`../goal-flow-agents/wiki/05-constraints.md`) ·
02 — The goal lifecycle (`../goal-flow-agents/wiki/02-goal-lifecycle.md`).
This guide is the code walkthrough; the wiki is the map.

---

## File map

```
CONTRACT.md                       # CANONICAL wire protocol. Four mirrors follow it
data/memory/family_profile.json   # the household constraint store — one entry per fact
run.sh                            # sources .env, then uvicorn on ${WS_HOST:-0.0.0.0}:${WS_PORT:-8000}

src/goalflow_cloud/
  server.py        (2232)  # the WS hub: sessions, routing, the create bracket, speech  ← START HERE
  graph/nodes.py   (2139)  # the StateGraph: nodes, routers, interrupts, checkpointer
  models/contract.py (850) # the Pydantic mirror of every frame (extras allowed)
  board.py          (592)  # BoardService: frames → one GoalSummary per goal
  memory/store.py   (548)  # load, resolve per goal, append captures
  config.py         (159)  # frozen Settings from env
  speech/cues.py    (533)  # WHAT is said, and the rules behind each cue
  speech/client.py  (182)  # the fish.audio call — the ONE place we talk to it
  speech/utterances.py(115)# id → text/bytes, so a URL can never be a synthesis oracle

scripts/
  verify_generic_gate.py   # gate 10: actionability is generic (needs a key — the slow one)
  verify_persistence.py    # gate 12: a goal survives a cloud restart at its gate
  verify_board.py          # gate 13: the board fold's numbers are derived and add up
  verify_mirrors.py        # gate 14: no contract mirror and no allowlist has drifted
  verify_constraints.py    # gate 15: resolution per goal; the enforced set is never narrowed
  verify_capture.py        # gate 16: a rule is captured only when the user says yes
  verify_crossgoal.py      # gate 17: the cross-goal blast radius, authorship, newest-wins
  verify_close_hold.py     # gate 18: the webview outlasts its save
  verify_refusal_replay.py # gate 27: a refusal reaches its webview
  verify_dates.py          # gate 28: a named weekday means that weekday
  verify_worldclock.py     # gate 29: the cloud reasons in the DEVICE's day
  verify_speech.py         # gate 31: the voice says the right thing, its absence costs nothing
  verify_no_hang.py        # gate 33: a dispatch is always answered
  verify_rate_limit.py     # gate 37: the retry layering. Offline — no key, no network
  verify_speech_fanout.py  # the five cues reach the right surfaces
  run_graph_demo.py        # run the graph on any goal text, print the contract
  e2e_two_goals.py         # NOT a gate: the real two-goal demo, headless, against a live stack
```

---

## 1. The graph (`graph/nodes.py`)

One `StateGraph` per goal. `thread_id = goal_id`. Compiled with a **SQLite checkpointer** at
`data/goalflow.db`.

```
interpret_goal → detect_constraints
detect_constraints ─(route_after_interpret_or_capture)→ load_memory | capture_gate
                                                      | decline_out_of_scope | explain_block
capture_gate → END
load_memory → present_understanding ─(route_after_understanding)→ build_contract | goal_declined
build_contract → dispatch_to_device → collect_plan
  ─(route_on_safety)→ hitl_approval | relay_decisions | explain_block | precheck_wait
hitl_approval → relay_decisions → monitor
  ─(route_on_monitor)→ hitl_approval (the adapt loop) | monitor | finalize
explain_block → finalize → END
goal_declined | decline_out_of_scope | precheck_wait → END
```

### The checkpointer is SQLite, and that is load-bearing

`MemorySaver` spans one process. A goal sits at its approval gate for hours, and a board goal
spans days. With an in-memory saver every restart silently drops every paused goal: the card
stays on the board and nothing can ever resume it, which is worse than losing it visibly.

`check_same_thread=False` is safe **here** because the hub holds a per-goal lock around every
invoke (`goal_lock`), and a thread id *is* a goal id. Two threads never touch one checkpoint.

Gate 12 proves it: kill the cloud mid-run, restart, and the goal is still waiting at its gate.

### The four interrupts

| Node | Waits for | Resumed by |
|---|---|---|
| `present_understanding` | The user's answer to the confirm gate | `understanding_response` |
| `collect_plan` | The device's plan | `plan_ready` |
| `hitl_approval` | The user's approval | `approval` |
| `monitor` | A world tick | `status` or `proposal` |

> **LangGraph re-executes a node from the top when it resumes from `interrupt()`.** Anything
> above the `interrupt()` call runs twice. That is why `present_understanding` builds its
> `thought` line deterministically instead of asking a model: as an LLM call it ran twice per
> goal, for a string no surface renders.

### `GraphState`

A `TypedDict`, checkpointed whole:

`goal_text` · `intent` · `memory` · `proposed_constraints` · `statement_only` ·
`understanding` · `understanding_confirmed` · `contract` · `plan` · `pending_approvals` ·
`decisions` · `task_status` · `event_log` (append-only, `Annotated[..., add]`) · `goal_id` ·
`correlation_id` · `error` · `approval_frame` · `monitor_frame` · `explanation` ·
`device_capabilities` · `world_today`

### The three LLM calls

This repository makes **three** structured calls. Only the first can end a run; the other two
degrade to a deterministic fallback.

| Call | Node | Fails how |
|---|---|---|
| `interpret_goal` | `interpret_goal` | Sets `state["error"]` → `explain_block`. **No scripted fallback** |
| `detect_constraints` | `detect_constraints` | Proposes nothing. The goal proceeds |
| `_relevant_soft_ids` | inside `load_memory` | Falls back to `applies_to` tag matching |

**Only soft preferences go near the model.** The hard block is assembled by code from store data.

### `interpret_goal` — and two things it is handed

`with_structured_output(InterpretedIntent)` produces `{domain, objective, title,
success_criteria, scope, time_window, actionable, decline_reason}`.

**It is judged against the device, not against a list.** `capability_digest()` renders the
connected device's advertised modules and domains into the prompt, and the model then answers
whether the goal can plausibly be advanced *using those functions*. What this assistant can do is
a fact about the device that is plugged in, not a fact about the cloud. Gate 10.

**It is handed a calendar, not asked to compute one.** `_calendar_block()` writes the next
fortnight into the prompt, dated and named:

```
  2026-08-02 Sunday  <- today
  2026-08-03 Monday
  …
```

Given only *"Real today is 2026-08-02 (Sunday)"* the interpreter resolved **every** named weekday
one day early, four times out of four, at temperature 0. It was not misreading a calendar; it was
not consulting one. It counted ordinals from today and called the answer Tuesday.

That lands in the worst place, because `_align_away_window` takes `intent.time_window` verbatim
and the away window empties days out of every other goal's plan. The arithmetic is now done in
code and the model does a lookup. Gate 28.

### `_today(state)` — the cloud reasons in the device's day

The device runs a `SimulatedClock` stepped by *Advance day*. The cloud used to call
`date.today()`. The two agree until the second minute of a run.

The hub learns the world's day from frames the device already sends
(`status.payload.sim_date`, `day_advanced.sim_date`) and keeps it **per session**. `start_goal`
stamps it into the state, and every node calls `_today(state)`.

> **Stamped once per run, on purpose.** A goal is interpreted, grounded and dispatched against a
> single day. Re-reading the clock mid-run would let a world tick shift a goal's dates underneath
> it.

It falls back to real today when nothing has reported — which is also when nothing has been
simulated, so the two agree. Gate 29.

### The nodes, one line each

| Node | Does |
|---|---|
| `interpret_goal` | Words → structured intent, plus the actionability verdict |
| `detect_constraints` | Does this message *state* a household rule? It proposes; it never writes |
| `capture_gate` | The statement-only path: offer the rules, write only what was ticked, end |
| `load_memory` | Resolve the store for **this goal's domain** |
| `present_understanding` | The confirm gate. Builds `knew`, `constraints`, `preferences`, then interrupts |
| `build_contract` | Intent + memory → a validated `Dispatch`. `hard` copied **verbatim** |
| `dispatch_to_device` | Hand it to the hub |
| `collect_plan` | Park until `plan_ready`; extract `requires_approval` proposals |
| `hitl_approval` | The tiered-approval pause. Nothing firm executes before it returns |
| `relay_decisions` | Build the device's `approval` frame. Auto-tier defaults to approved |
| `monitor` | Park until the device speaks. A material change sets `adapting` |
| `precheck_wait` | **"Not yet."** End the run *without* completing the goal |
| `explain_block` | **"Never."** A safety block or an LLM error, in the user's words |
| `goal_declined` | The user said no at the gate. No dispatch ever happened |
| `decline_out_of_scope` | The interpreter judged it outside what the device can do |
| `finalize` | Close out |

> **`precheck_wait` and `explain_block` must stay separate.** A precheck block is *"not yet"*;
> a safety block is *"never"*. An empty plan flowing on to `relay_decisions` sends an empty
> approval, the device answers `status(done)`, and the board reads **Completed** for a goal that
> never ran.

### Hub-facing entry points

`start_goal(graph, goal_text, goal_id, …)` invokes the graph until it pauses at
`present_understanding`, and returns the state carrying `understanding` or `error`.

`resume_goal(graph, goal_id, resume_value)` resumes whichever interrupt is paused, with
`Command(resume=…)` on the same thread.

### Reliability

| Concern | Function | Rule |
|---|---|---|
| One HTTP client | `_shared_http_client()` | One `httpx` client per process. Four calls once meant four TLS handshakes |
| Rate limits | `is_rate_limited`, `rate_limit_delay` | A 429 backs off in **seconds**, jittered |
| Transient errors | `is_transient`, `invoke_llm` | Retried. A provider hiccup must not kill a goal |
| Timing | `timed_llm(site)` | Records the wall clock of every call site |
| Provider | `describe_routing()` | Printed at startup, so one glance shows whether the pin took |

---

## 2. The hub (`server.py`)

FastAPI, one `/ws` endpoint, and one HTTP route (`GET /speech/{filename}`).

### Sessions

A `Session` is **one home**: exactly one device agent and N user interfaces, keyed by
`device_id`. `ConnectionRegistry` owns them.

- A device **owns** its `device_id` — a stable self-generated UUID. A reconnect 1012-closes only
  the previous socket of the **same** id. Other homes are untouched.
- **User-interface sockets are never evicted.**
- A user interface binds by sending `device_id` in `hello`, by auto-bind when exactly one device
  is online, or by answering the `devices` list with `select_device`. **Frames from an unbound
  interface are dropped.**
- A device `hello` with no id defaults to `"default"` — zero-configuration single pairing.

### Routing

`route_message(sender_role, device_id, frame)` is one dispatch table on `(role, type)`. The
session comes from the **sending socket**, never from the role alone.

| `type` | From | Action |
|---|---|---|
| `user_goal` | ui | `emit_chat_ui_open` **first**, then run the graph to the gate; fold onto the board |
| `understanding_response` | ui | Resume the gate. Confirmed → send `dispatch`; declined → end, and re-snapshot the board |
| `approval` | ui | Resume `hitl_approval`, forward to the device, close the create bracket, fan out a household change |
| `control` | ui | Retire finished goals first on a goal-less `advance_day`, then forward |
| `board_get` | ui | Send this session's `board_snapshot` |
| `goal_state_get` | ui | Reply from the board cache: the last `present_plan`, then the last `status` |
| `capabilities` | device | Cache per session; relay; replay to a UI that binds later |
| `agent_event` | device | **Passthrough relay.** A `task_update` also folds onto the board |
| `plan_ready` | device | Resume `collect_plan`; re-wrap as `present_plan` with `payload.knew`; fold |
| `proposal` / `status` | device | Relay, fold, and feed the `monitor` interrupt |
| `day_advanced` | device | Learn `sim_date`, then relay to the boards |

> **Only `plan_ready` is deduplicated.** A device reconnect replay would otherwise resume the
> graph twice. `status`, `proposal` and `agent_event` legitimately recur inside one goal, and all
> of them must reach the surfaces. The client reducers dedupe `agent_event` on `seq`.

`send_to_device` returns whether the frame was delivered. An undelivered dispatch surfaces as a
terminal `status` to that session's interfaces, rather than leaving one stuck on "planning".

### Surface-aware delivery

Every cloud → ui frame goes through **one** fan-out point, `send_to_uis`. One interest predicate
sits in that send loop.

| `hello.surface` | Receives |
|---|---|
| absent, `"chat"`, `"board"` | Everything. Their own `INBOUND_TYPES` allowlists filter |
| `"input"` | **Only** `hello_ack`, `goal_accepted`, `chat_ui_open`, `chat_ui_close`, `notice` |

Point-to-point frames (`hello_ack`, `devices`) are outside the fork — an input surface still
needs the picker.

The fork is deliberately not extended to `chat` and `board`. A server-side per-type table would
reintroduce the "forked to nobody, so silently dropped" failure this contract warns about.

### The create-phase bracket

| Function | Job |
|---|---|
| `emit_chat_ui_open` | Open or retarget the webview, and create the replay cache |
| `emit_chat_ui_close` | Close it, and clear the cache |
| `_close_after` | A timed close, for a refusal |
| `_close_when_saved` | A **bounded** wait, for a household change |

> **The open fires the instant `user_goal` arrives, before interpretation.** Interpretation is a
> multi-second round trip. Emitting the open afterwards meant the slowest wait in the product
> happened with no webview on screen at all. The frame carries `goal_text` so the panel can show
> the user's own words while it waits.

**The replay cache** holds, per session, `{goal_id, goal_text, understanding?, present_plan?,
notice?}`. On the bind of a `"chat"` socket the cloud replays `chat_ui_open`, then the cached
`notice` **alone** if there is one (it is terminal), else the `understanding`, else the
`present_plan`.

`resolve_understanding` drops the understanding from the cache the moment it is confirmed.
Otherwise a socket reconnecting during planning is handed back the gate it already answered, and
the surface jumps backwards.

### The cross-goal fan-out

`fan_out_household_change(device_id, approved_goal_id)` is the one adaptation path that does not
ask the user.

Its first act is a guard, `_window_already_household`:

> **Only the goal that AUTHORED the window may promote it.** `contract.constraints.hard` is the
> *resolved* set, so it carries a household window the goal may merely have inherited. Without
> the guard, every goal re-promoted what it had just read.

Authorship is decided by asking what the household already holds for **everyone**: resolving
against the empty domain matches `applies_to: ["*"]` and skips domain-scoped entries. If this
goal's window is already there, this goal is a reader.

Then every other active goal is re-resolved, and where the enforced set actually moved the cloud
sends `control: constraints_changed` carrying the account's new `hard`, a `steer` and one
sentence for the board. Gates 17 and 25.

### Speech

| Function | Job |
|---|---|
| `emit_speech` | Compose the sentence, mint a deterministic id, send the frame |
| `_warm_utterance` | Start synthesis in parallel, before anyone fetches |
| `speech_audio` | `GET /speech/{filename}` — the hub's only HTTP route |

- **The frame carries a path, not audio.** The cloud does not know which host and port a client
  reached it on; the client resolves the path against its own socket's origin.
- **Synthesis happens on the fetch.** The gate never waits on the provider, and an utterance
  nobody plays is never billed.
- **The id is deterministic** per `(goal_id, cue)`, so the replay cache cannot pay twice.
- **An unknown id is a 404.** The route is not a synthesis oracle against the account.
- **One cue is one frame per SENTENCE.** A browser handed a chunked MP3 with no `Content-Length`
  waits for the complete body, so one long utterance meant seconds of silence.
- **No key ⇒ the frame is never sent**, and every surface renders exactly as it would in
  silence. This is the one thing in the system that fails silently, deliberately.

`speech/cues.py` carries the composition rules for all six cues. The governing one:

> **The voice is not a second copy of the screen.** The screen is parallel and skimmable; the
> voice is serial and slow. Its job is to say *when you are needed* and *what changed while you
> were not looking*.

### Structured logging

- Every record is stamped with `correlation_id` and `goal_id` through a `contextvars`-backed
  `CorrelationIdFilter`, so one goal's trail is greppable across the hub, the graph and the LLM
  calls.
- `log_frame()` emits one INFO line per frame (`direction`, `role`, `type`). Bodies at DEBUG.
- Graph nodes log `graph_node_enter` / `graph_node_exit` / `graph_node_error`.

---

## 3. The board fold (`board.py`)

`BoardService` is the hub's third tier. Every other frame is about **one** goal; the board is the
session-level view of **all** goals.

It holds `device_id → goal_id → GoalSummary` plus a monotonic `board_seq`, and folds
`understanding`, the contract, `task_update`, `plan_ready`, `status` and `proposal` into one
summary per goal.

**The fold is deterministic — no LLM, no I/O.** Gate 13 replays a frame sequence and checks the
card a person would read.

The card is born at `on_understanding`, not at dispatch, so a goal blocked behind a human is
visible.

### Four derivation rules

These are cloud-internal, not wire semantics.

- **`alerts.count` is what is OUTSTANDING, not a running tally.** An adaptation's alert clears
  when the goal *leaves* `adapting` — the test is `task_status != "adapting"`, **not** whether
  something executed, because a **decline** executes nothing and still answers the alert.
  `danger` never downgrades to `warn`.
- **`activity` lists only what OCCURRED.** A completed task title and a status note are pushed. A
  pending proposal is **not** — it is waiting on a person and belongs in `next_step`. Recording
  it as activity made the card print the same sentence as both ✓ done and ➡ next.
- **Progress is day-based once a goal is running.** `_day_progress` places `status.sim_date` in
  the goal's window, so one *Advance day* moves every card. It falls back to the task-DAG
  percentage during planning. The window spans `max(the plan's own day span, the goal's eta)` —
  a checklist that all happens on one evening has span 1, and without the `eta` floor a single
  tick would drive that card to 100% with the deadline still days out.
- **The subtitle is rebuilt from the window and `sim_date`**, the same two values that move the
  progress bar, so the two cannot disagree. A subtitle written once at dispatch says the same
  thing forever, which is indistinguishable from a card that stopped updating.

`retire_completed` drops every `done` goal on the next `advance_day`. The removal reaches the
surface as a fresh `board_snapshot`, because a `board_update` can only replace a card, never
retract one.

`on_device_offline` marks every unfinished card at risk.

---

## 4. Household policy (`memory/store.py`)

`data/memory/family_profile.json` is a **library of entries**, one per fact:

```json
{ "id": "c-cap-travel", "kind": "budget_cap", "value": 1500.0,
  "enforcement": "hard", "source": "account", "scope": "domain",
  "applies_to": ["vacation_prep"], "expires_day_offset": 8 }
```

It is the **account's** copy of policy, and it pushes down to the device. The device owns world
state and never sources policy.

`resolve_constraints(profile, domain, today, soft_ids)` returns
`{hard, hard_display, soft, context, applied}`.

| Kind group | Rule |
|---|---|
| **hard list** — `allergens`, `dietary`, `medical` | **Unioned across every entry, `applies_to` ignored.** The enforced set is never narrowed by relevance |
| **hard scalar** — `budget_cap`, `quiet_hours`, `peak_hours`, `away_window`, `budget_envelope` | **Domain-picked.** Most specific `applies_to` wins |
| **soft** | Picked by relevance: the small LLM pass, with tag matching as a complete fallback |

`_specificity` scores an entry: 2 for a goal-scoped match, 1 for a domain match, 0 for `"*"`,
and −1 for no match.

`_stricter` breaks a tie:

1. **Numbers order.** A lower cap is stricter, and that is a fact about the values.
2. **Windows do not order.** So the **later entry** wins.

> **Why the later entry.** The store is append-only, so a later position is literally *"said
> afterwards"*. At equal specificity the later statement is the one a person means. It also gets
> the seed right for free, because fixtures are written first. `captured_on` is not enough to
> order by: it has day granularity, and a run states every one of these rules on the same
> simulated day.

**Dates are day offsets** resolved against today at load, so a seeded world never goes stale. A
chat capture writes an absolute `expires_on` instead. Expiry is checked **first**, so a retired
constraint can never be unioned in by the never-narrow rule.

### Display is not enforcement

An entry may carry `display_to`. It changes nothing about resolution. It produces a second block,
`hard_display`, for the cards.

> **Hiding a chip never hides a rule.** That is why they are two blocks rather than one filtered
> block, and why gate 15 pairs every display assertion with the enforcement assertion beside it.

### Capture

`append_constraints` is the **single write path**, and it enforces two rules the caller cannot
skip: a capture may only **tighten**, and it only ever **appends**.

Nothing is written until `understanding_response.accepted_constraint_ids` names the id. A diet
name is expanded into what it forbids, because the device's vocabulary matches things, not
labels — *"captured but unenforceable"* is the one outcome capture must never have, and from the
screen it looks identical to success.

A goal-scoped rule gets the goal's horizon as its expiry, so *"keep the party under $150"* cannot
quietly cap every birthday after it. Gate 16.

### A scratch store

`GOALFLOW_PROFILE_PATH` points the store at a scratch copy — the cloud's equivalent of the
device's `--data`. A configured path that does not exist yet is **seeded from the committed
profile on first use**, so a run never dirties the seed.

> Delete the scratch file between runs. **Name the file** — a glob such as
> `data/memory/*_profile.json` also matches `family_profile.json`.

---

## 5. Config (`config.py`)

A frozen `Settings` dataclass, read from the environment through `.env`.

| Variable | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | **Required.** LLM-only, no fallback |
| `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL` | The endpoint and the model |
| `OPENROUTER_PROVIDER_ORDER` | **Pin it.** See below |
| `OPENROUTER_PROVIDER_ALLOW_FALLBACKS` | `false` |
| `GOALFLOW_PROFILE_PATH` | A scratch constraint store |
| `FISH_API_KEY`, `SPEECH_ENABLED` | The voice. Absent means no `speech` frame is ever sent |
| `WS_HOST`, `WS_PORT` | The bind address, default `0.0.0.0:8000` |
| `LOG_LEVEL` | |

> **Pinning the provider is the whole difference.** Unpinned, OpenRouter load-balances this model
> across nineteen endpoints whose throughput spans 39 times. Measured on the same input, four
> runs took 59 s, 175 s, 145 s and 189 s unpinned; the same four took 8.4 s, 8.4 s, 8.7 s and
> 10.1 s pinned. `allow_fallbacks=false` is deliberate: the next-best provider measured 203 to
> 234 s, so a fallback is a silent stall rather than a degrade.

---

## 6. Extending it

**A new graph node.** Write the function in `graph/nodes.py` (return partial state; append to
`event_log` via `_event`), then register it in `build_graph()` with its edges or a router. If it
pauses for external input, use `interrupt()` and have the hub resume it with `resume_goal` — the
checkpointer makes the pause durable for free. Remember that everything above the `interrupt()`
runs again on resume.

**A new message type.** Edit `CONTRACT.md` **first** — it is the anchor. Then the Pydantic model
in `models/contract.py`, then a `route_message` branch, then the three TypeScript mirrors, the C#
contracts, **and the three `INBOUND_TYPES` allowlists**. Run `verify_mirrors.py`. Models allow
extra fields, so a purely additive payload key does not break an older peer.

**A new domain.** Nothing changes here. The contract is generic. The device advertises the domain
through `capabilities`, and the interpreter emits the slug and a free-form `scope`.

**A new constraint kind.** Add it to `HARD_LIST_KINDS` or `HARD_SCALAR_KINDS` in `store.py`, then
bind a device rule to the new key. A hard kind with no device rule logs
`constraint_unknown_hard_kind` at resolution — it resolves, dispatches, and enforces nothing.

**Real memory.** Replace the JSON read with a vector store for **soft** preferences only. The
hard block must stay a deterministic, non-semantic lookup.

**Multi-home memory.** `store.py` reads one `family_profile.json` for every session. The graph is
isolated per `goal_id`; the household store is not. Keying it by `device_id` is the next real
step.
