"""Run the scripted scenario with each dispatcher and write the hand-in outputs.

    python run_demo.py [scenarios/farm.json]
    COORDINATOR=llm python run_demo.py      # adds the Claude coordinator (needs ANTHROPIC_API_KEY)

Runs FCFS baseline and the greedy coordinator; with COORDINATOR=llm also the Claude one.
Writes:
  out/event_log.jsonl           the coordinator's run (Claude if COORDINATOR=llm, else greedy): the hand-in log
  out/event_log_greedy.jsonl    greedy run (only when Claude is the hand-in coordinator)
  out/event_log_baseline.jsonl  baseline run
  out/comparison.md             side-by-side metrics
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from orchestrator.baseline import FCFSDispatcher
from orchestrator.eventlog import EventLog
from orchestrator.fleet_agent import FleetAgent
from orchestrator.llm_agent import LLMFleetAgent
from orchestrator.maintenance_agent import MaintenanceAgent
from orchestrator.models import load_scenario
from orchestrator.sim import Sim

ROOT = Path(__file__).parent
OUT = ROOT / "out"


def run_one(scenario: Path, dispatcher, log_path: Path) -> tuple[dict, Sim]:
    state, events, meta = load_scenario(scenario)
    log = EventLog(log_path, dispatcher=dispatcher.name)
    sim = Sim(state, events, dispatcher, log, horizon=meta.get("horizon", 480))
    MaintenanceAgent().attach(sim)  # same repair crews for baseline and coordinator
    try:
        summary = sim.run()
    finally:
        log.close()
    return summary, sim


ROWS = [  # (label, key, higher is better; None = informational)
    ("Jobs completed", "jobs_completed", True),
    ("Deadlines met", "deadlines_met", True),
    ("Priority-weighted work", "priority_weighted_work", True),
    ("Travel time (tractor-min)", "travel_ticks", False),
    ("Travel distance (km)", "travel_km", False),
    ("Travel per job completed (km)", "travel_per_job", False),
    ("Idle time (tractor-min)", "idle_ticks", False),
    ("Fuel used (L)", "fuel_used", False),
    ("Constraint violations", "violations", False),
    ("Sim minutes run", "ticks", None),
    ("Replans", "replans", None),
]


def with_derived(summary: dict, sim: Sim) -> dict:
    s = dict(summary)
    s["travel_per_job"] = round(s["travel_km"] / s["jobs_completed"], 2) if s["jobs_completed"] else "-"
    sources = [r.get("source", "") for r in sim.log.records if r["kind"] == "plan"]
    s["plans_by_source"] = {src: sources.count(src) for src in dict.fromkeys(sources)}
    return s


def comparison_md(scenario: Path, runs: list[tuple[str, dict, Sim]]) -> str:
    """runs: [(column name, summary, sim)], baseline first."""
    first = runs[0][1]
    names = [n for n, _, _ in runs]
    lines = [f"# {' vs '.join(reversed(names))}", "",
             f"Scenario: `{scenario.relative_to(ROOT) if scenario.is_relative_to(ROOT) else scenario}` "
             f"({first['jobs_total']} jobs incl. ones added by events). Same scenario, same scripted events, "
             "same simulator, same Maintenance agent.",
             "", "| Metric | " + " | ".join(names) + " | Best |",
             "|---|" + "---:|" * len(runs) + ":---:|"]
    for label, key, higher_better in ROWS:
        vals = [summ[key] for _, summ, _ in runs]
        cells = [f"{v} / {summ['priority_weighted_max']}" if key == "priority_weighted_work" else str(v)
                 for v, (_, summ, _) in zip(vals, runs)]
        best = ""
        nums = [v for v in vals if isinstance(v, (int, float))]
        if higher_better is not None and len(set(nums)) > 1:
            target = max(nums) if higher_better else min(nums)
            best = ", ".join(n for n, v in zip(names, vals) if v == target)
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {best} |")

    llm = [(n, summ) for n, summ, sim in runs if getattr(sim.dispatcher, "name", "") == "claude-coordinator"]
    for n, summ in llm:
        lines += ["", f"**{n}: who made each plan:** " +
                  ", ".join(f"{src} x{c}" for src, c in summ["plans_by_source"].items())]

    for name, summ, _ in runs:
        lines += ["", f"## {name}: per tractor", "",
                  "| Tractor | Travel (min) | Travel (km) | Work (min) | Idle (min) | Refuel (min) | Out of service (min) | Fuel used (L) |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for tid, v in summ["per_tractor"].items():
            lines.append(f"| {tid} | {v['travel_ticks']} | {v['travel_km']} | {v['work_ticks']} | {v['idle_ticks']} "
                         f"| {v['refuel_ticks']} | {v['broken_ticks']} | {v['fuel_used']} |")

    for name, _, sim in runs:
        if sim.metrics.violations:
            lines += ["", f"## {name}: violations", ""]
            lines += [f"- t={v['t']} {v['kind']}: {v['detail']}" for v in sim.metrics.violations]
    return "\n".join(lines) + "\n"


def main() -> None:
    scenario = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "scenarios" / "farm.json"
    OUT.mkdir(exist_ok=True)
    use_llm = os.environ.get("COORDINATOR", "greedy") == "llm"  # set in .env

    plan = [("Baseline (FCFS)", FCFSDispatcher(), "event_log_baseline.jsonl"),
            ("Greedy coordinator", FleetAgent(), "event_log_greedy.jsonl" if use_llm else "event_log.jsonl")]
    if use_llm:
        plan.append(("Claude coordinator", LLMFleetAgent(), "event_log.jsonl"))

    runs = []
    for name, dispatcher, log_name in plan:
        print(f"Running {name} ...", flush=True)
        summary, sim = run_one(scenario, dispatcher, OUT / log_name)
        runs.append((name, with_derived(summary, sim), sim))
    md = comparison_md(scenario, runs)
    (OUT / "comparison.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
