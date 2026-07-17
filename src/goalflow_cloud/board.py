"""Agent Board's truth — derived, not reported.

The board shows every goal at once, where every other frame is about one goal. This
folds the frames the hub ALREADY routes (``dispatch``, ``plan_ready``,
``task_update``, ``status``, ``proposal``, ``approval``) into one ``GoalSummary``
per goal.

WHY THE CLOUD AND NOT THE DEVICE: the hub already sees every frame a goal produces,
and it is the only place that sees ALL goals of a session — the device knows its own
goals, but the board is a session-level view. Asking the device for a board would
mean inventing a frame that duplicates what the hub can already compute.

WHY DERIVED AND NOT GUESSED: v2 had ten task-status strings and no task model, so a
board built then could only have inferred progress from plan-day vs the clock — a
number that looks authoritative and is fiction. Every field here traces to something
the device actually said: ``progress_pct`` and ``next_step`` come from the device's
task DAG via ``task_update``, ``eta`` from the contract's own time window, ``alerts``
from real blocked/deferred/adapting events.

Deterministic and side-effect free: no LLM, no I/O. That is what makes it testable
and what keeps the board honest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from goalflow_cloud.models.contract import GoalAlerts, GoalSummary

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BoardService:
    """Per-session board state: one GoalSummary per goal, folded from frames.

    ``board_seq`` is monotonic per session so a UI can detect a dropped update and
    heal with ``board_get``. Without it a lost frame leaves a card permanently stale
    with no way to notice.
    """

    def __init__(self) -> None:
        #: device_id -> goal_id -> GoalSummary
        self._goals: dict[str, dict[str, GoalSummary]] = {}
        #: device_id -> board_seq
        self._seq: dict[str, int] = {}
        #: goal_id -> the last present_plan payload (for goal_state_get drill-in)
        self._plans: dict[str, dict[str, Any]] = {}
        #: goal_id -> the last status frame (for goal_state_get)
        self._statuses: dict[str, dict[str, Any]] = {}
        #: goal_id -> the understanding awaiting a human (for goal_state_get drill-in).
        #: Dropped once confirmed: a settled gate is not something to rejoin.
        self._understandings: dict[str, dict[str, Any]] = {}

    # --- reads ---

    def snapshot(self, device_id: str) -> tuple[int, list[GoalSummary]]:
        """Every goal of a session, newest first."""
        goals = sorted(
            self._goals.get(device_id, {}).values(),
            key=lambda g: g.updated_at,
            reverse=True,
        )
        return self._seq.get(device_id, 0), goals

    def cached_plan(self, goal_id: str) -> dict[str, Any] | None:
        return self._plans.get(goal_id)

    def cached_status(self, goal_id: str) -> dict[str, Any] | None:
        return self._statuses.get(goal_id)

    def cached_understanding(self, goal_id: str) -> dict[str, Any] | None:
        return self._understandings.get(goal_id)

    def forget_goal(self, device_id: str, goal_id: str) -> None:
        self._goals.get(device_id, {}).pop(goal_id, None)
        self._plans.pop(goal_id, None)
        self._statuses.pop(goal_id, None)
        self._understandings.pop(goal_id, None)

    # --- the fold ---

    def on_understanding(self, device_id: str, goal_id: str, understanding: dict[str, Any],
                         client_ref: str | None) -> GoalSummary:
        """The cloud has read the goal back and wants a human to confirm it.

        THE CARD IS BORN HERE, not at dispatch. A goal stuck behind a person is
        precisely what a board exists to surface; creating the card only once the
        contract dispatches means the single state where a human is the blocker is
        the one state the board cannot show. Before this, a goal started from the
        board sat on its optimistic placeholder forever, with nothing anywhere saying
        it was waiting on you.

        The understanding dict carries title/domain/time_window already — the same
        keys `_subtitle` reads off a contract — so the card is complete from the
        first frame rather than filling in later.
        """
        self._understandings[goal_id] = understanding
        window = understanding.get("time_window") or {}
        return self._put(device_id, GoalSummary(
            goal_id=goal_id,
            client_ref=client_ref,
            title=_title(understanding.get("title") or understanding.get("objective") or "New goal"),
            subtitle=_subtitle(understanding),
            domain=understanding.get("domain") or "",
            state="waiting",
            task_status="interpreting",
            eta=window.get("end") or None,
            next_step="Confirm what the agent understood",
            # An alert, because this one genuinely wants a person. The board is
            # read-mostly by design, so the card's job is to say so and hand off to
            # the chat UI — not to grow its own confirm button.
            alerts=GoalAlerts(count=1, severity="warn"),
            updated_at=_now(),
        ))

    def on_goal_created(self, device_id: str, goal_id: str, contract: dict[str, Any],
                        client_ref: str | None) -> GoalSummary:
        """A dispatch went out: the goal exists and is being planned.

        Overwrites whatever the understanding gate put up, which is what clears that
        gate's alert — confirming IS the resolution.
        """
        self._understandings.pop(goal_id, None)
        existing = self._get(device_id, goal_id)
        window = contract.get("time_window") or {}
        summary = GoalSummary(
            goal_id=goal_id,
            # The post-confirmation dispatch path has no client_ref to hand us; keep
            # the one the card was born with so a UI can still tie it to its submit.
            client_ref=client_ref or (existing.client_ref if existing else None),
            title=_title(contract.get("title") or contract.get("objective") or "New goal"),
            subtitle=_subtitle(contract),
            domain=contract.get("domain") or "",
            state="waiting",
            task_status="planning",
            eta=window.get("end"),
            next_step="Working out the steps…",
            updated_at=_now(),
        )
        return self._put(device_id, summary)

    def on_task_update(self, device_id: str, goal_id: str, payload: dict[str, Any]) -> GoalSummary | None:
        """The device's task ledger moved — the board's numbers come from here.

        progress/pending/next_step are taken verbatim: the device derives them from
        real task state, and re-deriving them here would be a second source of truth
        that drifts.
        """
        summary = self._get(device_id, goal_id)
        if summary is None:
            return None

        updates: dict[str, Any] = {
            "progress_pct": payload.get("progress_pct", summary.progress_pct),
            "pending_tasks": payload.get("pending_tasks", summary.pending_tasks),
            "updated_at": _now(),
        }
        # next_step goes null once nothing is left; don't overwrite a real step with
        # None just because this frame didn't carry one.
        if payload.get("next_step") is not None or payload.get("pending_tasks") == 0:
            updates["next_step"] = payload.get("next_step")

        if payload.get("failure_reason"):
            updates["alerts"] = _bump(summary.alerts, "danger")

        # A finished task IS the activity line — "Grocery delivery confirmed". Task
        # titles are already written as short human phrases by the planner, which is
        # why they read well here and the plan's explanation (a paragraph) does not.
        if payload.get("state") == "completed" and payload.get("title"):
            updates["activity"] = _push(summary.activity, payload["title"])

        return self._put(device_id, summary.model_copy(update=updates))

    def on_plan_ready(self, device_id: str, goal_id: str, payload: dict[str, Any]) -> GoalSummary | None:
        """A plan arrived: cache it for drill-in, and reflect what it says."""
        self._plans[goal_id] = payload
        summary = self._get(device_id, goal_id)
        if summary is None:
            return None

        alerts = summary.alerts
        state = "waiting"
        if (payload.get("safety") or {}).get("gate") == "blocked":
            # Blocked by the house rules — never, not "not yet". Worth an alert.
            alerts = _bump(alerts, "danger")
            state = "at_risk"
        precheck = payload.get("precheck") or {}
        if precheck and precheck.get("ok") is False:
            # The world isn't ready. Not the user's fault and not forever, so it is
            # 'waiting', not 'at_risk' — the distinction the Pre-check Engine exists
            # to make, carried through to the chip a person actually reads.
            alerts = _bump(alerts, "warn")

        needs_approval = any(p.get("requires_approval") for p in payload.get("proposals") or [])
        return self._put(device_id, summary.model_copy(update={
            "task_status": "awaiting_approval" if needs_approval else "monitoring",
            "state": state if state == "at_risk" else ("waiting" if needs_approval else "on_track"),
            "alerts": alerts,
            # Deliberately NOT payload["explanation"]. That field is the planner's
            # RATIONALE — a paragraph — and clipping it to fit a card yields "The
            # 7-day vegetarian dinner plan leverages existing inven…", which is the
            # exact failure `_title` exists to prevent. Activity is filled by
            # completed tasks instead; until one lands, the card shows none, which is
            # honest: nothing has happened yet.
            "updated_at": _now(),
        }))

    def on_status(self, device_id: str, goal_id: str, frame: dict[str, Any]) -> GoalSummary | None:
        """A status tick: executions, monitoring notes, completion."""
        self._statuses[goal_id] = frame
        summary = self._get(device_id, goal_id)
        if summary is None:
            return None

        payload = frame.get("payload") or {}
        task_status = frame.get("task_status") or summary.task_status
        alerts = summary.alerts

        # A deferred effect is the world's fault, not the plan's — warn, not danger.
        if any(e.get("result") == "deferred_precheck" for e in payload.get("executed") or []):
            alerts = _bump(alerts, "warn")

        done = task_status == "done"
        return self._put(device_id, summary.model_copy(update={
            "task_status": task_status,
            "state": "completed" if done else _state_for(task_status, alerts),
            "progress_pct": 100 if done else summary.progress_pct,
            "pending_tasks": 0 if done else summary.pending_tasks,
            "next_step": None if done else summary.next_step,
            "alerts": alerts,
            "activity": _push(summary.activity, payload.get("note")),
            "updated_at": _now(),
        }))

    def on_proposal(self, device_id: str, goal_id: str, frame: dict[str, Any]) -> GoalSummary | None:
        """An adaptation is waiting on the user — the board's 'Alerts'."""
        summary = self._get(device_id, goal_id)
        if summary is None:
            return None

        payload = frame.get("payload") or {}
        return self._put(device_id, summary.model_copy(update={
            "task_status": "adapting",
            "state": "waiting",
            "alerts": _bump(summary.alerts, "danger"),
            "next_step": payload.get("action") or summary.next_step,
            "activity": _push(summary.activity, payload.get("action")),
            "updated_at": _now(),
        }))

    def on_device_offline(self, device_id: str) -> list[GoalSummary]:
        """The device went away: every unfinished goal here is at risk.

        Nothing will answer for them, so saying so beats leaving cards that look
        alive — the same reasoning as the hub's existing device-offline notice.
        """
        changed = []
        for summary in list(self._goals.get(device_id, {}).values()):
            if summary.state == "completed":
                continue
            changed.append(self._put(device_id, summary.model_copy(update={
                "state": "at_risk",
                "alerts": _bump(summary.alerts, "danger"),
                "activity": _push(summary.activity, "The Family Hub went offline"),
                "updated_at": _now(),
            })))
        return changed

    # --- internals ---

    def _get(self, device_id: str, goal_id: str) -> GoalSummary | None:
        summary = self._goals.get(device_id, {}).get(goal_id)
        if summary is None:
            # A frame for a goal the board never saw start (e.g. after a cloud
            # restart, before M6's rehydration). Log rather than fabricate a card
            # from a fragment — a half-built summary is worse than a missing one.
            logger.debug("board_unknown_goal device_id=%s goal_id=%s", device_id, goal_id)
        return summary

    def _put(self, device_id: str, summary: GoalSummary) -> GoalSummary:
        self._goals.setdefault(device_id, {})[summary.goal_id] = summary
        self._seq[device_id] = self._seq.get(device_id, 0) + 1
        return summary

    def seq(self, device_id: str) -> int:
        return self._seq.get(device_id, 0)


def _state_for(task_status: str, alerts: GoalAlerts) -> str:
    """The board's four chips, from the lifecycle + alerts.

    'waiting' covers anything waiting on a HUMAN or on the WORLD — an open gate, an
    approval, a queued plan. Deliberately: from a glanceable board, "someone needs to
    do something" is one idea, and splitting it would make the user learn a taxonomy
    to read a card.
    """
    if task_status in ("awaiting_approval", "adapting", "created", "interpreting"):
        return "waiting"
    if alerts.severity == "danger":
        return "at_risk"
    return "on_track"


def _bump(alerts: GoalAlerts, severity: str) -> GoalAlerts:
    """One more alert. Danger sticks — a warn must never downgrade a danger."""
    worst = "danger" if "danger" in (alerts.severity, severity) else severity
    return GoalAlerts(count=alerts.count + 1, severity=worst)


def _push(activity: list[str], line: str | None) -> list[str]:
    """The last two human-readable things that happened; the board renders "a • b"."""
    if not line:
        return activity
    trimmed = line.strip()
    if len(trimmed) > 60:
        trimmed = trimmed[:57].rstrip() + "…"
    return ([trimmed] + [a for a in activity if a != trimmed])[:2]


def _title(text: str) -> str:
    """The card's headline.

    Prefers the interpreter's short noun phrase ("Birthday Party Preparation") over
    the objective, which is a SENTENCE — truncating one gives "Prepare my son's
    birthday party next Sund…", which is what a card should never look like. Falls
    back to a trimmed objective when a v2-era contract has no title.
    """
    text = text.strip()
    return text if len(text) <= 42 else text[:41].rstrip() + "…"


def _subtitle(contract: dict[str, Any]) -> str:
    """"Sun, Jun 22 • 20 Guests" — the date it's aiming at, plus a notable scope fact.

    Assembled from the contract rather than hardcoded per domain: scope is
    domain-flexible by design, so the board reads whatever numeric fact is there
    instead of knowing what a guest is.
    """
    parts: list[str] = []
    end = (contract.get("time_window") or {}).get("end")
    if end:
        try:
            parts.append(datetime.fromisoformat(end).strftime("%a, %b %-d"))
        except ValueError:
            parts.append(end)
    scope = contract.get("scope") or {}
    for key, value in scope.items():
        if isinstance(value, int) and value > 1:
            parts.append(f"{value} {key.replace('_', ' ').title()}")
            break
    return " • ".join(parts)
