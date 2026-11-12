# The cascade in Claude Code

`config/claude-code/` is a Claude Code plugin, `contraprova-cascade`, that rebuilds the structure of
the governed cascade (arm D in [the SWE-bench Pro study](../results/swepro-main/README.md)) out of
Claude Code subagents and hooks. This page covers how it works, how to configure it, and where it
differs from what was measured. The short version is in the [README](../README.md#use-the-cascade-in-your-own-project).

## What was measured and what is shipped

| | Measured (results/swepro-main, n=88) | Shipped plugin |
|---|---|---|
| Orchestrator | Opus 5.5 at `low`, outlines only, one brief | The session's model and effort (`model: inherit`, no `effort`); reads with Grep/Glob/Read, writes one brief |
| Worker | Haiku 5.5 at `medium`, single shot plus one repair turn, SEARCH/REPLACE edits | Haiku 5.5 at `medium`, an agentic loop with Read/Edit/Write/Bash, runs the test command itself |
| Gates | Scope, edits applied, visible tests, private tests, large-diff review, run by the harness in an offline container | Scope, protected test files and `$CONTRAPROVA_TEST_CMD`, run by a hook on your machine |
| On a block | One step: Haiku → Opus 5.5 at `low` | Stepwise: Haiku → Sonnet 5.5 → Opus 5.5, all at `medium` |
| Cost accounting | Recomputed from logged tokens | `--output-format json` estimates; optional event log via the MCP edge |

**The shipped configuration has not been measured.** The study's numbers (non-inferior to a single
Opus within 10 points, about 0.39× the cost per solved task) belong to the harness setup in the
left column. The plugin keeps the structure (cheap worker under a contract, an independent gate,
escalation only on a block) but changes the orchestrator, the escalation ladder and the worker's
loop. Treat it as the same idea, not the same result.

Why the study pinned the orchestrator to Opus `low`: arm A, the baseline, was a single Opus 5.5 at
`low`. At higher effort Opus solves nearly every task in one shot, so the comparison would have had
no headroom, and an expensive orchestrator would have hidden whether the cascade saves anything.
In your own work you are not running a controlled comparison, so pick the orchestrator you trust:
**Opus 5.5 at `medium` or `high` is recommended; Fable 5.2 also works as the orchestrator.** Fable is
never used for the worker or the escalation tiers (see [Guarantees](#guarantees-and-their-limits)).

## The agents

| Agent | Model | Effort | Tools | Job |
|---|---|---|---|---|
| `orchestrator` | inherit (session) | inherit (session) | `Agent(worker, escalation-sonnet, escalation-opus)`, Read, Grep, Glob | Plan from outlines, write one brief, dispatch, read the gate verdict, escalate one tier per block |
| `worker` | `claude-haiku-5-5` | `medium` | Read, Edit, Write, Grep, Glob, Bash | Do the brief inside its scope, run its test, report JSON |
| `escalation-sonnet` | `claude-sonnet-5-5` | `medium` | same as worker | Take over after the worker is blocked, with the failure output |
| `escalation-opus` | `claude-opus-5-5` | `medium` | same as worker | Take over after Sonnet is blocked |

The orchestrator has no edit or shell tools, so it cannot do the work itself. The worker and
escalation agents have no `Agent` tool, so they cannot delegate further.

The brief always starts with three lines, which the gate parses:

```
scope: src/parser.py, src/lexer/*.py
test: pytest -q tests/test_parser.py
goal: Accept trailing commas in argument lists.
```

Escalation rule: one attempt per tier, worker → `escalation-sonnet` → `escalation-opus`, stop at the
first pass, each step gets the same brief plus the previous tier's failure. The only time a tier is
retried is a pure scope, protected-file or contract violation, which is re-dispatched to the same
tier once before moving up. If Opus is also blocked, the orchestrator reports the task as failed.

## The gate

`scripts/gate.py` (standard library only) runs as two hooks on the main session's `Agent` tool:

- **PreToolUse**: for a cascade agent, refuses any per-call `model` override, then records the
  dirty files of the git working tree (path and sha256).
- **PostToolUse**: when that agent returns, works out which files it changed (so changes you had
  before the call do not count), and checks, in order:
  1. the brief has a `scope:` line (`contract`);
  2. no test file changed (`protected`, the same patterns as the SWE-bench Pro harness);
  3. every changed file matches a `scope:` glob (`scope`);
  4. `$CONTRAPROVA_TEST_CMD` exits 0 (`tests`), if you set it.

The verdict reaches the orchestrator as `additionalContext` next to the agent's result, for example:

```
contraprova gate (worker): BLOCK
ok scope: 1 changed file(s), all in scope
BLOCK tests: `pytest -q` exited 1
--- output (tail) ---
...
```

The orchestrator treats that note as authoritative over the agent's own report. The test command
comes only from your environment, never from the brief or the repository, so the hook never runs a
command a model or a cloned repository chose. Without `CONTRAPROVA_TEST_CMD` the gate still checks
scope and protected files, and the orchestrator falls back to the agent's JSON report for tests.

Settings: `CONTRAPROVA_TEST_CMD` (shell command, run from the repository root) and
`CONTRAPROVA_TEST_TIMEOUT` (seconds, default 540; the hook's own timeout is 600). Scope globs are
repository-relative; `*` also matches `/`, and an entry ending in `/` matches everything under it.

Dispatch one agent at a time (the orchestrator's prompt says so): the snapshot is per tool call,
but agents running in parallel would see each other's edits.

## Install

### Plugin (recommended)

```bash
claude plugin install contraprova-cascade --marketplace aphesist/contraprova
```

or, inside a session, `/plugin marketplace add aphesist/contraprova` then
`/plugin install contraprova-cascade@contraprova`. To try it from a checkout without installing:
`claude --plugin-dir ./config/claude-code --agent orchestrator`.

Plugin agents get scoped names (`contraprova-cascade:worker`); `claude --agent orchestrator` finds a
plugin agent by its bare name. The plugin ships the gate as plugin hooks (`hooks/hooks.json`), which
are active in every session while the plugin is enabled but only act on calls to the three cascade
agents.

### Without the plugin system

From a checkout of this repository:

```bash
pip install -e .
cd /path/to/your/project
contraprova init-claude            # ./.claude/agents and ./.claude/hooks/contraprova-gate.py
contraprova init-claude --user     # $CLAUDE_CONFIG_DIR or ~/.claude, for all projects
```

Existing files are kept unless you pass `--force`; `--no-gate` skips the hook. Plain agent
directories cannot carry `hooks.json`, so `init-claude` writes the gate into the orchestrator's
frontmatter `hooks`, which Claude Code runs when the orchestrator is the main session (`--agent`).
For a project install Claude Code runs those hooks only after you accept the workspace trust dialog
for the folder. Install one way or the other, not both, or the gate runs twice.

`init-claude` needs the checkout because the agent files live in `config/claude-code/`, outside the
Python package; a non-editable `pip install` cannot find them (use `--source` or the plugin).

## Run it

```bash
export CONTRAPROVA_TEST_CMD='pytest -q'
claude --agent orchestrator --model opus --effort high
```

`--model` and `--effort` (or `/model` and `/effort` in the session) set the orchestrator's model and
effort; the other agents keep their own. To make the orchestrator the default for a project, set
`"agent": "orchestrator"` in `.claude/settings.json`.

### Change an agent's effort or model

Edit the `effort:` line of `worker.md`, `escalation-sonnet.md` or `escalation-opus.md` (`low`,
`medium`, `high`, `xhigh`, `max`, as the model supports). Plugin files are managed by Claude Code, so
to customise a plugin install, disable the plugin, run `contraprova init-claude` and edit the copies.
`CLAUDE_CODE_EFFORT_LEVEL` overrides every agent's `effort` at once (and the session's own).

### Headless runs and cost

Scripted use (`claude -p`) needs an API key: Anthropic's terms do not allow driving a subscription
sign-in from scripts. Then:

```bash
claude -p "Fix the failing date parser test" --agent orchestrator --model opus --effort high \
  --output-format json --max-budget-usd 2 > run.json
jq '.total_cost_usd, .modelUsage' run.json
```

`modelUsage` breaks spend down by model, subagents included. Both figures are Claude Code's
client-side estimates; contraprova's own numbers are recomputed from token counts
(`src/contraprova/pricing.py`). Do not add `--bare` here: bare mode skips discovery of agents,
plugins and hooks (pass them with `--agents`/`--plugin-dir`, and plugin hooks still do not run).

### Log runs into contraprova's event log

```bash
claude mcp add --transport stdio contraprova -- \
  python -m contraprova.mcp_edge --log /absolute/path/to/cascade.jsonl
```

This gives the session `submit_intent` (propose `task.created`, `task.claimed`, `task.submitted`)
and `query_projection` (`tasks`, `cost`, `verify`). Ask the orchestrator to record each task, or
script it. The orchestrator's tool list does not include MCP tools; if you want it to log, add
`mcp__contraprova__submit_intent, mcp__contraprova__query_projection` to its `tools` line in a copied
agent file. Gate verdicts, usage and scores are kernel-only events and cannot be written through MCP
(see [docs/mcp.md](mcp.md)), so a Claude Code session can propose work but never certify it.

## Guarantees and their limits

- **No Fable below the orchestrator.** Each tier pins a full model ID, and the gate's PreToolUse hook
  denies any per-call `model` override on a cascade agent (a per-call `model` would otherwise take
  precedence over the frontmatter). Without the hook, add `"permissions": {"deny": ["Agent(model:*)"]}`
  to your settings for the same effect.
- **Organisation model policy wins.** If `availableModels` blocks a tier's model, Claude Code
  substitutes another one and warns; `/tasks` shows the model each subagent actually runs on.
- **The gate is advisory to the orchestrator.** It cannot undo an agent's edits; it tells the
  orchestrator, which decides the next step. Review the final diff yourself.
- **Not verified here without a model call**: that a plugin orchestrator's `Agent(...)` allowlist
  matches the plugin-scoped names. The tools line lists both the bare and the
  `contraprova-cascade:` names for that reason.

## Claude Code documentation this relies on

Checked on 2026-11-12 against Claude Code 2.1.322:

- Subagents: frontmatter fields (`model: inherit`, `effort`, `maxTurns`, `tools`), `Agent(type)`
  allowlists for `--agent` main threads, project/user/plugin scopes, model resolution order,
  frontmatter hooks: https://code.claude.com/docs/en/sub-agents
- Effort resolution, defaults (Opus/Sonnet/Haiku 5.5 default to `medium`), subagent effort:
  https://code.claude.com/docs/en/model-config
- Plugin manifest, standard layout, `${CLAUDE_PLUGIN_ROOT}`, plugin agent field limits:
  https://code.claude.com/docs/en/plugins/manifest-reference,
  https://code.claude.com/docs/en/plugins/components
- Marketplace file and relative plugin sources: https://code.claude.com/docs/en/plugins/marketplace-reference
- Installing (`/plugin marketplace add`, `claude plugin install --marketplace`):
  https://code.claude.com/docs/en/plugins/install
- Hooks: PreToolUse/PostToolUse on `Agent`, `additionalContext`, `permissionDecision`, exec-form
  `args`, `${CLAUDE_PROJECT_DIR}`: https://code.claude.com/docs/en/hooks
- `Agent(model:*)` deny rules: https://code.claude.com/docs/en/permissions
- `--agent`, `--bare`, `--output-format`, `--max-budget-usd`: https://code.claude.com/docs/en/cli-reference,
  https://code.claude.com/docs/en/headless
- `modelUsage`: https://code.claude.com/docs/en/agent-sdk/cost-tracking
- `claude mcp add` (stdio): https://code.claude.com/docs/en/mcp
- `CLAUDE_CONFIG_DIR`: https://code.claude.com/docs/en/env-vars
