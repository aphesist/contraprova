"""The replay UI on the real SWE-bench Pro main study (results/swepro-main/log.jsonl).

The committed export ui/data/swepro-main.json must be a fresh export of the committed log and
analysis, its costs must be pricing.request_cost of the logged tokens, its outcomes the projection's,
its headline numbers the analysis file's, and it must carry no file contents.
"""

import functools
import http.server
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from contraprova.log import EventLog
from contraprova.pricing import DEFAULT_TABLE, request_cost
from contraprova.projections import outcomes

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "results" / "swepro-main"
LOG = STUDY / "log.jsonl"
ANALYSIS = STUDY / "analysis.json"
COMMITTED = ROOT / "ui" / "data" / "swepro-main.json"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


export_ui_data = _load("export_ui_data")


@pytest.fixture(scope="module")
def study():
    return EventLog(LOG).events(), export_ui_data.export(LOG, ANALYSIS), json.loads(ANALYSIS.read_text(encoding="utf-8"))


def close(a, b):
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-9)


def test_export_is_deterministic_and_committed_copy_is_fresh(study):
    _, data, _ = study
    again = export_ui_data.export(LOG, ANALYSIS)
    assert export_ui_data.dumps(again) == export_ui_data.dumps(data)
    committed = json.loads(COMMITTED.read_text(encoding="utf-8"))
    assert committed == json.loads(export_ui_data.dumps(data)), "regenerate ui/data/swepro-main.json (see docs/ui.md)"


def test_export_is_compact():
    assert COMMITTED.stat().st_size < 1_500_000


def test_real_log_is_not_synthetic(study):
    events, data, _ = study
    assert data["synthetic"] is False
    assert data["source"]["n_events"] == len(events) == EventLog(LOG).verify()
    assert data["source"]["head_hash"] == events[-1].hash


def test_every_usage_cost_is_request_cost(study):
    events, data, _ = study
    tables = export_ui_data.ui_price_tables()
    n = 0
    for ev, rec in zip(events, data["events"]):
        if ev.type != "usage.recorded":
            assert "c" not in rec
            continue
        n += 1
        for j, name in enumerate(data["table_names"]):
            assert math.isclose(rec["c"][j], request_cost(ev.payload, tables[name]), rel_tol=0, abs_tol=1e-11)
    assert n == sum(e.type == "usage.recorded" for e in events) == 300


def test_arm_totals_match_the_analysis(study):
    _, data, analysis = study
    j = data["table_names"].index(DEFAULT_TABLE)
    final, total = data["final"]["arms"], analysis["cost"]["total"]
    assert close(final["A"]["cost"][j], total["A"]) and f"{final['A']['cost'][j]:.4f}" == "11.8125"
    assert close(final["D"]["cost"][j], total["D"]) and f"{final['D']['cost'][j]:.4f}" == "4.5328"
    assert close(final["B"]["cost"][j], total["B_derived"])
    # fork controls are measurement overhead: kept apart, in no arm's cost
    overhead = data["final"]["measurement_overhead"]
    assert set(overhead) == {"D"} and set(overhead["D"]) == {"control"}
    assert close(overhead["D"]["control"][j], total["D_measurement"]) and f"{overhead['D']['control'][j]:.4f}" == "0.0575"
    # all recorded spend = A + D + the overhead (B is a view of D's Haiku requests, not more spend)
    assert close(final["A"]["cost"][j] + final["D"]["cost"][j] + overhead["D"]["control"][j], total["all_recorded"])
    for arm, key in (("A", "A"), ("D", "D"), ("B", "B_derived")):
        assert close(final[arm]["cost"][j] / final[arm]["n_solved"], analysis["cost"]["per_solved"][key])
        assert close(final[arm]["cost_per_task"][j], analysis["cost"]["per_task_mean"][key])


def test_outcomes_match_the_projection(study):
    events, data, analysis = study
    outs = outcomes(events)
    final = data["final"]["arms"]
    for arm in ("A", "B", "D"):
        mine = [v for (a, _), v in outs.items() if a == arm]
        assert final[arm]["n_scored"] == len(mine) == 88 and final[arm]["n_solved"] == sum(mine)
    for arm, key in (("A", "A"), ("D", "D"), ("B", "B_derived")):
        assert final[arm]["wilson95"] == [round(x, 12) for x in analysis["solve"][key]["wilson95"]]
    labels = {e.payload["fork_id"]: f"D:{e.payload['role']}" for e in events if e.type == "fork.created"}
    forks = [e.payload for e in events if e.type == "outcome.scored" and "fork_id" in e.payload]
    for label in ("D:control", "D:treatment"):
        mine = [p["solved"] for p in forks if labels[p["fork_id"]] == label]
        assert final[label]["n_scored"] == len(mine) == 8 and final[label]["n_solved"] == sum(mine)
    esc = analysis["escalation_value"]
    assert data["final"]["escalation"]["D"]["treatment_solved"] == esc["treatment_opus"]["solved"]
    assert data["final"]["escalation"]["D"]["control_solved"] == esc["control_haiku_noise_floor"]["solved"]


def test_arms_forks_tasks_and_repos(study):
    events, data, _ = study
    meta = {a["id"]: a for a in data["arms"]}
    assert list(meta) == ["A", "B", "D", "D:control", "D:treatment"]
    assert meta["B"] == {"id": "B", "kind": "arm", "derived_from": "D"}
    assert meta["D:control"]["model"] == "claude-haiku-5-5" and meta["D:control"]["role"] == "control"
    assert meta["D:treatment"]["model"] == "claude-opus-5-5" and meta["D:treatment"]["role"] == "treatment"
    repo = {e.payload["exercise_id"]: e.payload["repo"] for e in events if e.type == "task.created"}
    assert all(a["task_id"].startswith("instance_") and a["repo"] == repo[a["task_id"]] for a in data["attempts"])
    assert data["study"]["repos"] == sorted(set(repo.values())) and data["study"]["benchmark"] == "swepro"
    fx = [f for e in data["events"] for f in e.get("fx", [])]
    blocks = [e for e in events if e.type == "gate.decided" and e.payload["verdict"] == "block"]
    assert sum(f[1] == "blocked" for f in fx) == len(blocks) == 16
    repairs = [e for e in events if e.type == "usage.recorded" and e.payload.get("repair") is True]
    assert sum(f[1] == "repair" for f in fx) == len(repairs) == 20
    assert sum("(repair turn)" in e["x"] for e in data["events"]) == len(repairs)


def test_no_file_contents_in_the_export(study):
    events, data, _ = study
    text = export_ui_data.dumps(data)
    for marker in ("diff --git", "@@ -", "<<<<<<< SEARCH", ">>>>>>> REPLACE", '"contents"', '"diff"', '"response"'):
        assert marker not in text, marker
    keys = {"s", "t", "a", "ts", "h", "x", "k", "p", "fx", "c", "st"}
    assert all(set(e) <= keys for e in data["events"])
    lines = set()
    for e in events:
        if e.type == "task.submitted":
            for field in ("diff", "response", "repair_response"):
                v = e.payload.get(field)
                if isinstance(v, str):
                    lines |= {ln.strip() for ln in v.splitlines() if len(ln.strip()) >= 30}
    assert lines and not [ln for ln in lines if json.dumps(ln)[1:-1] in text]


def fold(data):
    """The page's fold (ui/index.html fold + summarise), in Python, compared at every checkpoint."""
    T = len(data["table_names"])
    arms = {}
    results = []
    cps = {c["seq"]: c for c in data["checkpoints"]}
    for e in data["events"]:
        for ix, f, v in e.get("fx", []):
            at = data["attempts"][ix]
            m = arms.setdefault(at["arm"], {"cost": [0.0] * T, "tasks": set(), "out": {}})
            if f == "cost":
                m["cost"] = [a + b for a, b in zip(m["cost"], v)]
                m["tasks"].add(at["task_id"])
            elif f == "solved":
                m["out"][at["task_id"]] = v
                m["tasks"].add(at["task_id"])
        if e["s"] in cps:
            py = cps[e["s"]]["arms"]
            ok = set(py) == set(arms) and all(
                len(arms[a]["tasks"]) == s["n_tasks"] and len(arms[a]["out"]) == s["n_scored"]
                and sum(arms[a]["out"].values()) == s["n_solved"]
                and all(abs(x - y) <= 1e-9 + 1e-9 * abs(y) for x, y in zip(arms[a]["cost"], s["cost"]))
                for a, s in py.items())
            results.append(ok)
    return results


def test_replay_check_holds_at_every_checkpoint(study):
    _, data, _ = study
    res = fold(data)
    assert len(res) == len(data["checkpoints"]) == 30 and all(res)


def _signed(x, keep_zero=True):
    t = f"{abs(x):.1f}"
    if not keep_zero and t.endswith(".0"):
        t = t[:-2]
    return ("−" if round(x, 1) < 0 else "+" if round(x, 1) > 0 else "") + t


def test_honesty_rows_match_the_analysis(study):
    _, data, a = study
    rows = data["study"]["honesty"]
    pr, co, ev = a["primary"], a["co_primary"], a["escalation_value"]
    assert [r["word"] for r in rows] == [f"measured (pre-registered, n={a['n_pairs']})", "measured", "measured, imprecise"]
    assert all(r["kind"] == "measured" for r in rows)
    lo, hi = pr["ci95_points"]
    assert f"{_signed(pr['diff_D_minus_A_points'], False)} pts [{_signed(lo)}, {_signed(hi)}]" in rows[0]["claim"]
    assert "−2.3 pts [−8.0, +3.4]" in rows[0]["claim"] and pr["non_inferior"] is True
    assert f"{co['cost_per_solved_ratio_D_over_A']:.2f} [{co['ci95'][0]:.2f}, {co['ci95'][1]:.2f}]" in rows[1]["claim"]
    assert "0.39 [0.30, 0.51]" in rows[1]["claim"] and co["cheaper"] is True
    assert f"{_signed(ev['EV_points'], False)} pts [{_signed(ev['ci95_points'][0])}, {_signed(ev['ci95_points'][1])}], {ev['n_forks']} forks" in rows[2]["claim"]
    assert "+50 pts [+12.5, +87.5], 8 forks" in rows[2]["claim"]
    # every number in a row is one the analysis gives (rounded as shown)
    allowed = {f"{abs(x):.{d}f}" for x in (pr["diff_D_minus_A_points"], lo, hi, pr["margin_points"], co["cost_per_solved_ratio_D_over_A"],
                                          *co["ci95"], ev["EV_points"], *ev["ci95_points"]) for d in (0, 1, 2)} | {str(ev["n_forks"])}
    for r in rows:
        for num in re.findall(r"\d+(?:\.\d+)?", r["claim"].split(":", 1)[1]):
            assert num in allowed, (num, r["claim"])


def test_stat_tiles_match_the_analysis(study):
    _, data, a = study
    tiles = data["study"]["tiles"]
    by = {t.get("arm"): t for t in tiles if t.get("arm")}
    for arm, key in (("A", "A"), ("D", "D"), ("B", "B_derived")):
        s = a["solve"][key]
        assert by[arm]["value"] == f"{100 * s['rate']:.1f}%" and by[arm]["small"] == f"{s['solved']}/{s['n']}"
        assert by[arm]["note"] == f"95% wilson {100 * s['wilson95'][0]:.1f}%–{100 * s['wilson95'][1]:.1f}%"
    rest = [t for t in tiles if not t.get("arm")]
    assert rest[0]["value"] == f"${a['cost']['per_solved']['D']:.3f}" and f"${a['cost']['per_solved']['A']:.3f}" in rest[0]["note"]
    assert rest[1]["value"] == str(a["escalation_value"]["n_forks"]) == "8" and rest[1]["small"] == f"/ {a['n_pairs']}"
    assert rest[2]["value"] == f"{100 * a['orchestrator_share_D']['dollars']:.1f}%"


def test_an_analysis_of_another_log_is_refused(tmp_path):
    bad = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    bad["solve"]["D"]["solved"] += 1
    p = tmp_path / "analysis.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="does not describe this log"):
        export_ui_data.export(LOG, p)


def test_page_shows_the_main_study_by_default_and_links_the_demo():
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'MAIN_DATA = "data/swepro-main.json"' in html and 'DEMO_DATA = "demo-data.json"' in html
    assert 'href="./?demo"' in html and (ROOT / "ui" / "demo-data.json").is_file()
    # the study's numbers are rendered from the export, never typed into the page
    assert "D.study.honesty" in html and "st.tiles" in html
    for literal in ("2.3", "0.39", "87.5", "11.8125", "4.5328"):
        assert literal not in html.split("<script>", 1)[1], literal


# ------------------------------------------------------------------ the page itself, in a browser (when one is installed)

NODE = shutil.which("node") or ("/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else None)
NODE_PATH = os.environ.get("NODE_PATH") or "/opt/node22/lib/node_modules"

BROWSER_SCRIPT = r"""
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [], origins = new Set();
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => origins.add(new URL(r.url()).origin));
  await page.goto(process.argv[2], { waitUntil: "networkidle" });
  await page.waitForSelector("#app:not([hidden])");
  const out = await page.evaluate(() => ({
    check: document.getElementById("check").textContent,
    banner: !document.getElementById("banner").hidden,
    rows: [...document.querySelectorAll("#honesty tbody tr")].map((tr) => [...tr.cells].map((c) => c.textContent.trim())),
    tiles: document.querySelectorAll("#tiles .lab-stat").length,
  }));
  await browser.close();
  console.log(JSON.stringify({ ...out, errors, origins: [...origins] }));
})();
"""


def _have_playwright():
    if not NODE:
        return False
    r = subprocess.run([NODE, "-e", "require.resolve('playwright')"], env={**os.environ, "NODE_PATH": NODE_PATH}, capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _have_playwright(), reason="node with playwright is not installed")
def test_page_honesty_rows_in_a_browser_match_the_analysis(study, tmp_path):
    _, data, _ = study
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "ui"))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    script = tmp_path / "page.js"
    script.write_text(BROWSER_SCRIPT, encoding="utf-8")
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        r = subprocess.run([NODE, str(script), url], env={**os.environ, "NODE_PATH": NODE_PATH}, capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown()
    if r.returncode != 0 and "Executable doesn't exist" in r.stderr:
        pytest.skip("playwright has no browser installed")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["errors"] == [] and out["origins"] == [url.rstrip("/")]
    assert out["banner"] is False and out["tiles"] == len(data["study"]["tiles"])
    n = len(data["checkpoints"])
    assert out["check"] == f"replay check: {n}/{n} checkpoints match python"
    rows = out["rows"]
    assert [r[0] for r in rows[:3]] == [h["claim"] for h in data["study"]["honesty"]]
    assert [r[1] for r in rows[:3]] == [h["word"] for h in data["study"]["honesty"]]
    assert rows[3] == [f"this page's fold of the log equals python's arm totals at every checkpoint ({n}/{n})", "tested"]
