"""The five things the create phase says out loud, and the rules behind them (v11.1).

THE MEASUREMENT THAT SHAPES ALL OF THIS. Timed on a real run:

    chat_ui_open -> understanding      3.13s
    Pre-Check      active -> pass      0.02s
    Capability Mgr active -> done      0.00s
    Task Manager   active -> done      2.33s
    Grounding                          10s+, 7 tool calls, 22 thinking events

**Five of the seven harness engines resolve in under 100ms, and a spoken sentence takes
2-5 seconds.** So per-engine narration is not a design option that was rejected — it is
arithmetically impossible. A voice announcing "Pre-Check cleared" would still be talking
somewhere around the Planner, describing the opening of a run whose plan is already on
screen. The same arithmetic rules out narrating the thinking stream: 22 events in ~15s,
each written for the eye.

WHAT FOLLOWS FROM THAT, and it is the rule every composer here obeys: **the voice is not
a second copy of the screen.** The screen is parallel and skimmable; the voice is serial
and slow. Its job is to say WHEN YOU ARE NEEDED and WHAT CHANGED WHILE YOU WERE NOT
LOOKING. Everything else is the screen's job, and saying it twice makes the voice noise
that people learn to talk over.

The five cues:

    understanding   the read + the rules that could hurt someone + the question   ~10s
    working_start   what it is doing, once grounding really begins                ~5s
    working_plan    what it is doing now, once the planner really begins          ~4s
    plan            what this plan IS — the one cue an LLM writes (see below)     ~8s
    approvals       what needs a human, by name if it spends money                ~8s
    saved           closure                                                       ~5s

ONE OF THESE IS NOT DETERMINISTIC. ``plan`` cannot be assembled from fields: "chicken
three nights, fish on Thursday, and everything that would have spoiled gets used up
before you go away" is a judgement about a plan, and code can only count rows. So the
DEVICE writes it, in the same compose call that produces the plan — see
``plan_narration``. Zero extra round trips, and it cannot contradict the plan because
the same model wrote both.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Emotion
# ---------------------------------------------------------------------------

#: fish.audio S2 emotion cues are `[bracket]` and free-form; S1's `(parens)` are the
#: legacy syntax and are NOT what this sends. Verified against the live API: a tagged
#: sentence returns different audio, so these are interpreted rather than politely
#: ignored — and they are not read aloud.
#:
#: EMOTION IS EARNED BY THE MOMENT, which is why this is a table and not a constant.
#: `[excited]` belongs on `plan`, where there is genuinely good news. The same tag on the
#: understanding gate — over a peanut allergy — would read as a system that does not
#: understand what it is holding, and would erode the exact trust that gate exists to
#: build. Enthusiasm about safety data is a tell.
CUE_EMOTION: dict[str, str] = {
    "understanding": "warm",
    "working_start": "thoughtful",
    "working_plan": "gentle",
    "plan": "excited",
    "approvals": "warm",
    "saved": "cheerful",
}

#: Anything in square brackets. Used to STRIP tags, never to find them.
_TAG = re.compile(r"\[[^\]]*\]")


def strip_tags(text: str) -> str:
    """Remove emotion cues, leaving the words a person would read.

    THE CAPTION AND THE AUDIO ARE NOT THE SAME STRING. `speech.payload.text` is what a
    UI shows, what a screen reader announces, and what is left when synthesis fails —
    and "[warm] Here's what I understood" is none of those things. The tagged variant
    exists only between here and fish.audio.
    """
    return re.sub(r"\s{2,}", " ", _TAG.sub("", text)).strip()


def apply_emotion(text: str, cue: str) -> str:
    """Prefix ``text`` with this cue's emotion, if it has one and none is present."""
    if not text.strip() or _TAG.search(text):
        return text
    emotion = CUE_EMOTION.get(cue)
    return f"[{emotion}] {text}" if emotion else text


# ---------------------------------------------------------------------------
# Shared phrasing
# ---------------------------------------------------------------------------

#: Constraint kinds that can HURT SOMEONE rather than merely inconvenience them.
#:
#: This is the whole of the screen-1 length decision. Naming every rule was measured at
#: 12.0s against 10.6s for these two and a count, and 8.2s for a bare count — so the
#: names are what the sentence costs, not the date window that was cut first. Naming
#: THESE is worth the seconds: a listener whose eyes are elsewhere needs to hear that
#: the allergy was understood. "No pork" and an away window do not carry that weight;
#: they are on the card, and the card is what the button is on.
SAFETY_KINDS = ("allergen", "medic", "health", "condition")


def _is_safety(kind: str, label: str) -> bool:
    haystack = f"{kind} {label}".lower()
    return any(word in haystack for word in SAFETY_KINDS)


def speak_list(items: list[str], limit: int = 3) -> str:
    """["a","b","c","d","e"] -> "a, b, c and 2 more". Oxford-free: this is speech.

    One over the limit is NAMED — "and 1 more" is longer than the name it hides and
    tells you less.
    """
    present = [item.strip() for item in items if item and item.strip()]
    named = present[: limit + 1] if len(present) == limit + 1 else present[:limit]
    remaining = len(present) - len(named)
    if not named:
        return ""
    if remaining > 0:
        return ", ".join(named) + f" and {remaining} more"
    if len(named) == 1:
        return named[0]
    return ", ".join(named[:-1]) + f" and {named[-1]}"


def _money(value: Any) -> str:
    """58.2 -> "fifty-eight dollars". Spoken money is words, and cents are noise.

    fish's `normalize` would read "$58.20" acceptably, but it cannot know that the cents
    on an ESTIMATED grocery total are false precision. A voice that says "fifty-eight
    dollars and twenty cents" about a number that will not be exactly that is claiming
    an accuracy it does not have.
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    return f"about ${amount:,.0f}"


# ---------------------------------------------------------------------------
# Cue 2 — the composing screen
# ---------------------------------------------------------------------------


def working_start(constraints: list[dict[str, Any]] | None) -> str:
    """Grounding has really begun: what it is reading, and what it is holding.

    Fired on the device's own ``harness`` beat rather than on a timer, so it can never
    describe a phase that has passed. The constraints are named a SECOND time here —
    deliberately, and it is the only repetition in the whole flow: at the gate they were
    a promise, and here they are being kept while the user watches something else happen.
    """
    safety = [
        str(row.get("label") or "").strip()
        for row in constraints or []
        if _is_safety(str(row.get("kind") or ""), str(row.get("label") or ""))
    ]
    held = speak_list(safety, limit=2)
    if held:
        return f"Checking your kitchen and your calendar, holding the {held}."
    return "Checking your kitchen and your calendar."


def working_plan() -> str:
    """The planner has really begun.

    Says a rules check is coming WITHOUT naming the Safety Policy Engine. The two-tier
    harness is what this product is, and a family should be able to hear that their
    rules are enforced rather than suggested — but "Safety Policy Engine" is our word
    for our machinery, and v9 spent a whole pass removing exactly that vocabulary from
    the screens.
    """
    return "Now putting the week together, then I'll check it against your rules."


# ---------------------------------------------------------------------------
# Cue 3 — the plan. THE ONE AN LLM WRITES.
# ---------------------------------------------------------------------------

#: Longest narration we will speak from the device. A model told "two short sentences"
#: mostly obliges, but "mostly" is not a contract, and an unbounded string here is a
#: paragraph read aloud at a person who has been waiting.
MAX_NARRATION_CHARS = 260


def plan_narration(payload: dict[str, Any] | None) -> str:
    """What the device said about its own plan, or "" — never a template.

    WHY THERE IS NO FALLBACK. Every other composer here degrades: no constraints, fewer
    words. This one does not, because the deterministic version is worthless — code can
    say "seven rows, three needing approval", which is a description of a data structure,
    not of a week's dinners. If the model did not write a narration, the screen still
    shows the whole plan and the voice has nothing to add. Silence beats counting.

    Long narrations are DROPPED rather than truncated: a sentence cut mid-clause is
    worse than no sentence, and the plan is on screen either way.
    """
    narration = str((payload or {}).get("narration") or "").strip()
    if not narration:
        return ""
    if len(narration) > MAX_NARRATION_CHARS:
        logger.info("plan_narration_dropped chars=%d limit=%d", len(narration), MAX_NARRATION_CHARS)
        return ""
    return narration


# ---------------------------------------------------------------------------
# Cue 4 — the approvals
# ---------------------------------------------------------------------------


def approvals(payload: dict[str, Any] | None) -> str:
    """What needs a human — FIRM tier by name, everything else counted.

    The tiers already encode the judgement, so the voice follows them rather than
    inventing its own: `firm` spends money or cannot be undone, `light` is a quick yes,
    `auto` has already happened. Naming every proposal turns a six-item plan into a
    twenty-second recital; naming only the count makes the listener hunt the screen for
    what it meant. Naming what spends money, and counting the rest, is the split the
    plan itself already draws.

    Sent as its OWN utterance rather than appended to the plan narration, even though
    they land on the same screen (ProposalList renders inside PlanCard): two utterances
    can be scheduled, and this one can be dropped when the user starts tapping.
    """
    proposals = (payload or {}).get("proposals") or []
    firm, light, auto = [], 0, 0
    for proposal in proposals:
        tier = str(proposal.get("tier") or "").lower()
        if tier == "firm":
            firm.append(proposal)
        elif tier == "auto":
            auto += 1
        else:
            light += 1

    parts: list[str] = []
    if firm:
        named = []
        for proposal in firm:
            action = str(proposal.get("action") or "").strip()
            amount = _money((proposal.get("args") or {}).get("estimatedTotal"))
            named.append(f"{action}, {amount}" if amount and action else action or amount)
        count = len(firm)
        parts.append(
            f"{'One thing needs' if count == 1 else f'{count} things need'} your approval: "
            f"{speak_list(named, limit=2)}."
        )

    quick = light
    if quick:
        parts.append(
            f"{'One' if quick == 1 else str(quick)} quicker "
            f"{'one' if quick == 1 else 'ones'} to okay as well."
        )
    if auto and not parts:
        # Nothing to ask about at all — say so rather than saying nothing, because a
        # silent plan screen and a broken voice look identical from the sofa.
        return "Nothing needs your approval — I've handled it all."
    if auto:
        parts.append(f"{auto} smaller {'one' if auto == 1 else 'ones'} I've already handled.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Cue 5 — saved
# ---------------------------------------------------------------------------


def saved(updating_others: bool = False) -> str:
    """The goal is on the board and this surface is about to close.

    THIS CUE HAS A HARD DEADLINE THE OTHERS DO NOT: the chat UI's `MIN_SAVING_MS` is
    3800ms, and the webview is unmounted by Bixby on `chat_ui_close` — which kills the
    document and the audio with it. The first version of this line measured **4.6s**, so
    every single run would have ended on "...tell you if anything cha—". A voice cut off
    mid-word does not read as a timing bug; it reads as a crash.

    Shortened to 3.1s rather than lengthening the dwell. The saving screen's timing was
    tuned in v7 against what a person needs to see, and slowing the product down so the
    voice can finish a sentence is the wrong way round — the sentence should be shorter.

    ``updating_others`` is the v7 cross-goal moment: approving this goal changed the
    household and other goals are being re-planned. That path holds the surface for
    20-30s, so it has room to say more — and it must, because a voice promising to
    "watch from here" while the screen is visibly still working describes a calm that
    has not started.
    """
    if updating_others:
        return "Saved to your Family Board. I'm updating your other goals to match."
    return "Saved to your Family Board. I'll watch it from here."
