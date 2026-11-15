# Claude coordinator vs Greedy coordinator vs Baseline (FCFS)

Scenario: `scenarios/farm.json` (11 jobs incl. ones added by events). Same scenario, same scripted events, same simulator, same Maintenance agent.

Idle time counts a tractor stranded after running out of fuel (a planning failure); time lost to a breakdown counts as out of service.

| Metric | Baseline (FCFS) | Greedy coordinator | Claude coordinator | Best |
|---|---:|---:|---:|:---:|
| Jobs completed | 11 | 11 | 11 |  |
| Deadlines met | 7 | 11 | 11 | Greedy coordinator, Claude coordinator |
| Priority-weighted work | 22.0 / 22 | 22.0 / 22 | 22.0 / 22 |  |
| Travel time (tractor-min) | 295 | 294 | 318 | Greedy coordinator |
| Travel distance (km) | 66.4 | 67.0 | 73.0 | Baseline (FCFS) |
| Travel per job completed (km) | 6.04 | 6.09 | 6.64 | Baseline (FCFS) |
| Idle time (tractor-min, incl. stranded without fuel) | 428 | 376 | 352 | Claude coordinator |
| Fuel used (L) | 316.7 | 317.1 | 326.7 | Baseline (FCFS) |
| Constraint violations | 1 | 0 | 0 | Greedy coordinator, Claude coordinator |
| Sim minutes run | 426 | 410 | 410 |  |
| Replans | 19 | 10 | 10 |  |

**Claude coordinator: who made each plan:** claude-coordinator x10

## Baseline (FCFS): per tractor

| Tractor | Travel (min) | Travel (km) | Work (min) | Idle (min) | of which stranded, no fuel (min) | Refuel (min) | Out of service (min) | Fuel used (L) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T1 | 81 | 20.2 | 150 | 195 | 5 | 0 | 0 | 92.4 |
| T2 | 68 | 17.0 | 120 | 233 | 0 | 0 | 5 | 75.2 |
| T3 | 146 | 29.2 | 280 | 0 | 0 | 0 | 0 | 149.1 |

## Greedy coordinator: per tractor

| Tractor | Travel (min) | Travel (km) | Work (min) | Idle (min) | of which stranded, no fuel (min) | Refuel (min) | Out of service (min) | Fuel used (L) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T1 | 84 | 21.0 | 90 | 231 | 0 | 5 | 0 | 69.6 |
| T2 | 80 | 20.0 | 180 | 145 | 0 | 0 | 5 | 104.0 |
| T3 | 130 | 26.0 | 280 | 0 | 0 | 0 | 0 | 143.5 |

## Claude coordinator: per tractor

| Tractor | Travel (min) | Travel (km) | Work (min) | Idle (min) | of which stranded, no fuel (min) | Refuel (min) | Out of service (min) | Fuel used (L) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| T1 | 96 | 24.0 | 160 | 149 | 0 | 5 | 0 | 102.4 |
| T2 | 92 | 23.0 | 110 | 203 | 0 | 0 | 5 | 80.8 |
| T3 | 130 | 26.0 | 280 | 0 | 0 | 0 | 0 | 143.5 |

## Baseline (FCFS): violations

- t=150 out_of_fuel: T1 ran out of fuel at sw
