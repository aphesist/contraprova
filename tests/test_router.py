"""Tests of the router: features, policy, training view, and replayable training."""

import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from contraprova.interfaces import HAIKU, OPUS, SONNET, TIERS, RouteDecision, Router
from contraprova.log import EventLog
from contraprova.router import (
    DEFAULT_EXPECTED_COST,
    FEATURE_NAMES,
    Example,
    LinearRouter,
    featurize,
    retrain_from_log,
    train,
    trained_payload,
    training_examples,
)

TS = "2026-11-11T00:00:00.000Z"
COST = {HAIKU: 0.01, SONNET: 0.08, OPUS: 0.16}


def hand_router(p_haiku: float, p_sonnet: float, p_opus: float, *, threshold: float, epsilon: float = 0.2):
    def logit(p):
        return math.log(p / (1 - p))

    zeros = tuple(0.0 for _ in FEATURE_NAMES)
    return LinearRouter(
        weights={a: zeros for a in TIERS},
        bias={HAIKU: logit(p_haiku), SONNET: logit(p_sonnet), OPUS: logit(p_opus)},
        hyperparameters={"seed": 0, "epsilon": epsilon, "threshold": threshold, "expected_cost": COST},
    )


# ------------------------------------------------------------------ features


def test_featurize_has_at_most_eight_bounded_features():
    f = featurize(
        {
            "language": "rust",
            "stub_lines": 40,
            "visible_tests": 10,
            "visible_failures": 3,
            "error_class": "compile",
            "diff_lines": 25,
            "tamper": True,
            "control_agree": False,
            "attempt": 2,
        }
    )
    assert tuple(f) == FEATURE_NAMES and len(f) <= 8
    assert all(0.0 <= v <= 1.0 for v in f.values())
    assert f["visible_fail_frac"] == pytest.approx(0.3)
    assert f["error_class"] == 1.0 and f["tamper"] == 1.0 and f["control_agree"] == 0.0
    assert f["attempt"] == 0.5


def test_featurize_defaults():
    f = featurize({})
    assert f["visible_fail_frac"] == 0.0 and f["control_agree"] == 0.5 and f["attempt"] == 0.0
    assert f["error_class"] == 0.0 and f["language"] == 1.0
    assert featurize({"error_class": "segfault"})["error_class"] == featurize({"error_class": "other"})["error_class"]


@given(
    st.fixed_dictionaries(
        {},
        optional={
            "stub_lines": st.integers(-5, 10**6),
            "visible_tests": st.integers(0, 500),
            "visible_failures": st.integers(0, 600),
            "diff_lines": st.integers(0, 10**6),
            "attempt": st.integers(1, 20),
            "language": st.sampled_from(["python", "go", "cobol"]),
            "control_agree": st.sampled_from([True, False, None]),
        },
    )
)
def test_featurize_always_in_unit_interval(rec):
    assert all(0.0 <= v <= 1.0 for v in featurize(rec).values())


# ------------------------------------------------------------------ policy


def test_router_satisfies_protocol_and_propensities_sum_to_one():
    r: Router = hand_router(0.3, 0.8, 0.85, threshold=1.0)
    d = r.decide(featurize({}), rng_seed=7)
    assert isinstance(d, RouteDecision)
    assert d.action in TIERS and d.router_hash == r.router_hash
    assert math.fsum(d.propensities.values()) == pytest.approx(1.0, abs=1e-12)
    assert all(p >= 0.2 / 3 - 1e-12 for p in d.propensities.values())
    assert set(d.features) == set(FEATURE_NAMES)


def test_decide_is_deterministic_and_matches_propensities():
    r = hand_router(0.3, 0.8, 0.85, threshold=1.0)
    x = featurize({})
    assert r.decide(x, rng_seed=3) == r.decide(x, rng_seed=3)
    counts = {a: 0 for a in TIERS}
    n = 6000
    for s in range(n):
        counts[r.decide(x, rng_seed=s).action] += 1
    probs = r.action_probabilities(x)
    for a in TIERS:
        assert counts[a] / n == pytest.approx(probs[a], abs=0.02)


def test_escalates_only_when_gain_per_dollar_beats_threshold():
    # Sonnet adds 0.5 solve probability for 0.07 extra USD: about 7.1 per dollar.
    # Opus adds 0.05 over Sonnet for 0.08 extra: 0.625 per dollar.
    x = featurize({})
    assert hand_router(0.3, 0.8, 0.85, threshold=1.0).greedy_action(x) == SONNET
    assert hand_router(0.3, 0.8, 0.85, threshold=10.0).greedy_action(x) == HAIKU
    assert hand_router(0.3, 0.8, 0.85, threshold=0.1).greedy_action(x) == OPUS
    probs = hand_router(0.3, 0.8, 0.85, threshold=1.0).action_probabilities(x)
    assert probs[SONNET] == pytest.approx(0.8 + 0.2 / 3)


def test_epsilon_must_be_positive():
    with pytest.raises(ValueError):
        hand_router(0.3, 0.8, 0.85, threshold=1.0, epsilon=0.0)


def test_missing_cost_rejected():
    with pytest.raises(ValueError):
        LinearRouter(
            weights={a: (0.0,) * 8 for a in TIERS},
            bias={a: 0.0 for a in TIERS},
            hyperparameters={"epsilon": 0.2, "threshold": 1.0, "expected_cost": {HAIKU: 0.1}},
        )


# ------------------------------------------------------------------ training


def synthetic_examples(n: int, seed: int) -> list[Example]:
    """Haiku fails on compile errors; Sonnet and Opus mostly solve."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        err = rng.choice(["none", "assert", "compile"])
        x = featurize({"error_class": err, "language": rng.choice(["python", "go"]), "attempt": 2})
        a = rng.choice(TIERS)
        p = {HAIKU: 0.1 if err == "compile" else 0.7, SONNET: 0.85, OPUS: 0.9}[a]
        out.append(Example(x, a, rng.random() < p, 1 / 3, 0.0, f"t{i}", i, "route"))
    return out


def test_training_learns_the_signal():
    r = train(synthetic_examples(600, 1), seed=0, iterations=300)
    p_compile = r.predict(featurize({"error_class": "compile", "attempt": 2}))
    p_none = r.predict(featurize({"error_class": "none", "attempt": 2}))
    assert p_compile[HAIKU] < p_none[HAIKU] - 0.2
    assert p_compile[SONNET] > p_compile[HAIKU] + 0.3
    assert r.greedy_action(featurize({"error_class": "compile", "attempt": 2})) != HAIKU


def test_training_is_reproducible_and_seed_sensitive():
    ex = synthetic_examples(120, 2)
    a = train(ex, seed=5, iterations=50)
    b = train(list(ex), seed=5, iterations=50)
    assert a.router_hash == b.router_hash and a == b
    assert train(ex, seed=6, iterations=50).router_hash != a.router_hash
    assert train(ex, seed=5, iterations=50, l2=0.1).router_hash != a.router_hash
    assert len(a.router_hash) == 64


def test_untrained_action_predicts_about_half():
    ex = [e for e in synthetic_examples(60, 3) if e.action == HAIKU]
    r = train(ex, seed=0, iterations=20)
    assert r.predict(featurize({}))[OPUS] == pytest.approx(0.5, abs=0.01)


example_st = st.builds(
    lambda err, a, solved, i: Example(featurize({"error_class": err}), a, solved, 1 / 3, 0.0, f"t{i}", i, "route"),
    st.sampled_from(["none", "assert", "timeout", "compile"]),
    st.sampled_from(TIERS),
    st.booleans(),
    st.integers(0, 1000),
)


@settings(max_examples=30, deadline=None)
@given(st.lists(example_st, max_size=25), st.integers(0, 2**31))
def test_retraining_reproduces_hash(examples, seed):
    a = train(examples, seed=seed, iterations=20)
    b = train(examples, seed=seed, iterations=20)
    assert a.router_hash == b.router_hash
    assert a.decide(featurize({}), rng_seed=seed) == b.decide(featurize({}), rng_seed=seed)


# ------------------------------------------------------------------ training view from the log


def usage(model, task_id, out_tokens=1_000_000, **tags):
    return {"model": model, "input_tokens": 0, "output_tokens": out_tokens, "task_id": task_id, **tags}


def build_log(path):
    log = EventLog(path)
    r = hand_router(0.3, 0.8, 0.85, threshold=1.0)
    for t in ("t1", "t2", "t3"):
        log.append("task.created", "planner", {"task_id": t, "title": t, "scope": ["src/*"]}, ts=TS)

    def route(t, seed, arm="D"):
        d = r.decide(featurize({"error_class": "assert"}), rng_seed=seed)
        payload = {
            "task_id": t,
            "action": d.action,
            "propensities": dict(d.propensities),
            "router_hash": d.router_hash,
            "features": dict(d.features),
            "arm": arm,
        }
        return log.append("route.decided", "router", payload, ts=TS), d

    e1, d1 = route("t1", 1)
    log.append("usage.recorded", "worker", usage(d1.action, "t1", arm="D"), ts=TS)
    log.append("usage.recorded", "worker", usage(d1.action, "t1", arm="B"), ts=TS)  # other arm: ignored
    log.append("outcome.scored", "scorer", {"arm": "D", "task_id": "t1", "solved": True}, ts=TS)
    e2, d2 = route("t2", 2)
    log.append("outcome.scored", "scorer", {"arm": "D", "task_id": "t2", "solved": False}, ts=TS)
    fc = log.append(
        "fork.created",
        "harness",
        {"task_id": "t2", "from_seq": e2.seq, "model": HAIKU, "role": "control", "seed": 1},
        ts=TS,
    )
    ft = log.append(
        "fork.created",
        "harness",
        {"task_id": "t2", "from_seq": e2.seq, "model": SONNET, "role": "treatment", "seed": 1, "fork_id": "f-t2-s"},
        ts=TS,
    )
    log.append("usage.recorded", "worker", usage(SONNET, "t2", fork_id="f-t2-s"), ts=TS)
    log.append("outcome.scored", "scorer", {"arm": "D", "task_id": "t2", "solved": True, "fork_id": "f-t2-s"}, ts=TS)
    log.append("outcome.scored", "scorer", {"arm": "D", "task_id": "t2", "solved": False, "fork_id": fc.seq}, ts=TS)
    route("t3", 3)  # never scored
    return log, (e1, d1), (e2, d2), fc, ft


def test_training_examples_link_decisions_forks_and_cost(tmp_path):
    log, (e1, d1), (e2, _), fc, ft = build_log(tmp_path / "log.jsonl")
    ex = training_examples(log.events())
    assert [(e.seq, e.source) for e in ex] == [(e1.seq, "route"), (e2.seq, "route"), (fc.seq, "control"), (ft.seq, "treatment")]
    r1, r2, c, t = ex
    assert r1.action == d1.action and r1.solved and r1.propensity == d1.propensities[d1.action]
    assert r1.router_hash == d1.router_hash
    out_price = {HAIKU: 0.50, SONNET: 10.00, OPUS: 20.00}[d1.action]
    assert r1.cost == pytest.approx(out_price)
    assert not r2.solved and r2.cost == 0.0
    assert c.action == HAIKU and not c.solved and c.propensity == 1.0 and c.features == r2.features
    assert t.action == SONNET and t.solved and t.cost == pytest.approx(10.0)


def test_training_prefix_excludes_later_outcomes(tmp_path):
    log, (e1, _), (e2, _), *_ = build_log(tmp_path / "log.jsonl")
    ex = training_examples(log.events(), upto_seq=e2.seq)
    assert [e.seq for e in ex] == [e1.seq]


def test_router_trained_event_replays(tmp_path):
    log, *_ = build_log(tmp_path / "log.jsonl")
    evs = log.events()
    prefix = evs[-1].seq
    ex = training_examples(evs, upto_seq=prefix)
    router = train(ex, seed=11, iterations=40, expected_cost=COST)
    log.append("router.trained", "trainer", trained_payload(router, prefix_seq=prefix, n_examples=len(ex)), ts=TS)
    evs = log.events()
    payload = evs[-1].payload  # as stored: round-tripped through canonical JSON
    replayed = retrain_from_log(evs, payload)
    assert replayed.router_hash == router.router_hash == payload["router_hash"]

    bad = dict(payload, hyperparameters=dict(payload["hyperparameters"], l2=0.5))
    with pytest.raises(ValueError):
        retrain_from_log(evs, bad)
    with pytest.raises(ValueError):
        retrain_from_log(evs, dict(payload, n_examples=1))


def test_default_costs_are_ordered():
    assert DEFAULT_EXPECTED_COST[HAIKU] < DEFAULT_EXPECTED_COST[SONNET] < DEFAULT_EXPECTED_COST[OPUS]


# ------------------------------------------------------------------ a harness-produced log


def test_training_examples_on_a_harness_log(tmp_path):
    """Harness outcomes use the exercise id as task_id and the kernel id as log_task_id; forks link by fork_id."""
    from pathlib import Path

    from contraprova.harness.arms import BudgetGuard, Harness, ScoreResult, run_arm
    from contraprova.harness.polyglot import load_exercises, split_tests
    from contraprova.harness.run import FakeClient, UniformRouter
    from contraprova.interfaces import GateResult

    exercises = load_exercises(Path(__file__).parent / "fixtures" / "polyglot")
    blocked = {"python/clamp", "python/word-tally"}

    def gates(sub, ctx):
        first_try = ctx.task_id.count(":") == 1
        return [GateResult("T0.visible_tests", "block" if first_try and ctx.exercise.id in blocked else "pass", "2 failed")]

    def scorer(sub, ctx):
        return ScoreResult(ctx.model != HAIKU)  # only the treatment "solves"

    h = Harness(EventLog(tmp_path / "log.jsonl"), FakeClient(), scorer, gates, UniformRouter(), BudgetGuard(None))
    run_arm(h, "D", exercises, {e.id: split_tests(e, 7) for e in exercises}, "train")
    evs = h.log.events()
    ex = training_examples(evs)

    routes = [e for e in ex if e.source == "route"]
    forks = [e for e in ex if e.source != "route"]
    # One closed decision per exercise: the initial one when the gate passed, the post-failure one otherwise.
    assert sorted(e.task_id for e in routes) == sorted(f"D:{x.id}" for x in exercises)
    d_out = {e.payload["log_task_id"]: e.payload["solved"] for e in evs
             if e.type == "outcome.scored" and "fork_id" not in e.payload}
    assert all(e.solved == d_out[e.task_id] for e in routes)
    assert all(0 < e.propensity <= 1 for e in routes)

    # Three forks per blocked exercise, each linked to its fork outcome and fork usage.
    assert sorted(e.task_id for e in forks) == sorted(f"D:{x}" for x in blocked for _ in range(3))
    assert sorted(e.source for e in forks) == ["control"] * 4 + ["treatment"] * 2
    assert all(e.solved == (e.action != HAIKU) and e.propensity == 1.0 and e.cost > 0 for e in forks)
