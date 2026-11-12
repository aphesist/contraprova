# contraprova analysis

Price table: `anthropic-2026-11` (fingerprint `2857c0b5d7bf`); dollars recomputed from recorded tokens.
Log: 1460 events, head `6bfb8893b4df`. Intervals are 95%: Wilson for proportions, percentile bootstrap (10000 resamples, seed 0) for costs, shares and paired differences.

## Arms: solve rate and cost

| Arm | n tasks | Solved | Solve rate [95% CI] | Cost per task [95% CI] | Cost per solved task [95% CI] | Total recorded cost |
|---|---|---|---|---|---|---|
| A | 88 | 85/88 | 96.6% [90.5%, 98.8%] | $0.1342 [$0.1125, $0.1594] | $0.1390 [$0.1160, $0.1656] | $11.8125 |
| B | 88 | 77/88 | 87.5% [79.0%, 92.9%] | $0.0000 [$0.0000, $0.0000] | $0.0000 [$0.0000, $0.0000] | $0.0000 |
| D | 88 | 83/88 | 94.3% [87.4%, 97.5%] | $0.0515 [$0.0406, $0.0658] | $0.0546 [$0.0421, $0.0711] | $4.5328 |

Arm cost includes the one fork branch each task shipped: D $1.3016.

## Measurement overhead

| Parent arm | Fork role | Forks | Requests | Cost |
|---|---|---|---|---|
| D | control | 8 | 11 | $0.0575 |

Measurement overhead total: $0.0575. This is fork spend no arm shipped (controls and unshipped treatments): it exists only to measure escalation value, would not be spent in production, and is in no arm's cost. Escalation value still prices every fork.

## Arms: token mix

| Arm | n tasks | Requests | Orchestrator token share [95% CI] | Orchestrator $ share [95% CI] | Cache-read share of prompt [95% CI] |
|---|---|---|---|---|---|
| A | 88 | 97 | 0.0% [0.0%, 0.0%] | 0.0% [0.0%, 0.0%] | 0.0% [0.0%, 0.0%] |
| B | 88 | 0 | n/a (n=88) | n/a (n=88) | n/a (n=88) |
| D | 88 | 192 | 13.1% [11.4%, 14.8%] | 62.7% [49.2%, 79.3%] | 0.0% [0.0%, 0.0%] |

## Paired contrasts: primary (Holm across m=5)

| Contrast (a vs b) | n shared | a solved | b solved | a only | b only | Exact McNemar p | Holm p | Δ solve rate a−b [95% CI] | Δ cost per solved a−b [95% CI] |
|---|---|---|---|---|---|---|---|---|---|
| La vs La-shadow | 0 | 0/0 | 0/0 | 0 | 0 | n/a | 1.0000 | n/a (n=0) | n/a (n=0) |
| B vs L1 | 0 | 0/0 | 0/0 | 0 | 0 | n/a | 1.0000 | n/a (n=0) | n/a (n=0) |
| La vs L1 | 0 | 0/0 | 0/0 | 0 | 0 | n/a | 1.0000 | n/a (n=0) | n/a (n=0) |
| Lb vs La | 0 | 0/0 | 0/0 | 0 | 0 | n/a | 1.0000 | n/a (n=0) | n/a (n=0) |
| Lc vs D | 0 | 0/0 | 0/0 | 0 | 0 | n/a | 1.0000 | n/a (n=0) | n/a (n=0) |

Contrasts with no shared tasks stay in the Holm family with p = 1.

## Paired contrasts: secondary (unadjusted)

| Contrast (a vs b) | n shared | a solved | b solved | a only | b only | Exact McNemar p | Holm p | Δ solve rate a−b [95% CI] | Δ cost per solved a−b [95% CI] |
|---|---|---|---|---|---|---|---|---|---|
| D vs A | 88 | 83/88 | 85/88 | 2 | 4 | 0.6875 | - | -2.3 pts [-8.0 pts, +3.4 pts] | -$0.0844 [-$0.1107, -$0.0611] |
| D vs B | 88 | 83/88 | 77/88 | 6 | 0 | 0.0312 | - | +6.8 pts [+2.3 pts, +12.5 pts] | $0.0546 [$0.0421, $0.0712] |

## Escalation value

| Treatment vs control | n fork points | Treatment solved | Control solved | Treatment only | Control only | Exact McNemar p | EV [95% CI] | Noise floor: control-vs-control divergence [95% CI] | Extra cost per fork [95% CI] | $ per extra solve [95% CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 vs claude-haiku-5-5 | 8 | 6/8 | 2/8 | 4 | 0 | 0.1250 | +50.0 pts [+12.5 pts, +87.5 pts] | n/a (n=0) (n=0 pairs) | $0.1555 [$0.0861, $0.2397] | $0.3110 [$0.1158, $1.3667] |

EV = solve(treatment) − solve(same-model control) at the same fork point. The noise floor is how often two controls with different seeds disagree.

## Gate quality

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| T0.scope+T0.edits+T0.visible_tests | T0 | 36 | 23 | 13 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| T0.scope+T0.edits+T0.visible_tests+T1.private_tests | T0 | 68 | 65 | 3 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| accept.ungated | accept | 88 | 88 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

By tier:

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| T0 | T0 | 104 | 88 | 16 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| accept | accept | 88 | 88 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

A block is true when the blocked patch, replayed against the score tests, fails them.

## Cost-quality frontier

| Arm | n tasks | Cost per task | Solve rate | On frontier |
|---|---|---|---|---|
| B | 88 | $0.0000 | 87.5% | yes |
| D | 88 | $0.0515 | 94.3% | yes |
| A | 88 | $0.1342 | 96.6% | yes |

Frontier (cheapest first): B → D → A. Intervals for these points are in the arms table; the frontier is recomputed under `anthropic-2026-11`.

