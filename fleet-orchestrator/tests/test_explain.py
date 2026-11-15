"""Unit tests for the templated explanations."""
from pathlib import Path

from orchestrator.explain import diff_plans, explain_plan, why_text
from orchestrator.fleet_agent import FleetAgent
from orchestrator.models import BROKEN, DONE, IN_PROGRESS, Event, Plan, Stop, load_scenario

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "farm.json"


def state():
    return load_scenario(SCENARIO)[0]


def test_diff_plans_moved_new_dropped_unchanged():
    s = state()
    s.jobs["J9"].status = DONE
    old = {"T1": [Stop("job", "J6"), Stop("job", "J9")], "T2": [Stop("job", "J2"), Stop("job", "J4")],
           "T3": [Stop("job", "J5")]}
    new = {"T1": [Stop("refuel", "D1"), Stop("job", "J6"), Stop("job", "J4")], "T2": [],
           "T3": [Stop("job", "J5"), Stop("job", "J7")]}
    d = diff_plans(old, new, s)
    assert d["moved"] == {"J4": ("T2", "T1")}
    assert d["newly_assigned"] == {"J7": "T3"}
    assert d["dropped"] == {"J2": "T2"}
    assert "J9" not in d["dropped"]        # done jobs are ignored
    assert d["unchanged"] == []


def test_breakdown_explanation_from_history():
    s = state()
    agent = FleetAgent()
    first = agent.plan(s, None)
    tid = next(t for t, q in first.queues.items() if t in ("T1", "T2") and q)
    jid = next(st.target for st in first.queues[tid] if st.kind == "job")
    # T2/T1 was 60% through its first job when it broke (what the sim would leave behind).
    s.t = 30
    job = s.jobs[jid]
    job.remaining, job.completed_by = 0.4, [tid]
    s.tractors[tid].status = BROKEN
    s.tractors[tid].location = s.job_node(jid)     # it was working at the field
    plan = agent.plan(s, Event(30, "breakdown", {"tractor": tid}))
    other = "T1" if tid == "T2" else "T2"
    assert plan.explanation.startswith(f"{tid} broke down at t=30 while spraying")
    assert "(60% done)" in plan.explanation
    assert f"Its remaining work went to {other}" in plan.explanation


def test_explain_without_history_lists_queues():
    s = state()
    plan = FleetAgent().plan(s, None)
    assert plan.explanation.startswith("Initial plan at t=0.")
    assert "T3:" in plan.explanation


def test_why_text_chosen_and_rejected():
    s = state()
    plan = FleetAgent().plan(s, None)
    txt = why_text("J6", plan.why["J6"], s)
    chosen = plan.why["J6"]["chosen"]
    assert f"was given to {chosen}" in txt
    assert "ETA t=" in txt and "Cost" in txt
    assert "T3 rejected: T3 carries a mower" in txt


def test_why_text_unassignable():
    s = state()
    s.tractors["T3"].status = BROKEN
    plan = FleetAgent().plan(s, Event(0, "breakdown", {"tractor": "T3"}))
    txt = why_text("J5", plan.why["J5"], s)
    assert "could not be assigned" in txt
    assert "T3: T3 is broken down" in txt
    assert "retried" in txt


def test_why_text_missing_record_and_in_progress():
    s = state()
    s.jobs["J7"].status, s.jobs["J7"].owner, s.jobs["J7"].remaining = IN_PROGRESS, "T3", 0.5
    txt = why_text("J7", None, s)
    assert "not planned yet" in txt and "In progress on T3, 50% done" in txt
    assert why_text("NOPE", None, s) == "Unknown job NOPE."


def test_breakdown_on_the_road_back_to_a_half_done_job_is_not_called_working():
    s = state()
    agent = FleetAgent()
    first = agent.plan(s, None)
    tid = next(t for t, q in first.queues.items() if t in ("T1", "T2") and q)
    jid = next(st.target for st in first.queues[tid] if st.kind == "job")
    s.jobs[jid].remaining, s.jobs[jid].completed_by = 0.7, [tid]   # worked it earlier
    s.tractors[tid].status = BROKEN                                # still at the depot, not the field
    plan = agent.plan(s, Event(5, "breakdown", {"tractor": tid}))
    assert f"on its way to" in plan.explanation.split(".")[0]
    assert "% done" not in plan.explanation.split(".")[0]


def test_dropped_job_without_record_is_mentioned():
    s = state()
    old = {"T1": [Stop("job", "J6")], "T2": [Stop("job", "J2")], "T3": [Stop("job", "J5")]}
    new = {"T1": [Stop("job", "J6")], "T2": [], "T3": [Stop("job", "J5")]}
    txt = explain_plan(old, Plan(queues=new), s, None)
    assert "J2 (spraying North Pasture) was taken off T2's queue." in txt
    assert "No change to assignments" not in txt
