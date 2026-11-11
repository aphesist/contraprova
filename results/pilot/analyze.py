"""Pilot stop-rule analysis. Re-runs visible (and gate-private) tests locally on every Haiku first
attempt in the log ($0): arms B and C were never gated, D's initial Haiku attempts were."""
import json, sys, math
from collections import defaultdict
from pathlib import Path
from contraprova.log import EventLog
from contraprova.projections import usage_records, arm_usage_records
from contraprova.pricing import PRICE_TABLES, DEFAULT_TABLE, request_cost
from contraprova.stats import wilson_interval as wilson
from contraprova.harness.polyglot import load_exercises, Partition, TestSplit
from contraprova.harness.arms import TaskContext
from contraprova.harness.runners import polyglot_gates
from contraprova.interfaces import Submission, HAIKU

pb, logp, outp = sys.argv[1:4]
ev = EventLog(logp).events()
part = Partition.load(Path(logp).parent / "partition.json")
by = {e.id: e for e in load_exercises(pb)}
T = PRICE_TABLES[DEFAULT_TABLE]
out = {}

# spend
recs = usage_records(ev)
tot = sum(request_cost(u, T) for u in recs)
def group(rs, k):
    g = defaultdict(float)
    for u in rs: g[str(u.get(k))] += request_cost(u, T)
    return dict(g)
out["spend_total"] = tot
out["spend_by_model"] = group(recs, "model")
out["spend_by_arm_raw"] = group(recs, "arm")
out["spend_by_arm_shipped_rule"] = group(arm_usage_records(ev), "arm")
tasks = {e.payload["task_id"]: e.payload for e in ev if e.type == "task.created"}
split_of = {tid: p.get("split") for tid, p in tasks.items()}
per = defaultdict(lambda: defaultdict(float))
for u in recs:
    per[split_of.get(u["task_id"], "?")][u["arm"]] += request_cost(u, T)
n_tasks = defaultdict(lambda: defaultdict(int))
for tid, p in tasks.items():
    if "fork_of" not in p: n_tasks[p["split"]][p["arm"]] += 1
out["per_task_cost"] = {s: {a: per[s][a] / n_tasks[s][a] for a in per[s] if n_tasks[s][a]} for s in per}
out["n_tasks"] = {s: dict(v) for s, v in n_tasks.items()}
out["max_prompt_tokens"] = {}
for u in recs:
    pt = u["input_tokens"] + u.get("cache_read_tokens", 0) + u.get("cache_write_5m_tokens", 0) + u.get("cache_write_1h_tokens", 0)
    out["max_prompt_tokens"][u["model"]] = max(out["max_prompt_tokens"].get(u["model"], 0), pt)
out["haiku_over_100k"] = sum(1 for u in recs if u["model"] == HAIKU and u["input_tokens"] + u.get("cache_read_tokens", 0) + u.get("cache_write_5m_tokens", 0) > 100_000)

# outcomes
outc = defaultdict(dict)
for e in ev:
    p = e.payload
    if e.type == "outcome.scored" and "arm" in p and "fork_id" not in p:
        outc[p.get("split")][(p["arm"], p["task_id"])] = p["solved"]
out["solved"] = {s: {a: f"{sum(v for (aa, _), v in d.items() if aa == a)}/{sum(1 for (aa, _) in d if aa == a)}" for a in "ABCD"} for s, d in outc.items()}

# Haiku first attempts
subs = [e.payload for e in ev if e.type == "task.submitted" and e.payload["attempt"] == 1 and e.payload["model"] == HAIKU]
rows = []
for s in subs:
    tid = s["task_id"]; t = tasks[tid]
    if "fork_of" in t: continue
    if t["arm"] not in ("B", "C", "D"): continue
    ex = by[t["exercise_id"]]
    ts = TestSplit.from_dict(ex.id, part.test_splits[ex.id])
    ctx = TaskContext(t["arm"], tid, ex, ts, HAIKU, 1, split=t["split"])
    res = polyglot_gates(Submission(tid, s["contents"], 1), ctx)
    v = {r.gate: r.verdict for r in res}
    rows.append({"task": tid, "split": t["split"], "method": ts.method, "parse_errors": s["parse_errors"],
                 "visible": v.get("T0.visible_tests", "not_run"), "private": v.get("T1.private_tests", "n/a"),
                 "scope": v.get("T0.scope"), "review": v.get("T2.review", "n/a"),
                 "reasons": [r.reason[:150] for r in res if r.verdict != "pass"]})
out["haiku_first_attempts"] = rows
n = len(rows)
fail_vis = sum(1 for r in rows if r["scope"] != "pass" or r["visible"] != "pass")
fail_gate = sum(1 for r in rows if any(x == "block" for x in (r["scope"], r["visible"], r["private"], r["review"])))
vis_ok_priv_fail = sum(1 for r in rows if r["visible"] == "pass" and r["private"] == "block")
out["haiku_visible_fail"] = {"k": fail_vis, "n": n, "rate": fail_vis / n if n else None, "wilson95": wilson(fail_vis, n) if n else None}
out["haiku_any_gate_fail"] = {"k": fail_gate, "n": n, "wilson95": wilson(fail_gate, n) if n else None}
out["haiku_visible_pass_private_fail"] = {"k": vis_ok_priv_fail, "n": n}
for key in ("haiku_visible_fail", "haiku_any_gate_fail"):
    k = out[key]["k"]; lo, hi = out[key]["wilson95"]
    out[key]["forks_train_eval_180"] = {"point": 180 * k / n, "lo": 180 * lo, "hi": 180 * hi}
# forks observed
out["forks_observed"] = sum(1 for e in ev if e.type == "fork.created")
out["blocks_observed"] = [(e.payload["task_id"], e.payload["gate"], e.payload["reason"][:150]) for e in ev
                          if e.type == "gate.decided" and e.payload["verdict"] == "block"]
# protocol: orchestrator/reviewer roles never submit files
out["orchestrator_submissions"] = sum(1 for e in ev if e.type == "task.submitted" and not e.actor.startswith("worker:"))
json.dump(out, open(outp, "w"), indent=1, default=str)
print(json.dumps({k: v for k, v in out.items() if k != "haiku_first_attempts"}, indent=1, default=str))
