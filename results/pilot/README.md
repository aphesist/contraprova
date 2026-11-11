# Aider Polyglot pilot: summary

> **The benchmark was too easy: Haiku 5.5 is already too good at it.**
>
> Haiku's first attempts failed the visible tests 0 times in 48. A single Haiku 5.5 solved 17 of 18
> tasks at about 1/25 of what a single Opus 5.5 cost. On this benchmark, orchestration only adds
> cost, and the question this project exists to answer (does escalating beat retrying the cheap
> model?) cannot be answered here, because Haiku almost never fails, so almost nothing gets escalated.

This is a **pilot**: 18 tasks per arm. It was run to check the stop rules and estimate per-task cost,
not to support claims. The main study moves to harder tasks. The full write-up is in
[REPORT.md](REPORT.md), and the generated analysis is in [contraprova-report.md](contraprova-report.md).

## What ran

- **Tasks:** 5 smoke and 13 pilot exercises from Aider Polyglot, 18 in all, across six languages.
  Two of the 15 pilot exercises were excluded: cpp/meetup needs Boost, and go/counter has an empty
  test suite.
- **Arms:** each of the four arms ran on the same 18 exercises.
  - **A:** a single Opus 5.5 at low effort.
  - **B:** a single Haiku 5.5.
  - **C:** an Opus orchestrator dispatching a Haiku worker and reviewing the result.
  - **D:** the governed swarm, with test gates and a router choosing between Haiku and Sonnet.
- **Spend:** **$1.98** in total ($1.982955 recomputed from the logged tokens with price table
  `anthropic-2026-11`). Opus accounted for $1.84 of it.
- **Log:** [`log.jsonl`](log.jsonl), 504 events. `contraprova verify` confirms the hash chain is intact.

## Results (n = 18 per arm, 95% intervals)

| Arm | Solved | Solve rate [Wilson CI] | Cost per task [bootstrap CI] | Total cost |
|---|---|---|---|---|
| A: single Opus 5.5 (low effort) | 18/18 | 100.0% [82.4%, 100.0%] | $0.0323 [$0.0275, $0.0372] | $0.5814 |
| B: single Haiku 5.5 | 17/18 | 94.4% [74.2%, 99.0%] | $0.0013 [$0.0011, $0.0016] | $0.0239 |
| C: Opus orchestrator + Haiku worker | 18/18 | 100.0% [82.4%, 100.0%] | $0.0469 [$0.0408, $0.0530] | $0.8435 |
| D: governed swarm | 18/18 | 100.0% [82.4%, 100.0%] | $0.0297 [$0.0265, $0.0330] | $0.5342 |

- **No arm's solve rate is distinguishable from another's.** The 18 tasks are almost all solved by
  every arm, so the intervals overlap heavily. D vs B, the only contrast with any disagreement,
  differs on one task (exact McNemar p = 1.0).
- **Cost does differ.** B costs about 1/25 of A per task ($0.0013 vs $0.0323; the exact ratio is
  1/24.3). C costs more than A, because it adds an Opus brief and an Opus review to every task.
- **Haiku first attempts:** 0/48 failed the visible tests (Wilson upper bound 7.4%). 2/48 passed the
  visible tests but failed the gate-private ones (4.2% [1.2%, 14.0%]). Only Python exercises have
  gate-private tests: elsewhere the visible and score tests are the same suite, so passing the
  visible tests means solving the task.

## Why the escalation question cannot be answered here

Stop rule 2 triggered. No fork ran in the pilot. Even counting the gate-private failures as fork
points, about 7.5 forks [2.1, 25.2] are projected over the 180 train and evaluation tasks, far
below the 40 that the escalation-value estimate needs. Three arms reach 18/18, so there is no room
left to show a quality gain, and every orchestrated arm only adds cost on top of single Haiku.

## What was fixed after the pilot

- **Prompt caching:** no request read from or wrote to the cache. The only cache breakpoint was on
  system prompts of 50 to 105 tokens, below the 512-token minimum, so the system prompts were
  silently not cached, and no cache-write premium was paid either. Requests now put a breakpoint on
  long exercise prefixes that a later request with the same model repeats (arm C's brief and review,
  arm D's two Haiku fork controls). See [docs/design.md](../../docs/design.md).
- **T2.review:** with no reviewer configured, this gate would have blocked manifest edits and correct
  solutions over 200 lines, creating spurious forks. Arm D now has a Sonnet diff judge with real API
  runs, and records T2 as skipped when there is no reviewer.

These changes alter the prompts, so the hashes listed in REPORT.md under stop rule 4 no longer
apply. They were never frozen.
