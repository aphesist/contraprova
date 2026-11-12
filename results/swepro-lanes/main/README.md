# Contract Lanes main run (2026-11-12): results

Pre-registration: [../PREREG.md](../PREREG.md), committed (1ed672e) before any paid request. Arms La
and D on 37 fresh `lanes-main` tasks. 40 were listed; 3 with counted prompts over 90K were dropped as
pre-registered and not refilled. Every task completed in both arms. There was no harness error, no
validation failure and no budget stop.

- **Run:** `python -m contraprova.harness.run --benchmark swepro --lanes --split lanes-main --arms La,D
  --client anthropic --log runs/lanes-main.jsonl --budget-usd 25 --validate-before-run --no-log-contents
  --delete-images --lanes-splits results/swepro-lanes/splits.json`.
- **Files here:**
  - `log.jsonl`: `contraprova verify` gives 1370 events, chain intact, replay clean; 0 `contents`
    fields and 173 `contents_sha256` fields, so no repository file contents;
  - `report.md` (`contraprova report --lanes`);
  - `analysis.json` (paired contrasts recomputed from the log);
  - `summary.json`, `preflight.json`, `log.validation.jsonl`.
- The paid phase took 2 h 11 min, after about 1 h 45 min of $0 validation. The verifier ran 98 times.
  Every task image was deleted afterwards, and dockerd was stopped.
- All harness files still match the PREREG hashes.

## Primary result (pre-registered)

**La vs La-shadow, exact McNemar, Holm m = 1, alpha 0.05:**

- La solved **36/37** and La-shadow **28/37**.
- Discordant pairs: La-only **8**, shadow-only **0**.
- **p = 0.0078** (Holm p = 0.0078 < 0.05), so the null is **rejected**.
- Paired difference: **+21.6 points [95% bootstrap CI +8.1, +35.1]**.

Contract Lanes with the governed merge and escalation solves more than the same lanes' first replies
joined without contracts, gates or escalation.

## Secondary results (descriptive, unadjusted)

| | Estimate [95% CI] | n |
|---|---|---|
| Solve rates (Wilson) | La 36/37 = 97.3% [86.2, 99.5]; D 34/37 = 91.9% [78.7, 97.2]; derived B 32/37 = 86.5% [72.0, 94.1] | 37 |
| **La vs D, solve rate** | +5.4 points [+0.0, +13.5]; discordant La-only 2 / D-only 0; McNemar p = 0.50 | 37 |
| **La vs D, cost per solved task** (La/D ratio) | **1.07 [0.62, 1.76]**: no measurable cost difference | 37 |
| Chain: shadow → first | 28 → 32: +10.8 points [−2.7, +24.3]; 6 vs 2 discordant, p = 0.29 | 37 |
| Chain: first → Sonnet round | 32 → 36: +10.8 points [+2.7, +21.6]; 4 vs 0, p = 0.13 | 37 |
| Chain: Sonnet round → final | 36 → 36: no change | 37 |
| La vs derived B | +10.8 points [+2.7, +21.6]; 4 vs 0, p = 0.13 | 37 |
| D vs derived B | +5.4 points [+0.0, +13.5]; 2 vs 0, p = 0.50 | 37 |
| D's escalation value (Opus treatment vs Haiku control at fork points) | treatment 2/4, control 2/4, EV +0 points; extra cost per fork $0.33 [0.15, 0.55] | 4 forks |

### Cost (recomputed from logged tokens, `anthropic-2026-11`)

| | Total | Per task [95% CI] | Per solved task [95% CI] |
|---|---|---|---|
| La | $3.7439 | $0.1012 [0.063, 0.153] | $0.1040 [0.063, 0.167] |
| D (incl. its shipped Opus branches, $1.3370) | $3.2964 | $0.0891 [0.056, 0.137] | $0.0970 [0.058, 0.154] |
| D fork controls (measurement, in no arm's cost) | $0.0345 | | |
| **Total recorded** | **$7.0749** (cap $25) | | |

- **By model:** Opus $5.2087, Sonnet $1.0598, Haiku $0.8063.
- The pre-flight expected $19.97, so the measurement is 0.35 times the pre-flight. The pre-flight
  assumed a first-merge block rate of 0.45 and a D fork rate of 0.5; this run measured 5/37 and 4/37.
- **Project total** under the owner's approvals: about $27.35 before this run, **about $34.42** after.

### Contract Lanes internals

- **k chosen:** k = 2 on 29 tasks and k = 1 on 8 (the planner's own choice: 0 plan failures, 0 plan
  repairs). **k ≥ 3 still never appeared** on these ≥3-file tasks.
- **Merge rounds** (rounds: tasks): 1: 32, 2: 2, 3: 2, 4: 1. Ladder step reached per lane: Haiku 58,
  Haiku repair 2, Sonnet 4, Opus 2.
- **Blame on the 10 blocked governed merges:**
  - rule 1: 3 (basis `tests:assigned` 2, `edits` 1);
  - rule 3: 7 (`tests:unattributed` 4, `T2.review:unattributed` 2, **`tests:widened` 1**);
  - **rule 2: 0.** Rule 2 has still never fired on real output; widening fired once.
- **Planner share:** $1.8793 of La's $3.7439 (**50.2%** of La's dollars, 8.0% of its tokens). D's Opus
  brief is 47.9% of D's dollars.
- **Clashes** (governed merge, deterministic detectors):
  - interface_change_request 2 (1 task);
  - unattributable_failure 7 (3 tasks);
  - wasted_lane 3 (3 tasks);
  - duplicated_work, interface_mismatch and scope_violation 0.
- **First-reply edit failures:** La lanes 10 of 66 requests (7 parse, 3 apply; 8 tasks); D 4 of 37
  (1 parse, 3 apply).
- **Gate quality** (blocked patches replayed on the score tests):
  - the test gates' blocks were all true (7/7);
  - **T2.review blocked La twice on one task (openlibrary-4b7ea297); both blocked merges would have
    passed the score tests.** The Sonnet reviewer objected to a `requirements.txt` change (dropping
    `wikitextparser` and moving it to a new file). That is a reasonable code-review concern, but the
    tests did not need it;
  - one `T0.edits` block was also a false block by replay.

### Per-task differences (all other tasks were solved by La, D and B alike)

| Task | La | D | derived B | La-shadow | Note |
|---|---|---|---|---|---|
| NodeBB-04998908 | ✓ | ✗ | ✗ | ✓ | La's first merge passed |
| openlibrary-7cbfb812 | ✓ | ✗ | ✗ | ✗ | La solved it after escalation (Sonnet round) |
| ansible-949c503f | ✗ | ✗ | ✗ | ✗ | Every arm failed |

On the remaining 34 tasks La and D both solved. La-shadow failed 8 of those that La solved.

## Disclosures (as pre-registered) and deviations

- The probe reused 12 already-seen tasks; these 37 are fresh. k ≥ 3 did not occur in the probe or
  here. Blame rule 2 has not run on real output, here or in the probe. Blame widening fired once.
- **Deviation 1 ($0, no effect on results):** `--validate-before-run` validated all 40 listed tasks,
  including the 3 dropped for prompt size. All 40 passed. Dropped tasks skip the after-task image
  cleanup, so their 3 images (about 11 GB) stayed on disk; I removed them by hand while the paid phase
  was starting. No measured task used them.
- **Deviation 2:** none in arms, prompts, gates, scoring, pricing or the stop rule. The explicit
  `--lanes-splits` path was stated in the PREREG.
- **Caveats:**
  - La and D are both near the ceiling on these tasks (97% and 92%), so La vs D has little room to
    separate and the cost-ratio CI is wide.
  - D's escalation value rests on 4 forks.
  - The clash detectors are deterministic and not validated against human labels.
