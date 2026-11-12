"""Coordination-failure detectors for Contract Lanes (docs/lanes-plan.md section 2.4).

Every detector is deterministic and code-level: it reads the lane plan, the lanes' parsed edit
blocks and files, the merges and the verifier output the kernel already holds. None calls a model.
Each returns :class:`Clash` records; ``run_la`` writes one ``coordination.detected`` event per
clash. The detectors are heuristics that have not been validated against human labels.

| kind | detector | MAST |
|---|---|---|
| ``overlapping_scope`` | two lanes of the planner's raw plan claim the same file | 1.2 |
| ``scope_violation`` | a lane block outside its files (rejected in La, applied in the shadow merge) | 1.2 |
| ``interface_mismatch`` | T0.interfaces fails; a lane uses a symbol no lane provides and the base does not define; or the test output shows ImportError, AttributeError, a TypeError on a call, "is not a function" or "Cannot find module" next to a frozen symbol | 2.4, 1.1 |
| ``interface_change_request`` | a lane posts ``INTERFACE REQUEST:`` | 2.4 |
| ``overwrite`` | two lanes create the same file, two lanes change the same file, or (shadow) a later lane's block removes lines another lane added ("reverted") | 2.5 |
| ``duplicated_work`` | the same new top-level symbol is defined by two lanes | 1.3 |
| ``unattributable_failure`` | a gate block that blame had to assign to every lane (rule 3) | 3.2 |
| ``wasted_lane`` | a lane changed nothing, or all its blocks were rejected or dropped | n/a |
| ``regression_after_escalation`` | an escalated round breaks a visible test that passed in the round before | 2.5 |
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .outline import JS_EXT, PY_EXT, outline_file

CLASH_KINDS: tuple[str, ...] = (
    "overlapping_scope", "scope_violation", "interface_mismatch", "interface_change_request", "overwrite",
    "duplicated_work", "unattributable_failure", "wasted_lane", "regression_after_escalation",
)
MAST: dict[str, str] = {
    "overlapping_scope": "1.2", "scope_violation": "1.2", "interface_mismatch": "2.4", "interface_change_request": "2.4",
    "overwrite": "2.5", "duplicated_work": "1.3", "unattributable_failure": "3.2", "wasted_lane": "",
    "regression_after_escalation": "2.5",
}
# Substrings of test output that point at a broken cross-lane interface (checked next to a frozen symbol).
INTERFACE_MARKERS: tuple[str, ...] = ("ImportError", "ModuleNotFoundError", "AttributeError", "TypeError",
                                      "is not a function", "Cannot find module", "is not defined",
                                      "is not exported", "has no exported member")


@dataclass(frozen=True)
class Clash:
    kind: str
    lanes: tuple[str, ...]
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def mast(self) -> str:
        return MAST.get(self.kind, "")

    def payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "lanes": list(self.lanes), "evidence": dict(self.evidence), "mast": self.mast}


# ------------------------------------------------------------------ names defined in code


_JS_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:function\*?|class|interface|enum|type|namespace)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^(?:export\s+)?(?:declare\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function|\()"),
    re.compile(r"^(?:static\s+|async\s+|get\s+|set\s+|public\s+|private\s+|protected\s+|readonly\s+)*"
               r"([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("),
    re.compile(r"^([A-Za-z_$][\w$]*)\s*:"),
    re.compile(r"exports\.([A-Za-z_$][\w$]*)\s*="),
)
_JS_SKIP = frozenset({"if", "for", "while", "switch", "catch", "return", "function", "constructor", "super"})


def python_names(source: str, top_level_only: bool = False) -> set[str] | None:
    """Names a Python module defines (functions, classes, methods, assigned module names), or None
    if it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    out: set[str] = set()
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and (top_level_only or node in tree.body):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def js_names(path: str, source: str, top_level_only: bool = False) -> set[str]:
    out: set[str] = set()
    for e in outline_file(path, source):
        if top_level_only and e.depth > 0:
            continue
        text = e.text.strip()
        for pat in _JS_NAME_PATTERNS:
            for m in pat.finditer(text):
                if m.group(1) not in _JS_SKIP:
                    out.add(m.group(1))
    return out


def defined_names(files: Mapping[str, str], top_level_only: bool = False) -> dict[str, set[str]]:
    """path -> names defined there (Python by ``ast``, JavaScript/TypeScript from the outline);
    files of other types, or Python that does not parse, are left out."""
    out: dict[str, set[str]] = {}
    for path, src in files.items():
        if path.endswith(PY_EXT):
            names = python_names(src, top_level_only)
            if names is not None:
                out[path] = names
        elif path.endswith(JS_EXT):
            out[path] = js_names(path, src, top_level_only)
    return out


def last_component(symbol: str) -> str:
    """``qtutils.interpolate_color`` -> ``interpolate_color``; ``Cls#method()`` -> ``method``."""
    s = re.split(r"[(\s<]", symbol.strip().strip("`"), maxsplit=1)[0]
    parts = [p for p in re.split(r"[.#:]+", s) if p]
    return parts[-1] if parts else s


# ------------------------------------------------------------------ detectors


def overlapping_scopes(raw_lanes: Sequence[tuple[str, Sequence[str]]]) -> list[Clash]:
    """Pairs of lanes in the planner's raw output that claim the same file."""
    out = []
    for i, (a, fa) in enumerate(raw_lanes):
        for b, fb in raw_lanes[i + 1:]:
            both = sorted(set(fa) & set(fb))
            if both:
                out.append(Clash("overlapping_scope", (a, b), {"files": both}))
    return out


def unresolved_uses(lanes: Sequence[Any], frozen: Mapping[str, str], base_names: Iterable[str]) -> list[Clash]:
    """A lane uses a symbol that no other lane provides, the board does not freeze and the base
    files do not define (an interface the plan left dangling)."""
    provided = {last_component(s) for ln in lanes for s in ln.exports} | {last_component(s) for s in frozen}
    known = provided | set(base_names)
    out = []
    for ln in lanes:
        missing = sorted({last_component(s) for s in ln.imports} - known)
        if missing:
            out.append(Clash("interface_mismatch", (ln.lane_id,), {"stage": "plan", "unresolved": missing}))
    return out


def lane_round_clashes(lane_id: str, sub: Any, requests: Sequence[Mapping[str, str]]) -> list[Clash]:
    """Scope violations (rejected blocks), interface change requests and a wasted lane for one
    lane submission (a ``ProSubmission`` with ``info['rejected_blocks']``)."""
    out = []
    rejected = list((getattr(sub, "info", {}) or {}).get("rejected_blocks", []))
    if rejected:
        out.append(Clash("scope_violation", (lane_id,), {"mode": "governed", "action": "rejected",
                                                         "blocks": rejected[:20], "n": len(rejected)}))
    for r in requests:
        out.append(Clash("interface_change_request", (lane_id,), dict(r)))
    if not sub.files:
        why = "all blocks rejected" if rejected and getattr(sub, "blocks", 0) == len(rejected) else "no change applied"
        out.append(Clash("wasted_lane", (lane_id,), {"reason": why, "blocks": getattr(sub, "blocks", 0)}))
    return out


def duplicated_work(lane_files: Mapping[str, Mapping[str, str]], base: Mapping[str, str]) -> list[Clash]:
    """The same new top-level name defined by two lanes (in any of their changed files); dunder
    boilerplate such as ``__metaclass__`` is ignored."""
    added: dict[str, set[str]] = {}
    for lane_id, files in lane_files.items():
        after = defined_names(files, top_level_only=True)
        before = defined_names({p: base[p] for p in files if p in base}, top_level_only=True)
        names: set[str] = set()
        for p, ns in after.items():
            names |= {n for n in ns - before.get(p, set()) if not (n.startswith("__") and n.endswith("__"))}
        added[lane_id] = names
    out = []
    ids = list(added)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            both = sorted(added[a] & added[b])
            if both:
                out.append(Clash("duplicated_work", (a, b), {"symbols": both[:20]}))
    return out


def merge_conflicts(conflicts: Sequence[tuple[str, Sequence[str]]], mode: str) -> list[Clash]:
    """Two lanes changed (or created) the same file: in the governed merge the first lane in plan
    order wins; in the shadow merge the later creation wins."""
    return [Clash("overwrite", tuple(lanes), {"mode": mode, "path": path}) for path, lanes in conflicts]


def added_lines(search: str, replace: str) -> list[str]:
    """Non-trivial lines a block adds (in REPLACE, not in SEARCH)."""
    sm = difflib.SequenceMatcher(None, search.splitlines(), replace.splitlines(), autojunk=False)
    out = []
    for tag, _, _, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            out.extend(ln for ln in replace.splitlines()[j1:j2] if len(ln.strip()) > 3)
    return out


def interface_output_clashes(output: str, frozen: Mapping[str, str], owners: Mapping[str, str],
                             all_lanes: Sequence[str]) -> list[Clash]:
    """Test-output lines that carry an interface marker next to a frozen symbol."""
    out = []
    names = {last_component(s): s for s in frozen}
    seen = set()
    for line in output.splitlines():
        if not any(m in line for m in INTERFACE_MARKERS):
            continue
        for short, sym in names.items():
            if short and re.search(rf"(?<![\w$]){re.escape(short)}(?![\w$])", line) and sym not in seen:
                seen.add(sym)
                lane = owners.get(sym)
                out.append(Clash("interface_mismatch", (lane,) if lane else tuple(all_lanes),
                                 {"stage": "tests", "symbol": sym, "line": line.strip()[:300]}))
    return out


def regressions(prev_passed: Iterable[str], now_failed: Iterable[str], escalated: Sequence[str]) -> list[Clash]:
    broke = sorted(set(prev_passed) & set(now_failed))
    return [Clash("regression_after_escalation", tuple(escalated), {"tests": broke[:20], "n": len(broke)})] if broke else []
