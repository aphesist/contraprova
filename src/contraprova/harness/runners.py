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
import subprocess
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
# A directory holding junit-platform-console-standalone-*.jar and the assertj/byte-buddy jars every
# Java exercise's build.gradle asks for. Java runs as javac + the JUnit console launcher, not Gradle:
# the Gradle wrapper would download a ~130 MB distribution per machine.
JAVA_TOOLCHAIN_ENV = "CONTRAPROVA_JAVA_TOOLCHAIN"


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


def _java_jars() -> tuple[str, list[str]]:
    d = Path(os.environ.get(JAVA_TOOLCHAIN_ENV, ""))
    jars = sorted(str(p) for p in d.glob("*.jar")) if str(d) else []
    console = next((j for j in jars if "junit-platform-console-standalone" in j), "")
    return console, [j for j in jars if j != console]


def _java(tests: Sequence[str]) -> list[str]:
    console, libs = _java_jars()
    cp = ":".join([console, *libs])
    script = (f'mkdir -p build && javac -nowarn -encoding UTF-8 -d build -cp "{cp}" $(find src -name "*.java") '
              f'&& exec java -jar "{console}" --disable-banner --disable-ansi-colors --details=summary '
              f'--class-path "build:{":".join(libs)}" --scan-class-path')
    return ["sh", "-c", script]


def _java_env_ok() -> str | None:
    console, _ = _java_jars()
    if not console:
        return f"JUnit console launcher not found (set {JAVA_TOOLCHAIN_ENV} to a dir with the jars)"
    return None


RUNNERS: dict[str, Runner] = {
    "python": Runner("python", (), _python),
    "javascript": Runner("javascript", ("node", "sh"), _javascript, _unskip_js, _js_env_ok),
    "go": Runner("go", ("go",), _go),
    "rust": Runner("rust", ("cargo",), _rust),
    "cpp": Runner("cpp", ("cmake", "g++", "make", "sh"), _cpp),
    "java": Runner("java", ("java", "javac", "sh"), _java, _unskip_java, _java_env_ok),
}


def available_languages() -> dict[str, str | None]:
    """language -> None if its runner can run here, else why not."""
    return {lang: r.missing() for lang, r in sorted(RUNNERS.items())}


def exercise_unavailable(ex: Exercise) -> str | None:
    """None if this exercise's tests can run here, else why not (language toolchain or a
    per-exercise system library such as Boost for some C++ exercises)."""
    r = RUNNERS.get(ex.language)
    if r is None:
        return f"no test runner for {ex.language}"
    why = r.missing()
    if why:
        return f"{ex.language} toolchain unavailable: {why}"
    cm = ex.path / "CMakeLists.txt"
    if ex.language == "cpp" and cm.is_file() and "find_package(Boost" in cm.read_text(errors="replace") \
            and not Path("/usr/include/boost").is_dir():
        return "needs the Boost C++ libraries, which are not installed"
    return None


def runner_for(ex: Exercise) -> Runner:
    why = exercise_unavailable(ex)
    if why:
        raise RuntimeError(why if "toolchain" in why else f"exercise unavailable: {why}")
    return RUNNERS[ex.language]


_COUNT_RES: Mapping[str, tuple[str, ...]] = {
    "python": (r"(\d+) (?:passed|failed|errors?)\b",),
    "javascript": (r"^Tests:.*?(\d+) total",),
    "rust": (r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored",),
    "cpp": (r"All tests passed \(\d+ assertions? in (\d+) test cases?\)", r"test cases:\s+(\d+)"),
    "java": (r"\[\s*(\d+) tests found\s*\]",),
}


def discovered_tests(language: str, output: str) -> int | None:
    """Number of test cases a runner's output says it ran (None if unrecognised).

    Go is counted from ``go test -v`` output: leaf ``--- PASS/FAIL`` lines (a test with subtests
    counts its subtests only)."""
    if language == "go":
        names = re.findall(r"^\s*--- (?:PASS|FAIL|SKIP): (\S+)", output, re.M)
        if not names:
            return None
        parents = {n.rsplit("/", 1)[0] for n in names if "/" in n}
        return sum(1 for n in names if n not in parents)
    if language == "python":
        tail = [ln for ln in output.splitlines() if re.search(_COUNT_RES["python"][0], ln)]
        return sum(int(x) for x in re.findall(_COUNT_RES["python"][0], tail[-1])) if tail else None
    for pat in _COUNT_RES.get(language, ()):
        hits = re.findall(pat, output, re.M)
        if hits:
            if language == "rust":
                return sum(sum(int(x) for x in h) for h in hits)
            return int(hits[-1])
    return None


def reference_files(ex: Exercise) -> dict[str, str]:
    """The exercise's reference solution mapped onto its solution paths (by file extension)."""
    out = {}
    for sol in ex.solution_files:
        ext = Path(sol).suffix
        match = [e for e in ex.example_files if Path(e).suffix == ext and (ex.path / e).is_file()]
        if match:
            out[sol] = ex.read(match[0])
    return out


def run_full_suite(ex: Exercise, files: Mapping[str, str], *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int | None, str]:
    """Run the exercise's complete, unsplit test suite against ``files`` (for checks, not scoring).

    Returns (exit code or None on timeout, full combined output). Go runs with ``-v`` so
    :func:`discovered_tests` can count subtests."""
    r = runner_for(ex)
    tests = r.prepare({rel: ex.read(rel) for rel in (*ex.test_files, *ex.extra_test_files) if (ex.path / rel).is_file()})
    cmd = ["go", "test", "-count=1", "-v", "./..."] if ex.language == "go" else r.command(list(tests))
    with tempfile.TemporaryDirectory(prefix="contraprova-check-") as d:
        ws = build_workspace(ex, files, tests, Path(d) / ex.name)
        try:
            p = subprocess.run(cmd, cwd=ws, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None, "timeout"
        return p.returncode, p.stdout.decode("utf-8", errors="replace")


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
                         private_dir=pdir, timeout=timeout, dir_name=ctx.exercise.name)


def polyglot_scorer(sub: Submission, ctx: TaskContext, *, timeout: float = DEFAULT_TIMEOUT) -> ScoreResult:
    """Run the score tests (seen by no model and no gate) against the submission."""
    r = runner_for(ctx.exercise)
    tests = r.prepare(ctx.test_files("score"))
    with tempfile.TemporaryDirectory(prefix="contraprova-score-") as d:
        ws = build_workspace(ctx.exercise, sub.files, tests, Path(d) / ctx.exercise.name)
        res = run_command(r.command(list(tests)), ws, timeout=timeout)
    return ScoreResult(res.passed, {"runner": ctx.exercise.language, "n_score_tests": len(ctx.test_split.score),
                                    "returncode": res.returncode, "timed_out": res.timed_out,
                                    "counts": dict(res.counts), "output_sha256": sha256_text(res.output)})
