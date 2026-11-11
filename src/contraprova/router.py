"""The router: a small model fitted from the log, and the policy built on it.

The router is a projection. ``training_examples`` reads the event log,
``train`` fits a per-action logistic regression with deterministic batch
gradient descent, and the resulting weights, hyperparameters and feature schema
hash to ``router_hash``. Retraining on the same log prefix with the same
hyperparameters reproduces the same hash, so a ``route.decided`` event can be
traced back to the exact log prefix that produced the router that made it.

Event payload fields consumed by ``training_examples``
------------------------------------------------------

``route.decided`` (one per routing decision)
    ``task_id`` (str), ``action`` (str, one of ``TIERS``), ``propensities``
    (object: action -> probability the policy gave it), ``router_hash`` (str),
    ``features`` (object: feature name -> number, as returned by
    ``LinearRouter.decide`` / ``featurize``). Optional: ``arm`` (str).

Payloads follow docs/events.md. Two task ids matter: the **kernel task id**
(``task_id`` on route, fork and usage events) and the **pairing key**
(``task_id`` on ``outcome.scored``, the benchmark exercise id). An outcome
names the kernel task it scores in ``log_task_id``; linking always uses it.

``fork.created`` (one per counterfactual re-run)
    ``fork_id`` (str, unique in the log), ``task_id`` (str: the parent kernel
    task), ``from_seq`` (int: the log position the fork re-runs from),
    ``model`` (str, one of ``TIERS``: the action the fork takes), ``role``
    ("control" | "treatment"), ``seed`` (int). Older logs without
    ``fork_id`` fall back to the decimal string of the fork event's own
    ``seq``. Optional: ``features`` (object; defaults to the features of the
    latest ``route.decided`` for the same task at or before ``from_seq``);
    other optional fields (``prefix_hash``, ``fork_task_id``, ``branch``,
    ``arm``) are ignored.

``outcome.scored``
    Arm outcome: ``arm`` (str), ``task_id`` (pairing key), ``solved`` (bool),
    optional ``log_task_id`` (the kernel task it scores; ``task_id`` is used
    when absent). Fork outcome: ``fork_id`` naming the fork it scores, plus
    ``solved``; it is linked by ``fork_id`` alone. Gate replays (``gate_seq``)
    are ignored.

``usage.recorded`` (only for the ``cost`` field)
    The token fields of ``contraprova.usage.usage_payload`` plus ``model``,
    tagged with the kernel ``task_id`` and either ``fork_id`` (fork usage,
    linked by ``fork_id`` alone, whatever its ``task_id``) or ``arm``
    (main-run usage). Arm matching for usage and outcomes is exact, except that
    a decision without ``arm`` accepts usage and scores from any arm.

Linking rules
-------------

* A routing decision's outcome is the first arm ``outcome.scored`` whose
  ``log_task_id`` (else ``task_id``) is the decision's ``task_id`` (and the
  same ``arm``, if the decision names one) after the decision and before the
  next decision for that task and arm. Its cost is the priced
  ``usage.recorded`` events for that task and arm, without ``fork_id``,
  between the decision and its outcome. Its propensity is
  ``propensities[action]``.
* A fork's outcome is the ``outcome.scored`` whose ``fork_id`` equals the fork
  id; its cost is the usage tagged with that ``fork_id``. Forks are run
  deliberately, not sampled, so their propensity is 1.0 and their ``source``
  is the fork role: they are training data for the reward model but must not
  enter importance-weighted estimators (see ``contraprova.ope``).
* Decisions or forks without a scored outcome inside the prefix are skipped.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .events import Event, canonical_json
from .interfaces import TIERS, RouteDecision
from .pricing import DEFAULT_TABLE, PRICE_TABLES, ModelPrice, request_cost

SCHEMA_VERSION = 1

# --------------------------------------------------------------- features

LANGUAGES: tuple[str, ...] = ("python", "javascript", "go", "rust", "java", "cpp")
# Error classes ordered roughly by how far the attempt got: none < assert < timeout < other < compile.
ERROR_CLASSES: tuple[str, ...] = ("none", "assert", "timeout", "other", "compile")

FEATURE_NAMES: tuple[str, ...] = (
    "language",
    "stub_size",
    "visible_fail_frac",
    "error_class",
    "diff_size",
    "tamper",
    "control_agree",
    "attempt",
)


def _log_scale(n: Any, top: float) -> float:
    """log1p(n) / log1p(top), clipped to [0, 1]."""
    x = max(0.0, float(n or 0))
    return min(1.0, math.log1p(x) / math.log1p(top))


def featurize(record: Mapping[str, Any]) -> dict[str, float]:
    """Build the router's feature dict (8 numbers, each in [0, 1]) from a plain dict.

    Expected keys (all optional; missing means the stated default):

    ``language`` (str)          one of ``LANGUAGES``; encoded as its index / 5,
                                unknown -> 1.0. Default unknown.
    ``stub_lines`` (int)        lines in the exercise stub; log-scaled to 1000.
    ``visible_tests`` (int)     number of visible tests.
    ``visible_failures`` (int)  visible tests that failed on the last attempt.
                                Both enter as one feature, the failure fraction
                                ``visible_failures / visible_tests`` (0 if no tests).
    ``error_class`` (str)       "none" | "assert" | "timeout" | "other" |
                                "compile"; encoded as index / 4, unknown ->
                                "other". Default "none".
    ``diff_lines`` (int)        lines changed by the last attempt; log-scaled to 1000.
    ``tamper`` (bool)           the last attempt touched a protected test file.
    ``control_agree`` (bool|None) whether a same-model control retry reached the
                                same verdict as the attempt; None (default) -> 0.5.
    ``attempt`` (int)           1-based attempt number; encoded as
                                min(1, (attempt - 1) / 2). Default 1.
    """
    lang = str(record.get("language", "")).lower()
    language = LANGUAGES.index(lang) / (len(LANGUAGES) - 1) if lang in LANGUAGES else 1.0

    n_tests = int(record.get("visible_tests", 0) or 0)
    n_fail = int(record.get("visible_failures", 0) or 0)
    fail_frac = min(1.0, max(0.0, n_fail / n_tests)) if n_tests > 0 else 0.0
    attempt = int(record.get("attempt", 1) or 1)

    err = str(record.get("error_class", "none") or "none").lower()
    if err not in ERROR_CLASSES:
        err = "other"
    error_class = ERROR_CLASSES.index(err) / (len(ERROR_CLASSES) - 1)

    agree = record.get("control_agree")
    control_agree = 0.5 if agree is None else (1.0 if agree else 0.0)

    return {
        "language": language,
        "stub_size": _log_scale(record.get("stub_lines", 0), 1000),
        "visible_fail_frac": fail_frac,
        "error_class": error_class,
        "diff_size": _log_scale(record.get("diff_lines", 0), 1000),
        "tamper": 1.0 if record.get("tamper") else 0.0,
        "control_agree": control_agree,
        "attempt": min(1.0, max(0.0, (attempt - 1) / 2)),
    }


def _vector(features: Mapping[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


# ------------------------------------------------------------ cost inputs


def expected_cost_from_prices(
    input_tokens: int = 20_000,
    output_tokens: int = 4_000,
    table: Mapping[str, ModelPrice] | None = None,
    actions: Sequence[str] = TIERS,
) -> dict[str, float]:
    """Expected USD per attempt for each action, from an assumed token budget."""
    table = PRICE_TABLES[DEFAULT_TABLE] if table is None else table
    return {
        a: request_cost({"model": a, "input_tokens": input_tokens, "output_tokens": output_tokens}, table)
        for a in actions
    }


DEFAULT_EXPECTED_COST: dict[str, float] = expected_cost_from_prices()

# ------------------------------------------------------------ the router


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


ROUND = 12  # decimals kept in stored weights, so the hash pins exactly what predicts


@dataclass(frozen=True)
class LinearRouter:
    """Per-action logistic models of P(solve | action, features), plus the policy.

    The policy maximises ``P(solve | a) - threshold * expected_cost[a]``: it moves
    to a dearer action only when the predicted gain in solve probability per
    extra dollar exceeds ``threshold``. With probability ``epsilon`` it instead
    picks an action uniformly, so every action has propensity >= epsilon / K.
    """

    weights: Mapping[str, tuple[float, ...]]  # action -> one weight per FEATURE_NAMES
    bias: Mapping[str, float]
    hyperparameters: Mapping[str, Any]  # seed, iterations, learning_rate, l2, init_scale, epsilon, threshold, expected_cost
    actions: tuple[str, ...] = TIERS
    feature_names: tuple[str, ...] = FEATURE_NAMES
    router_hash: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        eps = float(self.hyperparameters["epsilon"])
        if not 0.0 < eps <= 1.0:
            raise ValueError("epsilon must be in (0, 1] so every action keeps positive propensity")
        cost = self.hyperparameters["expected_cost"]
        missing = [a for a in self.actions if a not in cost]
        if missing:
            raise ValueError(f"expected_cost has no entry for {missing}")
        object.__setattr__(self, "router_hash", self._compute_hash())

    # -- identity

    def canonical(self) -> dict[str, Any]:
        return {
            "schema": {"version": SCHEMA_VERSION, "features": list(self.feature_names), "actions": list(self.actions)},
            "hyperparameters": dict(self.hyperparameters),
            "weights": {a: {"bias": self.bias[a], "w": list(self.weights[a])} for a in self.actions},
        }

    def _compute_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical()).encode("utf-8")).hexdigest()

    @property
    def epsilon(self) -> float:
        return float(self.hyperparameters["epsilon"])

    @property
    def threshold(self) -> float:
        return float(self.hyperparameters["threshold"])

    @property
    def expected_cost(self) -> Mapping[str, float]:
        return self.hyperparameters["expected_cost"]

    # -- model

    def predict(self, features: Mapping[str, float]) -> dict[str, float]:
        """P(solve | action, features) for each action."""
        x = _vector(features)
        return {
            a: _sigmoid(self.bias[a] + math.fsum(w * xi for w, xi in zip(self.weights[a], x))) for a in self.actions
        }

    def greedy_action(self, features: Mapping[str, float]) -> str:
        p = self.predict(features)
        cost = self.expected_cost
        best, best_u = self.actions[0], -math.inf
        # Actions are ordered cheapest first, and only a strictly better utility moves
        # up, so ties go to the cheaper action.
        for a in self.actions:
            u = p[a] - self.threshold * float(cost[a])
            if u > best_u:
                best, best_u = a, u
        return best

    def action_probabilities(self, features: Mapping[str, float]) -> dict[str, float]:
        """The policy's distribution over actions: ε-greedy around ``greedy_action``."""
        g = self.greedy_action(features)
        k = len(self.actions)
        base = self.epsilon / k
        return {a: base + (1.0 - self.epsilon if a == g else 0.0) for a in self.actions}

    def decide(self, features: Mapping[str, float], *, rng_seed: int) -> RouteDecision:
        probs = self.action_probabilities(features)
        u = random.Random(rng_seed).random()
        chosen = self.actions[-1]
        acc = 0.0
        for a in self.actions:
            acc += probs[a]
            if u < acc:
                chosen = a
                break
        feats = {name: float(features.get(name, 0.0)) for name in self.feature_names}
        return RouteDecision(action=chosen, propensities=probs, router_hash=self.router_hash, features=feats)


# ------------------------------------------------------------ training view


@dataclass(frozen=True)
class Example:
    """One (features, action, solved, propensity, cost) record extracted from the log."""

    features: Mapping[str, float]
    action: str
    solved: bool
    propensity: float
    cost: float
    task_id: str
    seq: int  # seq of the route.decided or fork.created event
    source: str  # "route", "control" or "treatment"
    router_hash: str | None = None


def _usage_cost(u: Mapping[str, Any], table: Mapping[str, ModelPrice] | None) -> float:
    if table is None:
        return 0.0
    return request_cost(u, table)


def training_examples(
    events: Iterable[Event],
    *,
    upto_seq: int | None = None,
    price_table: Mapping[str, ModelPrice] | None = None,
) -> list[Example]:
    """Extract training examples from the log (see the module docstring for fields).

    Only events with ``seq <= upto_seq`` are read (all events if None). Examples
    are returned in the order of their anchoring event's ``seq``. ``price_table``
    defaults to the default table in ``contraprova.pricing``.
    """
    table = PRICE_TABLES[DEFAULT_TABLE] if price_table is None else price_table
    evs = [e for e in events if upto_seq is None or e.seq <= upto_seq]
    evs.sort(key=lambda e: e.seq)

    # Open routing decisions keyed by (task_id, arm); value is a mutable record.
    open_routes: dict[tuple[Any, Any], dict[str, Any]] = {}
    route_features: dict[str, list[tuple[int, Mapping[str, float]]]] = {}
    forks: dict[str, dict[str, Any]] = {}
    done: list[tuple[int, Example]] = []

    def close(rec: dict[str, Any], solved: bool) -> None:
        p = rec["payload"]
        action = p["action"]
        done.append(
            (
                rec["seq"],
                Example(
                    features={k: float(v) for k, v in p["features"].items()},
                    action=action,
                    solved=solved,
                    propensity=float(p["propensities"][action]),
                    cost=rec["cost"],
                    task_id=p["task_id"],
                    seq=rec["seq"],
                    source="route",
                    router_hash=p.get("router_hash"),
                ),
            )
        )

    for ev in evs:
        p = ev.payload
        if ev.type == "route.decided":
            key = (p["task_id"], p.get("arm"))
            open_routes[key] = {"seq": ev.seq, "payload": p, "cost": 0.0}  # an unscored older one is dropped
            route_features.setdefault(p["task_id"], []).append((ev.seq, p["features"]))
        elif ev.type == "fork.created":
            fid = str(p["fork_id"]) if p.get("fork_id") is not None else str(ev.seq)
            feats = p.get("features")
            if feats is None:
                prior = [f for s, f in route_features.get(p["task_id"], []) if s <= int(p["from_seq"])]
                feats = prior[-1] if prior else None
            if feats is not None:
                forks[fid] = {"seq": ev.seq, "payload": p, "features": feats, "cost": 0.0}
        elif ev.type == "usage.recorded":
            tid = p.get("task_id")
            if p.get("fork_id") is not None:
                # Fork usage may name the child task (fork_task_id); the fork id alone links it.
                rec = forks.get(str(p["fork_id"]))
                if rec is not None:
                    rec["cost"] += _usage_cost(p, table)
            else:
                rec = open_routes.get((tid, p.get("arm"))) or open_routes.get((tid, None))
                if rec is not None:
                    rec["cost"] += _usage_cost(p, table)
        elif ev.type == "outcome.scored":
            if p.get("gate_seq") is not None and p.get("fork_id") is None:
                continue  # a gate replay scores a blocked patch, not a decision
            solved = bool(p["solved"])
            if p.get("fork_id") is not None:
                # The outcome's task_id is the pairing key, not the fork's parent kernel task.
                rec = forks.pop(str(p["fork_id"]), None)
                if rec is not None:
                    fp = rec["payload"]
                    done.append(
                        (
                            rec["seq"],
                            Example(
                                features={k: float(v) for k, v in rec["features"].items()},
                                action=fp["model"],
                                solved=solved,
                                propensity=1.0,
                                cost=rec["cost"],
                                task_id=fp["task_id"],
                                seq=rec["seq"],
                                source=str(fp.get("role", "treatment")),
                            ),
                        )
                    )
            else:
                arm = p.get("arm")
                kernel_tid = p.get("log_task_id") or p["task_id"]
                rec = open_routes.pop((kernel_tid, arm), None)
                if rec is None:
                    rec = open_routes.pop((kernel_tid, None), None)
                if rec is not None:
                    close(rec, solved)

    done.sort(key=lambda t: t[0])
    return [ex for _, ex in done]


# ------------------------------------------------------------ training

DEFAULT_HYPERPARAMETERS: dict[str, Any] = {
    "iterations": 500,
    "learning_rate": 0.5,
    "l2": 0.01,
    "init_scale": 0.01,
    "epsilon": 0.2,
    "threshold": 1.0,  # required gain in P(solve) per extra USD of expected cost
}


def train(
    examples: Sequence[Example],
    *,
    seed: int,
    iterations: int = DEFAULT_HYPERPARAMETERS["iterations"],
    learning_rate: float = DEFAULT_HYPERPARAMETERS["learning_rate"],
    l2: float = DEFAULT_HYPERPARAMETERS["l2"],
    init_scale: float = DEFAULT_HYPERPARAMETERS["init_scale"],
    epsilon: float = DEFAULT_HYPERPARAMETERS["epsilon"],
    threshold: float = DEFAULT_HYPERPARAMETERS["threshold"],
    expected_cost: Mapping[str, float] | None = None,
    actions: Sequence[str] = TIERS,
) -> LinearRouter:
    """Fit one L2-regularised logistic regression per action by full-batch gradient descent.

    Each action's model is trained only on examples that took that action. The
    initial weights are drawn from ``random.Random(seed)``; everything else is a
    fixed sequence of float operations, so the result (and its hash) depends only
    on the examples, in order, and the arguments. Actions with no examples keep
    their (near-zero) initial weights and predict about 0.5.
    """
    cost = dict(DEFAULT_EXPECTED_COST if expected_cost is None else expected_cost)
    actions = tuple(actions)
    rng = random.Random(seed)
    d = len(FEATURE_NAMES)
    weights: dict[str, tuple[float, ...]] = {}
    bias: dict[str, float] = {}
    for a in actions:
        w = [rng.uniform(-init_scale, init_scale) for _ in range(d)]
        b = 0.0
        data = [(_vector(ex.features), 1.0 if ex.solved else 0.0) for ex in examples if ex.action == a]
        n = len(data)
        if n:
            for _ in range(iterations):
                gw = [0.0] * d
                gb = 0.0
                for x, y in data:
                    err = _sigmoid(b + math.fsum(wi * xi for wi, xi in zip(w, x))) - y
                    gb += err
                    for j in range(d):
                        gw[j] += err * x[j]
                w = [w[j] - learning_rate * (gw[j] / n + l2 * w[j]) for j in range(d)]
                b -= learning_rate * gb / n
        weights[a] = tuple(round(v, ROUND) + 0.0 for v in w)
        bias[a] = round(b, ROUND) + 0.0
    hp = {
        "seed": seed,
        "iterations": iterations,
        "learning_rate": learning_rate,
        "l2": l2,
        "init_scale": init_scale,
        "epsilon": epsilon,
        "threshold": threshold,
        "expected_cost": {a: float(cost[a]) for a in actions},
    }
    return LinearRouter(weights=weights, bias=bias, hyperparameters=hp, actions=actions)


# ------------------------------------------------------------ router.trained


def trained_payload(router: LinearRouter, *, prefix_seq: int, n_examples: int) -> dict[str, Any]:
    """Payload for a ``router.trained`` event.

    Keys: ``prefix_seq`` (last seq of the log prefix trained on), ``n_examples``,
    ``router_hash`` (sha256 of weights + hyperparameters + feature schema),
    ``hyperparameters`` (everything ``train`` needs to reproduce the router),
    ``feature_names``, ``actions``, ``schema_version``.
    """
    return {
        "prefix_seq": prefix_seq,
        "n_examples": n_examples,
        "router_hash": router.router_hash,
        "hyperparameters": dict(router.hyperparameters),
        "feature_names": list(router.feature_names),
        "actions": list(router.actions),
        "schema_version": SCHEMA_VERSION,
    }


def retrain_from_log(
    events: Iterable[Event],
    payload: Mapping[str, Any],
    *,
    price_table: Mapping[str, ModelPrice] | None = None,
) -> LinearRouter:
    """Rebuild the router a ``router.trained`` payload describes, from the log alone.

    Raises ValueError if the rebuilt router's hash or example count differs from
    the payload: the projection did not replay.
    """
    if list(payload.get("feature_names", FEATURE_NAMES)) != list(FEATURE_NAMES):
        raise ValueError("router.trained payload uses a different feature schema")
    examples = training_examples(events, upto_seq=int(payload["prefix_seq"]), price_table=price_table)
    hp = dict(payload["hyperparameters"])
    seed = hp.pop("seed")
    router = train(examples, seed=seed, actions=tuple(payload.get("actions", TIERS)), **hp)
    if len(examples) != int(payload["n_examples"]):
        raise ValueError(f"replay found {len(examples)} examples, payload says {payload['n_examples']}")
    if router.router_hash != payload["router_hash"]:
        raise ValueError("replayed router hash does not match the router.trained payload")
    return router


__all__ = [
    "DEFAULT_EXPECTED_COST",
    "DEFAULT_HYPERPARAMETERS",
    "ERROR_CLASSES",
    "FEATURE_NAMES",
    "LANGUAGES",
    "Example",
    "LinearRouter",
    "expected_cost_from_prices",
    "featurize",
    "retrain_from_log",
    "train",
    "trained_payload",
    "training_examples",
]
