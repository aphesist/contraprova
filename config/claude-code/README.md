# contraprova-cascade (Claude Code plugin)

A governed cascade built from Claude Code subagents and a gate hook:

1. `orchestrator` (your session's model and effort; Opus 5.5 at `medium` or `high` recommended,
   Fable 5.2 also works) reads outlines, writes one brief with `scope:`, `test:` and `goal:` lines,
   and dispatches it. It has no edit or shell tools.
2. `worker` (Haiku 5.5, `medium`) does the job inside the scope and runs the test.
3. The gate (`scripts/gate.py`, hooks on the `Agent` tool) checks the files the agent changed against
   the scope, refuses changes to test files, and runs `$CONTRAPROVA_TEST_CMD` if set.
4. On a block the orchestrator escalates one tier at a time: `escalation-sonnet` (Sonnet 5.5,
   `medium`), then `escalation-opus` (Opus 5.5, `medium`), each with the brief and the previous
   failure. Fable is never used for a tier.

## Install

```bash
claude plugin install contraprova-cascade --marketplace aphesist/contraprova
```

In a session: `/plugin install contraprova-cascade --marketplace aphesist/contraprova`, or
`/plugin marketplace add aphesist/contraprova` then `/plugin install contraprova-cascade@contraprova`.

Without the plugin system, from a checkout: `pip install -e .` then `contraprova init-claude`
(or `--user`) in your project.

## Run

```bash
export CONTRAPROVA_TEST_CMD='pytest -q'      # optional, but it makes the gate run your tests
claude --agent orchestrator --model opus --effort high
```

This structure follows arm D of contraprova's SWE-bench Pro study, but the shipped configuration
(session-inherited orchestrator, Haiku → Sonnet → Opus ladder, agentic workers) has not been
measured. Details, limits and the documentation it relies on: [docs/claude-code.md](https://github.com/aphesist/contraprova/blob/main/docs/claude-code.md).
