# SWE-bench Pro harness

The main study moves from Aider Polyglot to **SWE-bench Pro V2 (public), restricted to its five
Python/JavaScript repositories with images of at most 6.1 GB**: ansible, openlibrary, qutebrowser
(Python), NodeBB and element-web (JavaScript/TypeScript). That is 327 of V2's 642 tasks. teleport
(11.8 GB images), protonmail/webclients (22.6 GB) and the Go repositories are left out.

Code: `src/contraprova/harness/` — `swepro.py` (tasks, partition, test split, verifier files,
context), `outline.py` (what the orchestrator sees), `edits.py` (the edit format), `docker.py`
(daemon, image cache, verifier), `swepro_arms.py` (arms, gates, scorer, task loop, pre-flight),
`swepro_run.py` (command line). Tests: `tests/test_swepro.py`, `tests/test_swepro_edits.py`.

## Why this benchmark

The Polyglot pilot showed the escalation question cannot be asked there: Haiku 5.5 solved 17 of
18 tasks, so almost nothing failed and nothing was escalated ([pilot](../results/pilot/README.md)).
SWE-bench Pro is the only public, test-scored set with a large published gap between the two
models this study is about (Anthropic's system cards: Haiku 5.5 64.8%, Opus 5.5 89.9%, both at max
effort in an agentic scaffold). Its tasks are also decomposable: most gold patches touch several
files, and every task ships `requirements` and `interface` fields that read like lane contracts.
Whether the gap survives single-shot, low-effort requests is what the probe tests.

## What a task looks like to each role

| Role | Sees | Never sees |
|---|---|---|
| Worker (Haiku; Opus in arm A; Sonnet as fork treatment) | problem statement, requirements, interface, **full content of the files the gold patch changes**, names of files it may create, names of the visible tests, the planner's brief (C, D) | test code, the gold patch, private or score test names |
| Orchestrator (Opus: arm C/D brief, arm C review) | problem statement, requirements, interface, **outline** of the same files, names of the visible tests; the review adds the worker's diff | any file body |
| T2 reviewer (Sonnet, arm D) | the outline and the diff | any file body |
| Gates (arm D) | test results by name | — |
| Scorer | the official reward | — |

This is the "oracle files" setting of the feasibility study: the harness picks the files, the
models do not search the repository. It applies to every arm equally.

### The outline rule, and why it is the crux

The hypothesis is that a governed swarm (an Opus planner directing Haiku workers) costs less per
solved task than a single Opus. On Pro the files a worker needs average about 25K tokens. If the
orchestrator read them, arms C and D would pay Opus input prices on the same 25K tokens as arm A
*plus* the workers' cost, and the swarm would be more expensive by construction, whatever its
quality. So the orchestrator plans from an outline: paths, line counts, imports, class and function
signatures with one-line docstrings (Python, via `ast`), declaration lines (JavaScript/TypeScript,
via regular expressions over the file with strings and comments blanked), and nothing else. On the
12 probe tasks the outline is 81 to 7,341 estimated tokens (21K in all) against worker prompts of
3,073 to 37,774 (213K in all; table below).

Two checks keep the rule honest. `tests/test_swepro.py` builds fixture files in which every body
line carries a marker, and asserts that no marker and no body statement reaches the orchestrator
prompt, while the worker prompt contains the files in full; and that the orchestrator prompt stays
under its bound (`LEAD_MAX_TOKENS_EST`, 16K estimated tokens) even for files of more than 100K
tokens, by dropping nesting levels from the outline and then cutting it. The review prompts add the
diff, capped at 40K characters.

The outline may make the planner worse than one that reads everything. That is part of what is
measured, not a flaw to hide: the claim is about the cost-quality trade-off of exactly this design.

## Edit format

Workers answer with SEARCH/REPLACE blocks instead of whole files (`edits.py` has the full rules):

```
<<<<<<< SEARCH lib/ansible/plugins/inventory/__init__.py
lines copied exactly from the current file
=======
the lines that replace them
>>>>>>> REPLACE
```

- The SEARCH text must occur exactly once in the file as it stands after earlier blocks; matching
  is exact, with a single fallback that ignores trailing whitespace (still required to be unique).
  An empty SEARCH creates a file. CRLF files keep CRLF; files without a final newline keep none.
- Aider's layout (path on the line above, optionally inside a code fence) is accepted too.
- A malformed or truncated block is reported and dropped, never half-applied; a block that does not
  match is reported and skipped; the others still apply. Errors go into `task.submitted`
  (`parse_errors`) and, in arm D, block the submission at gate `T0.edits` with the errors as the
  reason the worker sees on retry.
- Blocks on a test file (the official verifier's rule, `swepro.is_test_path`) or outside the task's
  files are rejected before anything is applied, in every arm.

Every in-scope gold patch was pushed through this path (context downloaded at the base commit):
323 of 327 convert and apply without error. The other four delete or rename a file, which the
format cannot express; those tasks are excluded (`ProTask.unsupported_gold`), so the reference
solution of every remaining task is expressible by a worker.

**One repair turn** (every arm, forks included). If a reply was cut off at `max_tokens`, or some
block failed to parse or apply, the worker gets one follow-up user turn holding only that edit error
(for truncation: "your answer was cut off at the output limit; reply with only the remaining edit
blocks"). It never includes test names, test output or results. The blocks that applied plus the
repair's blocks are applied together; there is no second repair. The repair request is tagged
`repair` in `usage.recorded` and counts toward the arm's cost; `task.submitted` records
`repair_used` and the failure before and after it. `--no-repair` turns it off. Added after probe 2,
where four of Opus's five failures were edit-format failures.

Whole-file returns were dropped because a 50K-character file would cost Opus about $0.30 per
attempt in output alone and a truncated reply would lose the whole file. Worker `max_tokens` is 32K
for Haiku and Opus (equal since probe 3; probes 1 and 2 used 16K/32K for Haiku and 12K for Opus) and 16K for Sonnet.

## Scoring and gates

Each task image (`ghcr.io/scaleapi/swe-bench_pro-v2:<instance_id>`) holds the repository at the
base commit in `/app`. The verifier is the official one, run the way the feasibility study
validated: plain `docker run --rm --network none`, with the task's `tests/` directory (`test.sh`,
`run_script.sh`, `parser.py`, `config.json`, `test_patch.patch`) mounted at `/tests`, the
submission's changed files copied over `/app`, and `bash /tests/test.sh`. `test.sh` resets test
files, applies the test patch, runs the selected test files, parses the results and writes
`reward.txt` (1 if every fail-to-pass and pass-to-pass test passed) and `output.json` (per-test
status). Default timeout 30 minutes per run, memory 8 GB.

Test split per task (seeded, deterministic, frozen in the partition file):

- **visible**: ceil(n/2) of the n fail-to-pass tests, ranked by sha256 of seed, task and test id.
  Workers see their names; gate `T0.visible_tests` requires them to pass, and its block reason
  names the visible tests that failed.
- **gate-private**: the remaining fail-to-pass tests. Gate `T1.private_tests` requires them; its
  block reason gives only a count.
- **score**: all fail-to-pass and all pass-to-pass tests, i.e. the official reward.

Unlike the Polyglot split, the gate subsets lie inside the score set, because the official score is
defined on the full set. One verifier run per distinct submission serves the gates and the scorer
(memoised by the hash of the changed files); the gates read only their own subsets.

Arm D's gates, in order, stopping at the first block, folded into one `gate.decided` per submission:
`T0.scope` (lane contract), `T0.edits`, `T0.visible_tests`, `T1.private_tests` and, when required,
`T2.review`. Evidence carries `tests_sha256` (verifier files and test patch), counts, run time and
the hash of the submitted files.

### T2.review threshold

The Polyglot rule required a review above 200 changed lines. Counting added plus removed lines, the
gold patches of the 327 in-scope tasks have a median of 71, a 75th percentile of 136, a 90th of 290
and a 95th of 496 (the feasibility study, counting differently, put a quarter above 190). A 200-line
rule would send a large share of correct patches to review. The threshold is configurable
(`--t2-max-lines`) and defaults to **500 changed lines**, which only the largest ~5% of gold patches
exceed; a manifest edit (`package.json`, `setup.py`, `requirements.txt`, ...) still always requires
review. The reviewer sees the outline and the diff only. Without `--client anthropic` (or with
`--no-t2-review`) T2 is recorded as skipped, as on Polyglot.

## Docker

- **Daemon.** In these cloud sessions `dockerd` is installed but not running at start. A run that
  needs Docker calls `docker.ensure_dockerd()`: if `docker info` fails and the process is root, it
  starts `dockerd` in the background (log: `$TMPDIR/contraprova-dockerd.log`), waits for it, and
  stops it again when the run ends. A daemon that was already running is left alone.
- **Image cache.** `docker.ImageCache` pulls on demand, never more than one pull at a time (a lock
  file serialises pulls across processes), and deletes the least recently used images *it* pulled
  so they stay under `--max-image-gb` (default 20 GB), keeping room for the next pull (7 GB, the
  largest in-scope image rounded up). It never deletes images it did not pull.
- **Context files** are copied out of the image with `docker create` + `docker cp`; the container is
  never started. They are cached under `<data>/context/<id>/`.

## Loop order and resume

Tasks are the outer loop and arms the inner loop, so each image is pulled once per task for all
arms, gates and forks. Before loading a task, the loop checks the log: a (task, arm) whose kernel
task (`<arm>:<instance_id>`) exists is skipped, and a task with every arm done is not loaded at all.
An interrupted run is resumed by running the same command again. Tasks whose gold patch deletes or
renames a file (4 of 327) are excluded, and so are tasks whose estimated worker
prompt exceeds 90K tokens (`--max-prompt-tokens`; estimate = characters / 3, deliberately high) are
excluded and listed in the summary, so Haiku stays below its 100K long-context price step.

## Data and partition

Nothing from the benchmark is committed. `<data>` (default `data/swepro/`, git-ignored) holds:

- `tasks.jsonl`: the 327 in-scope rows of `ScaleAI/SWE-bench_Pro` (config `default`, split `test`),
  downloaded with the standard library from the Hugging Face datasets server (checked identical to
  the parquet file used by the feasibility study);
- `verifiers/<id>/`: the task's verifier files from `scaleapi/SWE-bench_Pro-os` (`v2/tasks/<id>/tests/`),
  checked against the dataset row (same test lists, same test patch);
- `context/<id>/`: the files the worker sees;
- `partition.json`: the frozen partition with its sha256 and `dataset_sha256`;
- `images.json`: the image cache's state.

Partition (seed 20261111): **probe** = the feasibility study's 12 tasks exactly; **smoke** = one
task per repository drawn from the rest; **main** = the remaining 310 in a seeded order that
interleaves repositories in proportion to their size, so any prefix (`--limit N`) is stratified.

## Command line

```bash
# $0: data, verifier files and context for a split (one image at a time, deleted afterwards)
python -m contraprova.harness.run --benchmark swepro --action prepare --split probe --delete-images

# $0: empty patch must score 0, gold patch through the SEARCH/REPLACE path must score 1
python -m contraprova.harness.run --benchmark swepro --action validate --split probe --out validate.json

# $0: the whole loop with offline fakes (workers reply with the gold patch; nothing is executed)
python -m contraprova.harness.run --benchmark swepro --split probe --arms A,B,C,D --log runs/dry.jsonl \
    --budget-usd 5 --dry-run

# the probe: pre-flight only (free count_tokens calls), then the paid run
python -m contraprova.harness.run --benchmark swepro --action preflight --client anthropic --log runs/probe.jsonl --budget-usd 2.5
python -m contraprova.harness.run --benchmark swepro --probe --client anthropic --log runs/probe.jsonl --budget-usd 2.5

# arms on a split
python -m contraprova.harness.run --benchmark swepro --split main --limit 60 --arms A,B,C,D \
    --client anthropic --router MODULE:ROUTER --log runs/main.jsonl --budget-usd 30 --max-image-gb 20
```

### The probe and its pre-flight

`--probe` runs single Haiku twice (arms B and B2: two independent samples, since the API has no
sampling seed) and single Opus at low effort (arm A) on the 12 probe tasks, `max_tokens` 16K for
Haiku and 12K for Opus. Tasks run in ascending order of estimated worker-prompt size with all three
requests of a task together, so each image is pulled once and a budget stop loses only the largest
tasks (the feasibility study proposed all Haiku first, then Opus in ascending size; that order
would pull every image twice).

Before any paid request the pre-flight builds every probe prompt exactly as it will be sent and
counts it with the free `count_tokens` endpoint (`AnthropicClient.count_tokens`). Each count is
priced at the expected output (an answer of 1.3 times the gold patch as SEARCH/REPLACE text at 3.3
characters per token, plus a thinking allowance of 2,000 tokens for Haiku and 800 for Opus
calibrated on the pilot, capped at `max_tokens`) and at the worst case (all of `max_tokens`). The
run refuses to start (exit code 3) if expected cost × 1.2 (`--preflight-margin`) exceeds
`--budget-usd`; the budget's hard cap still stops the run before any request that could cross it.
`--action preflight` prints the pre-flight and stops.

## $0 validation on the 12 probe tasks

Run on 2026-11-11 with `--action validate --split probe` (no API calls): for each task the image was
pulled alone, the official verifier ran once on the unchanged base commit and once on the gold patch
converted to SEARCH/REPLACE blocks and applied by the harness's own parser and applier, then the image
was deleted. In every image the repository's HEAD was the task's base commit, the gold patch also
passed both gate subsets by per-test status, and the empty patch failed the visible subset. Context
files read from the images were byte-identical to the same files downloaded at the base commit. Raw
results: [results/swepro-validation/probe-validation.json](../results/swepro-validation/probe-validation.json).
`docker system df` was 0 B afterwards and the daemon the run had started was stopped.

Token columns are estimates (characters / 3): the worker prompt includes a 250-word brief allowance;
the lead prompt is the orchestrator's brief request (outline context). Test times are whole container
runs, including `test.sh`'s reset and test-patch steps.

| # | Task | Image GB | Pull s | Empty: reward, s | Gold via edit path: reward, s | F2P (visible/private) | P2P | Worker prompt tok (est) | Outline tok (est) | Lead prompt tok (est) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | NodeBB-da0211b1 | 3.45 | 27.1 | 0, 3.9 | 1, 4.4 | 2 (1/1) | 23 | 3,073 | 81 | 1,312 |
| 2 | NodeBB-f2082d7d | 3.64 | 22.2 | 0, 5.2 | 1, 5.3 | 6 (3/3) | 111 | 19,594 | 1,506 | 2,654 |
| 3 | ansible-29aea9ff | 2.18 | 20.5 | 0, 28.3 | 1, 29.5 | 4 (2/2) | 10 | 9,667 | 1,007 | 2,766 |
| 4 | ansible-34db57a4 | 1.93 | 16.7 | 0, 25.9 | 1, 27.5 | 3 (2/1) | 0 | 34,743 | 625 | 1,521 |
| 5 | ansible-a20a5270 | 2.04 | 18.2 | 0, 25.3 | 1, 27.3 | 1 (1/0) | 56 | 19,496 | 1,452 | 2,434 |
| 6 | element-web-41dfec20 | 4.46 | 36.4 | 0, 30.4 | 1, 34.2 | 3 (2/1) | 102 | 6,674 | 216 | 1,761 |
| 7 | element-web-ad26925b | 4.53 | 31.8 | 0, 30.6 | 1, 31.4 | 17 (9/8) | 184 | 16,232 | 673 | 3,586 |
| 8 | openlibrary-25858f9f | 4.15 | 29.3 | 0, 3.2 | 1, 3.1 | 13 (7/6) | 33 | 24,271 | 3,147 | 5,535 |
| 9 | openlibrary-58999808 | 4.22 | 29.1 | 0, 4.5 | 1, 4.4 | 3 (2/1) | 0 | 3,874 | 403 | 1,818 |
| 10 | openlibrary-6e889f4a | 2.91 | 22.2 | 0, 2.9 | 1, 2.6 | 7 (4/3) | 10 | 21,384 | 2,725 | 4,416 |
| 11 | qutebrowser-5fdc83e5 | 2.15 | 17.1 | 0, 8.8 | 1, 6.6 | 11 (6/5) | 46 | 37,774 | 7,341 | 8,497 |
| 12 | qutebrowser-a84ecfb8 | 2.48 | 19.0 | 0, 7.7 | 1, 7.7 | 3 (2/1) | 28 | 16,023 | 1,865 | 2,957 |

All ok: True; failures 0. Totals: pull 290 s, tests 361 s, images 38.1 GB, worker prompts 212,805 est. tokens, outlines 21,041.

## Contract Lanes (arm La)

The lanes study ([lanes-plan.md](lanes-plan.md)) adds arm `La` to this harness: code in
`harness/lanes.py` (planner, board, lane prompts and runner, governed and shadow merges,
`T0.interfaces`, blame, the per-lane ladder, the gold-lane check, the lane pre-flight) and
`harness/lanes_detect.py` (clash detectors); tests in `tests/test_lanes.py`.

- **Planner**: Opus 5.5, effort low, `max_tokens` 8K, `ProHarness.lead_context` (outline, no file
  body) plus the list of files to assign; one strict JSON plan of 1 to 4 lanes, one plan-repair turn,
  else one lane (`fallback: single_lane`).
- **Board**: the planner's interfaces plus every function, class or method named in the task's
  `interface` field, frozen in one kernel step (`interface.frozen`; no model call).
- **Lanes**: Haiku 5.5 medium, 32K `max_tokens`, one edit-repair turn; own files in full, the other
  files as an outline, the frozen interfaces, its goal and visible tests; blocks outside its files
  or on tests are rejected before anything applies.
- **Merge and gates**: union of the lanes' files, then `T0.scope`, `T0.edits`, `T0.interfaces`,
  `T0.visible_tests`, `T1.private_tests`, `T2.review` (above 500 lines) on the merged patch.
- **Blame and ladder**: a blocked merge blames lanes (direct, then failing test files and
  tracebacks, then all lanes); each blamed lane moves one step on its own ladder: a Haiku repair
  with the gate feedback, then Sonnet 5.5 medium, then Opus 5.5 low. Lanes not blamed keep their
  output. The task stops at the first passing merge, or unsolved when a lane is blamed at Opus
  (at most 3 rounds after the first). Fable is refused by `LaneConfig`.
- **Shadow merge** (`La-shadow`): the lanes' first replies joined with no contract, no interface
  check, no repair and no escalation, scored once on the score tests ($0 in tokens). `La-first`
  (first governed merge) and `La-sonnet` (ladder without its Opus step) are derived the same way.

### Task lists

`--action select-lanes` freezes `<data>/lanes-splits.json` (its own file with a sha256, so the main
partition `b409bcdd…` is unchanged; it records that partition's hash). The fresh pool is the main
split in its seeded order minus probe, probe2 and the main study's eligible 90; tiers by gold files,
changed lines and the estimated counted worker prompt ((gold-file bytes + statement) / 3 + 1,212,
× 1.075). On the in-scope dataset: tier 1 = 30, tier 2 = 15, tier 3 = 22 tasks, exactly the plan's
counts. `lanes-main` = all of tier 1 then the first 10 of tier 2 (40 tasks: openlibrary 16, ansible
8, qutebrowser 6, element-web 5, NodeBB 5; mean 6.4 gold files, 343 changed lines). `lanes-probe` =
6 tier-1 tasks of the main 90 that have A and D outcomes plus 6 tier-1 tasks of probe 1/2 (ranked
by a seeded hash). The committed copy is
[results/swepro-lanes/splits.json](../results/swepro-lanes/splits.json).

### Command line

```bash
# $0: freeze the task lists (needs every task's context files under <data>/context)
python -m contraprova.harness.run --benchmark swepro --action select-lanes

# $0: empty/gold check plus the gold-lane check (gold split by file into k = 3 lanes, governed and
# shadow merges must score 1, a conflicting out-of-scope lane must be rejected) on real images
python -m contraprova.harness.run --benchmark swepro --action validate-lanes --split lanes-probe --out v.json

# $0: the whole loop with fakes; --dry-solve escalate|conflict exercises repair, escalation, forks
python -m contraprova.harness.run --benchmark swepro --lanes --split lanes-probe --arms La,D \
    --log runs/lanes-dry.jsonl --budget-usd 25 --dry-run --dry-solve escalate

# free pre-flight (count_tokens on the planner prompt and every lane prompt of a k = 3 by-file split,
# and D's prompts), then the paid probe (La only, as planned) and the main run (La and D)
python -m contraprova.harness.run --benchmark swepro --lanes --action preflight --split lanes-probe --arms La \
    --client anthropic --log runs/lanes-probe.jsonl --budget-usd 5
python -m contraprova.harness.run --benchmark swepro --lanes --split lanes-probe --arms La --client anthropic \
    --log runs/lanes-probe.jsonl --budget-usd 5 --validate-before-run --no-log-contents --delete-images
python -m contraprova.harness.run --benchmark swepro --lanes --split lanes-main --arms La,D --client anthropic \
    --log runs/lanes-main.jsonl --budget-usd 25 --p-fork 0.35 --validate-before-run --no-log-contents --delete-images

# the report: La vs La-shadow is the whole primary family (exact McNemar); La vs D by paired bootstrap
contraprova report --lanes runs/lanes-main.jsonl
```

`--lanes` runs D as the main study's cascade (`--cascade`), runs the lane pre-flight before any
paid request and refuses (exit 3) when expected cost × `--preflight-margin` exceeds `--budget-usd`;
tasks whose counted planner, lane or D worker prompt exceeds 90K are excluded. With
`--validate-before-run` each task also passes the gold-lane check before La runs.

## Licences

- contraprova itself is Apache-2.0 (see the repository licence).
- The SWE-bench Pro harness and verifier scripts (`test.sh`, `run_script.sh`, `parser.py`) are
  Scale AI's, MIT-licensed. They are downloaded at run time and executed only inside task
  containers; they are not vendored.
- Task content (problem statements, requirements, interfaces, gold and test patches, repository
  code in the images) stays under the upstream repositories' licences: ansible (GPL-3.0),
  openlibrary (AGPL-3.0), qutebrowser (GPL-3.0), NodeBB (GPL-3.0) and element-web (AGPL-3.0 or
  GPL-3.0). It is used for evaluation and is never committed here. Run logs embed file contents
  and patches, so publishing a log redistributes that content under those licences.
