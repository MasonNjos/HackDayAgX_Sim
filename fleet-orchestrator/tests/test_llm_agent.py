"""Claude coordinator tests with a fake client: no network, no API key."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.fleet_agent import FleetAgent
from orchestrator.llm_agent import LLMFleetAgent
from orchestrator.models import Event, Stop, load_scenario
from orchestrator.safety_agent import validate

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "farm.json"


@pytest.fixture
def state():
    return load_scenario(SCENARIO)[0]


# --- fake Anthropic client ---------------------------------------------------

def tool_use(name, inp, i=0):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=f"tu_{name}_{i}")


def reply(*blocks, stop="tool_use"):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


def text(t):
    return SimpleNamespace(type="text", text=t)


class FakeClient:
    """Returns scripted responses; a callable entry gets the request kwargs."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw["messages"], default=lambda o: o.__dict__)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(kw) if callable(item) else item


def submit(queues: dict, explanation="Kept the greedy plan.", reasons=None):
    return tool_use("submit_plan", {
        "queues": [{"tractor": tid, "stops": [{"kind": s.kind, "target": s.target} for s in q]}
                   for tid, q in queues.items()],
        "explanation": explanation,
        "reasons": reasons or [],
    })


def greedy_queues(state):
    return FleetAgent().plan(state, None).queues


# --- tests -----------------------------------------------------------------------

def test_valid_submission_is_accepted(state):
    queues = greedy_queues(state)
    client = FakeClient(reply(submit(queues, "All jobs assigned.", [{"job": "J6", "reason": "closest sprayer"}])))
    plan = LLMFleetAgent(client).plan(state, None)
    assert validate(plan, state) == []
    assert plan.queues == queues
    assert plan.explanation.startswith("[Claude] All jobs assigned. Route plan: ")
    assert plan.why["J6"]["decided_by"] == "claude"
    assert plan.why["J6"]["claude_reason"] == "closest sprayer"
    assert not any("Claude unavailable" in w for w in plan.warnings)


def test_start_of_day_brief_has_all_jobs_and_only_the_greedy_score(state):
    client = FakeClient(reply(submit(greedy_queues(state))))
    LLMFleetAgent(client).plan(state, None)
    brief = json.loads(client.calls[0][0]["content"])
    assert brief["mode"] == "full_day"
    assert brief["trigger"] == "start of day"
    assert {t["id"] for t in brief["tractors"]} == {"T1", "T2", "T3"}
    assert {j["id"] for j in brief["open_jobs"]} == set(state.jobs)
    assert "greedy_plan" not in brief                # Claude builds its own routes
    assert brief["greedy_score"]["total_km"] > 0


def test_replan_brief_contains_greedy_plan(state):
    client = FakeClient(reply(submit(greedy_queues(state))))
    LLMFleetAgent(client).plan(state, Event(0, "breakdown", {"tractor": "T2"}))
    brief = json.loads(client.calls[0][0]["content"])
    assert brief["mode"] == "replan"
    assert brief["greedy_plan"]["queues"]["T1"]


def test_system_prompt_depends_on_mode(state):
    seen = []

    def capture(kw):
        seen.append(kw["system"])
        return reply(submit(greedy_queues(state)))

    LLMFleetAgent(FakeClient(capture)).plan(state, None)
    state.t = 200
    LLMFleetAgent(FakeClient(capture)).plan(state, None)
    assert "START OF THE DAY" in seen[0] and "REPLAN" not in seen[0]
    assert "REPLAN" in seen[1] and "START OF THE DAY" not in seen[1]


def test_evaluate_plan_scores_a_whole_plan(state):
    queues = greedy_queues(state)
    client = FakeClient(
        reply(tool_use("evaluate_plan", {"queues": [
            {"tractor": tid, "stops": [{"kind": s.kind, "target": s.target} for s in q]}
            for tid, q in queues.items()]})),
        reply(submit(queues)),
    )
    LLMFleetAgent(client).plan(state, None)
    res = client.calls[1][-1]["content"][0]
    data = json.loads(res["content"])
    assert not res["is_error"]
    assert data["ok"] and data["violations"] == []
    assert data["total_km"] > 0 and data["unassigned_jobs"] == []
    assert set(data["timelines"]) == {"T1", "T2", "T3"}


def _reorder_t3(state, order):
    queues = greedy_queues(state)
    q = {tid: list(stops) for tid, stops in queues.items()}
    q["T3"] = [Stop("job", j) for j in order] + [s for s in queues["T3"] if s.kind != "job"]
    return queues, q


@pytest.mark.parametrize("order, why", [
    (["J5", "J3", "J10", "J7"], "less efficient than the greedy plan"),  # 75 km vs 69, finishes later, no deadline gain
    (["J3", "J5", "J7", "J10"], "more deadline risk"),               # J5 and J10 late
])
def test_full_day_plan_worse_than_greedy_is_sent_back(state, order, why):
    queues, worse = _reorder_t3(state, order)
    client = FakeClient(reply(submit(worse)), reply(submit(queues)))
    plan = LLMFleetAgent(client).plan(state, None)
    assert plan.queues == queues
    result = client.calls[1][-1]["content"][0]
    assert result["is_error"] and why in result["content"]


def test_full_day_plan_shorter_than_greedy_is_accepted(state):
    _, better = _reorder_t3(state, ["J10", "J5", "J7", "J3"])        # 63.2 km vs 69, all on time
    plan = LLMFleetAgent(FakeClient(reply(submit(better, "Mower does the west side first.")))).plan(state, None)
    assert plan.queues == better
    assert "Route plan: 63.2 km, 477 idle min (greedy 69.0 km, 535 idle min; FCFS baseline" in plan.explanation
    assert plan.explanation.endswith("0 deadline(s) at risk.")


def test_start_of_day_brief_has_fcfs_baseline_target(state):
    client = FakeClient(reply(submit(greedy_queues(state))))
    LLMFleetAgent(client).plan(state, None)
    brief = json.loads(client.calls[0][0]["content"])
    base = brief["fcfs_baseline_target"]
    assert base["jobs_completed"] > 0 and base["travel_km"] > 0 and "idle_minutes" in base
    assert brief["greedy_score"]["idle_minutes"] >= 0
    assert state.t == 0 and all(j.status == "pending" for j in state.jobs.values())  # what-if ran on a copy


def test_idle_is_time_waiting_for_the_last_tractor(state):
    from orchestrator.llm_agent import LLMFleetAgent as L
    score = L._score(greedy_queues(state), state)
    ends = score["route_end_minute"]
    assert score["idle_minutes"] == sum(score["last_finish_minute"] - e for e in ends.values())


def test_rejected_plan_goes_back_to_claude_then_fixed(state):
    queues = greedy_queues(state)
    bad = {tid: list(q) for tid, q in queues.items()}
    bad["T3"] = bad["T3"] + [bad["T1"][0]]          # J6 (spray) also on the mower: two owners + wrong implement
    client = FakeClient(reply(submit(bad)), reply(submit(queues)))
    plan = LLMFleetAgent(client).plan(state, None)
    assert plan.queues == queues
    result = client.calls[1][-1]["content"][0]
    assert result["is_error"] is True and "Plan rejected" in result["content"]


def test_dropping_a_feasible_job_is_rejected(state):
    queues = greedy_queues(state)
    short = {tid: list(q) for tid, q in queues.items()}
    short["T3"] = []
    client = FakeClient(reply(submit(short)), reply(text("done"), stop="end_turn"),
                        reply(text("done"), stop="end_turn"))
    plan = LLMFleetAgent(client).plan(state, None)
    assert plan.queues == queues                     # fell back to greedy
    assert any("missing" in c[-1]["content"][0]["content"] for c in client.calls[1:2])
    assert any("Claude unavailable" in w for w in plan.warnings)


def test_evaluate_queue_reports_etas_and_failures(state):
    queues = greedy_queues(state)
    client = FakeClient(
        reply(tool_use("evaluate_queue", {"tractor": "T1", "stops": [{"kind": "job", "target": "J6"}]}, 0),
              tool_use("evaluate_queue", {"tractor": "T3", "stops": [{"kind": "job", "target": "J6"}]}, 1)),
        reply(submit(queues)),
    )
    LLMFleetAgent(client).plan(state, None)
    ok, bad = client.calls[1][-1]["content"]
    data = json.loads(ok["content"])
    assert data["ok"] and data["steps"][0]["stop"] == "job:J6" and data["steps"][0]["late_by"] == 0
    assert bad["is_error"] and "mower" in bad["content"]


@pytest.mark.parametrize("script", [
    [RuntimeError("network down")],
    [reply(stop="refusal")],
    [reply(text("thinking..."), stop="end_turn"), reply(text("still thinking"), stop="end_turn")],
    [reply(text("x"), stop="max_tokens")],
])
def test_failures_fall_back_to_greedy(state, script):
    plan = LLMFleetAgent(FakeClient(*script)).plan(state, None)
    assert plan.queues == greedy_queues(state)
    assert plan.warnings[0].startswith("Claude unavailable")
    assert plan.explanation.startswith("[greedy fallback]")
    assert validate(plan, state) == []


def test_no_client_disables_claude_for_the_run(state):
    agent = LLMFleetAgent()
    agent._disabled = "no Claude client: missing key"
    plan = agent.plan(state, Event(0, "breakdown", {"tractor": "T2"}))
    assert "missing key" in plan.warnings[0]


def test_full_sim_run_with_claude_has_no_violations():
    sim_mod = pytest.importorskip("orchestrator.sim")

    def echo_greedy(kw):  # Claude that always submits the plan greedy would make right now
        brief = json.loads(kw["messages"][0]["content"])
        queues = FleetAgent().plan(live["state"], None).queues
        return reply(tool_use("submit_plan", {
            "queues": [{"tractor": tid, "stops": [{"kind": s.kind, "target": s.target} for s in q]}
                       for tid, q in queues.items()],
            "explanation": f"Plan for {brief['trigger']}.", "reasons": []}))

    live = {}

    def run(dispatcher):
        state, events, meta = load_scenario(SCENARIO)
        live["state"] = state
        sim = sim_mod.Sim(state, events, dispatcher, horizon=meta.get("horizon", 480))
        while not sim.finished():
            sim.step()
        return sim

    claude = run(LLMFleetAgent(FakeClient(*([echo_greedy] * 500))))
    greedy = run(FleetAgent())
    assert claude.metrics.summary(claude.state)["violations"] == 0
    assert claude.last_explanation.startswith("[Claude]")
    done = lambda s: {j.id for j in s.state.jobs.values() if j.status == "done"}
    assert done(claude) == done(greedy)


def test_mid_day_replan_without_event_is_not_called_start_of_day(state):
    state.t = 200
    client = FakeClient(reply(submit(greedy_queues(state))))
    LLMFleetAgent(client).plan(state, None)
    brief = json.loads(client.calls[0][0]["content"])
    assert "routine replan" in brief["trigger"] and "stalled" in brief["trigger"]
