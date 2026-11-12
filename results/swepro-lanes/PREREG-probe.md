# Contract Lanes probe on SWE-bench Pro: pre-registration

Written and committed **before any paid request** of this probe, on 2026-11-12, at `main` = e1100bc
(the rewritten history; see `results/HISTORY.md`). Nothing below is changed after the run starts.
Design: [docs/lanes-plan.md](../../docs/lanes-plan.md) (§2 mechanism, §7 go rule, §9 as built).

**This probe is descriptive only.** It decides GO/NO-GO for the main Contract Lanes run; it is not
part of the main analysis, and its numbers are pilot measurements on 12 tasks.

## Tasks

Split **`lanes-probe`**, 12 tasks, ordered list sha256
**`74574d74feb98bf97731c1c299f289ee521eb9191a626e716bc7824654c4854f`** (`results/swepro-lanes/splits.json`, file sha256
`66847506882f7bcd511f828797844b05ec1fdc69220dee92e7620f0b24b1c88d`, base partition `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`).

These are already-used large tasks (≥3 gold files, ≥150 changed lines, counted prompt ≤90K): 6 from
the main study and 6 from probes 1 and 2. They are deliberately not fresh; the fresh pool
(`lanes-main`) stays untouched.

| # | instance_id | Source |
|---|---|---|
| 1 | `instance_internetarchive__openlibrary-d109cc7e6e161170391f98f9a6fa1d02534c18e4-ve8c8d62a2b60610a3c4631f5f23ed866bada9818` | main study (A, D results) |
| 2 | `instance_ansible__ansible-ecea15c508f0e081525be036cf76bbb56dbcdd9d-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` | main study (A, D results) |
| 3 | `instance_NodeBB__NodeBB-767973717be700f46f06f3e7f4fc550c63509046-vnan` | main study (A, D results) |
| 4 | `instance_internetarchive__openlibrary-d40ec88713dc95ea791b252f92d2f7b75e107440-v13642507b4fc1f8d234172bf8129942da2c2ca26` | main study (A, D results) |
| 5 | `instance_internetarchive__openlibrary-b112069e31e0553b2d374abb5f9c5e05e8f3dbbe-ve8c8d62a2b60610a3c4631f5f23ed866bada9818` | main study (A, D results) |
| 6 | `instance_ansible__ansible-eea46a0d1b99a6dadedbb6a3502d599235fa7ec3-v390e508d27db7a51eece36bb6d9698b63a5b638a` | main study (A, D results) |
| 7 | `instance_element-hq__element-web-66d0b318bc6fee0d17b54c1781d6ab5d5d323135-vnan` | probe 1/2 |
| 8 | `instance_ansible__ansible-b748edea457a4576847a10275678127895d2f02f-v1055803c3a812189a1133297f7f5468579283f86` | probe 1/2 |
| 9 | `instance_internetarchive__openlibrary-630221ab686c64e75a2ce253c893c033e4814b2e-v93c53c13d5f9b383ebb411ee7750b49dcd1a34c6` | probe 1/2 |
| 10 | `instance_element-hq__element-web-ad26925bb6628260cfe0fcf90ec0a8cba381f4a4-vnan` | probe 1/2 |
| 11 | `instance_ansible__ansible-d72025be751c894673ba85caa063d835a0ad3a8c-v390e508d27db7a51eece36bb6d9698b63a5b638a` | probe 1/2 |
| 12 | `instance_internetarchive__openlibrary-25858f9f0c165df25742acf8309ce909773f0cdd-v13642507b4fc1f8d234172bf8129942da2c2ca26` | probe 1/2 |

## Arm

**La only** (Contract Lanes, governed merge). Arm D is not rerun in the probe; it reruns in the main
study for pairing on fresh tasks. The shadow merge (La-shadow) and the derived points (La-first,
La-sonnet) are scored at $0, as built (docs/lanes-plan.md §2.1 and §9).

- **Planner:** Opus 5.5 (`claude-opus-5-5`), effort `low`, `max_tokens` 8,000. It sees the outline
  only, never file bodies. It chooses k lanes, 1 ≤ k ≤ 4, and freezes the interfaces. An invalid plan
  gets one repair turn, then falls back to a single lane (k = 1).
- **Lanes:** Haiku 5.5, effort `medium`, `max_tokens` 32,000, one edit-repair turn. Each lane gets its
  own files in full and outlines of the others.
- **Governed merge and gates:** `T0.scope` per lane (out-of-scope blocks rejected and counted),
  `T0.interfaces`, `T0.edits`, `T0.visible_tests`, `T1.private_tests`, and `T2.review` above 500
  changed lines.
- **Blame:**
  1. the visible tests assigned to a lane;
  2. file paths in the verifier's tracebacks that fall in a lane's scope;
  3. otherwise all lanes.

  Lanes see only D's retry feedback: visible test names, or a count of private failures.
- **Per-lane ladder** for blamed lanes: **Haiku → Haiku repair (same tier, gate feedback) → Sonnet 5.5
  (`medium`, 32K) → Opus 5.5 (`low`, 32K)**. At most 3 merge rounds after the first, and **never
  Fable** (`LaneConfig` refuses any model outside {Haiku, Sonnet, Opus}).
- `LaneConfig` as run: `{"planner_model": "claude-opus-5-5", "planner_effort": "low", "planner_max_tokens": 8000, "max_lanes": 4, "ladder": [["haiku", "claude-haiku-5-5", "medium"], ["haiku-repair", "claude-haiku-5-5", "medium"], ["sonnet", "claude-sonnet-5-5", "medium"], ["opus", "claude-opus-5-5", "low"]], "lane_max_tokens": 32000, "max_rounds": 3, "plan_repair": true, "lane_prompt_max_tokens": 90000}`.

Command:

```
python -m contraprova.harness.run --benchmark swepro --lanes --split lanes-probe --arms La \
  --client anthropic --log runs/lanes-probe.jsonl --budget-usd 5 --validate-before-run \
  --no-log-contents --delete-images --lanes-splits results/swepro-lanes/splits.json
```

`--lanes-splits` points at the committed list, because the default path
(`data/swepro/lanes-splits.json`) does not exist in this container. The content is the same frozen
list.

## Budget and stopping

- **Hard cap $5.00** (`--budget-usd 5`). The budget guard refuses any request whose worst case would
  cross it. Tasks run in the listed order; a budget stop leaves the rest unrun, and they are reported
  as not run.
- **Pre-flight** (free count_tokens; `preflight.json`): expected **$2.3233**
  (× 1.2 = $2.79 ≤ $5). The planner is $1.2857, lanes
  $0.1298, Sonnet $0.483 and Opus $0.3888.
  Worst case $91.6342, bounded only by the guard.
- Each task gets the $0 empty/gold and gold-lane validation right before it runs
  (`--validate-before-run`). A task that fails is skipped and recorded, not replaced.

## Go rule (docs/lanes-plan.md §7; all four must hold)

1. **≥ 10 of 12** tasks complete with no harness error.
2. The planner chooses **k ≥ 2 on ≥ 8 of 12** tasks, and falls back to **k = 1 on ≤ 2**.
3. Measured La cost is **≤ $0.35 per task**, and the main-run estimate, re-run on the probe's measured
   rates, fits the main cap ($25) at expected × 1.2.
4. La solves **≥ 6 of 12**.

Reported but not decisive:

- La vs La-shadow discordance, and the chain shadow → first → Sonnet → final;
- clash counts by type;
- which blame rule fired, on real failing tests;
- La vs the existing A and D results on the 6 main-study tasks.

## Frozen (sha256)

- Lane prompts (LANE_RETRY, LANE_SYSTEM, LANE_TASK, PLAN_REPAIR, PLAN_REQUEST, PLAN_SCHEMA): `c8c3887024f6c345087e9245819f81cfa451b5d88851a6d0d1e69ebb85117b83`
- Pro worker prompts (EDIT_FORMAT, PRO_BRIEF_REQUEST, PRO_LEAD_CONTEXT, PRO_LEAD_SYSTEM, PRO_REVIEW_REQUEST, PRO_T2_SYSTEM, PRO_T2_TASK, PRO_WORKER_RETRY, PRO_WORKER_SYSTEM, PRO_WORKER_TASK, REPAIR_ERRORS, REPAIR_TRUNCATED): `1c5c88e732ac2cf03f98fdd0e983ca44561cf02c25522196dec793594dc76641`
- `LaneConfig().to_dict()`: `659a2769f3b6d179111caab9d3985f3346e14d1753384d3f50971de61305ad31`
- Price table `anthropic-2026-11`: `2857c0b5d7bfd1ab25f7c01b8e6bcf5345e45831aa86110338876f1be30096ef`
- Partition: `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`
- Harness files:
  - `src/contraprova/harness/lanes.py`: `079117c50fd8ef9771641de0f342e2af2026206948aaa2761671b67861787948`
  - `src/contraprova/harness/lanes_detect.py`: `bc28406cf136dc46b85ffe92ca5165bde09abdfa847cace21c0ba6f47c70fdf5`
  - `src/contraprova/harness/swepro_arms.py`: `cdbb9c7916def962508fea18f22b3ea01dcf6392c3aff6f1c8a87973c8c33976`
  - `src/contraprova/harness/arms.py`: `70aea7cdefe72c5b27b357a59af773fe98e55fe2d29d1915075d502a361e19c3`
  - `src/contraprova/harness/swepro_run.py`: `22e7859bc39d1352077c0b8a2cd80e6d5b3736669ad96a96c1529b35e16b68e9`
  - `src/contraprova/harness/edits.py`: `991d771697acfc299d87407381ff59b403d1b43eae24ad501cc4241be642108c`
  - `src/contraprova/harness/outline.py`: `f2ca1e483e27ff6342b6a6d05b904fce4cba9584462c85d114e410b47954bc0e`
  - `src/contraprova/harness/swepro.py`: `2404e7a1d480cf6e18e872a3077fb057d8e7fbb2caddb6bac4943a3c583cc803`
  - `src/contraprova/harness/docker.py`: `0532914c5c11cc85b9a4ad7e6d09692756084293cd855a3b4a9c449110d284a4`
  - `src/contraprova/analysis.py`: `318ec923237da4141022d727e70978dc086a6869f35a01da743eb1d57519be68`
