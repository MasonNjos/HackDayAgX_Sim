"""Run the scripted scenario with the FCFS baseline and the coordinator, write hand-in outputs.

    python run_demo.py [scenarios/farm.json]

Writes:
  out/event_log.jsonl           coordinator run (the hand-in event log)
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


ROWS = [
    ("Jobs completed", "jobs_completed", True),
    ("Deadlines met", "deadlines_met", True),
    ("Priority-weighted work", "priority_weighted_work", True),
    ("Travel time (tractor-min)", "travel_ticks", False),
    ("Travel distance (km)", "travel_km", False),
    ("Idle time (tractor-min)", "idle_ticks", False),
    ("Fuel used (L)", "fuel_used", False),
    ("Constraint violations", "violations", False),
    ("Sim minutes run", "ticks", None),
    ("Replans", "replans", None),
]


def comparison_md(scenario: Path, base: dict, coord: dict, coord_sim: Sim, base_sim: Sim) -> str:
    lines = [f"# Coordinator vs FCFS baseline", "",
             f"Scenario: `{scenario.relative_to(ROOT) if scenario.is_relative_to(ROOT) else scenario}` "
             f"({base['jobs_total']} jobs incl. ones added by events). Same scenario, same scripted events, same simulator.",
             "", "| Metric | Baseline (FCFS) | Coordinator | Better |", "|---|---:|---:|:---:|"]
    for label, key, higher_better in ROWS:
        b, c = base[key], coord[key]
        if key == "priority_weighted_work":
            b, c = f"{b} / {base['priority_weighted_max']}", f"{c} / {coord['priority_weighted_max']}"
            bv, cv = base[key], coord[key]
        else:
            bv, cv = b, c
        mark = ""
        if higher_better is not None and bv != cv:
            mark = "coordinator" if (cv > bv) == higher_better else "baseline"
        lines.append(f"| {label} | {b} | {c} | {mark} |")

    for name, summ in (("Baseline", base), ("Coordinator", coord)):
        lines += ["", f"## {name}: per tractor", "",
                  "| Tractor | Travel (min) | Travel (km) | Work (min) | Idle (min) | Refuel (min) | Out of service (min) | Fuel used (L) |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for tid, v in summ["per_tractor"].items():
            lines.append(f"| {tid} | {v['travel_ticks']} | {v['travel_km']} | {v['work_ticks']} | {v['idle_ticks']} "
                         f"| {v['refuel_ticks']} | {v['broken_ticks']} | {v['fuel_used']} |")

    for name, sim in (("Baseline", base_sim), ("Coordinator", coord_sim)):
        if sim.metrics.violations:
            lines += ["", f"## {name}: violations", ""]
            lines += [f"- t={v['t']} {v['kind']}: {v['detail']}" for v in sim.metrics.violations]
    return "\n".join(lines) + "\n"


def main() -> None:
    scenario = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "scenarios" / "farm.json"
    OUT.mkdir(exist_ok=True)
    base, base_sim = run_one(scenario, FCFSDispatcher(), OUT / "event_log_baseline.jsonl")
    # COORDINATOR=llm (in .env) runs the Claude coordinator; anything else the greedy one.
    coordinator = LLMFleetAgent() if os.environ.get("COORDINATOR", "greedy") == "llm" else FleetAgent()
    print(f"Coordinator: {coordinator.name}", flush=True)
    coord, coord_sim = run_one(scenario, coordinator, OUT / "event_log.jsonl")
    md = comparison_md(scenario, base, coord, coord_sim, base_sim)
    (OUT / "comparison.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
