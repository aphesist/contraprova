# Claude Code orchestrator configuration

A ready-to-use orchestrator/worker pair for [Claude Code subagents](https://code.claude.com/docs/en/sub-agents).
The orchestrator runs on Opus 5.5 at low effort and may only plan, route and review. Workers run on
Haiku 5.5 and do the edits.

## Use it

Copy the agents into a project and start Claude Code with the orchestrator as the main agent:

```bash
mkdir -p .claude/agents
cp config/claude-code/agents/*.md .claude/agents/
claude --agent orchestrator
```

The orchestrator's tool list (`Agent(worker), Read, Grep, Glob`) is what keeps it from doing the work
itself: it has no edit or shell tools.

For scripted runs, use an API key (not a subscription sign-in) and record usage per model:

```bash
export CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1   # workers may not spawn workers
claude --bare -p "..." --agent orchestrator --output-format json --max-budget-usd 1
```

`--output-format json` reports `modelUsage` per model, including subagents. contraprova treats those
figures as estimates and recomputes cost from token counts (see `src/contraprova/pricing.py`).

## Why low effort for the orchestrator

At list prices Opus 5.5 costs 40 times Haiku 5.5 per token. In a swarm the orchestrator's share of
tokens decides whether the setup is cheap at all, so its plans and briefs are kept short on purpose.
Whether that trade-off pays is exactly what contraprova measures; until then it is a hypothesis.
