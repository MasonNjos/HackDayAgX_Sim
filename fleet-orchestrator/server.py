"""Command-center API. Owner: Person A.

    uvicorn server:app --port 8000          (docker compose up does this)

One live simulation runs in a background thread. Every endpoint is a plain `def`, so
FastAPI runs it in its threadpool and the blocking lock never stalls the event loop.

Routes (all under /api; the Vite dev server proxies /api here):
  GET  /api/state                 full snapshot for the dashboard
  POST /api/event                 {"kind": "breakdown", "params": {"tractor": "T2"}}
  POST /api/jobs                  {"jobs": [{...Job fields...}, ...]}  operator submits jobs
  GET  /api/why/{job_id}          scoring record + text for "why this tractor?"
  POST /api/speed                 {"ticks_per_sec": 4, "paused": false}
  POST /api/reset                 {"dispatcher": "coordinator" | "baseline"}
If dashboard/dist exists (built React app), it is served at /.
"""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from orchestrator.baseline import FCFSDispatcher
from orchestrator.eventlog import EventLog
from orchestrator.fleet_agent import FleetAgent
from orchestrator.models import EVENT_KINDS, Event, load_scenario
from orchestrator.sim import Sim

ROOT = Path(__file__).parent
SCENARIO = ROOT / "scenarios" / "farm.json"
DIST = ROOT / "dashboard" / "dist"
DISPATCHERS = {"coordinator": FleetAgent, "baseline": FCFSDispatcher}


class Runner:
    """Owns the live Sim and the thread that ticks it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ticks_per_sec = 2.0
        self.paused = True  # start paused so the presenter controls the start
        self.sim: Sim | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.reset("coordinator")

    def reset(self, dispatcher: str) -> None:
        state, events, meta = load_scenario(SCENARIO)
        disp = DISPATCHERS[dispatcher]()
        sim = Sim(state, events, disp, EventLog(dispatcher=disp.name), horizon=meta.get("horizon", 480))
        with self.lock:
            self.sim = sim
            self.paused = True

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sim")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                if not self.paused and not self.sim.finished():
                    self.sim.step()
                delay = 1.0 / max(self.ticks_per_sec, 0.1)
            self._stop.wait(delay)


runner = Runner()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runner.start()
    yield
    runner.stop()


app = FastAPI(title="Fleet Orchestrator", lifespan=lifespan)


# --- request bodies ----------------------------------------------------------

class EventIn(BaseModel):
    kind: str
    params: dict = Field(default_factory=dict)


class JobsIn(BaseModel):
    jobs: list[dict]


class SpeedIn(BaseModel):
    ticks_per_sec: float | None = None
    paused: bool | None = None


class ResetIn(BaseModel):
    dispatcher: str = "coordinator"


# --- routes ------------------------------------------------------------------

def _state() -> dict:
    snap = runner.sim.snapshot()
    snap["speed"] = {"ticks_per_sec": runner.ticks_per_sec, "paused": runner.paused}
    snap["dispatchers"] = list(DISPATCHERS)
    return snap


@app.get("/api/state")
def get_state() -> dict:
    with runner.lock:
        return _state()


@app.post("/api/event")
def post_event(ev: EventIn) -> dict:
    if ev.kind not in EVENT_KINDS:
        raise HTTPException(400, f"unknown event kind {ev.kind!r}; expected one of {sorted(EVENT_KINDS)}")
    with runner.lock:
        s = runner.sim.state
        for key, pool in (("tractor", s.tractors), ("edge", s.edges)):
            if key in ev.params and ev.params[key] not in pool:
                raise HTTPException(404, f"unknown {key} {ev.params[key]!r}")
        if ev.kind == "priority_change" and ev.params.get("job") not in s.jobs:
            raise HTTPException(404, f"unknown job {ev.params.get('job')!r}")
        runner.sim.inject(Event(t=s.t, kind=ev.kind, params=ev.params))
        # Apply now (even while paused) so the operator sees the replan immediately.
        _apply_pending()
        return _state()


@app.post("/api/jobs")
def post_jobs(body: JobsIn) -> dict:
    with runner.lock:
        s = runner.sim.state
        for d in body.jobs:
            missing = {"id", "type", "field", "priority", "deadline"} - d.keys()
            if missing:
                raise HTTPException(400, f"job {d.get('id', '?')} missing {sorted(missing)}")
            if d["field"] not in s.fields:
                raise HTTPException(404, f"unknown field {d['field']!r}")
            if d["id"] in s.jobs:
                raise HTTPException(409, f"job {d['id']!r} already exists")
        for d in body.jobs:
            job = {k: d[k] for k in ("id", "type", "field", "priority", "deadline")}
            job["submitted_at"] = s.t
            runner.sim.inject(Event(t=s.t, kind="new_job", params={"job": job}))
        _apply_pending()
        return _state()


def _apply_pending() -> None:
    """Apply queued events at the current tick without advancing time (caller holds the lock)."""
    sim = runner.sim
    if not sim.started:
        return  # step() applies them together with the initial plan
    while sim.pending_events and sim.pending_events[0].t <= sim.t:
        ev = sim.pending_events.pop(0)
        sim.apply_event(ev)
        sim.replan(ev)


@app.get("/api/why/{job_id}")
def get_why(job_id: str) -> dict:
    with runner.lock:
        s = runner.sim.state
        if job_id not in s.jobs:
            raise HTTPException(404, f"unknown job {job_id!r}")
        rec = runner.sim.why.get(job_id)
        return {"job": job_id, "record": rec, "text": why_text(job_id, rec, runner.sim)}


def why_text(job_id: str, rec: dict | None, sim: Sim) -> str:
    """Prefer B's explain.why_text if it exists; otherwise a simple template."""
    try:
        from orchestrator import explain  # type: ignore
        if hasattr(explain, "why_text"):
            return explain.why_text(job_id, rec, sim.state)
    except ImportError:
        pass
    job = sim.state.jobs[job_id]
    if rec is None:
        return f"{job_id} has not been planned yet ({sim.dispatcher.name})."
    if not rec.get("chosen"):
        rej = "; ".join(f"{t}: {r}" for t, r in rec.get("rejected", {}).items())
        return f"{job_id} could not be assigned. {rej or rec.get('reason', '')}".strip()
    parts = [f"{job_id} went to {rec['chosen']}"]
    if "eta" in rec:
        parts.append(f"finishing around t={rec['eta']} (deadline t={job.deadline})")
    text = ", ".join(parts) + "."
    alts = rec.get("alternatives") or {}
    if alts:
        text += " Other options cost more: " + ", ".join(f"{t} ({c})" for t, c in alts.items()) + "."
    rej = rec.get("rejected") or {}
    if rej:
        text += " Not eligible: " + "; ".join(rej.values()) + "."
    return text


@app.post("/api/speed")
def post_speed(body: SpeedIn) -> dict:
    with runner.lock:
        if body.ticks_per_sec is not None:
            runner.ticks_per_sec = min(max(body.ticks_per_sec, 0.1), 60.0)
        if body.paused is not None:
            runner.paused = body.paused
        return _state()


@app.post("/api/reset")
def post_reset(body: ResetIn) -> dict:
    if body.dispatcher not in DISPATCHERS:
        raise HTTPException(400, f"dispatcher must be one of {list(DISPATCHERS)}")
    runner.reset(body.dispatcher)
    with runner.lock:
        return _state()


if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="dashboard")
