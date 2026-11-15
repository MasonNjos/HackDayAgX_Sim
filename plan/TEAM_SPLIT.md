# Two-person split (due 5:00 PM)

Companion to [PLAN.md](PLAN.md). About 5 hours from a noon start. The split follows the code boundary so the two of you rarely touch the same file:

- **Person A: World and Command Center.** Simulator, scenario, baseline, metrics, server, dashboard, demo script. Owns "the thing judges see".
- **Person B: Orchestrator.** Fleet Agent, Safety Agent, explanations, "why?". Owns "the thing judges score".

Nav Agent goes to A because the sim needs routes to move tractors. B only calls it.

## File ownership

| Person A | Person B | Shared (change only together) |
|---|---|---|
| `orchestrator/sim.py` | `orchestrator/fleet_agent.py` | `orchestrator/models.py` |
| `orchestrator/nav_agent.py` | `orchestrator/safety_agent.py` | `scenarios/farm.json` |
| `orchestrator/baseline.py` | `orchestrator/explain.py` | `README.md` (last hour) |
| `orchestrator/metrics.py`, `eventlog.py` | `tests/test_invariants.py`, `tests/test_scenarios.py` | |
| `server.py`, `dashboard/index.html`, `run_demo.py` | | |

Git: each person works on their own branch (`a/...`, `b/...`) and merges to `main` at every sync point. Changes to a shared file go in a small commit of their own, announced to the other person first.

## 12:00–12:30: write the contract together (do not skip)

This half hour is what lets you work in parallel for the next four hours. Write and commit these on `main` before splitting:

1. **`models.py`**: dataclasses for `Tractor`, `Field`, `Job`, `Event`, `Depot`, plus `WorldState` (all tractors, fields, jobs, graph, current tick, blocked edges).
2. **The dispatcher interface.** The sim talks to both dispatchers only through this:

   ```python
   @dataclass
   class Stop:
       kind: str            # "job" | "refuel"
       target: str          # job id or depot id

   @dataclass
   class Plan:
       queues: dict[str, list[Stop]]   # tractor id -> ordered stops
       explanation: str                # plain-language summary of what changed
       warnings: list[str]             # e.g. deadline risks, unassignable jobs + reason

   class Dispatcher(Protocol):
       def plan(self, state: WorldState, trigger: Event | None) -> Plan: ...
   ```

   The sim calls `plan()` at start, on every event, and whenever a tractor finishes its queue. It then executes the head of each queue. It never interrupts an in-progress job unless that tractor has broken down.
3. **Nav signature** (A implements; B relies on it):
   `nav.route(state, from_node, to_node) -> (path: list[str], distance: float) | None` (None means unreachable).
4. **`farm.json` skeleton**: 3 tractors, 8 fields, ~10 jobs, nodes/edges with one "bridge", one depot, and scripted events (a breakdown at around t=40, plus a blocked bridge and a priority change). Use rough numbers; A tunes them later.
5. A stub `FleetAgent.plan()` that returns empty queues, so A's sim runs from minute one.

## Block 1: 12:30–2:00, build the core in parallel

**A**
- `nav_agent.py`: Dijkstra over the graph, skipping blocked edges.
- `sim.py`: tick loop, movement along routes, fuel burn, work progress on `remaining`, breakdown and other events, calling `plan()`.
- `eventlog.py`: one JSONL line per event, assignment, plan change and job completion.
- `baseline.py` (FCFS) and `metrics.py`: jobs completed, travel time, idle time, deadlines met, violations.
- Stub `run_demo.py` that runs the baseline and writes `out/comparison.md`.

**B**
- Shared predicates in `safety_agent.py`: `compatible(tractor, job)`, `fuel_ok(tractor, stops, state)`, `route_ok(...)`, and `validate(plan, state) -> list[violation]`.
- `fleet_agent.py`: eligibility filter (using the predicates above), then feasibility/ETA, then greedy assignment by urgency and lowest marginal cost. Unassignable jobs go into `warnings` with a reason.
- `tests/test_invariants.py`: run the sim over random seeds and breakdown times, and call `validate()` after every tick. Use A's sim once it lands. Until then, test `plan()` on hand-built states.

**2:00 sync (15 min):** merge both branches. `python run_demo.py` should now run baseline and coordinator side by side, with numbers in `comparison.md`. Fix interface mismatches together.

## Block 2: 2:15–3:30, must-haves

**A**
- `server.py` (FastAPI + uvicorn): start the sim in a background thread from the app's `lifespan`, with a `threading.Lock` around state. Write endpoints as plain `def`, not `async def`, so FastAPI runs them in its threadpool and a blocking lock can't stall the event loop. Serve `dashboard/` with `StaticFiles`. Routes: `GET /state`, `POST /event`, `POST /jobs`, `GET /why/<job>`, `POST /speed`.
- `dashboard/index.html`: grid map with fields, tractors, routes and fuel bars; job table; explanation panel; event log; buttons for Break down, Add job/Submit job list, and speed.

**B**
- Breakdown recovery: release the broken tractor's queue, keep its partial `remaining`, run a full replan, validate, then commit.
- `explain.py`: diff the old and new plans to produce text like "T2 broke down at t=40 while spraying Field C (60% done). Its remaining work went to T1…"
- Store a scoring record per assignment (eligible tractors, why the others were rejected, cost terms), which `/why/<job>` renders.
- Scenario test: breakdown of the only compatible tractor, so the job is reported unassignable with a reason.

**3:30 sync:** all must-haves should work end to end in the dashboard. Click "Break down" and check that the explanation appears, the work moves to another tractor, and nothing is duplicated.

## Block 3: 3:30–4:15, stretch goals (take them in this order, stop at 4:15)

**B**
1. Blocked route: replan with the edge removed.
2. Priority change: reorder queued jobs only.
3. Fuel planning: insert refuel stops when a tractor would drop below its reserve.
4. 2-opt/insertion improvement on each tractor's queue.

**A**
1. Dashboard buttons for Block route, Change priority and Why?
2. Tune `farm.json` so the comparison is honest but clear: the coordinator should win on jobs completed, travel and idle time, with 0 violations.
3. Optional: Claude API prose for "why?", falling back to the template text.

## 4:15 code freeze. 4:15–4:45: hand-in

- **A:** run `python run_demo.py` on clean `main` and commit `out/event_log.jsonl` and `out/comparison.md`.
- **B:** write the README: how to run, "own simulation (not AgOpenWeb)", design summary, scenario description, and the comparison table.
- **Both:** rehearse the 2–3 minute demo twice. Order: start the run, trigger a breakdown, show the explanation, ask "why?", show the comparison table. Decide who presents and who clicks.

**4:45:** submit the code link and hand-in files. The last 15 minutes are buffer.

## If you fall behind

Cut from the bottom of this list first. Never cut the must-haves.

1. Claude API prose
2. 2-opt improvement
3. Fuel/refuel stop insertion (keep the fuel *eligibility* check, which is a must-have)
4. Priority change
5. Blocked route

If the 2:00 merge is late, A keeps building the dashboard against the stub dispatcher. If the 3:30 checkpoint fails, both of you drop stretch work and fix breakdown recovery and the explanation together.
