# Plan: Autonomous Tractor Fleet Coordinator (ATC challenge)

## Context
Hackathon task (due 5:00 PM): build a coordinator that assigns jobs to 2-3 simulated tractors and re-plans on breakdowns (stretch: blocked routes, priority changes). Scored on constraint compliance, completed work vs a FCFS baseline, adaptation, efficiency (travel/idle), and explanations/demo. Judges score the coordinator, not the sim.

Decisions made: own lightweight simulator (not AgOpenWeb), algorithmic core with templated explanations (optional LLM for "why?"), Python + one static HTML dashboard. AgOpenWeb is only an optional stretch integration target. Code lives in `fleet-orchestrator/` in this repo.

Rubric weights (the point values disagree with the scales in the brief; the scales sum to 20, so assume these): Explanations & Demo 5, Constraint Compliance 4, Completed Work 4, Operational Efficiency 4, Adaptation 3. Tiebreaker: "Would a farm operator trust this plan?" Takeaways: the demo and explanations are worth as much as anything else, so the dashboard must not be left to the end; zero violations is worth 4 points, so the Safety checks and invariant tests are essential; the hand-in comparison must report exactly jobs completed, travel time and idle time.

## Layout
```
fleet-orchestrator/
  README.md                 # run steps, "own simulation", design summary
  scenarios/farm.json       # tractors, fields, jobs, scripted events (the hand-in config)
  orchestrator/
    models.py               # Tractor, Field, Job, Event, Assignment (dataclasses)
    sim.py                  # tick loop, movement, fuel burn, work progress, event injection
    fleet_agent.py          # state tracking + assignment (core)
    nav_agent.py            # travel time/route between fields, blocked-edge aware
    safety_agent.py         # validates a plan before it is committed
    explain.py              # templated plan-change + "why?" text (optional LLM hook)
    baseline.py             # FCFS dispatcher, same sim, for comparison
    metrics.py              # jobs done, deadlines met, travel, idle, fuel
    eventlog.py             # JSONL event log
  server.py                 # FastAPI (uvicorn): state JSON, POST events/jobs, /why/<job>
  dashboard/index.html      # grid map + tractor table + job table + log + buttons
  run_demo.py               # runs scripted scenario for both dispatchers, writes outputs
  tests/                    # invariants + scenario tests
  out/                      # event_log.jsonl, comparison.md (hand-in artifacts)
```

## Data model (key constraints)
- Tractor: id, location (node), implement (sprayer/mower; capabilities derived via `IMPLEMENT_JOBS`), fuel and capacity, one burn rate per active tick, speed, work rate, status (idle/travelling/working/refuelling/broken), current job. Fuel reserve is a global constant. Route progress and x/y live in the sim; travel/idle totals live in metrics.
- Field: id, grid x/y, size (work duration per implement type), road node it attaches to.
- Road network: `nodes` and `edges` (length, optional label such as "bridge") in the scenario. Travel uses shortest paths over this graph, so a blocked edge has a real effect.
- Depots: refuel/charge locations; tractors start at one. Needed for the "enough fuel to get back" check.
- Job: id, type, field, priority, deadline, remaining work, status (pending/assigned/in_progress/done/unassignable). `remaining` is a fraction of the job (0-1), so partial progress carries over between tractors whose work rates differ. Duration is computed per tractor.
- Events: breakdown(tractor, t), route_blocked(edge, t), priority_change(job, new, t), new_job.
- Sim time is discrete ticks (e.g. 1 tick = 1 sim minute), deterministic for a given seed.

## Fleet Agent (core algorithm)
1. Eligibility filter (hard constraints): implement/capability match; fuel sufficient for travel + work + return to a refuel point (reserve margin); tractor not broken.
2. Feasibility: ETA = tractor free time + travel (Nav Agent) + work duration <= deadline. Infeasible jobs are flagged "unassignable" with a reason rather than silently dropped.
3. Assignment: greedy by urgency (priority, then slack = deadline - earliest finish), choosing the tractor with lowest marginal cost (travel + idle + lateness penalty); then 2-opt/insertion improvement on each tractor's ordered queue. No exact search: the search space (3^10 assignments times queue orders) is too large to be worth the time.
4. Single source of truth: each job has exactly one owner (assignment table); a replan is computed as a whole, diffed against the current plan, then applied atomically (prevents double assignment).
5. Refuel/charge stops inserted into queues when fuel would drop below reserve (stretch: fuel planning).

## Replanning on events
- Breakdown: mark tractor broken; its in-progress job returns to the pool with `remaining` preserved (completed work stays credited); its queue is released; run full replan over unfinished jobs for the remaining tractors; Safety Agent validates; commit; emit explanation.
- Blocked route: Nav Agent recomputes paths with the edge removed; replan if ETAs/deadlines/fuel change.
- Priority change / new job: re-score and replan. Rule: never preempt a job in progress; only reorder or reassign queued jobs. This prevents thrashing and is easy to explain.
- Replanning is triggered immediately on the event tick (fast adaptation).

## Safety Agent (pre-commit checks)
No job with two owners; implement/capability match; fuel sufficient with reserve; no route through a blocked edge; completed jobs never reassigned; deadline feasibility reported (violations are warnings flagged to the operator, hard constraints reject the plan). A rejected plan falls back to the last valid plan plus a reason. The Safety Agent and the Fleet Agent's eligibility filter call the same predicate functions (one implementation), and the tests also run Safety as the invariant checker after every tick.

## Explanations
- Plan-change summary generated from the plan diff: "T2 broke down at t=40 while spraying Field C (60% done). Its remaining work was given to T1 (sprayer, 3 km away, enough fuel). T3's mowing order was unchanged. Job J4 may miss its deadline by 12 min."
- Per-assignment "why?": store the scoring record when a job is assigned (eligible tractors, why others were rejected, cost terms); `/why/<job>` renders it. Optional LLM pass (Claude API) turns the structured record into prose, with the templated text as the fallback so the demo never depends on the network.

## Baseline and metrics
`baseline.py`: first-come-first-served, jobs in submitted order, to the first compatible idle tractor, no fuel lookahead, naive reassign on breakdown. Run both on identical scenarios and seeds. If a baseline tractor runs out of fuel, it stalls in place and the stall is logged as a violation; both dispatchers are counted the same way. Design `farm.json` so the coordinator's advantages show honestly: jobs submitted far from their best-placed tractor, a tight-deadline job submitted late, one low-fuel tractor, and a mid-job breakdown. Say this in the README. Metrics: jobs completed, deadlines met, priority-weighted work, total travel distance/time, idle time per tractor, constraint violations (should be 0 for the coordinator). Output `out/comparison.md`.

## Dashboard
Single HTML page polling `/state`: grid map with fields (coloured by status/priority), tractor markers with routes and fuel bars, job table (owner, status, deadline, progress), explanation panel, event log, buttons (Break down tractor, Block route, Change priority, Add job, "Why?"). Simulation-speed control for the demo. "Add job" / "Submit job list" covers the "operator submits jobs" must-have. The server runs the sim in a background thread, with a lock around state so button events can't race a tick.

## Build order (time-boxed)
1. Models, scenario JSON (nodes/edges/depots), sim loop, event log (core of everything).
2. Baseline FCFS plus metrics, and a stub `run_demo.py` that writes `comparison.md`. This is the integration test from here on.
3. Server plus a bare dashboard (map, tractor and job tables, polling `/state`), so there is always something to demo.
4. Fleet agent with eligibility, feasibility, greedy assignment, Nav and Safety agents.
5. Breakdown recovery plus plan-diff explanations, with the explanation panel and breakdown button on the dashboard (must-haves complete here).
6. Stretch, in order of judging value: blocked routes and priority changes, fuel/refuel planning, "why?", then optional LLM prose.
7. Polish the hand-in artifacts (README, scenario, event log, comparison) and rehearse a 2-3 minute demo: start, breakdown, explanation, "why?", comparison table.
8. Only if time remains: AgOpenWeb adapter (translate assignments to its field/track commands over its WebSocket/`id|arg` command protocol; see `Shared/AgOpenWeb.RemoteWiring/RemoteServerWiring.cs`). Treat as optional; the README should state "own simulation".

## Verification
- Unit tests: invariants (single owner per job, no incompatible implements, fuel never negative, completed work never reassigned or double-counted) checked after every tick in randomised scenarios (multiple seeds, random breakdown times).
- Scenario tests: breakdown mid-job, breakdown of the only compatible tractor (job reported unassignable with the reason), blocked bridge, priority flip, low fuel.
- Demo run: `python run_demo.py` produces `out/event_log.jsonl` and `out/comparison.md`; coordinator must beat baseline on jobs done and travel/idle with 0 violations. Then `uvicorn server:app` and open the dashboard to check the visual flow end to end.

## Hand-in checklist
Code link, README (how to run; own simulation), `scenarios/farm.json`, `out/event_log.jsonl`, `out/comparison.md`.
