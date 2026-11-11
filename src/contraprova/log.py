"""The append-only event log: one JSON object per line, single writer.

``append`` takes an exclusive file lock, re-reads the log, checks the new event
against the current projection, and only then writes it. An illegal event is
never stored, so replaying the log can never fail on a transition.
"""

from __future__ import annotations

import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .events import GENESIS_HASH, Event, verify_chain
from .projections import apply_task_event, project_tasks


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [Event.from_json(line) for line in f if line.strip()]


class EventLog:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def events(self) -> list[Event]:
        evs = read_events(self.path)
        verify_chain(evs)
        return evs

    def verify(self) -> int:
        """Check the chain and replay every transition. Returns the event count."""
        evs = self.events()
        project_tasks(evs)
        return len(evs)

    def append(self, type_: str, actor: str, payload: Mapping[str, Any], ts: str | None = None) -> Event:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                evs = self.events()
                prev = evs[-1].hash if evs else GENESIS_HASH
                ev = Event.create(len(evs), type_, actor, ts or utc_now(), payload, prev)
                apply_task_event(project_tasks(evs), ev)  # raises InvalidTransition
                f.write(ev.to_json() + "\n")
                f.flush()
                os.fsync(f.fileno())
                return ev
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
