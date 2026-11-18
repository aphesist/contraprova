"""One test per provider usage shape: each maps onto the neutral ``usage.recorded`` payload.

Neutral meaning (usage.py): input_tokens is uncached input, output_tokens includes reasoning.
"""

from types import SimpleNamespace

import pytest

from contraprova.interfaces import ModelRef
from contraprova.usage import (
    USAGE_MAPPERS,
    anthropic_usage_payload,
    deepseek_usage_payload,
    gemini_usage_payload,
    ollama_usage_payload,
    openai_usage_payload,
    provider_usage_payload,
    usage_payload,
)
from contraprova.events import Event
from contraprova.pricing import PriceTable, Price, request_cost

REQUIRED = {"model", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens"}


def test_anthropic_shape():
    api = {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 56,
           "cache_creation": {"ephemeral_5m_input_tokens": 7, "ephemeral_1h_input_tokens": 8}}
    p = anthropic_usage_payload(api, "claude-haiku-5-5", latency_ms=420, request_id="req_1", role="worker")
    assert p == {"model": "claude-haiku-5-5", "provider": "anthropic", "input_tokens": 12, "output_tokens": 34,
                 "cache_read_tokens": 56, "cache_write_5m_tokens": 7, "cache_write_1h_tokens": 8, "usage_schema": 2,
                 "latency_ms": 420, "request_id": "req_1", "role": "worker"}
    legacy = usage_payload(api, "claude-haiku-5-5")  # the original mapping is unchanged
    assert {k: p[k] for k in legacy} == legacy and "provider" not in legacy and "usage_schema" not in legacy
    # The SDK object and the older total-only cache field work as before.
    sdk = SimpleNamespace(input_tokens=1, output_tokens=2, cache_read_input_tokens=0, cache_creation=None,
                          cache_creation_input_tokens=9)
    q = anthropic_usage_payload(sdk, ModelRef("anthropic", "claude-opus-5-5"))
    assert (q["cache_write_5m_tokens"], q["cache_write_1h_tokens"], q["model"], q["provider"]) == (9, 0, "claude-opus-5-5", "anthropic")
    assert "reasoning_tokens" not in q


def test_openai_chat_shape_subtracts_cached_tokens_and_reads_reasoning():
    usage = {"prompt_tokens": 1000, "completion_tokens": 300, "total_tokens": 1300,
             "prompt_tokens_details": {"cached_tokens": 600, "audio_tokens": 0},
             "completion_tokens_details": {"reasoning_tokens": 120}}
    p = openai_usage_payload(usage, "gpt-5", request_id="chatcmpl-1", latency_ms=900)
    assert p == {"model": "gpt-5", "provider": "openai", "input_tokens": 400, "output_tokens": 300,
                 "cache_read_tokens": 600, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
                 "reasoning_tokens": 120, "usage_schema": 2, "latency_ms": 900, "request_id": "chatcmpl-1"}


def test_openai_responses_shape():
    usage = {"input_tokens": 2000, "output_tokens": 500, "total_tokens": 2500,
             "input_tokens_details": {"cached_tokens": 1500},
             "output_tokens_details": {"reasoning_tokens": 200}}
    p = openai_usage_payload(usage, ModelRef("openai", "gpt-5"), subscription=True)
    assert (p["input_tokens"], p["cache_read_tokens"], p["output_tokens"], p["reasoning_tokens"]) == (500, 1500, 500, 200)
    assert p["subscription"] is True and p["provider"] == "openai"


def test_openai_shape_without_details_and_from_an_sdk_object():
    sdk = SimpleNamespace(prompt_tokens=50, completion_tokens=7, prompt_tokens_details=None, completion_tokens_details=None)
    p = openai_usage_payload(sdk, "gpt-4.1-mini", provider="openrouter")
    assert (p["input_tokens"], p["cache_read_tokens"], p["output_tokens"], p["provider"]) == (50, 0, 7, "openrouter")
    assert "reasoning_tokens" not in p  # unknown is left out, not zero


def test_gemini_shape_subtracts_cache_and_bills_thoughts_as_output():
    usage = {"promptTokenCount": 3000, "cachedContentTokenCount": 2000, "candidatesTokenCount": 150,
             "thoughtsTokenCount": 850, "totalTokenCount": 4000}
    p = gemini_usage_payload(usage, "gemini-2.5-pro")
    assert p == {"model": "gemini-2.5-pro", "provider": "google", "input_tokens": 1000, "output_tokens": 1000,
                 "cache_read_tokens": 2000, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
                 "reasoning_tokens": 850, "usage_schema": 2}
    sdk = SimpleNamespace(prompt_token_count=10, cached_content_token_count=None, candidates_token_count=4, thoughts_token_count=None)
    q = gemini_usage_payload(sdk, "gemini-2.5-flash")
    assert (q["input_tokens"], q["output_tokens"], q["reasoning_tokens"]) == (10, 4, 0)


def test_deepseek_shape_uses_hit_and_miss_counts():
    usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200,
             "completion_tokens": 350, "completion_tokens_details": {"reasoning_tokens": 300}}
    p = deepseek_usage_payload(usage, "deepseek-reasoner")
    assert (p["input_tokens"], p["cache_read_tokens"], p["output_tokens"], p["reasoning_tokens"], p["provider"]) == (
        200, 800, 350, 300, "deepseek")
    # No miss count: the misses are what is left of the prompt.
    q = deepseek_usage_payload({"prompt_tokens": 100, "prompt_cache_hit_tokens": 30, "completion_tokens": 5}, "deepseek-chat")
    assert (q["input_tokens"], q["cache_read_tokens"]) == (70, 30)


def test_ollama_shape():
    usage = {"prompt_eval_count": 26, "eval_count": 298, "total_duration": 5_043_500_000, "load_duration": 1}
    p = ollama_usage_payload(usage, "llama3.2")
    assert p == {"model": "llama3.2", "provider": "ollama", "input_tokens": 26, "output_tokens": 298,
                 "cache_read_tokens": 0, "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0, "usage_schema": 2,
                 "latency_ms": 5043}
    assert ollama_usage_payload({"eval_count": 1}, "m", latency_ms=7)["latency_ms"] == 7
    assert "latency_ms" not in ollama_usage_payload({"prompt_eval_count": 1, "eval_count": 1}, "m")


@pytest.mark.parametrize("shape, usage", [
    ("anthropic", {"input_tokens": 1}),
    ("openai", {"prompt_tokens": 1}),
    ("gemini", {"promptTokenCount": 1}),
    ("deepseek", {"prompt_tokens": 1}),
    ("ollama", {"prompt_eval_count": 1}),
])
def test_every_shape_yields_the_required_fields_and_a_valid_event(shape, usage):
    p = provider_usage_payload(shape, usage, "m", role="worker", task_id="t")
    assert REQUIRED <= set(p) and p["usage_schema"] == 2 and p["role"] == "worker"
    assert all(isinstance(p[k], int) and p[k] >= 0 for k in REQUIRED - {"model"})
    Event.create(0, "usage.recorded", "plugin", "2026-11-18T09:00:00.000Z", p, "0" * 64)  # JSON-clean


def test_all_shapes_are_registered():
    assert set(USAGE_MAPPERS) == {"anthropic", "openai", "gemini", "deepseek", "ollama"}
    with pytest.raises(ValueError, match="unknown usage shape"):
        provider_usage_payload("mistral", {}, "m")


@pytest.mark.parametrize("fn, usage", [
    (openai_usage_payload, {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 6}}),
    (gemini_usage_payload, {"promptTokenCount": 5, "cachedContentTokenCount": 6}),
    (deepseek_usage_payload, {"prompt_tokens": 5, "prompt_cache_hit_tokens": 6}),
    (openai_usage_payload, {"prompt_tokens": -1}),
    (openai_usage_payload, {"completion_tokens": 2, "completion_tokens_details": {"reasoning_tokens": 3}}),
])
def test_inconsistent_usage_is_an_error_not_a_guess(fn, usage):
    with pytest.raises(ValueError):
        fn(usage, "m")


def test_the_mapped_payload_prices_the_same_for_every_provider():
    """Equal real usage must cost the same however a provider reports it."""
    table = PriceTable("t", {ModelRef(p, "m"): Price(input=2.0, output=8.0, cache_read=0.5) for p in
                             ("openai", "google", "deepseek", "ollama")})
    openai = openai_usage_payload({"prompt_tokens": 1000, "completion_tokens": 100,
                                   "prompt_tokens_details": {"cached_tokens": 400}}, "m")
    gemini = gemini_usage_payload({"promptTokenCount": 1000, "cachedContentTokenCount": 400, "candidatesTokenCount": 100}, "m")
    deepseek = deepseek_usage_payload({"prompt_cache_hit_tokens": 400, "prompt_cache_miss_tokens": 600,
                                       "completion_tokens": 100}, "m")
    costs = [request_cost(p, table) for p in (openai, gemini, deepseek)]
    assert costs[0] == pytest.approx((600 * 2.0 + 400 * 0.5 + 100 * 8.0) / 1e6)
    assert costs == [costs[0]] * 3
