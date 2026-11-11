# Replay UI

`ui/index.html` is a static page that shows an event log as evidence. It has three linked
panels, all showing the state **as of event N** on the timeline:

1. **Attempts**: one point per (arm, task), including fork re-runs (labelled `D:control`, `D:treatment` from
   each fork's role), coloured by arm by default.
2. **Cost per task vs solve rate**: one point per arm with a 95% Wilson interval, the Pareto
   frontier over arms A–D, a price-table selector and a table view of the same numbers.
3. **Replay**: a scrubber, play/step buttons and an event strip. Clicking a point lists every event
   for its task (the event trail); clicking a trail row moves the scrubber to that event.

Below them an honesty table says what each claim on the page rests on.

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

The page makes no requests outside its own folder: its data file and the vendored lab kit
(`ui/vendor/lab-kit/v1/`: `lab.css`, `lab.js`, the two fonts). No CDN, no analytics. It works on
GitHub Pages as-is (it loads `demo-data.json` next to it). The `--html` copy inlines the kit too
(stylesheet, fonts as data URIs, script), so it is one file. `tests/test_ui_export.py` checks every
resource the page and the kit name, and the kit's checksums.

## Design: the aphes.ist lab kit

The page is styled entirely by the owner's design system, the aphes.ist **lab kit 1.0.0**, vendored
unchanged in `ui/vendor/lab-kit/v1/` (`SOURCE.md` there names the repository, branch, commit,
version, checksums and font licences). To update it, copy `system/lab-kit/v1/` from the
`design-system` branch of `aphesimos/aphesis` again and update `SOURCE.md`; never edit the copy.

- The shell is the kit's: `body.lab > .lab-page`, the header with the φ, the project name and the
  theme and fog switches (fog is off by default), an Aphesis Script title with a Latin gloss,
  Special Elite text. The page's own `<style>` only lays things out, on the kit's tokens.
- Colours are the kit's tokens, read at draw time: SVG marks take `.lab-c1`…`.lab-c8`, `.lab-ink`,
  `.lab-dim`; the canvases read `Lab.color(...)` and redraw on `Lab.onTheme`. Arms take the
  categorical slots in the order of `arms` (A, B, C, D, D:control, D:treatment).
- Readability rules: one series is ink (the "escalated" colouring); two or more take the slots in
  order with a legend, plus direct labels when four or fewer are shown; never more than eight
  (further arms fold into the dim context colour). Text is never in a series colour. Red marks one
  thing per chart: the selected attempt (a ring on the map, `.lab-ring` on its arm in the cost
  chart). Wilson intervals are stippled bands (a range); points are solid (a value). The Pareto
  frontier is an ink step line. Blocked attempts are diamonds and escalated ones triangles; the event
  strip gives each kind (gate block, escalation, fork re-run, solved) its own lane, slot and shape,
  so colour is never the only cue. Every chart has a caption, an `aria-label` stating what it shows
  at the scrubber, and the arm numbers are in a table. Nothing moves on load; playback runs only
  when the reader presses play.

## Data conventions the exporter relies on

| Event | Payload used |
|---|---|
| `task.created` | `task_id`, optional `language`, optional `synthetic` |
| `route.decided` | `task_id`, `action` (model), `propensities`, `router_hash`, optional `decision`, `features.attempt` |
| `fork.created` | `fork_id`, `task_id` (parent), `from_seq`, `model`, `role` (`control` or `treatment`), optional `arm`, `fork_task_id` |
| `usage.recorded` | the `contraprova.usage.usage_payload` fields plus tags `arm`, `role`, `task_id`, `fork_id` |
| `outcome.scored` | arm outcome `arm`, `task_id`, `solved`, optional `branch_task_id`; fork outcome `fork_id`, `task_id`, `solved`; gate replays are listed but not counted |
| `gate.decided` | `task_id`, `gate`, `verdict`, `reason` |

Payloads follow [events.md](events.md).

- Attempts are keyed by the pairing key (the outcome's `task_id`). A kernel task id is mapped to it
  through `task.created.exercise_id`, so harness logs (`D:python/leap`) and the demo (one shared task
  per exercise) both work.
- Task lifecycle events (route, claim, submit, gate) are attributed to the arm named by their task's
  `task.created`, else to the governed arm `D`; a fork child's (`fork_task_id`) go to its fork.
- Fork re-runs are fork events: usage and outcomes keyed by `fork_id`. For display only, the exporter
  labels each fork `<parent arm>:<role>` (`D:control`, `D:treatment`; further controls at the same
  fork point are `D:control-2`, ...), and lists those labels in `arms` with `kind: "fork"`, `parent`,
  `role` and `replicate`. Every fork's cost is shown under the fork's label. The branch an arm shipped (its outcome's `branch_task_id`) also counts toward that arm, as a cost delta on the arm's attempt at the arm's outcome event; the rest of the fork spend is measurement overhead (`final.measurement_overhead`, shown under the arm table) and in no arm's cost (see statistics.md).
- An attempt's **model tier** is the strongest model that did worker work on it (roles `worker`,
  `control`, `treatment`, or the routed model). **Escalated** means a route to a non-Haiku model on a
  retry (`decision: after_gate_failure`, or `features.attempt > 0`, the `router.featurize` encoding of attempt 2 and later) or a treatment fork. **Blocked** means at least one `gate.decided`
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
- **Colour encodes only logged facts** (arm, outcome, model tier, escalated, gate), one at a time. Size
  is cost under the selected price table.
- **Fork re-runs are not comparable with the arms.** They exist only for tasks whose first gate
  blocked, so they are drawn hollow, labelled with their n where labels are shown, and kept off the frontier.
- **Small n.** Early in the replay an arm may have a handful of scored tasks; the stippled Wilson
  bands show how little that says.
- **Replay check.** On load the page folds every event and compares its arm totals (cost under every
  table, task counts, solved counts, Wilson bounds) with the totals Python wrote every 50 events. The
  badge under the title shows how many checkpoints matched (N/N), and the honesty table lists it as
  *tested*.
- **Synthetic data is labelled.** If any `task.created` payload has `"synthetic": true`, the export
  sets `synthetic: true` and the page shows a "synthetic demo data — not results" callout, a
  watermark on the map, and marks every quantitative claim in the honesty table "synthetic demo, not
  results" (for a real log they read measured, modelled or hypothesis). `ui/demo-data.json` is synthetic: its solve probabilities are invented and say nothing
  about any model.
- **Re-pricing.** The selector offers every table in `contraprova.pricing.PRICE_TABLES` plus a
  what-if with Haiku at half price (built with `pricing.scale_table`). It is a what-if, not a
  published price.
