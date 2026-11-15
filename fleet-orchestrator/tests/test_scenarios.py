"""Scenario tests for the Fleet Agent on hand-built states (no sim needed)."""
from pathlib import Path

import pytest

from orchestrator.fleet_agent import FleetAgent
from orchestrator.models import ASSIGNED, BROKEN, IN_PROGRESS, TRAVELLING, WORKING, Event, Job, Plan, Stop, load_scenario
from orchestrator.safety_agent import compatible, simulate_queue, validate

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "farm.json"


@pytest.fixture
def state():
    return load_scenario(SCENARIO)[0]


def owners(plan: Plan) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for tid, q in plan.queues.items():
        for s in q:
            if s.kind == "job":
                out.setdefault(s.target, []).append(tid)
    return out


def assert_safe(plan, state):
    assert validate(plan, state) == []
    assert not [w for w in plan.warnings if w.startswith("SAFETY")]
    for job_id, ts in owners(plan).items():
        assert len(ts) == 1, f"{job_id} owned by {ts}"
        assert compatible(state.tractors[ts[0]], state.jobs[job_id])


def test_initial_plan_assigns_every_job_once(state):
    plan = FleetAgent().plan(state, None)
    assert_safe(plan, state)
    assert set(owners(plan)) == set(state.jobs)
    for j in state.jobs.values():
        if j.type == "mow":
            assert owners(plan)[j.id] == ["T3"]
    assert plan.why["J6"]["chosen"] in ("T1", "T2")


def test_urgent_job_goes_first(state):
    plan = FleetAgent().plan(state, None)
    tid = owners(plan)["J6"][0]
    assert plan.queues[tid][0] == Stop("job", "J6")


def test_plan_does_not_mutate_state(state):
    before = repr(state)
    FleetAgent().plan(state, None)
    assert repr(state) == before


def test_breakdown_mid_job_moves_work_without_duplicates(state):
    t2, j4 = state.tractors["T2"], state.jobs["J4"]
    state.t = 60
    t2.status, t2.current_job = BROKEN, None
    j4.status, j4.remaining, j4.owner = IN_PROGRESS, 0.4, None
    plan = FleetAgent().plan(state, Event(60, "breakdown", {"tractor": "T2"}))
    assert_safe(plan, state)
    assert plan.queues["T2"] == []
    assert owners(plan)["J4"] == ["T1"]
    # partial progress carries over: 40% of 25 ha at 0.5 ha/tick = 20 ticks of work
    step = simulate_queue(state.tractors["T1"], plan.queues["T1"], state).step_for("J4")
    assert step.work_ticks == 20


def test_working_tractor_keeps_its_current_job(state):
    t3 = state.tractors["T3"]
    t3.status, t3.current_job, t3.location = WORKING, "J7", "se"
    state.jobs["J7"].status = IN_PROGRESS
    plan = FleetAgent().plan(state, None)
    assert_safe(plan, state)
    assert plan.queues["T3"][0] == Stop("job", "J7")


def test_only_mower_broken_reports_mow_jobs_unassignable(state):
    state.tractors["T3"].status = BROKEN
    plan = FleetAgent().plan(state, Event(0, "breakdown", {"tractor": "T3"}))
    assert_safe(plan, state)
    mow = {j.id for j in state.jobs.values() if j.type == "mow"}
    assert not mow & set(owners(plan))
    for jid in mow:
        msg = next(w for w in plan.warnings if w.startswith(f"{jid} unassignable"))
        assert "T3 is broken down" in msg
        assert plan.why[jid]["chosen"] is None


def test_blocked_bridge_still_valid(state):
    state.blocked_edges = {"bridge"}
    plan = FleetAgent().plan(state, Event(0, "route_blocked", {"edge": "bridge"}))
    assert_safe(plan, state)
    assert set(owners(plan)) == set(state.jobs)


def test_low_fuel_tractor_refuels_before_working(state):
    t1 = state.tractors["T1"]
    t1.fuel = t1.reserve_fuel + 1
    plan = FleetAgent().plan(state, None)
    assert_safe(plan, state)
    q = plan.queues["T1"]
    assert q and q[0].kind == "refuel"


def test_validate_catches_violations(state):
    bad = Plan(queues={
        "T1": [Stop("job", "J6"), Stop("job", "J3")],   # J3 is mowing: wrong implement
        "T2": [Stop("job", "J6")],                      # J6 has two owners
        "T3": [],
    })
    v = validate(bad, state)
    assert any("two owners" in x for x in v)
    assert any("J3 on T1" in x for x in v)


def test_validate_catches_fuel_and_broken(state):
    state.tractors["T1"].fuel = 21
    state.tractors["T2"].status = BROKEN
    bad = Plan(queues={"T1": [Stop("job", "J1")], "T2": [Stop("job", "J2")], "T3": []})
    v = validate(bad, state)
    assert any(x.startswith("T1:") and "reserve" in x for x in v)
    assert any("T2 is broken" in x for x in v)


def test_tractor_below_reserve_can_still_refuel(state):
    t1 = state.tractors["T1"]
    t1.fuel = t1.reserve_fuel - 1          # already under reserve, parked at the depot
    plan = FleetAgent().plan(state, None)
    assert_safe(plan, state)
    assert plan.queues["T1"] and plan.queues["T1"][0] == Stop("refuel", "D1")


def test_low_fuel_after_current_job_gets_refuel(state):
    t1 = state.tractors["T1"]
    t1.status, t1.current_job, t1.location, t1.fuel = WORKING, "J6", "west", 30
    state.jobs["J6"].status = IN_PROGRESS
    plan = FleetAgent().plan(state, None)
    assert_safe(plan, state)
    q = plan.queues["T1"]
    assert q[0] == Stop("job", "J6")
    assert len(q) > 1 and q[1].kind == "refuel"


def test_unreachable_current_job_is_released(state):
    t1 = state.tractors["T1"]
    t1.status, t1.current_job = TRAVELLING, "J4"   # driving from the depot towards River Flats
    state.jobs["J4"].status = ASSIGNED
    state.blocked_edges = {"bridge", "south_road"}  # east side cut off completely
    plan = FleetAgent().plan(state, Event(0, "route_blocked", {"edge": "south_road"}))
    assert_safe(plan, state)
    assert "J4" not in owners(plan)
    assert any(w.startswith("J4 unassignable") and "no open road" in w for w in plan.warnings)
    assert plan.queues["T1"]                        # T1 picks up west-side work instead


def test_job_on_unknown_field_is_reported_not_crashing(state):
    state.jobs["J99"] = Job(id="J99", type="spray", field="F99", priority=2, deadline=300)
    plan = FleetAgent().plan(state, Event(0, "new_job", {}))
    assert_safe(plan, state)
    assert "J99" not in owners(plan)
    assert any(w.startswith("J99 unassignable: unknown field F99") for w in plan.warnings)


# --- through the real sim: breakdown recovery -----------------------------------

from orchestrator.models import DONE, PENDING  # noqa: E402
from orchestrator.sim import Sim  # noqa: E402


def sim_until_working(tid: str, job_type: str, extra_events=(), setup=None):
    """Scenario without its scripted breakdown, stepped until tid is part-way through a job."""
    state, events, meta = load_scenario(SCENARIO)
    if setup:
        setup(state)
    events = [e for e in events if e.kind != "breakdown"] + list(extra_events)
    sim = Sim(state, events, FleetAgent(), horizon=meta.get("horizon", 480))
    for _ in range(400):
        sim.step()
        tr = state.tractors[tid]
        job = state.jobs.get(tr.current_job) if tr.current_job else None
        if tr.status == WORKING and job and job.type == job_type and 0.2 < job.remaining < 0.8:
            return sim, job
    raise AssertionError(f"{tid} never worked a {job_type} job")


def break_now(sim, tid):
    sim.inject(Event(sim.t, "breakdown", {"tractor": tid}))
    sim.step()


def test_sim_breakdown_of_only_mower_reports_unassignable(state):
    sim, job = sim_until_working("T3", "mow")
    t_break, remaining = sim.t, job.remaining
    break_now(sim, "T3")
    s = sim.state
    assert s.tractors["T3"].status == BROKEN
    assert job.status == PENDING and job.owner is None
    assert job.remaining == pytest.approx(remaining)          # partial work kept
    open_mow = [j for j in s.jobs.values() if j.type == "mow" and j.status != DONE]
    assert job in open_mow
    for j in open_mow:
        assert all(st.target != j.id for q in sim.queues.values() for st in q)
        msg = next(w for w in sim.last_warnings if w.startswith(f"{j.id} unassignable"))
        assert "T3 is broken down" in msg
        assert sim.why[j.id]["chosen"] is None
    assert sim.last_explanation.startswith(f"T3 broke down at t={t_break} while mowing")
    assert f"{round((1 - remaining) * 100)}% done" in sim.last_explanation
    assert "cannot be assigned right now" in sim.last_explanation
    assert sim.metrics.violations == []


def test_sim_repair_puts_unassignable_mow_jobs_back(state):
    sim, job = sim_until_working("T3", "mow")
    break_now(sim, "T3")
    sim.inject(Event(sim.t + 10, "repair", {"tractor": "T3"}))
    sim.run()
    assert job.status == DONE and job.completed_by == ["T3"]
    assert sim.metrics.violations == []


def test_sim_breakdown_mid_spray_moves_work_to_other_sprayer(state):
    sim, job = sim_until_working("T2", "spray")
    remaining = job.remaining
    break_now(sim, "T2")
    assert job.remaining == pytest.approx(remaining)
    owners_now = [tid for tid, q in sim.queues.items() for st in q if st.target == job.id]
    assert owners_now == ["T1"]
    exp = sim.last_explanation
    assert exp.startswith("T2 broke down") and f"{round((1 - remaining) * 100)}% done" in exp
    assert "Its remaining work went to T1" in exp
    assert sim.metrics.violations == []
    sim.run()
    assert job.status == DONE and job.completed_by == ["T2", "T1"]
    assert sim.metrics.violations == []


def test_invalid_plan_falls_back_to_a_safe_one(state, monkeypatch):
    agent = FleetAgent()
    first = agent.plan(state, None)
    bad = {"T1": [Stop("job", "J3")], "T2": [Stop("job", "J3")], "T3": []}  # wrong implement + two owners
    monkeypatch.setattr(agent, "_build", lambda s, t: (bad, {}, []))
    plan = agent.plan(state, None)
    assert validate(plan, state) == []
    assert plan.queues == first.queues
    assert any(w.startswith("SAFETY: plan rejected") for w in plan.warnings)
    assert plan.explanation.startswith("Safety check rejected the new plan")



def test_sim_breakdown_after_refuel_is_described_as_working(state):
    """T1 starts just above reserve, so its first plan is [refuel D1, J6, ...]. Refuelling and
    starting J6 don't trigger a replan, so the agent's last plan still has the refuel at its head."""
    def low_t1(s):
        s.tractors["T1"].fuel = s.tractors["T1"].reserve_fuel + 1
    sim, job = sim_until_working("T1", "spray", setup=low_t1)
    last = sim.dispatcher._last_queues["T1"]
    assert last and last[0].kind == "refuel"          # the stale head this test is about
    t_break, remaining = sim.t, job.remaining
    break_now(sim, "T1")
    exp = sim.last_explanation
    assert exp.startswith(f"T1 broke down at t={t_break} while spraying")
    assert f"{round((1 - remaining) * 100)}% done" in exp
    assert "Its remaining work went to T2" in exp
    assert sim.metrics.violations == []


def test_fallback_keeps_unassignable_warnings_and_marks_held_back_jobs(state, monkeypatch):
    agent = FleetAgent()
    agent.plan(state, None)
    # Cut the east side off so the previous plan is no longer valid either: the agent
    # must fall back to "jobs in progress only" (nothing is in progress at t=0).
    state.blocked_edges = {"bridge", "south_road"}
    bad = {"T1": [Stop("job", "J3")], "T2": [], "T3": []}
    rejected_why = {"J5": {"chosen": None, "eligible": [], "rejected": {"T3": "T3 is broken down"}}}
    monkeypatch.setattr(agent, "_build", lambda s, t: (bad, rejected_why, ["J5 unassignable (test)"]))
    plan = agent.plan(state, Event(0, "route_blocked", {"edge": "south_road"}))
    assert validate(plan, state) == []
    assert all(q == [] for q in plan.queues.values())
    assert "J5 unassignable (test)" in plan.warnings
    assert any(w.startswith("SAFETY: plan rejected") and "only the jobs in progress" in w for w in plan.warnings)
    assert plan.why["J5"]["rejected"] == {"T3": "T3 is broken down"}
    for jid in ("J1", "J2", "J4"):
        assert plan.why[jid]["chosen"] is None and "held back" in plan.why[jid]["reason"]
    assert "No change to assignments" not in plan.explanation
    assert "held back because the new plan failed the safety check" in plan.explanation


def test_fallback_previous_plan_skips_refuel_already_done(state, monkeypatch):
    t1 = state.tractors["T1"]
    t1.fuel = t1.reserve_fuel + 1
    agent = FleetAgent()
    first = agent.plan(state, None)
    assert first.queues["T1"][0].kind == "refuel"
    t1.fuel = t1.fuel_capacity                        # refuelled since that plan
    monkeypatch.setattr(agent, "_build", lambda s, t: ({"T1": [Stop("job", "J3")], "T2": [], "T3": []}, {}, []))
    plan = agent.plan(state, None)
    assert validate(plan, state) == []
    assert plan.queues["T1"] and plan.queues["T1"][0].kind == "job"
