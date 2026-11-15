"""API hardening: bad input is rejected with 4xx, and a sim error never kills the loop."""
import pytest

pytest.importorskip("httpx")  # needed by FastAPI's TestClient
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402

client = TestClient(server.app)  # not used as a context manager: no background thread


@pytest.fixture(autouse=True)
def fresh():
    server.runner.reset("coordinator")
    yield


def start():
    """Run the first step (initial plan) so events apply immediately."""
    with server.runner.lock:
        server.runner.sim.step()


# --- #1: events with missing / bad params --------------------------------------------

@pytest.mark.parametrize("kind,params", [
    ("breakdown", {}), ("repair", {}), ("route_blocked", {}), ("route_unblocked", {}),
    ("priority_change", {"job": "J1"}), ("priority_change", {"priority": 3}), ("new_job", {}),
])
def test_event_missing_params_is_400_and_not_queued(kind, params):
    before = len(server.runner.sim.pending_events)
    r = client.post("/api/event", json={"kind": kind, "params": params})
    assert r.status_code == 400
    assert len(server.runner.sim.pending_events) == before


@pytest.mark.parametrize("prio", ["high", None, 7, 0, 2.5, True])
def test_priority_change_bad_value_is_400(prio):
    r = client.post("/api/event", json={"kind": "priority_change", "params": {"job": "J1", "priority": prio}})
    assert r.status_code == 400


def test_priority_change_numeric_string_is_accepted():
    start()
    r = client.post("/api/event", json={"kind": "priority_change", "params": {"job": "J1", "priority": "3"}})
    assert r.status_code == 200
    assert server.runner.sim.state.jobs["J1"].priority == 3


def test_new_job_event_is_validated_like_jobs():
    r = client.post("/api/event", json={"kind": "new_job", "params": {"job": {"id": "J50", "type": "mow"}}})
    assert r.status_code == 400
    r = client.post("/api/event", json={"kind": "new_job", "params": {
        "job": {"type": "mow", "field": "F2", "priority": 2, "deadline": 400, "extra": 1}}})
    assert r.status_code == 200


def test_bad_event_before_start_cannot_crash_start():
    client.post("/api/event", json={"kind": "breakdown", "params": {}})
    server.runner.paused = False
    server.runner.tick()
    assert server.runner.error is None
    assert server.runner.sim.t == 1


# --- #2: an exception in a step pauses the sim instead of killing the thread ----------

def test_sim_error_pauses_and_is_reported(monkeypatch):
    server.runner.paused = False
    monkeypatch.setattr(server.runner.sim, "step", lambda: (_ for _ in ()).throw(KeyError("boom")))
    delay = server.runner.tick()                 # must not raise
    assert delay > 0
    assert server.runner.paused is True
    assert "boom" in server.runner.error
    state = client.get("/api/state").json()
    assert "boom" in state["error"]
    assert any(r["kind"] == "sim_error" for r in state["log"])
    client.post("/api/reset", json={"dispatcher": "coordinator"})
    assert client.get("/api/state").json()["error"] is None


# --- #3 / #5: job values and types -----------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"priority": "high"}, {"priority": 9}, {"deadline": "soon"}, {"deadline": -5},
    {"deadline": 2.5}, {"id": 5}, {"type": "harvest"}, {"field": "F99"},
])
def test_bad_job_values_are_rejected(bad):
    job = {"type": "mow", "field": "F2", "priority": 2, "deadline": 400} | bad
    code = 404 if "field" in bad else 400
    before = len(server.runner.sim.pending_events)
    r = client.post("/api/jobs", json={"jobs": [job]})
    assert r.status_code == code, r.text
    assert len(server.runner.sim.pending_events) == before


def test_numeric_strings_are_converted():
    start()
    r = client.post("/api/jobs", json={"jobs": [{"id": "J20", "type": "mow", "field": "F2",
                                                 "priority": "3", "deadline": "400"}]})
    assert r.status_code == 200
    j = server.runner.sim.state.jobs["J20"]
    assert (j.priority, j.deadline) == (3, 400)


def test_one_bad_job_rejects_the_whole_list():
    good = {"id": "J30", "type": "mow", "field": "F2", "priority": 2, "deadline": 400}
    before = len(server.runner.sim.pending_events)      # the scenario's scripted events
    r = client.post("/api/jobs", json={"jobs": [good, good | {"id": "J31", "type": "harvest"}]})
    assert r.status_code == 400
    assert len(server.runner.sim.pending_events) == before


# --- #4: ids picked by the server, also before Start -------------------------------------

def test_two_jobs_before_start_get_distinct_ids():
    job = {"type": "spray", "field": "F3", "priority": 2, "deadline": 300}
    assert client.post("/api/jobs", json={"jobs": [job]}).status_code == 200
    assert client.post("/api/jobs", json={"jobs": [job, job]}).status_code == 200
    ids = [e.params["job"]["id"] for e in server.runner.sim.pending_events if e.kind == "new_job"]
    assert sorted(ids) == ["J11", "J12", "J13", "J14"]   # J11 is the scenario's scripted job at t=240


def test_explicit_duplicate_id_is_still_409():
    job = {"id": "J2", "type": "spray", "field": "F3", "priority": 2, "deadline": 300}
    assert client.post("/api/jobs", json={"jobs": [job]}).status_code == 409


# --- #6: NaN / inf speed ---------------------------------------------------------------------

@pytest.mark.parametrize("raw", ['{"ticks_per_sec": NaN}', '{"ticks_per_sec": Infinity}'])
def test_non_finite_speed_is_400(raw):
    before = server.runner.ticks_per_sec
    r = client.post("/api/speed", content=raw, headers={"content-type": "application/json"})
    assert r.status_code in (400, 422)
    assert server.runner.ticks_per_sec == before


def test_normal_speed_still_works():
    r = client.post("/api/speed", json={"ticks_per_sec": 8})
    assert r.status_code == 200 and server.runner.ticks_per_sec == 8


# --- #9: why_text errors fall back to the template ----------------------------------------------

def test_why_falls_back_when_explain_raises(monkeypatch):
    start()
    from orchestrator import explain
    monkeypatch.setattr(explain, "why_text", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    r = client.get("/api/why/J1")
    assert r.status_code == 200
    assert r.json()["text"].startswith("J1")


# --- second review --------------------------------------------------------------------

@pytest.mark.parametrize("kind,params", [
    ("breakdown", {"tractor": ["T1"]}), ("route_blocked", {"edge": {"id": "bridge"}}),
    ("priority_change", {"job": {}, "priority": 3}), ("priority_change", {"job": ["J1"], "priority": 3}),
])
def test_unhashable_event_params_are_400_not_500(kind, params):
    assert client.post("/api/event", json={"kind": kind, "params": params}).status_code == 400


@pytest.mark.parametrize("bad", [{"field": ["F2"]}, {"field": {}}, {"type": ["mow"]}])
def test_unhashable_job_values_are_400_not_500(bad):
    job = {"type": "mow", "field": "F2", "priority": 2, "deadline": 400} | bad
    assert client.post("/api/jobs", json={"jobs": [job]}).status_code == 400


def _break_sim(monkeypatch):
    server.runner.paused = False
    monkeypatch.setattr(server.runner.sim, "step", lambda: (_ for _ in ()).throw(KeyError("boom")))
    server.runner.tick()
    monkeypatch.undo()


def test_cannot_resume_after_error_until_reset(monkeypatch):
    _break_sim(monkeypatch)
    t = server.runner.sim.t
    r = client.post("/api/speed", json={"paused": False})
    assert r.status_code == 409 and "Reset" in r.json()["detail"]
    assert server.runner.paused is True
    server.runner.tick()                                   # loop does not step a failed run
    assert server.runner.sim.t == t
    assert client.post("/api/speed", json={"ticks_per_sec": 5}).status_code == 200   # speed still OK
    client.post("/api/reset", json={"dispatcher": "coordinator"})
    assert client.post("/api/speed", json={"paused": False}).status_code == 200


def test_error_applying_an_event_pauses_and_reports(monkeypatch):
    start()
    server.runner.paused = False
    monkeypatch.setattr(server.runner.sim, "apply_event", lambda ev: (_ for _ in ()).throw(RuntimeError("bad apply")))
    r = client.post("/api/event", json={"kind": "breakdown", "params": {"tractor": "T1"}})
    assert r.status_code == 500 and "press Reset" in r.json()["detail"]
    assert server.runner.paused is True
    assert "bad apply" in client.get("/api/state").json()["error"]


def test_range_messages_name_only_the_real_bound():
    r = client.post("/api/jobs", json={"jobs": [{"type": "mow", "field": "F2", "priority": 2, "deadline": -5}]})
    assert "at least 0" in r.json()["detail"] and "None" not in r.json()["detail"]
    r = client.post("/api/event", json={"kind": "priority_change", "params": {"job": "J1", "priority": 7}})
    assert "between 1 and 3" in r.json()["detail"]


def test_deadline_in_the_past_is_rejected():
    start()
    with server.runner.lock:
        for _ in range(20):
            server.runner.sim.step()
    t = server.runner.sim.t
    past = {"type": "mow", "field": "F2", "priority": 2, "deadline": t - 1}
    r = client.post("/api/jobs", json={"jobs": [past]})
    assert r.status_code == 400 and f"at least {t}" in r.json()["detail"]
    assert client.post("/api/jobs", json={"jobs": [past | {"deadline": t}]}).status_code == 200


def test_why_fallback_does_not_write_to_the_log(monkeypatch):
    start()
    from orchestrator import explain
    monkeypatch.setattr(explain, "why_text", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    n = len(server.runner.sim.log.records)
    for _ in range(3):
        assert client.get("/api/why/J1").status_code == 200
    assert len(server.runner.sim.log.records) == n
