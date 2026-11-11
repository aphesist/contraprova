# Event payloads (canonical, v0.2)

This is the contract between the kernel, the harness, the router, the analysis and the UI. Code that
writes events must follow it; code that reads events may rely on it. Fields marked *optional* may be
absent. Unknown extra fields are allowed and ignored.

## Two kinds of task id

- **Kernel task id** (`task_id` on lifecycle, route, fork and usage events): the id of a task in the
  log, e.g. `D:python/leap` or a fork child `D:python/leap:treatment`.
- **Pairing key** (`task_id` on `outcome.scored`): the benchmark exercise id, e.g. `python/leap`, so
  that arms can be compared task by task. `outcome.scored` also carries `log_task_id`, the kernel task
  id it scores. Anything that links an outcome back to a route or fork uses `log_task_id`.

## Lifecycle

| Event | Required | Optional |
|---|---|---|
| `task.created` | `task_id`, `title`, `scope` | `kind`, `max_attempts`, `supersedes`, `arm`, `exercise_id`, `language`, `split`, `partition_sha256`, `contract`, `prompt`, `system`, `fork_of`, `synthetic` |
| `task.claimed` | `task_id` | `worker`, `attempt` |
| `task.submitted` | `task_id` | `attempt`, `model`, `files`, `contents`, `files_sha256`, `response` (raw worker reply, used to rebuild fork prefixes), `parse_errors` |
| `gate.decided` | `task_id`, `gate`, `verdict` (`pass`/`block`), `reason` | `attempt`, `tier`, `tests_sha256`, `results` / `gates` (per-gate detail), `evidence` |

**One `gate.decided` per submission.** The task projection treats the first `pass` as final, so a run
of several gates is folded into one event (`gates.decision_payload`). Gate `reason` text is shown to
workers on retry, so it must never contain gate-private test source.

## Routing and forks

| Event | Required | Optional |
|---|---|---|
| `route.decided` | `task_id`, `action` (a model id), `propensities` (model → probability, sums to 1), `router_hash`, `features` | `arm`, `decision` (`initial`/`after_gate_failure`), `rng_seed` |
| `router.trained` | `prefix_seq`, `n_examples`, `router_hash`, `hyperparameters`, `feature_names`, `actions` | `schema_version` |
| `fork.created` | `fork_id`, `task_id` (the parent kernel task), `from_seq`, `model`, `role` (`control`/`treatment`), `seed` | `prefix_hash`, `fork_task_id` (kernel id of the child task, if one is created), `branch` (e.g. `control-1`), `arm` |

`fork_id` is unique in the log. Two controls at the same fork point differ in `seed`; their
disagreement rate is the noise floor for escalation value.

## Measurement

| Event | Required | Optional |
|---|---|---|
| `usage.recorded` | `model`, token counts (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens`) | `role` (`orchestrator`/`worker`/`reviewer`), `arm`, `task_id`, `fork_id`, `request_id`, `batch` |
| `outcome.scored`, arm outcome | `arm`, `task_id` (pairing key), `solved` | `log_task_id`, `shipped`, `split`, `model`, `evidence`, `gate_passed`, `router_action` |
| `outcome.scored`, fork outcome | `fork_id`, `task_id` (pairing key), `solved` | `log_task_id`, `arm` (the parent arm, informational), `gate_passed` |
| `outcome.scored`, gate replay | `gate_seq` (seq of the blocked `gate.decided`), `solved` (would the blocked patch have passed the score tests) | `task_id` |

Rules:

- An `outcome.scored` with a `fork_id` is a **fork outcome**. It never counts toward an arm's solve
  rate (`projections.outcomes` skips it). Fork usage carries the same `fork_id` and is likewise kept
  out of arm totals.
- Usage is written exactly once, by whoever owns the log for that run (the harness). Clients return
  usage in `Completion.usage` and do not write to the log themselves unless wrapped by
  `RecordingClient` in a context where nothing else records it.
- Dollars are never stored. Cost is always recomputed from tokens and a price table.
