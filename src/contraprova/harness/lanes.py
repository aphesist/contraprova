"""Contract Lanes (arm La) on SWE-bench Pro: planner, interface board, lanes, merges, blame, ladder.

The pipeline (docs/lanes-plan.md section 2.1; the owner's design where it differs, noted below):

1. **Plan.** One Opus 5.5 request at low effort (fixed, pre-registered) sees the orchestrator's
   context (``ProHarness.lead_context``: statement, requirements, interface, outline, visible test
   names; never a file body) plus the list of files to assign, and returns one JSON lane plan: 1 to
   ``max_lanes`` lanes (cap 4), each with its files (a partition of the task's files), goal,
   symbols, the interfaces it provides and uses, and its visible tests; and the cross-lane
   ``interfaces`` with frozen signatures. :func:`parse_plan` validates it strictly (every file in
   exactly one lane, no test file, 1 <= k <= cap, each lane prompt within the token bound). An
   invalid plan gets one repair turn; if it is still invalid the task runs as one lane
   (``fallback: single_lane``, logged, the task stays in the analysis).
2. **Freeze (the interface round).** The kernel writes the board in one step: each lane's
   provided and needed signatures and the frozen interfaces (the planner's, plus every function or
   class named in the task's ``interface`` field, source ``task_interface``) go into one
   ``interface.frozen`` event and are immutable for the task. There is no model call in this round.
   A lane that needs a change writes ``INTERFACE REQUEST: <symbol>: <signature>``; the kernel logs
   ``interface.requested`` with status ``rejected`` and counts a clash.
3. **Lanes.** One request per lane: Haiku 5.5, effort medium, ``max_tokens`` 32K, one edit-repair
   turn on a parse, apply or truncation failure (as in the main study). A lane sees the task text,
   the frozen interfaces, its own contract, its own files in full, the outline only of the other
   files, and its visible tests. Its SEARCH/REPLACE blocks are applied with a scope checker built
   from its files: blocks on other lanes' files or test files are rejected before anything applies.
4. **Governed merge.** The lanes' file sets are unioned (disjoint by construction; any overlap is
   detected and counted, the first lane in plan order wins). Gates on the merged patch, one
   ``gate.decided`` on the parent task: ``T0.scope``, ``T0.edits``, ``T0.interfaces`` (new,
   deterministic: every checked frozen symbol is still defined, and for Python the frozen parameter
   names are still accepted), ``T0.visible_tests``, ``T1.private_tests`` and ``T2.review`` above 500
   changed lines.
5. **Blame.** A block is assigned to lanes: rule 1, direct (the lanes with edit errors, the owners
   of a broken interface, or the lanes the plan assigned a failing test); rule 2, failing test
   files and verifier tracebacks that name a lane's file; rule 3, every lane. A blamed lane sees
   only what D's retry sees: gate reasons with visible test names, or a count of gate-private
   failures.
6. **Per-lane ladder** (owner's design): a blamed lane first gets one repair attempt on its own
   tier (Haiku, with the gate feedback), then escalates one step per further blame: Sonnet 5.5
   (medium; the plan said low), then Opus 5.5 (low). Only blamed lanes move; the others keep their
   output. After each round the lanes are re-merged and re-gated; the task stops at the first pass.
   A lane blamed again at Opus ends the task unsolved. Nothing ever runs on Fable 5.2:
   :class:`LaneConfig` refuses any model outside ``{haiku, sonnet, opus}``.
7. **Shadow merge (La-shadow).** The lanes' first replies (no edit repair) are concatenated with
   no contracts: out-of-scope blocks apply if their SEARCH matches, a later creation of the same
   file wins, no interface check, no gate, no escalation. Scored once on the full score tests by
   the verifier; it costs no tokens. Derived outcomes ``La-first`` (the governed first-round merge,
   shipped only if its gates passed) and ``La-sonnet`` (the state the ladder reaches without the Opus
   step) split the governed result into parts.

Events (docs/events.md): ``lane.planned``, ``interface.frozen``, ``interface.requested``,
``merge.decided``, ``coordination.detected``; lanes are kernel tasks ``La:<id>:<lane>``
(``kind: lane``, ``max_attempts`` = ladder length, attempts are ladder steps) and the merge is the
parent task ``La:<id>``. Every La request (planner, planner repair, lanes, edit repairs, lane
repairs, escalations) is ``usage.recorded`` with ``arm: "La"``: all of it is La's cost, and the
budget guard checks each request before it is sent.
"""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from ..contracts import DEFAULT_PROTECTED, LaneContract
from ..gates import T0_VISIBLE, T1_PRIVATE
from ..interfaces import FABLE, HAIKU, OPUS, SONNET, GateResult, Message, Submission
from .arms import TaskContext, _gate_dict, format_files
from .edits import EDIT_FORMAT, apply_edits, diff_to_blocks, format_blocks, parse_edits, scope_checker, unified_patch
from .lanes_detect import (
    Clash,
    added_lines,
    defined_names,
    duplicated_work,
    interface_output_clashes,
    lane_round_clashes,
    last_component,
    merge_conflicts,
    overlapping_scopes,
    regressions,
    unresolved_uses,
)
from .outline import render_outline
from .polyglot import sha256_json, sha256_text
from .swepro import CHARS_PER_TOKEN, TEST_PATTERNS, ProExercise, ProTestSplit, estimate_tokens, is_test_path

LANE_ARM = "La"
SHADOW_ARM = "La-shadow"
FIRST_ARM = "La-first"
SONNET_ARM = "La-sonnet"
DERIVED_ARMS: tuple[str, ...] = (SHADOW_ARM, FIRST_ARM, SONNET_ARM)
LANE_MODELS = frozenset({HAIKU, SONNET, OPUS})
T0_INTERFACES = "T0.interfaces"
MAX_LANES = 4
LANE_OUTLINE_MAX_CHARS = 24_000
LANE_PROMPT_MAX_TOKENS = 90_000
# Per-lane ladder: (step, model, effort). Attempt n of a lane runs step n-1.
DEFAULT_LADDER: tuple[tuple[str, str, str | None], ...] = (
    ("haiku", HAIKU, "medium"),
    ("haiku-repair", HAIKU, "medium"),
    ("sonnet", SONNET, "medium"),
    ("opus", OPUS, "low"),
)
_LANE_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_REQUEST = re.compile(r"^\s*INTERFACE REQUEST:\s*([^:\n]+?)\s*:\s*(.+?)\s*$", re.M)


@dataclass(frozen=True)
class LaneConfig:
    """Arm La's fixed settings (pre-registered). Any model outside ``LANE_MODELS`` (Fable 5.2 above
    all) is refused for the planner, the lanes and every escalation tier."""

    planner_model: str = OPUS
    planner_effort: str | None = "low"
    planner_max_tokens: int = 8_000
    max_lanes: int = MAX_LANES
    ladder: tuple[tuple[str, str, str | None], ...] = DEFAULT_LADDER
    lane_max_tokens: int = 32_000
    max_rounds: int = 3  # merge rounds after the first; bounded by the ladder too
    plan_repair: bool = True
    lane_prompt_max_tokens: int = LANE_PROMPT_MAX_TOKENS
    outline_max_chars: int = LANE_OUTLINE_MAX_CHARS

    def __post_init__(self) -> None:
        models = [self.planner_model, *(m for _, m, _ in self.ladder)]
        bad = sorted({m for m in models if m not in LANE_MODELS})
        if bad or FABLE in models:
            raise ValueError(f"arm La runs only on {sorted(LANE_MODELS)}; refused: {bad or [FABLE]}")
        if not self.ladder or not 1 <= self.max_lanes <= MAX_LANES:
            raise ValueError("arm La needs a non-empty ladder and 1 <= max_lanes <= 4")

    def to_dict(self) -> dict[str, Any]:
        return {"planner_model": self.planner_model, "planner_effort": self.planner_effort,
                "planner_max_tokens": self.planner_max_tokens, "max_lanes": self.max_lanes,
                "ladder": [list(x) for x in self.ladder], "lane_max_tokens": self.lane_max_tokens,
                "max_rounds": self.max_rounds, "plan_repair": self.plan_repair,
                "lane_prompt_max_tokens": self.lane_prompt_max_tokens}


# ------------------------------------------------------------------ prompts

LANE_SYSTEM = (
    "You are a careful software engineer working on one lane of a change to a large existing repository. "
    "Other engineers change the task's other files in parallel. You are given the issue, its requirements, the "
    "interfaces frozen for all lanes, and the full current content of your lane's files only. Change only your "
    "lane's files and create only the new files listed for your lane. Keep every frozen interface exactly as "
    "written: other lanes rely on it. You cannot run code or see the tests' source.\n\n" + EDIT_FORMAT
)

LANE_TASK = """Repository: {repo} ({language})
Task: {instance_id}
Lane: {lane_id} of {k} (files: {lane_files})

## Problem statement
{statement}

## Requirements
{requirements}

## Interface (from the task)
{interface}

## Your lane's goal
{goal}

## Frozen interfaces (shared by all lanes; never change them)
{frozen}

## Interfaces your lane provides to other lanes
{provides}

## Interfaces your lane uses from other lanes (implemented by them; call them as written)
{uses}

## Your files (full current content)
{files}

## New files your lane may create
{new_files}

## Other files of the task (outline only; other lanes change them; do not edit them)
{outline}

## Tests your lane should help make pass (names only; their code is not shown)
{tests}

## Interface changes
The interfaces above are frozen. If your lane cannot work without a change, still implement against the frozen \
interface and add one line per symbol: INTERFACE REQUEST: <symbol>: <signature you need>
"""

PLAN_SCHEMA = """{"lanes": [{"lane_id": "L1", "goal": "what this lane changes and why", "files": ["path"],
            "symbols": ["names this lane may change"], "provides": ["name(params) it provides to other lanes"],
            "uses": ["name(params) it uses from other lanes"], "tests": ["visible test names, copied exactly"]}],
 "interfaces": [{"symbol": "name", "signature": "name(params) -> type", "path": "file", "owner": "L1"}],
 "rationale": "one sentence"}"""

PLAN_REQUEST = """
## Request: lane plan
Split this change into 1 to {max_lanes} lanes. Each lane is done by a separate programmer, in parallel, who \
sees the full content of its own files only and the outline above of the others. Use fewer lanes when the files \
are tightly coupled; one lane is allowed.

Rules:
- Assign every file listed below to exactly one lane: no file in two lanes, none left out. Never assign a test file.
- For each lane give its goal (at most 80 words), the symbols it may change, the interfaces it provides to other \
lanes and those it uses from them, and the visible tests (names exactly as listed above) it should help make pass.
- Freeze every interface that crosses lanes (a symbol one lane defines or changes and another lane uses) with its \
exact signature (Python: name(params) -> type; JavaScript/TypeScript: name(params)), its file and the lane that \
owns it. Include every function, method or class named in the task's Interface section.

## Files to assign
{files}

Reply with one JSON object in a ```json code block and nothing else, in this schema:
{schema}"""

PLAN_REPAIR = """Your lane plan could not be used:

{errors}

Reply with the corrected JSON object only, in the same schema, in a ```json code block."""

LANE_RETRY = """Your lane's previous attempt was rejected when all lanes were merged and tested, and was discarded.

## Your lane's previous changes (diff against your files above)
{diff}

## Why the merged change was rejected
{feedback}

Write a corrected solution for your lane as SEARCH/REPLACE blocks against the ORIGINAL files shown above."""


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {x}" for x in items) or "(none)"


# ------------------------------------------------------------------ the plan


@dataclass(frozen=True)
class Lane:
    lane_id: str
    allowed: tuple[str, ...]  # existing task files this lane owns
    new_files: tuple[str, ...] = ()  # files this lane may create
    goal: str = ""
    symbols: tuple[str, ...] = ()
    exports: tuple[str, ...] = ()  # interfaces it provides
    imports: tuple[str, ...] = ()  # interfaces it uses
    tests: tuple[str, ...] = ()  # visible tests assigned by the plan

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(sorted({*self.allowed, *self.new_files}))

    def to_dict(self) -> dict[str, Any]:
        return {"lane_id": self.lane_id, "allowed": list(self.allowed), "new_files": list(self.new_files),
                "goal": self.goal, "symbols": list(self.symbols), "exports": list(self.exports),
                "imports": list(self.imports), "tests": list(self.tests)}


@dataclass(frozen=True)
class Interface:
    symbol: str
    signature: str = ""
    path: str | None = None
    owner: str | None = None
    source: str = "planner"  # "planner" | "task_interface"
    kind: str = ""  # task interface "Type" (function, class, method...), if known

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "signature": self.signature, "path": self.path, "owner": self.owner,
                "source": self.source, "kind": self.kind}


@dataclass(frozen=True)
class LanePlan:
    lanes: tuple[Lane, ...]
    interfaces: tuple[Interface, ...] = ()
    rationale: str = ""
    fallback: str | None = None  # "single_lane" when the planner's plan was unusable

    @property
    def k(self) -> int:
        return len(self.lanes)

    def lane(self, lane_id: str) -> Lane:
        return next(ln for ln in self.lanes if ln.lane_id == lane_id)

    def owner_of(self, path: str) -> str | None:
        return next((ln.lane_id for ln in self.lanes if path in ln.files), None)

    def to_dict(self) -> dict[str, Any]:
        return {"lanes": [ln.to_dict() for ln in self.lanes], "interfaces": [i.to_dict() for i in self.interfaces],
                "rationale": self.rationale, "fallback": self.fallback}

    @property
    def sha256(self) -> str:
        return sha256_json(self.to_dict())

    def frozen(self) -> dict[str, str]:
        """symbol -> signature (the board's ``interfaces`` field)."""
        return {i.symbol: i.signature for i in self.interfaces}


@dataclass(frozen=True)
class PlanParse:
    plan: LanePlan | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    raw_lanes: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (lane_id, files) as the planner wrote them


def json_object(text: str) -> tuple[dict | None, str]:
    """The first JSON object in ``text``: a fenced ```json block, else the outermost braces."""
    cands = [m.group(1) for m in re.finditer(r"```(?:json)?[ \t]*\n(.*?)```", text, re.S)]
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        cands.append(text[i:j + 1])
    err = "no JSON object found in the reply"
    for c in cands:
        try:
            d = json.loads(c)
        except ValueError as e:
            err = f"the JSON does not parse: {e}"
            continue
        if isinstance(d, dict):
            return d, ""
        err = "the reply's JSON is not an object"
    return None, err


def _strs(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,) if v.strip() else ()
    if isinstance(v, (list, tuple)):
        return tuple(str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip())
    return ()


def task_interfaces(text: str, task_files: Sequence[str] = ()) -> list[Interface]:
    """Functions, classes and methods named in a task's ``interface`` field (``- Path:`` /
    ``- Name:`` / ``- Type:`` / ``- Input:`` / ``- Output:`` blocks). Entries of type ``file`` and
    names that are not identifiers are skipped."""
    out: list[Interface] = []
    cur: dict[str, str] = {}

    def flush() -> None:
        name = cur.get("name", "").strip().strip("`").strip()
        kind = cur.get("type", "").strip().strip("`").lower()
        if name and re.fullmatch(r"[A-Za-z_$][\w$]*(?:[.#][A-Za-z_$][\w$]*)*", name) and kind != "file":
            path = cur.get("path", "").strip().strip("`").strip() or None
            sig = f"{name}({cur.get('input', '').strip()})"
            if cur.get("output", "").strip():
                sig += f" -> {cur['output'].strip()}"
            out.append(Interface(name, sig[:300], path if path in set(task_files) else path, None, "task_interface",
                                 kind))
        cur.clear()

    for line in text.splitlines():
        m = re.match(r"^\s*[-*]?\s*(path|name|type|input|inputs|output|outputs|description)\s*:\s*(.*)$", line, re.I)
        if not m:
            if not line.strip():
                if "name" in cur:
                    flush()
            continue
        key = m.group(1).lower().rstrip("s") if m.group(1).lower() in ("inputs", "outputs") else m.group(1).lower()
        if key in cur and key in ("path", "name"):
            flush()
        cur[key] = m.group(2)
    flush()
    seen, uniq = set(), []
    for i in out:
        if i.symbol not in seen:
            seen.add(i.symbol)
            uniq.append(i)
    return uniq


def seed_interfaces(planned: Sequence[Interface], task_iface: Sequence[Interface], lanes: Sequence[Lane]) -> tuple[Interface, ...]:
    """The board: the planner's interfaces, then every task-interface symbol it did not list (owner =
    the lane owning its path, if any)."""
    have = {last_component(i.symbol) for i in planned}
    out = list(planned)
    for t in task_iface:
        if last_component(t.symbol) in have:
            continue
        owner = next((ln.lane_id for ln in lanes if t.path and t.path in ln.files), None)
        out.append(replace(t, owner=owner))
        have.add(last_component(t.symbol))
    return tuple(out)


def parse_plan(text: str, ex: ProExercise, ts: ProTestSplit, *, max_lanes: int = MAX_LANES,
               lane_tokens=None, lane_prompt_max_tokens: int = LANE_PROMPT_MAX_TOKENS) -> PlanParse:
    """Validate a planner reply strictly. ``lane_tokens(plan, lane)`` (optional) estimates a lane's
    prompt; a lane above ``lane_prompt_max_tokens`` makes the plan invalid. Unknown test names are
    dropped with a warning; everything about files is an error."""
    d, err = json_object(text)
    if d is None:
        return PlanParse(None, (err,))
    errors: list[str] = []
    warnings: list[str] = []
    existing = set(ex.context.files)
    new = set(ex.context.new_files)
    task_files = existing | new
    visible = set(ts.visible)
    raw = d.get("lanes")
    if not isinstance(raw, list) or not raw:
        return PlanParse(None, ("'lanes' must be a non-empty list",))
    raw_lanes: list[tuple[str, tuple[str, ...]]] = []
    lanes: list[Lane] = []
    owners: dict[str, list[str]] = {}
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            errors.append(f"lane {n} is not an object")
            continue
        lid = str(item.get("lane_id") or f"L{n}").strip()
        if not _LANE_ID.match(lid):
            errors.append(f"lane {n}: lane_id {lid!r} must be 1-16 letters, digits, '-' or '_'")
            lid = f"L{n}"
        if any(ln.lane_id == lid for ln in lanes):
            errors.append(f"lane_id {lid!r} is used twice")
        files = tuple(dict.fromkeys(_strs(item.get("files") or item.get("allowed"))))
        files += tuple(f for f in _strs(item.get("new_files")) if f not in files)
        raw_lanes.append((lid, files))
        for f in files:
            if is_test_path(f):
                errors.append(f"lane {lid}: {f} is a test file; lanes never own tests")
            elif f not in task_files:
                errors.append(f"lane {lid}: {f} is not one of the files to assign")
            else:
                owners.setdefault(f, []).append(lid)
        if not files:
            errors.append(f"lane {lid} has no files")
        tests = _strs(item.get("tests"))
        unknown = [t for t in tests if t not in visible]
        if unknown:
            warnings.append(f"lane {lid}: {len(unknown)} test name(s) not among the visible tests were dropped")
        lanes.append(Lane(lid, tuple(sorted(f for f in files if f in existing)), tuple(sorted(f for f in files if f in new)),
                          str(item.get("goal") or "").strip()[:2000], _strs(item.get("symbols")),
                          _strs(item.get("provides") or item.get("exports")), _strs(item.get("uses") or item.get("imports")),
                          tuple(t for t in tests if t in visible)))
    if len(raw) > max_lanes:
        errors.append(f"{len(raw)} lanes; at most {max_lanes} are allowed")
    for f, ls in sorted(owners.items()):
        if len(ls) > 1:
            errors.append(f"{f} is assigned to lanes {', '.join(ls)}; each file belongs to exactly one lane")
    for f in sorted(task_files - set(owners)):
        errors.append(f"{f} is assigned to no lane")
    ifaces: list[Interface] = []
    raw_if = d.get("interfaces", [])
    if isinstance(raw_if, dict):
        raw_if = [{"symbol": k, "signature": v} for k, v in raw_if.items()]
    if not isinstance(raw_if, list):
        errors.append("'interfaces' must be a list")
        raw_if = []
    ids = {ln.lane_id for ln in lanes}
    for item in raw_if:
        if not isinstance(item, dict) or not str(item.get("symbol") or "").strip():
            warnings.append("an interface entry without a symbol was dropped")
            continue
        owner = str(item.get("owner") or "").strip() or None
        if owner is not None and owner not in ids:
            warnings.append(f"interface {item['symbol']}: unknown owner {owner!r} dropped")
            owner = None
        path = str(item.get("path") or "").strip() or None
        ifaces.append(Interface(str(item["symbol"]).strip()[:200], str(item.get("signature") or "").strip()[:400],
                                path, owner, "planner"))
    plan = LanePlan(tuple(lanes), tuple(ifaces), str(d.get("rationale") or "").strip()[:1000])
    if lane_tokens is not None and not errors:
        for ln in lanes:
            n = lane_tokens(plan, ln)
            if n > lane_prompt_max_tokens:
                errors.append(f"lane {ln.lane_id}'s prompt is about {n} tokens, above {lane_prompt_max_tokens}")
    return PlanParse(None if errors else plan, tuple(errors), tuple(warnings), tuple(raw_lanes))


def single_lane_plan(ex: ProExercise, ts: ProTestSplit, fallback: str | None = "single_lane") -> LanePlan:
    """One lane owning every file (the fallback when the plan is unusable)."""
    return LanePlan((Lane("L1", tuple(sorted(ex.context.files)), tuple(sorted(ex.context.new_files)),
                          "the whole task", tests=tuple(ts.visible)),), (), "", fallback)


def by_file_plan(ex: ProExercise, k: int = 3) -> LanePlan:
    """A naive plan: the task's files in path order cut into ``k`` contiguous lanes (the pre-flight's
    k = 3 split, the gold-lane validation and the dry run's fake planner)."""
    files = list(ex.solution_files)
    k = max(1, min(k, len(files)))
    q, r = divmod(len(files), k)
    lanes, start = [], 0
    for n in range(k):
        chunk = files[start:start + q + (1 if n < r else 0)]
        start += len(chunk)
        if chunk:
            lanes.append(Lane(f"L{n + 1}", tuple(f for f in chunk if f in ex.context.files),
                              tuple(f for f in chunk if f not in ex.context.files), f"change {', '.join(chunk)}"))
    return LanePlan(tuple(lanes), (), "files in path order, cut into contiguous lanes")


def plan_reply(plan: LanePlan) -> str:
    """A plan rendered as a planner reply (fakes and tests)."""
    d = {"lanes": [{"lane_id": ln.lane_id, "goal": ln.goal, "files": list(ln.files), "symbols": list(ln.symbols),
                    "provides": list(ln.exports), "uses": list(ln.imports), "tests": list(ln.tests)} for ln in plan.lanes],
         "interfaces": [{"symbol": i.symbol, "signature": i.signature, "path": i.path, "owner": i.owner}
                        for i in plan.interfaces if i.source == "planner"],
         "rationale": plan.rationale}
    return "```json\n" + json.dumps(d, indent=1) + "\n```\n"


# ------------------------------------------------------------------ prompts for a lane


def lane_scope(lane: Lane):
    return scope_checker(lane.files, (*DEFAULT_PROTECTED, *TEST_PATTERNS))


def lane_contract(task_id: str, lane: Lane) -> LaneContract:
    return LaneContract(lane_id=lane.lane_id, task_id=task_id, allowed=lane.files,
                        protected=tuple(dict.fromkeys((*DEFAULT_PROTECTED, *TEST_PATTERNS))), symbols=lane.symbols)


def lane_prompt(ex: ProExercise, ts: ProTestSplit, plan: LanePlan, lane: Lane,
                outline_max_chars: int = LANE_OUTLINE_MAX_CHARS) -> str:
    t = ex.task
    own = {p: ex.context.files[p] for p in lane.allowed if p in ex.context.files}
    others = {p: c for p, c in ex.context.files.items() if p not in lane.files}
    other_new = tuple(p for p in ex.context.new_files if p not in lane.files)
    outline = render_outline(others, new_files=other_new, max_chars=outline_max_chars) if (others or other_new) else "(none)"
    frozen = [f"{i.signature or i.symbol}" + (f"  [file {i.path}]" if i.path else "")
              + (f"  [owner {i.owner}]" if i.owner else "") for i in plan.interfaces]
    tests = lane.tests or ts.visible
    note = "" if lane.tests else "(the plan assigned none; these are the whole task's visible tests)\n"
    return LANE_TASK.format(
        repo=t.repo, language=t.language, instance_id=t.instance_id, lane_id=lane.lane_id, k=plan.k,
        lane_files=", ".join(lane.files), statement=t.problem_statement.strip(),
        requirements=t.requirements.strip() or "(none)", interface=t.interface.strip() or "(none)",
        goal=lane.goal or "(see the problem statement)", frozen=_bullets(frozen), provides=_bullets(lane.exports),
        uses=_bullets(lane.imports), files=format_files(own) or "(none: this lane only creates files)",
        new_files=_bullets(lane.new_files), outline=outline, tests=note + _bullets(tests))


def lane_retry_prompt(base: str, ex: ProExercise, lane: Lane, previous: Mapping[str, str], feedback: str,
                      feedback_chars: int = 3000, diff_chars: int = 40_000) -> str:
    own = {p: ex.context.files[p] for p in lane.allowed if p in ex.context.files}
    diff = unified_patch(own, {p: c for p, c in previous.items() if p in lane.files})
    if len(diff) > diff_chars:
        diff = diff[:diff_chars] + "\n[diff truncated]\n"
    return base + "\n" + LANE_RETRY.format(diff=diff or "(no change was applied)",
                                           feedback=feedback.strip()[:feedback_chars] or "(no details)")


def interface_requests(text: str) -> list[dict[str, str]]:
    return [{"symbol": m.group(1).strip()[:200], "requested_signature": m.group(2).strip()[:400]}
            for m in _REQUEST.finditer(text or "")]


# ------------------------------------------------------------------ T0.interfaces


def _param_names(signature: str, name: str) -> list[str] | None:
    """Parameter names of a Python-style signature ``name(a, b=1) -> T`` (None if it does not parse)."""
    s = signature.strip().strip("`")
    s = re.sub(r"^(?:async\s+)?def\s+", "", s)
    m = re.search(r"\((.*)\)", s, re.S)
    if not m:
        return None
    try:
        fn = ast.parse(f"def f({m.group(1)}): pass").body[0]
    except SyntaxError:
        return None
    a = fn.args  # type: ignore[attr-defined]
    names = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    names += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
    return [n for n in names if n not in ("self", "cls")]


def _python_defs(files: Mapping[str, str], name: str) -> list[list[str]]:
    out = []
    for path, src in files.items():
        if not path.endswith((".py", ".pyi")):
            continue
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                a = node.args
                ps = [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
                ps += [x.arg for x in (a.vararg, a.kwarg) if x is not None]
                out.append([p for p in ps if p not in ("self", "cls")])
    return out


def checked_interfaces(plan: LanePlan, base: Mapping[str, str]) -> list[Interface]:
    """The frozen symbols T0.interfaces checks: those the base files define (they must keep
    existing), those a lane provides, and task-interface functions, classes and methods."""
    base_names = set().union(*defined_names(base).values()) if base else set()
    exported = {last_component(s) for ln in plan.lanes for s in ln.exports}
    out = []
    for i in plan.interfaces:
        short = last_component(i.symbol)
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", short or ""):
            continue
        if short in base_names or short in exported or (i.source == "task_interface"
                                                         and i.kind in ("function", "class", "method", "")):
            out.append(i)
    return out


def interface_check(plan: LanePlan, base: Mapping[str, str], merged: Mapping[str, str]) -> GateResult:
    """T0.interfaces on the merged view (base files overlaid with the merged changes).

    A checked symbol (:func:`checked_interfaces`) must still be defined: Python by ``ast``
    (functions, classes, methods, module names), JavaScript/TypeScript by the outline's declaration
    lines or, failing that, as a word in some task file. For a planner-frozen Python signature the
    frozen parameter names must all be accepted by some definition of that name. Deterministic; the
    reason names the symbols, never test information."""
    view = {**base, **merged}
    names = defined_names(view)
    defined = set().union(*names.values()) if names else set()
    js_text = "\n".join(c for p, c in view.items() if not p.endswith((".py", ".pyi")))
    missing, mismatched = [], []
    checked = checked_interfaces(plan, base)
    for i in checked:
        short = last_component(i.symbol)
        present = short in defined or bool(re.search(rf"(?<![\w$]){re.escape(short)}(?![\w$])", js_text))
        if not present:
            missing.append(i)
            continue
        if i.source == "planner" and i.signature:
            want = _param_names(i.signature, short)
            defs = _python_defs(view, short)
            if want is not None and defs and not any(set(want) <= set(d) for d in defs):
                mismatched.append((i, want, defs[0]))
    if not missing and not mismatched:
        return GateResult(T0_INTERFACES, "pass", "", {"checked": [i.symbol for i in checked]})
    parts = [f"frozen interface {i.symbol} is not defined in the merged change" for i in missing]
    parts += [f"frozen interface {i.symbol} expects parameters ({', '.join(w)}), found ({', '.join(d)})"
              for i, w, d in mismatched]
    owners = sorted({o for o in (_owner(plan, i) for i in [*missing, *(m[0] for m in mismatched)]) if o})
    return GateResult(T0_INTERFACES, "block", "; ".join(parts), {
        "checked": [i.symbol for i in checked], "missing": [i.symbol for i in missing],
        "mismatched": [i.symbol for i, _, _ in mismatched], "owners": owners})


def _owner(plan: LanePlan, i: Interface) -> str | None:
    if i.owner:
        return i.owner
    if i.path and plan.owner_of(i.path):
        return plan.owner_of(i.path)
    short = last_component(i.symbol)
    return next((ln.lane_id for ln in plan.lanes if any(last_component(s) == short for s in ln.exports)), None)


def interface_gate(plan: LanePlan, base: Mapping[str, str]):
    """T0.interfaces as a ``ProGates.pre_test`` check."""
    def check(sub: Submission, ctx: TaskContext) -> GateResult:
        del ctx
        return interface_check(plan, base, sub.files)

    check.gate_name = T0_INTERFACES  # type: ignore[attr-defined]
    return check


# ------------------------------------------------------------------ merges


@dataclass(frozen=True)
class MergeResult:
    files: dict[str, str]
    errors: tuple[str, ...] = ()  # edit errors carried into T0.edits (scope rejections are counted, not carried)
    conflicts: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (path, lanes) changed by more than one lane
    out_of_scope: tuple[dict, ...] = ()  # shadow: blocks applied outside their lane's files
    reverted: tuple[dict, ...] = ()  # shadow: lines one lane added that a later lane removed
    lanes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # lane -> paths it contributed


def merge_governed(plan: LanePlan, subs: Mapping[str, Submission]) -> MergeResult:
    """Union of the lanes' changed files, in plan order. Lane scopes are disjoint, so a path changed
    by two lanes means the plan or a scope check failed: it is reported as a conflict and the first
    lane keeps it. A lane's parse/apply/truncation errors are carried into T0.edits; blocks its scope
    rejected are not (they are counted as scope violations)."""
    files: dict[str, str] = {}
    by: dict[str, list[str]] = {}
    errors: list[str] = []
    contributed: dict[str, tuple[str, ...]] = {}
    for ln in plan.lanes:
        sub = subs.get(ln.lane_id)
        if sub is None:
            continue
        mine = []
        for p in sorted(sub.files):
            by.setdefault(p, []).append(ln.lane_id)
            if p not in files:
                files[p] = sub.files[p]
                mine.append(p)
        contributed[ln.lane_id] = tuple(mine)
        errors += [f"lane {ln.lane_id}: {e}" for e in getattr(sub, "edit_errors", ()) if ": rejected, " not in e]
    conflicts = tuple((p, tuple(ls)) for p, ls in sorted(by.items()) if len(ls) > 1)
    errors += [f"{p} was changed by lanes {', '.join(ls)}; kept lane {ls[0]}'s version" for p, ls in conflicts]
    return MergeResult(files, tuple(errors), conflicts, lanes=contributed)


def merge_shadow(plan: LanePlan, replies: Mapping[str, str], base: Mapping[str, str]) -> MergeResult:
    """The ungoverned join of the lanes' first replies (no edit repair): every block in plan order,
    with no lane scope (only test files are refused, as in every arm, since the official verifier
    resets them anyway), no interface check and no gate. Two lanes creating the same file: the later
    creation wins. A block whose SEARCH does not match the current text is skipped."""
    parsed = [(ln, parse_edits(replies.get(ln.lane_id, "")).blocks) for ln in plan.lanes]
    last_create: dict[str, str] = {}
    creators: dict[str, list[str]] = {}
    for ln, blocks in parsed:
        for b in blocks:
            if b.search == "":
                last_create[b.path] = ln.lane_id
                creators.setdefault(b.path, []).append(ln.lane_id)
    protect = scope_checker(("*",), (*DEFAULT_PROTECTED, *TEST_PATTERNS))
    view = dict(base)
    errors, oos, reverted = [], [], []
    inserted: list[tuple[str, str, str]] = []  # (lane, path, line)
    touched: dict[str, list[str]] = {}
    for ln, blocks in parsed:
        for b in blocks:
            if b.search == "" and last_create.get(b.path) != ln.lane_id:
                continue  # a later lane creates the same file and wins
            if b.search == "" and b.path in view and b.path not in base:
                view.pop(b.path)  # overwritten creation
            res = apply_edits([b], view, scope=protect)
            if res.errors:
                errors += [f"lane {ln.lane_id}: {e}" for e in res.errors]
                continue
            gone = [x for x in inserted if x[0] != ln.lane_id and x[1] == b.path and x[2] in b.search
                    and x[2] not in b.replace]
            if gone:
                reverted.append({"by": ln.lane_id, "of": sorted({x[0] for x in gone}), "path": b.path,
                                 "lines": len(gone)})
            view.update(res.files)
            inserted += [(ln.lane_id, b.path, line) for line in added_lines(b.search, b.replace)]
            touched.setdefault(b.path, []).append(ln.lane_id)
            if b.path not in ln.files:
                oos.append({"lane": ln.lane_id, "path": b.path, "block": b.index})
    files = {p: c for p, c in view.items() if base.get(p) != c}
    conflicts = [(p, tuple(dict.fromkeys(ls))) for p, ls in sorted(creators.items()) if len(set(ls)) > 1]
    conflicts += [(p, tuple(dict.fromkeys(ls))) for p, ls in sorted(touched.items())
                  if len(set(ls)) > 1 and p not in creators]
    return MergeResult(files, tuple(errors), tuple(conflicts), tuple(oos), tuple(reverted),
                       {ln.lane_id: tuple(sorted({p for p, ls in touched.items() if ln.lane_id in ls})) for ln in plan.lanes})


# ------------------------------------------------------------------ blame


def _stem(path: str) -> str:
    b = path.rsplit("/", 1)[-1]
    b = re.sub(r"\.(py|pyi|js|jsx|ts|tsx|mjs|cjs)$", "", b)
    b = re.sub(r"(\.test|\.spec|_test)$", "", b)
    return re.sub(r"^test_", "", b).lower()


def blame(plan: LanePlan, results: Sequence[GateResult], *, failed_tests: Sequence[str] = (),
          output: str = "") -> tuple[tuple[str, ...], int, str]:
    """(blamed lane ids, rule, basis) for a blocked merged decision.

    Rule 1 (direct): T0.edits -> the lanes whose edit errors blocked it; T0.interfaces -> the owners
    of the broken symbols; a test gate -> the lanes the plan assigned a failing test.
    Rule 2: a failing test's file stem equals a lane file's stem, or the verifier output names a
    lane file (tracebacks; the kernel reads them, workers never see them).
    Rule 3: every lane (nothing else attributes the block)."""
    all_ids = tuple(ln.lane_id for ln in plan.lanes)
    block = next((r for r in results if r.verdict != "pass"), None)
    if block is None:
        return (), 0, "none"
    ev = block.evidence if isinstance(block.evidence, Mapping) else {}
    if block.gate == "T0.edits":
        errs = [str(e) for e in ev.get("errors", [])]
        lanes = tuple(i for i in all_ids if any(e.startswith(f"lane {i}:") for e in errs))
        if lanes:
            return lanes, 1, "edits"
        return all_ids, 3, "edits:no-change"
    if block.gate == T0_INTERFACES:
        lanes = tuple(i for i in all_ids if i in set(ev.get("owners", [])))
        return (lanes, 1, "interfaces") if lanes else (all_ids, 3, "interfaces:unowned")
    if block.gate in (T0_VISIBLE, T1_PRIVATE):
        failed = set(failed_tests)
        lanes = tuple(ln.lane_id for ln in plan.lanes if failed & set(ln.tests))
        if lanes:
            return lanes, 1, "tests:assigned"
        hit = set()
        stems = {_stem(t.split("::")[0].split(" | ")[0].strip()) for t in failed}
        for ln in plan.lanes:
            for p in ln.files:
                if _stem(p) in stems or (output and (p in output or (p.count("/") >= 1 and p.split("/", 1)[1] in output))):
                    hit.add(ln.lane_id)
        lanes = tuple(i for i in all_ids if i in hit)
        if lanes:
            return lanes, 2, "tests:traceback"
        return all_ids, 3, "tests:unattributed"
    return all_ids, 3, f"{block.gate}:unattributed"


# ------------------------------------------------------------------ the arm


@dataclass
class _LaneState:
    lane: Lane
    task_id: str
    base_prompt: str
    step: int = -1  # ladder index of the latest attempt
    sub: Any = None
    first: Any = None
    last_feedback: str = ""


def _planner_context(h, ex: ProExercise, ts: ProTestSplit, max_lanes: int) -> str:
    files = [f"{p} ({ex.context.files[p].count(chr(10)) + 1} lines)" for p in sorted(ex.context.files)]
    files += [f"{p} (new file)" for p in sorted(ex.context.new_files)]
    return h.lead_context(ex, ts) + PLAN_REQUEST.format(max_lanes=max_lanes, files=_bullets(files), schema=PLAN_SCHEMA)


def plan_lanes(h, ex: ProExercise, ts: ProTestSplit, tid: str, cfg: LaneConfig) -> tuple[LanePlan, dict[str, Any]]:
    """The planner request (and at most one plan-repair turn). Returns the plan (the single-lane
    fallback when the reply stays invalid) and the ``lane.planned`` extras."""
    user = _planner_context(h, ex, ts, cfg.max_lanes)

    def lane_tokens(plan: LanePlan, lane: Lane) -> int:
        return estimate_tokens(LANE_SYSTEM + lane_prompt(ex, ts, plan, lane, cfg.outline_max_chars))

    kw = dict(arm=LANE_ARM, role="orchestrator", task_id=tid, model=cfg.planner_model, system=h.lead_system,
              max_tokens=cfg.planner_max_tokens, effort=cfg.planner_effort)
    c = h.call(user=user, extra_tags={"lane_role": "planner"}, **kw)
    got = parse_plan(c.text, ex, ts, max_lanes=cfg.max_lanes, lane_tokens=lane_tokens,
                     lane_prompt_max_tokens=cfg.lane_prompt_max_tokens)
    info: dict[str, Any] = {"repair_used": False, "parse_errors": list(got.errors), "warnings": list(got.warnings),
                            "stop_reason": c.stop_reason, "response": c.text}
    raw = list(got.raw_lanes)
    if got.plan is None and cfg.plan_repair:
        history = (Message("user", user), Message("assistant", c.text.rstrip() or "(no output)"))
        c2 = h.call(user=PLAN_REPAIR.format(errors=_bullets(got.errors[:20])), history=history,
                    extra_tags={"lane_role": "planner", "repair": True}, **kw)
        got = parse_plan(c2.text, ex, ts, max_lanes=cfg.max_lanes, lane_tokens=lane_tokens,
                         lane_prompt_max_tokens=cfg.lane_prompt_max_tokens)
        info.update(repair_used=True, repair_errors=list(got.errors), repair_response=c2.text,
                    repair_stop_reason=c2.stop_reason)
        info["warnings"] += list(got.warnings)
        raw = list(got.raw_lanes) or raw
    plan = got.plan or single_lane_plan(ex, ts)
    plan = replace(plan, interfaces=seed_interfaces(plan.interfaces, task_interfaces(ex.task.interface,
                                                                                      ex.solution_files), plan.lanes))
    info["raw_lanes"] = raw
    return plan, info


def _emit_clashes(h, tid: str, plan_id: str, clashes: Sequence[Clash], rnd: int | None, mode: str) -> int:
    for cl in clashes:
        h.emit("coordination.detected", "kernel", {"task_id": tid, "plan_id": plan_id, "round": rnd, "mode": mode,
                                                  **cl.payload()})
    return len(clashes)


def _merged_payload(h, ex: ProExercise, tid: str, attempt: int, rnd: int, files: Mapping[str, str],
                    errors: Sequence[str], lanes: Mapping[str, int | None], mode: str) -> dict[str, Any]:
    from ..gates import changed_lines

    files = dict(files)
    return {"task_id": tid, "attempt": attempt, "model": "merge", "files": sorted(files),
            **({"contents": files} if h.log_contents else
               {"contents_sha256": {p: sha256_text(c) for p, c in sorted(files.items())}}),
            "files_sha256": sha256_json(files), "parse_errors": list(errors), "response": "",
            "diff": unified_patch(ex.context.files, files),
            "changed_lines": changed_lines(Submission(tid, files), ex.context.files),
            "merge": mode, "round": rnd, "lanes": dict(lanes)}


def run_la(h, ex: ProExercise, ts: ProTestSplit, split: str = "", cfg: LaneConfig | None = None) -> bool:
    """Arm La on one task (see the module docstring). Returns whether La solved it."""
    from .swepro_arms import ProGates, ProSubmission

    cfg = cfg or getattr(h, "lane_config", None) or LaneConfig()
    tid = f"{LANE_ARM}:{ex.id}"
    contract = h.contract(tid, ex)
    cdict = {"allowed": list(contract.allowed), "protected": list(contract.protected)}
    max_rounds = min(cfg.max_rounds, len(cfg.ladder) - 1)
    h.create(LANE_ARM, tid, ex, split, max_attempts=1 + max_rounds, contract=cdict, lane_config=cfg.to_dict())

    # 1. plan
    plan, pinfo = plan_lanes(h, ex, ts, tid, cfg)
    plan_sha = plan.sha256
    plan_id = f"plan-{sha256_text(tid + ':' + plan_sha)[:12]}"
    planned = {"task_id": tid, "plan_id": plan_id, "model": cfg.planner_model, "k": plan.k,
               "lanes": [ln.to_dict() for ln in plan.lanes], "plan_sha256": plan_sha,
               "parse_errors": pinfo["parse_errors"], "repair_used": pinfo["repair_used"],
               "fallback": plan.fallback, "rationale": plan.rationale, "warnings": pinfo["warnings"],
               "stop_reason": pinfo["stop_reason"]}
    if pinfo["repair_used"]:
        planned["repair_errors"] = pinfo["repair_errors"]
    if h.log_contents:
        planned["response"] = pinfo["response"]
        if pinfo["repair_used"]:
            planned["repair_response"] = pinfo["repair_response"]
    h.emit("lane.planned", f"planner:{cfg.planner_model}", planned)

    # 2. freeze: the board, in one kernel step
    frozen = plan.frozen()
    board = {ln.lane_id: {"provides": list(ln.exports), "needs": list(ln.imports)} for ln in plan.lanes}
    h.emit("interface.frozen", "kernel", {
        "task_id": tid, "plan_id": plan_id, "interfaces": frozen, "interfaces_sha256": sha256_json(frozen),
        "source": sorted({i.source for i in plan.interfaces}) or ["none"],
        "entries": [i.to_dict() for i in plan.interfaces], "board": board})
    base_names = set().union(*defined_names(ex.context.files).values()) if ex.context.files else set()
    _emit_clashes(h, tid, plan_id, overlapping_scopes(pinfo["raw_lanes"]), None, "plan")
    _emit_clashes(h, tid, plan_id, unresolved_uses(plan.lanes, frozen, base_names), None, "plan")

    # gates for this task: D's, with T0.interfaces before any test
    gates = h.gates
    if isinstance(gates, ProGates):
        gates = replace(gates, pre_test=(interface_gate(plan, ex.context.files),))
    verifier = getattr(h.gates, "verifier", None)

    states: dict[str, _LaneState] = {}
    for ln in plan.lanes:
        ltid = f"{tid}:{ln.lane_id}"
        lc = lane_contract(ltid, ln)
        h.create(LANE_ARM, ltid, ex, split, max_attempts=len(cfg.ladder), kind="lane", scope=list(ln.files),
                 parent=tid, plan_id=plan_id, lane_id=ln.lane_id,
                 contract={"allowed": list(lc.allowed), "protected": list(lc.protected), "symbols": list(lc.symbols)})
        states[ln.lane_id] = _LaneState(ln, ltid, lane_prompt(ex, ts, plan, ln, cfg.outline_max_chars))

    def run_lane(st: _LaneState, rnd: int) -> None:
        st.step += 1
        step, model, effort = cfg.ladder[st.step]
        prompt = st.base_prompt if st.sub is None else lane_retry_prompt(
            st.base_prompt, ex, st.lane, st.sub.files, st.last_feedback, h.config.feedback_chars)
        sub = h._work(LANE_ARM, st.task_id, ex, model, prompt, st.step + 1, effort, scope=lane_scope(st.lane),
                      max_tokens=cfg.lane_max_tokens, system=LANE_SYSTEM,
                      extra={"plan_id": plan_id, "lane_id": st.lane.lane_id, "tier": step, "round": rnd},
                      extra_tags={"lane_id": st.lane.lane_id, "tier": step})
        for r in interface_requests(sub.response + "\n" + str(sub.info.get("repair_response", ""))):
            h.emit("interface.requested", f"worker:{model}", {"task_id": st.task_id, "plan_id": plan_id,
                                                              "lane_id": st.lane.lane_id, "status": "rejected",
                                                              "reason": "interfaces are frozen for this task", **r})
            st.__dict__.setdefault("requests", []).append(r)
        st.sub = sub
        if st.first is None:
            st.first = sub

    def gate_merged(rnd: int, attempt: int, merged: MergeResult):
        h.emit("task.claimed", "kernel:merge", {"task_id": tid, "worker": "merge", "attempt": attempt})
        lanes_seq = {i: getattr(s.sub, "seq", None) for i, s in states.items()}
        h.emit("task.submitted", "kernel:merge", _merged_payload(h, ex, tid, attempt, rnd, merged.files, merged.errors,
                                                                 lanes_seq, "governed"))
        sub = ProSubmission(task_id=tid, files=merged.files, attempt=attempt, edit_errors=merged.errors)
        ctx = TaskContext(LANE_ARM, tid, ex, ts, "merge", attempt, contract, split, h.t2_reviewer(LANE_ARM, tid, ex))
        results = list(gates(sub, ctx)) if gates is not None else []
        passed = bool(results) and all(r.verdict == "pass" for r in results)
        reason = "; ".join(f"[{r.gate}] {r.reason}" for r in results if r.verdict != "pass")
        gev = h.emit("gate.decided", "gate", {
            "task_id": tid, "attempt": attempt, "verdict": "pass" if passed else "block",
            "gate": "+".join(r.gate for r in results) or "none", "tier": "merge", "round": rnd,
            "reason": reason[: h.config.feedback_chars] if reason else ("all gates passed" if passed else "no gates ran"),
            "results": [_gate_dict(r) for r in results]})
        return sub, ctx, passed, results, gev.seq

    def test_result(results: Sequence[GateResult], files: Mapping[str, str]):
        ran = any(r.gate in (T0_VISIBLE, T1_PRIVATE) for r in results)
        return verifier.run(ex, files) if (ran and verifier is not None) else None

    rounds: list[dict[str, Any]] = []
    prev_visible_passed: set[str] = set()
    escalated: tuple[str, ...] = ()
    rnd = 0
    final = None
    while True:
        to_run = [states[i] for i in (escalated or tuple(states))]
        for st in to_run:
            run_lane(st, rnd)
        for st in to_run:
            _emit_clashes(h, tid, plan_id, lane_round_clashes(st.lane.lane_id, st.sub, st.__dict__.pop("requests", [])),
                          rnd, "governed")
        merged = merge_governed(plan, {i: s.sub for i, s in states.items()})
        _emit_clashes(h, tid, plan_id, merge_conflicts(merged.conflicts, "governed"), rnd, "governed")
        if rnd == 0 or escalated:
            _emit_clashes(h, tid, plan_id, duplicated_work({i: s.sub.files for i, s in states.items()},
                                                           ex.context.files), rnd, "governed")
        sub, ctx, passed, results, gseq = gate_merged(rnd, rnd + 1, merged)
        res = test_result(results, merged.files)
        failed: list[str] = []
        if res is not None:
            failed = list(res.failed(ts.visible)) + list(res.failed(ts.gate_private))
            vis_failed = set(res.failed(ts.visible))
            if rnd > 0:
                _emit_clashes(h, tid, plan_id, regressions(prev_visible_passed, vis_failed, escalated), rnd, "governed")
            prev_visible_passed = set(ts.visible) - vis_failed
            owners = {i.symbol: _owner(plan, i) for i in plan.interfaces}
            _emit_clashes(h, tid, plan_id, interface_output_clashes(res.output_tail, frozen,
                                                                    {k: v for k, v in owners.items() if v},
                                                                    list(states)), rnd, "governed")
        iface = next((r for r in results if r.gate == T0_INTERFACES and r.verdict != "pass"), None)
        if iface is not None:
            _emit_clashes(h, tid, plan_id, [Clash("interface_mismatch", tuple(iface.evidence.get("owners") or states),
                                                  {"stage": "gate", "reason": iface.reason})], rnd, "governed")
        blamed: tuple[str, ...] = ()
        rule, basis = 0, "none"
        if not passed:
            blamed, rule, basis = blame(plan, results, failed_tests=failed, output=res.output_tail if res else "")
            if rule == 3:
                _emit_clashes(h, tid, plan_id, [Clash("unattributable_failure", blamed, {"basis": basis})],
                              rnd, "governed")
        top = max(s.step for s in states.values())
        h.emit("merge.decided", "kernel", {
            "task_id": tid, "plan_id": plan_id, "round": rnd, "mode": "governed",
            "lanes": {i: getattr(s.sub, "seq", None) for i, s in states.items()},
            "tiers": {i: cfg.ladder[s.step][0] for i, s in states.items()},
            "merged_sha256": sha256_json(dict(merged.files)), "verdict": "pass" if passed else "block",
            "blamed": list(blamed), "blame_rule": rule, "blame_basis": basis, "gate_seq": gseq,
            "conflicts": [list(c) for c in merged.conflicts]})
        r = h.evaluate(sub, ctx)
        if not passed and r is not None:
            h.emit("outcome.scored", "scorer", {"gate_seq": gseq, "task_id": ex.id, "solved": bool(r.solved),
                                                "log_task_id": tid, "arm": LANE_ARM, "replay": "merged"})
        rounds.append({"round": rnd, "passed": passed, "solved": bool(r and r.solved), "top_step": top,
                       "evidence": dict(r.evidence) if r else {}, "sub": sub, "ctx": ctx})

        if rnd == 0:
            _shadow(h, ex, ts, split, tid, plan, plan_id, states, ctx)
            _derived(h, FIRST_ARM, ex, split, tid, rounds[0], "first", passed)

        if passed:
            final = rounds[-1]
            break
        stuck = [i for i in blamed if states[i].step + 1 >= len(cfg.ladder)]
        if stuck or rnd >= max_rounds:
            final = rounds[-1]
            break
        feedback = "\n".join(f"[{x.gate}] {x.reason}" for x in results if x.verdict != "pass")
        for i in blamed:
            st = states[i]
            h.emit("gate.decided", "kernel", {"task_id": st.task_id, "attempt": st.step + 1, "verdict": "block",
                                              "gate": "lane.blame", "reason": feedback[: h.config.feedback_chars],
                                              "merge_gate_seq": gseq, "blame_rule": rule, "results": []})
            st.last_feedback = feedback
            nxt = cfg.ladder[st.step + 1]
            h.emit("route.decided", "kernel", {
                "task_id": st.task_id, "arm": LANE_ARM, "decision": "lane_escalation", "action": nxt[1],
                "propensities": {nxt[1]: 1.0}, "router_hash": sha256_json({"ladder": [list(x) for x in cfg.ladder]}),
                "features": h.features(ex, ts, attempt=2, submission=st.sub, results=results, contract=contract),
                "step": nxt[0], "rng_seed": 0})
        escalated = blamed
        rnd += 1

    passed = final["passed"]
    for st in states.values():
        h.emit("gate.decided", "kernel", {"task_id": st.task_id, "attempt": st.step + 1,
                                          "verdict": "pass" if passed else "block",
                                          "gate": "lane.merged" if passed else "lane.unsolved",
                                          "reason": "the merged change passed every gate" if passed else
                                          "the task ended without a passing merge", "results": []})
    sonnet_idx = next((n for n, (s, _, _) in enumerate(cfg.ladder) if s == "sonnet"), len(cfg.ladder) - 1)
    upto = [x for x in rounds if x["top_step"] <= sonnet_idx]
    if upto:
        pick = next((x for x in upto if x["passed"]), upto[-1])
        _derived(h, SONNET_ARM, ex, split, tid, pick, "sonnet", pick["passed"])
    solved = bool(final["solved"]) and passed
    h.emit("outcome.scored", "scorer", {
        "arm": LANE_ARM, "task_id": ex.id, "solved": solved, "shipped": passed, "log_task_id": tid, "split": split,
        "model": "lanes", "evidence": final["evidence"], "gate_passed": passed, "rounds": len(rounds), "k": plan.k,
        "plan_id": plan_id, "fallback": plan.fallback,
        "tiers": {i: cfg.ladder[s.step][0] for i, s in states.items()}})
    return solved


def _derived(h, arm: str, ex: ProExercise, split: str, tid: str, rec: Mapping[str, Any], merge: str, shipped: bool) -> None:
    h.emit("outcome.scored", "scorer", {
        "arm": arm, "derived_from": LANE_ARM, "merge": merge, "task_id": ex.id,
        "solved": bool(rec["solved"]) and shipped, "shipped": shipped, "log_task_id": tid, "split": split,
        "model": "lanes", "evidence": dict(rec["evidence"]), "gate_passed": rec["passed"], "round": rec["round"]})


def _shadow(h, ex: ProExercise, ts: ProTestSplit, split: str, tid: str, plan: LanePlan, plan_id: str,
            states: Mapping[str, _LaneState], ctx: TaskContext) -> bool:
    """La-shadow: the first replies joined with no contracts, scored once on the score tests."""
    replies = {i: s.first.response for i, s in states.items()}
    m = merge_shadow(plan, replies, ex.context.files)
    clashes = [Clash("scope_violation", (o["lane"],), {"mode": "shadow", "action": "applied", **o})
               for o in m.out_of_scope]
    clashes += merge_conflicts(m.conflicts, "shadow")
    clashes += [Clash("overwrite", (x["by"], *x["of"]), {"mode": "shadow", "reverted": True, **x}) for x in m.reverted]
    _emit_clashes(h, tid, plan_id, clashes, 0, "shadow")
    sub = Submission(f"{tid}:shadow", m.files)
    r = h.evaluate(sub, ctx)
    h.emit("merge.decided", "kernel", {
        "task_id": tid, "plan_id": plan_id, "round": 0, "mode": "shadow",
        "lanes": {i: getattr(s.first, "seq", None) for i, s in states.items()},
        "merged_sha256": sha256_json(dict(m.files)), "verdict": "pass", "gated": False,
        "files": sorted(m.files), "errors": list(m.errors)[:50]})
    h.emit("outcome.scored", "scorer", {
        "arm": SHADOW_ARM, "derived_from": LANE_ARM, "merge": "shadow", "task_id": ex.id, "solved": bool(r.solved),
        "shipped": True, "log_task_id": tid, "split": split, "model": "lanes", "evidence": dict(r.evidence)})
    return bool(r.solved)


# ------------------------------------------------------------------ gold lanes (validation, fakes)


def gold_lane_replies(ex: ProExercise, plan: LanePlan) -> dict[str, str]:
    """Each lane's share of the gold patch as a SEARCH/REPLACE reply (no model sees the gold patch)."""
    blocks = diff_to_blocks(ex.task.patch, ex.context.files)
    return {ln.lane_id: format_blocks([b for b in blocks if b.path in ln.files]) for ln in plan.lanes}


def conflicting_reply(ex: ProExercise, plan: LanePlan, lane_id: str) -> str | None:
    """``lane_id``'s gold reply plus one out-of-scope block that rewrites another lane's file: the
    other lane's first gold block with its REPLACE text replaced by a syntax-breaking line. The
    governed merge must reject it; the shadow merge applies it."""
    from .outline import JS_EXT, PY_EXT

    blocks = diff_to_blocks(ex.task.patch, ex.context.files)
    others = [b for b in blocks if b.search and b.path not in plan.lane(lane_id).files]
    other = next((b for b in others if b.path.endswith((*PY_EXT, *JS_EXT))), None)  # a code file, so it breaks
    if other is None:
        return None
    mine = [b for b in blocks if b.path in plan.lane(lane_id).files]
    bad = replace(other, replace=other.search + ("raise SystemExit('conflict')  # (\n" if other.path.endswith(".py")
                                                 else "throw new Error('conflict'); (\n"), index=len(mine) + 1)
    return format_blocks([*mine, bad])


def gold_lane_check(ex: ProExercise, plan: LanePlan, score) -> dict[str, Any]:
    """The plan's $0 gold-lane check: push each lane's gold blocks through the lane applier, the
    governed merge, T0.interfaces and the shadow merge; then a conflicting lane (an out-of-scope block
    breaking another lane's file) through both merges. ``score(files) -> (reward, statuses)``.
    Returns a row with ``ok``: governed and shadow both score 1, T0.interfaces passes, and the
    conflicting block is rejected by the governed merge (which still scores 1). The board is seeded
    from the task's interface field exactly as in a run."""
    plan = replace(plan, interfaces=seed_interfaces(plan.interfaces, task_interfaces(ex.task.interface, ex.solution_files),
                                                    plan.lanes))
    replies = gold_lane_replies(ex, plan)
    subs = {}
    errors = []
    for ln in plan.lanes:
        parsed = parse_edits(replies[ln.lane_id])
        applied = apply_edits(parsed.blocks, ex.context.files, scope=lane_scope(ln))
        errors += list(parsed.errors + applied.errors)
        subs[ln.lane_id] = Submission(ln.lane_id, applied.files)
    gov = merge_governed(plan, subs)
    iface = interface_check(plan, ex.context.files, gov.files)
    shadow = merge_shadow(plan, replies, ex.context.files)
    row: dict[str, Any] = {"k": plan.k, "lanes": {ln.lane_id: list(ln.files) for ln in plan.lanes},
                           "lane_edit_errors": errors, "governed_files": len(gov.files),
                           "interfaces_checked": iface.evidence.get("checked", []),
                           "interfaces_verdict": iface.verdict, "interfaces_reason": iface.reason,
                           "governed_equals_shadow": gov.files == shadow.files}
    row["governed_reward"] = score(gov.files)
    row["shadow_reward"] = row["governed_reward"] if gov.files == shadow.files else score(shadow.files)
    bad_lane = plan.lanes[0].lane_id
    bad = conflicting_reply(ex, plan, bad_lane) if plan.k > 1 else None
    if bad is not None:
        ln = plan.lane(bad_lane)
        parsed = parse_edits(bad)
        applied = apply_edits(parsed.blocks, ex.context.files, scope=lane_scope(ln))
        rejected = [e for e in applied.errors if ": rejected, " in e]
        gov2 = merge_governed(plan, {**subs, bad_lane: Submission(bad_lane, applied.files)})
        sh2 = merge_shadow(plan, {**replies, bad_lane: bad}, ex.context.files)
        row["conflict"] = {"rejected_by_governed": len(rejected), "governed_unchanged": gov2.files == gov.files,
                           "shadow_out_of_scope_applied": len(sh2.out_of_scope),
                           "shadow_reward": score(sh2.files)}
    ok = (not errors and row["governed_reward"] == 1 and row["shadow_reward"] == 1 and iface.verdict == "pass")
    if bad is not None:
        c = row["conflict"]
        ok = ok and c["rejected_by_governed"] >= 1 and c["governed_unchanged"]
    row["ok"] = ok
    return row


def lane_prompt_tokens(ex: ProExercise, ts: ProTestSplit, plan: LanePlan) -> dict[str, int]:
    """Estimated tokens (characters / 3) of each lane's prompt under ``plan``."""
    return {ln.lane_id: math.ceil(len(LANE_SYSTEM + lane_prompt(ex, ts, plan, ln)) / CHARS_PER_TOKEN) for ln in plan.lanes}


# ------------------------------------------------------------------ pre-flight


@dataclass(frozen=True)
class LaneAssumptions:
    """The plan's section 5.2 expected-scenario parameters (measured where marked, else assumed)."""

    plan_out: int = 4000  # planner output tokens (assumed; D's brief measured at 874)
    haiku_intercept: float = 2697.0  # Haiku output fit: intercept + slope x gold lines per lane (measured)
    haiku_slope: float = 12.41
    opus_intercept: float = 1137.0  # Opus-low output fit (measured); Sonnet = 1.5 x this (assumed)
    opus_slope: float = 5.05
    sonnet_factor: float = 1.5
    edit_repair_rate: float = 0.08  # measured on D's Haiku worker
    edit_repair_out: int = 2041  # measured
    p_block: float = 0.45  # first merge blocked (assumed)
    blamed: float = 1.3  # lanes blamed per block (assumed)
    q_opus: float = 0.45  # share of Sonnet-escalated lanes that reach Opus (assumed)
    retry_extra: int = 3000  # retry prompt growth: the previous diff and the feedback (assumed)
    escalation_repair: float = 1.1  # edit-repair allowance on escalated requests (assumed)


@dataclass(frozen=True)
class LanePreflightRow:
    task_id: str
    k: int
    planner_input: int  # counted
    lane_inputs: tuple[int, ...]  # counted, by-file split
    gold_lines: int
    expected: float
    worst: float
    by_part: Mapping[str, float]


def lane_preflight(h, ex: ProExercise, ts: ProTestSplit, count_tokens, cfg: LaneConfig | None = None, *, k: int = 3,
                   a: LaneAssumptions | None = None, table: str | None = None) -> LanePreflightRow:
    """Counted (free ``count_tokens``) planner prompt and lane prompts under a naive by-file split
    of ``k`` lanes, priced with the plan's cost model (expected), and the worst case: the planner
    and its repair at ``max_tokens``, every lane through every ladder step at ``max_tokens`` with an
    edit repair each."""
    from ..pricing import DEFAULT_TABLE, PER_MTOK, PRICE_TABLES
    from .swepro import gold_size

    cfg = cfg or LaneConfig()
    a = a or LaneAssumptions()
    prices = PRICE_TABLES[table or DEFAULT_TABLE]

    def usd(model: str, n_in: float, n_out: float) -> float:
        inp, outp = prices[model].rates(int(n_in))
        return (n_in * inp + n_out * outp) / PER_MTOK

    plan = by_file_plan(ex, k)
    p_in = int(count_tokens(model=cfg.planner_model, system=h.lead_system,
                            messages=[Message("user", _planner_context(h, ex, ts, cfg.max_lanes))]))
    lane_in = tuple(int(count_tokens(model=HAIKU, system=LANE_SYSTEM,
                                     messages=[Message("user", lane_prompt(ex, ts, plan, ln, cfg.outline_max_chars))]))
                    for ln in plan.lanes)
    kk = len(lane_in)
    lines = gold_size(ex.task)[1]
    per = lines / max(kk, 1)
    h_out = a.haiku_intercept + a.haiku_slope * per
    o_out = a.opus_intercept + a.opus_slope * per
    mean_in = sum(lane_in) / max(kk, 1)
    nb = a.p_block * a.blamed
    parts = {
        "planner": usd(cfg.planner_model, p_in, a.plan_out),
        "lanes": sum(usd(HAIKU, n, h_out) for n in lane_in),
        "edit_repairs": sum(a.edit_repair_rate * usd(HAIKU, n + h_out, a.edit_repair_out) for n in lane_in),
        "haiku_repair_round": nb * usd(HAIKU, mean_in + a.retry_extra, h_out),
        "sonnet": nb * usd(SONNET, mean_in + a.retry_extra, o_out * a.sonnet_factor) * a.escalation_repair,
        "opus": nb * a.q_opus * usd(OPUS, mean_in + a.retry_extra + 1000, o_out) * a.escalation_repair,
    }
    mt, pmt = cfg.lane_max_tokens, cfg.planner_max_tokens
    worst = usd(cfg.planner_model, p_in, pmt) + usd(cfg.planner_model, p_in + pmt, pmt)
    for n in lane_in:
        for _, model, _ in cfg.ladder:
            n2 = n + a.retry_extra
            worst += usd(model, n2, mt) + usd(model, n2 + mt, mt)
    return LanePreflightRow(ex.id, kk, p_in, lane_in, lines, sum(parts.values()), worst,
                            {key: round(v, 6) for key, v in parts.items()})
