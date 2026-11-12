# contraprova analysis

Price table: `anthropic-2026-11` (fingerprint `2857c0b5d7bf`); dollars recomputed from recorded tokens.
Log: 1370 events, head `1683634bbcf4`. Intervals are 95%: Wilson for proportions, percentile bootstrap (10000 resamples, seed 0) for costs, shares and paired differences.

## Arms: solve rate and cost

| Arm | n tasks | Solved | Solve rate [95% CI] | Cost per task [95% CI] | Cost per solved task [95% CI] | Total recorded cost |
|---|---|---|---|---|---|---|
| B | 37 | 32/37 | 86.5% [72.0%, 94.1%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |
| D | 37 | 34/37 | 91.9% [78.7%, 97.2%] | $0.0891 [$0.0558, $0.1366] | $0.0970 [$0.0582, $0.1543] | $3.2964 |
| La | 37 | 36/37 | 97.3% [86.2%, 99.5%] | $0.1012 [$0.0633, $0.1530] | $0.1040 [$0.0633, $0.1666] | $3.7439 |
| La-sonnet | 37 | 36/37 | 97.3% [86.2%, 99.5%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |
| La-first | 37 | 32/37 | 86.5% [72.0%, 94.1%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |
| La-shadow | 37 | 28/37 | 75.7% [59.9%, 86.6%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |

Arm cost includes the one fork branch each task shipped: D $1.3370.

## Measurement overhead

| Parent arm | Fork role | Forks | Requests | Cost |
|---|---|---|---|---|
| D | control | 4 | 4 | $0.0345 |

Measurement overhead total: $0.0345. This is fork spend no arm shipped (controls and unshipped treatments): it exists only to measure escalation value, would not be spent in production, and is in no arm's cost. Escalation value still prices every fork.

## Arms: token mix

| Arm | n tasks | Requests | Orchestrator token share [95% CI] | Orchestrator $ share [95% CI] | Cache-read share of prompt [95% CI] |
|---|---|---|---|---|---|
| B | 37 | 0 | n/a (n=37) | n/a (n=37) | n/a (n=37) |
| D | 37 | 86 | 9.6% [7.9%, 11.5%] | 47.9% [31.3%, 75.9%] | 0.0% [0.0%, 0.0%] |
| La | 37 | 137 | 8.0% [6.3%, 10.2%] | 50.2% [33.8%, 79.1%] | 0.0% [0.0%, 0.0%] |
| La-sonnet | 37 | 0 | n/a (n=37) | n/a (n=37) | n/a (n=37) |
| La-first | 37 | 0 | n/a (n=37) | n/a (n=37) | n/a (n=37) |
| La-shadow | 37 | 0 | n/a (n=37) | n/a (n=37) | n/a (n=37) |

## Paired contrasts: primary (Holm across m=1)

| Contrast (a vs b) | n shared | a solved | b solved | a only | b only | Exact McNemar p | Holm p | Δ solve rate a−b [95% CI] | Δ cost per solved a−b [95% CI] |
|---|---|---|---|---|---|---|---|---|---|
| La vs La-shadow | 37 | 36/37 | 28/37 | 8 | 0 | 0.0078 | 0.0078 | +21.6 pts [+8.1 pts, +35.1 pts] | $0.1040 [$0.0635, $0.1656] |

## Paired contrasts: secondary (unadjusted)

| Contrast (a vs b) | n shared | a solved | b solved | a only | b only | Exact McNemar p | Holm p | Δ solve rate a−b [95% CI] | Δ cost per solved a−b [95% CI] |
|---|---|---|---|---|---|---|---|---|---|
| La vs D | 37 | 36/37 | 34/37 | 2 | 0 | 0.5000 | - | +5.4 pts [+0.0 pts, +13.5 pts] | $0.0070 [-$0.0490, $0.0591] |
| La vs La-sonnet | 37 | 36/37 | 36/37 | 0 | 0 | 1.0000 | - | +0.0 pts [+0.0 pts, +0.0 pts] | $0.1040 [$0.0627, $0.1650] |
| La-sonnet vs La-first | 37 | 36/37 | 32/37 | 4 | 0 | 0.1250 | - | +10.8 pts [+2.7 pts, +21.6 pts] | $0.0000 [$0.0000, $0.0000] |
| La-first vs La-shadow | 37 | 32/37 | 28/37 | 6 | 2 | 0.2891 | - | +10.8 pts [-2.7 pts, +24.3 pts] | $0.0000 [$0.0000, $0.0000] |
| La vs B | 37 | 36/37 | 32/37 | 4 | 0 | 0.1250 | - | +10.8 pts [+2.7 pts, +21.6 pts] | $0.1040 [$0.0631, $0.1655] |
| D vs B | 37 | 34/37 | 32/37 | 2 | 0 | 0.5000 | - | +5.4 pts [+0.0 pts, +13.5 pts] | $0.0970 [$0.0587, $0.1528] |

## Escalation value

| Treatment vs control | n fork points | Treatment solved | Control solved | Treatment only | Control only | Exact McNemar p | EV [95% CI] | Noise floor: control-vs-control divergence [95% CI] | Extra cost per fork [95% CI] | $ per extra solve [95% CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 vs claude-haiku-5-5 | 4 | 2/4 | 2/4 | 0 | 0 | 1.0000 | +0.0 pts [+0.0 pts, +0.0 pts] | n/a (n=0) (n=0 pairs) | $0.3256 [$0.1480, $0.5534] | n/a (n=4) |

EV = solve(treatment) − solve(same-model control) at the same fork point. The noise floor is how often two controls with different seeds disagree.

## Gate quality

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| T0.scope+T0.edits | T0 | 1 | 0 | 1 | 1 | 0.0% [0.0%, 79.3%] (n=1) | 100.0% [20.7%, 100.0%] (n=1) |
| T0.scope+T0.edits+T0.interfaces+T0.visible_tests | T0 | 6 | 3 | 3 | 3 | 100.0% [43.9%, 100.0%] (n=3) | 0.0% [0.0%, 56.1%] (n=3) |
| T0.scope+T0.edits+T0.interfaces+T0.visible_tests+T1.private_tests | T0 | 34 | 30 | 4 | 4 | 100.0% [51.0%, 100.0%] (n=4) | 0.0% [0.0%, 49.0%] (n=4) |
| T0.scope+T0.edits+T0.interfaces+T0.visible_tests+T1.private_tests+T2.review | T0 | 5 | 3 | 2 | 2 | 0.0% [0.0%, 65.8%] (n=2) | 100.0% [34.2%, 100.0%] (n=2) |
| T0.scope+T0.edits+T0.visible_tests | T0 | 8 | 3 | 5 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| T0.scope+T0.edits+T0.visible_tests+T1.private_tests | T0 | 34 | 31 | 3 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| T0.scope+T0.edits+T0.visible_tests+T1.private_tests+T2.review | T0 | 3 | 3 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| lane.blame | lane | 16 | 0 | 16 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| lane.merged | lane | 64 | 64 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| lane.unsolved | lane | 2 | 0 | 2 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

By tier:

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| T0 | T0 | 91 | 73 | 18 | 10 | 70.0% [39.7%, 89.2%] (n=10) | 30.0% [10.8%, 60.3%] (n=10) |
| lane | lane | 82 | 64 | 18 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

A block is true when the blocked patch, replayed against the score tests, fails them.

## Cost-quality frontier

| Arm | n tasks | Cost per task | Solve rate | On frontier |
|---|---|---|---|---|
| La-sonnet | 37 | $0.0000 | 97.3% | yes |
| B | 37 | $0.0000 | 86.5% | no |
| La-first | 37 | $0.0000 | 86.5% | no |
| La-shadow | 37 | $0.0000 | 75.7% | no |
| D | 37 | $0.0891 | 91.9% | no |
| La | 37 | $0.1012 | 97.3% | no |

Frontier (cheapest first): La-sonnet. Intervals for these points are in the arms table; the frontier is recomputed under `anthropic-2026-11`.

## Contract Lanes

Tasks planned: 37; lanes chosen (k: tasks): 1: 8, 2: 29; single-lane fallbacks: 0; plan repairs: 0.
Merge rounds (rounds: tasks): 1: 32, 2: 2, 3: 2, 4: 1. Ladder step reached per lane: haiku 58, haiku-repair 2, opus 2, sonnet 4. Blame rule of blocked merges (rule: blocks): 1: 3, 3: 7.
Planner cost $1.8793 of La's $3.7439 (50.2%); every planner, lane, repair and escalation request is La's cost.
Cost per solved task, La / D (shared tasks): 1.07 [0.62, 1.76] (estimation).

### Coordination clashes (deterministic detectors, not validated against human labels)

| Clash | governed | Tasks with one |
|---|---|---|
| interface_change_request | 2 | 1 |
| unattributable_failure | 7 | 3 |
| wasted_lane | 3 | 3 |

### First-reply edit failures (truncated / parse / apply)

| Group | Requests | Truncated | Parse | Apply | Tasks with any |
|---|---|---|---|---|---|
| La lanes | 66 | 0 | 7 | 3 | 8 |
| D | 37 | 0 | 1 | 3 | 4 |

