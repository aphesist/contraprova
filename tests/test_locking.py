"""The portable single-writer lock: real on POSIX, patched for Windows."""

import multiprocessing
import os
import threading
import time
import types

import pytest

from contraprova import locking
from contraprova.events import verify_chain
from contraprova.locking import LockTimeout, exclusive_lock
from contraprova.log import EventLog, read_events

TS = "2026-11-11T00:00:00.000Z"
posix_only = pytest.mark.skipif(locking.fcntl is None, reason="needs fcntl")


# ------------------------------------------------------------------ POSIX, for real


@posix_only
def test_posix_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "log.jsonl"
    with open(path, "a+") as a, open(path, "a+") as b:
        with exclusive_lock(a, tmp_path / "x.lock"):
            with pytest.raises(LockTimeout):
                with exclusive_lock(b, tmp_path / "x.lock", timeout=0.05):
                    pytest.fail("a second holder got the lock")
        with exclusive_lock(b, tmp_path / "x.lock", timeout=0.05):  # released by the first exit
            pass


@posix_only
def test_posix_lock_is_released_when_the_body_raises(tmp_path):
    path = tmp_path / "log.jsonl"
    with open(path, "a+") as a, open(path, "a+") as b:
        with pytest.raises(RuntimeError):
            with exclusive_lock(a, tmp_path / "x.lock"):
                raise RuntimeError("boom")
        with exclusive_lock(b, tmp_path / "x.lock", timeout=0.05):
            pass


@posix_only
def test_posix_lock_blocks_until_released(tmp_path):
    path = tmp_path / "log.jsonl"
    order: list[str] = []
    with open(path, "a+") as a, open(path, "a+") as b:
        def waiter():
            with exclusive_lock(b, tmp_path / "x.lock"):  # no timeout: waits
                order.append("waiter")

        with exclusive_lock(a, tmp_path / "x.lock"):
            t = threading.Thread(target=waiter)
            t.start()
            time.sleep(0.1)
            order.append("holder")
        t.join(5)
    assert order == ["holder", "waiter"]


@posix_only
def test_posix_does_not_create_a_sidecar_file(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("task.created", "o", {"task_id": "t", "title": "x", "scope": []}, TS)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["log.jsonl"]


def _append_many(path, worker, n):
    log = EventLog(path)
    for i in range(n):
        log.append("task.created", f"w{worker}", {"task_id": f"t{worker}-{i}", "title": "x", "scope": []}, TS)


@posix_only
def test_concurrent_writers_keep_one_gap_free_chain(tmp_path):
    path = tmp_path / "log.jsonl"
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_append_many, args=(path, w, 15)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    evs = read_events(path)
    assert len(evs) == 60
    verify_chain(evs)


def test_append_waits_with_a_timeout_when_asked(tmp_path):
    if locking.fcntl is None:
        pytest.skip("needs fcntl")
    path = tmp_path / "log.jsonl"
    holder = open(path, "a+")
    try:
        with exclusive_lock(holder, tmp_path / "l"):
            log = EventLog(path, lock_timeout=0.05)
            with pytest.raises(LockTimeout):
                log.append("task.created", "o", {"task_id": "t", "title": "x", "scope": []}, TS)
    finally:
        holder.close()
    assert EventLog(path).verify() == 0  # nothing was written


# ------------------------------------------------------------------ Windows, patched


class FakeMsvcrt(types.SimpleNamespace):
    """Records msvcrt.locking calls; the first ``busy`` lock attempts fail like a held lock."""

    def __init__(self, busy=0):
        super().__init__(LK_UNLCK=0, LK_LOCK=1, LK_NBLCK=2, calls=[], busy=busy)

    def locking(self, fd, mode, nbytes):
        assert isinstance(fd, int)
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK and self.busy > 0:
            self.busy -= 1
            raise OSError(36, "Resource deadlock avoided")  # what Windows raises for a held range


@pytest.fixture
def windows(monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setattr(locking, "fcntl", None)
    monkeypatch.setattr(locking, "msvcrt", fake)
    monkeypatch.setattr(locking, "POLL_SECONDS", 0.001)
    return fake


def test_windows_locks_one_byte_of_a_sidecar_file_and_unlocks(windows, tmp_path):
    lock = tmp_path / "log.jsonl.lock"
    with open(tmp_path / "log.jsonl", "a+") as f:
        with exclusive_lock(f, lock):
            assert lock.exists()
            assert windows.calls == [(windows.LK_NBLCK, 1)]
    assert windows.calls == [(windows.LK_NBLCK, 1), (windows.LK_UNLCK, 1)]


def test_windows_waits_while_busy_then_takes_the_lock(windows, tmp_path):
    windows.busy = 3
    with open(tmp_path / "log.jsonl", "a+") as f:
        with exclusive_lock(f, tmp_path / "l", timeout=5):
            pass
    assert [m for m, _ in windows.calls].count(windows.LK_NBLCK) == 4


def test_windows_times_out_and_never_unlocks_a_lock_it_never_held(windows, tmp_path):
    windows.busy = 10**9
    with open(tmp_path / "log.jsonl", "a+") as f:
        with pytest.raises(LockTimeout):
            with exclusive_lock(f, tmp_path / "l", timeout=0.05):
                pytest.fail("lock taken")
    assert all(m == windows.LK_NBLCK for m, _ in windows.calls)


def test_windows_unlocks_when_the_body_raises(windows, tmp_path):
    with open(tmp_path / "log.jsonl", "a+") as f:
        with pytest.raises(RuntimeError):
            with exclusive_lock(f, tmp_path / "l"):
                raise RuntimeError("boom")
    assert windows.calls[-1] == (windows.LK_UNLCK, 1)


def test_event_log_appends_through_the_windows_path(windows, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("task.created", "o", {"task_id": "t", "title": "x", "scope": []}, TS)
    log.append("task.claimed", "w", {"task_id": "t"}, TS)
    assert log.verify() == 2
    assert (tmp_path / "log.jsonl.lock").exists()
    assert [m for m, _ in windows.calls] == [windows.LK_NBLCK, windows.LK_UNLCK] * 2


def test_a_windows_written_log_is_byte_identical_to_a_posix_one(windows, tmp_path, monkeypatch):
    def write(path):
        log = EventLog(path)
        log.append("task.created", "o", {"task_id": "t", "title": "aφesis", "scope": []}, TS)
        log.append("task.claimed", "w", {"task_id": "t"}, TS)
        return path.read_bytes()

    via_msvcrt = write(tmp_path / "a.jsonl")
    monkeypatch.undo()  # back to the real platform lock
    assert write(tmp_path / "b.jsonl") == via_msvcrt


def test_append_opens_the_log_without_newline_translation(tmp_path, monkeypatch):
    """On Windows a text-mode write turns LF into CRLF; the log must stay LF."""
    from contraprova import log as log_module

    seen = {}
    real_open = open

    def spy(file, mode="r", *a, **kw):
        seen[os.fspath(file)] = kw
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(log_module, "open", spy, raising=False)
    EventLog(tmp_path / "log.jsonl").append("task.created", "o", {"task_id": "t", "title": "x", "scope": []}, TS)
    assert seen[str(tmp_path / "log.jsonl")]["newline"] == "\n"


def test_no_primitive_at_all_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(locking, "fcntl", None)
    monkeypatch.setattr(locking, "msvcrt", None)
    with open(tmp_path / "f", "a+") as f:
        with pytest.raises(RuntimeError):
            with exclusive_lock(f, tmp_path / "l"):
                pytest.fail("ran without a lock")
