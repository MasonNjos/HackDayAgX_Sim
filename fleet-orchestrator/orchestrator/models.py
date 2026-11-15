"""Shared data model and dispatcher contract.

SHARED FILE: Person A and Person B change this only together (see plan/TEAM_SPLIT.md).

Units: 1 tick = 1 sim minute. Distances in km. Fuel in litres. Area in hectares.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# --- status values -----------------------------------------------------------

IDLE, TRAVELLING, WORKING, REFUELLING, BROKEN = "idle", "travelling", "working", "refuelling", "broken"
TRACTOR_STATUSES = {IDLE, TRAVELLING, WORKING, REFUELLING, BROKEN}

PENDING, ASSIGNED, IN_PROGRESS, DONE, UNASSIGNABLE = "pending", "assigned", "in_progress", "done", "unassignable"
JOB_STATUSES = {PENDING, ASSIGNED, IN_PROGRESS, DONE, UNASSIGNABLE}

EVENT_KINDS = {"breakdown", "repair", "route_blocked", "route_unblocked", "priority_change", "new_job"}


# --- world -------------------------------------------------------------------

@dataclass
class Node:
    id: str
    x: float
    y: float


@dataclass
class Edge:
    id: str
    a: str          # node id
    b: str          # node id
    length: float   # km
    label: str = ""  # e.g. "bridge"


@dataclass
class Depot:
    id: str
    node: str       # refuelling happens here


@dataclass
class Field:
    id: str
    name: str
    node: str       # road node the field is entered from
    area: float     # hectares


@dataclass
class Tractor:
    id: str
    implement: str               # "sprayer" | "mower" | ...
    capabilities: list[str]      # job types it can do, e.g. ["spray"]
    speed: float                 # km per tick
    work_rate: float             # hectares per tick while working
    fuel: float                  # litres now
    fuel_capacity: float
    burn_per_km: float
    burn_per_work_tick: float
    reserve: float = 0.10        # fraction of capacity never planned below
    location: str = ""           # node id: current node, or last node passed while travelling
    status: str = IDLE
    route: list[str] = field(default_factory=list)  # remaining nodes to visit (sim-owned)
    edge_progress: float = 0.0   # km travelled along the current edge (sim-owned)
    current_job: str | None = None
    # running totals for metrics (sim-owned)
    travel_km: float = 0.0
    travel_ticks: int = 0
    work_ticks: int = 0
    idle_ticks: int = 0

    @property
    def reserve_fuel(self) -> float:
        return self.reserve * self.fuel_capacity


@dataclass
class Job:
    id: str
    type: str                    # "spray" | "mow" | ...
    field: str                   # field id
    priority: int                # 1 low .. 3 high
    deadline: int                # tick
    submitted_at: int = 0        # tick; FCFS order
    remaining: float = 1.0       # fraction of the job still to do (0..1)
    status: str = PENDING
    owner: str | None = None     # tractor id; exactly one owner or None
    completed_at: int | None = None
    completed_by: list[str] = field(default_factory=list)  # every tractor that did part of it


@dataclass
class Event:
    t: int
    kind: str                    # one of EVENT_KINDS
    params: dict = field(default_factory=dict)
    # breakdown/repair: {"tractor": "T2"}
    # route_blocked/route_unblocked: {"edge": "bridge"}
    # priority_change: {"job": "J7", "priority": 3}
    # new_job: {"job": {...Job fields...}}


@dataclass
class WorldState:
    t: int
    nodes: dict[str, Node]
    edges: dict[str, Edge]
    depots: dict[str, Depot]
    fields: dict[str, Field]
    tractors: dict[str, Tractor]
    jobs: dict[str, Job]
    blocked_edges: set[str] = field(default_factory=set)

    def open_jobs(self) -> list[Job]:
        """Jobs that still need work (not done, not given up)."""
        return [j for j in self.jobs.values() if j.status not in (DONE, UNASSIGNABLE)]

    def active_tractors(self) -> list[Tractor]:
        return [t for t in self.tractors.values() if t.status != BROKEN]

    def job_node(self, job_id: str) -> str:
        return self.fields[self.jobs[job_id].field].node


# --- dispatcher contract -----------------------------------------------------

@dataclass
class Stop:
    kind: str     # "job" | "refuel"
    target: str   # job id or depot id


@dataclass
class Plan:
    queues: dict[str, list[Stop]]                      # tractor id -> ordered stops
    explanation: str = ""                              # plain-language summary of what changed
    warnings: list[str] = field(default_factory=list)  # deadline risks, unassignable jobs + reason
    why: dict[str, dict] = field(default_factory=dict) # job id -> scoring record for /why/<job>


class Dispatcher(Protocol):
    name: str

    def plan(self, state: WorldState, trigger: Event | None) -> Plan:
        """Return the full plan for all tractors.

        Called by the sim at start (trigger=None), on every event, and whenever
        a tractor finishes its queue. The sim executes the head of each queue and
        never interrupts a tractor's in-progress job unless it has broken down.
        A queue whose head is the tractor's current job means "keep going".
        """
        ...


# --- scenario loading --------------------------------------------------------

def job_from_dict(d: dict) -> Job:
    return Job(**d)


def load_scenario(path: str | Path) -> tuple[WorldState, list[Event], dict]:
    """Load scenarios/*.json. Returns (initial state, scripted events, meta)."""
    raw = json.loads(Path(path).read_text())
    depots = {d["id"]: Depot(**d) for d in raw["depots"]}
    tractors = {}
    for d in raw["tractors"]:
        t = Tractor(**d)
        if not t.location:
            t.location = next(iter(depots.values())).node
        tractors[t.id] = t
    state = WorldState(
        t=0,
        nodes={d["id"]: Node(**d) for d in raw["nodes"]},
        edges={d["id"]: Edge(**d) for d in raw["edges"]},
        depots=depots,
        fields={d["id"]: Field(**d) for d in raw["fields"]},
        tractors=tractors,
        jobs={d["id"]: job_from_dict(d) for d in raw["jobs"]},
    )
    events = sorted((Event(**e) for e in raw.get("events", [])), key=lambda e: e.t)
    return state, events, raw.get("meta", {})
