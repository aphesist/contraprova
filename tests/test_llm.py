import io
import json
import urllib.error
import urllib.request
from email.message import Message as HeaderMap

import pytest

from contraprova import llm
from contraprova.interfaces import HAIKU, OPUS, SONNET, Message
from contraprova.llm import (
    ANTHROPIC_VERSION,
    AnthropicClient,
    Budget,
    BudgetExceeded,
    FakeLLM,
    LLMError,
    MissingAPIKey,
    RecordingClient,
    usage_recorder,
)
from contraprova.log import EventLog
from contraprova.pricing import PRICE_TABLES, request_cost
from contraprova.projections import usage_records

KEY = "test-key-not-real"
TS = "2026-11-11T00:00:00.000Z"

OK_RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": HAIKU,
    "content": [
        {"type": "thinking", "thinking": "", "signature": "x"},
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
    ],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 50,
        "cache_creation": {"ephemeral_5m_input_tokens": 50, "ephemeral_1h_input_tokens": 0},
    },
}


def headers(**kv):
    h = HeaderMap()
    for k, v in kv.items():
        h[k.replace("_", "-")] = v
    return h


class FakeResponse:
    def __init__(self, body, hdrs=None):
        self._raw = json.dumps(body).encode()
        self.headers = hdrs or headers(request_id="req_abc")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, etype, retry_after=None):
    body = json.dumps({"type": "error", "error": {"type": etype, "message": "nope"}}).encode()
    h = headers(request_id="req_err")
    if retry_after is not None:
        h["retry-after"] = str(retry_after)
    return urllib.error.HTTPError(llm.API_URL, code, "err", h, io.BytesIO(body))


@pytest.fixture
def transport(monkeypatch):
    """Replace urlopen with a queue of outcomes; record every request."""
    state = {"requests": [], "outcomes": []}

    def fake_urlopen(req, timeout=None):
        state["requests"].append(req)
        out = state["outcomes"].pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CONTRAPROVA_KEY_WORKER", raising=False)
    monkeypatch.delenv("CONTRAPROVA_KEY_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("CONTRAPROVA_KEY_WORKER", KEY)
    return state


def make(**kw):
    sleeps = []
    kw.setdefault("sleep", sleeps.append)
    c = AnthropicClient("worker", **kw)
    return c, sleeps


MSGS = [Message("user", "do the thing")]


def test_request_shape(transport):
    transport["outcomes"].append(FakeResponse(OK_RESPONSE))
    c, _ = make()
    out = c.complete(model=HAIKU, system="you are a worker", messages=MSGS, max_tokens=2000, effort="low",
                     tags={"task_id": "t1", "arm": "D"})
    req = transport["requests"][0]
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.get_method() == "POST"
    h = {k.lower(): v for k, v in req.header_items()}
    assert h["x-api-key"] == KEY
    assert h["anthropic-version"] == ANTHROPIC_VERSION == "2026-10-21"
    assert h["content-type"] == "application/json"
    body = json.loads(req.data)
    assert body == {
        "model": HAIKU,
        "max_tokens": 2000,
        "system": [{"type": "text", "text": "you are a worker", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "do the thing"}],
        "output_config": {"effort": "low"},
    }
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in body
    assert out.text == "hello world"
    assert out.model == HAIKU
    assert out.request_id == "req_abc"
    assert out.usage == {
        "model": HAIKU, "input_tokens": 12, "output_tokens": 34, "cache_read_tokens": 100,
        "cache_write_5m_tokens": 50, "cache_write_1h_tokens": 0,
        "role": "worker", "task_id": "t1", "arm": "D",
    }


def test_effort_omitted_and_1h_ttl_and_no_cache(transport):
    transport["outcomes"] += [FakeResponse(OK_RESPONSE), FakeResponse(OK_RESPONSE), FakeResponse(OK_RESPONSE)]
    c, _ = make(cache_ttl="1h")
    c.complete(model=OPUS, system="s", messages=MSGS, max_tokens=10)
    body = json.loads(transport["requests"][0].data)
    assert "output_config" not in body
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    c2, _ = make(cache_system=False)
    c2.complete(model=OPUS, system="s", messages=MSGS, max_tokens=10)
    assert "cache_control" not in json.loads(transport["requests"][1].data)["system"][0]
    c2.complete(model=OPUS, system="", messages=MSGS, max_tokens=10)
    assert "system" not in json.loads(transport["requests"][2].data)


def test_identical_calls_are_byte_identical(transport):
    transport["outcomes"] += [FakeResponse(OK_RESPONSE), FakeResponse(OK_RESPONSE)]
    c, _ = make()
    for _ in range(2):
        c.complete(model=HAIKU, system="s", messages=MSGS, max_tokens=10)
    assert transport["requests"][0].data == transport["requests"][1].data


def test_usage_with_1h_breakdown(transport):
    resp = dict(OK_RESPONSE, usage={"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 30,
                                    "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20}})
    transport["outcomes"].append(FakeResponse(resp))
    c, _ = make()
    u = c.complete(model=HAIKU, system="s", messages=MSGS, max_tokens=10).usage
    assert (u["cache_write_5m_tokens"], u["cache_write_1h_tokens"], u["cache_read_tokens"]) == (10, 20, 0)


def test_key_fallback_and_role_specific_env(transport, monkeypatch):
    monkeypatch.delenv("CONTRAPROVA_KEY_WORKER")
    monkeypatch.delenv("PROJECTS_ANTHROPIC_KEY", raising=False)
    with pytest.raises(MissingAPIKey) as ei:
        make()[0].complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    assert "CONTRAPROVA_KEY_WORKER" in str(ei.value) and "ANTHROPIC_API_KEY" in str(ei.value)
    assert transport["requests"] == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fallback-key")
    monkeypatch.setenv("CONTRAPROVA_KEY_ORCHESTRATOR", "orch-key")
    transport["outcomes"] += [FakeResponse(OK_RESPONSE), FakeResponse(OK_RESPONSE)]
    make()[0].complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    AnthropicClient("orchestrator").complete(model=OPUS, system="", messages=MSGS, max_tokens=5)
    keys = [dict((k.lower(), v) for k, v in r.header_items())["x-api-key"] for r in transport["requests"]]
    assert keys == ["fallback-key", "orch-key"]


def test_key_never_stored_or_shown(transport):
    c, _ = make()
    assert KEY not in repr(c)
    assert KEY not in json.dumps({k: repr(v) for k, v in vars(c).items()})
    transport["outcomes"].append(http_error(401, "authentication_error"))
    with pytest.raises(LLMError) as ei:
        c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    assert KEY not in str(ei.value) and ei.value.status == 401


def test_retries_then_succeeds(transport):
    transport["outcomes"] += [
        http_error(429, "rate_limit_error"),
        http_error(529, "overloaded_error"),
        http_error(500, "api_error"),
        urllib.error.URLError(ConnectionResetError()),
        FakeResponse(OK_RESPONSE),
    ]
    c, sleeps = make(backoff_base=1.0, backoff_max=60.0)
    assert c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5).text == "hello world"
    assert len(transport["requests"]) == 5
    assert len(sleeps) == 4
    # exponential: base * 2^attempt, plus up to 25% jitter
    for i, s in enumerate(sleeps):
        assert 2**i <= s <= 2**i * 1.25


def test_retry_after_is_honoured_and_capped(transport):
    transport["outcomes"] += [http_error(429, "rate_limit_error", retry_after=7), http_error(429, "rate_limit_error", retry_after=999),
                              FakeResponse(OK_RESPONSE)]
    c, sleeps = make(backoff_max=30.0)
    c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    assert sleeps[0] >= 7 and sleeps[1] == 30.0


def test_retries_are_bounded(transport):
    transport["outcomes"] += [http_error(529, "overloaded_error") for _ in range(10)]
    c, sleeps = make(max_retries=3)
    with pytest.raises(LLMError) as ei:
        c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    assert len(transport["requests"]) == 4 and len(sleeps) == 3
    assert ei.value.status == 529 and ei.value.error_type == "overloaded_error"


@pytest.mark.parametrize("code,etype", [(400, "invalid_request_error"), (401, "authentication_error"),
                                        (403, "permission_error"), (404, "not_found_error")])
def test_client_errors_not_retried(transport, code, etype):
    transport["outcomes"].append(http_error(code, etype))
    c, sleeps = make()
    with pytest.raises(LLMError) as ei:
        c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    assert (ei.value.status, ei.value.error_type, ei.value.request_id) == (code, etype, "req_err")
    assert len(transport["requests"]) == 1 and sleeps == []


def test_overloaded_error_in_200_body_is_retried(transport):
    transport["outcomes"] += [FakeResponse({"type": "error", "error": {"type": "overloaded_error", "message": "x"}}),
                              FakeResponse(OK_RESPONSE)]
    c, sleeps = make()
    c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    assert len(sleeps) == 1


def test_validation(transport):
    c, _ = make()
    with pytest.raises(ValueError):
        c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5, effort="extreme")
    with pytest.raises(ValueError):
        c.complete(model=HAIKU, system="", messages=[Message("user", "a"), Message("assistant", "prefill")], max_tokens=5)
    with pytest.raises(ValueError):
        c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=0)
    with pytest.raises(ValueError):
        AnthropicClient(cache_ttl="10m")
    assert transport["requests"] == []


# ------------------------------------------------------------------ budget


def test_budget_charges_actual_usage(transport):
    transport["outcomes"].append(FakeResponse(OK_RESPONSE))
    b = Budget(cap_usd=1.0)
    c, _ = make(budget=b)
    out = c.complete(model=HAIKU, system="s", messages=MSGS, max_tokens=100)
    assert b.calls == 1
    assert b.spent_usd == pytest.approx(request_cost(out.usage, PRICE_TABLES["anthropic-2026-11"]))


def test_budget_blocks_before_the_call(transport):
    # Opus output is $20/MTok: 16000 max tokens is up to $0.32 of output alone.
    b = Budget(cap_usd=0.30)
    c, _ = make(budget=b)
    with pytest.raises(BudgetExceeded):
        c.complete(model=OPUS, system="s", messages=MSGS, max_tokens=16000)
    assert transport["requests"] == [] and b.spent_usd == 0
    transport["outcomes"].append(FakeResponse(dict(OK_RESPONSE, model=OPUS)))
    c.complete(model=OPUS, system="s", messages=MSGS, max_tokens=1000)
    assert len(transport["requests"]) == 1


def test_budget_shared_and_exhausts(transport):
    b = Budget(cap_usd=0.01, spent_usd=0.0099)
    c, _ = make(budget=b)
    with pytest.raises(BudgetExceeded):
        c.complete(model=HAIKU, system="", messages=MSGS, max_tokens=4000)
    assert transport["requests"] == []


def test_budget_refuses_unpriced_model(transport):
    c, _ = make(budget=Budget(cap_usd=100))
    with pytest.raises(BudgetExceeded):
        c.complete(model="claude-unknown", system="", messages=MSGS, max_tokens=5)


def test_estimate_is_an_upper_bound_for_fake_usage():
    msgs = [Message("user", "x" * 4000)]
    est = llm.estimate_cost(model=HAIKU, system="y" * 8000, messages=msgs, max_tokens=500,
                            table=PRICE_TABLES["anthropic-2026-11"])
    f = FakeLLM(["z" * 4000])
    u = f.complete(model=HAIKU, system="y" * 8000, messages=msgs, max_tokens=500).usage
    assert est >= request_cost(u, PRICE_TABLES["anthropic-2026-11"])


# ------------------------------------------------------------------ on_usage, recorder, fake


def test_on_usage_callback_appends_to_log(transport, tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    transport["outcomes"].append(FakeResponse(OK_RESPONSE))
    c, _ = make(on_usage=usage_recorder(log))
    out = c.complete(model=HAIKU, system="s", messages=MSGS, max_tokens=5, tags={"task_id": "t1"})
    recs = usage_records(log.events())
    assert recs == [out.usage]


def test_fake_is_deterministic_and_scripted():
    def run():
        f = FakeLLM(["one", "two"])
        a = f.complete(model=HAIKU, system="sys", messages=MSGS, max_tokens=50, tags={"task_id": "t"})
        b = f.complete(model=HAIKU, system="sys", messages=MSGS, max_tokens=50)
        with pytest.raises(FakeLLM.ScriptExhausted):
            f.complete(model=HAIKU, system="sys", messages=MSGS, max_tokens=50)
        return a, b, f

    (a1, b1, f1), (a2, b2, _) = run(), run()
    assert (a1, b1) == (a2, b2)
    assert (a1.text, b1.text) == ("one", "two")
    assert a1.usage["cache_write_5m_tokens"] == 1 and a1.usage["cache_read_tokens"] == 0
    assert b1.usage["cache_read_tokens"] == 1 and b1.usage["cache_write_5m_tokens"] == 0
    assert a1.usage["task_id"] == "t" and a1.usage["role"] == "worker"
    assert len(f1.calls) == 3


def test_fake_callable_script_and_budget():
    b = Budget(cap_usd=1.0)
    f = FakeLLM(lambda model, system, messages: f"{model}:{messages[-1].content}", budget=b)
    out = f.complete(model=HAIKU, system="", messages=MSGS, max_tokens=50)
    assert out.text == f"{HAIKU}:do the thing"
    assert b.calls == 1 and b.spent_usd > 0


def test_recording_client(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    rc = RecordingClient(FakeLLM(["a", "b"]), log, actor="worker-1")
    rc.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5, tags={"task_id": "t1", "arm": "B"})
    rc.complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    evs = log.events()
    assert [e.type for e in evs] == ["usage.recorded"] * 2
    assert evs[0].actor == "worker-1"
    assert evs[0].payload["arm"] == "B" and evs[0].payload["request_id"] == "fake_000001"
    assert log.verify() == 2


def test_shared_project_key_beats_fallback_and_base_url_is_pinned(transport, monkeypatch):
    monkeypatch.delenv("CONTRAPROVA_KEY_WORKER")
    monkeypatch.setenv("PROJECTS_ANTHROPIC_KEY", "shared-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fallback-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    transport["outcomes"] += [FakeResponse(OK_RESPONSE)]
    make()[0].complete(model=HAIKU, system="", messages=MSGS, max_tokens=5)
    req = transport["requests"][-1]
    assert dict((k.lower(), v) for k, v in req.header_items())["x-api-key"] == "shared-key"
    assert req.full_url == "https://api.anthropic.com/v1/messages"


# ------------------------------------------------------------------ message prefix caching


def test_cache_upto_marks_the_stable_prefix():
    c, _ = make()
    body = c.build_body(model=HAIKU, system="s", messages=[Message("user", "PREFIX|tail", cache_upto=7)],
                        max_tokens=10)
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "PREFIX|", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "tail"}]}]
    whole = c.build_body(model=HAIKU, system="s", messages=[Message("user", "all", cache_upto=99)], max_tokens=10)
    assert whole["messages"][0]["content"] == [{"type": "text", "text": "all", "cache_control": {"type": "ephemeral"}}]
    off, _ = make(cache_system=False)
    plain = off.build_body(model=HAIKU, system="s", messages=[Message("user", "PREFIX|tail", cache_upto=7)],
                           max_tokens=10)
    assert plain["messages"] == [{"role": "user", "content": "PREFIX|tail"}]
    too_many = [Message("user", "a", 1), Message("assistant", "b", 1), Message("user", "c", 1),
                Message("assistant", "d", 1), Message("user", "e", 1)]
    with pytest.raises(ValueError, match="breakpoints"):
        c.build_body(model=HAIKU, system="", messages=too_many, max_tokens=10)
    with pytest.raises(ValueError, match="breakpoints"):
        c.build_body(model=HAIKU, system="s", messages=too_many[1:], max_tokens=10)


def test_fake_reads_a_repeated_marked_prefix():
    f = FakeLLM(lambda *a: "ok")
    prefix = "x" * 400
    a = f.complete(model=HAIKU, system="", messages=[Message("user", prefix + "one", len(prefix))], max_tokens=5)
    b = f.complete(model=HAIKU, system="", messages=[Message("user", prefix + "two", len(prefix))], max_tokens=5)
    c = f.complete(model=SONNET, system="", messages=[Message("user", prefix + "two", len(prefix))], max_tokens=5)
    assert (a.usage["cache_write_5m_tokens"], a.usage["cache_read_tokens"]) == (100, 0)
    assert (b.usage["cache_write_5m_tokens"], b.usage["cache_read_tokens"]) == (0, 100)
    assert c.usage["cache_read_tokens"] == 0  # caches are per model
    assert a.usage["input_tokens"] == 1


def test_estimate_counts_a_marked_prefix_as_a_cache_write():
    from contraprova.llm import estimate_cost
    from contraprova.pricing import DEFAULT_TABLE

    table = PRICE_TABLES[DEFAULT_TABLE]
    text = "y" * 10_000
    plain = estimate_cost(model=OPUS, system="", messages=[Message("user", text)], max_tokens=1, table=table)
    marked = estimate_cost(model=OPUS, system="", messages=[Message("user", text, len(text))], max_tokens=1,
                           table=table)
    assert marked > plain
