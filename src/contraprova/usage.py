"""Turn a provider's token usage into a ``usage.recorded`` payload.

Cost is never taken from a harness estimate. It is recomputed later from these token counts, which
come straight from the provider's response.

One neutral meaning, whatever the provider (``usage_schema`` 2):

- ``input_tokens``: **uncached** input only. Providers whose prompt count includes cached tokens
  (OpenAI, Gemini, DeepSeek) are mapped by subtracting them.
- ``cache_read_tokens``: input served from a cache. ``cache_write_5m_tokens`` / ``cache_write_1h_tokens``:
  input written to a cache; a provider with a single write bucket reports it as ``5m``.
- ``output_tokens``: everything billed as output, **including** ``reasoning_tokens``.
- ``reasoning_tokens`` (optional): the part of ``output_tokens`` spent thinking, when the provider
  reports it. A subset, never an addition.

Optional fields are written only when known: ``provider``, ``reasoning_tokens``, ``latency_ms``,
``request_id`` and ``subscription`` (billed to a plan, not the API). ``usage_payload`` is the original
Anthropic mapping and its output is unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .interfaces import ModelRef

USAGE_SCHEMA = 2


def _get(obj: Any, name: str, default: Any = 0) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def usage_payload(usage: Any, model: str, **tags: Any) -> dict[str, Any]:
    """Accepts the SDK's Usage object or a plain dict. ``tags`` might be role, arm, task_id."""
    creation = _get(usage, "cache_creation", None)
    w5 = _get(creation, "ephemeral_5m_input_tokens", None)
    w1 = _get(creation, "ephemeral_1h_input_tokens", None)
    if w5 is None and w1 is None:
        # Older responses only report a total; the default cache TTL is 5 minutes.
        w5, w1 = _get(usage, "cache_creation_input_tokens", 0) or 0, 0
    payload = {
        "model": model,
        "input_tokens": int(_get(usage, "input_tokens", 0) or 0),
        "output_tokens": int(_get(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(_get(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write_5m_tokens": int(w5 or 0),
        "cache_write_1h_tokens": int(w1 or 0),
    }
    payload.update(tags)
    return payload


# ------------------------------------------------------------------ neutral mappings


def _first(obj: Any, *names: str) -> Any:
    """The first of ``names`` that ``obj`` (a mapping or an SDK object) has with a value, else None."""
    for name in names:
        value = _get(obj, name, None)
        if value is not None:
            return value
    return None


def _count(value: Any) -> int:
    n = int(value or 0)
    if n < 0:
        raise ValueError(f"negative token count {n}")
    return n


def _uncached(total_input: int, cached: int, provider: str) -> int:
    """Input excluding cached tokens, for providers whose input count includes them."""
    if cached > total_input:
        raise ValueError(f"{provider} usage reports {cached} cached tokens of {total_input} input tokens")
    return total_input - cached


def _split_model(model: str | ModelRef, provider: str | None, default_provider: str) -> tuple[str, str]:
    if isinstance(model, ModelRef):
        return model.id, provider or model.provider
    return model, provider or default_provider


def _neutral(
    model: str,
    provider: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
    reasoning: int | None = None,
    latency_ms: int | None = None,
    request_id: str | None = None,
    subscription: bool | None = None,
    tags: Mapping[str, Any],
) -> dict[str, Any]:
    if reasoning is not None and reasoning > output_tokens:
        raise ValueError(f"{provider} usage reports {reasoning} reasoning tokens of {output_tokens} output tokens")
    payload: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_5m_tokens": cache_write_5m,
        "cache_write_1h_tokens": cache_write_1h,
        "usage_schema": USAGE_SCHEMA,
    }
    for key, value in (("reasoning_tokens", reasoning), ("latency_ms", latency_ms), ("request_id", request_id),
                       ("subscription", subscription)):
        if value is not None:
            payload[key] = value
    payload.update(tags)
    return payload


def anthropic_usage_payload(usage: Any, model: str | ModelRef, *, provider: str | None = None,
                            latency_ms: int | None = None, request_id: str | None = None,
                            subscription: bool | None = None, **tags: Any) -> dict[str, Any]:
    """Anthropic Messages ``usage`` (``input_tokens`` already excludes cache) as a schema-2 payload.

    Same counts as ``usage_payload``, plus ``provider`` and ``usage_schema``. Anthropic does not report
    reasoning separately, so ``reasoning_tokens`` is absent.
    """
    model_id, prov = _split_model(model, provider, "anthropic")
    base = usage_payload(usage, model_id)
    return _neutral(model_id, prov, input_tokens=base["input_tokens"], output_tokens=base["output_tokens"],
                    cache_read=base["cache_read_tokens"], cache_write_5m=base["cache_write_5m_tokens"],
                    cache_write_1h=base["cache_write_1h_tokens"], latency_ms=latency_ms, request_id=request_id,
                    subscription=subscription, tags=tags)


def openai_usage_payload(usage: Any, model: str | ModelRef, *, provider: str | None = None,
                         latency_ms: int | None = None, request_id: str | None = None,
                         subscription: bool | None = None, **tags: Any) -> dict[str, Any]:
    """OpenAI chat completions or responses ``usage``, and anything that copies either shape.

    - chat: ``prompt_tokens``, ``completion_tokens``, ``prompt_tokens_details.cached_tokens``,
      ``completion_tokens_details.reasoning_tokens``;
    - responses: ``input_tokens``, ``output_tokens``, ``input_tokens_details.cached_tokens``,
      ``output_tokens_details.reasoning_tokens``.

    ``prompt_tokens`` / ``input_tokens`` *include* the cached tokens, so they are subtracted.
    ``completion_tokens`` already includes reasoning. OpenAI has no cache-write charge.
    """
    model_id, prov = _split_model(model, provider, "openai")
    prompt = _count(_first(usage, "prompt_tokens", "input_tokens"))
    completion = _count(_first(usage, "completion_tokens", "output_tokens"))
    cached = _count(_first(_first(usage, "prompt_tokens_details", "input_tokens_details"), "cached_tokens"))
    reasoning = _first(_first(usage, "completion_tokens_details", "output_tokens_details"), "reasoning_tokens")
    return _neutral(model_id, prov, input_tokens=_uncached(prompt, cached, "openai"), output_tokens=completion,
                    cache_read=cached, reasoning=None if reasoning is None else _count(reasoning),
                    latency_ms=latency_ms, request_id=request_id, subscription=subscription, tags=tags)


def gemini_usage_payload(usage: Any, model: str | ModelRef, *, provider: str | None = None,
                         latency_ms: int | None = None, request_id: str | None = None,
                         subscription: bool | None = None, **tags: Any) -> dict[str, Any]:
    """Gemini ``usageMetadata`` (camelCase from REST, snake_case from the SDK).

    ``promptTokenCount`` includes ``cachedContentTokenCount``, so it is subtracted. Gemini reports
    ``thoughtsTokenCount`` *outside* ``candidatesTokenCount`` but bills it as output, so
    ``output_tokens`` is their sum and ``reasoning_tokens`` the thoughts.
    """
    model_id, prov = _split_model(model, provider, "google")
    prompt = _count(_first(usage, "promptTokenCount", "prompt_token_count"))
    cached = _count(_first(usage, "cachedContentTokenCount", "cached_content_token_count"))
    candidates = _count(_first(usage, "candidatesTokenCount", "candidates_token_count"))
    thoughts = _count(_first(usage, "thoughtsTokenCount", "thoughts_token_count"))
    return _neutral(model_id, prov, input_tokens=_uncached(prompt, cached, "gemini"), output_tokens=candidates + thoughts,
                    cache_read=cached, reasoning=thoughts, latency_ms=latency_ms, request_id=request_id,
                    subscription=subscription, tags=tags)


def deepseek_usage_payload(usage: Any, model: str | ModelRef, *, provider: str | None = None,
                           latency_ms: int | None = None, request_id: str | None = None,
                           subscription: bool | None = None, **tags: Any) -> dict[str, Any]:
    """DeepSeek ``usage``: ``prompt_cache_hit_tokens`` and ``prompt_cache_miss_tokens``.

    A hit is a cache read; a miss is uncached input. If only ``prompt_tokens`` and the hits are
    given, the misses are the difference. ``completion_tokens`` includes
    ``completion_tokens_details.reasoning_tokens``.
    """
    model_id, prov = _split_model(model, provider, "deepseek")
    hit = _count(_first(usage, "prompt_cache_hit_tokens"))
    miss = _first(usage, "prompt_cache_miss_tokens")
    uncached = _count(miss) if miss is not None else _uncached(_count(_first(usage, "prompt_tokens")), hit, "deepseek")
    reasoning = _first(_first(usage, "completion_tokens_details"), "reasoning_tokens")
    return _neutral(model_id, prov, input_tokens=uncached, output_tokens=_count(_first(usage, "completion_tokens")),
                    cache_read=hit, reasoning=None if reasoning is None else _count(reasoning),
                    latency_ms=latency_ms, request_id=request_id, subscription=subscription, tags=tags)


def ollama_usage_payload(usage: Any, model: str | ModelRef, *, provider: str | None = None,
                         latency_ms: int | None = None, request_id: str | None = None,
                         subscription: bool | None = None, **tags: Any) -> dict[str, Any]:
    """Ollama ``/api/chat`` or ``/api/generate`` response: ``prompt_eval_count`` and ``eval_count``.

    No cache buckets. ``total_duration`` (nanoseconds), if present and ``latency_ms`` is not given,
    becomes ``latency_ms``.
    """
    model_id, prov = _split_model(model, provider, "ollama")
    if latency_ms is None:
        total_ns = _first(usage, "total_duration")
        latency_ms = None if total_ns is None else _count(total_ns) // 1_000_000
    return _neutral(model_id, prov, input_tokens=_count(_first(usage, "prompt_eval_count")),
                    output_tokens=_count(_first(usage, "eval_count")), latency_ms=latency_ms,
                    request_id=request_id, subscription=subscription, tags=tags)


USAGE_MAPPERS: Mapping[str, Callable[..., dict[str, Any]]] = {
    "anthropic": anthropic_usage_payload,
    "openai": openai_usage_payload,
    "gemini": gemini_usage_payload,
    "deepseek": deepseek_usage_payload,
    "ollama": ollama_usage_payload,
}


def provider_usage_payload(shape: str, usage: Any, model: str | ModelRef, **kwargs: Any) -> dict[str, Any]:
    """Map ``usage`` of the given response ``shape`` (a key of ``USAGE_MAPPERS``) to a payload.

    ``shape`` names the response format, not the vendor: an OpenAI-compatible hub is ``"openai"`` with
    ``provider="openrouter"``. Raises ValueError for an unknown shape.
    """
    try:
        mapper = USAGE_MAPPERS[shape]
    except KeyError:
        raise ValueError(f"unknown usage shape {shape!r}; expected one of {sorted(USAGE_MAPPERS)}") from None
    return mapper(usage, model, **kwargs)
