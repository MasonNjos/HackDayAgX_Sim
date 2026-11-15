"""Claude coordinator: Claude makes the dispatch decisions, the Safety Agent approves them.

Owner: Person B. Design: plan/LLM_AGENT.md.

Start of the day (minute 0, no event): Claude gets the whole job list and builds every
tractor's route from scratch, scored with `evaluate_plan`; it must match the greedy plan on
deadlines and not drive further, or it is sent back to improve. Greedy queues are hidden then
(only their score is shown) so Claude does not just copy them.

Every other replan is one short tool loop with Claude:
  1. Claude gets a brief of the world (tractors, open jobs, distances, blocked roads)
     plus the greedy FleetAgent's plan as a known-good starting point;
  2. it can test ideas with `evaluate_queue` (safety_agent.simulate_queue: ETAs, fuel,
     the reason a queue fails);
  3. it commits with `submit_plan`; the plan must pass safety.validate() and keep every
     job the greedy plan could place, otherwise the errors go back to Claude to fix.

Never worse than greedy: with no credentials, an API error, a refusal, a timeout, too
many turns or no valid plan, the greedy plan is returned with a warning saying so.
Settings come from the environment (see example.env).
"""
from __future__ import annotations

import copy
import json
import os
import time

from . import nav_agent
from . import safety_agent as safety
from .fleet_agent import FleetAgent
from .models import BROKEN, DONE, Event, Plan, Stop, WorldState

MODEL = os.environ.get("LLM_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("LLM_EFFORT", "medium")          # low | medium | high | xhigh | max
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT", "45"))    # per API request
BUDGET_S = float(os.environ.get("LLM_BUDGET", "90"))      # whole plan() call
MAX_TURNS = int(os.environ.get("LLM_MAX_TURNS", "8"))
MAX_TOKENS = 16000

SYSTEM = """You are the dispatcher for an autonomous fleet of farm tractors. There is no human \
operator in the loop: your plan is applied as soon as it passes the safety check.

You are called at the start of the day, after every event (breakdown, repair, blocked or \
reopened road, priority change, new job) and whenever a tractor runs out of work. Each call \
you return the complete plan: an ordered queue of stops for every tractor.

Goals, in order:
1. Complete as much work as possible, weighted by priority (3 = high, 1 = low).
2. Meet deadlines, most important first.
3. Minimise travel and idle time.

Hard rules (a plan breaking one is rejected):
- A job can be in at most one queue. Done jobs must not appear.
- A tractor can only do job types its implement allows (`can_do`).
- Broken tractors get an empty queue.
- If a tractor has `must_keep` set, its queue must start with its current job.
- Every stop must be reachable over open roads.
- Repairs are automatic: when a tractor breaks down or a road is blocked, a repair crew is \
sent at once (`repairs_on_the_way` gives when each fix lands). Until then, plan with what works \
now; you are called again the moment a repair lands. Mention the repair time in the explanation.
- Fuel: after every job a tractor must still be able to drive to the nearest depot and arrive \
above its reserve. Add a {"kind": "refuel", "target": <depot id>} stop before a job when needed.

Tools: evaluate_queue simulates one tractor's queue (each stop's start/finish minute, fuel left, \
minutes late, or why it fails); evaluate_plan scores a whole plan (rule violations, total km, \
late jobs, jobs left out) without applying it; submit_plan commits.

Writing style:
- Time: every time in the brief and tool results is in minutes since the start of the day (`now_minute`, deadlines, start/finish). Durations are minutes too. In `explanation` and `reasons`, write points in time as "minute 60" and durations as "12 min". Never use the word "tick".
- In submit_plan, `explanation` is 2-4 plain sentences for a farm operator: what triggered this \
plan, what changed and why, and any deadline at risk. `reasons` gives one short sentence per \
assigned job saying why that tractor."""

HOW_FULL_DAY = """This is the START OF THE DAY. You have the whole day's job list. Build each \
tractor's route yourself, from scratch:
- Group jobs that are close together on the same tractor and order each route to cut driving: \
avoid crossing the river (bridge or south road) back and forth, and avoid doubling back.
- Keep high-priority and tight-deadline jobs on time; that comes before distance.
- Put refuel stops where they are needed, ideally when a route already passes the depot.
- Draft 2-3 candidate plans, compare them with evaluate_plan, then submit the best one.
- Every job must be assigned unless no tractor can do it.
- Efficiency means less travel and less idle time. Cut idle the honest way: make the route of the \
tractor that finishes last (`last_finish_minute`) shorter, and split shared work evenly between \
tractors that can do it. Never add driving just to fill time; extra km is never a gain.
- Targets: `fcfs_baseline_target` is what a first-come-first-served dispatcher does over the same \
day (no events). Aim for less travel than it, overall and per job, with more jobs on time. \
`greedy_score` is a simple rule-based plan: you must match it on deadlines (`weighted_late`) and \
not drive further or finish later without a gain, or the plan is rejected.
- Some idle cannot be avoided: when only one tractor can do a job type (e.g. one mower), the \
others wait once their own work is done. Say so in the explanation if it applies.
- In `explanation`, describe the route plan (who covers which part of the farm) and compare its \
km and idle with the baseline's and the greedy plan's."""

HOW_REPLAN = """This is a REPLAN during the day. Be quick:
- The brief includes a greedy plan that already passes every rule. Start from it and change it \
only where you can show a gain, checked with evaluate_queue or evaluate_plan.
- Do not leave out a job the greedy plan assigned.
- A few tool calls at most, then call submit_plan."""

FULL_DAY, REPLAN = "full_day", "replan"
KM_TOLERANCE = 0.5   # a full-day plan may be this much longer than greedy and still count as "not worse"
FINISH_TOLERANCE = 5  # ... or finish the day this many minutes later
MAX_TURNS_START = int(os.environ.get("LLM_MAX_TURNS_START", "14"))
BUDGET_START_S = float(os.environ.get("LLM_BUDGET_START", "180"))
BASELINE_HORIZON = 480  # minutes; the what-if FCFS run for the start-of-day target

_STOPS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["job", "refuel"]},
            "target": {"type": "string", "description": "job id for a job stop, depot id for a refuel stop"},
        },
        "required": ["kind", "target"],
        "additionalProperties": False,
    },
}

_QUEUES = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"tractor": {"type": "string"}, "stops": _STOPS},
        "required": ["tractor", "stops"],
        "additionalProperties": False,
    },
}

TOOLS = [
    {
        "name": "evaluate_plan",
        "description": "Score a complete plan (one queue per tractor) without applying it: hard-rule "
                       "violations, total km, travel minutes, the minute the last tractor finishes, "
                       "late jobs with minutes late, weighted_late (sum of the priorities of late "
                       "jobs) and open jobs left out, plus each tractor's stop-by-stop timeline.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"queues": _QUEUES},
            "required": ["queues"],
            "additionalProperties": False,
        },
    },
    {
        "name": "evaluate_queue",
        "description": "Simulate one tractor's queue from its current position and return, per stop, "
                       "the start and finish minute, fuel left and minutes late, or the reason the queue "
                       "fails (fuel, unreachable road, wrong implement). Does not change anything.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"tractor": {"type": "string"}, "stops": _STOPS},
            "required": ["tractor", "stops"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_plan",
        "description": "Submit the complete plan: one queue per tractor (empty for idle or broken "
                       "tractors). It is checked against the hard rules; if it fails you get the "
                       "violations back to fix. If it passes it is applied immediately.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "queues": _QUEUES,
                "explanation": {"type": "string"},
                "reasons": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"job": {"type": "string"}, "reason": {"type": "string"}},
                        "required": ["job", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["queues", "explanation", "reasons"],
            "additionalProperties": False,
        },
    },
]


class LLMFailure(Exception):
    """Claude could not produce a valid plan; the greedy plan is used instead."""


def _stops(raw: list[dict]) -> list[Stop]:
    return [Stop(s["kind"], s["target"]) for s in raw]


def _queue_str(stops: list[Stop]) -> list[str]:
    return [f"{s.kind}:{s.target}" for s in stops]


class LLMFleetAgent:
    name = "claude-coordinator"

    def __init__(self, client=None) -> None:
        self.greedy = FleetAgent()
        self._client = client
        self._disabled: str | None = None   # reason Claude is off for this run (e.g. no credentials)
        self.maintenance = None             # set by MaintenanceAgent.attach(): repairs on the way
        self._baseline_cache: dict[int, dict | None] = {}   # id(state) -> FCFS what-if numbers

    # --- dispatcher entry point ---------------------------------------------

    def plan(self, state: WorldState, trigger: Event | None) -> Plan:
        greedy = self.greedy.plan(state, trigger)
        mode = FULL_DAY if state.t == 0 and trigger is None else REPLAN
        try:
            client = self._get_client()
            plan = self._ask_claude(client, state, trigger, greedy, mode)
        except LLMFailure as exc:
            return self._fallback(greedy, str(exc))
        except Exception as exc:  # any SDK/network error: never let Claude break the run
            return self._fallback(greedy, f"{type(exc).__name__}: {exc}")
        # Keep the greedy agent's memory in step with what is actually sent, so its
        # explanations and safety fallback diff against the plan the sim is running.
        self.greedy._last_queues = {tid: list(q) for tid, q in plan.queues.items()}
        return plan

    def _fallback(self, greedy: Plan, reason: str) -> Plan:
        greedy.warnings = [f"Claude unavailable ({reason[:200]}); used the greedy plan"] + greedy.warnings
        greedy.explanation = "[greedy fallback] " + greedy.explanation
        return greedy

    def _get_client(self):
        if self._disabled:
            raise LLMFailure(self._disabled)
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=1)
            except Exception as exc:  # missing package or no credentials: stop trying this run
                self._disabled = f"no Claude client: {type(exc).__name__}: {exc}"
                raise LLMFailure(self._disabled) from exc
        return self._client

    # --- the tool loop ----------------------------------------------------------

    def _ask_claude(self, client, state: WorldState, trigger: Event | None, greedy: Plan,
                    mode: str = REPLAN) -> Plan:
        greedy_score = self._score(greedy.queues, state)
        if mode == FULL_DAY:
            self._baseline_cache = {id(state): self._baseline_score(state)}
        brief = self._brief(state, trigger, greedy, mode, greedy_score)
        messages = [{"role": "user", "content": json.dumps(brief)}]
        required = {s.target for q in greedy.queues.values() for s in q if s.kind == "job"}
        system = SYSTEM + "\n\nHow to work:\n" + (HOW_FULL_DAY if mode == FULL_DAY else HOW_REPLAN)
        max_turns, budget = (MAX_TURNS_START, BUDGET_START_S) if mode == FULL_DAY else (MAX_TURNS, BUDGET_S)
        started, nudged = time.monotonic(), False

        for _ in range(max_turns):
            if time.monotonic() - started > budget:
                raise LLMFailure(f"no plan within {budget:.0f} s")
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": EFFORT},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if resp.stop_reason == "refusal":
                raise LLMFailure("Claude declined the request")
            if resp.stop_reason == "max_tokens":
                raise LLMFailure("response was cut off")
            messages.append({"role": "assistant", "content": resp.content})

            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                if nudged:
                    raise LLMFailure("Claude ended without submitting a plan")
                nudged = True
                messages.append({"role": "user", "content": "Call submit_plan with your final plan now."})
                continue

            results = []
            for b in uses:
                if b.name == "evaluate_queue":
                    content, is_error = self._evaluate(b.input, state)
                elif b.name == "evaluate_plan":
                    content, is_error = self._evaluate_plan(b.input, state)
                elif b.name == "submit_plan":
                    plan, content = self._check_submit(b.input, state, trigger, greedy, required,
                                                       mode, greedy_score)
                    if plan is not None:
                        return plan
                    is_error = True
                else:
                    content, is_error = f"unknown tool {b.name}", True
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        raise LLMFailure(f"no valid plan after {max_turns} turns")

    # --- tools ------------------------------------------------------------------

    def _evaluate(self, args: dict, state: WorldState) -> tuple[str, bool]:
        try:
            tractor = state.tractors[args["tractor"]]
            stops = _stops(args["stops"])
        except (KeyError, TypeError) as exc:
            return f"bad input: {exc!r}", True
        for s in stops:
            if s.kind == "job" and s.target in state.jobs:
                why = safety.incompatibility_reason(tractor, state.jobs[s.target])
                if why:
                    return f"{s.target}: {why}", True
        sim = safety.simulate_queue(tractor, stops, state)
        out = {"ok": sim.ok, "steps": self._step_rows(sim, state), "total_km": round(sim.travel_km, 1)}
        if not sim.ok:
            out["fails_at_stop"] = len(sim.steps)
            out["reason"] = sim.reason
        return json.dumps(out), False

    @staticmethod
    def _step_rows(sim, state: WorldState) -> list[dict]:
        rows = []
        for st in sim.steps:
            row = {"stop": f"{st.stop.kind}:{st.stop.target}", "start": st.start, "finish": st.finish,
                   "km": round(st.travel_km, 1), "fuel_after": round(st.fuel_after, 1)}
            if st.stop.kind == "job":
                row["late_by"] = max(0, st.finish - state.jobs[st.stop.target].deadline)
            rows.append(row)
        return rows

    @staticmethod
    def _parse_queues(raw: list[dict], state: WorldState) -> dict[str, list[Stop]]:
        """Claude's queue list -> {tractor: stops}, every tractor present. Raises ValueError."""
        queues = {tid: [] for tid in state.tractors}
        for q in raw:
            if q["tractor"] not in queues:
                raise ValueError(f"unknown tractor {q['tractor']}")
            queues[q["tractor"]] = _stops(q["stops"])
        return queues

    @staticmethod
    def _score(queues: dict[str, list[Stop]], state: WorldState) -> dict:
        """How good a whole plan is: rule violations, distance, idle, lateness, jobs left out.

        Idle = for each working tractor, the minutes between the end of its route and the end of
        the last tractor's route (the sim counts it idle then, since every job is taken).
        """
        violations = safety.validate(Plan(queues=queues), state)
        km, travel, last, late, assigned, ends = 0.0, 0, state.t, [], set(), {}
        for tid, q in queues.items():
            tractor = state.tractors.get(tid)
            if tractor is None:
                continue
            sim = safety.simulate_queue(tractor, q, state)
            km += sim.travel_km
            travel += sim.travel_ticks
            last = max(last, sim.end_t)
            if tractor.status != BROKEN:
                ends[tid] = sim.end_t
            for st in sim.steps:
                if st.stop.kind != "job":
                    continue
                job = state.jobs[st.stop.target]
                assigned.add(job.id)
                if st.finish > job.deadline:
                    late.append({"job": job.id, "priority": job.priority, "late_by": st.finish - job.deadline})
        left_out = sorted(j.id for j in state.jobs.values()
                          if j.status != DONE and j.field in state.fields and j.id not in assigned)
        return {
            "ok": not violations,
            "violations": violations,
            "total_km": round(km, 1),
            "travel_minutes": travel,
            "last_finish_minute": last,
            "idle_minutes": sum(last - e for e in ends.values()),
            "route_end_minute": ends,
            "late_jobs": late,
            "weighted_late": sum(x["priority"] for x in late),
            "unassigned_jobs": left_out,
        }

    def _evaluate_plan(self, args: dict, state: WorldState) -> tuple[str, bool]:
        try:
            queues = self._parse_queues(args["queues"], state)
        except (KeyError, TypeError, ValueError) as exc:
            return f"bad input: {exc}", True
        out = self._score(queues, state)
        out["timelines"] = {tid: self._step_rows(safety.simulate_queue(state.tractors[tid], q, state), state)
                            for tid, q in queues.items()}
        return json.dumps(out), False

    def _baseline_score(self, state: WorldState) -> dict | None:
        """Run the FCFS baseline from this state to the end of the day, no events, on a copy.

        This is the "first come, first served" comparison point Claude is asked to beat.
        """
        from .baseline import FCFSDispatcher
        from .maintenance_agent import MaintenanceAgent
        from .sim import Sim   # imported here: sim is only needed for this what-if run
        try:
            sim = Sim(copy.deepcopy(state), [], FCFSDispatcher(), horizon=BASELINE_HORIZON)
            MaintenanceAgent().attach(sim)   # same fuel/repair crews as the real comparison
            summ = sim.run()
        except Exception:  # the target is a hint; never let it break planning
            return None
        return {"jobs_completed": summ["jobs_completed"], "deadlines_met": summ["deadlines_met"],
                "travel_km": summ["travel_km"], "travel_minutes": summ["travel_ticks"],
                "idle_minutes": summ["idle_ticks"], "minutes_run": summ["ticks"],
                "travel_km_per_job": round(summ["travel_km"] / summ["jobs_completed"], 1)
                if summ["jobs_completed"] else None}

    @staticmethod
    def _worse_than_greedy(score: dict, greedy_score: dict) -> str:
        """Why a full-day plan is worse than greedy, or "" if it is at least as good."""
        if score["weighted_late"] > greedy_score["weighted_late"]:
            return (f"more deadline risk than the greedy plan: weighted_late {score['weighted_late']} "
                    f"vs greedy {greedy_score['weighted_late']} (late: {score['late_jobs']})")
        if score["weighted_late"] < greedy_score["weighted_late"]:
            return ""
        # Distance and the day's finish are the checks. Idle is reported, not enforced: a tractor
        # can always look less idle by driving more, which is exactly what we do not want.
        km, g_km = score["total_km"], greedy_score["total_km"]
        end, g_end = score["last_finish_minute"], greedy_score["last_finish_minute"]
        if km > g_km + KM_TOLERANCE and end >= g_end:
            return (f"less efficient than the greedy plan with no deadline gain: {km} km vs greedy {g_km} km, "
                    f"and the day does not finish earlier (minute {end} vs {g_end})")
        if end > g_end + FINISH_TOLERANCE and km >= g_km:
            return (f"less efficient than the greedy plan with no deadline gain: the last tractor finishes at "
                    f"minute {end} vs greedy {g_end}, with no distance saved ({km} vs {g_km} km)")
        return ""

    def _check_submit(self, args: dict, state: WorldState, trigger, greedy: Plan, required: set[str],
                      mode: str = REPLAN, greedy_score: dict | None = None):
        """(plan, "") if accepted, else (None, error text for Claude)."""
        try:
            queues = self._parse_queues(args["queues"], state)
            explanation = str(args["explanation"]).strip()
            reasons = {r["job"]: r["reason"] for r in args["reasons"]}
        except (KeyError, TypeError, ValueError) as exc:
            return None, f"bad input: {exc}"

        plan = Plan(queues=queues)
        problems = safety.validate(plan, state)
        assigned = {s.target for q in queues.values() for s in q if s.kind == "job"}
        missing = sorted(required - assigned)
        if missing:
            problems.append(f"jobs the greedy plan could do are missing: {', '.join(missing)}")
        score = None
        if not problems and mode == FULL_DAY and greedy_score is not None:
            score = self._score(queues, state)
            worse = self._worse_than_greedy(score, greedy_score)
            if worse:
                problems.append(worse)
        if problems:
            return None, "Plan rejected. Fix these and call submit_plan again:\n- " + "\n- ".join(problems)

        unassignable = [w for w in greedy.warnings if " unassignable" in w]
        plan.warnings = unassignable + safety.deadline_warnings(plan, state)
        plan.explanation = "[Claude] " + explanation
        if score is not None:
            line = (f" Route plan: {score['total_km']} km, {score['idle_minutes']} idle min "
                    f"(greedy {greedy_score['total_km']} km, {greedy_score['idle_minutes']} idle min")
            base = self._baseline_cache.get(id(state))
            if base:
                line += f"; FCFS baseline {base['travel_km']} km, {base['idle_minutes']} idle min"
            plan.explanation += line + f"); {len(score['late_jobs'])} deadline(s) at risk."
        plan.why = self._why(state, trigger, queues, greedy.why, reasons)
        return plan, ""

    def _why(self, state: WorldState, trigger, queues, greedy_why: dict, reasons: dict) -> dict:
        """greedy why records, re-pointed to Claude's choices, with Claude's reason added."""
        trig = {"kind": trigger.kind, "t": trigger.t, "params": trigger.params} if trigger else None
        why = {j: r for j, r in greedy_why.items() if r.get("chosen") is None}  # unassignable records
        for tid, q in queues.items():
            sim = safety.simulate_queue(state.tractors[tid], q, state)
            for s in q:
                if s.kind != "job":
                    continue
                job, step = state.jobs[s.target], sim.step_for(s.target)
                g = greedy_why.get(s.target, {})
                if g.get("chosen") == tid:
                    rec = dict(g)
                else:
                    rec = {"chosen": tid, "deadline": job.deadline, "priority": job.priority,
                           "eligible": g.get("eligible", [tid]), "rejected": g.get("rejected", {}),
                           "planned_at": state.t, "trigger": trig, "greedy_choice": g.get("chosen")}
                    if step is not None:
                        rec["eta"] = step.finish
                        rec["distance_km"] = round(step.travel_km, 1)
                rec["decided_by"] = "claude"
                if s.target in reasons:
                    rec["claude_reason"] = reasons[s.target]
                why[s.target] = rec
        return why

    # --- brief ------------------------------------------------------------------

    def _brief(self, state: WorldState, trigger: Event | None, greedy: Plan,
               mode: str = REPLAN, greedy_score: dict | None = None) -> dict:
        open_jobs = [j for j in state.jobs.values() if j.status != DONE and j.field in state.fields]
        nodes = sorted({t.location for t in state.tractors.values()}
                       | {state.fields[j.field].node for j in open_jobs}
                       | {d.node for d in state.depots.values()})
        km = {}
        for a in nodes:
            row = {}
            for b in nodes:
                r = nav_agent.route(state, a, b)
                row[b] = round(r[1], 1) if r else None
            km[a] = row
        if mode == FULL_DAY:
            # Only the score, not the queues, so Claude builds its own routes instead of copying them.
            score = greedy_score or self._score(greedy.queues, state)
            greedy_part = {"greedy_score": {k: score[k] for k in
                                            ("total_km", "travel_minutes", "idle_minutes", "last_finish_minute",
                                             "late_jobs", "weighted_late", "unassigned_jobs")},
                           "greedy_warnings": greedy.warnings}
            base = self._baseline_cache.get(id(state))
            if base:
                greedy_part["fcfs_baseline_target"] = base
        else:
            greedy_eta = {}
            for tid, q in greedy.queues.items():
                sim = safety.simulate_queue(state.tractors[tid], q, state)
                greedy_eta[tid] = [{"stop": f"{st.stop.kind}:{st.stop.target}", "finish": st.finish}
                                   for st in sim.steps]
            greedy_part = {"greedy_plan": {
                "queues": {tid: _queue_str(q) for tid, q in greedy.queues.items()},
                "finish_minutes": greedy_eta,
                "warnings": greedy.warnings,
            }}
        return {
            "mode": mode,
            "now_minute": state.t,
            "units": "times and durations in minutes; distances km; fuel litres; area hectares",
            "trigger": ({"kind": trigger.kind, "params": trigger.params} if trigger
                        else "start of day" if state.t == 0
                        else "routine replan, no new event: a tractor finished its queue, stalled "
                             "(e.g. ran out of fuel) or cannot reach its next stop; check tractor statuses"),
            "tractors": [{
                "id": t.id, "implement": t.implement, "can_do": t.capabilities, "status": t.status,
                "at_node": t.location, "fuel": round(t.fuel, 1), "fuel_capacity": t.fuel_capacity,
                "reserve": round(t.reserve_fuel, 1), "litres_per_minute": t.burn_rate,
                "km_per_minute": t.speed, "hectares_per_minute": t.work_rate,
                "current_job": t.current_job, "must_keep": safety.must_keep_current(t, state),
            } for t in state.tractors.values()],
            "open_jobs": [{
                "id": j.id, "type": j.type, "field": state.fields[j.field].name,
                "node": state.fields[j.field].node, "hectares_left": round(j.remaining * state.fields[j.field].area, 1),
                "priority": j.priority, "deadline": j.deadline, "status": j.status, "owner": j.owner,
            } for j in open_jobs],
            "depots": [{"id": d.id, "node": d.node} for d in state.depots.values()],
            "blocked_roads": sorted(state.blocked_edges),
            "road_km_between_nodes": km,
            **greedy_part,
            "broken_tractors": [t.id for t in state.tractors.values() if t.status == BROKEN],
            "repairs_on_the_way": self.maintenance.pending() if self.maintenance else [],
        }
