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

from goalflow_cloud.config import get_settings
from goalflow_cloud.graph import nodes as graph_nodes
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
graph = graph_nodes.build_graph()


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

    """
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(
        "ts=%(asctime)s level=%(levelname)s logger=%(name)s "
        "correlation_id=%(correlation_id)s goal_id=%(goal_id)s msg=%(message)s"
    )
    if not root.handlers:
        handler = logging.StreamHandler()
        root.addHandler(handler)
    for handler in root.handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)
        if not any(isinstance(f, CorrelationIdFilter) for f in handler.filters):
            handler.addFilter(CorrelationIdFilter())


def log_frame(direction: str, role: str, frame: dict[str, Any]) -> None:
    """One structured INFO line per frame: direction, role, type, ids.

    """
    correlation_id_var.set(str(frame.get("correlation_id") or "-"))
    goal_id_var.set(str(frame.get("goal_id") or "-"))
    logger.info(
        "frame direction=%s role=%s type=%s",
        direction,
        role,
        frame.get("type", "-"),
    )
    logger.debug("frame_body direction=%s role=%s body=%s", direction, role, frame)


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

        """
        old = self._connections.get(role)
        if old is not None and old is not websocket:
            try:
                await old.close(code=1012, reason="replaced by a new connection")
            except RuntimeError:
                pass
        self._connections[role] = websocket
        session_id = str(uuid4())
        ack = HelloAck(role=role, session_id=session_id).model_dump(mode="json")
        log_frame("out", role, ack)
        await websocket.send_json(ack)
        return session_id

    async def unregister(self, role: Role, websocket: WebSocket | None = None) -> None:
        """Drop the socket for ``role`` if it is still the active one."""
        if websocket is None or self._connections.get(role) is websocket:
            self._connections.pop(role, None)

    async def send_to(self, role: Role, frame: dict[str, Any]) -> None:
        """Send ``frame`` as JSON to the registered role; structured-log it.

        """
        websocket = self._connections.get(role)
        log_frame("out", role, frame)
        if websocket is None:
            logger.warning("send_drop role=%s reason=not_connected type=%s", role, frame.get("type"))
            return
        await websocket.send_json(frame)


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

    """
    await websocket.accept()
    role: Role | None = None
    try:
        first = await websocket.receive_json()
        log_frame("in", "unknown", first)
        hello = Hello(**first)
        role = hello.role
        await registry.register(role, websocket)
        if role == "ui" and device_capabilities is not None:
            await registry.send_to("ui", device_capabilities.model_dump(mode="json"))

        while True:
            frame = await websocket.receive_json()
            log_frame("in", role, frame)
            await route_message(role, frame)
    except ValidationError:
        logger.exception("websocket_protocol_error role=%s", role or "unknown")
        await websocket.close(code=1003, reason="invalid contract frame")
    except WebSocketDisconnect:
        logger.info("websocket_disconnect role=%s", role or "unknown")
    finally:
        if role is not None:
            await registry.unregister(role, websocket)


async def route_message(sender_role: Role, frame: dict[str, Any]) -> None:
    """Route an inbound frame on (type, sender role) — CONTRACT v2 table.

    """
    frame_type = frame.get("type")
    if sender_role == "device":
        goal_id = frame.get("goal_id")
        correlation_id = frame.get("correlation_id")
        if goal_id and correlation_id:
            suffix = frame.get("seq") if frame_type == "agent_event" else ""
            dedupe_key = f"{frame_type}:{correlation_id}:{suffix}"
            seen = seen_correlation_ids.setdefault(goal_id, set())
            if dedupe_key in seen:
                logger.info("frame_dedupe_drop role=device type=%s", frame_type)
                return
            seen.add(dedupe_key)

    if sender_role == "ui" and frame_type == "user_goal":
        await handle_user_goal(UserGoal(**frame))
    elif sender_role == "ui" and frame_type == "approval":
        await handle_approval(Approval(**frame))
    elif sender_role == "ui" and frame_type == "control":
        await registry.send_to("device", Control(**frame).model_dump(mode="json"))
    elif sender_role == "device" and frame_type == "capabilities":
        await handle_capabilities(Capabilities(**frame))
    elif sender_role == "device" and frame_type == "agent_event":
        await relay_agent_event(AgentEvent(**frame))
    elif sender_role == "device" and frame_type == "plan_ready":
        await handle_plan_ready(PlanReady(**frame))
    elif sender_role == "device" and frame_type == "proposal":
        proposal = Proposal(**frame)
        await registry.send_to("ui", proposal.model_dump(mode="json"))
        await graph_resume_monitor(proposal.goal_id, proposal.model_dump(mode="json"))
    elif sender_role == "device" and frame_type == "status":
        status = Status(**frame)
        await registry.send_to("ui", status.model_dump(mode="json"))
        await graph_resume_monitor(status.goal_id, status.model_dump(mode="json"))
    else:
        logger.warning("unknown_route role=%s type=%s", sender_role, frame_type)


# --- ui -> cloud -> device -------------------------------------------------


async def handle_user_goal(user_goal: UserGoal) -> None:
    """Run the LangGraph pipeline and dispatch the generic Task Contract.

    """
    import asyncio

    goal_id = str(uuid4())
    goal_id_var.set(goal_id)
    logger.info("task_status status=created")
    state = await asyncio.to_thread(graph_nodes.start_goal, graph, user_goal.text, goal_id)
    if state.get("error"):
        await registry.send_to(
            "ui",
            {
                "type": "status",
                "goal_id": goal_id,
                "correlation_id": state.get("correlation_id") or "-",
                "task_status": "done",
                "payload": {"material": False, "executed": [], "note": state["error"]},
            },
        )
        return
    frame = state["contract"]
    dispatched_contracts[goal_id] = frame
    logger.info("task_status status=planning")
    await registry.send_to("device", frame)


async def handle_approval(approval: Approval) -> None:
    """Resume the graph's interrupt() with the decisions; forward to device.

    """
    import asyncio

    decisions = [decision.model_dump(mode="json") for decision in approval.payload.decisions]
    await asyncio.to_thread(graph_nodes.resume_goal, graph, approval.goal_id, decisions)
    await registry.send_to("device", approval.model_dump(mode="json"))


# --- device -> cloud -> ui ---------------------------------------------------


async def handle_capabilities(capabilities: Capabilities) -> None:
    """Cache the device module registry and relay it to the UI.

    """
    global device_capabilities
    device_capabilities = capabilities
    await registry.send_to("ui", capabilities.model_dump(mode="json"))


async def relay_agent_event(event: AgentEvent) -> None:
    """PASSTHROUGH relay of the device's live stream to the UI.

    """
    try:
        graph.update_state(
            {"configurable": {"thread_id": event.goal_id}},
            {
                "event_log": [
                    {
                        "event": "agent_event",
                        "goal_id": event.goal_id,
                        "correlation_id": event.correlation_id,
                        "payload": event.model_dump(mode="json"),
                    }
                ]
            },
        )
    except Exception:
        logger.exception("graph_event_log_append_failed")
    await registry.send_to("ui", event.model_dump(mode="json"))


async def handle_plan_ready(plan_ready: PlanReady) -> None:
    """Resume the graph with the plan; re-wrap as present_plan (+knew) for UI.

    """
    import asyncio

    state = await asyncio.to_thread(
        graph_nodes.resume_goal,
        graph,
        plan_ready.goal_id,
        plan_ready.model_dump(mode="json"),
    )
    payload = plan_ready.payload.model_dump(mode="json")
    payload["knew"] = build_knew(dispatched_contracts.get(plan_ready.goal_id))
    present = PresentPlan(
        goal_id=plan_ready.goal_id,
        correlation_id=plan_ready.correlation_id,
        task_status=plan_ready.task_status,
        payload=payload,
    )
    await registry.send_to("ui", present.model_dump(mode="json"))

    approval_frame = state.get("approval_frame")
    if approval_frame:
        await registry.send_to("device", approval_frame)


def build_knew(contract: dict[str, Any] | None) -> dict[str, Any]:
    """The UI-facing "what it knew" summary from a dispatched contract.

    GENERIC: surfaces constraints.hard (safety policy), constraints.soft
    (preferences), and context — no domain-specific field names.

    """
    if not contract:
        return {}
    hard = (contract.get("constraints") or {}).get("hard") or {}
    soft = (contract.get("constraints") or {}).get("soft") or {}
    context = contract.get("context") or {}

    knew: dict[str, Any] = {}

    def add(label: str, value: Any) -> None:
        # Only surface flat, display-ready values (str / list[str]); never raw
        # nested objects — the UI renders these as chips.
        if isinstance(value, list):
            items = [str(v) for v in value if str(v).strip()]
            if items:
                knew[label] = items
        elif isinstance(value, str) and value.strip():
            knew[label] = value
        elif isinstance(value, (int, float)) and value:
            knew[label] = str(value)

    add("allergens", hard.get("allergens"))
    add("dietary", hard.get("dietary"))
    add("medical", hard.get("medical"))
    if hard.get("budget_cap"):
        knew["budget"] = f"${hard.get('budget_cap')}"
    if hard.get("quiet_hours"):
        knew["quiet hours"] = str(hard.get("quiet_hours"))
    add("dislikes", soft.get("dislikes"))
    add("prefer", soft.get("prefer"))
    add("notes", context.get("notes"))
    return knew


async def graph_resume_monitor(goal_id: str, frame: dict[str, Any]) -> None:
    """Best-effort graph monitor resume for device status/proposal frames."""
    import asyncio

    try:
        state = await asyncio.to_thread(graph_nodes.resume_goal, graph, goal_id, frame)
    except Exception:
        logger.exception("graph_monitor_resume_failed")
        return

    approval_frame = state.get("approval_frame")
    if approval_frame and frame.get("type") == "proposal":
        logger.debug("adapt_approval_frame_ready goal_id=%s", goal_id)


setup_logging()
