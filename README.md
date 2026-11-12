# contraprova

> Escalation is a claim. *Contraprova* is the control.

An event-sourced orchestrator for cheap Claude workers, built to answer one question with numbers:
**when a cheap model fails, does escalating to a stronger model beat simply trying the cheap model
again?**

A strong model (Claude Opus 5.5, at low effort) plans, routes and reviews. Cheap workers (Claude Haiku
5.5) do narrow jobs. Every fact goes into an append-only, hash-chained log, and everything else
(task state, cost, the routing policy, the cost-quality frontier) is a projection of that log.

When a worker fails, contraprova forks the log and re-runs the task twice from the same point: once
with the same cheap model (the *contraprova*, or control) and once with a stronger one. The gap
between them is what escalation actually buys.

## Status

**v0.2: all parts built and integrated on one event schema ([events.md](docs/events.md)).** One real
API run so far: a pilot on Aider Polyglot (18 tasks per arm, $1.98).
[Pilot summary](results/pilot/README.md): **the benchmark was too easy. Haiku 5.5 is already too good
at it**, so orchestration only added cost and the escalation question could not be tested there. The
main study moves to harder tasks.

| Part | State |
|---|---|
| Hash-chained event log with a validating single writer | Built, tested |
| Task projection: claim → submit → gate, attempt limits, done is final | Built, tested |
| Cost projection from token counts, re-priceable under any price table | Built, tested |
| Statistics: Wilson intervals, exact McNemar, paired bootstrap, Holm | Built, tested |
| Lane contracts and submission gates (scope, protected tests, temp-workspace test runner) | Built, tested |
| Router fitted from the log, replayable by hash; off-policy estimators with a self-audit | Built, tested on dry-run logs |
| Messages API client (stdlib), shared budget cap, forks re-run from a logged point | Built, tested; used for the pilot (forks not yet exercised live) |
| Aider Polyglot harness, arms A–D, frozen partition, `--dry-run` with offline fakes | Built; pilot run on 18 tasks per arm |
| Aider Polyglot pilot (5 smoke + 13 pilot exercises, arms A–D) | **Run**: [results](results/pilot/README.md) |
| SWE-bench Pro harness (Python/JS subset, 327 tasks): outlines for the orchestrator, SEARCH/REPLACE edits, Docker verifier, probe pre-flight | Built, tested; scorer validated at $0 on the 12 probe tasks (empty patch 0, gold patch 1). Probe not yet run. [docs](docs/swepro.md) |
| MCP edge (`submit_intent`, `query_projection`) | Built, tested |
| Analysis and `contraprova report`: per-arm, paired, escalation value, gate quality, frontier | Built; run on the pilot log |
| Replay UI | Built; shows the real SWE-bench Pro main study (replay check 30/30), with the synthetic demo one link away. [docs](docs/ui.md) |
| Reusable Claude Code orchestrator/worker configuration | Written, not yet evaluated |
| Local track (Contract Lanes on a rented GPU) | Planned |

## Honesty table

Every claim this project will make, and what kind of evidence stands behind it today.

| Claim | Status today | Evidence it will need |
|---|---|---|
| Replay is deterministic; finished tasks never change; any edit to history is detected | **Tested** (property-based, not proof) | Hypothesis runs in `tests/test_properties.py` |
| Re-pricing the history is exact and linear in the price table | **Tested** | `tests/test_pricing.py` |
| On Aider Polyglot, single Haiku 5.5 matches the orchestrated arms at ~1/25 of Opus's cost | **Measured (pilot, n=18; benchmark at ceiling)** | [results/pilot](results/pilot/README.md): Haiku 17/18 (94% [74%, 99%]) vs 18/18 for A, C, D; $0.0013 [$0.0011, $0.0016] vs Opus $0.0323 [$0.0275, $0.0372] per task |
| The governed cascade (Opus plans from outlines, Haiku works, escalate to Opus on a gate block) solves as many tasks as a single Opus at low effort | **Measured (pre-registered, n=88 fresh SWE-bench Pro tasks)**: non-inferior within 10 points | [results/swepro-main](results/swepro-main/README.md): 83/88 vs 85/88, difference −2.3 points [−8.0, +3.4]; single Opus is near its ceiling (96.6%) |
| The governed cascade costs less per solved task than a single Opus at low effort | **Measured (pre-registered, n=88)** | $0.055 vs $0.139 per solved task, ratio 0.39 [0.30, 0.51], about 61% cheaper |
| Escalating to a stronger model beats a same-model Haiku retry | Measured but imprecise (8 forks) | Opus 6/8 vs Haiku retry 2/8 at the same fork points: +50 points [+12.5, +87.5], exact McNemar p = 0.125 |
| The shipped Claude Code plugin (session-inherited orchestrator, Haiku → Sonnet → Opus on a gate block) keeps the measured cascade's quality and cost advantage | Hypothesis: not measured; the measured cascade was a fixed Haiku → Opus under an Opus-`low` orchestrator, run by the harness | The same paired study with the plugin's configuration, run headless with logged usage |
| A router learned from the log beats a fixed cascade | Hypothesis | Regret against the full fork matrix |
| Log-only (off-policy) estimates recover the true policy value | Hypothesis | Estimate minus full-matrix truth |
| Lane contracts and a test gate beat an ungated merge of the same lane outputs | Hypothesis | Shadow merge, exact McNemar |
| A Sonnet reviewer is an independent check on Haiku | Hypothesis, and probably weak | Same vendor; correlated errors expected |
| Results generalise beyond one benchmark | Hypothesis | Out of scope for this study |

Rows move from *hypothesis* to *measured* only when a result with its confidence interval is committed
under `results/`.

## Quick start

```bash
pip install -e ".[dev]"
pytest

contraprova verify  path/to/log.jsonl              # check the hash chain and replay every event
contraprova tasks   path/to/log.jsonl              # task state
contraprova cost    path/to/log.jsonl --by role    # re-price recorded usage
contraprova report  path/to/log.jsonl              # study report in Markdown, every number with its interval

# all four arms on the pilot split, offline fakes, $0 (needs a polyglot-benchmark checkout)
for arm in A B C D; do
  python -m contraprova.harness.run --arm $arm --split pilot --polyglot PATH --log runs/pilot.jsonl \
      --budget-usd 5 --dry-run
done
```

Python 3.11+, no runtime dependencies.

## Use the cascade in your own project

`config/claude-code/` is a Claude Code plugin that rebuilds the cascade's structure from subagents
and a gate hook: an orchestrator plans from outlines and writes one brief with an explicit file scope
and test command, a Haiku 5.5 worker does the job, a gate checks the changed files and runs your
tests, and only a blocked attempt escalates, one tier at a time, to Sonnet 5.5 and then Opus 5.5.

**Prerequisites:** [Claude Code](https://code.claude.com/docs/en/overview) 2.1.292 or later, signed in
with a Claude plan or an API key. Scripted or headless use (`claude -p`) needs an API key under
Anthropic's terms. `python3` on your PATH for the gate.

**Install** (one command; or in a session, `/plugin marketplace add aphesist/contraprova` then
`/plugin install contraprova-cascade@contraprova`):

```bash
claude plugin install contraprova-cascade --marketplace aphesist/contraprova
```

Without the plugin system, from a checkout of this repository: `pip install -e .`, then in your
project `contraprova init-claude` (into `./.claude`; `--user` for `~/.claude`; existing files are
kept unless `--force`).

**First run**, from your project's root:

```bash
export CONTRAPROVA_TEST_CMD='pytest -q'   # the gate runs this after every agent; optional but recommended
claude --agent orchestrator --model opus --effort high
```

| Agent | Model | Does |
|---|---|---|
| `orchestrator` | your session's (`--model`, `--effort`) | Reads outlines, writes a brief starting with `scope:`, `test:`, `goal:`, dispatches, reads the gate, escalates. No edit or shell tools |
| `worker` | Haiku 5.5, `medium` | Works inside the scope, never edits tests, runs the test, reports a JSON block |
| `escalation-sonnet` | Sonnet 5.5, `medium` | Same contract; gets the brief and the worker's failure |
| `escalation-opus` | Opus 5.5, `medium` | Same contract; gets the brief and Sonnet's failure |

- **Orchestrator model:** Opus 5.5 at `medium` or `high` is recommended; Fable 5.2 also works. No
  tier below it ever runs Fable. The study pinned Opus at `low` only to keep its single-Opus baseline
  cheap and below its ceiling (at high effort Opus one-shots nearly every task).
- **Test command and scope:** tell the orchestrator the test command in your request, or let it find
  the narrowest one; it writes the scope into the brief. The gate runs only `$CONTRAPROVA_TEST_CMD`
  from your environment, never a command from the brief or the repository.
- **Cost:** `claude -p "..." --agent orchestrator --output-format json | jq '.total_cost_usd, .modelUsage'`
  gives Claude Code's per-model estimate, subagents included.
- **Event log:** `claude mcp add --transport stdio contraprova -- python -m contraprova.mcp_edge --log /abs/path/log.jsonl`
  lets a session propose task events into a contraprova log ([MCP edge](docs/mcp.md)).

**What this is and is not.** The plugin reproduces the cascade's structure. The measured numbers come
from this repo's harness, not from Claude Code sessions, and from a different configuration: a fixed
Haiku → Opus cascade under an Opus-`low` orchestrator. The shipped Haiku → Sonnet → Opus ladder with a
session-inherited orchestrator has not been measured. Details, the gate, limits and the Claude Code
documentation this relies on: [docs/claude-code.md](docs/claude-code.md).

## Documents

- [Design](docs/design.md): architecture, kernel rules, what is new and what is not, the experiment
- [Event payloads](docs/events.md): the one schema every module writes and reads
- [SWE-bench Pro harness](docs/swepro.md): the harder benchmark, the outline rule, edit format, scoring
- [Statistics](docs/statistics.md), [replay UI](docs/ui.md), [MCP edge](docs/mcp.md)
- [The cascade in Claude Code](docs/claude-code.md): the plugin, the gate hook, what differs from the study
- [Related work](docs/related-work.md): ESAA, replay research, routing, verification, parallel lanes
- Next step: [Contract Lanes study plan](docs/lanes-plan.md), with Haiku lanes vs cascade D on large multi-file SWE-bench Pro tasks, a shadow-merge headline and a cost estimate from measured tokens

## Budget

About US$45 of Claude API credit and about US$10–16 of rented GPU time. Every run uses an API key
with per-role keys, so token counts can be checked against Anthropic's own usage records.

## Credits

contraprova's separation of proposals from a validating single writer comes from
[ESAA](https://github.com/elzobrito/ESAA-Core) by Elzo Brito dos Santos Filho
([arXiv 2602.23193](https://arxiv.org/abs/2602.23193)), which is the more complete governance system.
See [related work](docs/related-work.md) for the rest.

## Licence

Apache-2.0.
