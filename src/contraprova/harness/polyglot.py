"""Aider Polyglot dataset: loading, the frozen partition, and the test split.

The benchmark (github.com/Aider-AI/polyglot-benchmark) is never vendored. The
loader takes a path to a checkout laid out as
``<root>/<language>/exercises/practice/<exercise>/`` with Exercism's
``.meta/config.json`` naming the solution, test and example files.

Nothing here executes benchmark code. Python test files are parsed with
``ast`` (which does not run them) and rewritten by deleting line ranges.

Partition
    Exercises are shuffled per language with a fixed seed and dealt into
    smoke / pilot / train / eval / reserve, proportionally by language. The
    result is written as canonical JSON together with its sha256, so a run can
    prove it used the partition that was frozen before the pilot.

Test split
    For an exercise with at least ``MIN_SPLIT_TESTS`` tests, every test is
    assigned to exactly one of ``visible`` (shown to workers),
    ``gate_private`` (seen only by the governed arm's gate) and ``score``
    (seen by no model and no gate). Assignment is by a seeded hash of the test
    id, so it is deterministic and independent of file order.

    Python is split at test-function level. For other languages, and for any
    exercise with fewer tests, the documented fallback applies: ``visible``
    and ``score`` are both the full suite and ``gate_private`` is empty. Under
    the fallback the score tests are *not* hidden from workers; analyses must
    report fallback exercises separately (``TestSplit.method == "fallback"``).
"""

from __future__ import annotations

import ast
import hashlib
import json
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..events import canonical_json

LANGUAGES: tuple[str, ...] = ("cpp", "go", "java", "javascript", "python", "rust")
SPLITS: tuple[str, ...] = ("smoke", "pilot", "train", "eval", "reserve")
DEFAULT_SIZES: Mapping[str, int] = {"smoke": 5, "pilot": 15, "train": 90, "eval": 90, "reserve": 25}
DEFAULT_SEED = 20261111
MIN_SPLIT_TESTS = 6
SUBSETS: tuple[str, ...] = ("visible", "gate_private", "score")
PARTITION_VERSION = 1

# Files a worker never sees: reference solutions, generators, approach write-ups.
_HIDDEN_DIRS = (".meta", ".approaches", ".articles", ".docs")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_json(obj) -> str:
    return sha256_text(canonical_json(obj))


# ------------------------------------------------------------------ exercises


@dataclass(frozen=True)
class Exercise:
    id: str  # "<language>/<name>"
    language: str
    name: str
    path: Path
    solution_files: tuple[str, ...]
    test_files: tuple[str, ...]
    example_files: tuple[str, ...] = ()
    extra_test_files: tuple[str, ...] = ()  # e.g. Go's cases_test.go

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text(encoding="utf-8", errors="replace")

    def instructions(self) -> str:
        """introduction + instructions + instructions.append, as Aider's harness does."""
        parts = []
        for name in ("introduction.md", "instructions.md", "instructions.append.md"):
            p = self.path / ".docs" / name
            if p.is_file():
                parts.append(p.read_text(encoding="utf-8", errors="replace").strip())
        return "\n\n".join(parts)

    def stubs(self) -> dict[str, str]:
        return {rel: self.read(rel) for rel in self.solution_files if (self.path / rel).is_file()}

    def fingerprint(self) -> str:
        """sha256 over the exercise's solution stubs and test files."""
        h = {rel: sha256_text(self.read(rel)) for rel in (*self.solution_files, *self.test_files, *self.extra_test_files)
             if (self.path / rel).is_file()}
        return sha256_json(h)


def load_exercise(path: Path, language: str) -> Exercise:
    cfg = json.loads((path / ".meta" / "config.json").read_text(encoding="utf-8"))
    files = cfg.get("files", {})
    extra = tuple(f for f in files.get("editor", []) if re.search(r"(_test|\.spec|\.test)\.", f))
    return Exercise(
        id=f"{language}/{path.name}",
        language=language,
        name=path.name,
        path=path,
        solution_files=tuple(files.get("solution", [])),
        test_files=tuple(files.get("test", [])),
        example_files=tuple(files.get("example", [])),
        extra_test_files=extra,
    )


def load_exercises(root: str | Path, languages: Iterable[str] = LANGUAGES) -> list[Exercise]:
    """All exercises under ``root``, sorted by id. Languages that are absent are skipped."""
    root = Path(root)
    out = []
    for lang in languages:
        practice = root / lang / "exercises" / "practice"
        if not practice.is_dir():
            continue
        for d in sorted(practice.iterdir()):
            if d.is_dir() and (d / ".meta" / "config.json").is_file():
                out.append(load_exercise(d, lang))
    if not out:
        raise FileNotFoundError(f"no exercises found under {root}")
    return sorted(out, key=lambda e: e.id)


# ------------------------------------------------------------------ partition


def scale_sizes(total: int, sizes: Mapping[str, int] = DEFAULT_SIZES) -> dict[str, int]:
    """Split sizes for ``total`` exercises: ``sizes`` itself if it fits exactly, else scaled
    proportionally by largest remainder (ties broken by split order)."""
    base = sum(sizes.values())
    if total == base:
        return dict(sizes)
    names = list(sizes)
    return _apportion({k: sizes[k] * total / base for k in names}, total, names)


def _apportion(exact: Mapping[str, float], total: int, order: Sequence[str], caps: Mapping[str, int] | None = None) -> dict[str, int]:
    out = {k: int(exact[k]) for k in order}
    if caps:
        out = {k: min(out[k], caps[k]) for k in order}
    rank = sorted(order, key=lambda k: (-(exact[k] - int(exact[k])), order.index(k)))
    short = total - sum(out.values())
    while short > 0:
        progressed = False
        for k in rank:
            if short == 0:
                break
            if caps is None or out[k] < caps[k]:
                out[k] += 1
                short -= 1
                progressed = True
        if not progressed:
            raise ValueError("cannot apportion: capacity exhausted")
    return out


@dataclass(frozen=True)
class Partition:
    seed: int
    sizes: Mapping[str, int]
    splits: Mapping[str, tuple[str, ...]]  # split -> sorted exercise ids
    dataset_sha256: str
    test_splits: Mapping[str, Mapping] = field(default_factory=dict)  # exercise id -> TestSplit.to_dict()

    def body(self) -> dict:
        return {
            "version": PARTITION_VERSION,
            "seed": self.seed,
            "sizes": dict(self.sizes),
            "splits": {k: list(v) for k, v in self.splits.items()},
            "dataset_sha256": self.dataset_sha256,
            "test_splits": {k: dict(v) for k, v in sorted(self.test_splits.items())},
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.body())

    def split_of(self, exercise_id: str) -> str:
        for k, ids in self.splits.items():
            if exercise_id in ids:
                return k
        raise KeyError(exercise_id)

    def to_json(self) -> str:
        return canonical_json({**self.body(), "sha256": self.sha256})

    def save(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json() + "\n", encoding="utf-8")
        return self.sha256

    @classmethod
    def load(cls, path: str | Path) -> "Partition":
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        part = cls(
            seed=d["seed"],
            sizes=d["sizes"],
            splits={k: tuple(v) for k, v in d["splits"].items()},
            dataset_sha256=d["dataset_sha256"],
            test_splits=d.get("test_splits", {}),
        )
        if d.get("sha256") != part.sha256:
            raise ValueError(f"partition {path}: sha256 does not match its content")
        return part


def dataset_sha256(exercises: Sequence[Exercise]) -> str:
    return sha256_json({e.id: e.fingerprint() for e in sorted(exercises, key=lambda e: e.id)})


def partition_ids(
    ids_by_language: Mapping[str, Sequence[str]],
    seed: int = DEFAULT_SEED,
    sizes: Mapping[str, int] = DEFAULT_SIZES,
) -> dict[str, tuple[str, ...]]:
    """Language-stratified deterministic partition of exercise ids.

    Each language's ids are sorted, then shuffled with ``Random(f"{seed}:{lang}")``.
    Splits are filled in order; each split takes from every language in proportion
    to what that language has left (largest remainder), so per-language shares
    stay as even as integer counts allow and the totals are exact.
    """
    langs = sorted(ids_by_language)
    total = sum(len(v) for v in ids_by_language.values())
    sz = scale_sizes(total, sizes)
    pools = {}
    for lang in langs:
        ids = sorted(ids_by_language[lang])
        random.Random(f"{seed}:{lang}").shuffle(ids)
        pools[lang] = ids
    remaining = {lang: len(pools[lang]) for lang in langs}
    out: dict[str, tuple[str, ...]] = {}
    names = list(sz)
    for i, split in enumerate(names):
        rem_total = sum(remaining.values())
        if i == len(names) - 1:
            take = dict(remaining)
        elif rem_total == 0:
            take = {lang: 0 for lang in langs}
        else:
            exact = {lang: remaining[lang] * sz[split] / rem_total for lang in langs}
            take = _apportion(exact, sz[split], langs, caps=remaining)
        chosen = []
        for lang in langs:
            n = take[lang]
            used = len(pools[lang]) - remaining[lang]
            chosen.extend(pools[lang][used : used + n])
            remaining[lang] -= n
        out[split] = tuple(sorted(chosen))
    return out


def make_partition(
    exercises: Sequence[Exercise],
    seed: int = DEFAULT_SEED,
    sizes: Mapping[str, int] = DEFAULT_SIZES,
    with_test_splits: bool = True,
) -> Partition:
    by_lang: dict[str, list[str]] = {}
    for e in exercises:
        by_lang.setdefault(e.language, []).append(e.id)
    splits = partition_ids(by_lang, seed, sizes)
    ts = {e.id: split_tests(e, seed).to_dict() for e in exercises} if with_test_splits else {}
    return Partition(seed, scale_sizes(len(exercises), sizes), splits, dataset_sha256(exercises), ts)


# ------------------------------------------------------------------ test discovery

# Heuristic test-case patterns per language (used for counting and, except for
# Python, only for counting). Go counts table rows, since most Go exercises use
# one test function over a table of cases.
_COUNT_PATTERNS: Mapping[str, str] = {
    "javascript": r"^\s*x?(?:test|it)\(",
    "java": r"@Test\b",
    "rust": r"#\[test\]",
    "cpp": r"\bTEST_CASE\(",
}
_JS_NAME = re.compile(r"""^\s*x?(?:test|it)\(\s*(['"`])(.*?)\1""", re.M)


@dataclass(frozen=True)
class PyTest:
    id: str  # "Class.test_name" or "test_name" for module-level tests
    start: int  # first line (1-based, including decorators)
    end: int  # last line (inclusive)
    file: str


def python_tests(source: str, file: str = "") -> list[PyTest]:
    """Test functions in a unittest/pytest file, found with ``ast`` (nothing is executed)."""
    tree = ast.parse(source)
    out = []

    def visit(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                out.append(PyTest(prefix + node.name, start, node.end_lineno or node.lineno, file))
            elif isinstance(node, ast.ClassDef) and not prefix:
                visit(node.body, node.name + ".")

    visit(tree.body, "")
    return out


def list_tests(ex: Exercise) -> list[str]:
    """Test ids for an exercise. Exact for Python and (by name) JavaScript; synthetic
    ``#n`` ids from the counting heuristic elsewhere."""
    ids: list[str] = []
    multi = len(ex.test_files) > 1
    for rel in ex.test_files:
        if not (ex.path / rel).is_file():
            continue
        src = ex.read(rel)
        pre = f"{rel}::" if multi else ""
        if ex.language == "python":
            ids.extend(pre + t.id for t in python_tests(src, rel))
        elif ex.language == "javascript":
            names = [m.group(2) for m in _JS_NAME.finditer(src)]
            n = len(re.findall(_COUNT_PATTERNS["javascript"], src, re.M))
            if len(names) != n or len(set(names)) != n:
                names = [f"#{i}" for i in range(n)]
            ids.extend(pre + x for x in names)
    if ex.language in ("python", "javascript"):
        return ids
    return [f"#{i}" for i in range(count_tests(ex))]


def count_tests(ex: Exercise) -> int:
    """Heuristic number of test cases.

    python: ``def test*`` functions (via ast); javascript: ``test(``/``xtest(``/``it(``
    calls; java: ``@Test``; rust: ``#[test]``; cpp: ``TEST_CASE(``; go: the largest of
    ``description:`` fields, one-tab or two-tab ``{`` table rows, and ``func Test*``
    across the test and ``*_test`` editor files.
    """
    texts = [ex.read(r) for r in (*ex.test_files, *ex.extra_test_files) if (ex.path / r).is_file()]
    if ex.language == "python":
        return sum(len(python_tests(t)) for t in texts)
    text = "\n".join(texts)
    if ex.language == "go":
        return max(
            len(re.findall(r"^\s*description:", text, re.M)),
            len(re.findall(r"^\t\t?\{\s*$", text, re.M)),
            len(re.findall(r"^func Test\w+", text, re.M)),
        )
    pat = _COUNT_PATTERNS.get(ex.language)
    return len(re.findall(pat, text, re.M)) if pat else 0


# ------------------------------------------------------------------ test split


@dataclass(frozen=True)
class TestSplit:
    __test__ = False  # not a pytest class

    exercise_id: str
    method: str  # "function" (test-level split) | "fallback" (full suite visible and scored)
    tests: tuple[str, ...]
    visible: tuple[str, ...]
    gate_private: tuple[str, ...]
    score: tuple[str, ...]

    def subset(self, name: str) -> tuple[str, ...]:
        if name not in SUBSETS:
            raise ValueError(f"unknown test subset {name!r}")
        return getattr(self, name)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "tests": list(self.tests),
            "visible": list(self.visible),
            "gate_private": list(self.gate_private),
            "score": list(self.score),
        }

    @classmethod
    def from_dict(cls, exercise_id: str, d: Mapping) -> "TestSplit":
        return cls(exercise_id, d["method"], *(tuple(d[k]) for k in ("tests", "visible", "gate_private", "score")))


def split_sizes(n: int) -> tuple[int, int, int]:
    """(visible, gate_private, score) counts for ``n >= MIN_SPLIT_TESTS`` tests: a third each,
    at least two each, with any remainder going to visible."""
    k = max(2, n // 3)
    return n - 2 * k, k, k


def split_tests(ex: Exercise, seed: int = DEFAULT_SEED) -> TestSplit:
    tests = tuple(list_tests(ex))
    if ex.language != "python" or len(tests) < MIN_SPLIT_TESTS:
        return TestSplit(ex.id, "fallback", tests, tests, (), tests)
    ranked = sorted(tests, key=lambda t: sha256_text(f"{seed}:{ex.id}:{t}"))
    nv, ng, _ = split_sizes(len(tests))
    order = {t: i for i, t in enumerate(tests)}

    def keep(xs):
        return tuple(sorted(xs, key=order.__getitem__))

    return TestSplit(ex.id, "function", tests, keep(ranked[:nv]), keep(ranked[nv : nv + ng]), keep(ranked[nv + ng :]))


def _filter_python(source: str, keep: set[str], file: str, prefix: str) -> str:
    lines = source.splitlines(keepends=True)
    tests = python_tests(source, file)
    drop = [t for t in tests if prefix + t.id not in keep]
    dropped = set()
    for t in drop:
        dropped.update(range(t.start, t.end + 1))
    # A class whose body would become empty gets a ``pass`` so the file still parses.
    tree = ast.parse(source)
    need_pass: dict[int, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            body_lines = set()
            for b in node.body:
                start = min([b.lineno] + [d.lineno for d in getattr(b, "decorator_list", [])])
                body_lines.update(range(start, (b.end_lineno or b.lineno) + 1))
            if body_lines and body_lines <= dropped:
                first = node.body[0]
                indent = re.match(r"\s*", lines[first.lineno - 1]).group(0)
                need_pass[min(body_lines)] = f"{indent}pass\n"
    out = []
    for i, line in enumerate(lines, start=1):
        if i in need_pass:
            out.append(need_pass[i])
        if i not in dropped:
            out.append(line)
    text = "".join(out)
    return re.sub(r"\n{4,}", "\n\n\n", text)


def materialize_tests(ex: Exercise, split: TestSplit, subset: str) -> dict[str, str]:
    """Test file contents (path -> text) holding only ``subset``'s tests.

    Python with ``method == "function"``: each test file is rewritten without the
    other subsets' test functions (helpers, imports and setUp are kept). Fallback:
    ``visible`` and ``score`` get the unmodified suite, ``gate_private`` gets nothing.
    """
    names = split.subset(subset)
    if split.method == "fallback":
        if subset == "gate_private":
            return {}
        return {rel: ex.read(rel) for rel in (*ex.test_files, *ex.extra_test_files) if (ex.path / rel).is_file()}
    if ex.language != "python":
        raise NotImplementedError(f"test-level materialization for {ex.language}")
    keep = set(names)
    multi = len(ex.test_files) > 1
    out = {}
    for rel in ex.test_files:
        if (ex.path / rel).is_file():
            out[rel] = _filter_python(ex.read(rel), keep, rel, f"{rel}::" if multi else "")
    return out


# ------------------------------------------------------------------ workspaces


def build_workspace(ex: Exercise, files: Mapping[str, str], tests: Mapping[str, str], dest: str | Path) -> Path:
    """Copy the exercise to ``dest`` without hidden directories, original tests or
    reference solutions, then write ``files`` (a submission) and ``tests`` over it."""
    dest = Path(dest)
    test_like = set(ex.test_files) | set(ex.extra_test_files) | set(ex.example_files)

    def ignore(dirpath, names):
        rel_dir = Path(dirpath).relative_to(ex.path)
        out = []
        for n in names:
            rel = (rel_dir / n).as_posix()
            if (rel_dir == Path(".") and n in _HIDDEN_DIRS) or rel in test_like:
                out.append(n)
        return out

    shutil.copytree(ex.path, dest, ignore=ignore, dirs_exist_ok=True)
    for rel, text in {**files, **tests}.items():
        p = dest / rel
        if dest.resolve() not in p.resolve().parents:
            raise ValueError(f"path escapes workspace: {rel}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return dest


def run_python_tests(workdir: str | Path, test_files: Sequence[str], timeout: float = 120) -> tuple[bool, str]:
    """Run pytest on ``test_files`` inside ``workdir``. Returns (all passed, output tail).

    This executes the submission and the tests: call it only inside the sandbox the
    experiment provides, never on a host you care about.
    """
    if not test_files:
        return False, "no tests"
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *test_files]
    try:
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]
