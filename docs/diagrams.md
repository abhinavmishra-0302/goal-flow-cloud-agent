# GoalFlow v2 Diagrams

(The cloud StateGraph diagram lives in [ARCHITECTURE.md](ARCHITECTURE.md).)

## 1. Full flow (sequence)

Happy path plus a later adaptation. Note the gates: a **confirm-understanding
gate** pauses on `present_understanding` before anything is dispatched; the
**safety filter** runs as deterministic code on the device before anything is
surfaced; the **approval gate** is the user, reached via the cloud's LangGraph
`interrupt()`. New in v2: the device advertises `capabilities`, `agent_event`s
stream live (device → cloud → UI passthrough) while the device plans, and the
UI can fire a `control { command: "trigger_event" }` at any time for the
event-driven demo (pure passthrough to the device — no cloud logic change).

```mermaid
sequenceDiagram
    autonumber
    participant UI as UI (wow UI)
    participant Cloud as Cloud Agent (WS hub + LangGraph)
    participant Device as Device Agent (SK planner)

    Note over UI,Device: Handshake (each client opens ONE outbound WS to the cloud)
    UI->>Cloud: hello { role: "ui" }
    Cloud-->>UI: hello_ack { session_id }
    Device->>Cloud: hello { role: "device" }
    Cloud-->>Device: hello_ack { session_id }
    Device->>Cloud: capabilities { modules: [Inventory, ShoppingList, Safety…] }
    Cloud->>UI: capabilities (relayed)

    Note over UI,Cloud: Goal intake (ANY domain — meal_plan, guest_dinner, …)
    UI->>Cloud: user_goal { "we've got 6 people over Saturday — sort it" }
    Note over Cloud: interpret_goal (LLM structured output)<br/>→ load_memory (hard verbatim / soft bias)<br/>→ present_understanding = interrupt()
    Cloud->>UI: understanding { objective, domain, knew ("what it knew"), thought }

    Note over UI: CONFIRM-UNDERSTANDING GATE (user) — waits before any dispatch.
    UI->>Cloud: understanding_response { confirmed: true }
    Note over Cloud: interrupt() resumed (route_after_understanding)<br/>confirmed → build_contract → dispatch<br/>declined → goal_declined, status{task_status:"done"} to UI, run ends
    Cloud->>Device: dispatch { goal_id, domain, objective, success_criteria,<br/>constraints{hard,soft}, scope, time_window, autonomy:"tiered", context }

    Note over Device: SK function-calling planner works;<br/>SAFETY FILTER (code) enforces constraints.hard only.
    Device-->>Cloud: agent_event { seq:1, phase: grounding }
    Cloud-->>UI: agent_event (passthrough)
    Device-->>Cloud: agent_event { seq:2, tool_call: Inventory.GetExpiringItems }
    Cloud-->>UI: agent_event (passthrough)
    Device-->>Cloud: agent_event { seq:3, thinking / tool_result / plan_progress… }
    Cloud-->>UI: agent_event (passthrough)

    Device->>Cloud: plan_ready { task_status: awaiting_approval,<br/>plan + TIERED proposals + safety + impact + explanation }
    Note over Cloud: graph resumes → hitl_approval = interrupt()<br/>(state checkpointed, thread_id = goal_id)
    Cloud->>UI: present_plan { payload + knew ("what it knew") }

    Note over UI: APPROVAL GATE (user) — waits. Nothing firm executes.
    UI->>Cloud: approval { decisions: [ {p1, approved:true} ] }
    Note over Cloud: interrupt() resumed with decisions
    Cloud->>Device: approval { correlation_id, decisions }
    Device->>Cloud: status { task_status: executing, executed:[…] }
    Cloud->>UI: status (relayed)

    Note over UI,Device: Later — adaptation (a material world change)
    Device->>Cloud: proposal { task_status: adapting, trigger, tier, requires_approval }
    Cloud->>UI: proposal (relayed — the adapt loop re-enters hitl_approval)
    UI->>Cloud: approval { decisions: [ {a1, approved:true} ] }
    Cloud->>Device: approval
    Device->>Cloud: status { task_status: done }
    Cloud->>UI: status (relayed)

    Note over UI,Device: Event-driven demo (any time): the device ships a demo_events<br/>catalog on plan_ready; the UI can fire one on demand.
    UI->>Cloud: control { command: "trigger_event", goal_id, payload: { event_id } }
    Cloud->>Device: control (forwarded — no cloud logic change, pure passthrough)
```

## 2. Three tiers + WS hub (components)

```mermaid
flowchart LR
    subgraph UIT["UI tier — wow UI"]
        Stream[Live stream: progress rail,<br/>tool-call chips, thinking]
        Plan[Plan-as-hero + tiered approvals]
        WSC[WS client]
        Stream --> WSC
        Plan --> WSC
    end

    subgraph CLOUD["Cloud tier — goal-flow-cloud-agent (WS HUB)"]
        Hub[FastAPI /ws hub<br/>registry by role · correlation-id logs]
        Router[Router: type + role<br/>agent_event = passthrough]
        Graph["LangGraph StateGraph<br/>interpret_goal → load_memory<br/>→ present_understanding (interrupt) → build_contract<br/>→ dispatch → collect_plan → hitl_approval (interrupt)<br/>→ relay_decisions → monitor (adapt loop)"]
        CP[(Checkpointer<br/>thread_id = goal_id)]
        Mem[(family_profile.json<br/>hard safety block vs soft prefs)]
        LLM[OpenRouter LLM<br/>openai/gpt-oss-120b<br/>LLM-only, no fallback]
        Hub --> Router
        Router --> Graph
        Graph --> CP
        Graph --> Mem
        Graph --> LLM
    end

    subgraph DEV["Device tier — on-device agent (other repo)"]
        Caps[Capability modules (SK plugins)<br/>advertised via capabilities]
        Planner[SK auto function-calling planner]
        Gate[["SAFETY FILTER<br/>deterministic code<br/>enforces constraints.hard only"]]
        Act[Actuators / local state<br/>sole authority · generic clock]
        Planner --> Gate --> Act
        Caps --> Planner
    end

    WSC <-- "one outbound WS" --> Hub
    Hub <-- "one outbound WS" --> Planner

    UIT -. "NEVER directly" .- DEV
```
