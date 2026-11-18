"""The append-only event log: one JSON object per line, single writer.

``append`` takes an exclusive file lock, re-reads the log, checks the new event
against the current projection, and only then writes it. An illegal event is
never stored, so replaying the log can never fail on a transition.

With ``incremental=True`` the writer keeps the chain's tail (event count, last hash) and the task
projection in memory, valid for exactly the file it last saw (size, mtime, inode). While the file
is unchanged an append verifies and validates in O(1); if anything about the file differs, it falls
back to a full verify. A full verify always runs on :meth:`EventLog.open` and :meth:`EventLog.verify`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .events import GENESIS_HASH, Event, verify_chain
from .locking import exclusive_lock
from .projections import TaskView, apply_task_event, apply_task_event_inplace, project_tasks


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [Event.from_json(line) for line in f if line.strip()]


FileKey = tuple[int, int, int]  # (size, mtime_ns, inode)


@dataclass
class _Tail:
    """What a full verify established about one exact version of the file."""

    key: FileKey
    count: int
    last_hash: str
    tasks: dict[str, TaskView]


def _file_key(path: Path) -> FileKey:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns, st.st_ino


class EventLog:
    def __init__(self, path: str | os.PathLike, *, lock_timeout: float | None = None, incremental: bool = False):
        """``lock_timeout``: seconds ``append`` waits for the writer lock (None waits forever).

        ``incremental`` opts in to the cached tail described in the module docstring. The first
        append (or :meth:`verify`) of such a log does one full verify; later ones are O(1) while
        the file is exactly as this instance left it.
        """
        self.path = Path(path)
        self.lock_timeout = lock_timeout
        self.lock_path = self.path.with_name(self.path.name + ".lock")  # used on Windows only
        self.incremental = incremental
        self._tail: _Tail | None = None

    @classmethod
    def open(cls, path: str | os.PathLike, *, incremental: bool = True, lock_timeout: float | None = None) -> "EventLog":
        """Open a log after a full verify of its chain and every transition (``ChainError`` /
        ``InvalidTransition`` if it is broken). A missing file is an empty log."""
        log = cls(path, lock_timeout=lock_timeout, incremental=incremental)
        log.verify()
        return log

    def events(self) -> list[Event]:
        evs = read_events(self.path)
        verify_chain(evs)
        return evs

    def verify(self) -> int:
        """Check the chain and replay every transition, always in full. Returns the event count.

        An incremental log also refreshes its cached tail from this pass.
        """
        key = _file_key(self.path) if self.path.exists() else None  # before reading: a racing write then misses the cache
        evs = self.events()
        tasks = project_tasks(evs)
        if self.incremental and key is not None:
            self._tail = _Tail(key, len(evs), evs[-1].hash if evs else GENESIS_HASH, tasks)
        return len(evs)

    def tasks(self) -> dict[str, TaskView]:
        """The task projection of the log as it is now (a copy). Uses the cached one if still valid."""
        if self.incremental and self.path.exists():
            tail = self._tail
            if tail is not None and tail.key == _file_key(self.path):
                return dict(tail.tasks)
        return project_tasks(self.events())

    def append(self, type_: str, actor: str, payload: Mapping[str, Any], ts: str | None = None) -> Event:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": no CRLF translation on Windows, so the bytes match a POSIX log.
        with open(self.path, "a+", encoding="utf-8", newline="\n") as f, exclusive_lock(f, self.lock_path, timeout=self.lock_timeout):
            if self.incremental:
                return self._append_incremental(f, type_, actor, payload, ts)
            evs = self.events()
            prev = evs[-1].hash if evs else GENESIS_HASH
            ev = Event.create(len(evs), type_, actor, ts or utc_now(), payload, prev)
            apply_task_event(project_tasks(evs), ev)  # raises InvalidTransition
            f.write(ev.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
            return ev

    def _append_incremental(self, f: Any, type_: str, actor: str, payload: Mapping[str, Any], ts: str | None) -> Event:
        """The same checks as the full path, from the cached tail when the file is unchanged."""
        tail = self._tail
        if tail is None or tail.key != _file_key(self.path):
            self.verify()  # full: chain, every transition; refreshes self._tail
            tail = self._tail
            assert tail is not None
        ev = Event.create(tail.count, type_, actor, ts or utc_now(), payload, tail.last_hash)
        apply_task_event_inplace(tail.tasks, ev)  # raises InvalidTransition, leaving the cache untouched
        try:
            f.write(ev.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
            self._tail = _Tail(_file_key(self.path), tail.count + 1, ev.hash, tail.tasks)
        except BaseException:
            self._tail = None  # the cache is ahead of the file; the next append verifies in full
            raise
        return ev
