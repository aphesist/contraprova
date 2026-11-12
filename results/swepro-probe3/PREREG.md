# SWE-bench Pro probe 3: pre-registration

Written and committed **before any paid request** of this probe, on 2026-11-12. Nothing below is
changed after the run.

## What this probe is (and is not)

**This is an instrument re-check, not a fresh sample.** It reruns **the same 12 tasks as probe 2**
(split `probe2`, ids below), which the models and we have already seen in probe 2. Its purpose is
to check whether probe 2's result depended on the measuring instrument, since four of Opus's five
failures there were edit-format failures (truncation at 12K output tokens, blocks that did not
apply or parse). The models are stateless, but our choice to re-run these tasks was made after
seeing probe 2. The result is therefore not an independent estimate. **The main study will use
untouched tasks** (split `main`, which excludes `probe` and `probe2` for good).

## Tasks

Partition sha256 **`b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`**, the same
partition as probe 2 (`data/swepro/partition.json`, seed 20261111). The tasks are
`contraprova.harness.swepro.PROBE2_IDS`. They run in ascending order of counted prompt tokens
(count_tokens; the same for Haiku and Opus):

| # | Task (instance_id) | Counted prompt tokens |
|---|---|---|
| 1 | `instance_ansible__ansible-d62496fe416623e88b90139dc7917080cb04ce70-v0f01c69f1e2528b935359cfe578530722bca2c59` | 4,012 |
| 2 | `instance_element-hq__element-web-ecfd1736e5dd9808e87911fc264e6c816653e1a9-vnan` | 7,412 |
| 3 | `instance_ansible__ansible-40ade1f84b8bb10a63576b0ac320c13f57c87d34-v6382ea168a93d80a64aab1fbd8c4f02dc5ada5bf` | 9,947 |
| 4 | `instance_NodeBB__NodeBB-8168c6c40707478f71b8af60300830fe554c778c-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e` | 12,540 |
| 5 | `instance_element-hq__element-web-66d0b318bc6fee0d17b54c1781d6ab5d5d323135-vnan` | 17,018 |
| 6 | `instance_ansible__ansible-e9e6001263f51103e96e58ad382660df0f3d0e39-v30a923fb5c164d6cd18280c02422f75e611e8fb2` | 17,550 |
| 7 | `instance_internetarchive__openlibrary-53d376b148897466bb86d5accb51912bbbe9a8ed-v08d8e8889ec945ab821fb156c04c7d2e2810debb` | 27,099 |
| 8 | `instance_internetarchive__openlibrary-322d7a46cdc965bfabbf9500e98fde098c9d95b2-v13642507b4fc1f8d234172bf8129942da2c2ca26` | 27,477 |
| 9 | `instance_ansible__ansible-d72025be751c894673ba85caa063d835a0ad3a8c-v390e508d27db7a51eece36bb6d9698b63a5b638a` | 28,313 |
| 10 | `instance_ansible__ansible-b748edea457a4576847a10275678127895d2f02f-v1055803c3a812189a1133297f7f5468579283f86` | 46,051 |
| 11 | `instance_internetarchive__openlibrary-630221ab686c64e75a2ce253c893c033e4814b2e-v93c53c13d5f9b383ebb411ee7750b49dcd1a34c6` | 53,318 |
| 12 | `instance_qutebrowser__qutebrowser-3e21c8214a998cb1058defd15aabb24617a76402-v5fc38aaf22415ab0b70567368332beee7955b367` | 54,231 |

## Changes from probe 2 (harness commit 0d80260; the same for every arm)

1. **Opus `max_tokens` 32,000** (was 12,000), the same as Haiku's 32,000. Efforts are unchanged.
2. **One repair turn.** If a worker reply was cut off at `max_tokens`, or some SEARCH/REPLACE block
   failed to parse or apply, the worker gets **one** follow-up user turn:
   - for truncation: "Your answer was cut off at the output limit. Reply with only the remaining
     SEARCH/REPLACE edit blocks; do not repeat blocks you already sent in full.";
   - otherwise: the parse/apply error lines only, asking for corrected blocks.

   The repair turn never contains test names, test output or results. The blocks that applied plus
   the repair's blocks are applied together, and there is no second repair. The repair request is
   tagged `repair` in `usage.recorded` with the same arm and task tags, so it counts toward that
   arm's cost. `task.submitted` records `repair_used`, `original_failure` and `final_failure`.
   Tests: `tests/test_swepro.py` (repair on parse error, apply error and truncation; no test
   information in the repair prompt; at most one repair; cost includes the repair).

## Configuration

- Arms: **B** and **B2** are two independent samples of Haiku 5.5 (`claude-haiku-5-5`) at effort
  `medium`; **A** is Opus 5.5 (`claude-opus-5-5`) at effort `low`. Single-shot apart from the repair
  turn; oracle-files setting (`docs/swepro.md`).
- `max_tokens`: **Haiku 32,000, Opus 32,000.**
- Command: `python -m contraprova.harness.run --benchmark swepro --probe --probe-split probe2
  --no-log-contents --client anthropic --budget-usd 5 --log runs/swepro-probe3/log.jsonl
  --delete-images`.
- **Hard cap $5.00.** Pre-flight (free count_tokens): expected **$2.9546** (× 1.2 =
  $3.5455 ≤ $5.00), worst case $9.3449 (above the cap, so the
  budget guard stops before any request that could cross $5.00; the largest tasks run last).
  - The pre-flight's expected cost does not price repair turns, or Opus outputs longer than probe 2's
    12K limit. On probe 2's evidence (two tasks truncated in every arm), the actual spend may be
    higher, roughly $3.5–4. If the guard stops the run early, that will be reported, and tasks not
    run will count as not run, never as solved.
- Log: per-file sha256, diffs, edit blocks (including the repair reply) and test output. Never full
  repository file contents.

## Decision rule (unchanged from probes 1 and 2)

**GO** if Haiku (sample 1, arm B) fails **≥ 4/12** AND Opus (arm A) solves **≥ 3 more** than Haiku
AND Opus solves **≥ 6/12**.

- The decision is made on **all failures** (after the repair turn, which is now part of every arm).
- Reported separately, without changing the decision:
  - logic-only failures (all failures minus edit-format failures that remain after repair);
  - each task's failure type before and after repair, and whether a repair was used;
  - how many of B's failures B2 solves (the retry noise floor).
