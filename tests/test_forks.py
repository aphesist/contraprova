import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from contraprova.events import Event
from contraprova.forks import (
    conversation_prefix,
    fork,
    fork_pair,
    list_forks,
    run_fork,
)
from contraprova.interfaces import HAIKU, SONNET, Message
from contraprova.llm import AnthropicClient, FakeLLM, RecordingClient
from contraprova.log import EventLog
from contraprova.projections import project_tasks, usage_records

TS = "2026-11-11T00:00:00.000Z"
SYSTEM = "You are a worker. Change only files in scope."


def failed_once(log, tid="t1"):
    """task.created(0) claimed(1) submitted(2) gate block(3) + a hidden score that must never leak."""
    log.append("task.created", "orchestrator",
               {"task_id": tid, "title": "fix", "prompt": "Make the tests pass.", "scope": ["src/a.py"], "system": SYSTEM}, TS)
    log.append("task.claimed", "worker-1", {"task_id": tid}, TS)
    log.append("task.submitted", "worker-1", {"task_id": tid, "response": "ATTEMPT-1 OUTPUT", "files": ["src/a.py"]}, TS)
    log.append("gate.decided", "gate", {"task_id": tid, "gate": "T0.visible_tests", "verdict": "block",
                                        "reason": "2 failing"}, TS)


@pytest.fixture
def log(tmp_path):
    lg = EventLog(tmp_path / "log.jsonl")
    failed_once(lg)
    return lg


def test_prefix_reconstruction(log):
    p = conversation_prefix(log, "t1", 3)
    assert p.system == SYSTEM
    assert p.messages == (
        Message("user", "Make the tests pass.\n\nFiles you may change:\n- src/a.py"),
        Message("assistant", "ATTEMPT-1 OUTPUT"),
        Message("user", "The T0.visible_tests check blocked your submission: 2 failing\nPlease try again."),
    )
    assert p.runnable and p.prefix_hash == log.events()[3].hash
    # From the creation point the prefix is just the task.
    p0 = conversation_prefix(log, "t1", 0)
    assert p0.messages == p.messages[:1]
    assert conversation_prefix(log, "t1", 0, system="override").system == "override"


def test_fork_created_payload(log):
    fid = fork(log, "t1", 3, HAIKU, "control", 7)
    ev = log.events()[-1]
    assert ev.type == "fork.created"
    assert ev.payload == {"fork_id": fid, "task_id": "t1", "from_seq": 3, "model": HAIKU, "role": "control",
                          "seed": 7, "prefix_hash": log.events()[3].hash}
    assert fid.startswith("fork-") and len(fid) == 21
    assert list_forks(log)[fid].seq == ev.seq
    # forks do not change task state
    assert project_tasks(log.events())["t1"].status == "todo"


def test_fork_validation(log):
    with pytest.raises(ValueError):
        fork(log, "t1", 3, HAIKU, "treatment-ish", 1)
    with pytest.raises(ValueError):
        fork(log, "t1", 99, HAIKU, "control", 1)
    with pytest.raises(ValueError):
        fork(log, "t1", 2, HAIKU, "control", 1)  # ends on the assistant turn
    log.append("task.created", "orchestrator", {"task_id": "t2", "title": "other"}, TS)
    with pytest.raises(ValueError):
        fork(log, "t1", 4, HAIKU, "control", 1)  # seq 4 belongs to t2
    fork(log, "t1", 3, HAIKU, "control", 1)
    with pytest.raises(ValueError):
        fork(log, "t1", 3, HAIKU, "control", 1)  # duplicate
    fork(log, "t1", 3, HAIKU, "control", 2)  # a replicate with another seed is fine


def test_prefix_hash_stable_as_log_grows(log):
    fid = fork(log, "t1", 3, HAIKU, "control", 1)
    before = conversation_prefix(log, "t1", 3)
    # Later history (a retry that passes, a hidden score, usage) must not change the prefix.
    log.append("task.claimed", "worker-2", {"task_id": "t1"}, TS)
    log.append("task.submitted", "worker-2", {"task_id": "t1", "response": "FUTURE OUTPUT"}, TS)
    log.append("gate.decided", "gate", {"task_id": "t1", "gate": "tests", "verdict": "pass"}, TS)
    log.append("outcome.scored", "scorer", {"task_id": "t1", "arm": "D", "solved": True}, TS)
    after = conversation_prefix(log, "t1", 3)
    assert before == after
    assert list_forks(log)[fid].prefix_hash == after.prefix_hash


def test_run_fork_calls_live_and_never_stitches_future(log):
    c_id, t_id = fork_pair(log, "t1", 3, control_model=HAIKU, treatment_model=SONNET, seed=3)
    # Future recorded output exists in the log...
    log.append("task.claimed", "worker-2", {"task_id": "t1"}, TS)
    log.append("task.submitted", "worker-2", {"task_id": "t1", "response": "FUTURE OUTPUT"}, TS)
    fake = FakeLLM(["LIVE-C", "LIVE-T"])
    rc = RecordingClient(fake, log)
    c = run_fork(log, c_id, rc, max_tokens=1000, effort="low")
    t = run_fork(log, t_id, rc, max_tokens=1000)
    assert (c.text, t.text) == ("LIVE-C", "LIVE-T")
    assert len(fake.calls) == 2
    for call in fake.calls:
        sent = json.dumps([m.content for m in call["messages"]])
        assert "FUTURE OUTPUT" not in sent
        assert call["messages"][-1].role == "user"
        assert call["system"] == SYSTEM
    assert [call["model"] for call in fake.calls] == [HAIKU, SONNET]
    recs = usage_records(log.events())
    assert [(r["arm"], r["fork_id"], r["task_id"]) for r in recs] == [("control", c_id, "t1"), ("treatment", t_id, "t1")]


def test_run_fork_detects_tampered_prefix(log, tmp_path):
    fid = fork(log, "t1", 3, HAIKU, "control", 1)
    events = log.events()
    # Rebuild a log where the fork point's content differs but the fork event claims the old hash.
    other = EventLog(tmp_path / "other.jsonl")
    for ev in events[:3]:
        other.append(ev.type, ev.actor, ev.payload, ev.ts)
    other.append("gate.decided", "gate", {"task_id": "t1", "gate": "T0.visible_tests", "verdict": "block", "reason": "edited"}, TS)
    other.append("fork.created", "forker", events[-1].payload, TS)
    with pytest.raises(ValueError):
        run_fork(other, fid, FakeLLM(["x"]), max_tokens=5)


def test_fork_request_bytes_match_original_call(log, monkeypatch):
    """The fork's prefix renders to the same request bytes the original runner sends for the same history."""
    monkeypatch.setenv("CONTRAPROVA_KEY_WORKER", "k")
    client = AnthropicClient("worker")
    p = conversation_prefix(log, "t1", 3)
    original = client.encode_body(client.build_body(model=HAIKU, system=p.system, messages=p.messages, max_tokens=900))
    fid = fork(log, "t1", 3, HAIKU, "control", 1)
    f = list_forks(log)[fid]
    again = conversation_prefix(log, f.task_id, f.from_seq)
    replay = client.encode_body(client.build_body(model=f.model, system=again.system, messages=again.messages, max_tokens=900))
    assert original == replay


# ------------------------------------------------------------------ properties


steps = st.lists(
    st.tuples(st.sampled_from(["claim_submit_block", "claim_submit_pass", "usage", "other_task"]),
              st.text(max_size=20)),
    max_size=12,
)


def build(tmp_path_factory, plan):
    lg = EventLog(tmp_path_factory.mktemp("p") / "log.jsonl")
    lg.append("task.created", "o", {"task_id": "t", "title": "T", "prompt": "do it", "max_attempts": 50}, TS)
    lg.append("task.created", "o", {"task_id": "u", "title": "U"}, TS)
    done = False
    for i, (kind, text) in enumerate(plan):
        if kind.startswith("claim") and not done:
            lg.append("task.claimed", "w", {"task_id": "t"}, TS)
            lg.append("task.submitted", "w", {"task_id": "t", "response": f"<<R{i}>>{text}"}, TS)
            verdict = "block" if kind.endswith("block") else "pass"
            lg.append("gate.decided", "g", {"task_id": "t", "gate": "tests", "verdict": verdict, "reason": text}, TS)
            done = verdict == "pass"
        elif kind == "usage":
            lg.append("usage.recorded", "llm", {"model": HAIKU, "input_tokens": 1, "output_tokens": 1, "task_id": "t"}, TS)
        elif kind == "other_task":
            lg.append("outcome.scored", "s", {"task_id": "t", "arm": "D", "solved": bool(text)}, TS)
    return lg


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(plan=steps, extra=steps, data=st.data())
def test_property_prefix_is_pure_and_stable(tmp_path_factory, plan, extra, data):
    lg = build(tmp_path_factory, plan)
    events = lg.events()
    t_seqs = [e.seq for e in events if e.payload.get("task_id") == "t" and e.type != "usage.recorded"
              and e.type != "outcome.scored"]
    seq = data.draw(st.sampled_from(t_seqs))
    spy = FakeLLM([])  # any call would raise ScriptExhausted and be recorded
    p1 = conversation_prefix(events, "t", seq)
    # Replaying the prefix performs no model call.
    assert spy.calls == []
    # Appending arbitrary later events does not change the prefix or its hash.
    for kind, text in extra:
        lg.append("usage.recorded", "llm", {"model": HAIKU, "input_tokens": len(text), "output_tokens": 0}, TS)
    p2 = conversation_prefix(lg, "t", seq)
    assert p1 == p2
    assert p1.prefix_hash == events[seq].hash
    # Every submission up to the fork point is in the prefix; none after it is.
    rendered = "\x00".join(m.content for m in p1.messages)
    for e in lg.events():
        if e.type == "task.submitted":
            marker = e.payload["response"].split(">>")[0] + ">>"
            assert (marker in rendered) == (e.seq <= seq)


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(plan=steps)
def test_property_fork_makes_exactly_one_call(tmp_path_factory, plan):
    lg = build(tmp_path_factory, plan)
    events = lg.events()
    user_points = [e.seq for e in events if e.payload.get("task_id") == "t"
                   and (e.type == "task.created" or (e.type == "gate.decided" and e.payload["verdict"] == "block"))]
    fid = fork(lg, "t", user_points[-1], HAIKU, "control", 0)
    spy = FakeLLM(["ok"])
    run_fork(lg, fid, spy, max_tokens=10)
    assert len(spy.calls) == 1


def test_event_payload_roundtrip_keeps_prefix(log, tmp_path):
    """Re-reading the log from disk (fresh Event objects) yields the same prefix bytes."""
    a = conversation_prefix(log, "t1", 3)
    b = conversation_prefix([Event.from_json(e.to_json()) for e in log.events()], "t1", 3)
    assert a == b
