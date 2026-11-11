# SWE-bench Pro probe 2: harder tasks (2026-11-11)

**These are pilot measurements on 12 tasks**, used for the pre-registered GO/NO-GO decision.
Pre-registration: [PREREG.md](PREREG.md), committed (0414b74) before any paid request. Nothing in it
was changed afterwards.

- **Tasks:** split `probe2`, 12 tasks from Pro's HARD-51 subset (selection and replacements in
  PREREG.md). Partition sha256 `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`
  (`partition.json`).
- **Arms:** B and B2 are two independent samples of Haiku 5.5 at effort `medium`, `max_tokens` 32,000.
  A is Opus 5.5 at effort `low`, `max_tokens` 12,000. Single-shot, oracle-files setting.
- **Log:** `log.jsonl`. `contraprova verify` gives 216 events, chain intact, replay clean. The verifier
  ran 34 times; wall time was 59 min. Every image was deleted after its task (`/var/lib/docker` is
  1.1 MB afterwards).
- **Pre-flight:** expected $2.9546 (× 1.2 = $3.55, within the $4.50 cap); worst case $4.5449. The
  budget guard never stopped the run.

## Data withheld from this directory (licence hygiene)

The run used `--no-log-contents`. Each `task.submitted` event carries `contents_sha256` (sha256 of
every changed file) instead of the files' full new content. The committed log keeps:

- the unified diff against the base files;
- the worker's raw reply (its SEARCH/REPLACE blocks);
- gate and verifier results, with the tail of the test output.

Full repository files (GPL-3.0 / AGPL-3.0 upstream code) are not stored in the log, so publishing it
does not redistribute whole files. Prompts were never logged. The diffs and edit blocks still quote
short excerpts of upstream code, and the probe-1 log (`results/swepro-probe/`) still holds full
contents. Task data, verifier files and context stay under `data/swepro/`, which is git-ignored.

## Spend (recomputed from logged tokens, `anthropic-2026-11`)

| | USD |
|---|---|
| **Total** | **$2.7286** |
| claude-opus-5-5 (arm A) | $2.4795 |
| claude-haiku-5-5 | $0.2491 |
| arm B | $0.1222 |
| arm B2 | $0.1269 |

Spend under the owner's approvals so far: Polyglot pilot $1.9830 + probe 1 $1.4556 + probe 2 $2.7286 =
**$6.1672**.

## Per task

Ordered by counted prompt size. ✓ means solved (official reward 1). ✗ means failed, shown as F2P
passed/total, plus the failure type:

- **logic**: the edits applied cleanly but tests failed.
- **edit (trunc)**: the reply was cut off at `max_tokens`.
- **edit (apply)** / **edit (parse)**: a SEARCH/REPLACE block did not match the file, or was malformed.

| # | Task | Prompt tok | B (Haiku) | B2 (Haiku) | A (Opus low) |
|---|---|---|---|---|---|
| 1 | ansible-d62496fe | 4,012 | ✗ 21/22, logic | ✓ | ✓ |
| 2 | element-web-ecfd1736 | 7,412 | ✓ | ✓ | ✓ |
| 3 | ansible-40ade1f8 | 9,947 | ✗ 0/48, edit (trunc at 32,000) | ✗ 0/48, edit (trunc at 32,000) | ✗ 0/48, edit (trunc at 12,000) |
| 4 | NodeBB-8168c6c4 | 12,540 | ✗ 2/4, edit (apply) | ✗ 1/4, logic | ✗ 3/4, edit (apply) |
| 5 | element-web-66d0b318 | 17,018 | ✗ 23/24, logic | ✓ | ✓ (one block did not apply; still solved) |
| 6 | ansible-e9e60012 | 17,550 | ✓ | ✓ | ✓ |
| 7 | openlibrary-53d376b1 | 27,099 | ✗ 3/4, logic | ✗ 0/4 (P2P 3/4), logic | ✓ |
| 8 | openlibrary-322d7a46 | 27,477 | ✓ (31,946 output tokens) | ✓ | ✓ |
| 9 | ansible-d72025be | 28,313 | ✗ 0/5, edit (trunc at 32,000) | ✗ 0/5, edit (trunc at 32,000) | ✗ 0/5, edit (trunc at 12,000) |
| 10 | ansible-b748edea | 46,051 | ✗ 4/5, logic | ✗ 4/5, logic | ✗ 0/5, logic |
| 11 | openlibrary-630221ab | 53,318 | ✓ | ✗ 8/9, logic | ✗ 0/9, edit (parse: block without a path) |
| 12 | qutebrowser-3e21c821 | 54,231 | ✓ | ✓ | ✓ |

The largest prompt was 54,231 tokens; no request came near Haiku's 100K price step.

## Counts

| | Solved | All failures | Edit-format failures | Logic-only failures |
|---|---|---|---|---|
| Haiku, sample 1 (B) | 5/12 | **7** | 3 (2 trunc, 1 apply) | **4** |
| Haiku, sample 2 (B2) | 6/12 | 6 | 2 (2 trunc) | 4 |
| Opus low (A) | **7/12** | 5 | 4 (2 trunc, 1 apply, 1 parse) | 1 |

- **Retry noise floor:** B2 solves **2 of B's 7 failures** (#1 and #5, both near misses on one F2P
  test), and B2 fails one task B solved (#11). So the two Haiku samples disagree on 3 of 12 tasks.
- **Truncation:** tasks #3 and #9 (gold patches of 653 and 294 changed lines) were cut off in all
  three arms, Haiku at 32K and Opus at 12K.

## Pre-registered decision (decided on all failures)

Rule: **GO** if Haiku (B) fails ≥ 4/12 **and** Opus solves ≥ 3 more than Haiku **and** Opus solves
≥ 6/12.

- Haiku fails 7/12, at least 4: met.
- Opus − Haiku = 7 − 5 = **+2**, below 3: **not met**.
- Opus solves 7/12, at least 6: met.

**Verdict: NO-GO.** The rule fails on the size of the Opus gap.

Reported alongside, not part of the decision: on logic-only failures, Haiku fails 4 and Opus 1. Four
of Opus's five failures are edit-format failures: two truncations at its 12K `max_tokens`, one block
that did not apply, and one block without a path.

Of the pre-registered fallback branches, "no Opus gap → suggest an Opus medium re-probe (needs owner
approval)" is the one that matches a gap failure. The data adds a caveat for any such re-probe:
Opus's shortfall here is mostly edit format, not reasoning. Two tasks truncate at 12K output tokens
in every arm. A re-probe that only raises Opus's effort, without raising its `max_tokens` (or
splitting the largest patches), would keep losing those two tasks. These are owner decisions;
nothing beyond the probe was started.
