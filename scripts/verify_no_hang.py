"""gate 33 — a dispatch is ALWAYS answered, and a failed plan is a message.

Run:  python scripts/verify_no_hang.py     (no API key, no network)

WHAT THIS PROTECTS, and it is the worst failure this system had.

A provider 429 during grounding made the device throw. Program.cs caught it, wrote
`frame handling failed for dispatch` to a log nobody watches, and told NO ONE. The
cloud's graph sat in `collect_plan` waiting for a `plan_ready` that would never arrive;
the webview sat on "grounding" forever; the create-phase bracket never closed. There is
no timeout anywhere in that chain — not in the graph, not in the hub, not in the UI — so
the goal hung until someone reloaded the page. In front of an audience that is the demo
over, and the only visible symptom is a spinner.

The 429 was the trigger; the hang was the bug, and ANY exception during planning caused
it. So the invariants here are about the answer, not about rate limits:

  * the device's failure path must produce a plan_ready that ROUTES — `precheck.ok`
    false, so the cloud holds rather than completing;
  * it must never claim a safety block, which would blame the household's rules for a
    provider's outage;
  * it must never look COMPLETE — an empty plan flowing to relay_decisions makes the
    board read "Completed" for a goal that never ran;
  * and on the create phase it must reach the user as a readable notice with a close,
    not as "Composed your plan · 0 steps", which is a confident lie rather than a stall.

The device half is checked by reading the C# — a Python gate cannot run .NET. That is a
real limit and worth naming: this pins the SHAPE and the wiring, and the device's own
`verify/` chain plus a live run cover the behaviour.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIBLINGS = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))

from goalflow_cloud.graph import nodes as graph_nodes  # noqa: E402

AGENT = SIBLINGS / "goal-flow-device-agent-ubuntu/src/GoalFlow.Device/Agent/GoalAgent.cs"
SERVER = ROOT / "src/goalflow_cloud/server.py"


def main() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    # --- the device always answers ----------------------------------------------------
    agent = AGENT.read_text() if AGENT.exists() else ""
    check(bool(agent), "could not read GoalAgent.cs — the device half is unchecked")
    if agent:
        run_async = agent[agent.index("public async Task<PlanReady> RunAsync"):][:4000]
        check("catch (Exception ex) when (ex is not OperationCanceledException)" in run_async,
              "RunAsync must CATCH — an exception escaping it reaches a frame handler that "
              "logs and tells nobody, and the UI hangs on grounding until a page reload")
        check("BuildFailureHold" in run_async,
              "...and answer with a plan_ready, not just log")
        check("OperationCanceledException" in run_async,
              "a genuine cancellation must still propagate — a shutdown is not a failed plan")

        hold = agent[agent.index("private static PlanReady BuildFailureHold"):][:2000]
        check("Ok = false" in hold,
              "the hold must set precheck.ok=false — that is what routes it to precheck_wait "
              "instead of completing the goal")
        check("SafetyGates.Passed" in hold,
              "and must report safety PASSED: claiming a block would blame the household's "
              "rules for a provider outage")
        check("TaskStatuses.Monitoring" in hold and "Done" not in hold.split("TaskStatus")[1][:60],
              "the goal is WAITING, not done")
        check("Detail =" in hold,
              "the reason must ride on a precheck RESULT row — the cloud reads the card's "
              "waiting line from results[].detail, so a verdict with no rows holds silently")

        # --- a 429 is not a modelling failure ----------------------------------------
        check("RateLimitRetries" in agent,
              "rate limits need their OWN retry counter — a 429 must not spend one of the "
              "three attempts reserved for a model returning unusable JSON")
        m = re.search(r"private const int RateLimitRetries = (\d+)", agent)
        check(bool(m) and int(m.group(1)) >= 4,
              "and enough of them: measured recovery is ~3s against a 2/4/8s backoff, so "
              "fewer than four retries can lose a demo to one busy minute")
        for site in ("compose", "grounding"):
            block = agent[agent.index(f'BackOffAsync("{site}"'):][:400]
            check("attempt--" in block or "rateLimitRetries" in block,
                  f"the {site} loop must not let a 429 consume an attempt AND grow the prompt — "
                  f"the limit is on TOKENS per window, so retrying bigger is the one thing "
                  f"guaranteed to make it worse")

    # --- the cloud turns a hold into something readable -------------------------------
    server = SERVER.read_text()
    handler = server[server.index("async def handle_plan_ready"):][:3000]
    check('precheck.get("ok") is False' in handler,
          "the hub must NOTICE a precheck-held plan before relaying it as a normal plan")
    check("create_phase_goal" in handler,
          "...and only reroute the CREATE phase — a hold on a board adaptation has no "
          "webview to close and belongs on the board")
    check("Notice(" in handler and "planning_held" in handler,
          "a create-phase hold reaches the user as a terminal NOTICE — 'Composed your plan "
          "· 0 steps' is worse than the hang it replaced")
    check("capture_notice" in handler,
          "cached, so a webview that binds late does not find an empty phase (gate 27's bug)")
    check("_close_after" in handler,
          "and the bracket CLOSES — a webview left open on a failure is the hang again, "
          "wearing a message")
    check(handler.index("return") < handler.index("PresentPlan("),
          "the held path must RETURN before the present_plan relay, or the UI gets both")

    # --- the graph still holds rather than completing ---------------------------------
    check(graph_nodes.route_on_safety({"plan": {"precheck": {"ok": False}}}) == "precheck_wait",
          "a precheck-blocked plan routes to precheck_wait")
    check(graph_nodes.route_on_safety({"plan": {"precheck": {"ok": False}},
                                       "pending_approvals": [{"x": 1}]}) == "precheck_wait",
          "...even with approvals pending — a held plan must never reach relay_decisions, "
          "which would send an empty approval and let the board read Completed")
    check(graph_nodes.precheck_wait({"plan": {"precheck": {"ok": False}}})["task_status"]
          == "monitoring",
          "and it holds at monitoring rather than done")

    for f in failures:
        print(f"  FAIL {f}")
    print("gate 33 (no silent hang): " + ("PASS" if not failures else f"FAIL: {len(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
