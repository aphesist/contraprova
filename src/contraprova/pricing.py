"""Price tables and cost computation.

The log stores tokens, never dollars. Dollars are a projection: the same
history can be re-priced under any table without re-running anything.

Field names follow the Anthropic Messages API ``usage`` object, where
``input_tokens`` excludes cache reads and cache writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

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


def request_cost(u: Mapping, table: Mapping[str, ModelPrice]) -> float:
    """USD for one recorded request."""
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


def scale_table(table: Mapping[str, ModelPrice], k: float) -> dict[str, ModelPrice]:
    """Multiply every rate by ``k`` (handy for currency conversion and what-if pricing)."""
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


def cost_by(records, table: Mapping[str, ModelPrice], key: str) -> dict[str, float]:
    """Total USD grouped by a usage field such as ``role``, ``model``, ``arm`` or ``task_id``."""
    totals: dict[str, float] = {}
    for u in records:
        k = str(u.get(key, "unknown"))
        totals[k] = totals.get(k, 0.0) + request_cost(u, table)
    return totals
