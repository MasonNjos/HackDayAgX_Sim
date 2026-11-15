"""Run metrics: jobs completed, deadlines met, travel, idle, fuel, constraint violations.

Owner: Person A. The sim calls record_tick() once per tick and add_violation() when a
hard constraint is broken; summary() produces the numbers for comparison.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import BROKEN, DONE, IDLE, REFUELLING, TRAVELLING, WORKING, WorldState


@dataclass
class TractorTotals:
    travel_ticks: int = 0
    travel_km: float = 0.0
    work_ticks: int = 0
    idle_ticks: int = 0
    refuel_ticks: int = 0
    broken_ticks: int = 0
    fuel_used: float = 0.0


@dataclass
class Metrics:
    per_tractor: dict[str, TractorTotals] = field(default_factory=dict)
    violations: list[dict] = field(default_factory=list)
    ticks: int = 0
    replans: int = 0

    def _tt(self, tid: str) -> TractorTotals:
        return self.per_tractor.setdefault(tid, TractorTotals())

    def record_tick(self, state: WorldState) -> None:
        self.ticks += 1
        for tr in state.tractors.values():
            tt = self._tt(tr.id)
            if tr.status == TRAVELLING:
                tt.travel_ticks += 1
            elif tr.status == WORKING:
                tt.work_ticks += 1
            elif tr.status == IDLE:
                tt.idle_ticks += 1
            elif tr.status == REFUELLING:
                tt.refuel_ticks += 1
            elif tr.status == BROKEN:
                tt.broken_ticks += 1

    def add_travel(self, tid: str, km: float) -> None:
        self._tt(tid).travel_km += km

    def add_fuel(self, tid: str, litres: float) -> None:
        self._tt(tid).fuel_used += litres

    def add_violation(self, t: int, kind: str, detail: str) -> dict:
        v = {"t": t, "kind": kind, "detail": detail}
        self.violations.append(v)
        return v

    def summary(self, state: WorldState) -> dict:
        jobs = list(state.jobs.values())
        done = [j for j in jobs if j.status == DONE]
        on_time = [j for j in done if j.completed_at is not None and j.completed_at <= j.deadline]
        tot = self.per_tractor.values()
        return {
            "ticks": self.ticks,
            "jobs_total": len(jobs),
            "jobs_completed": len(done),
            "deadlines_met": len(on_time),
            "priority_weighted_work": round(sum(j.priority * (1.0 - j.remaining) for j in jobs), 2),
            "priority_weighted_max": sum(j.priority for j in jobs),
            "travel_ticks": sum(t.travel_ticks for t in tot),
            "travel_km": round(sum(t.travel_km for t in tot), 2),
            "idle_ticks": sum(t.idle_ticks for t in tot),
            "fuel_used": round(sum(t.fuel_used for t in tot), 1),
            "violations": len(self.violations),
            "replans": self.replans,
            "per_tractor": {k: vars(v) | {"travel_km": round(v.travel_km, 2), "fuel_used": round(v.fuel_used, 1)}
                            for k, v in sorted(self.per_tractor.items())},
        }
