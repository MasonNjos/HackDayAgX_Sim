import { useState } from "react";
import type { LogRecord, State, Why } from "../types";

/** Display names for dispatchers / plan sources. */
export const SOURCE_LABEL: Record<string, string> = {
  "claude-coordinator": "Claude", claude: "Claude",
  "greedy fallback": "Greedy fallback",
  coordinator: "Greedy", baseline: "FCFS baseline",
};
export const sourceLabel = (s: string) => SOURCE_LABEL[s] ?? s;

export function ExplanationPanel({ state }: { state: State }) {
  return (
    <div className="explain">
      <div className="explain-head">
        {state.plan_source && (
          <span className={`source src-${state.plan_source.replace(/\W+/g, "-")}`}
                title="Which coordinator made the plan the tractors are following">
            Plan by {sourceLabel(state.plan_source)}
          </span>
        )}
        {state.planning && (
          <span className="planning" role="status">
            <i className="spinner" /> {sourceLabel(state.planning.dispatcher)} is replanning
            {state.planning.trigger ? ` after ${state.planning.trigger.replace(/_/g, " ")}` : ""}… {state.planning.seconds.toFixed(0)}s
          </span>
        )}
      </div>
      <p>{state.explanation || <span className="muted">No plan yet. Press Start.</span>}</p>
      {state.warnings.length > 0 && (
        <ul className="warnings">{state.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
      )}
    </div>
  );
}

export function WhyPanel({ why, onClose }: { why: Why | null; onClose: () => void }) {
  if (!why) return <p className="muted">Click <b>Why?</b> on a job (or a field on the map) to see why it went where it did.</p>;
  const rec = why.record as Record<string, any> | null;
  return (
    <div className="why">
      <div className="why-head"><b>Why {why.job}?</b><button className="small-btn" onClick={onClose}>Close</button></div>
      <p>{why.text}</p>
      {rec?.cost && (
        <dl className="terms">
          {Object.entries(rec.cost as Record<string, number>).map(([k, v]) => (
            <div key={k}><dt>{k.replace(/_/g, " ")}</dt><dd>{v}</dd></div>
          ))}
        </dl>
      )}
    </div>
  );
}

function describe(r: LogRecord): string {
  const p = r as Record<string, any>;
  switch (r.kind) {
    case "event": return `EVENT ${p.event} ${JSON.stringify(p.params)}`;
    case "assignment": return `${p.job} → ${p.tractor}${p.previous ? ` (was ${p.previous})` : ""}`;
    case "plan": return `${p.source ? `[${sourceLabel(p.source)}] ` : ""}replan${p.trigger ? ` (${p.trigger})` : ""}: ${p.explanation ?? ""}`;
    case "plan_discarded": return `plan dropped: ${p.reason}`;
    case "dispatcher_error": return `PLANNER ERROR ${p.error}`;
    case "job_started": return `${p.tractor} started ${p.job}`;
    case "job_completed": return `${p.tractor} finished ${p.job}${p.on_time ? "" : " — LATE"}`;
    case "job_released": return `${p.job} released by ${p.tractor} (${p.reason}, ${Math.round((1 - p.remaining) * 100)}% done)`;
    case "violation": return `VIOLATION ${p.violation}: ${p.detail}`;
    case "refuelled": return `${p.tractor} refuelled`;
    case "unreachable": return `${p.tractor} cannot reach ${p.stop}`;
    default: return `${r.kind} ${JSON.stringify({ ...p, t: undefined, kind: undefined, dispatcher: undefined })}`;
  }
}

export function EventLog({ log }: { log: LogRecord[] }) {
  return (
    <ol className="log" reversed>
      {[...log].reverse().map((r, i) => (
        <li key={i} className={`log-${r.kind}`}><span className="num muted">t={r.t}</span> {describe(r)}</li>
      ))}
    </ol>
  );
}

export function OperatorControls({ state, onEvent, onJobs }: {
  state: State;
  onEvent: (kind: string, params: Record<string, unknown>) => void;
  onJobs: (jobs: Record<string, unknown>[]) => void;
}) {
  const open = state.jobs.filter((j) => j.status !== "done");
  const [prioJob, setPrioJob] = useState("");
  const [prio, setPrio] = useState(3);
  const [job, setJob] = useState({ type: "spray", field: state.fields[0]?.id ?? "", priority: 2, deadline: 300 });
  const [bulk, setBulk] = useState("");

  return (
    <div className="controls">
      <div className="ctl-group">
        <h3>Change priority</h3>
        <select value={prioJob} onChange={(e) => setPrioJob(e.target.value)}>
          <option value="">job…</option>
          {open.map((j) => <option key={j.id} value={j.id}>{j.id} (P{j.priority})</option>)}
        </select>
        <select value={prio} onChange={(e) => setPrio(Number(e.target.value))}>
          {[1, 2, 3].map((p) => <option key={p} value={p}>P{p}</option>)}
        </select>
        <button disabled={!prioJob} onClick={() => onEvent("priority_change", { job: prioJob, priority: prio })}>Apply</button>
      </div>

      <div className="ctl-group">
        <h3>Add job</h3>
        <select value={job.type} onChange={(e) => setJob({ ...job, type: e.target.value })}>
          <option value="spray">spray</option><option value="mow">mow</option>
        </select>
        <select value={job.field} onChange={(e) => setJob({ ...job, field: e.target.value })}>
          {state.fields.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
        </select>
        <select value={job.priority} onChange={(e) => setJob({ ...job, priority: Number(e.target.value) })}>
          {[1, 2, 3].map((p) => <option key={p} value={p}>P{p}</option>)}
        </select>
        <label className="inline">deadline t=
          <input type="number" min={state.t} value={job.deadline}
                 onChange={(e) => setJob({ ...job, deadline: Number(e.target.value) })} />
        </label>
        {/* No id: the server picks the next free one, including jobs queued before Start. */}
        <button onClick={() => onJobs([{ ...job }])}>Submit</button>
      </div>

      <details className="ctl-group">
        <summary>Submit a job list (JSON)</summary>
        <textarea rows={4} value={bulk} onChange={(e) => setBulk(e.target.value)}
                  placeholder='[{"type":"mow","field":"F2","priority":2,"deadline":400}]  (id optional)' />
        <button onClick={() => {
          try { onJobs(JSON.parse(bulk)); setBulk(""); } catch { alert("Not valid JSON"); }
        }}>Submit list</button>
      </details>

      <p className="muted small">Tip: click a road on the map to block or reopen it.</p>
    </div>
  );
}
