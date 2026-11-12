"""Arms A-D on SWE-bench Pro: prompts, the edit path, Docker gates and scorer, and the task loop.

``ProHarness`` is ``arms.Harness`` with the benchmark hooks overridden, so the arm logic (A, B,
C's brief/review/re-dispatch, D's router, gates and forks), budgeting, usage recording and event
payloads are exactly the Polyglot harness's. What differs:

* **Edit format.** Workers answer with SEARCH/REPLACE blocks (``harness.edits``), applied to the
  files they were shown. Blocks outside the task's files or on test files are rejected before
  anything is applied. ``task.submitted`` carries the full new content of each changed file,
  the reply (``response``), the unified diff and every parse/apply error.
* **Context rule.** Workers see the full files. The orchestrator (arm C/D brief, arm C review)
  and the T2 reviewer see an *outline*: file list, signatures (``harness.outline``), the problem
  statement, requirements, interface and visible test names; the reviewers add the diff. Never a
  file body. This is the crux of the cost hypothesis (docs/swepro.md).
* **Gates** (arm D): ``T0.scope`` (lane contract), ``T0.edits`` (every block applied, at least
  one file changed), ``T0.visible_tests`` (visible fail-to-pass tests pass), ``T1.private_tests``
  (the other fail-to-pass tests pass) and ``T2.review`` when the diff is large (more than
  ``t2_max_lines`` changed lines, default 500) or touches a manifest. The test gates and the
  scorer share one verifier run per distinct submission (memoised by file hash): the official
  ``test.sh`` in an offline container, whose per-test statuses give the gate verdicts and whose
  reward is the score.
* **Score** = the official reward (all fail-to-pass and pass-to-pass tests), seen by no model.
* **Loop order**: tasks outermost, arms inside (:func:`run_tasks`), so each image is pulled once
  per task; finished (task, arm) pairs found in the log are skipped, so a run resumes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from ..contracts import DEFAULT_PROTECTED, LaneContract, check_scope
from ..gates import MANIFEST_FILES, T0_SCOPE, T0_VISIBLE, T1_PRIVATE, T2_REVIEW, changed_lines, review_gate
from ..interfaces import HAIKU, OPUS, SONNET, Completion, GateResult, Message, Submission
from ..pricing import PER_MTOK, PRICE_TABLES, DEFAULT_TABLE
from ..projections import project_tasks
from .arms import (
    ArmConfig,
    BudgetExceeded,
    Harness,
    ParsedFiles,
    ScoreResult,
    TaskContext,
    format_files,
    parse_review_verdict,
)
from .edits import EDIT_FORMAT, apply_edits, diff_to_blocks, format_blocks, parse_edits, scope_checker, unified_patch
from .outline import render_outline
from .polyglot import sha256_json, sha256_text
from .swepro import CHARS_PER_TOKEN, TEST_PATTERNS, ProExercise, ProTestSplit, estimate_tokens, verifier_sha256

PRO_ARMS: tuple[str, ...] = ("A", "B", "C", "D", "B2", "La")  # B2: a second Haiku sample (probe); La: lanes
DEFAULT_T2_MAX_LINES = 500
OUTLINE_MAX_CHARS = 24_000  # about 8K tokens: the outline part of an orchestrator prompt
# Hard bound (estimated tokens) on the orchestrator's brief prompt; the review prompts add the diff,
# itself capped at DIFF_MAX_CHARS (about 13K tokens).
LEAD_MAX_TOKENS_EST = 16_000
DIFF_MAX_CHARS = 40_000
MANIFESTS = frozenset(MANIFEST_FILES | {"setup.py", "setup.cfg", "requirements.txt", "package-lock.json",
                                         "yarn.lock", "tsconfig.json", "babel.config.js", "Makefile"})

# ------------------------------------------------------------------ prompts

PRO_WORKER_SYSTEM = (
    "You are a careful software engineer resolving one issue in a large existing repository. You are "
    "given the issue, its requirements, the interface it must provide, and the full current content of "
    "the files to change. Change only those files, and create only the new files listed. Keep existing "
    "public names and behaviour unless the task says otherwise. You cannot run code or see the tests' "
    "source.\n\n" + EDIT_FORMAT
)

PRO_WORKER_TASK = """Repository: {repo} ({language})
Task: {instance_id}

## Problem statement
{statement}

## Requirements
{requirements}

## Interface
{interface}

## Files you may change (full current content)
{files}

## New files you may create
{new_files}

## Tests that must pass (names only; their code is not shown)
{tests}
{brief}"""

PRO_WORKER_RETRY = """Your previous attempt was rejected and discarded.

## Your previous changes (diff against the files above)
{diff}

## Why it was rejected
{feedback}

Write a corrected solution as SEARCH/REPLACE blocks against the ORIGINAL files shown above."""

PRO_LEAD_SYSTEM = (
    "You lead a programmer who resolves one issue in a large repository. You see an outline of the "
    "files involved (paths, signatures, one-line summaries), never their full content, and you cannot "
    "edit files or run code. The message gives the task first and your request last; do exactly what "
    "the request asks."
)

PRO_LEAD_CONTEXT = """Repository: {repo} ({language})
Task: {instance_id}

## Problem statement
{statement}

## Requirements
{requirements}

## Interface
{interface}

## Outline of the files the programmer may change
{outline}

## Tests that must pass (names only)
{tests}
"""

PRO_BRIEF_REQUEST = """
## Request: brief
Write a brief for the programmer (at most 250 words): which file and function to change for each \
requirement, the approach, edge cases and pitfalls. Name functions and files exactly. No code longer than \
one line."""

PRO_REVIEW_REQUEST = """
## Request: review
Review the programmer's change below by reading the diff; you cannot run it. Answer with ACCEPT on the \
first line, or REDISPATCH on the first line followed by at most 120 words of concrete feedback.

## Submitted change (unified diff)
{diff}"""

PRO_T2_SYSTEM = (
    "You check one large diff to an existing repository before it is merged. Tests run separately: do not "
    "judge whether the code is correct. You see an outline of the files and the diff only. Block only if "
    "the diff (1) adds a dependency that an offline build cannot fetch; (2) breaks a build or manifest "
    "file; (3) disables, deletes or weakens tests; or (4) does something unrelated to the task, such as "
    "network, file-system or process access, or obfuscated code. Otherwise pass. Reply with exactly one "
    "line: PASS, or BLOCK: followed by one sentence naming the rule broken."
)

PRO_T2_TASK = """Task: {instance_id} ({repo})
Files changed: {files}
Lines changed: {changed}
Review required because: {why}

## Outline of the files involved
{outline}

## Diff against the base commit
{diff}
"""


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {x}" for x in items) or "(none)"


def _cap(text: str, n: int, note: str) -> str:
    return text if len(text) <= n else text[:n] + f"\n[{note}]\n"


# ------------------------------------------------------------------ submissions


@dataclass(frozen=True)
class ProSubmission(Submission):
    """A Submission that remembers its edit errors and reply (for the T0.edits gate)."""

    edit_errors: tuple[str, ...] = ()
    blocks: int = 0
    response: str = ""  # the first reply's text (the lanes' shadow merge re-parses it)
    info: Mapping[str, Any] = field(default_factory=dict)  # the repair-turn fields logged on task.submitted
    seq: int | None = None  # seq of the task.submitted event


# ------------------------------------------------------------------ verifier, gates, scorer


class Verifier:
    """Runs the official verifier for (task, files) at most once: gates and scorer share results.

    ``run_fn(image, tests_dir, files) -> docker.VerifierResult``; ``ensure_image(image)`` is
    called first (e.g. ``ImageCache.ensure``)."""

    def __init__(self, run_fn: Callable[..., Any], ensure_image: Callable[[str], Any] | None = None):
        self.run_fn = run_fn
        self.ensure_image = ensure_image
        self.memo: dict[tuple[str, str], Any] = {}
        self.runs = 0

    def run(self, ex: ProExercise, files: Mapping[str, str]):
        key = (ex.id, sha256_json(dict(files)))
        if key not in self.memo:
            if self.ensure_image is not None:
                self.ensure_image(ex.task.image)
            self.memo[key] = self.run_fn(ex.task.image, ex.verifier, dict(files))
            self.runs += 1
        return self.memo[key]


def _tests_digest(ex: ProExercise) -> str:
    vs = verifier_sha256(ex.verifier) if ex.verifier is not None and ex.verifier.is_dir() else ""
    return sha256_json({"verifier": vs, "test_patch": sha256_text(ex.task.test_patch)})


def changed_line_count(sub: Submission, ex: ProExercise) -> int:
    return changed_lines(sub, ex.context.files)


def manifests_touched(sub: Submission) -> list[str]:
    return sorted(p for p in sub.files if p.rsplit("/", 1)[-1] in MANIFESTS)


@dataclass
class ProGates:
    """Arm D's gates for SWE-bench Pro (see the module docstring). One call = one submission."""

    verifier: Verifier
    t2_max_lines: int = DEFAULT_T2_MAX_LINES
    # Deterministic checks run after T0.edits passes and before any test runs (arm La adds
    # T0.interfaces here). Empty for arm D.
    pre_test: Sequence[Callable[[Submission, TaskContext], GateResult]] = ()

    def __call__(self, sub: Submission, ctx: TaskContext) -> list[GateResult]:
        ex: ProExercise = ctx.exercise  # type: ignore[assignment]
        ts: ProTestSplit = ctx.test_split  # type: ignore[assignment]
        digest = _tests_digest(ex)
        changed = changed_line_count(sub, ex)
        manifests = manifests_touched(sub)
        need_t2 = changed > self.t2_max_lines or bool(manifests)
        tiers = [T0_SCOPE, "T0.edits", *(getattr(c, "gate_name", "T0.pre_test") for c in self.pre_test), T0_VISIBLE] + ([T1_PRIVATE] if ts.gate_private else []) + \
            ([T2_REVIEW] if need_t2 else [])
        common = {"tests_sha256": digest, "tiers": tiers,
                  "blast_radius": {"changed_lines": changed, "max_changed_lines": self.t2_max_lines,
                                   "manifests": manifests}}
        out: list[GateResult] = []

        def add(r: GateResult) -> bool:
            out.append(GateResult(r.gate, r.verdict, r.reason, {**r.evidence, **common}))
            return r.verdict == "pass"

        contract = ctx.contract
        if contract is not None:
            v = [{"path": x.path, "reason": x.reason} for x in check_scope(contract, sorted(sub.files))]
            if not add(GateResult(T0_SCOPE, "block" if v else "pass", "; ".join(f"{x['path']}: {x['reason']}" for x in v),
                                  {"touched": sorted(sub.files), "violations": v})):
                return out
        errors = tuple(getattr(sub, "edit_errors", ()))
        if errors or not sub.files:
            reason = "; ".join(errors) if errors else "no file was changed (no SEARCH/REPLACE block applied)"
            add(GateResult("T0.edits", "block", reason, {"errors": list(errors), "files": len(sub.files)}))
            return out
        add(GateResult("T0.edits", "pass", "", {"errors": [], "files": len(sub.files)}))
        for check in self.pre_test:
            if not add(check(sub, ctx)):
                return out
        res = self.verifier.run(ex, sub.files)
        run_ev = {"seconds": round(res.seconds, 1), "timed_out": res.timed_out, "returncode": res.returncode,
                  "files_sha256": sha256_json(dict(sub.files))}
        vis_failed = res.failed(ts.visible)
        ev = {"tests": {"counts": {"ran": len(ts.visible), "passed": len(ts.visible) - len(vis_failed),
                                   "failed": len(vis_failed)}, "timed_out": res.timed_out,
                        "failed_ids": vis_failed}, "run": run_ev}
        if res.timed_out:
            reason = "the test run timed out"
        else:
            reason = "visible tests failed: " + ", ".join(vis_failed) if vis_failed else ""
        if not add(GateResult(T0_VISIBLE, "block" if vis_failed or res.timed_out else "pass", reason, ev)):
            return out
        if ts.gate_private:
            priv_failed = res.failed(ts.gate_private)
            ev = {"tests": {"counts": {"ran": len(ts.gate_private), "passed": len(ts.gate_private) - len(priv_failed),
                                       "failed": len(priv_failed)}}, "run": run_ev}
            # The reason is shown to workers on retry: counts only, never private test names.
            reason = f"{len(priv_failed)} of {len(ts.gate_private)} gate-private tests failed" if priv_failed else ""
            if not add(GateResult(T1_PRIVATE, "block" if priv_failed else "pass", reason, ev)):
                return out
        if need_t2:
            add(review_gate(sub, contract or LaneContract(ctx.task_id, ctx.task_id, ()), ctx.reviewer,
                            missing_reviewer="skip"))
        return out


@dataclass
class ProScorer:
    """The official reward for a submission (all fail-to-pass and pass-to-pass tests)."""

    verifier: Verifier

    def __call__(self, sub: Submission, ctx: TaskContext) -> ScoreResult:
        ex: ProExercise = ctx.exercise  # type: ignore[assignment]
        ts: ProTestSplit = ctx.test_split  # type: ignore[assignment]
        res = self.verifier.run(ex, sub.files)
        f2p_ok = len(res.passed(ts.tests))
        p2p = [t for t in ts.score if t not in set(ts.tests)]
        return ScoreResult(res.reward == 1, {
            "runner": "swepro-test.sh", "reward": res.reward, "n_score_tests": len(ts.score),
            "f2p_passed": f2p_ok, "f2p_total": len(ts.tests), "p2p_passed": len(res.passed(p2p)), "p2p_total": len(p2p),
            "seconds": round(res.seconds, 1), "timed_out": res.timed_out, "returncode": res.returncode,
            "files_sha256": sha256_json(dict(sub.files)), "tests_sha256": _tests_digest(ex),
            "output_sha256": sha256_text(res.output_tail),
        })


# ------------------------------------------------------------------ harness


REPAIR_TRUNCATED = ("Your answer was cut off at the output limit. Reply with only the remaining SEARCH/REPLACE "
                    "edit blocks; do not repeat blocks you already sent in full.")
REPAIR_ERRORS = ("Some of your SEARCH/REPLACE blocks could not be used:\n\n{errors}\n\n"
                 "Reply with only corrected edit blocks for those edits. The blocks that applied are kept; "
                 "do not repeat them.")
REPAIR_MAX_ERRORS = 20


def edit_failure(stop_reason: str | None, parse_errors: Sequence[str], apply_errors: Sequence[str]) -> str | None:
    """The edit-format failure of one reply: "truncated" (cut off at max_tokens), "parse" (a block
    was malformed), "apply" (a block did not match its file or was out of scope), or None."""
    if stop_reason == "max_tokens":
        return "truncated"
    if parse_errors:
        return "parse"
    if apply_errors:
        return "apply"
    return None


def repair_message(failure: str, errors: Sequence[str]) -> str:
    """The one repair turn's text: only the edit error, never test names, test output or results."""
    if failure == "truncated":
        return REPAIR_TRUNCATED
    shown = list(errors[:REPAIR_MAX_ERRORS])
    if len(errors) > REPAIR_MAX_ERRORS:
        shown.append(f"... and {len(errors) - REPAIR_MAX_ERRORS} more")
    return REPAIR_ERRORS.format(errors="\n".join(f"- {e}" for e in shown))


def pro_arm_config(**overrides: Any) -> ArmConfig:
    """Arm settings for Pro: workers get 32K output tokens on Haiku and Opus (the same for both, so
    neither arm is cut off sooner; probe 2 truncated both at 32K and 12K) and 16K on Sonnet; the
    orchestrator 4K (its thinking counts against max_tokens)."""
    base = dict(worker_max_tokens=16_000,
                worker_max_tokens_by_model=((HAIKU, 32_000), (SONNET, 16_000), (OPUS, 32_000)),
                orchestrator_max_tokens=4_000, feedback_chars=3_000, t2_diff_chars=DIFF_MAX_CHARS)
    base.update(overrides)
    return ArmConfig(**base)


@dataclass
class ProHarness(Harness):
    t2_max_lines: int = DEFAULT_T2_MAX_LINES
    # One repair turn per worker request (every arm, forks included): if the reply was cut off at
    # max_tokens, or some SEARCH/REPLACE block failed to parse or apply, one follow-up user turn
    # gives only that edit error (never test names or results); the blocks that applied plus the
    # repair's blocks are applied together. The repair request is tagged ``repair`` and costs the arm.
    repair: bool = True
    # False: task.submitted carries per-file sha256 (``contents_sha256``) instead of the files' full
    # new content, so a log can be published without redistributing repository code. The diff, the
    # worker's reply (its edit blocks) and test results are still logged.
    log_contents: bool = True
    outline_max_chars: int = OUTLINE_MAX_CHARS

    worker_system = PRO_WORKER_SYSTEM
    lead_system = PRO_LEAD_SYSTEM
    brief_request = PRO_BRIEF_REQUEST

    # ---- prompts

    def worker_prompt(self, ex: ProExercise, ts: ProTestSplit, brief: str = "") -> str:  # type: ignore[override]
        t = ex.task
        b = f"\n## Brief from the planner\n{brief.strip()}\n" if brief.strip() else ""
        return PRO_WORKER_TASK.format(
            repo=t.repo, language=t.language, instance_id=t.instance_id, statement=t.problem_statement.strip(),
            requirements=t.requirements.strip() or "(none)", interface=t.interface.strip() or "(none)",
            files=format_files(ex.context.files), new_files=_bullets(ex.context.new_files),
            tests=_bullets(ts.visible), brief=b)

    def outline(self, ex: ProExercise, max_chars: int | None = None) -> str:
        cap = self.outline_max_chars if max_chars is None else min(max_chars, self.outline_max_chars)
        return render_outline(ex.context.files, new_files=ex.context.new_files, max_chars=max(cap, 2000))

    def lead_context(self, ex: ProExercise, ts: ProTestSplit) -> str:  # type: ignore[override]
        """What the orchestrator sees: task text, outline and visible test names. No file bodies.

        The whole orchestrator prompt is kept under ``LEAD_MAX_TOKENS_EST`` estimated tokens by
        shrinking the outline (``render_outline`` drops nesting levels, then cuts)."""
        t = ex.task
        fields = dict(repo=t.repo, language=t.language, instance_id=t.instance_id,
                      statement=t.problem_statement.strip(), requirements=t.requirements.strip() or "(none)",
                      interface=t.interface.strip() or "(none)", tests=_bullets(ts.visible))
        rest = len(PRO_LEAD_CONTEXT.format(outline="", **fields)) + len(self.lead_system) + len(self.brief_request)
        room = int(LEAD_MAX_TOKENS_EST * CHARS_PER_TOKEN) - rest
        return PRO_LEAD_CONTEXT.format(outline=self.outline(ex, room), **fields)

    def retry_prompt(self, base: str, previous: Mapping[str, str], feedback: str, ex=None) -> str:  # type: ignore[override]
        fb = feedback.strip()[: self.config.feedback_chars] or "(no details)"
        diff = unified_patch(ex.context.files, previous) if ex is not None else ""
        diff = _cap(diff, DIFF_MAX_CHARS, "diff truncated") or "(no change was applied)"
        return base + "\n" + PRO_WORKER_RETRY.format(diff=diff, feedback=fb)

    def review_prompt(self, context: str, sub: Submission, ex: ProExercise) -> str:  # type: ignore[override]
        diff = _cap(unified_patch(ex.context.files, sub.files), DIFF_MAX_CHARS, "diff truncated")
        return context + PRO_REVIEW_REQUEST.format(diff=diff or "(no change was applied)")

    # ---- the edit path

    def scope(self, ex: ProExercise):
        return scope_checker(ex.solution_files, (*DEFAULT_PROTECTED, *TEST_PATTERNS))

    def _work(self, arm: str, task_id: str, ex: ProExercise, model: str, prompt: str, attempt: int,  # type: ignore[override]
              effort: str | None = None, role: str = "worker", fork_id: str | None = None,
              cache_upto: int = 0, *, scope=None, max_tokens: int | None = None, system: str | None = None,
              extra: Mapping[str, Any] | None = None, extra_tags: Mapping[str, Any] | None = None) -> Submission:
        """One worker request plus at most one edit-repair turn, then ``task.submitted``.

        Arm La passes the lane's own ``scope`` (blocks outside the lane's files are rejected before
        anything is applied and listed in ``rejected_blocks``), its tier's ``max_tokens``, the lane
        system prompt, ``extra`` fields for ``task.submitted`` and ``extra_tags`` for usage."""
        self.emit("task.claimed", f"worker:{model}", {"task_id": task_id, "worker": model, "attempt": attempt})
        if effort is None:
            effort = self.worker_effort(model)
        mt = max_tokens or self.worker_max_tokens(model)
        kw = dict(arm=arm, role=role, task_id=task_id, model=model, system=system or self.worker_system,
                  max_tokens=mt, effort=effort, fork_id=fork_id)
        c = self.call(user=prompt, cache_upto=cache_upto, extra_tags=extra_tags, **kw)
        lane_scope = scope is not None
        scope = scope or self.scope(ex)
        parsed = parse_edits(c.text)
        applied = apply_edits(parsed.blocks, ex.context.files, scope=scope)
        failure = edit_failure(c.stop_reason, parsed.errors, applied.errors)
        first_errors = list(parsed.errors + applied.errors)
        info: dict[str, Any] = {"repair_used": False, "original_failure": failure, "original_errors": first_errors,
                                "stop_reason": c.stop_reason}
        blocks, final, last_parse, last_stop = list(parsed.blocks), applied, parsed.errors, c.stop_reason
        sent = list(parsed.blocks)  # every block the worker sent, the repair turn's included
        if failure and self.repair:
            history = (Message("user", prompt, cache_upto), Message("assistant", c.text.rstrip() or "(no output)"))
            c2 = self.call(user=repair_message(failure, first_errors), history=history,
                           extra_tags={**dict(extra_tags or {}), "repair": True}, **kw)
            parsed2 = parse_edits(c2.text)
            offset = max((b.index for b in parsed.blocks), default=0)
            ok = set(applied.applied)
            blocks = [b for b in parsed.blocks if b.index in ok] + [replace(b, index=b.index + offset)
                                                                   for b in parsed2.blocks]
            sent += [replace(b, index=b.index + offset) for b in parsed2.blocks]
            final = apply_edits(blocks, ex.context.files, scope=scope)
            last_parse, last_stop = parsed2.errors, c2.stop_reason
            info.update(repair_used=True, repair_stop_reason=c2.stop_reason, repair_response=c2.text,
                        repair_blocks=len(parsed2.blocks))
        errors = list(last_parse + final.errors)
        if last_stop == "max_tokens":
            errors.append(f"reply cut off at max_tokens ({mt}); the unfinished block was dropped")
        info["final_failure"] = edit_failure(last_stop, last_parse, final.errors)
        if lane_scope:
            info["rejected_blocks"] = [{"block": b.index, "path": b.path, "reason": scope(b.path)}
                                       for b in sent if scope(b.path)]
        info.update(extra or {})
        self._last_blocks = len(blocks)
        return self.submit(task_id, attempt, model, ParsedFiles(final.files, tuple(errors)), ex, response=c.text,
                           extra=info)

    def parse_reply(self, text: str, ex: ProExercise) -> ParsedFiles:  # type: ignore[override]
        parsed = parse_edits(text)
        applied = apply_edits(parsed.blocks, ex.context.files, scope=self.scope(ex))
        self._last_blocks = len(parsed.blocks)
        return ParsedFiles(applied.files, parsed.errors + applied.errors)

    def submit(self, task_id: str, attempt: int, model: str, parsed: ParsedFiles, ex: ProExercise,  # type: ignore[override]
               response: str | None = None, extra: Mapping[str, Any] | None = None) -> Submission:
        files = dict(parsed.files)
        diff = unified_patch(ex.context.files, files)
        ev = self.emit("task.submitted", f"worker:{model}", {
            "task_id": task_id, "attempt": attempt, "model": model, "files": sorted(files),
            **({"contents": files} if self.log_contents else
               {"contents_sha256": {p: sha256_text(c) for p, c in sorted(files.items())}}),
            "files_sha256": sha256_json(files), "parse_errors": list(parsed.errors),
            "response": response if response is not None else "", "diff": diff,
            "edit_blocks": getattr(self, "_last_blocks", 0), "changed_lines": changed_lines(Submission(task_id, files),
                                                                                          ex.context.files),
            **dict(extra or {}),
        })
        return ProSubmission(task_id=task_id, files=files, attempt=attempt, edit_errors=tuple(parsed.errors),
                             blocks=getattr(self, "_last_blocks", 0), response=response or "",
                             info=dict(extra or {}), seq=getattr(ev, "seq", None))

    def create(self, arm: str, task_id: str, ex: ProExercise, split: str, max_attempts: int, **extra) -> None:  # type: ignore[override]
        extra.setdefault("benchmark", "swepro")
        extra.setdefault("repo", ex.task.repo)
        extra.setdefault("image", ex.task.image)
        extra.setdefault("base_commit", ex.task.base_commit)
        for k, v in ex.meta.items():
            extra.setdefault(k, v)
        super().create(arm, task_id, ex, split, max_attempts, **extra)

    def contract(self, tid: str, ex: ProExercise) -> LaneContract:  # type: ignore[override]
        return LaneContract(lane_id=tid, task_id=tid, allowed=ex.solution_files,
                            protected=tuple(dict.fromkeys((*DEFAULT_PROTECTED, *TEST_PATTERNS))))

    def t2_reviewer(self, arm: str, task_id: str, ex: ProExercise, fork_id: str | None = None):  # type: ignore[override]
        """T2.review for Pro: the review model sees the outline and the diff, never file bodies."""
        cfg = self.config
        if cfg.t2_review_model is None:
            return None
        model = cfg.t2_review_model
        outline = self.outline(ex)

        def review(sub: Submission, contract: LaneContract) -> GateResult:
            del contract
            diff = unified_patch(ex.context.files, sub.files)
            truncated = len(diff) > cfg.t2_diff_chars
            diff = _cap(diff, cfg.t2_diff_chars, "diff truncated")
            changed = changed_line_count(sub, ex)
            why = []
            if manifests_touched(sub):
                why.append("touches " + ", ".join(manifests_touched(sub)))
            if changed > self.t2_max_lines:
                why.append(f"more than {self.t2_max_lines} changed lines")
            user = PRO_T2_TASK.format(instance_id=ex.id, repo=ex.task.repo, files=", ".join(sorted(sub.files)) or "(none)",
                                      changed=changed, why="; ".join(why) or "requested by the gate", outline=outline,
                                      diff=diff or "(no changes)")
            c = self.call(arm=arm, role="reviewer", task_id=task_id, model=model, system=PRO_T2_SYSTEM, user=user,
                          max_tokens=cfg.t2_review_max_tokens, effort=cfg.t2_review_effort, fork_id=fork_id)
            verdict, reason = parse_review_verdict(c.text)
            evidence = {"status": "reviewed", "model": c.model or model, "reply": c.text.strip()[:500],
                        "diff_truncated": truncated}
            if verdict is None:
                return GateResult(T2_REVIEW, "block", "reviewer reply was not PASS or BLOCK: <reason>",
                                  {**evidence, "parse_error": True})
            return GateResult(T2_REVIEW, verdict, reason, evidence)

        return review

    def run_task(self, arm: str, ex, ts, split: str = "") -> bool:  # type: ignore[override]
        if arm == "B2":
            return self.run_single("B2", ex, ts, split)
        if arm == "La":
            from .lanes import run_la

            return run_la(self, ex, ts, split)
        return super().run_task(arm, ex, ts, split)

    # ---- sizes

    def prompt_sizes(self, ex: ProExercise, ts: ProTestSplit) -> dict[str, int]:
        """Estimated tokens (``swepro.estimate_tokens``) of the worker prompt (with a 250-word brief
        allowance) and of the orchestrator prompt (outline context plus the brief request)."""
        worker = self.worker_system + self.worker_prompt(ex, ts, "x" * 1800)
        lead = self.lead_system + self.lead_context(ex, ts) + self.brief_request
        return {"worker_prompt_tokens_est": estimate_tokens(worker), "lead_prompt_tokens_est": estimate_tokens(lead),
                "outline_tokens_est": estimate_tokens(self.outline(ex))}


# ------------------------------------------------------------------ gold path (validation, dry runs)


def gold_reply(ex: ProExercise) -> str:
    """The gold patch rendered as a worker reply in the SEARCH/REPLACE format."""
    return format_blocks(diff_to_blocks(ex.task.patch, ex.context.files))


def gold_files(ex: ProExercise, scope=None) -> tuple[dict[str, str], tuple[str, ...]]:
    """(changed files, errors) from pushing the gold patch through parse_edits + apply_edits,
    exactly as a worker's reply is processed."""
    parsed = parse_edits(gold_reply(ex))
    applied = apply_edits(parsed.blocks, ex.context.files, scope=scope)
    return applied.files, parsed.errors + applied.errors


# ------------------------------------------------------------------ offline fakes (dry run)


class ProFakeClient:
    """Answers offline. Workers reply with the gold patch as SEARCH/REPLACE blocks if ``solve`` is
    True for the task (a callable on the task id), else with no edits; orchestrators with a canned
    brief; reviewers with ``review``; T2 reviewers with PASS. ``exercises`` maps id -> exercise.
    Usage is synthetic (about four characters per token). ``count_tokens`` is free and offline.

    Arm La: the planner gets a by-file plan of ``lanes_k`` lanes (``lanes.by_file_plan``) and each
    lane its share of the gold patch. ``policy(task_id, model, lane)`` (``lane`` is ``(lane_id,
    k)`` for a lane request, else None) overrides ``solve`` per request: ``"gold"``, ``"none"`` or
    ``"conflict"`` (a lane's gold blocks plus one out-of-scope block that breaks another file)."""

    def __init__(self, exercises: Mapping[str, ProExercise], solve: Callable[[str], bool] = lambda i: True,
                 review: str = "ACCEPT", policy: Callable[[str, str, Any], str] | None = None, lanes_k: int = 3):
        self.exercises = exercises
        self.solve = solve
        self.review = review
        self.policy = policy
        self.lanes_k = lanes_k
        self.calls: list[dict[str, Any]] = []

    def _task_of(self, prompt: str) -> str | None:
        for line in prompt.splitlines():
            if line.startswith("Task: "):
                return line[6:].split()[0]
        return None

    _LANE_LINE = re.compile(r"^Lane: (\S+) of (\d+) \(files: (.*)\)$", re.M)

    def _worker_text(self, tid: str | None, model: str, prompt: str) -> str:
        from .lanes import by_file_plan, plan_reply

        if tid not in self.exercises:
            return "I could not find a safe change."
        ex = self.exercises[tid]
        if "## Request: lane plan" in prompt:
            return plan_reply(by_file_plan(ex, self.lanes_k))
        m = self._LANE_LINE.search(prompt)
        lane = (m.group(1), int(m.group(2))) if m else None
        action = self.policy(tid, model, lane) if self.policy else ("gold" if self.solve(tid) else "none")
        if action == "none":
            return "I could not find a safe change."
        blocks = diff_to_blocks(ex.task.patch, ex.context.files)
        if m is None:
            return "Here are the changes.\n\n" + format_blocks(blocks)
        files = {f.strip() for f in m.group(3).split(",")}
        mine = [b for b in blocks if b.path in files]
        if action == "conflict":
            other = next((b for b in blocks if b.search and b.path not in files), None)
            if other is not None:
                mine.append(replace(other, replace=other.search + "conflicting line from another lane\n",
                                    index=len(mine) + 1))
        return "Here are the changes for my lane.\n\n" + format_blocks(mine)

    def complete(self, *, model: str, system: str, messages: Sequence[Message], max_tokens: int,
                 effort: str | None = None, tags: Mapping[str, Any] | None = None) -> Completion:
        prompt = messages[-1].content
        role = (tags or {}).get("role", "")
        tid = self._task_of(prompt)
        if role == "orchestrator" and "## Request: lane plan" in prompt:
            text = self._worker_text(tid, model, prompt)
        elif role == "orchestrator":
            text = "Brief: change the functions named in the interface; keep signatures; handle empty input."
        elif role == "reviewer":
            text = "PASS" if system == PRO_T2_SYSTEM else self.review
        else:
            text = self._worker_text(tid, model, prompt)
        self.calls.append({"model": model, "role": role, "effort": effort, "tags": dict(tags or {}),
                           "prompt": prompt, "system": system, "max_tokens": max_tokens})
        tin = (len(system) + len(prompt)) // 4 + 1
        usage = {"model": model, "input_tokens": tin, "output_tokens": min(len(text) // 4 + 1, max_tokens),
                 "cache_read_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0}
        return Completion(text=text, model=model, usage=usage, request_id=f"fake-{len(self.calls)}")

    def count_tokens(self, *, model: str, system: str, messages: Sequence[Message]) -> int:
        return (len(system) + sum(len(m.content) for m in messages)) // 4 + 1


@dataclass(frozen=True)
class OracleResult:
    """A VerifierResult stand-in for dry runs: nothing is executed."""

    reward: int
    statuses: Mapping[str, str]
    returncode: int = 0
    timed_out: bool = False
    seconds: float = 0.0
    output_tail: str = ""
    error: str = ""

    def passed(self, tests):
        return [t for t in tests if self.statuses.get(t) == "PASSED"]

    def failed(self, tests):
        return [t for t in tests if self.statuses.get(t) != "PASSED"]


def gold_oracle(exercises: Mapping[str, ProExercise]) -> Callable[..., OracleResult]:
    """A verifier ``run_fn`` that executes nothing: a submission passes every test iff its files
    equal the gold patch's files (as produced by the edit path). Its output names each file that
    differs from gold in a traceback-like line, so arm La's blame (rule 2) has something to read."""
    gold = {}

    def run(image: str, tests_dir, files: Mapping[str, str]) -> OracleResult:
        ex = next(e for e in exercises.values() if e.task.image == image)
        if ex.id not in gold:
            gold[ex.id] = gold_files(ex)[0]
        ok = dict(files) == gold[ex.id]
        status = "PASSED" if ok else "FAILED"
        bad = sorted(p for p in set(gold[ex.id]) | set(files) if files.get(p) != gold[ex.id].get(p))
        tail = "".join(f'  File "/app/{p}", line 1, in <module>\nAssertionError\n' for p in bad)
        return OracleResult(1 if ok else 0, {t: status for t in ex.test_split.score}, output_tail=tail)

    return run


# ------------------------------------------------------------------ the task loop


@dataclass
class ProRunSummary:
    arms: tuple[str, ...]
    attempted: dict[str, int] = field(default_factory=dict)
    solved: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None
    spent_usd: float = 0.0
    tasks_done: list[str] = field(default_factory=list)


ExerciseProvider = Callable[[str], "ProExercise | str"]  # an exercise, or the reason it is excluded


def run_tasks(h: ProHarness, task_ids: Sequence[str], arms: Sequence[str], provider: ExerciseProvider, *,
              split: str = "", after_task: Callable[[ProExercise], None] | None = None) -> ProRunSummary:
    """Run ``arms`` on each task, tasks outermost (each image is pulled once per task).

    A (task, arm) whose kernel task already exists in the log is skipped, and a task whose arms
    are all done is not loaded at all, so an interrupted run resumes where it stopped.
    ``provider(task_id)`` returns the exercise (loading context, pulling the image if needed) or a
    string saying why the task is excluded. Stops cleanly on ``BudgetExceeded``.
    ``after_task(ex)`` runs after a task's arms (e.g. to delete its image)."""
    s = ProRunSummary(tuple(arms), {a: 0 for a in arms}, {a: 0 for a in arms}, {a: 0 for a in arms})
    existing = project_tasks(h.log.events())
    for tid in task_ids:
        todo = [a for a in arms if f"{a}:{tid}" not in existing]
        for a in arms:
            if a not in todo:
                s.skipped[a] += 1
        if not todo:
            continue
        got = provider(tid)
        if isinstance(got, str):
            s.excluded[tid] = got
            continue
        ex = got
        try:
            for arm in todo:
                s.attempted[arm] += 1
                s.solved[arm] += int(h.run_task(arm, ex, ex.test_split, split))
        except BudgetExceeded as e:
            s.stopped = str(e)
            break
        finally:
            if after_task is not None:
                after_task(ex)
        s.tasks_done.append(tid)
    s.spent_usd = h.budget.spent(h.log)
    return s


# ------------------------------------------------------------------ probe pre-flight


@dataclass(frozen=True)
class PreflightRow:
    task_id: str
    model: str
    samples: int
    input_tokens: int
    expected_output_tokens: int
    max_tokens: int
    expected_usd: float
    worst_usd: float


# Thinking allowance on top of the answer, calibrated on the Polyglot pilot (feasibility study §3).
THINKING_ALLOWANCE = {HAIKU: 2000, OPUS: 800, SONNET: 1500}


def expected_answer_tokens(ex: ProExercise) -> int:
    """Expected answer tokens: 1.3 x the gold patch as SEARCH/REPLACE text at 3.3 chars per token
    (the feasibility study's estimate). The gold patch sizes the estimate only; no model sees it."""
    try:
        chars = len(gold_reply(ex))
    except (ValueError, KeyError):
        chars = len(ex.task.patch)
    return math.ceil(1.3 * chars / 3.3)


def preflight(h: ProHarness, exercises: Sequence[ProExercise], plan: Sequence[tuple[str, int]],
              count_tokens: Callable[..., int], table: str = DEFAULT_TABLE) -> list[PreflightRow]:
    """Exact prompt tokens (``count_tokens``, the free endpoint) for each (model, samples) in
    ``plan`` on each exercise, priced at expected and worst-case output.

    Expected output = :func:`expected_answer_tokens` + ``THINKING_ALLOWANCE[model]``, capped at
    the model's ``max_tokens``; worst case = ``max_tokens``. The prompts are exactly the single-arm
    requests (system + worker prompt, no brief)."""
    prices = PRICE_TABLES[table]
    rows = []
    for ex in exercises:
        prompt = h.worker_prompt(ex, ex.test_split)
        answer = expected_answer_tokens(ex)
        for model, samples in plan:
            n_in = int(count_tokens(model=model, system=h.worker_system, messages=[Message("user", prompt)]))
            mt = h.worker_max_tokens(model)
            out = min(answer + THINKING_ALLOWANCE.get(model, 1500), mt)
            inp, outp = prices[model].rates(n_in)
            exp = samples * (n_in * inp + out * outp) / PER_MTOK
            worst = samples * (n_in * inp + mt * outp) / PER_MTOK
            rows.append(PreflightRow(ex.id, model, samples, n_in, out, mt, exp, worst))
    return rows


# ------------------------------------------------------------------ main-study pre-flight


@dataclass(frozen=True)
class MainPreflightRow:
    task_id: str
    a_input: int  # counted: Opus worker prompt (no brief)
    d_lead_input: int  # counted: Opus orchestrator brief request (outline only)
    d_worker_input: int  # counted: Haiku worker prompt with a brief-sized placeholder
    answer_tokens: int
    expected_a: float
    expected_d_shipped: float  # brief + Haiku attempt + p_fork x Opus treatment
    expected_d_measurement: float  # p_fork x Haiku control (fork overhead, not D's cost)
    worst: float


def main_preflight(h: ProHarness, exercises: Sequence[ProExercise], count_tokens: Callable[..., int], *,
                   p_fork: float = 0.5, repair_rate: float = 0.3, brief_tokens: int = 1500,
                   table: str = DEFAULT_TABLE) -> list[MainPreflightRow]:
    """Expected and worst-case cost per task of arm A and of arm D's fixed cascade, from exact
    prompt counts (``count_tokens``, free). Assumptions (stated so they can be checked afterwards):
    D's gate blocks with probability ``p_fork`` (then a Haiku control and an Opus treatment run on
    the retry prompt, about the worker prompt plus the answer); each worker request triggers a
    repair turn with probability ``repair_rate`` (the whole conversation again as input plus 1,000
    output tokens); the brief costs ``brief_tokens`` of output; T2 review is not priced (rare)."""
    prices = PRICE_TABLES[table]

    def usd(model: str, n_in: int, n_out: int) -> float:
        inp, outp = prices[model].rates(n_in)
        return (n_in * inp + n_out * outp) / PER_MTOK

    def worker(model: str, n_in: int, answer: int) -> tuple[float, float]:
        mt = h.worker_max_tokens(model)
        out = min(answer + THINKING_ALLOWANCE.get(model, 1500), mt)
        exp = usd(model, n_in, out) + repair_rate * usd(model, n_in + out, 1000)
        worst = usd(model, n_in, mt) + usd(model, n_in + mt, mt)
        return exp, worst

    rows = []
    for ex in exercises:
        ts = ex.test_split
        answer = expected_answer_tokens(ex)
        a_in = int(count_tokens(model=OPUS, system=h.worker_system, messages=[Message("user", h.worker_prompt(ex, ts))]))
        lead = h.lead_context(ex, ts) + h.brief_request
        l_in = int(count_tokens(model=OPUS, system=h.lead_system, messages=[Message("user", lead)]))
        d_prompt = h.worker_prompt(ex, ts, "word " * 250)
        d_in = int(count_tokens(model=HAIKU, system=h.worker_system, messages=[Message("user", d_prompt)]))
        retry_in = d_in + answer + 500
        a_exp, a_worst = worker(OPUS, a_in, answer)
        h_exp, h_worst = worker(HAIKU, d_in, answer)
        t_exp, t_worst = worker(OPUS, retry_in, answer)
        c_exp, c_worst = worker(HAIKU, retry_in, answer)
        brief = usd(OPUS, l_in, brief_tokens)
        brief_worst = usd(OPUS, l_in, h.config.orchestrator_max_tokens)
        rows.append(MainPreflightRow(
            ex.id, a_in, l_in, d_in, answer, a_exp, brief + h_exp + p_fork * t_exp, p_fork * c_exp,
            a_worst + brief_worst + h_worst + t_worst + c_worst))
    return rows
