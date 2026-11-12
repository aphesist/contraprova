"""Pre-registered analysis of the SWE-bench Pro main study (results/swepro-main/PREREG.md).

    python results/swepro-main/analyze.py results/swepro-main/log.jsonl results/swepro-main/analysis.json

Completed task pairs only: a task counts when both arm A and arm D have an outcome. Intervals are
95%: paired percentile bootstrap over tasks (10,000 resamples, seed 0); Wilson for proportions.
Costs are recomputed from logged tokens with the frozen price table. D's cost follows the
shipped-branch rule (contraprova.projections.arm_usage_records). The derived arm B costs its own
Haiku worker requests (first attempt plus repair).
"""

from __future__ import annotations

import json
import random
import sys
from collections import defaultdict

from contraprova.interfaces import HAIKU, OPUS
from contraprova.log import EventLog
from contraprova.pricing import DEFAULT_TABLE, PRICE_TABLES, request_cost
from contraprova.projections import arm_usage_records, usage_records
from contraprova.stats import discordant, mcnemar_exact, paired_bootstrap_ci, wilson_interval

ITERS, SEED, ALPHA = 10_000, 0, 0.05
T = PRICE_TABLES[DEFAULT_TABLE]


def bare(task_id: str) -> str:
    """'D:instance_x:treatment' -> 'instance_x'."""
    parts = task_id.split(":")
    return parts[1] if len(parts) > 1 else parts[0]


def ratio_ci(cost_d, solved_d, cost_a, solved_a, iters=ITERS, seed=SEED):
    """Ratio of cost per solved task (D/A) with a paired percentile bootstrap over tasks."""
    def ratio(ix):
        sd, sa = sum(solved_d[i] for i in ix), sum(solved_a[i] for i in ix)
        if not sd or not sa:
            return float("inf")
        return (sum(cost_d[i] for i in ix) / sd) / (sum(cost_a[i] for i in ix) / sa)
    n = len(cost_d)
    point = ratio(range(n))
    rng = random.Random(seed)
    vals = sorted(ratio([rng.randrange(n) for _ in range(n)]) for _ in range(iters))
    return point, vals[int(ALPHA / 2 * iters)], vals[min(iters - 1, int((1 - ALPHA / 2) * iters))]


def main(log_path: str, out_path: str | None = None) -> dict:
    ev = EventLog(log_path).events()
    out: dict = {"log_events": len(ev)}

    # Outcomes
    arm_out: dict[str, dict[str, dict]] = defaultdict(dict)
    fork_out: dict[str, dict] = {}
    for e in ev:
        p = e.payload
        if e.type != "outcome.scored":
            continue
        if "fork_id" in p:
            fork_out[str(p["fork_id"])] = p
        elif "arm" in p:
            arm_out[p["arm"]][p["task_id"]] = p
    tasks = [t for t in arm_out["A"] if t in arm_out["D"]]
    n = len(tasks)
    out["n_pairs"] = n
    out["partial_tasks_dropped"] = sorted(set(arm_out["A"]) ^ set(arm_out["D"]))
    sA = [bool(arm_out["A"][t]["solved"]) for t in tasks]
    sD = [bool(arm_out["D"][t]["solved"]) for t in tasks]
    sB = [bool(arm_out["B"][t]["solved"]) for t in tasks]

    def rate(xs):
        k = sum(xs)
        lo, hi = wilson_interval(k, len(xs))
        return {"solved": k, "n": len(xs), "rate": k / len(xs), "wilson95": [lo, hi]}

    out["solve"] = {"A": rate(sA), "D": rate(sD), "B_derived": rate(sB)}

    # Primary: non-inferiority of D vs A (margin 10 points)
    diffs = [int(d) - int(a) for d, a in zip(sD, sA)]
    m, lo, hi = paired_bootstrap_ci(diffs, ITERS, ALPHA, SEED)
    d_only, a_only = discordant(sD, sA)
    out["primary"] = {"diff_D_minus_A_points": 100 * m, "ci95_points": [100 * lo, 100 * hi],
                      "margin_points": -10, "non_inferior": 100 * lo > -10,
                      "discordant": {"D_only": d_only, "A_only": a_only},
                      "mcnemar_p": mcnemar_exact(d_only, a_only)}

    # Costs per task
    recs = arm_usage_records(ev)
    cost = defaultdict(lambda: defaultdict(float))
    for u in recs:
        cost[u["arm"]][bare(u["task_id"])] += request_cost(u, T)
    b_cost = defaultdict(float)
    for u in usage_records(ev):
        if u.get("arm") == "D" and u.get("role") == "worker" and u["model"] == HAIKU and "fork_id" not in u:
            b_cost[bare(u["task_id"])] += request_cost(u, T)
    cA = [cost["A"][t] for t in tasks]
    cD = [cost["D"][t] for t in tasks]
    cB = [b_cost[t] for t in tasks]
    cM = [cost["D:measurement"][t] for t in tasks]
    out["cost"] = {
        "total": {"A": sum(cA), "D": sum(cD), "D_measurement": sum(cM), "B_derived": sum(cB),
                  "all_recorded": sum(request_cost(u, T) for u in usage_records(ev))},
        "per_task_mean": {"A": sum(cA) / n, "D": sum(cD) / n, "B_derived": sum(cB) / n},
        "per_solved": {"A": sum(cA) / max(sum(sA), 1), "D": sum(cD) / max(sum(sD), 1),
                       "B_derived": sum(cB) / max(sum(sB), 1)},
    }
    pr, plo, phi = ratio_ci(cD, sD, cA, sA)
    out["co_primary"] = {"cost_per_solved_ratio_D_over_A": pr, "ci95": [plo, phi], "cheaper": phi < 1}

    # Secondary: B (derived) vs A
    mb, lob, hib = paired_bootstrap_ci([int(b) - int(a) for b, a in zip(sB, sA)], ITERS, ALPHA, SEED)
    b_only, a_only_b = discordant(sB, sA)
    out["secondary_B_vs_A"] = {"diff_points": 100 * mb, "ci95_points": [100 * lob, 100 * hib],
                               "mcnemar_p": mcnemar_exact(b_only, a_only_b)}

    # Secondary: escalation value at fork points
    forks = defaultdict(dict)
    for fid, p in fork_out.items():
        if bare(p["log_task_id"]) in tasks:
            forks[bare(p["log_task_id"])][p.get("branch")] = bool(p["solved"])
    pairs = [(v["treatment"], v["control-1"]) for v in forks.values() if "treatment" in v and "control-1" in v]
    if pairs:
        ev_m, ev_lo, ev_hi = paired_bootstrap_ci([int(t) - int(c) for t, c in pairs], ITERS, ALPHA, SEED)
        t_only, c_only = discordant([t for t, _ in pairs], [c for _, c in pairs])
        out["escalation_value"] = {
            "n_forks": len(pairs), "fork_rate": len(pairs) / n,
            "treatment_opus": rate([t for t, _ in pairs]), "control_haiku_noise_floor": rate([c for _, c in pairs]),
            "EV_points": 100 * ev_m, "ci95_points": [100 * ev_lo, 100 * ev_hi],
            "mcnemar_p": mcnemar_exact(t_only, c_only)}
    else:
        out["escalation_value"] = {"n_forks": 0}

    # Secondary: repairs and failure categories (the submission each arm's outcome scored)
    subs = {}
    for e in ev:
        if e.type == "task.submitted":
            subs[e.payload["task_id"]] = e.payload
    rep = defaultdict(lambda: {"requests_with_repair": 0, "worker_requests": 0})
    for tid, p in subs.items():
        key = tid.split(":")[0] + (":" + tid.split(":")[2] if tid.count(":") == 2 else "")
        if bare(tid) in tasks:
            rep[key]["worker_requests"] += 1
            rep[key]["requests_with_repair"] += int(bool(p.get("repair_used")))
    out["repairs"] = dict(rep)
    cats = {}
    for arm, solved_list in (("A", sA), ("D", sD), ("B_derived", sB)):
        c = {"solved": 0, "logic": 0, "edit_format": 0}
        for t, s in zip(tasks, solved_list):
            if s:
                c["solved"] += 1
                continue
            src = arm_out["D" if arm != "A" else "A"][t]
            ltid = src.get("branch_task_id") if arm == "D" and src.get("branch_task_id") else (
                f"D:{t}" if arm != "A" else f"A:{t}")
            c["edit_format" if (subs.get(ltid, {}).get("final_failure")) else "logic"] += 1
        cats[arm] = c
    out["failure_categories"] = cats

    # Secondary: orchestrator share in D
    d_recs = [u for u in recs if u["arm"] == "D" and bare(u["task_id"]) in tasks]
    orch = [u for u in d_recs if u.get("role") == "orchestrator"]
    tok = lambda u: (u["input_tokens"] + u.get("cache_read_tokens", 0) + u.get("cache_write_5m_tokens", 0)
                     + u.get("cache_write_1h_tokens", 0) + u["output_tokens"])
    out["orchestrator_share_D"] = {
        "dollars": sum(request_cost(u, T) for u in orch) / max(sum(request_cost(u, T) for u in d_recs), 1e-12),
        "tokens": sum(tok(u) for u in orch) / max(sum(tok(u) for u in d_recs), 1)}
    out["t2_reviews"] = sum(1 for u in usage_records(ev) if u.get("role") == "reviewer")
    out["per_task"] = [{"task": t, "A": a, "D": d, "B": b, "cost_A": ca, "cost_D": cd,
                        "D_branch": arm_out["D"][t].get("branch"),
                        "fork": forks.get(t, {})} for t, a, d, b, ca, cd in zip(tasks, sA, sD, sB, cA, cD)]
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    return out


if __name__ == "__main__":
    res = main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps({k: v for k, v in res.items() if k != "per_task"}, indent=1))
