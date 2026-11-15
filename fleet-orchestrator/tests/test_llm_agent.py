"""Claude coordinator tests with a fake client: no network, no API key."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.fleet_agent import FleetAgent
from orchestrator.llm_agent import LLMFleetAgent
from orchestrator.models import Event, load_scenario
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
    assert plan.explanation == "[Claude] All jobs assigned."
    assert plan.why["J6"]["decided_by"] == "claude"
    assert plan.why["J6"]["claude_reason"] == "closest sprayer"
    assert not any("Claude unavailable" in w for w in plan.warnings)


def test_brief_contains_state_and_greedy_plan(state):
    client = FakeClient(reply(submit(greedy_queues(state))))
    LLMFleetAgent(client).plan(state, None)
    brief = json.loads(client.calls[0][0]["content"])
    assert brief["trigger"] == "start of day"
    assert {t["id"] for t in brief["tractors"]} == {"T1", "T2", "T3"}
    assert {j["id"] for j in brief["open_jobs"]} == set(state.jobs)
    assert brief["greedy_plan"]["queues"]["T1"]


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

    def echo_greedy(kw):  # Claude that always submits the greedy plan from the brief
        brief = json.loads(kw["messages"][0]["content"])
        return reply(tool_use("submit_plan", {
            "queues": [{"tractor": tid, "stops": [{"kind": s.split(":")[0], "target": s.split(":")[1]} for s in q]}
                       for tid, q in brief["greedy_plan"]["queues"].items()],
            "explanation": f"Plan for {brief['trigger']}.", "reasons": []}))

    def run(dispatcher):
        state, events, meta = load_scenario(SCENARIO)
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
    assert "routine replan" in brief["trigger"]
