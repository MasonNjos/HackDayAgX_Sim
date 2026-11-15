import type { Edge, Job, State } from "../types";

const PAD = 0.9;      // world units of margin around the graph
const SCALE = 64;     // px per world unit
export const TRACTOR_COLORS = ["#2563eb", "#d97706", "#7c3aed", "#0891b2", "#db2777"];

const JOB_RANK: Record<string, number> = { in_progress: 0, assigned: 1, pending: 2, unassignable: 3, done: 4 };

/** The job that best describes a field's state (active work first). */
export function fieldJob(jobs: Job[]): Job | undefined {
  return [...jobs].sort((a, b) => JOB_RANK[a.status] - JOB_RANK[b.status] || b.priority - a.priority)[0];
}

interface Props {
  state: State;
  selectedJob: string | null;
  onSelectJob: (id: string) => void;
  onToggleEdge: (edge: Edge) => void;
}

export function FarmMap({ state, selectedJob, onSelectJob, onToggleEdge }: Props) {
  const xs = state.nodes.map((n) => n.x);
  const ys = state.nodes.map((n) => n.y);
  const minX = Math.min(...xs) - PAD, maxX = Math.max(...xs) + PAD;
  const minY = Math.min(...ys) - PAD, maxY = Math.max(...ys) + PAD;
  const W = (maxX - minX) * SCALE, H = (maxY - minY) * SCALE;
  const px = (x: number) => (x - minX) * SCALE;
  const py = (y: number) => (maxY - y) * SCALE; // world y points up
  const node = Object.fromEntries(state.nodes.map((n) => [n.id, n]));
  const colorOf = Object.fromEntries(state.tractors.map((t, i) => [t.id, TRACTOR_COLORS[i % TRACTOR_COLORS.length]]));

  return (
    <svg className="map" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Farm map">
      {/* roads */}
      {state.edges.map((e) => {
        const a = node[e.a], b = node[e.b];
        const mx = (px(a.x) + px(b.x)) / 2, my = (py(a.y) + py(b.y)) / 2;
        return (
          <g key={e.id} className={`road ${e.blocked ? "blocked" : ""}`} onClick={() => onToggleEdge(e)}>
            <title>{`${e.label || e.id} · ${e.length} km — click to ${e.blocked ? "reopen" : "block"}`}</title>
            <line x1={px(a.x)} y1={py(a.y)} x2={px(b.x)} y2={py(b.y)} className="road-hit" />
            <line x1={px(a.x)} y1={py(a.y)} x2={px(b.x)} y2={py(b.y)} className="road-line" />
            {e.label && <text x={mx} y={my - 8} className="road-label">{e.label}</text>}
            {e.blocked && <text x={mx} y={my + 5} className="road-x">✕</text>}
          </g>
        );
      })}

      {/* fields */}
      {state.fields.map((f) => {
        const jobs = state.jobs.filter((j) => j.field === f.id);
        const j = fieldJob(jobs);
        const size = 26 + Math.sqrt(f.area) * 4;
        const selected = jobs.some((x) => x.id === selectedJob);
        return (
          <g key={f.id} className={`field status-${j?.status ?? "none"} ${selected ? "selected" : ""}`}
             onClick={() => j && onSelectJob(j.id)} transform={`translate(${px(f.x)},${py(f.y)})`}>
            <title>{`${f.name} (${f.area} ha)\n${jobs.map((x) => `${x.id} ${x.type} P${x.priority} ${x.status}`).join("\n")}`}</title>
            <rect x={-size / 2} y={-size / 2} width={size} height={size} rx={6} />
            {j && j.status === "in_progress" && (
              <rect className="progress" x={-size / 2} y={size / 2 - 5} height={5} rx={2}
                    width={size * (1 - j.remaining)} />
            )}
            <text y={-size / 2 - 6} className="field-name">{f.name}</text>
            <text y={4} className="field-jobs">{jobs.map((x) => x.id).join(" ")}</text>
          </g>
        );
      })}

      {/* depots */}
      {state.depots.map((d) => (
        <g key={d.id} transform={`translate(${px(node[d.node].x)},${py(node[d.node].y)})`} className="depot">
          <title>{`Depot ${d.id} (refuel)`}</title>
          <path d="M-14,10 L-14,-6 L0,-16 L14,-6 L14,10 Z" />
          <text y={5}>⛽</text>
        </g>
      ))}

      {/* planned routes */}
      {state.tractors.map((t) => t.route.length > 1 && (
        <polyline key={`r-${t.id}`} className="route" stroke={colorOf[t.id]}
                  points={t.route.map(([x, y]) => `${px(x)},${py(y)}`).join(" ")} />
      ))}

      {/* tractors (offset slightly so tractors on one node don't overlap) */}
      {state.tractors.map((t, i) => {
        const ox = (i - (state.tractors.length - 1) / 2) * 16;
        const frac = Math.max(0, Math.min(1, t.fuel / t.fuel_capacity));
        return (
          <g key={t.id} className={`tractor ${t.status}`} transform={`translate(${px(t.x) + ox},${py(t.y) - 22})`}>
            <title>{`${t.id} ${t.implement} · ${t.status} · fuel ${t.fuel}/${t.fuel_capacity} L`}</title>
            <circle r={11} fill={colorOf[t.id]} />
            <text y={4}>{t.id}</text>
            {t.status === "broken" && <text className="broken-x" x={9} y={-7}>⚠</text>}
            <rect className="fuel-bg" x={-12} y={14} width={24} height={4} rx={2} />
            <rect className={`fuel ${frac < 0.2 ? "low" : ""}`} x={-12} y={14} width={24 * frac} height={4} rx={2} />
          </g>
        );
      })}
    </svg>
  );
}
