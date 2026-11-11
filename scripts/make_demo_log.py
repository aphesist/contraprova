"""Generate a SYNTHETIC demo event log for the replay UI.

    python scripts/make_demo_log.py demo.jsonl [--seed 20261111] [--tasks 30]

Nothing here calls a model. Token counts, gate verdicts and outcomes are drawn
from a seeded random generator with made-up solve probabilities, so the log
says nothing about how any real model performs. Every ``task.created`` payload
carries ``"synthetic": true`` and the exporter turns that into a banner on the
page.

The log is written through ``contraprova.log.EventLog`` (the single writer), so
it passes ``contraprova verify``. Timestamps are derived from the sequence
number, so the same seed always produces a byte-identical file.

Conventions used (the replay UI relies on them):

* ``task.created`` is shared by all arms; ``max_attempts`` is 2.
* Arms A, B and C only record ``usage.recorded`` and ``outcome.scored``.
* Arm D (governed) goes through route -> claim -> submit -> gate, up to twice.
* When D's first gate blocks, the log is forked at that gate event with
  ``contraprova.forks.fork`` and the task is re-run twice: a ``control`` fork
  (same cheap model) and a ``treatment`` fork (stronger model). Payloads follow
  docs/events.md: fork usage carries the ``fork_id``, and each re-run is scored
  as a fork outcome keyed by ``fork_id``, so it never overwrites or counts
  toward D's own outcome. The exporter labels the re-runs ``D:control`` and
  ``D:treatment`` from the fork's role, for display only.
* There is one task per exercise, shared by every arm, so the kernel task id
  and the pairing key are the same string.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contraprova.forks import fork  # noqa: E402
from contraprova.log import EventLog  # noqa: E402
from contraprova.usage import usage_payload  # noqa: E402

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"
LANGUAGES = ["python", "go", "rust", "javascript", "java", "cpp"]
EXERCISES = [
    "bowling", "react", "zipper", "forth", "poker", "grep", "ledger", "wordy",
    "phone-number", "book-store", "tree-building", "dominoes", "connect",
    "variable-length-quantity", "list-ops", "sgf-parsing", "two-bucket",
    "pov", "rest-api", "transpose",
]
# Made-up base solve probabilities. Not estimates of anything.
P_SOLVE = {OPUS: 0.88, SONNET: 0.80, HAIKU: 0.58}
ROUTER_HASH = hashlib.sha256(b"synthetic-demo-router-v0").hexdigest()
T0 = datetime(2026, 11, 11, 9, 0, 0, tzinfo=timezone.utc)


class Writer:
    def __init__(self, path: Path):
        self.log = EventLog(path)
        self.n = 0

    def ts(self) -> str:
        return (T0 + timedelta(seconds=7 * self.n)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def emit(self, type_: str, actor: str, payload: dict):
        ev = self.log.append(type_, actor, payload, self.ts())
        self.n += 1
        return ev

    def fork(self, tid: str, from_seq: int, model: str, role: str, seed: int) -> str:
        fid = fork(self.log, tid, from_seq, model, role, seed, actor="forker", ts=self.ts(), extra={"arm": "D"})
        self.n += 1
        return fid


def tokens(rng: random.Random, model: str, role: str) -> dict:
    base_in = {"orchestrator": 1800, "reviewer": 3500}.get(role, 6000)
    usage = {
        "input_tokens": int(base_in * rng.uniform(0.5, 1.8)),
        "output_tokens": int((350 if role == "orchestrator" else 1400) * rng.uniform(0.4, 2.2)),
        "cache_read_input_tokens": int(rng.choice([0, 0, 2048, 4096]) * rng.uniform(0.8, 1.2)),
        "cache_creation_input_tokens": rng.choice([0, 0, 0, 1024]),
    }
    if model == OPUS and role == "worker":
        usage["output_tokens"] = int(usage["output_tokens"] * 0.8)
    return usage


def solves(rng: random.Random, model: str, difficulty: float) -> bool:
    return rng.random() < max(0.03, P_SOLVE[model] - 0.55 * difficulty)


def run_simple_arm(w: Writer, rng: random.Random, arm: str, tid: str, d: float) -> None:
    if arm == "A":
        plan = [(OPUS, "worker")] * rng.choice([1, 1, 2])
        model = OPUS
    elif arm == "B":
        plan = [(HAIKU, "worker")] * rng.choice([1, 2, 2])
        model = HAIKU
    else:  # C: ungoverned swarm, Opus orchestrator dispatching Haiku workers
        plan = [(OPUS, "orchestrator")] + [(HAIKU, "worker")] * rng.choice([1, 2, 3])
        model = HAIKU
    for m, role in plan:
        w.emit("usage.recorded", f"runner-{arm}", usage_payload(tokens(rng, m, role), m, arm=arm, role=role, task_id=tid))
    # Several workers give the swarm a small edge over a single Haiku call.
    bonus = 0.06 if arm == "C" else 0.0
    w.emit("outcome.scored", "scorer", {"arm": arm, "task_id": tid, "solved": solves(rng, model, d - bonus)})


def run_governed(w: Writer, rng: random.Random, tid: str, lang: str, d: float) -> None:
    w.emit("usage.recorded", "orchestrator", usage_payload(tokens(rng, OPUS, "orchestrator"), OPUS, arm="D", role="orchestrator", task_id=tid))
    passed = False
    blocks = 0
    for attempt in (1, 2):
        p_haiku = 0.9 if blocks == 0 else 0.3
        action = HAIKU if rng.random() < p_haiku else SONNET
        w.emit(
            "route.decided",
            "router",
            {
                "task_id": tid,
                "action": action,
                "propensities": {HAIKU: p_haiku, SONNET: round(1 - p_haiku, 10)},
                "router_hash": ROUTER_HASH,
                "features": {"language": lang, "attempt": attempt, "prior_blocks": blocks},
            },
        )
        worker = f"{'haiku' if action == HAIKU else 'sonnet'}-worker-{rng.randint(1, 4)}"
        w.emit("task.claimed", worker, {"task_id": tid, "worker": worker})
        for _ in range(rng.choice([1, 1, 2])):
            w.emit("usage.recorded", worker, usage_payload(tokens(rng, action, "worker"), action, arm="D", role="worker", task_id=tid))
        w.emit("task.submitted", worker, {"task_id": tid, "files": [f"{lang}/{tid.split('/')[-1]}"]})
        if rng.random() < 0.25:
            w.emit("usage.recorded", "reviewer", usage_payload(tokens(rng, SONNET, "reviewer"), SONNET, arm="D", role="reviewer", task_id=tid))
        ok = solves(rng, action, d - 0.05)
        if ok:
            gate = w.emit("gate.decided", "gate", {"task_id": tid, "gate": "visible-tests", "verdict": "pass", "reason": "all visible tests pass"})
            passed = True
            break
        gate_name, reason = rng.choice(
            [("visible-tests", "visible tests fail"), ("scope", "edited a file outside the declared scope"), ("protected-tests", "modified a protected test file")]
        )
        gate = w.emit("gate.decided", "gate", {"task_id": tid, "gate": gate_name, "verdict": "block", "reason": reason})
        blocks += 1
        if blocks == 1:
            fork_counterfactuals(w, rng, tid, d, gate.seq)
    # Hidden score tests occasionally disagree with the gate.
    solved = passed and rng.random() > 0.06
    w.emit("outcome.scored", "scorer", {"arm": "D", "task_id": tid, "log_task_id": tid, "solved": solved})


def fork_counterfactuals(w: Writer, rng: random.Random, tid: str, d: float, from_seq: int) -> None:
    for role, model in (("control", HAIKU), ("treatment", SONNET)):
        seed = rng.randint(0, 2**31 - 1)
        fid = w.fork(tid, from_seq, model, role, seed)
        for _ in range(rng.choice([1, 2])):
            w.emit("usage.recorded", f"fork-{role}",
                   usage_payload(tokens(rng, model, "worker"), model, arm="D", role="worker", task_id=tid, fork_id=fid))
        # A task that already failed once is harder than average for its model.
        w.emit("outcome.scored", "scorer",
               {"fork_id": fid, "task_id": tid, "log_task_id": tid, "arm": "D", "solved": solves(rng, model, d + 0.15)})


def build(path: Path, seed: int = 20261111, n_tasks: int = 30) -> int:
    if path.exists():
        path.unlink()
    rng = random.Random(seed)
    w = Writer(path)
    tasks = []
    for i in range(n_tasks):
        lang = LANGUAGES[i % len(LANGUAGES)]
        ex = EXERCISES[(i * 7) % len(EXERCISES)]
        tid = f"{lang}/{ex}"
        d = round(rng.uniform(0.0, 1.0), 3)
        tasks.append((tid, lang, d))
    for tid, lang, d in tasks:
        w.emit(
            "task.created",
            "orchestrator",
            {"task_id": tid, "title": f"SYNTHETIC: {tid}", "kind": "code", "scope": [f"{lang}/{tid.split('/')[-1]}"], "max_attempts": 2, "synthetic": True, "language": lang},
        )
        for arm in ("A", "B", "C"):
            run_simple_arm(w, rng, arm, tid, d)
        run_governed(w, rng, tid, lang, d)
    return w.n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write a seeded SYNTHETIC demo log (no models are called).")
    ap.add_argument("out", type=Path)
    ap.add_argument("--seed", type=int, default=20261111)
    ap.add_argument("--tasks", type=int, default=30)
    args = ap.parse_args(argv)
    n = build(args.out, args.seed, args.tasks)
    print(f"wrote {n} SYNTHETIC events to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
