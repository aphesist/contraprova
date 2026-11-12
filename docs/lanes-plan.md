# Contract Lanes on SWE-bench Pro: study plan

Status: **built and validated at $0; not yet pre-registered; no paid request made.** The owner
approved the study (probe cap $5, main cap $25). Arm La, the shadow and derived scorers, the clash
detectors, the selection rule, the lane pre-flight, the CLI and the report are implemented as in §7
(`harness/lanes.py`, `harness/lanes_detect.py`, `tests/test_lanes.py`; how to run it:
[swepro.md](swepro.md#contract-lanes-arm-la)). Section 9 lists where the build differs from this plan
and what the $0 validation found. The rest of this document is the plan as written. Every cost figure is computed from logged tokens of the main study
([results/swepro-main](../results/swepro-main/README.md), 300 requests) with contraprova's own pricing
code (`pricing.request_cost`, table `anthropic-2026-11`). Each assumption is labelled **measured** or
**assumed**. A PREREG.md that freezes the items in §6 must be committed before any paid request.

## 1. Question and hypotheses

**Question.** Split a large multi-file task into contract-scoped Haiku lanes:

1. Opus plans the lanes from the outline.
2. Interfaces are frozen on a shared board.
3. The lanes run in parallel. Each lane sees only its own files in full.
4. Their edits are merged under contract checks.
5. The visible-test gate checks the merged patch, and blamed lanes escalate one tier at a time.

Does this beat the single-worker cascade D on large multi-file tasks, at similar or lower cost?

| | Hypothesis | Contrast |
|---|---|---|
| **H1 (primary)** | A governed merge solves more tasks than an ungated **shadow merge** of the same first-pass lane outputs | La vs La-shadow, exact McNemar. La-shadow costs no tokens |
| H2 | La solves at least as many tasks as D | La − D, paired bootstrap CI (estimation) |
| H3 | La's cost per solved task is similar to or below D's | ratio La/D, paired bootstrap CI |
| H4 | Lanes have fewer truncation and edit-format failures than D's single Haiku worker | first-reply failure counts, La lanes vs D |
| H5 (descriptive) | Lane clashes, by type, are rare and mostly caught by the governed merge | clash counts per detector (§2.4) |

A is not run (§4). La vs A is read through D, using the main study's D-vs-A result.

**Prior expectation, stated in advance.** La will probably cost **more** than D, not less. The Opus
planner writes contracts and interfaces instead of an 874-token brief, and that makes up about half of
La's expected cost (§5). H3 is therefore the hypothesis most likely to fail. The case for lanes rests
on H1, H2 and H4.

## 2. Mechanism, mapped to the code

### 2.1 Pipeline (arm La)

1. **Plan.** One Opus 5.5 request. It sees the lead context D's brief already uses
   (`ProHarness.lead_context`: statement, requirements, interface, outline, visible test names; no
   file bodies; at most 16K estimated tokens). It returns a JSON lane plan:
   - `k` lanes, 1 ≤ k ≤ 4, chosen by the planner;
   - per lane: `lane_id`, `allowed` (files it owns: a partition of the task's solution files plus
     new files), `symbols` it may change, `exports` and `imports` (signatures), and the visible tests
     it is expected to make pass;
   - `interfaces`: every cross-lane symbol with its frozen signature, seeded from the task's
     `interface` field.

   The kernel validates the plan:
   - every solution file is owned by exactly one lane;
   - no lane owns a test path;
   - k ≤ 4;
   - each lane prompt fits in 90K counted tokens.

   An invalid plan gets one repair turn, as edits do. If it is still invalid, the task runs as a
   single lane (k = 1). That is logged as a plan failure, and the task stays in the analysis.
2. **Freeze.** The kernel writes the interfaces to the board (`interface.frozen`). They are now
   immutable for this task. There is no Haiku interface round, which keeps cost down. A lane that
   needs a change says so in a structured trailer (`interface.requested`). The kernel rejects the
   request and counts it as a clash. A live interface round on a shared blackboard (arm Lb) is out of
   scope.
3. **Implement.** One Haiku 5.5 request per lane (effort `medium`, `max_tokens` 32K, one edit-repair
   turn, as in D). Each lane prompt has:
   - the shared header (statement, requirements, interface);
   - the frozen interfaces and its own contract;
   - its own files in full;
   - the **outline only** of every other file;
   - its assigned visible test names.

   The lane answers with SEARCH/REPLACE blocks, applied with a scope checker built from the lane's
   `allowed`.
4. **Governed merge.** The per-lane file sets are unioned, which is disjoint by construction. Then the
   checks run:
   - `T0.scope` per lane: out-of-scope blocks are rejected before applying and counted;
   - `T0.interfaces` (new, deterministic): every frozen symbol still exists in the merged outline
     with its frozen signature, compared with `outline.py`;
   - D's gates on the merged patch: `T0.edits`, `T0.visible_tests`, `T1.private_tests`, and
     `T2.review` above 500 changed lines.
5. **Blame.** A gate block is attributed to lanes in this order:
   1. visible tests the plan assigned to a lane;
   2. file paths in the verifier's tracebacks that fall in a lane's scope (the kernel reads the
      output; workers never see it);
   3. failing that, every lane that changed code.

   A blamed lane sees only the same feedback D's retry gets: visible test names, or a count of
   gate-private failures.
6. **Stepwise escalation per lane** (owner's design). Each blamed lane moves up one tier:
   **Haiku 5.5 → Sonnet 5.5 → Opus 5.5**. Lanes that passed keep their output. After each step the
   lanes are re-merged and re-gated, and the task stops at the first pass. A lane that reaches Opus
   and is blamed again ends the task as unsolved. There are at most 2 escalation rounds per task.
   Nothing escalates to Fable 5.2: the router's action set for lanes is `{haiku, sonnet, opus}`,
   and a test asserts it. Each escalated request keeps the one edit-repair turn. Tier models:
   - Sonnet: effort `low`, `max_tokens` 32K;
   - Opus: effort `low`, `max_tokens` 32K.
7. **Shadow merge (La-shadow).** The first-pass lane outputs, before any escalation, are joined with
   **no contracts**:
   - out-of-scope blocks are applied if their SEARCH text matches the base file;
   - two lanes creating the same file: the later one in plan order wins;
   - no interface check, no gate, no escalation.

   The result is scored once on the official reward. It costs no tokens, only one verifier run.
   It approximates the ungoverned "plan, fan out, concatenate" join.
8. **Derived points (cost $0).** Scoring two more points splits H1 into its parts:
   - **La-first**: the governed merge of the first pass, contract-filtered, no escalation;
   - **La-sonnet**: the state after the Sonnet round.

   The chain shadow → first → Sonnet → final shows how much comes from contracts and how much from
   escalation. All are scored with one verifier run per distinct merged file set (memoised by hash,
   as `Verifier.run` already is).

### 2.2 What already exists

| Need | Existing code |
|---|---|
| Lane contracts, scope check, overlap check | `contracts.py`: `LaneContract`, `check_scope`, `overlapping_scopes` |
| Gates, one `gate.decided` per submission | `gates.py` (`run_gates`, `decision_payload`, `review_gate`); Pro gates in `swepro_arms.ProGates` |
| Edit parsing and application with a scope checker; repair turn | `harness/edits.py` (`parse_edits`, `apply_edits`, `scope_checker`); `ProHarness._work` |
| Outline for the planner; signatures for the interface check | `harness/outline.py` (`outline_file`, `render_outline`) |
| Official verifier, memoised; scorer | `swepro_arms.Verifier`, `ProScorer`, `harness/docker.py` |
| Attempt limits, done-is-final, hash chain, usage, cost | `projections.py`, `log.py`, `usage.py`, `pricing.py` |
| Re-execution from a logged point (if lane forks are wanted later) | `forks.py` |
| Arms `La` and `La-shadow` and the primary contrast La vs La-shadow | `analysis.py` (`ARM_ORDER`, `DEFAULT_PRIMARY`) |
| $0 empty/gold validation, pre-flight, budget guard, resume | `swepro_run.py`, `swepro_arms.main_preflight`, `arms.BudgetGuard` |

### 2.3 What must be built

- **Lane planner:** request prompt, JSON schema, parser and validator, with a k = 1 fallback.
- **Board:** `interface.frozen` and `interface.requested` events, and the frozen-signature check
  (`T0.interfaces`).
- **Lane runner:** `ProHarness.run_la`.
  - Each lane is a kernel task `La:<id>:<lane>` with `kind: "lane"`, `max_attempts: 3`. Its attempts
    are the tiers, so the existing attempt limit enforces the ladder.
  - Requests go out serially or in threads. Wall-clock time is not a claim of this study.
- **Merge:** `merge_governed` and `merge_shadow`, plus the blame rule.
- **Per-lane escalation:** `route.decided` with `decision: "lane_escalation"` and propensities
  `{next tier: 1.0}`; a fixed policy, not a learned router.
- **Shadow and derived scorers:** they write arm outcomes for `La-shadow`, `La-first` and `La-sonnet`
  with `derived_from: "La"`.
- **Clash detectors** (§2.4) and the lane metrics section of `contraprova report`.
- **CLI:** `--arms La`, the lane-plan pre-flight, and the selection rule (§3) as a tested function.

### 2.4 Clash detectors (deterministic, keyed to MAST)

| Clash | Detector | MAST mode |
|---|---|---|
| Scope violation | A lane block outside its `allowed` files (rejected in La, applied in the shadow merge) | 1.2 |
| Interface mismatch | `T0.interfaces` fails, or the test output shows ImportError, AttributeError, a TypeError on a call signature, "is not a function" or "Cannot find module" on a frozen symbol | 2.4, 1.1 |
| Interface change request | A lane posts `interface.requested` | 2.4 |
| Overwrite | Two lanes create the same new file, or (shadow only) a later block rewrites lines another lane added | 2.5 |
| Duplicated work | The same new top-level symbol is defined by two lanes (outline of the added code) | 1.3 |
| Unattributable failure | A gate block that blame step 3 has to assign to every lane | 3.2 |
| Wasted lane | A lane changes nothing, or all its blocks are rejected or dropped | n/a |
| Regression after escalation | An escalated lane breaks a visible test that passed in the previous round | 2.5 |

These detectors are code-level, so they need no judge model. A detector κ against human labels is out
of scope for this run. It is reported as not validated.

### 2.5 Proposed event payloads (for docs/events.md; not added yet)

Add these to `events.EVENT_TYPES` (the writer validates types):

| Event | Required | Optional |
|---|---|---|
| `lane.planned` | `task_id` (the La parent task), `plan_id`, `model`, `k`, `lanes` (list of {`lane_id`, `allowed`, `symbols`, `exports`, `imports`, `tests`, `new_files`}), `plan_sha256` | `parse_errors`, `repair_used`, `fallback` (`single_lane` when the plan was invalid), `rationale` |
| `interface.frozen` | `task_id`, `plan_id`, `interfaces` (symbol → signature), `interfaces_sha256` | `source` (`task_interface`/`planner`) |
| `interface.requested` | `task_id`, `plan_id`, `lane_id`, `symbol`, `requested_signature`, `status` (`rejected`) | `reason` |
| `lane.submitted` | `task_id` (lane kernel task), `plan_id`, `lane_id`, `tier` (`haiku`/`sonnet`/`opus`), `round`, `files_sha256` | `rejected_blocks` (out-of-scope), `changed_lines` |
| `merge.decided` | `task_id`, `plan_id`, `round`, `mode` (`governed`/`shadow`), `lanes` (lane_id → submission seq), `merged_sha256`, `verdict` (`pass`/`block`) | `blamed` (lane_ids), `blame_rule` (1/2/3), `gate_seq` (the merged `gate.decided`) |
| `coordination.detected` | `task_id`, `plan_id`, `kind` (a §2.4 row), `lanes` | `round`, `mode`, `evidence`, `mast` |

Lane kernel tasks use the existing `task.created` (`kind: "lane"`, `parent`, `plan_id`, `contract`),
`task.claimed`, `task.submitted` and `gate.decided` (the blamed lane's share of the merged decision).
The `lane.submitted` row above is an index event that links a lane submission to its plan and tier.
It can be dropped if those fields go onto `task.submitted` as optional fields instead. Usage carries
`arm: "La"`, `role` (`orchestrator` for the planner, `worker` for lanes), `task_id` (the lane's kernel
id) and an optional `lane_id`. Every La token is La's cost: no fork rule applies, because nothing is
measurement overhead. Shadow and derived outcomes are `outcome.scored` arm outcomes with
`derived_from: "La"` and `merge: "shadow" | "first" | "sonnet"`.

## 3. Task selection

**Rule.** Use SWE-bench Pro Python/JS tasks that no probe and no main-study run has used:

- not in `probe`, not in `probe2`, not in `results/swepro-main/eligible-90.txt`;
- the gold patch must be expressible in the edit format;
- tasks come in the partition's seeded, repository-interleaved `main` order (partition sha256
  `b409bcdd…`, seed 20261111).

The counts below are computed offline from the cached dataset rows (`gold_size`, non-test files and
lines). Prompt size is estimated as (gold-file chars + statement chars) / 3 + 1,212, the median
overhead measured against the main log's `worker_prompt_tokens_est`. That estimate is then scaled by
1.075, the median counted/estimated ratio in the main pre-flight (maximum 1.35).

| Tier | Filter | Fresh tasks |
|---|---|---|
| 1 | ≥3 gold files, ≥150 changed lines, counted worker prompt ≤90K | **30** (33 before the 1.075 scaling) |
| 2 | ≥3 files, 100–149 lines, ≤90K | 15 |
| 3 | ≥3 files, ≥100 lines, 90K–180K (D's Haiku crosses the 100K long-context price step; lanes stay below it) | 22 |

Of the 60 tasks in scope that meet ≥3 files and ≥150 lines, 11 have prompts over 90K, 7 were in the
main 90, 6 were probe tasks, and 3 delete or rename a file.

- **The strict pool caps n at about 30.** n = 40 needs tier 2 as well.
- n = 60 needs 15 tier-3 tasks. That changes D's pricing regime and makes the lane-vs-single prompt
  contrast unequal. **Recommendation: n = 40** (all of tier 1, then tier 2 in seeded order).
- Tier 1 alone (n ≈ 30) is the fallback if the budget is tighter.
- Exact eligibility uses free `count_tokens` at pre-flight, as in the main study.
- The 40-task list has a mean of 6.4 gold files, 343 changed lines and 43K tokens of gold-file source.
- Repository mix: openlibrary 16, ansible 8, qutebrowser 6, element-web 5, NodeBB 5.

**Probe tasks.** Twelve already-used large tasks (≥3 files, ≥150 lines, all ≤90K; openlibrary-8a9d9d32
is excluded because it failed validation):

- 6 from the main 90, which already have paired A and D results;
- 6 from probe 1 and probe 2, which have A and B results.

Using them keeps the fresh pool untouched, as probe 3 did. All 6 of the main-90 ones were solved by
both A and D, a reminder of the ceiling risk.

## 4. Arms

| Arm | What | Run? |
|---|---|---|
| **La** | The pipeline in §2.1. Planner: Opus 5.5 effort `low` (fixed, pre-registered). Lanes: Haiku 5.5 `medium`. Ladder Haiku → Sonnet 5.5 `low` → Opus 5.5 `low` | Yes |
| **La-shadow** | Ungated merge of La's first-pass lane outputs | Derived, $0 (verifier only) |
| La-first, La-sonnet | Decomposition points (§2.1, step 8) | Derived, $0 |
| **D** | The main study's frozen cascade, unchanged (Opus brief, Haiku worker, gates, fork to an Opus treatment plus a Haiku control as measurement) | **Yes, rerun**: fresh tasks have no D result, and pairing needs one |
| B | D's first Haiku attempt, as in the main study | Derived, $0 |
| **A** | Single Opus 5.5 `low` | **Drop** (recommended) |

**Why drop A.** On the main study A solved 96.6% and cost $0.13 per task. On fresh large tasks it is
modelled at $0.26 per task, about $10.4 at n = 40, which is 42% of an La+D+A budget. The question here
is La vs D. A's relation to D is already measured (non-inferior, 61% cheaper per solve). Running A only
adds power to a contrast nobody asked about. A ceiling check costs nothing: all 12 probe tasks already
have logged A results.

**Why the planner is Opus `low`.** It matches the main study's brief and the D brief that is rerun
here, so La vs D does not change the planner's model. It is also the cheapest Opus setting. Opus
`medium`/`high` or Fable 5.2 as planner is a later study (cost sensitivity in §5). Changing the planner
mid-study would break pairing.

**Confound to disclose.** La escalates Haiku → Sonnet → Opus per lane, while D jumps from Haiku
straight to Opus. La vs D is therefore a contrast between two whole designs. H1 is not affected,
because the shadow merge and La share the same first pass.

## 5. Cost estimate

### 5.1 Measured inputs (main study log, 88 tasks, 300 requests)

| Request | n | Prompt tokens mean / median / p90 | Output tokens mean / median / p90 | Mean $ |
|---|---|---|---|---|
| D brief (Opus low, outline) | 88 | 3,710 / 3,558 / 6,449 | 874 / 860 / 1,075 | $0.0323 |
| D brief, the 6 large tasks | 6 | 5,196 / 4,997 / 9,886 | 892 | — |
| D Haiku worker, first reply | 88 | 22,426 / 16,884 / 48,717 | 3,855 / 2,825 / 7,606 (max 22,746) | $0.0042 |
| D Haiku edit repair | 7 | 18,785 | 2,041 | $0.0029 |
| D fork: Opus treatment | 8 | 19,766 | 3,206 | $0.1432 |
| D fork: Haiku control (measurement) | 8 | 19,766 | 7,294 | $0.0056 |
| A Opus worker, first reply | 88 | 21,586 / 16,012 / 47,637 | 1,609 / 1,330 / 3,016 | $0.1185 |
| A Opus edit repair | 9 | 37,113 | 261 | $0.1537 |

- **Rates.**
  - Haiku edit-repair rate: 7/88 = 8% (2/6 on the large tasks).
  - A repair rate: 9/88.
  - D fork rate: 8/88 = 9.1%, and 2/6 on the large tasks.
  - No request read from or wrote to the cache. Cache prices therefore do not enter this estimate:
    lanes share no byte-identical prefix long enough to cache across models.
- **Output fits** (least squares on first replies against gold changed lines):
  - Haiku output = 2,697 + 12.41 × lines;
  - Opus output = 1,137 + 5.05 × lines.
  - The intercept is Haiku's per-request thinking overhead. Lanes pay it k times.
- **Model check.** On the 6 large main-90 tasks the model gives D $0.129 and A $0.200 per task in the
  expected scenario. The measured values are D $0.145 and A $0.206.
- **Prices** (`anthropic-2026-11`, per MTok, input/output):
  - Haiku 5.5: $0.10/$0.50, or $0.50/$2.50 above 100K;
  - Sonnet 5.5: $2/$10;
  - Opus 5.5: $4/$20;
  - Fable 5.2: $8/$40.

### 5.2 Model and assumptions

Per La task:

- planner = Opus(lead_in + plan_extra, plan_out);
- lanes = k × Haiku(src/k + F, (2,697 + 12.41·L/k) · m);
- edit repair = k × r × Haiku(lane_in + lane_out, 2,041);
- Sonnet tier = p_block × b × Sonnet(lane_in + 3K, (1,137 + 5.05·L/k) · s) × 1.1;
- Opus tier = p_block × b × q × Opus(lane_in + 4K, 1,137 + 5.05·L/k) × 1.1.

Here src is the task's own counted gold-file tokens and L its gold changed lines (both **measured**
per task). The factor 1.1 is a repair allowance.

D per task = brief + Haiku worker + repair + f × (Opus treatment × 1.1 + Haiku control). A per task =
Opus worker + repair. All terms are priced with `request_cost`.

| Parameter | Low | Expected | High | Basis |
|---|---|---|---|---|
| k, lanes | 2 | 3 | 4 | assumed (cap 4; never more than gold files) |
| lead_in, planner prompt | 5,000 | 7,000 | 12,000 | measured anchor: large-task brief prompt mean 5,196, p90 9,886; fresh tasks are bigger |
| plan_extra, schema and instructions | 1,000 | 1,500 | 2,500 | assumed |
| plan_out, contracts and interfaces | 2,500 | 4,000 | 6,000 | assumed (brief measured at 874; 3 to 7 times that) |
| F, per-lane header, board, contract and other files' outline | 4,000 | 6,000 | 9,000 | assumed (statement measured ≈1.6K, prompt overhead ≈1.2K) |
| m, multiplier on the Haiku output fit | 0.8 | 1.0 | 1.5 | fit measured, multiplier assumed |
| r, edit-repair rate per lane | 5% | 8% | 20% | measured 8%; 2/6 on large tasks |
| p_block, first merged patch blocked | 30% | 45% | 65% | assumed (D measured 9.1% overall and 33% on large tasks; lanes add coordination failures) |
| b, lanes blamed per block | 1.0 | 1.3 | 2.0 | assumed |
| s, Sonnet output relative to the Opus-low fit | 1.2 | 1.5 | 2.0 | assumed (Sonnet has never run as a worker in this harness) |
| q, share of Sonnet-escalated lanes that reach Opus | 30% | 45% | 60% | assumed (D measured: the Opus treatment solved 6/8 after a block; a middle tier rescues fewer) |
| f, D fork rate on these tasks | 20% | 35% | 55% | measured 9.1% overall and 2/6 on large tasks; scaled up |
| A repair rate | 5% | 10% | 20% | measured 9/88 |

### 5.3 Worked arithmetic (expected scenario, mean task of the n = 40 list: src 43,000 tokens, L 343 lines, k 3)

| Component | Arithmetic | $ per task |
|---|---|---|
| Planner (Opus) | 8,500 × $4/M + 4,000 × $20/M = 0.034 + 0.080 | **0.114** |
| 3 Haiku lanes | input 43,000 + 3 × 6,000 = 61,000 × $0.10/M = 0.0061; output 3 × (2,697 + 12.41 × 114) = 12,335 × $0.50/M = 0.0062 | 0.012 |
| Edit repairs | 3 × 0.08 × Haiku(≈25K in, 2,041 out) | 0.001 |
| Sonnet tier | 0.45 × 1.3 = 0.585 lanes × [(20,333 + 3,000) × $2/M + 2,573 × $10/M] × 1.1 | 0.047 |
| Opus tier | 0.585 × 0.45 = 0.263 lanes × [(20,333 + 4,000) × $4/M + 1,715 × $20/M] × 1.1 | 0.038 |
| **La** | | **0.212** |
| D: brief + worker + repair + treatment (+ control) | 0.0455 + 0.0087 + 0.0975 (+ 0.0029 measurement) | **0.155** |
| A | Opus(≈46K in, 2,870 out) + repair | 0.261 |

(The table shows the arithmetic on the mean task. The totals below average the model over each task.)

### 5.4 Per task and totals

| Scenario | La / task | D / task (incl. measurement) | A / task | **La + D, n = 40** | La + D + A, n = 40 | La + D, n = 60 | La + D + A, n = 60 |
|---|---|---|---|---|---|---|---|
| Low | $0.126 | $0.102 | $0.251 | **$9.12** | $19.17 | $16.83 | $37.28 |
| Expected | $0.212 | $0.155 | $0.261 | **$14.65** | $25.10 | $26.55 | $47.87 |
| High | $0.421 | $0.236 | $0.281 | **$26.25** | $37.50 | $46.52 | $69.56 |

- La alone at n = 40 costs $5.03 / $8.47 / $16.83 (low / expected / high).
- n = 60 includes 15 tier-3 tasks: per task, La is $0.139 / $0.231 / $0.457 and D is $0.141 / $0.211
  / $0.319.
- Tier 1 only (n = 30): La + D is about $11.1 expected and $19.9 high.
- **Probe** (12 tasks, La only): $1.40 / $2.38 / $4.76. With a D rerun on the 6 probe-1/probe-2 tasks
  as well, add about $1.

**Where La's money goes (expected).** Planner 54%, Sonnet tier 22%, Opus tier 18%, Haiku lanes 6%. The
lanes are almost free. **The planner's output and the escalation rate decide whether La is cheaper than
D.** D's expected $0.155 is mostly its Opus treatment.

**Worst case per La task.** Every request hits `max_tokens`: an 8K plan, 4 lanes × 32K each with
repair, then all 4 lanes go to Sonnet (16K) and to Opus (32K, with repair). That comes to $6.88. The
budget guard checks each request's worst case against the remaining cap, as now.

**Planner sensitivity (planner $ per task, expected scenario):**

| Planner | Low | Expected | High |
|---|---|---|---|
| Opus 5.5 low (pre-registered) | $0.074 | $0.114 | $0.178 |
| Opus 5.5 medium (output × 1.75, assumed) | $0.112 | $0.174 | $0.268 |
| Fable 5.2, same tokens | $0.148 | $0.228 | $0.356 |
| Fable 5.2, output × 1.75 | $0.223 | $0.348 | $0.536 |

With Opus medium as planner, expected La rises to about $0.27 per task (+$2.4 at n = 40). With Fable it
rises to about $0.33 to $0.45 per task.

### 5.5 Recommended caps

- **Probe:** hard cap **$5** (`--budget-usd 5`).
- **Main (La + D, n = 40):** hard cap **$25**, with a pre-flight requirement of expected × 1.2 ≤ cap,
  re-estimated from the probe's measured tokens. At the expected $14.65 the run completes with margin.
  In the high scenario the cap stops it at about n = 38 in fixed order, which is acceptable because the
  stop depends only on spend.
- With A added: cap $40.

**Budget note.** The project has spent $25.89 of the original ~$45 API envelope. Probe plus main, up to
about $30, needs the owner's approval. Without it, tier 1 only (n ≈ 30, cap $18) fits.

## 6. Statistics

- **Primary (H1):** La vs La-shadow on the same tasks.
  - Exact McNemar, two-sided, α = 0.05.
  - The family is exactly {La vs La-shadow}, passed explicitly to `analyze` (not the local-track
    default family). Holm with m = 1 is the raw p.
  - Effect size: paired bootstrap CI (10,000 resamples, seed 0).
- **Secondary** (no multiplicity claims):
  - La − D solve rate, paired bootstrap CI;
  - La/D cost per solved task, bootstrap ratio CI;
  - the decomposition shadow → first → Sonnet → final;
  - first-reply truncation and edit failures, La lanes vs D (per request and per task);
  - clash counts by type, governed vs shadow;
  - k chosen, tier reached per lane, planner fallback rate;
  - B (derived) vs La.
- **Power for H1.** Exact McNemar at α = 0.05, by multinomial enumeration. Each row gives P(governed
  solves, shadow fails) and P(shadow solves, governed fails):

  | Governed-only / shadow-only | n = 30 | n = 40 | n = 60 |
  |---|---|---|---|
  | 10% / 3% | 4% | 8% | 19% |
  | 15% / 3% | 15% | 28% | 50% |
  | 20% / 3% | 34% | **52%** | 77% |
  | 25% / 5% | 40% | 57% | 80% |

  The expected rescue rate is about 20%: 45% of tasks blocked, and roughly half of them rescued by
  escalation, against D's measured 6/8. **At n = 40 the primary test has about even odds.** It
  detects only a large governance effect. A non-significant result means "not measured".
- **La vs D.** At 30% discordance, n = 40 gives a minimum detectable difference of about 24 points
  ((1.96 + 0.84)·√(0.30/40)). This contrast is estimation only. No non-inferiority claim is
  pre-registered, because its CI would be about ±15 points.
- **Stop rules.**
  - Tasks run in the fixed seeded order. For each task, D runs first, then La, its derived scores and
    the shadow merge, so a stop leaves complete pairs.
  - The only stop is the budget guard refusing a request at the hard cap.
  - There is no interim look and no outcome-based stop.
  - A task that fails $0 validation is skipped and not replaced.
- **Pre-register:**
  - the task list and its sha256;
  - the partition hash;
  - the planner, lane and escalation prompts with their hashes, and the plan JSON schema;
  - ArmConfig: models, efforts, `max_tokens`, the k cap, the ladder, at most 2 escalation rounds;
  - the blame rule, the merge and shadow rules, and the detector definitions;
  - the price table hash and the hashes of the harness files;
  - the analysis script and the hypotheses with expected directions (including the expectation in §1
    that H3 probably fails).
- **$0 validation per task** (before its arms run):
  - the empty patch scores 0 and the gold patch scores 1, as in the main study;
  - **gold-lane check:** split the gold patch by file into a k = 2 or 3 fixture plan, then push each
    lane's gold blocks through the lane applier, `merge_governed`, `T0.interfaces` and
    `merge_shadow`. Both merges must score 1, and the interface check must pass. This validates the
    merge code on real tasks without a model.
- **Pre-flight:**
  - free `count_tokens` on the planner prompt and on each lane prompt under a naive by-file split
    (k = 3);
  - exclusion if any lane prompt or D's worker prompt exceeds 90K;
  - expected cost from §5 using the probe's measured rates;
  - refusal if expected × 1.2 exceeds the cap.

## 7. Build plan

| Step | Files | Effort | Tests |
|---|---|---|---|
| 1. Event types and payload doc | `events.py` (EVENT_TYPES), `docs/events.md` | 0.5 h | Writer accepts and validates the new types; replay unchanged |
| 2. Lane plan: prompt, schema, parser, validator, k = 1 fallback | new `harness/lanes.py`, prompts in `swepro_arms.py` | 4 h | Valid and invalid plans (missing file, overlap, test path, k > 4), repair turn, fallback; the planner prompt contains no file body (marker test, as for outlines) |
| 3. Lane prompts and runner (`run_la`), lane kernel tasks, attempt = tier | `swepro_arms.py`, `lanes.py` | 6 h | A lane prompt holds its own files in full and no other file's body; the ladder is Haiku → Sonnet → Opus and never Fable; at most 2 rounds; the attempt limit holds |
| 4. Governed and shadow merge, `T0.interfaces`, blame | `lanes.py` | 5 h | Out-of-scope block rejected vs applied; duplicate new file; signature change detected; blame rules 1–3 on fixtures; no private test name reaches a lane |
| 5. Clash detectors, `coordination.detected` | new `lanes_detect.py` | 3 h | One fixture per detector |
| 6. Derived scorers (La-shadow, La-first, La-sonnet), memoised verifier | `swepro_arms.py` | 2 h | One verifier run per distinct file set; derived outcomes never count as La |
| 7. Analysis and report: lane metrics, explicit primary family | `analysis.py` | 3 h | Report on a synthetic La log; the cost of La includes planner and escalations |
| 8. CLI, selection function, pre-flight, fakes (gold-based plan and lane replies) | `swepro_run.py`, `swepro.py`, `ProFakeClient` | 4 h | Selection reproduces the counts in §3 from fixtures; a dry run of La end to end with gold lanes scores 1 for La and La-shadow |
| 9. $0 validation on the 12 probe tasks (gold-lane check) | — | 1 h of runs | All pass |

About 3 to 4 working days and 1,200 to 1,600 lines including tests. The whole suite must stay green.

**Probe first** (12 used large tasks, §3; La only, plus the shadow and derived scores; cap $5).
**GO to the main run only if all of these hold:**

1. ≥10 of 12 tasks complete with no harness error.
2. The planner chooses k ≥ 2 on ≥8 of 12 tasks, and falls back to k = 1 on ≤2. Otherwise lanes
   collapse into a single worker.
3. Measured La cost is ≤ $0.35 per task (1.6 times expected), and the main estimate re-run on the
   probe's rates fits the cap at expected × 1.2.
4. La solves ≥ 6 of 12.

Reported but not decisive:

- the La vs La-shadow discordance;
- clash counts;
- La vs the existing D and A results on the 6 main-90 tasks.

On NO-GO, write the probe up and stop. Prompts may change between the probe and the main
pre-registration, never after it.

## 8. Risks and honest limits

- **Small pool, low power.** Only about 30 fresh tasks meet the strict filter, and n = 40 already
  relaxes it. H1 has about 50% power at n = 40.
- **Ceiling.** A and D both solved all 6 large tasks in the main study. If D is near 90% on fresh
  large tasks, La has little room on H2. H1 still has room, because the shadow merge may fail where
  La is rescued.
- **La is probably not cheaper than D.** The planner alone is modelled at $0.114 per task against D's
  whole $0.155. A cheaper planner (shorter schema output, or a Sonnet planner) would be a different
  study.
- **The shadow contrast is not a pure measure of contracts.** It bundles the scope filter, the
  interface check, the gate and the escalation tokens. The derived points split it apart, but La
  spends tokens that La-shadow does not. H1 says what governance buys, not that it is free.
- **Lanes see other files only as outlines.** Cross-file logic split badly by an outline-only planner
  may fail in ways a single worker with every file would not. That is part of what is measured.
- **Blame errors.** A wrong attribution escalates the wrong lane and wastes money. Rule 3 (blame all)
  bounds the error but raises the cost.
- **Design confound.** La's ladder includes Sonnet and D's does not. Sonnet has never run as a worker
  in this harness, so its output length, edit-format behaviour and cost are assumptions.
- **Detectors are unvalidated heuristics.** There is no human κ in this run.
- **Generalisation.** Single-shot, oracle-files setting, one benchmark subset, one vendor. The probe
  reuses tasks that were already seen. Logs follow `--no-log-contents` licence hygiene, as before.

## 9. As built (2026-11-12) and the $0 validation

No API request was made. Everything below ran with fake clients and, for the gold-lane check, the
official verifier in Docker.

### Where the build differs from this plan

| Item | Plan | Built | Why |
|---|---|---|---|
| Sonnet tier | effort `low` | effort `medium`, `max_tokens` 32K | owner's design |
| Ladder | Haiku → Sonnet → Opus, at most 2 escalation rounds | per-lane ladder Haiku → **Haiku repair** (same tier, gate feedback) → Sonnet → Opus; at most 3 merge rounds after the first; only blamed lanes move | owner's design: one repair attempt before escalating |
| Blame rule 3 | every lane that changed code | every lane | owner's design ("otherwise all lanes") |
| Blame widening (after the probe) | — | when a test gate blocks again and rule 1 would blame exactly the lanes it blamed the round before, rule 2 adds any lane a failing test's stem or a traceback names, keeping those lanes; if it adds none, every lane is blamed (rule 3), basis `tests:widened` | in the probe, ansible-ecea15c5 had rule 1 blame L1 for its assigned test in three rounds while L1 climbed to Opus, and the task ended unsolved; a lane whose repair does not help may not be the cause |
| Interface round | `interface.frozen` | one kernel step: the planner's interfaces plus every function/class/method of the task's `interface` field, with each lane's provides/needs on the board; a plan-time detector flags uses no lane provides | single cheap step, no model call |
| `lane.submitted` | new event | dropped; `plan_id`, `lane_id`, `tier`, `round`, `rejected_blocks` are optional `task.submitted` fields | the plan allowed this; one fewer type |
| `T0.interfaces` | frozen symbol still present with its signature | checked symbols = those the base defines, those a lane provides, and task-interface functions/classes/methods; present = defined (Python `ast`; JS/TS outline, else a word in a task file); for planner-frozen Python signatures the frozen parameter names must be accepted by some definition | a free-text signature compared literally would block correct patches |
| Scope rejections | — | rejected out-of-scope blocks are counted as `scope_violation` clashes but not carried into `T0.edits`; parse/apply/truncation errors are | a lane that strays should not block a merge that is otherwise fine |
| Shadow merge | first-pass outputs | the lanes' **first replies**, without their edit-repair turns | owner: no repair in the shadow |
| Gate replays | — | every blocked merge is scored on the score tests (`outcome.scored` with `gate_seq`), so gate quality covers La | free (verifier only) |
| Duplicated work | same new top-level symbol in two lanes | dunder names ignored | `__metaclass__ = type` boilerplate flagged on a gold split |
| Probe arms | La only | La only (`--arms La`); main runs `--arms La,D` | D's rerun is for pairing on the fresh tasks |

### Task lists (`results/swepro-lanes/splits.json`)

Recomputed by `--action select-lanes` from the cached dataset and context files, against partition
`b409bcdd…`: tier 1 = 30, tier 2 = 15, tier 3 = 22, exactly §3's counts. `lanes-main` = 40 tasks
(sha256 of the ordered list `9661965e…`); `lanes-probe` = 12 tasks (`74574d74…`): from the main 90
openlibrary-d109cc7e, ansible-ecea15c5, NodeBB-76797371, openlibrary-d40ec887, openlibrary-b112069e,
ansible-eea46a0d; from probe 1/2 element-web-66d0b318, ansible-b748edea, openlibrary-630221ab,
element-web-ad26925b, ansible-d72025be, openlibrary-25858f9f. File sha256 `1cc6131c…`.

### Dry runs (fakes, the 12 probe tasks, arms La and D)

| `--dry-solve` | La | La-first | La-sonnet | La-shadow | D | What it exercised |
|---|---|---|---|---|---|---|
| `all` (gold) | 12/12 | 12/12 | 12/12 | 12/12 | 12/12 | planner k = 3 on all 12, one round, T0.interfaces passed on every gold merge |
| `escalate` | 12/12 | 0/12 | 12/12 | 0/12 | 12/12 (B 0/12) | Haiku fails the last lane: blamed (rule 1 via T0.interfaces 6, rule 2 via tracebacks 18), Haiku repair, then Sonnet passes; D forks to its Opus treatment |
| `conflict` | 12/12 | 12/12 | 12/12 | 1/12 | 12/12 | lane L1 adds an out-of-scope block: rejected by the governed merge on 11 tasks (the 12th had no other-lane block to conflict with), applied by the shadow merge |

Every log verified (`contraprova verify`: chain intact, replay clean). The fake's token counts are
characters / 4, so these runs say nothing about real cost.

### Gold-lane check on real images (`--action validate-lanes`, `results/swepro-lanes/validate-lanes.json`)

The gold patch split by file into k = 3 lanes, pushed through each lane's applier, the governed
merge, T0.interfaces (board seeded from the task's interface field) and the shadow merge; then lane
L1 given one extra out-of-scope block that rewrites another lane's code file.

| Task | Image GB | Empty / gold | Governed | Shadow | T0.interfaces (checked) | Conflicting lane: rejected / governed unchanged / shadow score |
|---|---|---|---|---|---|---|
| ansible-ecea15c5 | 2.16 | 0 / 1 | 1 | 1 | pass (none) | 1 / yes / 0 |
| NodeBB-76797371 | 3.46 | 0 / 1 | 1 | 1 | pass (1) | 1 / yes / 0 |
| openlibrary-d40ec887 | 3.19 | 0 / 1 | 1 | 1 | pass (3) | 1 / yes / 1 |
| ansible-eea46a0d | 2.44 | 0 / 1 | 1 | 1 | pass (2) | no code block in another lane to conflict with |

All 4 ok, 0 failures. The conflicting block was always rejected and the governed result unchanged;
in the shadow merge it broke the score on 2 of 3 tasks (on openlibrary-d40ec887 the inserted line
sits in code the tests do not reach). Afterwards `docker system df` showed 0 images and the daemon
was stopped.

### Still needed before the paid probe

1. A PREREG.md freezing §6's items (prompts and their hashes, `LaneConfig`, ladder, blame, merge,
   shadow and detector rules, task-list hashes above, price table, harness file hashes, analysis).
2. An API key in the session (`--client anthropic`) and the free count_tokens pre-flight on the 12
   probe tasks; the run refuses to start if expected × 1.2 exceeds the $5 cap.
3. `--validate-before-run` repeats the empty/gold and gold-lane checks per task as images are pulled.
