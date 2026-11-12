"""SWE-bench Pro V2 (public), Python/JavaScript subset: tasks, the frozen partition, the test split,
verifier files and task context.

Scope
    Five repositories with images of at most 6.1 GB: ansible, openlibrary, qutebrowser (Python),
    NodeBB, element-web (JavaScript/TypeScript); 327 of V2's 642 tasks. teleport (11.8 GB),
    protonmail/webclients (22.6 GB) and the Go repositories are excluded (docs/swepro.md).

Data
    Nothing from the benchmark is vendored. :func:`fetch_dataset` downloads V2's rows from the
    Hugging Face datasets server (``ScaleAI/SWE-bench_Pro``, config ``default``, split ``test``)
    with the standard library and writes the in-scope rows as JSON lines; :func:`load_tasks` reads
    that file. :func:`verifier_dir` downloads a task's official verifier files from
    ``scaleapi/SWE-bench_Pro-os`` (``v2/tasks/<id>/tests/``) on first use and checks them against
    the dataset row. Everything downloaded is data: it is parsed, never executed on the host.

Partition (seeded, stratified, persisted with its sha256, like ``polyglot.Partition``)
    ``probe``  exactly the 12 tasks of the feasibility study's probe (:data:`PROBE_IDS`).
    ``probe2`` the 12 harder tasks of the second probe (:data:`PROBE2_IDS`), chosen from Pro's
               HARD-51 subset by :func:`probe2_candidates` and validated at $0. Both probe sets
               are kept out of ``smoke`` and ``main`` for good.
    ``smoke``  one task per repository, drawn with the seed from the rest.
    ``main``   everything else, in a seeded order that interleaves repositories in proportion
               to their size, so any prefix (``--limit N``) is itself stratified.

Test split (per task, deterministic)
    ``visible``       about half of the fail-to-pass tests, chosen by a seeded hash of the test id
                      (at least one). Workers are shown these test *names* (never test code), and
                      the visible gate requires them to pass.
    ``gate_private``  the other fail-to-pass tests: arm D's T1 gate requires them to pass; models
                      see only a count when they fail.
    ``score``         all fail-to-pass plus all pass-to-pass tests: the official verifier's reward.
    Unlike Polyglot's disjoint split, gate tests are a subset of the score tests: the official
    score is defined on the full set, and the visible/private tests are carved out of it.

Context
    Workers get the full current content of the files the gold patch changes (the "oracle
    files" setting of the feasibility study) plus the names of files the gold patch creates. The
    files come from the task's image at the base commit (``docker create`` + ``docker cp``; the
    container never starts), or, for dry runs without Docker, from the repository at the base
    commit over HTTPS. Test files are never part of the context.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .edits import parse_unified_diff
from .polyglot import Partition, sha256_json, sha256_text

REPOS: tuple[str, ...] = ("NodeBB/NodeBB", "ansible/ansible", "element-hq/element-web", "internetarchive/openlibrary",
                          "qutebrowser/qutebrowser")
EXCLUDED_REPOS_NOTE = "teleport and protonmail/webclients (images 11.8 / 22.6 GB) and the Go repositories"
LANGUAGE_NAMES = {"py": "python", "js": "javascript", "ts": "javascript"}
# The Contract Lanes study's splits live in their own frozen file (:class:`LaneSplits`), so the
# main partition (and its sha256) stays exactly as the main study froze it.
LANE_SPLITS: tuple[str, ...] = ("lanes-probe", "lanes-main")
SPLITS: tuple[str, ...] = ("smoke", "probe", "probe2", "main", *LANE_SPLITS)
# Pro's HARD-51 subset is defined in scaleapi/SWE-bench_Pro-os, v2/hard51_ids.txt (one instance_id
# per line; v2/README.md); the dataset's ``hard`` column and its ``hard`` config carry the same 51
# ids (checked 2026-11-11: identical, 25 of them in this module's five repositories).
HARD51_URL = "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/main/v2/hard51_ids.txt"
PROBE2_SIZE = 12
DEFAULT_SEED = 20261111
SMOKE_PER_REPO = 1
DATASET = "ScaleAI/SWE-bench_Pro"
DATASET_CONFIG = "default"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
VERIFIER_BASE = "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/main/v2/tasks"
RAW_BASE = "https://raw.githubusercontent.com"
VERIFIER_FILES: tuple[str, ...] = ("test.sh", "run_script.sh", "parser.py", "config.json", "test_patch.patch")
# Conservative characters per token for prompt-size estimates (code tokenizes at ~3.2-3.8 chars
# per token; Haiku 5.5's tokenizer uses slightly more tokens). The probe pre-flight replaces the
# estimate with exact counts from the free count_tokens endpoint.
CHARS_PER_TOKEN = 3.0
MAX_WORKER_PROMPT_TOKENS = 90_000

# The 12 probe tasks of the feasibility study (seed 20261111, stratified 3/3/2/2/2 by repository).
PROBE_IDS: tuple[str, ...] = (
    "instance_ansible__ansible-29aea9ff3466e4cd2ed00524b9e56738d568ce8b-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5",
    "instance_ansible__ansible-a20a52701402a12f91396549df04ac55809f68e9-v1055803c3a812189a1133297f7f5468579283f86",
    "instance_ansible__ansible-34db57a47f875d11c4068567b9ec7ace174ec4cf-v1055803c3a812189a1133297f7f5468579283f86",
    "instance_internetarchive__openlibrary-6e889f4a733c9f8ce9a9bd2ec6a934413adcedb9-ve8c8d62a2b60610a3c4631f5f23ed866bada9818",
    "instance_internetarchive__openlibrary-58999808a17a26b387f8237860a7a524d1e2d262-v08d8e8889ec945ab821fb156c04c7d2e2810debb",
    "instance_internetarchive__openlibrary-25858f9f0c165df25742acf8309ce909773f0cdd-v13642507b4fc1f8d234172bf8129942da2c2ca26",
    "instance_qutebrowser__qutebrowser-a84ecfb80a00f8ab7e341372560458e3f9cfffa2-v2ef375ac784985212b1805e1d0431dc8f1b3c171",
    "instance_qutebrowser__qutebrowser-5fdc83e5da6222fe61163395baaad7ae57fa2cb4-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d",
    "instance_NodeBB__NodeBB-da0211b1a001d45d73b4c84c6417a4f1b0312575-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e",
    "instance_NodeBB__NodeBB-f2082d7de85eb62a70819f4f3396dd85626a0c0a-vd59a5728dfc977f44533186ace531248c2917516",
    "instance_element-hq__element-web-41dfec20bfe9b62cddbbbf621bef2e9aa9685157-vnan",
    "instance_element-hq__element-web-ad26925bb6628260cfe0fcf90ec0a8cba381f4a4-vnan",
)

# The 12 tasks of the second probe: chosen by :func:`probe2_candidates` (seed 20261111) and validated
# at $0 (empty patch scores 0, gold through the SEARCH/REPLACE path scores 1). Two first picks whose
# counted prompt exceeded 90,000 tokens were replaced by the next two queue entries; see
# results/swepro-probe2/PREREG.md for the candidate order and any replacement.
PROBE2_IDS: tuple[str, ...] = (
    "instance_NodeBB__NodeBB-8168c6c40707478f71b8af60300830fe554c778c-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e",
    "instance_ansible__ansible-d62496fe416623e88b90139dc7917080cb04ce70-v0f01c69f1e2528b935359cfe578530722bca2c59",
    "instance_ansible__ansible-40ade1f84b8bb10a63576b0ac320c13f57c87d34-v6382ea168a93d80a64aab1fbd8c4f02dc5ada5bf",
    "instance_ansible__ansible-b748edea457a4576847a10275678127895d2f02f-v1055803c3a812189a1133297f7f5468579283f86",
    "instance_ansible__ansible-d72025be751c894673ba85caa063d835a0ad3a8c-v390e508d27db7a51eece36bb6d9698b63a5b638a",
    "instance_element-hq__element-web-66d0b318bc6fee0d17b54c1781d6ab5d5d323135-vnan",
    "instance_internetarchive__openlibrary-53d376b148897466bb86d5accb51912bbbe9a8ed-v08d8e8889ec945ab821fb156c04c7d2e2810debb",
    "instance_internetarchive__openlibrary-630221ab686c64e75a2ce253c893c033e4814b2e-v93c53c13d5f9b383ebb411ee7750b49dcd1a34c6",
    "instance_internetarchive__openlibrary-322d7a46cdc965bfabbf9500e98fde098c9d95b2-v13642507b4fc1f8d234172bf8129942da2c2ca26",
    "instance_qutebrowser__qutebrowser-3e21c8214a998cb1058defd15aabb24617a76402-v5fc38aaf22415ab0b70567368332beee7955b367",
    "instance_ansible__ansible-e9e6001263f51103e96e58ad382660df0f3d0e39-v30a923fb5c164d6cd18280c02422f75e611e8fb2",
    "instance_element-hq__element-web-ecfd1736e5dd9808e87911fc264e6c816653e1a9-vnan",
)

_CODE_DIRS = {"tests", "test", "__tests__", "spec", "specs"}
_DATA_DIRS = {"testdata", "test_data", "fixtures", "fixture", "mocks", "mock", "__snapshots__"}


def is_test_path(path: str) -> bool:
    """The official verifier's test-code rule (``test.sh`` resets these paths before testing,
    so an edit to one could never count). Workers may never edit them."""
    segs = path.split("/")
    parents, b = segs[:-1], segs[-1]
    if any(s in _DATA_DIRS for s in parents):
        return False
    if any(s in _CODE_DIRS for s in parents):
        return True
    if b == "conftest.py":
        return True
    return bool(re.search(r"_test\.(go|py|js|jsx|ts|tsx)$", b) or re.search(r"^test_.*\.py$", b)
                or re.search(r"\.(test|spec)\.(js|jsx|ts|tsx)$", b))


# Contract patterns for the same rule (fnmatch), added to contracts.DEFAULT_PROTECTED.
TEST_PATTERNS: tuple[str, ...] = (
    "tests/*", "*/tests/*", "test/*", "*/test/*", "__tests__/*", "*/__tests__/*", "spec/*", "*/spec/*",
    "specs/*", "*/specs/*", "conftest.py", "*/conftest.py", "test_*.py", "*/test_*.py", "*_test.py",
    "*_test.js", "*_test.ts", "*_test.tsx", "*_test.jsx", "*.test.*", "*.spec.*",
)


def _json_list(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        return tuple(str(x) for x in raw)
    if not raw:
        return ()
    try:
        v = json.loads(raw)
    except ValueError:
        import ast

        v = ast.literal_eval(raw)  # literal parsing only; never evaluated as code
    return tuple(str(x) for x in v)


# ------------------------------------------------------------------ tasks


@dataclass(frozen=True)
class ProTask:
    instance_id: str
    repo: str
    repo_language: str  # dataset value: "py" | "js"
    base_commit: str
    problem_statement: str
    requirements: str
    interface: str
    patch: str  # gold patch: never shown to any model
    test_patch: str  # never shown to any model
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    selected_test_files: tuple[str, ...]
    image: str
    hard: bool = False

    @property
    def id(self) -> str:
        return self.instance_id

    @property
    def language(self) -> str:
        return LANGUAGE_NAMES.get(self.repo_language, self.repo_language)

    def gold_paths(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(files the gold patch modifies, files it creates), test files excluded, sorted."""
        modified, created = [], []
        for fd in parse_unified_diff(self.patch):
            if is_test_path(fd.path):
                continue
            (created if fd.old_path is None else modified).append(fd.path)
        return tuple(sorted(set(modified))), tuple(sorted(set(created)))

    def unsupported_gold(self) -> str | None:
        """Why the gold patch cannot be expressed as SEARCH/REPLACE blocks (it deletes or renames a
        non-test file), or None. Such tasks are excluded: a worker could not express the reference
        solution either, and the scorer could not be validated on them."""
        for fd in parse_unified_diff(self.patch):
            if not is_test_path(fd.path) and (fd.deleted or fd.renamed):
                return f"the gold patch {'deletes' if fd.deleted else 'renames'} {fd.path}"
        return None

    def fingerprint(self) -> str:
        return sha256_json({
            "instance_id": self.instance_id, "base_commit": self.base_commit, "image": self.image,
            "patch": sha256_text(self.patch), "test_patch": sha256_text(self.test_patch),
            "fail_to_pass": list(self.fail_to_pass), "pass_to_pass": list(self.pass_to_pass),
            "statement": sha256_text(self.problem_statement + "\0" + self.requirements + "\0" + self.interface),
        })


def task_from_row(row: Mapping[str, Any]) -> ProTask:
    return ProTask(
        instance_id=row["instance_id"], repo=row["repo"], repo_language=row["repo_language"],
        base_commit=row["base_commit"], problem_statement=row.get("problem_statement") or "",
        requirements=row.get("requirements") or "", interface=row.get("interface") or "",
        patch=row["patch"], test_patch=row["test_patch"], fail_to_pass=_json_list(row["fail_to_pass"]),
        pass_to_pass=_json_list(row["pass_to_pass"]),
        selected_test_files=_json_list(row.get("selected_test_files_to_run") or "[]"),
        image=row["docker_image"], hard=bool(row.get("hard", False)),
    )


def load_tasks(path: str | Path, repos: Iterable[str] = REPOS) -> list[ProTask]:
    """In-scope tasks from a JSON-lines file written by :func:`fetch_dataset`, sorted by id."""
    keep = set(repos)
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row["repo"] in keep:
                out.append(task_from_row(row))
    if not out:
        raise FileNotFoundError(f"no in-scope tasks in {path}")
    return sorted(out, key=lambda t: t.instance_id)


def dataset_sha256(tasks: Sequence[ProTask]) -> str:
    return sha256_json({t.instance_id: t.fingerprint() for t in sorted(tasks, key=lambda t: t.instance_id)})


def _get_json(url: str, *, timeout: float = 60.0, retries: int = 4) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"user-agent": "contraprova-harness"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = e
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"GET {url} failed: {last}")


def fetch_dataset(dest: str | Path, *, repos: Iterable[str] = REPOS, page: int = 100) -> int:
    """Download V2 from the datasets server and write the in-scope rows to ``dest`` (JSON lines,
    sorted by id). Refuses rows the server truncated. Returns the number of rows written."""
    keep = set(repos)
    rows: list[dict] = []
    offset, total = 0, None
    while total is None or offset < total:
        q = urllib.parse.urlencode({"dataset": DATASET, "config": DATASET_CONFIG, "split": "test",
                                    "offset": offset, "length": page})
        data = _get_json(f"{ROWS_URL}?{q}")
        total = int(data["num_rows_total"])
        for item in data["rows"]:
            if item.get("truncated_cells"):
                raise RuntimeError(f"row {item.get('row_idx')}: server truncated {item['truncated_cells']}")
            if item["row"]["repo"] in keep:
                rows.append(item["row"])
        offset += page
    rows.sort(key=lambda r: r["instance_id"])
    p = Path(dest)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return len(rows)


# ------------------------------------------------------------------ test split


@dataclass(frozen=True)
class ProTestSplit:
    __test__ = False

    exercise_id: str
    method: str  # "f2p-half"
    tests: tuple[str, ...]  # fail-to-pass, dataset order
    visible: tuple[str, ...]
    gate_private: tuple[str, ...]
    score: tuple[str, ...]  # fail-to-pass + pass-to-pass

    def to_dict(self) -> dict:
        return {"method": self.method, "tests": list(self.tests), "visible": list(self.visible),
                "gate_private": list(self.gate_private), "score": list(self.score)}

    @classmethod
    def from_dict(cls, exercise_id: str, d: Mapping) -> "ProTestSplit":
        return cls(exercise_id, d["method"], *(tuple(d[k]) for k in ("tests", "visible", "gate_private", "score")))


def split_tests(task: ProTask, seed: int = DEFAULT_SEED) -> ProTestSplit:
    """Visible = ceil(n/2) of the n fail-to-pass tests (ranked by sha256 of seed, task and test id),
    gate-private = the rest, score = fail-to-pass + pass-to-pass. Lists keep dataset order."""
    f2p = tuple(dict.fromkeys(task.fail_to_pass))
    ranked = sorted(f2p, key=lambda t: sha256_text(f"{seed}:{task.instance_id}:{t}"))
    nv = max(1, math.ceil(len(f2p) / 2)) if f2p else 0
    vis = set(ranked[:nv])
    visible = tuple(t for t in f2p if t in vis)
    private = tuple(t for t in f2p if t not in vis)
    score = tuple(dict.fromkeys((*f2p, *task.pass_to_pass)))
    return ProTestSplit(task.instance_id, "f2p-half", f2p, visible, private, score)


# ------------------------------------------------------------------ partition


def gold_size(task: ProTask) -> tuple[int, int]:
    """(non-test files the gold patch touches, non-test lines it adds or removes)."""
    files, lines, cur = set(), 0, None
    for ln in task.patch.splitlines():
        if ln.startswith("diff --git "):
            cur = ln.split(" b/", 1)[-1]
            if not is_test_path(cur):
                files.add(cur)
        elif cur and not is_test_path(cur) and ln[:1] in "+-" and not ln.startswith(("+++ ", "--- ")):
            lines += 1
    return len(files), lines


def probe2_candidates(tasks: Sequence[ProTask], seed: int = DEFAULT_SEED, *, n: int = PROBE2_SIZE,
                      exclude: Iterable[str] = PROBE_IDS,
                      fits: Callable[[ProTask], str | None] | None = None) -> dict[str, Any]:
    """The second probe's selection rule (no model, no execution; $0).

    Pool: HARD-51 tasks (``task.hard``) not in ``exclude``, whose gold patch the edit format can
    express and, if ``fits`` is given, for which ``fits(task)`` returns None (e.g. worker prompt
    within the token bound; a string is the reason a task is left out). Each repository's pool is
    shuffled with ``Random(f"{seed}:probe2:{repo}")``; the ``n`` places are shared out across
    repositories in proportion to pool size (largest remainder, ties by repository name). Returns
    ``selected`` (in repository order), ``queue`` (replacements, in the order to use them: the rest of
    each repository's hard pool interleaved, then the fallback) and ``left_out`` (id -> reason).
    The fallback, used when the hard pool is short, is every other eligible task ordered by most
    gold files, then most changed lines, then id.
    """
    excl = set(exclude)
    left_out: dict[str, str] = {}
    pool: dict[str, list[str]] = {}
    rest: list[ProTask] = []
    for t in sorted(tasks, key=lambda t: t.instance_id):
        if t.instance_id in excl:
            continue
        why = t.unsupported_gold() or (fits(t) if fits else None)
        if why:
            if t.hard:
                left_out[t.instance_id] = why
            continue
        if t.hard:
            pool.setdefault(t.repo, []).append(t.instance_id)
        else:
            rest.append(t)
    for repo in pool:
        random.Random(f"{seed}:probe2:{repo}").shuffle(pool[repo])
    total = sum(len(v) for v in pool.values())
    repos = sorted(pool)
    take = {r: 0 for r in repos}
    if total:
        k = min(n, total)
        exact = {r: len(pool[r]) * k / total for r in repos}
        take = {r: min(int(exact[r]), len(pool[r])) for r in repos}
        for r in sorted(repos, key=lambda r: (-(exact[r] - int(exact[r])), r)):
            if sum(take.values()) >= k:
                break
            if take[r] < len(pool[r]):
                take[r] += 1
    selected = [tid for r in repos for tid in pool[r][: take[r]]]
    leftovers = {r: pool[r][take[r]:] for r in repos}
    queue: list[str] = []
    while any(leftovers.values()):
        for r in repos:
            if leftovers[r]:
                queue.append(leftovers[r].pop(0))
    fallback = [t.instance_id for t in sorted(rest, key=lambda t: (-gold_size(t)[0], -gold_size(t)[1], t.instance_id))]
    short = n - len(selected)
    selected += fallback[:short] if short > 0 else []
    queue += fallback[max(short, 0):]
    return {"selected": selected, "queue": queue, "left_out": left_out,
            "quota": take, "hard_pool": {r: len(v) for r, v in pool.items()}}


def partition_tasks(tasks: Sequence[ProTask], seed: int = DEFAULT_SEED,
                    probe_ids: Sequence[str] = PROBE_IDS,
                    probe2_ids: Sequence[str] = PROBE2_IDS) -> dict[str, tuple[str, ...]]:
    """probe = ``probe_ids`` exactly; probe2 = ``probe2_ids`` exactly; smoke = ``SMOKE_PER_REPO`` per
    repository from the rest; main = everything else in a seeded, repository-interleaved order (see
    the module docstring). Neither probe set ever enters smoke or main."""
    ids = {t.instance_id for t in tasks}
    missing = [i for i in probe_ids if i not in ids]
    if missing:
        raise ValueError(f"probe tasks missing from the dataset: {missing}")
    missing2 = [i for i in probe2_ids if i not in ids]
    if missing2:
        raise ValueError(f"probe2 tasks missing from the dataset: {missing2}")
    if set(probe_ids) & set(probe2_ids):
        raise ValueError("probe and probe2 overlap")
    held = set(probe_ids) | set(probe2_ids)
    by_repo: dict[str, list[str]] = {}
    for t in tasks:
        if t.instance_id not in held:
            by_repo.setdefault(t.repo, []).append(t.instance_id)
    smoke: list[str] = []
    rest: dict[str, list[str]] = {}
    for repo in sorted(by_repo):
        pool = sorted(by_repo[repo])
        random.Random(f"{seed}:{repo}").shuffle(pool)
        smoke.extend(pool[:SMOKE_PER_REPO])
        rest[repo] = pool[SMOKE_PER_REPO:]
    # Interleave: the k-th task of a repository with n tasks sits at (k + 0.5) / n.
    keyed = [((k + 0.5) / len(pool), repo, tid) for repo, pool in rest.items() for k, tid in enumerate(pool)]
    main = tuple(tid for _, _, tid in sorted(keyed))
    return {"smoke": tuple(sorted(smoke)), "probe": tuple(sorted(probe_ids)), "probe2": tuple(sorted(probe2_ids)),
            "main": main}


def make_partition(tasks: Sequence[ProTask], seed: int = DEFAULT_SEED,
                   probe2_ids: Sequence[str] | None = None) -> Partition:
    splits = partition_tasks(tasks, seed, probe2_ids=PROBE2_IDS if probe2_ids is None else probe2_ids)
    ts = {t.instance_id: split_tests(t, seed).to_dict() for t in tasks}
    return Partition(seed, {k: len(v) for k, v in splits.items()}, splits, dataset_sha256(tasks), ts)


def load_or_create_partition(path: str | Path, tasks: Sequence[ProTask], seed: int = DEFAULT_SEED) -> Partition:
    p = Path(path)
    if p.exists():
        part = Partition.load(p)
        if part.dataset_sha256 != dataset_sha256(tasks):
            raise ValueError(f"{p}: frozen for a different dataset (dataset_sha256 mismatch)")
        return part
    part = make_partition(tasks, seed)
    part.save(p)
    return part


# ------------------------------------------------------------------ Contract Lanes task selection

# docs/lanes-plan.md section 3. The worker-prompt estimate is (gold-file bytes + statement chars) / 3
# plus the median overhead measured against the main log's worker_prompt_tokens_est (1,212), scaled
# by the median counted/estimated ratio of the main pre-flight (1.075). Exact eligibility is decided
# later by the free count_tokens pre-flight.
LANE_PROMPT_OVERHEAD = 1212
LANE_COUNT_SCALE = 1.075
LANE_MIN_FILES = 3
LANE_TIER1_LINES = 150
LANE_TIER2_LINES = 100
LANE_PROMPT_CAP = 90_000
LANE_TIER3_CAP = 180_000
LANE_MAIN_TIER2 = 10
LANE_PROBE_PER_SOURCE = 6
LANES_SPLITS_VERSION = 1


def lane_prompt_estimate(task: ProTask, source_bytes: int) -> int:
    """Estimated counted worker-prompt tokens for the lanes selection rule (see above)."""
    stmt = len(task.problem_statement) + len(task.requirements) + len(task.interface)
    return math.ceil(((source_bytes + stmt) / CHARS_PER_TOKEN + LANE_PROMPT_OVERHEAD) * LANE_COUNT_SCALE)


def lane_tier(task: ProTask, prompt_tokens: int) -> int | None:
    """1: >=3 gold files, >=150 changed lines, prompt <=90K; 2: >=3 files, 100-149 lines, <=90K;
    3: >=3 files, >=100 lines, 90K-180K; None otherwise (also when the gold patch deletes or renames
    a file, which the edit format cannot express)."""
    if task.unsupported_gold():
        return None
    nf, nl = gold_size(task)
    if nf < LANE_MIN_FILES or nl < LANE_TIER2_LINES:
        return None
    if prompt_tokens <= LANE_PROMPT_CAP:
        return 1 if nl >= LANE_TIER1_LINES else 2
    return 3 if prompt_tokens <= LANE_TIER3_CAP else None


def lanes_selection(tasks: Sequence[ProTask], partition: Partition, *, main90: Iterable[str],
                    main90_scored: Iterable[str], source_bytes: Callable[[ProTask], int],
                    seed: int = DEFAULT_SEED, n_tier2: int = LANE_MAIN_TIER2,
                    probe_per_source: int = LANE_PROBE_PER_SOURCE) -> dict[str, Any]:
    """The Contract Lanes task lists (no model, no execution; $0).

    Fresh pool: tasks of the partition's ``main`` split (its seeded, repository-interleaved order)
    not in ``probe``, ``probe2`` or ``main90`` (the main study's eligible list), sorted into
    :func:`lane_tier` tiers. ``lanes-main`` = every tier-1 task, then the first ``n_tier2`` tier-2
    tasks, each in that order.

    ``lanes-probe`` = already-used large tasks (tier 1: >=3 files, >=150 lines, prompt <=90K,
    expressible): ``probe_per_source`` from ``main90_scored`` (main-90 tasks with both an A and a D
    outcome; a task that failed validation has none) and as many from probe + probe2, each source
    ranked by sha256 of (seed, "lanes-probe", id). ``source_bytes(task)`` is the UTF-8 size of the
    files the gold patch modifies (the worker's context)."""
    splits = partition.splits
    probes = set(splits.get("probe", ())) | set(splits.get("probe2", ()))
    m90, scored = set(main90), set(main90_scored)
    by_id = {t.instance_id: t for t in tasks}
    est = {t.instance_id: lane_prompt_estimate(t, source_bytes(t)) for t in tasks}
    tiers: dict[int, list[str]] = {1: [], 2: [], 3: []}
    for tid in splits["main"]:
        if tid in probes or tid in m90 or tid not in by_id:
            continue
        tier = lane_tier(by_id[tid], est[tid])
        if tier is not None:
            tiers[tier].append(tid)
    main = tiers[1] + tiers[2][:n_tier2]

    def large(tid: str) -> bool:
        t = by_id.get(tid)
        return t is not None and lane_tier(t, est[tid]) == 1

    def rank(tid: str) -> str:
        return sha256_text(f"{seed}:lanes-probe:{tid}")

    from_main = sorted((i for i in scored & m90 if large(i)), key=rank)
    from_probes = sorted((i for i in probes if large(i)), key=rank)
    if len(from_main) < probe_per_source or len(from_probes) < probe_per_source:
        raise ValueError(f"lanes-probe needs {probe_per_source} large tasks per source; found "
                         f"{len(from_main)} (main 90) and {len(from_probes)} (probe 1/2)")
    probe = from_main[:probe_per_source] + from_probes[:probe_per_source]
    return {"lanes-probe": probe, "lanes-main": main, "tiers": tiers,
            "probe_sources": {"main90": from_main[:probe_per_source], "probe12": from_probes[:probe_per_source]},
            "prompt_tokens_est": {i: est[i] for i in sorted(set(probe) | set(main) | set(tiers[3]))}}


@dataclass(frozen=True)
class LaneSplits:
    """The Contract Lanes task lists, frozen with their sha256 (like :class:`Partition`)."""

    splits: Mapping[str, tuple[str, ...]]  # split -> ids in run order (not sorted)
    base_partition_sha256: str
    dataset_sha256: str
    rule: Mapping[str, Any]
    tiers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    probe_sources: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    prompt_tokens_est: Mapping[str, int] = field(default_factory=dict)

    def body(self) -> dict:
        return {"version": LANES_SPLITS_VERSION, "splits": {k: list(v) for k, v in self.splits.items()},
                "sizes": {k: len(v) for k, v in self.splits.items()},
                "base_partition_sha256": self.base_partition_sha256, "dataset_sha256": self.dataset_sha256,
                "rule": dict(self.rule), "tiers": {k: list(v) for k, v in self.tiers.items()},
                "probe_sources": {k: list(v) for k, v in self.probe_sources.items()},
                "prompt_tokens_est": dict(sorted(self.prompt_tokens_est.items()))}

    @property
    def sha256(self) -> str:
        return sha256_json(self.body())

    def split_sha256(self, name: str) -> str:
        """sha256 of one task list (its ids in run order), for pre-registration."""
        return sha256_json(list(self.splits[name]))

    def save(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**self.body(), "sha256": self.sha256,
                                 "split_sha256": {k: self.split_sha256(k) for k in self.splits}},
                                indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return self.sha256

    @classmethod
    def load(cls, path: str | Path) -> "LaneSplits":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        ls = cls({k: tuple(v) for k, v in d["splits"].items()}, d["base_partition_sha256"], d["dataset_sha256"],
                 d["rule"], {k: tuple(v) for k, v in d.get("tiers", {}).items()},
                 {k: tuple(v) for k, v in d.get("probe_sources", {}).items()}, d.get("prompt_tokens_est", {}))
        if d.get("sha256") != ls.sha256:
            raise ValueError(f"lane splits {path}: sha256 does not match its content")
        return ls


def make_lane_splits(tasks: Sequence[ProTask], partition: Partition, **kw: Any) -> LaneSplits:
    """:func:`lanes_selection` frozen as a :class:`LaneSplits` (the rule's parameters included)."""
    sel = lanes_selection(tasks, partition, **kw)
    rule = {"min_files": LANE_MIN_FILES, "tier1_lines": LANE_TIER1_LINES, "tier2_lines": LANE_TIER2_LINES,
            "prompt_cap": LANE_PROMPT_CAP, "tier3_cap": LANE_TIER3_CAP, "overhead": LANE_PROMPT_OVERHEAD,
            "count_scale": LANE_COUNT_SCALE, "n_tier2": kw.get("n_tier2", LANE_MAIN_TIER2),
            "probe_per_source": kw.get("probe_per_source", LANE_PROBE_PER_SOURCE),
            "seed": kw.get("seed", DEFAULT_SEED), "order": "partition main order (seeded, repository-interleaved)"}
    return LaneSplits({k: tuple(sel[k]) for k in LANE_SPLITS}, partition.sha256, partition.dataset_sha256, rule,
                      {str(k): tuple(v) for k, v in sel["tiers"].items()},
                      {k: tuple(v) for k, v in sel["probe_sources"].items()}, sel["prompt_tokens_est"])


def scored_tasks(log_path: str | Path, arms: Sequence[str] = ("A", "D")) -> set[str]:
    """Pairing keys with an own (not derived) arm outcome for every arm in ``arms`` in a JSON-lines log."""
    seen: dict[str, set[str]] = {}
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        p = e.get("payload", {})
        if (e.get("type") == "outcome.scored" and p.get("fork_id") is None and p.get("gate_seq") is None
                and p.get("arm") in arms and p.get("derived_from") is None):
            seen.setdefault(str(p["task_id"]), set()).add(str(p["arm"]))
    return {t for t, a in seen.items() if set(arms) <= a}


def context_bytes(task: ProTask, data_dir: str | Path) -> int:
    """UTF-8 size of the cached context files of ``task`` (the files its gold patch modifies)."""
    modified, _ = task.gold_paths()
    root = Path(data_dir) / "context" / task.instance_id / "files"
    return sum((root / p).stat().st_size for p in modified if (root / p).is_file())


# ------------------------------------------------------------------ verifier files


def _get_bytes(url: str, *, timeout: float = 60.0, retries: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"user-agent": "contraprova-harness"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FileNotFoundError(url) from e
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"GET {url} failed: {last}")


def check_verifier(task: ProTask, d: Path) -> None:
    """The downloaded config and test patch must describe the same tests as the dataset row."""
    cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
    if set(_json_list(cfg.get("fail_to_pass", []))) != set(task.fail_to_pass) or \
            set(_json_list(cfg.get("pass_to_pass", []))) != set(task.pass_to_pass):
        raise ValueError(f"{task.instance_id}: verifier config.json disagrees with the dataset's test lists")
    tp = (d / "test_patch.patch").read_text(encoding="utf-8")
    if tp.strip() != task.test_patch.strip():
        raise ValueError(f"{task.instance_id}: verifier test_patch.patch differs from the dataset's test_patch")


def verifier_dir(task: ProTask, data_dir: str | Path, *, fetch: bool = True) -> Path:
    """The directory holding the task's official verifier files (downloaded once, then cached)."""
    d = Path(data_dir) / "verifiers" / task.instance_id
    if all((d / f).is_file() for f in VERIFIER_FILES):
        return d
    if not fetch:
        raise FileNotFoundError(f"verifier files for {task.instance_id} are not in {d}")
    tmp = d.with_name(d.name + ".partial")
    tmp.mkdir(parents=True, exist_ok=True)
    for f in VERIFIER_FILES:
        (tmp / f).write_bytes(_get_bytes(f"{VERIFIER_BASE}/{task.instance_id}/tests/{f}"))
    check_verifier(task, tmp)
    if d.exists():
        import shutil

        shutil.rmtree(d)
    tmp.rename(d)
    return d


def verifier_sha256(d: Path) -> str:
    return sha256_json({f: sha256_text((d / f).read_text(encoding="utf-8", errors="replace")) for f in VERIFIER_FILES})


# ------------------------------------------------------------------ context


@dataclass(frozen=True)
class TaskFiles:
    """The current content of the files a task may change, as the worker sees them."""

    files: Mapping[str, str]  # path -> content at the base commit (modified files only)
    new_files: tuple[str, ...]  # files the task may create
    source: str  # "image" | "raw"
    missing: tuple[str, ...] = ()  # modified paths that were not found (should be empty)

    def chars(self) -> int:
        return sum(len(v) for v in self.files.values())


def _read_tree(root: Path, paths: Sequence[str]) -> dict[str, str]:
    out = {}
    for p in paths:
        f = root / p
        if f.is_file():
            out[p] = f.read_bytes().decode("utf-8", errors="replace")
    return out


def load_context(task: ProTask, data_dir: str | Path, *, source: str = "image", ensure_image=None) -> TaskFiles:
    """Load (and cache under ``data_dir/context/<id>``) the files the task may change.

    ``source="image"`` copies them out of the task image (``ensure_image(image)`` is called first,
    e.g. ``ImageCache.ensure``); ``source="raw"`` downloads them from the repository at the base
    commit. A cached copy is reused whatever its source."""
    modified, created = task.gold_paths()
    root = Path(data_dir) / "context" / task.instance_id
    meta = root / "meta.json"
    if meta.is_file():
        m = json.loads(meta.read_text(encoding="utf-8"))
        files = _read_tree(root / "files", modified)
        return TaskFiles(files, created, m.get("source", "?"), tuple(p for p in modified if p not in files))
    tmp = root.with_name(root.name + ".partial")
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    (tmp / "files").mkdir(parents=True)
    if source == "image":
        from .docker import read_files

        if ensure_image is not None:
            ensure_image(task.image)
        read_files(task.image, modified, tmp / "files")
    elif source == "raw":
        for p in modified:
            try:
                data = _get_bytes(f"{RAW_BASE}/{task.repo}/{task.base_commit}/{urllib.parse.quote(p)}")
            except FileNotFoundError:
                continue
            f = tmp / "files" / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
    else:
        raise ValueError(f"unknown context source {source!r}")
    files = _read_tree(tmp / "files", modified)
    (tmp / "meta.json").write_text(json.dumps({"source": source, "files": sorted(files), "created": list(created),
                                               "sha256": sha256_json(files)}, indent=1), encoding="utf-8")
    if root.exists():
        import shutil

        shutil.rmtree(root)
    tmp.rename(root)
    return TaskFiles(files, created, source, tuple(p for p in modified if p not in files))


def estimate_tokens(text: str) -> int:
    """Conservative prompt-token estimate (``CHARS_PER_TOKEN``)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


# ------------------------------------------------------------------ the exercise the harness sees


@dataclass
class ProExercise:
    """A task plus its context, shaped like ``polyglot.Exercise`` so ``arms.Harness`` can drive it."""

    task: ProTask
    context: TaskFiles
    test_split: ProTestSplit
    verifier: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # prompt token estimates etc., for the log

    @property
    def id(self) -> str:
        return self.task.instance_id

    @property
    def name(self) -> str:
        return self.task.instance_id

    @property
    def language(self) -> str:
        return self.task.language

    @property
    def solution_files(self) -> tuple[str, ...]:
        """Paths the worker may write: the files shown plus the files it may create."""
        return tuple(sorted(set(self.context.files) | set(self.context.new_files)))

    @property
    def test_files(self) -> tuple[str, ...]:
        return ()

    @property
    def extra_test_files(self) -> tuple[str, ...]:
        return ()

    def stubs(self) -> dict[str, str]:
        return dict(self.context.files)

    def instructions(self) -> str:
        return self.task.problem_statement
