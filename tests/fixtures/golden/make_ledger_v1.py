"""Regenerate ``ledger-v1.jsonl``, the cross-implementation golden log.

    python tests/fixtures/golden/make_ledger_v1.py

The file is a contract: hypotaxis (and any other implementation) copies it and must reproduce
every line and hash. Existing lines never change. A new event type is appended to ``NEW``
(or a new list) and the file regenerated; a changed line is a breaking change. See README.md.

Only the standard library and ``contraprova.events`` are used. Timestamps are fixed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))

from contraprova.events import GENESIS_HASH, Event  # noqa: E402

OUT = HERE / "ledger-v1.jsonl"
T0 = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)
STEP = timedelta(milliseconds=1500)

OPUS = "orchestrator:anthropic/claude-opus-5-5"
HAIKU_W = "worker:anthropic/claude-haiku-5-5"
SONNET_W = "worker:anthropic/claude-sonnet-5-5"
TID = "D:python/leap"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64

Spec = tuple[str, str, dict]

# The 14 types of contraprova v0.2, with their documented payloads. Floats are kept to values
# whose shortest repr has no exponent (0.5, 1.0), so any JSON writer prints them the same way.
EXISTING: list[Spec] = [
    ("task.created", OPUS, {
        "task_id": TID, "title": "leap year: fix the divisibility rule", "scope": ["src/leap.py"],
        "kind": "code", "max_attempts": 2, "arm": "D", "exercise_id": "python/leap",
        "language": "python", "split": "pilot"}),
    ("route.decided", OPUS, {
        "task_id": TID, "action": "claude-haiku-5-5", "propensities": {"claude-haiku-5-5": 1.0},
        "router_hash": "fixed-cascade", "decision": "initial", "arm": "D",
        "features": {"language": 0.0, "stub_size": 0.5, "attempt": 0.0}}),
    ("task.claimed", HAIKU_W, {"task_id": TID, "worker": HAIKU_W, "attempt": 1}),
    ("usage.recorded", "plugin", {
        "model": "claude-haiku-5-5", "input_tokens": 1200, "output_tokens": 340,
        "cache_read_tokens": 0, "cache_write_5m_tokens": 800, "cache_write_1h_tokens": 0,
        "role": "worker", "arm": "D", "task_id": TID, "request_id": "req_0001"}),
    ("task.submitted", HAIKU_W, {
        "task_id": TID, "attempt": 1, "model": "claude-haiku-5-5", "files": ["src/leap.py"],
        "files_sha256": {"src/leap.py": SHA_A}}),
    ("gate.decided", "gate", {
        "task_id": TID, "gate": "T0.visible_tests", "verdict": "block",
        "reason": "exit code 1 (failed=2); não passou: ação inválida", "attempt": 1,
        "tests_sha256": SHA_B}),
    ("fork.created", "orchestrator", {
        "fork_id": "fork-1", "task_id": TID, "from_seq": 5, "model": "claude-haiku-5-5",
        "role": "control", "seed": 1, "prefix_hash": SHA_C, "branch": "control-1", "arm": "D"}),
    ("fork.created", "orchestrator", {
        "fork_id": "fork-2", "task_id": TID, "from_seq": 5, "model": "claude-sonnet-5-5",
        "role": "treatment", "seed": 2, "prefix_hash": SHA_C, "branch": "treatment", "arm": "D",
        "fork_task_id": "D:python/leap:treatment"}),
    ("route.decided", OPUS, {
        "task_id": TID, "action": "claude-sonnet-5-5", "propensities": {"claude-sonnet-5-5": 1.0},
        "router_hash": "fixed-cascade", "decision": "after_gate_failure", "arm": "D",
        "features": {"language": 0.0, "stub_size": 0.5, "attempt": 1.0}}),
    ("task.claimed", SONNET_W, {"task_id": TID, "worker": SONNET_W, "attempt": 2}),
    ("usage.recorded", "plugin", {
        "model": "claude-sonnet-5-5", "input_tokens": 2100, "output_tokens": 410,
        "cache_read_tokens": 800, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
        "role": "worker", "arm": "D", "task_id": TID, "batch": False}),
    ("task.submitted", SONNET_W, {
        "task_id": TID, "attempt": 2, "model": "claude-sonnet-5-5", "files": ["src/leap.py"],
        "files_sha256": {"src/leap.py": SHA_B}}),
    ("gate.decided", "gate", {
        "task_id": TID, "gate": "T1.private_tests", "verdict": "pass", "reason": "", "attempt": 2,
        "tests_sha256": SHA_B}),
    ("outcome.scored", "scorer", {
        "arm": "D", "task_id": "python/leap", "solved": True, "log_task_id": TID, "shipped": True}),
    ("router.trained", "orchestrator", {
        "prefix_seq": 13, "n_examples": 1, "router_hash": SHA_A,
        "hyperparameters": {"epochs": 100, "l2": 0.5, "lr": 0.5},
        "feature_names": ["language", "stub_size", "attempt"],
        "actions": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"], "schema_version": 1}),
    ("task.created", OPUS, {
        "task_id": "La:inst-1", "title": "aφesis: split into two lanes", "scope": ["src/a.py", "src/b.py"],
        "kind": "code", "max_attempts": 1, "arm": "La"}),
    ("lane.planned", OPUS, {
        "task_id": "La:inst-1", "plan_id": "plan-1", "model": "claude-opus-5-5", "k": 2,
        "lanes": [
            {"lane_id": "a", "allowed": ["src/a.py"], "new_files": [], "goal": "implement f",
             "symbols": ["f"], "exports": ["a.f"], "imports": [], "tests": ["tests/test_a.py"]},
            {"lane_id": "b", "allowed": ["src/b.py"], "new_files": [], "goal": "use f",
             "symbols": ["g"], "exports": ["b.g"], "imports": ["a.f"], "tests": ["tests/test_b.py"]},
        ],
        "plan_sha256": SHA_A}),
    ("interface.frozen", OPUS, {
        "task_id": "La:inst-1", "plan_id": "plan-1",
        "interfaces": {"a.f": "def f(x: int) -> int"}, "interfaces_sha256": SHA_B}),
    ("interface.requested", "worker:anthropic/claude-haiku-5-5", {
        "task_id": "La:inst-1:b", "plan_id": "plan-1", "lane_id": "b", "symbol": "a.f",
        "requested_signature": "def f(x: int, y: int) -> int", "status": "rejected",
        "reason": "the frozen interface is not negotiable"}),
    ("merge.decided", "orchestrator", {
        "task_id": "La:inst-1", "plan_id": "plan-1", "round": 1, "mode": "governed",
        "lanes": {"a": 12, "b": 13}, "merged_sha256": SHA_C, "verdict": "pass"}),
    ("coordination.detected", "orchestrator", {
        "task_id": "La:inst-1", "plan_id": "plan-1", "kind": "overlapping_scope",
        "lanes": ["a", "b"], "round": 1, "mode": "plan", "evidence": {"paths": ["src/shared.py"]}}),
]

# The 14 types the hypotaxis harness adds (hypotaxis docs/LEDGER.md). Appended after EXISTING, so
# the lines above keep their bytes and hashes.
NEW: list[Spec] = [
    ("session.opened", "plugin", {
        "session_id": "sess-0001", "mode": "governed", "cwd": "/work/demo", "hermes_version": "0.9.0",
        "plugin_version": "0.1.0", "profile": "default", "gate_command_sha256": SHA_A,
        "price_table": "anthropic-2026-11",
        "ladder": {"same_tier_scope_retry": True, "tiers": [
            {"name": "cheap", "model": "anthropic/claude-haiku-5-5", "effort": "medium", "max_tokens": 8000, "retries": 0},
            {"name": "strong", "model": "anthropic/claude-opus-5-5", "effort": "medium", "max_tokens": 8000, "retries": 0}]}}),
    ("tool.called", "plugin", {
        "tool_call_id": "call_1", "tool": "edit_file", "status": "ok", "duration_ms": 12,
        "args_sha256": SHA_B, "task_id": TID}),
    ("tool.called", "plugin", {
        "tool_call_id": "call_2", "tool": "shell", "status": "blocked", "duration_ms": 0,
        "error_code": "hx.policy.denied", "args_sha256": SHA_C}),
    ("ship.applied", "plugin", {
        "ship_id": "ship-1", "task_id": TID, "journal_id": "journal-1", "files": [
            {"path": "src/leap.py", "sha256_before": SHA_A, "sha256_after": SHA_B},
            {"path": "src/new.py", "sha256_before": None, "sha256_after": SHA_C}]}),
    ("checkpoint.committed", "plugin", {
        "ship_id": "ship-1", "ref": "refs/hypotaxis/ship-1", "commit": "d" * 40}),
    ("ship.restored", "user", {"ship_id": "ship-1", "reason": "undo"}),
    ("approval.requested", "plugin", {
        "approval_id": "ap-1", "kind": "shell", "summary": "run pytest -q", "rule": "ask:shell",
        "task_id": TID, "teammate": "alice"}),
    ("approval.decided", "user", {
        "approval_id": "ap-1", "decision": "approve", "decided_by": "user", "surface": "desktop"}),
    ("blank.opened", "orchestrator:anthropic/claude-opus-5-5", {
        "blank_id": "bl-1", "question": "which date format?", "options": ["iso", "br"], "blocks": True,
        "task_id": TID}),
    ("blank.filled", "user", {"blank_id": "bl-1", "answer": "iso", "decided_by": "user"}),
    ("blank.opened", "orchestrator:anthropic/claude-opus-5-5", {
        "blank_id": "bl-2", "question": "rename the module?", "options": [], "blocks": False}),
    ("blank.lapsed", "plugin", {"blank_id": "bl-2"}),
    ("feedforward.set", "plugin", {
        "turn_id": "turn-1", "decision": "test_command", "value": "pytest -q", "source": "config"}),
    ("mode.changed", "user", {"from": "governed", "to": "ungoverned"}),
    ("usage.recorded", "plugin", {
        "model": "gpt-5", "provider": "openai", "input_tokens": 900, "output_tokens": 120,
        "cache_read_tokens": 300, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
        "reasoning_tokens": 64, "latency_ms": 850, "request_id": "resp_0001", "usage_schema": 2,
        "subscription": False, "role": "worker"}),
    ("budget.blocked", "plugin", {"cap_usd": 2.5, "price_table": "anthropic-2026-11"}),
    ("session.closed", "plugin", {"session_id": "sess-0001", "reason": "user_quit"}),
]


def specs() -> list[Spec]:
    return EXISTING + NEW


def build(items: list[Spec] | None = None) -> list[Event]:
    """The events of the golden log, chained from the genesis hash, with fixed timestamps."""
    events: list[Event] = []
    prev = GENESIS_HASH
    for i, (type_, actor, payload) in enumerate(specs() if items is None else items):
        ts = (T0 + i * STEP).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        ev = Event.create(i, type_, actor, ts, payload, prev)
        events.append(ev)
        prev = ev.hash
    return events


def render(events: list[Event]) -> bytes:
    """The file's bytes: one canonical JSON line per event, LF line ends, UTF-8."""
    return "".join(ev.to_json() + "\n" for ev in events).encode("utf-8")


if __name__ == "__main__":
    data = render(build())
    OUT.write_bytes(data)
    lines = data.count(b"\n")
    print(f"wrote {OUT} ({len(data)} bytes, {lines} events)")
