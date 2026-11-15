# Coordinator vs FCFS baseline

Scenario: `scenarios/farm.json` (11 jobs incl. ones added by events). Same scenario, same scripted events, same simulator.

| Metric | Baseline (FCFS) | Coordinator | Better |
|---|---:|---:|:---:|
| Jobs completed | 6 | 10 | coordinator |
| Deadlines met | 4 | 10 | coordinator |
| Priority-weighted work | 12.84 / 22 | 20.2 / 22 | coordinator |
| Travel time (tractor-min) | 221 | 393 | baseline |
| Travel distance (km) | 47.9 | 86.1 | baseline |
| Idle time (tractor-min) | 53 | 115 | baseline |
| Fuel used (L) | 233.4 | 334.2 | baseline |
| Constraint violations | 1 | 0 | coordinator |
| Sim minutes run | 480 | 480 |  |
| Replans | 12 | 6 |  |

## Baseline: per tractor

| Tractor | Travel (min) | Travel (km) | Work (min) | Idle (min) | Refuel (min) | Out of service (min) | Fuel used (L) |
|---|---:|---:|---:|---:|---:|---:|---:|
| T1 | 44 | 11.0 | 106 | 0 | 0 | 330 | 60.0 |
| T2 | 30 | 7.5 | 30 | 0 | 0 | 420 | 24.0 |
| T3 | 147 | 29.4 | 280 | 53 | 0 | 0 | 149.4 |

## Coordinator: per tractor

| Tractor | Travel (min) | Travel (km) | Work (min) | Idle (min) | Refuel (min) | Out of service (min) | Fuel used (L) |
|---|---:|---:|---:|---:|---:|---:|---:|
| T1 | 120 | 30.0 | 240 | 115 | 5 | 0 | 144.0 |
| T2 | 30 | 7.5 | 30 | 0 | 0 | 420 | 24.0 |
| T3 | 243 | 48.6 | 232 | 0 | 5 | 0 | 166.2 |

## Baseline: violations

- t=150 out_of_fuel: T1 ran out of fuel at east
