"""Command-center API. Owner: Person A.

    uvicorn server:app --port 8000          (docker compose up does this)

One live simulation runs in a background thread. Every endpoint is a plain `def`, so
FastAPI runs it in its threadpool and the blocking lock never stalls the event loop.

Routes (all under /api; the Vite dev server proxies /api here):
  GET  /api/state                 full snapshot for the dashboard
  POST /api/event                 {"kind": "breakdown", "params": {"tractor": "T2"}}
  POST /api/jobs                  {"jobs": [{...Job fields...}, ...]}  operator submits jobs
                                  ("id" is optional: the server picks the next free J<n>)
  GET  /api/why/{job_id}          scoring record + text for "why this tractor?"
  POST /api/speed                 {"ticks_per_sec": 4, "paused": false}
  POST /api/reset                 {"dispatcher": "coordinator" | "baseline"}
If dashboard/dist exists (built React app), it is served at /.
"""
from __future__ import annotations

import math
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from orchestrator.baseline import FCFSDispatcher
from orchestrator.eventlog import EventLog
from orchestrator.fleet_agent import FleetAgent
from orchestrator.models import EVENT_KINDS, IMPLEMENT_JOBS, Event, load_scenario
from orchestrator.sim import Sim

ROOT = Path(__file__).parent
SCENARIO = ROOT / "scenarios" / "farm.json"
DIST = ROOT / "dashboard" / "dist"
DISPATCHERS = {"coordinator": FleetAgent, "baseline": FCFSDispatcher}
JOB_TYPES = sorted({t for types in IMPLEMENT_JOBS.values() for t in types})


class Runner:
    """Owns the live Sim and the thread that ticks it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ticks_per_sec = 2.0
        self.paused = True  # start paused so the presenter controls the start
        self.sim: Sim | None = None
        self.error: str | None = None  # last exception from a sim step (sim is paused when set)
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
            self.error = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="sim")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.tick())

    def tick(self) -> float:
        """Advance one step if running; returns the delay until the next one.

        An exception in the sim must not kill this thread (the dashboard would freeze on
        its last frame and Reset could not recover it): log it, pause, and keep serving.
        """
        with self.lock:
            if not self.paused and not self.sim.finished():
                try:
                    self.sim.step()
                except Exception as exc:
                    self.paused = True
                    self.error = f"t={self.sim.t}: {exc!r}"
                    self.sim.log.write(self.sim.t, "sim_error", error=repr(exc),
                                       trace=traceback.format_exc(limit=5))
            return 1.0 / max(self.ticks_per_sec, 0.1)


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
    snap["error"] = runner.error
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
        params = dict(ev.params)
        missing = [k for k in REQUIRED_PARAMS[ev.kind] if k not in params]
        if missing:
            raise HTTPException(400, f"{ev.kind} needs params {missing}")
        for key, pool in (("tractor", s.tractors), ("edge", s.edges)):
            if key in params and params[key] not in pool:
                raise HTTPException(404, f"unknown {key} {params[key]!r}")
        if ev.kind == "priority_change":
            if params["job"] not in s.jobs:
                raise HTTPException(404, f"unknown job {params['job']!r}")
            params["priority"] = _as_int(params["priority"], "priority", 1, 3)
        if ev.kind == "new_job":
            if not isinstance(params["job"], dict):
                raise HTTPException(400, "new_job needs params.job to be an object")
            params["job"] = _clean_job(params["job"], _taken_ids(), s.t)
        runner.sim.inject(Event(t=s.t, kind=ev.kind, params=params))
        # Apply now (even while paused) so the operator sees the replan immediately.
        _apply_pending()
        return _state()


@app.post("/api/jobs")
def post_jobs(body: JobsIn) -> dict:
    with runner.lock:
        s = runner.sim.state
        # Validate the whole list before injecting any of it. `taken` grows as we go,
        # so duplicates within this request are caught too.
        taken = _taken_ids()
        jobs = []
        for d in body.jobs:
            if not isinstance(d, dict):
                raise HTTPException(400, f"each job must be an object, got {d!r}")
            jobs.append(_clean_job(d, taken, s.t))
        for job in jobs:
            runner.sim.inject(Event(t=s.t, kind="new_job", params={"job": job}))
        _apply_pending()
        return _state()


REQUIRED_PARAMS = {
    "breakdown": ["tractor"], "repair": ["tractor"],
    "route_blocked": ["edge"], "route_unblocked": ["edge"],
    "priority_change": ["job", "priority"], "new_job": ["job"],
}
assert set(REQUIRED_PARAMS) == EVENT_KINDS


def _as_int(v, name: str, lo: int | None = None, hi: int | None = None) -> int:
    """Accept ints and integer-valued strings/floats ("3", 3.0); reject anything else."""
    if isinstance(v, bool):
        raise HTTPException(400, f"{name} must be an integer, got {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be an integer, got {v!r}")
    if not math.isfinite(f) or f != int(f):
        raise HTTPException(400, f"{name} must be an integer, got {v!r}")
    n = int(f)
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise HTTPException(400, f"{name} must be between {lo} and {hi}, got {n}")
    return n


def _taken_ids() -> set[str]:
    """Job ids in use: existing jobs plus new_job events queued but not applied yet
    (e.g. submitted before Start). Caller holds the lock."""
    sim = runner.sim
    return set(sim.state.jobs) | {e.params["job"]["id"] for e in sim.pending_events if e.kind == "new_job"}


def _clean_job(d: dict, taken: set[str], t: int) -> dict:
    """Validate one submitted job and return the dict the sim gets. Adds its id to `taken`.

    "id" is optional: without one the server picks the next free J<n>, so two jobs
    added before Start (not yet visible in /api/state) never collide.
    """
    s = runner.sim.state
    missing = sorted({"type", "field", "priority", "deadline"} - d.keys())
    if missing:
        raise HTTPException(400, f"job {d.get('id', '?')} missing {missing}")
    jid = d.get("id")
    if jid in (None, ""):
        n = 1
        while f"J{n}" in taken:
            n += 1
        jid = f"J{n}"
    elif not isinstance(jid, str):
        raise HTTPException(400, f"job id must be a string, got {jid!r}")
    if jid in taken:
        raise HTTPException(409, f"job {jid!r} already exists or is repeated in this list")
    if d["type"] not in JOB_TYPES:
        raise HTTPException(400, f"job {jid}: no tractor can do {d['type']!r}; expected one of {JOB_TYPES}")
    if d["field"] not in s.fields:
        raise HTTPException(404, f"unknown field {d['field']!r}")
    job = {"id": jid, "type": d["type"], "field": d["field"],
           "priority": _as_int(d["priority"], f"job {jid} priority", 1, 3),
           "deadline": _as_int(d["deadline"], f"job {jid} deadline", 0),
           "submitted_at": t}
    taken.add(jid)
    return job


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
    """Prefer B's explain.why_text; fall back to a simple template if it fails."""
    try:
        from orchestrator import explain  # type: ignore
        if hasattr(explain, "why_text"):
            return explain.why_text(job_id, rec, sim.state)
    except Exception as exc:  # a bad record must not turn "Why?" into a 500
        sim.log.write(sim.t, "warning", detail=f"why_text failed for {job_id}: {exc!r}")
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
            if not math.isfinite(body.ticks_per_sec):
                raise HTTPException(400, "ticks_per_sec must be a finite number")
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
