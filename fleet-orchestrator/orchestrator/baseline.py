"""First-come-first-served baseline dispatcher. Owner: Person A.

Deliberately naive, as the brief's comparison point:
  - jobs are taken in submitted order (submitted_at, then scenario order),
  - each goes to the first compatible tractor that has nothing to do,
  - one job per tractor at a time, no travel cost, deadline or priority awareness,
  - no fuel lookahead and never refuels (a tractor that runs dry stalls),
  - on a breakdown the sim releases the job and it is simply handed out again.
"""
from __future__ import annotations

from .models import ASSIGNED, IN_PROGRESS, PENDING, Event, Plan, Stop, WorldState


class FCFSDispatcher:
    name = "baseline"

    def plan(self, state: WorldState, trigger: Event | None) -> Plan:
        active = sorted(t.id for t in state.active_tractors())
        queues: dict[str, list[Stop]] = {tid: [] for tid in state.tractors}

        # Keep whatever each tractor already owns.
        for job in state.jobs.values():
            if job.owner in active and job.status in (ASSIGNED, IN_PROGRESS):
                queues[job.owner].append(Stop("job", job.id))

        order = list(state.jobs)  # dict preserves scenario/insertion order
        pending = sorted((j for j in state.jobs.values() if j.status == PENDING and j.submitted_at <= state.t),
                         key=lambda j: (j.submitted_at, order.index(j.id)))
        given = []
        for job in pending:
            for tid in active:
                tr = state.tractors[tid]
                if not queues[tid] and job.type in tr.capabilities:
                    queues[tid].append(Stop("job", job.id))
                    given.append(f"{job.id}->{tid}")
                    break

        return Plan(queues=queues,
                    explanation=("FCFS: " + ", ".join(given)) if given else "FCFS: no changes")
