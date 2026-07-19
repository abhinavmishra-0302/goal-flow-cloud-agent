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
from datetime import date, datetime, timedelta, timezone
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
        #: goal_id -> the dispatched time_window {start, end} — for DAY-BASED progress
        #: (v3.2): once running, progress is how far the sim date has moved through it.
        self._windows: dict[str, dict[str, Any]] = {}
        #: device_id -> the device's current proactive suggestions (M8). NOT goals —
        #: a list of {id, kind, title, subtitle, detail, goal_text} the board renders
        #: as "Upcoming & Suggested", each acceptable into a real goal.
        self._suggestions: dict[str, list[dict[str, Any]]] = {}

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

    # --- proactive suggestions (M8) ---

    def on_suggestions(self, device_id: str, items: list[dict[str, Any]]) -> None:
        """The device re-scanned: replace the list wholesale (idempotent, like a snapshot)."""
        self._suggestions[device_id] = list(items)

    def suggestions(self, device_id: str) -> list[dict[str, Any]]:
        return list(self._suggestions.get(device_id, []))

    def take_suggestion(self, device_id: str, suggestion_id: str) -> dict[str, Any] | None:
        """Remove and return one suggestion — accepting or dismissing consumes it.

        Returning it lets the accept path read its goal_text; a dismiss ignores the
        return. Removing on BOTH keeps the list honest: an accepted suggestion has
        become a goal and a dismissed one is gone, so neither should linger as a card.
        """
        remaining = self._suggestions.get(device_id, [])
        taken = next((s for s in remaining if s.get("id") == suggestion_id), None)
        if taken is not None:
            self._suggestions[device_id] = [s for s in remaining if s.get("id") != suggestion_id]
        return taken

    def forget_goal(self, device_id: str, goal_id: str) -> None:
        self._goals.get(device_id, {}).pop(goal_id, None)
        self._plans.pop(goal_id, None)
        self._statuses.pop(goal_id, None)
        self._understandings.pop(goal_id, None)
        self._windows.pop(goal_id, None)

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
        self._windows[goal_id] = window
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

        # Anchor day-by-day progress to TODAY (monitoring begins now), not the dispatched
        # window start — an event goal's start is the event date, which would keep
        # progress at 0% until then.
        start = date.today().isoformat()
        plan_span = max((item.get("day") or 0) for item in (payload.get("plan") or [{}])) or 1
        end = date.fromisoformat(start) + timedelta(days=plan_span)

        # The horizon is the GOAL's own deadline when it has one, not the spread of the
        # plan's items. Until v3.5 the device numbered plan days by list POSITION, so the
        # span happened to equal the item count and every goal got a 5-7 day window by
        # accident. Now that days are real dates, a vacation checklist that all happens on
        # departure evening has span 1 — and a single Advance day drove the card to 100%
        # with the trip still six days out. Take whichever is later: a goal is not finished
        # before its deadline, and the window must still be long enough for the plan.
        if summary.eta:
            try:
                end = max(end, date.fromisoformat(summary.eta))
            except ValueError:
                pass  # a non-ISO eta is not a reason to lose the window

        self._windows[goal_id] = {"start": start, "end": end.isoformat()}

        alerts = summary.alerts
        needs_approval = any(p.get("requires_approval") for p in payload.get("proposals") or [])
        precheck = payload.get("precheck") or {}
        precheck_blocked = bool(precheck.get("ok") is False)
        # The failing probe's `detail` is the remediation sentence ("you are signed out
        # — sign in and this will resume"). Surfacing it as the card's next step turns a
        # blocked goal from a mystery into an instruction.
        block_reason = next(
            (r.get("detail") for r in precheck.get("results") or []
             if r.get("status") == "fail" and r.get("detail")),
            None,
        )

        # State is decided ONCE here, most-severe-first, rather than set and then
        # overwritten by a downstream ternary — which is the bug this replaces: a
        # precheck block set state="waiting" and the ternary recomputed it to
        # "on_track", so a goal the world had blocked rendered GREEN.
        if (payload.get("safety") or {}).get("gate") == "blocked":
            # Blocked by the house rules — never, not "not yet". Worth a danger alert.
            alerts = _bump(alerts, "danger")
            state = "at_risk"
        elif precheck_blocked:
            # The world isn't ready (signed out, appliance offline). Not the user's
            # fault and not forever, so 'waiting', not 'at_risk' — the exact
            # distinction the Pre-check Engine exists to make, now actually reaching
            # the chip instead of being discarded.
            alerts = _bump(alerts, "warn")
            state = "waiting"
        elif needs_approval:
            state = "waiting"
        else:
            state = "on_track"

        updates: dict[str, Any] = {
            "task_status": "awaiting_approval" if needs_approval else "monitoring",
            "state": state,
            "alerts": alerts,
            # Deliberately NOT payload["explanation"]. That field is the planner's
            # RATIONALE — a paragraph — and clipping it to fit a card yields "The
            # 7-day vegetarian dinner plan leverages existing inven…", which is the
            # exact failure `_title` exists to prevent. Activity is filled by
            # completed tasks instead; until one lands, the card shows none, which is
            # honest: nothing has happened yet.
            "updated_at": _now(),
        }
        if precheck_blocked and block_reason:
            # A blocked goal has no task DAG, so next_step would otherwise sit on the
            # "Working out the steps…" placeholder forever. Show the fix instead.
            updates["next_step"] = block_reason
        return self._put(device_id, summary.model_copy(update=updates))

    @staticmethod
    def _day_progress(window: dict[str, Any], sim_date: str | None) -> int | None:
        """DAY-BASED progress (v3.2): how far the sim date has moved through the goal's
        time window, 0–100. Deliberately CLOCK-DERIVED — the v3 rule was 'progress only
        from the task DAG'; a running goal's progress is now the calendar, so advancing a
        day moves every card. Returns None when there's no usable window/date (planning
        phase), leaving the task-DAG progress in place."""
        start, end = window.get("start"), window.get("end")
        if not (start and end and sim_date):
            return None
        try:
            s, e, d = date.fromisoformat(start), date.fromisoformat(end), date.fromisoformat(sim_date)
        except ValueError:
            return None
        span = (e - s).days
        if span <= 0:
            return None
        return max(0, min(100, round((d - s).days / span * 100)))

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
        # It also holds the goal: the effect runs when the world recovers, so the card
        # is Waiting (on the world), not On Track. _state_for only flips out of
        # on_track on a danger, so a warn-only deferral would otherwise read green —
        # the same false-green the plan-ready precheck fix above closes.
        deferred = any(e.get("result") == "deferred_precheck" for e in payload.get("executed") or [])

        # An adaptation that has been approved and actually RAN is resolved — its alert
        # has to go. _bump only ever counted upward and nothing cleared it, so approving
        # the proposal left "1 alert — tap to review" on the card for the goal's whole
        # life, and _state_for kept pinning the card to At Risk off the stale danger.
        # A finished goal likewise carries no open alerts.
        # The test is leaving "adapting", NOT having executed something: a DECLINE
        # executes nothing, so keying off `executed` alone would leave the alert standing
        # for exactly the case a user is most likely to notice — they said no, and the
        # card went on insisting it needed them.
        executed = payload.get("executed") or []
        answered = summary.task_status == "adapting" and task_status != "adapting"
        if task_status == "done" or answered or (executed and not deferred and task_status != "adapting"):
            alerts = GoalAlerts(count=0, severity=None)

        # ...and only THEN raise what this tick reports. Order matters: a deferral that
        # arrives in the same tick that answers an adaptation is a NEW problem and must
        # survive the clear above. A deferred effect is the world's fault, not the
        # plan's — warn, not danger. It also holds the goal: the effect runs when the
        # world recovers, so the card is Waiting (on the world), not On Track.
        if deferred:
            alerts = _bump(alerts, "warn")

        # Most-severe-first, so a deferral can't downgrade a goal that already has a
        # danger (an adaptation waiting on a person outranks an effect waiting on the
        # world). _state_for handles the ordinary case.
        if task_status == "done":
            state = "completed"
        elif alerts.severity == "danger":
            state = "at_risk"
        elif deferred:
            state = "waiting"
        else:
            state = _state_for(task_status, alerts)
        done = task_status == "done"
        # DAY-BASED progress (v3.2): a running goal's progress is where the sim date sits
        # in its window; a world tick that advances the day therefore moves the card. Falls
        # back to the task-DAG progress when there's no usable window/sim_date.
        day_prog = self._day_progress(self._windows.get(goal_id) or {}, payload.get("sim_date"))
        progress = 100 if done else (day_prog if day_prog is not None else summary.progress_pct)
        return self._put(device_id, summary.model_copy(update={
            "task_status": task_status,
            "state": state,
            "progress_pct": progress,
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
        # NOT pushed to `activity`: a proposal is what the agent WANTS to do, and it is
        # waiting on a person — it has not happened. Recording it as activity claimed it
        # had, so the card showed the very same sentence twice: once as "✓ done" and
        # again as "➡ next". activity stays the log of things that actually occurred;
        # the pending action belongs in next_step alone.
        return self._put(device_id, summary.model_copy(update={
            "task_status": "adapting",
            "state": "waiting",
            "alerts": _bump(summary.alerts, "danger"),
            "next_step": payload.get("action") or summary.next_step,
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
