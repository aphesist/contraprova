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

**v0.1: the kernel.** Nothing has been measured yet. The experiment has not run.

| Part | State |
|---|---|
| Hash-chained event log with a validating single writer | Built, tested |
| Task projection: claim → submit → gate, attempt limits, done is final | Built, tested |
| Cost projection from token counts, re-priceable under any price table | Built, tested |
| Statistics: Wilson intervals, exact McNemar, paired bootstrap, Holm | Built, tested |
| Lane contracts: declared file scopes, protected test files | Built, tested |
| Reusable Claude Code orchestrator/worker configuration | Written, not yet evaluated |
| Messages API runner, forks, router, gates, Aider Polyglot harness | Planned |
| Local track (Contract Lanes on a rented GPU) | Planned |
| Replay UI | Planned |

## Honesty table

Every claim this project will make, and what kind of evidence stands behind it today.

| Claim | Status today | Evidence it will need |
|---|---|---|
| Replay is deterministic; finished tasks never change; any edit to history is detected | **Tested** (property-based, not proof) | Hypothesis runs in `tests/test_properties.py` |
| Re-pricing the history is exact and linear in the price table | **Tested** | `tests/test_pricing.py` |
| The governed swarm costs less per solved task than a single Opus at low effort | Hypothesis | Paired bootstrap CI on 90 tasks |
| Escalating to Sonnet beats a same-model Haiku retry | Hypothesis | Paired CI over about 60 forks, with the control as noise floor |
| A router learned from the log beats a fixed cascade | Hypothesis | Regret against the full fork matrix |
| Log-only (off-policy) estimates recover the true policy value | Hypothesis | Estimate minus full-matrix truth |
| Lane contracts and a test gate beat an ungated merge of the same lane outputs | Hypothesis | Shadow merge, exact McNemar |
| A Sonnet reviewer is an independent check on Haiku | Hypothesis, and probably weak | Same vendor; correlated errors expected |
| Results generalise beyond Aider Polyglot | Hypothesis | Out of scope for this study |

Rows move from *hypothesis* to *measured* only when a result with its confidence interval is committed
under `results/`.

## Quick start

```bash
pip install -e ".[dev]"
pytest

contraprova verify  path/to/log.jsonl              # check the hash chain and replay every event
contraprova tasks   path/to/log.jsonl              # task state
contraprova cost    path/to/log.jsonl --by role    # re-price recorded usage
```

Python 3.11+, no runtime dependencies.

## Use the orchestrator configuration on its own

`config/claude-code/` holds an orchestrator/worker pair for Claude Code subagents: Opus 5.5 limited to
plan, route and review (it has no edit tools), Haiku 5.5 workers bound to file-scope contracts. See
[its README](config/claude-code/README.md).

## Documents

- [Design](docs/design.md): architecture, kernel rules, what is new and what is not, the experiment
- [Related work](docs/related-work.md): ESAA, replay research, routing, verification, parallel lanes

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
