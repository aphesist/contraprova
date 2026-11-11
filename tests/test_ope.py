"""Off-policy estimators on synthetic data where the true policy value is known exactly."""

import math
import random
import statistics

import pytest

from contraprova.interfaces import HAIKU, OPUS, SONNET, TIERS
from contraprova.ope import (
    FullInfoStep,
    LoggedStep,
    audit,
    direct_method,
    dr,
    estimate_minus_truth,
    ips,
    snips,
    steps_from_examples,
    true_value,
)
from contraprova.router import Example, featurize

XS = [i / 9 for i in range(10)]  # contexts, uniform


def p_solve(x: float, a: str) -> float:
    return {HAIKU: 0.8 - 0.6 * x, SONNET: 0.5 + 0.3 * x, OPUS: 0.6 + 0.2 * x}[a]


def logging_policy(ctx):
    # Favours Haiku on easy contexts; every action keeps probability >= 0.1.
    x = ctx["x"]
    fav = HAIKU if x < 0.5 else SONNET
    return {a: 0.1 + (0.7 if a == fav else 0.0) for a in TIERS}


def target_policy(ctx):
    x = ctx["x"]
    return {a: 1.0 if a == (HAIKU if x < 0.3 else OPUS) else 0.0 for a in TIERS}


def stochastic_target(ctx):
    return {HAIKU: 0.2, SONNET: 0.5, OPUS: 0.3}


def exact_value(target) -> float:
    return statistics.fmean(sum(p * p_solve(x, a) for a, p in target({"x": x}).items()) for x in XS)


def simulate(n: int, seed: int) -> list[FullInfoStep]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        x = rng.choice(XS)
        ctx = {"x": x}
        mu = logging_policy(ctx)
        u, acc, a = rng.random(), 0.0, TIERS[-1]
        for b in TIERS:
            acc += mu[b]
            if u < acc:
                a = b
                break
        rewards = {b: 1.0 if rng.random() < p_solve(x, b) else 0.0 for b in TIERS}
        out.append(FullInfoStep(ctx, a, mu[a], rewards))
    return out


def bad_model(ctx, a):
    return 0.5  # deliberately wrong


def good_model(ctx, a):
    return p_solve(ctx["x"], a)


@pytest.mark.parametrize("target", [target_policy, stochastic_target])
def test_estimators_are_unbiased_over_seeded_simulation(target):
    truth = exact_value(target)
    runs = 300
    est = {"ips": [], "snips": [], "dr_bad": [], "dr_good": []}
    for seed in range(runs):
        logged = [s.logged() for s in simulate(200, seed)]
        est["ips"].append(ips(logged, target))
        est["snips"].append(snips(logged, target))
        est["dr_bad"].append(dr(logged, target, bad_model))
        est["dr_good"].append(dr(logged, target, good_model))
    for name, vals in est.items():
        mean = statistics.fmean(vals)
        se = statistics.stdev(vals) / math.sqrt(runs)
        assert abs(mean - truth) < max(4 * se, 0.005), (name, mean, truth, se)
    # A good reward model reduces variance.
    assert statistics.stdev(est["dr_good"]) < statistics.stdev(est["ips"])


def test_direct_method_with_wrong_model_is_biased_but_dr_is_not():
    truth = exact_value(target_policy)
    logged = [s.logged() for s in simulate(20000, 99)]
    assert abs(direct_method(logged, target_policy, bad_model) - truth) > 0.05
    assert abs(dr(logged, target_policy, bad_model) - truth) < 0.02


def test_ips_with_target_equal_to_logging_is_mean_reward():
    logged = [s.logged() for s in simulate(500, 3)]
    mean_r = statistics.fmean(s.reward for s in logged)
    assert ips(logged, logging_policy) == pytest.approx(mean_r)
    assert snips(logged, logging_policy) == pytest.approx(mean_r)


def test_audit_reports_estimate_minus_truth():
    steps = simulate(2000, 7)
    rep = audit(steps, target_policy, reward_model=good_model)
    assert rep.n == 2000
    assert rep.truth == pytest.approx(true_value(steps, target_policy))
    assert set(rep.estimates) == {"ips", "snips", "dr", "dm"}
    for k, v in rep.estimates.items():
        assert rep.errors[k] == pytest.approx(v - rep.truth)
        assert abs(rep.errors[k]) < 0.12  # IPS weights reach 10 here, so its SE is about 0.03
    assert estimate_minus_truth(rep.estimates["ips"], steps, target_policy) == pytest.approx(rep.errors["ips"])
    assert set(audit(steps, target_policy).estimates) == {"ips", "snips"}


def test_true_value_on_known_matrix():
    steps = [
        FullInfoStep({"x": 0.0}, HAIKU, 0.5, {HAIKU: 1.0, SONNET: 0.0, OPUS: 1.0}),
        FullInfoStep({"x": 1.0}, OPUS, 0.5, {HAIKU: 0.0, SONNET: 1.0, OPUS: 1.0}),
    ]
    assert true_value(steps, target_policy) == 1.0
    assert true_value(steps, stochastic_target) == pytest.approx((0.2 + 0.3 + 0.5 + 0.3) / 2)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        ips([], target_policy)
    with pytest.raises(ValueError):
        snips([LoggedStep({"x": 0.0}, HAIKU, 1.0, 0.0)], target_policy)
    assert snips([LoggedStep({"x": 0.9}, HAIKU, 1.0, 0.5)], target_policy) == 0.0


def test_steps_from_examples_drops_forks():
    f = featurize({})
    ex = [
        Example(f, HAIKU, True, 0.4, 0.02, "t1", 1, "route"),
        Example(f, SONNET, False, 1.0, 0.1, "t1", 2, "treatment"),
        Example(f, HAIKU, False, 1.0, 0.01, "t1", 3, "control"),
    ]
    steps = steps_from_examples(ex, cost_weight=10.0)
    assert len(steps) == 1
    assert steps[0].reward == pytest.approx(1.0 - 0.2) and steps[0].propensity == 0.4
