"""Offline tests for the SWE-bench Pro harness: synthetic tasks, fake clients, no Docker, no network."""

import json
from pathlib import Path

import pytest

from contraprova.harness.arms import BudgetGuard
from contraprova.harness.docker import ImageCache, docker_available
from contraprova.harness.edits import unified_patch
from contraprova.harness.outline import outline_js, outline_python, render_outline
from contraprova.harness.polyglot import Partition
from contraprova.harness.run import FixedRouter, UniformRouter
from contraprova.harness.swepro import (
    PROBE2_IDS,
    PROBE_IDS,
    gold_size,
    probe2_candidates,
    ProExercise,
    TaskFiles,
    dataset_sha256,
    estimate_tokens,
    is_test_path,
    load_context,
    load_tasks,
    make_partition,
    partition_tasks,
    split_tests,
    task_from_row,
)
from contraprova.harness.swepro_arms import (
    PRO_LEAD_SYSTEM,
    LEAD_MAX_TOKENS_EST,
    OracleResult,
    ProFakeClient,
    ProGates,
    ProHarness,
    ProScorer,
    Verifier,
    gold_files,
    gold_oracle,
    gold_reply,
    preflight,
    pro_arm_config,
    run_tasks,
)
from contraprova.interfaces import HAIKU, OPUS, SONNET
from contraprova.log import EventLog
from contraprova.projections import outcomes, project_tasks

# ------------------------------------------------------------------ synthetic tasks

PY_SRC = '''"""Inventory helpers."""
import os
from typing import Any

LIMIT = 10


class Grouper(object):
    """Group hosts by keys."""

    def __init__(self, keys, *, strict=False):
        self.keys = keys
        self.secret_marker_alpha = strict

    def group(self, host: dict, default: Any = None) -> list:
        """Return the group names for a host."""
        names = []
        for key in self.keys:
            value = host.get(key, default)
            if value is None:
                continue
            names.append(f"{key}_{value}")
        return names


def build(keys):
    return Grouper(keys).group({"secret_marker_beta": 1})
'''

JS_SRC = """'use strict';

const db = require('../database');
// a comment with function fake() {
const Posts = module.exports;

Posts.getPost = async function (pid) {
\tconst post = await db.getObject(`post:${pid}`);
\treturn post.secret_marker_gamma;
};

function helper(a, b) {
\treturn a + b + 'secret_marker_delta';
}

class Cache extends Map {
\tconstructor(opts) {
\t\tsuper();
\t\tthis.opts = opts;
\t}

\tpeek(key) {
\t\treturn this.get(key);
\t}
}

module.exports.helper = helper;
"""

REPO_OF = {"ansible": "ansible/ansible", "internetarchive": "internetarchive/openlibrary",
           "qutebrowser": "qutebrowser/qutebrowser", "NodeBB": "NodeBB/NodeBB", "element-hq": "element-hq/element-web"}


def _repo(iid):
    return next(r for k, r in REPO_OF.items() if iid.startswith(f"instance_{k}__"))


def make_row(iid):
    repo = _repo(iid)
    js = repo in ("NodeBB/NodeBB", "element-hq/element-web")
    path = "src/posts.js" if js else "lib/inventory.py"
    orig = {path: JS_SRC if js else PY_SRC}
    new = dict(orig)
    if js:
        new[path] = JS_SRC.replace("return a + b + 'secret_marker_delta';", "return a * b;")
    else:
        new[path] = PY_SRC.replace("            if value is None:\n                continue\n",
                                   "            if value is None:\n                value = 'none'\n")
    new["changelogs/fragments/x.yml"] = "bugfixes: [x]\n"
    patch = unified_patch(orig, new)
    f2p = [f"tests/test_x.py::test_{i}" for i in range(5)] if not js else [f"test/x.js | X | case {i}" for i in range(3)]
    return {
        "instance_id": iid, "repo": repo, "repo_language": "js" if js else "py", "base_commit": "b" * 40,
        "problem_statement": f"Fix grouping for {iid[-8:]}.", "requirements": "- Must handle None.",
        "interface": "No new interfaces are introduced.", "patch": patch,
        "test_patch": "diff --git a/tests/t.py b/tests/t.py\n", "fail_to_pass": json.dumps(f2p),
        "pass_to_pass": json.dumps(["tests/test_x.py::test_old"]), "selected_test_files_to_run": "[]",
        "docker_image": f"img:{iid}", "hard": False, "_files": orig,
    }


EXTRA = [f"instance_{k}__{r.split('/')[1]}-{'%040x' % (i + 7 * n)}-vnan" for n, (k, r) in enumerate(REPO_OF.items())
         for i in range(4)]
ALL_IDS = list(PROBE_IDS) + list(PROBE2_IDS) + EXTRA


@pytest.fixture(scope="module")
def rows():
    return [make_row(i) for i in ALL_IDS]


@pytest.fixture()
def data_dir(tmp_path, rows):
    """A data dir with tasks.jsonl and a pre-filled context cache (so nothing is downloaded)."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "tasks.jsonl").write_text("".join(json.dumps({k: v for k, v in r.items() if k != "_files"}) + "\n"
                                           for r in rows))
    for r in rows:
        root = d / "context" / r["instance_id"]
        for p, text in r["_files"].items():
            (root / "files" / p).parent.mkdir(parents=True, exist_ok=True)
            (root / "files" / p).write_text(text)
        (root / "meta.json").write_text(json.dumps({"source": "raw"}))
    return d


@pytest.fixture()
def exercises(data_dir):
    tasks = load_tasks(data_dir / "tasks.jsonl")
    out = {}
    for t in tasks:
        ctx = load_context(t, data_dir, source="raw")
        out[t.instance_id] = ProExercise(t, ctx, split_tests(t))
    return out


def harness(tmp_path, exercises, *, solve=lambda i: True, review="ACCEPT", budget=None, gates=True, router=None,
            config=None, run_fn=None):
    v = Verifier(run_fn or gold_oracle(exercises))
    return ProHarness(EventLog(tmp_path / "log.jsonl"), ProFakeClient(exercises, solve, review), ProScorer(v),
                      ProGates(v) if gates else None, router or UniformRouter(), BudgetGuard(budget),
                      config or pro_arm_config(), "p" * 64), v


# ------------------------------------------------------------------ dataset, split, partition


def test_task_fields_and_gold_paths(rows):
    t = task_from_row(rows[0])
    assert t.language == "python" and t.fail_to_pass[0].startswith("tests/")
    mod, created = t.gold_paths()
    assert mod == ("lib/inventory.py",) and created == ("changelogs/fragments/x.yml",)
    assert task_from_row(make_row(PROBE_IDS[-1])).language == "javascript"


@pytest.mark.parametrize("path,is_test", [
    ("tests/unit/test_a.py", True), ("test/user.js", True), ("src/a.test.ts", True), ("lib/conftest.py", True),
    ("lib/x_test.py", True), ("test/mocks/databasemock.js", False), ("src/fixtures/a.py", False),
    ("lib/ansible/x.py", False), ("src/components/Pill.tsx", False),
])
def test_is_test_path_matches_the_official_rule(path, is_test):
    assert is_test_path(path) is is_test


def test_split_tests_is_deterministic_half_visible(rows):
    t = task_from_row(rows[0])
    s = split_tests(t, 1)
    assert len(s.visible) == 3 and len(s.gate_private) == 2  # ceil(5 / 2)
    assert set(s.visible) | set(s.gate_private) == set(t.fail_to_pass) and not set(s.visible) & set(s.gate_private)
    assert s.score[-1] == "tests/test_x.py::test_old" and set(t.fail_to_pass) <= set(s.score)
    assert split_tests(t, 1) == s  # deterministic for a seed
    assert {split_tests(t, k).visible for k in range(8)} != {s.visible}  # and the seed matters
    one = task_from_row({**rows[0], "fail_to_pass": '["only"]'})
    assert split_tests(one).visible == ("only",) and split_tests(one).gate_private == ()


def test_partition_probe_is_exact_and_stratified(data_dir, tmp_path):
    tasks = load_tasks(data_dir / "tasks.jsonl")
    sp = partition_tasks(tasks, 5)
    assert set(sp["probe"]) == set(PROBE_IDS) and len(sp["probe"]) == 12
    assert len(sp["smoke"]) == 5 and len({_repo(i) for i in sp["smoke"]}) == 5
    assert set(sp["main"]) == set(EXTRA) - set(sp["smoke"])
    assert set(sp["probe2"]) == set(PROBE2_IDS)
    assert not set(sp["smoke"]) & set(sp["probe"])
    # Any prefix of main is spread across repositories.
    assert len({_repo(i) for i in sp["main"][:5]}) == 5
    assert partition_tasks(tasks, 5) == sp and partition_tasks(tasks, 6)["main"] != sp["main"]
    part = make_partition(tasks, 5)
    part.save(tmp_path / "p.json")
    again = Partition.load(tmp_path / "p.json")
    assert again.sha256 == part.sha256 and again.dataset_sha256 == dataset_sha256(tasks)
    assert again.test_splits[PROBE_IDS[0]]["method"] == "f2p-half"


def test_partition_refuses_missing_probe_tasks(data_dir):
    tasks = [t for t in load_tasks(data_dir / "tasks.jsonl") if t.instance_id != PROBE_IDS[0]]
    with pytest.raises(ValueError, match="probe tasks missing"):
        partition_tasks(tasks)


# ------------------------------------------------------------------ outline: the orchestrator's view


def test_python_outline_has_signatures_not_bodies():
    text = "\n".join(e.text for e in outline_python(PY_SRC))
    assert "class Grouper(object)" in text and "def group(self, host: dict, default: Any=None) -> list" in text
    assert "def __init__(self, keys, *, strict=False)" in text and "Return the group names" in text
    assert "imports: os, typing" in text and "LIMIT" in text
    assert "secret_marker" not in text and "names.append" not in text


def test_js_outline_has_declarations_not_bodies():
    entries = outline_js(JS_SRC)
    text = "\n".join(e.text for e in entries)
    for want in ("Posts.getPost = async function (pid)", "function helper(a, b)", "class Cache extends Map",
                 "constructor(opts)", "peek(key)", "const Posts = module.exports;", "module.exports.helper = helper;"):
        assert want in text, want
    assert "secret_marker" not in text and "fake()" not in text and "db.getObject" not in text


def test_orchestrator_prompt_has_no_file_bodies_and_stays_bounded(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises)
    for ex in exercises.values():
        lead = h.lead_context(ex, ex.test_split) + h.brief_request
        assert "secret_marker" not in lead  # every body line of both fixture files carries a marker
        for src in ex.context.files.values():
            body = [ln.strip() for ln in src.splitlines() if ln.strip().startswith(("return", "names", "for ", "const post"))]
            assert body and not any(b in lead for b in body)
        assert estimate_tokens(PRO_LEAD_SYSTEM + lead) <= LEAD_MAX_TOKENS_EST
        # The workers, by contrast, see the full files.
        assert all(src in h.worker_prompt(ex, ex.test_split) for src in ex.context.files.values())


def test_orchestrator_bound_holds_with_huge_files(tmp_path, exercises):
    ex = exercises[PROBE_IDS[0]]
    huge = "".join(f"class C{i}:\n    def m{i}(self, a, b):\n        return a - b  # secret_marker\n" for i in range(6000))
    big = ProExercise(ex.task, TaskFiles({"lib/inventory.py": huge, "lib/other.py": huge}, (), "raw"), ex.test_split)
    h, _ = harness(tmp_path, exercises)
    lead = h.lead_system + h.lead_context(big, big.test_split) + h.brief_request
    assert estimate_tokens(huge) > 100_000 and estimate_tokens(lead) <= LEAD_MAX_TOKENS_EST
    assert "secret_marker" not in lead and "return a - b" not in lead and "class C0" in lead


def test_outline_size_cap_holds_for_huge_files():
    big = "\n".join(f"def f{i}(a, b, c):\n    return a + b + c + {i}\n" for i in range(5000))
    files = {"big.py": big, "big2.py": big.replace("def f", "def g")}
    out = render_outline(files, max_chars=24_000)
    assert len(out) <= 24_000 + 100 and "return" not in out


def test_t2_reviewer_sees_outline_and_diff_only(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises, config=pro_arm_config(t2_review_model=SONNET))
    ex = exercises[PROBE_IDS[0]]
    from contraprova.harness.swepro_arms import ProSubmission

    files, _ = gold_files(ex)
    review = h.t2_reviewer("D", "D:x", ex)
    res = review(ProSubmission("D:x", files), None)
    assert res.verdict == "pass"
    call = h.client.calls[-1]
    assert call["role"] == "reviewer" and call["model"] == SONNET
    assert "@@" in call["prompt"] and "secret_marker_alpha" not in call["prompt"]  # diff lines, not bodies


# ------------------------------------------------------------------ arms, gates, scorer


def test_all_arms_dry_run_solve_with_gold_and_log_one_gate_decision_per_submission(tmp_path, exercises):
    h, v = harness(tmp_path, exercises)
    ids = list(PROBE_IDS[:3])
    s = run_tasks(h, ids, ["A", "B", "C", "D", "B2"], lambda i: exercises[i], split="probe")
    assert s.stopped is None and all(s.solved[a] == 3 for a in s.arms)
    evs = h.log.events()
    subs = [e for e in evs if e.type == "task.submitted"]
    gates = [e for e in evs if e.type == "gate.decided"]
    assert len(subs) == len(gates)  # one gate.decided per submission
    d = [e for e in gates if e.payload["task_id"].startswith("D:")]
    assert all(e.payload["verdict"] == "pass" for e in d)
    assert d[0].payload["gate"].startswith("T0.scope+T0.edits+T0.visible_tests")
    assert v.runs == 3  # gates and scorer share one verifier run per distinct submission
    sub = subs[0].payload
    assert sub["parse_errors"] == [] and "<<<<<<< SEARCH" in sub["response"] and sub["diff"].startswith("diff --git")
    created = [e.payload for e in evs if e.type == "task.created"]
    assert all(c["benchmark"] == "swepro" and c["image"].startswith("img:") for c in created)
    tasks = project_tasks(evs)
    assert {f"{a}:{i}" for a in s.arms for i in ids} <= set(tasks)
    assert len([o for o in outcomes(evs)]) == 15


def test_resume_skips_finished_tasks_without_loading_them(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises)
    run_tasks(h, PROBE_IDS[:2], ["B"], lambda i: exercises[i])
    loaded = []

    def provider(i):
        loaded.append(i)
        return exercises[i]

    h2, _ = harness(tmp_path, exercises)
    s = run_tasks(h2, PROBE_IDS[:3], ["B", "A"], provider)
    assert loaded == list(PROBE_IDS[:3])  # A still to do on all three
    s = run_tasks(h2, PROBE_IDS[:3], ["B", "A"], provider)
    assert s.skipped == {"B": 3, "A": 3} and loaded == list(PROBE_IDS[:3])  # nothing reloaded


def test_unsolved_worker_gives_edit_gate_block_and_forks(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises, solve=lambda i: False, router=FixedRouter(HAIKU))
    s = run_tasks(h, PROBE_IDS[:1], ["D"], lambda i: exercises[i])
    assert s.solved["D"] == 0
    evs = h.log.events()
    first = next(e for e in evs if e.type == "gate.decided")
    assert first.payload["verdict"] == "block" and "no file was changed" in first.payload["reason"]
    assert sum(e.type == "fork.created" for e in evs) == 3


def test_visible_gate_names_visible_failures_private_gate_counts_only(tmp_path, exercises):
    ex = exercises[PROBE_IDS[0]]
    ts = ex.test_split

    def run_fn(image, tests, files):
        st = {t: "PASSED" for t in ts.score}
        st[ts.gate_private[0]] = "FAILED"
        return OracleResult(0, st)

    h, v = harness(tmp_path, exercises, run_fn=run_fn, router=FixedRouter(HAIKU))
    h.run_task("D", ex, ts, "probe")
    g = next(e for e in h.log.events() if e.type == "gate.decided").payload
    assert g["verdict"] == "block" and g["gate"].endswith("T1.private_tests")
    assert g["reason"] == "[T1.private_tests] 1 of 2 gate-private tests failed"
    assert ts.gate_private[0] not in g["reason"]
    assert g["results"][-1]["evidence"]["tests_sha256"]

    def run_vis(image, tests, files):
        return OracleResult(0, {t: "FAILED" for t in ts.score})

    h2, _ = harness(tmp_path / "b", exercises, run_fn=run_vis, router=FixedRouter(HAIKU))
    (tmp_path / "b").mkdir(exist_ok=True)
    h2.run_task("D", ex, ts, "probe")
    g2 = next(e for e in h2.log.events() if e.type == "gate.decided").payload
    assert g2["gate"].endswith("T0.visible_tests") and all(t in g2["reason"] for t in ts.visible)


def test_t2_threshold_is_configurable(tmp_path, exercises):
    ex = exercises[PROBE_IDS[0]]
    h, v = harness(tmp_path, exercises)
    files, _ = gold_files(ex)
    from contraprova.harness.arms import TaskContext
    from contraprova.harness.swepro_arms import ProSubmission

    ctx = TaskContext("D", "D:x", ex, ex.test_split, HAIKU, 1, h.contract("D:x", ex))
    assert [r.gate for r in ProGates(v, 500)(ProSubmission("D:x", files), ctx)][-1] == "T1.private_tests"
    low = ProGates(v, 1)(ProSubmission("D:x", files), ctx)
    assert low[-1].gate == "T2.review" and low[-1].evidence["status"] == "skipped"


def test_scope_rejects_test_and_out_of_lane_edits(tmp_path, exercises):
    ex = exercises[PROBE_IDS[0]]
    h, _ = harness(tmp_path, exercises)
    reply = ("<<<<<<< SEARCH tests/test_x.py\n\n=======\nassert True\n>>>>>>> REPLACE\n"
             "<<<<<<< SEARCH setup.py\n\n=======\nx\n>>>>>>> REPLACE\n" + gold_reply(ex))
    parsed = h.parse_reply(reply, ex)
    assert parsed.files == gold_files(ex)[0]
    assert sum("rejected" in e for e in parsed.errors) == 2


def test_worker_max_tokens_by_model(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises)
    assert (h.worker_max_tokens(HAIKU), h.worker_max_tokens(OPUS), h.worker_max_tokens(SONNET)) == (32000, 32000, 16000)
    run_tasks(h, PROBE_IDS[:1], ["A", "B"], lambda i: exercises[i])
    mt = {c["model"]: c["max_tokens"] for c in h.client.calls}
    assert mt == {OPUS: 32000, HAIKU: 32000}


def test_budget_stop_is_clean(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises, budget=0.05)
    s = run_tasks(h, PROBE_IDS[:4], ["A"], lambda i: exercises[i])
    assert s.stopped and "cap" in s.stopped


def test_preflight_prices_exact_counts(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises)
    exs = [exercises[i] for i in PROBE_IDS[:2]]
    seen = []

    def count(model, system, messages):
        seen.append((model, len(messages[0].content)))
        return 10_000

    rows = preflight(h, exs, [(HAIKU, 2), (OPUS, 1)], count)
    assert len(rows) == 4 and len(seen) == 4
    r_h = next(r for r in rows if r.model == HAIKU)
    r_o = next(r for r in rows if r.model == OPUS)
    assert r_h.max_tokens == 32000 and r_o.max_tokens == 32000
    assert r_o.expected_usd == pytest.approx((10_000 * 4 + r_o.expected_output_tokens * 20) / 1e6)
    assert r_h.worst_usd == pytest.approx(2 * (10_000 * 0.1 + 32000 * 0.5) / 1e6)


def test_cli_dry_run_probe_and_refusal(tmp_path, data_dir, capsys):
    from contraprova.harness.run import main

    log = tmp_path / "runs" / "probe.jsonl"
    args = ["--benchmark", "swepro", "--probe", "--data", str(data_dir), "--log", str(log), "--dry-run",
            "--context-source", "raw"]
    assert main(args + ["--budget-usd", "0.0001"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["refused"] and out["preflight"]["expected_usd"] > 0.0001
    assert not log.exists() or not log.read_text()
    assert main(args + ["--budget-usd", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["arms"] == ["B", "B2", "A"] and out["solved"] == {"B": 12, "B2": 12, "A": 12}
    order = out["preflight"]["order"]
    assert sorted(order) == sorted(PROBE_IDS)
    # resume: everything is skipped
    assert main(args + ["--budget-usd", "5"]) == 0
    assert json.loads(capsys.readouterr().out)["skipped"] == {"B": 12, "B2": 12, "A": 12}


def test_cli_preflight_action_makes_no_paid_request(tmp_path, data_dir, capsys):
    from contraprova.harness.swepro_run import main

    assert main(["--action", "preflight", "--data", str(data_dir), "--log", str(tmp_path / "l.jsonl"),
                 "--budget-usd", "1", "--dry-run", "--context-source", "raw"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["preflight"]["rows"]) == 24 and not (tmp_path / "l.jsonl").exists()


# ------------------------------------------------------------------ image cache (fake docker)


class FakeDocker:
    def __init__(self, sizes):
        self.sizes = sizes
        self.present: set[str] = set()
        self.cmds = []

    def __call__(self, cmd, timeout=None, input=None, check=False):
        import subprocess

        self.cmds.append(cmd)
        out, rc = b"", 0
        if cmd[:3] == ["docker", "image", "inspect"]:
            img = cmd[-1]
            rc = 0 if img in self.present else 1
            out = str(self.sizes.get(img, 0)).encode() if "--format" in cmd else b"[]"
        elif cmd[:2] == ["docker", "pull"]:
            self.present.add(cmd[-1])
        elif cmd[:4] == ["docker", "image", "rm", "-f"]:
            self.present.discard(cmd[-1])
        return subprocess.CompletedProcess(cmd, rc, out, b"")


def test_image_cache_lru_eviction_under_cap(tmp_path):
    gb = 10**9
    fd = FakeDocker({"a": 6 * gb, "b": 5 * gb, "c": 6 * gb, "d": 4 * gb})
    t = iter(range(100))
    cache = ImageCache(13 * gb, state_path=tmp_path / "s.json", reserve_bytes=6 * gb, runner=fd, clock=lambda: next(t))
    assert cache.ensure("a").pulled
    assert cache.ensure("b").pulled  # 6 + 6 reserve fits 13 before the pull... a stays
    assert fd.present == {"a", "b"}
    cache.ensure("a")  # touch a: b is now least recently used
    assert not cache.ensure("a").pulled
    cache.ensure("c")
    assert fd.present == {"a", "c"} and cache.held_bytes() <= 13 * gb
    fd.present.add("foreign")  # an image the cache did not pull is never deleted
    cache.ensure("d")
    assert "foreign" in fd.present and cache.held_bytes() <= 13 * gb
    assert cache.remove("d") and "d" not in fd.present and not cache.remove("foreign")
    assert cache.clear() == ["c"]
    assert fd.present == {"foreign"}
    assert sum(1 for c in fd.cmds if c[:2] == ["docker", "pull"]) == 4


@pytest.mark.skipif(not docker_available(), reason="Docker daemon not available")
def test_docker_daemon_answers():
    from contraprova.harness.docker import system_df

    assert "Images" in system_df()


# ------------------------------------------------------------------ second probe


def test_probe2_is_held_out_of_smoke_and_main(data_dir):
    tasks = load_tasks(data_dir / "tasks.jsonl")
    chosen = tuple(EXTRA[:3])
    sp = partition_tasks(tasks, 5, probe2_ids=chosen)
    assert sp["probe2"] == tuple(sorted(chosen))
    held = set(PROBE_IDS) | set(chosen)
    assert not held & set(sp["smoke"]) and not held & set(sp["main"])
    assert len(sp["smoke"]) + len(sp["main"]) + 12 + 3 == len(tasks)
    with pytest.raises(ValueError, match="overlap"):
        partition_tasks(tasks, 5, probe2_ids=(PROBE_IDS[0],))
    with pytest.raises(ValueError, match="probe2 tasks missing"):
        partition_tasks(tasks, 5, probe2_ids=("instance_nope",))
    part = make_partition(tasks, 5, probe2_ids=chosen)
    assert part.splits["probe2"] == tuple(sorted(chosen)) and part.sizes["probe2"] == 3


def test_probe2_candidates_stratified_seeded_and_excluding(rows):
    from dataclasses import replace
    tasks = [task_from_row(r) for r in rows]
    hard = {t.instance_id for t in tasks[12:12 + 14]} | {PROBE_IDS[0]}
    tasks = [replace(t, hard=t.instance_id in hard) for t in tasks]
    c = probe2_candidates(tasks, 7, n=6)
    assert len(c["selected"]) == 6 and PROBE_IDS[0] not in c["selected"] + c["queue"]
    assert set(c["selected"]) <= hard and not set(c["selected"]) & set(c["queue"])
    assert sum(c["quota"].values()) == 6 and len([r for r, k in c["quota"].items() if k]) >= 3
    assert probe2_candidates(tasks, 7, n=6) == c and probe2_candidates(tasks, 8, n=6)["selected"] != c["selected"]
    # The rest of the hard pool comes first in the queue, then the fallback (non-hard tasks).
    nq = len(hard) - 1 - 6
    assert set(c["queue"][:nq]) <= hard and not set(c["queue"][nq:]) & hard
    # fits() leaves tasks out with its reason; a short hard pool is filled from the fallback.
    drop = sorted(hard - {PROBE_IDS[0]})[:2]
    c2 = probe2_candidates(tasks, 7, n=15, fits=lambda t: "too big" if t.instance_id in drop else None)
    assert set(c2["left_out"]) == set(drop) and set(c2["left_out"].values()) == {"too big"}
    # 14 hard tasks outside the probe, 2 left out: 12 hard ones, then 3 from the fallback.
    assert len(c2["selected"]) == 15 and len(set(c2["selected"]) & hard) == 12


def test_gold_size_counts_non_test_files_and_lines(rows):
    t = task_from_row(rows[0])
    files, lines = gold_size(t)
    assert files == 2 and lines >= 3


def test_log_contents_off_logs_hashes_not_file_bodies(tmp_path, exercises):
    h, _ = harness(tmp_path, exercises)
    h.log_contents = False
    run_tasks(h, [PROBE_IDS[0]], ["B"], lambda i: exercises[i])
    sub = next(e.payload for e in h.log.events() if e.type == "task.submitted")
    assert "contents" not in sub and sub["contents_sha256"] and sub["diff"]
    body = next(iter(exercises[PROBE_IDS[0]].context.files.values()))
    assert body not in json.dumps([e.payload for e in h.log.events()])


def test_probe_cli_flags_parse():
    from contraprova.harness.swepro_run import _parser
    a = _parser().parse_args(["--probe", "--probe-split", "probe2", "--haiku-max-tokens", "32000", "--no-log-contents",
                              "--opus-max-tokens", "24000", "--no-repair"])
    assert a.probe_split == "probe2" and a.haiku_max_tokens == 32000 and a.no_log_contents
    assert a.opus_max_tokens == 24000 and a.no_repair


# ------------------------------------------------------------------ the repair turn


class ScriptedClient:
    """Replies with ``script`` in order: (text, stop_reason) pairs. Records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete(self, *, model, system, messages, max_tokens, effort=None, tags=None):
        from contraprova.interfaces import Completion
        text, stop = self.script.pop(0)
        self.calls.append({"model": model, "messages": list(messages), "tags": dict(tags or {}),
                           "max_tokens": max_tokens})
        usage = {"model": model, "input_tokens": 1000 * len(messages), "output_tokens": 500,
                 "cache_read_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0}
        return Completion(text=text, model=model, usage=usage, stop_reason=stop, request_id=f"r{len(self.calls)}")


def _gold_blocks(ex):
    from contraprova.harness.edits import diff_to_blocks
    return list(diff_to_blocks(ex.task.patch, ex.context.files))


def _repair_harness(tmp_path, exercises, script, **kw):
    h, v = harness(tmp_path, exercises)
    h.client = ScriptedClient(script)
    for k, val in kw.items():
        setattr(h, k, val)
    return h


def _no_test_info(text, ex):
    names = set(ex.test_split.tests) | set(ex.test_split.score)
    return not any(n in text for n in names) and "pass" not in text.lower().replace("passed", "") \
        and "test" not in text.lower()


def _run_b(h, ex):
    run_tasks(h, [ex.id], ["B"], lambda i: ex)
    ev = h.log.events()
    sub = next(e.payload for e in ev if e.type == "task.submitted")
    out = next(e.payload for e in ev if e.type == "outcome.scored")
    usage = [e.payload for e in ev if e.type == "usage.recorded"]
    return sub, out, usage


def test_repair_after_parse_error(tmp_path, exercises):
    from contraprova.harness.edits import format_blocks
    ex = exercises[PROBE_IDS[0]]
    blocks = _gold_blocks(ex)
    good = format_blocks(blocks)
    broken = good.replace("=======\n", "", 1)  # first block loses its divider: a parse error
    h = _repair_harness(tmp_path, exercises, [(broken, "end_turn"), (format_blocks(blocks[:1]), "end_turn")])
    sub, out, usage = _run_b(h, ex)
    assert sub["repair_used"] and sub["original_failure"] == "parse" and sub["final_failure"] is None
    assert out["solved"] and len(h.client.calls) == 2
    repair_req = h.client.calls[1]["messages"]
    assert [m.role for m in repair_req] == ["user", "assistant", "user"]
    assert repair_req[1].content == broken.rstrip() and "could not be used" in repair_req[2].content
    assert _no_test_info(repair_req[2].content, ex)


def test_repair_after_apply_error(tmp_path, exercises):
    from dataclasses import replace as dc_replace
    from contraprova.harness.edits import format_blocks
    ex = exercises[PROBE_IDS[0]]
    blocks = _gold_blocks(ex)
    i = next(k for k, b in enumerate(blocks) if b.search)
    wrong = list(blocks)
    wrong[i] = dc_replace(blocks[i], search="this line is not in the file\n")
    h = _repair_harness(tmp_path, exercises, [(format_blocks(wrong), "end_turn"),
                                              (format_blocks([blocks[i]]), "end_turn")])
    sub, out, _ = _run_b(h, ex)
    assert sub["repair_used"] and sub["original_failure"] == "apply" and sub["original_errors"]
    assert out["solved"] and sub["parse_errors"] == []
    msg = h.client.calls[1]["messages"][-1].content
    assert "SEARCH" in msg and _no_test_info(msg, ex)


def test_repair_after_truncation_applies_the_union(tmp_path, exercises):
    from contraprova.harness.edits import format_blocks
    ex = exercises[PROBE_IDS[0]]
    blocks = _gold_blocks(ex)
    assert len(blocks) >= 2
    first = format_blocks(blocks[:1]) + "\n" + format_blocks(blocks[1:])[:40]  # cut off inside block 2
    h = _repair_harness(tmp_path, exercises, [(first, "max_tokens"), (format_blocks(blocks[1:]), "end_turn")])
    sub, out, _ = _run_b(h, ex)
    assert sub["original_failure"] == "truncated" and sub["repair_used"] and out["solved"]
    from contraprova.harness.swepro_arms import REPAIR_TRUNCATED
    assert h.client.calls[1]["messages"][-1].content == REPAIR_TRUNCATED


def test_at_most_one_repair_and_cost_includes_it(tmp_path, exercises):
    from contraprova.harness.edits import format_blocks
    from contraprova.pricing import PRICE_TABLES, DEFAULT_TABLE, cost_by
    from contraprova.projections import arm_usage_records
    ex = exercises[PROBE_IDS[0]]
    broken = format_blocks(_gold_blocks(ex)).replace("=======\n", "", 1)
    h = _repair_harness(tmp_path, exercises, [(broken, "end_turn"), (broken, "max_tokens"), ("unused", "end_turn")])
    sub, out, usage = _run_b(h, ex)
    assert len(h.client.calls) == 2 and not out["solved"]
    assert sub["final_failure"] == "truncated" and any("cut off" in e for e in sub["parse_errors"])
    assert [u.get("repair", False) for u in usage] == [False, True]
    assert all(u["arm"] == "B" and u["task_id"] == f"B:{ex.id}" for u in usage)
    by_arm = cost_by(arm_usage_records(h.log.events()), PRICE_TABLES[DEFAULT_TABLE], "arm")
    assert by_arm["B"] == pytest.approx(sum(cost_by([u], PRICE_TABLES[DEFAULT_TABLE], "arm")["B"] for u in usage))


def test_no_repair_when_the_reply_is_clean_or_repair_is_off(tmp_path, exercises):
    from contraprova.harness.edits import format_blocks
    ex = exercises[PROBE_IDS[0]]
    good = format_blocks(_gold_blocks(ex))
    h = _repair_harness(tmp_path / "a", exercises, [(good, "end_turn")])
    sub, out, usage = _run_b(h, ex)
    assert out["solved"] and not sub["repair_used"] and sub["original_failure"] is None and len(usage) == 1
    h2 = _repair_harness(tmp_path / "b", exercises, [(good.replace("=======\n", "", 1), "end_turn")], repair=False)
    sub2, out2, usage2 = _run_b(h2, ex)
    assert not sub2["repair_used"] and sub2["original_failure"] == "parse" and len(usage2) == 1 and not out2["solved"]


def test_repair_message_never_carries_test_information():
    from contraprova.harness.swepro_arms import repair_message
    errs = [f"block {i} (lib/x.py): SEARCH text not found in the file" for i in range(30)]
    m = repair_message("apply", errs)
    assert "block 0" in m and "and 10 more" in m and "test" not in m.lower()


# ------------------------------------------------------------------ main study: the fixed cascade


def _cascade_config():
    return pro_arm_config(fork_branches=(("control-1", "control", HAIKU), ("treatment", "treatment", OPUS)),
                          worker_effort_by_model=((OPUS, "low"),), derive_b=True)


class OpusSolvesClient(ProFakeClient):
    """Haiku workers reply with nothing; Opus workers reply with the gold patch."""

    def complete(self, *, model, system, messages, max_tokens, effort=None, tags=None):
        self.solve = (lambda i: True) if model == OPUS else (lambda i: False)
        return super().complete(model=model, system=system, messages=messages, max_tokens=max_tokens,
                                effort=effort, tags=tags)


def test_cascade_router_is_a_fixed_policy():
    from contraprova.harness.run import CascadeRouter
    r = CascadeRouter(HAIKU, OPUS)
    first = r.decide({"attempt": 0.0}, rng_seed=1)
    after = r.decide({"attempt": 0.5}, rng_seed=1)
    assert (first.action, first.propensities) == (HAIKU, {HAIKU: 1.0})
    assert (after.action, after.propensities) == (OPUS, {OPUS: 1.0})


def test_cascade_blocked_forks_haiku_control_and_ships_opus_treatment(tmp_path, exercises):
    from contraprova.harness.run import CascadeRouter
    from contraprova.pricing import PRICE_TABLES, DEFAULT_TABLE, cost_by
    from contraprova.projections import arm_usage_records
    h, _ = harness(tmp_path, exercises, router=CascadeRouter(HAIKU, OPUS), config=_cascade_config())
    h.client = OpusSolvesClient(exercises)
    ex = exercises[PROBE_IDS[0]]
    run_tasks(h, [ex.id], ["A", "D"], lambda i: ex)
    ev = h.log.events()
    forks = [e.payload for e in ev if e.type == "fork.created"]
    assert sorted((f["branch"], f["model"]) for f in forks) == [("control-1", HAIKU), ("treatment", OPUS)]
    arms = {p["arm"]: p for p in (e.payload for e in ev if e.type == "outcome.scored") if "arm" in p}
    assert arms["A"]["solved"] and arms["D"]["solved"] and arms["D"]["branch"] == "treatment"
    assert arms["B"]["derived_from"] == "D" and not arms["B"]["solved"] and arms["B"]["gate_passed"] is False
    # Efforts: Opus low (arm A and the treatment), Haiku medium; the brief is the orchestrator's.
    workers = [(c["model"], c["effort"]) for c in h.client.calls if c["role"] == "worker"]
    assert set(workers) == {(OPUS, "low"), (HAIKU, "medium")}
    # D's cost = brief + Haiku attempt + the shipped Opus branch; the Haiku control is measurement.
    recs = arm_usage_records(ev)
    roles = {(r["arm"], r["model"], r.get("role")) for r in recs}
    assert ("D:measurement", HAIKU, "worker") in roles and ("D", OPUS, "worker") in roles
    assert ("D", OPUS, "orchestrator") in roles and ("D", HAIKU, "worker") in roles
    by_arm = cost_by(recs, PRICE_TABLES[DEFAULT_TABLE], "arm")
    assert by_arm["D"] > 0 and by_arm["D:measurement"] > 0 and "B" not in by_arm


def test_cascade_gate_pass_ships_haiku_and_derives_the_same_b(tmp_path, exercises):
    from contraprova.harness.run import CascadeRouter
    h, _ = harness(tmp_path, exercises, router=CascadeRouter(HAIKU, OPUS), config=_cascade_config())
    ex = exercises[PROBE_IDS[0]]
    run_tasks(h, [ex.id], ["D"], lambda i: ex)
    ev = h.log.events()
    assert not [e for e in ev if e.type == "fork.created"]
    arms = {p["arm"]: p for p in (e.payload for e in ev if e.type == "outcome.scored") if "arm" in p}
    assert arms["D"]["solved"] and arms["B"]["solved"] and arms["D"]["branch"] == "initial"


def test_main_preflight_prices_both_arms(tmp_path, exercises):
    from contraprova.harness.swepro_arms import main_preflight
    h, _ = harness(tmp_path, exercises, config=_cascade_config())
    rows = main_preflight(h, [exercises[PROBE_IDS[0]]], lambda **k: 10_000, p_fork=0.5, repair_rate=0.0)
    r = rows[0]
    assert r.a_input == r.d_lead_input == r.d_worker_input == 10_000
    assert r.expected_a > r.expected_d_measurement > 0 and r.worst > r.expected_a + r.expected_d_shipped


def test_cli_cascade_dry_run_end_to_end(tmp_path, data_dir, capsys):
    from contraprova.harness.run import main
    log = tmp_path / "runs" / "main.jsonl"
    ids = tmp_path / "ids.txt"
    tasks = load_tasks(data_dir / "tasks.jsonl")
    main_ids = partition_tasks(tasks)["main"][:3]
    ids.write_text("\n".join(main_ids) + "\n")
    args = ["--benchmark", "swepro", "--split", "main", "--arms", "A,D", "--cascade", "--task-file", str(ids),
            "--data", str(data_dir), "--log", str(log), "--dry-run", "--dry-solve", "none", "--context-source", "raw",
            "--budget-usd", "5", "--no-log-contents"]
    assert main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["attempted"] == {"A": 3, "D": 3}
    ev = EventLog(log).events()
    assert len([e for e in ev if e.type == "fork.created"]) == 6  # every D task blocked: 2 branches each
    assert {e.payload.get("arm") for e in ev if e.type == "outcome.scored" and "arm" in e.payload} == {"A", "B", "D"}
