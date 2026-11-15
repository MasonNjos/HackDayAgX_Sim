// Mirrors the JSON from GET /api/state (Sim.snapshot() + server extras).

export interface Node { id: string; x: number; y: number }
export interface Edge { id: string; a: string; b: string; length: number; label: string; blocked: boolean }
export interface Depot { id: string; node: string }
export interface Field { id: string; name: string; node: string; area: number; x: number; y: number }

export type TractorStatus = "idle" | "travelling" | "working" | "refuelling" | "broken" | "complete";
export interface Tractor {
  id: string;
  implement: string;
  status: TractorStatus;
  x: number;
  y: number;
  location: string;
  fuel: number;
  fuel_capacity: number;
  current_job: string | null;
  stall_reason: string | null;
  queue: string[];          // "job:J4" | "refuel:D1"
  route: [number, number][]; // from its position to its queue head
}

export type JobStatus = "pending" | "assigned" | "in_progress" | "done" | "unassignable";
export interface Job {
  id: string;
  type: string;
  field: string;
  priority: number;
  deadline: number;
  submitted_at: number;
  remaining: number;
  status: JobStatus;
  owner: string | null;
  completed_at: number | null;
  completed_by: string[];
}

export interface LogRecord { t: number; kind: string; [key: string]: unknown }

export interface Summary {
  jobs_total: number;
  jobs_completed: number;
  deadlines_met: number;
  travel_ticks: number;
  travel_km: number;
  idle_ticks: number;
  fuel_used: number;
  violations: number;
  replans: number;
}

export interface State {
  t: number;
  horizon: number;
  finished: boolean;
  dispatcher: string;
  dispatchers: string[];
  nodes: Node[];
  edges: Edge[];
  depots: Depot[];
  fields: Field[];
  tractors: Tractor[];
  jobs: Job[];
  explanation: string;
  plan_source: string;      // who made the committed plan: "claude-coordinator", "greedy fallback", "coordinator", "baseline"
  planning: { trigger: string | null; seconds: number; dispatcher: string } | null; // a replan is being computed
  warnings: string[];
  log: LogRecord[];
  metrics: Summary;
  speed: { ticks_per_sec: number; paused: boolean };
  error?: string | null;  // set when the sim hit an exception; cleared by Reset
}

export interface Why { job: string; record: Record<string, unknown> | null; text: string }
