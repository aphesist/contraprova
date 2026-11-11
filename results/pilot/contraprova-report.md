# contraprova analysis

Price table: `anthropic-2026-11` (fingerprint `2857c0b5d7bf`); dollars recomputed from recorded tokens.
Log: 504 events, head `695fe913360b`. Intervals are 95%: Wilson for proportions, percentile bootstrap (10000 resamples, seed 0) for costs, shares and paired differences.

## Arms: solve rate and cost

| Arm | n tasks | Solved | Solve rate [95% CI] | Cost per task [95% CI] | Cost per solved task [95% CI] | Total recorded cost |
|---|---|---|---|---|---|---|
| A | 18 | 18/18 | 100.0% [82.4%, 100.0%] | $0.0323 [$0.0275, $0.0372] | $0.0323 [$0.0275, $0.0372] | $0.5814 |
| B | 18 | 17/18 | 94.4% [74.2%, 99.0%] | $0.0013 [$0.0011, $0.0016] | $0.0014 [$0.0011, $0.0018] | $0.0239 |
| C | 18 | 18/18 | 100.0% [82.4%, 100.0%] | $0.0469 [$0.0408, $0.0530] | $0.0469 [$0.0408, $0.0530] | $0.8435 |
| D | 18 | 18/18 | 100.0% [82.4%, 100.0%] | $0.0297 [$0.0265, $0.0330] | $0.0297 [$0.0265, $0.0330] | $0.5342 |

## Arms: token mix

| Arm | n tasks | Requests | Orchestrator token share [95% CI] | Orchestrator $ share [95% CI] | Cache-read share of prompt [95% CI] |
|---|---|---|---|---|---|
| A | 18 | 18 | 0.0% [0.0%, 0.0%] | 0.0% [0.0%, 0.0%] | 0.0% [0.0%, 0.0%] |
| B | 18 | 18 | 0.0% [0.0%, 0.0%] | 0.0% [0.0%, 0.0%] | 0.0% [0.0%, 0.0%] |
| C | 18 | 54 | 28.1% [27.0%, 29.3%] | 51.5% [49.0%, 54.2%] | 0.0% [0.0%, 0.0%] |
| D | 18 | 36 | 42.5% [41.1%, 44.1%] | 82.4% [73.4%, 90.9%] | 0.0% [0.0%, 0.0%] |

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
| D vs A | 18 | 18/18 | 18/18 | 0 | 0 | 1.0000 | - | +0.0 pts [+0.0 pts, +0.0 pts] | -$0.0026 [-$0.0071, $0.0015] |
| D vs B | 18 | 18/18 | 17/18 | 1 | 0 | 1.0000 | - | +5.6 pts [+0.0 pts, +16.7 pts] | $0.0283 [$0.0251, $0.0315] |
| D vs C | 18 | 18/18 | 18/18 | 0 | 0 | 1.0000 | - | +0.0 pts [+0.0 pts, +0.0 pts] | -$0.0172 [-$0.0226, -$0.0118] |
| C vs A | 18 | 18/18 | 18/18 | 0 | 0 | 1.0000 | - | +0.0 pts [+0.0 pts, +0.0 pts] | $0.0146 [$0.0121, $0.0171] |

## Escalation value

No fork point has both a scored control and a scored treatment.

## Gate quality

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| C.review | C | 18 | 18 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| T0.scope+T0.visible_tests | T0 | 15 | 15 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| T0.scope+T0.visible_tests+T1.private_tests | T0 | 3 | 3 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| accept.ungated | accept | 36 | 36 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

By tier:

| Gate | Tier | n decisions | Passed | Blocked | Blocks replayed | True-block rate [95% CI] | False-block rate [95% CI] |
|---|---|---|---|---|---|---|---|
| C | C | 18 | 18 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| T0 | T0 | 18 | 18 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |
| accept | accept | 36 | 36 | 0 | 0 | n/a (n=0) (n=0) | n/a (n=0) (n=0) |

A block is true when the blocked patch, replayed against the score tests, fails them.

## Cost-quality frontier

| Arm | n tasks | Cost per task | Solve rate | On frontier |
|---|---|---|---|---|
| B | 18 | $0.0013 | 94.4% | yes |
| D | 18 | $0.0297 | 100.0% | yes |
| A | 18 | $0.0323 | 100.0% | no |
| C | 18 | $0.0469 | 100.0% | no |

Frontier (cheapest first): B → D. Intervals for these points are in the arms table; the frontier is recomputed under `anthropic-2026-11`.

