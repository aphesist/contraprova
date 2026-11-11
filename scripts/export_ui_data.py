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

``--html`` additionally writes a copy of ``ui/index.html`` with the data
inlined, which works when opened straight from disk (``file://``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contraprova.events import Event  # noqa: E402
from contraprova.log import EventLog  # noqa: E402
from contraprova.pricing import DEFAULT_TABLE, PRICE_TABLES, ModelPrice, prompt_tokens, request_cost, scale_table  # noqa: E402
from contraprova.projections import apply_task_event, pareto_frontier  # noqa: E402
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
        return f"{p.get('model')} {p.get('role', '')}: in {prompt_tokens(p)} / out {p.get('output_tokens', 0)} tok"
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

    def attempt(arm: str, tid: str, seq: int) -> int:
        key = (arm, tid)
        if key not in attempt_index:
            attempt_index[key] = len(attempts)
            attempts.append(
                {"id": f"{arm}|{tid}", "arm": arm, "task_id": tid, "language": task_lang.get(tid, tid.split("/")[0] if "/" in tid else "?"), "first_seq": seq}
            )
            feats.append({"in": 0.0, "out": 0.0, "requests": 0.0, "gates": 0.0, "cache": 0.0})
        return attempt_index[key]

    # Kernel task id -> pairing key (exercise id), and the display label of each fork.
    pair_key: dict[str, str] = {}
    task_arm: dict[str, str] = {}
    fork_label: dict[str, str] = {}  # fork_id -> label
    fork_parent: dict[str, str] = {}  # fork_id -> parent kernel task id
    child_fork: dict[str, str] = {}  # fork child kernel task id -> fork_id
    fork_point_roles: dict[tuple[str, Any, str], int] = {}
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
            if p.get("fork_task_id"):
                child_fork[str(p["fork_task_id"])] = fid

    def key(kernel_tid: str) -> str:
        return pair_key.get(kernel_tid, kernel_tid)

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
            if p.get("fork_id") is not None and str(p["fork_id"]) in fork_label:
                arm = fork_label[str(p["fork_id"])]  # fork cost never counts toward the parent arm
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
            feats[i]["in"] += prompt_tokens(p)
            feats[i]["out"] += int(p.get("output_tokens", 0))
            feats[i]["cache"] += int(p.get("cache_read_tokens", 0))
            feats[i]["requests"] += 1
            ac = arm_cost.setdefault(arm, [0.0] * len(tvals))
            for j, c in enumerate(costs):
                ac[j] += c
            arm_tasks.setdefault(arm, set()).add(tid)
            tc = att_cost.setdefault(i, [0.0] * len(tvals))
            for j, c in enumerate(costs):
                tc[j] += c
        elif ev.type == "outcome.scored" and p.get("gate_seq") is None:
            if p.get("fork_id") is not None:
                arm = fork_label.get(str(p["fork_id"]))
            else:
                arm = str(p["arm"])
            if arm is not None:  # an outcome naming an unknown fork is shown but not counted
                i = attempt(arm, tid, ev.seq)
                rec["p"] = i
                fx.append([i, "solved", bool(p["solved"])])
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
        "arms": [{"id": a, "kind": "arm"} for a in main_arms] + [{"id": a, "kind": "fork", **fork_meta[a]} for a in fork_arms],
        "tier_order": TIER_ORDER,
        "attempts": attempts,
        "layout": layout["meta"],
        "events": out_events,
        "checkpoints": checkpoints,
        "final": {"arms": final_arms, "pareto": pareto, "escalation": _escalation(arm_outcome, arm_cost, arm_tasks, len(tvals))},
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


def export(log_path: str | Path) -> dict[str, Any]:
    log = EventLog(log_path)
    log.verify()
    return build_export(log.events(), str(log_path))


def dumps(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def inline_html(template: str, data_json: str) -> str:
    marker = '<script id="embedded-data" type="application/json"></script>'
    if marker not in template:
        raise ValueError("ui/index.html has no embedded-data placeholder")
    safe = data_json.replace("</", "<\\/")
    return template.replace(marker, f'<script id="embedded-data" type="application/json">{safe}</script>', 1)


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export a contraprova log for the replay UI.")
    ap.add_argument("log")
    ap.add_argument("-o", "--out", default="ui/data.json")
    ap.add_argument("--html", help="also write a self-contained copy of ui/index.html with the data inlined")
    args = ap.parse_args(None if argv is None else list(argv))
    data = export(args.log)
    text = dumps(data)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    msg = f"wrote {args.out}: {data['source']['n_events']} events, {len(data['attempts'])} attempts"
    if args.html:
        template = (Path(__file__).resolve().parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
        Path(args.html).write_text(inline_html(template, text), encoding="utf-8")
        msg += f", standalone page {args.html}"
    print(msg + (" (SYNTHETIC)" if data["synthetic"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
