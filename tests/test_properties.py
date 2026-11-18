"""Property-based tests of the projection.

These are the kernel's formal claims, checked on randomly generated histories:
replay is deterministic, finished tasks never change, and any edit to a stored
event is detected.
"""

import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from contraprova.events import GENESIS_HASH, ChainError, Event, verify_chain
from contraprova.log import EventLog, read_events
from contraprova.projections import (
    TERMINAL,
    InvalidTransition,
    apply_task_event,
    apply_task_event_inplace,
    project_tasks,
)

TS = "2026-11-11T00:00:00.000Z"
TASK_IDS = ["t1", "t2", "t3"]

action = st.one_of(
    st.tuples(st.just("task.created"), st.sampled_from(TASK_IDS), st.none()),
    st.tuples(st.just("task.claimed"), st.sampled_from(TASK_IDS), st.none()),
    st.tuples(st.just("task.submitted"), st.sampled_from(TASK_IDS), st.none()),
    st.tuples(st.just("gate.decided"), st.sampled_from(TASK_IDS), st.sampled_from(["pass", "block"])),
    st.tuples(st.just("usage.recorded"), st.sampled_from(TASK_IDS), st.none()),
)


def payload_for(type_, tid, verdict):
    if type_ == "task.created":
        return {"task_id": tid, "title": tid, "scope": ["src/*"], "max_attempts": 2}
    if type_ == "gate.decided":
        return {"task_id": tid, "gate": "tests", "verdict": verdict}
    if type_ == "usage.recorded":
        return {"task_id": tid, "model": "claude-haiku-5-5", "input_tokens": 10, "output_tokens": 5}
    return {"task_id": tid}


def build_history(actions):
    """Keep only the actions a single writer would accept, exactly as EventLog.append does."""
    events, tasks, prev = [], {}, GENESIS_HASH
    for type_, tid, verdict in actions:
        ev = Event.create(len(events), type_, "actor", TS, payload_for(type_, tid, verdict), prev)
        try:
            tasks = apply_task_event(tasks, ev)
        except InvalidTransition:
            continue
        events.append(ev)
        prev = ev.hash
    return events, tasks


@settings(max_examples=300)
@given(st.lists(action, max_size=60))
def test_replay_is_deterministic(actions):
    events, incremental = build_history(actions)
    assert project_tasks(events) == incremental
    assert project_tasks(events) == project_tasks(list(events))
    verify_chain(events)


@settings(max_examples=300)
@given(st.lists(action, max_size=60))
def test_terminal_tasks_never_change(actions):
    events, _ = build_history(actions)
    tasks, frozen = {}, {}
    for ev in events:
        tasks = apply_task_event(tasks, ev)
        for tid, view in tasks.items():
            if tid in frozen:
                assert view == frozen[tid]
            elif view.status in TERMINAL:
                frozen[tid] = view


@settings(max_examples=300)
@given(st.lists(action, max_size=60))
def test_attempts_never_exceed_limit(actions):
    _, tasks = build_history(actions)
    for t in tasks.values():
        assert t.attempts <= t.max_attempts


@settings(max_examples=200)
@given(st.lists(action, min_size=1, max_size=40), st.data())
def test_any_payload_edit_breaks_the_chain(actions, data):
    events, _ = build_history(actions)
    if not events:
        return
    i = data.draw(st.integers(0, len(events) - 1))
    ev = events[i]
    forged = Event(ev.seq, ev.type, ev.actor, ev.ts, {**ev.payload, "forged": True}, ev.prev_hash, ev.hash)
    try:
        verify_chain(events[:i] + [forged] + events[i + 1 :])
    except ChainError:
        return
    raise AssertionError("edit went undetected")


# ------------------------------------------------------------ incremental projection

harness_action = st.tuples(
    st.sampled_from(["tool.called", "ship.applied", "ship.restored", "session.closed"]),
    st.sampled_from(TASK_IDS + ["ghost"]),
    st.none(),
)


def payload_for_any(type_, tid, verdict):
    if type_ == "tool.called":
        return {"tool_call_id": "c", "tool": "t", "status": "ok", "duration_ms": 1, "task_id": tid}
    if type_ == "ship.applied":
        return {"ship_id": "s", "files": [], "task_id": tid}
    if type_ == "ship.restored":
        return {"ship_id": "s", "reason": "undo", "task_id": tid}
    if type_ == "session.closed":
        return {"session_id": "s", "reason": "quit"}
    return payload_for(type_, tid, verdict)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.one_of(action, harness_action), st.booleans()), max_size=35))
def test_incremental_projection_equals_full_replay(steps):
    """After every append, accepted or rejected, the cached projection is project_tasks(all events).

    ``external`` steps are written by a second writer that shares the file, so the cache goes stale
    and must be rebuilt. A third log fed the same actions the slow way must end up byte-identical.
    """
    with tempfile.TemporaryDirectory() as d:
        path, ref_path = Path(d, "log.jsonl"), Path(d, "ref.jsonl")
        fast, other, ref = EventLog(path, incremental=True), EventLog(path), EventLog(ref_path)
        for (type_, tid, verdict), external in steps:
            payload = payload_for_any(type_, tid, verdict)
            outcomes = []
            for log in ((other if external else fast), ref):
                try:
                    log.append(type_, "actor", payload, TS)
                    outcomes.append(True)
                except InvalidTransition:
                    outcomes.append(False)
            assert outcomes[0] == outcomes[1]
            assert fast.tasks() == project_tasks(read_events(path))
        assert path.read_bytes() == ref_path.read_bytes() if path.exists() else not ref_path.exists()
        assert fast.verify() == len(read_events(path))
        assert fast.tasks() == project_tasks(read_events(path))


@settings(max_examples=200)
@given(st.lists(action, max_size=60))
def test_inplace_application_equals_the_pure_one(actions):
    events, _ = build_history(actions)
    pure, inplace = {}, {}
    for ev in events:
        pure = apply_task_event(pure, ev)
        apply_task_event_inplace(inplace, ev)
        assert inplace == pure


@settings(max_examples=200)
@given(st.lists(action, max_size=40), action)
def test_a_rejected_event_never_touches_the_projection(actions, extra):
    events, tasks = build_history(actions)
    type_, tid, verdict = extra
    ev = Event.create(len(events), type_, "a", TS, payload_for(type_, tid, verdict), events[-1].hash if events else GENESIS_HASH)
    snapshot = dict(tasks)
    try:
        apply_task_event_inplace(tasks, ev)
    except InvalidTransition:
        assert tasks == snapshot
