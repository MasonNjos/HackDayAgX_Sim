"""Fleet Agent: assigns jobs to tractors and replans on events. Core of the challenge.

Owner: Person B.

plan() is a pure function of the world state (it never mutates it):
  1. pin each working tractor's current job at the head of its queue (no preemption);
  2. eligibility via safety_agent predicates (implement, broken, route, fuel);
  3. feasibility/ETA via safety_agent.simulate_queue (inserting a refuel stop if needed);
  4. greedy: most urgent job first (priority, then least slack), to the tractor
     with the lowest marginal cost (travel + waiting + lateness penalty).
Jobs no tractor can take go to warnings with the reason for every tractor.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import safety_agent as safety
from .models import BROKEN, DONE, Event, Job, Plan, Stop, WorldState

LATE_PENALTY = 5.0   # cost per tick past the deadline
WAIT_WEIGHT = 0.2    # cost per tick before the tractor can start (spreads load)


@dataclass
class Option:
    tractor: str
    stops: list[Stop]          # full new queue for that tractor
    sim: safety.QueueSim
    finish: int
    cost: float
    terms: dict
    refuel: bool


class FleetAgent:
    name = "coordinator"

    def plan(self, state: WorldState, trigger: Event | None) -> Plan:
        queues: dict[str, list[Stop]] = {}
        pinned: set[str] = set()
        for t in state.tractors.values():
            queues[t.id] = []
            job = state.jobs.get(t.current_job) if t.current_job else None
            if t.status != BROKEN and job is not None and job.status != DONE:
                queues[t.id] = [Stop("job", job.id)]
                pinned.add(job.id)

        sims = {tid: safety.simulate_queue(state.tractors[tid], q, state) for tid, q in queues.items()}
        # Everything not done, including jobs marked unassignable earlier: a repair may make them possible.
        pool = [j for j in state.jobs.values() if j.status != DONE and j.id not in pinned]
        why: dict[str, dict] = {}
        warnings: list[str] = []

        while pool:
            best_key, best_job, best_opt, best_rec = None, None, None, None
            for job in list(pool):
                options, rejected = self._options(job, state, queues, sims)
                if not options:
                    pool.remove(job)  # adding stops never makes a job easier, so it stays infeasible
                    reason = "; ".join(f"{tid}: {r}" for tid, r in rejected.items()) or "no tractors"
                    warnings.append(f"{job.id} unassignable (needs {job.type} on "
                                    f"{state.fields[job.field].name}): {reason}")
                    why[job.id] = {"chosen": None, "eligible": [], "rejected": rejected}
                    continue
                opt = min(options.values(), key=lambda o: (o.cost, o.tractor))
                slack = job.deadline - opt.finish
                key = (-job.priority, slack, job.id)
                if best_key is None or key < best_key:
                    best_key, best_job, best_opt = key, job, opt
                    best_rec = {
                        "chosen": opt.tractor,
                        "eta": opt.finish,
                        "deadline": job.deadline,
                        "priority": job.priority,
                        "cost": opt.terms,
                        "refuel_first": opt.refuel,
                        "eligible": sorted(options),
                        "alternatives": {tid: round(o.cost, 1) for tid, o in options.items() if tid != opt.tractor},
                        "rejected": rejected,
                    }
            if best_job is None:
                break
            queues[best_opt.tractor] = best_opt.stops
            sims[best_opt.tractor] = best_opt.sim
            why[best_job.id] = best_rec
            pool.remove(best_job)

        plan = Plan(queues=queues, why=why)
        violations = safety.validate(plan, state)
        plan.warnings = warnings + safety.deadline_warnings(plan, state) + [f"SAFETY: {v}" for v in violations]
        plan.explanation = self._summary(plan, state, trigger)
        return plan

    # --- helpers -------------------------------------------------------------

    def _options(self, job: Job, state: WorldState, queues, sims) -> tuple[dict[str, Option], dict[str, str]]:
        options: dict[str, Option] = {}
        rejected: dict[str, str] = {}
        for t in state.tractors.values():
            reason = safety.incompatibility_reason(t, job)
            if reason:
                rejected[t.id] = reason
                continue
            opt = self._try(t.id, job, state, queues[t.id], sims[t.id])
            if isinstance(opt, str):
                rejected[t.id] = opt
            else:
                options[t.id] = opt
        return options, rejected

    def _try(self, tid: str, job: Job, state: WorldState, queue: list[Stop], base: safety.QueueSim):
        tractor = state.tractors[tid]
        candidates = [(queue + [Stop("job", job.id)], False)]
        last_is_refuel = bool(queue) and queue[-1].kind == "refuel"
        if not last_is_refuel:
            depot = safety.nearest_depot(state, base.end_node)
            if depot is not None:
                candidates.append((queue + [Stop("refuel", depot[0]), Stop("job", job.id)], True))

        first_reason = ""
        for stops, refuel in candidates:
            sim = safety.simulate_queue(tractor, stops, state)
            if not sim.ok:
                first_reason = first_reason or sim.reason
                if sim.failed_kind != "fuel":
                    break  # a refuel stop can't fix a route problem
                continue
            step = sim.step_for(job.id)
            added_travel = sim.travel_ticks - base.travel_ticks
            wait = step.start - state.t
            late = max(0, step.finish - job.deadline)
            refuel_ticks = safety.REFUEL_TICKS if refuel else 0
            cost = added_travel + refuel_ticks + WAIT_WEIGHT * wait + LATE_PENALTY * late
            terms = {"travel_ticks": added_travel, "wait_ticks": wait, "late_ticks": late,
                     "refuel_ticks": refuel_ticks, "total": round(cost, 1)}
            return Option(tid, stops, sim, step.finish, cost, terms, refuel)
        return first_reason or "no feasible schedule"

    def _summary(self, plan: Plan, state: WorldState, trigger: Event | None) -> str:
        assigned = {tid: [s.target for s in q if s.kind == "job"] for tid, q in plan.queues.items()}
        parts = [f"{tid}: {' > '.join(js) if js else 'idle'}" for tid, js in assigned.items()]
        n_unassignable = sum(1 for w in plan.warnings if " unassignable " in w)
        head = f"t={state.t}"
        if trigger is not None:
            head += f" after {trigger.kind} {trigger.params}"
        tail = f"; {n_unassignable} unassignable" if n_unassignable else ""
        return f"{head}. Plan: " + "; ".join(parts) + tail
