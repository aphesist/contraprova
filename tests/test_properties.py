"""Property-based tests of the projection.

These are the kernel's formal claims, checked on randomly generated histories:
replay is deterministic, finished tasks never change, and any edit to a stored
event is detected.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from contraprova.events import GENESIS_HASH, ChainError, Event, verify_chain
from contraprova.projections import TERMINAL, InvalidTransition, apply_task_event, project_tasks

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
