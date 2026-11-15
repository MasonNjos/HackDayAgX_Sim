# Autonomous Tractor Fleet Coordinator

A fleet of three simulated tractors works a farm for one day with no human operator. **Claude
plans the work** (which tractor does which job, in what order, when to refuel). A **Safety agent**
must approve every plan before it runs, and a **Maintenance agent** sends repair crews when
something breaks. When a tractor breaks down, a road is blocked, a priority changes or a new
job arrives, the fleet replans immediately and explains why in plain language. An operator can
still step in from the dashboard, but nothing needs a click.

**This is our own simulation, not AgOpenWeb.** The simulator, scenario, coordinator and dashboard
were all built for this hackathon.

## Quick start

Everything runs in Docker, so you need nothing else installed. From `fleet-orchestrator/`:

```bash
cp example.env .env           # then put your key in .env: ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build     # dashboard: http://localhost:5173   API: http://localhost:8000/api/state
```

In the dashboard, press **▶ Start**. With `COORDINATOR=llm` in `.env` the run starts on Claude. To
start over with another coordinator, pick it in the **coordinator** dropdown and press **Reset**:
**Claude**, **Greedy** (the deterministic planner) or **FCFS baseline** (first come, first served).
A badge in the explanation panel shows which coordinator made the plan the tractors are following.

**Map or Satellite.** The farm map has two views; the toggle above it remembers your choice.
**Map** (the default) is a plain diagram and works offline. **Satellite** draws the same farm on
real satellite imagery of the Imperial Valley, California (Esri World Imagery, via Leaflet): every
farm road sits on a real farm road, so tractors drive along real roads, and the Alamo River runs
under the bridge and the south road. Both views work the same way: click a road to block or reopen
it, click a field for "why?". Satellite needs an internet connection for the imagery, and it is
drawn at about 1.6× the sim's distances (the real roads are farther apart than the scenario's).

**No API key?** Everything still works: the coordinator falls back to the greedy planner, with the
same safety checks, and says so in the dashboard's warnings.

Other commands (also from `fleet-orchestrator/`):

| What | Command |
|---|---|
| Run the tests (offline, no key needed) | `docker compose run --rm app pytest` |
| Baseline vs coordinator comparison, writes `out/` | `docker compose run --rm app python run_demo.py` (add `-e COORDINATOR=llm` after `run` to include Claude) |
| One URL, built dashboard (for judges) | `docker build -t fleet . && docker run --rm -p 8000:8000 fleet`, then http://localhost:8000. To use Claude, add `--env-file .env` (only if `.env` exists). |

Rebuild (`--build`) only after changing `requirements.txt` or `dashboard/package.json`.

### Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (empty) | Claude credentials. Empty means the greedy planner runs. |
| `COORDINATOR` | `greedy` | `llm` to let Claude plan (dashboard and `run_demo.py`). |
| `LLM_MODEL` | `claude-opus-5-5` | Claude model. |
| `LLM_EFFORT` | `medium` | `low` gives faster replans. |
| `LLM_TIMEOUT` / `LLM_BUDGET` | `45` / `90` s | Per request / per replan; past the budget the greedy plan is used. |
| `LLM_MAX_TURNS` | `8` | Tool-use turns per replan. |
| `LLM_MAX_TURNS_START` / `LLM_BUDGET_START` | `14` / `180` s | Turns and time for the start-of-day route plan. |
| `REPAIR_TRACTOR_MIN` | `5` | Ticks until a broken tractor is repaired. |
| `REPAIR_ROAD_MIN` | `10` | Ticks until a blocked road or bridge reopens. |

`.env` is git-ignored; `example.env` is the template.

## How it works

```
 scripted events / dashboard clicks
                 |
                 v
   Simulator  -- every event --> Maintenance agent: books a repair (tractor 5 ticks, road 10 ticks)
   (1 tick =        |
    1 minute)       v
             Fleet coordinator  <-- brief: tractors, jobs, roads, repairs on the way, greedy plan
              (Claude)            (start of day: greedy score only; Claude routes from scratch)
                                  tools: evaluate_plan, evaluate_queue (ETAs, fuel), submit_plan
                    |
                    v
             Safety agent: validate() -- rejected? errors go back to Claude to fix
                    |                   -- still failing / no key / API error? greedy plan
                    v
             plan committed + plain-language explanation + "why?" record per job
```

| Agent | File | Role |
|---|---|---|
| **Fleet coordinator (Claude)** | `orchestrator/llm_agent.py` | **Start of day:** gets the whole job list and builds every tractor's route from scratch (only the greedy plan's score is shown, not its routes), compares candidate plans with `evaluate_plan` (km, lateness, rule violations), and must match greedy on deadlines without driving further or finishing later. It is also given a what-if run of the FCFS baseline (km, idle) as the target to beat. **Replans:** starts from the greedy plan and keeps it quick. Commits with `submit_plan` (queues, explanation, a reason per job). |
| **Fleet agent (greedy)** | `orchestrator/fleet_agent.py` | Deterministic planner: eligibility, then feasibility and ETA, then the most urgent job (priority, then least slack) to the tractor with the lowest cost (travel + waiting + lateness). Adds refuel stops. Claude's starting point and fallback. |
| **Safety agent** | `orchestrator/safety_agent.py` | One shared set of checks used by both planners, `validate()` and the tests. |
| **Maintenance agent** | `orchestrator/maintenance_agent.py` | Sends a repair crew on every breakdown and blocked road, and a fuel crew to any tractor that runs dry. Repairs arrive as normal events, so the fleet replans on them. |
| **Nav agent** | `orchestrator/nav_agent.py` | Shortest routes over the road graph, avoiding blocked roads. |
| **Explanations** | `orchestrator/explain.py` | Plan-change summaries and the "why this tractor?" text (Claude's reason, ETA, cost, and why the other tractors were rejected). |

### Rules every plan must follow

Hard rules (a plan that breaks one is rejected before it runs):
- each job has at most one tractor, and finished jobs are never reassigned;
- a tractor only does jobs its implement can do (sprayer: spray, mower: mow);
- broken tractors get no work;
- a tractor is never pulled off the job it is working on;
- every route uses open roads only;
- fuel: after every job a tractor can still reach a depot above its 10% reserve; refuel stops are added when needed.

Soft rule: deadlines. A likely miss is shown as a warning, not a rejection.

**Never worse than greedy:** with no key, an API error or timeout, a refusal, or a plan that still
fails the checks after Claude's retries, the greedy plan runs instead and the dashboard says why.
Tests and the safety checks are the same either way.

## The scenario: River farm (`scenarios/farm.json`)

Three tractors, eight fields, ten jobs (an eleventh arrives mid-day), one depot, and a day of at most 480 minutes (the horizon; a run ends earlier once every job is done). A river splits the
farm: the **bridge** is the short crossing and the south road is a long detour.

- **T1** (sprayer) starts low on fuel; **T2** (sprayer); **T3** is the only mower.
- Jobs are submitted far-first and the most urgent job (J6) late, which hurts first come, first served.

Scripted events (the Maintenance agent repairs each one):

| Tick | Event | Repair |
|---:|---|---|
| 60 | T2 breaks down mid-job | back at t=65 |
| 150 | Bridge blocked | reopens at t=160 |
| 200 | J7 (Bottom Field) raised to priority 3 | |
| 240 | New job J11 submitted | |

Repairs are part of the world, so the baseline gets them too and the comparison stays fair.

## Results

<!-- TODO at hand-in: run the command below, commit out/, and paste the table from out/comparison.md here. -->

_Final table to be added from `out/comparison.md`._

The hand-in `out/` files are produced by one run with Claude, with the key in `.env`:

```bash
docker compose run --rm -e COORDINATOR=llm app python run_demo.py
```

(`-e` matters: a variable set in front of `docker compose` on your own shell is not passed into the
container; only `.env` and `-e` are.) That run compares the FCFS baseline, the greedy coordinator
and the Claude coordinator. Without an API key, `run_demo.py` compares the baseline with the greedy
coordinator only; it still finishes with 0 violations.

## Hand-in files

- Code: this repository
- Scenario: `fleet-orchestrator/scenarios/farm.json`
- Event logs: `fleet-orchestrator/out/event_log.jsonl` (the hand-in coordinator: Claude),
  `out/event_log_greedy.jsonl` (greedy coordinator) and `out/event_log_baseline.jsonl` (FCFS baseline)
- Comparison: `fleet-orchestrator/out/comparison.md`

## Repository layout

```
fleet-orchestrator/
  orchestrator/      sim, agents (llm, fleet, safety, maintenance, nav), explain, metrics, event log
  scenarios/         farm.json
  dashboard/         React dashboard (map and satellite views, tractors, jobs, explanations, event log, operator buttons)
  server.py          FastAPI: /api/state, /api/event, /api/jobs, /api/why/{job}, /api/speed
  run_demo.py        baseline vs coordinator -> out/comparison.md, out/event_log.jsonl
  tests/             invariants, scenarios, Claude coordinator (offline fake client), maintenance, server
plan/                PLAN.md, TEAM_SPLIT.md, LLM_AGENT.md, LEAFLET.md
```
