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

    Injected VERBATIM from family memory (memory/store.py "hard" block);
    never generated or paraphrased by the LLM. Extra keys are allowed so new
    policy dimensions can flow through without a contract bump.
    """

    allergens: list[str] = Field(default_factory=list)
    medical: list[str] = Field(default_factory=list)
    dietary: list[str] = Field(default_factory=list)
    #: Currency-agnostic spend ceiling (None = no cap).
    budget_cap: float | None = None
    #: e.g. {"start": "21:30", "end": "07:00"} — no noisy appliances inside.
    quiet_hours: dict[str, str] | None = None


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
      thinking:      {"text": "..."}
      tool_call:     {"module": "...", "function": "...", "args": {...}}
      tool_result:   {"module": "...", "function": "...", "summary": "..."}
      plan_progress: {"item": {...}}
      task_update:   {"task_id", "title", "state", "depends_on", "progress_pct",
                      "pending_tasks", "next_step", "retry_count", "failure_reason"}

    ``task_update`` (v3) is how the cloud learns a goal's shape and progress: the task
    DAG lives on the DEVICE (only it can ground a decomposition), so Agent Board's
    numbers are folded from these rather than guessed from the clock.
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


class PlanPayload(_ContractModel):
    """The plan_ready payload: plan + tiered proposals + safety + impact."""

    plan: list[PlanItem] = Field(default_factory=list)
    proposals: list[PlanProposal] = Field(default_factory=list)
    safety: SafetyResult
    impact: list[ImpactItem] = Field(default_factory=list)
    explanation: str = ""
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
    kind: Literal["out_of_scope", "declined"] = "out_of_scope"
    message: str


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
    command: Literal["advance_day", "reset", "set_date", "trigger_event"]
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


class Suggestion(_ContractModel):
    """One proactive suggestion — a goal the device thinks is worth doing, unprompted.

    DERIVED FROM LOCAL STATE by the device (expiring food, low stock), because only the
    device sees the fridge. It is NOT a goal yet: it becomes one only if a person taps
    accept, at which point ``goal_text`` is submitted as an ordinary ``user_goal`` and
    runs the normal understand→plan→approve flow. So a suggestion can never act on its
    own — the same "a person decides" line the whole system holds.
    """

    id: str
    #: "expiring" | "restock" — the scan that produced it (drives the card's glyph).
    kind: str
    title: str
    subtitle: str = ""
    detail: str = ""
    #: The goal text submitted verbatim as a user_goal if this is accepted.
    goal_text: str


class Suggestions(_ContractModel):
    """The device's current suggestion list.

    Goal-LESS: this is the one frame the device sends that isn't about a goal already
    in flight — a proactive scan, not a reaction. The cloud relays the list to the
    boards (on change and on bind); the chat UI never sees it.
    """

    type: Literal["suggestions"] = "suggestions"
    items: list[Suggestion] = Field(default_factory=list)


class SuggestionAction(_ContractModel):
    """A board acted on a suggestion: accept it (→ a real goal) or dismiss it."""

    type: Literal["suggestion_action"] = "suggestion_action"
    suggestion_id: str
    #: "accept" | "dismiss".
    action: str
    #: UI-minted id echoed back in goal_accepted when an accept mints a goal.
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
        Control,
        DayAdvanced,
        BoardSnapshot,
        BoardUpdate,
        BoardGet,
        GoalStateGet,
        GoalAccepted,
        Suggestions,
        SuggestionAction,
    ],
    Field(discriminator="type"),
]
