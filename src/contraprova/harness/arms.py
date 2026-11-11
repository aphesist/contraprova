"""The four API arms of the experiment, built against ``contraprova.interfaces`` only.

A  single strong model (Opus 5.5, low effort), one attempt, no gates
B  single Haiku 5.5, one attempt, no gates
C  ungoverned swarm: the orchestrator writes a brief, a Haiku worker edits, the
   orchestrator reviews the result by reading it and may re-dispatch once
D  governed swarm: a lane contract, injected gates, the router picks the
   worker's tier; on the first gate failure the log is forked into two Haiku
   controls and one Sonnet treatment, re-executed from the same point

Every model call goes through the injected ``LLMClient`` and every step is an
event in the ``EventLog``. The harness, not the client, writes
``usage.recorded``: clients must not append to the log themselves, so a
``RecordingClient`` or a client with an ``on_usage`` callback is refused
(it would record every request twice).

Payloads follow docs/events.md. Kernel task ids are ``"<arm>:<exercise id>"``
(fork children add ``":control-1"``, ``":control-2"`` or ``":treatment"``).
``outcome.scored`` uses the bare exercise id as ``task_id`` (the pairing key)
and names the kernel task in ``log_task_id``.

Arm D's forks are created with :func:`contraprova.forks.fork`, so ``fork_id``,
``seed`` and ``prefix_hash`` are exactly the library's. Each fork is scored
with a **fork outcome** (``fork_id``, never counted toward an arm) and its
usage carries the ``fork_id``. Arm D's own shipped result stays an arm outcome
(``arm`` "D", ``log_task_id`` the parent task, ``branch_task_id`` the branch
it shipped from). The re-execution prompt is the harness's own retry prompt
(the first prompt plus the rejected files and the gate's reason), not
``forks.conversation_prefix``: the harness sends single-turn requests and does
not store its prompts in the log, so a multi-turn prefix rebuilt from the log
would not be the request the worker actually saw.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..contracts import DEFAULT_PROTECTED, LaneContract, check_scope
from ..forks import fork as create_fork
from ..gates import changed_lines
from ..interfaces import HAIKU, OPUS, SONNET, Completion, GateResult, LLMClient, Message, Router, Submission
from ..llm import RecordingClient
from ..log import EventLog
from ..pricing import DEFAULT_TABLE, PER_MTOK, PRICE_TABLES, request_cost
from ..projections import project_tasks, usage_records
from ..router import featurize
from .polyglot import Exercise, TestSplit, materialize_tests, sha256_json, sha256_text

ARMS: tuple[str, ...] = ("A", "B", "C", "D")
# (branch, role, model). The two controls differ only in seed; their disagreement is the noise floor.
FORK_BRANCHES: tuple[tuple[str, str, str], ...] = (
    ("control-1", "control", HAIKU),
    ("control-2", "control", HAIKU),
    ("treatment", "treatment", SONNET),
)

# ------------------------------------------------------------------ prompts

FILE_FORMAT = """Return every file you change in full, each in this exact form:

<<<FILE path/relative/to/exercise>>>
...complete file content...
<<<END>>>

Only the file blocks are read; text outside them is ignored. Never return test files."""

WORKER_SYSTEM = (
    "You are a careful programmer solving one small exercise. Edit only the solution files you are given. "
    "Keep the existing public names and signatures. Do not use network access or extra dependencies.\n\n"
    + FILE_FORMAT
)

# Prompt caching: every system prompt is a constant, so it is byte-identical across tasks, but each
# is far below the 512-token minimum cacheable prefix of Opus 5.5, Sonnet 5.5 and Haiku 5.5 and is
# never cached on its own. What can be cached is a long exercise prefix that a later request with
# the same model repeats exactly, so the request-specific part always comes last: the planner's
# brief after the tests, a retry's feedback after the first prompt, and the lead's request after
# the exercise context. See ``Harness.call``'s ``cache_upto`` and docs/design.md.

WORKER_TASK = """Exercise: {exercise_id} ({language})

## Instructions
{instructions}

## Solution files (current content)
{stubs}

## Tests you can see
{tests}
{brief}"""

WORKER_RETRY = """Your previous attempt was rejected.

## Your previous files
{previous}

## Why it was rejected
{feedback}

Fix the problem and return the complete corrected files."""

# The orchestrator's brief and the arm C review are both Opus requests about the same exercise, so
# they share one system prompt and one exercise context and differ only in the request at the end.
LEAD_SYSTEM = (
    "You lead a junior programmer on one small exercise. You cannot edit files or run code. "
    "The message gives the exercise first and your request last; do exactly what the request asks."
)

LEAD_CONTEXT = """Exercise: {exercise_id} ({language})

## Instructions
{instructions}

## Solution files (starting content)
{stubs}

## Visible tests
{tests}
"""

BRIEF_REQUEST = """
## Request: brief
Write a short brief for the junior programmer (at most 150 words): the approach, edge cases the tests check, \
and pitfalls. No code longer than one line."""

REVIEW_REQUEST = """
## Request: review
Review the submitted solution below by reading it; you cannot run it. Answer with ACCEPT on the first line, \
or REDISPATCH on the first line followed by at most 100 words of concrete feedback.

## Submitted files
{files}"""

# ------------------------------------------------------------------ file format

_OPEN = re.compile(r"^[ \t]*<<<[ \t]*FILE[:\s]+(.+?)[ \t]*>>>[ \t]*$")
_CLOSE = re.compile(r"^[ \t]*<<<[ \t]*END(?:[ \t]+FILE)?[ \t]*>>>[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


@dataclass(frozen=True)
class ParsedFiles:
    files: dict[str, str]
    errors: tuple[str, ...] = ()


def format_files(files: Mapping[str, str]) -> str:
    """Render files in the worker format (also used to show files inside prompts)."""
    parts = []
    for path in sorted(files):
        body = files[path]
        if body and not body.endswith("\n"):
            body += "\n"
        parts.append(f"<<<FILE {path}>>>\n{body}<<<END>>>")
    return "\n\n".join(parts)


def _clean_path(raw: str) -> str | None:
    p = raw.strip().strip("`'\"").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or ".." in p.split("/") or re.match(r"^[A-Za-z]:", p):
        return None
    return p


def parse_files(text: str) -> ParsedFiles:
    """Parse ``<<<FILE path>>> ... <<<END>>>`` blocks.

    Tolerates prose around blocks, CRLF line endings, ``FILE:`` with a colon, quoted
    paths, and a Markdown code fence wrapped *inside* a block (stripped). A later block
    for the same path replaces an earlier one. Unsafe paths (absolute, ``..``) and an
    unterminated final block are reported in ``errors`` and dropped: a truncated file
    is never returned as if it were complete.
    """
    files: dict[str, str] = {}
    errors: list[str] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = _OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        raw = m.group(1)
        body: list[str] = []
        j = i + 1
        while j < len(lines) and not _CLOSE.match(lines[j]) and not _OPEN.match(lines[j]):
            body.append(lines[j])
            j += 1
        if j >= len(lines) or _OPEN.match(lines[j]):
            errors.append(f"unterminated block for {raw.strip()!r}")
            i = j
            continue
        path = _clean_path(raw)
        if path is None:
            errors.append(f"unsafe path {raw.strip()!r}")
        else:
            if body and _FENCE.match(body[0]) and len(body) >= 2 and _FENCE.match(body[-1]) and body[-1].strip() in ("```", "~~~"):
                body = body[1:-1]
            content = "\n".join(body)
            files[path] = content + "\n" if content else ""
        i = j + 1
    return ParsedFiles(files, tuple(errors))


# ------------------------------------------------------------------ injected parts


@dataclass(frozen=True)
class ScoreResult:
    solved: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskContext:
    """What gates and scorers get to see about one attempt."""

    arm: str
    task_id: str
    exercise: Exercise
    test_split: TestSplit
    model: str
    attempt: int = 1
    contract: LaneContract | None = None
    split: str = ""

    def test_files(self, subset: str) -> dict[str, str]:
        return materialize_tests(self.exercise, self.test_split, subset)


GateFn = Callable[[Submission, TaskContext], Sequence[GateResult]]
ScoreFn = Callable[[Submission, TaskContext], "ScoreResult | bool"]


class BudgetExceeded(RuntimeError):
    """The next request could push recorded spend past the cap."""


@dataclass
class BudgetGuard:
    """Cumulative kill switch over everything recorded in the log.

    Before each request: recorded spend (re-priced from ``usage.recorded``) plus a
    worst-case bound for the request (prompt at ``chars_per_token`` and the full
    ``max_tokens`` of output) must stay within ``cap_usd``.
    """

    cap_usd: float | None
    table: str = DEFAULT_TABLE
    chars_per_token: float = 2.0

    def spent(self, log: EventLog) -> float:
        prices = PRICE_TABLES[self.table]
        return sum(request_cost(u, prices) for u in usage_records(log.events()))

    def worst_case(self, model: str, prompt_chars: int, max_tokens: int) -> float:
        p = PRICE_TABLES[self.table][model]
        tokens_in = int(prompt_chars / self.chars_per_token) + 1
        inp, out = p.rates(tokens_in)
        return (tokens_in * inp + max_tokens * out) / PER_MTOK

    def check(self, log: EventLog, model: str, prompt_chars: int, max_tokens: int) -> None:
        if self.cap_usd is None:
            return
        spent = self.spent(log)
        bound = self.worst_case(model, prompt_chars, max_tokens)
        if spent + bound > self.cap_usd:
            raise BudgetExceeded(f"spent ${spent:.4f} + next request up to ${bound:.4f} > cap ${self.cap_usd:.2f}")


@dataclass(frozen=True)
class ArmConfig:
    strong_model: str = OPUS
    strong_effort: str | None = "low"
    cheap_model: str = HAIKU
    worker_effort: str | None = "medium"  # Haiku and Sonnet workers (arm A's single model uses strong_effort)
    worker_max_tokens: int = 8000
    orchestrator_max_tokens: int = 1000
    feedback_chars: int = 2000


# ------------------------------------------------------------------ harness


def self_recording(client: Any) -> str | None:
    """Describe why ``client`` would write usage itself, or None if it does not.

    Looks through role dispatchers (anything with a ``clients`` mapping).
    """
    if isinstance(client, RecordingClient):
        return "a RecordingClient"
    if getattr(client, "on_usage", None) is not None:
        return f"{type(client).__name__} with an on_usage callback"
    for name, inner in sorted((getattr(client, "clients", None) or {}).items()):
        why = self_recording(inner)
        if why:
            return f"{why} (role {name!r})"
    return None


def _gate_dict(r: GateResult) -> dict:
    return {"gate": r.gate, "verdict": r.verdict, "reason": r.reason, "evidence": dict(r.evidence)}


# ------------------------------------------------------------------ route features

# Substrings (lower-cased) of a blocking gate's reason or test output that mean the code did not build/load.
COMPILE_MARKERS: tuple[str, ...] = (
    "syntaxerror", "indentationerror", "importerror", "modulenotfounderror", "compile error", "compilation",
    "could not compile", "cannot find symbol", "error[e", "undefined:", "build failed", "unexpected token",
)


def _test_counts(r: GateResult) -> Mapping[str, int]:
    tests = r.evidence.get("tests") if isinstance(r.evidence, Mapping) else None
    counts = tests.get("counts") if isinstance(tests, Mapping) else None
    return counts if isinstance(counts, Mapping) else {}


def _failures(counts: Mapping[str, int]) -> int:
    return sum(int(counts.get(k, 0) or 0) for k in ("failed", "failures", "errors"))


def error_class(results: Sequence[GateResult]) -> str:
    """``router.ERROR_CLASSES`` label for a gate run, from its first blocking result.

    "none" if nothing blocked; "timeout" if the test run timed out; "compile" if the
    reason or test output shows the code did not build or load (``COMPILE_MARKERS``);
    "assert" if a test gate counted failing tests (or the text mentions an assertion);
    "other" for everything else (scope violations, review, an unrecognised failure).
    """
    blocking = next((r for r in results if r.verdict != "pass"), None)
    if blocking is None:
        return "none"
    tests = blocking.evidence.get("tests") if isinstance(blocking.evidence, Mapping) else None
    tests = tests if isinstance(tests, Mapping) else {}
    text = f"{blocking.reason}\n{tests.get('output', '')}".lower()
    if tests.get("timed_out") or "timed out" in text:
        return "timeout"
    if any(m in text for m in COMPILE_MARKERS):
        return "compile"
    if _failures(_test_counts(blocking)) > 0 or "assert" in text:
        return "assert"
    return "other"


def route_record(ex: Exercise, ts: TestSplit, *, attempt: int = 1, submission: Submission | None = None,
                 results: Sequence[GateResult] = (), contract: LaneContract | None = None) -> dict[str, Any]:
    """The ``router.featurize`` input record for a routing decision on ``ex``.

    Before any attempt (``submission`` None, no ``results``) only the task is known:
    language, stub size, number of visible tests, attempt 1, no failures. After a gate
    failure the blocked attempt adds: visible failures (from the visible-test gate's
    counts, when it ran and reported them; the test total is taken from those counts
    too, else the split's visible tests), ``error_class`` (see :func:`error_class`),
    ``diff_lines`` (lines changed against the stubs), and ``tamper`` (the attempt wrote a
    protected path, by the contract or by a gate's scope violations).
    ``control_agree`` is left unknown: no control has run when the router decides.
    """
    stubs = ex.stubs()
    rec: dict[str, Any] = {
        "language": ex.language,
        "stub_lines": sum(len(s.splitlines()) for s in stubs.values()),
        "visible_tests": len(ts.visible),
        "visible_failures": 0,
        "error_class": error_class(results),
        "diff_lines": 0,
        "tamper": False,
        "control_agree": None,
        "attempt": attempt,
    }
    for r in results:
        counts = _test_counts(r)
        if "visible" in r.gate and counts:
            fails = _failures(counts)
            total = int(counts.get("ran", 0) or 0) or (int(counts.get("passed", 0) or 0) + fails)
            rec["visible_failures"] = fails
            if total:
                rec["visible_tests"] = total
    if submission is not None:
        rec["diff_lines"] = changed_lines(submission, stubs)
        if contract is not None:
            rec["tamper"] = any(v.reason == "protected" for v in check_scope(contract, sorted(submission.files)))
    for r in results:
        violations = r.evidence.get("violations") if isinstance(r.evidence, Mapping) else None
        if any(isinstance(v, Mapping) and v.get("reason") in ("protected", "test_tampered") for v in violations or ()):
            rec["tamper"] = True
    return rec


@dataclass
class Harness:
    log: EventLog
    client: LLMClient
    scorer: ScoreFn
    gates: GateFn | None = None
    router: Router | None = None
    budget: BudgetGuard = field(default_factory=lambda: BudgetGuard(None))
    config: ArmConfig = field(default_factory=ArmConfig)
    partition_sha256: str = ""

    def __post_init__(self) -> None:
        culprit = self_recording(self.client)
        if culprit:
            raise ValueError(f"the harness records usage itself; {culprit} would record every request twice")

    # ---- events

    def emit(self, type_: str, actor: str, payload: Mapping[str, Any]):
        return self.log.append(type_, actor, payload)

    def call(self, *, arm: str, role: str, task_id: str, model: str, system: str, user: str,
             max_tokens: int, effort: str | None = None, fork_id: str | None = None,
             cache_upto: int = 0) -> Completion:
        """One single-turn request. ``cache_upto`` marks the first ``cache_upto`` characters of
        ``user`` as a prefix that a later request with the same model and system repeats exactly;
        set it only where such a request is planned, since a cache write costs a premium."""
        self.budget.check(self.log, model, len(system) + len(user), max_tokens)
        tags = {"arm": arm, "role": role, "task_id": task_id}
        if fork_id is not None:
            tags["fork_id"] = fork_id
        c = self.client.complete(model=model, system=system, messages=[Message("user", user, cache_upto)],
                                 max_tokens=max_tokens, effort=effort, tags=tags)
        usage = dict(c.usage)
        usage["model"] = usage.get("model") or c.model or model
        usage.update(tags)
        if c.request_id:
            usage.setdefault("request_id", c.request_id)
        self.emit("usage.recorded", f"{role}:{usage['model']}", usage)
        return c

    def create(self, arm: str, task_id: str, ex: Exercise, split: str, max_attempts: int, **extra) -> None:
        self.emit("task.created", "harness", {
            "task_id": task_id, "title": ex.id, "kind": "code", "scope": list(ex.solution_files),
            "max_attempts": max_attempts, "arm": arm, "exercise_id": ex.id, "language": ex.language,
            "split": split, "partition_sha256": self.partition_sha256, **extra,
        })

    def submit(self, task_id: str, attempt: int, model: str, parsed: ParsedFiles, ex: Exercise) -> Submission:
        files = {p: c for p, c in parsed.files.items()}
        self.emit("task.submitted", f"worker:{model}", {
            "task_id": task_id, "attempt": attempt, "model": model, "files": sorted(files),
            "contents": files, "files_sha256": sha256_json(files), "parse_errors": list(parsed.errors),
        })
        return Submission(task_id=task_id, files=files, attempt=attempt)

    def evaluate(self, sub: Submission, ctx: TaskContext) -> ScoreResult:
        r = self.scorer(sub, ctx)
        return ScoreResult(r) if isinstance(r, bool) else r

    def outcome(self, arm: str, sub: Submission, ctx: TaskContext, r: ScoreResult, shipped: bool = True, **extra) -> bool:
        """Append ``outcome.scored``. ``shipped=False`` (a gate kept the change out) means unsolved."""
        solved = bool(r.solved) and shipped
        self.emit("outcome.scored", "scorer", {
            "arm": arm, "task_id": ctx.exercise.id, "solved": solved, "shipped": shipped, "log_task_id": sub.task_id,
            "split": ctx.split, "model": ctx.model, "evidence": dict(r.evidence), **extra,
        })
        return solved

    def fork_outcome(self, fork_id: str, sub: Submission, ctx: TaskContext, r: ScoreResult, **extra) -> bool:
        """Append a fork outcome: keyed by ``fork_id``, never counted toward an arm's solve rate."""
        self.emit("outcome.scored", "scorer", {
            "fork_id": fork_id, "task_id": ctx.exercise.id, "solved": bool(r.solved), "log_task_id": sub.task_id,
            "split": ctx.split, "model": ctx.model, "evidence": dict(r.evidence), **extra,
        })
        return bool(r.solved)

    def score(self, arm: str, sub: Submission, ctx: TaskContext, **extra) -> bool:
        return self.outcome(arm, sub, ctx, self.evaluate(sub, ctx), **extra)

    # ---- prompts

    def worker_prompt(self, ex: Exercise, ts: TestSplit, brief: str = "") -> str:
        b = f"\n## Brief from the planner\n{brief.strip()}\n" if brief.strip() else ""
        return WORKER_TASK.format(exercise_id=ex.id, language=ex.language, instructions=ex.instructions(),
                                  brief=b, stubs=format_files(ex.stubs()),
                                  tests=format_files(materialize_tests(ex, ts, "visible")) or "(none)")

    def retry_prompt(self, base: str, previous: Mapping[str, str], feedback: str) -> str:
        fb = feedback.strip()[: self.config.feedback_chars] or "(no details)"
        return base + "\n" + WORKER_RETRY.format(previous=format_files(previous) or "(no files parsed)", feedback=fb)

    def _work(self, arm: str, task_id: str, ex: Exercise, model: str, prompt: str, attempt: int,
              effort: str | None = None, role: str = "worker", fork_id: str | None = None,
              cache_upto: int = 0) -> Submission:
        self.emit("task.claimed", f"worker:{model}", {"task_id": task_id, "worker": model, "attempt": attempt})
        if effort is None:
            effort = self.config.worker_effort
        c = self.call(arm=arm, role=role, task_id=task_id, model=model, system=WORKER_SYSTEM, user=prompt,
                      max_tokens=self.config.worker_max_tokens, effort=effort, fork_id=fork_id,
                      cache_upto=cache_upto)
        return self.submit(task_id, attempt, model, parse_files(c.text), ex)

    def lead_context(self, ex: Exercise, ts: TestSplit) -> str:
        """The exercise part shared by the orchestrator's brief and arm C's review (same bytes)."""
        return LEAD_CONTEXT.format(exercise_id=ex.id, language=ex.language, instructions=ex.instructions(),
                                   stubs=format_files(ex.stubs()),
                                   tests=format_files(materialize_tests(ex, ts, "visible")) or "(none)")

    def _brief(self, arm: str, task_id: str, ex: Exercise, ts: TestSplit, cache_context: bool = False) -> str:
        context = self.lead_context(ex, ts)
        c = self.call(arm=arm, role="orchestrator", task_id=task_id, model=self.config.strong_model,
                      system=LEAD_SYSTEM, user=context + BRIEF_REQUEST, max_tokens=self.config.orchestrator_max_tokens,
                      effort=self.config.strong_effort, cache_upto=len(context) if cache_context else 0)
        return c.text

    def _accept(self, task_id: str, attempt: int, gate: str, reason: str) -> None:
        self.emit("gate.decided", "harness", {"task_id": task_id, "attempt": attempt, "verdict": "pass",
                                              "gate": gate, "reason": reason, "results": []})

    # ---- arms

    def run_single(self, arm: str, ex: Exercise, ts: TestSplit, split: str) -> bool:
        """Arms A and B."""
        cfg = self.config
        model, effort = (cfg.strong_model, cfg.strong_effort) if arm == "A" else (cfg.cheap_model, None)
        tid = f"{arm}:{ex.id}"
        self.create(arm, tid, ex, split, max_attempts=1)
        sub = self._work(arm, tid, ex, model, self.worker_prompt(ex, ts), 1, effort)
        self._accept(tid, 1, "accept.ungated", f"arm {arm} has no gates")
        return self.score(arm, sub, TaskContext(arm, tid, ex, ts, model, 1, split=split))

    def run_c(self, ex: Exercise, ts: TestSplit, split: str) -> bool:
        arm, cfg = "C", self.config
        tid = f"C:{ex.id}"
        self.create(arm, tid, ex, split, max_attempts=2)
        # The review repeats the brief's system prompt and exercise context with the same model, so
        # the brief writes that prefix to the cache and the review reads it.
        brief = self._brief(arm, tid, ex, ts, cache_context=True)
        base = self.worker_prompt(ex, ts, brief)
        sub = self._work(arm, tid, ex, cfg.cheap_model, base, 1)
        context = self.lead_context(ex, ts)
        review = context + REVIEW_REQUEST.format(files=format_files(sub.files) or "(no files parsed)")
        c = self.call(arm=arm, role="reviewer", task_id=tid, model=cfg.strong_model, system=LEAD_SYSTEM,
                      user=review, max_tokens=cfg.orchestrator_max_tokens, effort=cfg.strong_effort,
                      cache_upto=len(context))
        first, _, rest = c.text.strip().partition("\n")
        redispatch = first.strip().upper().startswith("REDISPATCH")
        self.emit("gate.decided", f"reviewer:{cfg.strong_model}", {
            "task_id": tid, "attempt": 1, "verdict": "block" if redispatch else "pass", "gate": "C.review",
            "reason": (rest.strip() or first.strip())[: cfg.feedback_chars], "results": [],
        })
        if redispatch:
            sub = self._work(arm, tid, ex, cfg.cheap_model, self.retry_prompt(base, sub.files, rest or first), 2)
            self._accept(tid, 2, "C.accept_after_redispatch", "one re-dispatch allowed; no further review")
        return self.score(arm, sub, TaskContext(arm, tid, ex, ts, cfg.cheap_model, sub.attempt, split=split))

    def _gate(self, sub: Submission, ctx: TaskContext) -> tuple[bool, list[GateResult]]:
        assert self.gates is not None
        results = list(self.gates(sub, ctx))
        passed = bool(results) and all(r.verdict == "pass" for r in results)
        reason = "; ".join(f"[{r.gate}] {r.reason}" for r in results if r.verdict != "pass")
        self.emit("gate.decided", "gate", {
            "task_id": ctx.task_id, "attempt": ctx.attempt, "verdict": "pass" if passed else "block",
            "gate": "+".join(r.gate for r in results) or "none", "tier": ctx.model,
            "reason": reason[: self.config.feedback_chars] if reason else ("all gates passed" if passed else "no gates ran"),
            "results": [_gate_dict(r) for r in results],
        })
        return passed, results

    def _route(self, arm: str, task_id: str, decision: str, features: Mapping[str, float]) -> str:
        assert self.router is not None
        seed = int(sha256_text(f"{task_id}:{decision}")[:8], 16)
        d = self.router.decide(features, rng_seed=seed)
        self.emit("route.decided", "router", {
            "task_id": task_id, "arm": arm, "decision": decision, "action": d.action, "propensities": dict(d.propensities),
            "router_hash": d.router_hash, "features": dict(features), "rng_seed": seed,
        })
        return d.action

    @staticmethod
    def features(ex: Exercise, ts: TestSplit, *, attempt: int = 1, submission: Submission | None = None,
                 results: Sequence[GateResult] = (), contract: LaneContract | None = None) -> dict[str, float]:
        """Router features: ``router.featurize`` of :func:`route_record` (keys = ``router.FEATURE_NAMES``)."""
        return featurize(route_record(ex, ts, attempt=attempt, submission=submission, results=results,
                                      contract=contract))

    def contract(self, tid: str, ex: Exercise) -> LaneContract:
        protected = tuple(DEFAULT_PROTECTED) + tuple(ex.test_files) + tuple(ex.extra_test_files)
        return LaneContract(lane_id=tid, task_id=tid, allowed=tuple(ex.solution_files), protected=protected)

    def run_d(self, ex: Exercise, ts: TestSplit, split: str) -> bool:
        if self.gates is None or self.router is None:
            raise ValueError("arm D needs gates and a router")
        arm = "D"
        tid = f"D:{ex.id}"
        contract = self.contract(tid, ex)
        cdict = {"allowed": list(contract.allowed), "protected": list(contract.protected)}
        self.create(arm, tid, ex, split, max_attempts=1, contract=cdict)
        brief = self._brief(arm, tid, ex, ts)
        base = self.worker_prompt(ex, ts, brief)
        # Initial decision: only what is known before the attempt (attempt 1, no failures yet).
        tier = self._route(arm, tid, "initial", self.features(ex, ts))
        sub = self._work(arm, tid, ex, tier, base, 1)
        ctx = TaskContext(arm, tid, ex, ts, tier, 1, contract, split)
        passed, results = self._gate(sub, ctx)
        if passed:
            return self.score(arm, sub, ctx, gate_passed=True, branch="initial")

        # First gate failure: fork from the blocking gate.decided and re-execute both ways.
        fork_point = self.log.events()[-1].seq
        feedback = "\n".join(f"[{r.gate}] {r.reason}" for r in results if r.verdict != "pass")
        retry = self.retry_prompt(base, sub.files, feedback)
        # After the gate failure: the gate's evidence about the blocked attempt.
        action = self._route(arm, tid, "after_gate_failure",
                             self.features(ex, ts, attempt=2, submission=sub, results=results, contract=contract))
        chosen = "control-1" if action == HAIKU else "treatment"
        chosen_result = None
        for branch, role, model in FORK_BRANCHES:
            child = f"{tid}:{branch}"
            seed = int(sha256_text(f"{tid}:{fork_point}:{branch}")[:8], 16)
            fid = create_fork(self.log, tid, fork_point, model, role, seed, actor="harness",
                              extra={"fork_task_id": child, "branch": branch, "arm": arm})
            self.create(arm, child, ex, split, max_attempts=1, contract=cdict, fork_of=tid, fork_id=fid,
                        branch=branch)
            # Branches that share a model send byte-identical requests (the two Haiku controls):
            # the first writes the whole prompt to the cache and the next reads it.
            shared = sum(1 for _, _, m in FORK_BRANCHES if m == model) > 1
            csub = self._work(arm, child, ex, model, retry, 1, fork_id=fid, cache_upto=len(retry) if shared else 0)
            cctx = TaskContext(arm, child, ex, ts, model, 1, contract, split)
            cpassed, _ = self._gate(csub, cctx)
            r = self.evaluate(csub, cctx)
            self.fork_outcome(fid, csub, cctx, r, arm=arm, gate_passed=cpassed, branch=branch)
            if branch == chosen:
                chosen_result = (csub, cctx, cpassed, r)
        assert chosen_result is not None
        csub, cctx, cpassed, r = chosen_result
        return self.outcome(arm, csub, cctx, r, shipped=cpassed, gate_passed=cpassed, branch=chosen,
                            router_action=action, log_task_id=tid, branch_task_id=csub.task_id)

    def run_task(self, arm: str, ex: Exercise, ts: TestSplit, split: str = "") -> bool:
        if arm in ("A", "B"):
            return self.run_single(arm, ex, ts, split)
        if arm == "C":
            return self.run_c(ex, ts, split)
        if arm == "D":
            return self.run_d(ex, ts, split)
        raise ValueError(f"unknown arm {arm!r}")


@dataclass(frozen=True)
class RunSummary:
    arm: str
    attempted: int
    solved: int
    skipped: int
    stopped: str | None  # reason the run stopped early, if it did
    spent_usd: float


def run_arm(h: Harness, arm: str, exercises: Sequence[Exercise], test_splits: Mapping[str, TestSplit],
            split: str = "") -> RunSummary:
    """Run ``arm`` over ``exercises`` in order, skipping any whose task already exists in
    the log (so an interrupted run can resume). Stops cleanly on ``BudgetExceeded``."""
    existing = project_tasks(h.log.events())
    attempted = solved = skipped = 0
    stopped = None
    for ex in exercises:
        if f"{arm}:{ex.id}" in existing:
            skipped += 1
            continue
        try:
            attempted += 1
            solved += int(h.run_task(arm, ex, test_splits[ex.id], split))
        except BudgetExceeded as e:
            stopped = str(e)
            break
    return RunSummary(arm, attempted, solved, skipped, stopped, h.budget.spent(h.log))
