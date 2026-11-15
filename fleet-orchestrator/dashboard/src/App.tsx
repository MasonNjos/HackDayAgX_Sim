import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { FarmMap } from "./components/FarmMap";
import { SatMap } from "./components/SatMap";
import { EventLog, ExplanationPanel, OperatorControls, WhyPanel, sourceLabel } from "./components/Panels";
import { JobTable, TractorTable } from "./components/Tables";
import type { Edge, State, Why } from "./types";

const POLL_MS = 400;
const VIEW_KEY = "fleet.mapView";
type MapView = "map" | "satellite";

function loadView(): MapView {
  try { return localStorage.getItem(VIEW_KEY) === "satellite" ? "satellite" : "map"; } catch { return "map"; }
}

/** Server dispatcher name (state.dispatcher) -> key accepted by POST /api/reset. */
const resetKey = (name: string) => (name === "claude-coordinator" ? "claude" : name);

export default function App() {
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [why, setWhy] = useState<Why | null>(null);
  const [view, setView] = useState<MapView>(loadView);
  const pickView = (v: MapView) => { setView(v); try { localStorage.setItem(VIEW_KEY, v); } catch { /* ignore */ } };
  const [resetTo, setResetTo] = useState<string | null>(null); // null = the current dispatcher
  const [resetting, setResetting] = useState(false);
  // Bumped by every operator action. A poll that started before the latest action is dropped,
  // so a stale /api/state can't overwrite the fresh state the action returned (e.g. after Reset).
  const actionSeq = useRef(0);

  // Poll /api/state. Actions also return the fresh state, so the UI updates at once.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      const seq = actionSeq.current;
      try {
        const s = await api.state();
        if (alive && seq === actionSeq.current) { setState(s); setError((e) => (e?.startsWith("API") ? null : e)); }
      } catch {
        if (alive) setError("API unreachable — is the server running on :8000?");
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const run = useCallback(async (p: Promise<State>) => {
    actionSeq.current += 1;
    try { setState(await p); setError(null); } catch (e) { setError((e as Error).message); }
  }, []);

  const doReset = useCallback(async (dispatcher: string) => {
    setResetting(true);
    setWhy(null);
    await run(api.reset(dispatcher));
    setResetTo(null);
    setResetting(false);
  }, [run]);

  const showWhy = useCallback(async (job: string) => {
    try { setWhy(await api.why(job)); } catch (e) { setError((e as Error).message); }
  }, []);

  // Keep an open "why" fresh as the plan changes.
  const whyJob = why?.job;
  useEffect(() => { if (whyJob) showWhy(whyJob); }, [state?.metrics.replans, whyJob, showWhy]);

  if (!state) return <div className="loading">{error ?? "Connecting…"}</div>;

  const m = state.metrics;
  const toggleEdge = (e: Edge) => run(api.event(e.blocked ? "route_unblocked" : "route_blocked", { edge: e.id }));

  return (
    <div className="app">
      <header>
        <div className="title">
          <h1>Fleet Command Center</h1>
          <span className={`badge ${state.dispatcher}`}>{sourceLabel(state.dispatcher)}</span>
          {state.planning && <span className="pill planning-pill"><i className="spinner" /> planning</span>}
        </div>
        <div className="clock num">
          minute <b>{state.t}</b> <span className="muted">of {state.horizon}</span>
          {state.finished && <span className="pill done">finished</span>}
        </div>
        <div className="transport">
          <button className="primary" onClick={() => run(api.speed({ paused: !state.speed.paused }))}>
            {state.speed.paused ? (state.t === 0 ? "▶ Start" : "▶ Resume") : "❚❚ Pause"}
          </button>
          <label className="inline">speed
            <select value={state.speed.ticks_per_sec}
                    onChange={(e) => run(api.speed({ ticks_per_sec: Number(e.target.value) }))}>
              {[1, 2, 5, 10, 20, 50].map((v) => <option key={v} value={v}>{v} min/s</option>)}
            </select>
          </label>
          <label className="inline">coordinator
            {/* Holds the user's choice (not reset to a placeholder on every 400 ms poll, which
                fought the open menu); the Reset button applies it. */}
            <select value={resetTo ?? resetKey(state.dispatcher)} onChange={(e) => setResetTo(e.target.value)}>
              {state.dispatchers.map((d) => <option key={d} value={d}>{sourceLabel(d)}</option>)}
            </select>
          </label>
          <button disabled={resetting} onClick={() => doReset(resetTo ?? resetKey(state.dispatcher))}
                  title="Restart the scenario from minute 0 with the chosen coordinator">
            {resetting ? "Resetting…" : "↺ Reset"}
          </button>
        </div>
      </header>

      {state.error && (
        <div className="error" role="alert">
          Simulation stopped on an error: {state.error}. Use ↺ Reset to start over.
        </div>
      )}
      {error && <div className="error" role="alert">{error}<button className="small-btn" onClick={() => setError(null)}>×</button></div>}

      <section className="kpis">
        <Kpi label="Jobs done" value={`${m.jobs_completed}/${m.jobs_total}`} />
        <Kpi label="Deadlines met" value={m.deadlines_met} />
        <Kpi label="Travel" value={`${m.travel_km} km`} />
        <Kpi label="Idle" value={`${m.idle_ticks} min`} />
        <Kpi label="Violations" value={m.violations} bad={m.violations > 0} />
        <Kpi label="Replans" value={m.replans} />
      </section>

      <main>
        <section className="card map-card">
          <div className="map-head">
            <h2>Farm</h2>
            <div className="view-toggle" role="group" aria-label="Map view">
              <button className={view === "map" ? "on" : ""} aria-pressed={view === "map"} onClick={() => pickView("map")}>Map</button>
              <button className={view === "satellite" ? "on" : ""} aria-pressed={view === "satellite"} onClick={() => pickView("satellite")}>Satellite</button>
            </div>
          </div>
          {view === "map"
            ? <FarmMap state={state} selectedJob={why?.job ?? null} onSelectJob={showWhy} onToggleEdge={toggleEdge} />
            : <SatMap state={state} selectedJob={why?.job ?? null} onSelectJob={showWhy} onToggleEdge={toggleEdge} />}
          <Legend />
        </section>

        <section className="side">
          <div className="card">
            <h2>What changed</h2>
            <ExplanationPanel state={state} />
          </div>
          <div className="card">
            <h2>Why?</h2>
            <WhyPanel why={why} onClose={() => setWhy(null)} />
          </div>
          <div className="card">
            <h2>Tractors</h2>
            <TractorTable state={state}
                          onBreak={(id) => run(api.event("breakdown", { tractor: id }))}
                          onRepair={(id) => run(api.event("repair", { tractor: id }))} />
          </div>
        </section>

        <section className="card jobs-card">
          <h2>Jobs</h2>
          <JobTable state={state} selected={why?.job ?? null} onWhy={showWhy} />
        </section>

        <section className="card">
          <h2>Operator</h2>
          <OperatorControls state={state} onEvent={(k, p) => run(api.event(k, p))} onJobs={(j) => run(api.jobs(j))} />
        </section>

        <section className="card log-card">
          <h2>Event log</h2>
          <EventLog log={state.log} />
        </section>
      </main>
    </div>
  );
}

function Kpi({ label, value, bad }: { label: string; value: string | number; bad?: boolean }) {
  return <div className={`kpi ${bad ? "bad" : ""}`}><span className="num">{value}</span><small>{label}</small></div>;
}

function Legend() {
  const items = [["pending", "pending"], ["assigned", "assigned"], ["in_progress", "in progress"], ["done", "done"]];
  return (
    <div className="legend">
      {items.map(([k, l]) => <span key={k}><i className={`sw status-${k}`} />{l}</span>)}
      <span><i className="sw blocked-sw" />blocked road</span>
      <span>- - planned route</span>
    </div>
  );
}
