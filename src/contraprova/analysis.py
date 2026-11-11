"""Analysis: from an event log to the study's numbers, each with an interval and its n.

Everything here is a pure function of the events and a price table. Dollars are
recomputed from recorded tokens (``contraprova.pricing``), so the whole report
can be re-priced without re-running anything. Bootstrap resampling is seeded
per statistic from ``seed`` and a stable label, so a result does not depend on
which other arms or contrasts happen to be in the log.

Payloads follow docs/events.md (see also docs/statistics.md, "Data the
analysis reads"). Unknown extra fields are ignored. Fields read:

``outcome.scored``
    Arm outcome: ``arm``, ``task_id`` (the pairing key), ``solved`` (all
    required); optional ``log_task_id`` (the kernel task it scores).
    Fork outcome: ``fork_id``, ``solved`` (required); ``arm`` (the parent
    arm, informational), ``task_id`` and ``log_task_id`` are ignored.
    Gate replay: ``gate_seq`` (seq of the ``gate.decided`` event replayed) and
    ``solved`` (did the blocked patch pass the score tests).
    A later score for the same key replaces an earlier one.
``usage.recorded``
    ``model`` and token counts (required, as written by ``usage_payload``);
    optional tags ``arm``, ``role``, ``task_id`` (a kernel task id), ``fork_id``.
    Usage with a ``fork_id`` is fork cost (escalation value uses all of it).
    Exactly one fork per task can also be arm cost: the branch the arm shipped
    (its arm outcome's ``branch_task_id`` is the fork's ``fork_task_id``, see
    ``projections.shipped_forks``); that usage counts toward the arm's totals
    and the shipped task's cost. Every other fork's usage is **measurement
    overhead**, reported per (parent arm, fork role) and never in an arm's
    cost. Arm usage is attributed to the pairing key of its kernel
    task: the ``exercise_id`` of its ``task.created`` or the ``task_id`` of an
    arm outcome whose ``log_task_id`` names it; otherwise the kernel id itself.
``fork.created``
    ``fork_id``, ``task_id`` (parent kernel task), ``from_seq``, ``model``,
    ``role`` ("control" | "treatment"), ``seed`` per docs/events.md;
    ``task_id``, ``model`` and ``role`` are enforced. For older logs
    ``fork_id`` defaults to ``"fork@<seq>"`` and ``from_seq``/``seed`` may be
    absent. ``fork_task_id`` identifies the shipped branch; ``arm`` (else the
    usage's ``arm``) names the parent arm of overhead. ``prefix_hash`` and
    ``branch`` are ignored.
``gate.decided``
    ``gate``, ``verdict`` required; ``evidence.tier`` and
    ``evidence.replay_solved`` optional.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, Iterable, Mapping, Sequence

from .events import Event, canonical_json
from .pricing import DEFAULT_TABLE, PRICE_TABLES, ModelPrice, prompt_tokens, request_cost
from .projections import pareto_frontier, shipped_forks
from .stats import discordant, holm, mcnemar_exact, paired_bootstrap_ci, wilson_interval

Contrast = tuple[str, str]

# Primary contrasts from docs/design.md; Holm adjustment runs across exactly this family.
DEFAULT_PRIMARY: tuple[Contrast, ...] = (
    ("La", "La-shadow"),  # shadow merge: governed vs ungoverned merge of the same lane outputs
    ("B", "L1"),
    ("La", "L1"),
    ("Lb", "La"),
    ("Lc", "D"),
)
# API-arm contrasts reported unadjusted when both arms are present.
DEFAULT_SECONDARY: tuple[Contrast, ...] = (("D", "A"), ("D", "B"), ("D", "C"), ("C", "A"))
DEFAULT_ITERS = 10_000
ALPHA = 0.05
ORCHESTRATOR_ROLE = "orchestrator"
ARM_ORDER = ("A", "B", "C", "D", "L1", "La", "La-shadow", "Lb", "Lc")

PriceTable = str | Mapping[str, ModelPrice]


# ------------------------------------------------------------------ result types


@dataclass(frozen=True)
class Estimate:
    """A point estimate with a 95% interval and the n it rests on.

    ``value`` is None when undefined (for example n == 0). ``high`` may be
    ``math.inf`` for a ratio whose denominator can be zero in a resample.
    """

    value: float | None
    low: float | None
    high: float | None
    n: int
    method: str  # "wilson" | "bootstrap" | "none"


def _none(n: int = 0) -> Estimate:
    return Estimate(None, None, None, n, "none")


@dataclass(frozen=True)
class ArmSummary:
    arm: str
    n: int  # tasks with an outcome for this arm
    solved: int
    solve_rate: Estimate
    cost_per_task: Estimate  # USD, over scored tasks
    cost_per_solved: Estimate  # USD, over scored tasks
    total_cost: float  # USD, the arm's own usage (no fork_id) plus the fork branches it shipped
    unattributed_cost: float  # part of total_cost not tied to a scored task
    requests: int
    orchestrator_token_share: Estimate
    orchestrator_cost_share: Estimate
    cache_read_share: Estimate  # cache-read tokens / prompt tokens
    shipped_fork_cost: float = 0.0  # part of total_cost spent in the fork branches the arm shipped


@dataclass(frozen=True)
class OverheadRow:
    """Fork spend an arm did not ship: it exists only to measure escalation value."""

    arm: str  # the parent arm of the forks
    role: str  # "control" | "treatment"
    forks: int  # forks with overhead usage
    requests: int
    cost: float  # USD


@dataclass(frozen=True)
class ContrastResult:
    a: str
    b: str
    n: int  # shared scored tasks
    a_solved: int
    b_solved: int
    a_only: int
    b_only: int
    p_exact: float | None
    p_holm: float | None  # None unless the contrast is in the primary family
    solve_diff: Estimate  # solve(a) - solve(b)
    cost_per_task_diff: Estimate  # USD, a - b
    cost_per_solved_diff: Estimate  # USD, a - b
    primary: bool


@dataclass(frozen=True)
class EscalationValue:
    treatment_model: str
    control_model: str
    n: int  # fork points with a scored treatment and a scored control
    treatment_solved: int
    control_solved: int
    treatment_only: int
    control_only: int
    p_exact: float | None
    ev: Estimate  # solve(treatment) - solve(control)
    extra_cost_per_fork: Estimate  # USD, cost(treatment) - cost(control)
    usd_per_extra_solve: Estimate
    noise_floor: Estimate  # control-vs-control divergence for control_model


@dataclass(frozen=True)
class GateSummary:
    gate: str  # gate name, or tier name for tier rows
    tier: str
    n: int
    passed: int
    blocked: int
    replayed: int  # blocks with a known replay outcome
    true_blocks: int  # replayed blocks that failed the score tests
    false_blocks: int  # replayed blocks that would have passed
    true_block_rate: Estimate
    false_block_rate: Estimate


@dataclass(frozen=True)
class Analysis:
    table_name: str
    table_fingerprint: str
    seed: int
    iters: int
    n_events: int
    head_hash: str | None
    arms: dict[str, ArmSummary]
    contrasts: list[ContrastResult]
    escalation: list[EscalationValue]
    noise_floors: dict[str, Estimate]
    gates: list[GateSummary]
    gate_tiers: list[GateSummary]
    pareto_points: list[tuple[str, float, float]]
    frontier: list[tuple[str, float, float]]
    untagged_cost: float
    measurement_overhead: list[OverheadRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def measurement_overhead_cost(self) -> float:
        return math.fsum(o.cost for o in self.measurement_overhead)


# ------------------------------------------------------------------ helpers


def resolve_table(table: PriceTable) -> tuple[str, dict[str, ModelPrice], str]:
    """(name, table, fingerprint). A mapping equal to a named table takes that name."""
    if isinstance(table, str):
        if table not in PRICE_TABLES:
            raise KeyError(f"unknown price table {table!r}")
        name, mapping = table, dict(PRICE_TABLES[table])
    else:
        mapping = dict(table)
        name = next((k for k, v in sorted(PRICE_TABLES.items()) if dict(v) == mapping), "custom")
    body = canonical_json({m: asdict(p) for m, p in sorted(mapping.items())})
    return name, mapping, hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _derive(seed: int, *labels: str) -> int:
    h = hashlib.sha256(canonical_json([seed, *labels]).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def _percentile(vals: list[float], alpha: float = ALPHA) -> tuple[float | None, float | None]:
    vals = sorted(v for v in vals if v == v)  # drop NaN
    m = len(vals)
    if m == 0:
        return None, None
    return vals[int((alpha / 2) * m)], vals[min(m - 1, int((1 - alpha / 2) * m))]


def _bootstrap(
    columns: Mapping[str, Sequence[float]],
    stats: Mapping[str, Callable[[Mapping[str, float]], float]],
    iters: int,
    seed: int,
) -> dict[str, tuple[float | None, float | None]]:
    """Percentile bootstrap over rows; every statistic sees the same resamples.

    Each statistic is a function of the column sums of a resample. NaN results
    are dropped; infinities are kept (they make an interval unbounded).
    """
    n = len(next(iter(columns.values())))
    rng = random.Random(seed)
    rows = range(n)
    draws: dict[str, list[float]] = {k: [] for k in stats}
    names = list(columns)
    getters = {c: list(columns[c]).__getitem__ for c in names}
    for _ in range(iters):
        idx = rng.choices(rows, k=n)
        sums = {c: sum(map(getters[c], idx)) for c in names}
        for k, fn in stats.items():
            draws[k].append(fn(sums))
    return {k: _percentile(v) for k, v in draws.items()}


def _ratio(num: float, den: float) -> float:
    if den == 0:
        return math.nan if num == 0 else math.inf
    return num / den


def _share(num: float, den: float) -> float:
    return math.nan if den == 0 else num / den


def _wilson(k: int, n: int) -> Estimate:
    if n == 0:
        return _none(0)
    lo, hi = wilson_interval(k, n)
    return Estimate(k / n, lo, hi, n, "wilson")


def _arm_key(arm: str) -> tuple[int, str]:
    return (ARM_ORDER.index(arm) if arm in ARM_ORDER else len(ARM_ORDER), arm)


# ------------------------------------------------------------------ parsing


@dataclass
class _Usage:
    cost: float = 0.0
    orch_cost: float = 0.0
    tokens: int = 0
    orch_tokens: int = 0
    cache_read: int = 0
    prompt: int = 0
    requests: int = 0

    def add(self, u: Mapping, usd: float) -> None:
        p = prompt_tokens(u)
        tok = p + int(u.get("output_tokens", 0))
        self.cost += usd
        self.tokens += tok
        self.cache_read += int(u.get("cache_read_tokens", 0))
        self.prompt += p
        self.requests += 1
        if u.get("role") == ORCHESTRATOR_ROLE:
            self.orch_cost += usd
            self.orch_tokens += tok


@dataclass(frozen=True)
class _Fork:
    fork_id: str
    seq: int
    task_id: str
    from_seq: int | None
    model: str
    role: str
    seed: object
    arm: str | None = None


@dataclass
class _Data:
    outcomes: dict[str, dict[str, bool]] = field(default_factory=dict)
    forks: dict[str, _Fork] = field(default_factory=dict)
    fork_solved: dict[str, bool] = field(default_factory=dict)
    fork_cost: dict[str, float] = field(default_factory=dict)
    task_usage: dict[str, dict[str, _Usage]] = field(default_factory=dict)
    arm_usage: dict[str, _Usage] = field(default_factory=dict)
    shipped_cost: dict[str, float] = field(default_factory=dict)  # arm -> usage of the fork branches it shipped
    overhead: dict[tuple[str, str], _Usage] = field(default_factory=dict)  # (parent arm, role) -> unshipped fork usage
    overhead_forks: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    untagged: _Usage = field(default_factory=_Usage)
    gates: list[Event] = field(default_factory=list)
    replays: dict[int, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _require(ev: Event, *keys: str) -> None:
    missing = [k for k in keys if k not in ev.payload]
    if missing:
        raise ValueError(f"event {ev.seq} ({ev.type}) lacks required field(s): {', '.join(missing)}")


def _parse(events: Sequence[Event], table: Mapping[str, ModelPrice]) -> _Data:
    d = _Data()
    pair_key: dict[str, str] = {}  # kernel task id -> pairing key
    for ev in events:
        p = ev.payload
        if ev.type == "task.created" and p.get("exercise_id") is not None:
            pair_key[str(p["task_id"])] = str(p["exercise_id"])
        elif (ev.type == "outcome.scored" and p.get("fork_id") is None and p.get("gate_seq") is None
              and p.get("log_task_id") is not None and p.get("task_id") is not None):
            pair_key.setdefault(str(p["log_task_id"]), str(p["task_id"]))
    for ev in events:
        if ev.type != "fork.created":
            continue
        _require(ev, "task_id", "model", "role")
        p = ev.payload
        if p["role"] not in ("control", "treatment"):
            d.warnings.append(f"fork at seq {ev.seq} has role {p['role']!r}; ignored")
            continue
        fid = str(p["fork_id"]) if p.get("fork_id") is not None else f"fork@{ev.seq}"
        if fid in d.forks:
            d.warnings.append(f"duplicate fork_id {fid!r} at seq {ev.seq}; the later one is used")
        fs = p.get("from_seq")
        d.forks[fid] = _Fork(fid, ev.seq, str(p["task_id"]), None if fs is None else int(fs), p["model"], p["role"],
                             p.get("seed"), None if p.get("arm") is None else str(p["arm"]))
    shipped = shipped_forks(events)

    unknown_fork_usage = 0
    for ev in events:
        p = ev.payload
        if ev.type == "outcome.scored":
            if p.get("gate_seq") is not None:
                _require(ev, "solved")
                d.replays[int(p["gate_seq"])] = bool(p["solved"])
            elif p.get("fork_id") is not None:
                _require(ev, "solved")
                fid = str(p["fork_id"])
                if fid not in d.forks:
                    d.warnings.append(f"outcome at seq {ev.seq} names unknown fork {fid!r}; ignored")
                else:
                    d.fork_solved[fid] = bool(p["solved"])
            else:
                _require(ev, "arm", "task_id", "solved")
                d.outcomes.setdefault(str(p["arm"]), {})[str(p["task_id"])] = bool(p["solved"])
        elif ev.type == "usage.recorded":
            _require(ev, "model")
            try:
                usd = request_cost(p, table)
            except KeyError as e:
                raise KeyError(f"event {ev.seq}: {e.args[0]}; add the model to the price table") from None
            if p.get("fork_id") is not None:
                fid = str(p["fork_id"])
                if fid in d.forks:
                    d.fork_cost[fid] = d.fork_cost.get(fid, 0.0) + usd
                    if fid in shipped:  # the branch the arm shipped: arm cost
                        arm, key = shipped[fid]
                        d.arm_usage.setdefault(arm, _Usage()).add(p, usd)
                        d.task_usage.setdefault(arm, {}).setdefault(key, _Usage()).add(p, usd)
                        d.shipped_cost[arm] = d.shipped_cost.get(arm, 0.0) + usd
                    else:  # measurement overhead
                        f = d.forks[fid]
                        okey = (f.arm or str(p.get("arm") or "?"), f.role)
                        d.overhead.setdefault(okey, _Usage()).add(p, usd)
                        d.overhead_forks.setdefault(okey, set()).add(fid)
                    continue
                unknown_fork_usage += 1
                d.untagged.add(p, usd)
            elif p.get("arm") is not None:
                arm = str(p["arm"])
                d.arm_usage.setdefault(arm, _Usage()).add(p, usd)
                if p.get("task_id") is not None:
                    key = pair_key.get(str(p["task_id"]), str(p["task_id"]))
                    d.task_usage.setdefault(arm, {}).setdefault(key, _Usage()).add(p, usd)
            else:
                d.untagged.add(p, usd)
        elif ev.type == "gate.decided":
            _require(ev, "gate", "verdict")
            d.gates.append(ev)
    if unknown_fork_usage:
        d.warnings.append(f"{unknown_fork_usage} usage record(s) name an unknown fork_id; counted as untagged cost")
    return d


# ------------------------------------------------------------------ per arm


def _arm_summary(arm: str, d: _Data, seed: int, iters: int) -> ArmSummary:
    outs = d.outcomes.get(arm, {})
    tasks = sorted(outs)
    n = len(tasks)
    solved = sum(outs[t] for t in tasks)
    per_task = d.task_usage.get(arm, {})
    rows = [per_task.get(t, _Usage()) for t in tasks]
    arm_total = d.arm_usage.get(arm, _Usage())
    attributed = sum(r.cost for r in rows)
    requests = arm_total.requests

    shipped = d.shipped_cost.get(arm, 0.0)
    if n == 0:
        return ArmSummary(arm, 0, 0, _none(), _none(), _none(), arm_total.cost, arm_total.cost, requests, _none(), _none(),
                          _none(), shipped)

    cols = {
        "one": [1.0] * n,
        "solved": [float(outs[t]) for t in tasks],
        "cost": [r.cost for r in rows],
        "orch_cost": [r.orch_cost for r in rows],
        "tokens": [float(r.tokens) for r in rows],
        "orch_tokens": [float(r.orch_tokens) for r in rows],
        "cache_read": [float(r.cache_read) for r in rows],
        "prompt": [float(r.prompt) for r in rows],
    }
    stats = {
        "cost_per_task": lambda s: s["cost"] / s["one"],
        "cost_per_solved": lambda s: _ratio(s["cost"], s["solved"]),
        "orch_tokens": lambda s: _share(s["orch_tokens"], s["tokens"]),
        "orch_cost": lambda s: _share(s["orch_cost"], s["cost"]),
        "cache": lambda s: _share(s["cache_read"], s["prompt"]),
    }
    totals = {c: sum(v) for c, v in cols.items()}
    ci = _bootstrap(cols, stats, iters, _derive(seed, "arm", arm))

    def est(key: str, defined: bool) -> Estimate:
        if not defined:
            return _none(n)
        v = stats[key](totals)
        return Estimate(v, *ci[key], n, "bootstrap")

    return ArmSummary(
        arm=arm,
        n=n,
        solved=solved,
        solve_rate=_wilson(solved, n),
        cost_per_task=est("cost_per_task", True),
        cost_per_solved=est("cost_per_solved", solved > 0),
        total_cost=arm_total.cost,
        unattributed_cost=arm_total.cost - attributed,
        requests=requests,
        orchestrator_token_share=est("orch_tokens", totals["tokens"] > 0),
        orchestrator_cost_share=est("orch_cost", totals["cost"] > 0),
        cache_read_share=est("cache", totals["prompt"] > 0),
        shipped_fork_cost=shipped,
    )


# ------------------------------------------------------------------ contrasts


def _contrast(a: str, b: str, d: _Data, seed: int, iters: int, primary: bool) -> ContrastResult:
    oa, ob = d.outcomes.get(a, {}), d.outcomes.get(b, {})
    shared = sorted(set(oa) & set(ob))
    n = len(shared)
    if n == 0:
        return ContrastResult(a, b, 0, 0, 0, 0, 0, None, None, _none(), _none(), _none(), primary)
    sa = [oa[t] for t in shared]
    sb = [ob[t] for t in shared]
    a_only, b_only = discordant(sa, sb)
    diffs = [float(x) - float(y) for x, y in zip(sa, sb)]
    mean, lo, hi = paired_bootstrap_ci(diffs, iters, ALPHA, seed=_derive(seed, "contrast", a, b))

    ua, ub = d.task_usage.get(a, {}), d.task_usage.get(b, {})
    cols = {
        "one": [1.0] * n,
        "sa": [float(x) for x in sa],
        "sb": [float(x) for x in sb],
        "ca": [ua[t].cost if t in ua else 0.0 for t in shared],
        "cb": [ub[t].cost if t in ub else 0.0 for t in shared],
    }
    stats = {
        "cpt": lambda s: (s["ca"] - s["cb"]) / s["one"],
        "cps": lambda s: _ratio(s["ca"], s["sa"]) - _ratio(s["cb"], s["sb"]),
    }
    tot = {c: sum(v) for c, v in cols.items()}
    ci = _bootstrap(cols, stats, iters, _derive(seed, "contrast-cost", a, b))
    cps = (
        Estimate(stats["cps"](tot), *ci["cps"], n, "bootstrap")
        if tot["sa"] > 0 and tot["sb"] > 0
        else _none(n)
    )
    return ContrastResult(
        a=a,
        b=b,
        n=n,
        a_solved=sum(sa),
        b_solved=sum(sb),
        a_only=a_only,
        b_only=b_only,
        p_exact=mcnemar_exact(a_only, b_only),
        p_holm=None,
        solve_diff=Estimate(mean, lo, hi, n, "bootstrap"),
        cost_per_task_diff=Estimate(stats["cpt"](tot), *ci["cpt"], n, "bootstrap"),
        cost_per_solved_diff=cps,
        primary=primary,
    )


def paired_contrasts(
    events: Iterable[Event],
    table: PriceTable = DEFAULT_TABLE,
    *,
    primary: Sequence[Contrast] | None = None,
    secondary: Sequence[Contrast] = (),
    seed: int = 0,
    iters: int = DEFAULT_ITERS,
) -> list[ContrastResult]:
    """Primary contrasts (Holm-adjusted as one family) followed by unadjusted secondary ones.

    A primary contrast with no shared tasks stays in the Holm family with p = 1,
    so missing data never shrinks the family and loosens the correction.
    """
    _, tbl, _ = resolve_table(table)
    return _contrasts(_parse(list(events), tbl), primary, secondary, seed, iters)


def _contrasts(d, primary, secondary, seed, iters) -> list[ContrastResult]:
    prim = list(DEFAULT_PRIMARY if primary is None else primary)
    out = [_contrast(a, b, d, seed, iters, True) for a, b in prim]
    if out:
        adj = holm([1.0 if c.p_exact is None else c.p_exact for c in out])
        out = [_with_holm(c, p) for c, p in zip(out, adj)]
    seen = set(prim)
    out += [_contrast(a, b, d, seed, iters, False) for a, b in secondary if (a, b) not in seen]
    return out


def _with_holm(c: ContrastResult, p: float) -> ContrastResult:
    return replace(c, p_holm=p)


# ------------------------------------------------------------------ escalation


def _groups(d: _Data) -> dict[tuple[str, int | None], list[_Fork]]:
    groups: dict[tuple[str, int | None], list[_Fork]] = {}
    for f in sorted(d.forks.values(), key=lambda f: f.seq):
        if f.fork_id in d.fork_solved:
            groups.setdefault((f.task_id, f.from_seq), []).append(f)
    return groups


def _noise_floors(d: _Data) -> dict[str, Estimate]:
    """Per control model: share of fork points where two controls with different seeds disagree."""
    pairs: dict[str, list[bool]] = {}
    for forks in _groups(d).values():
        by_model: dict[str, list[_Fork]] = {}
        for f in forks:
            if f.role == "control":
                by_model.setdefault(f.model, []).append(f)
        for model, cs in by_model.items():
            first = cs[0]
            second = next((c for c in cs[1:] if c.seed != first.seed), None)
            if second is not None:
                pairs.setdefault(model, []).append(d.fork_solved[first.fork_id] != d.fork_solved[second.fork_id])
    return {m: _wilson(sum(v), len(v)) for m, v in sorted(pairs.items())}


def _escalation(d: _Data, seed: int, iters: int) -> tuple[list[EscalationValue], dict[str, Estimate]]:
    floors = _noise_floors(d)
    pairs: dict[tuple[str, str], list[tuple[bool, bool, float]]] = {}
    for forks in _groups(d).values():
        controls = [f for f in forks if f.role == "control"]
        if not controls:
            continue
        c = controls[0]  # earliest scored control is the primary control
        seen_t: set[str] = set()
        for t in forks:
            if t.role != "treatment" or t.model in seen_t:
                continue
            seen_t.add(t.model)
            dc = d.fork_cost.get(t.fork_id, 0.0) - d.fork_cost.get(c.fork_id, 0.0)
            pairs.setdefault((t.model, c.model), []).append((d.fork_solved[t.fork_id], d.fork_solved[c.fork_id], dc))

    out = []
    for (tm, cm), rows in sorted(pairs.items()):
        n = len(rows)
        ts = [r[0] for r in rows]
        cs = [r[1] for r in rows]
        t_only, c_only = discordant(ts, cs)
        diffs = [float(x) - float(y) for x, y in zip(ts, cs)]
        mean, lo, hi = paired_bootstrap_ci(diffs, iters, ALPHA, seed=_derive(seed, "ev", tm, cm))
        cols = {"one": [1.0] * n, "dsolve": diffs, "dcost": [r[2] for r in rows]}
        stats = {
            "extra": lambda s: s["dcost"] / s["one"],
            "per_solve": lambda s: s["dcost"] / s["dsolve"] if s["dsolve"] > 0 else math.inf,
        }
        tot = {k: sum(v) for k, v in cols.items()}
        ci = _bootstrap(cols, stats, iters, _derive(seed, "ev-cost", tm, cm))
        per_solve = (
            Estimate(tot["dcost"] / tot["dsolve"], *ci["per_solve"], n, "bootstrap") if tot["dsolve"] > 0 else _none(n)
        )
        out.append(
            EscalationValue(
                treatment_model=tm,
                control_model=cm,
                n=n,
                treatment_solved=sum(ts),
                control_solved=sum(cs),
                treatment_only=t_only,
                control_only=c_only,
                p_exact=mcnemar_exact(t_only, c_only),
                ev=Estimate(mean, lo, hi, n, "bootstrap"),
                extra_cost_per_fork=Estimate(stats["extra"](tot), *ci["extra"], n, "bootstrap"),
                usd_per_extra_solve=per_solve,
                noise_floor=floors.get(cm, _none()),
            )
        )
    return out, floors


def escalation_values(
    events: Iterable[Event], table: PriceTable = DEFAULT_TABLE, *, seed: int = 0, iters: int = DEFAULT_ITERS
) -> list[EscalationValue]:
    """EV per (treatment model, control model) over fork points; see docs/statistics.md."""
    _, tbl, _ = resolve_table(table)
    return _escalation(_parse(list(events), tbl), seed, iters)[0]


# ------------------------------------------------------------------ gates


def _tier(ev: Event) -> str:
    evidence = ev.payload.get("evidence") or {}
    if isinstance(evidence, Mapping) and "tier" in evidence:
        return str(evidence["tier"])
    gate = str(ev.payload["gate"])
    return gate.split(".", 1)[0]


def _replay(ev: Event, d: _Data) -> bool | None:
    if ev.seq in d.replays:
        return d.replays[ev.seq]
    evidence = ev.payload.get("evidence") or {}
    if isinstance(evidence, Mapping) and "replay_solved" in evidence:
        return bool(evidence["replay_solved"])
    return None


def _gate_rows(d: _Data) -> tuple[list[GateSummary], list[GateSummary]]:
    gate_seqs = {ev.seq: ev for ev in d.gates}
    for s in d.replays:
        if s not in gate_seqs:
            d.warnings.append(f"gate replay names seq {s}, which is not a gate.decided event; ignored")
        elif gate_seqs[s].payload["verdict"] != "block":
            d.warnings.append(f"gate replay names seq {s}, which was not a block; ignored")

    def summarise(name: str, tier: str, evs: list[Event]) -> GateSummary:
        blocks = [e for e in evs if e.payload["verdict"] == "block"]
        replays = [r for r in (_replay(e, d) for e in blocks) if r is not None]
        false_b = sum(replays)
        true_b = len(replays) - false_b
        return GateSummary(
            gate=name,
            tier=tier,
            n=len(evs),
            passed=sum(1 for e in evs if e.payload["verdict"] == "pass"),
            blocked=len(blocks),
            replayed=len(replays),
            true_blocks=true_b,
            false_blocks=false_b,
            true_block_rate=_wilson(true_b, len(replays)),
            false_block_rate=_wilson(false_b, len(replays)),
        )

    by_gate: dict[str, list[Event]] = {}
    by_tier: dict[str, list[Event]] = {}
    for ev in d.gates:
        by_gate.setdefault(str(ev.payload["gate"]), []).append(ev)
        by_tier.setdefault(_tier(ev), []).append(ev)
    gates = [summarise(g, _tier(evs[0]), evs) for g, evs in sorted(by_gate.items())]
    tiers = [summarise(t, t, evs) for t, evs in sorted(by_tier.items())]
    return gates, tiers


def gate_quality(events: Iterable[Event]) -> tuple[list[GateSummary], list[GateSummary]]:
    """(per gate, per tier) block counts and, where replays exist, true/false-block rates."""
    d = _Data()
    for ev in events:
        if ev.type == "gate.decided":
            _require(ev, "gate", "verdict")
            d.gates.append(ev)
        elif ev.type == "outcome.scored" and ev.payload.get("gate_seq") is not None:
            _require(ev, "solved")
            d.replays[int(ev.payload["gate_seq"])] = bool(ev.payload["solved"])
    return _gate_rows(d)


# ------------------------------------------------------------------ top level


def analyze(
    events: Iterable[Event],
    table: PriceTable = DEFAULT_TABLE,
    *,
    primary: Sequence[Contrast] | None = None,
    secondary: Sequence[Contrast] | None = None,
    seed: int = 0,
    iters: int = DEFAULT_ITERS,
) -> Analysis:
    """Every number in the report. ``secondary=None`` means the API-arm defaults present in the log."""
    events = list(events)
    name, tbl, fp = resolve_table(table)
    d = _parse(events, tbl)
    arm_names = sorted(set(d.outcomes) | set(d.arm_usage), key=_arm_key)
    arms = {a: _arm_summary(a, d, seed, iters) for a in arm_names}
    if secondary is None:
        secondary = [(a, b) for a, b in DEFAULT_SECONDARY if a in d.outcomes and b in d.outcomes]
    contrasts = _contrasts(d, primary, secondary, seed, iters)
    escalation, floors = _escalation(d, seed, iters)
    gates, tiers = _gate_rows(d)

    for a in arm_names:
        s = arms[a]
        if s.n == 0:
            d.warnings.append(f"arm {a} has recorded usage (${s.total_cost:.6f}) but no scored outcome")
        elif s.unattributed_cost > 1e-12:
            d.warnings.append(
                f"arm {a}: ${s.unattributed_cost:.6f} of usage has no task_id or names an unscored task; "
                "it is in the arm total but not in per-task statistics"
            )
    if d.untagged.requests:
        d.warnings.append(f"{d.untagged.requests} usage record(s) (${d.untagged.cost:.6f}) carry no arm or fork tag")
    unscored = sorted(set(d.forks) - set(d.fork_solved))
    if unscored:
        d.warnings.append(f"{len(unscored)} fork(s) have no outcome and are left out of escalation value")

    points = [(a, s.cost_per_task.value, s.solve_rate.value) for a, s in arms.items() if s.n > 0]
    overhead = [
        OverheadRow(arm, role, len(d.overhead_forks[(arm, role)]), u.requests, u.cost)
        for (arm, role), u in sorted(d.overhead.items(), key=lambda kv: (_arm_key(kv[0][0]), kv[0][1]))
    ]
    return Analysis(
        table_name=name,
        table_fingerprint=fp,
        seed=seed,
        iters=iters,
        n_events=len(events),
        head_hash=events[-1].hash if events else None,
        arms=arms,
        contrasts=contrasts,
        escalation=escalation,
        noise_floors=floors,
        gates=gates,
        gate_tiers=tiers,
        pareto_points=points,
        frontier=pareto_frontier(points),
        untagged_cost=d.untagged.cost,
        measurement_overhead=overhead,
        warnings=d.warnings,
    )


def arm_summaries(
    events: Iterable[Event], table: PriceTable = DEFAULT_TABLE, *, seed: int = 0, iters: int = DEFAULT_ITERS
) -> dict[str, ArmSummary]:
    _, tbl, _ = resolve_table(table)
    d = _parse(list(events), tbl)
    return {a: _arm_summary(a, d, seed, iters) for a in sorted(set(d.outcomes) | set(d.arm_usage), key=_arm_key)}


def pareto(events: Iterable[Event], table: PriceTable = DEFAULT_TABLE) -> tuple[list[tuple[str, float, float]], list[tuple[str, float, float]]]:
    """(points, frontier) of (arm, cost per task, solve rate) under ``table``."""
    s = arm_summaries(events, table, iters=1)
    points = [(a, v.cost_per_task.value, v.solve_rate.value) for a, v in s.items() if v.n > 0]
    return points, pareto_frontier(points)


# ------------------------------------------------------------------ Markdown


def _inf(x: float) -> str:
    # An interval end is infinite when some resamples have a zero denominator (no solves).
    return "∞" if x > 0 else "−∞"


def _pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    if math.isinf(x):
        return _inf(x)
    return f"{100 * x:.1f}%"


def _pts(x: float | None) -> str:
    if x is None:
        return "n/a"
    if math.isinf(x):
        return _inf(x)
    return f"{100 * x:+.1f} pts"


def _usd(x: float | None) -> str:
    if x is None:
        return "n/a"
    if math.isinf(x):
        return _inf(x)
    sign = "-" if x < 0 else ""
    a = abs(x)
    return f"{sign}${a:.4f}" if a >= 0.001 or a == 0 else f"{sign}${a:.6f}"


def _ci(e: Estimate, fmt: Callable[[float | None], str]) -> str:
    if e.value is None:
        return f"n/a (n={e.n})"
    return f"{fmt(e.value)} [{fmt(e.low)}, {fmt(e.high)}]"


def _p(p: float | None) -> str:
    if p is None:
        return "n/a"
    return f"{p:.4f}" if p >= 0.0001 else f"{p:.1e}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def report_markdown(
    events: Iterable[Event],
    table: PriceTable = DEFAULT_TABLE,
    *,
    primary: Sequence[Contrast] | None = None,
    secondary: Sequence[Contrast] | None = None,
    seed: int = 0,
    iters: int = DEFAULT_ITERS,
) -> str:
    r = analyze(events, table, primary=primary, secondary=secondary, seed=seed, iters=iters)
    L: list[str] = ["# contraprova analysis", ""]
    L.append(
        f"Price table: `{r.table_name}` (fingerprint `{r.table_fingerprint}`); dollars recomputed from recorded tokens."
    )
    head = f", head `{r.head_hash[:12]}`" if r.head_hash else ""
    L.append(
        f"Log: {r.n_events} events{head}. Intervals are 95%: Wilson for proportions, percentile bootstrap "
        f"({r.iters} resamples, seed {r.seed}) for costs, shares and paired differences."
    )

    L += ["", "## Arms: solve rate and cost", ""]
    rows = []
    for s in r.arms.values():
        rows.append(
            [
                s.arm,
                str(s.n),
                f"{s.solved}/{s.n}",
                _ci(s.solve_rate, _pct),
                _ci(s.cost_per_task, _usd),
                _ci(s.cost_per_solved, _usd),
                _usd(s.total_cost),
            ]
        )
    L += _table(
        ["Arm", "n tasks", "Solved", "Solve rate [95% CI]", "Cost per task [95% CI]", "Cost per solved task [95% CI]", "Total recorded cost"],
        rows,
    ) if rows else ["No arm outcomes or usage in the log."]
    shipped_arms = [s for s in r.arms.values() if s.shipped_fork_cost > 0]
    if shipped_arms:
        L += ["", "Arm cost includes the one fork branch each task shipped: "
              + ", ".join(f"{s.arm} {_usd(s.shipped_fork_cost)}" for s in shipped_arms) + "."]

    if r.measurement_overhead:
        L += ["", "## Measurement overhead", ""]
        L += _table(
            ["Parent arm", "Fork role", "Forks", "Requests", "Cost"],
            [[o.arm, o.role, str(o.forks), str(o.requests), _usd(o.cost)] for o in r.measurement_overhead],
        )
        L += ["", f"Measurement overhead total: {_usd(r.measurement_overhead_cost)}. This is fork spend no arm shipped "
              "(controls and unshipped treatments): it exists only to measure escalation value, would not be spent "
              "in production, and is in no arm's cost. Escalation value still prices every fork."]

    L += ["", "## Arms: token mix", ""]
    rows = [
        [
            s.arm,
            str(s.n),
            str(s.requests),
            _ci(s.orchestrator_token_share, _pct),
            _ci(s.orchestrator_cost_share, _pct),
            _ci(s.cache_read_share, _pct),
        ]
        for s in r.arms.values()
    ]
    if rows:
        L += _table(
            ["Arm", "n tasks", "Requests", "Orchestrator token share [95% CI]", "Orchestrator $ share [95% CI]", "Cache-read share of prompt [95% CI]"],
            rows,
        )
    else:
        L.append("No usage recorded.")

    def contrast_rows(cs: list[ContrastResult]) -> list[list[str]]:
        return [
            [
                f"{c.a} vs {c.b}",
                str(c.n),
                f"{c.a_solved}/{c.n}",
                f"{c.b_solved}/{c.n}",
                str(c.a_only),
                str(c.b_only),
                _p(c.p_exact),
                _p(c.p_holm) if c.primary else "-",
                _ci(c.solve_diff, _pts),
                _ci(c.cost_per_solved_diff, _usd),
            ]
            for c in cs
        ]

    head_c = ["Contrast (a vs b)", "n shared", "a solved", "b solved", "a only", "b only", "Exact McNemar p", "Holm p", "Δ solve rate a−b [95% CI]", "Δ cost per solved a−b [95% CI]"]
    prim = [c for c in r.contrasts if c.primary]
    sec = [c for c in r.contrasts if not c.primary]
    L += ["", f"## Paired contrasts: primary (Holm across m={len(prim)})", ""]
    if prim:
        L += _table(head_c, contrast_rows(prim))
        if any(c.n == 0 for c in prim):
            L += ["", "Contrasts with no shared tasks stay in the Holm family with p = 1."]
    else:
        L.append("No primary contrasts supplied.")
    if sec:
        L += ["", "## Paired contrasts: secondary (unadjusted)", ""]
        L += _table(head_c, contrast_rows(sec))

    L += ["", "## Escalation value", ""]
    if r.escalation:
        rows = [
            [
                f"{e.treatment_model} vs {e.control_model}",
                str(e.n),
                f"{e.treatment_solved}/{e.n}",
                f"{e.control_solved}/{e.n}",
                str(e.treatment_only),
                str(e.control_only),
                _p(e.p_exact),
                _ci(e.ev, _pts),
                f"{_ci(e.noise_floor, _pct)} (n={e.noise_floor.n} pairs)",
                _ci(e.extra_cost_per_fork, _usd),
                _ci(e.usd_per_extra_solve, _usd),
            ]
            for e in r.escalation
        ]
        L += _table(
            ["Treatment vs control", "n fork points", "Treatment solved", "Control solved", "Treatment only", "Control only", "Exact McNemar p", "EV [95% CI]", "Noise floor: control-vs-control divergence [95% CI]", "Extra cost per fork [95% CI]", "$ per extra solve [95% CI]"],
            rows,
        )
        L += ["", "EV = solve(treatment) − solve(same-model control) at the same fork point. "
              "The noise floor is how often two controls with different seeds disagree."]
    else:
        L.append("No fork point has both a scored control and a scored treatment.")

    L += ["", "## Gate quality", ""]
    if r.gates:
        head_g = ["Gate", "Tier", "n decisions", "Passed", "Blocked", "Blocks replayed", "True-block rate [95% CI]", "False-block rate [95% CI]"]

        def grow(g: GateSummary) -> list[str]:
            return [g.gate, g.tier, str(g.n), str(g.passed), str(g.blocked), str(g.replayed),
                    f"{_ci(g.true_block_rate, _pct)} (n={g.replayed})", f"{_ci(g.false_block_rate, _pct)} (n={g.replayed})"]

        L += _table(head_g, [grow(g) for g in r.gates])
        L += ["", "By tier:", ""]
        L += _table(head_g, [grow(g) for g in r.gate_tiers])
        L += ["", "A block is true when the blocked patch, replayed against the score tests, fails them."]
    else:
        L.append("No gate decisions in the log.")

    L += ["", "## Cost-quality frontier", ""]
    if r.pareto_points:
        on = {p[0] for p in r.frontier}
        rows = [
            [a, str(r.arms[a].n), _usd(c), _pct(q), "yes" if a in on else "no"]
            for a, c, q in sorted(r.pareto_points, key=lambda p: (p[1], -p[2], p[0]))
        ]
        L += _table(["Arm", "n tasks", "Cost per task", "Solve rate", "On frontier"], rows)
        L += ["", "Frontier (cheapest first): " + " → ".join(p[0] for p in r.frontier)
              + f". Intervals for these points are in the arms table; the frontier is recomputed under `{r.table_name}`."]
    else:
        L.append("No scored arms.")

    if r.warnings:
        L += ["", "## Warnings", ""]
        L += [f"- {w}" for w in r.warnings]
    return "\n".join(L) + "\n"
