"""Gates: the checks a worker's Submission must pass before it is accepted.

Tiers, cheapest first:

- ``T0.scope``: every written path is inside the lane contract and none is a
  protected (test) path. Also the test-tamper binding: the sha256 of every
  protected file in the workspace, taken before the attempt, must still match.
- ``T0.visible_tests``: the tests the worker could see pass on a temp copy.
- ``T1.private_tests``: gate-private tests pass on a temp copy.
- ``T2.review``: only when the blast radius is large (build/manifest files,
  many changed lines, or after an escalation). The reviewer is an injected
  callable; nothing here calls a model. With no reviewer, T2 blocks by default;
  ``missing_reviewer="skip"`` records it as skipped (a pass whose evidence says
  ``status: skipped``) instead.

``run_gates`` runs the required tiers in order and stops at the first block.
Every result's evidence carries ``tests_sha256`` so it can be bound into the
``gate.decided`` event. Deterministic apart from what the test command does.
"""

from __future__ import annotations

import difflib
import hashlib
import os
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from contraprova.contracts import LaneContract, check_scope
from contraprova.events import canonical_json
from contraprova.interfaces import GateResult, Submission
from contraprova.sandbox import IGNORED_DIRS, run_in_temp_workspace, safe_relpath

T0_SCOPE = "T0.scope"
T0_VISIBLE = "T0.visible_tests"
T1_PRIVATE = "T1.private_tests"
T2_REVIEW = "T2.review"
BASE_TIERS: tuple[str, ...] = (T0_SCOPE, T0_VISIBLE, T1_PRIVATE)

MANIFEST_FILES = frozenset(
    {
        "Cargo.toml",
        "go.mod",
        "package.json",
        "CMakeLists.txt",
        "pyproject.toml",
        "build.gradle",
        "build.gradle.kts",
        "pom.xml",
    }
)
DEFAULT_MAX_CHANGED_LINES = 200

Reviewer = Callable[[Submission, LaneContract], GateResult]


# ------------------------------------------------------------ test hashing


def hash_protected_files(workspace: Path, contract: LaneContract) -> dict[str, str]:
    """sha256 of every file in ``workspace`` matching the contract's protected patterns.

    Call this before the attempt starts. Keys are repo-relative POSIX paths.
    """
    out: dict[str, str] = {}
    for root, dirs, names in os.walk(workspace):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for name in sorted(names):
            full = Path(root, name)
            rel = full.relative_to(workspace).as_posix()
            if full.is_file() and any(fnmatchcase(rel, pat) for pat in contract.protected):
                out[rel] = hashlib.sha256(full.read_bytes()).hexdigest()
    return dict(sorted(out.items()))


def protected_digest(hashes: Mapping[str, str]) -> str:
    """One sha256 over the whole path -> hash map."""
    return hashlib.sha256(canonical_json(dict(hashes)).encode("utf-8")).hexdigest()


def changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Paths added, removed or modified between two hash maps."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


# ------------------------------------------------------------ tiering


def changed_lines(submission: Submission, original: Mapping[str, str] | None = None) -> int:
    """Lines added plus lines removed, against ``original`` (missing = new file)."""
    original = original or {}
    total = 0
    for path, new in sorted(submission.files.items()):
        old = original.get(path, "")
        for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
            if line.startswith(("+++", "---")):
                continue
            if line.startswith(("+", "-")):
                total += 1
    return total


def touches_manifest(submission: Submission) -> list[str]:
    return sorted(p for p in submission.files if PurePosixPath(p.replace("\\", "/")).name in MANIFEST_FILES)


def required_tiers(
    submission: Submission,
    contract: LaneContract,
    *,
    escalated: bool,
    original: Mapping[str, str] | None = None,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
) -> tuple[str, ...]:
    """The gate tiers this submission must pass, by blast radius. Pure.

    T2.review is added when the submission touches a build/manifest file,
    changes more than ``max_changed_lines`` lines against ``original``, or
    follows an escalation.
    """
    del contract  # part of the signature so tiering can later depend on the lane
    if escalated or touches_manifest(submission) or changed_lines(submission, original) > max_changed_lines:
        return BASE_TIERS + (T2_REVIEW,)
    return BASE_TIERS


def _tier_reasons(submission: Submission, *, escalated: bool, original: Mapping[str, str] | None, max_changed_lines: int) -> dict[str, Any]:
    return {
        "escalated": escalated,
        "manifests": touches_manifest(submission),
        "changed_lines": changed_lines(submission, original),
        "max_changed_lines": max_changed_lines,
    }


# ------------------------------------------------------------ gates


def scope_gate(
    submission: Submission,
    contract: LaneContract,
    *,
    test_hashes: Mapping[str, str],
    current_hashes: Mapping[str, str] | None = None,
) -> GateResult:
    """T0.scope: contract scope, protected paths, and the test-tamper binding.

    ``test_hashes`` were taken before the attempt. Any submission writing one of
    those paths is blocked, as is any difference from ``current_hashes`` (the
    protected files as they are now, if the worker had direct workspace access).
    """
    violations = [{"path": v.path, "reason": v.reason} for v in check_scope(contract, sorted(submission.files))]
    flagged = {v["path"] for v in violations}
    for path in sorted(submission.files):
        if path.replace("\\", "/") in test_hashes and path not in flagged:
            violations.append({"path": path, "reason": "protected"})
    if current_hashes is not None:
        for path in changed_files(test_hashes, current_hashes):
            violations.append({"path": path, "reason": "test_tampered"})
    evidence: dict[str, Any] = {
        "tests_sha256": protected_digest(test_hashes),
        "test_files": len(test_hashes),
        "touched": sorted(submission.files),
        "violations": violations,
    }
    if violations:
        reason = "; ".join(f"{v['path']}: {v['reason']}" for v in violations)
        return GateResult(T0_SCOPE, "block", reason, evidence)
    return GateResult(T0_SCOPE, "pass", "", evidence)


def run_test_gate(
    gate: str,
    submission: Submission,
    workspace: Path,
    command: Sequence[str],
    *,
    timeout: float = 60.0,
    overlay: Path | None = None,
    dir_name: str = "ws",
) -> GateResult:
    """Run ``command`` on a temp copy of ``workspace`` (named ``dir_name``) with the submission applied."""
    result = run_in_temp_workspace(workspace, submission.files, command, timeout=timeout, overlay=overlay,
                                   dir_name=dir_name)
    evidence = {"tests": result.summary()}
    if result.passed:
        return GateResult(gate, "pass", "", evidence)
    if result.timed_out:
        reason = f"timed out after {timeout}s"
    elif result.returncode is None:
        reason = "test command could not start"
    else:
        counts = ", ".join(f"{k}={v}" for k, v in sorted(result.counts.items()))
        reason = f"exit code {result.returncode}" + (f" ({counts})" if counts else "")
    return GateResult(gate, "block", reason, evidence)


MISSING_REVIEWER = ("block", "skip")


def review_gate(submission: Submission, contract: LaneContract, reviewer: Reviewer | None,
                missing_reviewer: str = "block") -> GateResult:
    """T2.review: delegate to the injected reviewer. With none configured, block, or with
    ``missing_reviewer="skip"`` pass with ``status: skipped`` in the evidence, so the missing
    check is recorded but never turns a submission into a gate failure."""
    if missing_reviewer not in MISSING_REVIEWER:
        raise ValueError(f"missing_reviewer must be one of {MISSING_REVIEWER}, got {missing_reviewer!r}")
    if reviewer is None:
        if missing_reviewer == "skip":
            return GateResult(T2_REVIEW, "pass", "review required but no reviewer configured; skipped",
                              {"status": "skipped"})
        return GateResult(T2_REVIEW, "block", "review required but no reviewer configured", {})
    res = reviewer(submission, contract)
    if res.verdict not in ("pass", "block"):
        return GateResult(T2_REVIEW, "block", f"reviewer returned unknown verdict {res.verdict!r}", dict(res.evidence))
    return GateResult(T2_REVIEW, res.verdict, res.reason, dict(res.evidence))


def _read_originals(workspace: Path, paths: Sequence[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        try:
            full = workspace.joinpath(*safe_relpath(p).parts)
        except ValueError:
            continue
        if full.is_file():
            out[p] = full.read_text(encoding="utf-8", errors="replace")
    return out


def run_gates(
    submission: Submission,
    contract: LaneContract,
    workspace: Path,
    *,
    visible_command: Sequence[str],
    private_command: Sequence[str] | None = None,
    private_dir: Path | None = None,
    test_hashes: Mapping[str, str] | None = None,
    escalated: bool = False,
    reviewer: Reviewer | None = None,
    missing_reviewer: str = "block",
    timeout: float = 60.0,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
    dir_name: str = "ws",
) -> list[GateResult]:
    """Run the required tiers in order, stopping at the first block.

    ``test_hashes`` should come from :func:`hash_protected_files` before the
    attempt; if omitted they are taken now, which binds the evidence but cannot
    detect tampering done directly in the workspace. ``private_dir`` is copied
    over the temp workspace before ``private_command`` runs (gate-private tests
    the worker never saw). If ``private_command`` is None, T1 is skipped and is
    absent from both the results and the recorded tier plan. ``missing_reviewer``
    is passed to :func:`review_gate`.
    """
    current = hash_protected_files(workspace, contract)
    before = current if test_hashes is None else dict(test_hashes)
    original = _read_originals(workspace, sorted(submission.files))
    tiers = required_tiers(submission, contract, escalated=escalated, original=original, max_changed_lines=max_changed_lines)
    if private_command is None:
        tiers = tuple(t for t in tiers if t != T1_PRIVATE)
    common = {
        "tests_sha256": protected_digest(before),
        "tiers": list(tiers),
        "blast_radius": _tier_reasons(submission, escalated=escalated, original=original, max_changed_lines=max_changed_lines),
    }

    results: list[GateResult] = []
    for tier in tiers:
        if tier == T0_SCOPE:
            res = scope_gate(submission, contract, test_hashes=before, current_hashes=current)
        elif tier == T0_VISIBLE:
            res = run_test_gate(T0_VISIBLE, submission, workspace, visible_command, timeout=timeout, dir_name=dir_name)
        elif tier == T1_PRIVATE:
            assert private_command is not None
            res = run_test_gate(T1_PRIVATE, submission, workspace, private_command, timeout=timeout, overlay=private_dir,
                                dir_name=dir_name)
        else:
            res = review_gate(submission, contract, reviewer, missing_reviewer)
        res = GateResult(res.gate, res.verdict, res.reason, {**res.evidence, **common})
        results.append(res)
        if res.verdict == "block":
            break
    return results


def decision_payload(task_id: str, results: Sequence[GateResult]) -> dict[str, Any]:
    """Fold a run's results into one ``gate.decided`` payload (verdict of the last gate run)."""
    if not results:
        raise ValueError("no gate results")
    last = results[-1]
    return {
        "task_id": task_id,
        "verdict": last.verdict,
        "gate": last.gate,
        "reason": last.reason,
        "tests_sha256": last.evidence.get("tests_sha256"),
        "gates": [{"gate": r.gate, "verdict": r.verdict, "reason": r.reason, "evidence": dict(r.evidence)} for r in results],
    }
