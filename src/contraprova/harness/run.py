"""Run one arm over one split of the frozen partition.

    python -m contraprova.harness.run --arm D --split pilot --polyglot PATH \\
        --log runs/x.jsonl --budget-usd 5 [--dry-run]

``--dry-run`` swaps in offline fakes (a client that echoes the stub files back,
a uniform router, a scope-plus-coin-flip gate and a scorer that executes
nothing), so the whole loop runs at $0. The fakes report synthetic token
counts, so the budget kill switch is exercised too.

A real run takes its parts by import path (``module:attr``; a class is
instantiated with no arguments, anything else is used as is): ``--client``,
``--router`` and ``--gates`` (arm D), and ``--scorer``.
``local_python_scorer`` below is one scorer you may name; it executes model
output and tests, so use it only inside a sandbox.

The Messages API client: ``--client anthropic`` builds one
``contraprova.llm.AnthropicClient`` per harness role (worker, orchestrator,
reviewer; keys ``CONTRAPROVA_KEY_<ROLE>``, then ``PROJECTS_ANTHROPIC_KEY``, then ``ANTHROPIC_API_KEY``) that
share one ``llm.Budget`` capped at ``--budget-usd`` and seeded with the spend
already recorded in the log (:func:`anthropic_role_clients`). The harness
writes ``usage.recorded`` itself, so these clients get no ``on_usage``
callback and are not wrapped in ``RecordingClient``; the harness refuses a
client that would record usage too. ``--client contraprova.llm:AnthropicClient``
also works, but then every role uses the worker key and only the log-based
kill switch applies. Nothing here calls the API until a run starts.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import check_scope
from ..interfaces import HAIKU, SONNET, Completion, GateResult, LLMClient, Message, RouteDecision, Submission
from ..llm import AnthropicClient, Budget
from ..log import EventLog
from .arms import ARMS, ArmConfig, BudgetGuard, Harness, ScoreResult, TaskContext, format_files, parse_files, run_arm
from .polyglot import (
    DEFAULT_SEED,
    SPLITS,
    Partition,
    TestSplit,
    build_workspace,
    dataset_sha256,
    load_exercises,
    make_partition,
    run_python_tests,
    sha256_text,
)

# ------------------------------------------------------------------ offline fakes


class FakeClient:
    """Answers offline. Worker prompts get the solution files from the prompt echoed back
    (or ``edits`` if given, keyed by path); orchestrator prompts get a canned brief; review
    prompts get ``review``. Usage is synthetic: about four characters per token."""

    def __init__(self, edits: Mapping[str, str] | None = None, review: str = "ACCEPT",
                 tokens_per_call: tuple[int, int] | None = None):
        self.edits = dict(edits or {})
        self.review = review
        self.tokens_per_call = tokens_per_call
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, model: str, system: str, messages: Sequence[Message], max_tokens: int,
                 effort: str | None = None, tags: Mapping[str, Any] | None = None) -> Completion:
        prompt = messages[-1].content
        role = (tags or {}).get("role", "")
        if role == "orchestrator":
            text = "Brief: read the tests first, handle empty input, keep the given signatures."
        elif role == "reviewer":
            text = self.review
        else:
            section = prompt.split("## Solution files", 1)[-1].split("## Tests you can see", 1)[0]
            files = parse_files(section).files
            files.update(self.edits)
            text = "Here is my solution.\n\n" + format_files(files)
        self.calls.append({"model": model, "role": role, "effort": effort, "tags": dict(tags or {}), "prompt": prompt})
        tin, tout = self.tokens_per_call or (len(system + prompt) // 4 + 1, len(text) // 4 + 1)
        usage = {"model": model, "input_tokens": tin, "output_tokens": min(tout, max_tokens),
                 "cache_read_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0}
        return Completion(text=text, model=model, usage=usage, request_id=f"fake-{len(self.calls)}")


class UniformRouter:
    """Random over ``actions`` with equal propensities (the exploration policy)."""

    def __init__(self, actions: Sequence[str] = (HAIKU, SONNET)):
        self.actions = tuple(actions)
        self.hash = sha256_text("uniform:" + ",".join(self.actions))

    def decide(self, features: Mapping[str, float], *, rng_seed: int) -> RouteDecision:
        p = 1 / len(self.actions)
        return RouteDecision(self.actions[rng_seed % len(self.actions)], {a: p for a in self.actions}, self.hash,
                             dict(features))


class FixedRouter:
    """Always the same action, with propensity 1."""

    def __init__(self, action: str = HAIKU):
        self.action = action

    def decide(self, features: Mapping[str, float], *, rng_seed: int) -> RouteDecision:
        return RouteDecision(self.action, {self.action: 1.0}, sha256_text("fixed:" + self.action), dict(features))


def dry_run_gates(sub: Submission, ctx: TaskContext) -> list[GateResult]:
    """Scope check against the lane contract, plus a deterministic coin flip that blocks
    about half of first attempts on the initial task, so the fork path runs too."""
    out = []
    if ctx.contract is not None:
        v = check_scope(ctx.contract, list(sub.files))
        out.append(GateResult("T0.scope", "block" if v else "pass", "; ".join(f"{x.path}: {x.reason}" for x in v)))
    flip = int(sha256_text(ctx.task_id)[:2], 16) % 2 == 0 and ctx.task_id.count(":") == 1
    out.append(GateResult("dry.coin", "block" if flip else "pass", "dry-run coin flip" if flip else ""))
    return out


def no_exec_scorer(sub: Submission, ctx: TaskContext) -> ScoreResult:
    """Executes nothing; every task counts as unsolved."""
    return ScoreResult(False, {"dry_run": True})


def local_python_scorer(sub: Submission, ctx: TaskContext) -> ScoreResult:
    """Run the score subset of a Python exercise's tests against the submission with pytest.

    Executes model output: use only inside a sandbox. Non-Python exercises are not handled.
    """
    ex = ctx.exercise
    if ex.language != "python":
        raise NotImplementedError("local_python_scorer only handles Python exercises")
    tests = ctx.test_files("score")
    with tempfile.TemporaryDirectory() as d:
        build_workspace(ex, sub.files, tests, d)
        ok, tail = run_python_tests(d, sorted(tests))
    return ScoreResult(ok, {"runner": "pytest", "n_score_tests": len(ctx.test_split.score),
                            "output_sha256": sha256_text(tail)})


# ------------------------------------------------------------------ real client

HARNESS_ROLES: tuple[str, ...] = ("worker", "orchestrator", "reviewer")


class RoleClients:
    """An LLMClient that sends each request to the client for ``tags["role"]`` (default ``worker``)."""

    def __init__(self, clients: Mapping[str, LLMClient], default: str = "worker"):
        if default not in clients:
            raise ValueError(f"no client for the default role {default!r}")
        self.clients = dict(clients)
        self.default = default

    def complete(self, *, model: str, system: str, messages: Sequence[Message], max_tokens: int,
                 effort: str | None = None, tags: Mapping[str, Any] | None = None) -> Completion:
        role = (tags or {}).get("role") or self.default
        client = self.clients.get(role, self.clients[self.default])
        return client.complete(model=model, system=system, messages=messages, max_tokens=max_tokens,
                               effort=effort, tags=tags)


def anthropic_role_clients(budget: Budget | None, **client_kwargs: Any) -> RoleClients:
    """One AnthropicClient per harness role, all charging the same ``budget``.

    No ``on_usage`` callback is set and nothing is wrapped in RecordingClient: the harness is the
    only writer of ``usage.recorded``. Building the clients makes no request and reads no key.
    """
    if "on_usage" in client_kwargs:
        raise ValueError("the harness records usage itself; do not pass on_usage")
    return RoleClients({r: AnthropicClient(r, budget=budget, **client_kwargs) for r in HARNESS_ROLES})


# ------------------------------------------------------------------ CLI


def load_object(spec: str):
    """``"package.module:attr"`` -> the attribute, instantiated with no arguments if it is a class."""
    mod, _, attr = spec.partition(":")
    if not attr:
        raise ValueError(f"expected module:attr, got {spec!r}")
    obj = getattr(importlib.import_module(mod), attr)
    return obj() if callable(obj) and isinstance(obj, type) else obj


def load_or_create_partition(path: Path, exercises, seed: int) -> Partition:
    if path.exists():
        part = Partition.load(path)
        if part.dataset_sha256 != dataset_sha256(exercises):
            raise SystemExit(f"{path}: frozen for a different dataset (dataset_sha256 mismatch)")
        return part
    part = make_partition(exercises, seed)
    part.save(path)
    return part


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m contraprova.harness.run", description=__doc__.split("\n")[0])
    ap.add_argument("--arm", required=True, choices=ARMS)
    ap.add_argument("--split", required=True, choices=SPLITS)
    ap.add_argument("--polyglot", required=True, help="path to a polyglot-benchmark checkout")
    ap.add_argument("--log", required=True)
    ap.add_argument("--budget-usd", type=float, required=True, help="cap on total recorded spend in this log")
    ap.add_argument("--dry-run", action="store_true", help="offline fakes; no API calls, nothing executed")
    ap.add_argument("--partition", default=None, help="partition JSON (default: <log dir>/partition.json)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--limit", type=int, default=None, help="only the first N exercises of the split")
    ap.add_argument("--client", help="'anthropic' (Messages API, one client per role, shared budget) or "
                    "module:attr of an LLMClient (class or instance)")
    ap.add_argument("--router", help="module:attr of a Router (arm D)")
    ap.add_argument("--gates", help="module:attr of a gate callable (arm D)")
    ap.add_argument("--scorer", help="module:attr of a scorer callable")
    ap.add_argument("--exclude", action="append", default=[], metavar="EXERCISE_ID",
                    help="leave this exercise out (repeatable); reported in the summary")
    ap.add_argument("--exclude-unrunnable", action="store_true",
                    help="leave out exercises whose tests cannot run here (contraprova.harness.runners)")
    args = ap.parse_args(argv)

    exercises = load_exercises(args.polyglot)
    log_path = Path(args.log)
    part_path = Path(args.partition) if args.partition else log_path.parent / "partition.json"
    part = load_or_create_partition(part_path, exercises, args.seed)
    by_id = {e.id: e for e in exercises}
    chosen = [by_id[i] for i in part.splits[args.split]][: args.limit]
    excluded: dict[str, str] = {}
    for ex in chosen:
        if ex.id in args.exclude:
            excluded[ex.id] = "excluded on the command line"
        elif args.exclude_unrunnable:
            from .runners import exercise_unavailable

            why = exercise_unavailable(ex)
            if why:
                excluded[ex.id] = why
    chosen = [ex for ex in chosen if ex.id not in excluded]
    splits = {e.id: TestSplit.from_dict(e.id, part.test_splits[e.id]) for e in chosen}

    if args.dry_run:
        client, router, gates, scorer = FakeClient(), UniformRouter(), dry_run_gates, no_exec_scorer
    else:
        missing = [n for n in ("client", "scorer") if not getattr(args, n)]
        if args.arm == "D":
            missing += [n for n in ("router", "gates") if not getattr(args, n)]
        if missing:
            ap.error("a real run needs " + ", ".join(f"--{m}" for m in missing) + " (or use --dry-run)")
        guard = BudgetGuard(args.budget_usd)
        if args.client == "anthropic":
            spent = guard.spent(EventLog(log_path))
            client = anthropic_role_clients(Budget(args.budget_usd, spent_usd=spent))
        else:
            client = load_object(args.client)
        scorer = load_object(args.scorer)
        router = load_object(args.router) if args.router else None
        gates = load_object(args.gates) if args.gates else None

    h = Harness(EventLog(log_path), client, scorer, gates, router, BudgetGuard(args.budget_usd), ArmConfig(),
                part.sha256)
    summary = run_arm(h, args.arm, chosen, splits, args.split)
    print(json.dumps({**summary.__dict__, "partition_sha256": part.sha256, "dry_run": args.dry_run,
                      "excluded": excluded}, indent=2))
    return 2 if summary.stopped else 0


if __name__ == "__main__":
    sys.exit(main())
