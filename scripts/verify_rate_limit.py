"""gate 37 — the cloud's 429 policy (v12.2).

Run:  python scripts/verify_rate_limit.py      (offline, no key, no network)

WHAT THIS PROTECTS. Every cloud LLM call used to run on LangChain's own retry: one retry
at `interpret_goal`, and NONE at the other two call sites. Its backoff is sub-second. The
measured recovery window on this provider is about three seconds, so a sub-second retry
fires into the same shut window, and a single 429 killed the goal before the confirmation
card was ever drawn. A user reported exactly this in a pre-demo run.

The device solved this in v11.2 and the numbers here are copied from it, so that one set
of measurements explains both tiers.

THE FAILURE THIS CANNOT SEE: whether the provider actually recovers in three seconds.
That was measured once, on the device side, and it is an assumption here.
"""

from __future__ import annotations

import sys
import time

from goalflow_cloud.graph.nodes import (
    RATE_LIMIT_BASE_S,
    RATE_LIMIT_MAX_S,
    RATE_LIMIT_RETRIES,
    TRANSIENT_RETRIES,
    invoke_llm,
    is_rate_limited,
    is_transient,
    rate_limit_delay,
)

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    if not ok:
        failures.append(what)


def expect_ok(call, what: str) -> None:
    """A call that must SUCCEED. An exception is a named failure, not a traceback.

    Without this, lowering RATE_LIMIT_RETRIES made the gate die with a stack trace
    instead of a sentence. The exit code was still non-zero, so a chain would catch it —
    but the reader got a traceback where the reason should have been.
    """
    try:
        check(call() == "ok", what)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{what} — it RAISED instead: {type(exc).__name__}: {exc}")


class _Response:
    def __init__(self, status_code: int | None = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _ApiError(Exception):
    def __init__(self, message: str, status_code: int | None = None, headers: dict | None = None) -> None:
        super().__init__(message)
        self.response = _Response(status_code, headers)


# --- 1. a 429 is recognised however the layers wrap it -------------------------------
# Matched on TEXT as well as status, because the exception TYPE depends on which layer
# raised it — httpx, openai, or LangChain wrapping either. Pinning the type is how a rate
# limit stops being recognised after a dependency bump, silently.
check(is_rate_limited(_ApiError("Error code: 429", status_code=429)), "a 429 status is a rate limit")
check(is_rate_limited(Exception("Error code: 429 - Too Many Requests")), "...and so is the text alone")
check(is_rate_limited(Exception("rate limit exceeded")), "...and the words 'rate limit'")
check(is_rate_limited(Exception("Provider returned rate-limit")), "...and the hyphenated spelling")
check(not is_rate_limited(Exception("401 Unauthorized")), "a bad key is NOT a rate limit — it must fail at once")
check(not is_rate_limited(Exception("could not parse the structured output")), "nor is a broken answer")

# --- 2. the wait is SECONDS, and it grows ---------------------------------------------
# THE NUMBERS BELOW ARE ABSOLUTE, NOT `>= RATE_LIMIT_BASE_S`.
#
# The first draft of this gate asserted `first >= RATE_LIMIT_BASE_S`, which is a
# tautology: lower the constant and the bar goes with it. Dropping the base to 50 ms —
# the exact regression this gate exists to prevent — passed cleanly. An assertion that
# reads its own subject cannot fail.
#
# 2.0s is the floor because the provider's measured recovery window is about three
# seconds. Anything faster retries into a window that has not reopened.
MEASURED_RECOVERY_FLOOR_S = 2.0

first = rate_limit_delay(1, Exception("429"))
second = rate_limit_delay(2, Exception("429"))
check(
    first >= MEASURED_RECOVERY_FLOOR_S,
    f"the first wait is at least {MEASURED_RECOVERY_FLOOR_S}s in ABSOLUTE terms. A 429 waits "
    "SECONDS; LangChain's own backoff waits milliseconds, which is wrong by a factor of a "
    f"thousand — got {first:.2f}s",
)
check(
    RATE_LIMIT_BASE_S >= MEASURED_RECOVERY_FLOOR_S,
    f"...and the CONSTANT itself is at least {MEASURED_RECOVERY_FLOOR_S}s — got {RATE_LIMIT_BASE_S}s",
)
check(first < 4.0, f"...and the first wait is not absurd either — got {first:.2f}s")
check(
    RATE_LIMIT_RETRIES >= 4,
    f"there are enough retries to outlast a few closed windows — got {RATE_LIMIT_RETRIES}",
)
check(second > first, f"the wait GROWS: {first:.2f}s then {second:.2f}s")
check(
    rate_limit_delay(20, Exception("429")) <= RATE_LIMIT_MAX_S + 0.5,
    f"and it is capped at {RATE_LIMIT_MAX_S}s — an unbounded curve is a hung demo",
)
# Jitter: two goals rate-limited by the same window must not march back in step.
waits = {round(rate_limit_delay(1, Exception("429")), 4) for _ in range(20)}
check(len(waits) > 1, "the wait carries jitter, so two goals do not retry in lockstep")

# A STATED wait always beats a guessed one. OpenRouter sends no Retry-After on this route
# (measured), but a proxy in front of it may.
stated = rate_limit_delay(1, _ApiError("429", 429, {"retry-after": "7"}))
check(abs(stated - 7.0) < 0.01, f"Retry-After is honoured — got {stated:.2f}s")
check(
    rate_limit_delay(1, _ApiError("429", 429, {"retry-after": "9999"})) <= RATE_LIMIT_MAX_S,
    "...but still capped, so a hostile header cannot stall the goal",
)
check(
    rate_limit_delay(1, _ApiError("429", 429, {"retry-after": "soon"})) >= RATE_LIMIT_BASE_S,
    "an unparseable Retry-After falls back to the curve rather than raising",
)

# --- 3. the two counters are SEPARATE -------------------------------------------------
# This is the v11.2 lesson. A 429 used to spend an ordinary attempt, so a rate-limit storm
# exhausted the budget without the model ever seeing the request.
calls = {"n": 0}
sleeps: list[float] = []
real_sleep = time.sleep
time.sleep = lambda s: sleeps.append(s)  # type: ignore[assignment]
try:
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] <= RATE_LIMIT_RETRIES:
            raise _ApiError("Error code: 429", status_code=429)
        return "ok"

    expect_ok(lambda: invoke_llm("gate", flaky), "a call that is rate-limited and then recovers RETURNS")
    check(
        calls["n"] == RATE_LIMIT_RETRIES + 1,
        f"...after exactly {RATE_LIMIT_RETRIES} retries — got {calls['n'] - 1}",
    )
    check(all(s >= RATE_LIMIT_BASE_S for s in sleeps), f"every wait was seconds-scale — got {sleeps}")

    # One 429 must not eat the transient budget, and the reverse.
    calls["n"] = 0
    sleeps.clear()

    def mixed() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _ApiError("Error code: 429", status_code=429)
        if calls["n"] == 2:
            raise Exception("Connection reset by peer")
        if calls["n"] == 3:
            raise _ApiError("Error code: 429", status_code=429)
        return "ok"

    expect_ok(lambda: invoke_llm("gate", mixed), "a 429 and a dropped socket do not exhaust each other")
    check(calls["n"] == 4, f"all four attempts ran — got {calls['n']}")

    # Over the budget, it raises. It must NOT return a fake answer.
    calls["n"] = 0
    sleeps.clear()

    def always_limited() -> str:
        calls["n"] += 1
        raise _ApiError("Error code: 429", status_code=429)

    raised = None
    try:
        invoke_llm("gate", always_limited)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check(raised is not None, "past the budget it RAISES — a fabricated intent is worse than a visible failure")
    check(calls["n"] == RATE_LIMIT_RETRIES + 1, f"and it tried {RATE_LIMIT_RETRIES + 1} times — got {calls['n']}")

    # A non-retryable error must cost NOTHING. This is the one that keeps a bad key fast.
    calls["n"] = 0
    sleeps.clear()

    def bad_key() -> str:
        calls["n"] += 1
        raise Exception("Error code: 401 - Unauthorized")

    try:
        invoke_llm("gate", bad_key)
    except Exception:  # noqa: BLE001
        pass
    check(calls["n"] == 1, f"a 401 is tried ONCE — got {calls['n']}")
    check(sleeps == [], "...and waits for nothing. A bad key must fail in a second, not in a minute.")

    # A dropped socket is retried FAST. It is not a shut window.
    calls["n"] = 0
    sleeps.clear()

    def flaky_socket() -> str:
        calls["n"] += 1
        if calls["n"] <= TRANSIENT_RETRIES:
            raise Exception("Connection aborted")
        return "ok"

    expect_ok(lambda: invoke_llm("gate", flaky_socket), "a dropped socket recovers")
    check(all(s < 2.0 for s in sleeps), f"...and it waits MILLISECONDS, not seconds — got {sleeps}")
finally:
    time.sleep = real_sleep  # type: ignore[assignment]

check(is_transient(Exception("Connection reset")), "a reset connection is transient")
check(not is_transient(Exception("401 Unauthorized")), "a bad key is NOT transient")
check(not is_transient(Exception("Error code: 400 - bad request")), "nor is a bad request")

# --- 4. every cloud LLM call goes through the policy -----------------------------------
import pathlib  # noqa: E402

source = pathlib.Path(__file__).resolve().parent.parent.joinpath(
    "src/goalflow_cloud/graph/nodes.py"
).read_text()
check(
    source.count('invoke_llm("') == 3,
    f"all THREE cloud LLM calls go through invoke_llm — found {source.count('invoke_llm(' + chr(34))}",
)
check("with timed_llm(" in source, "timed_llm still wraps each attempt, so a slow failure is still logged")

# THE TWO LAYERS BOTH STAY, and this assertion exists because I removed one and measured
# the damage. `invoke_llm` waits out a shut rate-limit window at SECOND scale. LangChain's
# own max_retries absorbs a transport hiccup inside one call at MILLISECOND scale. They do
# different jobs.
#
# v12.2 first set max_retries=0 at `interpret_goal`, arguing that only one layer should
# own retry. Measured with the live date gate (scripts/verify_dates.py, 16 real
# resolutions): 3 passes in 3 runs before, 3 in 6 with the retry gone, 3 in 3 again once
# restored. The argument was clean and the conclusion was wrong.
check(
    "max_retries=1" in source,
    "the transport-level retry at interpret_goal must stay. Removing it measurably "
    "increased date-resolution failures (3/3 -> 3/6 -> 3/3 on verify_dates). Re-run that "
    "gate SEVERAL times before changing this — one run of an LLM gate proves nothing.",
)

if failures:
    print(f"\ngate 37 FAILED — {len(failures)} check(s):\n", file=sys.stderr)
    for failure in failures:
        print(f"  x {failure}", file=sys.stderr)
    raise SystemExit(1)
print("gate 37 (cloud 429 policy) passed")
