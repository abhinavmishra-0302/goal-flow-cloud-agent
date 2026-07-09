"""FastAPI app + WebSocket hub for GoalFlow."""

from __future__ import annotations

import logging
from asyncio import to_thread
from copy import deepcopy
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from goalflow_cloud.graph.nodes import build_dispatch_frame
from goalflow_cloud.models.contract import (
    Approval,
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

app = FastAPI(title="goalflow-cloud-agent")


def _dump_model(model: object) -> dict:
    """Return a Pydantic model as a JSON-ready dict across v1/v2."""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)  # type: ignore[attr-defined]
    return model.dict(exclude_none=True)  # type: ignore[attr-defined]


class ConnectionRegistry:
    """Active WebSocket connections, keyed by role ("ui" | "device")."""

    def __init__(self) -> None:
        self._connections: dict[Role, WebSocket] = {}

    async def register(self, role: Role, websocket: WebSocket) -> str:
        """Store the socket under ``role`` and return a new session_id."""
        previous = self._connections.get(role)
        if previous is not None and previous is not websocket:
            await previous.close(code=1012, reason="replaced by newer connection")

        self._connections[role] = websocket
        session_id = str(uuid4())
        await self.send_to(role, _dump_model(HelloAck(role=role, session_id=session_id)))
        return session_id

    async def unregister(self, role: Role, websocket: WebSocket | None = None) -> None:
        """Drop the socket for ``role`` if it is still the active socket."""
        if websocket is None or self._connections.get(role) is websocket:
            self._connections.pop(role, None)

    async def send_to(self, role: Role, message: dict) -> None:
        """Serialize ``message`` as JSON and send it to the registered socket."""
        target = self._connections.get(role)
        message_type = message.get("type", "<unknown>")
        logger.info("outbound frame role=%s type=%s", role, message_type)
        if target is None:
            logger.warning("cannot send frame; role=%s is not connected type=%s", role, message_type)
            return
        await target.send_json(message)


registry = ConnectionRegistry()
dispatched_contracts: dict[str, dict[str, Any]] = {}


def _remember_dispatched_contract(frame: dict[str, Any]) -> None:
    """Cache the cloud-generated dispatch by goal_id for later UI context."""
    goal_id = frame.get("goal_id")
    if isinstance(goal_id, str) and goal_id:
        dispatched_contracts[goal_id] = deepcopy(frame)


def _knew_from_contract(contract: dict[str, Any] | None) -> dict[str, Any]:
    """Build the UI-facing personalization summary from a dispatch contract."""
    constraints = contract.get("constraints", {}) if contract else {}
    hard = constraints.get("hard", {}) if isinstance(constraints, dict) else {}
    soft = constraints.get("soft", {}) if isinstance(constraints, dict) else {}
    context_hints = contract.get("context_hints", {}) if contract else {}
    return {
        "dietary": list(hard.get("dietary", [])) if isinstance(hard, dict) else [],
        "dislikes": list(soft.get("dislikes", [])) if isinstance(soft, dict) else [],
        "prefer": list(soft.get("prefer", [])) if isinstance(soft, dict) else [],
        "notes": str(context_hints.get("notes", "")) if isinstance(context_hints, dict) else "",
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    """Single WS endpoint for both roles."""
    await websocket.accept()
    role: Role | None = None

    try:
        first_frame = await websocket.receive_json()
        hello = Hello(**first_frame)
        role = hello.role
        logger.info("inbound frame role=%s type=%s", role, hello.type)
        await registry.register(role, websocket)

        while True:
            frame = await websocket.receive_json()
            logger.info("inbound frame role=%s type=%s", role, frame.get("type", "<unknown>"))
            try:
                await route_message(role, frame)
            except ValidationError as exc:
                logger.warning("invalid contract frame role=%s: %s", role, exc)
                await websocket.close(code=1003, reason="invalid contract frame")
                break
    except ValidationError as exc:
        logger.warning("invalid handshake frame: %s", exc)
        await websocket.close(code=1002, reason="first frame must be hello")
    except WebSocketDisconnect:
        if role is not None:
            await registry.unregister(role, websocket)


async def route_message(sender_role: Role, message: dict) -> None:
    """Route an incoming frame on (type, sender role)."""
    message_type = message.get("type")

    if sender_role == "ui" and message_type == "user_goal":
        user_goal = UserGoal(**message)
        await handle_user_goal(user_goal.text)
        return

    if sender_role == "ui" and message_type == "control":
        Control(**message)
        await registry.send_to("device", message)
        return

    if sender_role == "device" and message_type == "plan_ready":
        plan_ready = PlanReady(**message)
        payload = _dump_model(plan_ready.payload)
        payload["knew"] = _knew_from_contract(dispatched_contracts.get(plan_ready.goal_id))
        present_plan = PresentPlan(
            goal_id=plan_ready.goal_id,
            correlation_id=plan_ready.correlation_id,
            task_status=plan_ready.task_status,
            payload=payload,
            display_hints={"surface": "meal_plan"},
        )
        await registry.send_to("ui", _dump_model(present_plan))
        return

    if sender_role == "device" and message_type == "proposal":
        Proposal(**message)
        await registry.send_to("ui", message)
        return

    if sender_role == "device" and message_type == "status":
        Status(**message)
        await registry.send_to("ui", message)
        return

    if sender_role == "ui" and message_type == "approval":
        Approval(**message)
        await registry.send_to("device", message)
        return

    logger.warning("unhandled frame role=%s type=%s", sender_role, message_type)


async def handle_user_goal(text: str) -> None:
    """Turn a user goal into a graph-produced ``dispatch`` and send it."""
    frame = await to_thread(build_dispatch_frame, text)
    _remember_dispatched_contract(frame)
    await registry.send_to("device", frame)
