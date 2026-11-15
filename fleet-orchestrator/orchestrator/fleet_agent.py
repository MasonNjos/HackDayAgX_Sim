"""Fleet Agent: assigns jobs to tractors and replans on events. Core of the challenge.

Owner: Person B. STUB: returns empty queues so the sim runs from minute one.
Replace with: eligibility (safety_agent predicates) -> feasibility/ETA -> greedy by urgency.
"""
from __future__ import annotations

from .models import Event, Plan, WorldState


class FleetAgent:
    name = "coordinator"

    def plan(self, state: WorldState, trigger: Event | None) -> Plan:
        return Plan(
            queues={t.id: [] for t in state.tractors.values()},
            explanation="(stub) coordinator not implemented yet",
        )
