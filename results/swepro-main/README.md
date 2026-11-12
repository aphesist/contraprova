# SWE-bench Pro main study: results (2026-11-12)

Pre-registration: [PREREG.md](PREREG.md), committed (f43ad38) before any paid request. Run: arms A
and D on the pre-registered list of 90 `main` tasks, in its seeded order. The pre-registered analysis
is [analyze.py](analyze.py); its output is `analysis.json`. The independent `contraprova report` is in
[contraprova-report.md](contraprova-report.md) and gives the same paired D-vs-A contrast.

## Headline

**n = 88 completed task pairs.** Two of the 90 listed tasks failed the $0 validation (their gold
patch scored 0), so they were skipped and not replaced, as pre-registered. The budget guard never
stopped the run.

| | Verdict | Estimate [95% CI] |
|---|---|---|
| **Primary:** D non-inferior to A (margin −10 points) | **Non-inferior** | solve_D − solve_A = **-2.3 points [-8.0, 3.4]**; discordant pairs D-only 2 / A-only 4, exact McNemar p = 0.688 |
| **Co-primary:** D cheaper per solved task | **Cheaper** | cost per solved task D/A = **0.393 [0.299, 0.510]** |

In this setting (oracle files, single-shot workers with one repair turn), the governed cascade's
solve rate is within 10 points of a single Opus 5.5 at low effort, at about 39% of its cost per solved
task. The point estimate still favours A by 2.3 points, and the interval does not exclude a loss of up
to 8 points. Arm A is near its ceiling on these tasks (96.6%), so this sample could not show D doing much better than A.

## Solve rates (Wilson 95%)

| Arm | Solved |
|---|---|
| A: single Opus 5.5, low | 85/88 = 96.6% [90.5%, 98.8%] |
| D: governed cascade | 83/88 = 94.3% [87.4%, 97.5%] |
| B (derived): D's first, brief-assisted Haiku attempt | 77/88 = 87.5% [79.0%, 92.9%] |

## Cost (recomputed from logged tokens, `anthropic-2026-11`)

| | Total | Per task (mean) | Per solved task |
|---|---|---|---|
| A | $11.8125 | $0.1342 | $0.1390 |
| D (brief + Haiku attempt + shipped Opus branch) | $4.5328 | $0.0515 | $0.0546 |
| D fork controls (measurement overhead, not D's cost) | $0.0575 | | |
| B (derived; its Haiku requests only) | $0.3872 | $0.0044 | $0.0050 |

- **Total recorded spend:** **$16.4028** (Opus $15.9581, Haiku $0.4448; no Sonnet,
  because no T2 review was triggered). The hard cap was $50; the pre-flight expected $32.65 (it assumed
  D forks on half the tasks; the measured fork rate is 9.1%).
- **Project total** under the owner's approvals: Polyglot pilot $1.9830 + probes $1.4556, $2.7286, $3.3164 +
  this run $16.4028 = $25.8864.

## Secondary (descriptive, no multiplicity claims)

- **Escalation value at fork points** (D's gate blocked the Haiku attempt; n = 8 forks):
  - the Opus treatment solved 6/8;
  - the Haiku retry control (the noise floor) solved 2/8;
  - EV = **+50.0 points [12.5, 87.5]**, exact McNemar
    p = 0.125.

  With 8 forks this is very imprecise. The cascade gated 80 of 88 Haiku attempts through without
  escalation.
- **B (derived) vs A:** -9.1 points [-17.0, -2.3],
  McNemar p = 0.039.
  - A brief-assisted single Haiku attempt is measurably worse than Opus.
  - The gates and the escalation recover most of the gap: D's estimate is -2.3 points.
- **Repair turns** (requests that used one / worker requests):
  - A 9/88;
  - D's Haiku attempt 7/88;
  - fork controls 3/8;
  - fork treatments 1/8.
- **Failure categories** (after repair):
  - A: 3 logic, 0 edit-format;
  - D: 5 logic, 0 edit-format;
  - B (derived): 11 logic, 0 edit-format.

  No edit-format failure survived the repair turn.
- **Orchestrator share in D:** 62.7% of D's dollars and 13.1% of its tokens.
  The Opus brief is most of D's cost; the Haiku workers are cheap.
- **T2 reviews triggered:** 0. **D fork rate:** 9.1% (8/88).

## Deviations and incidents (disclosed)

1. **Disk full after task 10; harness fix mid-run.** The per-task validation ran `docker run` on images
   not pulled through the image cache, so docker pulled them implicitly and `--delete-images` never
   removed them. The disk filled and the process stopped **between tasks**: no partial task, and the
   log verifies.
   - Fix b587f67: validation pulls through the cache first.
   - The run resumed with the identical command; the resume skips completed tasks.
   - **This changes the hash of `src/contraprova/harness/swepro_run.py`** from the PREREG value
     (`bf8f9729…`) to `771dd3217e37ef4da6dc78a0f5d79728a86a8c4ff16aea37fc5d3b82fdd1877f`. Every other hashed file matches the PREREG values. The change touches
     image handling in validation only, not prompts, arms, gates, scoring or pricing.
2. Two tasks (qutebrowser-deeb15d6, NodeBB-76797371) have a spurious "validation failed" row caused by
   the full disk. They had not run; both were re-validated OK on resume and ran normally. Their rows are
   kept in `log.validation.jsonl`.
3. Two tasks genuinely failed validation (gold reward 0): openlibrary-3f7db6bb and openlibrary-8a9d9d32.
   They were skipped, so n = 88.

## Data withheld (licence hygiene)

The log was written with `--no-log-contents`: `task.submitted` holds per-file sha256, the diff, the
worker's reply (edit blocks, repair replies included) and test output, never full repository files.
Task data, verifier files and context stay under the git-ignored `data/swepro/`.

Files here: `log.jsonl` (`contraprova verify`: 1460 events, chain intact),
`log.validation.jsonl`, `analysis.json`, `contraprova-report.md`, `preflight.json`, `eligible-90.txt`,
and the run summaries.

## Per task (in run order)

✓ solved, ✗ not. For D, the branch shipped: `initial` (gates passed, Haiku shipped) or `treatment` (fork,
Opus shipped). Costs follow the arm cost rules above.

| # | Task | A | D (branch) | B derived | Fork: Haiku control / Opus treatment | Cost A | Cost D |
|---|---|---|---|---|---|---|---|
| 1 | `internetarchive__openlibrary-0dc5b20fa186f9714f8a838178597e6` | ✓ | ✓ (initial) | ✓ | — | $0.090 | $0.031 |
| 2 | `ansible__ansible-189fcb37f973f0b1d52b555728208eeb9a6fce83-v9` | ✓ | ✓ (initial) | ✓ | — | $0.101 | $0.045 |
| 3 | `qutebrowser__qutebrowser-6b320dc18662580e1313d2548fdd6231d2a` | ✓ | ✗ (initial) | ✗ | — | $0.111 | $0.049 |
| 4 | `element-hq__element-web-27139ca68eb075a4438c18fca184887002a4` | ✓ | ✓ (initial) | ✓ | — | $0.026 | $0.022 |
| 5 | `NodeBB__NodeBB-00c70ce7b0541cfc94afe567921d7668cdc8f4ac-vnan` | ✓ | ✓ (initial) | ✓ | — | $0.169 | $0.033 |
| 6 | `internetarchive__openlibrary-e390c1212055dd84a262a798e53487e` | ✓ | ✗ (treatment) | ✗ | control ✗ / treatment ✗ | $0.141 | $0.219 |
| 7 | `qutebrowser__qutebrowser-0b621cb0ce2b54d3f93d8d41d8ff4257888` | ✓ | ✓ (initial) | ✓ | — | $0.023 | $0.020 |
| 8 | `element-hq__element-web-2760bfc8369f1bee640d6d7a7e910783143d` | ✓ | ✗ (treatment) | ✗ | control ✗ / treatment ✗ | $0.323 | $0.301 |
| 9 | `internetarchive__openlibrary-1351c59fd43689753de1fca32c78d53` | ✓ | ✓ (initial) | ✓ | — | $0.115 | $0.035 |
| 10 | `ansible__ansible-ecea15c508f0e081525be036cf76bbb56dbcdd9d-vb` | ✓ | ✓ (treatment) | ✗ | control ✓ / treatment ✓ | $0.350 | $0.458 |
| 11 | `qutebrowser__qutebrowser-deeb15d6f009b3ca0c3bd503a7cef07462b` | ✓ | ✓ (treatment) | ✗ | control ✗ / treatment ✓ | $0.079 | $0.137 |
| 12 | `NodeBB__NodeBB-767973717be700f46f06f3e7f4fc550c63509046-vnan` | ✓ | ✓ (initial) | ✓ | — | $0.153 | $0.041 |
| 13 | `internetarchive__openlibrary-d109cc7e6e161170391f98f9a6fa1d0` | ✓ | ✓ (initial) | ✓ | — | $0.255 | $0.064 |
| 14 | `ansible__ansible-f327e65d11bb905ed9f15996024f857a95592629-vb` | ✓ | ✓ (initial) | ✓ | — | $0.226 | $0.033 |
| 15 | `ansible__ansible-ed6581e4db2f1bec5a772213c3e186081adc162d-v0` | ✓ | ✓ (initial) | ✓ | — | $0.082 | $0.037 |
| 16 | `qutebrowser__qutebrowser-ed19d7f58b2664bb310c7cb6b52c5b9a06e` | ✓ | ✓ (initial) | ✓ | — | $0.237 | $0.052 |
| 17 | `internetarchive__openlibrary-431442c92887a3aece3f8aa771dd029` | ✓ | ✓ (initial) | ✓ | — | $0.056 | $0.030 |
| 18 | `element-hq__element-web-b7fea97bb68c6628a644580076f840109132` | ✓ | ✓ (initial) | ✓ | — | $0.085 | $0.029 |
| 19 | `ansible__ansible-ea04e0048dbb3b63f876aad7020e1de8eee9f362-v1` | ✓ | ✓ (initial) | ✓ | — | $0.311 | $0.051 |
| 20 | `internetarchive__openlibrary-7bf3238533070f2d24bafbb26eedf67` | ✓ | ✓ (initial) | ✓ | — | $0.716 | $0.060 |
| 21 | `ansible__ansible-c1f2df47538b884a43320f53e787197793b105e8-v9` | ✗ | ✓ (initial) | ✓ | — | $0.103 | $0.048 |
| 22 | `qutebrowser__qutebrowser-44e64199ed38003253f0296badd4a447645` | ✓ | ✓ (initial) | ✓ | — | $0.075 | $0.030 |
| 23 | `NodeBB__NodeBB-087e6020e490b4a1759f38c1ad03869511928263-vf2c` | ✓ | ✓ (initial) | ✓ | — | $0.090 | $0.035 |
| 24 | `qutebrowser__qutebrowser-5e0d6dc1483cb3336ea0e3dcbd4fe4aa00f` | ✓ | ✓ (initial) | ✓ | — | $0.298 | $0.049 |
| 25 | `internetarchive__openlibrary-111347e9583372e8ef91c82e0612ea4` | ✓ | ✓ (initial) | ✓ | — | $0.120 | $0.042 |
| 26 | `ansible__ansible-3b823d908e8a5d17674f8c26d337d3114b7493b1-v0` | ✓ | ✓ (initial) | ✓ | — | $0.167 | $0.046 |
| 27 | `element-hq__element-web-18c03daa865d3c5b10e52b669cd50be34c67` | ✓ | ✓ (initial) | ✓ | — | $0.052 | $0.024 |
| 28 | `qutebrowser__qutebrowser-cc360cd4a34a126274c7b51f3b63afbaf3e` | ✓ | ✓ (initial) | ✓ | — | $0.078 | $0.038 |
| 29 | `internetarchive__openlibrary-d40ec88713dc95ea791b252f92d2f7b` | ✓ | ✓ (initial) | ✓ | — | $0.153 | $0.047 |
| 30 | `ansible__ansible-12734fa21c08a0ce8c84e533abdc560db2eb1955-v7` | ✓ | ✓ (initial) | ✓ | — | $0.057 | $0.035 |
| 31 | `NodeBB__NodeBB-4327a09d76f10a79109da9d91c22120428d3bdb9-vnan` | ✓ | ✓ (initial) | ✓ | — | $0.103 | $0.032 |
| 32 | `qutebrowser__qutebrowser-e57b6e0eeeb656eb2c84d6547d5a0a7333e` | ✓ | ✓ (initial) | ✓ | — | $0.130 | $0.033 |
| 33 | `internetarchive__openlibrary-b112069e31e0553b2d374abb5f9c5e0` | ✓ | ✓ (initial) | ✓ | — | $0.135 | $0.044 |
| 34 | `qutebrowser__qutebrowser-77c3557995704a683cdb67e2a3055f7547f` | ✓ | ✓ (initial) | ✓ | — | $0.051 | $0.026 |
| 35 | `internetarchive__openlibrary-f8cc11d9c1575fdba5ac66aee0befca` | ✓ | ✓ (initial) | ✓ | — | $0.032 | $0.025 |
| 36 | `ansible__ansible-cd473dfb2fdbc97acf3293c134b21cbbcfa89ec3-vb` | ✓ | ✓ (treatment) | ✗ | control ✗ / treatment ✓ | $0.051 | $0.115 |
| 37 | `element-hq__element-web-cf3c899dd1f221aa1a1f4c5a80dffc05b9c2` | ✓ | ✓ (initial) | ✓ | — | $0.169 | $0.048 |
| 38 | `NodeBB__NodeBB-1ea9481af6125ffd6da0592ed439aa62af0bca11-vd59` | ✓ | ✓ (initial) | ✓ | — | $0.062 | $0.021 |
| 39 | `internetarchive__openlibrary-798055d1a19b8fa0983153b709f460b` | ✓ | ✓ (initial) | ✓ | — | $0.049 | $0.021 |
| 40 | `ansible__ansible-a1569ea4ca6af5480cf0b7b3135f5e12add28a44-v0` | ✓ | ✓ (initial) | ✓ | — | $0.074 | $0.023 |
| 41 | `internetarchive__openlibrary-00bec1e7c8f3272c469a58e1377df03` | ✓ | ✓ (initial) | ✓ | — | $0.069 | $0.033 |
| 42 | `qutebrowser__qutebrowser-996487c43e4fcc265b541f9eca1e7930e3c` | ✓ | ✓ (initial) | ✓ | — | $0.285 | $0.051 |
| 43 | `ansible__ansible-3889ddeb4b780ab4bac9ca2e75f8c1991bcabe83-v0` | ✓ | ✓ (initial) | ✓ | — | $0.112 | $0.037 |
| 44 | `NodeBB__NodeBB-70b4a0e2aebebe8f2f559de6680093d96a697b2f-vnan` | ✓ | ✓ (initial) | ✓ | — | $0.148 | $0.042 |
| 45 | `internetarchive__openlibrary-72321288ea790a3ace9e36f1c05b68c` | ✓ | ✓ (initial) | ✓ | — | $0.108 | $0.036 |
| 46 | `qutebrowser__qutebrowser-35168ade46184d7e5b91dfa04ca42fe2abd` | ✓ | ✓ (initial) | ✓ | — | $0.062 | $0.036 |
| 47 | `ansible__ansible-f8ef34672b961a95ec7282643679492862c688ec-vb` | ✓ | ✓ (initial) | ✓ | — | $0.231 | $0.072 |
| 48 | `internetarchive__openlibrary-123e6e5e1c85b9c07d1e98f70bfc480` | ✗ | ✓ (initial) | ✓ | — | $0.091 | $0.037 |
| 49 | `qutebrowser__qutebrowser-8d05f0282a271bfd45e614238bd1b555c58` | ✓ | ✓ (initial) | ✓ | — | $0.065 | $0.038 |
| 50 | `ansible__ansible-748f534312f2073a25a87871f5bd05882891b8c4-v0` | ✓ | ✓ (initial) | ✓ | — | $0.065 | $0.027 |
| 51 | `internetarchive__openlibrary-f3b26c2c0721f8713353fe4b3412303` | ✓ | ✓ (initial) | ✓ | — | $0.087 | $0.044 |
| 52 | `NodeBB__NodeBB-be43cd25974681c9743d424238b7536c357dc8d3-vf2c` | ✓ | ✓ (initial) | ✓ | — | $0.188 | $0.037 |
| 53 | `qutebrowser__qutebrowser-36ade4bba504eb96f05d32ceab9972df7eb` | ✓ | ✓ (initial) | ✓ | — | $0.030 | $0.030 |
| 54 | `ansible__ansible-eea46a0d1b99a6dadedbb6a3502d599235fa7ec3-v3` | ✓ | ✓ (treatment) | ✗ | control ✗ / treatment ✓ | $0.188 | $0.218 |
| 55 | `internetarchive__openlibrary-5c6c22f3d2edf2f1b10f5dc335e32cb` | ✓ | ✓ (initial) | ✓ | — | $0.124 | $0.043 |
| 56 | `ansible__ansible-a6e671db25381ed111bbad0ab3e7d97366395d05-v0` | ✓ | ✓ (treatment) | ✗ | control ✓ / treatment ✓ | $0.038 | $0.073 |
| 57 | `element-hq__element-web-ca58617cee8aa91c93553449bfdf9b3465a5` | ✓ | ✓ (initial) | ✓ | — | $0.149 | $0.027 |
| 58 | `internetarchive__openlibrary-3f580a5f244c299d936d73d9e327ba8` | ✓ | ✓ (initial) | ✓ | — | $0.092 | $0.035 |
| 59 | `NodeBB__NodeBB-b1f9ad5534bb3a44dab5364f659876a4b7fe34c1-vnan` | ✓ | ✓ (initial) | ✓ | — | $0.182 | $0.042 |
| 60 | `ansible__ansible-379058e10f3dbc0fdcaf80394bd09b18927e7d33-v1` | ✓ | ✓ (initial) | ✓ | — | $0.667 | $0.064 |
| 61 | `internetarchive__openlibrary-3aeec6afed9198d734b7ee1293f03ca` | ✓ | ✓ (initial) | ✓ | — | $0.054 | $0.027 |
| 62 | `ansible__ansible-cd9c4eb5a6b2bfaf4a6709f001ce3d0c92c1eed2-v0` | ✓ | ✓ (initial) | ✓ | — | $0.075 | $0.026 |
| 63 | `qutebrowser__qutebrowser-7b603dd6bf195e3e723ce08ff64a82b406e` | ✓ | ✓ (initial) | ✓ | — | $0.056 | $0.029 |
| 64 | `NodeBB__NodeBB-7b8bffd763e2155cf88f3ebc258fa68ebe18188d-vf2c` | ✓ | ✓ (initial) | ✓ | — | $0.076 | $0.038 |
| 65 | `ansible__ansible-3db08adbb1cc6aa9941be5e0fc810132c6e1fa4b-vb` | ✓ | ✓ (initial) | ✓ | — | $0.118 | $0.027 |
| 66 | `qutebrowser__qutebrowser-96b997802e942937e81d2b8a32d08f00d3f` | ✓ | ✓ (initial) | ✓ | — | $0.070 | $0.041 |
| 67 | `internetarchive__openlibrary-d8162c226a9d576f094dc1830c4c1ff` | ✓ | ✓ (initial) | ✓ | — | $0.097 | $0.033 |
| 68 | `ansible__ansible-11c1777d56664b1acb56b387a1ad6aeadef1391d-v0` | ✓ | ✓ (initial) | ✓ | — | $0.041 | $0.020 |
| 69 | `qutebrowser__qutebrowser-e34dfc68647d087ca3175d9ad3f023c30d8` | ✗ | ✗ (initial) | ✗ | — | $0.104 | $0.041 |
| 70 | `internetarchive__openlibrary-03095f2680f7516fca35a58e665bf2a` | ✓ | ✓ (treatment) | ✗ | control ✗ / treatment ✓ | $0.034 | $0.073 |
| 71 | `ansible__ansible-d9f1866249756efc264b00ff7497e92c11a9885f-v0` | ✓ | ✓ (initial) | ✓ | — | $0.166 | $0.041 |
| 72 | `element-hq__element-web-459df4583e01e4744a52d45446e341833854` | ✓ | ✓ (initial) | ✓ | — | $0.227 | $0.033 |
| 73 | `NodeBB__NodeBB-f9ce92df988db7c1ae55d9ef96d247d27478bc70-vf2c` | ✓ | ✓ (initial) | ✓ | — | $0.025 | $0.024 |
| 74 | `qutebrowser__qutebrowser-ebfe9b7aa0c4ba9d451f993e08955004aae` | ✓ | ✓ (initial) | ✓ | — | $0.160 | $0.039 |
| 75 | `ansible__ansible-106909db8b730480615f4a33de0eb5b710944e78-v0` | ✓ | ✓ (initial) | ✓ | — | $0.165 | $0.041 |
| 76 | `qutebrowser__qutebrowser-9ed748effa8f3bcd804612d9291da017b51` | ✓ | ✗ (initial) | ✗ | — | $0.118 | $0.050 |
| 77 | `internetarchive__openlibrary-08ac40d050a64e1d2646ece4959af0c` | ✓ | ✓ (initial) | ✓ | — | $0.136 | $0.038 |
| 78 | `element-hq__element-web-44b98896a79ede48f5ad7ff22619a39d5f6f` | ✓ | ✓ (initial) | ✓ | — | $0.072 | $0.028 |
| 79 | `ansible__ansible-5c225dc0f5bfa677addeac100a8018df3f3a9db1-v1` | ✓ | ✓ (initial) | ✓ | — | $0.280 | $0.035 |
| 80 | `NodeBB__NodeBB-f083cd559d69c16481376868c8da65172729c0ca-vnan` | ✓ | ✓ (initial) | ✓ | — | $0.133 | $0.042 |
| 81 | `internetarchive__openlibrary-9c392b60e2c6fa1d68cb68084b4b4ff` | ✓ | ✓ (initial) | ✓ | — | $0.031 | $0.028 |
| 82 | `qutebrowser__qutebrowser-fea33d607fde83cf505b228238cf3659364` | ✓ | ✓ (initial) | ✓ | — | $0.064 | $0.031 |
| 83 | `ansible__ansible-a7d2a4e03209cff1e97e59fd54bb2b05fdbdbec6-v0` | ✓ | ✓ (initial) | ✓ | — | $0.172 | $0.042 |
| 84 | `element-hq__element-web-a692fe21811f88d92e8f7047fc615e4f1f98` | ✓ | ✓ (initial) | ✓ | — | $0.112 | $0.040 |
| 85 | `internetarchive__openlibrary-a48fd6ba9482c527602bc081491d9e8` | ✓ | ✓ (initial) | ✓ | — | $0.166 | $0.039 |
| 86 | `ansible__ansible-1ee70fc272aff6bf3415357c6e13c5de5b928d9b-v1` | ✓ | ✓ (initial) | ✓ | — | $0.032 | $0.021 |
| 87 | `internetarchive__openlibrary-4a5d2a7d24c9e4c11d3069220c0685b` | ✓ | ✓ (initial) | ✓ | — | $0.313 | $0.027 |
| 88 | `NodeBB__NodeBB-bad15643013ca15affe408b75eba9e47cc604bb2-vd59` | ✓ | ✓ (initial) | ✓ | — | $0.034 | $0.026 |
