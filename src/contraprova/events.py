"""Event records and the hash chain.

Every fact contraprova knows is an event in an append-only log. Each event
commits to the one before it through ``prev_hash``, so editing, dropping or
reordering any past event breaks every hash after it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

GENESIS_HASH = "0" * 64

# The 14 types of contraprova v0.2: the study and the engine (docs/events.md).
CORE_EVENT_TYPES = frozenset(
    {
        # task lifecycle
        "task.created",
        "task.claimed",
        "task.submitted",
        "gate.decided",
        # routing and escalation
        "route.decided",
        "router.trained",
        "fork.created",
        # measurement
        "usage.recorded",
        "outcome.scored",
        # Contract Lanes (docs/lanes-plan.md): the plan, the interface board, merges, clashes
        "lane.planned",
        "interface.frozen",
        "interface.requested",
        "merge.decided",
        "coordination.detected",
    }
)

# Types a desktop harness writes around the task lifecycle (hypotaxis docs/LEDGER.md). They never
# change a task's state; they are validated for their required fields only (``payload_problems``).
HARNESS_EVENT_TYPES = frozenset(
    {
        "session.opened",
        "session.closed",
        "tool.called",
        "ship.applied",
        "ship.restored",
        "checkpoint.committed",
        "approval.requested",
        "approval.decided",
        "blank.opened",
        "blank.filled",
        "blank.lapsed",
        "feedforward.set",
        "mode.changed",
        "budget.blocked",
    }
)

EVENT_TYPES = CORE_EVENT_TYPES | HARNESS_EVENT_TYPES

# Required payload keys of the harness types. Other keys are allowed and ignored.
REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = {
    "session.opened": ("session_id", "mode", "cwd", "hermes_version", "plugin_version"),
    "session.closed": ("session_id", "reason"),
    "tool.called": ("tool_call_id", "tool", "status", "duration_ms"),
    "ship.applied": ("ship_id", "files"),
    "ship.restored": ("ship_id", "reason"),
    "checkpoint.committed": ("ship_id", "ref", "commit"),
    "approval.requested": ("approval_id", "kind", "summary", "rule"),
    "approval.decided": ("approval_id", "decision", "decided_by", "surface"),
    "blank.opened": ("blank_id", "question", "options", "blocks"),
    "blank.filled": ("blank_id", "answer", "decided_by"),
    "blank.lapsed": ("blank_id",),
    "feedforward.set": ("turn_id", "decision", "value", "source"),
    "mode.changed": ("from", "to"),
    "budget.blocked": ("cap_usd", "price_table"),
}

# Closed value sets, as listed in docs/events.md.
ALLOWED_VALUES: Mapping[tuple[str, str], frozenset[str]] = {
    ("session.opened", "mode"): frozenset({"governed", "ungoverned"}),
    ("tool.called", "status"): frozenset({"ok", "error", "blocked"}),
    ("approval.decided", "decision"): frozenset({"approve", "deny", "timeout"}),
    ("feedforward.set", "source"): frozenset({"config", "convention", "model", "user"}),
}

SHIP_FILE_KEYS = ("path", "sha256_before", "sha256_after")


def payload_problems(type_: str, payload: Mapping[str, Any]) -> list[str]:
    """What is wrong with a harness event's payload; empty if it is acceptable.

    Checks required keys, the closed value sets and the shape of ``ship.applied`` ``files``.
    Types outside ``HARNESS_EVENT_TYPES`` are never checked here (their payloads are unchanged
    from contraprova v0.2).
    """
    required = REQUIRED_FIELDS.get(type_)
    if required is None:
        return []
    problems = [f"missing {key!r}" for key in required if key not in payload]
    for (t, key), allowed in ALLOWED_VALUES.items():
        if t == type_ and key in payload and payload[key] not in allowed:
            problems.append(f"{key} must be one of {sorted(allowed)}, got {payload[key]!r}")
    if type_ == "blank.opened" and "blocks" in payload:
        blocks = payload["blocks"]
        if not isinstance(blocks, list) or not all(isinstance(b, str) for b in blocks):
            problems.append("blocks must be a list of strings (the steps that wait for the answer)")
    if type_ == "ship.applied" and "files" in payload:
        files = payload["files"]
        if not isinstance(files, list) or not all(isinstance(f, Mapping) and all(k in f for k in SHIP_FILE_KEYS) for f in files):
            problems.append(f"files must be a list of objects with {', '.join(SHIP_FILE_KEYS)}")
    return problems


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_hash(seq: int, type_: str, actor: str, ts: str, payload: Mapping[str, Any], prev_hash: str) -> str:
    body = {"seq": seq, "type": type_, "actor": actor, "ts": ts, "payload": payload, "prev_hash": prev_hash}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    actor: str
    ts: str
    payload: Mapping[str, Any]
    prev_hash: str
    hash: str

    @classmethod
    def create(cls, seq: int, type_: str, actor: str, ts: str, payload: Mapping[str, Any], prev_hash: str) -> "Event":
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type_!r}")
        # Round-trip through canonical JSON so the stored payload is exactly what was hashed.
        payload = json.loads(canonical_json(dict(payload)))
        return cls(seq, type_, actor, ts, payload, prev_hash, compute_hash(seq, type_, actor, ts, payload, prev_hash))

    def to_json(self) -> str:
        return canonical_json(
            {
                "seq": self.seq,
                "type": self.type,
                "actor": self.actor,
                "ts": self.ts,
                "payload": self.payload,
                "prev_hash": self.prev_hash,
                "hash": self.hash,
            }
        )

    @classmethod
    def from_json(cls, line: str) -> "Event":
        d = json.loads(line)
        return cls(d["seq"], d["type"], d["actor"], d["ts"], d["payload"], d["prev_hash"], d["hash"])


class ChainError(Exception):
    """The log does not form a valid hash chain."""


def verify_chain(events: list[Event]) -> None:
    """Raise ChainError unless ``events`` is a gap-free, untampered chain."""
    prev = GENESIS_HASH
    for i, ev in enumerate(events):
        if ev.seq != i:
            raise ChainError(f"event {i}: expected seq {i}, found {ev.seq}")
        if ev.prev_hash != prev:
            raise ChainError(f"event {i}: prev_hash does not match the previous event")
        if ev.type not in EVENT_TYPES:
            raise ChainError(f"event {i}: unknown type {ev.type!r}")
        expected = compute_hash(ev.seq, ev.type, ev.actor, ev.ts, ev.payload, ev.prev_hash)
        if ev.hash != expected:
            raise ChainError(f"event {i}: content does not match its hash")
        prev = ev.hash
