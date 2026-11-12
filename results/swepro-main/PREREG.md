# SWE-bench Pro main study: pre-registration

Written and committed **before any paid request** of this run, on 2026-11-12. Nothing below is
changed after the run starts.

## Question

Does the governed cascade (arm D) match a single Opus (arm A) in solve rate at lower cost on SWE-bench
Pro (Python/JS subset, oracle-files setting)?

## Tasks and stopping rule

- **Tasks:** the `main` split only; never a `probe` or `probe2` task. Partition sha256
  `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`, seed 20261111. Taken in the split's seeded, repository-interleaved order:
  the first 90 tasks whose counted worker prompt (count_tokens: Opus without a brief, Haiku with a
  brief-sized placeholder) is at most 90,000 tokens and whose gold patch the edit format can
  express.
  - The list is below and in `eligible-90.txt` (sha256 `2755997b128c0876735b3c2c915a64e15c7ad9fd546e1afbd942769e4dbfa65c`).
  - Mix: NodeBB 12, ansible 24, element-hq 9, internetarchive 26, qutebrowser 19.
  - The 20 tasks skipped on the way, with reasons, are listed at the end.
- **Order:** task by task; for each task, arm A then arm D (with its forks) before the next task. A
  stop therefore leaves only complete pairs, plus at most one partial task, which is dropped from
  the analysis.
- **Per-task validation ($0):** right before a task's arms run, the empty patch must score 0 (and
  fail the visible subset), and the gold patch pushed through the SEARCH/REPLACE path must score 1
  (and pass both gate subsets), with HEAD = base commit. A task that fails is skipped and recorded in
  `log.validation.jsonl`; it is not replaced.
- **Stopping rule:** process the list in order until all 90 are done, or until the budget guard
  refuses a request because recorded spend plus that request's worst case would exceed the
  **hard cap of $50.00** (`--budget-usd 50`, this run alone). The stop depends only on spend in a
  fixed order, never on outcomes. There is no interim analysis and no early stop on results. The
  analysis uses completed task pairs only.
- **Expected n:** 90. The pre-flight (free count_tokens) expects $32.6496 for 90 tasks
  (A $17.9497, D shipped $14.4178, D measurement
  $0.2821), assuming D's gate blocks half the tasks and a repair rate
  of 0.3. The owner's target is about $40, with up to $50 allowed. A report goes to the parent session
  after the first 10 tasks, with spend and the projected final n.

## Arms (harness commit with the hashes below)

- **A: single Opus.** `claude-opus-5-5`, effort `low`, `max_tokens` 32,000, one repair turn. Worker
  prompt: problem statement, requirements, interface, full content of the gold patch's files, names
  of the visible tests. No gates.
- **D: governed cascade, a fixed policy** (`CascadeRouter`; no learned router this run):
  1. The Opus orchestrator (`low`, `max_tokens` 4,000) writes a brief from the **outline** only
     (no file bodies).
  2. A Haiku worker (`claude-haiku-5-5`, `medium`, 32,000, one repair turn) gets the full files and
     the brief, under the lane contract.
  3. Gates, stopping at the first block: `T0.scope`, `T0.edits`, `T0.visible_tests`,
     `T1.private_tests`, and `T2.review` (Sonnet 5.5 diff judge, only when a patch exceeds 500
     changed lines or edits a manifest).
  4. If the gates pass, D ships that attempt.
  5. If they block, the log forks at the blocking gate decision into a **Haiku control** (fresh
     seed) and an **Opus treatment** (`low`, 32,000, one repair turn). Both get the same retry
     prompt: the first prompt, the diff of the blocked attempt and the gate's reason. The visible
     gate names failing visible tests; the private gate gives only a count. **D ships the Opus
     treatment's result** (gated; a treatment that the gates block counts as unsolved).
  - D's cost = the brief + the Haiku attempt (with repair) + the shipped Opus branch. The Haiku
    control is measurement overhead and is excluded from D's cost (`arm_usage_records`,
    docs/events.md).
- **B (derived, not run separately):** D's first Haiku attempt, scored on the full score tests, is
  recorded as an arm-B outcome (`derived_from: "D"`). It is therefore a brief-assisted single Haiku
  attempt, not an unassisted one. Its cost for analysis is that Haiku attempt's usage (with
  repair).
- Contract Lanes and arm C are not part of this run.
- The log stores per-file sha256, diffs, edit blocks and test output, never full repository file
  contents (`--no-log-contents`).

Command: `python -m contraprova.harness.run --benchmark swepro --split main --arms A,D --cascade
--task-file results/swepro-main/eligible-90.txt --validate-before-run --client anthropic
--budget-usd 50 --no-log-contents --delete-images --log runs/swepro-main/log.jsonl`

## Analyses

Intervals are 95%: paired bootstrap with 10,000 resamples over tasks, seed 0; Wilson for single
proportions. n is reported everywhere.

- **Primary: non-inferiority of D vs A on paired solve rate**, margin δ = 10 points. D is
  **non-inferior** if the lower bound of the 95% paired bootstrap CI of (solve_D − solve_A) is above
  −10 points. The exact McNemar p (two-sided) on the discordant pairs is also reported.
- **Co-primary: cost per solved task**, the ratio D/A, with a 95% paired bootstrap CI (resampling
  tasks; within each resample, total cost / number solved, per arm). D is **cheaper** if the upper
  bound is below 1. Costs are recomputed from logged tokens with the frozen price table; D's cost
  follows the shipped-branch rule above.
- **Secondary** (descriptive, no multiplicity claims):
  - escalation value at fork points: the Opus treatment's solve rate minus the Haiku control's, with
    the control as the retry noise floor, with n forks and CI;
  - B (derived) vs A, paired solve-rate difference with CI;
  - repair-turn usage and outcomes per arm;
  - failure categories (logic vs edit-format remaining after repair) per arm;
  - the orchestrator's token and dollar share in D;
  - cost per task per arm, and D's fork rate;
  - T2 reviews triggered.

## Frozen (sha256)

- Prompts (canonical JSON of EDIT_FORMAT, PRO_BRIEF_REQUEST, PRO_LEAD_CONTEXT, PRO_LEAD_SYSTEM, PRO_REVIEW_REQUEST, PRO_T2_SYSTEM, PRO_T2_TASK, PRO_WORKER_RETRY, PRO_WORKER_SYSTEM, PRO_WORKER_TASK, REPAIR_ERRORS, REPAIR_TRUNCATED): `1c5c88e732ac2cf03f98fdd0e983ca44561cf02c25522196dec793594dc76641`
- ArmConfig as run (`pro_arm_config` with the `--cascade` overrides and the Sonnet T2 reviewer):
  `6a150dacaca1c7678df16ee220414bbbabe0a945b94f87f39eb1fb493ac22a35`
- Price table `anthropic-2026-11`: `2857c0b5d7bfd1ab25f7c01b8e6bcf5345e45831aa86110338876f1be30096ef`
- Partition: `b409bcddf07ce2e0534bf4a0cbac18822588d73a1caf1a117e5e5538cb7f69ad`
- Task list `eligible-90.txt`: `2755997b128c0876735b3c2c915a64e15c7ad9fd546e1afbd942769e4dbfa65c`
- Harness files:
  - `src/contraprova/harness/swepro_arms.py`: `b46e0de62747b1fec24cdb2249ba72efd05955b84d0fcf824e853b96c98d8c50`
  - `src/contraprova/harness/arms.py`: `70aea7cdefe72c5b27b357a59af773fe98e55fe2d29d1915075d502a361e19c3`
  - `src/contraprova/harness/swepro_run.py`: `bf8f9729c1abf1c3eca398ea969b71d6db8c9d1b405cedc13f8c5fb1a349b62f`
  - `src/contraprova/harness/edits.py`: `991d771697acfc299d87407381ff59b403d1b43eae24ad501cc4241be642108c`
  - `src/contraprova/harness/outline.py`: `f2ca1e483e27ff6342b6a6d05b904fce4cba9584462c85d114e410b47954bc0e`
  - `src/contraprova/harness/swepro.py`: `f301ded76f6c5b2fff4a94559c44827d4b4639236d647923204301eeca78da35`
  - `src/contraprova/harness/docker.py`: `0532914c5c11cc85b9a4ad7e6d09692756084293cd855a3b4a9c449110d284a4`

## Task list (in run order)

| # | instance_id |
|---|---|
| 1 | `instance_internetarchive__openlibrary-0dc5b20fa186f9714f8a838178597e69f549d026-v2d9a6c849c60ed19fd0858ce9e40b7cc8e097e59` |
| 2 | `instance_ansible__ansible-189fcb37f973f0b1d52b555728208eeb9a6fce83-v906c969b551b346ef54a2c0b41e04f632b7b73c2` |
| 3 | `instance_qutebrowser__qutebrowser-6b320dc18662580e1313d2548fdd6231d2a97e6d-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d` |
| 4 | `instance_element-hq__element-web-27139ca68eb075a4438c18fca184887002a4ffbc-vnan` |
| 5 | `instance_NodeBB__NodeBB-00c70ce7b0541cfc94afe567921d7668cdc8f4ac-vnan` |
| 6 | `instance_internetarchive__openlibrary-e390c1212055dd84a262a798e53487e771d3fb64-v8717e18970bcdc4e0d2cea3b1527752b21e74866` |
| 7 | `instance_qutebrowser__qutebrowser-0b621cb0ce2b54d3f93d8d41d8ff4257888a87e5-v2ef375ac784985212b1805e1d0431dc8f1b3c171` |
| 8 | `instance_element-hq__element-web-2760bfc8369f1bee640d6d7a7e910783143d4c5f-vnan` |
| 9 | `instance_internetarchive__openlibrary-1351c59fd43689753de1fca32c78d539a116ffc1-v29f82c9cf21d57b242f8d8b0e541525d259e2d63` |
| 10 | `instance_ansible__ansible-ecea15c508f0e081525be036cf76bbb56dbcdd9d-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` |
| 11 | `instance_qutebrowser__qutebrowser-deeb15d6f009b3ca0c3bd503a7cef07462bd16b4-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d` |
| 12 | `instance_NodeBB__NodeBB-767973717be700f46f06f3e7f4fc550c63509046-vnan` |
| 13 | `instance_internetarchive__openlibrary-d109cc7e6e161170391f98f9a6fa1d02534c18e4-ve8c8d62a2b60610a3c4631f5f23ed866bada9818` |
| 14 | `instance_ansible__ansible-f327e65d11bb905ed9f15996024f857a95592629-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` |
| 15 | `instance_internetarchive__openlibrary-3f7db6bbbcc7c418b3db72d157c6aed1d45b2ccf-v430f20c722405e462d9ef44dee7d34c41e76fe7a` |
| 16 | `instance_ansible__ansible-ed6581e4db2f1bec5a772213c3e186081adc162d-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 17 | `instance_qutebrowser__qutebrowser-ed19d7f58b2664bb310c7cb6b52c5b9a06ea60b2-v059c6fdc75567943479b23ebca7c07b5e9a7f34c` |
| 18 | `instance_internetarchive__openlibrary-431442c92887a3aece3f8aa771dd029738a80eb1-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c` |
| 19 | `instance_element-hq__element-web-b7fea97bb68c6628a644580076f840109132f074-vnan` |
| 20 | `instance_ansible__ansible-ea04e0048dbb3b63f876aad7020e1de8eee9f362-v1055803c3a812189a1133297f7f5468579283f86` |
| 21 | `instance_internetarchive__openlibrary-7bf3238533070f2d24bafbb26eedf675d51941f6-v08d8e8889ec945ab821fb156c04c7d2e2810debb` |
| 22 | `instance_ansible__ansible-c1f2df47538b884a43320f53e787197793b105e8-v906c969b551b346ef54a2c0b41e04f632b7b73c2` |
| 23 | `instance_qutebrowser__qutebrowser-44e64199ed38003253f0296badd4a447645067b6-v2ef375ac784985212b1805e1d0431dc8f1b3c171` |
| 24 | `instance_NodeBB__NodeBB-087e6020e490b4a1759f38c1ad03869511928263-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e` |
| 25 | `instance_qutebrowser__qutebrowser-5e0d6dc1483cb3336ea0e3dcbd4fe4aa00fc1742-v5149fcda2a9a6fe1d35dfed1bade1444a11ef271` |
| 26 | `instance_internetarchive__openlibrary-111347e9583372e8ef91c82e0612ea437ae3a9c9-v2d9a6c849c60ed19fd0858ce9e40b7cc8e097e59` |
| 27 | `instance_ansible__ansible-3b823d908e8a5d17674f8c26d337d3114b7493b1-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 28 | `instance_element-hq__element-web-18c03daa865d3c5b10e52b669cd50be34c67b2e5-vnan` |
| 29 | `instance_qutebrowser__qutebrowser-cc360cd4a34a126274c7b51f3b63afbaf3e05a02-v5fc38aaf22415ab0b70567368332beee7955b367` |
| 30 | `instance_internetarchive__openlibrary-d40ec88713dc95ea791b252f92d2f7b75e107440-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| 31 | `instance_ansible__ansible-12734fa21c08a0ce8c84e533abdc560db2eb1955-v7eee2454f617569fd6889f2211f75bc02a35f9f8` |
| 32 | `instance_NodeBB__NodeBB-4327a09d76f10a79109da9d91c22120428d3bdb9-vnan` |
| 33 | `instance_qutebrowser__qutebrowser-e57b6e0eeeb656eb2c84d6547d5a0a7333ecee85-v2ef375ac784985212b1805e1d0431dc8f1b3c171` |
| 34 | `instance_internetarchive__openlibrary-b112069e31e0553b2d374abb5f9c5e05e8f3dbbe-ve8c8d62a2b60610a3c4631f5f23ed866bada9818` |
| 35 | `instance_qutebrowser__qutebrowser-77c3557995704a683cdb67e2a3055f7547fa22c3-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d` |
| 36 | `instance_internetarchive__openlibrary-f8cc11d9c1575fdba5ac66aee0befca970da8d64-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| 37 | `instance_ansible__ansible-cd473dfb2fdbc97acf3293c134b21cbbcfa89ec3-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` |
| 38 | `instance_element-hq__element-web-cf3c899dd1f221aa1a1f4c5a80dffc05b9c21c85-vnan` |
| 39 | `instance_NodeBB__NodeBB-1ea9481af6125ffd6da0592ed439aa62af0bca11-vd59a5728dfc977f44533186ace531248c2917516` |
| 40 | `instance_internetarchive__openlibrary-798055d1a19b8fa0983153b709f460be97e33064-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| 41 | `instance_ansible__ansible-a1569ea4ca6af5480cf0b7b3135f5e12add28a44-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 42 | `instance_internetarchive__openlibrary-00bec1e7c8f3272c469a58e1377df03f955ed478-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| 43 | `instance_qutebrowser__qutebrowser-996487c43e4fcc265b541f9eca1e7930e3c5cf05-v2ef375ac784985212b1805e1d0431dc8f1b3c171` |
| 44 | `instance_ansible__ansible-3889ddeb4b780ab4bac9ca2e75f8c1991bcabe83-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 45 | `instance_NodeBB__NodeBB-70b4a0e2aebebe8f2f559de6680093d96a697b2f-vnan` |
| 46 | `instance_internetarchive__openlibrary-72321288ea790a3ace9e36f1c05b68c93f7eec43-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` |
| 47 | `instance_qutebrowser__qutebrowser-35168ade46184d7e5b91dfa04ca42fe2abd82717-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d` |
| 48 | `instance_ansible__ansible-f8ef34672b961a95ec7282643679492862c688ec-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` |
| 49 | `instance_internetarchive__openlibrary-123e6e5e1c85b9c07d1e98f70bfc480bc8016890-v2733ff199fb72f0d033a30dc62cb0a4742e3a7f4` |
| 50 | `instance_qutebrowser__qutebrowser-8d05f0282a271bfd45e614238bd1b555c58b3fc1-v35616345bb8052ea303186706cec663146f0f184` |
| 51 | `instance_ansible__ansible-748f534312f2073a25a87871f5bd05882891b8c4-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 52 | `instance_internetarchive__openlibrary-f3b26c2c0721f8713353fe4b341230332e30008d-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` |
| 53 | `instance_NodeBB__NodeBB-be43cd25974681c9743d424238b7536c357dc8d3-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e` |
| 54 | `instance_qutebrowser__qutebrowser-36ade4bba504eb96f05d32ceab9972df7eb17bcc-v2ef375ac784985212b1805e1d0431dc8f1b3c171` |
| 55 | `instance_ansible__ansible-eea46a0d1b99a6dadedbb6a3502d599235fa7ec3-v390e508d27db7a51eece36bb6d9698b63a5b638a` |
| 56 | `instance_internetarchive__openlibrary-5c6c22f3d2edf2f1b10f5dc335e32cb6a5f40341-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c` |
| 57 | `instance_ansible__ansible-a6e671db25381ed111bbad0ab3e7d97366395d05-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 58 | `instance_element-hq__element-web-ca58617cee8aa91c93553449bfdf9b3465a5119b-vnan` |
| 59 | `instance_internetarchive__openlibrary-3f580a5f244c299d936d73d9e327ba873b6401d9-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` |
| 60 | `instance_NodeBB__NodeBB-b1f9ad5534bb3a44dab5364f659876a4b7fe34c1-vnan` |
| 61 | `instance_ansible__ansible-379058e10f3dbc0fdcaf80394bd09b18927e7d33-v1055803c3a812189a1133297f7f5468579283f86` |
| 62 | `instance_internetarchive__openlibrary-3aeec6afed9198d734b7ee1293f03ca94ff970e1-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| 63 | `instance_ansible__ansible-cd9c4eb5a6b2bfaf4a6709f001ce3d0c92c1eed2-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 64 | `instance_qutebrowser__qutebrowser-7b603dd6bf195e3e723ce08ff64a82b406e3f6b6-v9f8e9d96c85c85a605e382f1510bd08563afc566` |
| 65 | `instance_internetarchive__openlibrary-8a9d9d323dfcf2a5b4f38d70b1108b030b20ebf3-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| 66 | `instance_NodeBB__NodeBB-7b8bffd763e2155cf88f3ebc258fa68ebe18188d-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e` |
| 67 | `instance_ansible__ansible-3db08adbb1cc6aa9941be5e0fc810132c6e1fa4b-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5` |
| 68 | `instance_qutebrowser__qutebrowser-96b997802e942937e81d2b8a32d08f00d3f4bc4e-v5fc38aaf22415ab0b70567368332beee7955b367` |
| 69 | `instance_internetarchive__openlibrary-d8162c226a9d576f094dc1830c4c1ffd0be2dd17-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c` |
| 70 | `instance_ansible__ansible-11c1777d56664b1acb56b387a1ad6aeadef1391d-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 71 | `instance_qutebrowser__qutebrowser-e34dfc68647d087ca3175d9ad3f023c30d8c9746-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d` |
| 72 | `instance_internetarchive__openlibrary-03095f2680f7516fca35a58e665bf2a41f006273-v8717e18970bcdc4e0d2cea3b1527752b21e74866` |
| 73 | `instance_ansible__ansible-d9f1866249756efc264b00ff7497e92c11a9885f-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 74 | `instance_element-hq__element-web-459df4583e01e4744a52d45446e34183385442d6-vnan` |
| 75 | `instance_NodeBB__NodeBB-f9ce92df988db7c1ae55d9ef96d247d27478bc70-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e` |
| 76 | `instance_qutebrowser__qutebrowser-ebfe9b7aa0c4ba9d451f993e08955004aaec4345-v059c6fdc75567943479b23ebca7c07b5e9a7f34c` |
| 77 | `instance_ansible__ansible-106909db8b730480615f4a33de0eb5b710944e78-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 78 | `instance_qutebrowser__qutebrowser-9ed748effa8f3bcd804612d9291da017b514e12f-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d` |
| 79 | `instance_internetarchive__openlibrary-08ac40d050a64e1d2646ece4959af0c42bf6b7b5-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4` |
| 80 | `instance_element-hq__element-web-44b98896a79ede48f5ad7ff22619a39d5f6ff03c-vnan` |
| 81 | `instance_ansible__ansible-5c225dc0f5bfa677addeac100a8018df3f3a9db1-v173091e2e36d38c978002990795f66cfc0af30ad` |
| 82 | `instance_NodeBB__NodeBB-f083cd559d69c16481376868c8da65172729c0ca-vnan` |
| 83 | `instance_internetarchive__openlibrary-9c392b60e2c6fa1d68cb68084b4b4ff04d0cb35c-v2d9a6c849c60ed19fd0858ce9e40b7cc8e097e59` |
| 84 | `instance_qutebrowser__qutebrowser-fea33d607fde83cf505b228238cf365936437a63-v9f8e9d96c85c85a605e382f1510bd08563afc566` |
| 85 | `instance_ansible__ansible-a7d2a4e03209cff1e97e59fd54bb2b05fdbdbec6-v0f01c69f1e2528b935359cfe578530722bca2c59` |
| 86 | `instance_element-hq__element-web-a692fe21811f88d92e8f7047fc615e4f1f986b0f-vnan` |
| 87 | `instance_internetarchive__openlibrary-a48fd6ba9482c527602bc081491d9e8ae6e8226c-vfa6ff903cb27f336e17654595dd900fa943dcd91` |
| 88 | `instance_ansible__ansible-1ee70fc272aff6bf3415357c6e13c5de5b928d9b-v1055803c3a812189a1133297f7f5468579283f86` |
| 89 | `instance_internetarchive__openlibrary-4a5d2a7d24c9e4c11d3069220c0685b736d5ecde-v13642507b4fc1f8d234172bf8129942da2c2ca26` |
| 90 | `instance_NodeBB__NodeBB-bad15643013ca15affe408b75eba9e47cc604bb2-vd59a5728dfc977f44533186ace531248c2917516` |

## Skipped on the way to 90 (pre-flight)

- `instance_ansible__ansible-d33bedc48fdd933b5abd65a77c081876298e2f07-v0f01c69f1e2528b935359cfe578530722bca2c59`: counted worker prompt 102695 tokens > 90000
- `instance_qutebrowser__qutebrowser-473a15f7908f2bb6d670b0e908ab34a28d8cf7e2-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d`: worker prompt ~158020 tokens > 90000
- `instance_element-hq__element-web-f14374a51c153f64f313243f2df6ea4971db4e15`: worker prompt ~121808 tokens > 90000
- `instance_NodeBB__NodeBB-bd80d36e0dcf78cd4360791a82966078b3a07712-v4fbcfae8b15e4ce5d132c408bca69ebb9cf146ed`: worker prompt ~135799 tokens > 90000
- `instance_qutebrowser__qutebrowser-e70f5b03187bdd40e8bf70f5f3ead840f52d1f42-v02ad04386d5238fe2d1a1be450df257370de4b6a`: counted worker prompt 94869 tokens > 90000
- `instance_element-hq__element-web-6961c256035bed0b7640a6e5907652c806968478-vnan`: worker prompt ~116016 tokens > 90000
- `instance_internetarchive__openlibrary-b67138b316b1e9c11df8a4a8391fe5cc8e75ff9f-ve8c8d62a2b60610a3c4631f5f23ed866bada9818`: the gold patch deletes openlibrary/catalog/marc/parse_xml.py
- `instance_ansible__ansible-c616e54a6e23fa5616a1d56d243f69576164ef9b-v1055803c3a812189a1133297f7f5468579283f86`: worker prompt ~146985 tokens > 90000
- `instance_element-hq__element-web-f3534b42df3dcfe36dc48bddbf14034085af6d30-vnan`: worker prompt ~119834 tokens > 90000
- `instance_ansible__ansible-b2a289dcbb702003377221e25f62c8a3608f0e89-v173091e2e36d38c978002990795f66cfc0af30ad`: worker prompt ~91182 tokens > 90000
- `instance_qutebrowser__qutebrowser-66cfa15c372fa9e613ea5a82d3b03e4609399fb6-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d`: worker prompt ~161915 tokens > 90000
- `instance_element-hq__element-web-880428ab94c6ea98d3d18dcaeb17e8767adcb461-vnan`: the gold patch renames src/toasts/UnverifiedSessionToast.tsx
- `instance_element-hq__element-web-1216285ed2e82e62f8780b6702aa0f9abdda0b34-vnan`: worker prompt ~108824 tokens > 90000
- `instance_element-hq__element-web-5dfde12c1c1c0b6e48f17e3405468593e39d9492-vnan`: worker prompt ~114702 tokens > 90000
- `instance_qutebrowser__qutebrowser-16de05407111ddd82fa12e54389d532362489da9-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d`: worker prompt ~157327 tokens > 90000
- `instance_qutebrowser__qutebrowser-52708364b5f91e198defb022d1a5b4b3ebd9b563-v2ef375ac784985212b1805e1d0431dc8f1b3c171`: worker prompt ~114811 tokens > 90000
- `instance_element-hq__element-web-aeabf3b18896ac1eb7ae9757e66ce886120f8309-vnan`: worker prompt ~128747 tokens > 90000
- `instance_element-hq__element-web-404c412bcb694f04ba0c4d5479541203d701bca0-vnan`: worker prompt ~112314 tokens > 90000
- `instance_internetarchive__openlibrary-e010b2a13697de70170033902ba2e27a1e1acbe9-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4`: counted worker prompt 102278 tokens > 90000
- `instance_qutebrowser__qutebrowser-0fc6d1109d041c69a68a896db87cf1b8c194cef7-v2ef375ac784985212b1805e1d0431dc8f1b3c171`: counted worker prompt 108545 tokens > 90000
