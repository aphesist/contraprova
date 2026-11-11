"""Projections: pure functions from the event log to readable state.

Nothing here performs I/O or calls a model. Given the same events, every
function returns the same result, which is what makes replay meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from .events import Event

TERMINAL = frozenset({"done", "failed"})
DEFAULT_MAX_ATTEMPTS = 3


class InvalidTransition(Exception):
    """An event is not allowed in the current state."""


@dataclass(frozen=True)
class TaskView:
    task_id: str
    title: str
    kind: str
    scope: tuple[str, ...]
    status: str = "todo"
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    supersedes: str | None = None
    worker: str | None = None


Tasks = Mapping[str, TaskView]


def _require(tasks: Tasks, task_id: str) -> TaskView:
    if task_id not in tasks:
        raise InvalidTransition(f"unknown task {task_id!r}")
    return tasks[task_id]


def apply_task_event(tasks: Tasks, ev: Event) -> dict[str, TaskView]:
    """Return the task map after ``ev``. Raises InvalidTransition if ``ev`` is illegal."""
    out = dict(tasks)
    p = ev.payload
    if ev.type == "task.created":
        tid = p["task_id"]
        if tid in out:
            raise InvalidTransition(f"task {tid!r} already exists")
        sup = p.get("supersedes")
        if sup is not None and _require(out, sup).status not in TERMINAL:
            raise InvalidTransition(f"task {tid!r} can only supersede a finished task")
        out[tid] = TaskView(
            task_id=tid,
            title=p.get("title", ""),
            kind=p.get("kind", "code"),
            scope=tuple(p.get("scope", ())),
            max_attempts=int(p.get("max_attempts", DEFAULT_MAX_ATTEMPTS)),
            supersedes=sup,
        )
    elif ev.type == "task.claimed":
        t = _require(out, p["task_id"])
        if t.status != "todo":
            raise InvalidTransition(f"task {t.task_id!r} is {t.status}, cannot be claimed")
        out[t.task_id] = replace(t, status="in_progress", worker=p.get("worker", ev.actor))
    elif ev.type == "task.submitted":
        t = _require(out, p["task_id"])
        if t.status != "in_progress":
            raise InvalidTransition(f"task {t.task_id!r} is {t.status}, cannot be submitted")
        out[t.task_id] = replace(t, status="review", attempts=t.attempts + 1)
    elif ev.type == "gate.decided":
        t = _require(out, p["task_id"])
        if t.status != "review":
            raise InvalidTransition(f"task {t.task_id!r} is {t.status}, nothing to gate")
        verdict = p["verdict"]
        if verdict == "pass":
            out[t.task_id] = replace(t, status="done")
        elif verdict == "block":
            nxt = "failed" if t.attempts >= t.max_attempts else "todo"
            out[t.task_id] = replace(t, status=nxt, worker=None)
        else:
            raise InvalidTransition(f"unknown gate verdict {verdict!r}")
    elif ev.type in ("route.decided", "fork.created"):
        _require(out, p["task_id"])
    # usage.recorded and outcome.scored do not change task state.
    return out


def project_tasks(events: Iterable[Event]) -> dict[str, TaskView]:
    tasks: dict[str, TaskView] = {}
    for ev in events:
        tasks = apply_task_event(tasks, ev)
    return tasks


# ---------------------------------------------------------------- measurement


def usage_records(events: Iterable[Event]) -> list[Mapping]:
    return [ev.payload for ev in events if ev.type == "usage.recorded"]


def outcomes(events: Iterable[Event]) -> dict[tuple[str, str], bool]:
    """(arm, task_id) -> solved. A later score for the same pair replaces an earlier one."""
    out: dict[tuple[str, str], bool] = {}
    for ev in events:
        if ev.type == "outcome.scored":
            out[(ev.payload["arm"], ev.payload["task_id"])] = bool(ev.payload["solved"])
    return out


def pareto_frontier(points: Iterable[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """Non-dominated (name, cost, quality) points: lower cost and higher quality are better.

    Returned sorted by cost. A point is dominated if another is no worse on both
    axes and strictly better on at least one.
    """
    pts = list(points)
    keep = []
    for name, c, q in pts:
        dominated = any((c2 <= c and q2 >= q) and (c2 < c or q2 > q) for _, c2, q2 in pts)
        if not dominated:
            keep.append((name, c, q))
    return sorted(keep, key=lambda x: (x[1], -x[2], x[0]))
