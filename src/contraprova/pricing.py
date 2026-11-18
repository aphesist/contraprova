"""Price tables and cost computation.

The log stores tokens, never dollars. Dollars are a projection: the same
history can be re-priced under any table without re-running anything.

Field names follow the Anthropic Messages API ``usage`` object, where
``input_tokens`` excludes cache reads and cache writes (``usage.py`` maps every other provider onto
that meaning).

Two table types price the same records. ``ModelPrice`` tables (``PRICE_TABLES``) carry Anthropic's
multipliers and are what the study ran on. ``PriceTable`` (``NEUTRAL_PRICE_TABLES``, a models.dev
loader) carries absolute per-Mtok rates per bucket for any provider. ``request_cost``, ``cost_by``
and ``scale_table`` accept either.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal, Mapping, Self

from .interfaces import ModelRef

PER_MTOK = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_read_mult: float = 0.1
    # Some models charge the whole request at a higher rate above a prompt size.
    long_threshold: int | None = None
    long_input: float | None = None
    long_output: float | None = None

    def rates(self, prompt_tokens: int) -> tuple[float, float]:
        if self.long_threshold is not None and prompt_tokens > self.long_threshold:
            assert self.long_input is not None and self.long_output is not None
            return self.long_input, self.long_output
        return self.input, self.output


CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0
BATCH_MULT = 0.5

# Source: Anthropic API pricing page, checked 2026-11-11.
PRICE_TABLES: dict[str, dict[str, ModelPrice]] = {
    "anthropic-2026-11": {
        "claude-haiku-5-5": ModelPrice(0.10, 0.50, 0.1, long_threshold=100_000, long_input=0.50, long_output=2.50),
        "claude-sonnet-5-5": ModelPrice(2.00, 10.00, 0.05),
        "claude-opus-5-5": ModelPrice(4.00, 20.00, 0.05),
        "claude-fable-5-2": ModelPrice(8.00, 40.00, 0.025),
    },
}
DEFAULT_TABLE = "anthropic-2026-11"


def prompt_tokens(u: Mapping) -> int:
    return (
        int(u.get("input_tokens", 0))
        + int(u.get("cache_read_tokens", 0))
        + int(u.get("cache_write_5m_tokens", 0))
        + int(u.get("cache_write_1h_tokens", 0))
    )


def request_cost(u: Mapping, table: "Mapping[str, ModelPrice] | PriceTable") -> float:
    """USD for one recorded request: the API-equivalent cost, whatever the plan it was billed on."""
    if isinstance(table, PriceTable):
        return table.price_for(u).cost(u)
    model = u["model"]
    if model not in table:
        raise KeyError(f"no price for model {model!r}")
    price = table[model]
    inp, out = price.rates(prompt_tokens(u))
    usd = (
        int(u.get("input_tokens", 0)) * inp
        + int(u.get("cache_read_tokens", 0)) * inp * price.cache_read_mult
        + int(u.get("cache_write_5m_tokens", 0)) * inp * CACHE_WRITE_5M_MULT
        + int(u.get("cache_write_1h_tokens", 0)) * inp * CACHE_WRITE_1H_MULT
        + int(u.get("output_tokens", 0)) * out
    ) / PER_MTOK
    if u.get("batch"):
        usd *= BATCH_MULT
    return usd


def scale_table(table: "Mapping[str, ModelPrice] | PriceTable", k: float) -> "dict[str, ModelPrice] | PriceTable":
    """Multiply every rate by ``k`` (handy for currency conversion and what-if pricing)."""
    if isinstance(table, PriceTable):
        return table.scaled(k)
    out = {}
    for name, p in table.items():
        out[name] = ModelPrice(
            p.input * k,
            p.output * k,
            p.cache_read_mult,
            p.long_threshold,
            None if p.long_input is None else p.long_input * k,
            None if p.long_output is None else p.long_output * k,
        )
    return out


def cost_by(records, table: "Mapping[str, ModelPrice] | PriceTable", key: str) -> dict[str, float]:
    """Total USD grouped by a usage field such as ``role``, ``model``, ``arm`` or ``task_id``."""
    totals: dict[str, float] = {}
    for u in records:
        k = str(u.get(key, "unknown"))
        totals[k] = totals.get(k, 0.0) + request_cost(u, table)
    return totals


# ------------------------------------------------------------------ neutral price tables


def _times(rate: float | None, k: float) -> float | None:
    return None if rate is None else rate * k


@dataclass(frozen=True)
class Rates:
    """Absolute USD per million tokens, one rate per bucket.

    A bucket left ``None`` is billed at another rate: ``cache_read`` and ``cache_write_5m`` at
    ``input`` (no discount, no premium), ``cache_write_1h`` at ``cache_write_5m``, and ``reasoning``
    at ``output``. Reasoning tokens are a subset of the output tokens (see ``usage.py``): they are
    billed at ``reasoning`` instead of ``output``, never in addition to it.
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None
    reasoning: float | None = None

    def resolved(self) -> Self:
        """The same rates with every ``None`` replaced by the rate it falls back to."""
        write_5m = self.input if self.cache_write_5m is None else self.cache_write_5m
        return replace(
            self,
            cache_read=self.input if self.cache_read is None else self.cache_read,
            cache_write_5m=write_5m,
            cache_write_1h=write_5m if self.cache_write_1h is None else self.cache_write_1h,
            reasoning=self.output if self.reasoning is None else self.reasoning,
        )

    def scaled(self, k: float) -> Self:
        """Every rate times ``k``."""
        return replace(self, input=self.input * k, output=self.output * k, cache_read=_times(self.cache_read, k),
                       cache_write_5m=_times(self.cache_write_5m, k), cache_write_1h=_times(self.cache_write_1h, k),
                       reasoning=_times(self.reasoning, k))


@dataclass(frozen=True, kw_only=True)
class PriceTier(Rates):
    """Rates that replace the base rates for the *whole* request once its prompt exceeds
    ``above_prompt_tokens`` (prompt = uncached input + cache reads + cache writes)."""

    above_prompt_tokens: int


@dataclass(frozen=True, kw_only=True)
class Price(Rates):
    """One model's rates, optional long-context tiers (``PriceTier``) and the batch multiplier."""

    tiers: tuple[PriceTier, ...] = ()
    batch_mult: float = 0.5

    def rates_for(self, prompt: int) -> Rates:
        """The resolved rates for a request with ``prompt`` prompt tokens: the tier with the highest
        threshold the prompt exceeds, else the base rates."""
        hit = [t for t in self.tiers if prompt > t.above_prompt_tokens]
        chosen: Rates = max(hit, key=lambda t: t.above_prompt_tokens) if hit else self
        return chosen.resolved()

    def cost(self, u: Mapping[str, Any]) -> float:
        """USD for one usage record under these rates."""
        r = self.rates_for(prompt_tokens(u))
        out = int(u.get("output_tokens", 0))
        reasoning = min(int(u.get("reasoning_tokens", 0) or 0), out)
        assert r.cache_read is not None and r.cache_write_5m is not None
        assert r.cache_write_1h is not None and r.reasoning is not None
        usd = (
            int(u.get("input_tokens", 0)) * r.input
            + int(u.get("cache_read_tokens", 0)) * r.cache_read
            + int(u.get("cache_write_5m_tokens", 0)) * r.cache_write_5m
            + int(u.get("cache_write_1h_tokens", 0)) * r.cache_write_1h
            + (out - reasoning) * r.output
            + reasoning * r.reasoning
        ) / PER_MTOK
        if u.get("batch"):
            usd *= self.batch_mult
        return usd

    def scaled(self, k: float) -> Self:
        """Every rate, tier rates included, times ``k``; the batch multiplier is a ratio and stays."""
        return replace(super().scaled(k), tiers=tuple(t.scaled(k) for t in self.tiers))


@dataclass(frozen=True)
class PriceTable:
    """A versioned table of ``Price`` per ``ModelRef``, with absolute per-Mtok rates."""

    version: str
    entries: Mapping[ModelRef, Price] = field(default_factory=dict)

    def models(self) -> tuple[str, ...]:
        """Sorted bare model ids in the table (what usage records carry in ``model``)."""
        return tuple(sorted({ref.id for ref in self.entries}))

    def price_for(self, u: Mapping[str, Any]) -> Price:
        """The price of the model a usage record names. Raises KeyError if there is none.

        A record with ``provider`` is looked up as ``provider/model``. Older records (and
        Anthropic-era logs) carry only the bare id: it must then match exactly one entry, or be a
        ``"provider/id"`` string itself.
        """
        model = str(u["model"])
        provider = u.get("provider")
        if provider:
            ref = ModelRef(str(provider), model)
            if ref in self.entries:
                return self.entries[ref]
            raise KeyError(f"no price for model {str(ref)!r}")
        matches = [ref for ref in self.entries if ref.id == model]
        if len(matches) == 1:
            return self.entries[matches[0]]
        if len(matches) > 1:
            raise KeyError(f"model {model!r} is priced for several providers {sorted(str(m) for m in matches)}; "
                           "the usage record needs a provider")
        if "/" in model and ModelRef.parse(model) in self.entries:
            return self.entries[ModelRef.parse(model)]
        raise KeyError(f"no price for model {model!r}")

    def scaled(self, k: float) -> "PriceTable":
        """Every rate times ``k``; the version gets a ``*k`` suffix so a scaled table is never mistaken for its source."""
        return PriceTable(f"{self.version}*{k:g}", {ref: p.scaled(k) for ref, p in self.entries.items()})


def table_from_model_prices(version: str, table: Mapping[str, ModelPrice], provider: str = "anthropic") -> PriceTable:
    """Express a multiplier table (``ModelPrice``) as absolute rates for ``provider``.

    Each rate is computed with the same multipliers ``request_cost`` applies, so costs agree.
    """

    def rates(inp: float, out: float, read_mult: float) -> dict[str, float]:
        return {"input": inp, "output": out, "cache_read": inp * read_mult,
                "cache_write_5m": inp * CACHE_WRITE_5M_MULT, "cache_write_1h": inp * CACHE_WRITE_1H_MULT}

    entries: dict[ModelRef, Price] = {}
    for model, p in table.items():
        tiers: tuple[PriceTier, ...] = ()
        if p.long_threshold is not None:
            assert p.long_input is not None and p.long_output is not None
            tiers = (PriceTier(**rates(p.long_input, p.long_output, p.cache_read_mult), above_prompt_tokens=p.long_threshold),)
        entries[ModelRef(provider, model)] = Price(**rates(p.input, p.output, p.cache_read_mult), tiers=tiers,
                                                   batch_mult=BATCH_MULT)
    return PriceTable(version, entries)


NEUTRAL_PRICE_TABLES: dict[str, PriceTable] = {
    name: table_from_model_prices(name, legacy) for name, legacy in PRICE_TABLES.items()
}


def _rate(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value != value:
        raise ValueError(f"{where}: expected a non-negative number of USD per million tokens, got {value!r}")
    return float(value)


def _models_dev_rates(cost: Mapping[str, Any], where: str) -> dict[str, float]:
    for key in ("input", "output"):
        if key not in cost:
            raise ValueError(f"{where}: cost has no {key!r}")
    out = {"input": _rate(cost["input"], f"{where}.input"), "output": _rate(cost["output"], f"{where}.output")}
    for src, dst in (("cache_read", "cache_read"), ("cache_write", "cache_write_5m"), ("reasoning", "reasoning")):
        if cost.get(src) is not None:
            out[dst] = _rate(cost[src], f"{where}.{src}")
    return out


def price_table_from_models_dev(data: Mapping[str, Any], version: str, *, providers: Iterable[str] | None = None) -> PriceTable:
    """Build a ``PriceTable`` from a models.dev-style dict: provider -> model -> ``cost``.

    ``cost`` holds USD per million tokens: ``input``, ``output`` and optionally ``cache_read``,
    ``cache_write`` (a single bucket: priced as the 5-minute write) and ``reasoning``;
    ``context_over_200k`` (same keys) becomes a long-context tier above 200,000 prompt tokens. A
    provider's models may sit directly under it or under its ``models`` key (the models.dev file).
    A model with no ``cost`` is left out, so pricing it raises KeyError rather than guessing.
    ``providers`` restricts the load. A malformed ``cost`` raises ValueError naming the path.
    """
    wanted = None if providers is None else set(providers)
    entries: dict[ModelRef, Price] = {}
    for provider, body in data.items():
        if wanted is not None and provider not in wanted:
            continue
        models = body.get("models", body) if isinstance(body, Mapping) else {}
        for model_id, model in models.items():
            cost = model.get("cost") if isinstance(model, Mapping) else None
            if not isinstance(cost, Mapping):
                continue
            where = f"{provider}.{model_id}.cost"
            tiers: tuple[PriceTier, ...] = ()
            long = cost.get("context_over_200k")
            if isinstance(long, Mapping):
                tiers = (PriceTier(**_models_dev_rates(long, where + ".context_over_200k"), above_prompt_tokens=200_000),)
            entries[ModelRef(provider, str(model_id))] = Price(**_models_dev_rates(cost, where), tiers=tiers)
    return PriceTable(version, entries)


# ------------------------------------------------------------------ billed cost: API or subscription

PricingMode = Literal["api", "subscription", "auto"]


@dataclass(frozen=True)
class Cost:
    """What a request costs. ``usd`` is what is billed; ``api_equivalent_usd`` what the API would charge."""

    usd: float
    api_equivalent_usd: float
    subscription: bool

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(self.usd + other.usd, self.api_equivalent_usd + other.api_equivalent_usd,
                    self.subscription and other.subscription)


def request_cost_detail(u: Mapping, table: "Mapping[str, ModelPrice] | PriceTable", mode: PricingMode = "auto") -> Cost:
    """Billed and API-equivalent cost of one request.

    ``mode``: ``"api"`` bills the API price; ``"subscription"`` bills 0 (the request was covered by a
    plan) and keeps the API price alongside; ``"auto"`` (default) uses ``"subscription"`` when the
    record says ``subscription: true``. The API-equivalent figure needs a price for the model, so an
    unpriced model raises KeyError in every mode.
    """
    if mode not in ("api", "subscription", "auto"):
        raise ValueError(f"unknown pricing mode {mode!r}")
    api = request_cost(u, table)
    covered = mode == "subscription" or (mode == "auto" and u.get("subscription") is True)
    return Cost(0.0 if covered else api, api, covered)


def total_cost(records: Iterable[Mapping], table: "Mapping[str, ModelPrice] | PriceTable", mode: PricingMode = "auto") -> Cost:
    """Sum of ``request_cost_detail`` over ``records`` (``subscription`` is true only if all are covered)."""
    total = Cost(0.0, 0.0, True)
    n = 0
    for u in records:
        total, n = total + request_cost_detail(u, table, mode), n + 1
    return total if n else Cost(0.0, 0.0, False)
