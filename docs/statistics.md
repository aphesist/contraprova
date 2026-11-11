# Statistics plan

This is how contraprova turns an event log into the study's numbers. The code is
`src/contraprova/analysis.py` (built on `src/contraprova/stats.py`); `report_markdown(events, table)` prints
everything below as one Markdown report. Every number in that report carries a 95% interval and the n it
rests on, and the report's header names the price table used.

## The design is paired

Every arm runs on the same evaluation tasks. So instead of comparing two solve rates as if they came from
different samples, we compare the two arms task by task. Most tasks are easy for both arms or hard for both;
those tasks say nothing about which arm is better. Only the **discordant** tasks matter: the ones that one
arm solved and the other did not. Pairing removes the task-difficulty noise, which is why a paired test
detects much smaller differences than an unpaired one on the same 90 tasks.

## Which method, and why

| Quantity | Method | Why |
|---|---|---|
| One arm's solve rate | Wilson score interval | Stays inside [0, 1] and keeps close to its nominal 95% coverage at small n and near 0% or 100%, where the textbook "p ± 1.96·SE" interval fails |
| Two arms on the same tasks: is there a difference? | Exact McNemar test | Uses only the discordant tasks. Under "no difference", each discordant task is a fair coin flip, so the p-value is an exact binomial tail, with no large-sample approximation |
| Two arms on the same tasks: how big is the difference? | Paired bootstrap | Resample *tasks* with replacement, keeping both arms' results for a task together, and read the 2.5th and 97.5th percentiles of the resampled difference |
| Costs, cost per solved task, token shares | Bootstrap over tasks | These are ratios of sums (dollars over solves, tokens over tokens), which have no simple formula for an interval. The bootstrap resamples tasks and recomputes the ratio |
| Several primary comparisons at once | Holm correction | With five tests at 5% each, the chance of at least one false positive is up to 23%. Holm keeps that family-wise chance at 5% and is never less powerful than Bonferroni |

A cost-per-solved interval can be unbounded (shown as ∞): if some resamples contain no solved task, the cost
per solved task is infinite in those resamples. That is the honest answer for an arm that rarely solves.

All bootstrap runs are seeded. Each statistic's seed is derived from the report seed and a stable label (the
arm or contrast name), so a number does not change when other arms are added to the log, and the same log
and seed always produce the same report.

### Holm, in one paragraph

Sort the m primary p-values from smallest to largest. Multiply the smallest by m, the next by m − 1, and so
on down to 1; then make the sequence non-decreasing, capped at 1. A contrast is significant if its adjusted
p is below 0.05. The family is fixed before data are seen. The default primary family follows the design:

1. shadow merge: `La` vs `La-shadow` (the same lane outputs, merged with and without contracts and gate)
2. `B` vs `L1`
3. `La` vs `L1`
4. `Lb` vs `La`
5. `Lc` vs `D`

Any other list can be supplied. If a primary contrast has no shared tasks, it stays in the family with
p = 1, so missing data can never shrink the family and loosen the correction. API-arm contrasts such as
`D` vs `A` are reported as secondary, with unadjusted p-values, unless they are put in the primary list.

## Power at n = 90

**One arm.** A solve rate near 50% on 90 tasks has a 95% interval of about ±10 points
(1.96 × √(0.25 / 90) ≈ 0.103). Example: 45/90 gives Wilson [39.9%, 60.1%].

**Two arms, paired.** The smallest difference we can reliably detect depends on the discordance rate, the
share of tasks where the two arms disagree. With 30% discordance (27 of 90 tasks), a 5% two-sided test with
80% power detects about (1.96 + 0.84) × √(0.30 / 90) ≈ **16 points**. With Holm at five contrasts the
strictest threshold is 0.05 / 5 = 0.01, which raises that to about 20 points for the first contrast to be
tested. More discordance means less power: differences smaller than this will often be reported as
"not significant", which means "not measured", not "no difference".

## Escalation value

When a cheap worker fails, the log is forked at that point and the task is re-run twice or more:

- **control**: the same cheap model again (role `control`, a fresh seed),
- **treatment**: a stronger model (role `treatment`).

For a treatment model T and its control model C, over every fork point that has both a scored control and a
scored treatment:

> **EV(T vs C) = solve rate of T − solve rate of the same-model control C**

The pairs are the fork points, so EV gets an exact McNemar p-value and a paired bootstrap interval just like
an arm contrast. When a fork point has several controls, the earliest one is the control for EV; when it
has several treatments of the same model, the earliest is used.

**Noise floor.** At fork points that also have a second control with a different seed, we count how often the
two controls disagree. That control-vs-control divergence rate (with a Wilson interval) is how much a
same-model retry moves the outcome by chance alone. An EV that is not clearly larger than the noise floor
is not evidence that escalation helps; it is what retrying would have given anyway.

**Cost.** Extra cost per fork is cost(treatment fork) − cost(control fork), averaged over fork points, with a
bootstrap interval. **Dollars per extra solve** is the total extra cost divided by the net extra solves
(treatment-only minus control-only). It is undefined when there are no net extra solves, and its interval
is unbounded above when some resamples have none.

## Arm cost and measurement overhead

Arm D forks the log at its first gate failure and runs three branches (two Haiku controls and one
Sonnet treatment), but in production it would run only the one it routes to. So:

- **Arm D's cost per task** = D's own usage (records without `fork_id`: orchestrator, first attempt)
  **+ the usage of the one fork branch it shipped**. The shipped branch is found from D's arm outcome:
  its `branch_task_id` is the `fork_task_id` of a `fork.created`, whose `fork_id` tags that branch's
  usage (`projections.shipped_forks`). Arm totals, cost per task, cost per solved task, contrasts and
  the frontier all use this cost. A task whose first gate passed ships no fork branch.
- **Measurement overhead** = every other fork's usage: the controls, and the treatment when D shipped
  a control. It exists only to measure escalation value, would not be spent by D in production, and
  is in no arm's cost. The report lists it in its own table by parent arm and fork role, with a total.
- **Escalation value** still prices every fork (treatment cost − control cost), shipped or not.
- **The budget guard** counts all recorded spend, arm cost and overhead alike: it caps what the
  experiment spends, not what an arm costs.

`contraprova cost --by arm` uses the same rule: shipped-branch usage is grouped under its arm and the
rest of the fork usage under `<arm>:measurement`. Other groupings (`role`, `model`, `task_id`) group
the raw records.

## Gate quality

For each gate (and each tier: `T0`, `T1`, ... taken from the gate name prefix or `evidence.tier`) the report
counts decisions, passes and blocks. If blocked submissions were replayed against the score tests, each
replayed block is either a **true block** (the patch fails the score tests: the gate was right) or a
**false block** (the patch would have passed: the gate cost a solve). Both rates get Wilson intervals over
the replayed blocks only.

## Cost-quality frontier

Each scored arm is a point (cost per task, solve rate). The frontier is the set of arms that no other arm
beats on both axes (`projections.pareto_frontier`). Costs come from recorded tokens times a price table, so
re-pricing (any table, including a scaled one) moves the costs and possibly the frontier, but never a solve
rate, McNemar p or EV.

## Data the analysis reads

Payload shapes are defined in [events.md](events.md); this table lists only what the analysis
enforces. Missing **required** fields raise an error, because silently dropping an outcome would bias the result. A
usage record whose model has no price in the table also raises. Missing **optional** tags are handled and
listed under "Warnings" in the report.

| Event | Required | Optional, and what happens without it |
|---|---|---|
| `outcome.scored` (arm outcome) | `arm`, `task_id` (pairing key), `solved` | A later score for the same (arm, task) replaces an earlier one. `log_task_id` maps the kernel task to the pairing key for cost attribution. `branch_task_id` names the fork branch the arm shipped, whose usage becomes arm cost |
| `outcome.scored` (fork outcome) | `fork_id`, `solved` | Never counts toward an arm's solve rate. An unknown `fork_id` is a warning |
| `outcome.scored` (gate replay) | `gate_seq` (seq of the `gate.decided` block replayed), `solved` (did the blocked patch pass the score tests) | `task_id` is informational. Pointing at a pass or a non-gate event is a warning |
| `usage.recorded` | `model` and token counts, as written by `usage.usage_payload` | `arm`: without it (and without `fork_id`) the cost is "untagged". `task_id`: without it the cost is in the arm total but not in per-task statistics; a kernel task id is mapped to its pairing key (via `task.created.exercise_id` or an outcome's `log_task_id`). `role`: only `"orchestrator"` counts toward the orchestrator share. `fork_id`: the cost belongs to that fork; it is also arm cost if the fork is the branch the arm shipped, otherwise measurement overhead (see above) |
| `fork.created` | `task_id`, `model`, `role` (`"control"` or `"treatment"`); events.md also requires `fork_id`, `from_seq`, `seed` | `fork_task_id` identifies the branch an arm outcome shipped (via its `branch_task_id`); `arm` names the parent arm of overhead (else the usage's `arm`). `prefix_hash` and `branch` are ignored. `from_seq`: forks are grouped into fork points by (`task_id`, `from_seq`). `seed`: two controls count toward the noise floor only if their seeds differ. `fork_id`: defaults to `fork@<seq>`, the seq of the `fork.created` event |
| `gate.decided` | `gate`, `verdict` | `evidence.tier` overrides the tier taken from the gate name. `evidence.replay_solved` (bool) is an inline alternative to a gate-replay outcome; a replay outcome event takes precedence |

Definitions used throughout: tokens of a request = prompt tokens (input + cache reads + cache writes) +
output tokens. Cache-read share = cache-read tokens / prompt tokens. Per-arm cost statistics use only usage
tied to a scored task of that arm; "Total recorded cost" includes every usage record tagged with the arm
(without `fork_id`) plus the fork branches the arm shipped.

## Worked example

Suppose arms D and A both ran the 90 evaluation tasks. D solved 45 and A solved 36.

- Solve rates: D 45/90 = 50.0% [39.9%, 60.1%]; A 36/90 = 40.0%. The intervals overlap, but that is not
  the right test for paired data.
- Pairing: D solved 18 tasks that A failed; A solved 9 that D failed; the other 63 tasks agree.
  The discordance is 27/90 = 30%.
- Exact McNemar: under "no difference" the 27 discordant tasks are coin flips, and a split of 18–9 or more
  extreme (either way) has probability **p = 0.122**.
- Paired bootstrap of the difference (`stats.paired_bootstrap_ci`, seed 0, 10 000 resamples): **+10.0 points [−1.1, +21.1]**.
- Conclusion: D looks 10 points better, but at 30% discordance the study can only reliably detect about 16
  points, and the interval includes zero. The report would say "not measured", not "D is better".

If this were one of five primary contrasts with raw p-values 0.004, 0.03, 0.07, 0.2 and 0.5, Holm would
adjust them to 0.02, 0.12, 0.21, 0.4 and 0.5: only the first stays below 0.05.

For escalation: at 30 fork points, Sonnet solved 18 and the Haiku control 6, with no fork point where only
the control solved. EV = +40.0 points [+23.3, +56.7], exact McNemar p = 0.0005. Two Haiku controls with
different seeds disagreed at 3 of 30 fork points, a noise floor of 10.0% [3.5%, 25.6%]: the EV clears it
comfortably. At $0.002 extra per fork, each extra solve cost 30 × $0.002 / 12 = $0.005.
