"""Command line for SWE-bench Pro: prepare data, validate the scorer at $0, pre-flight and run arms.

    python -m contraprova.harness.run --benchmark swepro --split probe --arms B,A \\
        --log runs/pro.jsonl --budget-usd 2.5 [--dry-run] [--client anthropic] [--max-image-gb 20]

(``python -m contraprova.harness.swepro_run`` takes the same arguments.) Actions (``--action``):

``run`` (default)
    Arms over the split, tasks outermost and arms inside, resuming from the log. A real run needs
    Docker (the daemon is started if it is installed but not running, and stopped again at the
    end) and ``--client``; arm D also needs ``--router``. ``--dry-run`` uses offline fakes: a
    client whose workers reply with the gold patch as SEARCH/REPLACE blocks (or nothing, with
    ``--dry-solve none``), a uniform router and a verifier that executes nothing.

``--probe``
    The feasibility probe: single Haiku twice (arms B and B2, two independent samples) and single
    Opus at low effort (arm A) on the 12 probe tasks, max_tokens 16K (Haiku) / 12K (Opus). Tasks run
    in ascending order of worker-prompt size, each task's three requests together (one image pull
    per task), so a budget stop loses only the largest tasks. Before any request a pre-flight counts
    every prompt with the free ``count_tokens`` endpoint, prices it at the expected output
    (``swepro_arms.preflight``) and refuses to start if expected cost x ``--preflight-margin``
    (default 1.2) exceeds ``--budget-usd``. ``--action preflight`` prints the pre-flight only.

``prepare``
    Download the dataset rows (if missing), the split's verifier files and context files (from
    each image, one pull at a time; ``--delete-images`` removes each image afterwards).

``validate``
    The $0 scorer check: per task, pull the image, run the empty patch (must score 0) and the gold
    patch pushed through the SEARCH/REPLACE edit path (must score 1), delete the image, and report
    image size, pull time, test time, results and prompt sizes (``--out`` writes JSON).

``--lanes`` (Contract Lanes, docs/lanes-plan.md)
    Arms La and D (D as the main study's cascade) on ``--split lanes-probe`` or ``lanes-main``.
    Before any paid request a pre-flight counts the planner prompt and every lane prompt of a
    by-file split (``--lanes-k``, default 3) and D's prompts with the free ``count_tokens``
    endpoint, prices them (``lanes.lane_preflight``, ``swepro_arms.main_preflight``), drops tasks
    with a counted prompt above ``--max-prompt-tokens`` and refuses to start (exit 3) if expected
    cost x margin exceeds ``--budget-usd``. ``--action preflight --lanes`` prints it only.

``select-lanes``
    Freeze the Contract Lanes task lists (tiers, lanes-main, lanes-probe) with their sha256 in
    ``--lanes-splits`` (default ``<data>/lanes-splits.json``).

``validate-lanes``
    The $0 check for arm La: per task, the empty/gold check plus the gold-lane check (the gold patch
    split by file into lanes through the lane applier, the governed and shadow merges and
    T0.interfaces; both merges must score 1 and a conflicting out-of-scope lane must be rejected).

Data lives in ``--data`` (default ``data/swepro``): ``tasks.jsonl``, ``verifiers/``, ``context/``,
``partition.json`` and the image cache state. None of it is committed (upstream licences).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..interfaces import HAIKU, OPUS, SONNET
from ..llm import AnthropicClient, Budget
from ..log import EventLog
from .arms import BudgetGuard
from .docker import GB, ImageCache, ensure_dockerd, image_head, run_verifier, stop_dockerd, system_df
from .swepro import (
    DEFAULT_SEED,
    MAX_WORKER_PROMPT_TOKENS,
    LANE_SPLITS,
    SPLITS,
    LaneSplits,
    ProExercise,
    ProTestSplit,
    fetch_dataset,
    load_context,
    load_or_create_partition,
    load_tasks,
    context_bytes,
    make_lane_splits,
    scored_tasks,
    verifier_dir,
)
from .lanes import LaneAssumptions, LaneConfig, by_file_plan, gold_lane_check, lane_preflight
from .swepro_arms import (
    DEFAULT_T2_MAX_LINES,
    PRO_ARMS,
    ProFakeClient,
    ProGates,
    ProHarness,
    ProScorer,
    Verifier,
    gold_files,
    gold_oracle,
    main_preflight,
    preflight,
    pro_arm_config,
    run_tasks,
)

PROBE_ARMS = ("B", "B2", "A")
PROBE_PLAN = ((HAIKU, 2), (OPUS, 1))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m contraprova.harness.run --benchmark swepro",
                                 description="SWE-bench Pro (py/js subset) harness")
    ap.add_argument("--benchmark", default="swepro", choices=["swepro"])
    ap.add_argument("--action", default="run",
                    choices=["run", "prepare", "validate", "preflight", "select-lanes", "validate-lanes"])
    ap.add_argument("--lanes", action="store_true",
                    help="Contract Lanes study: arms La,D by default, D as the main study's cascade, the lane "
                         "pre-flight before any paid request (docs/lanes-plan.md)")
    ap.add_argument("--max-lanes", type=int, default=4, help="arm La: most lanes the planner may choose (cap 4)")
    ap.add_argument("--lanes-k", type=int, default=3,
                    help="lanes in the naive by-file split (lane pre-flight, gold-lane validation, dry-run planner)")
    ap.add_argument("--lanes-splits", default=None,
                    help="frozen Contract Lanes task lists (default: <data>/lanes-splits.json; --action select-lanes)")
    ap.add_argument("--main90", default="results/swepro-main/eligible-90.txt",
                    help="select-lanes: the main study's eligible task list")
    ap.add_argument("--main-log", default="results/swepro-main/log.jsonl",
                    help="select-lanes: the main study's log (tasks with A and D outcomes)")
    ap.add_argument("--p-block", type=float, default=0.45, help="lane pre-flight: assumed first-merge block rate")
    ap.add_argument("--split", default="probe", choices=SPLITS)
    ap.add_argument("--arms", default="B", help=f"comma-separated, from {','.join(PRO_ARMS)}")
    ap.add_argument("--probe", action="store_true", help="the feasibility probe (arms B,B2,A; split --probe-split)")
    ap.add_argument("--probe-split", default="probe", choices=["probe", "probe2"],
                    help="which probe set --probe and --action preflight run (default: probe)")
    ap.add_argument("--haiku-max-tokens", type=int, default=None,
                    help="worker max_tokens for Haiku (default: pro_arm_config's 32K)")
    ap.add_argument("--opus-max-tokens", type=int, default=None,
                    help="worker max_tokens for Opus (default: pro_arm_config's 32K)")
    ap.add_argument("--cascade", action="store_true",
                    help="main-study design: arm D is the fixed cascade (Haiku first; after a gate block a Haiku "
                         "control and an Opus treatment fork, D ships the Opus branch), Opus workers at low effort, "
                         "and D's first Haiku attempt is also scored as a derived arm B")
    ap.add_argument("--task-file", default=None, help="run exactly these task ids (one per line, in order)")
    ap.add_argument("--eligible-out", default=None, help="preflight --cascade: write the eligible task ids here")
    ap.add_argument("--validate-before-run", action="store_true",
                    help="run the $0 empty/gold check on each task right before its arms; exclude failures")
    ap.add_argument("--p-fork", type=float, default=0.5, help="preflight --cascade: assumed D gate-block rate")
    ap.add_argument("--repair-rate", type=float, default=0.3, help="preflight --cascade: assumed repair rate")
    ap.add_argument("--no-repair", action="store_true",
                    help="no repair turn after a truncated, unparsable or unappliable reply")
    ap.add_argument("--no-log-contents", action="store_true",
                    help="log per-file sha256 instead of full file contents in task.submitted (publishable logs)")
    ap.add_argument("--data", default="data/swepro", help="data directory (tasks, verifiers, context, partition)")
    ap.add_argument("--log", default=None, help="event log (required for run)")
    ap.add_argument("--partition", default=None, help="partition JSON (default: <data>/partition.json)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--limit", type=int, default=None, help="only the first N tasks of the split")
    ap.add_argument("--task", action="append", default=[], help="only this task id (repeatable)")
    ap.add_argument("--budget-usd", type=float, default=None, help="cap on total recorded spend in the log")
    ap.add_argument("--dry-run", action="store_true", help="offline fakes; no API calls, nothing executed")
    ap.add_argument("--dry-solve", default="all", choices=["all", "none", "escalate", "conflict"],
                    help="dry-run workers: gold, nothing, 'escalate' (Haiku fails D's worker and La's last lane, "
                         "so D forks and La repairs then escalates that lane) or 'conflict' (La's first lane also "
                         "writes an out-of-scope block)")
    ap.add_argument("--client", help="'anthropic' (one Messages API client per role, shared budget) or module:attr")
    ap.add_argument("--router", help="module:attr of a Router (arm D)")
    ap.add_argument("--no-t2-review", action="store_true", help="no T2.review judge in arm D")
    ap.add_argument("--t2-max-lines", type=int, default=DEFAULT_T2_MAX_LINES,
                    help="T2.review is required above this many changed lines (default 500)")
    ap.add_argument("--max-image-gb", type=float, default=20.0, help="disk cap for cached task images")
    ap.add_argument("--delete-images", action="store_true", help="delete each task's image when it is done")
    ap.add_argument("--context-source", default=None, choices=["image", "raw"],
                    help="where context files come from (default: image; raw for dry runs)")
    ap.add_argument("--test-timeout", type=float, default=1800.0, help="seconds per verifier run")
    ap.add_argument("--max-prompt-tokens", type=int, default=MAX_WORKER_PROMPT_TOKENS,
                    help="exclude tasks whose estimated worker prompt exceeds this (default 90000)")
    ap.add_argument("--preflight-margin", type=float, default=1.2)
    ap.add_argument("--out", default=None, help="validate: write the per-task results here (JSON)")
    return ap


def _tasks(args) -> tuple[list, Any]:
    data = Path(args.data)
    tasks_path = data / "tasks.jsonl"
    if not tasks_path.exists():
        print(f"downloading the dataset rows to {tasks_path}", file=sys.stderr)
        fetch_dataset(tasks_path)
    tasks = load_tasks(tasks_path)
    part = load_or_create_partition(Path(args.partition) if args.partition else data / "partition.json", tasks, args.seed)
    return tasks, part


def _lanes_path(args) -> Path:
    return Path(args.lanes_splits) if args.lanes_splits else Path(args.data) / "lanes-splits.json"


def _lane_splits(args, part) -> LaneSplits:
    p = _lanes_path(args)
    if not p.exists():
        raise SystemExit(f"{p} does not exist; freeze the Contract Lanes task lists with --action select-lanes")
    ls = LaneSplits.load(p)
    if ls.base_partition_sha256 != part.sha256:
        raise SystemExit(f"{p} was frozen against partition {ls.base_partition_sha256[:12]}, not {part.sha256[:12]}")
    return ls


def _split_ids(args, part) -> list[str]:
    if args.split in LANE_SPLITS:
        return list(_lane_splits(args, part).splits[args.split])
    return list(part.splits[args.split])


def _selected(args, part) -> list[str]:
    ids = _split_ids(args, part)
    if args.task:
        ids = [i for i in ids if i in set(args.task)]
    return ids[: args.limit] if args.limit else ids


def _cache(args) -> ImageCache:
    return ImageCache(int(args.max_image_gb * GB), state_path=Path(args.data) / "images.json")


def _exercise(task, part, args, h: ProHarness | None, cache: ImageCache | None, *, with_verifier: bool) -> ProExercise | str:
    why = task.unsupported_gold()
    if why:
        return why
    source = args.context_source or "image"
    ctx = load_context(task, args.data, source=source, ensure_image=cache.ensure if cache else None)
    if ctx.missing:
        return f"context files missing from the {ctx.source}: {', '.join(ctx.missing)}"
    if not ctx.files and not ctx.new_files:
        return "the gold patch changes no non-test file"
    vdir = verifier_dir(task, args.data) if with_verifier else None
    ts = ProTestSplit.from_dict(task.instance_id, part.test_splits[task.instance_id])
    ex = ProExercise(task, ctx, ts, vdir)
    if h is not None:
        sizes = h.prompt_sizes(ex, ts)
        ex.meta.update(sizes)
        if sizes["worker_prompt_tokens_est"] > args.max_prompt_tokens:
            return f"worker prompt ~{sizes['worker_prompt_tokens_est']} tokens > {args.max_prompt_tokens}"
    return ex


# ------------------------------------------------------------------ actions


def action_prepare(args) -> int:
    tasks, part = _tasks(args)
    by_id = {t.instance_id: t for t in tasks}
    started = ensure_dockerd() if (args.context_source or "image") == "image" else None
    cache = _cache(args) if (args.context_source or "image") == "image" else None
    try:
        for tid in _selected(args, part):
            t = by_id[tid]
            verifier_dir(t, args.data)
            ctx = load_context(t, args.data, source=args.context_source or "image",
                               ensure_image=cache.ensure if cache else None)
            if args.delete_images and cache is not None:
                cache.remove(t.image)
            print(json.dumps({"task": tid, "files": len(ctx.files), "chars": ctx.chars(), "missing": list(ctx.missing)}))
    finally:
        stop_dockerd(started)
    return 0


def gold_lanes_row(ex: ProExercise, args, k: int) -> dict[str, Any]:
    """The plan's gold-lane check on the real verifier: the gold patch split by file into ``k`` lanes,
    pushed through the lane applier, the governed merge, T0.interfaces and the shadow merge (both
    must score 1), then a conflicting lane (an out-of-scope block breaking another lane's file) that
    the governed merge must reject. Each distinct file set runs the verifier once."""
    memo: dict[str, Any] = {}

    def score(files):
        key = json.dumps(dict(files), sort_keys=True)
        if key not in memo:
            memo[key] = run_verifier(ex.task.image, ex.verifier, dict(files), timeout=args.test_timeout).reward
        return memo[key]

    row = gold_lane_check(ex, by_file_plan(ex, k), score)
    row["verifier_runs"] = len(memo)
    return row


def validate_exercise(ex: ProExercise, h: ProHarness, args, cache: ImageCache | None = None,
                      lanes_k: int | None = None) -> dict[str, Any]:
    """The $0 check for one loaded task: the empty patch must score 0 and fail the visible subset,
    the gold patch through the edit path must score 1 and pass both gate subsets by per-test status.
    Returns a row with ``ok``. The image is pulled through ``cache`` first, so the cache owns it and
    ``--delete-images`` can remove it (a bare ``docker run`` would pull it outside the cache)."""
    t = ex.task
    row: dict[str, Any] = {"task": t.instance_id, "repo": t.repo}
    try:
        if cache is not None:
            cache.ensure(t.image)
        row["head_is_base"] = image_head(t.image) == t.base_commit
        empty = run_verifier(t.image, ex.verifier, {}, timeout=args.test_timeout)
        files, errors = gold_files(ex, scope=h.scope(ex))
        gold = run_verifier(t.image, ex.verifier, files, timeout=args.test_timeout)
        ts = ex.test_split
        row.update(empty_reward=empty.reward, gold_reward=gold.reward, gold_edit_errors=list(errors),
                   gold_visible_ok=not gold.failed(ts.visible), gold_private_ok=not gold.failed(ts.gate_private),
                   empty_visible_ok=not empty.failed(ts.visible), empty_s=round(empty.seconds, 1),
                   gold_s=round(gold.seconds, 1))
        row["ok"] = (row["head_is_base"] and empty.reward == 0 and gold.reward == 1 and not errors
                     and bool(gold.statuses) and row["gold_visible_ok"] and row["gold_private_ok"]
                     and not row["empty_visible_ok"])
        if lanes_k and row["ok"]:
            row["gold_lanes"] = gold_lanes_row(ex, args, lanes_k)
            row["ok"] = bool(row["gold_lanes"]["ok"])
    except Exception as e:
        row["ok"] = False
        row["error"] = f"{type(e).__name__}: {e}"
    return row


def action_validate(args) -> int:
    """Empty patch must score 0, gold patch through the edit path must score 1."""
    tasks, part = _tasks(args)
    by_id = {t.instance_id: t for t in tasks}
    started = ensure_dockerd()
    cache = _cache(args)
    h = ProHarness(EventLog(Path(args.data) / "validate-scratch.jsonl"), ProFakeClient({}), lambda s, c: False,
                   config=pro_arm_config(), t2_max_lines=args.t2_max_lines)
    rows = []
    failures = 0
    try:
        for tid in _selected(args, part):
            t = by_id[tid]
            row: dict[str, Any] = {"task": tid, "repo": t.repo}
            try:
                pull = cache.ensure(t.image)
                row.update(image_gb=round(pull.bytes / GB, 2), pull_s=round(pull.seconds, 1), pulled=pull.pulled)
                head = image_head(t.image)
                row["head_is_base"] = head == t.base_commit
                ex = _exercise(t, part, args, h, cache, with_verifier=True)
                if isinstance(ex, str):
                    raise RuntimeError(ex)
                row.update(ex.meta)
                row["f2p"], row["p2p"] = len(ex.test_split.tests), len(ex.test_split.score) - len(ex.test_split.tests)
                row["visible"], row["gate_private"] = len(ex.test_split.visible), len(ex.test_split.gate_private)
                empty = run_verifier(t.image, ex.verifier, {}, timeout=args.test_timeout)
                row.update(empty_reward=empty.reward, empty_s=round(empty.seconds, 1))
                files, errors = gold_files(ex, scope=h.scope(ex))
                row["gold_edit_errors"] = list(errors)
                row["gold_files"] = len(files)
                gold = run_verifier(t.image, ex.verifier, files, timeout=args.test_timeout)
                ts = ex.test_split
                row.update(gold_reward=gold.reward, gold_s=round(gold.seconds, 1),
                           gold_visible_ok=not gold.failed(ts.visible), gold_private_ok=not gold.failed(ts.gate_private),
                           empty_visible_ok=not empty.failed(ts.visible))
                if gold.reward != 1:
                    row["gold_missing"] = gold.failed(ts.score)[:20]
                    row["gold_tail"] = gold.output_tail[-1500:]
                row["gold_statuses"] = len(gold.statuses)
                # The gates read per-test statuses, so gold must also pass the visible and private
                # subsets by those statuses, and the empty patch must fail the visible subset.
                row["ok"] = (empty.reward == 0 and gold.reward == 1 and not errors and bool(gold.statuses)
                             and row["gold_visible_ok"] and row["gold_private_ok"] and not row["empty_visible_ok"])
            except Exception as e:  # report and continue with the next task
                row["ok"] = False
                row["error"] = f"{type(e).__name__}: {e}"
            finally:
                cache.remove(t.image)  # validation always deletes the image it pulled
            failures += 0 if row.get("ok") else 1
            rows.append(row)
            print(json.dumps({k: v for k, v in row.items() if k != "gold_tail"}), flush=True)
        df = system_df()
    finally:
        stop_dockerd(started)
    out = {"tasks": rows, "failures": failures, "docker_system_df": df}
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return 1 if failures else 0


def _client(args, log_path: Path):
    if args.client == "anthropic":
        from .run import anthropic_role_clients

        spent = BudgetGuard(args.budget_usd).spent(EventLog(log_path))
        return anthropic_role_clients(Budget(args.budget_usd, spent_usd=spent))
    from .run import load_object

    return load_object(args.client)


def _count_fn(client):
    """The count_tokens callable of a client (a RoleClients dispatcher uses its worker client)."""
    inner = getattr(client, "clients", {}).get("worker", client) if hasattr(client, "clients") else client
    fn = getattr(inner, "count_tokens", None)
    if fn is None:
        raise SystemExit("the client has no count_tokens method; the probe pre-flight needs one")
    return fn


def _dry_policy(mode: str):
    """The dry-run fake's per-request behaviour for ``--dry-solve escalate|conflict`` (None otherwise)."""
    if mode == "escalate":
        # Haiku fails D's worker (D forks; its Opus treatment solves) and La's last lane (blamed, repaired
        # on Haiku, then escalated to Sonnet, which solves).
        return lambda tid, model, lane: "none" if model == HAIKU and (lane is None or lane[0] == f"L{lane[1]}") else "gold"
    if mode == "conflict":
        return lambda tid, model, lane: "conflict" if lane is not None and lane[0] == "L1" and model == HAIKU else "gold"
    return None


def action_run(args, preflight_only: bool = False) -> int:
    if args.lanes:
        args.cascade = True
        if args.arms == "B":
            args.arms = "La,D"
    if args.probe:
        args.split = args.probe_split
        arms = PROBE_ARMS
    else:
        arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    bad = [a for a in arms if a not in PRO_ARMS]
    if bad:
        raise SystemExit(f"unknown arms {bad}; choose from {PRO_ARMS}")
    if not args.log:
        raise SystemExit("--log is required")
    if args.budget_usd is None:
        raise SystemExit("--budget-usd is required")
    if not args.dry_run and not args.client:
        raise SystemExit("a real run needs --client (or use --dry-run)")
    if "D" in arms and not args.dry_run and not args.router and not args.cascade:
        raise SystemExit("arm D needs --router (or use --dry-run)")
    tasks, part = _tasks(args)
    by_id = {t.instance_id: t for t in tasks}
    ids = _selected(args, part)
    if args.task_file:
        wanted = [ln.strip() for ln in Path(args.task_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
        outside = [i for i in wanted if i not in set(_split_ids(args, part))]
        if outside:
            raise SystemExit(f"--task-file has {len(outside)} ids outside split {args.split!r}: {outside[:3]}")
        ids = wanted[: args.limit] if args.limit else wanted
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if args.context_source is None:  # a dry run never pulls an image unless asked to
        args.context_source = "raw" if args.dry_run else "image"
    started = ensure_dockerd() if (not args.dry_run) else None
    cache = _cache(args) if (args.context_source == "image" or not args.dry_run) else None
    try:
        overrides: dict[str, Any] = {}
        if args.haiku_max_tokens or args.opus_max_tokens:
            by_model = dict(pro_arm_config().worker_max_tokens_by_model)
            if args.haiku_max_tokens:
                by_model[HAIKU] = args.haiku_max_tokens
            if args.opus_max_tokens:
                by_model[OPUS] = args.opus_max_tokens
            overrides["worker_max_tokens_by_model"] = tuple(sorted(by_model.items()))
        if args.cascade:
            overrides.update(fork_branches=(("control-1", "control", HAIKU), ("treatment", "treatment", OPUS)),
                             worker_effort_by_model=((OPUS, "low"),), derive_b=True)
        config = pro_arm_config(**overrides)
        exercises: dict[str, ProExercise] = {}
        if args.dry_run:
            from .run import CascadeRouter, UniformRouter

            client = ProFakeClient(exercises, solve=(lambda i: args.dry_solve != "none"),
                                   policy=_dry_policy(args.dry_solve), lanes_k=args.lanes_k)
            router = CascadeRouter(HAIKU, OPUS) if args.cascade else UniformRouter()
            verifier = Verifier(gold_oracle(exercises))
        else:
            client = _client(args, log_path)
            router = None
            if args.router:
                from .run import load_object

                router = load_object(args.router)
            elif args.cascade:
                from .run import CascadeRouter

                router = CascadeRouter(HAIKU, OPUS)
            if args.client == "anthropic" and not args.no_t2_review:
                config = pro_arm_config(t2_review_model=SONNET, **overrides)
            verifier = Verifier(lambda image, tests, files: run_verifier(image, tests, files, timeout=args.test_timeout),
                                ensure_image=cache.ensure if cache else None)
        h = ProHarness(EventLog(log_path), client, ProScorer(verifier), ProGates(verifier, args.t2_max_lines), router,
                       BudgetGuard(args.budget_usd), config, part.sha256, t2_max_lines=args.t2_max_lines,
                       log_contents=not args.no_log_contents, repair=not args.no_repair)

        excluded: dict[str, str] = {}

        validation_path = log_path.with_name(log_path.stem + ".validation.jsonl")

        def provider(tid: str):
            if tid in exercises:
                return exercises[tid]
            got = _exercise(by_id[tid], part, args, h, cache, with_verifier=not args.dry_run)
            if not isinstance(got, str) and args.validate_before_run and not args.dry_run and not preflight_only:
                row = validate_exercise(got, h, args, cache, lanes_k=args.lanes_k if "La" in arms else None)
                with validation_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
                if not row["ok"]:
                    if cache is not None and args.delete_images:
                        cache.remove(got.task.image)
                    return "failed the $0 empty/gold validation: " + json.dumps(
                        {k: row.get(k) for k in ("empty_reward", "gold_reward", "gold_edit_errors", "head_is_base",
                                                 "error") if k in row})
            if not isinstance(got, str):
                exercises[tid] = got
            return got

        h.lane_config = LaneConfig(max_lanes=args.max_lanes)  # type: ignore[attr-defined]
        pre = None
        if args.lanes and "La" in arms:
            # Contract Lanes: count every planner prompt and every lane prompt of a naive by-file split
            # (free), price La with the plan's cost model and D with the main study's, drop tasks whose
            # counted lane prompt or D worker prompt exceeds the bound, and refuse a run whose expected
            # cost x margin exceeds the cap. Nothing paid is sent before this.
            count = _count_fn(client)
            assume = LaneAssumptions(p_block=args.p_block)
            eligible, la_rows, d_rows = [], [], []
            for tid in ids:
                got = provider(tid)
                if isinstance(got, str):
                    excluded[tid] = got
                    continue
                lr = lane_preflight(h, got, got.test_split, count, h.lane_config, k=args.lanes_k, a=assume)
                dr = (main_preflight(h, [got], count, p_fork=args.p_fork, repair_rate=args.repair_rate)[0]
                      if "D" in arms else None)
                biggest = max([*lr.lane_inputs, lr.planner_input, dr.d_worker_input if dr else 0])
                if biggest > args.max_prompt_tokens:
                    excluded[tid] = f"counted prompt {biggest} tokens > {args.max_prompt_tokens}"
                    exercises.pop(tid, None)
                    continue
                got.meta["prompt_tokens_exact"] = {"planner": lr.planner_input, "lanes_by_file": list(lr.lane_inputs),
                                                   **({"d_worker": dr.d_worker_input, "d_lead": dr.d_lead_input}
                                                      if dr else {})}
                eligible.append(tid)
                la_rows.append(lr)
                d_rows.append(dr)
                if cache is not None and args.delete_images:
                    cache.remove(got.task.image)
            exp_la = sum(r.expected for r in la_rows)
            exp_d = sum(r.expected_d_shipped for r in d_rows if r)
            exp_m = sum(r.expected_d_measurement for r in d_rows if r)
            expected = exp_la + exp_d + exp_m
            parts = {k: round(sum(r.by_part[k] for r in la_rows), 4) for k in (la_rows[0].by_part if la_rows else {})}
            pre = {"n": len(eligible), "order": eligible, "expected_usd": round(expected, 4),
                   "expected_by_arm": {"La": round(exp_la, 4), "D_shipped": round(exp_d, 4),
                                       "D_measurement": round(exp_m, 4)},
                   "la_by_part": parts, "worst_case_usd": round(sum(r.worst for r in la_rows)
                                                                + sum(r.worst for r in d_rows if r), 4),
                   "margin": args.preflight_margin, "cap_usd": args.budget_usd,
                   "fits": expected * args.preflight_margin <= args.budget_usd,
                   "assumptions": {**assume.__dict__, "p_fork": args.p_fork, "repair_rate": args.repair_rate,
                                   "lanes_k": args.lanes_k},
                   "lane_config": h.lane_config.to_dict(),
                   "rows": [{"la": r.__dict__, "d": (d.__dict__ if d else None)} for r, d in zip(la_rows, d_rows)]}
            ids = eligible
            if preflight_only:
                print(json.dumps({"preflight": pre, "excluded": excluded}, indent=2, default=list))
                return 0
            if expected * args.preflight_margin > args.budget_usd:
                print(json.dumps({"preflight": {k: v for k, v in pre.items() if k != "rows"},
                                  "refused": "expected cost x margin exceeds --budget-usd"}, indent=2))
                return 3
        elif preflight_only and args.cascade:
            # Main study: walk the split in its seeded order, keep tasks whose counted worker prompt
            # (Haiku with a brief, Opus without) is within the bound, until --limit are eligible.
            target = args.limit or len(ids)
            eligible, rows = [], []
            for tid in (_split_ids(args, part) if not args.task_file else ids):
                if len(eligible) >= target:
                    break
                got = provider(tid)
                if isinstance(got, str):
                    excluded[tid] = got
                    continue
                r = main_preflight(h, [got], _count_fn(client), p_fork=args.p_fork, repair_rate=args.repair_rate)[0]
                biggest = max(r.a_input, r.d_worker_input)
                if biggest > args.max_prompt_tokens:
                    excluded[tid] = f"counted worker prompt {biggest} tokens > {args.max_prompt_tokens}"
                    continue
                eligible.append(tid)
                rows.append(r)
                exercises.pop(tid, None)
            exp_a = sum(r.expected_a for r in rows)
            exp_d = sum(r.expected_d_shipped for r in rows)
            exp_m = sum(r.expected_d_measurement for r in rows)
            expected = exp_a + exp_d + exp_m
            pre = {"n": len(eligible), "expected_usd": round(expected, 4), "expected_by_part":
                   {"A": round(exp_a, 4), "D_shipped": round(exp_d, 4), "D_measurement": round(exp_m, 4)},
                   "worst_case_usd": round(sum(r.worst for r in rows), 4), "margin": args.preflight_margin,
                   "cap_usd": args.budget_usd, "fits": expected * args.preflight_margin <= args.budget_usd,
                   "assumptions": {"p_fork": args.p_fork, "repair_rate": args.repair_rate},
                   "per_task_expected_usd": round(expected / max(len(rows), 1), 4),
                   "n_that_fits": int(args.budget_usd / args.preflight_margin / max(expected / max(len(rows), 1), 1e-9)),
                   "rows": [r.__dict__ for r in rows]}
            if args.eligible_out:
                Path(args.eligible_out).write_text("".join(i + "\n" for i in eligible), encoding="utf-8")
            print(json.dumps({"preflight": pre, "excluded": excluded}, indent=2))
            return 0
        if args.probe or preflight_only:
            # Order by worker prompt size and count every prompt (free) before any paid request.
            loaded = []
            for tid in ids:
                got = provider(tid)
                if isinstance(got, str):
                    excluded[tid] = got
                else:
                    loaded.append(got)
                    if cache is not None and args.delete_images:
                        cache.remove(got.task.image)
            loaded.sort(key=lambda e: (e.meta["worker_prompt_tokens_est"], e.id))
            ids = [e.id for e in loaded]
            rows = preflight(h, loaded, PROBE_PLAN, _count_fn(client))
            expected = sum(r.expected_usd for r in rows)
            worst = sum(r.worst_usd for r in rows)
            pre = {"expected_usd": round(expected, 4), "worst_case_usd": round(worst, 4),
                   "margin": args.preflight_margin, "cap_usd": args.budget_usd,
                   "input_tokens": {m: sum(r.input_tokens for r in rows if r.model == m) for m, _ in PROBE_PLAN},
                   "order": ids, "rows": [r.__dict__ for r in rows]}
            for e in loaded:
                e.meta["prompt_tokens_exact"] = {r.model: r.input_tokens for r in rows if r.task_id == e.id}
            if preflight_only:
                print(json.dumps({"preflight": pre, "excluded": excluded}, indent=2))
                return 0
            if expected * args.preflight_margin > args.budget_usd:
                print(json.dumps({"preflight": pre, "refused": "expected cost x margin exceeds --budget-usd"}, indent=2))
                return 3

        def after(ex: ProExercise) -> None:
            if cache is not None and args.delete_images:
                cache.remove(ex.task.image)

        t0 = time.monotonic()
        s = run_tasks(h, ids, arms, provider, split=args.split, after_task=after)
        s.excluded.update(excluded)
        print(json.dumps({**s.__dict__, "partition_sha256": part.sha256, "dry_run": args.dry_run,
                          "t2_review_model": config.t2_review_model, "t2_max_lines": args.t2_max_lines,
                          "verifier_runs": verifier.runs, "log_contents": not args.no_log_contents, "repair": not args.no_repair,
                          "worker_max_tokens_by_model": dict(config.worker_max_tokens_by_model), "seconds": round(time.monotonic() - t0, 1),
                          "preflight": {k: v for k, v in (pre or {}).items() if k != "rows"} or None}, indent=2))
        return 2 if s.stopped else 0
    finally:
        stop_dockerd(started)


def action_select_lanes(args) -> int:
    """Freeze the Contract Lanes task lists ($0): the fresh tiers, lanes-main (all tier 1, then
    ``n_tier2`` tier-2 tasks in the partition's seeded order) and the 12-task lanes-probe. Needs the
    context files of every task (``--action prepare`` or a filled ``<data>/context``)."""
    tasks, part = _tasks(args)
    main90 = [ln.strip() for ln in Path(args.main90).read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    missing = [t.instance_id for t in tasks if not (Path(args.data) / "context" / t.instance_id / "meta.json").is_file()]
    if missing:
        raise SystemExit(f"{len(missing)} tasks have no cached context in {args.data}/context (e.g. {missing[0]})")
    ls = make_lane_splits(tasks, part, main90=main90, main90_scored=scored_tasks(args.main_log),
                          source_bytes=lambda t: context_bytes(t, args.data), seed=args.seed)
    path = _lanes_path(args)
    sha = ls.save(path)
    out = {"path": str(path), "sha256": sha, "base_partition_sha256": ls.base_partition_sha256,
           "sizes": {k: len(v) for k, v in ls.splits.items()}, "tiers": {k: len(v) for k, v in ls.tiers.items()},
           "split_sha256": {k: ls.split_sha256(k) for k in ls.splits}}
    if args.out:
        ls.save(args.out)
    print(json.dumps(out, indent=2))
    return 0


def action_validate_lanes(args) -> int:
    """The $0 check for arm La on real images: per task, the empty/gold check and the gold-lane check
    (``gold_lanes_row``); the image is deleted afterwards and the daemon stopped if this started it."""
    tasks, part = _tasks(args)
    by_id = {t.instance_id: t for t in tasks}
    started = ensure_dockerd()
    cache = _cache(args)
    h = ProHarness(EventLog(Path(args.data) / "validate-scratch.jsonl"), ProFakeClient({}), lambda s, c: False,
                   config=pro_arm_config(), t2_max_lines=args.t2_max_lines)
    rows, failures = [], 0
    try:
        for tid in _selected(args, part):
            t = by_id[tid]
            row: dict[str, Any] = {"task": tid}
            try:
                pull = cache.ensure(t.image)
                row.update(image_gb=round(pull.bytes / GB, 2), pull_s=round(pull.seconds, 1))
                ex = _exercise(t, part, args, h, cache, with_verifier=True)
                if isinstance(ex, str):
                    raise RuntimeError(ex)
                t0 = time.monotonic()
                row.update(validate_exercise(ex, h, args, cache, lanes_k=args.lanes_k))
                row["seconds"] = round(time.monotonic() - t0, 1)
            except Exception as e:
                row["ok"] = False
                row["error"] = f"{type(e).__name__}: {e}"
            finally:
                cache.remove(t.image)
            failures += 0 if row.get("ok") else 1
            rows.append(row)
            print(json.dumps(row), flush=True)
        df = system_df()
    finally:
        stop_dockerd(started)
    out = {"tasks": rows, "failures": failures, "docker_system_df": df}
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "prepare":
        return action_prepare(args)
    if args.action == "validate":
        return action_validate(args)
    if args.action == "select-lanes":
        return action_select_lanes(args)
    if args.action == "validate-lanes":
        return action_validate_lanes(args)
    if args.action == "preflight":
        args.probe = not (args.cascade or args.lanes)
        return action_run(args, preflight_only=True)
    return action_run(args)


if __name__ == "__main__":
    sys.exit(main())
