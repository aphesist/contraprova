"""Off-policy evaluation: the value of a target policy, estimated from logged decisions.

The log records, for each routing decision, the context (features), the action
taken, the probability the logging policy gave that action, and the reward.
From those alone these estimators predict how a *different* policy would have
done:

* ``ips``   inverse propensity scoring; unbiased when propensities are right and
            positive wherever the target policy puts mass.
* ``snips`` self-normalised IPS; slightly biased, much lower variance.
* ``dr``    doubly robust; unbiased if either the propensities or the reward
            model is right.

On the evaluation set every action is also run (forks), so the true value of a
policy is known. ``audit`` reports each estimate minus that truth, which is how
contraprova checks its own off-policy claims.

Policies are callables from a context to a distribution over actions, e.g.
``LinearRouter.action_probabilities``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

Context = Mapping[str, float]
Policy = Callable[[Context], Mapping[str, float]]
RewardModel = Callable[[Context, str], float]


@dataclass(frozen=True)
class LoggedStep:
    context: Context
    action: str
    reward: float
    propensity: float  # logging policy's probability of ``action`` in ``context``


@dataclass(frozen=True)
class FullInfoStep:
    """A logged step for which the reward of every action is known."""

    context: Context
    action: str  # the action the logging policy actually took
    propensity: float
    rewards: Mapping[str, float]  # action -> reward

    def logged(self) -> LoggedStep:
        return LoggedStep(self.context, self.action, float(self.rewards[self.action]), self.propensity)


def _check(steps: Sequence[LoggedStep]) -> None:
    if not steps:
        raise ValueError("no logged steps")
    for s in steps:
        if not s.propensity > 0.0:
            raise ValueError(f"propensity must be positive, got {s.propensity!r}")


def _weights(steps: Sequence[LoggedStep], target: Policy) -> list[float]:
    return [float(target(s.context).get(s.action, 0.0)) / s.propensity for s in steps]


def ips(steps: Sequence[LoggedStep], target: Policy) -> float:
    """Inverse propensity scoring: mean of (pi(a|x) / mu(a|x)) * r."""
    _check(steps)
    w = _weights(steps, target)
    return math.fsum(wi * s.reward for wi, s in zip(w, steps)) / len(steps)


def snips(steps: Sequence[LoggedStep], target: Policy) -> float:
    """Self-normalised IPS: sum(w r) / sum(w). Returns 0.0 if every weight is 0."""
    _check(steps)
    w = _weights(steps, target)
    total = math.fsum(w)
    if total == 0.0:
        return 0.0
    return math.fsum(wi * s.reward for wi, s in zip(w, steps)) / total


def dr(steps: Sequence[LoggedStep], target: Policy, reward_model: RewardModel) -> float:
    """Doubly robust: mean of sum_a pi(a|x) q(x,a) + w (r - q(x, a_logged))."""
    _check(steps)
    terms = []
    for s in steps:
        pi = target(s.context)
        direct = math.fsum(float(p) * reward_model(s.context, a) for a, p in pi.items())
        w = float(pi.get(s.action, 0.0)) / s.propensity
        terms.append(direct + w * (s.reward - reward_model(s.context, s.action)))
    return math.fsum(terms) / len(terms)


def direct_method(steps: Sequence[LoggedStep], target: Policy, reward_model: RewardModel) -> float:
    """The reward model alone: mean of sum_a pi(a|x) q(x,a)."""
    if not steps:
        raise ValueError("no logged steps")
    return math.fsum(
        math.fsum(float(p) * reward_model(s.context, a) for a, p in target(s.context).items()) for s in steps
    ) / len(steps)


def true_value(steps: Sequence[FullInfoStep], target: Policy) -> float:
    """The target policy's value on these contexts, from the full reward matrix."""
    if not steps:
        raise ValueError("no steps")
    return math.fsum(
        math.fsum(float(p) * float(s.rewards[a]) for a, p in target(s.context).items()) for s in steps
    ) / len(steps)


@dataclass(frozen=True)
class OPEAudit:
    truth: float
    estimates: Mapping[str, float]  # estimator name -> estimate
    errors: Mapping[str, float]  # estimator name -> estimate - truth
    n: int


def audit(steps: Sequence[FullInfoStep], target: Policy, reward_model: RewardModel | None = None) -> OPEAudit:
    """Estimate the target's value from the logged view only, and subtract the truth.

    Reports ``ips`` and ``snips``; ``dr`` and ``dm`` too when a reward model is given.
    """
    logged = [s.logged() for s in steps]
    truth = true_value(steps, target)
    est = {"ips": ips(logged, target), "snips": snips(logged, target)}
    if reward_model is not None:
        est["dr"] = dr(logged, target, reward_model)
        est["dm"] = direct_method(logged, target, reward_model)
    return OPEAudit(truth=truth, estimates=est, errors={k: v - truth for k, v in est.items()}, n=len(steps))


def estimate_minus_truth(estimate: float, steps: Sequence[FullInfoStep], target: Policy) -> float:
    """Any estimate of the target's value minus its full-information value on ``steps``."""
    return estimate - true_value(steps, target)


def steps_from_examples(examples: Iterable, *, cost_weight: float = 0.0) -> list[LoggedStep]:
    """Logged steps from ``contraprova.router.Example`` records.

    Only ``source == "route"`` examples are used: fork re-runs were not sampled
    by the logging policy and would bias importance weighting. Reward is
    ``solved - cost_weight * cost``.
    """
    return [
        LoggedStep(
            context=ex.features,
            action=ex.action,
            reward=(1.0 if ex.solved else 0.0) - cost_weight * ex.cost,
            propensity=ex.propensity,
        )
        for ex in examples
        if ex.source == "route"
    ]


__all__ = [
    "FullInfoStep",
    "LoggedStep",
    "OPEAudit",
    "audit",
    "direct_method",
    "dr",
    "estimate_minus_truth",
    "ips",
    "snips",
    "steps_from_examples",
    "true_value",
]
