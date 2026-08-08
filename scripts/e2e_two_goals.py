"""END-TO-END SCENARIO RUNNER — the two-goal demo, driven headlessly.

    python scripts/e2e_two_goals.py <device_id> [ticks]

Drives the real flow against a real cloud + real device, as a `board` surface:

    goal 1 "prepare the weekly meal plan"  -> confirm -> approve
    advance one day
    goal 2 "I will be out for Sunday and Monday" -> confirm -> approve
    advance N days, printing the board after every tick

NOT A GATE — it needs the whole stack up and it spends real LLM calls. It is the tool
for questions a gate cannot answer, because they only appear when the cloud, the device
and the clock interact: does a card retire on the right day, does a goal complete before
its work is done, does the board still hold a goal nobody is working on.

It is how the v11.2 completion bug was found and how the fix was proven over ten runs
(see `../goal-flow-device-agent-ubuntu` gate 34). It also caught the fix that was WRONG:
clamping a goal's last day to its window retired a 7-day meal plan on a 4-day window, and
only a run that watched both cards to the end showed it.

RUN IT AGAINST AN ISOLATED STACK, never a demo the user is watching:

    # hub on a spare port, scratch profile so the seed stays clean
    WS_PORT=8010 GOALFLOW_PROFILE_PATH=/tmp/scratch.json \
        .venv/bin/uvicorn goalflow_cloud.server:app --host 127.0.0.1 --port 8010

    # device pointed at it, FRESH data dir and device id PER RUN
    dotnet run --project GoalFlow.Device.csproj -- \
        --connect ws://127.0.0.1:8010/ws --data ./data-r1 --device-id r1

A reused `--data` dir leaks goal state between runs and shows up as phantom duplicate
cards on the board — every "impossible" result while writing this turned out to be that.
"""
import asyncio, json, sys, time
import websockets

URL = "ws://127.0.0.1:8010/ws"
DEVICE = sys.argv[1] if len(sys.argv) > 1 else "board-hub"
TICKS = int(sys.argv[2]) if len(sys.argv) > 2 else 8


class Run:
    def __init__(self, ws):
        self.ws = ws
        self.cards = {}
        self.sim_date = None
        self.windows = {}

    async def pump(self, until, timeout=240):
        """Read frames until `until(frame)` returns truthy; return that frame."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=deadline - time.time())
            f = json.loads(raw)
            t = f.get("type")
            if t == "board_snapshot":
                self.cards = {g["goal_id"]: g for g in f.get("goals", [])}
            elif t == "board_update":
                g = f.get("goal") or {}
                if g.get("goal_id"):
                    self.cards[g["goal_id"]] = g
            elif t == "day_advanced":
                self.sim_date = f.get("sim_date")
            elif t == "present_plan":
                w = (f.get("payload") or {}).get("time_window") or {}
                days = [r.get("day") for r in (f.get("payload") or {}).get("plan", [])]
                self.windows[f["goal_id"]] = (w, days)
            hit = until(f)
            if hit:
                return f
        raise TimeoutError("gave up")

    def board(self):
        return [(c.get("title", "")[:34], c.get("state"), c.get("progress_pct")) for c in self.cards.values()]


async def goal(run, text, label):
    await run.ws.send(json.dumps({"type": "user_goal", "text": text, "client_ref": label}))
    u = await run.pump(lambda f: f.get("type") == "understanding")
    gid = u["goal_id"]
    print(f"  [{label}] understood: {u['payload']['objective'][:56]}  domain={u['payload'].get('domain')}")
    print(f"           window={u['payload'].get('time_window')}")
    await run.ws.send(json.dumps({"type": "understanding_response", "goal_id": gid,
                                  "payload": {"confirmed": True, "accepted_constraint_ids": []}}))
    p = await run.pump(lambda f: f.get("type") == "present_plan")
    props = p["payload"].get("proposals", [])
    days = sorted({r.get("day") for r in p["payload"].get("plan", [])})
    print(f"           plan rows={len(p['payload'].get('plan', []))} days={days} proposals={len(props)}")
    await run.ws.send(json.dumps({"type": "approval", "goal_id": gid,
        "correlation_id": p.get("correlation_id", "-"),
        "payload": {"decisions": [{"proposal_id": q["proposal_id"], "approved": True} for q in props]}}))
    await run.pump(lambda f: f.get("type") == "status")
    return gid


async def tick(run, n):
    await run.ws.send(json.dumps({"type": "control", "command": "advance_day", "payload": {}}))
    await run.pump(lambda f: f.get("type") == "day_advanced", timeout=180)
    await asyncio.sleep(1.5)          # let the fold + retirement land
    await run.pump(lambda f: False, timeout=2) if False else None
    try:
        await asyncio.wait_for(run.pump(lambda f: False), timeout=2.5)
    except (asyncio.TimeoutError, TimeoutError):
        pass
    print(f"  tick {n}: sim_date={run.sim_date}  board={run.board()}")


async def main():
    async with websockets.connect(URL, max_size=None) as ws:
        run = Run(ws)
        await ws.send(json.dumps({"type": "hello", "role": "ui", "device_id": DEVICE, "surface": "board"}))
        await run.pump(lambda f: f.get("type") == "hello_ack")
        print("GOAL 1 — weekly meal plan")
        g1 = await goal(run, "prepare the weekly meal plan for the family", "g1")
        print(f"  board after g1: {run.board()}")

        print("ADVANCE one day")
        await tick(run, 0)

        print("GOAL 2 — out Sunday and Monday")
        g2 = await goal(run, "I will be out for Sunday and Monday, prepare the home", "g2")
        print(f"  board after g2: {run.board()}")
        w, days = run.windows.get(g2, ({}, []))
        print(f"  >>> g2 window={w} plan days={sorted(set(days))}")

        print(f"ADVANCING {TICKS} days")
        for n in range(1, TICKS + 1):
            await tick(run, n)
            if g2 not in run.cards and not getattr(run, "_g2_gone", False):
                run._g2_gone = True
                print(f"  *** g2 (home-away) RETIRED on tick {n} (sim_date={run.sim_date}) ***")
            if g1 not in run.cards and not getattr(run, "_g1_gone", False):
                run._g1_gone = True
                print(f"  *** g1 (meal plan)  RETIRED on tick {n} (sim_date={run.sim_date}) ***")
            if not run.cards:
                print(f"  *** BOARD EMPTY on tick {n} (sim_date={run.sim_date}) ***")
                break
        else:
            left = [(k[:8], v.get("title")) for k, v in run.cards.items()]
            print(f"  *** BUG: still on the board after {TICKS} ticks: {left} ***")

asyncio.run(main())
