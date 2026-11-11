# Design

> Escalation is a claim. *Contraprova* is the control.

In Brazilian Portuguese a *contraprova* is the confirmatory re-test: you do not trust a result until
it has been checked again under the same conditions. This project applies that idea to multi-agent
orchestration.

## The question

Cheap models fail. When a Claude Haiku 5.5 worker fails a task, the usual move is to escalate it to a
stronger model. But a failed attempt is also just one sample: a second Haiku attempt might have
passed too. Published routers and cascades compare "escalate" against "give up", almost never against
"try the cheap model again". So nobody knows what escalation actually buys.

contraprova measures it. When a worker fails, the log is forked at that point and the task is
re-executed both ways: by the same cheap model again (the control) and by a stronger model (the
treatment). The difference in solve rate, with a confidence interval, is the **escalation value**.

## Architecture

```
            plan / brief / review                       typed intents
  Opus 5.5 ─────────────────────────┐        ┌──────── Haiku 5.5 workers
  orchestrator (low effort,         │        │         (one narrow job each)
  no edit tools)                    ▼        ▼
                              ┌──────────────────────┐
     router (a projection) ──▶│  single writer       │──▶ gates (scope, protected tests,
                              │  validates, appends  │     visible tests, private tests)
                              └──────────┬───────────┘
                                         ▼
                           append-only, hash-chained event log
                                         │
          ┌──────────────┬───────────────┼───────────────┬───────────────┐
          ▼              ▼               ▼               ▼               ▼
     task state     cost (tokens ×   Pareto frontier   router fit     replay UI
                    price table)
```

Everything readable is a **projection**: a pure function of the log. Replaying the log reproduces
every view exactly, including the router.

### Kernel rules

- **Workers propose, the kernel decides.** Workers never write to the log or the repository directly.
  A single writer checks each proposed event against the current state and appends it only if legal.
- **Hash chain on by default.** Each event commits to the previous one, so any edit, deletion or
  reordering of history is detected by `contraprova verify`.
- **Done is final.** A finished task never changes. A fix is a new task that `supersedes` it.
- **Attempt limits.** A task that fails its gate too many times becomes `failed`.
- **Tokens, never dollars.** `usage.recorded` stores token counts from the API response. Dollars are a
  projection over a versioned price table, so the whole history can be re-priced without re-running.
- **The orchestrator is mechanically limited.** It has no edit or shell tools. During measured runs it
  also cannot choose a worker's model: the router does, so routing probabilities stay valid.

### Event vocabulary

The payload of every event, required and optional fields, is defined once in
[events.md](events.md); code that writes events follows it and code that reads them relies on it.

| Event | Meaning |
|---|---|
| `task.created` | A job with its file scope; may `supersedes` a finished task |
| `task.claimed` | A worker takes the job |
| `task.submitted` | A worker proposes a result |
| `gate.decided` | The gates pass or block the submission (one decision per submission) |
| `route.decided` | Which model gets the job, with the probability of each choice and the router's hash |
| `router.trained` | The log prefix a router was fitted on and its weight hash, so the fit can be replayed |
| `fork.created` | A counterfactual re-run from a past point, marked control or treatment, with its own `fork_id` |
| `usage.recorded` | Tokens for one API request, tagged with role, arm, task and, for forks, `fork_id` |
| `outcome.scored` | Whether an arm (or a fork, by `fork_id`) solved a task on the hidden score tests, or whether a blocked patch would have passed (gate replay) |

### Prompt caching

In the pilot no request read from the cache, and none wrote to it either (`results/pilot/log.jsonl`:
zero cache-read and zero cache-write tokens on all 126 requests). The cause was where the cache
breakpoint sat. The client put `cache_control` only on the system prompt, and every system prompt is
50 to 105 tokens, far below the 512-token minimum cacheable prefix of Opus 5.5, Sonnet 5.5 and
Haiku 5.5. A prefix below the minimum is silently not cached, so no write premium was paid, but
nothing was saved either. The system prompts are not padded to reach the minimum.

What is worth caching is a long exercise prefix that a later request with the same model repeats
byte for byte. The harness now marks one (`Message.cache_upto`, a second `cache_control` breakpoint
at the end of that prefix) only where such a repeat is planned, because a cache write costs a premium:

- **Arm C:** the orchestrator's brief and the review are both Opus requests. They now share one
  system prompt (`LEAD_SYSTEM`) and one exercise context (`LEAD_CONTEXT`: instructions, starting
  files, visible tests), and only the request at the end differs. The brief writes the prefix and
  the review reads it.
- **Arm D forks:** the two Haiku controls send identical requests, so the first writes the whole
  prompt and the second reads it. The Sonnet treatment runs once and marks nothing.
- **Arms A and B, arm D's brief and first attempt:** one request per model, so nothing is marked.

Prompts keep their variable part last: the planner's brief comes after the visible tests, and a
retry's feedback after the first prompt. `tests/test_harness.py` checks that system prompts are
byte-identical across tasks and that the shared prefixes match byte for byte. Caches are per model,
so the cross-model handoffs (Opus brief to Haiku worker, Haiku attempt to Sonnet treatment) cannot share.

## What is new, and what is not

**Inherited ideas (see [related work](related-work.md)):** the separation of proposals from a
validating single writer, replayable projections, done-immutability, scoped writes and attempt limits
all come from ESAA, which remains the more complete governance system. contraprova is its own small
kernel, not a fork of ESAA.

**New in contraprova:**

1. **Control-corrected escalation value.** Every escalation is compared with a same-model retry from
   the same fork point. Forks are re-executed, never stitched together from recorded outputs, because
   stitching mispredicts outcomes once the model changes.
2. **A router that is a projection.** The router is a small model fitted from the log with a fixed
   seed. Its weight hash is written into each routing decision, and replaying the log must reproduce it.
3. **Self-audited off-policy evaluation.** Routing choices are logged with their probabilities and a
   share of tasks is routed at random. On the evaluation set, where every alternative was also run,
   the report shows how far the log-only estimates are from the truth.
4. **A re-priceable cost-quality frontier.** Cost per solved task and the Pareto frontier can be
   recomputed under any price table.

**Supporting craft (built, but not claimed as novel):** gates whose depth grows with how much an edit
touches; read-only tests with tamper detection; property-based tests of the kernel; MCP only at the
edges, as a thin façade over `submit_intent` and `query_projection`.

## Contract Lanes (local track)

A separate track runs a local open model on a rented GPU. A planner splits one job into lanes, and each
lane works under a **LaneContract**: the files and symbols it may change, the interfaces it must keep,
and protected paths it may never touch. Lanes agree on interfaces on a shared blackboard, implement in
parallel, and are merged only if tests pass.

The headline local measurement is the **shadow merge**: the same lane outputs joined with and without
contracts and the test gate. Because the tokens are identical, the difference isolates what governance
is worth. The pattern of planning and fanning out over a shared prefix is credited to
Skeleton-of-Thought and TensorFold; no code from either is used.

## Experiment

The benchmark is Aider Polyglot (225 exercises in six languages, scored by unit tests). Its tasks are
small enough that every counterfactual fork is cheap to re-execute. Exercises are partitioned with a
fixed seed before any pilot: 5 smoke, 15 pilot, 90 router training, 90 evaluation, 25 reserve. Tests
are split into visible (seen by workers), gate-private (seen only by the governed arm's gate) and score
(seen by no model and no gate).

| Arm | Setup |
|---|---|
| A | Single Opus 5.5 at low effort (the fair strong baseline) |
| B | Single Haiku 5.5 |
| C | Ungoverned swarm: Opus orchestrator dispatching Haiku workers |
| D | Governed swarm with learned escalation |
| L1, La, Lb, Lc | Local track: single local worker, Contract Lanes in separate contexts, Contract Lanes on a shared blackboard, local worker escalating to Haiku |

**T2.review in arm D.** The gates add `T2.review` when a submission touches a manifest file
(`Cargo.toml`, `go.mod`, `package.json`, ...) or changes more than 200 lines. Rust lists `Cargo.toml`
as a solution file, and some correct reference solutions are over 200 lines (go/poker is 288). If T2
blocked whenever no reviewer was configured, those correct submissions would become gate failures and
spurious forks. So:

- With `--client anthropic`, arm D configures a cheap diff judge (`Harness.t2_reviewer`): Sonnet 5.5
  at low effort, called through the same injected client, so the call is budgeted and recorded with
  role `reviewer` and, in a fork branch, its `fork_id`. It sees the unified diff against the starting
  files and a short rubric. It blocks only for a dependency an offline build cannot fetch, a broken
  build or manifest, weakened tests, or code unrelated to the exercise, and leaves correctness to the
  tests. Its reply must be exactly `PASS` or `BLOCK: <reason>`. Any other reply blocks, with
  `parse_error` in the evidence. `--no-t2-review` turns it off, and dry runs never configure it.
- With no reviewer, `runners.polyglot_gates` records T2 as skipped: a pass whose evidence says
  `status: skipped`, so the missing check is in the log but never causes a fork. The library default
  in `gates.run_gates` still blocks (`missing_reviewer="block"`).

All arms see the same tasks, so comparisons are paired: exact McNemar tests and paired bootstrap
intervals, with Holm correction across the primary contrasts. At 90 tasks a solve rate carries about
±10 points of uncertainty, and a paired comparison detects differences of about 16 points.

**Budget:** about $45 of API credit and about $10 of GPU rental for the base plan (about $16 with a
stretch goal). A pilot with pre-registered stop rules replaces every per-task estimate before the main
run. Configurations, prompts and the price table are frozen and their hashes committed before then.
