"""Smoke tests for the shared contract: scenario loads, graph is sane, stub dispatcher conforms."""
from pathlib import Path

from orchestrator.fleet_agent import FleetAgent
from orchestrator.models import EVENT_KINDS, Plan, load_scenario
from orchestrator.nav_agent import route

SCENARIO = Path(__file__).parent.parent / "scenarios" / "farm.json"


def test_scenario_references_are_valid():
    state, events, meta = load_scenario(SCENARIO)
    for e in state.edges.values():
        assert e.a in state.nodes and e.b in state.nodes
    for f in state.fields.values():
        assert f.node in state.nodes
    for d in state.depots.values():
        assert d.node in state.nodes
    for j in state.jobs.values():
        assert j.field in state.fields
        assert any(j.type in t.capabilities for t in state.tractors.values()), j.id
    for ev in events:
        assert ev.kind in EVENT_KINDS
    assert 2 <= len(state.tractors) <= 3 and 5 <= len(state.fields) <= 10


def test_every_field_reachable_and_bridge_detour():
    state, _, _ = load_scenario(SCENARIO)
    for f in state.fields.values():
        assert route(state, "depot", f.node) is not None
    _, direct = route(state, "west", "east")
    state.blocked_edges.add("bridge")
    path, detour = route(state, "west", "east")
    assert "sw" in path and detour > direct


def test_stub_dispatcher_returns_plan_for_every_tractor():
    state, _, _ = load_scenario(SCENARIO)
    plan = FleetAgent().plan(state, None)
    assert isinstance(plan, Plan)
    assert set(plan.queues) == set(state.tractors)
