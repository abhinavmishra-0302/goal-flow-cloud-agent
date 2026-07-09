# GoalFlow Diagrams

## 1. Full flow (sequence)

Happy path plus a later calendar-triggered adaptation. Note the two gates:
the **safety gate** runs as deterministic code on the device before anything
is surfaced; the **approval gate** is the user, reached via the cloud.

```mermaid
sequenceDiagram
    autonumber
    participant UI as UI (tablet chat)
    participant Cloud as Cloud Agent (WS hub)
    participant Device as Device Agent

    Note over UI,Device: Handshake (each client opens ONE outbound WS to the cloud)
    UI->>Cloud: hello { role: "ui" }
    Cloud-->>UI: hello_ack { session_id }
    Device->>Cloud: hello { role: "device" }
    Cloud-->>Device: hello_ack { session_id }

    Note over UI,Cloud: Goal intake
    UI->>Cloud: user_goal { "help my family eat healthier…" }
    Note over Cloud: M1: hardcoded contract<br/>M2: ambiguity → memory → decompose → relay
    Cloud->>Device: dispatch { goal_id, constraints.hard, … }

    Note over Device: Harness pipeline plans.<br/>SAFETY GATE (code) checks constraints.hard — blocks on violation.
    Device->>Cloud: plan_ready { correlation_id: disp-001,<br/>task_status: awaiting_approval, plan + proposals }
    Cloud->>UI: present_plan { same payload + display hints }

    Note over UI: APPROVAL GATE (user) — waits.
    UI->>Cloud: approval { decisions: [ {p1, approved:true} ] }
    Cloud->>Device: approval { correlation_id, decisions }
    Note over Device: Only now does the device act on p1.
    Device->>Cloud: status { task_status: executing,<br/>"added 3 items to shopping list" }
    Cloud->>UI: status (relayed)

    Note over UI,Device: Later — adaptation (calendar event shrinks Wed prep window)
    Device->>Cloud: proposal { correlation_id: evt-014, task_status: adapting,<br/>p7: add_prep_task, requires_approval:true }
    Cloud->>UI: proposal (relayed for decision)
    UI->>Cloud: approval { decisions: [ {p7, approved:true} ] }
    Cloud->>Device: approval { correlation_id: evt-014 }
    Device->>Cloud: status { task_status: done }
    Cloud->>UI: status (relayed)
```

## 2. Three tiers + WS hub (components)

```mermaid
flowchart LR
    subgraph UIT["UI tier — goal-flow-agent-chat-ui"]
        Chat[ChatView]
        Plan[PlanCard]
        Mic[MicButton / Web Speech STT]
        WSC[ws.ts client]
        Chat --> WSC
        Plan --> WSC
        Mic --> Chat
    end

    subgraph CLOUD["Cloud tier — goal-flow-cloud-agent (WS HUB)"]
        Hub[FastAPI /ws hub<br/>connection registry by role]
        Router[Router: type + role]
        Graph["LangGraph pipeline (M2)<br/>ambiguity → memory → decompose → relay"]
        Mem[(family_profile.json<br/>hard vs soft memory)]
        LLM[OpenRouter LLM<br/>anthropic/claude-sonnet-5<br/>+ mock fallback]
        Hub --> Router
        Router --> Graph
        Graph --> Mem
        Graph --> LLM
    end

    subgraph DEV["Device tier — on-device agent (other repo)"]
        Harness[Harness pipeline]
        Gate[["SAFETY GATE<br/>deterministic code<br/>reads constraints.hard only"]]
        Act[Actuators / local state<br/>sole authority]
        Harness --> Gate --> Act
    end

    WSC <-- "one outbound WS" --> Hub
    Hub <-- "one outbound WS" --> Harness

    UIT -. "NEVER directly" .- DEV
```
