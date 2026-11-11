import json

import pytest

from contraprova.events import ChainError
from contraprova.log import EventLog
from contraprova.projections import InvalidTransition, is_arm_outcome, outcomes, project_tasks

TS = "2026-11-11T00:00:00.000Z"


def new_task(log, tid="t1", **extra):
    return log.append("task.created", "orchestrator", {"task_id": tid, "title": "x", "scope": ["src/a.py"], **extra}, TS)


def run_once(log, tid, verdict):
    log.append("task.claimed", "worker-1", {"task_id": tid}, TS)
    log.append("task.submitted", "worker-1", {"task_id": tid, "files": ["src/a.py"]}, TS)
    log.append("gate.decided", "gate", {"task_id": tid, "gate": "tests", "verdict": verdict}, TS)


def test_happy_path(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    new_task(log)
    run_once(log, "t1", "pass")
    assert log.verify() == 4
    t = project_tasks(log.events())["t1"]
    assert (t.status, t.attempts) == ("done", 1)


def test_done_is_immutable(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    new_task(log)
    run_once(log, "t1", "pass")
    with pytest.raises(InvalidTransition):
        log.append("task.claimed", "worker-2", {"task_id": "t1"}, TS)
    # A fix is a new task that supersedes the finished one.
    new_task(log, "t2", supersedes="t1")
    assert project_tasks(log.events())["t2"].supersedes == "t1"


def test_cannot_supersede_open_task(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    new_task(log)
    with pytest.raises(InvalidTransition):
        new_task(log, "t2", supersedes="t1")


def test_attempt_limit(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    new_task(log, max_attempts=2)
    run_once(log, "t1", "block")
    assert project_tasks(log.events())["t1"].status == "todo"
    run_once(log, "t1", "block")
    assert project_tasks(log.events())["t1"].status == "failed"


def test_rejected_event_is_not_written(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    new_task(log)
    with pytest.raises(InvalidTransition):
        log.append("task.submitted", "worker-1", {"task_id": "t1"}, TS)  # never claimed
    assert log.verify() == 1


def test_tampering_is_detected(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    new_task(log)
    run_once(log, "t1", "block")
    lines = path.read_text().splitlines()
    rec = json.loads(lines[3])
    rec["payload"]["verdict"] = "pass"
    lines[3] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainError):
        log.verify()


def test_deletion_is_detected(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    new_task(log)
    run_once(log, "t1", "pass")
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    with pytest.raises(ChainError):
        log.verify()


def test_unknown_event_type(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    with pytest.raises(ValueError):
        log.append("task.vanished", "x", {}, TS)


# ------------------------------------------------------------------ outcomes


def test_outcomes_skip_fork_outcomes_and_gate_replays(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    new_task(log, "D:ex/a", exercise_id="ex/a")
    run_once(log, "D:ex/a", "block")
    gate_seq = len(log.events()) - 1
    log.append("outcome.scored", "scorer", {"arm": "D", "task_id": "ex/a", "solved": False, "log_task_id": "D:ex/a"}, TS)
    # A fork outcome with an informational parent arm must not replace D's own outcome.
    log.append("outcome.scored", "scorer", {"fork_id": "fork-1", "arm": "D", "task_id": "ex/a", "solved": True}, TS)
    # A gate replay, with and without a task_id.
    log.append("outcome.scored", "scorer", {"gate_seq": gate_seq, "task_id": "ex/a", "solved": True}, TS)
    log.append("outcome.scored", "scorer", {"gate_seq": gate_seq, "solved": True}, TS)
    log.append("outcome.scored", "scorer", {"arm": "B", "task_id": "ex/a", "solved": True}, TS)
    assert outcomes(log.events()) == {("D", "ex/a"): False, ("B", "ex/a"): True}


def test_is_arm_outcome():
    assert is_arm_outcome({"arm": "A", "task_id": "x", "solved": True})
    assert is_arm_outcome({"arm": "A", "task_id": "x", "solved": True, "fork_id": None})
    assert not is_arm_outcome({"fork_id": "fork-1", "task_id": "x", "solved": True})
    assert not is_arm_outcome({"gate_seq": 0, "solved": True})
    assert not is_arm_outcome({"gate_seq": 0, "solved": True, "arm": "D", "task_id": "x"})
