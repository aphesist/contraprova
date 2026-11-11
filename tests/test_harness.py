"""Offline tests for the Aider Polyglot harness: synthetic fixtures and fake clients only."""

import ast
import json
from collections import Counter
from pathlib import Path

import pytest

from contraprova.harness.arms import (
    BudgetExceeded,
    BudgetGuard,
    Harness,
    ScoreResult,
    format_files,
    parse_files,
    run_arm,
)
from contraprova.harness.polyglot import (
    DEFAULT_SIZES,
    Partition,
    TestSplit,
    build_workspace,
    count_tests,
    list_tests,
    load_exercises,
    make_partition,
    materialize_tests,
    partition_ids,
    python_tests,
    scale_sizes,
    split_tests,
)
from contraprova.harness.run import FakeClient, FixedRouter, UniformRouter, dry_run_gates, local_python_scorer, main, no_exec_scorer
from contraprova.interfaces import HAIKU, OPUS, SONNET, GateResult
from contraprova.log import EventLog
from contraprova.projections import outcomes, project_tasks, usage_records

FIXTURES = Path(__file__).parent / "fixtures" / "polyglot"
# Per-language exercise counts of the real benchmark (Aider-AI/polyglot-benchmark).
REAL_COUNTS = {"cpp": 26, "go": 39, "java": 47, "javascript": 49, "python": 34, "rust": 30}


@pytest.fixture(scope="module")
def exercises():
    return load_exercises(FIXTURES)


def by_id(exercises, i):
    return next(e for e in exercises if e.id == i)


def splits_for(exercises, seed=7):
    return {e.id: split_tests(e, seed) for e in exercises}


# ------------------------------------------------------------------ dataset


def test_loader_reads_config(exercises):
    assert [e.id for e in exercises] == [
        "javascript/shout", "python/clamp", "python/leap-lite", "python/word-tally"]
    wt = by_id(exercises, "python/word-tally")
    assert wt.solution_files == ("word_tally.py",) and wt.test_files == ("word_tally_test.py",)
    assert "lower-cased" in wt.instructions()
    assert set(wt.stubs()) == {"word_tally.py"}


def test_count_and_list_tests(exercises):
    assert {e.id: count_tests(e) for e in exercises} == {
        "javascript/shout": 6, "python/clamp": 6, "python/leap-lite": 3, "python/word-tally": 7}
    assert list_tests(by_id(exercises, "javascript/shout"))[:2] == ["a word", "two words"]
    assert "WordTallyTest.test_two_words" in list_tests(by_id(exercises, "python/word-tally"))
    assert "WordTallyTest.assertTally" not in list_tests(by_id(exercises, "python/word-tally"))


# ------------------------------------------------------------------ partition


def fake_ids(counts):
    return {lang: [f"{lang}/ex{i:02d}" for i in range(n)] for lang, n in counts.items()}


def test_partition_real_shape_is_stratified_and_exact():
    ids = fake_ids(REAL_COUNTS)
    p = partition_ids(ids, seed=1)
    assert {k: len(v) for k, v in p.items()} == dict(DEFAULT_SIZES)
    flat = [x for v in p.values() for x in v]
    assert len(flat) == len(set(flat)) == 225
    # Every split's per-language count is within one of its proportional share.
    for split, members in p.items():
        per = Counter(x.split("/")[0] for x in members)
        for lang, n in REAL_COUNTS.items():
            assert abs(per[lang] - n * len(members) / 225) < 1 + 1e-9, (split, lang)


def test_partition_is_deterministic_and_seed_sensitive():
    ids = fake_ids(REAL_COUNTS)
    assert partition_ids(ids, seed=3) == partition_ids({k: list(reversed(v)) for k, v in ids.items()}, seed=3)
    assert partition_ids(ids, seed=3) != partition_ids(ids, seed=4)


@pytest.mark.parametrize("total", [0, 1, 4, 7, 100, 226, 300])
def test_partition_other_totals(total):
    sizes = scale_sizes(total)
    assert sum(sizes.values()) == total
    counts = {"python": total // 2, "go": total - total // 2}
    p = partition_ids(fake_ids(counts), seed=0)
    assert sum(len(v) for v in p.values()) == total
    assert {k: len(v) for k, v in p.items()} == sizes


def test_partition_json_roundtrip_and_hash(exercises, tmp_path):
    a = make_partition(exercises, seed=11)
    b = make_partition(list(reversed(exercises)), seed=11)
    assert a.to_json() == b.to_json() and a.sha256 == b.sha256
    digest = a.save(tmp_path / "p.json")
    stored = json.loads((tmp_path / "p.json").read_text())
    assert stored["sha256"] == digest == a.sha256
    loaded = Partition.load(tmp_path / "p.json")
    assert loaded.sha256 == a.sha256 and loaded.splits == a.splits
    # Tampering is detected.
    moved = stored["splits"]["train"].pop()
    stored["splits"]["smoke"].append(moved)
    (tmp_path / "p.json").write_text(json.dumps(stored))
    with pytest.raises(ValueError):
        Partition.load(tmp_path / "p.json")
    assert make_partition(exercises, seed=12).dataset_sha256 == a.dataset_sha256


# ------------------------------------------------------------------ test split


def test_python_test_split_is_deterministic_and_disjoint(exercises):
    wt = by_id(exercises, "python/word-tally")
    s1, s2 = split_tests(wt, seed=5), split_tests(wt, seed=5)
    assert s1 == s2 and s1.method == "function"
    parts = [set(s1.visible), set(s1.gate_private), set(s1.score)]
    assert set().union(*parts) == set(s1.tests) and sum(map(len, parts)) == len(s1.tests) == 7
    assert all(len(p) >= 2 for p in parts)
    assert TestSplit.from_dict(wt.id, s1.to_dict()) == s1
    seeds = {split_tests(wt, seed=k).score for k in range(20)}
    assert len(seeds) > 1  # the seed matters


def test_fallbacks(exercises):
    leap = split_tests(by_id(exercises, "python/leap-lite"))
    shout = split_tests(by_id(exercises, "javascript/shout"))
    for s in (leap, shout):
        assert s.method == "fallback" and s.visible == s.score == s.tests and s.gate_private == ()
    assert materialize_tests(by_id(exercises, "javascript/shout"), shout, "gate_private") == {}


def test_materialized_python_subsets_parse_and_partition(exercises):
    for name in ("python/word-tally", "python/clamp"):
        ex = by_id(exercises, name)
        s = split_tests(ex, seed=9)
        seen = []
        for subset in ("visible", "gate_private", "score"):
            (src,) = materialize_tests(ex, s, subset).values()
            ast.parse(src)
            got = [t.id for t in python_tests(src)]
            assert got == list(s.subset(subset))
            seen += got
            if name == "python/word-tally":
                assert "def assertTally" in src and "def setUp" in src
        assert sorted(seen) == sorted(s.tests)


def test_score_tests_run_against_reference(exercises, tmp_path):
    ex = by_id(exercises, "python/word-tally")
    s = split_tests(ex)
    example = {"word_tally.py": (ex.path / ".meta" / "example.py").read_text()}
    ws = build_workspace(ex, example, materialize_tests(ex, s, "score"), tmp_path / "ws")
    assert not (ws / ".meta").exists()
    from contraprova.harness.arms import TaskContext

    ctx = TaskContext("A", "A:" + ex.id, ex, s, OPUS)
    from contraprova.interfaces import Submission

    assert local_python_scorer(Submission("t", example), ctx).solved
    assert not local_python_scorer(Submission("t", ex.stubs()), ctx).solved


# ------------------------------------------------------------------ file format


def test_parser_roundtrip():
    files = {"a.py": "x = 1\n", "pkg/b.py": "def f():\n    return '```'\n"}
    assert parse_files(format_files(files)).files == files


def test_parser_tolerates_noise():
    text = (
        "Sure! Here you go.\r\n"
        "<<< FILE: ./src/x.py >>>\r\n```python\r\nprint(1)\r\n```\r\n<<<END>>>\r\n"
        "and also\n<<<FILE 'y.txt'>>>\n\n<<<END FILE>>>\n"
        "<<<FILE src/x.py>>>\nprint(2)\n<<<END>>>\n"
    )
    r = parse_files(text)
    assert r.files == {"src/x.py": "print(2)\n", "y.txt": ""}
    assert r.errors == ()


def test_parser_rejects_unsafe_and_truncated():
    text = "<<<FILE ../etc/passwd>>>\nx\n<<<END>>>\n<<<FILE /abs.py>>>\nx\n<<<END>>>\n" \
           "<<<FILE ok.py>>>\nfine\n<<<END>>>\n<<<FILE cut.py>>>\npartial"
    r = parse_files(text)
    assert r.files == {"ok.py": "fine\n"}
    assert len(r.errors) == 3 and any("unterminated" in e for e in r.errors)


def test_parser_open_without_close_before_next_block():
    r = parse_files("<<<FILE a.py>>>\nx\n<<<FILE b.py>>>\ny\n<<<END>>>")
    assert r.files == {"b.py": "y\n"} and len(r.errors) == 1


def test_parser_no_blocks():
    assert parse_files("I could not solve it.").files == {}


# ------------------------------------------------------------------ arms


def harness(tmp_path, client=None, router=None, gates=dry_run_gates, scorer=no_exec_scorer, cap=None):
    return Harness(EventLog(tmp_path / "log.jsonl"), client or FakeClient(), scorer, gates,
                   router or UniformRouter(), BudgetGuard(cap))


def oracle_scorer(sub, ctx):
    ok = bool(sub.files) and sub.files != ctx.exercise.stubs()
    return ScoreResult(ok, {"oracle": True})


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
def test_each_arm_emits_a_valid_log(arm, exercises, tmp_path):
    h = harness(tmp_path)
    summary = run_arm(h, arm, exercises, splits_for(exercises), "pilot")
    assert summary.attempted == len(exercises) and summary.stopped is None
    assert h.log.verify() > 0
    evs = h.log.events()
    types = Counter(e.type for e in evs)
    for t in ("task.created", "task.claimed", "task.submitted", "gate.decided", "usage.recorded", "outcome.scored"):
        assert types[t] > 0, t
    for u in usage_records(evs):
        assert u["arm"] == arm and u["role"] and u["task_id"].startswith(arm + ":") and u["model"]
    outs = outcomes(evs)
    assert {(arm, e.id) for e in exercises} <= set(outs)
    tasks = project_tasks(evs)
    assert all(t.status in ("done", "failed") for t in tasks.values())
    # Re-running skips finished tasks instead of failing.
    again = run_arm(h, arm, exercises, splits_for(exercises), "pilot")
    assert again.skipped == len(exercises) and again.attempted == 0


def test_arm_models_and_effort(exercises, tmp_path):
    ex = [by_id(exercises, "python/clamp")]
    for arm, expect in (("A", {(OPUS, "low", "worker")}), ("B", {(HAIKU, None, "worker")})):
        c = FakeClient()
        run_arm(harness(tmp_path / arm, client=c), arm, ex, splits_for(ex))
        assert {(x["model"], x["effort"], x["role"]) for x in c.calls} == expect
    c = FakeClient(review="REDISPATCH\nhandle low > high")
    h = harness(tmp_path / "C", client=c)
    run_arm(h, "C", ex, splits_for(ex))
    assert [x["role"] for x in c.calls] == ["orchestrator", "worker", "reviewer", "worker"]
    gates = [e.payload for e in h.log.events() if e.type == "gate.decided"]
    assert [g["verdict"] for g in gates] == ["block", "pass"]
    assert project_tasks(h.log.events())["C:python/clamp"].attempts == 2


def test_arm_d_forks_on_first_gate_failure(exercises, tmp_path):
    ex = [by_id(exercises, "python/word-tally")]

    def block_initial(sub, ctx):
        return [GateResult("T1.private_tests", "block" if ctx.task_id.count(":") == 1 else "pass", "2 failed")]

    c = FakeClient()
    h = harness(tmp_path, client=c, router=FixedRouter(SONNET), gates=block_initial, scorer=oracle_scorer)
    run_arm(h, "D", ex, splits_for(ex), "eval")
    assert h.log.verify()
    evs = h.log.events()
    forks = [e.payload for e in evs if e.type == "fork.created"]
    assert [(f["branch"], f["model"]) for f in forks] == [("control", HAIKU), ("control", HAIKU), ("treatment", SONNET)]
    assert len({f["fork_point_seq"] for f in forks}) == 1
    routes = [e.payload for e in evs if e.type == "route.decided"]
    assert [r["decision"] for r in routes] == ["initial", "after_gate_failure"]
    assert all(abs(sum(r["propensities"].values()) - 1) < 1e-9 and r["router_hash"] for r in routes)
    outs = outcomes(evs)
    assert set(a for a, _ in outs) == {"D", "D/control-1", "D/control-2", "D/treatment"}
    d = [e.payload for e in evs if e.type == "outcome.scored" and e.payload["arm"] == "D"][0]
    assert d["branch"] == "treatment" and d["router_action"] == SONNET
    tasks = project_tasks(evs)
    assert tasks["D:python/word-tally"].status == "failed"
    assert tasks["D:python/word-tally:treatment"].status == "done"
    workers = [x for x in c.calls if x["role"] == "worker"]
    assert [x["model"] for x in workers] == [SONNET, HAIKU, HAIKU, SONNET]
    # The three branches are re-executed from the same point: identical prompts carrying the gate's reason.
    assert "2 failed" in workers[1]["prompt"] and "2 failed" not in workers[0]["prompt"]
    assert workers[1]["prompt"] == workers[2]["prompt"] == workers[3]["prompt"]


def test_arm_d_gate_block_means_not_shipped(exercises, tmp_path):
    ex = [by_id(exercises, "python/clamp")]
    h = harness(tmp_path, router=FixedRouter(HAIKU),
                gates=lambda s, c: [GateResult("T0.visible_tests", "block", "fail")], scorer=oracle_scorer)
    run_arm(h, "D", ex, splits_for(ex))
    d = [e.payload for e in h.log.events() if e.type == "outcome.scored" and e.payload["arm"] == "D"][0]
    assert d["branch"] == "control-1" and d["shipped"] is False and d["solved"] is False


def test_contract_scope_gate_blocks_test_edits(exercises, tmp_path):
    ex = [by_id(exercises, "python/clamp")]
    c = FakeClient(edits={"clamp_test.py": "# tampered\n"})
    h = harness(tmp_path, client=c, router=FixedRouter(HAIKU))
    run_arm(h, "D", ex, splits_for(ex))
    first = next(e.payload for e in h.log.events() if e.type == "gate.decided")
    scope = next(r for r in first["results"] if r["gate"] == "T0.scope")
    assert scope["verdict"] == "block" and "protected" in scope["reason"]


# ------------------------------------------------------------------ budget


def test_kill_switch_stops_before_cap(exercises, tmp_path):
    # Each fake Opus call reports 100 input + 2000 output tokens: $0.0004 + $0.04.
    c = FakeClient(tokens_per_call=(100, 2_000))
    h = harness(tmp_path, client=c, cap=0.10)
    h.config = type(h.config)(worker_max_tokens=2_000, orchestrator_max_tokens=2_000)
    summary = run_arm(h, "A", exercises, splits_for(exercises))
    assert summary.stopped and "cap" in summary.stopped
    assert summary.attempted < len(exercises)
    assert len(c.calls) == 2 and summary.spent_usd <= 0.10
    assert h.log.verify()


def test_kill_switch_reads_prior_spend_from_log(exercises, tmp_path):
    h = harness(tmp_path, cap=1.0)
    h.log.append("task.created", "x", {"task_id": "old", "title": "old"})
    h.log.append("usage.recorded", "x", {"model": OPUS, "input_tokens": 0, "output_tokens": 50_000, "task_id": "old"})
    assert h.budget.spent(h.log) == pytest.approx(1.0)
    with pytest.raises(BudgetExceeded):
        h.budget.check(h.log, HAIKU, 10, 10)
    summary = run_arm(h, "B", exercises, splits_for(exercises))
    assert summary.attempted == 1 and summary.stopped
    assert len(usage_records(h.log.events())) == 1


# ------------------------------------------------------------------ CLI


def test_cli_dry_run(tmp_path, capsys):
    log = tmp_path / "runs" / "x.jsonl"
    rc = main(["--arm", "D", "--split", "train", "--polyglot", str(FIXTURES), "--log", str(log),
               "--budget-usd", "5", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["dry_run"] and out["stopped"] is None
    part = Partition.load(tmp_path / "runs" / "partition.json")
    assert out["partition_sha256"] == part.sha256
    assert out["attempted"] == len(part.splits["train"])
    assert EventLog(log).verify() > 0
    # Second invocation reuses the frozen partition and resumes.
    assert main(["--arm", "D", "--split", "train", "--polyglot", str(FIXTURES), "--log", str(log),
                 "--budget-usd", "5", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["partition_sha256"] == part.sha256


def test_cli_real_run_requires_parts(tmp_path):
    with pytest.raises(SystemExit):
        main(["--arm", "D", "--split", "pilot", "--polyglot", str(FIXTURES), "--log", str(tmp_path / "x.jsonl"),
              "--budget-usd", "1"])
