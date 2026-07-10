"""GoalFlow v2 FastAPI WebSocket hub — DESIGN SKELETON (signatures + TODO).

The cloud is the only path between UI and device (they NEVER talk directly).
Routing (CONTRACT v2, see /CONTRACT.md):

    ui -> cloud:      user_goal (run graph -> dispatch), approval (resume
                      interrupt + forward), control (forward)
    device -> cloud:  capabilities (cache + relay to ui),
                      agent_event (PASSTHROUGH relay to ui),
                      plan_ready (resume graph; re-wrap as present_plan +knew),
                      proposal / status (relay to ui; feed monitor)

Structured logging is a first-class requirement: leveled, correlation-id
tagged (contextvars-backed filter), one line per inbound/outbound frame.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from goalflow_cloud.models.contract import (
    AgentEvent,
    Approval,
    Capabilities,
    Control,
    Hello,
    HelloAck,
    PlanReady,
    PresentPlan,
    Proposal,
    Role,
    Status,
    UserGoal,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="goalflow-cloud-agent", version="2.0.0-design")


# ---------------------------------------------------------------------------
# Structured logging (correlation-id tagged)
# ---------------------------------------------------------------------------

#: Current frame's correlation id — every log record inside a frame's handling
#: is tagged with it via CorrelationIdFilter.
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")
goal_id_var: ContextVar[str] = ContextVar("goal_id", default="-")


class CorrelationIdFilter(logging.Filter):
    """Stamp every record with correlation_id/goal_id from contextvars."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        record.goal_id = goal_id_var.get()
        return True


def setup_logging() -> None:
    """Configure leveled, structured, correlation-id-tagged logging.

    TODO(v2-M1):
    - level from config.get_settings().log_level;
    - formatter emitting structured key=value records including
      %(correlation_id)s and %(goal_id)s;
    - attach CorrelationIdFilter to the root handler.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def log_frame(direction: str, role: str, frame: dict[str, Any]) -> None:
    """One structured INFO line per frame: direction, role, type, ids.

    TODO(v2-M1): set correlation_id_var/goal_id_var from the frame, log
    type/ids at INFO and the payload body at DEBUG.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


# ---------------------------------------------------------------------------
# Connection registry
# ---------------------------------------------------------------------------


class ConnectionRegistry:
    """Active WebSocket connections, keyed by role ("ui" | "device").

    One active socket per role; a reconnect replaces the previous socket.
    """

    def __init__(self) -> None:
        self._connections: dict[Role, WebSocket] = {}

    async def register(self, role: Role, websocket: WebSocket) -> str:
        """Store the socket under ``role``; reply hello_ack; return session_id.

        TODO(v2-M1): close a replaced socket (code 1012); send
        HelloAck(role=role, session_id=uuid4()).
        """
        raise NotImplementedError("v2 design stub — implemented in the build pass")

    async def unregister(self, role: Role, websocket: WebSocket | None = None) -> None:
        """Drop the socket for ``role`` if it is still the active one."""
        raise NotImplementedError("v2 design stub — implemented in the build pass")

    async def send_to(self, role: Role, frame: dict[str, Any]) -> None:
        """Send ``frame`` as JSON to the registered role; structured-log it.

        TODO(v2-M1): log_frame("out", role, frame); warn (don't raise) when
        the role is not connected.
        """
        raise NotImplementedError("v2 design stub — implemented in the build pass")


registry = ConnectionRegistry()

#: Device-advertised module registry, cached for late-joining UIs.
device_capabilities: Capabilities | None = None

#: Dispatched Task Contracts by goal_id (source of present_plan's "knew").
dispatched_contracts: dict[str, dict[str, Any]] = {}

#: Seen device correlation_ids per goal (dedupe on reconnect/replay).
seen_correlation_ids: dict[str, set[str]] = {}


# ---------------------------------------------------------------------------
# WS endpoint + routing
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Single WS endpoint for both roles.

    TODO(v2-M1): accept; first frame MUST be Hello (else close 1002);
    registry.register(role); loop receive_json -> route_message(role, frame);
    ValidationError -> close 1003; WebSocketDisconnect -> unregister.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


async def route_message(sender_role: Role, frame: dict[str, Any]) -> None:
    """Route an inbound frame on (type, sender role) — CONTRACT v2 table.

    TODO(v2-M1): validate with the matching model, then:
      ui:     user_goal -> handle_user_goal; approval -> handle_approval;
              control  -> forward to device (Control(**frame)).
      device: capabilities -> handle_capabilities;
              agent_event  -> relay_agent_event (passthrough);
              plan_ready   -> handle_plan_ready;
              proposal     -> relay to ui (Proposal(**frame));
              status       -> relay to ui (Status(**frame)).
    Dedupe device frames on correlation_id (seen_correlation_ids).
    Unknown (type, role) -> structured WARNING, never a crash.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


# --- ui -> cloud -> device -------------------------------------------------


async def handle_user_goal(user_goal: UserGoal) -> None:
    """Run the LangGraph pipeline and dispatch the generic Task Contract.

    TODO(v2-M1): mint goal_id; graph_nodes.start_goal(...) off the event loop
    (to_thread); remember the frame in dispatched_contracts (for "knew");
    registry.send_to("device", frame). LLM failure -> structured error to UI,
    never a scripted fallback.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


async def handle_approval(approval: Approval) -> None:
    """Resume the graph's interrupt() with the decisions; forward to device.

    TODO(v2-M1): graph_nodes.resume_goal(goal_id, decisions);
    registry.send_to("device", approval frame).
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


# --- device -> cloud -> ui ---------------------------------------------------


async def handle_capabilities(capabilities: Capabilities) -> None:
    """Cache the device module registry and relay it to the UI.

    TODO(v2-M1): store in device_capabilities; send to ui (late-joining UIs
    get the cached copy on register).
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


async def relay_agent_event(event: AgentEvent) -> None:
    """PASSTHROUGH relay of the device's live stream to the UI.

    TODO(v2-M1): validate envelope only; forward the frame unchanged
    (ordering/dedupe via seq); append to the goal's event log. Never block
    the stream on graph work.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


async def handle_plan_ready(plan_ready: PlanReady) -> None:
    """Resume the graph with the plan; re-wrap as present_plan (+knew) for UI.

    TODO(v2-M1): graph_nodes.resume_goal(goal_id, payload); build
    payload.knew from dispatched_contracts[goal_id] (constraints + context —
    the personalization "what it knew"); send PresentPlan to ui.
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")


def build_knew(contract: dict[str, Any] | None) -> dict[str, Any]:
    """The UI-facing "what it knew" summary from a dispatched contract.

    GENERIC: surfaces constraints.hard (safety policy), constraints.soft
    (preferences), and context — no domain-specific field names.

    TODO(v2-M1): implement the extraction (empty dict when contract is None).
    """
    raise NotImplementedError("v2 design stub — implemented in the build pass")
