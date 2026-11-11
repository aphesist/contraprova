# Replay UI

`ui/index.html` is a static page that shows an event log as evidence. It has three linked
panels, all showing the state **as of event N** on the timeline:

1. **Attempts**: one point per (arm, task), including fork re-runs (`D:control`, `D:treatment`).
2. **Cost per task vs solve rate**: one point per arm with a 95% Wilson interval, the Pareto
   frontier over arms A–D, a price-table selector and a table view of the same numbers.
3. **Replay**: a scrubber, play/step buttons and an event strip. Clicking a point lists every event
   for its task (the event trail); clicking a trail row moves the scrubber to that event.

Python is the source of truth. `scripts/export_ui_data.py` verifies the log (hash chain plus full
replay), prices every `usage.recorded` event under every price table with
`contraprova.pricing.request_cost`, and writes one compact record per event with the deltas it
applies to an attempt. The page only folds those deltas up to event N. It does no pricing and no
task-state logic. It recomputes only the Wilson interval and the Pareto frontier at the scrubber
position, using the same rules as `contraprova.stats.wilson_interval` and
`contraprova.projections.pareto_frontier`.

## Regenerate and view

```bash
# 1. a SYNTHETIC demo log (seeded, no model calls), written through EventLog
python scripts/make_demo_log.py /tmp/demo.jsonl
contraprova verify /tmp/demo.jsonl

# 2. export it (this is how ui/demo-data.json is made; a test checks it is up to date)
python scripts/export_ui_data.py /tmp/demo.jsonl -o ui/demo-data.json

# 3. view it
python -m http.server -d ui 8000      # then open http://localhost:8000/
```

For a real log:

```bash
python scripts/export_ui_data.py path/to/log.jsonl -o ui/data.json
# open http://localhost:8000/?data=data.json
```

Browsers block `fetch` from `file://`. To open the page straight from disk, either use the file
picker it shows, or build a standalone copy with the data inlined:

```bash
python scripts/export_ui_data.py path/to/log.jsonl -o /tmp/data.json --html /tmp/replay.html
```

The page makes no network requests besides fetching its own data file: no CDN, no fonts, no
analytics. It works on GitHub Pages as-is (it loads `demo-data.json` next to it).

## Data conventions the exporter relies on

| Event | Payload used |
|---|---|
| `task.created` | `task_id`, optional `language`, optional `synthetic` |
| `route.decided` | `task_id`, `action` (model), `propensities`, `router_hash`, `features.attempt` |
| `fork.created` | `task_id`, `from_seq`, `model`, `role` (`control` or `treatment`), `seed` |
| `usage.recorded` | the `contraprova.usage.usage_payload` fields plus tags `arm`, `role`, `task_id` |
| `outcome.scored` | `arm`, `task_id`, `solved` |
| `gate.decided` | `task_id`, `gate`, `verdict`, `reason` |

- Task lifecycle events (route, claim, submit, gate) carry no arm tag. They are attributed to the
  governed arm `D`; only that arm goes through the single writer's lifecycle.
- Fork re-runs are recorded under arms `D:control` and `D:treatment`, so their outcomes never replace
  D's own outcome (`projections.outcomes` keys on (arm, task)).
- An attempt's **model tier** is the strongest model that did worker work on it (roles `worker`,
  `control`, `treatment`, or the routed model). **Escalated** means a route to a non-Haiku model on a
  retry (`features.attempt > 1`) or a treatment fork. **Blocked** means at least one `gate.decided`
  with verdict `block`. **Solved** comes only from `outcome.scored`.
- Cost per task for an arm is its total cost divided by the number of tasks it has usage or
  outcomes for, so far.

## How the point layout is computed

Each attempt gets six features from the **complete** log: log input tokens (including cache reads
and writes), log output tokens, output share of tokens, number of requests, number of gate
decisions, and cache-read share of the prompt. They are standardised, and the first two principal
components are found by power iteration with deflation (standard library only; the sign is fixed
so the largest loading is positive). Coordinates are scaled to [0, 1] and get a ±2% jitter derived
from `sha256(attempt id)`, so the layout is identical on every run. The export records the method,
the explained variance and the top loadings, and the axis labels show them.

## Honesty notes

- **The axes of the point map are a layout, not meaning.** Nearby points used similar amounts of
  work; nothing about task semantics is implied. No embedding model is involved.
- **Positions use the whole log** so points do not jump while scrubbing. Visibility, colour and size
  are the state at event N; the position of a point that has just appeared reflects work it has not
  done yet.
- **Colour encodes only logged facts** (outcome, model tier, escalated, gate), one at a time. Size
  is cost under the selected price table.
- **Fork re-runs are not comparable with the arms.** They exist only for tasks whose first gate
  blocked, so they are drawn hollow, labelled with their n, and kept off the frontier.
- **Small n.** Early in the replay an arm may have a handful of scored tasks; the Wilson whiskers
  show how little that says.
- **Replay check.** On load the page folds every event and compares its arm totals (cost under every
  table, task counts, solved counts, Wilson bounds) with the totals Python wrote every 50 events. The
  badge in the header shows how many checkpoints matched.
- **Synthetic data is labelled.** If any `task.created` payload has `"synthetic": true`, the export
  sets `synthetic: true` and the page shows a "SYNTHETIC DEMO DATA — not results" banner and a
  watermark. `ui/demo-data.json` is synthetic: its solve probabilities are invented and say nothing
  about any model.
- **Re-pricing.** The selector offers every table in `contraprova.pricing.PRICE_TABLES` plus a
  what-if with Haiku at half price (built with `pricing.scale_table`). It is a what-if, not a
  published price.
