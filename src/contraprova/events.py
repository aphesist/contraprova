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

EVENT_TYPES = frozenset(
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
