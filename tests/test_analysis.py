import math

import pytest

from contraprova.analysis import (
    DEFAULT_PRIMARY,
    analyze,
    arm_summaries,
    escalation_values,
    gate_quality,
    paired_contrasts,
    pareto,
    report_markdown,
    resolve_table,
)
from contraprova.log import EventLog
from contraprova.pricing import PRICE_TABLES, ModelPrice, request_cost, scale_table
from contraprova.projections import usage_records
from contraprova.stats import wilson_interval
from contraprova.usage import usage_payload

T = PRICE_TABLES["anthropic-2026-11"]
TS = "2026-11-11T00:00:00.000Z"
ITERS = 2000
HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


def close(a, b, tol=1e-12):
    return math.isclose(a, b, rel_tol=tol, abs_tol=1e-15)


def add(log, type_, payload):
    return log.append(type_, "test", payload, ts=TS)


def outcome(log, arm, task, solved, **kw):
    return add(log, "outcome.scored", {"arm": arm, "task_id": task, "solved": solved, **kw})


def usage(log, model, usage_obj, **tags):
    return add(log, "usage.recorded", usage_payload(usage_obj, model, **tags))


def three_arm_log(path):
    """A: Opus alone, 6/10. C: Opus orchestrator + Haiku worker, 5/10. B: Haiku alone, 3/10."""
    log = EventLog(path)
    for i in range(10):
        t = f"t{i}"
        outcome(log, "A", t, i < 6)
        usage(log, OPUS, {"input_tokens": 1000, "output_tokens": 500}, arm="A", role="worker", task_id=t)
        outcome(log, "C", t, i < 5)
        usage(log, OPUS, {"input_tokens": 1000, "output_tokens": 100}, arm="C", role="orchestrator", task_id=t)
        usage(
            log,
            HAIKU,
            {"input_tokens": 10_000, "cache_read_input_tokens": 8000, "output_tokens": 2000},
            arm="C",
            role="worker",
            task_id=t,
        )
        outcome(log, "B", t, i < 3)
        usage(log, HAIKU, {"input_tokens": 10_000, "output_tokens": 2000}, arm="B", role="worker", task_id=t)
    return log


@pytest.fixture(scope="module")
def three(tmp_path_factory):
    return three_arm_log(tmp_path_factory.mktemp("three") / "log.jsonl").events()


# ------------------------------------------------------------------ arms


def test_arm_numbers_match_hand_computation(three):
    s = arm_summaries(three, seed=0, iters=ITERS)
    a, c = s["A"], s["C"]
    assert (a.n, a.solved) == (10, 6)
    assert a.solve_rate.value == 0.6 and a.solve_rate.n == 10
    # Wilson 6/10 by hand: centre 0.572239, half-width 0.259572.
    assert math.isclose(a.solve_rate.low, 0.312667, abs_tol=1e-5)
    assert math.isclose(a.solve_rate.high, 0.831811, abs_tol=1e-5)
    # Opus: 1000 x $4/M + 500 x $20/M = $0.014 per task.
    assert close(a.cost_per_task.value, 0.014) and close(a.total_cost, 0.14)
    assert close(a.cost_per_task.low, 0.014) and close(a.cost_per_task.high, 0.014)
    assert close(a.cost_per_solved.value, 0.14 / 6)
    assert a.cost_per_solved.low < 0.14 / 6 < a.cost_per_solved.high
    assert a.orchestrator_token_share.value == 0.0
    # C: orchestrator 1000 in + 100 out on Opus = $0.006 (1100 tokens);
    # worker Haiku 10000 in + 8000 cache read + 2000 out = $0.00208 (20000 tokens).
    assert close(c.cost_per_task.value, 0.00808)
    assert close(c.orchestrator_token_share.value, 1100 / 21_100)
    assert close(c.orchestrator_cost_share.value, 0.006 / 0.00808)
    assert close(c.cache_read_share.value, 8000 / 19_000)
    assert c.requests == 20 and c.unattributed_cost == pytest.approx(0.0, abs=1e-15)


def test_cost_per_solved_interval_is_unbounded_when_no_solves_possible(tmp_path):
    log = EventLog(tmp_path / "l.jsonl")
    outcome(log, "B", "t0", True)
    outcome(log, "B", "t1", False)
    usage(log, HAIKU, {"input_tokens": 1000}, arm="B", task_id="t0")
    usage(log, HAIKU, {"input_tokens": 3000}, arm="B", task_id="t1")
    s = arm_summaries(log.events(), iters=ITERS)["B"]
    assert close(s.cost_per_solved.value, 0.0004)
    assert math.isinf(s.cost_per_solved.high)  # some resamples draw only t1


# ------------------------------------------------------------------ contrasts


def contrast_log(path):
    log = EventLog(path)
    y = set(range(10))
    arms = {
        "Y": y,
        "X": (y - {0}) | set(range(10, 17)),  # X only 7, Y only 1
        "Z": y | set(range(10, 18)),  # Z only 8, Y only 0
        "W": (y - {0, 1}) | {10, 11},  # 2 and 2
    }
    for arm, solved in arms.items():
        for i in range(20):
            outcome(log, arm, f"t{i}", i in solved)
    return log


def test_mcnemar_holm_and_paired_difference(tmp_path):
    evs = contrast_log(tmp_path / "c.jsonl").events()
    prim = [("X", "Y"), ("Z", "Y"), ("W", "Y")]
    cs = paired_contrasts(evs, primary=prim, secondary=[("X", "Z")], iters=ITERS)
    x, z, w, sec = cs
    assert (x.n, x.a_only, x.b_only) == (20, 7, 1)
    assert close(x.p_exact, 2 * 9 / 256)  # 2 * (C(8,0)+C(8,1)) / 2^8
    assert close(z.p_exact, 2 / 256)
    assert w.p_exact == 1.0
    # Holm by hand: sorted 0.0078125*3, 0.0703125*2, 1.0*1.
    assert close(z.p_holm, 0.0234375)
    assert close(x.p_holm, 0.140625)
    assert w.p_holm == 1.0
    assert not sec.primary and sec.p_holm is None
    assert close(x.solve_diff.value, 0.3) and x.solve_diff.n == 20
    assert 0 < x.solve_diff.low < 0.3 < x.solve_diff.high


def test_missing_primary_contrast_stays_in_holm_family(tmp_path):
    evs = contrast_log(tmp_path / "c.jsonl").events()
    cs = paired_contrasts(evs, primary=[("X", "Y"), ("Z", "Y"), ("W", "Y"), ("Q", "Y")], iters=200)
    x, z, w, q = cs
    assert q.n == 0 and q.p_exact is None and q.p_holm == 1.0
    assert close(z.p_holm, 0.0078125 * 4)
    assert close(x.p_holm, 0.0703125 * 3)


def test_default_primary_family_is_the_design_list(three):
    cs = paired_contrasts(three, iters=50)
    assert [(c.a, c.b) for c in cs] == list(DEFAULT_PRIMARY)
    assert all(c.p_holm == 1.0 for c in cs)


def test_api_arm_secondary_contrast(three):
    r = analyze(three, secondary=[("C", "A")], iters=ITERS)
    (ca,) = [c for c in r.contrasts if not c.primary]
    assert (ca.a_only, ca.b_only) == (0, 1)
    assert close(ca.cost_per_task_diff.value, 0.00808 - 0.014)
    assert close(ca.cost_per_solved_diff.value, 0.0808 / 5 - 0.14 / 6)


# ------------------------------------------------------------------ escalation


def fork_log(path, control_solves, treatment_solves, second_control_solves, n=30):
    log = EventLog(path)
    for i in range(n):
        t = f"e{i}"
        created = add(log, "task.created", {"task_id": t, "title": t, "scope": []})
        for fid, model, role, seed, solves in (
            (f"{t}-c1", HAIKU, "control", 1, control_solves),
            (f"{t}-c2", HAIKU, "control", 2, second_control_solves),
            (f"{t}-t1", SONNET, "treatment", 1, treatment_solves),
        ):
            add(
                log,
                "fork.created",
                {"task_id": t, "from_seq": created.seq, "model": model, "role": role, "seed": seed, "fork_id": fid},
            )
            add(log, "outcome.scored", {"fork_id": fid, "solved": i in solves})
            tokens = {"input_tokens": 10_000} if model == HAIKU else {"input_tokens": 1000, "output_tokens": 100}
            usage(log, model, tokens, role="worker", task_id=t, fork_id=fid)
    return log


def test_escalation_value_positive(tmp_path):
    evs = fork_log(tmp_path / "f.jsonl", set(range(6)), set(range(18)), set(range(9))).events()
    (e,) = escalation_values(evs, seed=0, iters=ITERS)
    assert (e.treatment_model, e.control_model, e.n) == (SONNET, HAIKU, 30)
    assert (e.treatment_solved, e.control_solved, e.treatment_only, e.control_only) == (18, 6, 12, 0)
    assert close(e.ev.value, 0.4)
    assert 0 < e.ev.low < 0.4 < e.ev.high
    assert close(e.p_exact, 2 / 2**12)
    # Haiku fork $0.001, Sonnet fork $0.003: $0.002 extra per fork, 30 x 0.002 / 12 extra solves.
    assert close(e.extra_cost_per_fork.value, 0.002)
    assert close(e.usd_per_extra_solve.value, 0.005)
    # Controls with seeds 1 and 2 disagree on tasks 6, 7, 8.
    assert close(e.noise_floor.value, 0.1) and e.noise_floor.n == 30
    assert (e.noise_floor.low, e.noise_floor.high) == wilson_interval(3, 30)


def test_escalation_value_negative(tmp_path):
    evs = fork_log(tmp_path / "f.jsonl", set(range(15)), set(range(5)), set(range(15)), n=20).events()
    (e,) = escalation_values(evs, iters=ITERS)
    assert close(e.ev.value, -10 / 20)
    assert e.ev.high < 0
    assert e.usd_per_extra_solve.value is None
    assert e.noise_floor.value == 0.0


def test_fork_id_defaults_to_event_seq(tmp_path):
    log = EventLog(tmp_path / "f.jsonl")
    add(log, "task.created", {"task_id": "t", "title": "t", "scope": []})
    c = add(log, "fork.created", {"task_id": "t", "from_seq": 0, "model": HAIKU, "role": "control", "seed": 1})
    t = add(log, "fork.created", {"task_id": "t", "from_seq": 0, "model": SONNET, "role": "treatment", "seed": 1})
    add(log, "outcome.scored", {"fork_id": f"fork@{c.seq}", "solved": False})
    add(log, "outcome.scored", {"fork_id": f"fork@{t.seq}", "solved": True})
    (e,) = escalation_values(log.events(), iters=100)
    assert e.n == 1 and e.ev.value == 1.0 and e.noise_floor.n == 0


# ------------------------------------------------------------------ gates


def test_gate_block_counts_and_replay_rates(tmp_path):
    log = EventLog(tmp_path / "g.jsonl")

    def attempt(t):
        add(log, "task.claimed", {"task_id": t, "worker": "w"})
        add(log, "task.submitted", {"task_id": t})

    def gate(t, name, verdict, **evidence):
        p = {"task_id": t, "gate": name, "verdict": verdict, "reason": ""}
        if evidence:
            p["evidence"] = evidence
        return add(log, "gate.decided", p)

    for t in ("g0", "g1", "g2", "g3", "g4"):
        add(log, "task.created", {"task_id": t, "title": t, "scope": []})
    attempt("g0")
    b0 = gate("g0", "T0.scope", "block")
    add(log, "outcome.scored", {"gate_seq": b0.seq, "task_id": "g0", "solved": False})  # true block
    attempt("g0")
    gate("g0", "T1.private_tests", "pass")
    attempt("g1")
    gate("g1", "T0.scope", "block", replay_solved=True)  # false block
    attempt("g1")
    gate("g1", "T1.private_tests", "pass")
    attempt("g2")
    gate("g2", "T1.private_tests", "block")  # no replay
    attempt("g2")
    gate("g2", "T1.private_tests", "pass")
    attempt("g3")
    gate("g3", "T0.scope", "pass")
    attempt("g4")
    gate("g4", "visible", "pass", tier="T0")

    gates, tiers = gate_quality(log.events())
    g = {x.gate: x for x in gates}
    s = g["T0.scope"]
    assert (s.tier, s.n, s.passed, s.blocked, s.replayed, s.true_blocks, s.false_blocks) == ("T0", 3, 1, 2, 2, 1, 1)
    assert s.true_block_rate.value == 0.5 and s.true_block_rate.n == 2
    p = g["T1.private_tests"]
    assert (p.n, p.passed, p.blocked, p.replayed) == (4, 3, 1, 0)
    assert p.true_block_rate.value is None
    assert g["visible"].tier == "T0"
    tt = {x.gate: x for x in tiers}
    assert (tt["T0"].n, tt["T0"].blocked, tt["T1"].n) == (4, 2, 4)
    # analyze() agrees with the standalone function
    assert analyze(log.events(), iters=10).gates == gates


# ------------------------------------------------------------------ pricing and Pareto


def test_repricing_changes_costs_not_solve_rates(three):
    base = analyze(three, secondary=[("C", "A")], iters=ITERS)
    double = analyze(three, scale_table(T, 2.0), secondary=[("C", "A")], iters=ITERS)
    assert double.table_name == "custom" and double.table_fingerprint != base.table_fingerprint
    for arm in base.arms:
        b, d = base.arms[arm], double.arms[arm]
        assert b.solve_rate == d.solve_rate
        assert close(d.total_cost, 2 * b.total_cost)
        assert close(d.cost_per_task.value, 2 * b.cost_per_task.value)
        assert close(d.orchestrator_cost_share.value, b.orchestrator_cost_share.value)
    for cb, cd in zip(base.contrasts, double.contrasts):
        assert (cb.p_exact, cb.p_holm, cb.solve_diff) == (cd.p_exact, cd.p_holm, cd.solve_diff)


def test_pareto_frontier_moves_under_repricing(three):
    _, frontier = pareto(three)
    assert [p[0] for p in frontier] == ["B", "C", "A"]
    pricey_haiku = dict(T)
    pricey_haiku[HAIKU] = ModelPrice(10.0, 50.0)
    points, frontier = pareto(three, pricey_haiku)
    assert [p[0] for p in frontier] == ["A"]
    assert {p[0]: p[2] for p in points} == {"A": 0.6, "B": 0.3, "C": 0.5}


def test_named_table_is_recognised_from_a_mapping():
    assert resolve_table(dict(T))[0] == "anthropic-2026-11"
    assert resolve_table("anthropic-2026-11") == resolve_table(dict(T))


# ------------------------------------------------------------------ robustness and report


def test_determinism_under_seed(three):
    a = analyze(three, secondary=[("C", "A")], seed=7, iters=ITERS)
    b = analyze(three, secondary=[("C", "A")], seed=7, iters=ITERS)
    c = analyze(three, secondary=[("C", "A")], seed=8, iters=ITERS)
    assert a == b
    assert report_markdown(three, seed=7, iters=ITERS) == report_markdown(three, seed=7, iters=ITERS)
    assert a != c


def test_arm_interval_does_not_depend_on_other_arms(three):
    only_a = [e for e in three if e.payload.get("arm") == "A"]
    assert arm_summaries(only_a, iters=ITERS)["A"] == arm_summaries(three, iters=ITERS)["A"]


def test_missing_optional_tags_become_warnings(tmp_path):
    log = EventLog(tmp_path / "m.jsonl")
    outcome(log, "A", "t0", True)
    usage(log, OPUS, {"input_tokens": 1000}, arm="A", task_id="t0")
    usage(log, OPUS, {"input_tokens": 1000}, arm="A")  # no task_id
    usage(log, OPUS, {"input_tokens": 1000})  # no arm
    usage(log, HAIKU, {"input_tokens": 1000}, arm="E", task_id="t0")  # arm without outcomes
    add(log, "outcome.scored", {"fork_id": "nope", "solved": True})
    r = analyze(log.events(), iters=50)
    assert close(r.arms["A"].total_cost, 0.008) and close(r.arms["A"].unattributed_cost, 0.004)
    assert close(r.arms["A"].cost_per_task.value, 0.004)
    assert close(r.untagged_cost, 0.004)
    assert r.arms["E"].n == 0 and r.arms["E"].solve_rate.value is None
    text = " ".join(r.warnings)
    for needle in ("no task_id", "no arm or fork tag", "unknown fork", "arm E"):
        assert needle in text
    report_markdown(log.events(), iters=50)  # renders without error


def test_required_fields_and_unknown_models_raise(tmp_path):
    log = EventLog(tmp_path / "r.jsonl")
    add(log, "outcome.scored", {"arm": "A", "solved": True})
    with pytest.raises(ValueError, match="task_id"):
        analyze(log.events(), iters=10)
    log2 = EventLog(tmp_path / "r2.jsonl")
    usage(log2, "claude-unknown", {"input_tokens": 1}, arm="A")
    with pytest.raises(KeyError, match="claude-unknown"):
        analyze(log2.events(), iters=10)


def test_report_markdown_content(three, tmp_path):
    md = report_markdown(three, primary=[("A", "B"), ("C", "B")], seed=0, iters=ITERS)
    assert md.splitlines()[2].startswith("Price table: `anthropic-2026-11` (fingerprint `")
    assert "| A | 10 | 6/10 | 60.0% [31.3%, 83.2%] | $0.0140 [$0.0140, $0.0140] |" in md
    assert "## Paired contrasts: primary (Holm across m=2)" in md
    assert "| A vs B | 10 | 6/10 | 3/10 | 3 | 0 | 0.2500 | 0.5000 |" in md  # Holm: 0.25 x 2
    assert "## Paired contrasts: secondary (unadjusted)" in md  # API-arm defaults present
    assert "Frontier (cheapest first): B → C → A" in md
    assert "nan" not in md.lower()
    custom = report_markdown(three, scale_table(T, 0.5), iters=50)
    assert "Price table: `custom`" in custom
    fevs = fork_log(tmp_path / "f.jsonl", set(range(6)), set(range(18)), set(range(9))).events()
    fmd = report_markdown(fevs, iters=ITERS)
    assert f"| {SONNET} vs {HAIKU} | 30 | 18/30 | 6/30 | 12 | 0 |" in fmd
    assert "+40.0 pts" in fmd and "(n=30 pairs)" in fmd


def test_canonical_harness_shaped_events(tmp_path):
    """docs/events.md shapes: kernel ids on usage, pairing keys on outcomes, fork outcomes by fork_id."""
    log = EventLog(tmp_path / "h.jsonl")
    for arm in ("B", "D"):
        add(log, "task.created", {"task_id": f"{arm}:ex/a", "title": "ex/a", "scope": ["a.py"], "arm": arm, "exercise_id": "ex/a"})
    usage(log, HAIKU, {"input_tokens": 1000, "output_tokens": 100}, arm="B", role="worker", task_id="B:ex/a")
    outcome(log, "B", "ex/a", False, log_task_id="B:ex/a")
    usage(log, OPUS, {"input_tokens": 1000, "output_tokens": 100}, arm="D", role="orchestrator", task_id="D:ex/a")
    add(log, "task.claimed", {"task_id": "D:ex/a"})
    add(log, "task.submitted", {"task_id": "D:ex/a", "files": ["a.py"]})
    gate = add(log, "gate.decided", {"task_id": "D:ex/a", "gate": "T0.visible_tests", "verdict": "block", "reason": "x"})
    fids = []
    for i, (branch, role, model) in enumerate((("control-1", "control", HAIKU), ("control-2", "control", HAIKU),
                                               ("treatment", "treatment", SONNET))):
        fid = f"fork-{i:016d}"
        fids.append(fid)
        add(log, "fork.created", {"fork_id": fid, "task_id": "D:ex/a", "from_seq": gate.seq, "model": model, "role": role,
                                  "seed": 100 + i, "prefix_hash": gate.hash, "fork_task_id": f"D:ex/a:{branch}",
                                  "branch": branch, "arm": "D"})
        usage(log, model, {"input_tokens": 1000, "output_tokens": 100}, arm="D", role="worker",
              task_id=f"D:ex/a:{branch}", fork_id=fid)
        outcome(log, "D", "ex/a", role == "treatment" or branch == "control-2", fork_id=fid,
                log_task_id=f"D:ex/a:{branch}", gate_passed=True, branch=branch)
    outcome(log, "D", "ex/a", True, log_task_id="D:ex/a", branch_task_id="D:ex/a:treatment", shipped=True)
    add(log, "outcome.scored", {"gate_seq": gate.seq, "solved": True})

    r = analyze(log.events(), iters=200)
    assert set(r.arms) == {"B", "D"}
    d = r.arms["D"]
    assert (d.n, d.solved) == (1, 1)
    # D's cost is its own orchestrator plus the treatment branch it shipped; the controls are overhead.
    opus = T[OPUS].input * 1000 / 1e6 + T[OPUS].output * 100 / 1e6
    sonnet = T[SONNET].input * 1000 / 1e6 + T[SONNET].output * 100 / 1e6
    haiku = T[HAIKU].input * 1000 / 1e6 + T[HAIKU].output * 100 / 1e6
    assert close(d.total_cost, opus + sonnet, 1e-9) and close(d.shipped_fork_cost, sonnet, 1e-9)
    assert close(d.cost_per_solved.value, opus + sonnet, 1e-9)
    assert [(o.arm, o.role, o.forks) for o in r.measurement_overhead] == [("D", "control", 2)]
    assert close(r.measurement_overhead_cost, 2 * haiku, 1e-9)
    assert d.unattributed_cost == pytest.approx(0, abs=1e-12)  # kernel id D:ex/a mapped to pairing key ex/a
    assert r.arms["B"].unattributed_cost == pytest.approx(0, abs=1e-12)
    (e,) = r.escalation
    assert (e.treatment_model, e.control_model, e.n, e.treatment_solved, e.control_solved) == (SONNET, HAIKU, 1, 1, 0)
    assert r.noise_floors[HAIKU].n == 1 and r.noise_floors[HAIKU].value == 1.0  # the two controls disagree
    assert r.gates[0].replayed == 1 and r.gates[0].false_blocks == 1
    assert not [w for w in r.warnings if "unknown fork" in w or "no outcome" in w]
    md = report_markdown(log.events(), iters=200)
    assert f"| {SONNET} vs {HAIKU} | 1 |" in md


def shipped_branch_log(path, ship):
    """Two D tasks with forks. ex/a ships the branch ``ship``; ex/b passes its first gate (no fork).

    Token counts differ per request so every dollar figure below has one source.
    """
    log = EventLog(path)
    add(log, "task.created", {"task_id": "D:ex/a", "title": "ex/a", "scope": ["a.py"], "arm": "D", "exercise_id": "ex/a"})
    add(log, "task.created", {"task_id": "D:ex/b", "title": "ex/b", "scope": ["b.py"], "arm": "D", "exercise_id": "ex/b"})
    usage(log, OPUS, {"input_tokens": 2000, "output_tokens": 300}, arm="D", role="orchestrator", task_id="D:ex/a")
    usage(log, HAIKU, {"input_tokens": 5000, "output_tokens": 700}, arm="D", role="worker", task_id="D:ex/a")
    add(log, "task.claimed", {"task_id": "D:ex/a"})
    add(log, "task.submitted", {"task_id": "D:ex/a", "files": ["a.py"]})
    gate = add(log, "gate.decided", {"task_id": "D:ex/a", "gate": "T0.visible_tests", "verdict": "block", "reason": "x"})
    branches = (("control-1", "control", HAIKU, 4000, 600), ("control-2", "control", HAIKU, 4100, 650),
                ("treatment", "treatment", SONNET, 4200, 800))
    for i, (branch, role, model, tin, tout) in enumerate(branches):
        fid = f"fork-{branch}"
        add(log, "fork.created", {"fork_id": fid, "task_id": "D:ex/a", "from_seq": gate.seq, "model": model, "role": role,
                                  "seed": i, "fork_task_id": f"D:ex/a:{branch}", "branch": branch, "arm": "D"})
        usage(log, model, {"input_tokens": tin, "output_tokens": tout}, arm="D", role="worker",
              task_id=f"D:ex/a:{branch}", fork_id=fid)
        outcome(log, "D", "ex/a", role == "treatment", fork_id=fid, log_task_id=f"D:ex/a:{branch}")
    outcome(log, "D", "ex/a", ship == "treatment", log_task_id="D:ex/a", branch_task_id=f"D:ex/a:{ship}", branch=ship)
    usage(log, HAIKU, {"input_tokens": 3000, "output_tokens": 500}, arm="D", role="worker", task_id="D:ex/b")
    outcome(log, "D", "ex/b", True, log_task_id="D:ex/b", branch="initial")
    return log, {b[0]: (b[2], b[3], b[4]) for b in branches}


def _usd(model, tin, tout):
    return (T[model].input * tin + T[model].output * tout) / 1e6


@pytest.mark.parametrize("ship", ["treatment", "control-1"])
def test_arm_d_cost_includes_only_the_shipped_branch(tmp_path, ship):
    log, branches = shipped_branch_log(tmp_path / "s.jsonl", ship)
    evs = log.events()
    r = analyze(evs, iters=200)
    d = r.arms["D"]
    own_a = _usd(OPUS, 2000, 300) + _usd(HAIKU, 5000, 700)
    own_b = _usd(HAIKU, 3000, 500)
    shipped = _usd(*branches[ship])
    assert close(d.total_cost, own_a + own_b + shipped, 1e-9)
    assert close(d.shipped_fork_cost, shipped, 1e-9)
    assert d.unattributed_cost == pytest.approx(0, abs=1e-12)
    assert close(d.cost_per_task.value, (own_a + own_b + shipped) / 2, 1e-9)
    solved = 1 + (ship == "treatment")
    assert close(d.cost_per_solved.value, (own_a + own_b + shipped) / solved, 1e-9)
    assert d.requests == 4  # orchestrator, worker, shipped branch, ex/b worker

    # Everything not shipped is measurement overhead, by role.
    unshipped = {b: _usd(*v) for b, v in branches.items() if b != ship}
    by_role = {(o.arm, o.role): o for o in r.measurement_overhead}
    ctl = sum(v for b, v in unshipped.items() if b.startswith("control"))
    trt = sum(v for b, v in unshipped.items() if b == "treatment")
    assert close(by_role[("D", "control")].cost, ctl, 1e-9)
    assert by_role[("D", "control")].forks == (1 if ship.startswith("control") else 2)
    if ship == "treatment":
        assert ("D", "treatment") not in by_role
    else:
        assert close(by_role[("D", "treatment")].cost, trt, 1e-9) and by_role[("D", "treatment")].forks == 1
    assert close(r.measurement_overhead_cost, sum(unshipped.values()), 1e-9)

    # Escalation value still prices every fork.
    (e,) = r.escalation
    assert close(e.extra_cost_per_fork.value, _usd(*branches["treatment"]) - _usd(*branches["control-1"]), 1e-9)

    # Nothing is lost or double-counted: arm cost + overhead = all recorded spend, which the
    # budget guard counts in full.
    from contraprova.harness.arms import BudgetGuard

    total = sum(request_cost(u, T) for u in usage_records(evs))
    assert close(d.total_cost + r.measurement_overhead_cost, total, 1e-9)
    guard = BudgetGuard(None)
    assert close(guard.spent(log), total, 1e-9) and guard.spent(log) > d.total_cost

    md = report_markdown(evs, iters=200)
    assert "## Measurement overhead" in md and "| D | control |" in md
    assert "Arm cost includes the one fork branch" in md


def test_no_shipped_branch_means_all_fork_spend_is_overhead(tmp_path):
    """The demo log's shape: D retries in its own task, so its outcome names no branch_task_id."""
    log, branches = shipped_branch_log(tmp_path / "s.jsonl", "treatment")
    evs = [e for e in log.events()]
    # Drop branch_task_id by rebuilding the outcome payloads in a fresh log.
    log2 = EventLog(tmp_path / "s2.jsonl")
    for e in evs:
        p = {k: v for k, v in e.payload.items() if k != "branch_task_id"}
        log2.append(e.type, e.actor, p, ts=TS)
    r = analyze(log2.events(), iters=50)
    assert r.arms["D"].shipped_fork_cost == 0.0
    assert close(r.measurement_overhead_cost, sum(_usd(*v) for v in branches.values()), 1e-9)


def test_cli_cost_by_arm_splits_shipped_branch_from_measurement(tmp_path, capsys):
    from contraprova import cli
    from contraprova.projections import arm_usage_records

    log, branches = shipped_branch_log(tmp_path / "s.jsonl", "treatment")
    assert cli.main(["cost", str(log.path), "--by", "arm"]) == 0
    rows = dict(line.split("\t")[:2] for line in capsys.readouterr().out.strip().splitlines())
    d = _usd(OPUS, 2000, 300) + _usd(HAIKU, 5000, 700) + _usd(HAIKU, 3000, 500) + _usd(*branches["treatment"])
    overhead = _usd(*branches["control-1"]) + _usd(*branches["control-2"])
    assert set(rows) == {"D", "D:measurement", "total"}
    assert rows["D"] == f"${d:.6f}" and rows["D:measurement"] == f"${overhead:.6f}"
    total = sum(request_cost(u, T) for u in usage_records(log.events()))
    assert rows["total"] == f"${total:.6f}"
    # Other groupings are unchanged: generic cost_by over the raw records.
    assert cli.main(["cost", str(log.path), "--by", "role"]) == 0
    roles = dict(line.split("\t")[:2] for line in capsys.readouterr().out.strip().splitlines())
    assert set(roles) == {"orchestrator", "worker", "total"}
    assert all(":" not in str(u.get("arm")) for u in usage_records(log.events()))  # the log itself is untouched
    assert len(arm_usage_records(log.events())) == len(usage_records(log.events()))
