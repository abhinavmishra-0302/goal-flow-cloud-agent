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
AgentEventKind = Literal[
    "phase",
    "thinking",
    "tool_call",
    "tool_result",
    "plan_progress",
]


class _ContractModel(BaseModel):
    """Base for every contract model: lenient on unknown fields."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class Hello(_ContractModel):
    """Client -> cloud, first frame on connect: registers the client's role."""

    type: Literal["hello"] = "hello"
    role: Role


class HelloAck(_ContractModel):
    """Cloud -> client: acknowledges registration and assigns a session."""

    type: Literal["hello_ack"] = "hello_ack"
    role: Role
    session_id: str


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
    outside what GoalFlow can act on (only meal planning + guest dinners are)."""

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
    commands — never a hardcoded date."""

    type: Literal["control"] = "control"
    goal_id: str
    command: Literal["advance_day", "reset", "set_date", "trigger_event"]
    payload: ControlPayload = Field(default_factory=ControlPayload)


# ---------------------------------------------------------------------------
# Discriminated union of every CONTRACT v2 message
# ---------------------------------------------------------------------------

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
    ],
    Field(discriminator="type"),
]
