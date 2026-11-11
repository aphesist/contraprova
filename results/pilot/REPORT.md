# Pilot report (2026-11-11)

**Every number here is a pilot measurement on a small sample: 5 smoke + 13 pilot exercises per arm
(n = 18).** These are for the stop rules and per-task cost estimates, not for study claims.

## What ran

- Log: `results/pilot/log.jsonl`. `contraprova verify`: 504 events, chain intact, replay clean.
  Partition sha256 `97bbfea11d71fe3ecd20317add1309a1807d386c96658529e914bc5ea71e4340`
  (`partition.json`, seed 20261111, written before the first API call).
- Arms A–D over the smoke split (5) and the pilot split (15, of which 13 ran; see exclusions). All
  four arms ran on the same 18 exercises, through the `--client anthropic` path: one AnthropicClient per
  role, a shared `llm.Budget`, and the base URL pinned to api.anthropic.com.
- Models and effort: arm A is Opus 5.5 at `low` effort. The orchestrator and reviewer (arms C and D)
  are also Opus 5.5 at `low`. Haiku 5.5 and Sonnet 5.5 workers run at `medium`. Worker max_tokens
  is 8000.
- Real gates and scorer: `contraprova.harness.runners:polyglot_gates` runs `T0.scope`, the visible
  tests and the gate-private tests. `polyglot_scorer` runs the score tests.
- Arm D router: `contraprova.harness.run:UniformRouter`, which picks Haiku or Sonnet with propensity
  0.5 each. No learned router exists before the train split.
- The `contraprova report` output is in `contraprova-report.md`.

## Measured spend (recomputed from logged tokens, price table `anthropic-2026-11`)

| | Smoke (step 2) | Pilot split (step 3) | Total |
|---|---|---|---|
| All | $0.5520 | $1.4310 | **$1.9830** |

| By model | Total |
|---|---|
| claude-opus-5-5 | $1.8409 |
| claude-sonnet-5-5 | $0.0781 |
| claude-haiku-5-5 | $0.0640 |

| Arm | Total | Per task, smoke (n=5) | Per task, pilot (n=13) | Per task, all [95% bootstrap CI] (n=18) | Solved |
|---|---|---|---|---|---|
| A | $0.5814 | $0.0333 | $0.0319 | $0.0323 [$0.0275, $0.0372] | 18/18 |
| B | $0.0239 | $0.0014 | $0.0013 | $0.0013 [$0.0011, $0.0016] | 17/18 |
| C | $0.8435 | $0.0463 | $0.0471 | $0.0469 [$0.0408, $0.0530] | 18/18 |
| D | $0.5342 | $0.0294 | $0.0298 | $0.0297 [$0.0265, $0.0330] | 18/18 |

No forks ran, so arm D's shipped-branch cost equals its raw cost. Opus is 93% of all spend. In arm D
the Opus orchestrator is 82% of the dollars, and in arm C 52%.

## Haiku first-attempt failure rate on visible tests

Every Haiku first attempt in arms B, C and D counts (B and C were re-gated offline at $0 with
`results/pilot/analyze.py`; D's were gated live). Forks are excluded. n = 48: B 18, C 18, D 12. D's
other 6 initial attempts were routed to Sonnet.

| Measure | k/n | Rate [95% Wilson CI] |
|---|---|---|
| Fails visible tests (or scope) | 0/48 | 0.0% [0.0%, 7.4%] |
| Passes visible, fails gate-private | 2/48 | 4.2% [1.2%, 14.0%] |
| Fails any gate | 2/48 | 4.2% [1.2%, 14.0%] |

The two gate-private failures were B:python/forth (17/18 private tests passed) and B:python/dot-dsl
(3/4 passed). B:python/forth also failed the score tests. Only Python exercises are split at test
level: 41 of the 48 attempts were on fallback exercises, which have no gate-private tests. A
gate-private failure is therefore possible on only 7 of the 48 attempts, and 2 of those 7 failed.

**Implied forks on train + eval (90 + 90)** = 180 × rate. This assumes every initial attempt is Haiku;
a router that sends some initial attempts to Sonnet lowers it further.

- Fork point "fails a gate" (as designed, with fallback exercises gated only on visible tests):
  **7.5 forks [2.1, 25.2]**.
- Fork point "fails visible" alone: **0 [0, 13.3]**.

## Stop rules (pre-registered), verdicts

1. **A's cost per task > $0.20 → cut A to n=60.** A measured **$0.0323 per task** [$0.0275, $0.0372]
   (n=18). **Not triggered**: A stays at its planned n.
2. **Haiku first-attempt visible-test failure < 15% → widen fork points to "passes visible, fails
   gate-private"; if projected forks on train+eval < 40 → SWE-bench fallback for the EV study.**
   Visible failure is **0/48 (0%, upper 95% bound 7.4%)**, so the first condition is **triggered**.
   With widened fork points, the projection is **7.5 forks [2.1, 25.2]**. Even the upper bound is
   below 40, so the second condition is also **triggered**. **The rule calls for the SWE-bench
   fallback for the escalation-value study.** Aider Polyglot is too easy for Haiku 5.5 at medium effort
   to produce enough fork points. This design change is for the owner to make; I have not changed the
   design.
3. **Protocol violation (orchestrator edits code, or any Haiku request > 100K prompt tokens) → fix and
   re-pilot.** **No violation.** Every `task.submitted` came from a `worker:` actor, and orchestrator
   and reviewer completions are never parsed as files. The largest prompts were Haiku 6,572,
   Opus 7,108 and Sonnet 4,276 tokens. No Haiku request exceeded 100K.
4. **Freeze and hash configs, prompts and the price table before a main run.** These are the hashes I
   would freeze (sha256):
   - Prompts (the eight prompt constants in `harness/arms.py`, canonical JSON):
     `fc2d6834ac229fed1fe98502eec84bdfc839922944bbb1d230253a4a3accd67f`
   - Price table `anthropic-2026-11` (canonical JSON):
     `2857c0b5d7bfd1ab25f7c01b8e6bcf5345e45831aa86110338876f1be30096ef`
   - `ArmConfig()` (canonical JSON): `385f801a69358ca8c64e99c58a0b7183fbcbe70ec6843befb1ae959430c43d59`
   - `src/contraprova/harness/arms.py`: `0c4451f4a3431cd3963c1ba420ab453df0b1a330f0355074e672ab0ded775627`
   - `src/contraprova/harness/runners.py`: `5678305a3f97e9197b449059eb9babea0fbf3ac90c8ef175f69b8d45a0b2170a`
   - `config/claude-code/agents/orchestrator.md`: `41df20f3c755cae843c9d80e8ecdd4377f959c83fc3d69fb5b18f13c2415c1f2`
   - `config/claude-code/agents/worker.md`: `1e4edb7d55e2a6267419c7c51ee9d2a4cb0b504078697f15a8d7067f6f1817c8`
   - `config/claude-code/README.md`: `f7a2596df44e8d376fa18c4da48aeedf1de027e749336408aaf3d3c80e0b56af`
   - `partition.json` (file): `9635e0bdec6d64db36266ab5f97b4aa8e964b66c3f61ae7edf8777b04bf83345`
     (content sha256 as above)

   Nothing has been frozen yet. If the rule 2 fallback is adopted, these change.
5. **Spend caps respected.** The smoke step spent $0.5520 against its $1.50 cap, and the whole pilot
   $1.9830 against $5.00. The budget guard never stopped a run.

## Languages and exercises excluded

- No language was excluded: all six toolchains run in this container.
  - Java runs as `javac` plus the JUnit console launcher, with three jars from Maven Central
    (`CONTRAPROVA_JAVA_TOOLCHAIN`). The Gradle wrapper was not used because it downloads a
    ~130 MB distribution.
  - JavaScript uses one shared jest 29 install, linked into every workspace (`CONTRAPROVA_JS_TOOLCHAIN`).
- **cpp/meetup** (pilot) was excluded because it needs Boost `date_time`, which is not installed.
  2 of the 26 C++ exercises need it. Design finding: the main run needs Boost, or these
  exercises must be dropped by rule.
- **go/counter** (pilot) was excluded because it is a deprecated "write the test suite" exercise whose
  test file is empty, so any submission passes, including the stub. Design finding: drop it from the
  dataset, or treat any exercise whose runner discovers 0 tests as invalid.

## Polyglot test-count check (smoke + pilot)

The heuristic count in `harness/polyglot.py` was compared with each language's real runner on the
reference solution (`check_counts.py`, output in `test_counts.json`):

- **Matches (18 of 20):** go/react 13, java/protein-translation 31, javascript/food-chain 10,
  python/forth 54, rust/variable-length-quantity 26, cpp/perfect-numbers 13, go/bowling 31,
  go/poker 40, java/go-counting 11, java/palindrome-products 12, java/twelve-days 15,
  javascript/forth 49, javascript/resistor-color-trio 6, javascript/word-search 22, python/dot-dsl 12,
  python/transpose 12, rust/pig-latin 22, rust/say 19.
  - cpp/perfect-numbers shows a failure in `test_counts.json` because that file predates the
    workspace-name fix (Exercism's CMake files take the project name from the directory). It was
    re-run afterwards and matches.
  - rust/pig-latin was counted on the stub, because its reference solution needs the `regex` crate
    and the runner is offline.
- **go/counter:** heuristic 0, and the runner also finds 0 tests (degenerate exercise; see above).
- **cpp/meetup:** heuristic 90, not verified because Boost is missing.

No mismatch between the heuristic and the runner was found where the runner could run.

## Protocol violations

None (see rule 3).

## Other findings for the owner

- **T2.review blocks in arm D with no reviewer configured.** `run_gates` adds `T2.review` for
  manifest edits or changes over 200 lines, and with no reviewer it blocks. go/poker's reference
  solution (288 lines) trips it. It never fired in the pilot, but on train/eval it would turn correct
  large solutions into gate failures and spurious forks. Rust lists `Cargo.toml` as a solution file,
  so a worker that returns it unchanged would also trigger T2.
- **Rust runs offline**, so a solution that adds a crate fails to build. rust/pig-latin's own
  reference solution is an example.
- **No prompt caching took effect:** the cache-read share was 0% in every arm. The cached system
  prompts are short, probably below the minimum cacheable length, so cache-write premiums may be paid
  for nothing. This is small at these prompt sizes.
- **The score tests are not hidden on fallback exercises** (every non-Python exercise): visible and
  score are the same full suite, so visible-pass essentially implies solved. This and the ceiling
  effect (A, C and D at 18/18) are why the rule 2 fallback triggers.

## Design changes the rules call for

- **Rule 2: move the escalation-value study to the SWE-bench fallback.** Polyglot projects 7.5
  [2.1, 25.2] fork points on train+eval, against the 40 required.
- Rules 1, 3 and 5 call for no change. Rule 4 is the freeze to do before any main run.

No full run was started.
