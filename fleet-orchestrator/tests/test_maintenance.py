"""Maintenance agent: repair crews are sent automatically and the fleet replans on the fix."""
from pathlib import Path

from orchestrator.fleet_agent import FleetAgent
from orchestrator.maintenance_agent import MaintenanceAgent
from orchestrator.models import BROKEN, Event, load_scenario
from orchestrator.sim import Sim

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "farm.json"


def make_sim(events=None, **kw):
    state, scripted, meta = load_scenario(SCENARIO)
    sim = Sim(state, scripted if events is None else events, FleetAgent(), horizon=meta.get("horizon", 480))
    MaintenanceAgent(**kw).attach(sim)
    return sim


def run_until(sim, t):
    while sim.t <= t and not sim.finished():
        sim.step()


def test_broken_tractor_is_repaired_and_is_eligible_again():
    sim = make_sim([Event(60, "breakdown", {"tractor": "T2"})], tractor_min=45)
    run_until(sim, 60)
    assert sim.state.tractors["T2"].status == BROKEN
    assert sim.maintenance.pending() == [{"kind": "tractor", "id": "T2", "fixed_at": 105}]
    assert any(r["kind"] == "maintenance" and r["fixed_at"] == 105 for r in sim.log.records)
    run_until(sim, 105)
    assert sim.state.tractors["T2"].status != BROKEN
    assert sim.maintenance.pending() == []
    # The replan on the repair considers T2 again (greedy may still prefer another tractor).
    replanned = [r for r in sim.why.values() if r.get("planned_at") == 105 and r.get("chosen")]
    assert replanned
    assert all("broken" not in r.get("rejected", {}).get("T2", "") for r in replanned)
    assert any("T2" in r.get("eligible", []) for r in replanned)


def test_blocked_bridge_is_reopened():
    sim = make_sim([Event(150, "route_blocked", {"edge": "bridge"})], road_min=60)
    run_until(sim, 150)
    assert "bridge" in sim.state.blocked_edges
    run_until(sim, 210)
    assert "bridge" not in sim.state.blocked_edges


def test_one_crew_per_target():
    sim = make_sim([Event(10, "breakdown", {"tractor": "T1"}), Event(12, "breakdown", {"tractor": "T1"})])
    run_until(sim, 12)
    repairs = [e for e in sim.pending_events if e.kind == "repair"]
    assert len(repairs) == 1


def test_scripted_day_with_repairs_has_no_violations_and_beats_no_repairs():
    with_repairs = make_sim()
    with_repairs.run()
    state, events, meta = load_scenario(SCENARIO)
    without = Sim(state, events, FleetAgent(), horizon=meta.get("horizon", 480))
    without.run()
    a, b = with_repairs.metrics.summary(with_repairs.state), without.metrics.summary(without.state)
    assert a["violations"] == 0
    assert a["jobs_completed"] >= b["jobs_completed"]


def test_tractor_with_no_work_is_still_repaired():
    sim = make_sim([], tractor_min=5)
    while not sim.finished():
        sim.step()
    sim.inject(Event(sim.t, "breakdown", {"tractor": "T2"}))   # day is over, no work left
    for _ in range(10):
        if sim.finished():
            break
        sim.step()
    assert sim.state.tractors["T2"].status != BROKEN


def test_tractor_that_ran_out_of_fuel_is_refuelled_and_repaired():
    from orchestrator.baseline import FCFSDispatcher          # the baseline runs dry in the scripted day
    state, events, meta = load_scenario(SCENARIO)
    sim = Sim(state, events, FCFSDispatcher(), horizon=meta.get("horizon", 480))
    MaintenanceAgent(tractor_min=5).attach(sim)
    stalled_at = None
    while not sim.finished():
        sim.step()
        if stalled_at is None and "out_of_fuel" in sim.stall_reason.values():
            stalled_at = sim.t
            tid = next(t for t, r in sim.stall_reason.items() if r == "out_of_fuel")
    assert stalled_at is not None, "scenario no longer makes the baseline run dry"
    fixes = [r for r in sim.log.records if r["kind"] == "maintenance" and r.get("target") == tid]
    assert any(r["action"] == "repair_crew_dispatched" and "out of fuel" in r["detail"] for r in fixes)
    assert any(r["action"] == "fuel_delivered" for r in fixes)
    assert sim.state.tractors[tid].status != BROKEN


def test_dashboard_style_event_gets_a_repair():
    """The server applies dashboard events through sim.process_event, outside step()."""
    sim = make_sim([], road_min=10)
    run_until(sim, 20)
    sim.process_event(Event(sim.t, "route_blocked", {"edge": "south_road"}))
    assert sim.maintenance.pending() == [{"kind": "edge", "id": "south_road", "fixed_at": sim.t + 10}]
    run_until(sim, sim.t + 10)
    assert "south_road" not in sim.state.blocked_edges


def test_road_blocked_without_an_event_is_still_fixed():
    sim = make_sim([], road_min=10)
    run_until(sim, 5)
    sim.state.blocked_edges |= {"e_south", "bridge"}      # bypasses events entirely
    run_until(sim, 30)
    assert not sim.state.blocked_edges & {"e_south", "bridge"}


def test_blocked_road_reopens_even_when_it_cuts_off_a_job():
    """Regression: both roads to Bottom Field blocked left J7 unreachable for good."""
    sim = make_sim([Event(30, "route_blocked", {"edge": "e_south"}),
                    Event(30, "route_blocked", {"edge": "south_road"})], road_min=10)
    sim.run()
    assert sim.state.jobs["J7"].status == "done"
