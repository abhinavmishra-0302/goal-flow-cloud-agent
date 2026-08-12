# AGENTS.md — goal-flow-cloud-agent (coding-session guide)

Read this first. It is the fast path to being productive here without re-deriving the
architecture. **`CODE_GUIDE.md` is the walkthrough**; this file is the run sheet, the traps and
the conventions.

## What this repo is

The **cloud agent** of GoalFlow, a two-tier goal-based agent for the Samsung Tizen Family Hub.

This tier owns the **goal**: interpreting fuzzy text, resolving household policy, holding the two
human gates, and aggregating every running goal onto a board. It is a FastAPI WebSocket **hub** —
the surfaces and the device each open one outbound socket to it, and **they never talk to each
other**.

> **The canonical wire contract lives HERE: `CONTRACT.md`.** It is mirrored by
> `models/contract.py`, three `types/contract.ts` files and `Contracts/*.cs` in the device.
> **Change `CONTRACT.md` first** when the protocol moves, then every mirror in one pass.

Siblings, all under `~/ashu/git/`:

| Repo | Role |
|---|---|
| `goal-flow-agent-bixby-ui` | Where the user speaks or types |
| `goal-flow-agent-chat-ui` | The create-phase surface |
| `goal-flow-agent-board-ui` | Home — the Agent Board |
| `goal-flow-device-agent-ubuntu` | The .NET/SK device agent. **Source of truth for device code** |
| `goal-flow-device-agent-tizen` | The port, re-synced per milestone |
| `goal-flow-agents` | Docs. `docs/DESIGN.md` is the design record |

## Stack and run

Python 3.11+, FastAPI and `uvicorn`, **LangGraph** (a `StateGraph` with `interrupt()` gates and a
`SqliteSaver` checkpointer at `data/goalflow.db`, `thread_id = goal_id`).

**LLM-only.** There is no scripted or rules fallback. It fails loudly.

```bash
./run.sh                                   # sources .env, uvicorn on ${WS_HOST:-0.0.0.0}:${WS_PORT:-8000}, /ws
python scripts/run_graph_demo.py "<goal>"  # headless graph, no hub, no sockets
```

Full-stack commands live in one place: `../goal-flow-agents/docs/FINAL_DEMO.md`.

### Environment

| Variable | Note |
|---|---|
| `OPENROUTER_API_KEY` | **Required** |
| `OPENROUTER_MODEL` | Default `openai/gpt-oss-120b`. The `:free` variants are throttled and unusable |
| `OPENROUTER_PROVIDER_ORDER` | **Pin it.** See below |
| `OPENROUTER_PROVIDER_ALLOW_FALLBACKS` | `false`, deliberately |
| `OPENROUTER_REASONING_EFFORT` | Exists and ships **OFF**. Read the comment in `config.py` first |
| `GOALFLOW_PROFILE_PATH` | A scratch constraint store |
| `FISH_API_KEY` / `SPEECH_ENABLED` | The voice |
| `WS_HOST` / `WS_PORT` / `LOG_LEVEL` | |

> **`OPENROUTER_PROVIDER_ORDER=cerebras` decides how fast this is.** Unset, OpenRouter
> load-balances across nineteen endpoints spanning 39 times in throughput and lands on the slow
> ones — interpretation measured **19.7 s unpinned against 2.3 s pinned**. Fallbacks are off
> deliberately: the next-best provider measured 203 to 234 s on the real pipeline, slower than no
> pinning at all, so a fallback is a stall rather than a degrade.
>
> Every client is built by `graph/nodes.py:build_chat()`. The startup `llm_routing` line tells
> you in one glance whether the pin took.

> **`low` reasoning effort was measured to break the output outright.** The knob is built,
> documented and shipped off.

### How many LLM calls a goal costs here

**Three**, and each logs `llm_call site=… elapsed_ms=…`:

`interpret` · `detect_constraints` · `soft_select`

> A fourth once existed. It ran **twice** per goal — LangGraph re-executes a node from the top
> when it resumes from `interrupt()`, and the call sat above the interrupt — for a sentence no
> surface renders. That is two wasted round trips inside the interpretation window, and two more
> chances to draw a provider 429. **Anything you add above an `interrupt()` runs twice.**

## Where things are

`CODE_GUIDE.md` walks all of it. The four files that matter:

| File | Owns |
|---|---|
| `server.py` | The hub: sessions, routing, the create bracket, the cross-goal fan-out, speech |
| `graph/nodes.py` | The graph: nodes, routers, the four interrupts, the retry policy |
| `board.py` | The fold: every goal's frames → one `GoalSummary` |
| `memory/store.py` | The household store: load, resolve per goal, append captures |

### Three facts that send sessions to the wrong file

**The interpreter picks `domain` from what the DEVICE advertises.** It prefers one of
`capabilities.domains[].id` — the device routes on the exact string — and coins a new slug only
when none fit. There is **no** `_canonical_domain()` normaliser; that keyword hack was removed.
Six domains are advertised today: `meal_plan`, `guest_dinner`, `vacation_prep`, `birthday_party`,
`grocery_cost`, `energy_saving`.

**The dispatch `time_window` always STARTS at the device's today**, for every goal, because
monitoring begins now. The **end** is the interpreter's, falling back to today+6 only when it
gives none, or one at or before the start.

> There is **no meal-plan carve-out.** A previous version of this file claimed the meal window
> was pinned to `today..today+6`, and it is not — that claim sent a bug hunt to the wrong file.
> It matters, because the interpreter reads *"this week"* as ending on Sunday about half the
> time, so a seven-day meal plan routinely runs under a three- or four-day window. **The DEVICE
> reconciles that** (`ResolveLastDay` takes the later of the window and the plan), not the cloud.

**The cloud reasons in the DEVICE's day, not the machine's.** `_today(state)` reads `world_today`,
which the hub learns from `status.payload.sim_date` and `day_advanced.sim_date`, per session, and
`start_goal` stamps it **once per run**. Calling `date.today()` in a node is a bug: the two agree
only until the first *Advance day*.

## Contract touchpoints

| Direction | Frames |
|---|---|
| From a surface | `user_goal`, `understanding_response`, `approval`, `control`, `board_get`, `goal_state_get`, `select_device`, `hello` |
| From the device | `capabilities`, `agent_event`, `plan_ready`, `proposal`, `status`, `day_advanced` |
| To a surface | `hello_ack`, `devices`, `goal_accepted`, `chat_ui_open`/`chat_ui_close`, `understanding`, `speech`, `present_plan`, `notice`, `agent_event`, `proposal`, `status`, `board_snapshot`/`board_update`, `day_advanced` |
| To the device | `dispatch`, `approval`, `control` |

`CONTRACT.md` is authoritative for every shape.

> **Adding a frame type means five edits, not one.** `CONTRACT.md`, `models/contract.py`, three
> `types/contract.ts`, `Contracts/*.cs` — **and the three `INBOUND_TYPES` allowlists**, where a
> missing type is *silently dropped*. `verify_mirrors.py` gates all of it.

> **A `Literal` is a hard gate.** `control.command` once reached the device and `CONTRACT.md` but
> not the Python `Literal`, so the frame failed validation on the **sender's** side and the
> headline moment of a demonstration silently did not happen. Gate 14 now checks that enumeration
> too.

## The retry policy, and why it is two counters

Every cloud LLM call goes through `invoke_llm(site, call)`.

| Failure | Policy |
|---|---|
| A 429 | Waited out: 6 retries at 2 s, 4 s, 8 s…, capped at 20 s, jittered. It costs **no** ordinary attempt |
| A dropped socket | Retried fast: 2 retries at 0.4 s, 0.8 s. It costs **no** rate-limit attempt |
| Anything else — a bad key, a broken prompt | Raises at once. The LLM-only rule is to fail loudly |

Separate counters are the point. The numbers are copied from the device's policy deliberately, so
one set of measurements explains both tiers.

Before this, one call site had a single sub-second retry and the other two had none. The
provider's recovery window is about **three seconds**, so a single 429 killed the goal before the
confirmation card was drawn. Somebody hit exactly that in a pre-demonstration run.

> **`max_retries=1` at `interpret_goal` stays, and that was measured, not reasoned.** Setting it
> to 0 — so only one layer owned retry — took `verify_dates.py` from 3 passes in 3 runs to 3 in
> **6**. Restoring it returned 3 in 3. The two layers do different jobs at different time scales,
> and both are wanted. Gate 37 asserts it.
>
> **One run of an LLM gate proves nothing.** Re-run `verify_dates.py` several times before you
> touch this.

## The gates

```bash
python scripts/verify_constraints.py    # and the rest
```

Everything except gates 10 and 12 runs with **no key**. Gate 28's interpreter half needs one and
**skips** without it, rather than passing — a gate that cannot run must not be able to pass.

| Script | # | Pins |
|---|---|---|
| `verify_generic_gate.py` | 10 | Actionability is generic, judged against the device. **Needs a key; the slow one** |
| `verify_persistence.py` | 12 | A goal survives a restart, still at its gate |
| `verify_board.py` | 13 | The fold's numbers are derived and add up |
| `verify_mirrors.py` | 14 | No mirror, allowlist or enumeration has drifted |
| `verify_constraints.py` | 15 | Resolution per goal; the enforced set is never narrowed |
| `verify_capture.py` | 16 | A rule is written only when the user says yes |
| `verify_crossgoal.py` | 17 | The blast radius, the authorship guard, newest-wins |
| `verify_close_hold.py` | 18 | The webview outlasts its save |
| `verify_refusal_replay.py` | 27 | A refusal reaches its webview |
| `verify_dates.py` | 28 | A named weekday means that weekday |
| `verify_worldclock.py` | 29 | The cloud reasons in the device's day |
| `verify_speech.py` | 31 | The voice says the right thing, and its absence costs nothing |
| `verify_no_hang.py` | 33 | A dispatch is always answered |
| `verify_rate_limit.py` | 37 | The retry layering. **Offline** — no key, no network |
| `e2e_two_goals.py` | — | **NOT a gate.** The real two-goal demo, headless, against a live stack |

### Three of them exist because of a real failure, and are worth reading first

**Gate 17 — the cross-goal fan-out.** It pins the blast radius and the idempotence of the only
path that changes a plan without asking. It found a real bug on its first run:
`append_constraints` was not idempotent for an identical entry, so a re-sent approval wrote a
second away window and re-planned a goal the user had already watched change.

**Gate 18 — the create-phase dwell.** The webview must outlast the work it claims to be doing.
**The dwell cannot live in the chat UI** — Bixby unmounts the webview the instant
`chat_ui_close` arrives, so a hold inside the iframe is a hold nobody sees. The cloud owns the
bracket and therefore the dwell.

**Gate 27 — the refusal replay.** A refusal is the only create phase with **no round trip in
it**, so the `notice` was broadcast while Bixby was still mounting the webview it was meant for.
Nobody was connected, and the user watched a blank panel appear and close seconds later. Terminal
notices now join the create-phase replay cache; the mid-save `updating_goals` deliberately does
not.

## Two behaviours that will look like bugs

**The voice fails silently, and that is deliberate.** No `FISH_API_KEY` means no `speech` frame,
and every surface renders exactly as it would in silence. It is the **opposite** of the LLM-only
rule, and the reason is that a gate which is already complete and answerable loses nothing to a
missing voice. Read `speech/__init__.py` before making a failure here loud.

`SPEECH_ENABLED=false` silences it while leaving the key in place — the development switch,
because iterating on a surface with a key set means the fridge talks on every reload. The startup
`speech_routing` line names **which** silence you are in, so a quiet run is never a mystery.

**Autoplay is the surface's problem, and it is real.** A browser refuses `audio.play()` without a
user gesture, so the chat UI degrades to a *"Hear this"* tap. Do not "fix" that by assuming
autoplay.

## Conventions

- **Commit identity:** author as `ashuksingh11`
  (`31301999+ashuksingh11@users.noreply.github.com`). **Push only when explicitly asked.**
- **Confirm before moving between phases.** Every phase leaves durable artifacts behind.
- **LLM-only by design.** Do not add a scripted or rules fallback.
- **`CONTRACT.md` moves first**, then every mirror in one pass.
- Run the gate for whatever you touched, and always `verify_mirrors.py` if you touched a frame.

## Known limits

| Limit | Detail |
|---|---|
| **The household store is global** | `device_id` scopes delivery and `goal_id` scopes each graph run, but `family_profile.json` is shared across every session. Keying it by `device_id` is the next real step |
| **The goal-lock dictionary is unbounded** | One entry per `goal_id` ever seen. Each is a bare `asyncio.Lock`, so it is acceptable for now; real cleanup belongs with a goal store |
| **A closed-socket `receive_json` can raise** | Caught and logged, never fatal. Known benign log noise |
| **The device streams a lot of thinking** | Hundreds of frames per run. The surfaces coalesce them; do not add per-frame work to the relay path |

Graph writes are serialised **per goal** by `goal_lock`, and concurrent **across** goals — goal
B's interpretation must not wait on goal A's approval. Every graph entry point takes it.
