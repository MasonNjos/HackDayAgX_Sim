import type { State, Why } from "./types";

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, body === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json();
}

export const api = {
  state: () => call<State>("/state"),
  event: (kind: string, params: Record<string, unknown>) => call<State>("/event", { kind, params }),
  jobs: (jobs: Record<string, unknown>[]) => call<State>("/jobs", { jobs }),
  why: (job: string) => call<Why>(`/why/${encodeURIComponent(job)}`),
  speed: (s: { ticks_per_sec?: number; paused?: boolean }) => call<State>("/speed", s),
  reset: (dispatcher: string) => call<State>("/reset", { dispatcher }),
};
