"""Pydantic mirror of GoalFlow CONTRACT v2 (canonical spec: /CONTRACT.md).

Design notes
------------
- Every message is a JSON object discriminated by the ``type`` field.
- Task messages carry ``goal_id``; device<->cloud messages carry
  ``correlation_id`` (dedupe key; also correlates approvals to proposals).
- GENERIC & DOMAIN-AGNOSTIC: no meal-specific fields. A ``domain`` string
  carries the use case; domain specifics live in capability modules plus the
  free-form ``scope`` / ``context`` objects.
- ``Constraints.hard`` is the ONLY thing the device Safety filter enforces.
  It is injected verbatim from memory — never produced by LLM semantics.
- LENIENT on unknown fields: every model allows extras so the protocol can
  evolve (new payload keys) without breaking older peers.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared vocabulary
# ---------------------------------------------------------------------------

Role = Literal["ui", "device"]

#: UI socket surface (v4.1, ui-only, optional). Declared once at handshake and
#: immutable for the socket's lifetime. Absent/empty ⇒ the socket receives the full
#: session broadcast, exactly like every v3 client (which is what makes it additive).
#: Only ``"input"`` changes delivery today (see the delivery fork in server.py);
#: ``"chat"`` additionally opts in to the create-phase replay on bind. Kept lenient
#: (a plain ``str``) rather than a strict Literal so an absent/empty value and any
#: future surface name flow through without a validation drop.
Surface = Literal["input", "chat", "board"]

#: Proposal/action tier: reversibility x cost x risk.
#: auto  = reversible, do without asking (still reported);
#: light = cheap/low-risk, one-tap approval;
#: firm  = costly/irreversible (e.g. spends money), explicit approval required.
Tier = Literal["auto", "light", "firm", "adapt"]

#: Task-status lifecycle (CONTRACT v2):
#: created -> interpreting -> grounding -> planning -> checking ->
#: awaiting_approval -> executing -> monitoring -> adapting -> done
TaskStatus = Literal[
    "created",
    "interpreting",
    "grounding",
    "planning",
    "checking",
    "awaiting_approval",
    "executing",
    "monitoring",
    "adapting",
    "done",
]

#: agent_event stream event kinds.
#:
#: A Literal here is a HARD GATE, not documentation: an unlisted value fails
#: validation and the frame is DROPPED — silently, with the board simply sitting at
#: 0% and no error anywhere. That is exactly what happened when ``task_update`` was
#: added to CONTRACT.md and the C# mirror but not here, and it is why every mirror
#: must move in one pass.
AgentEventKind = Literal[
    "phase",
    "thinking",
    "tool_call",
    "tool_result",
    "plan_progress",
    #: v3: the device's task ledger moved — the board's progress/next-step/pending.
    "task_update",
    #: v5: which HARNESS ENGINE is at work (precheck / capability_manager / grounding /
    #: planner / safety / task_manager / approval / monitor_adapt). Drives the "harness
    #: pipeline" the UI lights up engine-by-engine. Relayed passthrough like every kind.
    "harness",
]


class _ContractModel(BaseModel):
    """Base for every contract model: lenient on unknown fields."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class Hello(_ContractModel):
    """Client -> cloud, first frame on connect: registers the client's role.

    ``device_id`` is the PAIRING KEY (a "home" = one device agent + N UIs).
    A device agent sends its own stable id (self-generated persistent UUID, or
    an override); a UI sends the device_id it wants to watch. Absent/empty means
    ``"default"`` (single-pair, zero-config back-compat) for a device, and
    "unbound — await discovery" for a UI. ``device_name`` is a device-only
    human label surfaced to UIs in the device picker.
    """

    type: Literal["hello"] = "hello"
    role: Role
    device_id: str = ""
    device_name: str = ""
    #: v4.1, ui-only: "input" | "chat" | "board". Absent/empty ⇒ full broadcast
    #: (a v3 client). Ignored on a role:"device" hello. Kept lenient (str, not a
    #: Literal) so "" and unknown future surfaces never fail validation.
    surface: str = ""


class HelloAck(_ContractModel):
    """Cloud -> client: acknowledges registration and assigns a session."""

    type: Literal["hello_ack"] = "hello_ack"
    role: Role
    session_id: str
    device_id: str = ""


class DeviceInfo(_ContractModel):
    """One connected device, for the UI's device picker."""

    device_id: str
    device_name: str = ""
    online: bool = True


class Devices(_ContractModel):
    """Cloud -> ui: the currently-connected device agents to pick from.

    Sent to a UI that connected without a ``device_id`` (and whenever the set
    changes) so it can auto-bind (exactly one) or show a picker.
    """

    type: Literal["devices"] = "devices"
    devices: list[DeviceInfo] = Field(default_factory=list)


class SelectDevice(_ContractModel):
    """ui -> cloud: bind this UI socket to a device_id (from the picker)."""

    type: Literal["select_device"] = "select_device"
    device_id: str


# ---------------------------------------------------------------------------
# capabilities (device -> cloud -> ui) — the module registry
# ---------------------------------------------------------------------------


class ModuleFunction(_ContractModel):
    """One callable function a capability module exposes to the planner."""

    name: str
    description: str = ""
    #: True when calling this function changes the world (must be proposed).
    side_effecting: bool = False
    #: Default approval tier for side-effecting calls (None for read-only).
    tier: Tier | None = None


class Module(_ContractModel):
    """A device module: a capability toolbox or a steering/harness module."""

    name: str
    #: "capability" = tools the planner may call; "steering" = harness module
    #: that guards/steers (e.g. the deterministic Safety filter).
    kind: Literal["capability", "steering"] = "capability"
    description: str = ""
    functions: list[ModuleFunction] = Field(default_factory=list)


class Capabilities(_ContractModel):
    """Device -> cloud -> ui: the device's advertised MODULE REGISTRY."""

    type: Literal["capabilities"] = "capabilities"
    modules: list[Module] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# user_goal (ui -> cloud)
# ---------------------------------------------------------------------------


class UserGoal(_ContractModel):
    """Raw natural-language goal from the user."""

    type: Literal["user_goal"] = "user_goal"
    text: str
    #: UI-minted id, echoed back in ``goal_accepted`` (v3).
    #:
    #: With two goals in flight the UI CANNOT tell which inbound goal_id belongs to
    #: which submission — it would have to adopt whichever arrives first and mis-key
    #: the card. Optional: a v2 client that omits it still works.
    client_ref: str | None = None


# ---------------------------------------------------------------------------
# dispatch (cloud -> device) — the GENERIC Task Contract
# ---------------------------------------------------------------------------


class HardConstraints(_ContractModel):
    """The safety policy — the ONLY block the device Safety filter enforces.

    Resolved for THIS GOAL by code (memory/store.py ``resolve_constraints``) from
    the household constraint store; never generated or paraphrased by the LLM.
    Extra keys are allowed so new policy dimensions can flow through without a
    contract bump.

    v6: the list kinds are unioned across the whole store regardless of domain (the
    enforced set is never narrowed by relevance), while the window/cap kinds below
    are domain-picked — which is why a vacation goal carries a travel cap and an
    away window where a meal goal carries the weekly grocery cap.
    """

    allergens: list[str] = Field(default_factory=list)
    medical: list[str] = Field(default_factory=list)
    dietary: list[str] = Field(default_factory=list)
    #: Currency-agnostic spend ceiling (None = no cap). Domain-picked in v6.
    budget_cap: float | None = None
    #: e.g. {"start": "21:30", "end": "07:00"} — no noisy appliances inside.
    quiet_hours: dict[str, str] | None = None
    #: v6, e.g. {"start": "17:00", "end": "21:00"} — peak electricity tariff; heavy
    #: appliance runs inside it are blocked on the goals scoped to it.
    peak_hours: dict[str, str] | None = None
    #: v6, ISO DATES e.g. {"start": "2026-07-30", "end": "2026-08-06"} — the house is
    #: empty; nothing may be scheduled to run in it. (Enforced from M2.)
    away_window: dict[str, str] | None = None
    #: v6-M3, e.g. {"cap": 600.0, "period": "monthly"} — the shared pool EVERY goal
    #: draws from. The device resolves this goal's effective ceiling as
    #: min(budget_cap, cap - spent), because per-goal caps alone cannot stop two
    #: goals spending the same money.
    budget_envelope: dict[str, Any] | None = None


class Constraints(_ContractModel):
    """hard = enforced safety policy; soft = free-form preference bias."""

    hard: HardConstraints = Field(default_factory=HardConstraints)
    soft: dict[str, Any] = Field(default_factory=dict)


class TimeWindow(_ContractModel):
    """ISO start/end, always RELATIVE to real today (or the control clock)."""

    start: str
    end: str


class Dispatch(_ContractModel):
    """Cloud -> device: the generic Task Contract.

    ``scope`` and ``context`` are domain-flexible objects; ``domain`` names
    the use case (e.g. "meal_plan", "guest_dinner") so the device can select
    its capability-module set.
    """

    type: Literal["dispatch"] = "dispatch"
    goal_id: str
    domain: str
    objective: str
    success_criteria: list[str] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    scope: dict[str, Any] = Field(default_factory=dict)
    time_window: TimeWindow
    autonomy: str = "tiered"
    context: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


# ---------------------------------------------------------------------------
# agent_event (device -> cloud -> ui) — the live stream
# ---------------------------------------------------------------------------


class AgentEvent(_ContractModel):
    """Streamed while the device works; relayed passthrough to the UI.

    Payload shape depends on ``event``:
      phase:         {"phase": "grounding"|"planning"|"checking"|"awaiting_approval"}
      thinking:      {"text": "...", "kind"?, "step"?, "detail"?}   # v7
      tool_call:     {"module": "...", "function": "...", "args": {...}}
      tool_result:   {"module": "...", "function": "...", "summary": "..."}
      plan_progress: {"item": {...}, "total": 7}   # total optional (v5.1)
      task_update:   {"task_id", "title", "state", "depends_on", "progress_pct",
                      "pending_tasks", "next_step", "retry_count", "failure_reason"}

    ``plan_progress.total`` (v5.1, optional) is the finished plan's item count. The
    device emits every item in one loop after a single non-streaming compose call, so
    without it a UI cannot know how many rows are still coming and cannot reserve them.
    Absent on pre-v5.1 devices — unknown, not zero.

    ``task_update`` (v3) is how the cloud learns a goal's shape and progress: the task
    DAG lives on the DEVICE (only it can ground a decomposition), so Agent Board's
    numbers are folded from these rather than guessed from the clock.

    ``thinking.kind`` (v7, optional) is ``narration`` | ``step`` | ``notice``; absent
    means ``narration``, which is every thinking event emitted before v7. A ``step``
    carries ``step`` (headline) and ``detail`` (sub-line) and is WHOLE on arrival, never
    fragmented — so a client renders it immediately instead of accumulating chunks and
    guessing where one thought ends. ``text`` still holds "step — detail", so a client
    that ignores the new fields is unaffected.
    """

    type: Literal["agent_event"] = "agent_event"
    goal_id: str
    correlation_id: str
    #: Monotonic per-goal sequence number (ordering + dedupe aid).
    seq: int
    event: AgentEventKind
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# plan_ready (device -> cloud) / present_plan (cloud -> ui)
# ---------------------------------------------------------------------------


class PlanItem(_ContractModel):
    """One generic step of the plan (domain-agnostic)."""

    id: str
    title: str
    detail: str = ""
    #: Optional ISO timestamp/date the step is scheduled for.
    when: str | None = None
    why: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    #: v7: absent or "planned" is a normal row; "skipped" is a day deliberately left
    #: empty — still rendered, styled down, carrying its reason. Deleting the row was
    #: the alternative and it is worse: a shorter plan says nothing about WHY.
    status: str | None = None
    status_reason: str | None = None


class PlanProposal(_ContractModel):
    """A TIERED proposed action attached to a plan.

    Proposals are proposals, not actions: nothing firm executes until an
    approval returns. ``module``/``function``/``args`` name the exact
    capability call that would run.
    """

    proposal_id: str
    action: str
    module: str
    function: str
    args: dict[str, Any] = Field(default_factory=dict)
    tier: Tier
    reason: str = ""
    requires_approval: bool = True


class SafetyResult(_ContractModel):
    """Outcome of the device-side deterministic Safety filter."""

    gate: Literal["passed", "blocked"]
    violations: list[str] = Field(default_factory=list)


class ImpactItem(_ContractModel):
    """One label/value impact metric (e.g. waste saved, estimated cost)."""

    label: str
    value: str


class RejectedOption(_ContractModel):
    """One option the planner considered and did not take, and why (v7)."""

    option: str
    reason: str


class PlanPayload(_ContractModel):
    """The plan_ready payload: plan + tiered proposals + safety + impact."""

    plan: list[PlanItem] = Field(default_factory=list)
    proposals: list[PlanProposal] = Field(default_factory=list)
    safety: SafetyResult
    impact: list[ImpactItem] = Field(default_factory=list)
    explanation: str = ""
    #: v7, model-authored and DISPLAY ONLY: how many options were weighed, and which
    #: were discarded with the reason each time. Nothing downstream reads them — a
    #: lookup table cannot reject, so this is the clearest evidence a person can be
    #: given that something reasoned. Absent is normal.
    considered: int | None = None
    rejected: list[RejectedOption] | None = None
    #: Cloud-added on present_plan only: the personalization "what it knew".
    knew: dict[str, Any] | None = None


class PlanReady(_ContractModel):
    """Device -> cloud: plan produced, awaiting user approval."""

    type: Literal["plan_ready"] = "plan_ready"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus = "awaiting_approval"
    payload: PlanPayload


class PresentPlan(_ContractModel):
    """Cloud -> ui: plan_ready relayed + ``payload.knew`` added by the cloud."""

    type: Literal["present_plan"] = "present_plan"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus = "awaiting_approval"
    payload: PlanPayload


# ---------------------------------------------------------------------------
# understanding (cloud -> ui) / understanding_response (ui -> cloud)
# ---------------------------------------------------------------------------


class UnderstandingPayload(_ContractModel):
    """Pre-planning confirmation payload shown before device dispatch."""

    objective: str
    domain: str = ""
    #: Display-ready hard-constraint chips, same shape as PlanPayload.knew.
    knew: dict[str, Any] = Field(default_factory=dict)
    #: v6, ADDITIVE: one row per applied HARD constraint — {id, kind, label, value,
    #: enforcement, source, why}. Provenance for the gate: a block the user cannot
    #: trace is a block they will not trust. `knew` is unchanged, so a UI may ignore
    #: this. v7: rows this domain does not display are omitted, so these line up
    #: one-for-one with `knew`'s chips.
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    #: v7, ADDITIVE: the SOFT half — {id, label, value, source, why}, one row per
    #: entry. Kept out of `knew` and out of `constraints` on purpose: a preference
    #: shapes the plan and can never block it, and a UI that renders the two alike
    #: teaches the reader that a chip is a chip. Empty is normal.
    preferences: list[dict[str, Any]] = Field(default_factory=list)
    #: v6-M4, ADDITIVE: household rules the user STATED in this message, awaiting a
    #: yes. Proposals only — the LLM never writes policy, so nothing here applies
    #: until it comes back in `understanding_response.accepted_constraint_ids`.
    proposed_constraints: list[dict[str, Any]] = Field(default_factory=list)
    #: v6-M4: this gate is a constraint capture, not a goal — there is no plan
    #: coming, and the UI should ask only about the rules.
    capture_only: bool = False
    thought: str = ""
    time_window: dict[str, str] | None = None


class Understanding(_ContractModel):
    """Cloud -> ui: interpreted objective + hard constraints, awaiting confirm."""

    type: Literal["understanding"] = "understanding"
    goal_id: str
    correlation_id: str | None = None
    task_status: TaskStatus = "grounding"
    payload: UnderstandingPayload


class UnderstandingResponsePayload(_ContractModel):
    confirmed: bool
    #: v6-M4: which proposed constraints the user actually said yes to. Absent or
    #: empty means none — silence never captures a household rule, and confirming
    #: the GOAL does not silently confirm a rule that rode along with it.
    accepted_constraint_ids: list[str] = Field(default_factory=list)


class UnderstandingResponse(_ContractModel):
    """UI -> cloud: confirm or decline the pre-planning understanding gate."""

    type: Literal["understanding_response"] = "understanding_response"
    goal_id: str
    payload: UnderstandingResponsePayload


# ---------------------------------------------------------------------------
# approval (ui -> cloud -> device)
# ---------------------------------------------------------------------------


class ApprovalDecision(_ContractModel):
    proposal_id: str
    approved: bool


class ApprovalPayload(_ContractModel):
    decisions: list[ApprovalDecision] = Field(default_factory=list)


class Approval(_ContractModel):
    """UI -> cloud -> device: the user's decisions, correlated back to the
    proposal via ``correlation_id``. This is the APPROVAL gate (user, via
    cloud) — distinct from the device's deterministic Safety gate."""

    type: Literal["approval"] = "approval"
    goal_id: str
    correlation_id: str
    payload: ApprovalPayload


# ---------------------------------------------------------------------------
# proposal (device -> cloud -> ui) — mid-task adaptation
# ---------------------------------------------------------------------------


class AdaptationPayload(_ContractModel):
    proposal_id: str
    action: str
    detail: str = ""
    #: What caused the adaptation (a material world change).
    trigger: str = ""
    tier: Tier = "light"
    requires_approval: bool = True


class Proposal(_ContractModel):
    """Device -> cloud -> ui: a generic mid-task adaptation proposal."""

    type: Literal["proposal"] = "proposal"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus = "adapting"
    payload: AdaptationPayload


# ---------------------------------------------------------------------------
# status (device -> cloud -> ui)
# ---------------------------------------------------------------------------


class StatusPayload(_ContractModel):
    day: str | None = None
    #: The device's current (possibly simulated) ISO date.
    sim_date: str | None = None
    #: True when a MATERIAL world change was detected (may trigger adapt).
    material: bool = False
    #: Actions executed since the last status (post-approval effects).
    executed: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""


class Status(_ContractModel):
    """Device -> cloud -> ui: progress/lifecycle update."""

    type: Literal["status"] = "status"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus
    payload: StatusPayload = Field(default_factory=StatusPayload)


# ---------------------------------------------------------------------------
# notice (cloud -> ui) — a terminal, non-plan message
# ---------------------------------------------------------------------------


class Notice(_ContractModel):
    """cloud -> ui: a terminal, non-plan message (e.g. an out-of-scope decline).

    Sent when the graph ends BEFORE any device dispatch — the goal was judged
    outside what the CONNECTED DEVICE advertises it can do (since v3-M4 the
    actionable set is the device's capabilities, not a fixed topic list)."""

    type: Literal["notice"] = "notice"
    goal_id: str
    #: "out_of_scope" = declined by the interpreter's actionability gate;
    #: "declined" (v4.1, active) = the create flow was cancelled (understanding
    #: gate declined / aborted) and is emitted ALONGSIDE ``chat_ui_close`` so the
    #: input (Bixby) surface can SPEAK the cancellation.
    #: "captured" (v6-M4) = the message stated a household rule rather than a goal;
    #: the rule was confirmed and remembered, and no plan was ever coming.
    #: v7: "updating_goals" is NOT terminal — it captions the chat's saving screen while
    #: the cloud pushes a household change to the user's other goals. Kept a plain str so
    #: a new kind never fails validation and vanishes.
    kind: str
    message: str
    #: v9: how long this surface has before the cloud closes the webview under it,
    #: in seconds. Only set where a close is genuinely already scheduled (today:
    #: the out-of-scope refusal, which posts ``_close_after`` in the same breath).
    #:
    #: It is on the wire because the alternative is a duplicated constant. The refusal
    #: card draws a real remaining-time indicator, and a countdown the UI GUESSED would
    #: drift silently the first time this dwell changed — which is exactly the bug the
    #: v9 pass deleted from the working screen. None means "no scheduled close", and
    #: the UI then shows no countdown rather than inventing one.
    closes_in_s: float | None = None


# ---------------------------------------------------------------------------
# chat_ui_open / chat_ui_close (cloud -> ui, v4.1) — the create-phase bracket
# ---------------------------------------------------------------------------


class ChatUiOpen(_ContractModel):
    """cloud -> ui: the create phase for ``goal_id`` has begun (v4.1).

    Dual role, one per surface: ``input`` (Bixby) opens/ensures-open the chat
    webview; ``chat`` (the webview) HARD-RESETS keyed to ``goal_id`` and thereafter
    ignores goal-scoped frames with a different ``goal_id``. The device never sees
    it. Emitted strictly BEFORE the goal's ``understanding`` frame.

    v7.4: emitted the INSTANT the goal is received, before interpretation runs — see
    ``handle_user_goal``. It therefore carries ``goal_text``, because the frame is now
    the only thing the chat surface knows for the 10-60s the interpreter is thinking,
    and a panel that cannot say what it is working on has nothing to show but a spinner.
    """

    type: Literal["chat_ui_open"] = "chat_ui_open"
    goal_id: str
    #: What the user actually said, verbatim. Optional: a v7.3 client that ignores it
    #: renders exactly as before, and the bind-time replay may not have it.
    goal_text: str = ""


class ChatUiClose(_ContractModel):
    """cloud -> ui: the create phase for ``goal_id`` is over (v4.1).

    Bixby closes the webview only if ``goal_id`` matches the goal it currently has
    open; the board owns the goal after. Emitted on the initial approval, on a
    declined understanding gate, and on a post-open terminal error. The device
    never sees it.
    """

    type: Literal["chat_ui_close"] = "chat_ui_close"
    goal_id: str


# ---------------------------------------------------------------------------
# control (ui -> cloud -> device)
# ---------------------------------------------------------------------------


class ControlPayload(_ContractModel):
    #: ISO date for command == "set_date".
    date: str | None = None
    #: Daily demo event id for command == "trigger_event".
    event_id: str | None = None


class Control(_ContractModel):
    """UI/operator -> cloud -> device: deterministic clock/lifecycle command.

    The device clock is GENERIC: real today by default, or set via these
    commands — never a hardcoded date.

    v3.2: ``goal_id`` is OPTIONAL. The clock is GLOBAL, so a clock command with no
    goal_id is a WORLD-level tick — the device advances it once and fans out to every
    active goal, emitting one ``day_advanced`` summary. A goal_id scopes the older
    per-goal path (a ``trigger_event``)."""

    type: Literal["control"] = "control"
    goal_id: str = ""
    #: A Literal here is a HARD GATE: an unlisted value fails validation and the frame is
    #: never sent — silently, from the sender's side. v7 added `constraints_changed` to
    #: the device and to CONTRACT.md and missed this line, and the symptom was a
    #: cross-goal fan-out that logged a pydantic error into the void while the demo's
    #: headline moment simply did not happen. Same lesson as AgentEventKind above: every
    #: mirror moves in one pass.
    command: Literal[
        "advance_day",
        "reset",
        "set_date",
        "trigger_event",
        #: v7: the account re-resolved this goal's constraints because ANOTHER goal was
        #: approved. The one adaptation path that does not ask.
        "constraints_changed",
    ]
    payload: ControlPayload = Field(default_factory=ControlPayload)


class DayEvent(_ContractModel):
    """One world event that happened on an advanced day."""

    id: str
    #: Human-readable headline, e.g. "Day 3 - football practice added Wed".
    title: str
    #: The change kind, e.g. "calendar.event_overlap".
    kind: str | None = None
    summary: str | None = None
    #: Goals this event was material to (empty = happened but touched no active goal).
    goal_ids: list[str] = Field(default_factory=list)


class DayAdvanced(_ContractModel):
    """device -> cloud -> ui: a GLOBAL world tick summary (v3.2).

    Emitted once per world-level ``control`` tick: the day's world events + which goals
    each touched. The board renders it as the "what happened today" card; the per-goal
    ``status``/``proposal`` frames alongside update each goal card. Chat never sees it."""

    type: Literal["day_advanced"] = "day_advanced"
    #: The new simulated date (ISO) after the tick.
    sim_date: str
    #: 1-based sim day from the earliest active goal's window start (0 if none).
    day: int = 0
    events: list[DayEvent] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Discriminated union of every CONTRACT v2 message
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Agent Board (v3) — the board watches EVERY goal; every other frame is about one
# ---------------------------------------------------------------------------


class GoalAlerts(_ContractModel):
    """Things wanting attention on a goal."""

    count: int = 0
    #: "danger" | "warn" | None — None when count is 0.
    severity: str | None = None


class GoalSummary(_ContractModel):
    """One goal, as Agent Board renders it.

    DERIVED BY THE CLOUD from frames it already routes (dispatch, plan_ready,
    task_update, status, proposal, approval). The device is not involved and never
    sends one of these.
    """

    goal_id: str
    client_ref: str | None = None
    title: str
    subtitle: str = ""
    domain: str = ""
    #: "on_track" | "at_risk" | "waiting" | "completed" — the board's four chips.
    state: str = "on_track"
    task_status: str = "created"
    progress_pct: int = 0
    next_step: str | None = None
    #: ISO date the goal is aiming at; the UI renders "2 days" by diffing.
    eta: str | None = None
    pending_tasks: int = 0
    alerts: GoalAlerts = Field(default_factory=GoalAlerts)
    #: The last couple of human-readable things that happened.
    activity: list[str] = Field(default_factory=list)
    #: v7: this plan changed WITHOUT an approval, because another goal the user already
    #: approved changed the household. Rendered as one informational line on the card and
    #: a dismissible notice on the detail page — deliberately NOT an alert, which means
    #: "you still have to decide". Here there is nothing left to decide.
    plan_changed_note: str | None = None
    updated_at: str = ""


class BoardSnapshot(_ContractModel):
    """Every goal in this session. Sent on UI bind and in reply to ``board_get``."""

    type: Literal["board_snapshot"] = "board_snapshot"
    #: Monotonic per session; a UI that sees a gap sends board_get and heals.
    board_seq: int = 0
    goals: list[GoalSummary] = Field(default_factory=list)


class BoardUpdate(_ContractModel):
    """One goal changed.

    Carries a WHOLE GoalSummary, replace-by-goal_id: deltas exist to avoid re-sending
    N goals, not to save bytes within one. Whole-object replacement is idempotent, so
    a duplicate or out-of-order update cannot corrupt a card.
    """

    type: Literal["board_update"] = "board_update"
    board_seq: int = 0
    goal: GoalSummary


class BoardGet(_ContractModel):
    """UI asks for a fresh snapshot (first paint, or healing a board_seq gap)."""

    type: Literal["board_get"] = "board_get"


class GoalStateGet(_ContractModel):
    """UI asks for one goal's cached plan + latest status (drill-in after a reload).

    The agent_event stream is deliberately NOT replayed: a rejoined view shows the
    plan and its ticks, not a re-run of the thinking.
    """

    type: Literal["goal_state_get"] = "goal_state_get"
    goal_id: str


class GoalAccepted(_ContractModel):
    """Ties a submission to its goal_id, so an optimistic card can re-key."""

    type: Literal["goal_accepted"] = "goal_accepted"
    goal_id: str
    client_ref: str | None = None


ContractMessage = Annotated[
    Union[
        Hello,
        HelloAck,
        Capabilities,
        UserGoal,
        Dispatch,
        AgentEvent,
        PlanReady,
        PresentPlan,
        Understanding,
        UnderstandingResponse,
        Approval,
        Proposal,
        Status,
        Notice,
        ChatUiOpen,
        ChatUiClose,
        Control,
        DayAdvanced,
        BoardSnapshot,
        BoardUpdate,
        BoardGet,
        GoalStateGet,
        GoalAccepted,
    ],
    Field(discriminator="type"),
]
