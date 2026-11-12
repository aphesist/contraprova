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
| `task.created` | `task_id`, `title`, `scope` | `kind`, `max_attempts`, `supersedes`, `arm`, `exercise_id`, `language`, `split`, `partition_sha256`, `contract`, `prompt`, `system`, `fork_of`, `synthetic`; SWE-bench Pro adds `benchmark` (`swepro`), `repo`, `image`, `base_commit`, `worker_prompt_tokens_est`, `lead_prompt_tokens_est`, `outline_tokens_est`, `prompt_tokens_exact` (model → count from the pre-flight) |
| `task.claimed` | `task_id` | `worker`, `attempt` |
| `task.submitted` | `task_id` | `attempt`, `model`, `files`, `contents`, `files_sha256`, `response` (raw worker reply, used to rebuild fork prefixes), `parse_errors`; SWE-bench Pro adds `diff` (unified diff against the base files), `edit_blocks` (blocks parsed), `changed_lines`, `contents_sha256` (instead of `contents` with `--no-log-contents`), and the repair turn's `repair_used`, `original_failure` / `final_failure` (`truncated`, `parse`, `apply` or null), `original_errors`, `stop_reason`, `repair_stop_reason`, `repair_response`, `repair_blocks` |
| `gate.decided` | `task_id`, `gate`, `verdict` (`pass`/`block`), `reason` | `attempt`, `tier`, `tests_sha256`, `results` / `gates` (per-gate detail), `evidence` |

On SWE-bench Pro, `contents` holds the full new content of each changed file only, and the gate names
include `T0.edits` (every SEARCH/REPLACE block applied); see [swepro.md](swepro.md). Arm `B2` is a
second, independent single-Haiku sample (the probe's retry noise floor). A worker's repair request is
a `usage.recorded` with the same tags plus `repair: true`, so it counts toward that arm's cost.

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

### `route.decided` features

`features` is exactly the output of `router.featurize(record)`: its keys are `router.FEATURE_NAMES`
(`language`, `stub_size`, `visible_fail_frac`, `error_class`, `diff_size`, `tamper`, `control_agree`,
`attempt`), each a number in [0, 1]. Writers build `record` from the documented input keys and nothing
else; the harness does it in `harness.arms.route_record`:

| Input key | `initial` decision (before the attempt) | `after_gate_failure` decision (from the blocked attempt) |
|---|---|---|
| `language` | the exercise language | same |
| `stub_lines` | lines in the solution stubs | same |
| `visible_tests` | visible tests in the split | the visible-test gate's counted total, if it ran and reported counts; else as before |
| `visible_failures` | 0 | failed + errors reported by the visible-test gate, if available; else 0 |
| `error_class` | `none` | from the first blocking gate result: `timeout` (test run timed out), `compile` (reason or test output shows the code did not build or load), `assert` (failing tests counted, or an assertion in the text), else `other` (scope, review, unrecognised) |
| `diff_lines` | 0 | lines changed by the blocked attempt against the stubs |
| `tamper` | false | the attempt wrote a protected path (contract check or a scope gate's `protected`/`test_tampered` violation) |
| `control_agree` | unknown (encoded 0.5) | unknown: no control has run when the router decides |
| `attempt` | 1 | 2 |

The synthetic demo log (`scripts/make_demo_log.py`) uses the same featurizer with made-up inputs.

## Contract Lanes (arm La)

Arm La on SWE-bench Pro ([lanes-plan.md](lanes-plan.md), code `harness/lanes.py`) adds five event
types to `events.EVENT_TYPES`. The parent task is `La:<instance_id>` (kind `code`; its attempts are
merge rounds); each lane is a kernel task `La:<instance_id>:<lane_id>` with `kind: "lane"`, `parent`,
`plan_id`, `lane_id`, `contract` and `max_attempts` = the ladder length (4), so attempt n runs ladder
step n (`haiku`, `haiku-repair`, `sonnet`, `opus`) and the projection's attempt limit enforces it.

| Event | Required | Optional |
|---|---|---|
| `lane.planned` | `task_id` (the parent), `plan_id`, `model`, `k`, `lanes` (list of {`lane_id`, `allowed`, `new_files`, `goal`, `symbols`, `exports`, `imports`, `tests`}), `plan_sha256` | `parse_errors`, `repair_used`, `repair_errors`, `fallback` (`single_lane` when the plan stayed invalid), `rationale`, `warnings`, `stop_reason`, `response` / `repair_response` (the planner's replies; left out with `--no-log-contents`) |
| `interface.frozen` | `task_id`, `plan_id`, `interfaces` (symbol → signature), `interfaces_sha256` | `source` (list of `planner` / `task_interface`), `entries` (each symbol's `signature`, `path`, `owner`, `source`, `kind`), `board` (lane_id → {`provides`, `needs`}): the interface round, written in one kernel step |
| `interface.requested` | `task_id` (the lane task), `plan_id`, `lane_id`, `symbol`, `requested_signature`, `status` (`rejected`) | `reason` |
| `merge.decided` | `task_id`, `plan_id`, `round`, `mode` (`governed`/`shadow`), `lanes` (lane_id → seq of its `task.submitted`), `merged_sha256`, `verdict` (`pass`/`block`) | governed: `blamed`, `blame_rule` (1 direct, 2 traceback/test file, 3 all lanes), `blame_basis`, `gate_seq` (the merged `gate.decided`), `tiers` (lane_id → ladder step), `conflicts`; shadow: `gated` (false), `files`, `errors` |
| `coordination.detected` | `task_id` (the parent), `plan_id`, `kind`, `lanes` | `round`, `mode` (`plan`/`governed`/`shadow`), `evidence`, `mast` |

`coordination.detected` kinds: `overlapping_scope`, `scope_violation`, `interface_mismatch`,
`interface_change_request`, `overwrite` (incl. `reverted: true` in the shadow merge),
`duplicated_work`, `unattributable_failure`, `wasted_lane`, `regression_after_escalation`
(definitions in `harness/lanes_detect.py`).

The plan's proposed `lane.submitted` was not added: its fields went onto the lane's `task.submitted`
as optional fields instead (`plan_id`, `lane_id`, `tier`, `round`, `rejected_blocks`). Existing
types carry the rest:

- the merge is a `task.claimed` / `task.submitted` (`model: "merge"`, `merge: "governed"`, `round`,
  `lanes`) / `gate.decided` (gates `T0.scope`, `T0.edits`, `T0.interfaces`, `T0.visible_tests`,
  `T1.private_tests`, `T2.review`) on the parent task;
- a blamed lane gets `gate.decided` (`gate: "lane.blame"`, `merge_gate_seq`, `blame_rule`) and, at
  the end, every lane `lane.merged` (pass) or `lane.unsolved` (block);
- each escalation is a `route.decided` with `decision: "lane_escalation"`, `action` the next tier's
  model, `propensities` {that model: 1.0} (a fixed policy), `step`, and the usual `features`;
- every blocked merge is replayed on the score tests (`outcome.scored` with `gate_seq`);
- usage carries `arm: "La"`, `role` (`orchestrator` for the planner, `worker` for lanes), the
  lane's kernel `task_id`, `lane_id`, `tier`, and `lane_role: "planner"` / `repair: true` where they
  apply. All of it is La's cost: no fork rule applies;
- derived outcomes `La-shadow`, `La-first` and `La-sonnet` are arm outcomes with
  `derived_from: "La"` and `merge` (`shadow`/`first`/`sonnet`); they have no usage of their own.

## Measurement

| Event | Required | Optional |
|---|---|---|
| `usage.recorded` | `model`, token counts (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_5m_tokens`, `cache_write_1h_tokens`) | `role` (`orchestrator`/`worker`/`reviewer`), `arm`, `task_id`, `fork_id`, `request_id`, `batch` |
| `outcome.scored`, arm outcome | `arm`, `task_id` (pairing key), `solved` | `log_task_id`, `branch_task_id` (kernel id of the fork branch shipped, if any), `branch`, `shipped`, `split`, `model`, `evidence`, `gate_passed`, `router_action`, `derived_from` (the arm whose attempt this derived arm scores, e.g. `B` from `D`; it has no usage of its own) |
| `outcome.scored`, fork outcome | `fork_id`, `task_id` (pairing key), `solved` | `log_task_id`, `arm` (the parent arm, informational), `gate_passed` |
| `outcome.scored`, gate replay | `gate_seq` (seq of the blocked `gate.decided`), `solved` (would the blocked patch have passed the score tests) | `task_id` |

Rules:

- An `outcome.scored` with a `fork_id` is a **fork outcome**. It never counts toward an arm's solve
  rate (`projections.outcomes` skips it). Fork usage carries the same `fork_id`.
- **Fork cost rule.** An arm outcome that shipped a fork branch names it in `branch_task_id` (the
  branch's kernel task id, equal to that fork's `fork_task_id`). That one fork's usage is part of the
  arm's cost: an arm's cost per task is its own usage (no `fork_id`) plus the usage of the branch it
  shipped (`projections.shipped_forks`). All other fork usage (controls, an unshipped treatment) is
  **measurement overhead**: it exists only to measure escalation value, is in no arm's cost, and is
  reported separately (by parent arm and fork role; `contraprova cost --by arm` shows it as
  `<arm>:measurement`). Budget guards count all spend. See [statistics.md](statistics.md).
- Usage is written exactly once, by whoever owns the log for that run (the harness). Clients return
  usage in `Completion.usage` and do not write to the log themselves unless wrapped by
  `RecordingClient` in a context where nothing else records it.
- Dollars are never stored. Cost is always recomputed from tokens and a price table.
