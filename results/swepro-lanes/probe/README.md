# Contract Lanes probe (2026-11-12): verdict GO

**Pilot measurements on 12 tasks. The probe is descriptive only: it decides whether the main run goes
ahead and is not part of the main analysis.**

Pre-registration: [../PREREG-probe.md](../PREREG-probe.md), committed (47fa0bc) before any paid request.
Arm La only, on split `lanes-probe` (12 tasks, ordered list sha256 `74574d74…`).

- **Run:** `python -m contraprova.harness.run --benchmark swepro --lanes --split lanes-probe --arms La
  --client anthropic --log runs/lanes-probe.jsonl --budget-usd 5 --validate-before-run --no-log-contents
  --delete-images --lanes-splits results/swepro-lanes/splits.json`.
- **Log:** `log.jsonl`. `contraprova verify` gives 343 events, chain intact, replay clean. The log holds
  0 `contents` fields and 47 `contents_sha256` fields (no repository file contents). The report is in
  `report.md` (`contraprova report --lanes`). Validation rows are in `log.validation.jsonl`, all 12 ok.
- Wall time was 55 min for the paid phase, after about an hour of $0 validation and context loading.
  The verifier ran 21 times. Every image was deleted, and the budget guard never stopped the run.

## Spend (recomputed from logged tokens, `anthropic-2026-11`)

| | USD |
|---|---|
| **Total (all arm La)** | **$1.4562** (cap $5.00) |
| claude-opus-5-5 | $1.0314 (planner $0.6581, 12 requests; Opus lane tier $0.3733, 2 requests incl. repair) |
| claude-sonnet-5-5 | $0.2366 (Sonnet lane tier, 2 requests incl. repair; no T2 review triggered) |
| claude-haiku-5-5 | $0.1881 (31 lane requests incl. edit repairs and Haiku-repair rounds) |

- **Per task:** $0.1213 [bootstrap 95% CI $0.062, $0.225]. The pre-flight expected $2.3233 ($0.194 per
  task), so the measurement is 0.63 times the pre-flight.
- **Project total** under the owner's approvals: about $25.89 before this probe, about $27.35 after.

## Go rule (docs/lanes-plan.md §7), verdict per rule

| # | Rule | Measured | Verdict |
|---|---|---|---|
| 1 | ≥ 10/12 tasks complete with no harness error | **12/12**; no exclusion, no stop, empty stderr | ✓ |
| 2 | planner picks k ≥ 2 on ≥ 8/12, falls back to k = 1 on ≤ 2 | **k = 2 on 11/12**; k = 1 on 1/12, and that was the planner's choice, not a fallback (0 plan failures, 0 plan repairs); never k = 3 or 4 | ✓ |
| 3a | La ≤ $0.35 per task | **$0.121** | ✓ |
| 3b | main-run estimate, re-run on the probe's rates, fits $25 at expected × 1.2 | free pre-flight on `lanes-main` (arms La, D) with the probe's first-merge block rate (p_block 0.33): **37 eligible tasks, expected $19.14** (La $6.71, D shipped $12.05, D measurement $0.38); × 1.2 = $22.96 ≤ $25 (`main-estimate-preflight.json`) | ✓ |
| 4 | La solves ≥ 6/12 | **10/12** = 83.3% [Wilson 55.2%, 95.3%] | ✓ |

**Verdict: GO.** All four rules hold. Whether to start the main run is the owner's decision; it was not
started.

The 3b estimate is conservative in two ways:

- it keeps the pre-flight's default D fork rate of 0.5, although the main study measured 9%;
- La's own pre-flight overestimated this probe by about 1.6 times.

It also shows that **3 of the 40 `lanes-main` tasks have a counted prompt over 90K tokens** and would be
excluded: openlibrary-e010b2a1 (102,278), openlibrary-5fb31263 (92,094) and openlibrary-09865f5f
(91,040). The main pre-registration has to state this; n = 37.

## Reported, not decisive

- **Chain of derived points** (all 12 tasks): La-shadow 5/12 → La-first 7/12 → La-sonnet 10/12 →
  La 10/12.
  - La vs La-shadow: 5 vs 0 discordant, McNemar p = 0.063, +41.7 points [+16.7, +66.7].
  - The governed merge alone (La-first vs shadow) is +2 tasks.
  - All 3 tasks gained after the first merge were gained in the **Haiku-repair** round, not by Sonnet.
    "La-sonnet" is the state after the round in which Sonnet would act, so it includes the Haiku-repair
    round.
  - Sonnet and Opus were reached on one task only (ansible-ecea15c5), which ended unsolved.
- **Merge rounds** (rounds: tasks): 1: 8, 2: 3, 4: 1. Ladder step reached per lane: Haiku 19,
  Haiku repair 3, Opus 1.
- **Blame rule** that fired on the 7 blocked merges: **rule 1 (assigned visible tests) 6 times, rule 3
  (all lanes) once, rule 2 (traceback paths) never.** Rule 2 was exercised only in the dry runs
  (docs/lanes-plan.md §9). On real failing tests it has 0 observations, so it is still untested on
  real output.
- **Clashes** (deterministic detectors, governed merge):
  - duplicated_work 1;
  - interface_mismatch 1;
  - unattributable_failure 1;
  - wasted_lane 2;
  - scope_violation 0.

  Each was on a different task.
- **Gate quality:** 7 blocked merges were replayed on the score tests and all 7 really failed (true-block
  7/7; false-block rate 0% [0%, 35%]).
- **First-reply edit failures in lanes:** 23 first replies; 1 truncated, 2 parse errors, 0 apply errors
  (3 tasks).
- **La vs earlier results on the same tasks** (different runs, different settings; not a paired
  contrast):
  - 6 main-study tasks: La 5/6; A 6/6; D 6/6. La's miss, **ansible-ecea15c5**, was solved by both A and
    D in the main study.
  - 6 probe tasks: La solved element-web-ad26925b (probe 1: Haiku failed, Opus solved), ansible-d72025be
    (probe 2 and 3: Haiku failed every time) and openlibrary-630221ab. Like most earlier runs, it failed
    ansible-b748edea.

## Things that looked wrong or worth a look

1. **ansible-ecea15c5: possible repeated mis-blame.**
   - Rule 1 blamed lane L1 at merge rounds 0, 1 and 2, because the failing visible test was assigned to
     L1.
   - L1 was escalated Haiku → Haiku repair → Sonnet → Opus and still failed; round 3 fell back to rule
     3, and the task ended unsolved.
   - A and D solved this task in the main study.
   - If the fault was in the other lane, rule 1 would keep escalating the wrong lane. On this task it
     cost about $0.60 of Sonnet and Opus lane requests for no solve.
   - Worth checking before the main run whether rule 1 should give way to rule 2 when a lane has
     already been escalated once without effect.
2. **The planner never chose k ≥ 3** (k = 2 on 11 tasks, k = 1 once), although these are ≥3-file tasks.
   That is fine for rule 2, but it means the 3- and 4-lane paths are untested on real plans.
3. **Blame rule 2 has no real observation** (see above).
4. **The planner is 45% of La's dollars** ($0.66 of $1.46) and the lanes only about 13%. La's cost
   is dominated by the Opus plan request, as D's is by its Opus brief.
5. **Report artefact:** `contraprova report --lanes` lists "La vs D" with n = 0 (D was not run in the
   probe) and puts the $0-cost derived points on the cost frontier. Both are expected for a La-only
   log, not errors.

## Deviations from the pre-registration

- None in the run. The `--lanes-splits` path was stated in the PREREG.
- After the run I made one extra $0 pre-flight on `lanes-main` (free count_tokens only) to evaluate rule
  3b. The PREREG names that check but does not give its command. It used `--context-source raw` and
  `--p-block 0.33` (the probe's measured first-merge block rate: 4 of 12 tasks were blocked at the first
  merge).
