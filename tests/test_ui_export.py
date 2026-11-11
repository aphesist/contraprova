import importlib.util
import json
import math
from pathlib import Path

import pytest

from contraprova.log import EventLog
from contraprova.pricing import DEFAULT_TABLE, PRICE_TABLES, cost_by, request_cost
from contraprova.projections import outcomes, usage_records

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


export_ui_data = _load("export_ui_data")
make_demo_log = _load("make_demo_log")

TS = "2026-11-11T00:00:00.000Z"


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "demo.jsonl"
    make_demo_log.build(path)
    return path, export_ui_data.export(path)


def close(a, b):
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-11)


def test_demo_log_verifies_and_is_marked_synthetic(demo):
    path, data = demo
    assert EventLog(path).verify() == data["source"]["n_events"] > 0
    assert data["synthetic"] is True
    assert data["source"]["head_hash"] == EventLog(path).events()[-1].hash


def test_demo_log_and_export_are_deterministic(demo, tmp_path):
    path, data = demo
    again = tmp_path / "demo.jsonl"
    make_demo_log.build(again)
    assert again.read_bytes() == path.read_bytes()
    assert export_ui_data.dumps(export_ui_data.export(again)) == export_ui_data.dumps(data)


def test_committed_demo_data_matches_a_fresh_export(demo):
    _, data = demo
    committed = json.loads((ROOT / "ui" / "demo-data.json").read_text(encoding="utf-8"))
    fresh = json.loads(export_ui_data.dumps(data))
    fresh["source"]["log"] = committed["source"]["log"]  # the file name is not part of the content
    assert committed == fresh, "regenerate ui/demo-data.json (see docs/ui.md)"


def test_required_keys(demo):
    _, data = demo
    for key in ("schema", "synthetic", "source", "default_table", "table_names", "price_tables", "arms", "tier_order",
                "attempts", "layout", "events", "checkpoints", "final"):
        assert key in data
    assert data["schema"] == "contraprova-ui/1"
    assert {a["id"] for a in data["arms"]} == {"A", "B", "C", "D", "D:control", "D:treatment"}
    for a in data["attempts"]:
        assert {"id", "arm", "task_id", "x", "y", "first_seq", "final_cost"} <= set(a)
        assert 0 <= a["x"] <= 1 and 0 <= a["y"] <= 1
    for i, e in enumerate(data["events"]):
        assert e["s"] == i and {"t", "a", "ts", "h", "x"} <= set(e)
    assert data["checkpoints"][-1]["seq"] == len(data["events"]) - 1
    assert data["final"]["arms"] == data["checkpoints"][-1]["arms"]
    assert {"method", "features", "explained_variance", "loadings_top", "note"} <= set(data["layout"])


def test_price_tables_include_every_published_table_and_the_haiku_what_if(demo):
    _, data = demo
    names = data["table_names"]
    assert set(PRICE_TABLES) <= set(names)
    assert data["default_table"] == DEFAULT_TABLE
    what_if = [n for n in names if "Haiku x0.5" in n]
    assert len(what_if) == 1
    base = data["price_tables"][DEFAULT_TABLE]
    half = data["price_tables"][what_if[0]]
    assert close(half["claude-haiku-5-5"]["input"], base["claude-haiku-5-5"]["input"] / 2)
    assert half["claude-opus-5-5"] == base["claude-opus-5-5"]


def test_costs_match_request_cost(demo):
    path, data = demo
    tables = export_ui_data.ui_price_tables()
    events = EventLog(path).events()
    n_usage = 0
    for ev, rec in zip(events, data["events"]):
        if ev.type != "usage.recorded":
            assert "c" not in rec
            continue
        n_usage += 1
        for j, name in enumerate(data["table_names"]):
            assert close(rec["c"][j], request_cost(ev.payload, tables[name]))
    assert n_usage > 0
    # Arm totals agree with the library's own grouping, under every table. Fork usage
    # (with a fork_id) is shown under the fork's label, never under its parent arm.
    labels = fork_labels(events)
    records = usage_records(events)
    arm_usage = [u for u in records if "fork_id" not in u]
    fork_usage = [{**u, "label": labels[u["fork_id"]]} for u in records if "fork_id" in u]
    assert fork_usage
    for j, name in enumerate(data["table_names"]):
        totals = {**cost_by(arm_usage, tables[name], "arm"), **cost_by(fork_usage, tables[name], "label")}
        assert set(totals) == set(data["final"]["arms"])
        for arm, usd in totals.items():
            assert close(data["final"]["arms"][arm]["cost"][j], usd)


def fork_labels(events):
    return {e.payload["fork_id"]: f"D:{e.payload['role']}" for e in events if e.type == "fork.created"}


def test_demo_uses_canonical_fork_events(demo):
    path, data = demo
    events = EventLog(path).events()
    forks = [e.payload for e in events if e.type == "fork.created"]
    assert forks and all({"fork_id", "task_id", "from_seq", "model", "role", "seed", "prefix_hash"} <= set(f) for f in forks)
    assert all(events[f["from_seq"]].hash == f["prefix_hash"] for f in forks)
    ids = {f["fork_id"] for f in forks}
    fork_outs = [e.payload for e in events if e.type == "outcome.scored" and "fork_id" in e.payload]
    assert len(fork_outs) == len(forks) and {o["fork_id"] for o in fork_outs} == ids
    # No arm uses the old "D:control" convention in the log itself; the label exists only in the export.
    assert all(":" not in str(u.get("arm", "")) for u in usage_records(events))
    assert all(":" not in str(e.payload.get("arm", "")) for e in events if e.type == "outcome.scored")
    meta = {a["id"]: a for a in data["arms"] if a["kind"] == "fork"}
    assert meta == {"D:control": {"id": "D:control", "kind": "fork", "parent": "D", "role": "control", "replicate": 1},
                    "D:treatment": {"id": "D:treatment", "kind": "fork", "parent": "D", "role": "treatment", "replicate": 1}}


def test_outcomes_and_frontier_match_the_projections(demo):
    path, data = demo
    events = EventLog(path).events()
    outs = dict(outcomes(events))
    labels = fork_labels(events)
    for e in events:
        if e.type == "outcome.scored" and "fork_id" in e.payload:
            outs[(labels[e.payload["fork_id"]], e.payload["task_id"])] = e.payload["solved"]
    for arm, s in data["final"]["arms"].items():
        mine = [v for (a, _), v in outs.items() if a == arm]
        assert s["n_scored"] == len(mine) and s["n_solved"] == sum(mine)
        lo, hi = s["wilson95"]
        assert 0 <= lo <= s["solve_rate"] <= hi <= 1
    for name, front in data["final"]["pareto"].items():
        assert front and all(":" not in arm for arm in front)


def test_not_synthetic_without_the_flag(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append("task.created", "orchestrator", {"task_id": "t1", "title": "x", "scope": ["a.py"]}, TS)
    log.append("usage.recorded", "w", {"model": "claude-haiku-5-5", "input_tokens": 1000, "output_tokens": 10, "arm": "B", "role": "worker", "task_id": "t1"}, TS)
    log.append("outcome.scored", "scorer", {"arm": "B", "task_id": "t1", "solved": True}, TS)
    data = export_ui_data.export(log.path)
    assert data["synthetic"] is False
    assert data["final"]["arms"]["B"]["n_solved"] == 1


def test_export_refuses_a_tampered_log(demo, tmp_path):
    path, _ = demo
    lines = path.read_text(encoding="utf-8").splitlines()
    bad = tmp_path / "bad.jsonl"
    rec = json.loads(lines[3])
    rec["actor"] = "someone-else"
    lines[3] = json.dumps(rec)
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(Exception):
        export_ui_data.export(bad)


def test_standalone_html_inlines_the_data(demo):
    _, data = demo
    template = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    html = export_ui_data.inline_html(template, export_ui_data.dumps(data))
    assert '<script id="embedded-data" type="application/json">{' in html
    assert "</script>" not in html.split('<script id="embedded-data" type="application/json">', 1)[1].split("</script>", 1)[0]


def test_page_makes_no_network_requests():
    html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    for needle in ("http://", "https://", "//cdn", "@import", "<link "):
        hits = [ln for ln in html.splitlines() if needle in ln and "w3.org/2000/svg" not in ln]
        assert not hits, hits


def test_shipped_fork_branch_counts_toward_its_arm(tmp_path):
    """An arm's cost includes the fork branch it shipped (outcome branch_task_id); other fork spend is
    measurement overhead. The page's fold of the cost deltas still matches Python at every checkpoint."""
    H, S = "claude-haiku-5-5", "claude-sonnet-5-5"
    log = EventLog(tmp_path / "log.jsonl")
    log.append("task.created", "h", {"task_id": "D:ex/a", "title": "ex/a", "scope": ["a"], "arm": "D", "exercise_id": "ex/a"}, TS)
    own = {"model": H, "input_tokens": 5000, "output_tokens": 700, "arm": "D", "role": "worker", "task_id": "D:ex/a"}
    log.append("usage.recorded", "w", own, TS)
    log.append("task.claimed", "w", {"task_id": "D:ex/a"}, TS)
    log.append("task.submitted", "w", {"task_id": "D:ex/a"}, TS)
    gate = log.append("gate.decided", "gate", {"task_id": "D:ex/a", "gate": "g", "verdict": "block", "reason": "x"}, TS)
    fork_usage = {}
    for branch, role, model, n in (("control-1", "control", H, 4000), ("treatment", "treatment", S, 4200)):
        fid = f"f-{branch}"
        log.append("fork.created", "h", {"fork_id": fid, "task_id": "D:ex/a", "from_seq": gate.seq, "model": model,
                                         "role": role, "seed": 1, "fork_task_id": f"D:ex/a:{branch}", "arm": "D"}, TS)
        u = {"model": model, "input_tokens": n, "output_tokens": 600, "arm": "D", "role": "worker",
             "task_id": f"D:ex/a:{branch}", "fork_id": fid}
        log.append("usage.recorded", "w", u, TS)
        fork_usage[branch] = u
        log.append("outcome.scored", "s", {"fork_id": fid, "task_id": "ex/a", "solved": role == "treatment"}, TS)
    log.append("outcome.scored", "s", {"arm": "D", "task_id": "ex/a", "log_task_id": "D:ex/a", "solved": True,
                                       "branch_task_id": "D:ex/a:treatment"}, TS)
    data = export_ui_data.export(log.path)
    tables = export_ui_data.ui_price_tables()
    for j, name in enumerate(data["table_names"]):
        t = tables[name]
        final = data["final"]["arms"]
        assert close(final["D"]["cost"][j], request_cost(own, t) + request_cost(fork_usage["treatment"], t))
        assert close(final["D:treatment"]["cost"][j], request_cost(fork_usage["treatment"], t))
        assert close(final["D:control"]["cost"][j], request_cost(fork_usage["control-1"], t))
        assert data["final"]["measurement_overhead"] == {"D": {"control": data["final"]["measurement_overhead"]["D"]["control"]}}
        assert close(data["final"]["measurement_overhead"]["D"]["control"][j], request_cost(fork_usage["control-1"], t))
    # Fold the deltas the way ui/index.html does and compare with the final Python totals.
    folded = {}
    for e in data["events"]:
        for ix, f, v in e.get("fx", []):
            if f == "cost":
                arm = data["attempts"][ix]["arm"]
                acc = folded.setdefault(arm, [0.0] * len(v))
                for j, c in enumerate(v):
                    acc[j] += c
    for arm, s in data["final"]["arms"].items():
        assert all(close(a, b) for a, b in zip(folded[arm], s["cost"]))
