"""End to end: every module reads the same log, written in the shapes of docs/events.md.

Arms A, B, C and D run through the harness API with the offline dry-run parts (no API call,
nothing executed) into ONE log, then the kernel, analysis, router, OPE and UI exporter all read it.
"""

import importlib.util
from pathlib import Path

import pytest

from contraprova import cli
from contraprova.analysis import analyze, report_markdown
from contraprova.harness.arms import ARMS, ArmConfig, BudgetGuard, Harness, run_arm
from contraprova.harness.polyglot import load_exercises, split_tests
from contraprova.harness.run import FakeClient, UniformRouter, dry_run_gates, no_exec_scorer
from contraprova.interfaces import HAIKU, SONNET
from contraprova.log import EventLog
from contraprova.ope import ips, snips, steps_from_examples
from contraprova.pricing import DEFAULT_TABLE, PRICE_TABLES, request_cost
from contraprova.projections import outcomes, usage_records
from contraprova.router import training_examples

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures" / "polyglot"


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    exercises = load_exercises(FIXTURES)
    splits = {e.id: split_tests(e, 7) for e in exercises}
    log = EventLog(tmp_path_factory.mktemp("e2e") / "all-arms.jsonl")
    h = Harness(log, FakeClient(), no_exec_scorer, dry_run_gates, UniformRouter(), BudgetGuard(5.0), ArmConfig(),
                "dry-run-partition")
    for arm in ARMS:
        summary = run_arm(h, arm, exercises, splits, "pilot")
        assert summary.stopped is None and summary.attempted == len(exercises)
    return log, exercises


def test_log_verifies(run):
    log, _ = run
    assert log.verify() == len(log.events()) > 0


def test_events_follow_the_canonical_schema(run):
    log, exercises = run
    evs = log.events()
    forks = [e.payload for e in evs if e.type == "fork.created"]
    assert forks, "the dry-run coin flip should block at least one first attempt of arm D"
    for f in forks:
        assert {"fork_id", "task_id", "from_seq", "model", "role", "seed"} <= set(f)
        assert evs[f["from_seq"]].type == "gate.decided" and evs[f["from_seq"]].payload["verdict"] == "block"
        assert f["prefix_hash"] == evs[f["from_seq"]].hash
    for e in evs:
        p = e.payload
        if e.type == "outcome.scored":
            assert p["task_id"] in {x.id for x in exercises}  # the pairing key
            assert ("fork_id" in p) or p["arm"] in ARMS
        if e.type == "usage.recorded":
            assert p["arm"] in ARMS and ":" in p["task_id"]  # a kernel task id
    assert {a for a, _ in outcomes(evs)} == set(ARMS)


def test_report_has_all_arms_and_an_escalation_row(run):
    log, exercises = run
    evs = log.events()
    r = analyze(evs, iters=200)
    assert list(r.arms) == list(ARMS)
    assert all(r.arms[a].n == len(exercises) for a in ARMS)
    assert all(r.arms[a].unattributed_cost == pytest.approx(0, abs=1e-12) for a in ARMS)
    n_points = len({(f.payload["task_id"], f.payload["from_seq"]) for f in evs if f.type == "fork.created"})
    (esc,) = r.escalation
    assert (esc.treatment_model, esc.control_model, esc.n) == (SONNET, HAIKU, n_points)
    assert r.noise_floors[HAIKU].n == n_points  # two controls with distinct seeds at every fork point
    assert not r.warnings, r.warnings
    # Arm D's cost: its own usage plus the one branch it shipped; the rest of the fork spend is overhead.
    total = sum(request_cost(u, PRICE_TABLES[DEFAULT_TABLE]) for u in usage_records(evs))
    assert sum(s.total_cost for s in r.arms.values()) + r.measurement_overhead_cost == pytest.approx(total, rel=1e-9)
    assert {(o.arm, o.role) for o in r.measurement_overhead} == {("D", "control"), ("D", "treatment")}
    md = report_markdown(evs, iters=200)
    for arm in ARMS:
        assert f"\n| {arm} | {len(exercises)} |" in md
    assert f"| {SONNET} vs {HAIKU} | {n_points} |" in md


def test_cli_verify_and_report(run, capsys):
    log, _ = run
    assert cli.main(["verify", str(log.path)]) == 0
    assert cli.main(["report", str(log.path)]) == 0
    out = capsys.readouterr().out
    assert "chain intact" in out and "## Escalation value" in out


def test_router_and_ope_read_the_same_log(run):
    log, exercises = run
    evs = log.events()
    ex = training_examples(evs)
    routes = [e for e in ex if e.source == "route"]
    forks = [e for e in ex if e.source != "route"]
    # One example per exercise, plus the initial decision of every forked task (unsolved at the fork).
    forked = {e.payload["task_id"] for e in evs if e.type == "fork.created"}
    assert sorted(e.task_id for e in routes) == sorted([f"D:{x.id}" for x in exercises] + list(forked))
    n_forks = sum(e.type == "fork.created" for e in evs)
    assert len(forks) == n_forks and {e.source for e in forks} == {"control", "treatment"}
    assert all(e.cost > 0 for e in forks)
    steps = steps_from_examples(ex, cost_weight=1.0)
    assert len(steps) == len(routes) and all(s.propensity == 0.5 for s in steps)

    def always_haiku(_ctx):
        return {HAIKU: 1.0, SONNET: 0.0}

    assert ips(steps, always_haiku) <= 0 and snips(steps, always_haiku) <= 0  # nothing is solved in a dry run


def test_ui_export_reads_the_same_log(run, tmp_path):
    log, exercises = run
    export_ui_data = _load_script("export_ui_data")
    data = export_ui_data.export(log.path)
    out = tmp_path / "data.json"
    assert export_ui_data.main([str(log.path), "-o", str(out)]) == 0 and out.stat().st_size > 0
    ids = {a["id"] for a in data["arms"]}
    assert set(ARMS) <= ids and {"D:control", "D:control-2", "D:treatment"} <= ids
    final = data["final"]["arms"]
    n_points = data["final"]["escalation"]["D"]["n_pairs"]
    assert n_points > 0
    for arm in ARMS:
        assert final[arm]["n_scored"] == len(exercises) and final[arm]["n_tasks"] == len(exercises)
    for label in ("D:control", "D:control-2", "D:treatment"):
        assert final[label]["n_scored"] == n_points
    # Attempts are keyed by exercise id, so arms line up task by task.
    assert {a["task_id"] for a in data["attempts"]} == {x.id for x in exercises}
    from contraprova.pricing import request_cost

    table = export_ui_data.ui_price_tables()[data["table_names"][0]]
    total = sum(request_cost(u, table) for u in usage_records(log.events()))
    # Every usage record's cost lands under its fork's label or its arm, and the arm also carries
    # the branch it shipped; so main arms + measurement overhead = all spend, and the fork labels
    # sum to the shipped branches + overhead.
    overhead = sum(c[0] for roles in data["final"]["measurement_overhead"].values() for c in roles.values())
    main = sum(final[a]["cost"][0] for a in ARMS)
    assert main + overhead == pytest.approx(total, rel=1e-9)
    from contraprova.analysis import analyze

    r = analyze(log.events(), data["table_names"][0], iters=10)
    assert final["D"]["cost"][0] == pytest.approx(r.arms["D"].total_cost, rel=1e-9)
    assert overhead == pytest.approx(r.measurement_overhead_cost, rel=1e-9)
    assert r.arms["D"].shipped_fork_cost > 0  # the dry run ships a fork branch for every blocked task
