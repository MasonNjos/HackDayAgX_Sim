"""Discrete-tick simulator. Owner: Person A.

The sim is the ONLY code that mutates WorldState. Dispatchers read state and return a
Plan; the sim validates and applies it (job owner/status), moves tractors, burns fuel,
progresses work, injects events and asks for a new plan:
  - at the start (trigger=None),
  - on every event (trigger=event),
  - when a tractor finishes its queue or stalls (trigger=None).

Hard rules enforced here (a plan that breaks one is logged as a violation and the
offending stop is dropped): one owner per job, implement must match the job, no work for
broken tractors, never reassign a completed job, never preempt a job in progress.
A tractor that runs out of fuel stalls in place (status BROKEN) and that is a violation.

Contract rules from models.Dispatcher implemented here: tractor.current_job is the job at
the head of its queue, while travelling to it as well as while working on it (rule 2); a
refuel stop waits REFUEL_TICKS at the depot, then fills the tank (rule 3).

1 tick = 1 sim minute. Movement is along graph edges; a tractor re-routes at every node,
so a newly blocked edge takes effect at the next node it reaches.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from . import nav_agent as nav
from .eventlog import EventLog
from .metrics import Metrics
from .models import (ASSIGNED, BROKEN, DONE, IDLE, IN_PROGRESS, PENDING, REFUELLING, TRAVELLING,
                     REFUEL_TICKS, WORKING, Dispatcher, Event, Plan, Stop, WorldState, job_from_dict)

EPS = 1e-9


@dataclass
class Leg:
    """A tractor part-way along one edge."""
    a: str            # node it left
    b: str            # node it is heading to
    edge: str
    length: float
    done_km: float = 0.0


class Sim:
    def __init__(self, state: WorldState, events: list[Event], dispatcher: Dispatcher,
                 log: EventLog | None = None, horizon: int = 480):
        self.state = state
        self.pending_events = sorted(events, key=lambda e: e.t)
        self.dispatcher = dispatcher
        self.log = log or EventLog(dispatcher=getattr(dispatcher, "name", ""))
        self.horizon = horizon
        self.metrics = Metrics()
        self.queues: dict[str, list[Stop]] = {tid: [] for tid in state.tractors}
        self.legs: dict[str, Leg | None] = {tid: None for tid in state.tractors}
        self.stall_reason: dict[str, str] = {}
        self.refuel_wait: dict[str, tuple[str, int]] = {}  # tractor -> (depot, ticks waited there)
        self.why: dict[str, dict] = {}
        # Observers (e.g. the Maintenance agent): on_event(sim, event) runs after each event and
        # before its replan; optional on_tick(sim) runs every tick. Apply events only through
        # process_event(), never apply_event() + replan() directly, or observers miss them.
        self.observers: list = []
        self.last_explanation = ""
        self.last_warnings: list[str] = []
        self._unreachable_logged: set[tuple[str, str]] = set()
        self.started = False
        self.last_source = ""      # who made the committed plan, e.g. "claude-coordinator" / "greedy fallback"
        # Deferred planning (live server): replan() only records a request; the server calls the
        # dispatcher outside its lock (a Claude replan takes seconds) and then commit_plan().
        # Offline runs (run_demo, tests) keep defer_plans False and plan synchronously.
        self.defer_plans = False
        self.plan_requests: list[Event | None] = []
        self.version = 0  # bumped on every change to the world, so a stale plan can be detected

    # ------------------------------------------------------------------ running

    @property
    def t(self) -> int:
        return self.state.t

    def finished(self) -> bool:
        if self.t >= self.horizon:
            return True
        return not self.state.open_jobs() and not self.pending_events

    def run(self) -> dict:
        while not self.finished():
            self.step()
        self.log.write(self.t, "run_end", summary=self.metrics.summary(self.state))
        return self.metrics.summary(self.state)

    def inject(self, event: Event) -> None:
        """Queue an event (e.g. from the dashboard). Applied at the start of the next step."""
        event.t = max(event.t, self.t)
        self.pending_events.append(event)
        self.pending_events.sort(key=lambda e: e.t)

    def step(self) -> None:
        """Advance one tick. In deferred mode, does nothing while a plan request is waiting."""
        if self.defer_plans and self.plan_requests:
            return
        if not self.started:
            self.started = True
            self.log.write(self.t, "run_start", tractors=list(self.state.tractors), jobs=list(self.state.jobs))
            self.replan(None)

        while self.pending_events and self.pending_events[0].t <= self.t:
            ev = self.pending_events.pop(0)
            self.process_event(ev)
        if self.defer_plans and self.plan_requests:
            return  # time stands still until the new plan is committed (same as an offline run)

        need_replan = False
        for tid in sorted(self.state.tractors):
            need_replan |= self._step_tractor(tid)
            self._sync_current(tid)
        for obs in self.observers:  # e.g. the Maintenance agent spotting a tractor that ran out of fuel
            if hasattr(obs, "on_tick"):
                obs.on_tick(self)

        self.metrics.record_tick(self.state, {t for t, r in self.stall_reason.items() if r == "out_of_fuel"})
        self.state.t += 1
        self.version += 1
        if need_replan and not self.finished():
            self.replan(None)

    # ------------------------------------------------------------------ events

    def process_event(self, ev: Event) -> None:
        """Apply an event, tell the observers (e.g. the Maintenance agent), then replan.

        Every event goes through here, scripted or from the dashboard, so no observer misses one.
        """
        self.apply_event(ev)
        for obs in self.observers:
            obs.on_event(self, ev)
        self.replan(ev)

    def apply_event(self, ev: Event) -> None:
        self.version += 1
        s, p = self.state, ev.params
        self.log.write(self.t, "event", event=ev.kind, params=p)
        if ev.kind == "breakdown":
            self._take_out_of_service(p["tractor"], "breakdown")
        elif ev.kind == "repair":
            tr = s.tractors[p["tractor"]]
            if tr.status == BROKEN:
                tr.status = IDLE
                self.stall_reason.pop(tr.id, None)
        elif ev.kind == "route_blocked":
            s.blocked_edges.add(p["edge"])
            self._unreachable_logged.clear()
        elif ev.kind == "route_unblocked":
            s.blocked_edges.discard(p["edge"])
            self._unreachable_logged.clear()
        elif ev.kind == "priority_change":
            if p.get("job") not in s.jobs:
                self.log.write(self.t, "warning", detail=f"priority_change for unknown job {p.get('job')!r} ignored")
                return
            s.jobs[p["job"]].priority = int(p["priority"])
        elif ev.kind == "new_job":
            d = dict(p["job"])
            d.setdefault("submitted_at", self.t)
            job = job_from_dict(d)
            if job.id in s.jobs:
                self.log.write(self.t, "warning", detail=f"new_job {job.id!r} already exists; ignored")
                return
            s.jobs[job.id] = job
        else:
            self.log.write(self.t, "warning", detail=f"unknown event kind {ev.kind!r}")

    def _take_out_of_service(self, tid: str, reason: str) -> None:
        """Broken or out of fuel: release its work (keeping partial progress) and its queue."""
        tr = self.state.tractors[tid]
        tr.status = BROKEN
        self.stall_reason[tid] = reason
        for job in self.state.jobs.values():
            if job.owner == tid and job.status in (ASSIGNED, IN_PROGRESS):
                self.log.write(self.t, "job_released", job=job.id, tractor=tid, reason=reason,
                               remaining=round(job.remaining, 3))
                job.status, job.owner = PENDING, None
        tr.current_job = None
        self.queues[tid] = []

    # ------------------------------------------------------------------ planning

    def replan(self, trigger: Event | None) -> None:
        if self.defer_plans:
            self.plan_requests.append(trigger)
            return
        try:
            plan = self.dispatcher.plan(self.state, trigger)
        except Exception as exc:  # a crashing dispatcher must not kill the demo
            self.log.write(self.t, "dispatcher_error", error=repr(exc))
            return
        self.apply_plan(plan, trigger)

    def apply_plan(self, plan: Plan, trigger: Event | None) -> None:
        s = self.state
        old_owner = {j.id: j.owner for j in s.jobs.values()}
        owner: dict[str, str] = {}
        new_queues: dict[str, list[Stop]] = {tid: [] for tid in s.tractors}

        def violate(kind: str, detail: str) -> None:
            v = self.metrics.add_violation(self.t, kind, detail)
            self.log.write(self.t, "violation", violation=v["kind"], detail=v["detail"])

        for tid, stops in plan.queues.items():
            tr = s.tractors.get(tid)
            if tr is None:
                violate("unknown_tractor", f"plan has a queue for unknown tractor {tid}")
                continue
            if tr.status == BROKEN and stops:
                violate("broken_tractor", f"plan gives work to out-of-service tractor {tid}")
                continue
            for stop in stops:
                if stop.kind == "refuel":
                    if stop.target not in s.depots:
                        violate("bad_stop", f"{tid}: unknown depot {stop.target}")
                        continue
                    new_queues[tid].append(stop)
                    continue
                if stop.kind != "job" or stop.target not in s.jobs:
                    violate("bad_stop", f"{tid}: unknown stop {stop.kind}:{stop.target}")
                    continue
                job = s.jobs[stop.target]
                if job.status == DONE:
                    violate("completed_reassigned", f"{job.id} is already done but was queued for {tid}")
                    continue
                if job.id in owner:
                    violate("double_owner", f"{job.id} queued for both {owner[job.id]} and {tid}")
                    continue
                if job.type not in tr.capabilities:
                    violate("incompatible", f"{tid} ({tr.implement}) cannot do {job.type} job {job.id}")
                    continue
                if job.status == IN_PROGRESS and job.owner not in (None, tid):
                    violate("preempted", f"{job.id} is in progress on {job.owner}; plan moved it to {tid}")
                    continue
                owner[job.id] = tid
                new_queues[tid].append(stop)

        # A job in progress always stays at the head of its owner's queue, whatever the
        # tractor is doing this tick (only a breakdown/stall releases it).
        for job in s.jobs.values():
            if job.status != IN_PROGRESS or job.owner not in s.tractors:
                continue
            tr = s.tractors[job.owner]
            if tr.status == BROKEN:
                continue
            q = [st for st in new_queues[tr.id] if not (st.kind == "job" and st.target == job.id)]
            new_queues[tr.id] = [Stop("job", job.id)] + q
            owner[job.id] = tr.id

        for job in s.jobs.values():
            if job.status in (DONE,):
                continue
            if job.id in owner:
                job.owner = owner[job.id]
                if job.status != IN_PROGRESS:
                    job.status = ASSIGNED
            elif job.status in (ASSIGNED, IN_PROGRESS):
                job.status, job.owner = PENDING, None

        self.queues = new_queues
        for tid in s.tractors:
            self._sync_current(tid)
        self.why.update(plan.why or {})
        self.last_explanation = plan.explanation
        self.last_warnings = list(plan.warnings)
        self.metrics.replans += 1
        self.version += 1
        self.last_source = getattr(plan, "source", None) or (
            "greedy fallback" if plan.explanation.startswith("[greedy fallback]")
            else getattr(self.dispatcher, "name", ""))

        for jid, tid in owner.items():
            if old_owner.get(jid) != tid:
                self.log.write(self.t, "assignment", job=jid, tractor=tid, previous=old_owner.get(jid))
        self.log.write(self.t, "plan", trigger=trigger.kind if trigger else None, source=self.last_source,
                       queues={tid: [f"{st.kind}:{st.target}" for st in q] for tid, q in new_queues.items()},
                       explanation=plan.explanation, warnings=plan.warnings)

    # ------------------------------------------------------------------ movement / work

    # --- deferred planning (used by the live server) ---------------------------------

    def take_plan_request(self) -> tuple[Event | None, WorldState, int] | None:
        """Pop the waiting plan requests as one: (trigger, state copy, version), or None.

        Several requests (e.g. two events in one tick) collapse into one plan, made for the
        latest state; the trigger passed on is the most recent event.
        """
        if not self.plan_requests:
            return None
        events = [r for r in self.plan_requests if r is not None]
        self.plan_requests.clear()
        return (events[-1] if events else None), copy.deepcopy(self.state), self.version

    def commit_plan(self, plan: Plan | None, trigger: Event | None, version: int, error: str | None = None) -> bool:
        """Apply a plan computed off-lock. Returns False if it was stale (the world changed meanwhile)."""
        if error is not None:
            self.log.write(self.t, "dispatcher_error", error=error)
            return False
        if version != self.version:
            self.log.write(self.t, "plan_discarded", reason="the world changed while planning; replanning")
            if not self.plan_requests:
                self.plan_requests.append(trigger)
            return False
        self.apply_plan(plan, trigger)
        return True

    def _sync_current(self, tid: str) -> None:
        """Contract rule 2: current_job is the job at the head of the queue (travelling or working)."""
        tr = self.state.tractors[tid]
        q = self.queues[tid]
        tr.current_job = q[0].target if (tr.status != BROKEN and q and q[0].kind == "job") else None
        # Reset the refuel wait unless the head is still a refuel at the same depot.
        wait = self.refuel_wait.get(tid)
        if wait and not (q and q[0].kind == "refuel" and q[0].target == wait[0]):
            self.refuel_wait.pop(tid, None)

    def _stop_node(self, stop: Stop) -> str:
        if stop.kind == "refuel":
            return self.state.depots[stop.target].node
        return self.state.job_node(stop.target)

    def _burn(self, tr) -> bool:
        """Burn one tick of fuel. Returns False (and stalls the tractor) if the tank is empty."""
        if tr.fuel + EPS < tr.burn_rate:
            tr.fuel = 0.0
            v = self.metrics.add_violation(self.t, "out_of_fuel", f"{tr.id} ran out of fuel at {tr.location}")
            self.log.write(self.t, "violation", violation=v["kind"], detail=v["detail"])
            self._take_out_of_service(tr.id, "out_of_fuel")
            return False
        tr.fuel -= tr.burn_rate
        self.metrics.add_fuel(tr.id, tr.burn_rate)
        return True

    def _step_tractor(self, tid: str) -> bool:
        """Advance one tractor by one tick. Returns True if a replan is needed."""
        s = self.state
        tr = s.tractors[tid]
        if tr.status == BROKEN:
            return False
        q = self.queues[tid]

        # Drop stops that no longer make sense (e.g. job finished by someone else).
        while q and q[0].kind == "job" and s.jobs[q[0].target].status == DONE:
            q.pop(0)

        leg = self.legs[tid]
        if not q:
            if leg:  # finish the edge it is on, then wait
                if not self._burn(tr):
                    return True
                tr.status = TRAVELLING
                self._move(tr, leg.b)
            else:
                tr.status = IDLE
            return False

        stop = q[0]
        target = self._stop_node(stop)

        if leg or tr.location != target:
            if not leg and nav.route(s, tr.location, target) is None:
                # Drop the stop and hand the job back so a dispatcher can place it elsewhere.
                q.pop(0)
                if stop.kind == "job":
                    job = s.jobs[stop.target]
                    if job.owner == tid and job.status == ASSIGNED:
                        job.status, job.owner = PENDING, None
                tr.status = IDLE
                key = (tid, target)
                if key in self._unreachable_logged:
                    return False  # already replanned for this once; avoid a replan every tick
                self._unreachable_logged.add(key)
                self.log.write(self.t, "unreachable", tractor=tid, target=target, stop=stop.target)
                return True
            if not self._burn(tr):
                return True
            tr.status = TRAVELLING
            self._move(tr, target)
            return False

        # At the stop.
        if stop.kind == "refuel":
            tr.status = REFUELLING
            depot, waited = self.refuel_wait.get(tid, (stop.target, 0))
            if depot != stop.target:  # head changed depot without a sync; start over
                waited = 0
            waited += 1
            self.refuel_wait[tid] = (stop.target, waited)
            if waited >= REFUEL_TICKS:
                tr.fuel = tr.fuel_capacity
                self.refuel_wait.pop(tid, None)
                q.pop(0)
                self.log.write(self.t, "refuelled", tractor=tid, fuel=tr.fuel)
                return not q
            return False

        job = s.jobs[stop.target]
        if not self._burn(tr):
            return True
        if job.status != IN_PROGRESS:
            self.log.write(self.t, "job_started", job=job.id, tractor=tid, remaining=round(job.remaining, 3))
        tr.status, tr.current_job = WORKING, job.id
        job.status, job.owner = IN_PROGRESS, tid
        if tid not in job.completed_by:
            job.completed_by.append(tid)
        area = s.fields[job.field].area
        job.remaining = max(0.0, job.remaining - tr.work_rate / area)
        if job.remaining <= EPS:
            job.remaining = 0.0
            job.status = DONE
            job.completed_at = self.t + 1  # finished at the end of this tick
            tr.current_job = None
            q.pop(0)
            self.log.write(job.completed_at, "job_completed", job=job.id, tractor=tid,
                           on_time=job.completed_at <= job.deadline, deadline=job.deadline,
                           worked_by=list(job.completed_by))
            return not q
        return False

    def _move(self, tr, target: str) -> None:
        """Move tr up to tr.speed km towards target, re-routing at each node."""
        s = self.state
        budget = tr.speed
        while budget > EPS:
            leg = self.legs[tr.id]
            if leg is None:
                if tr.location == target:
                    break
                r = nav.route(s, tr.location, target)
                if r is None or len(r[0]) < 2:
                    break
                nxt = r[0][1]
                eid = nav.edge_between(s, tr.location, nxt)
                leg = Leg(tr.location, nxt, eid, s.edges[eid].length)
                self.legs[tr.id] = leg
            step = min(budget, leg.length - leg.done_km)
            leg.done_km += step
            budget -= step
            self.metrics.add_travel(tr.id, step)
            if leg.done_km >= leg.length - EPS:
                tr.location = leg.b
                self.legs[tr.id] = None
                if tr.location == target:
                    break

    # ------------------------------------------------------------------ views

    def position(self, tid: str) -> tuple[float, float]:
        s = self.state
        leg = self.legs[tid]
        if leg is None:
            n = s.nodes[s.tractors[tid].location]
            return n.x, n.y
        a, b = s.nodes[leg.a], s.nodes[leg.b]
        f = leg.done_km / leg.length if leg.length else 1.0
        return a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f

    def _route_points(self, tid: str) -> list[list[float]]:
        """[x, y] points from the tractor's position to its queue head (for drawing)."""
        s, q = self.state, self.queues[tid]
        if not q or s.tractors[tid].status == BROKEN:
            return []
        leg = self.legs[tid]
        start = leg.b if leg else s.tractors[tid].location
        r = nav.route(s, start, self._stop_node(q[0]))
        if r is None:
            return []
        return [list(self.position(tid))] + [[s.nodes[n].x, s.nodes[n].y] for n in r[0]]

    def snapshot(self) -> dict:
        """JSON-able view of everything the dashboard needs."""
        s = self.state
        tractors = []
        for tr in s.tractors.values():
            x, y = self.position(tr.id)
            tractors.append({
                "id": tr.id, "implement": tr.implement, "status": tr.status, "x": x, "y": y,
                "location": tr.location, "fuel": round(tr.fuel, 1), "fuel_capacity": tr.fuel_capacity,
                "current_job": tr.current_job, "stall_reason": self.stall_reason.get(tr.id),
                "queue": [f"{st.kind}:{st.target}" for st in self.queues[tr.id]],
                "route": self._route_points(tr.id),
            })
        return {
            "t": s.t, "horizon": self.horizon, "finished": self.finished(),
            "dispatcher": getattr(self.dispatcher, "name", ""),
            "nodes": [vars(n) for n in s.nodes.values()],
            "edges": [vars(e) | {"blocked": e.id in s.blocked_edges} for e in s.edges.values()],
            "depots": [vars(d) for d in s.depots.values()],
            "fields": [vars(f) | {"x": s.nodes[f.node].x, "y": s.nodes[f.node].y} for f in s.fields.values()],
            "tractors": tractors,
            "jobs": [vars(j) | {"remaining": round(j.remaining, 3)} for j in s.jobs.values()],
            "explanation": self.last_explanation,
            "plan_source": self.last_source,
            "warnings": self.last_warnings,
            "log": self.log.recent(50),
            "metrics": self.metrics.summary(s),
        }
