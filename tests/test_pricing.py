import math

from hypothesis import given
from hypothesis import strategies as st

from contraprova.pricing import PRICE_TABLES, cost_by, request_cost, scale_table
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
