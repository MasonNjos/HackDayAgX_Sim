"""Maintenance agent: sends a repair crew as soon as something breaks. No human needed.

Owner: Person B.

- A tractor breaks down     -> a "repair" event is scheduled REPAIR_TRACTOR_MIN later.
  This holds whatever the tractor was doing, even with no work left, and also covers a
  tractor that stalled because it ran out of fuel (the crew brings fuel and fills the tank).
- A road (e.g. the bridge) is blocked -> a "route_unblocked" event is scheduled REPAIR_ROAD_MIN later.

The repair arrives as a normal sim event, so the coordinator (greedy or Claude) replans
and explains it like any other event. Repairs are part of the world: attach this to
every run, the baseline included, so the comparison stays fair.

    sim = Sim(...)
    MaintenanceAgent().attach(sim)
"""
from __future__ import annotations

import os

from .models import BROKEN, Event

REPAIR_TRACTOR_MIN = int(os.environ.get("REPAIR_TRACTOR_MIN", "5"))
REPAIR_ROAD_MIN = int(os.environ.get("REPAIR_ROAD_MIN", "10"))


class MaintenanceAgent:
    name = "maintenance"

    def __init__(self, tractor_min: int = REPAIR_TRACTOR_MIN, road_min: int = REPAIR_ROAD_MIN) -> None:
        self.tractor_min = tractor_min
        self.road_min = road_min
        # ("tractor"|"edge", id) -> the fix event this agent queued (its .t is when it lands)
        self.scheduled: dict[tuple[str, str], Event] = {}
        self.needs_fuel: set[str] = set()                 # tractors that stalled out of fuel

    def attach(self, sim) -> "MaintenanceAgent":
        sim.observers.append(self)
        sim.maintenance = self
        if hasattr(sim.dispatcher, "maintenance"):  # the Claude coordinator puts pending repairs in its brief
            sim.dispatcher.maintenance = self
        return self

    def pending(self) -> list[dict]:
        """Repairs on their way, for the Claude brief and the dashboard."""
        return [{"kind": k, "id": i, "fixed_at": ev.t} for (k, i), ev in sorted(self.scheduled.items())]

    def on_event(self, sim, ev: Event) -> None:
        p = ev.params
        if ev.kind == "breakdown":
            self._schedule(sim, "tractor", p.get("tractor"), self.tractor_min, "repair", {"tractor": p.get("tractor")})
        elif ev.kind == "route_blocked":
            self._schedule(sim, "edge", p.get("edge"), self.road_min, "route_unblocked", {"edge": p.get("edge")})
        elif ev.kind == "repair":
            tid = p.get("tractor")
            self._fixed(sim, ("tractor", tid), ev)
            tr = sim.state.tractors.get(tid)
            if tid in self.needs_fuel and tr is not None:
                self.needs_fuel.discard(tid)
                tr.fuel = tr.fuel_capacity
                sim.log.write(sim.t, "maintenance", action="fuel_delivered", target=tid,
                              detail=f"Repair crew filled {tid}'s tank ({tr.fuel_capacity:g} L).")
        elif ev.kind == "route_unblocked":
            self._fixed(sim, ("edge", p.get("edge")), ev)

    def _fixed(self, sim, key: tuple[str, str], ev: Event) -> None:
        """The target is fixed (by our crew, a dashboard click or the script).

        Cancel our crew's event if it is still queued, so a leftover crew can never
        repair a later breakdown of the same target early and wipe its booking.
        """
        booked = self.scheduled.pop(key, None)
        if booked is not None and booked is not ev:
            sim.pending_events = [e for e in sim.pending_events if e is not booked]

    def on_tick(self, sim) -> None:
        """Safety net: anything broken with no crew on the way gets one.

        Catches tractors that stalled out of fuel (no breakdown event) and any road
        blocked by a path that skipped on_event.
        """
        for edge in sorted(sim.state.blocked_edges):
            if ("edge", edge) not in self.scheduled:
                self._schedule(sim, "edge", edge, self.road_min, "route_unblocked", {"edge": edge})
        for tr in sim.state.tractors.values():
            if tr.status == BROKEN and ("tractor", tr.id) not in self.scheduled:
                reason = sim.stall_reason.get(tr.id, "breakdown")
                if reason == "out_of_fuel":
                    self.needs_fuel.add(tr.id)
                self._schedule(sim, "tractor", tr.id, self.tractor_min, "repair", {"tractor": tr.id},
                               why=reason.replace("_", " "))

    def _schedule(self, sim, kind: str, target: str | None, minutes: int, fix_kind: str, params: dict,
                  why: str = "") -> None:
        if target is None or (kind, target) in self.scheduled:
            return  # already a crew on the way
        at = sim.t + minutes
        fix = Event(at, fix_kind, params)
        self.scheduled[(kind, target)] = fix
        sim.inject(fix)
        what = f"tractor {target}" if kind == "tractor" else f"road {target}"
        sim.log.write(sim.t, "maintenance", action="repair_crew_dispatched", target=target,
                      fix=fix_kind, fixed_at=at,
                      detail=f"Repair crew sent to {what}{f' ({why})' if why else ''}; "
                             f"back in service at minute {at} ({minutes} min).")
