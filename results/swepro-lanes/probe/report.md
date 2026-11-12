# contraprova analysis

Price table: `anthropic-2026-11` (fingerprint `2857c0b5d7bf`); dollars recomputed from recorded tokens.
Log: 343 events, head `43f82853da5c`. Intervals are 95%: Wilson for proportions, percentile bootstrap (10000 resamples, seed 0) for costs, shares and paired differences.

## Arms: solve rate and cost

| Arm | n tasks | Solved | Solve rate [95% CI] | Cost per task [95% CI] | Cost per solved task [95% CI] | Total recorded cost |
|---|---|---|---|---|---|---|
| La | 12 | 10/12 | 83.3% [55.2%, 95.3%] | $0.1213 [$0.0621, $0.2253] | $0.1456 [$0.0661, $0.3408] | $1.4562 |
| La-sonnet | 12 | 10/12 | 83.3% [55.2%, 95.3%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |
| La-first | 12 | 7/12 | 58.3% [32.0%, 80.7%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |
| La-shadow | 12 | 5/12 | 41.7% [19.3%, 68.0%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |

## Arms: token mix

| Arm | n tasks | Requests | Orchestrator token share [95% CI] | Orchestrator $ share [95% CI] | Cache-read share of prompt [95% CI] |
|---|---|---|---|---|---|
| La | 12 | 47 | 8.3% [5.3%, 12.3%] | 45.2% [21.2%, 84.6%] | 0.0% [0.0%, 0.0%] |
| La-sonnet | 12 | 0 | n/a (n=12) | n/a (n=12) | n/a (n=12) |
| La-first | 12 | 0 | n/a (n=12) | n/a (n=12) | n/a (n=12) |
| La-shadow | 12 | 0 | n/a (n=12) | n/a (n=12) | n/a (n=12) |

## Paired contrasts: primary (Holm across m=1)

| Contrast (a vs b) | n shared | a solved | b solved | a only | b only | Exact McNemar p | Holm p | Δ solve rate a−b [95% CI] | Δ cost per solved a−b [95% CI] |
|---|---|---|---|---|---|---|---|---|---|
| La vs La-shadow | 12 | 10/12 | 5/12 | 5 | 0 | 0.0625 | 0.0625 | +41.7 pts [+16.7 pts, +66.7 pts] | $0.1456 [$0.0658, $0.3665] |

## Paired contrasts: secondary (unadjusted)

| Contrast (a vs b) | n shared | a solved | b solved | a only | b only | Exact McNemar p | Holm p | Δ solve rate a−b [95% CI] | Δ cost per solved a−b [95% CI] |
|---|---|---|---|---|---|---|---|---|---|
| La vs D | 0 | 0/0 | 0/0 | 0 | 0 | n/a | - | n/a (n=0) | n/a (n=0) |
| La vs La-sonnet | 12 | 10/12 | 10/12 | 0 | 0 | 1.0000 | - | +0.0 pts [+0.0 pts, +0.0 pts] | $0.1456 [$0.0669, $0.3447] |
| La-sonnet vs La-first | 12 | 10/12 | 7/12 | 3 | 0 | 0.2500 | - | +25.0 pts [+0.0 pts, +50.0 pts] | $0.0000 [$0.0000, $0.0000] |
| La-first vs La-shadow | 12 | 7/12 | 5/12 | 2 | 0 | 0.5000 | - | +16.7 pts [+0.0 pts, +41.7 pts] | $0.0000 [$0.0000, $0.0000] |
| La vs B | 0 | 0/0 | 0/0 | 0 | 0 | n/a | - | n/a (n=0) | n/a (n=0) |
| D vs B | 0 | 0/0 | 0/0 | 0 | 0 | n/a | - | n/a (n=0) | n/a (n=0) |

## Escalation value

No fork point has both a scored control and a scored treatment.

## Gate quality

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| T0.scope+T0.edits | T0 | 1 | 0 | 1 | 1 | 100.0% [20.7%, 100.0%] (n=1) | 0.0% [0.0%, 79.3%] (n=1) |
| T0.scope+T0.edits+T0.interfaces | T0 | 1 | 0 | 1 | 1 | 100.0% [20.7%, 100.0%] (n=1) | 0.0% [0.0%, 79.3%] (n=1) |
| T0.scope+T0.edits+T0.interfaces+T0.visible_tests | T0 | 4 | 0 | 4 | 4 | 100.0% [51.0%, 100.0%] (n=4) | 0.0% [0.0%, 49.0%] (n=4) |
| T0.scope+T0.edits+T0.interfaces+T0.visible_tests+T1.private_tests | T0 | 12 | 11 | 1 | 1 | 100.0% [20.7%, 100.0%] (n=1) | 0.0% [0.0%, 79.3%] (n=1) |
| lane.blame | lane | 6 | 0 | 6 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| lane.merged | lane | 22 | 22 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| lane.unsolved | lane | 1 | 0 | 1 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

By tier:

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| T0 | T0 | 18 | 11 | 7 | 7 | 100.0% [64.6%, 100.0%] (n=7) | 0.0% [0.0%, 35.4%] (n=7) |
| lane | lane | 29 | 22 | 7 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

A block is true when the blocked patch, replayed against the score tests, fails them.

## Cost-quality frontier

| Arm | n tasks | Cost per task | Solve rate | On frontier |
|---|---|---|---|---|
| La-sonnet | 12 | $0.0000 | 83.3% | yes |
| La-first | 12 | $0.0000 | 58.3% | no |
| La-shadow | 12 | $0.0000 | 41.7% | no |
| La | 12 | $0.1213 | 83.3% | no |

Frontier (cheapest first): La-sonnet. Intervals for these points are in the arms table; the frontier is recomputed under `anthropic-2026-11`.

## Contract Lanes

Tasks planned: 12; lanes chosen (k: tasks): 1: 1, 2: 11; single-lane fallbacks: 0; plan repairs: 0.
Merge rounds (rounds: tasks): 1: 8, 2: 3, 4: 1. Ladder step reached per lane: haiku 19, haiku-repair 3, opus 1. Blame rule of blocked merges (rule: blocks): 1: 6, 3: 1.
Planner cost $0.6581 of La's $1.4562 (45.2%); every planner, lane, repair and escalation request is La's cost.
Cost per solved task, La / D (shared tasks): n/a (n=0) (estimation).

### Coordination clashes (deterministic detectors, not validated against human labels)

| Clash | governed | Tasks with one |
|---|---|---|
| duplicated_work | 1 | 1 |
| interface_mismatch | 1 | 1 |
| unattributable_failure | 1 | 1 |
| wasted_lane | 2 | 2 |

### First-reply edit failures (truncated / parse / apply)

| Group | Requests | Truncated | Parse | Apply | Tasks with any |
|---|---|---|---|---|---|
| La lanes | 23 | 1 | 2 | 0 | 3 |
| D | 0 | 0 | 0 | 0 | 0 |

