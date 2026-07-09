"""Pydantic mirror of GoalFlow Contract v0.

CANONICAL SPEC: /CONTRACT.md — this module mirrors it field-for-field and must
never drift from it (the TypeScript mirror lives in
goal-flow-agent-chat-ui/src/types/contract.ts).

Design notes
------------
- Every message is a JSON object discriminated by the ``type`` field.
- Every task-related message carries ``goal_id``.
- Device<->cloud messages carry ``correlation_id`` (dedupe key; also correlates
  an approval back to its proposal).
- ``DispatchConstraints.hard`` is the ONLY thing the device safety gate reads.
- These models are a design artifact: fields and types are fully defined here
  even though no behavior exists yet (M1 uses them to serialize the hardcoded
  dispatch and validate device replies).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared vocabulary
# ---------------------------------------------------------------------------

Role = Literal["ui", "device"]

#: Task-status lifecycle:
#: created -> planning -> awaiting_approval -> executing -> adapting -> done
TaskStatus = Literal[
    "created",
    "planning",
    "awaiting_approval",
    "executing",
    "adapting",
    "done",
]


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


class Hello(BaseModel):
    """Client -> cloud, first frame on connect: registers the client's role."""

    type: Literal["hello"] = "hello"
    role: Role


class HelloAck(BaseModel):
    """Cloud -> client: acknowledges registration and assigns a session."""

    type: Literal["hello_ack"] = "hello_ack"
    role: Role
    session_id: str


# ---------------------------------------------------------------------------
# 1) user_goal (UI -> cloud)
# ---------------------------------------------------------------------------


class UserGoal(BaseModel):
    """Raw natural-language goal from the user."""

    type: Literal["user_goal"] = "user_goal"
    text: str


# ---------------------------------------------------------------------------
# 2) dispatch (cloud -> device) — the Task Contract
# ---------------------------------------------------------------------------


class DispatchScope(BaseModel):
    meal: str
    days: list[str]


class TimeWindow(BaseModel):
    start: str  # ISO date, e.g. "2026-07-13"
    end: str  # ISO date, e.g. "2026-07-17"


class HardConstraints(BaseModel):
    """The ONLY block the device safety gate reads. Injected verbatim from
    family memory — never produced by LLM semantics."""

    allergens: list[str]
    dietary: list[str]
    medical: list[str]


class SoftConstraints(BaseModel):
    """Preferences that bias planning only; never enforced by the safety gate."""

    dislikes: list[str]
    prefer: list[str]


class DispatchConstraints(BaseModel):
    hard: HardConstraints
    soft: SoftConstraints


class ContextHints(BaseModel):
    notes: str


class Dispatch(BaseModel):
    """The Task Contract sent to the device (M1: hardcoded; M2: LangGraph)."""

    type: Literal["dispatch"] = "dispatch"
    goal_id: str
    objective: str
    scope: DispatchScope
    time_window: TimeWindow
    constraints: DispatchConstraints
    optimization: list[str]
    autonomy: str  # e.g. "propose_all"
    context_hints: ContextHints
    reply_to: str  # e.g. "kb/device/meal-2026-w29"


# ---------------------------------------------------------------------------
# 3) plan_ready (device -> cloud)  /  4) present_plan (cloud -> UI)
# ---------------------------------------------------------------------------


class PlanItem(BaseModel):
    day: str
    dish: str
    why: list[str]


class PlanProposal(BaseModel):
    """A proposed action attached to a plan. Proposals are proposals, not
    actions: the device executes NOTHING until an approval returns."""

    proposal_id: str
    action: str  # e.g. "add_to_shopping_list"
    items: list[str]
    reason: str
    requires_approval: bool


class SafetyResult(BaseModel):
    """Outcome of the device-side deterministic safety gate."""

    gate: str  # e.g. "passed"
    hard_violations: list[str]


class PlanPayload(BaseModel):
    plan: list[PlanItem]
    proposals: list[PlanProposal]
    safety: SafetyResult
    impact: dict[str, Any] | None = None
    knew: dict[str, Any] | None = None


class PlanReady(BaseModel):
    """Device -> cloud: plan produced, awaiting user approval."""

    type: Literal["plan_ready"] = "plan_ready"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus
    payload: PlanPayload


class PresentPlan(BaseModel):
    """Cloud -> UI: the plan_ready payload relayed for rendering.

    Same payload shape; the cloud MAY add display hints.
    """

    type: Literal["present_plan"] = "present_plan"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus
    payload: PlanPayload
    display_hints: dict[str, str] | None = None  # optional cloud-added hints


# ---------------------------------------------------------------------------
# 5) proposal (device -> cloud) — adaptation
# ---------------------------------------------------------------------------


class AdaptationPayload(BaseModel):
    proposal_id: str
    action: str  # e.g. "add_prep_task"
    detail: str
    trigger: str  # what caused the adaptation, e.g. a calendar event
    requires_approval: bool


class Proposal(BaseModel):
    """Device -> cloud: a mid-task adaptation proposal (requires approval)."""

    type: Literal["proposal"] = "proposal"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus
    payload: AdaptationPayload


# ---------------------------------------------------------------------------
# 6) approval (cloud -> device)
# ---------------------------------------------------------------------------


class ApprovalDecision(BaseModel):
    proposal_id: str
    approved: bool


class ApprovalPayload(BaseModel):
    decisions: list[ApprovalDecision]


class Approval(BaseModel):
    """Cloud -> device: the user's decisions, correlated back to the proposal
    via ``correlation_id``. This is the APPROVAL gate (user, via cloud)."""

    type: Literal["approval"] = "approval"
    goal_id: str
    correlation_id: str
    payload: ApprovalPayload


# ---------------------------------------------------------------------------
# 7) status (device -> cloud)
# ---------------------------------------------------------------------------


class StatusPayload(BaseModel):
    note: str


class Status(BaseModel):
    """Device -> cloud: progress/lifecycle update."""

    type: Literal["status"] = "status"
    goal_id: str
    correlation_id: str
    task_status: TaskStatus
    payload: StatusPayload


# ---------------------------------------------------------------------------
# Discriminated union of every Contract v0 message
# ---------------------------------------------------------------------------

ContractMessage = Annotated[
    Union[
        Hello,
        HelloAck,
        UserGoal,
        Dispatch,
        PlanReady,
        PresentPlan,
        Proposal,
        Approval,
        Status,
    ],
    Field(discriminator="type"),
]
