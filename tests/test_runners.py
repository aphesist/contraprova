"""Real gates and scorer on the fixture exercises (executes the fixtures' own reference solutions)."""

from pathlib import Path

import pytest

from contraprova.harness import load_exercises, split_tests
from contraprova.harness.arms import TaskContext
from contraprova.harness.runners import (
    RUNNERS,
    _unskip_java,
    _unskip_js,
    available_languages,
    polyglot_gates,
    polyglot_scorer,
)
from contraprova.interfaces import Submission

FIXTURES = Path(__file__).parent / "fixtures" / "polyglot"


@pytest.fixture(scope="module")
def clamp():
    return next(e for e in load_exercises(FIXTURES) if e.id == "python/clamp")


def ctx_for(ex):
    return TaskContext("D", f"D:{ex.id}", ex, split_tests(ex), "claude-haiku-5-5", 1)


def test_python_reference_passes_gates_and_score(clamp):
    ctx = ctx_for(clamp)
    assert ctx.test_split.method == "function" and ctx.test_split.gate_private
    sub = Submission(ctx.task_id, {"clamp.py": clamp.read(".meta/example.py")}, 1)
    results = polyglot_gates(sub, ctx)
    assert [r.gate for r in results] == ["T0.scope", "T0.visible_tests", "T1.private_tests"]
    assert all(r.verdict == "pass" for r in results)
    assert results[1].evidence["tests"]["counts"]["passed"] == len(ctx.test_split.visible)
    assert results[2].evidence["tests"]["counts"]["passed"] == len(ctx.test_split.gate_private)
    score = polyglot_scorer(sub, ctx)
    assert score.solved and score.evidence["counts"]["passed"] == len(ctx.test_split.score)


def test_python_stub_blocks_at_visible_tests_and_fails_score(clamp):
    ctx = ctx_for(clamp)
    sub = Submission(ctx.task_id, clamp.stubs(), 1)
    results = polyglot_gates(sub, ctx)
    assert results[-1].gate == "T0.visible_tests" and results[-1].verdict == "block"
    assert results[-1].evidence["tests"]["counts"].get("failed", 0) > 0
    assert not polyglot_scorer(sub, ctx).solved


def test_writing_a_test_file_is_blocked_by_scope(clamp):
    ctx = ctx_for(clamp)
    sub = Submission(ctx.task_id, {"clamp.py": clamp.read(".meta/example.py"), "clamp_test.py": "pass\n"}, 1)
    results = polyglot_gates(sub, ctx)
    assert results[0].gate == "T0.scope" and results[0].verdict == "block" and len(results) == 1


def test_unskip_transforms():
    assert _unskip_js("test('a', f);\nxtest('b', g);\nxit('c', h)") == "test('a', f);\ntest('b', g);\nit('c', h)"
    java = "class T {\n  @Test\n  @Disabled(\"Remove to run test\")\n  public void b() {}\n}\n"
    assert "@Disabled" not in _unskip_java(java) and "@Test" in _unskip_java(java)


def test_every_language_has_a_runner_and_python_is_available():
    assert set(RUNNERS) == {"python", "javascript", "go", "rust", "cpp", "java"}
    assert available_languages()["python"] is None


def test_unavailable_language_raises(clamp, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    ex = next(e for e in load_exercises(FIXTURES) if e.language == "javascript")
    with pytest.raises(RuntimeError, match="toolchain unavailable"):
        polyglot_scorer(Submission("x", {}, 1), ctx_for(ex))


def test_discovered_tests_parses_each_runner():
    from contraprova.harness.runners import discovered_tests
    assert discovered_tests("python", "...\n3 passed, 1 failed in 0.1s") == 4
    assert discovered_tests("javascript", "Tests:       2 failed, 8 passed, 10 total") == 10
    assert discovered_tests("rust", "test result: ok. 0 passed; 0 failed; 0 ignored\n"
                                    "test result: FAILED. 3 passed; 2 failed; 1 ignored") == 6
    assert discovered_tests("cpp", "All tests passed (13 assertions in 12 test cases)") == 12
    assert discovered_tests("cpp", "test cases: 7 | 5 passed | 2 failed") == 7
    assert discovered_tests("java", "[        31 tests found           ]") == 31
    go = "--- PASS: TestA (0s)\n    --- PASS: TestA/x (0s)\n    --- FAIL: TestA/y (0s)\n--- PASS: TestB (0s)\n"
    assert discovered_tests("go", go) == 3
    assert discovered_tests("go", "ok  counter [no tests to run]") is None


def test_boost_exercises_are_unavailable_without_boost(tmp_path, monkeypatch):
    import json
    from contraprova.harness.polyglot import load_exercise
    from contraprova.harness.runners import exercise_unavailable
    d = tmp_path / "cpp" / "exercises" / "practice" / "meetup"
    (d / ".meta").mkdir(parents=True)
    (d / ".meta" / "config.json").write_text(json.dumps({"files": {"solution": ["meetup.cpp"], "test": ["meetup_test.cpp"]}}))
    (d / "CMakeLists.txt").write_text("find_package(Boost 1.58 REQUIRED COMPONENTS date_time)\n")
    ex = load_exercise(d, "cpp")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    real_is_dir = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: False if str(self) == "/usr/include/boost" else real_is_dir(self))
    assert "Boost" in exercise_unavailable(ex)


def test_temp_workspace_takes_the_given_directory_name():
    from contraprova.sandbox import run_in_temp_workspace
    r = run_in_temp_workspace(None, {"a.txt": "x"}, ["sh", "-c", "basename \"$PWD\" | tr a-z A-Z"],
                              dir_name="perfect-numbers")
    assert r.passed and r.output.strip() == "PERFECT-NUMBERS"


def test_run_cli_excludes_named_exercises(tmp_path):
    from contraprova.harness.run import main
    log = tmp_path / "x.jsonl"
    ids = [e.id for e in load_exercises(FIXTURES)]
    args = ["--arm", "B", "--split", "train", "--polyglot", str(FIXTURES), "--log", str(log), "--budget-usd", "1",
            "--dry-run"]
    for i in ids:
        args += ["--exclude", i]
    assert main(args) == 0
    assert not log.exists() or "task.created" not in log.read_text()


@pytest.mark.skipif(not __import__("os").environ.get("CONTRAPROVA_JS_TOOLCHAIN"), reason="jest toolchain not installed")
def test_javascript_reference_passes_with_jest(tmp_path):
    import shutil
    src = FIXTURES / "javascript" / "exercises" / "practice" / "shout"
    dst = tmp_path / "javascript" / "exercises" / "practice" / "shout"
    shutil.copytree(src, dst)
    # Real Polyglot exercises ship these two; the fixture does not.
    (dst / "package.json").write_text('{"name": "shout", "private": true}\n')
    (dst / "babel.config.js").write_text("module.exports = { presets: ['@exercism/babel-preset-javascript'] };\n")
    ex = load_exercises(tmp_path)[0]
    ctx = ctx_for(ex)
    sub = Submission(ctx.task_id, {"shout.js": ex.read(".meta/proof.ci.js")}, 1)
    assert polyglot_scorer(sub, ctx).solved


def _big(clamp):
    return clamp.read(".meta/example.py") + "".join(f"# padding line {i}\n" for i in range(250))


def test_t2_without_a_reviewer_is_skipped_not_blocked(clamp):
    ctx = ctx_for(clamp)
    sub = Submission(ctx.task_id, {"clamp.py": _big(clamp)}, 1)
    results = polyglot_gates(sub, ctx)
    assert [r.gate for r in results] == ["T0.scope", "T0.visible_tests", "T1.private_tests", "T2.review"]
    t2 = results[-1]
    assert t2.verdict == "pass" and t2.evidence["status"] == "skipped" and "skipped" in t2.reason
    assert all(r.verdict == "pass" for r in results)


def test_t2_uses_the_context_reviewer(clamp):
    from dataclasses import replace

    from contraprova.interfaces import GateResult

    seen = []

    def reviewer(sub, contract):
        seen.append(sorted(sub.files))
        return GateResult("T2.review", "block", "adds a dependency", {"status": "reviewed"})

    ctx = replace(ctx_for(clamp), reviewer=reviewer)
    results = polyglot_gates(Submission(ctx.task_id, {"clamp.py": _big(clamp)}, 1), ctx)
    assert results[-1].gate == "T2.review" and results[-1].verdict == "block" and seen == [["clamp.py"]]
    # A small diff never reaches T2, so the reviewer is not called.
    small = polyglot_gates(Submission(ctx.task_id, {"clamp.py": clamp.read(".meta/example.py")}, 1), ctx)
    assert "T2.review" not in [r.gate for r in small] and len(seen) == 1
