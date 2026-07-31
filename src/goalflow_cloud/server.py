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
from datetime import date, timedelta
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from goalflow_cloud.board import BoardService
from goalflow_cloud.config import get_settings
from goalflow_cloud.graph import nodes as graph_nodes
from goalflow_cloud.memory.store import append_constraints, load_family_profile, resolve_constraints
from goalflow_cloud.models.contract import (
    AgentEvent,
    Approval,
    BoardGet,
    BoardSnapshot,
    BoardUpdate,
    ChatUiClose,
    ChatUiOpen,
    GoalAccepted,
    GoalStateGet,
    Capabilities,
    Control,
    ControlPayload,
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


#: Types that recur many times per goal (streamed reasoning) — logged at DEBUG so the
#: INFO stream stays a readable lifecycle timeline. agent_event carries the device's
#: token-chunk thinking stream (dozens of frames per plan); it has a UI home (the chat-ui
#: reasoning transcript) but does not belong in the default relay log.
_HIGH_FREQUENCY_TYPES = frozenset({"agent_event"})


def log_frame(direction: str, role: str, frame: dict[str, Any]) -> None:
    """One structured line per relayed frame: direction, role, type, ids.

    High-frequency passthrough (see ``_HIGH_FREQUENCY_TYPES``) drops to DEBUG; every
    other frame is a discrete lifecycle/decision event and stays at INFO.
    """
    correlation_id_var.set(str(frame.get("correlation_id") or "-"))
    goal_id_var.set(str(frame.get("goal_id") or "-"))
    frame_type = frame.get("type", "-")
    level = logging.DEBUG if frame_type in _HIGH_FREQUENCY_TYPES else logging.INFO
    logger.log(
        level,
        "frame direction=%s role=%s type=%s",
        direction,
        role,
        frame_type,
    )
    logger.debug("frame_body direction=%s role=%s body=%s", direction, role, frame)


# ---------------------------------------------------------------------------
# Surface-aware delivery (v4.1) — the ONE delivery fork
# ---------------------------------------------------------------------------

#: What an ``input`` surface (Bixby, a native app) receives via the session fan-out.
#: Everything else in the broadcast it would only parse and drop, so the cloud simply
#: does not deliver it. Every OTHER surface (``chat``, ``board``, and absent/empty —
#: every v3 client) stays on the full broadcast, its client-side allowlist doing the
#: filtering. Handshake/discovery frames sent point-to-point (hello_ack, devices) are
#: OUTSIDE this fork — an input surface still needs the picker to bind.
INPUT_SURFACE_FRAMES = frozenset(
    {"hello_ack", "goal_accepted", "chat_ui_open", "chat_ui_close", "notice"}
)


def wants(surface: str, frame_type: str | None) -> bool:
    """Per-socket interest predicate for the session fan-out (v4.1).

    The ONE exception to "the cloud does not route by surface": an ``input`` socket
    receives only the five frames a native client acts on; every other surface
    (``chat``/``board``/absent) receives everything, unchanged from v3.
    """
    if surface == "input":
        return frame_type in INPUT_SURFACE_FRAMES
    return True


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
    #: v4.1 create-phase replay cache — the CURRENT create-phase goal's state,
    #: ``{"goal_id": str, "understanding": dict|None, "present_plan": dict|None,
    #: "notice": dict|None}`` (the exact frames as broadcast, present_plan INCLUDING
    #: payload.knew). Created at ``chat_ui_open``; understanding/present_plan/notice
    #: captured as they are sent; cleared at ``chat_ui_close``; replaced wholesale by a
    #: superseding open. Keyed by SESSION (not goal) — it answers "which goal is the
    #: webview about, right now" — so it is a tiny structure separate from the board's
    #: per-goal cache. Replayed to a freshly-bound ``chat`` socket in ``_bind_ui``.
    create_phase: dict[str, Any] | None = None


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
        #: reverse lookup for routing + teardown: ws -> (role, device_id | "", surface).
        #: ``surface`` (v4.1) is per-SOCKET state — a device is always "", a ui carries
        #: its hello.surface — so it lives here rather than on the flat ``Session.uis``.
        self._meta: dict[WebSocket, tuple[Role, str, str]] = {}

    # --- lookup ---
    def session_of(self, websocket: WebSocket) -> str:
        """The device_id a socket is bound to ("" if an unbound ui)."""
        meta = self._meta.get(websocket)
        return meta[1] if meta else ""

    def surface_of(self, websocket: WebSocket) -> str:
        """This socket's declared surface ("" = full broadcast / a v3 client)."""
        meta = self._meta.get(websocket)
        return meta[2] if meta else ""

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
        self._meta[websocket] = ("device", device_id, "")
        await self._ack(websocket, "device", device_id)
        await self.broadcast_devices()

    async def register_ui(self, websocket: WebSocket, device_id: str, surface: str = "") -> None:
        if device_id:
            await self._bind_ui(websocket, device_id, ack=True, surface=surface)
            return
        # No device_id: auto-bind when there's exactly ONE device (the common
        # single-home / zero-config case works on any UI, no picker needed).
        online = [s for s in self._sessions.values() if s.device is not None]
        if len(online) == 1:
            await self._bind_ui(websocket, online[0].device_id, ack=True, surface=surface)
            return
        # 0 or 2+ devices: hold unbound and offer the list for discovery/picker.
        # Surface is stored NOW (immutable for the socket's life) so a later
        # select_device rebind carries it without the hello being re-read.
        if websocket not in self._unbound_uis:
            self._unbound_uis.append(websocket)
        self._meta[websocket] = ("ui", "", surface)
        await self._ack(websocket, "ui", "")
        await self.send_devices(websocket)

    async def bind_ui(self, websocket: WebSocket, device_id: str) -> None:
        """Move an unbound ui into a session (from a select_device frame). Acks so
        the UI learns which device it landed on (hello_ack.device_id)."""
        if websocket in self._unbound_uis:
            self._unbound_uis.remove(websocket)
        await self._bind_ui(websocket, device_id, ack=True)

    async def _bind_ui(
        self, websocket: WebSocket, device_id: str, ack: bool, surface: str | None = None
    ) -> None:
        # Surface is immutable for the socket's life: a rebind (select_device) keeps
        # whatever was captured at the handshake, so resolve it from _meta when the
        # caller didn't pass one. _leave_session reads _meta without clearing it, so
        # this snapshot is still valid below.
        existing = self._meta.get(websocket)
        if surface is None:
            surface = existing[2] if existing else ""

        # Re-binding must MOVE the socket, not add it to a second session: leaving it in
        # the old session's ui list would deliver BOTH homes' frames to it — the exact
        # cross-session leak this registry exists to prevent.
        self._leave_session(websocket, keep=device_id)

        session = self._session(device_id)
        if websocket not in session.uis:
            session.uis.append(websocket)
        self._meta[websocket] = ("ui", device_id, surface)
        logger.info(
            "ui_bound device_id=%s surface=%s uis=%d device_online=%s",
            device_id,
            surface or "(broadcast)",
            len(session.uis),
            session.device is not None,
        )
        if ack:
            await self._ack(websocket, "ui", device_id)
        # Every ui gets the current list, bound or not: it needs the paired device's
        # NAME to display, and the list to offer a "change device" affordance. This is
        # point-to-point discovery — OUTSIDE the surface fork, so an input surface can
        # still bind via the picker.
        await self.send_devices(websocket)
        # The bind-time pushes are gated by the SAME interest predicate as the fan-out:
        # an input surface gets none of the capabilities/board firehose.
        if session.capabilities is not None and wants(surface, "capabilities"):
            caps = session.capabilities.model_dump(mode="json")
            log_frame("out", "ui", caps)
            try:
                await websocket.send_json(caps)
            except Exception:
                logger.debug("caps_replay_failed", exc_info=True)
        # First paint for a board: the session's goals, unprompted. A board that had
        # to ASK would render empty for a beat on every reload.
        if wants(surface, "board_snapshot"):
            await self._replay_board(websocket, device_id)
        # v4.1: rehydrate a freshly-bound chat webview with the CURRENT create-phase
        # goal — chat_ui_open (the reset), then the cached understanding (only while no
        # plan yet) or the cached present_plan. Mirrors board_snapshot-on-bind and kills
        # the connect-vs-understanding race. Chat surface ONLY: an absent-surface (v3)
        # client keeps byte-for-byte v3 behaviour.
        if surface == "chat" and session.create_phase is not None:
            await self._replay_create_phase(websocket, session.create_phase)

    def _leave_session(self, websocket: WebSocket, keep: str = "") -> None:
        """Remove a ui socket from the session it currently belongs to (if any other
        than ``keep``), GC'ing the session if that empties it."""
        previous = self._meta.get(websocket)
        if previous is None:
            return
        _, previous_id, _ = previous
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
        role, device_id, _ = meta
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
        # v4.1: the ONE delivery fork. Consult the interest predicate per target
        # socket, in the send loop — no per-call-site routing table, no new send
        # paths. Every surface but "input" takes everything (unchanged from v3).
        frame_type = frame.get("type")
        for websocket in targets:
            meta = self._meta.get(websocket)
            surface = meta[2] if meta else ""
            if not wants(surface, frame_type):
                continue
            try:
                await websocket.send_json(frame)
            except Exception:
                logger.warning("send_failed role=ui device_id=%s type=%s", device_id, frame.get("type"), exc_info=True)
                await self.unregister(websocket)

    async def _replay_board(self, websocket: WebSocket, device_id: str) -> None:
        """Send this session's board snapshot to one freshly-bound socket."""
        seq, goals = board.snapshot(device_id)
        try:
            if goals:
                frame = BoardSnapshot(board_seq=seq, goals=goals).model_dump(mode="json")
                log_frame("out", "ui", frame)
                await websocket.send_json(frame)
        except Exception:
            logger.debug("board_replay_failed", exc_info=True)

    async def _replay_create_phase(self, websocket: WebSocket, create_phase: dict[str, Any]) -> None:
        """Rehydrate one freshly-bound ``chat`` socket with the current create phase.

        chat_ui_open (the reset), then the cached understanding (only while no plan
        yet), then the cached present_plan. Point-to-point, like _replay_board.

        A cached NOTICE wins outright and is sent alone: it is terminal, and the chat
        UI's reducer clears the stage on it anyway, so replaying an understanding
        underneath it would only paint a screen the next frame tears down.

        WHY THE NOTICE IS HERE AT ALL: a refusal is the one create phase with no
        round-trip in it. The cloud opens the bracket and broadcasts the notice in the
        same breath, but Bixby is only just MOUNTING the webview off that same open —
        its socket connects some hundreds of ms later and had missed the only frame the
        phase will ever have. What the user saw was a blank webview appear and, four
        and a half seconds later, close. Cached, it arrives on bind like everything else.
        """
        goal_id = create_phase.get("goal_id")
        understanding = create_phase.get("understanding")
        present_plan = create_phase.get("present_plan")
        notice = create_phase.get("notice")
        try:
            # The text rides the replayed open too: a webview that binds DURING
            # interpretation is exactly the case this whole change exists for, and it
            # would otherwise rehydrate into the same blank panel.
            open_frame = ChatUiOpen(
                goal_id=goal_id, goal_text=create_phase.get("goal_text") or ""
            ).model_dump(mode="json")
            log_frame("out", "ui", open_frame)
            await websocket.send_json(open_frame)
            if notice is not None:
                log_frame("out", "ui", notice)
                await websocket.send_json(notice)
                return
            if present_plan is None and understanding is not None:
                log_frame("out", "ui", understanding)
                await websocket.send_json(understanding)
            if present_plan is not None:
                log_frame("out", "ui", present_plan)
                await websocket.send_json(present_plan)
        except Exception:
            logger.debug("create_phase_replay_failed", exc_info=True)

    # --- create-phase replay cache (v4.1) ---
    def open_create_phase(self, device_id: str, goal_id: str, goal_text: str = "") -> None:
        """A goal became the session's create-phase goal — create (or REPLACE
        wholesale, if a previous phase is still open) the replay cache."""
        self._session(device_id).create_phase = {
            "goal_id": goal_id,
            "goal_text": goal_text,
            "understanding": None,
            "present_plan": None,
            "notice": None,
        }

    def create_phase_goal(self, device_id: str) -> str | None:
        """The session's current create-phase goal_id (None = no phase open).

        The guard for every ``chat_ui_close``: a close fires only while its goal is
        still this — which is what stops board adaptation ``approval`` frames (whose
        goal is no longer the create-phase goal) from retriggering a close."""
        session = self._sessions.get(device_id)
        cp = session.create_phase if session else None
        return cp.get("goal_id") if cp else None

    def capture_understanding(self, device_id: str, goal_id: str, frame: dict[str, Any]) -> None:
        """Cache the understanding frame as broadcast (create-phase goal only)."""
        session = self._sessions.get(device_id)
        cp = session.create_phase if session else None
        if cp and cp.get("goal_id") == goal_id:
            cp["understanding"] = frame

    def capture_present_plan(self, device_id: str, goal_id: str, frame: dict[str, Any]) -> None:
        """Cache the present_plan frame as broadcast (create-phase goal only)."""
        session = self._sessions.get(device_id)
        cp = session.create_phase if session else None
        if cp and cp.get("goal_id") == goal_id:
            cp["present_plan"] = frame

    def capture_notice(self, device_id: str, goal_id: str, frame: dict[str, Any]) -> None:
        """Cache a TERMINAL notice as broadcast (create-phase goal only).

        Only the terminal kinds are worth caching — a mid-save ``updating_goals`` is a
        caption on a screen that is already up, and replaying it to a socket that binds
        afterwards would caption nothing.
        """
        if frame.get("kind") == "updating_goals":
            return
        session = self._sessions.get(device_id)
        cp = session.create_phase if session else None
        if cp and cp.get("goal_id") == goal_id:
            cp["notice"] = frame

    def clear_create_phase(self, device_id: str, goal_id: str) -> bool:
        """Clear the cache IFF ``goal_id`` is still the create-phase goal.

        Returns whether it matched — the caller emits ``chat_ui_close`` only then."""
        session = self._sessions.get(device_id)
        cp = session.create_phase if session else None
        if cp and cp.get("goal_id") == goal_id:
            session.create_phase = None
            return True
        return False

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
            # hello.surface (v4.1) is captured here and immutable for the socket's life.
            await registry.register_ui(websocket, hello.device_id, hello.surface)

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
        control = Control(**frame)
        # A finished goal is not work — it is a receipt. Retire it on the NEXT tick of
        # the world, so the user still sees the run it completed on, and the board goes
        # back to showing only what is live. Swept BEFORE the control reaches the device
        # so the retirement lands with the tap, not a round-trip later.
        if control.command == "advance_day" and not control.goal_id:
            await retire_completed_goals(device_id)
        await registry.send_to_device(device_id, control.model_dump(mode="json"))
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
        # v7 — the board now HAS the change the chat surface promised. Released here and
        # not on the control's send, because "we told the device" is not the thing the
        # user was waiting to be true.
        #
        # The release is "the first status this goal sends after the control was armed",
        # not "a status carrying a plan_changed_note": a constraint change that decided
        # nothing needed to move still answers, and keying on the note would hold the
        # webview open for the full timeout on exactly the quiet case. The looseness costs
        # nothing — the only other thing that makes a goal speak is a world tick, and the
        # user is in the chat, not on the board, for the whole of this window. If one did
        # slip in, the webview closes a little early rather than not at all.
        if (waiter := crossgoal_waiters.get(status.goal_id)) is not None:
            waiter.set()
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


async def emit_chat_ui_open(device_id: str, goal_id: str, goal_text: str = "") -> None:
    """Open the create-phase bracket (v4.1): create/replace the replay cache and
    broadcast ``chat_ui_open``. A superseding open for a new goal replaces the cache
    wholesale (no intervening close — Bixby retargets rather than close/reopen).

    Ordering guarantee: callers invoke this BEFORE the goal's ``understanding``, so
    the reset lands first on the wire (and the bind-time replay re-sends it in order).

    ``goal_text`` is what the user said, carried so the webview has something true to
    show while the interpreter thinks (v7.4).
    """
    frame = ChatUiOpen(goal_id=goal_id, goal_text=goal_text).model_dump(mode="json")
    registry.open_create_phase(device_id, goal_id, goal_text)
    await registry.send_to_uis(device_id, frame)


async def emit_chat_ui_close(device_id: str, goal_id: str) -> None:
    """Close the create-phase bracket (v4.1), GUARDED: fires only while ``goal_id``
    is still the session's current create-phase goal (the replay-cache key). That
    guard is what stops board adaptation ``approval`` frames — whose goal is no longer
    the create-phase goal — from retriggering a close. Clears the cache when it fires.
    """
    if registry.clear_create_phase(device_id, goal_id):
        await registry.send_to_uis(device_id, ChatUiClose(goal_id=goal_id).model_dump(mode="json"))


async def send_board_snapshot(device_id: str) -> None:
    """Every goal in this session — on bind, and to heal a board_seq gap."""
    seq, goals = board.snapshot(device_id)
    frame = BoardSnapshot(board_seq=seq, goals=goals).model_dump(mode="json")
    log_frame("out", "ui", frame)
    await registry.send_to_uis(device_id, frame)


async def retire_completed_goals(device_id: str) -> None:
    """Advance day: a goal the device already called done leaves the board.

    A SNAPSHOT, not a board_update — the update frame carries one GoalSummary and can
    only replace a card, so there is no way to say "this one is gone" except by
    re-stating the whole board. No-ops (and stays silent) when nothing finished.
    """
    retired = board.retire_completed(device_id)
    if not retired:
        return
    logger.info("board_retire_completed device_id=%s goals=%s", device_id, ",".join(retired))
    await send_board_snapshot(device_id)


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
                knew=understanding.get("knew")
                or graph_nodes._hard_knew(understanding.get("hard_display") or understanding.get("hard") or {}),
                constraints=understanding.get("constraints") or [],
                preferences=understanding.get("preferences") or [],
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

    # OPEN THE BRACKET NOW — before interpretation, not after it (v7.4).
    #
    # `start_goal` is an LLM round-trip that takes 10-60s, and until v7.4 the open was
    # emitted AFTERWARDS. So for the whole of the slowest, least-explicable wait in the
    # product, Bixby had not been told to show anything: the user spoke to the fridge and
    # the fridge's screen sat there. The understanding card then appeared all at once, as
    # if the work had been instant and the silence had been a fault.
    #
    # Nothing downstream needs interpretation to have finished — the open is a RESET keyed
    # to a goal_id, and the goal_id is minted above. Opening first costs nothing and buys
    # the entire interpretation window as visible, honest progress. The two early exits
    # below (error, out-of-scope) now inherit an already-open bracket, which is why the
    # error path closes it and the refusal path no longer opens one.
    await emit_chat_ui_open(device_id, goal_id, user_goal.text)

    async with goal_lock(goal_id):
        state = await asyncio.to_thread(
            graph_nodes.start_goal, graph, user_goal.text, goal_id, capabilities
        )
    if state.get("error"):
        # The bracket is open (above), so it has to be closed or the webview dangles on a
        # goal that ended before it began.
        await emit_chat_ui_close(device_id, goal_id)
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

    # Out-of-scope: the interpreter judged the goal outside what this device can advance.
    # The graph ended before any dispatch — the device is never involved.
    #
    # v7 GIVES THE REFUSAL A SURFACE. Until now this returned before opening the create
    # bracket, so the only place a decline appeared was the input surface speaking it: on
    # the Hub the user asked the fridge for something and the fridge's screen showed
    # nothing at all. A refusal is an answer, and an answer deserves to be shown where
    # every other answer is shown. The bracket opens, the notice fills it, and the cloud
    # closes it again a few seconds later — nobody should have to dismiss a "no".
    explanation = state.get("explanation")
    if isinstance(explanation, dict) and explanation.get("type") == "out_of_scope":
        notice = Notice(
            goal_id=goal_id,
            kind="out_of_scope",
            message=explanation.get("message") or "That goal is outside what I can help with.",
        )
        logger.info("task_status status=done gate=out_of_scope")
        # The bracket is ALREADY open (v7.4 opens it on arrival), and re-opening would
        # broadcast a second reset that wipes the panel the user has been watching think.
        notice_frame = notice.model_dump(mode="json")
        await registry.send_to_uis(device_id, notice_frame)
        # ...and cache it, because the webview this refusal is FOR does not exist yet —
        # Bixby is still mounting the iframe off the open we sent one line ago. Without
        # this, the only frame the phase ever has is broadcast to a socket that has not
        # connected, and the user watches an empty panel for the dwell. See
        # _replay_create_phase.
        registry.capture_notice(device_id, goal_id, notice_frame)
        asyncio.create_task(_close_after(device_id, goal_id, OUT_OF_SCOPE_DWELL_S))
        return

    # The bracket was opened on arrival (v7.4), so by here the webview has been up for
    # the whole interpretation and simply receives the understanding next.

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
                # v6: the provenance behind the chips, and (M4) any household rule the
                # user stated in the same breath. Both additive — a UI that ignores
                # them renders exactly as before.
                constraints=understanding.get("constraints") or [],
                # v7: the soft half, rendered as its own lighter section.
                preferences=understanding.get("preferences") or [],
                proposed_constraints=understanding.get("proposed_constraints") or [],
                capture_only=bool(understanding.get("capture_only")),
                thought=understanding.get("thought", ""),
                time_window=understanding.get("time_window") or None,
            ),
        )
        logger.info("task_status status=grounding gate=understanding capture_only=%s",
                    bool(understanding.get("capture_only")))
        understanding_frame = frame.model_dump(mode="json", exclude_none=True)
        await registry.send_to_uis(device_id, understanding_frame)
        # Cache the understanding exactly as broadcast, for replay to a chat webview
        # that binds mid-create.
        registry.capture_understanding(device_id, goal_id, understanding_frame)
        # A CAPTURE is not a goal: no plan is coming and nothing will run, so it must
        # not put a card on the board. A board that showed "we've gone vegan" as a
        # goal card would leave a row nothing can ever complete.
        if not understanding.get("capture_only"):
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

    # Degenerate terminal: neither an interrupt nor a contract, yet the bracket is
    # open. Close it (guarded) so the webview cannot dangle — the same reason the
    # post-confirmation terminal-error paths close below.
    await emit_chat_ui_close(device_id, goal_id)
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
            # v6-M4: which household rules the user ticked. The graph persists exactly
            # these and nothing else — an accepted GOAL never implies an accepted rule.
            {"confirmed": confirmed, "accepted_constraint_ids": response.payload.accepted_constraint_ids},
        )

    # v6-M4: a capture gate ends here — there is no contract and no card, just a
    # household rule that is now remembered (or wasn't). Answered before the
    # not-confirmed branch, because declining a capture is not a cancelled goal.
    explanation = state.get("explanation")
    if isinstance(explanation, dict) and explanation.get("type") == "captured":
        await emit_chat_ui_close(device_id, response.goal_id)
        await registry.send_to_uis(
            device_id,
            Notice(
                goal_id=response.goal_id,
                kind="captured",
                message=explanation.get("message") or "Noted.",
            ).model_dump(mode="json"),
        )
        logger.info("task_status status=done gate=capture")
        return

    if not confirmed:
        # Create phase cancelled at the gate. Close the bracket (guarded) and, per
        # user decision Q3, ALSO speak a declined notice so the input (Bixby) surface
        # can confirm the cancellation aloud — the input surface never sees the
        # status/board frames below.
        await emit_chat_ui_close(device_id, response.goal_id)
        await registry.send_to_uis(
            device_id,
            Notice(
                goal_id=response.goal_id,
                kind="declined",
                message="Okay, I've cancelled that. Just say the word when you want to try again.",
            ).model_dump(mode="json"),
        )
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
        # Post-open terminal error (the goal confirmed, then the graph failed). Close
        # the bracket (guarded) so the webview doesn't dangle.
        await emit_chat_ui_close(device_id, response.goal_id)
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
        # Post-open terminal error: confirmed, but no dispatch contract built. Close.
        await emit_chat_ui_close(device_id, response.goal_id)
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
    # v7 — THE CROSS-GOAL MOMENT. Approving a goal can change the HOUSEHOLD, and every
    # other running goal is planning against a household that no longer exists. Done
    # BEFORE the webview closes, so the chat surface can hold its "saving, and updating
    # your other goals…" screen for exactly as long as the work takes.
    waiting_on = await fan_out_household_change(device_id, approval.goal_id)
    # v4.1: the initial approval ends the create phase — the user's final tap, the
    # moment the board becomes the primary surface. Close the webview bracket. GUARDED
    # on "still the create-phase goal", so a board adaptation approval (whose goal is
    # no longer the create-phase goal) never retriggers a close.
    #
    # NOT AWAITED: this handler runs inline on the UI's receive loop, and the close can
    # now be a minute away. Blocking here would stop that UI being heard from — the same
    # socket the user is about to press Advance day on.
    asyncio.create_task(_close_when_saved(device_id, approval.goal_id, waiting_on))


#: How long a refusal stays on the chat surface before the cloud closes it (v7).
#:
#: Long enough to read a sentence and understand it was a decision rather than a glitch;
#: short enough that nobody reaches for a dismiss button that is deliberately not there.
#: A refusal needs no action, so asking for one would be the interface inventing work.
OUT_OF_SCOPE_DWELL_S = 4.5


async def _close_after(device_id: str, goal_id: str, seconds: float) -> None:
    """Close the create-phase bracket after a dwell, without blocking the handler.

    ``emit_chat_ui_close`` is already guarded on "still the session's create-phase goal",
    so if the user says something new during the dwell this becomes a no-op rather than
    closing the webview out from under their next goal.
    """
    try:
        await asyncio.sleep(seconds)
        await emit_chat_ui_close(device_id, goal_id)
    except Exception:  # noqa: BLE001 - a background task must never take the process down
        logger.exception("timed_close_failed goal=%s", goal_id)


#: The floor on how long the chat's "Saving your plan…" screen is up (v7).
#:
#: An approval used to close the webview within a round-trip — a few tens of
#: milliseconds — so the saving screen existed in the code and never existed on screen,
#: and the surface the user had just tapped vanished under their finger. The dwell is not
#: decoration: the tap moved the goal from the chat to the board, and a hand-off with no
#: visible moment reads as a crash. Long enough to register as a transition, short enough
#: that nobody is waiting on it.
SAVE_DWELL_S = 1.6

#: How long the webview will hold open waiting for another goal to finish re-planning
#: after a household change (v7). The re-plan is a real LLM call on the device, so this is
#: generous — but bounded, because a device that never answers must not strand the user in
#: a spinner with no way out. On timeout the webview closes and the board still gets the
#: change whenever it lands; the user simply is not watched over while it does.
CROSS_GOAL_WAIT_S = 180.0

#: goal_id -> "this goal's cross-goal re-plan has landed". Registered before the control
#: goes down the wire (never after — the device can answer faster than we can arm) and
#: fired by the status route when the re-planned goal reports back.
crossgoal_waiters: dict[str, asyncio.Event] = {}


async def _close_when_saved(device_id: str, goal_id: str, waiting_on: list[str]) -> None:
    """Hold the create-phase webview until the save it announced is actually true.

    THE SCREEN HAS TO OUTLAST THE WORK IT DESCRIBES. In Act 1 that is the dwell floor —
    there is nothing to wait for but the hand-off itself. In Act 3 the chat says it is
    "updating your other goals", and the honest moment to close is when the other goals
    have actually been updated: the user watches the sentence that names the meal plan,
    and finds the meal plan already changed when they get to the board.

    The cloud owns this and not the chat UI, because the chat UI does not own its own
    lifetime — Bixby unmounts the webview the instant `chat_ui_close` arrives, so a dwell
    held only inside the iframe is a dwell nobody sees.
    """
    try:
        started = asyncio.get_running_loop().time()
        for other in waiting_on:
            event = crossgoal_waiters.get(other)
            if event is None:
                continue
            try:
                await asyncio.wait_for(event.wait(), timeout=CROSS_GOAL_WAIT_S)
            except asyncio.TimeoutError:
                logger.warning("crossgoal_wait_timeout goal=%s waited_on=%s", goal_id, other)
            finally:
                crossgoal_waiters.pop(other, None)

        elapsed = asyncio.get_running_loop().time() - started
        if elapsed < SAVE_DWELL_S:
            await asyncio.sleep(SAVE_DWELL_S - elapsed)
        await emit_chat_ui_close(device_id, goal_id)
    except Exception:  # noqa: BLE001 - a background task must never take the process down
        logger.exception("deferred_close_failed goal=%s", goal_id)
        await emit_chat_ui_close(device_id, goal_id)


#: Hard kinds whose arrival changes what OTHER goals should be planning, not merely what
#: they may do. An away window empties days; a peak-tariff window only reshapes when
#: something runs, which the safety gate already handles at actuation without a re-plan.
HOUSEHOLD_WIDE_KINDS = ("away_window",)


async def fan_out_household_change(device_id: str, approved_goal_id: str) -> list[str]:
    """Promote an approved goal's window to the household, and re-plan whoever it moves.

    THE SHAPE OF THE MOMENT. Goal 2 says "we're away Thursday and Friday". Until it is
    approved that is a proposal, so it binds only itself; approving it makes it a fact
    about the household, and a meal week that is still planning dinners for Thursday is
    now planning against a household that does not exist.

    Two halves, and the order matters. First the window is WRITTEN to the store as a
    household-scoped, chat-sourced, self-expiring entry — so it survives a restart, shows
    its provenance, and retires itself rather than shadowing every future trip. Then every
    OTHER active goal is re-resolved against the new store and, where its enforced set
    actually moved, sent down to re-plan.

    NOT AN APPROVAL. The user approved this when they approved goal 2; asking again would
    be asking the same question twice. See ControlCommands.ConstraintsChanged.
    """
    contract = dispatched_contracts.get(approved_goal_id)
    if not contract:
        return []
    hard = (contract.get("constraints") or {}).get("hard") or {}
    window = next((hard[kind] for kind in HOUSEHOLD_WIDE_KINDS if isinstance(hard.get(kind), dict)), None)
    if not window or not (window.get("start") and window.get("end")):
        return []

    written = await asyncio.to_thread(
        append_constraints,
        [{
            "kind": "away_window",
            "value": {"start": window["start"], "end": window["end"]},
            "enforcement": "hard",
            "scope": "household",
            "applies_to": ["*"],
            "label": "away window",
            "note": f"you approved “{contract.get('title') or contract.get('objective', 'a goal')}”",
            # Retires itself the day the family is back. A permanent away window would
            # quietly empty every future meal week for the same two dates.
            "expires_on": window["end"],
        }],
    )
    if not written:
        # Already known — a re-sent approval, or a reconnect replaying one. The store is
        # append-only and idempotent enough to say nothing rather than write a duplicate.
        logger.info("household_window_unchanged goal=%s", approved_goal_id)
        return []

    logger.info("household_window_written goal=%s start=%s end=%s",
                approved_goal_id, window["start"], window["end"])

    profile = await asyncio.to_thread(load_family_profile)
    today = date.today()
    _, summaries = board.snapshot(device_id)
    pushed = 0
    #: Which goals the create-phase webview should wait for before it closes.
    waiting_on: list[str] = []
    for summary in summaries:
        if summary.goal_id == approved_goal_id or summary.state in ("done", "declined"):
            continue
        other = dispatched_contracts.get(summary.goal_id)
        if not other:
            continue
        domain = other.get("domain") or ""
        resolved = resolve_constraints(profile, domain, today=today)
        before = (other.get("constraints") or {}).get("hard") or {}
        after = resolved["hard"]
        if after == before:
            continue

        # Say it before doing it, so the chat's saving screen can caption itself with
        # what is actually happening rather than a generic spinner. Sent once, on the
        # first goal that moves — the user does not need a running commentary.
        if pushed == 0:
            await registry.send_to_uis(device_id, Notice(
                goal_id=approved_goal_id,
                kind="updating_goals",
                message=f"Updating {summary.title} — you're away {_window_words(window)}.",
            ).model_dump(mode="json"))
        pushed += 1

        note = (
            f"Plan changed — you're away {_window_words(window)}. Review."
        )
        source = contract.get("title") or contract.get("objective") or "another goal"
        steer = (
            f"The family is away from {window['start']} to {window['end']} inclusive — nobody is home "
            "on those dates.\n"
            "For EVERY plan row whose date falls inside that range: set status to \"skipped\", set the "
            "title to exactly \"Away — no meal planned\", and set status_reason to exactly "
            f"\"you're away · from {source}\". Do not invent other wording for those two fields — they "
            "are read by a person who wants to know why a day is empty, not what the system called it.\n"
            "Then adjust the days immediately before and after if it helps: use up what would spoil "
            "before leaving, and keep the first day back light, because the kitchen will be bare."
        )
        # ARM BEFORE SENDING. The device can answer faster than we can set this up, and a
        # waiter registered after the fact waits for an event that already fired.
        crossgoal_waiters[summary.goal_id] = asyncio.Event()
        waiting_on.append(summary.goal_id)
        await registry.send_to_device(device_id, Control(
            goal_id=summary.goal_id,
            command="constraints_changed",
            payload=ControlPayload(hard=after, steer=steer, note=note),
        ).model_dump(mode="json"))
        # Keep the cached contract in step, or the next fan-out compares against a
        # household two changes old and decides nothing moved.
        other.setdefault("constraints", {})["hard"] = after
        logger.info("household_change_pushed goal=%s domain=%s", summary.goal_id, domain)

    return waiting_on


def _window_words(window: dict[str, Any]) -> str:
    """"2026-07-30".."2026-07-31" -> "Thu & Fri" — the card has one line, not a date range."""
    try:
        start = date.fromisoformat(window["start"])
        end = date.fromisoformat(window["end"])
    except (KeyError, TypeError, ValueError):
        return "while you're away"
    days = [(start + timedelta(days=i)).strftime("%a") for i in range((end - start).days + 1)]
    if len(days) == 1:
        return days[0]
    if len(days) == 2:
        return f"{days[0]} & {days[1]}"
    return f"{days[0]}–{days[-1]}"


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
    present_frame = present.model_dump(mode="json")
    await registry.send_to_uis(device_id, present_frame)
    # Cache the present_plan exactly as broadcast (INCLUDING payload.knew) for replay
    # to a chat webview that binds mid-create — no-op unless this is the create-phase
    # goal (an adaptation replan for an already-approved goal isn't cached).
    registry.capture_present_plan(device_id, plan_ready.goal_id, present_frame)

    approval_frame = state.get("approval_frame")
    if approval_frame:
        await registry.send_to_device(device_id, approval_frame)


def _display_hard(hard: dict[str, Any], domain: str) -> dict[str, Any]:
    """The dispatched hard block, narrowed to the keys this domain shows.

    Re-resolves the store rather than threading a second block through the dispatch:
    the contract is the device's, and adding a display-only field to it would put a UI
    concern on the wire the device would have to be told to ignore. If the store cannot
    be read for any reason the block is returned WHOLE — an unfiltered chip row is a
    cosmetic problem, and a plan card that fails to render over one is not.
    """
    if not domain:
        return hard
    try:
        allowed = resolve_constraints(load_family_profile(), domain)["hard_display"]
    except Exception:  # noqa: BLE001 - display only; never fail a plan over a chip
        logger.warning("knew_display_filter_failed domain=%s — showing the full block", domain)
        return hard
    return {key: value for key, value in hard.items() if key in allowed}


def build_knew(contract: dict[str, Any] | None) -> dict[str, Any]:
    """The UI-facing "what it knew" summary from a dispatched contract.

    GENERIC: surfaces constraints.hard (safety policy), constraints.soft
    (preferences), and context — no domain-specific field names.

    v7: the hard half is filtered to what this DOMAIN displays, so the plan card's
    chips say the same thing the understanding card's did. Without this the gate would
    show three chips and the plan four, and the reader would reasonably assume
    something changed between them. The filter is a display concern only — the
    contract was dispatched with the full block, and it is the full block the device
    armed.
    """
    if not contract:
        return {}
    hard = (contract.get("constraints") or {}).get("hard") or {}
    soft = (contract.get("constraints") or {}).get("soft") or {}
    context = contract.get("context") or {}

    knew: dict[str, Any] = graph_nodes._hard_knew(_display_hard(hard, contract.get("domain") or ""))

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
