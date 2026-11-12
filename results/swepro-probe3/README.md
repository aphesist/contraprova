# SWE-bench Pro probe 3: instrument re-check on the probe 2 tasks (2026-11-12)

**This reruns the same 12 tasks as probe 2.** It is an instrument re-check, not a fresh sample, and
the main study will use untouched tasks. These are pilot measurements on 12 tasks. Pre-registration:
[PREREG.md](PREREG.md), committed (14d92e2) before any paid request and unchanged since.

- **Instrument changes since probe 2** (harness b133048, the same for every arm):
  - Opus `max_tokens` 32,000 (was 12,000), the same as Haiku.
  - One repair turn after a reply that was cut off at `max_tokens` or whose edit blocks failed to
    parse or apply. The repair turn holds only the edit error, never test names or results.
- **Arms:** B and B2 are two independent samples of Haiku 5.5 at effort `medium`; A is Opus 5.5 at
  effort `low`. Partition sha256 `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`
  (as probe 2; the partition file is in `results/swepro-probe2/`).
- **Log:** `log.jsonl`. `contraprova verify` gives 227 events, chain intact, replay clean. The
  verifier ran 35 times; wall time was 69 min. Every image was deleted (`/var/lib/docker` is 1.1 MB
  afterwards).
- **Pre-flight:** expected $2.9546 (× 1.2 = $3.55 ≤ $5.00); worst case $9.34. The budget guard never
  stopped the run, and all 36 requests plus 11 repair requests completed.
- **Withheld data (licence hygiene):** the run used `--no-log-contents`, so `task.submitted` carries
  `contents_sha256` instead of file contents (0 `contents` fields, 36 hash fields). The diffs, edit
  blocks (repair replies included) and test output are kept; prompts were never logged. Task data
  stays in the git-ignored `data/swepro/`.

## Spend (recomputed from logged tokens, `anthropic-2026-11`)

| | USD |
|---|---|
| **Total** | **$3.3164** |
| claude-opus-5-5 (arm A) | $3.0220 |
| claude-haiku-5-5 | $0.2945 |
| arm B | $0.1512 |
| arm B2 | $0.1432 |
| of which repair turns (11 requests) | $0.4130 (A $0.3762 over 3, B $0.0249 over 5, B2 $0.0119 over 3) |

Spend under the owner's approvals so far: $1.9830 + $1.4556 + $2.7286 + $3.3164 = **$9.4836**.

## Per task

Ordered by counted prompt size. For each arm the cell gives:

- the outcome: ✓ solved (reward 1), or ✗ failed with F2P passed/total;
- **before → after**: the edit-format failure of the first reply, and what was left after the repair
  turn. "—" means no edit-format failure, so no repair turn was sent.

| # | Task | Prompt tok | B (Haiku) | B2 (Haiku) | A (Opus low) |
|---|---|---|---|---|---|
| 1 | ansible-d62496fe | 4,012 | ✓ — | ✓ — | ✓ apply → none |
| 2 | element-web-ecfd1736 | 7,412 | ✓ — | ✓ — | ✓ — |
| 3 | ansible-40ade1f8 | 9,947 | ✗ 0/48, truncated → parse | ✗ 0/48, truncated → parse | ✓ — (15,050 output tokens) |
| 4 | NodeBB-8168c6c4 | 12,540 | ✗ 1/4, — (logic) | ✗ 2/4, — (logic) | ✗ 3/4, — (logic) |
| 5 | element-web-66d0b318 | 17,018 | ✓ — | ✓ — | ✓ apply → none |
| 6 | ansible-e9e60012 | 17,550 | ✓ apply → none | ✓ — | ✓ — |
| 7 | openlibrary-53d376b1 | 27,099 | ✗ 2/4, — (logic) | ✗ 3/4, — (logic) | ✓ — |
| 8 | openlibrary-322d7a46 | 27,477 | ✓ truncated → none | ✓ truncated → none | ✓ — |
| 9 | ansible-d72025be | 28,313 | ✗ 0/5, truncated → none (logic) | ✗ 0/5, truncated → none (logic) | ✓ — (16,738 output tokens) |
| 10 | ansible-b748edea | 46,051 | ✓ — | ✓ — | ✗ 0/5, — (logic) |
| 11 | openlibrary-630221ab | 53,318 | ✓ apply → none | ✓ — | ✗ 1/9, parse → none (logic) |
| 12 | qutebrowser-3e21c821 | 54,231 | ✓ — | ✓ — | ✓ — |

"Truncated" means the first reply hit 32,000 output tokens. Haiku hit that limit on #3, #8 and #9 in
both samples. Opus never did.

## Counts

| | Solved | All failures | Edit-format failures left after repair | Logic-only failures |
|---|---|---|---|---|
| Haiku, sample 1 (B) | 8/12 | **4** | 1 (#3: truncated, then a block without a path) | **3** |
| Haiku, sample 2 (B2) | 8/12 | 4 | 1 (#3) | 3 |
| Opus low (A) | **9/12** | 3 | 0 | 3 |

- **Retry noise floor:** B2 solves **0 of B's 4 failures**. The two Haiku samples solve exactly the
  same 8 tasks (in probe 2 they disagreed on 3).
- **Repair turns:** 11 used (B 5, B2 3, A 3). Six led to a solved task:
  - B #6, #8 and #11;
  - B2 #8;
  - A #1 and #5.

  Five did not: B and B2 #3 (still unparsable), B and B2 #9 (applied but wrong), and A #11 (applied
  but wrong).
- **Compared with probe 2** (same tasks; not an independent comparison):
  - Opus went from 7 to 9. The two tasks it lost to the 12K limit in probe 2 (#3 and #9) are solved
    at 32K without a repair. It fails #4, #10 and #11 in both probes.
  - Haiku sample 1 went from 5 to 8. #1, #5 and #10 are newly solved without any repair (run-to-run
    variation; its `max_tokens` was 32K in both probes). #6, #8 and #11 were solved in both probes,
    this time only after a repair turn. It fails #3, #4, #7 and #9 in both probes.
- **Paired view (A vs B):** Opus solves 3 of Haiku's 4 failures (#3, #7, #9), and Haiku solves 2 of
  Opus's 3 failures (#10, #11). Both fail #4. The net gap is 3 − 2 = +1.

## Pre-registered decision (decided on all failures, after repair)

Rule: **GO** if Haiku (B) fails ≥ 4/12 **and** Opus solves ≥ 3 more than Haiku **and** Opus solves
≥ 6/12.

- Haiku fails 4/12, at least 4: met.
- Opus − Haiku = 9 − 8 = **+1**, below 3: **not met**.
- Opus solves 9/12, at least 6: met.

**Verdict: NO-GO.** On logic-only failures it would be NO-GO too: Haiku and Opus each fail 3.

With the edit-format problems fixed, the single-shot gap between Opus 5.5 at low effort and Haiku
5.5 at medium effort on these 12 hard tasks is one task (9 vs 8). This is not because Opus fixes
nothing: it solves three of Haiku's four failures. But Haiku solves two tasks that Opus fails, so the
discordant pairs are 3 to 2. The owner decides what follows; nothing beyond the probe was started.
