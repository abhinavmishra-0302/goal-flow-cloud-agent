"""GoalFlow v2 FastAPI WebSocket hub — DESIGN SKELETON (signatures + TODO).

The cloud is the only path between UI and device (they NEVER talk directly).
Routing (CONTRACT v2, see /CONTRACT.md):

    ui -> cloud:      user_goal (run graph -> understanding interrupt),
                      understanding_response (resume -> dispatch or cancel),
                      approval (resume interrupt + forward), control (forward)
    device -> cloud:  capabilities (cache + relay to ui),
                      agent_event (PASSTHROUGH relay to ui),
                      plan_ready (resume graph; re-wrap as present_plan +knew),
                      proposal / status (relay to ui; feed monitor)

Structured logging is a first-class requirement: leveled, correlation-id
tagged (contextvars-backed filter), one line per inbound/outbound frame.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from goalflow_cloud.board import BoardService
from goalflow_cloud.config import get_settings
from goalflow_cloud.graph import nodes as graph_nodes
from goalflow_cloud.models.contract import (
    AgentEvent,
    Approval,
    BoardGet,
    BoardSnapshot,
    BoardUpdate,
    GoalAccepted,
    GoalStateGet,
    Capabilities,
    Control,
    DayAdvanced,
    Devices,
    Hello,
    HelloAck,
    Notice,
    PlanReady,
    PresentPlan,
    Proposal,
    Role,
    SelectDevice,
    Status,
    Suggestions,
    SuggestionAction,
    Understanding,
    UnderstandingPayload,
    UnderstandingResponse,
    UserGoal,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="goalflow-cloud-agent", version="3.0.0")
graph = graph_nodes.build_graph()

#: Agent Board's derived state (v3-M6). The hub already routes every frame a goal
#: produces and is the only place that sees ALL of a session's goals, so it folds
#: them here rather than asking the device for a view it would have to invent.
board = BoardService()


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


@dataclass
class Session:
    """One home: a single device agent + any number of watching UIs.

    Keyed by ``device_id`` (the pairing key). Frames route only to the paired
    peer(s) of a session — never globally across sessions.
    """

    device_id: str
    device_name: str = ""
    device: WebSocket | None = None
    uis: list[WebSocket] = field(default_factory=list)
    capabilities: Capabilities | None = None


class ConnectionRegistry:
    """Active connections, partitioned into per-``device_id`` SESSIONS.

    Each session holds at most one device socket + any number of ui sockets.
    A device reconnect replaces (1012-closes) only the PRIOR socket of the SAME
    ``device_id`` — other homes are untouched. UI sockets are never evicted (the
    old one-socket-per-role eviction caused a mutual-eviction storm between any
    two ui clients). A UI that connects without a device_id is held UNBOUND —
    it gets the device list for discovery but no goal frames until it selects one.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._unbound_uis: list[WebSocket] = []
        #: reverse lookup for routing + teardown: ws -> (role, device_id | "").
        self._meta: dict[WebSocket, tuple[Role, str]] = {}

    # --- lookup ---
    def session_of(self, websocket: WebSocket) -> str:
        """The device_id a socket is bound to ("" if an unbound ui)."""
        meta = self._meta.get(websocket)
        return meta[1] if meta else ""

    def _session(self, device_id: str) -> Session:
        return self._sessions.setdefault(device_id, Session(device_id=device_id))

    def device_list(self) -> list[dict[str, Any]]:
        return [
            {"device_id": s.device_id, "device_name": s.device_name or s.device_id, "online": True}
            for s in self._sessions.values()
            if s.device is not None
        ]

    # --- registration ---
    async def register_device(self, websocket: WebSocket, device_id: str, device_name: str) -> None:
        session = self._session(device_id)
        session.device_name = device_name or session.device_name or device_id
        old = session.device
        if old is not None and old is not websocket:
            try:
                await old.close(code=1012, reason="replaced by a new connection")
            except Exception:
                logger.debug("old_device_close_failed", exc_info=True)
        session.device = websocket
        self._meta[websocket] = ("device", device_id)
        await self._ack(websocket, "device", device_id)
        await self.broadcast_devices()

    async def register_ui(self, websocket: WebSocket, device_id: str) -> None:
        if device_id:
            await self._bind_ui(websocket, device_id, ack=True)
            return
        # No device_id: auto-bind when there's exactly ONE device (the common
        # single-home / zero-config case works on any UI, no picker needed).
        online = [s for s in self._sessions.values() if s.device is not None]
        if len(online) == 1:
            await self._bind_ui(websocket, online[0].device_id, ack=True)
            return
        # 0 or 2+ devices: hold unbound and offer the list for discovery/picker.
        if websocket not in self._unbound_uis:
            self._unbound_uis.append(websocket)
        self._meta[websocket] = ("ui", "")
        await self._ack(websocket, "ui", "")
        await self.send_devices(websocket)

    async def bind_ui(self, websocket: WebSocket, device_id: str) -> None:
        """Move an unbound ui into a session (from a select_device frame). Acks so
        the UI learns which device it landed on (hello_ack.device_id)."""
        if websocket in self._unbound_uis:
            self._unbound_uis.remove(websocket)
        await self._bind_ui(websocket, device_id, ack=True)

    async def _bind_ui(self, websocket: WebSocket, device_id: str, ack: bool) -> None:
        # Re-binding must MOVE the socket, not add it to a second session: leaving it in
        # the old session's ui list would deliver BOTH homes' frames to it — the exact
        # cross-session leak this registry exists to prevent.
        self._leave_session(websocket, keep=device_id)

        session = self._session(device_id)
        if websocket not in session.uis:
            session.uis.append(websocket)
        self._meta[websocket] = ("ui", device_id)
        if ack:
            await self._ack(websocket, "ui", device_id)
        # Every ui gets the current list, bound or not: it needs the paired device's
        # NAME to display, and the list to offer a "change device" affordance.
        await self.send_devices(websocket)
        if session.capabilities is not None:
            caps = session.capabilities.model_dump(mode="json")
            log_frame("out", "ui", caps)
            try:
                await websocket.send_json(caps)
            except Exception:
                logger.debug("caps_replay_failed", exc_info=True)
        # First paint for a board: the session's goals, unprompted. A board that had
        # to ASK would render empty for a beat on every reload.
        await self._replay_board(websocket, device_id)

    def _leave_session(self, websocket: WebSocket, keep: str = "") -> None:
        """Remove a ui socket from the session it currently belongs to (if any other
        than ``keep``), GC'ing the session if that empties it."""
        previous = self._meta.get(websocket)
        if previous is None:
            return
        _, previous_id = previous
        if not previous_id or previous_id == keep:
            return
        session = self._sessions.get(previous_id)
        if session is None:
            return
        if websocket in session.uis:
            session.uis.remove(websocket)
        if session.device is None and not session.uis:
            self._sessions.pop(previous_id, None)

    async def _ack(self, websocket: WebSocket, role: Role, device_id: str) -> None:
        ack = HelloAck(role=role, session_id=str(uuid4()), device_id=device_id).model_dump(mode="json")
        log_frame("out", role, ack)
        await websocket.send_json(ack)

    # --- teardown ---
    async def unregister(self, websocket: WebSocket) -> None:
        meta = self._meta.pop(websocket, None)
        if websocket in self._unbound_uis:
            self._unbound_uis.remove(websocket)
        if meta is None:
            return
        role, device_id = meta
        session = self._sessions.get(device_id)
        if session is None:
            return
        if role == "device" and session.device is websocket:
            session.device = None
            session.capabilities = None
            await self.broadcast_devices()
            # Nothing will answer for this session's unfinished goals now, so say so
            # rather than leaving cards that look alive.
            for summary in board.on_device_offline(device_id):
                await push_board(device_id, summary)
        elif role == "ui" and websocket in session.uis:
            session.uis.remove(websocket)
        if session.device is None and not session.uis:
            self._sessions.pop(device_id, None)

    # --- sending (per-session) ---
    async def send_to_device(self, device_id: str, frame: dict[str, Any]) -> bool:
        """Send to this session's device. Returns False if it wasn't delivered
        (no device connected / dead socket) so callers can tell the UI instead of
        leaving it hanging."""
        log_frame("out", "device", frame)
        session = self._sessions.get(device_id)
        websocket = session.device if session else None
        if websocket is None:
            logger.warning("send_drop role=device device_id=%s reason=not_connected type=%s", device_id, frame.get("type"))
            return False
        try:
            await websocket.send_json(frame)
            return True
        except Exception:
            logger.warning("send_failed role=device device_id=%s type=%s", device_id, frame.get("type"), exc_info=True)
            await self.unregister(websocket)
            return False

    async def send_to_uis(self, device_id: str, frame: dict[str, Any]) -> None:
        log_frame("out", "ui", frame)
        session = self._sessions.get(device_id)
        targets = list(session.uis) if session else []
        if not targets:
            logger.warning("send_drop role=ui device_id=%s reason=no_ui type=%s", device_id, frame.get("type"))
            return
        for websocket in targets:
            try:
                await websocket.send_json(frame)
            except Exception:
                logger.warning("send_failed role=ui device_id=%s type=%s", device_id, frame.get("type"), exc_info=True)
                await self.unregister(websocket)

    async def _replay_board(self, websocket: WebSocket, device_id: str) -> None:
        """Send this session's board snapshot + suggestions to one freshly-bound socket."""
        seq, goals = board.snapshot(device_id)
        try:
            if goals:
                frame = BoardSnapshot(board_seq=seq, goals=goals).model_dump(mode="json")
                log_frame("out", "ui", frame)
                await websocket.send_json(frame)
            # Suggestions can exist with zero goals (a fresh session whose device has
            # already scanned), so this is NOT gated on `goals` — a board that binds to
            # an idle home should still see "Expiring Soon".
            items = board.suggestions(device_id)
            if items:
                frame = Suggestions(items=items).model_dump(mode="json")
                log_frame("out", "ui", frame)
                await websocket.send_json(frame)
        except Exception:
            logger.debug("board_replay_failed", exc_info=True)

    def set_capabilities(self, device_id: str, capabilities: Capabilities) -> None:
        self._session(device_id).capabilities = capabilities

    def capabilities_of(self, device_id: str) -> dict[str, Any] | None:
        """What this session's device says it can do, as plain JSON.

        Feeds the interpreter's actionability gate (v3-M4). Returns None when no
        device has advertised yet — the gate then declines honestly rather than
        guessing, which is how it ended up hardcoded to two domains before.
        """
        capabilities = self._session(device_id).capabilities
        return capabilities.model_dump(mode="json") if capabilities else None

    # --- discovery ---
    async def send_devices(self, websocket: WebSocket) -> None:
        frame = Devices(devices=self.device_list()).model_dump(mode="json")
        log_frame("out", "ui", frame)
        try:
            await websocket.send_json(frame)
        except Exception:
            logger.debug("send_devices_failed", exc_info=True)

    async def broadcast_devices(self) -> None:
        """Refresh the device list on EVERY ui.

        Unbound uis need it to pick. BOUND uis need it too: a ui that was AUTO-bound
        (it sent no device_id and there was exactly one device at the time) made a GUESS
        — if a second device now appears, that guess is ambiguous and the ui must be able
        to re-ask. Without this, whether you get a picker depended on whether your tab
        happened to connect before or after the other agent started.
        """
        seen: set[WebSocket] = set()
        for websocket in list(self._unbound_uis):
            await self.send_devices(websocket)
            seen.add(websocket)
        for session in list(self._sessions.values()):
            for websocket in list(session.uis):
                if websocket not in seen:
                    await self.send_devices(websocket)
                    seen.add(websocket)


registry = ConnectionRegistry()

#: Dispatched Task Contracts by goal_id (source of present_plan's "knew").
dispatched_contracts: dict[str, dict[str, Any]] = {}

#: Seen device correlation_ids per goal (dedupe on reconnect/replay).
seen_correlation_ids: dict[str, set[str]] = {}

#: Goals whose understanding gate has already been resumed (multi-tab dedupe).
resolved_understandings: set[str] = set()


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
        if role == "device":
            # Absent device_id => "default" (single-pair, zero-config back-compat).
            await registry.register_device(websocket, hello.device_id or "default", hello.device_name)
        else:
            # A ui may arrive bound (?device=<id>) or unbound (await discovery).
            await registry.register_ui(websocket, hello.device_id)

        while True:
            frame = await websocket.receive_json()
            log_frame("in", role, frame)
            # A ui binds/rebinds to a device out-of-band (from the picker).
            if role == "ui" and frame.get("type") == "select_device":
                try:
                    await registry.bind_ui(websocket, SelectDevice(**frame).device_id)
                except ValidationError:
                    logger.exception("select_device_invalid")
                continue
            device_id = registry.session_of(websocket)
            if role == "ui" and not device_id:
                logger.warning("ui_frame_before_bind type=%s", frame.get("type"))
                continue
            try:
                await route_message(role, device_id, frame)
            except ValidationError:
                # A single malformed/mismatched frame must NOT drop the whole
                # connection — that closes the peer's socket and crashes the
                # device. Log the offending frame and keep the session alive.
                logger.exception(
                    "frame_validation_error role=%s type=%s", role, frame.get("type")
                )
    except ValidationError:
        # Only the initial hello handshake reaches here now.
        logger.exception("websocket_protocol_error role=%s", role or "unknown")
        try:
            await websocket.close(code=1003, reason="invalid contract frame")
        except RuntimeError:
            # Client already disconnected — closing again raises in Starlette.
            pass
    except WebSocketDisconnect:
        logger.info("websocket_disconnect role=%s", role or "unknown")
    finally:
        await registry.unregister(websocket)


async def route_message(sender_role: Role, device_id: str, frame: dict[str, Any]) -> None:
    """Route an inbound frame on (type, sender role) within its SESSION.

    ``device_id`` is the sender socket's session (the device it sent from, or a
    ui's bound device) — every relay targets the paired peer(s) of that session.
    """
    frame_type = frame.get("type")
    if sender_role == "device":
        goal_id = frame.get("goal_id")
        correlation_id = frame.get("correlation_id")
        # Dedupe ONLY plan_ready — a device reconnect replay would otherwise
        # double-resume the graph. status/proposal/agent_event legitimately RECUR
        # within a goal (every approval execution, every sustain tick, streaming)
        # and MUST all reach the UI; the device also resets agent_event seq per
        # execution, so a (type,correlation,seq) key wrongly collided and dropped
        # the 2nd approval's confirmation. The UI reducer dedupes agent_events by seq.
        if frame_type == "plan_ready" and goal_id and correlation_id:
            dedupe_key = f"{frame_type}:{correlation_id}"
            seen = seen_correlation_ids.setdefault(goal_id, set())
            if dedupe_key in seen:
                logger.info("frame_dedupe_drop role=device type=%s", frame_type)
                return
            seen.add(dedupe_key)

    if sender_role == "ui" and frame_type == "user_goal":
        await handle_user_goal(device_id, UserGoal(**frame))
    elif sender_role == "ui" and frame_type == "board_get":
        await send_board_snapshot(device_id)
    elif sender_role == "ui" and frame_type == "goal_state_get":
        await handle_goal_state_get(device_id, GoalStateGet(**frame))
    elif sender_role == "ui" and frame_type == "understanding_response":
        await handle_understanding_response(device_id, UnderstandingResponse(**frame))
    elif sender_role == "ui" and frame_type == "approval":
        await handle_approval(device_id, Approval(**frame))
    elif sender_role == "ui" and frame_type == "control":
        await registry.send_to_device(device_id, Control(**frame).model_dump(mode="json"))
    elif sender_role == "ui" and frame_type == "suggestion_action":
        await handle_suggestion_action(device_id, SuggestionAction(**frame))
    elif sender_role == "device" and frame_type == "suggestions":
        suggestions = Suggestions(**frame)
        board.on_suggestions(device_id, [s.model_dump(mode="json") for s in suggestions.items])
        await send_suggestions(device_id)
    elif sender_role == "device" and frame_type == "capabilities":
        await handle_capabilities(device_id, Capabilities(**frame))
    elif sender_role == "device" and frame_type == "agent_event":
        await relay_agent_event(device_id, AgentEvent(**frame))
    elif sender_role == "device" and frame_type == "plan_ready":
        await handle_plan_ready(device_id, PlanReady(**frame))
    elif sender_role == "device" and frame_type == "proposal":
        proposal = Proposal(**frame)
        await registry.send_to_uis(device_id, proposal.model_dump(mode="json"))
        await push_board(device_id, board.on_proposal(device_id, proposal.goal_id, proposal.model_dump(mode="json")))
        await graph_resume_monitor(proposal.goal_id, proposal.model_dump(mode="json"))
    elif sender_role == "device" and frame_type == "status":
        status = Status(**frame)
        await registry.send_to_uis(device_id, status.model_dump(mode="json"))
        await push_board(device_id, board.on_status(device_id, status.goal_id, status.model_dump(mode="json")))
        await graph_resume_monitor(status.goal_id, status.model_dump(mode="json"))
    elif sender_role == "device" and frame_type == "day_advanced":
        # v3.2 world tick summary — a board surface. The per-goal status/proposal frames
        # that ride alongside it already updated the cards; this is just the "what
        # happened today" list, relayed straight through to the boards.
        await registry.send_to_uis(device_id, DayAdvanced(**frame).model_dump(mode="json"))
    else:
        logger.warning("unknown_route role=%s type=%s", sender_role, frame_type)


# --- ui -> cloud -> device -------------------------------------------------


async def push_board(device_id: str, summary: Any | None) -> None:
    """One goal changed — tell the boards watching this session.

    A whole GoalSummary, replace-by-goal_id: idempotent, so a duplicate or
    out-of-order update cannot corrupt a card. No-ops when the fold ignored the
    frame (e.g. a goal the board never saw start).
    """
    if summary is None:
        return
    frame = BoardUpdate(board_seq=board.seq(device_id), goal=summary).model_dump(mode="json")
    log_frame("out", "ui", frame)
    await registry.send_to_uis(device_id, frame)


async def send_board_snapshot(device_id: str) -> None:
    """Every goal in this session — on bind, and to heal a board_seq gap."""
    seq, goals = board.snapshot(device_id)
    frame = BoardSnapshot(board_seq=seq, goals=goals).model_dump(mode="json")
    log_frame("out", "ui", frame)
    await registry.send_to_uis(device_id, frame)


async def send_suggestions(device_id: str) -> None:
    """The session's current proactive suggestions — on change and on bind (M8).

    Sent as a whole list every time, like a board_snapshot: idempotent, so a UI that
    reconnects or misses one just re-renders the current set. Only boards act on it;
    the chat UI ignores the frame.
    """
    frame = Suggestions(items=board.suggestions(device_id)).model_dump(mode="json")
    log_frame("out", "ui", frame)
    await registry.send_to_uis(device_id, frame)


async def handle_suggestion_action(device_id: str, action: SuggestionAction) -> None:
    """A board accepted or dismissed a suggestion.

    Accept turns it into an ordinary goal — the suggestion's goal_text becomes a
    user_goal, running the full understand → plan → approve flow, so a suggestion never
    acts on its own. Either way the suggestion is consumed and the refreshed list is
    pushed so the card disappears.
    """
    taken = board.take_suggestion(device_id, action.suggestion_id)
    if taken is not None and action.action == "accept":
        await handle_user_goal(
            device_id,
            UserGoal(text=taken["goal_text"], client_ref=action.client_ref),
        )
    await send_suggestions(device_id)


async def handle_goal_state_get(device_id: str, request: GoalStateGet) -> None:
    """Drill-in: whatever this goal is currently sitting on.

    The agent_event stream is deliberately NOT replayed — a rejoined view shows the
    plan and its ticks, not a re-run of the thinking.
    """
    # An unconfirmed understanding first: it is the whole reason to drill in from the
    # board (the card says "Confirm what the agent understood" and links here), and a
    # goal at this gate has neither a plan nor a status, so without this the tap
    # would land on an empty stage.
    understanding = board.cached_understanding(request.goal_id)
    if understanding is not None:
        await registry.send_to_uis(device_id, Understanding(
            goal_id=request.goal_id,
            payload=UnderstandingPayload(
                objective=understanding.get("objective", ""),
                domain=understanding.get("domain", ""),
                knew=understanding.get("knew") or graph_nodes._hard_knew(understanding.get("hard") or {}),
                thought=understanding.get("thought", ""),
                time_window=understanding.get("time_window") or None,
            ),
        ).model_dump(mode="json", exclude_none=True))
        return

    plan = board.cached_plan(request.goal_id)
    if plan is not None:
        await registry.send_to_uis(device_id, {
            "type": "present_plan",
            "goal_id": request.goal_id,
            "correlation_id": "-",
            "task_status": "monitoring",
            "payload": plan,
        })
    status = board.cached_status(request.goal_id)
    if status is not None:
        await registry.send_to_uis(device_id, status)
    if plan is None and status is None:
        logger.info("goal_state_get_miss goal_id=%s", request.goal_id)


async def send_device_offline(device_id: str, goal_id: str, correlation_id: str | None) -> None:
    """The paired device agent isn't connected — tell the UI instead of leaving it
    spinning on "planning" forever (the dispatch was dropped; nothing will answer).
    """
    logger.warning("dispatch_undelivered device_id=%s goal_id=%s reason=device_offline", device_id, goal_id)
    await registry.send_to_uis(
        device_id,
        {
            "type": "status",
            "goal_id": goal_id,
            "correlation_id": correlation_id or "-",
            "task_status": "done",
            "payload": {
                "material": False,
                "executed": [],
                "note": f"Device agent '{device_id}' isn't connected — start it and try again.",
            },
        },
    )


#: One lock per goal. Every graph invoke/resume must hold its goal's lock.
#:
#: LangGraph is synchronous, so each call hops to a worker thread via
#: ``asyncio.to_thread`` — and a goal has several things that can resume it at
#: once: a ``status`` tick and an ``approval`` for the SAME goal arrive
#: independently and both resume the same checkpoint from two OS threads. That is
#: a read-modify-write race on the checkpointer with no lock anywhere; v2's own
#: ARCHITECTURE.md flagged it as an open risk and it was survivable only because
#: one goal at a time meant the overlap was rare. Agent Board makes it routine.
#:
#: Keyed by goal, NOT global: goals must still run in parallel (goal B's
#: interpretation should not wait on goal A's approval). Per-goal serialised,
#: cross-goal concurrent.
#:
#: Unbounded by construction (one entry per goal_id ever seen) — acceptable for a
#: POC, and the entry is a bare asyncio.Lock. Real cleanup belongs with the goal
#: store in M6.
_goal_locks: dict[str, asyncio.Lock] = {}


def goal_lock(goal_id: str) -> asyncio.Lock:
    """The lock for one goal, created on first use.

    Safe without a lock of its own: this only ever runs on the event loop, and
    ``dict.setdefault`` is atomic with respect to it.
    """
    return _goal_locks.setdefault(goal_id, asyncio.Lock())


async def handle_user_goal(device_id: str, user_goal: UserGoal) -> None:
    """Run the graph to the understanding gate and send it to the UI.

    """
    import asyncio

    goal_id = str(uuid4())
    resolved_understandings.discard(goal_id)
    goal_id_var.set(goal_id)
    logger.info("task_status status=created")
    # Tie the submission to its goal_id straight away. With two goals in flight the
    # UI cannot otherwise tell which inbound goal_id is which — it would adopt
    # whichever arrives first and mis-key the card.
    if user_goal.client_ref:
        await registry.send_to_uis(
            device_id,
            GoalAccepted(goal_id=goal_id, client_ref=user_goal.client_ref).model_dump(mode="json"),
        )
    # Hand the interpreter what THIS session's device says it can do. The hub has
    # cached this since v2 and only ever relayed it to the UI; the gate that
    # decides what we can act on was a hardcoded list of two domains instead
    # (v3-M4). Now the answer follows the hardware that is plugged in.
    capabilities = registry.capabilities_of(device_id)
    async with goal_lock(goal_id):
        state = await asyncio.to_thread(
            graph_nodes.start_goal, graph, user_goal.text, goal_id, capabilities
        )
    if state.get("error"):
        await registry.send_to_uis(device_id,
            {
                "type": "status",
                "goal_id": goal_id,
                "correlation_id": state.get("correlation_id") or "-",
                "task_status": "done",
                "payload": {"material": False, "executed": [], "note": state["error"]},
            },
        )
        return

    # Out-of-scope: the interpreter judged the goal outside what GoalFlow acts on
    # (only meal plans + guest dinners). The graph ended before any dispatch — send
    # a terminal notice and stop; the device is never involved.
    explanation = state.get("explanation")
    if isinstance(explanation, dict) and explanation.get("type") == "out_of_scope":
        notice = Notice(
            goal_id=goal_id,
            kind="out_of_scope",
            message=explanation.get("message") or "That goal is outside what I can help with.",
        )
        logger.info("task_status status=done gate=out_of_scope")
        await registry.send_to_uis(device_id, notice.model_dump(mode="json"))
        return

    interrupt_payload = state.get("_interrupt")
    if isinstance(interrupt_payload, dict) and interrupt_payload.get("kind") == "understanding_confirmation":
        understanding = interrupt_payload.get("understanding") or {}
        hard = understanding.get("hard") or {}
        frame = Understanding(
            goal_id=goal_id,
            payload=UnderstandingPayload(
                objective=understanding.get("objective", ""),
                domain=understanding.get("domain", ""),
                knew=understanding.get("knew") or graph_nodes._hard_knew(hard),
                thought=understanding.get("thought", ""),
                time_window=understanding.get("time_window") or None,
            ),
        )
        logger.info("task_status status=grounding gate=understanding")
        await registry.send_to_uis(device_id, frame.model_dump(mode="json", exclude_none=True))
        # The board gets a card NOW. This gate can hold a goal indefinitely — it is
        # waiting on a person — so a board that only learns about goals at dispatch
        # would show nothing at all for the whole time it matters most.
        await push_board(device_id, board.on_understanding(
            device_id, goal_id, understanding, user_goal.client_ref))
        return

    frame = state.get("contract")
    if frame:
        dispatched_contracts[goal_id] = frame
        await push_board(device_id, board.on_goal_created(device_id, goal_id, frame, user_goal.client_ref))
        logger.info("task_status status=planning")
        if not await registry.send_to_device(device_id, frame):
            await send_device_offline(device_id, goal_id, state.get("correlation_id"))
        return

    await registry.send_to_uis(device_id,
        {
            "type": "status",
            "goal_id": goal_id,
            "correlation_id": state.get("correlation_id") or "-",
            "task_status": "done",
            "payload": {"material": False, "executed": [], "note": "Goal paused without an understanding payload."},
        },
    )


async def handle_understanding_response(device_id: str, response: UnderstandingResponse) -> None:
    """Resume the pre-planning gate; dispatch only after confirmed."""
    import asyncio

    if response.goal_id in resolved_understandings:
        logger.info("understanding_response_dedupe_drop goal_id=%s", response.goal_id)
        return
    resolved_understandings.add(response.goal_id)

    confirmed = response.payload.confirmed
    async with goal_lock(response.goal_id):
        state = await asyncio.to_thread(
            graph_nodes.resume_goal,
            graph,
            response.goal_id,
            {"confirmed": confirmed},
        )
    if not confirmed:
        await registry.send_to_uis(device_id,
            {
                "type": "status",
                "goal_id": response.goal_id,
                "correlation_id": state.get("correlation_id") or "-",
                "task_status": "done",
                "payload": {
                    "material": False,
                    "executed": [],
                    "note": "Goal cancelled before planning.",
                },
            },
        )
        # The board card was BORN at the understanding gate (M6). A decline means the
        # goal never became real, so drop the card — otherwise it sticks on "Confirm
        # what the agent understood" forever, since the raw status above bypasses the
        # board fold (only device frames are folded). A fresh snapshot is how the
        # board removes a card: board_update only upserts, but a snapshot is applied
        # authoritatively and drops what it no longer lists.
        board.forget_goal(device_id, response.goal_id)
        await send_board_snapshot(device_id)
        return

    if state.get("error"):
        await registry.send_to_uis(device_id,
            {
                "type": "status",
                "goal_id": response.goal_id,
                "correlation_id": state.get("correlation_id") or "-",
                "task_status": "done",
                "payload": {"material": False, "executed": [], "note": state["error"]},
            },
        )
        return

    frame = state.get("contract")
    if not frame:
        await registry.send_to_uis(device_id,
            {
                "type": "status",
                "goal_id": response.goal_id,
                "correlation_id": state.get("correlation_id") or "-",
                "task_status": "done",
                "payload": {
                    "material": False,
                    "executed": [],
                    "note": "Dispatch contract was not built after understanding confirmation.",
                },
            },
        )
        return

    dispatched_contracts[response.goal_id] = frame
    await push_board(device_id, board.on_goal_created(device_id, response.goal_id, frame, None))
    logger.info("task_status status=planning")
    if not await registry.send_to_device(device_id, frame):
        await send_device_offline(device_id, response.goal_id, state.get("correlation_id"))


async def handle_approval(device_id: str, approval: Approval) -> None:
    """Resume the graph's interrupt() with the decisions; forward to device.

    """
    import asyncio

    decisions = [decision.model_dump(mode="json") for decision in approval.payload.decisions]
    async with goal_lock(approval.goal_id):
        await asyncio.to_thread(graph_nodes.resume_goal, graph, approval.goal_id, decisions)
    await registry.send_to_device(device_id, approval.model_dump(mode="json"))


# --- device -> cloud -> ui ---------------------------------------------------


async def handle_capabilities(device_id: str, capabilities: Capabilities) -> None:
    """Cache the device module registry (per session) and relay it to the UI.

    """
    registry.set_capabilities(device_id, capabilities)
    await registry.send_to_uis(device_id, capabilities.model_dump(mode="json"))


async def relay_agent_event(device_id: str, event: AgentEvent) -> None:
    """PASSTHROUGH relay of the device's live stream to the UI.

    Also the board's data path: `task_update` carries the goal's progress, next step
    and pending count, derived by the DEVICE from its task DAG. The cloud cannot
    compute those — only the device can ground a decomposition — so this is the one
    place the board's numbers can come from.
    """
    if event.event == "task_update":
        await push_board(device_id, board.on_task_update(device_id, event.goal_id, event.payload or {}))
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
    await registry.send_to_uis(device_id, event.model_dump(mode="json"))


async def handle_plan_ready(device_id: str, plan_ready: PlanReady) -> None:
    """Resume the graph with the plan; re-wrap as present_plan (+knew) for UI.

    """
    import asyncio

    async with goal_lock(plan_ready.goal_id):
        state = await asyncio.to_thread(
            graph_nodes.resume_goal,
            graph,
            plan_ready.goal_id,
            plan_ready.model_dump(mode="json"),
        )
    payload = plan_ready.payload.model_dump(mode="json")
    payload["knew"] = build_knew(dispatched_contracts.get(plan_ready.goal_id))
    await push_board(device_id, board.on_plan_ready(device_id, plan_ready.goal_id, payload))
    present = PresentPlan(
        goal_id=plan_ready.goal_id,
        correlation_id=plan_ready.correlation_id,
        task_status=plan_ready.task_status,
        payload=payload,
    )
    await registry.send_to_uis(device_id, present.model_dump(mode="json"))

    approval_frame = state.get("approval_frame")
    if approval_frame:
        await registry.send_to_device(device_id, approval_frame)


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

    knew: dict[str, Any] = graph_nodes._hard_knew(hard)

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

    add("dislikes", soft.get("dislikes"))
    add("prefer", soft.get("prefer"))
    add("notes", context.get("notes"))
    return knew


async def graph_resume_monitor(goal_id: str, frame: dict[str, Any]) -> None:
    """Best-effort graph monitor resume for device status/proposal frames."""
    import asyncio

    try:
        async with goal_lock(goal_id):
            state = await asyncio.to_thread(graph_nodes.resume_goal, graph, goal_id, frame)
    except Exception:
        logger.exception("graph_monitor_resume_failed")
        return

    approval_frame = state.get("approval_frame")
    if approval_frame and frame.get("type") == "proposal":
        logger.debug("adapt_approval_frame_ready goal_id=%s", goal_id)


setup_logging()
