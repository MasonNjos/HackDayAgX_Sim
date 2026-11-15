"""Fleet Agent: assigns jobs to tractors and replans on events. Core of the challenge.

Owner: Person B.

plan() never mutates the world state. The agent does remember its last plan
(for explanations and the safety fallback), so use one FleetAgent per sim run.
Each plan:
  1. pin each working tractor's current job at the head of its queue (no preemption);
  2. eligibility via safety_agent predicates (implement, broken, route, fuel);
  3. feasibility/ETA via safety_agent.simulate_queue (inserting a refuel stop if needed);
  4. greedy: most urgent job first (priority, then least slack), to the tractor
     with the lowest marginal cost (travel + waiting + lateness penalty).
Jobs no tractor can take go to warnings with the reason for every tractor.

Before returning, the plan is checked with safety.validate(). If it fails, the
agent rebuilds a plan stop by stop: each tractor starts from its job in progress,
then takes the stops of the rejected plan and then of its previous plan, keeping
each stop only if its queue still validates. One bad stop costs only that stop.
Only if even "jobs in progress only" fails is the rejected plan returned, flagged
with SAFETY: warnings (the sim still drops bad stops).
The explanation is a diff against the last plan (explain.py).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import explain
from . import safety_agent as safety
from .models import BROKEN, DONE, REFUELLING, Event, Job, Plan, Stop, WorldState

LATE_PENALTY = 5.0   # cost per tick past the deadline
WAIT_WEIGHT = 0.2    # cost per tick before the tractor can start (spreads load)
LOW_FUEL = 0.25      # fraction of capacity: finishing a queue below this adds a refuel stop


@dataclass
class Option:
    tractor: str
    stops: list[Stop]          # full new queue for that tractor
    sim: safety.QueueSim
    finish: int
    cost: float
    terms: dict
    refuel: bool


def _pinned_queues(state: WorldState) -> dict[str, list[Stop]]:
    """Every tractor's queue holding only the job it must keep (no preemption):
    its current job, unless it is still driving there and the field is cut off."""
    return {t.id: [Stop("job", t.current_job)] if safety.must_keep_current(t, state) else []
            for t in state.tractors.values()}


def _trig(trigger: Event | None) -> dict | None:
    return {"kind": trigger.kind, "t": trigger.t, "params": trigger.params} if trigger else None


class FleetAgent:
    name = "coordinator"

    def __init__(self) -> None:
        self._last_queues: dict[str, list[Stop]] | None = None  # last plan returned (valid)
        self._last_fuel: dict[str, float] = {}                  # tractor fuel when it was made

    def plan(self, state: WorldState, trigger: Event | None) -> Plan:
        old = self._old_queues(state)
        queues, why, warnings = self._build(state, trigger)
        plan = Plan(queues=queues, why=why)
        violations = safety.validate(plan, state)
        note = ""
        if violations:
            fallback = self._fallback(state, old, queues)
            if fallback is not None:
                kept, label = fallback
                plan = Plan(queues=kept, why=self._held_back_why(state, kept, why, label, trigger))
                # Keep the "unassignable" warnings: those jobs are still unassignable.
                warnings = warnings + [f"SAFETY: plan rejected ({'; '.join(violations)}); kept {label}"]
                note = f"Safety check rejected the new plan ({violations[0]}); kept {label}. "
                violations = []
        plan.warnings = (warnings + safety.deadline_warnings(plan, state)
                         + [f"SAFETY: {v}" for v in violations])
        plan.explanation = note + explain.explain_plan(old, plan, state, trigger)
        # Remember what was actually sent, even a rejected plan: the sim runs it.
        self._last_queues = {tid: list(q) for tid, q in plan.queues.items()}
        self._last_fuel = {t.id: t.fuel for t in state.tractors.values()}
        return plan

    def _old_queues(self, state: WorldState) -> dict[str, list[Stop]] | None:
        """The last plan, minus what the tractors have done since.

        Finishing a job or a refuel only triggers a replan when the queue is empty,
        so the last plan can still list stops that are already done. Done jobs are
        dropped here. A refuel left at the head is dropped if it has evidently happened:
        the tank went up since the plan, or jobs before it were finished and the tank
        is still at least as full as it was (covers a plan made on a full tank).
        """
        if self._last_queues is None:
            return None
        out = {}
        for tid, last in self._last_queues.items():
            q, done_before = [], False
            for st in last:
                if st.kind == "job" and st.target in state.jobs and state.jobs[st.target].status == DONE:
                    done_before = done_before or not q
                    continue
                q.append(st)
            tr = state.tractors.get(tid)
            if q and q[0].kind == "refuel" and tr:
                was = self._last_fuel.get(tid, tr.fuel)
                if tr.fuel > was + 1e-9 or (done_before and tr.status != REFUELLING and tr.fuel >= was - 1e-9):
                    q = q[1:]
            out[tid] = q
        return out

    @staticmethod
    def _held_back_why(state: WorldState, kept, rejected_why: dict, label: str, trigger) -> dict:
        """why records for a fallback plan.

        Records for the jobs the kept plan still holds are not replaced (the sim keeps
        the ones from when they were assigned). Unassignable records stay. Every other
        open job gets a record saying it was held back, so /why never shows a stale owner.
        """
        kept_jobs = {s.target for q in kept.values() for s in q if s.kind == "job"}
        out = {j: r for j, r in rejected_why.items() if r.get("chosen") is None and j not in kept_jobs}
        reason = f"held back because the new plan failed the safety check (kept {label})"
        for j in state.jobs.values():
            if j.status != DONE and j.id not in kept_jobs and j.id not in out:
                out[j.id] = {"chosen": None, "eligible": [], "rejected": {}, "reason": reason,
                             "planned_at": state.t, "trigger": _trig(trigger)}
        return out

    def _build(self, state: WorldState, trigger: Event | None):
        """Greedy assignment. Returns (queues, why records, warnings)."""
        queues = _pinned_queues(state)
        pinned = {q[0].target for q in queues.values() if q}

        trig = _trig(trigger)
        sims = {tid: safety.simulate_queue(state.tractors[tid], q, state) for tid, q in queues.items()}
        why: dict[str, dict] = {}
        warnings: list[str] = []
        # Everything not done, including jobs marked unassignable earlier: a repair may make them possible.
        pool = []
        for j in state.jobs.values():
            if j.status == DONE or j.id in pinned:
                continue
            if j.field not in state.fields:
                warnings.append(f"{j.id} unassignable: unknown field {j.field}")
                why[j.id] = {"chosen": None, "eligible": [], "rejected": {}, "reason": f"unknown field {j.field}",
                              "planned_at": state.t, "trigger": trig}
                continue
            pool.append(j)

        while pool:
            best_key, best_job, best_opt, best_rec = None, None, None, None
            for job in list(pool):
                options, rejected = self._options(job, state, queues, sims)
                if not options:
                    pool.remove(job)  # adding stops never makes a job easier, so it stays infeasible
                    reason = "; ".join(f"{tid}: {r}" for tid, r in rejected.items()) or "no tractors"
                    warnings.append(f"{job.id} unassignable (needs {job.type} on "
                                    f"{state.fields[job.field].name}): {reason}")
                    why[job.id] = {"chosen": None, "eligible": [], "rejected": rejected,
                                   "planned_at": state.t, "trigger": trig}
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
                        "distance_km": opt.terms["distance_km"],
                        "slack": slack,
                        "options": {tid: {"eta": o.finish, "cost": round(o.cost, 1),
                                          "distance_km": o.terms["distance_km"], "refuel_first": o.refuel}
                                    for tid, o in options.items()},
                        "planned_at": state.t,
                        "trigger": trig,
                    }
            if best_job is None:
                break
            queues[best_opt.tractor] = best_opt.stops
            sims[best_opt.tractor] = best_opt.sim
            why[best_job.id] = best_rec
            pool.remove(best_job)

        self._top_up(state, queues, sims)
        return queues, why, warnings

    def _fallback(self, state: WorldState, old, rejected) -> tuple[dict[str, list[Stop]], str] | None:
        """A safe plan to use when the new one fails validation, or None.

        Each tractor starts from its job in progress, then is offered the stops of the
        rejected plan and then of the previous plan (`old`, completed stops removed).
        A stop is kept only if that tractor's queue still validates with it, so one bad
        stop never throws away the rest and idle tractors still get safe work.
        """
        queues = _pinned_queues(state)
        if safety.validate(Plan(queues=queues), state):
            return None
        base = {tid: list(q) for tid, q in queues.items()}
        owned = {q[0].target for q in queues.values() if q}
        for src in [rejected] + ([old] if old is not None else []):
            for tid in sorted(queues):
                if state.tractors[tid].status == BROKEN:
                    continue
                for st in src.get(tid, []):
                    if st.kind == "job" and (st.target in owned or st.target not in state.jobs
                                             or state.jobs[st.target].status == DONE):
                        continue
                    if st.kind == "refuel" and queues[tid] and queues[tid][-1].kind == "refuel":
                        continue
                    trial = queues[tid] + [st]
                    if not safety.validate(Plan(queues={tid: trial}), state):
                        queues[tid] = trial
                        if st.kind == "job":
                            owned.add(st.target)
        if queues == base:
            return queues, "only the jobs in progress"
        return queues, "only the safe stops of the new and previous plans"

    # --- helpers -------------------------------------------------------------

    def _top_up(self, state: WorldState, queues, sims) -> None:
        """Send a tractor to refuel at the end of its queue if it would finish low.

        Without this, a tractor at or near its reserve gets no jobs and nothing
        ever sends it to a depot, so it would stay stranded.
        """
        for t in state.tractors.values():
            q, sim = queues[t.id], sims[t.id]
            if t.status == BROKEN or not sim.ok or (q and q[-1].kind == "refuel"):
                continue
            if sim.fuel >= LOW_FUEL * t.fuel_capacity:
                continue
            depot = safety.nearest_depot(state, sim.end_node)
            if depot is None:
                continue
            stops = q + [Stop("refuel", depot[0])]
            new_sim = safety.simulate_queue(t, stops, state)
            if new_sim.ok:
                queues[t.id], sims[t.id] = stops, new_sim

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
                     "refuel_ticks": refuel_ticks, "total": round(cost, 1),
                     "distance_km": round(step.travel_km, 1)}
            return Option(tid, stops, sim, step.finish, cost, terms, refuel)
        return first_reason or "no feasible schedule"
