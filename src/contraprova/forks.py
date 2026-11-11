"""Counterfactual re-runs from a past point in the log.

A fork names a point in a task's history (``from_seq``) and a model to re-run the task with from
there. The control re-runs the same cheap model; the treatment runs a stronger one. Both are
**re-executed**: the conversation prefix up to and including ``from_seq`` is rebuilt from the log
and sent to the model live. Nothing recorded after ``from_seq`` is ever read, so no future output
is stitched into a fork.

Payload fields
--------------

Written, ``fork.created``::

    fork_id      str   "fork-" + first 16 hex of sha256(canonical_json of the five fields below + prefix_hash)
    task_id      str
    from_seq     int   seq of the fork point; the event there must belong to task_id
    model        str   model the fork re-runs with
    role         str   "control" | "treatment"
    seed         int   recorded for the harness's own randomness and to tell replicates apart
                       (the Messages API has no sampling seed: each run is an independent sample)
    prefix_hash  str   the chain hash of the event at from_seq, which commits to every event up to it

Read, only from events with ``seq <= from_seq`` and ``payload.task_id == task_id``:

    task.created    task_id, title, prompt (optional), scope (optional list), system (optional)
    task.submitted  response (optional str: the worker's reply text), else files
                    (optional: mapping path -> content, or list of paths)
    gate.decided    verdict; when "block": gate (optional), reason (optional)

Every other event type (task.claimed, route.decided, usage.recorded, fork.created and, above all,
outcome.scored, which carries hidden-test results) is ignored when building the prefix.

The rendering below is the single source of truth for turning a task's history into messages.
The runner that makes the original calls must build its conversation with
:func:`conversation_prefix` too, so a fork's prefix is byte-identical to the original request
and is served from the prompt cache.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .events import Event, canonical_json
from .interfaces import Completion, LLMClient, Message

FORK_ROLES = frozenset({"control", "treatment"})


@dataclass(frozen=True)
class Prefix:
    task_id: str
    from_seq: int
    system: str
    messages: tuple[Message, ...]
    prefix_hash: str  # chain hash of the event at from_seq

    @property
    def runnable(self) -> bool:
        """True when the prefix ends on a user turn, so the model can be asked to continue."""
        return bool(self.messages) and self.messages[-1].role == "user"


@dataclass(frozen=True)
class Fork:
    fork_id: str
    task_id: str
    from_seq: int
    model: str
    role: str
    seed: int
    prefix_hash: str
    seq: int  # seq of the fork.created event itself


# ------------------------------------------------------------------ rendering


def render_task(p: Mapping[str, Any]) -> str:
    text = str(p.get("prompt") or p.get("title") or "")
    scope = list(p.get("scope") or ())
    if scope:
        text += "\n\nFiles you may change:\n" + "\n".join(f"- {s}" for s in scope)
    return text


def render_submission(p: Mapping[str, Any]) -> str:
    if isinstance(p.get("response"), str):
        return p["response"]
    files = p.get("files")
    if isinstance(files, Mapping):
        return "\n\n".join(f"### {path}\n```\n{files[path]}\n```" for path in sorted(files))
    if files:
        return "Submitted: " + ", ".join(str(f) for f in files)
    return "(empty submission)"


def render_block(p: Mapping[str, Any]) -> str:
    gate = p.get("gate") or "gate"
    reason = p.get("reason") or "no reason given"
    return f"The {gate} check blocked your submission: {reason}\nPlease try again."


def _events(source: Any) -> list[Event]:
    return source.events() if hasattr(source, "events") else list(source)


def conversation_prefix(source: Any, task_id: str, from_seq: int, *, system: str | None = None) -> Prefix:
    """Rebuild the conversation a task had at ``from_seq`` (inclusive) from the log.

    ``source`` is an EventLog or a list of events. Pure: no model is called and nothing is
    written. ``system`` overrides the ``system`` field of task.created (the frozen worker prompt
    usually lives in config, not in the log).
    """
    events = _events(source)
    if not 0 <= from_seq < len(events):
        raise ValueError(f"from_seq {from_seq} is outside the log (0..{len(events) - 1})")
    if events[from_seq].payload.get("task_id") != task_id:
        raise ValueError(f"event {from_seq} does not belong to task {task_id!r}")
    sys_prompt: str | None = system
    messages: list[Message] = []
    created = False
    for ev in events[: from_seq + 1]:
        p = ev.payload
        if p.get("task_id") != task_id:
            continue
        if ev.type == "task.created":
            created = True
            if sys_prompt is None:
                sys_prompt = str(p.get("system") or "")
            messages.append(Message("user", render_task(p)))
        elif ev.type == "task.submitted":
            messages.append(Message("assistant", render_submission(p)))
        elif ev.type == "gate.decided" and p.get("verdict") == "block":
            messages.append(Message("user", render_block(p)))
    if not created:
        raise ValueError(f"task {task_id!r} was not created at or before seq {from_seq}")
    return Prefix(task_id, from_seq, sys_prompt or "", tuple(messages), events[from_seq].hash)


# ------------------------------------------------------------------ forks


def fork_id_for(task_id: str, from_seq: int, model: str, role: str, seed: int, prefix_hash: str) -> str:
    body = {"task_id": task_id, "from_seq": from_seq, "model": model, "role": role, "seed": seed,
            "prefix_hash": prefix_hash}
    return "fork-" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()[:16]


def list_forks(source: Any) -> dict[str, Fork]:
    out: dict[str, Fork] = {}
    for ev in _events(source):
        if ev.type == "fork.created" and "fork_id" in ev.payload:
            p = ev.payload
            out[p["fork_id"]] = Fork(p["fork_id"], p["task_id"], int(p["from_seq"]), p["model"],
                                     p["role"], int(p["seed"]), p["prefix_hash"], ev.seq)
    return out


def fork(log: Any, task_id: str, from_seq: int, model: str, role: str, seed: int, *,
         actor: str = "forker", ts: str | None = None) -> str:
    """Append a ``fork.created`` event and return the fork id.

    Raises ValueError if ``role`` is not control/treatment, the fork point is not one of the
    task's events, the prefix there does not end on a user turn (it could not be re-run without
    an assistant prefill), or an identical fork already exists.
    """
    if role not in FORK_ROLES:
        raise ValueError(f"role must be one of {sorted(FORK_ROLES)}, got {role!r}")
    if not model:
        raise ValueError("model is required")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an int")
    events = log.events()
    prefix = conversation_prefix(events, task_id, from_seq)
    if not prefix.runnable:
        raise ValueError(f"the conversation at seq {from_seq} ends on an assistant turn; fork after the gate decision")
    fid = fork_id_for(task_id, from_seq, model, role, seed, prefix.prefix_hash)
    if fid in list_forks(events):
        raise ValueError(f"fork {fid} already exists")
    payload = {"fork_id": fid, "task_id": task_id, "from_seq": from_seq, "model": model, "role": role,
               "seed": seed, "prefix_hash": prefix.prefix_hash}
    log.append("fork.created", actor, payload, ts)
    return fid


def run_fork(
    log: Any,
    fork_id: str,
    client: LLMClient,
    *,
    max_tokens: int,
    effort: str | None = None,
    system: str | None = None,
    tags: Mapping[str, Any] | None = None,
) -> Completion:
    """Re-execute a fork: rebuild its prefix from the log and make exactly one live call.

    The usage tags carry ``task_id``, ``fork_id`` and ``arm`` (the fork's role) unless ``tags``
    overrides them. Pass a RecordingClient (or a client with an ``on_usage`` recorder) to log usage.
    """
    events = log.events()
    forks = list_forks(events)
    if fork_id not in forks:
        raise KeyError(f"no fork {fork_id!r}")
    f = forks[fork_id]
    prefix = conversation_prefix(events, f.task_id, f.from_seq, system=system)
    if prefix.prefix_hash != f.prefix_hash:
        raise ValueError(f"fork {fork_id}: the event at seq {f.from_seq} no longer matches prefix_hash")
    all_tags = {"task_id": f.task_id, "fork_id": f.fork_id, "arm": f.role, **dict(tags or {})}
    return client.complete(model=f.model, system=prefix.system, messages=prefix.messages,
                           max_tokens=max_tokens, effort=effort, tags=all_tags)


def fork_pair(log: Any, task_id: str, from_seq: int, *, control_model: str, treatment_model: str,
              seed: int, actor: str = "forker") -> tuple[str, str]:
    """Create the control and treatment forks for one fork point. Returns (control_id, treatment_id)."""
    c = fork(log, task_id, from_seq, control_model, "control", seed, actor=actor)
    t = fork(log, task_id, from_seq, treatment_model, "treatment", seed, actor=actor)
    return c, t


__all__: Sequence[str] = (
    "FORK_ROLES", "Fork", "Prefix", "conversation_prefix", "fork", "fork_id_for", "fork_pair",
    "list_forks", "render_block", "render_submission", "render_task", "run_fork",
)
