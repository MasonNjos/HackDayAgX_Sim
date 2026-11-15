import type { State } from "../types";
import { TRACTOR_COLORS } from "./FarmMap";

export function TractorTable({ state, onBreak, onRepair }: {
  state: State; onBreak: (id: string) => void; onRepair: (id: string) => void;
}) {
  return (
    <table>
      <thead><tr><th>Tractor</th><th>Status</th><th>Fuel</th><th>Queue</th><th /></tr></thead>
      <tbody>
        {state.tractors.map((t, i) => {
          const frac = t.fuel / t.fuel_capacity;
          return (
            <tr key={t.id}>
              <td><span className="dot" style={{ background: TRACTOR_COLORS[i % TRACTOR_COLORS.length] }} />
                <b>{t.id}</b> <span className="muted">{t.implement}</span></td>
              <td><span className={`pill ${t.status}`}>{t.status}</span>
                {t.stall_reason && <div className="muted small">{t.stall_reason.replace(/_/g, " ")}</div>}</td>
              <td><div className="fuelcell">
                <div className="bar"><div className={frac < 0.2 ? "low" : ""} style={{ width: `${frac * 100}%` }} /></div>
                <span className="num">{Math.round(t.fuel)} L</span>
              </div></td>
              <td className="queue">{t.queue.map((q) => q.replace("job:", "").replace("refuel:", "⛽")).join(" → ") || "—"}</td>
              <td>{t.status === "broken"
                ? <button className="small-btn" onClick={() => onRepair(t.id)}>Repair</button>
                : <button className="small-btn danger" onClick={() => onBreak(t.id)}>Break down</button>}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export function JobTable({ state, selected, onWhy }: {
  state: State; selected: string | null; onWhy: (id: string) => void;
}) {
  const fieldName = Object.fromEntries(state.fields.map((f) => [f.id, f.name]));
  return (
    <table>
      <thead>
        <tr><th>Job</th><th>Field</th><th>Pri</th><th>Owner</th><th>Status</th><th>Progress</th><th>Deadline</th><th /></tr>
      </thead>
      <tbody>
        {state.jobs.map((j) => {
          const late = j.status === "done" ? (j.completed_at ?? 0) > j.deadline : state.t > j.deadline;
          return (
            <tr key={j.id} className={j.id === selected ? "selected" : ""}>
              <td><b>{j.id}</b> <span className="muted">{j.type}</span></td>
              <td>{fieldName[j.field] ?? j.field}</td>
              <td><span className={`prio p${j.priority}`}>P{j.priority}</span></td>
              <td>{j.owner ?? "—"}</td>
              <td><span className={`pill ${j.status}`}>{j.status.replace("_", " ")}</span></td>
              <td><div className="fuelcell">
                <div className="bar"><div className="work" style={{ width: `${(1 - j.remaining) * 100}%` }} /></div>
                <span className="num">{Math.round((1 - j.remaining) * 100)}%</span>
              </div></td>
              <td className={`num ${late ? "late" : ""}`}>
                minute {j.deadline}{j.status === "done" && <span className="muted"> (done at {j.completed_at})</span>}
              </td>
              <td><button className="small-btn" onClick={() => onWhy(j.id)}>Why?</button></td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
