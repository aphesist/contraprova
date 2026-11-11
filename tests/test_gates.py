"""Gates and the temp-workspace runner. No network, no API keys."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from contraprova.contracts import LaneContract
from contraprova.gates import (
    T0_SCOPE,
    T0_VISIBLE,
    T1_PRIVATE,
    T2_REVIEW,
    changed_lines,
    decision_payload,
    hash_protected_files,
    required_tiers,
    run_gates,
    run_test_gate,
    scope_gate,
    protected_digest,
)
from contraprova.events import GENESIS_HASH, Event
from contraprova.interfaces import GateResult, Submission
from contraprova.sandbox import parse_counts, run_in_temp_workspace, safe_relpath, truncate

PYTEST = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests")
UNITTEST = (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".")
PRIVATE = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "private")

GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
        "    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n"
    )
    return root


@pytest.fixture
def private(tmp_path: Path) -> Path:
    d = tmp_path / "priv" / "private"
    d.mkdir(parents=True)
    (d / "test_hidden.py").write_text("from calc import add\n\ndef test_neg():\n    assert add(-1, -2) == -3\n")
    return d.parent


@pytest.fixture
def contract() -> LaneContract:
    return LaneContract(lane_id="L1", task_id="t1", allowed=("calc.py", "src/*", "pyproject.toml"))


# ------------------------------------------------------------ scope / tamper


def test_scope_passes_inside_contract(contract):
    res = scope_gate(Submission("t1", {"calc.py": GOOD}), contract, test_hashes={})
    assert res.gate == T0_SCOPE and res.verdict == "pass"


def test_scope_blocks_out_of_scope_and_protected(contract):
    s = Submission("t1", {"calc.py": GOOD, "other.py": "x", "tests/test_calc.py": "", "../escape.py": ""})
    res = scope_gate(s, contract, test_hashes={})
    assert res.verdict == "block"
    reasons = {v["path"]: v["reason"] for v in res.evidence["violations"]}
    assert reasons == {"other.py": "out_of_scope", "tests/test_calc.py": "protected", "../escape.py": "out_of_scope"}
    assert "other.py" in res.reason and "tests/test_calc.py" in res.reason


def test_hashes_cover_protected_files_only(ws, contract):
    h = hash_protected_files(ws, contract)
    assert set(h) == {"tests/__init__.py", "tests/test_calc.py"}
    assert all(len(v) == 64 for v in h.values())
    assert protected_digest(h) == protected_digest(dict(reversed(list(h.items()))))


def test_touching_hashed_test_file_blocks_even_if_contract_allows(ws):
    loose = LaneContract("L1", "t1", allowed=("*",), protected=())
    hashes = {"tests/test_calc.py": "0" * 64}
    res = scope_gate(Submission("t1", {"tests/test_calc.py": "pass"}), loose, test_hashes=hashes)
    assert res.verdict == "block"
    assert res.evidence["violations"] == [{"path": "tests/test_calc.py", "reason": "protected"}]
    assert res.evidence["tests_sha256"] == protected_digest(hashes)


def test_direct_workspace_tamper_is_detected(ws, contract):
    before = hash_protected_files(ws, contract)
    (ws / "tests" / "test_calc.py").write_text("def test_nothing():\n    pass\n")
    results = run_gates(Submission("t1", {"calc.py": GOOD}), contract, ws, visible_command=PYTEST, test_hashes=before)
    assert [r.gate for r in results] == [T0_SCOPE]
    assert results[0].verdict == "block"
    assert {"path": "tests/test_calc.py", "reason": "test_tampered"} in results[0].evidence["violations"]


# ------------------------------------------------------------ test runner


def test_visible_tests_pass_and_fail(ws):
    ok = run_test_gate(T0_VISIBLE, Submission("t1", {"calc.py": GOOD}), ws, PYTEST, timeout=60)
    assert ok.verdict == "pass"
    assert ok.evidence["tests"]["counts"] == {"passed": 2}
    bad = run_test_gate(T0_VISIBLE, Submission("t1", {"calc.py": BAD}), ws, PYTEST, timeout=60)
    assert bad.verdict == "block"
    assert bad.evidence["tests"]["counts"].get("failed") == 1
    assert "failed=1" in bad.reason


def test_unittest_command_and_counts(ws):
    bad = run_test_gate(T0_VISIBLE, Submission("t1", {"calc.py": BAD}), ws, UNITTEST, timeout=60)
    assert bad.verdict == "block"
    assert bad.evidence["tests"]["counts"] == {"ran": 2, "failures": 1}


def test_real_workspace_is_untouched(ws):
    before = (ws / "calc.py").read_text()
    run_test_gate(T0_VISIBLE, Submission("t1", {"calc.py": GOOD, "src/new.py": "x = 1\n"}), ws, PYTEST)
    assert (ws / "calc.py").read_text() == before
    assert not (ws / "src").exists()


def test_timeout_blocks(tmp_path):
    cmd = (sys.executable, "-c", "import time; time.sleep(30)")
    res = run_in_temp_workspace(tmp_path, {}, cmd, timeout=0.5)
    assert res.timed_out and not res.passed and res.returncode is None


def test_missing_command_does_not_raise(tmp_path):
    res = run_in_temp_workspace(tmp_path, {}, ("/nonexistent/contraprova-binary",), timeout=5)
    assert not res.passed and res.returncode is None and "could not start" in res.output


def test_output_is_truncated_and_path_free(tmp_path):
    cmd = (sys.executable, "-c", "import os; print(os.getcwd()); print('x' * 10000)")
    res = run_in_temp_workspace(tmp_path, {}, cmd, timeout=30)
    assert len(res.output.encode()) <= 2048
    big = run_in_temp_workspace(tmp_path, {}, (sys.executable, "-c", "import os; print(os.getcwd())"), timeout=30)
    assert big.output.strip() == "<workspace>"


def test_truncate_keeps_tail():
    t = truncate("a" * 5000 + "END", 100)
    assert t.endswith("END") and len(t.encode()) <= 100
    assert truncate("short") == "short"


def test_parse_counts():
    assert parse_counts("== 3 passed, 1 failed, 2 skipped in 0.1s ==") == {"passed": 3, "failed": 1, "skipped": 2}
    assert parse_counts("Ran 4 tests in 0.001s\n\nOK\n") == {"ran": 4}
    assert parse_counts("Ran 4 tests in 0.0s\n\nFAILED (failures=1, errors=2)\n") == {"ran": 4, "failures": 1, "errors": 2}
    assert parse_counts("nothing here") == {}


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../../b", "", "C:/x"])
def test_safe_relpath_rejects(bad):
    with pytest.raises(ValueError):
        safe_relpath(bad)


# ------------------------------------------------------------ tiering


def test_tiers_base(contract):
    assert required_tiers(Submission("t1", {"calc.py": GOOD}), contract, escalated=False) == (T0_SCOPE, T0_VISIBLE, T1_PRIVATE)


@pytest.mark.parametrize("path", ["pyproject.toml", "src/Cargo.toml", "go.mod", "package.json", "CMakeLists.txt", "build.gradle", "pom.xml"])
def test_tiers_manifest_adds_review(contract, path):
    assert required_tiers(Submission("t1", {path: "x"}), contract, escalated=False)[-1] == T2_REVIEW


def test_tiers_escalation_and_size(contract):
    small = Submission("t1", {"calc.py": GOOD})
    assert T2_REVIEW in required_tiers(small, contract, escalated=True)
    big = Submission("t1", {"calc.py": "x\n" * 300})
    assert T2_REVIEW in required_tiers(big, contract, escalated=False)
    assert T2_REVIEW not in required_tiers(big, contract, escalated=False, max_changed_lines=1000)
    # unchanged content against the original counts as zero changed lines
    assert T2_REVIEW not in required_tiers(big, contract, escalated=False, original={"calc.py": "x\n" * 300})


def test_changed_lines():
    s = Submission("t1", {"a.py": "1\n2\n3\n"})
    assert changed_lines(s) == 3
    assert changed_lines(s, {"a.py": "1\nX\n3\n"}) == 2


# ------------------------------------------------------------ orchestration


def test_run_gates_all_pass(ws, private, contract):
    before = hash_protected_files(ws, contract)
    results = run_gates(
        Submission("t1", {"calc.py": GOOD}), contract, ws,
        visible_command=PYTEST, private_command=PRIVATE, private_dir=private, test_hashes=before,
    )
    assert [(r.gate, r.verdict) for r in results] == [(T0_SCOPE, "pass"), (T0_VISIBLE, "pass"), (T1_PRIVATE, "pass")]
    assert all(r.evidence["tests_sha256"] == protected_digest(before) for r in results)
    payload = decision_payload("t1", results)
    assert payload["verdict"] == "pass" and payload["tests_sha256"] == protected_digest(before)
    Event.create(0, "gate.decided", "gate", "2026-01-01T00:00:00Z", payload, GENESIS_HASH)  # JSON-safe


def test_private_tests_catch_what_visible_miss(ws, private, contract):
    sneaky = "def add(a, b):\n    return abs(a) + abs(b)\n"  # passes visible tests, fails negatives
    results = run_gates(Submission("t1", {"calc.py": sneaky}), contract, ws,
                        visible_command=PYTEST, private_command=PRIVATE, private_dir=private)
    assert [(r.gate, r.verdict) for r in results][-1] == (T1_PRIVATE, "block")
    assert not (ws / "private").exists()


def test_run_gates_stops_at_first_block(ws, contract):
    results = run_gates(Submission("t1", {"tests/test_calc.py": ""}), contract, ws, visible_command=PYTEST)
    assert [r.gate for r in results] == [T0_SCOPE]
    results = run_gates(Submission("t1", {"calc.py": BAD}), contract, ws, visible_command=PYTEST)
    assert [(r.gate, r.verdict) for r in results] == [(T0_SCOPE, "pass"), (T0_VISIBLE, "block")]


def test_run_gates_review_hook(ws, contract):
    calls: list[Submission] = []

    def reviewer(s: Submission, c: LaneContract) -> GateResult:
        calls.append(s)
        return GateResult(T2_REVIEW, "block", "looks wrong", {"model": "fake"})

    s = Submission("t1", {"calc.py": GOOD})
    assert [r.gate for r in run_gates(s, contract, ws, visible_command=PYTEST, reviewer=reviewer)] == [T0_SCOPE, T0_VISIBLE]
    assert calls == []
    results = run_gates(s, contract, ws, visible_command=PYTEST, escalated=True, reviewer=reviewer)
    assert results[-1].gate == T2_REVIEW and results[-1].verdict == "block" and results[-1].reason == "looks wrong"
    assert results[-1].evidence["model"] == "fake" and results[-1].evidence["tiers"][-1] == T2_REVIEW
    assert len(calls) == 1
    no_reviewer = run_gates(s, contract, ws, visible_command=PYTEST, escalated=True)
    assert no_reviewer[-1].verdict == "block" and "no reviewer" in no_reviewer[-1].reason


def test_evidence_is_deterministic(ws, contract):
    s = Submission("t1", {"calc.py": BAD})
    a = run_gates(s, contract, ws, visible_command=UNITTEST)
    b = run_gates(s, contract, ws, visible_command=UNITTEST)
    strip = lambda rs: [(r.gate, r.verdict, r.reason, r.evidence["tests_sha256"], r.evidence.get("tests", {}).get("counts")) for r in rs]
    assert strip(a) == strip(b)
