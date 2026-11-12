"""Export an event log to the JSON file the replay UI renders.

    python scripts/export_ui_data.py LOG.jsonl -o ui/data.json [--html standalone.html]

Python stays the source of truth. The page does no pricing, no task-state
logic and no statistics of its own beyond folding the per-event deltas written
here and recomputing a Wilson interval and Pareto frontier at the scrubber
position. At every checkpoint (and at the final event) the export also stores
the totals Python computed, so the page can check that its fold of the deltas
matches Python's projection.

The log is verified (hash chain plus full replay) before anything is exported.

Output layout (``schema: contraprova-ui/1``):

* ``events``: one compact record per event, with ``fx`` deltas for the
  attempt it touches (cost under every price table, solved, blocked, ...).
* ``attempts``: one point per (arm, task_id), with a deterministic 2D layout.
* ``price_tables``: every table in ``contraprova.pricing.PRICE_TABLES`` plus a
  "what if Haiku x0.5" variant built with ``pricing.scale_table``.
* ``checkpoints`` and ``final``: Python-computed totals for replay checks.
  ``final.measurement_overhead`` is fork spend no arm shipped, by parent arm
  and fork role, under every table.

Arm cost follows docs/statistics.md: an arm's cost includes the one fork
branch it shipped (its outcome's ``branch_task_id``). The export adds that
branch's cost to the arm, as a ``cost`` delta on the arm's attempt, at the
arm's outcome event (when the branch becomes the shipped one); the fork's own
label keeps showing every fork's cost.

``--html`` additionally writes a copy of ``ui/index.html`` with the data
inlined, which works when opened straight from disk (``file://``). The copy
also inlines the vendored lab kit (``ui/vendor/lab-kit/v1``: its stylesheet,
with the fonts as data URIs, and its script), so it needs no files beside it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contraprova.events import Event  # noqa: E402
from contraprova.log import EventLog  # noqa: E402
from contraprova.pricing import DEFAULT_TABLE, PRICE_TABLES, ModelPrice, prompt_tokens, request_cost, scale_table  # noqa: E402
from contraprova.projections import apply_task_event, pareto_frontier, shipped_forks  # noqa: E402
from contraprova.stats import wilson_interval  # noqa: E402

SCHEMA = "contraprova-ui/1"
HAIKU = "claude-haiku-5-5"
# Lifecycle events (route/claim/submit/gate) are attributed to the arm named by their
# task's task.created, else to the governed arm. Payloads follow docs/events.md: fork
# re-runs are fork events and fork outcomes keyed by fork_id. For display only, each
# fork gets the label "<parent arm>:<role>" ("D:control", "D:treatment"); a second
# control at the same fork point is "D:control-2", and so on.
GOVERNED_ARM = "D"
CHEAP_MODELS = frozenset({HAIKU})
# Strength order used only to label an attempt's "model tier" (strongest worker model).
TIER_ORDER = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"]
WORKER_ROLES = frozenset({"worker", "control", "treatment"})
CHECKPOINT_EVERY = 50
ROUND = 12  # decimal places for USD in the export


# ------------------------------------------------------------------ prices


def ui_price_tables() -> dict[str, dict[str, ModelPrice]]:
    """Every published table, plus a what-if with Haiku at half price."""
    tables: dict[str, dict[str, ModelPrice]] = {name: dict(t) for name, t in PRICE_TABLES.items()}
    base = PRICE_TABLES[DEFAULT_TABLE]
    if HAIKU in base:
        what_if = dict(base)
        what_if[HAIKU] = scale_table({HAIKU: base[HAIKU]}, 0.5)[HAIKU]
        tables[f"{DEFAULT_TABLE} + what-if Haiku x0.5"] = what_if
    return tables


def describe_table(t: Mapping[str, ModelPrice]) -> dict[str, Any]:
    return {m: {"input": p.input, "output": p.output, "cache_read_mult": p.cache_read_mult} for m, p in sorted(t.items())}


# ------------------------------------------------------------------ helpers


def is_main_arm(arm: str) -> bool:
    return ":" not in arm


def tier_rank(model: str) -> int:
    return TIER_ORDER.index(model) if model in TIER_ORDER else len(TIER_ORDER)


def summarise(ev: Event) -> str:
    p = ev.payload
    t = ev.type
    if t == "task.created":
        return f"created {p.get('task_id')}" + (" [synthetic]" if p.get("synthetic") else "")
    if t == "task.claimed":
        return f"{p.get('worker', ev.actor)} claims {p['task_id']}"
    if t == "task.submitted":
        return f"submits {p['task_id']}"
    if t == "gate.decided":
        return f"{p.get('gate', 'gate')}: {p['verdict']}" + (f" ({p['reason']})" if p.get("reason") else "")
    if t == "route.decided":
        props = p.get("propensities") or {}
        pr = props.get(p.get("action"))
        return f"route -> {p.get('action')}" + (f" (p={pr:.2f})" if isinstance(pr, (int, float)) else "")
    if t == "fork.created":
        return f"fork {p.get('role')} from #{p.get('from_seq')} on {p.get('model')}"
    if t == "usage.recorded":
        return (f"{p.get('model')} {p.get('role', '')}: in {prompt_tokens(p)} / out {p.get('output_tokens', 0)} tok"
                + (" (repair turn)" if p.get("repair") is True else ""))
    if t == "outcome.scored":
        verdict = "SOLVED" if p["solved"] else "not solved"
        if p.get("fork_id") is not None:
            return f"fork {p['fork_id']} {verdict}"
        if p.get("gate_seq") is not None:
            return f"replay of gate #{p['gate_seq']}: {'would pass' if p['solved'] else 'would fail'}"
        return f"arm {p['arm']} {verdict}"
    return t


def r(x: float) -> float:
    return round(x, ROUND)


# ------------------------------------------------------------------ layout


def _power_iteration(cov: list[list[float]], iters: int = 500) -> tuple[float, list[float]]:
    n = len(cov)
    v = [1.0 / math.sqrt(n) + 1e-3 * i for i in range(n)]
    lam = 0.0
    for _ in range(iters):
        w = [sum(cov[i][j] * v[j] for j in range(n)) for i in range(n)]
        norm = math.sqrt(sum(x * x for x in w))
        if norm < 1e-15:
            return 0.0, v
        v = [x / norm for x in w]
        lam = norm
    # Deterministic sign: the largest loading is positive.
    k = max(range(n), key=lambda i: (abs(v[i]), -i))
    if v[k] < 0:
        v = [-x for x in v]
    return lam, v


def pca_layout(rows: list[list[float]], names: list[str], ids: list[str]) -> dict[str, Any]:
    """Two-component PCA (standardised features, power iteration) plus a tiny seeded jitter."""
    n, d = len(rows), len(names)
    means = [sum(r_[j] for r_ in rows) / n for j in range(d)]
    sds = [math.sqrt(sum((r_[j] - means[j]) ** 2 for r_ in rows) / n) or 1.0 for j in range(d)]
    z = [[(r_[j] - means[j]) / sds[j] for j in range(d)] for r_ in rows]
    cov = [[sum(z[k][i] * z[k][j] for k in range(n)) / n for j in range(d)] for i in range(d)]
    total_var = sum(cov[i][i] for i in range(d)) or 1.0
    lam1, v1 = _power_iteration(cov)
    cov2 = [[cov[i][j] - lam1 * v1[i] * v1[j] for j in range(d)] for i in range(d)]
    lam2, v2 = _power_iteration(cov2)
    xs = [sum(zr[j] * v1[j] for j in range(d)) for zr in z]
    ys = [sum(zr[j] * v2[j] for j in range(d)) for zr in z]

    def norm(vals: list[float]) -> list[float]:
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        return [(v - lo) / span for v in vals]

    xs, ys = norm(xs), norm(ys)
    out = []
    for i, aid in enumerate(ids):
        h = hashlib.sha256(aid.encode()).digest()
        jx = (h[0] / 255 - 0.5) * 0.04
        jy = (h[1] / 255 - 0.5) * 0.04
        out.append((round(min(1, max(0, xs[i] + jx)), 5), round(min(1, max(0, ys[i] + jy)), 5)))

    def top(v: list[float]) -> list[list[Any]]:
        order = sorted(range(d), key=lambda j: (-abs(v[j]), j))[:4]
        return [[names[j], round(v[j], 3)] for j in order]

    return {
        "coords": out,
        "meta": {
            "method": "PCA (2 components, power iteration) of standardised per-attempt features from the full log, then a seeded jitter of +/-2% from sha256(attempt id)",
            "features": names,
            "explained_variance": [round(lam1 / total_var, 3), round(lam2 / total_var, 3)],
            "loadings_top": [top(v1), top(v2)],
            "note": "A layout, not a semantic embedding. Positions use the complete log so points do not move during replay; colour and size show the state at the scrubber position.",
        },
    }


# ------------------------------------------------------------------ export


def build_export(events: list[Event], log_name: str = "log.jsonl") -> dict[str, Any]:
    tables = ui_price_tables()
    table_names = list(tables)
    tvals = list(tables.values())

    synthetic = any(ev.type == "task.created" and ev.payload.get("synthetic") is True for ev in events)

    # Pass 1: attempts, their features, and what each event does to them.
    attempt_index: dict[tuple[str, str], int] = {}
    attempts: list[dict[str, Any]] = []
    feats: list[dict[str, float]] = []
    task_lang: dict[str, str] = {}
    task_repo: dict[str, str] = {}  # pairing key -> repository (SWE-bench Pro task.created.repo)

    def attempt(arm: str, tid: str, seq: int) -> int:
        key = (arm, tid)
        if key not in attempt_index:
            attempt_index[key] = len(attempts)
            attempts.append(
                {"id": f"{arm}|{tid}", "arm": arm, "task_id": tid, "language": task_lang.get(tid, tid.split("/")[0] if "/" in tid else "?"), "first_seq": seq}
            )
            if tid in task_repo:
                attempts[-1]["repo"] = task_repo[tid]
            feats.append({"in": 0.0, "out": 0.0, "requests": 0.0, "gates": 0.0, "cache": 0.0})
        return attempt_index[key]

    # Kernel task id -> pairing key (exercise id), and the display label of each fork.
    pair_key: dict[str, str] = {}
    task_arm: dict[str, str] = {}
    fork_label: dict[str, str] = {}  # fork_id -> label
    fork_parent: dict[str, str] = {}  # fork_id -> parent kernel task id
    child_fork: dict[str, str] = {}  # fork child kernel task id -> fork_id
    fork_point_roles: dict[tuple[str, Any, str], int] = {}
    fork_model: dict[str, str] = {}  # display label -> the fork's model
    derived_arms: dict[str, str] = {}  # derived arm -> the arm it was derived from (outcome derived_from)
    for ev in events:
        p = ev.payload
        if ev.type == "task.created":
            pair_key[p["task_id"]] = str(p.get("exercise_id") or p["task_id"])
            if p.get("arm") is not None:
                task_arm[p["task_id"]] = str(p["arm"])
        elif ev.type == "fork.created":
            fid = str(p["fork_id"]) if p.get("fork_id") is not None else f"fork@{ev.seq}"
            arm = str(p.get("arm") or task_arm.get(p["task_id"]) or GOVERNED_ARM)
            role = str(p.get("role"))
            point = (p["task_id"], p.get("from_seq"), role)
            k = fork_point_roles[point] = fork_point_roles.get(point, 0) + 1
            fork_label[fid] = f"{arm}:{role}" + (f"-{k}" if k > 1 else "")
            fork_parent[fid] = p["task_id"]
            if p.get("model"):
                fork_model.setdefault(fork_label[fid], str(p["model"]))
            if p.get("fork_task_id"):
                child_fork[str(p["fork_task_id"])] = fid
        elif ev.type == "outcome.scored" and p.get("derived_from") is not None and p.get("arm") is not None:
            derived_arms.setdefault(str(p["arm"]), str(p["derived_from"]))

    def key(kernel_tid: str) -> str:
        return pair_key.get(kernel_tid, kernel_tid)

    shipped = shipped_forks(events)  # fork_id -> (arm, pairing key) of the branch an arm shipped
    fork_cost: dict[str, list[float]] = {}  # fork_id -> cost so far under every table
    released: dict[str, int] = {}  # shipped fork_id -> its arm's attempt, once the arm's outcome named it
    # A derived arm (outcome derived_from, e.g. B from D's first attempt) costs the parent arm's own
    # worker requests on the outcome's model for that kernel task (first attempt plus repair). It is a
    # view of spend the parent arm already counts, so it is never added to the parent or to overhead.
    derived_pool: dict[tuple[str, str, str], list[float]] = {}  # (arm, kernel task, model) -> cost so far
    derived_released: dict[tuple[str, str, str], int] = {}  # pool -> the derived arm's attempt

    def add_cost(i: int, arm: str, tid: str, costs: list[float]) -> None:
        ac = arm_cost.setdefault(arm, [0.0] * len(tvals))
        tc = att_cost.setdefault(i, [0.0] * len(tvals))
        for j, c in enumerate(costs):
            ac[j] += c
            tc[j] += c
        arm_tasks.setdefault(arm, set()).add(tid)

    tasks: dict = {}
    out_events: list[dict[str, Any]] = []
    # Running totals for checkpoints, computed straight from payloads with pricing.request_cost.
    arm_cost: dict[str, list[float]] = {}
    arm_tasks: dict[str, set[str]] = {}
    arm_outcome: dict[str, dict[str, bool]] = {}
    checkpoints: list[dict[str, Any]] = []
    att_cost: dict[int, list[float]] = {}

    for ev in events:
        p = ev.payload
        tid = p.get("task_id")
        kernel_tid = tid
        if ev.type == "outcome.scored":
            pass  # task_id is already the pairing key
        elif ev.type == "fork.created" or (ev.type == "usage.recorded" and p.get("fork_id") is not None):
            parent = fork_parent.get(str(p.get("fork_id")), p.get("task_id")) if ev.type == "usage.recorded" else tid
            tid = key(parent) if parent is not None else tid
        elif tid is not None:
            tid = key(tid)
        rec: dict[str, Any] = {"s": ev.seq, "t": ev.type, "a": ev.actor, "ts": ev.ts, "h": ev.hash[:12], "x": summarise(ev)}
        if tid is not None:
            rec["k"] = tid
        fx: list[list[Any]] = []

        if ev.type == "task.created":
            task_lang.setdefault(tid, str(p.get("language") or (tid.split("/")[0] if "/" in tid else "?")))
            if p.get("repo"):
                task_repo.setdefault(tid, str(p["repo"]))
        elif ev.type in ("task.claimed", "task.submitted", "gate.decided", "route.decided"):
            # Lifecycle events belong to their task's arm; a fork child's belong to its fork.
            fid = child_fork.get(kernel_tid)
            label = fork_label[fid] if fid is not None else task_arm.get(kernel_tid, GOVERNED_ARM)
            i = attempt(label, tid, ev.seq)
            rec["p"] = i
            if ev.type == "route.decided":
                action = p.get("action")
                fx.append([i, "route", action])
                # features.attempt is router.featurize's encoding: (attempt - 1) / 2, so > 0 means a retry.
                retry = p.get("decision") == "after_gate_failure" or float((p.get("features") or {}).get("attempt", 0) or 0) > 0
                if action and action not in CHEAP_MODELS and retry:
                    fx.append([i, "escalated", True])
            elif ev.type == "gate.decided":
                feats[i]["gates"] += 1
                if p.get("verdict") == "block":
                    fx.append([i, "blocked", True])
        elif ev.type == "fork.created":
            fid = str(p["fork_id"]) if p.get("fork_id") is not None else f"fork@{ev.seq}"
            i = attempt(fork_label[fid], tid, ev.seq)
            rec["p"] = i
            fx.append([i, "fork", {"role": p.get("role"), "from_seq": p.get("from_seq"), "model": p.get("model")}])
            if p.get("role") == "treatment" and p.get("model") not in CHEAP_MODELS:
                fx.append([i, "escalated", True])
        elif ev.type == "usage.recorded":
            fid = str(p["fork_id"]) if p.get("fork_id") is not None else None
            if fid is not None and fid in fork_label:
                arm = fork_label[fid]  # every fork's cost shows under its label
            else:
                arm = str(p.get("arm", "unknown"))
            if tid is None:
                tid = "(untagged)"
                rec["k"] = tid
            i = attempt(arm, tid, ev.seq)
            rec["p"] = i
            costs = [request_cost(p, t) for t in tvals]
            rec["c"] = [r(c) for c in costs]
            fx.append([i, "cost", rec["c"]])
            if p.get("role") in WORKER_ROLES:
                fx.append([i, "model", p.get("model")])
            if p.get("repair") is True:
                fx.append([i, "repair", True])
            feats[i]["in"] += prompt_tokens(p)
            feats[i]["out"] += int(p.get("output_tokens", 0))
            feats[i]["cache"] += int(p.get("cache_read_tokens", 0))
            feats[i]["requests"] += 1
            add_cost(i, arm, tid, costs)
            if fid is not None and fid in fork_label:
                fc = fork_cost.setdefault(fid, [0.0] * len(tvals))
                for j, c in enumerate(costs):
                    fc[j] += c
                if fid in released:  # usage after the arm shipped this branch is the arm's too
                    k = released[fid]
                    fx.append([k, "cost", rec["c"]])
                    add_cost(k, attempts[k]["arm"], attempts[k]["task_id"], costs)
            elif fid is None and p.get("role") == "worker" and kernel_tid is not None:
                pool = (arm, str(kernel_tid), str(p.get("model")))
                acc = derived_pool.setdefault(pool, [0.0] * len(tvals))
                for j, c in enumerate(costs):
                    acc[j] += c
                if pool in derived_released:  # a request after the derived outcome is the derived arm's too
                    k = derived_released[pool]
                    fx.append([k, "cost", rec["c"]])
                    add_cost(k, attempts[k]["arm"], attempts[k]["task_id"], costs)
        elif ev.type == "outcome.scored" and p.get("gate_seq") is None:
            if p.get("fork_id") is not None:
                arm = fork_label.get(str(p["fork_id"]))
            else:
                arm = str(p["arm"])
            if arm is not None:  # an outcome naming an unknown fork is shown but not counted
                i = attempt(arm, tid, ev.seq)
                rec["p"] = i
                fx.append([i, "solved", bool(p["solved"])])
                sfid = child_fork.get(str(p.get("branch_task_id"))) if p.get("fork_id") is None else None
                if sfid is not None and shipped.get(sfid, (None,))[0] == arm and sfid not in released:
                    # The arm shipped this fork branch: its cost so far becomes the arm's cost.
                    released[sfid] = i
                    so_far = fork_cost.get(sfid, [0.0] * len(tvals))
                    fx.append([i, "cost", [r(c) for c in so_far]])
                    add_cost(i, arm, tid, so_far)
                if p.get("fork_id") is None and p.get("derived_from") is not None:
                    pool = (str(p["derived_from"]), str(p.get("log_task_id")), str(p.get("model")))
                    if pool not in derived_released:
                        derived_released[pool] = i
                        so_far = derived_pool.get(pool, [0.0] * len(tvals))
                        fx.append([i, "cost", [r(c) for c in so_far]])
                        add_cost(i, arm, tid, so_far)
                arm_outcome.setdefault(arm, {})[tid] = bool(p["solved"])
                arm_tasks.setdefault(arm, set()).add(tid)

        before = tasks.get(kernel_tid).status if kernel_tid in tasks else None
        tasks = apply_task_event(tasks, ev)
        after = tasks.get(kernel_tid).status if kernel_tid in tasks else None
        if after is not None and after != before:
            rec["st"] = after
            if "p" in rec:
                fx.append([rec["p"], "status", after])
        if fx:
            rec["fx"] = fx
        out_events.append(rec)

        last = ev.seq == len(events) - 1
        if (ev.seq + 1) % CHECKPOINT_EVERY == 0 or last:
            checkpoints.append({"seq": ev.seq, "arms": _arm_summary(arm_cost, arm_tasks, arm_outcome, len(tvals))})

    # Per-attempt facts at the end of the log (used for the layout and the tests).
    final_attempt_cost = {i: c for i, c in att_cost.items()}
    rows, ids = [], []
    names = ["log input tokens", "log output tokens", "output share of tokens", "requests", "gate decisions", "cache-read share of prompt"]
    for i, a in enumerate(attempts):
        f = feats[i]
        tot = f["in"] + f["out"]
        rows.append([
            math.log1p(f["in"]),
            math.log1p(f["out"]),
            f["out"] / tot if tot else 0.0,
            f["requests"],
            f["gates"],
            f["cache"] / f["in"] if f["in"] else 0.0,
        ])
        ids.append(a["id"])
    layout = pca_layout(rows, names, ids) if attempts else {"coords": [], "meta": {}}
    for i, a in enumerate(attempts):
        a["x"], a["y"] = layout["coords"][i]
        a["final_cost"] = [r(c) for c in final_attempt_cost.get(i, [0.0] * len(tvals))]

    final_arms = checkpoints[-1]["arms"] if checkpoints else {}
    pareto = {}
    for j, name in enumerate(table_names):
        pts = [(arm, s["cost_per_task"][j], s["solve_rate"]) for arm, s in final_arms.items() if is_main_arm(arm) and s["n_scored"] > 0]
        pareto[name] = [p[0] for p in pareto_frontier(pts)]

    main_arms = sorted(a for a in final_arms if is_main_arm(a))
    fork_arms = sorted(a for a in final_arms if not is_main_arm(a))
    fork_meta = {}
    for label in fork_arms:
        parent, _, rest = label.partition(":")
        role, _, k = rest.partition("-")
        fork_meta[label] = {"parent": parent, "role": role, "replicate": int(k) if k.isdigit() else 1}
        if label in fork_model:
            fork_meta[label]["model"] = fork_model[label]

    return {
        "schema": SCHEMA,
        "synthetic": synthetic,
        "source": {
            "log": Path(log_name).name,
            "n_events": len(events),
            "head_hash": events[-1].hash if events else None,
            "verified": True,
        },
        "default_table": DEFAULT_TABLE,
        "table_names": table_names,
        "price_tables": {name: describe_table(t) for name, t in tables.items()},
        "arms": [{"id": a, "kind": "arm", **({"derived_from": derived_arms[a]} if a in derived_arms else {})} for a in main_arms] + [{"id": a, "kind": "fork", **fork_meta[a]} for a in fork_arms],
        "tier_order": TIER_ORDER,
        "attempts": attempts,
        "layout": layout["meta"],
        "events": out_events,
        "checkpoints": checkpoints,
        "final": {"arms": final_arms, "pareto": pareto, "escalation": _escalation(arm_outcome, arm_cost, arm_tasks, len(tvals)),
                  "measurement_overhead": _overhead(fork_cost, fork_label, released, len(tvals))},
    }


def _arm_summary(arm_cost, arm_tasks, arm_outcome, n_tables) -> dict[str, Any]:
    out = {}
    for arm in sorted(set(arm_cost) | set(arm_outcome)):
        cost = arm_cost.get(arm, [0.0] * n_tables)
        n_tasks = len(arm_tasks.get(arm, ()))
        outs = arm_outcome.get(arm, {})
        n, k = len(outs), sum(outs.values())
        lo, hi = wilson_interval(k, n) if n else (0.0, 1.0)
        out[arm] = {
            "cost": [r(c) for c in cost],
            "n_tasks": n_tasks,
            "n_scored": n,
            "n_solved": k,
            "solve_rate": r(k / n) if n else 0.0,
            "wilson95": [r(lo), r(hi)],
            "cost_per_task": [r(c / n_tasks) if n_tasks else 0.0 for c in cost],
        }
    return out


def _overhead(fork_cost, fork_label, released, n_tables) -> dict[str, dict[str, list[float]]]:
    """Fork spend no arm shipped, as {parent arm: {fork role: cost under every table}}."""
    out: dict[str, dict[str, list[float]]] = {}
    for fid, costs in sorted(fork_cost.items()):
        if fid in released:
            continue
        parent, _, rest = fork_label[fid].partition(":")
        role = rest.partition("-")[0]
        acc = out.setdefault(parent, {}).setdefault(role, [0.0] * n_tables)
        for j, c in enumerate(costs):
            acc[j] += c
    return {a: {role: [r(c) for c in v] for role, v in sorted(roles.items())} for a, roles in sorted(out.items())}


def _escalation(arm_outcome, arm_cost, arm_tasks, n_tables) -> dict[str, Any]:
    """Paired fork summary: control vs treatment on the same forked tasks."""
    out = {}
    for base in sorted({a.split(":")[0] for a in arm_outcome if ":" in a}):
        ctl = arm_outcome.get(f"{base}:control", {})
        trt = arm_outcome.get(f"{base}:treatment", {})
        paired = sorted(set(ctl) & set(trt))
        out[base] = {
            "n_pairs": len(paired),
            "control_solved": sum(ctl[t] for t in paired),
            "treatment_solved": sum(trt[t] for t in paired),
            "treatment_only": sum(trt[t] and not ctl[t] for t in paired),
            "control_only": sum(ctl[t] and not trt[t] for t in paired),
        }
    return out


# ------------------------------------------------------------------ study (a pre-registered analysis)


def _pts(x: float, sign: bool = True, keep_zero: bool = True) -> str:
    """Percentage points, one decimal, with a real minus sign (and + when asked)."""
    txt = f"{abs(x):.1f}"
    if not keep_zero and txt.endswith(".0"):
        txt = txt[:-2]
    if round(x, 1) < 0:
        return "\u2212" + txt
    return ("+" if sign and round(x, 1) > 0 else "") + txt


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _usd(x: float) -> str:
    return f"${x:.3f}"


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-9)


def study_block(analysis: Mapping[str, Any], data: Mapping[str, Any], events: list[Event], name: str, digest: str) -> dict[str, Any]:
    """The study's headline numbers for the page, all taken from the analysis file.

    The analysis must describe this very log: its event count, solve counts and cost totals are
    checked against the export, and a mismatch is an error rather than a page that disagrees with
    itself. The honesty rows and stat tiles are formatted here, so the page renders them as given.
    """
    final, j = data["final"]["arms"], data["table_names"].index(data["default_table"])
    problems = []
    if analysis.get("log_events") != data["source"]["n_events"]:
        problems.append(f"log_events {analysis.get('log_events')} != {data['source']['n_events']}")
    solve, cost = analysis["solve"], analysis["cost"]
    for arm, key in (("A", "A"), ("D", "D"), ("B", "B_derived")):
        if key not in solve:
            continue
        s = final.get(arm, {})
        if (s.get("n_solved"), s.get("n_scored")) != (solve[key]["solved"], solve[key]["n"]):
            problems.append(f"{arm} solved {s.get('n_solved')}/{s.get('n_scored')} != {solve[key]['solved']}/{solve[key]['n']}")
        if not _close(s.get("cost", [math.nan] * (j + 1))[j], cost["total"][key]):
            problems.append(f"{arm} cost {s.get('cost')} != {cost['total'][key]}")
    overhead = sum(sum(v[j] for v in roles.values()) for roles in data["final"]["measurement_overhead"].values())
    if "D_measurement" in cost["total"] and not _close(overhead, cost["total"]["D_measurement"]):
        problems.append(f"measurement overhead {overhead} != {cost['total']['D_measurement']}")
    esc = analysis.get("escalation_value", {})
    pairs = data["final"]["escalation"].get("D", {})
    if esc and (esc["n_forks"], esc["treatment_opus"]["solved"], esc["control_haiku_noise_floor"]["solved"]) != (
            pairs.get("n_pairs"), pairs.get("treatment_solved"), pairs.get("control_solved")):
        problems.append(f"forks {esc['n_forks']} != {pairs}")
    if problems:
        raise ValueError("the analysis file does not describe this log: " + "; ".join(problems))

    created = [e.payload for e in events if e.type == "task.created"]
    n = analysis["n_pairs"]
    pr, co = analysis["primary"], analysis["co_primary"]
    lo, hi = pr["ci95_points"]
    rlo, rhi = co["ci95"]
    honesty = [
        {"claim": f"the cascade (D) is non-inferior to a single opus (A) within {_pts(pr['margin_points'], keep_zero=False)} pts: "
                  f"solve D \u2212 A = {_pts(pr['diff_D_minus_A_points'], keep_zero=False)} pts [{_pts(lo)}, {_pts(hi)}]",
         "kind": "measured", "word": f"measured (pre-registered, n={n})"},
        {"claim": f"cost per solved task, D/A: {co['cost_per_solved_ratio_D_over_A']:.2f} [{rlo:.2f}, {rhi:.2f}]",
         "kind": "measured", "word": "measured"},
    ]
    if esc:
        elo, ehi = esc["ci95_points"]
        honesty.append({"claim": f"escalation value at fork points (opus treatment \u2212 haiku retry): "
                                 f"{_pts(esc['EV_points'], keep_zero=False)} pts [{_pts(elo)}, {_pts(ehi)}], {esc['n_forks']} forks",
                        "kind": "measured", "word": "measured, imprecise"})

    def solve_tile(arm: str, key: str, label: str) -> dict[str, Any]:
        s = solve[key]
        w0, w1 = s["wilson95"]
        return {"arm": arm, "label": label, "value": _pct(s["rate"]), "small": f"{s['solved']}/{s['n']}",
                "note": f"95% wilson {_pct(w0)}\u2013{_pct(w1)}"}

    tiles = [solve_tile("A", "A", "arm A · single opus solves"), solve_tile("D", "D", "arm D · the cascade solves")]
    if "B_derived" in solve:
        tiles.append(solve_tile("B", "B_derived", "arm B · the cascade's first haiku try"))
    ps = cost["per_solved"]
    tiles.append({"label": "cost per solved task, the cascade", "value": _usd(ps["D"]),
                  "note": f"single opus (A) {_usd(ps['A'])}; D/A {co['cost_per_solved_ratio_D_over_A']:.2f} [{rlo:.2f}, {rhi:.2f}]"})
    if esc:
        tiles.append({"label": "forks after a gate block", "value": str(esc["n_forks"]), "small": f"/ {n}",
                      "note": f"{_pct(esc['fork_rate'])} of D's tasks; the other {n - esc['n_forks']} passed the gates on haiku"})
    share = analysis.get("orchestrator_share_D")
    if share:
        tiles.append({"label": "orchestrator share of cost", "value": _pct(share["dollars"]),
                      "note": f"of the cascade's dollars (the opus brief); {_pct(share['tokens'])} of its tokens"})
    return {
        "benchmark": next((str(c["benchmark"]) for c in created if c.get("benchmark")), None),
        "split": next((str(c["split"]) for c in created if c.get("split")), None),
        "repos": sorted({str(c["repo"]) for c in created if c.get("repo")}),
        "n_pairs": n,
        "analysis_source": {"file": name, "sha256": digest},
        "analysis": {k: v for k, v in analysis.items() if k != "per_task"},
        "honesty": honesty,
        "tiles": tiles,
    }


def export(log_path: str | Path, analysis_path: str | Path | None = None) -> dict[str, Any]:
    log = EventLog(log_path)
    log.verify()
    events = log.events()
    data = build_export(events, str(log_path))
    if analysis_path is not None:
        raw = Path(analysis_path).read_bytes()
        data["study"] = study_block(json.loads(raw), data, events, Path(analysis_path).name, hashlib.sha256(raw).hexdigest())
    return data


def dumps(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def inline_html(template: str, data_json: str) -> str:
    marker = '<script id="embedded-data" type="application/json"></script>'
    if marker not in template:
        raise ValueError("ui/index.html has no embedded-data placeholder")
    safe = data_json.replace("</", "<\\/")
    return template.replace(marker, f'<script id="embedded-data" type="application/json">{safe}</script>', 1)


KIT_DIR = Path(__file__).resolve().parents[1] / "ui" / "vendor" / "lab-kit" / "v1"
KIT_CSS_LINK = '<link rel="stylesheet" href="vendor/lab-kit/v1/lab.css">'
KIT_JS_TAG = '<script src="vendor/lab-kit/v1/lab.js" defer></script>'
FAVICON_LINK = '<link rel="icon" href="favicon.svg" type="image/svg+xml">'


def inline_kit(template: str, kit_dir: str | Path = KIT_DIR) -> str:
    """Replace the page's links to the vendored lab kit with the kit itself (fonts as data URIs), and the
    favicon with a data URI."""
    kit = Path(kit_dir)
    if KIT_CSS_LINK not in template or KIT_JS_TAG not in template:
        raise ValueError("ui/index.html does not link the vendored lab kit")
    css = (kit / "lab.css").read_text(encoding="utf-8")

    def font(match: re.Match[str]) -> str:
        data = base64.b64encode((kit / match.group(1)).read_bytes()).decode("ascii")
        return f'url("data:font/woff2;base64,{data}")'

    css = re.sub(r'url\("(fonts/[^"]+\.woff2)"\)', font, css)
    js = (kit / "lab.js").read_text(encoding="utf-8")
    if "</style" in css.lower() or "</script" in js.lower():
        raise ValueError("the lab kit cannot be inlined as it is")
    out = template.replace(KIT_CSS_LINK, f"<style>{css}</style>", 1)
    out = out.replace(KIT_JS_TAG, f"<script>{js}</script>", 1)
    icon = kit.parents[2] / "favicon.svg"   # ui/favicon.svg, contraprova's icon
    if FAVICON_LINK in out and icon.is_file():
        data = base64.b64encode(icon.read_bytes()).decode("ascii")
        out = out.replace(FAVICON_LINK, f'<link rel="icon" href="data:image/svg+xml;base64,{data}" type="image/svg+xml">', 1)
    return out


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export a contraprova log for the replay UI.")
    ap.add_argument("log")
    ap.add_argument("-o", "--out", default="ui/data.json")
    ap.add_argument("--html", help="also write a self-contained copy of ui/index.html with the data inlined")
    ap.add_argument("--analysis", help="a pre-registered analysis of this log (e.g. results/swepro-main/analysis.json): "
                                       "adds its headline numbers, checked against the log, for the page's findings and honesty table")
    args = ap.parse_args(None if argv is None else list(argv))
    data = export(args.log, args.analysis)
    text = dumps(data)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    msg = f"wrote {args.out}: {data['source']['n_events']} events, {len(data['attempts'])} attempts, {len(text) + 1} bytes"
    if args.html:
        template = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
        Path(args.html).write_text(inline_html(inline_kit(template), text), encoding="utf-8")
        msg += f", standalone page {args.html}"
    print(msg + (" (SYNTHETIC)" if data["synthetic"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
