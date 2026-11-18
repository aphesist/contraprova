"""Incremental verification: a cached tail and projection, validated against the file."""

import os
import statistics
import time

import pytest

from contraprova import log as log_module
from contraprova.events import GENESIS_HASH, ChainError, Event
from contraprova.log import EventLog, read_events
from contraprova.projections import InvalidTransition, project_tasks

TS = "2026-11-18T09:00:00.000Z"


def task(log, tid="t1", **extra):
    return log.append("task.created", "o", {"task_id": tid, "title": "x", "scope": ["a"], **extra}, TS)


@pytest.fixture
def reads(monkeypatch):
    """Count full reads of the log."""
    calls = []
    real = log_module.read_events

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(log_module, "read_events", counting)
    return calls


def test_incremental_is_off_by_default(tmp_path, reads):
    log = EventLog(tmp_path / "log.jsonl")
    task(log, "a")
    task(log, "b")
    assert len(reads) == 2  # every append re-reads, as before


def test_after_the_first_append_the_log_is_not_read_again(tmp_path, reads):
    log = EventLog(tmp_path / "log.jsonl", incremental=True)
    task(log, "a")
    assert len(reads) == 1  # one full verify on the first append
    for i in range(20):
        task(log, f"t{i}")
    assert len(reads) == 1
    assert log.verify() == 21 and len(reads) == 2  # on demand, always full
    assert log.tasks() == project_tasks(read_events(log.path))


def test_open_verifies_in_full_and_rejects_a_broken_chain(tmp_path, reads):
    path = tmp_path / "log.jsonl"
    log = EventLog(path)
    task(log, "a")
    task(log, "b")
    reads.clear()
    opened = EventLog.open(path)
    assert opened.incremental and len(reads) == 1
    task(opened, "c")
    assert len(reads) == 1  # the open primed the cache
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    with pytest.raises(ChainError):
        EventLog.open(path)


def test_open_of_a_missing_file_is_an_empty_log(tmp_path):
    log = EventLog.open(tmp_path / "new" / "log.jsonl")
    assert log.verify() == 0 and log.tasks() == {}
    task(log)
    assert log.verify() == 1


def test_another_writer_makes_the_cache_stale_and_the_next_append_is_full(tmp_path, reads):
    path = tmp_path / "log.jsonl"
    fast = EventLog(path, incremental=True)
    task(fast, "a")
    task(EventLog(path), "b")  # someone else appends
    reads.clear()
    ev = task(fast, "c")
    assert ev.seq == 2 and len(reads) == 1  # fell back to a full verify, and chained correctly
    assert fast.verify() == 3
    assert set(fast.tasks()) == {"a", "b", "c"}


def test_a_changed_file_with_the_same_size_is_caught_by_mtime(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path, incremental=True)
    task(log, "a")
    task(log, "b")
    stat = os.stat(path)
    forged = path.read_text().replace('"title":"x"', '"title":"y"')
    assert len(forged) == path.stat().st_size
    path.write_text(forged)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    with pytest.raises(ChainError):
        task(log, "c")


def test_a_replaced_file_is_caught_by_the_inode(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path, incremental=True)
    task(log, "a")
    stat = os.stat(path)
    forged = tmp_path / "forged"
    forged.write_text(path.read_text().replace('"title":"x"', '"title":"z"'))
    os.utime(forged, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size and mtime
    os.replace(forged, path)
    with pytest.raises(ChainError):
        task(log, "b")


def test_a_truncated_file_is_reverified_and_the_chain_continues_from_it(tmp_path):
    path = tmp_path / "log.jsonl"
    log = EventLog(path, incremental=True)
    for t in "abc":
        task(log, t)
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:2]) + "\n")
    ev = task(log, "d")
    assert ev.seq == 2 and ev.prev_hash == read_events(path)[1].hash
    assert log.verify() == 3 and set(log.tasks()) == {"a", "b", "d"}


def test_the_documented_limit_a_forgery_that_keeps_size_mtime_and_inode_needs_verify(tmp_path):
    """The fast path trusts (size, mtime, inode); verify() is the check that does not."""
    path = tmp_path / "log.jsonl"
    log = EventLog(path, incremental=True)
    task(log, "a")
    task(log, "b")
    st = os.stat(path)
    with open(path, "r+b") as f:  # edit in place, same length
        data = f.read().replace(b'"title":"x"', b'"title":"y"', 1)
        f.seek(0)
        f.write(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    task(log, "c")  # not noticed: size, mtime and inode still match the cache
    with pytest.raises(ChainError):
        log.verify()


def test_an_illegal_event_leaves_the_cache_and_the_file_untouched(tmp_path, reads):
    log = EventLog(tmp_path / "log.jsonl", incremental=True)
    task(log, "a")
    before = (log.path.read_bytes(), log.tasks())
    with pytest.raises(InvalidTransition):
        log.append("task.submitted", "w", {"task_id": "a"}, TS)  # never claimed
    with pytest.raises(InvalidTransition):
        log.append("ship.applied", "p", {"ship_id": "s", "files": [], "task_id": "ghost"}, TS)
    with pytest.raises(ValueError):
        log.append("nope", "x", {}, TS)
    assert (log.path.read_bytes(), log.tasks()) == before
    task(log, "b")
    assert len(reads) == 1  # still the fast path
    assert log.verify() == 2


def test_a_failed_write_drops_the_cache(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "log.jsonl", incremental=True)
    task(log, "a")
    monkeypatch.setattr(log_module.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        task(log, "b")
    monkeypatch.undo()
    assert log._tail is None
    ev = task(log, "c")  # reverified in full; the line that did reach the file is part of the chain
    assert ev.seq == len(read_events(log.path)) - 1
    assert log.verify() == ev.seq + 1


def test_incremental_and_full_writers_produce_identical_files(tmp_path):
    a, b = EventLog(tmp_path / "a.jsonl", incremental=True), EventLog(tmp_path / "b.jsonl")
    for log in (a, b):
        task(log, "t1")
        log.append("task.claimed", "w", {"task_id": "t1"}, TS)
        log.append("usage.recorded", "p", {"task_id": "t1", "model": "m", "input_tokens": 1}, TS)
        log.append("task.submitted", "w", {"task_id": "t1"}, TS)
        log.append("gate.decided", "g", {"task_id": "t1", "gate": "x", "verdict": "pass"}, TS)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


# ------------------------------------------------------------------ timing (slow)


def write_big_log(path, n):
    """A valid chain of ``n`` events (5,000 finished tasks plus usage and tool noise), written fast."""
    evs, prev = [], GENESIS_HASH

    def add(type_, actor, payload):
        nonlocal prev
        ev = Event.create(len(evs), type_, actor, TS, payload, prev)
        evs.append(ev)
        prev = ev.hash

    i = 0
    while len(evs) < n:
        tid = f"t{i}"
        add("task.created", "o", {"task_id": tid, "title": "x", "scope": ["src/a.py"]})
        add("task.claimed", "w", {"task_id": tid})
        for k in range(8):
            add("usage.recorded", "p", {"task_id": tid, "model": "claude-haiku-5-5", "input_tokens": 100 + k,
                                         "output_tokens": 50, "cache_read_tokens": 0, "cache_write_5m_tokens": 0,
                                         "cache_write_1h_tokens": 0})
        for k in range(4):
            add("tool.called", "p", {"tool_call_id": f"{tid}-{k}", "tool": "edit_file", "status": "ok", "duration_ms": 5})
        add("task.submitted", "w", {"task_id": tid})
        add("gate.decided", "g", {"task_id": tid, "gate": "tests", "verdict": "pass"})
        i += 1
    path.write_text("".join(e.to_json() + "\n" for e in evs[:n]), encoding="utf-8")
    return n


@pytest.mark.slow
def test_append_to_a_100k_event_log_is_fast(tmp_path, capsys):
    path = tmp_path / "big.jsonl"
    n = write_big_log(path, 100_000)
    log = EventLog(path, incremental=True)
    t = time.perf_counter()
    log.verify()  # the one full verify, on open
    full_s = time.perf_counter() - t
    log.append("usage.recorded", "p", {"task_id": "t0", "model": "m", "input_tokens": 1}, TS)  # warm
    times = []
    for k in range(500):
        t = time.perf_counter()
        log.append("tool.called", "p", {"tool_call_id": f"x{k}", "tool": "shell", "status": "ok", "duration_ms": 1}, TS)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    p50, p95, worst = statistics.median(times), times[int(len(times) * 0.95) - 1], times[-1]
    with capsys.disabled():
        print(f"\n100k-event log: full verify {full_s:.2f}s; incremental append p50 {p50:.2f} ms, "
              f"p95 {p95:.2f} ms, max {worst:.2f} ms")
    assert log.verify() == n + 1 + 500
    assert p95 < 5.0
