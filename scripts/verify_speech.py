"""v11 gate — the voice says the right thing, and its absence costs nothing.

Run:  python scripts/verify_speech.py    (no API key, no network — nothing here calls
                                          fish.audio, and that is deliberate)

WHAT THIS PROTECTS. Speech is the one feature in this repo allowed to fail silently,
which makes it the one feature whose failures nobody notices. The assertions are
therefore mostly about the silences:

  * WITH NO KEY the hub must behave exactly as it did in v10 — no ``speech`` frame at
    all. A voice-over that half-ships (a frame pointing at a URL that 503s) is worse
    than one that never shipped, because it teaches a UI to expect audio;
  * a gate with nothing worth saying says NOTHING, rather than voicing a fragment;
  * the utterance id is DETERMINISTIC per (goal, cue), because create-phase replay
    re-sends the frame and a random id would pay for the same sentence twice;
  * a URL is not a synthesis oracle — an id the cloud never minted resolves to nothing,
    so the hub's HTTP port cannot be used to spend the account's credits;
  * dates are spoken as dates. "2026-08-04" read aloud is a string of digits, and the
    ISO string is what every other surface in this system uses.

The composer is exercised against the SAME dict shape ``present_understanding`` builds,
not a hand-written one, so a field renamed in the graph fails here rather than on stage.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goalflow_cloud.graph.nodes import (  # noqa: E402
    _speak_date,
    _speak_list,
    _understanding_speech,
)
from goalflow_cloud.models.contract import Speech, SpeechPayload  # noqa: E402
from goalflow_cloud.speech import (  # noqa: E402
    lookup_utterance,
    mint_utterance,
    speech_enabled,
    utterance_id_for,
)
from goalflow_cloud.speech import cues  # noqa: E402
from goalflow_cloud.speech.client import speech_off_reason  # noqa: E402
from goalflow_cloud.speech.utterances import MAX_UTTERANCES, reset_utterances  # noqa: E402

#: The shape present_understanding hands to the interrupt — objective, window, and the
#: display-ready provenance rows behind the chips.
GOAL_GATE = {
    "objective": "Plan a week of healthy family dinners that cut food waste",
    "domain": "meal_plan",
    "time_window": {"start": "2026-08-04", "end": "2026-08-09"},
    "constraints": [
        {"id": "c1", "kind": "allergens", "label": "peanut allergy"},
        {"id": "c2", "kind": "medical", "label": "low sodium"},
    ],
    "preferences": [{"id": "s1", "label": "prefers white meat"}],
    "proposed_constraints": [],
}

CAPTURE_GATE = {
    "objective": "we've gone vegan",
    "capture_only": True,
    "constraints": [],
    "proposed_constraints": [{"id": "proposed-1", "kind": "dietary", "label": "no dairy"}],
}


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    # --- the key, and the off switch --------------------------------------------------
    previous = os.environ.pop("FISH_API_KEY", None)
    previous_switch = os.environ.pop("SPEECH_ENABLED", None)
    try:
        check(speech_enabled() is False, "with no FISH_API_KEY, speech is off")
        check("FISH_API_KEY" in speech_off_reason(),
              "and the reason SAYS the key is missing — silence is legal here, so the "
              "only thing between a quiet run and a debugging session is this string")
        os.environ["FISH_API_KEY"] = "   "
        check(speech_enabled() is False, "a whitespace-only key is not a key")

        os.environ["FISH_API_KEY"] = "test-key"
        check(speech_enabled() is True, "a key alone turns the voice on")

        # The v11.1 switch: silence WITHOUT vandalising a credential.
        for word in ("false", "FALSE", "0", "off", "no", "  False  "):
            os.environ["SPEECH_ENABLED"] = word
            check(speech_enabled() is False, f"SPEECH_ENABLED={word!r} silences the voice")
        check("SPEECH_ENABLED" in speech_off_reason(),
              "and the reason distinguishes a FLIPPED SWITCH from a missing key — "
              "otherwise the developer who set it last week hunts a key that is right there")

        for word in ("", "true", "1", "on", "yes", "anything-else"):
            os.environ["SPEECH_ENABLED"] = word
            check(speech_enabled() is True, f"SPEECH_ENABLED={word!r} leaves it on")

        # The switch must not be able to CONJURE a voice out of no key.
        os.environ["SPEECH_ENABLED"] = "true"
        os.environ["FISH_API_KEY"] = ""
        check(speech_enabled() is False, "SPEECH_ENABLED=true cannot speak without a key")
    finally:
        for name, value in (("FISH_API_KEY", previous), ("SPEECH_ENABLED", previous_switch)):
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    # --- what the gate says ----------------------------------------------------------
    spoken = _understanding_speech(GOAL_GATE)
    check(spoken.startswith("Here's what I understood."), "the read comes first")
    check(GOAL_GATE["objective"] in spoken, "the objective is spoken verbatim, not paraphrased")
    # v11.2 — THE VOICE NO LONGER ASKS. There is no speech recognition on this surface,
    # so a spoken question invites a reply into a microphone that is not listening, and
    # the two buttons underneath are already asking. The voice states; the screen asks.
    check(not spoken.rstrip().endswith("?"),
          "the goal gate does NOT ask out loud — nothing is listening for the answer")
    check("Shall I" not in spoken, "and specifically does not say 'Shall I…'")
    check("2026-08-04" not in spoken, "an ISO date is never read aloud")
    # v11.1 CUT THE WINDOW, and the measurement says why it went first: v11.0 ran 15.0s,
    # naming all four rules is 12.0s, naming only the safety-critical ones is 10.6s, and
    # a bare count is 8.2s. The window was the cheapest 3s to lose — it is on the card,
    # and the card is where the button is.
    check("August 4" not in spoken and "covers" not in spoken,
          "the date window is NOT spoken — it was the first 3s cut, and it is on the card")
    check("peanut allergy" in spoken and "low sodium" in spoken,
          "the SAFETY-critical rules are still named: a listener whose eyes are elsewhere "
          "has to hear that the allergy was understood")
    check("no pork" not in _understanding_speech({**GOAL_GATE, "constraints": [
              *GOAL_GATE["constraints"], {"kind": "dietary", "label": "no pork"}]}),
          "and the ones that cannot hurt anyone are COUNTED, not named — that is where "
          "the remaining seconds went")
    check("1 more rule" in _understanding_speech({**GOAL_GATE, "constraints": [
              *GOAL_GATE["constraints"], {"kind": "dietary", "label": "no pork"}]}),
          "counted, so nothing is silently dropped")
    check(
        "prefers white meat" not in spoken,
        "PREFERENCES ARE NOT SPOKEN — the gate is about what can block the plan, and a "
        "spoken list that mixes the two teaches the listener they are the same thing",
    )

    capture = _understanding_speech(CAPTURE_GATE)
    check("no dairy" in capture, "a capture names the rule")
    check("plan" not in capture.lower(), "a capture never promises a plan — there isn't one")
    check(capture.rstrip().endswith("?"), "a capture asks too")

    # --- the silences ----------------------------------------------------------------
    check(_understanding_speech({"objective": ""}) == "", "no objective, no sentence")
    check(
        _understanding_speech({"capture_only": True, "proposed_constraints": []}) == "",
        "a capture with nothing to capture stays quiet rather than saying half of it",
    )
    check(
        _understanding_speech({"objective": "Plan dinner", "constraints": []})
        == "Here's what I understood. Plan dinner.",
        "no constraints still yields a whole, sayable sentence",
    )

    # --- list and date helpers -------------------------------------------------------
    check(_speak_list(["a"]) == "a", "one item is just the item")
    check(_speak_list(["a", "b"]) == "a and b", "two items get an 'and', not a comma")
    check(_speak_list(["a", "b", "c"]) == "a, b and c", "three items are still a list")
    check(_speak_list(["a", "b", "c", "d"]) == "a, b, c and d",
          "ONE over the limit is named — '…and 1 more' is longer than the name it hides")
    check(_speak_list(["a", "b", "c", "d", "e"]) == "a, b, c and 2 more",
          "two over, and it summarises")
    check(_speak_list([]) == "" and _speak_list(["", ""]) == "", "empty labels are dropped")
    check(_speak_date("2026-08-04") == "August 4", "a date is a month and a day")
    check(_speak_date("2026-12-01") == "December 1", "with no leading zero")
    check(_speak_date("not-a-date") == "" and _speak_date("") == "", "an unreadable date is dropped, not guessed")

    # --- the utterance registry ------------------------------------------------------
    reset_utterances()
    first = mint_utterance("goal-1", "understanding", "Shall I plan it?")
    again = mint_utterance("goal-1", "understanding", "Shall I plan it?")
    check(first.id == again.id, "the same (goal, cue) mints the same id")
    check(first is again, "and the same utterance — a replay must not pay twice")
    check(first.id == utterance_id_for("goal-1", "understanding"), "the id is derivable, not random")

    first.audio = b"cached-mp3"
    kept = mint_utterance("goal-1", "understanding", "Shall I plan it?")
    check(kept.audio == b"cached-mp3", "re-minting identical text keeps the cached audio")
    replaced = mint_utterance("goal-1", "understanding", "Different text now.")
    check(
        replaced.audio == b"",
        "CHANGED TEXT DROPS THE AUDIO — cached bytes for a sentence we no longer say "
        "would speak something the card does not show",
    )

    check(lookup_utterance("u-never-minted-understanding") is None,
          "an id the cloud never minted resolves to nothing — the URL is not a synthesis oracle")
    check(lookup_utterance(first.id) is not None, "a minted id resolves")

    reset_utterances()
    for n in range(MAX_UTTERANCES + 5):
        mint_utterance(f"goal-{n}", "understanding", f"utterance {n}")
    check(lookup_utterance(utterance_id_for("goal-0", "understanding")) is None,
          "the registry is bounded — the oldest utterance is evicted")
    check(lookup_utterance(utterance_id_for(f"goal-{MAX_UTTERANCES + 4}", "understanding")) is not None,
          "and the newest is kept")

    # --- the frame -------------------------------------------------------------------
    frame = Speech(
        goal_id="goal-1",
        payload=SpeechPayload(
            utterance_id="u-goal-1-understanding",
            cue="understanding",
            text=spoken,
            url="/speech/u-goal-1-understanding.mp3",
        ),
    ).model_dump(mode="json")
    check(frame["type"] == "speech", "the frame discriminates on type like every other")
    check(frame["payload"]["url"].startswith("/speech/"),
          "the url is a PATH — the cloud does not know which host:port the UI reached it on")
    check("://" not in frame["payload"]["url"], "and never an absolute URL")
    check(frame["payload"]["text"] == spoken,
          "the text rides along: it is the caption, and the only thing left when synthesis fails")

    # --- v11.1: the other four cues ---------------------------------------------------
    #
    # The composing screen. Two beats, and the reason there are two and not seven is
    # measured: five of the harness's engines resolve in under 100ms while a spoken
    # sentence takes 2-5s, so narrating them is arithmetically impossible rather than
    # merely undesirable.
    held = cues.working_start([
        {"kind": "allergens", "label": "peanut allergy"},
        {"kind": "medical", "label": "low sodium"},
        {"kind": "dietary", "label": "no pork"},
    ], "meal_plan")
    # v11.6 — THE COMPOSING BEAT NO LONGER RE-NAMES THE CONSTRAINTS. Until now it did,
    # deliberately: at the gate the rules are a promise, here they are being kept. Heard
    # in a real run it is just repetition — the user read those rules on a card and
    # confirmed them with a tap seconds earlier. Asserted as an ABSENCE so the
    # well-argued original cannot quietly come back.
    for label in ("peanut allergy", "low sodium", "no pork"):
        check(label not in held,
              f"the composing beat must not say {label!r} again — the gate already did, "
              f"and the voice is serial: a sentence spent repeating the screen is a "
              f"sentence it cannot spend on anything else")
    check(cues.working_start([], "meal_plan") == "Checking your kitchen and your calendar.",
          "with no constraints it still says what it is doing")
    check(held == cues.working_start([], "meal_plan"),
          "and constraints make NO difference to it any more")
    # v11.7 — the close must outlast the goodbye, so the dwell has to KNOW how long the
    # goodbye is. SAVE_DWELL_S was 4.5s, calibrated in v11.2 against a `saved` line that
    # was one utterance; chunking then split it per sentence and the constant was never
    # revisited. Measured on a real run: approve at :21, chunk two still speaking when
    # the close fired at :25.
    check(cues.spoken_seconds(cues.saved()) > 4.5,
          "the fixed 4.5s dwell is SHORTER than the goodbye it exists to outlast — this "
          "check is why the dwell is now derived from the line rather than constant")
    check(cues.spoken_seconds(cues.saved(updating_others=True)) > cues.spoken_seconds(cues.saved()),
          "the cross-goal goodbye is the longer one and must hold the screen longer")
    check(cues.spoken_seconds("") > 1.0,
          "there is a LEAD before the first sound (synthesis + the frame's trip); an "
          "estimate that starts at zero would close the webview on the frame itself")
    check(cues.spoken_seconds("x" * 100) > cues.spoken_seconds("x" * 50),
          "and it must grow with the length, or it is a constant wearing a function's hat")

    check("rules" in cues.working_plan(),
          "the planner beat promises the rules check — the harness's whole claim, in a "
          "family's words rather than 'Safety Policy Engine'")

    # The plan. The ONE cue an LLM writes, and the one with no fallback.
    check(cues.plan_narration({"narration": "Chicken three nights."}) == "Chicken three nights.",
          "the device's narration is passed through verbatim")
    check(cues.plan_narration({}) == "" and cues.plan_narration(None) == "",
          "NO DETERMINISTIC FALLBACK — code can only count rows, and 'seven items, three "
          "needing approval' describes a data structure, not a week of dinners")
    check(cues.plan_narration({"narration": "x" * 900}) != "x" * 900,
          "an over-long narration does not go out whole")

    # The approvals. Firm by name, everything else counted.
    payload = {"proposals": [
        {"tier": "firm", "action": "place a grocery order", "args": {"estimatedTotal": 58.2}},
        {"tier": "firm", "action": "move Thursday's dinner"},
        {"tier": "light", "action": "add to the shopping list"},
        {"tier": "auto", "action": "defrost the chicken"},
        {"tier": "auto", "action": "set a reminder"},
    ]}
    spoken_approvals = cues.approvals(payload)
    check("2 things need your approval" in spoken_approvals, "firm proposals are counted")
    check("place a grocery order" in spoken_approvals, "and NAMED — they are what needs a human")
    check("$" not in spoken_approvals and "58" not in spoken_approvals,
          "NO AMOUNTS. The figure is an ESTIMATE the card prints exactly, so speaking a "
          "rounded version invites a decision on a number we just blurred")
    check("defrost the chicken" not in spoken_approvals,
          "auto proposals are never named — they already happened")
    check("quicker" not in spoken_approvals and "smaller" not in spoken_approvals,
          "and NO BOOKKEEPING: counting the light and auto rows lengthened the one "
          "utterance the user must act on with numbers they cannot act on")
    check("One thing needs your approval" in cues.approvals(
        {"proposals": [{"tier": "firm", "action": "order groceries"}]}),
        "one firm proposal is singular, not '1 things'")
    check("Nothing needs your approval" in cues.approvals(
        {"proposals": [{"tier": "auto", "action": "x"}]}),
        "an all-auto plan SAYS so — a silent plan screen and a broken voice look "
        "identical from the sofa")
    check(cues.approvals({"proposals": []}) == "", "and no proposals at all says nothing")

    # v11.2 — the composing beat must not name rules this DOMAIN does not display.
    #
    # The enforced set is deliberately never narrowed (v6), so a home-preparation goal
    # carries the household's allergens exactly like a meal goal does. Reading that block
    # aloud made a vacation goal announce "holding the peanuts and rohan low sodium" —
    # true of what is armed, absurd as a sentence, and the kind of noise that teaches a
    # listener to stop listening.
    check("kitchen" in cues.working_start([], "meal_plan"),
          "a meal goal is checking the kitchen")
    check("kitchen" not in cues.working_start([], "vacation_prep"),
          "a HOME-PREP goal is not — 'checking your kitchen' is a strange thing to say "
          "about locking up before a holiday")
    check("power" in cues.working_start([], "energy_saving"), "an energy goal reads the meter")
    check(cues.working_start([], "some_domain_coined_next_year"),
          "an unknown domain still gets a sentence — domains are coined by the interpreter, "
          "so a new one must be merely general rather than wrong")

    # v11.2 — a long narration is TRIMMED to whole sentences, never dropped.
    long_narration = "We have chicken three nights and fish on Thursday. " * 12
    trimmed = cues.plan_narration({"narration": long_narration})
    check(trimmed != "", "an over-long narration is no longer silence — the meal plan is the "
                         "wordiest domain and it kept losing its voice entirely")
    check(len(trimmed) <= cues.MAX_NARRATION_CHARS + 1, "trimmed to the budget")
    check(trimmed.rstrip().endswith("."), "and cut at a SENTENCE boundary, never mid-clause")

    # Saved.
    check("Family Board" in cues.saved(), "the close says where the goal went")
    # THE ONE CUE WITH A HARD DEADLINE: the chat UI unmounts at MIN_SAVING_MS = 3800ms,
    # taking the audio with it. Measured at ~16.8 chars/second in the demo voice, so the
    # budget is ~60 characters. The first version was 95 and was cut off mid-word on
    # every run — which does not read as a timing bug, it reads as a crash.
    check(len(cues.saved()) <= 60,
          f"the saved line must fit the 3800ms webview close (~60 chars); it is "
          f"{len(cues.saved())}")
    check("other goals" in cues.saved(updating_others=True),
          "and when the cross-goal fan-out is running it accounts for the wait rather "
          "than promising a calm that has not started")

    # --- emotion tags -----------------------------------------------------------------
    check(cues.apply_emotion("Your week's ready.", "plan").startswith("[excited]"),
          "the plan is excited — there is genuinely good news")
    check(cues.apply_emotion("Here's what I understood.", "understanding").startswith("[warm]"),
          "the gate is warm, NOT excited: enthusiasm over a peanut allergy reads as a "
          "system that does not understand what it is holding")
    check(cues.strip_tags("[warm] Here's what I understood.") == "Here's what I understood.",
          "TAGS NEVER REACH THE CAPTION — payload.text is what a screen reader announces")
    check(cues.strip_tags("[a] one [b] two") == "one two", "every tag, not just the first")
    check(cues.apply_emotion("[sad] already tagged", "plan") == "[sad] already tagged",
          "an explicit tag is never double-prefixed")
    for cue_name in ("understanding", "working_start", "working_plan", "plan", "approvals", "saved"):
        tagged = cues.apply_emotion("Some words.", cue_name)
        check(tagged.startswith("["), f"cue {cue_name!r} has an emotion")
        check(cues.strip_tags(tagged) == "Some words.", f"and cue {cue_name!r} strips clean")

    # --- v11.5: COMBINED tags -----------------------------------------------------------
    #
    # fish.audio takes up to three markers per sentence and charges nothing for them —
    # stripped before synthesis, outside the token limit, no added latency. So the only
    # reason to ship one tag was that nobody had read the docs.
    #
    # What this guards is the two ways the table goes wrong: a marker leaking into the
    # caption (now that there are twice as many of them), and the tag list quietly
    # growing past the vendor's own ceiling.
    for cue_name, tags in cues.CUE_EMOTION.items():
        check(len(tags) <= 3,
              f"cue {cue_name!r} carries {len(tags)} markers — fish.audio recommends at "
              f"most 3 per sentence")
        check(len(set(tags)) == len(tags), f"cue {cue_name!r} repeats a marker")
        tagged = cues.apply_emotion("Some words.", cue_name)
        check(tagged == "".join(f"[{t}]" for t in tags) + " Some words.",
              f"cue {cue_name!r} must emit every marker, at the FRONT — fish's own "
              f"guidance is that sentence-level cues work best at the start")
        check(cues.strip_tags(tagged) == "Some words.",
              f"and ALL of cue {cue_name!r}'s markers are stripped from the caption")
    check(cues.CUE_EMOTION["approvals"][1] != "emphasis",
          "approvals asks the user for consent — emphatic is pushy, and a pushy ask is "
          "worse than a flat one. Confident is the register that belongs here")
    check("excited" not in cues.CUE_EMOTION["understanding"],
          "the gate is never excited, however many markers it grows")

    # --- v11.2: chunking, which is what makes the voice feel immediate ----------------
    #
    # MEASURED, same voice, same sentence: unsplit the first audio arrives after 6.6s;
    # split into four, after 1.0s, with every chunk warm by 2.5s. A browser handed a
    # chunked mp3 with no Content-Length waits for the COMPLETE body, so utterance LENGTH
    # is silence — and that silence is why the plan and approvals cues were never heard
    # at all: they were still synthesizing when the webview closed 3.8s after approval.
    gate_line = ("Here's what I understood. Plan healthy dinners for the family for the week "
                 "— holding the peanut allergy and low sodium, plus two more rules.")
    chunks = cues.split_for_speech(gate_line)
    check(len(chunks) >= 3, "a three-sentence cue splits into at least three chunks")
    check(chunks[0] == "Here's what I understood.",
          "the FIRST chunk is the short lead sentence — it is the only one whose synthesis "
          "the listener experiences as silence")
    check(len(chunks[0]) <= 40,
          f"and it must stay short: ~0.035s per character, so {len(chunks[0])} chars is the wait")
    # WORDS, not characters: an em-dash is a pause marker and the chunk boundary IS the
    # pause, so the dash itself may go. A word may not.
    words = lambda s: [w for w in re.sub(r"[^\w$]+", " ", s).split() if w]
    check(words(" ".join(chunks)) == words(gate_line),
          "NOTHING IS LOST OR REORDERED — every word survives, in order")
    check(all(len(c) <= cues.MAX_CHUNK_CHARS for c in chunks),
          "no chunk exceeds the cap, or the cap is not doing anything")

    # A long single sentence with no dash — the shape the DEVICE's model writes.
    plan_line = ("We have chicken three nights, fish on Thursday, and everything that would "
                 "have spoiled gets used up before you go away.")
    plan_chunks = cues.split_for_speech(plan_line)
    check(len(plan_chunks) >= 2,
          "a long comma-list splits too — the plan narration is model-written, so its "
          "punctuation is not ours to choose")
    check(words(" ".join(plan_chunks)) == words(plan_line), "losing no words")
    check("nights," in " ".join(plan_chunks),
          "and KEEPING the comma — splitting on it must not swallow it, or the caption "
          "loses punctuation and the chunk loses a pause the synthesiser honours")
    # The FIRST chunk is the one that must be under the cap: it is the only wait the
    # listener experiences. A tail piece with nowhere left to split may run over.
    check(len(plan_chunks[0]) <= cues.MAX_CHUNK_CHARS,
          f"the first chunk is under the cap ({len(plan_chunks[0])} chars)")

    check(cues.split_for_speech("") == [] and cues.split_for_speech("   ") == [],
          "empty text yields no chunks, not one empty chunk")
    check(cues.split_for_speech("Just one short line.") == ["Just one short line."],
          "a short cue is left ALONE — chunking a 20-character sentence would add a round "
          "trip to save nothing")
    check(len(cues.split_for_speech("Saved. Ha.")) == 1,
          "a runt trailing chunk merges backwards rather than costing its own request")
    check("$124. 2 quicker" not in " | ".join(
              cues.split_for_speech("Approve the order, about $124. 2 quicker ones as well.")),
          "a sentence ending in a number splits from the next one — the boundary regex has "
          "to accept a DIGIT starting a sentence, not just a capital")

    # --- the HTTP route --------------------------------------------------------------
    #
    # Imported late and deliberately: importing the server pulls in the whole hub, and
    # everything above must be checkable without it.
    from fastapi.testclient import TestClient  # noqa: E402

    from goalflow_cloud import server  # noqa: E402

    reset_utterances()
    key_was = os.environ.pop("FISH_API_KEY", None)
    try:
        client = TestClient(server.app)
        check(client.get("/speech/u-never-minted.mp3").status_code == 404,
              "an unminted id is 404 — the route synthesizes nothing of the caller's choosing")

        utterance = mint_utterance("goal-http", "understanding", "Shall I go ahead?")
        check(client.get(f"/speech/{utterance.id}.mp3").status_code == 503,
              "a minted id with no key is 503, not a crash and not a silent 200")

        utterance.audio = b"ID3-pretend-this-is-mp3"
        cached = client.get(f"/speech/{utterance.id}.mp3")
        check(cached.status_code == 200, "cached audio is served")
        check(cached.content == b"ID3-pretend-this-is-mp3", "byte for byte")
        check(cached.headers.get("content-type", "").startswith("audio/mpeg"),
              "as audio/mpeg — a browser <audio> will not play application/octet-stream")
        check(client.get(f"/speech/{utterance.id}").status_code == 200,
              "the extension is cosmetic — the id is what resolves")
    finally:
        os.environ.pop("FISH_API_KEY", None)
        if key_was is not None:
            os.environ["FISH_API_KEY"] = key_was

    reset_utterances()
    for f in failures:
        print(f"  FAIL {f}")
    print("gate 31 (speech, 5 cues): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
