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
