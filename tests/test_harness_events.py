"""The event types a desktop harness adds (docs/events.md, hypotaxis docs/LEDGER.md)."""

import json
from pathlib import Path

import pytest

from contraprova.events import CORE_EVENT_TYPES, EVENT_TYPES, HARNESS_EVENT_TYPES, REQUIRED_FIELDS, payload_problems
from contraprova.log import EventLog
from contraprova.projections import InvalidTransition, project_tasks

TS = "2026-11-18T09:00:00.000Z"
GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "ledger-v1.jsonl"

FULL = {
    "session.opened": {"session_id": "s", "mode": "governed", "cwd": "/w", "hermes_version": "1", "plugin_version": "1"},
    "session.closed": {"session_id": "s", "reason": "quit"},
    "tool.called": {"tool_call_id": "c", "tool": "edit", "status": "ok", "duration_ms": 3},
    "ship.applied": {"ship_id": "sh", "files": [{"path": "a", "sha256_before": None, "sha256_after": "x"}]},
    "ship.restored": {"ship_id": "sh", "reason": "undo"},
    "checkpoint.committed": {"ship_id": "sh", "ref": "refs/x", "commit": "abc"},
    "approval.requested": {"approval_id": "a", "kind": "shell", "summary": "s", "rule": "r"},
    "approval.decided": {"approval_id": "a", "decision": "deny", "decided_by": "user", "surface": "cli"},
    "blank.opened": {"blank_id": "b", "question": "q", "options": [], "blocks": ["update callers"]},
    "blank.filled": {"blank_id": "b", "answer": "x", "decided_by": "user"},
    "blank.lapsed": {"blank_id": "b"},
    "feedforward.set": {"turn_id": "t", "decision": "d", "value": 1, "source": "model"},
    "mode.changed": {"from": "governed", "to": "ungoverned"},
    "budget.blocked": {"cap_usd": 1.5, "price_table": "anthropic-2026-11"},
}


def test_the_fourteen_new_types_are_registered():
    assert HARNESS_EVENT_TYPES == set(FULL) == set(REQUIRED_FIELDS)
    assert len(CORE_EVENT_TYPES) == 14 and len(HARNESS_EVENT_TYPES) == 14
    assert EVENT_TYPES == CORE_EVENT_TYPES | HARNESS_EVENT_TYPES
    assert not CORE_EVENT_TYPES & HARNESS_EVENT_TYPES


@pytest.mark.parametrize("type_", sorted(FULL))
def test_a_complete_payload_is_accepted_and_changes_no_task(tmp_path, type_):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("task.created", "o", {"task_id": "t", "title": "x", "scope": []}, TS)
    before = project_tasks(log.events())
    log.append(type_, "plugin", {**FULL[type_], "extra": "ignored"}, TS)
    assert log.verify() == 2
    assert project_tasks(log.events()) == before


@pytest.mark.parametrize("type_", sorted(FULL))
def test_every_required_key_is_enforced_and_a_rejected_event_is_not_written(tmp_path, type_):
    log = EventLog(tmp_path / "log.jsonl")
    for key in REQUIRED_FIELDS[type_]:
        payload = {k: v for k, v in FULL[type_].items() if k != key}
        assert payload_problems(type_, payload) == [f"missing {key!r}"]
        with pytest.raises(InvalidTransition, match=key):
            log.append(type_, "plugin", payload, TS)
    assert not (tmp_path / "log.jsonl").exists() or log.verify() == 0


@pytest.mark.parametrize(
    "type_, key, bad",
    [
        ("session.opened", "mode", "yolo"),
        ("tool.called", "status", "maybe"),
        ("approval.decided", "decision", "allow"),
        ("feedforward.set", "source", "guess"),
    ],
)
def test_closed_value_sets(type_, key, bad):
    assert payload_problems(type_, {**FULL[type_], key: bad})


@pytest.mark.parametrize("files", ["a", [{"path": "a"}], [1], {"path": "a"}])
def test_ship_files_must_be_a_list_of_complete_entries(files):
    assert payload_problems("ship.applied", {"ship_id": "s", "files": files})
    assert payload_problems("ship.applied", {"ship_id": "s", "files": []}) == []


@pytest.mark.parametrize("blocks", [True, "update callers", [1], [["x"]], {"a": "b"}, None])
def test_blank_blocks_is_a_list_of_strings(blocks):
    assert payload_problems("blank.opened", {**FULL["blank.opened"], "blocks": blocks})
    assert payload_problems("blank.opened", {**FULL["blank.opened"], "blocks": []}) == []


def test_existing_types_are_not_checked_so_old_logs_replay():
    assert payload_problems("usage.recorded", {}) == []
    assert payload_problems("task.created", {"task_id": "t"}) == []


def test_ship_events_naming_a_task_need_that_task(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    for type_ in ("ship.applied", "ship.restored"):
        with pytest.raises(InvalidTransition, match="unknown task"):
            log.append(type_, "plugin", {**FULL[type_], "task_id": "ghost"}, TS)
    log.append("ship.applied", "plugin", FULL["ship.applied"], TS)  # no task_id: fine
    log.append("task.created", "o", {"task_id": "t", "title": "x", "scope": []}, TS)
    log.append("ship.applied", "plugin", {**FULL["ship.applied"], "task_id": "t"}, TS)
    log.append("ship.restored", "plugin", {**FULL["ship.restored"], "task_id": "t"}, TS)
    assert log.verify() == 4


def test_other_harness_events_may_name_unknown_tasks(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("tool.called", "plugin", {**FULL["tool.called"], "task_id": "ghost"}, TS)
    assert log.verify() == 1


def test_a_task_in_flight_is_not_disturbed_by_harness_events(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("task.created", "o", {"task_id": "t", "title": "x", "scope": []}, TS)
    log.append("task.claimed", "w", {"task_id": "t"}, TS)
    for type_, payload in FULL.items():
        log.append(type_, "plugin", payload, TS)
    log.append("task.submitted", "w", {"task_id": "t"}, TS)
    log.append("gate.decided", "gate", {"task_id": "t", "gate": "g", "verdict": "pass"}, TS)
    assert project_tasks(log.events())["t"].status == "done"


def test_the_golden_log_has_every_new_type():
    types = {json.loads(line)["type"] for line in GOLDEN.read_text(encoding="utf-8").splitlines()}
    assert HARNESS_EVENT_TYPES <= types
