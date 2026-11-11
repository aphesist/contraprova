"""Real gates and a real score-test scorer for Aider Polyglot, one test runner per language.

The dry run uses fakes; a real run names these instead::

    --gates contraprova.harness.runners:polyglot_gates
    --scorer contraprova.harness.runners:polyglot_scorer

Both execute model-written code and the exercise's tests through ``contraprova.sandbox``, which
is only a throwaway working directory, not isolation: run them only inside a disposable container.

A language is usable only when its toolchain is on ``PATH`` (:func:`available_languages`).
Exercism marks all but the first test as skipped in several tracks; :meth:`Runner.prepare`
un-skips them in the copy that is executed (``xtest`` -> ``test`` for JavaScript, ``@Disabled``
removed for Java; Rust and C++ un-skip through the command), so every test in a subset runs.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..contracts import DEFAULT_PROTECTED, LaneContract
from ..gates import run_gates
from ..interfaces import GateResult, Submission
from ..sandbox import run_command
from .arms import ScoreResult, TaskContext
from .polyglot import Exercise, build_workspace, sha256_text

DEFAULT_TIMEOUT = 300.0
# Where JavaScript's jest toolchain is installed once and linked into every workspace.
JS_TOOLCHAIN_ENV = "CONTRAPROVA_JS_TOOLCHAIN"


def _unskip_js(text: str) -> str:
    return re.sub(r"\bxtest\(", "test(", re.sub(r"\bxit\(", "it(", text))


def _unskip_java(text: str) -> str:
    return re.sub(r"^\s*@Disabled(\([^)]*\))?\s*$\n?", "", text, flags=re.M)


@dataclass(frozen=True)
class Runner:
    language: str
    tools: tuple[str, ...]  # executables that must be on PATH
    build_command: Callable[[Sequence[str]], list[str]]
    transform: Callable[[str], str] | None = None
    env_ok: Callable[[], str | None] = field(default=lambda: None)  # extra precondition; message if unmet

    def missing(self) -> str | None:
        absent = [t for t in self.tools if shutil.which(t) is None]
        if absent:
            return "not on PATH: " + ", ".join(absent)
        return self.env_ok()

    def prepare(self, tests: Mapping[str, str]) -> dict[str, str]:
        return {p: (self.transform(t) if self.transform else t) for p, t in tests.items()}

    def command(self, test_files: Sequence[str]) -> list[str]:
        return self.build_command(sorted(test_files))


def _python(tests: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests]


def _javascript(tests: Sequence[str]) -> list[str]:
    tc = os.environ.get(JS_TOOLCHAIN_ENV, "")
    jest = str(Path(tc) / "node_modules" / ".bin" / "jest")
    # Link the shared node_modules in (the sandbox never copies node_modules), then run jest on the files.
    script = f'ln -s "{tc}/node_modules" node_modules && exec "{jest}" --ci --no-cache --watchman=false "$@"'
    return ["sh", "-c", script, "jest", *tests]


def _js_env_ok() -> str | None:
    tc = os.environ.get(JS_TOOLCHAIN_ENV)
    if not tc or not (Path(tc) / "node_modules" / ".bin" / "jest").exists():
        return f"jest toolchain not installed (set {JS_TOOLCHAIN_ENV} to a dir with node_modules/.bin/jest)"
    return None


def _go(tests: Sequence[str]) -> list[str]:
    return ["go", "test", "-count=1", "./..."]


def _rust(tests: Sequence[str]) -> list[str]:
    return ["cargo", "test", "--offline", "--quiet", "--", "--include-ignored"]


def _cpp(tests: Sequence[str]) -> list[str]:
    script = ("cmake -S . -B build -DEXERCISM_RUN_ALL_TESTS=1 -DCMAKE_BUILD_TYPE=Debug >/dev/null "
              "&& cmake --build build -j2")
    return ["sh", "-c", script]


def _java(tests: Sequence[str]) -> list[str]:
    return ["gradle", "test", "--offline", "--console=plain", "-q"]


RUNNERS: dict[str, Runner] = {
    "python": Runner("python", (), _python),
    "javascript": Runner("javascript", ("node", "sh"), _javascript, _unskip_js, _js_env_ok),
    "go": Runner("go", ("go",), _go),
    "rust": Runner("rust", ("cargo",), _rust),
    "cpp": Runner("cpp", ("cmake", "g++", "make", "sh"), _cpp),
    "java": Runner("java", ("java", "gradle"), _java, _unskip_java),
}


def available_languages() -> dict[str, str | None]:
    """language -> None if its runner can run here, else why not."""
    return {lang: r.missing() for lang, r in sorted(RUNNERS.items())}


def runner_for(ex: Exercise) -> Runner:
    r = RUNNERS.get(ex.language)
    if r is None:
        raise NotImplementedError(f"no test runner for {ex.language}")
    why = r.missing()
    if why:
        raise RuntimeError(f"{ex.language} toolchain unavailable: {why}")
    return r


def default_contract(ctx: TaskContext) -> LaneContract:
    ex = ctx.exercise
    protected = tuple(DEFAULT_PROTECTED) + tuple(ex.test_files) + tuple(ex.extra_test_files)
    return LaneContract(lane_id=ctx.task_id, task_id=ctx.task_id, allowed=tuple(ex.solution_files), protected=protected)


def _write(files: Mapping[str, str], dest: Path) -> Path:
    for rel, text in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return dest


def polyglot_gates(sub: Submission, ctx: TaskContext, *, timeout: float = DEFAULT_TIMEOUT) -> list[GateResult]:
    """T0.scope, T0.visible_tests and (when the split has any) T1.private_tests, via ``run_gates``.

    The workspace holds the exercise stubs and the visible tests; the gate-private tests are an
    overlay that replaces the test files before the private command runs, so T1 runs only them.
    """
    r = runner_for(ctx.exercise)
    visible = r.prepare(ctx.test_files("visible"))
    private = r.prepare(ctx.test_files("gate_private"))
    with tempfile.TemporaryDirectory(prefix="contraprova-gate-") as d:
        ws = build_workspace(ctx.exercise, {}, visible, Path(d) / "ws")
        pdir = _write(private, Path(d) / "private") if private else None
        return run_gates(sub, ctx.contract or default_contract(ctx), ws,
                         visible_command=r.command(list(visible)),
                         private_command=r.command(list(private)) if private else None,
                         private_dir=pdir, timeout=timeout)


def polyglot_scorer(sub: Submission, ctx: TaskContext, *, timeout: float = DEFAULT_TIMEOUT) -> ScoreResult:
    """Run the score tests (seen by no model and no gate) against the submission."""
    r = runner_for(ctx.exercise)
    tests = r.prepare(ctx.test_files("score"))
    with tempfile.TemporaryDirectory(prefix="contraprova-score-") as d:
        ws = build_workspace(ctx.exercise, sub.files, tests, Path(d) / "ws")
        res = run_command(r.command(list(tests)), ws, timeout=timeout)
    return ScoreResult(res.passed, {"runner": ctx.exercise.language, "n_score_tests": len(ctx.test_split.score),
                                    "returncode": res.returncode, "timed_out": res.timed_out,
                                    "counts": dict(res.counts), "output_sha256": sha256_text(res.output)})
