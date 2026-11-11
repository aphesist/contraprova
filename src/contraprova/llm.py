"""Claude Messages API client (standard library only), a scripted fake, and a recorder.

``AnthropicClient`` sends one non-streaming ``POST /v1/messages`` per call:

    headers: x-api-key, anthropic-version: 2026-10-21, content-type: application/json
    body:    {"model", "max_tokens",
              "system": [{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"[, "ttl": "1h"]}}],
              "messages": [{"role": "user"|"assistant", "content": str | [text blocks]}, ...],
              "output_config": {"effort": "low"|"medium"|"high"|"xhigh"|"max"}}   # only when effort is given

A message with ``cache_upto`` > 0 is sent as two text blocks, the first (its stable prefix) carrying
the same ``cache_control``, so a later request that repeats that prefix reads it from the cache.
At most four breakpoints are allowed per request. Caching is a byte-exact prefix match over
system then messages, and prefixes shorter than the model's minimum (512 tokens on Opus 5.5,
Sonnet 5.5 and Haiku 5.5) are silently not cached: no write premium, no read.

No ``thinking`` field is sent: on Opus 5.5, Sonnet 5.5 and Haiku 5.5 omitting it runs adaptive
thinking, and effort is the depth control. No ``temperature``/``top_p``/``top_k``: API version
2026-10-21 rejects them. The returned text is the concatenation of the ``text`` content blocks
(thinking blocks are skipped). Token counts come straight from the response ``usage`` object and
become a ``usage.recorded`` payload through :func:`contraprova.usage.usage_payload`.

The API key is read from the environment at the moment of each request and is never stored on
the client, logged, or put in an exception message.
"""

from __future__ import annotations

import json
import math
import os
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .interfaces import Completion, LLMClient, Message
from .pricing import (
    DEFAULT_TABLE,
    PRICE_TABLES,
    ModelPrice,
    request_cost,
)
from .usage import usage_payload

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CACHE_TTLS = frozenset({"5m", "1h"})
MAX_CACHE_BREAKPOINTS = 4
# 408/409/429 and every 5xx (529 is overloaded_error) are retryable; other 4xx are not.
RETRYABLE_STATUS = frozenset({408, 409, 429})
# Shared project key first; ANTHROPIC_API_KEY only as a local fallback. The base URL is pinned to
# API_URL on purpose: ANTHROPIC_BASE_URL is never read, because some runtimes point it elsewhere.
SHARED_KEY_ENV = "PROJECTS_ANTHROPIC_KEY"
FALLBACK_KEY_ENV = "ANTHROPIC_API_KEY"
# Conservative characters-per-token for the pre-call spend estimate (real tokenizers average ~3.5-4).
CHARS_PER_TOKEN_ESTIMATE = 2.5

UsageCallback = Callable[[Mapping[str, Any]], None]


# ------------------------------------------------------------------ errors


class LLMError(Exception):
    """The API returned an error, or retries ran out."""

    def __init__(self, message: str, *, status: int | None = None, error_type: str | None = None,
                 request_id: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.request_id = request_id
        self.retryable = retryable


class MissingAPIKey(LLMError):
    """None of the configured environment variables holds a key."""


class BudgetExceeded(Exception):
    """The next call could push spend past the cap, so it was not made."""


# ------------------------------------------------------------------ budget


@dataclass
class Budget:
    """A hard spending cap in USD, shared by every client it is injected into.

    ``check`` runs before a call with a worst-case estimate and raises :class:`BudgetExceeded` if
    ``spent + estimate`` would exceed ``cap_usd``. ``charge`` runs after the call with the cost of
    the tokens the API actually reported. Spend is re-priced from tokens, never taken on trust.
    """

    cap_usd: float
    spent_usd: float = 0.0
    calls: int = 0

    @property
    def remaining_usd(self) -> float:
        return self.cap_usd - self.spent_usd

    def check(self, estimate_usd: float) -> None:
        if self.spent_usd + estimate_usd > self.cap_usd:
            raise BudgetExceeded(
                f"call estimated at ${estimate_usd:.6f} would exceed the cap: "
                f"spent ${self.spent_usd:.6f} of ${self.cap_usd:.6f}"
            )

    def charge(self, usd: float) -> None:
        self.spent_usd += usd
        self.calls += 1


def estimate_tokens(text: str) -> int:
    """Deliberately high token estimate for a string (used only for the budget guard)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN_ESTIMATE) + 8 if text else 0


def estimate_cost(
    *,
    model: str,
    system: str,
    messages: Sequence[Message],
    max_tokens: int,
    table: Mapping[str, ModelPrice],
    cache_ttl: str | None = "5m",
) -> float:
    """Worst-case USD for one call: no cache hits, the system prompt written to cache at the
    write premium, every other input token at the full rate, and all ``max_tokens`` generated."""
    sys_tok = estimate_tokens(system)
    msg_tok = sum(estimate_tokens(m.content) + 4 for m in messages)
    # A marked message prefix may be written to the cache at the write premium too.
    pre_tok = 0 if cache_ttl is None else sum(
        min(estimate_tokens(m.content[: m.cache_upto]), estimate_tokens(m.content) + 4)
        for m in messages if m.cache_upto > 0)
    u: dict[str, Any] = {"model": model, "input_tokens": msg_tok - pre_tok, "output_tokens": max_tokens}
    if cache_ttl == "1h":
        u["cache_write_1h_tokens"] = sys_tok + pre_tok
    elif cache_ttl == "5m":
        u["cache_write_5m_tokens"] = sys_tok + pre_tok
    else:
        u["input_tokens"] += sys_tok
    # The token estimate errs high, so if it stays under a long-context threshold the real prompt
    # does too; above it, request_cost already switches to the long-context rates.
    cost = request_cost(u, table)
    return cost


# ------------------------------------------------------------------ shared plumbing


def _validate(model: str, max_tokens: int, effort: str | None, messages: Sequence[Message]) -> None:
    if not model:
        raise ValueError("model is required")
    if not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"effort must be one of {sorted(EFFORTS)}, got {effort!r}")
    if not messages:
        raise ValueError("messages must not be empty")
    if messages[-1].role != "user":
        # Assistant prefill returns a 400 on every current model.
        raise ValueError("the last message must have role 'user'")
    for m in messages:
        if m.role not in ("user", "assistant"):
            raise ValueError(f"message role must be 'user' or 'assistant', got {m.role!r}")


class _Metered:
    """Budget guard and usage callback shared by the real client and the fake."""

    def __init__(self, budget: Budget | None, on_usage: UsageCallback | None,
                 price_table: str | Mapping[str, ModelPrice], cache_ttl: str | None):
        self.budget = budget
        self.on_usage = on_usage
        self.table = PRICE_TABLES[price_table] if isinstance(price_table, str) else dict(price_table)
        self.cache_ttl = cache_ttl

    def _guard(self, model: str, system: str, messages: Sequence[Message], max_tokens: int) -> None:
        if self.budget is None:
            return
        if model not in self.table:
            raise BudgetExceeded(f"no price for model {model!r}; refusing to spend without one")
        ttl = self.cache_ttl if system else None
        self.budget.check(estimate_cost(model=model, system=system, messages=messages,
                                        max_tokens=max_tokens, table=self.table, cache_ttl=ttl))

    def _settle(self, payload: Mapping[str, Any]) -> None:
        if self.budget is not None:
            self.budget.charge(request_cost(payload, self.table))
        if self.on_usage is not None:
            self.on_usage(payload)


# ------------------------------------------------------------------ the real client


class AnthropicClient(_Metered):
    """:class:`~contraprova.interfaces.LLMClient` over the Claude Messages API, via urllib.

    ``role`` selects the key: ``CONTRAPROVA_KEY_<ROLE>`` (or ``key_env`` if given), then
    ``PROJECTS_ANTHROPIC_KEY``, then ``ANTHROPIC_API_KEY``. The role is also added to every usage payload as the ``role`` tag
    unless the caller's tags set one.
    """

    def __init__(
        self,
        role: str = "worker",
        *,
        key_env: str | None = None,
        budget: Budget | None = None,
        on_usage: UsageCallback | None = None,
        cache_system: bool = True,
        cache_ttl: str = "5m",
        price_table: str | Mapping[str, ModelPrice] = DEFAULT_TABLE,
        base_url: str = API_URL,
        timeout: float = 600.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        if cache_ttl not in CACHE_TTLS:
            raise ValueError(f"cache_ttl must be one of {sorted(CACHE_TTLS)}")
        super().__init__(budget, on_usage, price_table, cache_ttl if cache_system else None)
        self.role = role
        self.key_envs: tuple[str, ...] = (key_env or f"CONTRAPROVA_KEY_{role.upper()}", SHARED_KEY_ENV, FALLBACK_KEY_ENV)
        self.cache_system = cache_system
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._sleep = sleep
        self._rng = rng or random.Random()

    def __repr__(self) -> str:  # never show anything key-related beyond the variable names
        return f"AnthropicClient(role={self.role!r}, key_envs={self.key_envs!r})"

    # -- request construction (pure; also used by forks to check prefix stability)

    def _cache_control(self) -> dict[str, Any]:
        cc: dict[str, Any] = {"type": "ephemeral"}
        if self.cache_ttl == "1h":
            cc["ttl"] = "1h"
        return cc

    def _message(self, m: Message) -> dict[str, Any]:
        n = min(max(m.cache_upto, 0), len(m.content))
        if not self.cache_system or n == 0:
            return {"role": m.role, "content": m.content}
        blocks: list[dict[str, Any]] = [{"type": "text", "text": m.content[:n], "cache_control": self._cache_control()}]
        if m.content[n:]:
            blocks.append({"type": "text", "text": m.content[n:]})
        return {"role": m.role, "content": blocks}

    def build_body(self, *, model: str, system: str, messages: Sequence[Message], max_tokens: int,
                   effort: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens}
        breakpoints = 0
        if system:
            block: dict[str, Any] = {"type": "text", "text": system}
            if self.cache_system:
                block["cache_control"] = self._cache_control()
                breakpoints += 1
            body["system"] = [block]
        body["messages"] = [self._message(m) for m in messages]
        if self.cache_system:
            breakpoints += sum(1 for m in messages if m.cache_upto > 0 and m.content)
        if breakpoints > MAX_CACHE_BREAKPOINTS:
            raise ValueError(f"{breakpoints} cache breakpoints; the API allows {MAX_CACHE_BREAKPOINTS}")
        if effort is not None:
            body["output_config"] = {"effort": effort}
        return body

    def encode_body(self, body: Mapping[str, Any]) -> bytes:
        """Deterministic bytes, so an identical conversation is an identical (cache-hot) request."""
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _api_key(self) -> str:
        for name in self.key_envs:
            value = os.environ.get(name)
            if value:
                return value
        raise MissingAPIKey(f"no API key: set one of {', '.join(self.key_envs)}")

    # -- the call

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Message],
        max_tokens: int,
        effort: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Completion:
        _validate(model, max_tokens, effort, messages)
        self._guard(model, system, messages, max_tokens)
        data = self.encode_body(self.build_body(model=model, system=system, messages=messages,
                                                max_tokens=max_tokens, effort=effort))
        resp, request_id = self._post(data)
        served = resp.get("model") or model
        all_tags = {"role": self.role, **dict(tags or {})}
        payload = usage_payload(resp.get("usage") or {}, served, **all_tags)
        self._settle(payload)
        text = "".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")
        return Completion(text=text, model=served, usage=payload,
                          stop_reason=resp.get("stop_reason") or "end_turn",
                          request_id=request_id or resp.get("id"))

    def _post(self, data: bytes) -> tuple[dict[str, Any], str | None]:
        attempt = 0
        while True:
            # Built per attempt so the key lives only in this frame for the duration of one request.
            req = urllib.request.Request(
                self.base_url,
                data=data,
                method="POST",
                headers={
                    "x-api-key": self._api_key(),
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                    "accept": "application/json",
                },
            )
            retry_after: float | None = None
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    rid = r.headers.get("request-id")
                body = json.loads(raw.decode("utf-8"))
                if isinstance(body, dict) and body.get("type") == "error":
                    err = body.get("error") or {}
                    etype = err.get("type")
                    exc = LLMError(f"API error: {etype}: {err.get('message', '')}", error_type=etype,
                                   request_id=rid, retryable=etype in ("overloaded_error", "rate_limit_error", "api_error"))
                else:
                    return body, rid
            except urllib.error.HTTPError as e:
                exc = self._http_error(e)
                ra = e.headers.get("retry-after") if e.headers is not None else None
                try:
                    retry_after = float(ra) if ra is not None else None
                except ValueError:
                    retry_after = None
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
                reason = getattr(e, "reason", e)
                exc = LLMError(f"connection error: {type(reason).__name__}", retryable=True)
            if not exc.retryable or attempt >= self.max_retries:
                if exc.retryable:
                    raise LLMError(f"gave up after {attempt + 1} attempts: {exc}", status=exc.status,
                                   error_type=exc.error_type, request_id=exc.request_id) from exc
                raise exc
            self._sleep(self._delay(attempt, retry_after))
            attempt += 1

    def _http_error(self, e: urllib.error.HTTPError) -> LLMError:
        status = e.code
        etype, msg, rid = None, "", e.headers.get("request-id") if e.headers is not None else None
        try:
            body = json.loads(e.read().decode("utf-8"))
            err = body.get("error") or {}
            etype, msg = err.get("type"), err.get("message", "")
            rid = rid or body.get("request_id")
        except Exception:
            pass
        retryable = status in RETRYABLE_STATUS or status >= 500 or etype == "overloaded_error"
        return LLMError(f"HTTP {status} {etype or ''}: {msg}".strip(), status=status, error_type=etype,
                        request_id=rid, retryable=retryable)

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        backoff = min(self.backoff_base * (2 ** attempt), self.backoff_max)
        delay = backoff + self._rng.uniform(0, backoff / 4)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return min(delay, self.backoff_max)


# ------------------------------------------------------------------ fake


Script = Callable[[str, str, Sequence[Message]], str]


class FakeLLM(_Metered):
    """Deterministic stand-in for tests and dry runs. Makes no network calls.

    ``script`` is either a sequence of reply strings, returned in order (``ScriptExhausted`` once
    they run out), or a function ``(model, system, messages) -> str``. Usage is synthetic but
    deterministic: about four characters per token, with the system prompt reported as a cache
    write on first sight and a cache read afterwards, and likewise a message's marked prefix
    (``cache_upto``), keyed by model, system and the prefix. ``calls`` keeps every request.
    """

    class ScriptExhausted(Exception):
        pass

    def __init__(
        self,
        script: Sequence[str] | Script = (),
        *,
        role: str = "worker",
        budget: Budget | None = None,
        on_usage: UsageCallback | None = None,
        price_table: str | Mapping[str, ModelPrice] = DEFAULT_TABLE,
    ):
        super().__init__(budget, on_usage, price_table, "5m")
        self.role = role
        self._fn = script if callable(script) else None
        self._replies = list(script) if not callable(script) else []
        self._seen_systems: set[str] = set()
        self._seen_prefixes: set[tuple[str, str, str]] = set()
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _tok(text: str) -> int:
        return math.ceil(len(text) / 4)

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Message],
        max_tokens: int,
        effort: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Completion:
        _validate(model, max_tokens, effort, messages)
        self._guard(model, system, messages, max_tokens)
        idx = len(self.calls)
        self.calls.append({"model": model, "system": system, "messages": tuple(messages),
                           "max_tokens": max_tokens, "effort": effort, "tags": dict(tags or {})})
        if self._fn is not None:
            text = self._fn(model, system, messages)
        elif idx < len(self._replies):
            text = self._replies[idx]
        else:
            raise FakeLLM.ScriptExhausted(f"no scripted reply for call {idx + 1}")
        out_tok = min(self._tok(text), max_tokens)
        stop = "end_turn" if self._tok(text) <= max_tokens else "max_tokens"
        sys_tok = self._tok(system)
        hit = system in self._seen_systems
        self._seen_systems.add(system)
        read, write = (sys_tok, 0) if hit else (0, sys_tok)
        plain = 0
        for m in messages:
            n = min(max(m.cache_upto, 0), len(m.content))
            pre, rest = m.content[:n], m.content[n:]
            if not pre:
                plain += self._tok(m.content)
                continue
            plain += self._tok(rest)
            key = (model, system, pre)
            if key in self._seen_prefixes:
                read += self._tok(pre)
            else:
                write += self._tok(pre)
                self._seen_prefixes.add(key)
        raw_usage = {
            "input_tokens": plain,
            "output_tokens": out_tok,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
        }
        payload = usage_payload(raw_usage, model, **{"role": self.role, **dict(tags or {})})
        self._settle(payload)
        return Completion(text=text, model=model, usage=payload, stop_reason=stop,
                          request_id=f"fake_{idx + 1:06d}")


# ------------------------------------------------------------------ recorder


class RecordingClient:
    """Wraps any LLMClient and appends one ``usage.recorded`` event per successful call.

    The event payload is exactly ``Completion.usage`` (a :func:`usage_payload` dict: model,
    token counts and the tags), plus ``request_id`` when the API returned one.
    """

    def __init__(self, inner: LLMClient, log: Any, *, actor: str = "llm"):
        self.inner = inner
        self.log = log
        self.actor = actor

    def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Message],
        max_tokens: int,
        effort: str | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> Completion:
        c = self.inner.complete(model=model, system=system, messages=messages,
                                max_tokens=max_tokens, effort=effort, tags=tags)
        payload = dict(c.usage)
        if c.request_id and "request_id" not in payload:
            payload["request_id"] = c.request_id
        self.log.append("usage.recorded", self.actor, payload)
        return c


def usage_recorder(log: Any, actor: str = "llm") -> UsageCallback:
    """An ``on_usage`` callback that appends each payload to ``log`` as ``usage.recorded``."""
    return lambda payload: log.append("usage.recorded", actor, dict(payload))
