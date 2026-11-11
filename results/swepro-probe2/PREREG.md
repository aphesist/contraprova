# SWE-bench Pro probe 2: pre-registration

Written and committed **before any paid request** of this probe, on 2026-11-11. Nothing below is
changed after the run.

## Tasks (split `probe2`)

Partition: `data/swepro/partition.json` (not committed; rebuilt by the harness from the dataset rows),
sha256 **`b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`**, seed 20261111. The 12 ids are `contraprova.harness.swepro.PROBE2_IDS`.
Both probe sets are held out of `smoke` and `main` for good.

| # | Task (instance_id) | Repo | Counted prompt tokens (count_tokens, Haiku = Opus) | Gold: files / changed lines | F2P / P2P |
|---|---|---|---|---|---|
| 1 | `instance_ansible__ansible-d62496fe416623e88b90139dc7917080cb04ce70-v0f01c69f1e2528b935359cfe578530722bca2c59` | ansible/ansible | 4,012 | 3 / 35 | 22 / 92 |
| 2 | `instance_element-hq__element-web-ecfd1736e5dd9808e87911fc264e6c816653e1a9-vnan` | element-hq/element-web | 7,412 | 2 / 61 | 9 / 17 |
| 3 | `instance_ansible__ansible-40ade1f84b8bb10a63576b0ac320c13f57c87d34-v6382ea168a93d80a64aab1fbd8c4f02dc5ada5bf` | ansible/ansible | 9,947 | 2 / 653 | 48 / 0 |
| 4 | `instance_NodeBB__NodeBB-8168c6c40707478f71b8af60300830fe554c778c-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e` | NodeBB/NodeBB | 12,540 | 5 / 84 | 4 / 307 |
| 5 | `instance_element-hq__element-web-66d0b318bc6fee0d17b54c1781d6ab5d5d323135-vnan` | element-hq/element-web | 17,018 | 6 / 259 | 24 / 39 |
| 6 | `instance_ansible__ansible-e9e6001263f51103e96e58ad382660df0f3d0e39-v30a923fb5c164d6cd18280c02422f75e611e8fb2` | ansible/ansible | 17,550 | 2 / 96 | 8 / 20 |
| 7 | `instance_internetarchive__openlibrary-53d376b148897466bb86d5accb51912bbbe9a8ed-v08d8e8889ec945ab821fb156c04c7d2e2810debb` | internetarchive/openlibrary | 27,099 | 4 / 59 | 4 / 4 |
| 8 | `instance_internetarchive__openlibrary-322d7a46cdc965bfabbf9500e98fde098c9d95b2-v13642507b4fc1f8d234172bf8129942da2c2ca26` | internetarchive/openlibrary | 27,477 | 2 / 483 | 11 / 0 |
| 9 | `instance_ansible__ansible-d72025be751c894673ba85caa063d835a0ad3a8c-v390e508d27db7a51eece36bb6d9698b63a5b638a` | ansible/ansible | 28,313 | 5 / 294 | 5 / 0 |
| 10 | `instance_ansible__ansible-b748edea457a4576847a10275678127895d2f02f-v1055803c3a812189a1133297f7f5468579283f86` | ansible/ansible | 46,051 | 5 / 269 | 5 / 41 |
| 11 | `instance_internetarchive__openlibrary-630221ab686c64e75a2ce253c893c033e4814b2e-v93c53c13d5f9b383ebb411ee7750b49dcd1a34c6` | internetarchive/openlibrary | 53,318 | 6 / 328 | 9 / 0 |
| 12 | `instance_qutebrowser__qutebrowser-3e21c8214a998cb1058defd15aabb24617a76402-v5fc38aaf22415ab0b70567368332beee7955b367` | qutebrowser/qutebrowser | 54,231 | 1 / 59 | 1869 / 0 |

Tasks run in ascending order of prompt size, as listed.

## How the tasks were chosen

1. **Pool:** Pro's HARD-51 subset, defined in scaleapi/SWE-bench_Pro-os `v2/hard51_ids.txt` (cited in
   that repository's README and `v2/README.md`; also the Hugging Face config `hard`). The dataset's
   `hard` column carries the same 51 ids. 25 of them are in the five in-scope repositories.
   - Left out: openlibrary-6e889f4a (a first-probe task; probe-1 tasks stay excluded).
   - Left out: element-web-5dfde12c and element-web-772df302 (estimated worker prompt over 90K tokens).
   - Left out: openlibrary-b67138b3 (its gold patch deletes a file, which the edit format cannot express).
   - Pool: 21 tasks.
2. **Rule:** `swepro.probe2_candidates`.
   - Each repository's pool is shuffled with `Random("20261111:probe2:<repo>")`.
   - The 12 places are shared out by largest remainder in proportion to pool size: ansible 5,
     openlibrary 3, qutebrowser 2, NodeBB 1, element-web 1.
   - The rest of each pool forms a fixed replacement queue, interleaved by repository and followed by
     a fallback of the largest multi-file tasks.
   - Candidate order, validation and both pre-flights are in `selection.json`,
     `preflight-first-selection.json` and `preflight-final.json`.
3. **$0 validation of every task:** the empty patch scores 0, the gold patch pushed through the
   SEARCH/REPLACE path scores 1, and HEAD equals the base commit. All 12 first picks passed, and both
   replacements passed.
4. **Counted-token rule (owner-approved):** any task whose counted prompt (count_tokens) exceeds
   **90,000 tokens** is replaced by the next queue entries, in queue order. Each replacement is
   validated, and any that also exceeds 90K or fails validation is skipped. The bound is not
   tightened further. Replacements:
   - `instance_ansible__ansible-d33bedc48fdd933b5abd65a77c081876298e2f07-v0f01c69f1e2528b935359cfe578530722bca2c59`
     counted 102,435 tokens (> 90,000). Replaced by queue entry 1,
     `instance_ansible__ansible-e9e6001263f51103e96e58ad382660df0f3d0e39-v30a923fb5c164d6cd18280c02422f75e611e8fb2`
     (17,550 counted; validation ok).
   - `instance_qutebrowser__qutebrowser-e70f5b03187bdd40e8bf70f5f3ead840f52d1f42-v02ad04386d5238fe2d1a1be450df257370de4b6a`
     counted 94,609 tokens (> 90,000). Replaced by queue entry 2,
     `instance_element-hq__element-web-ecfd1736e5dd9808e87911fc264e6c816653e1a9-vnan`
     (7,412 counted; validation ok).
   - Final mix: ansible 5, openlibrary 3, element-web 2, qutebrowser 1, NodeBB 1.

## Configuration

- Arms: **B** and **B2** are two independent samples of Haiku 5.5 (`claude-haiku-5-5`) at effort
  `medium`; **A** is Opus 5.5 (`claude-opus-5-5`) at effort `low`. Single-shot, oracle-files setting
  (`docs/swepro.md`).
- `max_tokens`: **Haiku 32,000**, **Opus 12,000**. A reply cut off at `max_tokens` counts as an
  **edit-format failure** and is reported separately (it still counts as a failure for the decision).
- Command: `python -m contraprova.harness.run --benchmark swepro --probe --probe-split probe2
  --haiku-max-tokens 32000 --no-log-contents --client anthropic --budget-usd 4.5
  --log runs/swepro-probe2/log.jsonl --delete-images`.
- Hard cap **$4.50** (owner-approved). Pre-flight: expected **$2.9546**
  (× 1.2 = $3.5455 ≤ $4.50), worst case $4.5449.
- Log: `task.submitted` stores per-file sha256, the diff, the worker's reply (edit blocks) and test
  output, never full repository file contents.

## Decision rule (unchanged from probe 1)

**GO** if Haiku (sample 1, arm B) fails **≥ 4/12** AND Opus (arm A) solves **≥ 3 more** than Haiku
AND Opus solves **≥ 6/12**. The decision is made on **all failures**. Logic-only failures (all failures
minus edit-format failures: parse/apply errors or truncation at `max_tokens`) are reported alongside
but do not change the decision. Also reported: how many of B's failures B2 solves (the retry noise
floor), and per-task outcomes with failure type.
