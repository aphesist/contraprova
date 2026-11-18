"""Lane contracts: what a worker may touch, checked before anything is merged.

A worker declares nothing itself. The orchestrator issues a contract, and the
gate rejects any submission that writes outside it. Test files are protected
by default, because coding agents are known to edit tests to make them pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

DEFAULT_PROTECTED = ("tests/*", "*/tests/*", "test_*.py", "*_test.py", "*_test.go", "*.spec.*", "*.test.*")


@dataclass(frozen=True)
class LaneContract:
    lane_id: str
    task_id: str
    allowed: tuple[str, ...]
    protected: tuple[str, ...] = DEFAULT_PROTECTED
    symbols: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Violation:
    path: str
    reason: str  # "out_of_scope" | "protected"


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pat) for pat in patterns)


def check_scope(contract: LaneContract, touched: list[str]) -> list[Violation]:
    """Return every violation in ``touched`` (paths relative to the repo root)."""
    out = []
    for raw in touched:
        path = raw.replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path.startswith("/") or ".." in path.split("/"):
            out.append(Violation(raw, "out_of_scope"))
        elif _matches(path, contract.protected):
            out.append(Violation(raw, "protected"))
        elif not _matches(path, contract.allowed):
            out.append(Violation(raw, "out_of_scope"))
    return out


def overlapping_scopes(contracts: list[LaneContract]) -> list[tuple[str, str]]:
    """Pairs of lanes whose literal allowed paths collide (patterns compared as written)."""
    pairs = []
    for i, a in enumerate(contracts):
        for b in contracts[i + 1 :]:
            if set(a.allowed) & set(b.allowed):
                pairs.append((a.lane_id, b.lane_id))
    return pairs


# ------------------------------------------------------------------ the orchestrator's brief

_BRIEF_LINE = re.compile(r"^\s*[-*]?\s*(scope|test|goal)\s*:\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Brief:
    """An orchestrator's brief, read for its ``scope:``, ``test:`` and ``goal:`` lines.

    ``scope`` is the list of globs (None if there is no ``scope:`` line, which a gate blocks on).
    ``test`` is what the brief *says* the test command is, kept **for reporting only**: no part of the
    kernel runs it. A test command comes from configuration, never from a brief. ``body`` is the text
    with the first of each of those three lines removed.
    """

    scope: list[str] | None
    test: str | None
    goal: str | None
    body: str


def parse_brief(text: str) -> Brief:
    """Read a brief. The first ``scope:``, ``test:`` and ``goal:`` line wins (any case, optionally
    after a ``-`` or ``*`` bullet); backticks around a value are dropped; ``scope`` splits on commas
    and whitespace. Empty values count as absent. This is the same rule as the Claude Code plugin's
    standalone gate script (``config/claude-code/scripts/gate.py``), which a test keeps in step."""
    scope: list[str] | None = None
    test = goal = None
    seen: set[str] = set()
    rest: list[str] = []
    for line in text.splitlines():
        m = _BRIEF_LINE.match(line)
        if not m or m.group(1).lower() in seen:
            rest.append(line)
            continue
        key, value = m.group(1).lower(), m.group(2).strip().strip("`")
        seen.add(key)
        if key == "scope":
            scope = [p.strip().strip("`") for p in re.split(r"[,\s]+", value) if p.strip().strip("`")]
        elif key == "test":
            test = value or None
        else:
            goal = value or None
    return Brief(scope, test, goal, "\n".join(rest).strip("\n"))
