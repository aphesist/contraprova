import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from contraprova.interfaces import ModelRef
from contraprova.pricing import (
    NEUTRAL_PRICE_TABLES,
    PRICE_TABLES,
    Cost,
    ModelPrice,
    Price,
    PriceTable,
    PriceTier,
    cost_by,
    price_table_from_models_dev,
    request_cost,
    request_cost_detail,
    scale_table,
    table_from_model_prices,
    total_cost,
)
from contraprova.usage import usage_payload

T = PRICE_TABLES["anthropic-2026-11"]


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15)


def test_list_prices():
    # Haiku 5.5 list price applies to prompts up to 100K tokens.
    assert close(request_cost({"model": "claude-haiku-5-5", "input_tokens": 100_000}, T), 0.01)
    assert close(request_cost({"model": "claude-haiku-5-5", "output_tokens": 1_000_000}, T), 0.50)
    assert close(request_cost({"model": "claude-opus-5-5", "input_tokens": 1_000_000, "output_tokens": 1_000_000}, T), 24.0)
    assert close(request_cost({"model": "claude-fable-5-2", "output_tokens": 1_000_000}, T), 40.0)


def test_haiku_long_prompt_reprices_the_whole_request():
    small = {"model": "claude-haiku-5-5", "input_tokens": 100_000, "output_tokens": 1_000}
    big = {"model": "claude-haiku-5-5", "input_tokens": 100_001, "output_tokens": 1_000}
    assert close(request_cost(small, T), (100_000 * 0.10 + 1_000 * 0.50) / 1e6)
    assert close(request_cost(big, T), (100_001 * 0.50 + 1_000 * 2.50) / 1e6)


def test_cache_reads_count_toward_the_long_threshold():
    u = {"model": "claude-haiku-5-5", "input_tokens": 1_000, "cache_read_tokens": 150_000}
    assert close(request_cost(u, T), (1_000 * 0.50 + 150_000 * 0.50 * 0.1) / 1e6)


def test_cache_and_batch_multipliers():
    u = {"model": "claude-opus-5-5", "cache_read_tokens": 1_000_000}
    assert close(request_cost(u, T), 0.20)
    u = {"model": "claude-opus-5-5", "cache_write_5m_tokens": 1_000_000}
    assert close(request_cost(u, T), 5.0)
    u = {"model": "claude-opus-5-5", "cache_write_1h_tokens": 1_000_000}
    assert close(request_cost(u, T), 8.0)
    u = {"model": "claude-opus-5-5", "input_tokens": 1_000_000, "batch": True}
    assert close(request_cost(u, T), 2.0)


usage = st.fixed_dictionaries(
    {
        "model": st.sampled_from(sorted(T)),
        "input_tokens": st.integers(0, 300_000),
        "output_tokens": st.integers(0, 50_000),
        "cache_read_tokens": st.integers(0, 300_000),
        "cache_write_5m_tokens": st.integers(0, 50_000),
    }
)


@given(st.lists(usage, max_size=20), st.floats(0.01, 100))
def test_repricing_is_linear(records, k):
    base = sum(request_cost(u, T) for u in records)
    scaled = sum(request_cost(u, scale_table(T, k)) for u in records)
    assert math.isclose(scaled, base * k, rel_tol=1e-9, abs_tol=1e-12)


def test_cost_by_role():
    recs = [
        {"model": "claude-opus-5-5", "role": "orchestrator", "output_tokens": 1_000},
        {"model": "claude-haiku-5-5", "role": "worker", "output_tokens": 1_000},
        {"model": "claude-haiku-5-5", "role": "worker", "output_tokens": 1_000},
    ]
    totals = cost_by(recs, T, "role")
    assert close(totals["orchestrator"], 0.02)
    assert close(totals["worker"], 0.001)


def test_usage_payload_from_api_shape():
    api = {
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_read_input_tokens": 56,
        "cache_creation": {"ephemeral_5m_input_tokens": 7, "ephemeral_1h_input_tokens": 8},
    }
    p = usage_payload(api, "claude-haiku-5-5", role="worker", arm="D")
    assert p == {
        "model": "claude-haiku-5-5",
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_read_tokens": 56,
        "cache_write_5m_tokens": 7,
        "cache_write_1h_tokens": 8,
        "role": "worker",
        "arm": "D",
    }


def test_usage_payload_legacy_cache_total():
    p = usage_payload({"input_tokens": 1, "cache_creation_input_tokens": 9}, "claude-opus-5-5")
    assert (p["cache_write_5m_tokens"], p["cache_write_1h_tokens"]) == (9, 0)


# ------------------------------------------------------------------ neutral price tables

N = NEUTRAL_PRICE_TABLES["anthropic-2026-11"]


def test_the_anthropic_table_in_absolute_rates():
    opus = N.entries[ModelRef("anthropic", "claude-opus-5-5")]
    assert (opus.input, opus.output, opus.cache_read, opus.cache_write_5m, opus.cache_write_1h) == (4.0, 20.0, 0.2, 5.0, 8.0)
    haiku = N.entries[ModelRef("anthropic", "claude-haiku-5-5")]
    (tier,) = haiku.tiers
    assert (tier.above_prompt_tokens, tier.input, tier.output, tier.cache_read) == (100_000, 0.5, 2.5, 0.05)
    assert all(p.batch_mult == 0.5 for p in N.entries.values())
    assert N.models() == tuple(sorted(T))


records = st.fixed_dictionaries(
    {
        "model": st.sampled_from(sorted(T)),
        "input_tokens": st.integers(0, 300_000),
        "output_tokens": st.integers(0, 50_000),
        "cache_read_tokens": st.integers(0, 300_000),
        "cache_write_5m_tokens": st.integers(0, 50_000),
        "cache_write_1h_tokens": st.integers(0, 50_000),
        "batch": st.booleans(),
    }
)


@given(st.lists(records, max_size=20))
def test_the_new_table_reproduces_todays_costs(recs):
    for u in recs:
        assert math.isclose(request_cost(u, N), request_cost(u, T), rel_tol=1e-12, abs_tol=1e-15)
    assert math.isclose(sum(request_cost(u, N) for u in recs), sum(request_cost(u, T) for u in recs),
                        rel_tol=1e-12, abs_tol=1e-15)


def test_the_new_table_reproduces_the_list_price_tests():
    assert close(request_cost({"model": "claude-haiku-5-5", "input_tokens": 100_000}, N), 0.01)
    big = {"model": "claude-haiku-5-5", "input_tokens": 100_001, "output_tokens": 1_000}
    assert close(request_cost(big, N), (100_001 * 0.50 + 1_000 * 2.50) / 1e6)
    u = {"model": "claude-haiku-5-5", "input_tokens": 1_000, "cache_read_tokens": 150_000}
    assert close(request_cost(u, N), (1_000 * 0.50 + 150_000 * 0.50 * 0.1) / 1e6)
    assert close(request_cost({"model": "claude-opus-5-5", "cache_write_1h_tokens": 1_000_000}, N), 8.0)
    assert close(request_cost({"model": "claude-opus-5-5", "input_tokens": 1_000_000, "batch": True}, N), 2.0)
    assert close(cost_by([{"model": "claude-opus-5-5", "role": "o", "output_tokens": 1_000}], N, "role")["o"], 0.02)


usage_n = st.fixed_dictionaries(
    {
        "model": st.sampled_from(N.models()),
        "input_tokens": st.integers(0, 300_000),
        "output_tokens": st.integers(0, 50_000),
        "cache_read_tokens": st.integers(0, 300_000),
        "cache_write_5m_tokens": st.integers(0, 50_000),
        "cache_write_1h_tokens": st.integers(0, 50_000),
        "reasoning_tokens": st.integers(0, 100),
    }
)


@given(st.lists(usage_n, max_size=20), st.floats(0.01, 100))
def test_repricing_is_linear_on_the_new_table(records, k):
    base = sum(request_cost(u, N) for u in records)
    scaled = sum(request_cost(u, scale_table(N, k)) for u in records)
    assert math.isclose(scaled, base * k, rel_tol=1e-9, abs_tol=1e-12)


def test_scaling_a_neutral_table_scales_tiers_and_keeps_batch():
    s = scale_table(N, 3)
    assert isinstance(s, PriceTable) and s.version == "anthropic-2026-11*3"
    haiku = s.entries[ModelRef("anthropic", "claude-haiku-5-5")]
    assert (haiku.input, haiku.tiers[0].output, haiku.batch_mult) == (pytest.approx(0.3), pytest.approx(7.5), 0.5)


def test_unspecified_buckets_fall_back_and_reasoning_is_billed_inside_output():
    table = PriceTable("t", {ModelRef("p", "m"): Price(input=1.0, output=10.0)})
    assert close(request_cost({"model": "m", "cache_read_tokens": 1_000_000}, table), 1.0)  # no discount
    assert close(request_cost({"model": "m", "cache_write_1h_tokens": 1_000_000}, table), 1.0)
    u = {"model": "m", "output_tokens": 1_000_000, "reasoning_tokens": 400_000}
    assert close(request_cost(u, table), 10.0)  # a subset of output, not an addition
    priced = PriceTable("t", {ModelRef("p", "m"): Price(input=1.0, output=10.0, reasoning=3.0)})
    assert close(request_cost(u, priced), (600_000 * 10.0 + 400_000 * 3.0) / 1e6)


def test_tiers_pick_the_highest_threshold_exceeded():
    price = Price(input=1.0, output=2.0, tiers=(PriceTier(input=2.0, output=4.0, above_prompt_tokens=100),
                                                PriceTier(input=3.0, output=6.0, above_prompt_tokens=1_000)))
    assert price.rates_for(100).input == 1.0
    assert price.rates_for(101).input == 2.0
    assert price.rates_for(5_000).input == 3.0


def test_lookup_by_provider_and_the_ambiguous_bare_id():
    table = PriceTable("t", {ModelRef("a", "m"): Price(input=1.0, output=1.0), ModelRef("b", "m"): Price(input=2.0, output=2.0),
                             ModelRef("a", "only"): Price(input=3.0, output=3.0)})
    assert close(request_cost({"model": "m", "provider": "b", "input_tokens": 1_000_000}, table), 2.0)
    assert close(request_cost({"model": "only", "input_tokens": 1_000_000}, table), 3.0)  # a legacy bare id
    assert close(request_cost({"model": "a/m", "input_tokens": 1_000_000}, table), 1.0)
    with pytest.raises(KeyError, match="several providers"):
        request_cost({"model": "m", "input_tokens": 1}, table)
    with pytest.raises(KeyError):
        request_cost({"model": "m", "provider": "c", "input_tokens": 1}, table)
    with pytest.raises(KeyError):
        request_cost({"model": "ghost", "input_tokens": 1}, table)


MODELS_DEV = {
    "openai": {"models": {
        "gpt-x": {"name": "GPT X", "cost": {"input": 1.25, "output": 10, "cache_read": 0.125}},
        "o-x": {"cost": {"input": 2, "output": 8, "cache_read": 0.5, "reasoning": 8}},
        "no-price": {"name": "unpriced"},
    }},
    "anthropic": {"claude-x": {"cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75,
                                         "context_over_200k": {"input": 6, "output": 22.5}}}},
    "ollama": {"llama": {"cost": {"input": 0, "output": 0}}},
}


def test_models_dev_loader():
    t = price_table_from_models_dev(MODELS_DEV, "models.dev-test")
    assert t.version == "models.dev-test"
    assert set(map(str, t.entries)) == {"openai/gpt-x", "openai/o-x", "anthropic/claude-x", "ollama/llama"}
    gpt = t.entries[ModelRef("openai", "gpt-x")]
    assert (gpt.input, gpt.output, gpt.cache_read) == (1.25, 10.0, 0.125)
    claude = t.entries[ModelRef("anthropic", "claude-x")]
    assert claude.cache_write_5m == 3.75 and claude.tiers[0].above_prompt_tokens == 200_000
    u = {"model": "gpt-x", "provider": "openai", "input_tokens": 1_000_000, "cache_read_tokens": 1_000_000,
         "output_tokens": 100_000}
    assert close(request_cost(u, t), 1.25 + 0.125 + 1.0)
    big = {"model": "claude-x", "provider": "anthropic", "input_tokens": 200_001}
    assert close(request_cost(big, t), 200_001 * 6 / 1e6)
    assert request_cost({"model": "llama", "input_tokens": 5_000, "output_tokens": 500}, t) == 0.0
    with pytest.raises(KeyError):
        request_cost({"model": "no-price", "provider": "openai", "input_tokens": 1}, t)
    only = price_table_from_models_dev(MODELS_DEV, "v", providers=["ollama"])
    assert set(map(str, only.entries)) == {"ollama/llama"}


@pytest.mark.parametrize("cost", [{"output": 1}, {"input": -1, "output": 1}, {"input": "1", "output": 1}, {"input": True, "output": 1}])
def test_models_dev_loader_rejects_malformed_cost(cost):
    with pytest.raises(ValueError, match="p.m.cost"):
        price_table_from_models_dev({"p": {"m": {"cost": cost}}}, "v")


def test_a_loaded_table_reprices_linearly():
    t = price_table_from_models_dev(MODELS_DEV, "v")
    u = {"model": "gpt-x", "provider": "openai", "input_tokens": 7, "output_tokens": 9, "cache_read_tokens": 11}
    assert math.isclose(request_cost(u, scale_table(t, 2.5)), 2.5 * request_cost(u, t), rel_tol=1e-12)


def test_subscription_mode_bills_zero_and_keeps_the_api_equivalent():
    u = {"model": "claude-opus-5-5", "input_tokens": 1_000_000}
    assert request_cost_detail(u, N) == Cost(4.0, 4.0, False)  # auto: not flagged, so billed
    assert request_cost_detail({**u, "subscription": True}, N) == Cost(0.0, 4.0, True)
    assert request_cost_detail({**u, "subscription": False}, N, "subscription") == Cost(0.0, 4.0, True)
    assert request_cost_detail({**u, "subscription": True}, N, "api") == Cost(4.0, 4.0, False)
    assert request_cost_detail({**u, "subscription": True}, T) == Cost(0.0, 4.0, True)  # legacy tables too
    assert close(request_cost({**u, "subscription": True}, N), 4.0)  # request_cost stays API-equivalent
    with pytest.raises(ValueError):
        request_cost_detail(u, N, "free")
    with pytest.raises(KeyError):
        request_cost_detail({"model": "ghost", "subscription": True}, N)


def test_total_cost_mixes_plan_and_api_requests():
    recs = [{"model": "claude-opus-5-5", "output_tokens": 1_000_000, "subscription": True},
            {"model": "claude-haiku-5-5", "output_tokens": 1_000_000}]
    tot = total_cost(recs, N)
    assert (tot.usd, tot.api_equivalent_usd, tot.subscription) == (pytest.approx(0.5), pytest.approx(20.5), False)
    assert total_cost([], N) == Cost(0.0, 0.0, False)


def test_converting_a_custom_multiplier_table():
    legacy = {"m": ModelPrice(2.0, 8.0, 0.25)}
    t = table_from_model_prices("custom", legacy, provider="acme")
    u = {"model": "m", "input_tokens": 10, "cache_read_tokens": 20, "cache_write_5m_tokens": 30,
         "cache_write_1h_tokens": 40, "output_tokens": 50}
    assert math.isclose(request_cost(u, t), request_cost(u, legacy), rel_tol=1e-12)
    assert str(next(iter(t.entries))) == "acme/m"


def test_reasoning_is_never_double_charged():
    u = {"model": "m", "output_tokens": 1_000, "reasoning_tokens": 300}
    same = PriceTable("t", {ModelRef("p", "m"): Price(input=1.0, output=10.0)})
    assert close(request_cost(u, same), 1_000 * 10.0 / 1e6)  # no reasoning rate: output rate for all
    own = PriceTable("t", {ModelRef("p", "m"): Price(input=1.0, output=10.0, reasoning=4.0)})
    assert close(request_cost(u, own), (700 * 10.0 + 300 * 4.0) / 1e6)  # (output - reasoning) + reasoning
