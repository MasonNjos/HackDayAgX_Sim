"""Live-server planning (Person A): plans are computed off-lock and committed only if still fresh."""
import threading
import time
from pathlib import Path

import pytest

from orchestrator.fleet_agent import FleetAgent
from orchestrator.models import Event, load_scenario
from orchestrator.sim import Sim

server = pytest.importorskip("server")

FARM = Path(__file__).parent.parent / "scenarios" / "farm.json"


def drive_deferred(sim: Sim) -> None:
    """What the server thread does, without threads: plan off the live state, then commit."""
    while not sim.finished():
        req = sim.take_plan_request()
        if req is None:
            sim.step()
            continue
        trigger, state_copy, version = req
        sim.commit_plan(sim.dispatcher.plan(state_copy, trigger), trigger, version)


def test_deferred_run_matches_offline_run():
    st, ev, meta = load_scenario(FARM)
    offline = Sim(st, ev, FleetAgent(), horizon=meta["horizon"]).run()

    st, ev, meta = load_scenario(FARM)
    sim = Sim(st, ev, FleetAgent(), horizon=meta["horizon"])
    sim.defer_plans = True
    drive_deferred(sim)
    live = sim.metrics.summary(sim.state)
    for key in ("jobs_completed", "deadlines_met", "travel_km", "idle_ticks", "violations", "replans"):
        assert live[key] == offline[key], key


def test_stale_plan_is_discarded_and_replanned():
    st, ev, meta = load_scenario(FARM)
    sim = Sim(st, [], FleetAgent(), horizon=meta["horizon"])
    sim.defer_plans = True
    sim.step()                                   # start: requests the first plan
    trigger, copy_, version = sim.take_plan_request()
    plan = sim.dispatcher.plan(copy_, trigger)
    sim.apply_event_now(Event(sim.t, "breakdown", {"tractor": "T1"}))  # world changes meanwhile
    assert sim.commit_plan(plan, trigger, version) is False
    assert sim.plan_requests, "a fresh plan must be requested"
    assert all(not q for q in sim.queues.values())  # stale plan not applied


class SlowAgent(FleetAgent):
    name = "slow"

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()

    def plan(self, state, trigger):
        self.entered.set()
        time.sleep(0.5)
        return super().plan(state, trigger)


def test_state_stays_readable_while_planning(monkeypatch):
    monkeypatch.setitem(server.DISPATCHERS, "slow", SlowAgent)
    runner = server.Runner()
    runner.reset("slow")
    runner.paused = False
    t = threading.Thread(target=runner.tick)  # first tick: start -> plan (slowly, off-lock) -> step
    t.start()
    assert runner.sim.dispatcher.entered.wait(2)
    start = time.monotonic()
    with runner.lock:                          # what GET /api/state does
        assert runner.planning is not None
    assert time.monotonic() - start < 0.2, "lock was held during planning"
    t.join()
    assert runner.planning is None
    assert runner.sim.metrics.replans >= 1 and runner.sim.t == 1  # one tick() == one minute


def test_dashboard_breakdown_reaches_maintenance_agent():
    runner = server.Runner()
    runner.reset("coordinator")
    sim = runner.sim
    sim.defer_plans = False
    sim.step()
    sim.apply_event_now(Event(sim.t, "breakdown", {"tractor": "T2"}))
    assert any(e.kind == "repair" and e.params.get("tractor") == "T2" for e in sim.pending_events)
