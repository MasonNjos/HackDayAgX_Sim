"""Safety Agent: shared constraint predicates and pre-commit plan validation.

Owner: Person B. The Fleet Agent's eligibility filter, validate() and the tests
all use the predicates in this file, so there is one definition of "safe".

Hard constraints (validate() rejects): single owner per job, implement match,
no work for broken tractors, no preemption of a tractor's current job, no done
jobs in queues, every route reachable over open edges, fuel never below reserve
and always enough to get back to a depot.
Soft constraints (warnings only): deadlines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import nav_agent
from .models import BROKEN, DONE, REFUEL_TICKS, Job, Plan, Stop, Tractor, WorldState


# --- single-stop predicates --------------------------------------------------

def compatible(tractor: Tractor, job: Job) -> bool:
    return tractor.status != BROKEN and job.type in tractor.capabilities


def incompatibility_reason(tractor: Tractor, job: Job) -> str | None:
    if tractor.status == BROKEN:
        return f"{tractor.id} is broken down"
    if job.type not in tractor.capabilities:
        return f"{tractor.id} carries a {tractor.implement}, job needs {job.type}"
    return None


def work_ticks(tractor: Tractor, job: Job, state: WorldState) -> int:
    area = state.fields[job.field].area
    return math.ceil(job.remaining * area / tractor.work_rate) if job.remaining > 0 else 0


def nearest_depot(state: WorldState, node: str) -> tuple[str, float] | None:
    """(depot id, km) of the closest reachable depot from node, or None."""
    best = None
    for d in state.depots.values():
        r = nav_agent.route(state, node, d.node)
        if r is not None and (best is None or r[1] < best[1]):
            best = (d.id, r[1])
    return best


# --- queue simulation (the one place ETA and fuel are computed) --------------

@dataclass
class StopSim:
    stop: Stop
    start: int          # tick work/refuel starts (after travel)
    finish: int         # tick the stop is complete
    travel_km: float
    travel_ticks: int
    work_ticks: int
    fuel_after: float


@dataclass
class QueueSim:
    steps: list[StopSim] = field(default_factory=list)
    ok: bool = True
    reason: str = ""           # first failure, human readable
    failed_kind: str = ""      # "fuel" | "route" | "invalid"
    end_node: str = ""
    end_t: int = 0
    fuel: float = 0.0

    @property
    def travel_ticks(self) -> int:
        return sum(s.travel_ticks for s in self.steps)

    @property
    def travel_km(self) -> float:
        return sum(s.travel_km for s in self.steps)

    def step_for(self, job_id: str) -> StopSim | None:
        return next((s for s in self.steps if s.stop.kind == "job" and s.stop.target == job_id), None)


def _fail(sim: QueueSim, kind: str, reason: str) -> QueueSim:
    sim.ok, sim.failed_kind, sim.reason = False, kind, reason
    return sim


def _blocked_note(state: WorldState) -> str:
    return f" (blocked: {', '.join(sorted(state.blocked_edges))})" if state.blocked_edges else ""


def simulate_queue(tractor: Tractor, stops: list[Stop], state: WorldState) -> QueueSim:
    """Walk a tractor's queue from its current node, tick and fuel.

    Fuel rule: fuel never drops below the reserve, and after every job there is
    still enough fuel to reach the nearest depot above the reserve.
    The tractor's own in-progress job at the head is exempt from the fuel check:
    it was checked when committed, and planning from the last node passed
    over-counts distance for a tractor mid-edge.
    """
    sim = QueueSim(end_node=tractor.location, end_t=state.t, fuel=tractor.fuel)
    node, t, fuel = tractor.location, state.t, tractor.fuel
    reserve = tractor.reserve_fuel

    for i, stop in enumerate(stops):
        if stop.kind == "job":
            job = state.jobs.get(stop.target)
            if job is None:
                return _fail(sim, "invalid", f"unknown job {stop.target}")
            dest = state.job_node(job.id)
            label = f"{job.id} ({state.fields[job.field].name})"
        elif stop.kind == "refuel":
            depot = state.depots.get(stop.target)
            if depot is None:
                return _fail(sim, "invalid", f"unknown depot {stop.target}")
            dest = depot.node
            label = f"refuel at {depot.id}"
        else:
            return _fail(sim, "invalid", f"unknown stop kind {stop.kind!r}")

        r = nav_agent.route(state, node, dest)
        if r is None:
            return _fail(sim, "route", f"no open road from {node} to {label}{_blocked_note(state)}")
        km = r[1]
        tt = nav_agent.travel_ticks(tractor, km)
        fuel -= tt * tractor.burn_rate
        start = t + tt
        exempt = i == 0 and stop.kind == "job" and stop.target == tractor.current_job

        if stop.kind == "refuel":
            if fuel < reserve and not exempt:
                return _fail(sim, "fuel", f"would reach depot with {fuel:.0f} L, below reserve {reserve:.0f} L")
            wt = REFUEL_TICKS
            fuel = tractor.fuel_capacity
        else:
            wt = work_ticks(tractor, job, state)
            fuel -= wt * tractor.burn_rate
            if not exempt:
                back = nearest_depot(state, dest)
                if back is None:
                    return _fail(sim, "route", f"no depot reachable after {label}{_blocked_note(state)}")
                need = nav_agent.travel_ticks(tractor, back[1]) * tractor.burn_rate
                if fuel - need < reserve:
                    return _fail(sim, "fuel",
                                 f"after {label} it would have {fuel:.0f} L, needs {need:.0f} L to get back "
                                 f"to {back[0]} plus {reserve:.0f} L reserve")

        t = start + wt
        node = dest
        sim.steps.append(StopSim(stop, start, t, km, tt, wt, fuel))
        sim.end_node, sim.end_t, sim.fuel = node, t, fuel
    return sim


def fuel_ok(tractor: Tractor, stops: list[Stop], state: WorldState) -> tuple[bool, str]:
    sim = simulate_queue(tractor, stops, state)
    return (sim.failed_kind != "fuel", sim.reason if sim.failed_kind == "fuel" else "")


def route_ok(tractor: Tractor, stops: list[Stop], state: WorldState) -> tuple[bool, str]:
    sim = simulate_queue(tractor, stops, state)
    return (sim.failed_kind != "route", sim.reason if sim.failed_kind == "route" else "")


# --- whole-plan checks -------------------------------------------------------

def validate(plan: Plan, state: WorldState) -> list[str]:
    """Hard-constraint violations in a plan; empty list means safe to commit."""
    out: list[str] = []
    owner: dict[str, str] = {}

    for tid, stops in plan.queues.items():
        tractor = state.tractors.get(tid)
        if tractor is None:
            out.append(f"queue for unknown tractor {tid}")
            continue
        if tractor.status == BROKEN:
            if stops:
                out.append(f"{tid} is broken but has {len(stops)} stop(s) queued")
            continue
        if tractor.current_job and state.jobs.get(tractor.current_job, None) is not None \
                and state.jobs[tractor.current_job].status != DONE:
            if not stops or stops[0] != Stop("job", tractor.current_job):
                out.append(f"{tid} would be pulled off its current job {tractor.current_job}")

        for s in stops:
            if s.kind != "job":
                continue
            job = state.jobs.get(s.target)
            if job is None:
                continue  # simulate_queue reports it
            if job.status == DONE:
                out.append(f"{job.id} is already done but queued on {tid}")
            if job.id in owner:
                out.append(f"{job.id} has two owners: {owner[job.id]} and {tid}")
            owner[job.id] = tid
            why = incompatibility_reason(tractor, job)
            if why:
                out.append(f"{job.id} on {tid}: {why}")

        sim = simulate_queue(tractor, stops, state)
        if not sim.ok:
            out.append(f"{tid}: {sim.reason}")
    return out


def deadline_warnings(plan: Plan, state: WorldState) -> list[str]:
    out = []
    for tid, stops in plan.queues.items():
        tractor = state.tractors.get(tid)
        if tractor is None or tractor.status == BROKEN:
            continue
        sim = simulate_queue(tractor, stops, state)
        for s in sim.steps:
            if s.stop.kind != "job":
                continue
            job = state.jobs[s.stop.target]
            if s.finish > job.deadline:
                out.append(f"{job.id} may miss its deadline by {s.finish - job.deadline} min "
                           f"({tid} ETA t={s.finish}, deadline t={job.deadline})")
    return out
