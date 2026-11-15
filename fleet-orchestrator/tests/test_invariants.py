"""Invariants checked after every sim tick, over many seeds and breakdown times.

Skips until Person A's orchestrator/sim.py lands. The sim API used below is a
guess (Sim(state, events, dispatcher, seed).step() / .done / .state / .plan);
adjust it to the real one at the 2:00 sync.
"""
import random
from pathlib import Path

import pytest

from orchestrator.fleet_agent import FleetAgent
from orchestrator.models import DONE, Event, load_scenario
from orchestrator.safety_agent import validate

sim_mod = pytest.importorskip("orchestrator.sim")

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "farm.json"
CASES = [(seed, bt) for seed in range(5) for bt in (5, 40, 60, 120, 200)]


def check_tick(state, plan, done_by: dict[str, list[str]]):
    assert validate(plan, state) == [], f"t={state.t}"
    for t in state.tractors.values():
        assert t.fuel >= 0, f"{t.id} fuel {t.fuel} at t={state.t}"
    for j in state.jobs.values():
        assert 0.0 <= j.remaining <= 1.0
        if j.id in done_by:  # once done, never reopened or reassigned
            assert j.status == DONE and j.completed_by == done_by[j.id]
        elif j.status == DONE:
            done_by[j.id] = list(j.completed_by)
    working = [t.current_job for t in state.tractors.values() if t.current_job]
    assert len(working) == len(set(working)), f"two tractors on one job at t={state.t}"


@pytest.mark.parametrize("seed,breakdown_t", CASES)
def test_invariants_hold_every_tick(seed, breakdown_t):
    state, events, meta = load_scenario(SCENARIO)
    rng = random.Random(seed)
    victim = rng.choice(sorted(state.tractors))
    events = [e for e in events if e.kind != "breakdown"] + [Event(breakdown_t, "breakdown", {"tractor": victim})]
    events.sort(key=lambda e: e.t)

    sim = sim_mod.Sim(state, events, FleetAgent(), seed=seed)
    done_by: dict[str, list[str]] = {}
    for _ in range(meta.get("horizon", 480)):
        sim.step()
        check_tick(sim.state, sim.plan, done_by)
        if sim.done:
            break
