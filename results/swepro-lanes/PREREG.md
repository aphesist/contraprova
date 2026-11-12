# Contract Lanes main run on SWE-bench Pro: pre-registration

Written and committed **before any paid request** of this run, on 2026-11-12, at `main` = d3e164a.
Nothing below is changed after the run starts. Design: [docs/lanes-plan.md](../../docs/lanes-plan.md)
(§2 mechanism, §6 statistics, §9 as built, including blame widening). Probe:
[probe/README.md](probe/README.md) (GO on all four rules).

## Tasks

- **Split `lanes-main`:** 40 tasks, ordered list sha256 **`9661965e359551d5cb117bb75bcf65acae2faef853bea5a4ac0c30681d74e828`**
  (`results/swepro-lanes/splits.json`, file sha256 `66847506882f7bcd511f828797844b05ec1fdc69220dee92e7620f0b24b1c88d`;
  base partition `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`).
- **These are fresh tasks:** none was in `probe`, `probe2`, the main study's 90 or `lanes-probe`.
- **n = 37.** Under the plan's existing rule (counted prompt ≤ 90,000 tokens for every lane prompt and
  for D's worker prompt), the free pre-flight drops 3 tasks:
  - `instance_internetarchive__openlibrary-e010b2a13697de70170033902ba2e27a1e1acbe9-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4`: counted prompt 102278 tokens > 90000
  - `instance_internetarchive__openlibrary-5fb312632097be7e9ac6ab657964af115224d15d-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4`: counted prompt 92094 tokens > 90000
  - `instance_internetarchive__openlibrary-09865f5fb549694d969f0a8e49b9d204ef1853ca-ve8c8d62a2b60610a3c4631f5f23ed866bada9818`: counted prompt 91040 tokens > 90000
- **The frozen list is not refilled.** Tasks run in the listed order, and for each task arm La then
  arm D, before the next task.
- Each task gets the $0 empty/gold and gold-lane validation right before it runs
  (`--validate-before-run`). A task that fails is skipped and recorded, not replaced.

## Arms

- **La (Contract Lanes, governed merge).** As in the probe (`LaneConfig`:
  `{"planner_model": "claude-opus-5-5", "planner_effort": "low", "planner_max_tokens": 8000, "max_lanes": 4, "ladder": [["haiku", "claude-haiku-5-5", "medium"], ["haiku-repair", "claude-haiku-5-5", "medium"], ["sonnet", "claude-sonnet-5-5", "medium"], ["opus", "claude-opus-5-5", "low"]], "lane_max_tokens": 32000, "max_rounds": 3, "plan_repair": true, "lane_prompt_max_tokens": 90000}`):
  1. An Opus-low planner chooses 1 ≤ k ≤ 4 lanes from the outline only.
  2. Haiku-medium lanes (32K, one edit repair) each get their own files in full and outlines of the
     others.
  3. The governed merge runs `T0.scope`, `T0.interfaces`, `T0.edits`, the visible and private test
     gates, and `T2.review` above 500 lines.
  4. **Blame:**
     1. assigned visible tests;
     2. traceback and test-stem paths;
     3. all lanes.

     It now includes **blame widening** (d3e164a): when a test gate blocks again and rule 1 would blame
     exactly the lanes it blamed the round before, rule 2 adds any lane a failing test's stem or a
     traceback names, keeping those lanes. If that adds none, every lane is blamed (rule 3, basis
     `tests:widened`).
  5. **Per-lane ladder:** Haiku → Haiku repair → Sonnet medium → Opus low, at most 3 merge rounds after
     the first, never Fable.
  6. **Scored at $0:** La-shadow (the lanes' first replies joined without contracts, no gate) and the
     derived points La-first and La-sonnet.
- **D (governed cascade)**, exactly as in the main study (`--lanes` sets `--cascade`):
  1. An Opus-low brief from the outline.
  2. A Haiku-medium worker (32K, one repair).
  3. Gates.
  4. On a block, a fork into a Haiku control and an Opus-low treatment; D ships the treatment.

  D's cost excludes the fork control. A derived B is recorded.

Command:

```
python -m contraprova.harness.run --benchmark swepro --lanes --split lanes-main --arms La,D \
  --client anthropic --log runs/lanes-main.jsonl --budget-usd 25 --validate-before-run \
  --no-log-contents --delete-images --lanes-splits results/swepro-lanes/splits.json
```

## Budget and stopping

- **Hard cap $25.00** (`--budget-usd 25`). The run refuses to start if the pre-flight's expected × 1.2
  exceeds $25. The budget guard refuses any request whose worst case would cross the cap.
- The stop depends only on spend in the fixed order, never on outcomes. The analysis uses completed
  task pairs (La and D both scored) only.
- **Pre-flight** (free count_tokens; `main/preflight.json`): expected **$19.9748**
  (La $7.5474, D shipped $12.0491, D measurement
  $0.3783), so × 1.2 = $23.97 ≤ $25.
  - Assumptions: p_block 0.45 and D p_fork 0.5 (pre-flight defaults). The probe measured 0.33, and the
    main study measured a 9% fork rate.
  - Worst case $430.2846, bounded only by the guard.

## Analyses

95% intervals: paired percentile bootstrap over tasks (10,000 resamples, seed 0); Wilson for
proportions. n is reported everywhere.

- **Primary (docs/lanes-plan.md §6): La vs La-shadow** on paired solve rate. Exact McNemar test
  (two-sided) on the discordant pairs, Holm correction across m = 1 (so Holm p = p), **alpha 0.05**.
  The paired difference is reported with its bootstrap CI.
- **Secondary** (descriptive, unadjusted):
  - La vs D on paired solve rate (bootstrap CI, exact McNemar);
  - La vs D on cost per solved task, as the ratio La/D with a paired bootstrap CI;
  - the chain La-shadow → La-first → La-sonnet → La, each adjacent step with discordant counts;
  - D's escalation value at fork points (Opus treatment vs Haiku control);
  - clash counts by detector;
  - blame rule per blocked merge (1, 2, 3, `tests:widened`);
  - k chosen per task, plan failures and repairs;
  - the planner's share of La's cost;
  - cost per task per arm;
  - derived B vs La and vs D.
- Costs are recomputed from logged tokens with the frozen price table. La's cost includes every
  planner, lane, repair and escalation request. D's follows the shipped-branch rule.

## Disclosures

- The probe reused 12 already-seen tasks (6 from the main study, 6 from probes 1 and 2). The main run's
  37 tasks are fresh.
- **k ≥ 3 has not yet appeared in a real plan:** in the probe the planner chose k = 2 on 11 of 12 tasks
  and k = 1 once.
- **Blame rule 2 has not yet run on real output:** the probe's 7 blocked merges used rule 1 six times
  and rule 3 once. Blame widening is new since the probe and untested on real output.
- The harness changed between the probe and this run only by blame widening (d3e164a).

## Frozen (sha256)

- Lane prompts (LANE_RETRY, LANE_SYSTEM, LANE_TASK, PLAN_REPAIR, PLAN_REQUEST, PLAN_SCHEMA): `c8c3887024f6c345087e9245819f81cfa451b5d88851a6d0d1e69ebb85117b83`
- Pro worker prompts: `1c5c88e732ac2cf03f98fdd0e983ca44561cf02c25522196dec793594dc76641`
- `LaneConfig().to_dict()`: `659a2769f3b6d179111caab9d3985f3346e14d1753384d3f50971de61305ad31`
- D's ArmConfig (cascade, as run): `6a150dacaca1c7678df16ee220414bbbabe0a945b94f87f39eb1fb493ac22a35`
- Price table `anthropic-2026-11`: `2857c0b5d7bfd1ab25f7c01b8e6bcf5345e45831aa86110338876f1be30096ef`
- Partition: `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`
- Harness files:
  - `src/contraprova/harness/lanes.py`: `38b1b874fefa3f593d3dcd2906a3d7faecd96d88aefba174d365afcb355d4711`
  - `src/contraprova/harness/lanes_detect.py`: `bc28406cf136dc46b85ffe92ca5165bde09abdfa847cace21c0ba6f47c70fdf5`
  - `src/contraprova/harness/swepro_arms.py`: `cdbb9c7916def962508fea18f22b3ea01dcf6392c3aff6f1c8a87973c8c33976`
  - `src/contraprova/harness/arms.py`: `70aea7cdefe72c5b27b357a59af773fe98e55fe2d29d1915075d502a361e19c3`
  - `src/contraprova/harness/swepro_run.py`: `22e7859bc39d1352077c0b8a2cd80e6d5b3736669ad96a96c1529b35e16b68e9`
  - `src/contraprova/harness/edits.py`: `991d771697acfc299d87407381ff59b403d1b43eae24ad501cc4241be642108c`
  - `src/contraprova/harness/outline.py`: `f2ca1e483e27ff6342b6a6d05b904fce4cba9584462c85d114e410b47954bc0e`
  - `src/contraprova/harness/swepro.py`: `2404e7a1d480cf6e18e872a3077fb057d8e7fbb2caddb6bac4943a3c583cc803`
  - `src/contraprova/harness/docker.py`: `0532914c5c11cc85b9a4ad7e6d09692756084293cd855a3b4a9c449110d284a4`
  - `src/contraprova/analysis.py`: `318ec923237da4141022d727e70978dc086a6869f35a01da743eb1d57519be68`
  - `src/contraprova/stats.py`: `10a57a13098c001f71c23f175dc3ff8a44f8e03c56c9e65e335108d0238d8272`

## Task list (run order)

| # | instance_id | Status |
|---|---|---|
| 1 | `instance_element-hq__element-web-aec454dd6feeb93000380523cbb0b3681c0275fd-vnan` | run |
| 2 | `instance_NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5-vnan` | run |
| 3 | `instance_internetarchive__openlibrary-6fdbbeee4c0a7e976ff3e46fb1d36f4eb110c428-v08d8e8889ec945ab821fb156c04c7d2e2810debb` | run |
| 4 | `instance_ansible__ansible-949c503f2ef4b2c5d668af0492a5c0db1ab86140-v0f01c69f1e2528b935359cfe578530722bca2c59` | run |
| 5 | `instance_element-hq__element-web-fe14847bb9bb07cab1b9c6c54335ff22ca5e516a-vnan` | run |
| 6 | `instance_ansible__ansible-d2f80991180337e2be23d6883064a67dcbaeb662-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` | run |
| 7 | `instance_qutebrowser__qutebrowser-bedc9f7fadf93f83d8dee95feeecb9922b6f063f-v2ef375ac784985212b1805e1d0431dc8f1b3c171` | run |
| 8 | `instance_internetarchive__openlibrary-7cbfb812ef0e1f9716e2d6e85d538a96fcb79d13-vfa6ff903cb27f336e17654595dd900fa943dcd91` | run |
| 9 | `instance_qutebrowser__qutebrowser-e5340c449f23608803c286da0563b62f58ba25b0-v059c6fdc75567943479b23ebca7c07b5e9a7f34c` | run |
| 10 | `instance_qutebrowser__qutebrowser-de4a1c1a2839b5b49c3d4ce21d39de48d24e2091-v2ef375ac784985212b1805e1d0431dc8f1b3c171` | run |
| 11 | `instance_internetarchive__openlibrary-0a90f9f0256e4f933523e9842799e39f95ae29ce-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c` | run |
| 12 | `instance_ansible__ansible-bec27fb4c0a40c5f8bbcf26a475704227d65ee73-v30a923fb5c164d6cd18280c02422f75e611e8fb2` | run |
| 13 | `instance_element-hq__element-web-582a1b093fc0b77538052f45cbb9c7295f991b51-vnan` | run |
| 14 | `instance_internetarchive__openlibrary-7f6b722a10f822171501d027cad60afe53337732-ve8c8d62a2b60610a3c4631f5f23ed866bada9818` | run |
| 15 | `instance_internetarchive__openlibrary-308a35d6999427c02b1dbf5211c033ad3b352556-ve8c8d62a2b60610a3c4631f5f23ed866bada9818` | run |
| 16 | `instance_NodeBB__NodeBB-f1a80d48cc45877fcbadf34c2345dd9709722c7f-v4fbcfae8b15e4ce5d132c408bca69ebb9cf146ed` | run |
| 17 | `instance_internetarchive__openlibrary-30bc73a1395fba2300087c7f307e54bb5372b60a-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c` | run |
| 18 | `instance_qutebrowser__qutebrowser-3fd8e12949b8feda401930574facf09dd4180bba` | run |
| 19 | `instance_NodeBB__NodeBB-a5afad27e52fd336163063ba40dcadc80233ae10-vd59a5728dfc977f44533186ace531248c2917516` | run |
| 20 | `instance_element-hq__element-web-33e8edb3d508d6eefb354819ca693b7accc695e7` | run |
| 21 | `instance_internetarchive__openlibrary-92db3454aeaa02f89b4cdbc3103f7e95c9759f92-v2c55207218fb8a0138425cbf7d9675272e240b90` | run |
| 22 | `instance_internetarchive__openlibrary-8a5a63af6e0be406aa6c8c9b6d5f28b2f1b6af5a-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` | run |
| 23 | `instance_qutebrowser__qutebrowser-394bfaed6544c952c6b3463751abab3176ad4997-vafb3e8e01b31319c66c4e666b8a3b1d8ba55db24` | run |
| 24 | `instance_ansible__ansible-de5858f48dc9e1ce9117034e0d7e76806f420ca8-v1055803c3a812189a1133297f7f5468579283f86` | run |
| 25 | `instance_internetarchive__openlibrary-798a582540019363d14b2090755cc7b89a350788-v430f20c722405e462d9ef44dee7d34c41e76fe7a` | run |
| 26 | `instance_internetarchive__openlibrary-7c8dc180281491ccaa1b4b43518506323750d1e4-v298a7a812ceed28c4c18355a091f1b268fe56d86` | run |
| 27 | `instance_element-hq__element-web-d06cf09bf0b3d4a0fbe6bd32e4115caea2083168-vnan` | run |
| 28 | `instance_internetarchive__openlibrary-4b7ea2977be2747496ba792a678940baa985f7ea-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` | run |
| 29 | `instance_ansible__ansible-395e5e20fab9cad517243372fa3c3c5d9e09ab2a-v7eee2454f617569fd6889f2211f75bc02a35f9f8` | run |
| 30 | `instance_internetarchive__openlibrary-c506c1b0b678892af5cb22c1c1dbc35d96787a0a-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` | run |
| 31 | `instance_internetarchive__openlibrary-e010b2a13697de70170033902ba2e27a1e1acbe9-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` | **dropped:** counted prompt 102278 tokens > 90000 |
| 32 | `instance_internetarchive__openlibrary-5fb312632097be7e9ac6ab657964af115224d15d-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` | **dropped:** counted prompt 92094 tokens > 90000 |
| 33 | `instance_ansible__ansible-bf98f031f3f5af31a2d78dc2f0a58fe92ebae0bb-v1055803c3a812189a1133297f7f5468579283f86` | run |
| 34 | `instance_internetarchive__openlibrary-09865f5fb549694d969f0a8e49b9d204ef1853ca-ve8c8d62a2b60610a3c4631f5f23ed866bada9818` | **dropped:** counted prompt 91040 tokens > 90000 |
| 35 | `instance_internetarchive__openlibrary-1be7de788a444f6255e89c10ef6aa608550604a8-v29f82c9cf21d57b242f8d8b0e541525d259e2d63` | run |
| 36 | `instance_ansible__ansible-d58e69c82d7edd0583dd8e78d76b075c33c3151e-v173091e2e36d38c978002990795f66cfc0af30ad` | run |
| 37 | `instance_NodeBB__NodeBB-8ca65b0c78c67c1653487c02d1135e1b702185e1-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e` | run |
| 38 | `instance_qutebrowser__qutebrowser-85b867fe8d4378c8e371f055c70452f546055854-v2ef375ac784985212b1805e1d0431dc8f1b3c171` | run |
| 39 | `instance_ansible__ansible-5260527c4a71bfed99d803e687dd19619423b134-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` | run |
| 40 | `instance_NodeBB__NodeBB-9c576a0758690f45a6ca03b5884c601e473bf2c1-vd59a5728dfc977f44533186ace531248c2917516` | run |
