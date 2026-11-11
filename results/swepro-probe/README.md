# SWE-bench Pro probe (2026-11-11)

**These are pilot measurements on 12 tasks.** They feed the pre-registered GO/NO-GO decision; they
are not study results.

- Run: `python -m contraprova.harness.run --benchmark swepro --probe --client anthropic --budget-usd 2.5
  --log runs/swepro-probe/log.jsonl --delete-images`.
  - Arms: B and B2 are two independent samples of Haiku 5.5, and A is Opus 5.5 at low effort.
  - `max_tokens` is 16K for Haiku and 12K for Opus, in the oracle-files setting of `docs/swepro.md`.
  - Tasks ran in ascending order of prompt size.
- `log.jsonl`: `contraprova verify` gives 216 events, chain intact, replay clean.
  `partition.json`: sha256 `80ff79d67134f595df6f28c8b2b1235c4e70c7207577fb1cf001c0da279ad053`.
  `summary.json` is the run summary; `per_task.json` has per-task tokens, cost, parse errors and the
  official reward.
- Pre-flight, from free `count_tokens` calls: expected $1.9589 (× 1.2 = $2.35, within the $2.50 cap);
  worst case $4.0074. The budget guard never stopped the run.
- The verifier ran 35 times; wall time was 26.5 min. Every image was deleted after its task
  (`/var/lib/docker` is 1.1 MB afterwards), and the daemon the run started was stopped.
- Licence note: the log embeds repository file contents and patches from the upstream projects
  (GPL-3.0 / AGPL-3.0; see `docs/swepro.md`). Publishing it redistributes that content under those
  licences.

## Spend (recomputed from logged tokens, `anthropic-2026-11`)

| | USD |
|---|---|
| **Total** | **$1.4556** |
| claude-opus-5-5 (arm A) | $1.3648 |
| claude-haiku-5-5 | $0.0908 |
| arm B | $0.0457 |
| arm B2 | $0.0451 |

Per task: Opus $0.1137 on average (range $0.0275–$0.2411); Haiku $0.0038 per sample.
Spend under the owner's original $5 envelope: Polyglot pilot $1.9830 plus this probe $1.4556 =
$3.4386.

## Per task

Prompt tokens are the counted input of the worker request, the same for all three arms. "Out" is
output tokens. A ✗ marks a failure (reward 0), with F2P passed/total.

| # | Task | Prompt tok | B | B2 | A (Opus low) | Notes |
|---|---|---|---|---|---|---|
| 1 | NodeBB-da0211b1 | 2,890 | ✓ | ✓ | ✓ | |
| 2 | openlibrary-58999808 | 3,494 | ✓ | ✓ | ✓ | |
| 3 | element-web-41dfec20 | 6,353 | ✗ 1/3 F2P | ✗ 0/3 F2P, 3 P2P broken | ✓ | Haiku **logic** failure (both samples) |
| 4 | ansible-29aea9ff | 9,189 | ✓ | ✓ | ✓ | |
| 5 | qutebrowser-a84ecfb8 | 16,576 | ✓ | ✓ | ✓ | |
| 6 | element-web-ad26925b | 15,883 | ✗ 3/17 F2P | ✗ 0/17 F2P, 0/184 P2P | ✓ (8,878 out, $0.24) | Haiku **edit-format** failure: both samples hit `max_tokens` 16,000 and the last block was truncated (B: "no '=======' line after SEARCH"; B2: "no '>>>>>>> REPLACE' line") |
| 7 | ansible-a20a5270 | 19,408 | ✓ | ✓ | ✓ | |
| 8 | NodeBB-f2082d7d | 24,584 | ✓ | ✓ | ✓ | |
| 9 | openlibrary-6e889f4a | 22,745 | ✗ 6/7 F2P | ✗ 6/7 F2P | ✗ 6/7 F2P | **Logic** failure in all three; the same F2P test fails |
| 10 | openlibrary-25858f9f | 26,206 | ✓ | ✓ | ✓ | |
| 11 | ansible-34db57a4 | 36,660 | ✓ | ✓ | ✓ | |
| 12 | qutebrowser-5fdc83e5 | 38,726 | ✓ | ✓ | ✓ | |

No other parse or apply errors occurred in any arm. The largest prompt was 38,726 tokens, below
Haiku's 100K price step.

## Counts

- **Haiku failures (sample 1, arm B): 3/12.** 2 are logic failures (#3, #9) and 1 is an edit-format
  failure (#6, output truncated at `max_tokens`).
- **Retry noise floor:** B2 solved **0 of B's 3 failures** and failed none of B's 9 solves, so the
  two samples agree on all 12 tasks.
- **Opus solves: 11/12**, a gap of +2 over Haiku (9/12). Opus failed only #9, which both Haiku
  samples also failed.

## Pre-registered decision

Rule: **GO** if Haiku fails ≥ 4/12 **and** Opus solves ≥ 3 more than Haiku **and** Opus solves ≥ 6/12.

- Haiku fails 3/12, below 4: **not met**.
- Opus − Haiku = 11 − 9 = +2, below 3: **not met**.
- Opus solves 11/12, at least 6: met.

**Verdict: NO-GO.** No pre-registered branch matches exactly:

- "Haiku fails ≤ 2 → re-probe on HARD-51 or larger multi-file tasks". Haiku failed 3, one above that
  threshold. Counting only logic failures, Haiku fails 2. The third failure is the edit-format
  truncation, so this branch applies on logic failures.
- "No Opus gap → Opus medium re-probe (~$1.7, needs owner approval)". The gap is +2, not zero. Opus
  already solves 11/12 at low effort, so higher Opus effort can add at most one solve. The shortfall
  is on the Haiku side (too few failures), not the Opus side.
- "Opus ≤ 5 → split edit-format from logic failures first". Does not apply (Opus 11). The split is
  reported above anyway.

The probe points to the "harder tasks" branch: re-probe on HARD-51 or larger multi-file tasks. One
more finding is relevant to any re-probe: Haiku's one edit-format failure was output truncation at
16K `max_tokens` on a 17-test, 4-file task. Raising Haiku's `max_tokens` (or splitting large tasks)
would turn it into a logic test. That is a configuration choice for the owner; nothing beyond the
probe was started.
