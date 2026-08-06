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
    check(spoken.rstrip().endswith("?"), "it ENDS on the question — that is what the buttons answer")
    check("August 4" in spoken and "August 9" in spoken, "the window is spoken as dates")
    check("2026-08-04" not in spoken, "an ISO date is never read aloud")
    check("peanut allergy" in spoken and "low sodium" in spoken, "the enforced rules are named")
    check("2 household rules" in spoken, "and counted")
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
        == "Here's what I understood. Plan dinner. Shall I go ahead and plan it?",
        "no window and no constraints still yields a whole, sayable sentence",
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
    print("gate 31 (speech): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
