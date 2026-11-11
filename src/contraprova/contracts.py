"""Lane contracts: what a worker may touch, checked before anything is merged.

A worker declares nothing itself. The orchestrator issues a contract, and the
gate rejects any submission that writes outside it. Test files are protected
by default, because coding agents are known to edit tests to make them pass.
"""

from __future__ import annotations

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
