"""Sim + baseline smoke tests (Person A). Invariant/scenario tests for the coordinator are B's."""
from pathlib import Path

from orchestrator import nav_agent as nav
from orchestrator.baseline import FCFSDispatcher
from orchestrator.models import BROKEN, DONE, Event, Plan, Stop, load_scenario
from orchestrator.sim import Sim

FARM = Path(__file__).parent.parent / "scenarios" / "farm.json"


def make(dispatcher, events=None):
    state, evs, meta = load_scenario(FARM)
    return Sim(state, evs if events is None else events, dispatcher, horizon=meta["horizon"])


def test_nav_avoids_blocked_bridge():
    state, _, _ = load_scenario(FARM)
    _, short = nav.route(state, "west", "east")
    state.blocked_edges.add("bridge")
    path, detour = nav.route(state, "west", "east")
    assert "east" in path and detour > short


def test_baseline_runs_and_keeps_single_owner():
    sim = make(FCFSDispatcher())
    while not sim.finished():
        sim.step()
        owners = [st.target for q in sim.queues.values() for st in q if st.kind == "job"]
        assert len(owners) == len(set(owners))
        for tr in sim.state.tractors.values():
            assert tr.fuel >= 0
    assert sum(j.status == DONE for j in sim.state.jobs.values()) > 0


def test_breakdown_releases_job_and_keeps_progress():
    class OneJob:
        name = "one"
        def plan(self, state, trigger):
            q = {t: [] for t in state.tractors}
            if state.tractors["T2"].status != BROKEN and state.jobs["J6"].status != DONE:
                q["T2"] = [Stop("job", "J6")]
            return Plan(queues=q)

    sim = make(OneJob(), events=[Event(t=20, kind="breakdown", params={"tractor": "T2"})])
    for _ in range(21):
        sim.step()
    j6 = sim.state.jobs["J6"]
    assert sim.state.tractors["T2"].status == BROKEN
    assert j6.owner is None and j6.status == "pending"
    assert 0 < j6.remaining < 1  # partial work kept


def test_bad_events_do_not_crash_or_overwrite():
    sim = make(FCFSDispatcher(), events=[])
    sim.step()
    sim.inject(Event(t=sim.t, kind="priority_change", params={"job": "NOPE", "priority": 3}))
    sim.inject(Event(t=sim.t, kind="new_job", params={"job": {"id": "J2", "type": "spray", "field": "F1",
                                                               "priority": 1, "deadline": 999}}))
    for _ in range(25):
        sim.step()
    assert sim.state.jobs["J2"].priority == 2 and sim.state.jobs["J2"].remaining < 1


def test_in_progress_job_survives_plan_that_omits_it():
    class Forgetful:
        name = "forgetful"
        calls = 0
        def plan(self, state, trigger):
            self.calls += 1
            q = {t: [] for t in state.tractors}
            if self.calls == 1:
                q["T2"] = [Stop("job", "J6")]
            return Plan(queues=q)

    sim = make(Forgetful(), events=[])
    while sim.state.jobs["J6"].status != "in_progress":
        sim.step()
    sim.state.tractors["T2"].status = "refuelling"  # not WORKING this tick
    sim.replan(None)
    assert sim.state.jobs["J6"].owner == "T2" and sim.queues["T2"][0].target == "J6"


def test_unreachable_stop_released_and_replanned():
    sim = make(FCFSDispatcher(), events=[])
    sim.state.blocked_edges.update({"bridge", "south_road"})  # east side cut off
    for _ in range(5):
        sim.step()
    j1 = sim.state.jobs["J1"]  # F7 is east of the river
    assert j1.owner is None and j1.status == "pending"
    assert any(r["kind"] == "unreachable" for r in sim.log.records)


def test_refuel_wait_resets_when_depot_changes():
    from orchestrator.models import REFUEL_TICKS, Depot

    class Refuel:
        name = "refuel"
        depot = "D1"
        def plan(self, state, trigger):
            return Plan(queues={"T1": [Stop("refuel", self.depot)], "T2": [], "T3": []})

    d = Refuel()
    sim = make(d, events=[])
    sim.state.depots["D2"] = Depot("D2", "west")  # 2 km from the start depot
    sim.state.tractors["T1"].fuel = 50
    for _ in range(REFUEL_TICKS - 1):  # wait at D1, one tick short of full
        sim.step()
    assert sim.state.tractors["T1"].fuel == 50
    d.depot = "D2"
    sim.replan(None)
    sim.step()  # first tick travelling to D2 must not finish the refuel
    assert sim.state.tractors["T1"].fuel < 50
    while sim.state.tractors["T1"].location != "west":
        sim.step()
    for _ in range(REFUEL_TICKS - 1):
        sim.step()
        assert sim.state.tractors["T1"].fuel < 200  # full wait at D2 required
    sim.step()
    assert sim.state.tractors["T1"].fuel == 200


def test_sim_rejects_bad_plans():
    class Bad:
        name = "bad"
        def plan(self, state, trigger):
            # J3 is a mow job: sprayer T1 is incompatible; J2 double-assigned.
            return Plan(queues={"T1": [Stop("job", "J3"), Stop("job", "J2")], "T2": [Stop("job", "J2")], "T3": []})

    sim = make(Bad(), events=[])
    sim.step()
    kinds = {v["kind"] for v in sim.metrics.violations}
    assert {"incompatible", "double_owner"} <= kinds
    assert sim.state.jobs["J2"].owner == "T1"
    assert sim.state.jobs["J3"].owner is None


def test_stranded_without_fuel_counts_as_idle_not_out_of_service():
    from pathlib import Path
    from orchestrator.metrics import Metrics
    from orchestrator.models import BROKEN, load_scenario
    state = load_scenario(Path(__file__).resolve().parent.parent / "scenarios" / "farm.json")[0]
    state.tractors["T1"].status = BROKEN   # ran dry
    state.tractors["T2"].status = BROKEN   # broke down
    m = Metrics()
    m.record_tick(state, {"T1"})
    t1, t2 = m.per_tractor["T1"], m.per_tractor["T2"]
    assert (t1.idle_ticks, t1.stranded_ticks, t1.broken_ticks) == (1, 1, 0)
    assert (t2.idle_ticks, t2.stranded_ticks, t2.broken_ticks) == (0, 0, 1)
    assert m.summary(state)["stranded_ticks"] == 1


def _depot_nodes(sim):
    return {d.node for d in sim.state.depots.values()}


def test_day_end_all_tractors_park_at_depot():
    from orchestrator.fleet_agent import FleetAgent
    from orchestrator.maintenance_agent import MaintenanceAgent
    for dispatcher in (FleetAgent(), FCFSDispatcher()):
        sim = make(dispatcher)
        MaintenanceAgent().attach(sim)  # repairs the scripted breakdown, as in run_demo
        sim.run()
        assert sim.t < sim.horizon, "run should end once everyone is parked, not at the horizon"
        assert not sim.state.open_jobs()
        for tid, tr in sim.state.tractors.items():
            if tr.status != BROKEN:
                assert tr.location in _depot_nodes(sim) and sim.legs[tid] is None, tid
                assert tr.status == "complete", tid
        kinds = [r["kind"] for r in sim.log.records]
        assert "returning_home" in kinds and "parked" in kinds and kinds.count("day_complete") == 1


def test_day_end_idle_tractor_waits_while_jobs_are_open():
    class Nothing:
        name = "nothing"
        def plan(self, state, trigger):
            return Plan(queues={t: [] for t in state.tractors})
    state, _, meta = load_scenario(FARM)
    tr = state.tractors["T1"]
    tr.location = "east"  # away from the depot, with jobs still open
    sim = Sim(state, [], Nothing(), horizon=meta["horizon"])
    for _ in range(3):
        sim.step()
    assert tr.location == "east" and sim.legs["T1"] is None and "T1" not in sim.going_home
    for j in state.jobs.values():
        j.status, j.remaining = DONE, 0.0
    sim.step()
    assert "T1" in sim.going_home  # day over: heads home
