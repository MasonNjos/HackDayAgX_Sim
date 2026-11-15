"""Claude coordinator: Claude makes the dispatch decisions, the Safety Agent approves them.

Owner: Person B. Design: plan/LLM_AGENT.md.

Each plan() is one short tool loop with Claude:
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

How to work:
- The brief includes a greedy plan that already passes every rule. Start from it and change it \
only where you can show a gain: check alternatives with evaluate_queue (it gives each stop's \
start/finish tick, fuel left and lateness, or why the queue fails).
- Do not leave out a job the greedy plan assigned.
- Keep it quick: a few evaluate_queue calls at most, then call submit_plan.
- In submit_plan, `explanation` is 2-4 plain sentences for a farm operator: what triggered this \
plan, what changed and why, and any deadline at risk. `reasons` gives one short sentence per \
assigned job saying why that tractor."""

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

TOOLS = [
    {
        "name": "evaluate_queue",
        "description": "Simulate one tractor's queue from its current position and return, per stop, "
                       "the start and finish tick, fuel left and minutes late, or the reason the queue "
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
                "queues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"tractor": {"type": "string"}, "stops": _STOPS},
                        "required": ["tractor", "stops"],
                        "additionalProperties": False,
                    },
                },
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

    # --- dispatcher entry point ---------------------------------------------

    def plan(self, state: WorldState, trigger: Event | None) -> Plan:
        greedy = self.greedy.plan(state, trigger)
        try:
            client = self._get_client()
            plan = self._ask_claude(client, state, trigger, greedy)
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

    def _ask_claude(self, client, state: WorldState, trigger: Event | None, greedy: Plan) -> Plan:
        messages = [{"role": "user", "content": json.dumps(self._brief(state, trigger, greedy))}]
        required = {s.target for q in greedy.queues.values() for s in q if s.kind == "job"}
        started, nudged = time.monotonic(), False

        for _ in range(MAX_TURNS):
            if time.monotonic() - started > BUDGET_S:
                raise LLMFailure(f"no plan within {BUDGET_S:.0f} s")
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
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
                elif b.name == "submit_plan":
                    plan, content = self._check_submit(b.input, state, trigger, greedy, required)
                    if plan is not None:
                        return plan
                    is_error = True
                else:
                    content, is_error = f"unknown tool {b.name}", True
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        raise LLMFailure(f"no valid plan after {MAX_TURNS} turns")

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
        steps = []
        for st in sim.steps:
            row = {"stop": f"{st.stop.kind}:{st.stop.target}", "start": st.start, "finish": st.finish,
                   "km": round(st.travel_km, 1), "fuel_after": round(st.fuel_after, 1)}
            if st.stop.kind == "job":
                row["late_by"] = max(0, st.finish - state.jobs[st.stop.target].deadline)
            steps.append(row)
        out = {"ok": sim.ok, "steps": steps, "total_km": round(sim.travel_km, 1)}
        if not sim.ok:
            out["fails_at_stop"] = len(sim.steps)
            out["reason"] = sim.reason
        return json.dumps(out), False

    def _check_submit(self, args: dict, state: WorldState, trigger, greedy: Plan, required: set[str]):
        """(plan, "") if accepted, else (None, error text for Claude)."""
        try:
            queues = {t.id: [] for t in state.tractors.values()}
            for q in args["queues"]:
                if q["tractor"] not in queues:
                    return None, f"unknown tractor {q['tractor']}"
                queues[q["tractor"]] = _stops(q["stops"])
            explanation = str(args["explanation"]).strip()
            reasons = {r["job"]: r["reason"] for r in args["reasons"]}
        except (KeyError, TypeError) as exc:
            return None, f"bad input: {exc!r}"

        plan = Plan(queues=queues)
        problems = safety.validate(plan, state)
        assigned = {s.target for q in queues.values() for s in q if s.kind == "job"}
        missing = sorted(required - assigned)
        if missing:
            problems.append(f"jobs the greedy plan could do are missing: {', '.join(missing)}")
        if problems:
            return None, "Plan rejected. Fix these and call submit_plan again:\n- " + "\n- ".join(problems)

        unassignable = [w for w in greedy.warnings if " unassignable" in w]
        plan.warnings = unassignable + safety.deadline_warnings(plan, state)
        plan.explanation = "[Claude] " + explanation
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

    def _brief(self, state: WorldState, trigger: Event | None, greedy: Plan) -> dict:
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
        greedy_eta = {}
        for tid, q in greedy.queues.items():
            sim = safety.simulate_queue(state.tractors[tid], q, state)
            greedy_eta[tid] = [{"stop": f"{st.stop.kind}:{st.stop.target}", "finish": st.finish}
                               for st in sim.steps]
        return {
            "now_tick": state.t,
            "units": "1 tick = 1 minute; km; litres; hectares",
            "trigger": {"kind": trigger.kind, "params": trigger.params} if trigger else "start of day",
            "tractors": [{
                "id": t.id, "implement": t.implement, "can_do": t.capabilities, "status": t.status,
                "at_node": t.location, "fuel": round(t.fuel, 1), "fuel_capacity": t.fuel_capacity,
                "reserve": round(t.reserve_fuel, 1), "burn_per_tick": t.burn_rate,
                "km_per_tick": t.speed, "hectares_per_tick": t.work_rate,
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
            "greedy_plan": {
                "queues": {tid: _queue_str(q) for tid, q in greedy.queues.items()},
                "finish_ticks": greedy_eta,
                "warnings": greedy.warnings,
            },
            "broken_tractors": [t.id for t in state.tractors.values() if t.status == BROKEN],
            "repairs_on_the_way": self.maintenance.pending() if self.maintenance else [],
        }
