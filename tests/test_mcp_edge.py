import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from contraprova.log import EventLog
from contraprova.mcp_edge import (
    KERNEL_ONLY,
    MAX_LINE_BYTES,
    MAX_PAYLOAD_BYTES,
    MODERN_VERSION,
    McpEdge,
)

META = {
    "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
}
TS = "2026-11-11T00:00:00.000Z"


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "log.jsonl"


@pytest.fixture
def server(log_path):
    return McpEdge(log_path)


class Client:
    def __init__(self, server):
        self.server = server
        self.next_id = 0

    def raw(self, line):
        out = self.server.handle_line(line)
        return None if out is None else json.loads(out)

    def request(self, method, params=None, meta=True):
        self.next_id += 1
        params = dict(params or {})
        if meta:
            params["_meta"] = META
        resp = self.raw(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}))
        assert resp["id"] == self.next_id
        return resp

    def call(self, name, arguments):
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in resp, resp
        res = resp["result"]
        assert res["resultType"] == "complete"
        assert json.loads(res["content"][0]["text"]) == res["structuredContent"]
        return res

    def submit(self, type_, payload, actor="worker-1"):
        return self.call("submit_intent", {"type": type_, "actor": actor, "payload": payload})


@pytest.fixture
def client(server):
    return Client(server)


# ------------------------------------------------------------------ protocol


def test_discover(client):
    res = client.request("server/discover")["result"]
    assert res["resultType"] == "complete"
    assert res["supportedVersions"] == [MODERN_VERSION]
    assert "tools" in res["capabilities"]
    assert res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "contraprova"


def test_tools_list_schema(client):
    res = client.request("tools/list")["result"]
    assert res["resultType"] == "complete" and res["cacheScope"] in ("public", "private") and res["ttlMs"] > 0
    tools = {t["name"]: t for t in res["tools"]}
    assert list(tools) == ["submit_intent", "query_projection"]  # deterministic order
    sub = tools["submit_intent"]["inputSchema"]
    assert sub["type"] == "object" and sub["additionalProperties"] is False
    assert sub["required"] == ["type", "actor", "payload"]
    assert sub["properties"]["type"]["enum"] == ["task.created", "task.claimed", "task.submitted"]
    assert sub["properties"]["payload"]["type"] == "object"
    q = tools["query_projection"]["inputSchema"]
    assert q["properties"]["view"]["enum"] == ["tasks", "cost", "verify"]
    assert q["required"] == ["view"]
    assert set(q["properties"]) == {"view", "by", "prices"}


def test_malformed_json_is_parse_error(client):
    resp = client.raw('{"jsonrpc": "2.0", "id": 1, "method": ')
    assert resp == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    assert client.raw(b"\xff\xfe")["error"]["code"] == -32700
    assert client.raw('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"x":NaN}}')["error"]["code"] == -32700


def test_invalid_requests(client):
    assert client.raw("[1, 2]")["error"]["code"] == -32600  # batches
    assert client.raw('{"id": 1, "method": "tools/list"}')["error"]["code"] == -32600  # no jsonrpc
    assert client.raw('{"jsonrpc": "2.0", "id": 1, "method": 5}')["error"]["code"] == -32600
    assert client.raw('{"jsonrpc": "2.0", "id": null, "method": "tools/list"}')["error"]["code"] == -32600
    assert client.raw('{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []}')["error"]["code"] == -32602


def test_notifications_get_no_response(client):
    assert client.raw('{"jsonrpc": "2.0", "method": "notifications/initialized"}') is None
    assert client.raw('{"jsonrpc": "2.0", "method": "tools/call", "params": {}}') is None
    assert client.raw("") is None


def test_unknown_method_and_tool(client):
    assert client.request("resources/list")["error"]["code"] == -32601
    assert client.request("ping")["error"]["code"] == -32601  # removed in 2026-07-28
    resp = client.request("tools/call", {"name": "rm_rf", "arguments": {}})
    assert resp["error"]["code"] == -32602 and "Unknown tool" in resp["error"]["message"]


def test_missing_meta_without_initialize(client):
    resp = client.request("tools/list", meta=False)
    assert resp["error"]["code"] == -32602
    assert MODERN_VERSION in resp["error"]["message"]


def test_missing_client_capabilities(client):
    bad = {"io.modelcontextprotocol/protocolVersion": MODERN_VERSION}
    resp = client.raw(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": bad}}))
    assert resp["error"]["code"] == -32602


def test_unsupported_protocol_version(client):
    meta = {**META, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}
    resp = client.raw(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}))
    assert resp["error"]["code"] == -32022
    assert resp["error"]["data"] == {"supported": [MODERN_VERSION], "requested": "1900-01-01"}


def test_legacy_initialize_flow(client):
    resp = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}, meta=False)
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert resp["result"]["serverInfo"]["name"] == "contraprova"
    assert client.raw('{"jsonrpc": "2.0", "method": "notifications/initialized"}') is None
    tools = client.request("tools/list", meta=False)["result"]["tools"]
    assert len(tools) == 2
    assert client.request("ping", meta=False)["result"] == {}
    # An unknown requested version is answered with the newest legacy version.
    assert client.request("initialize", {"protocolVersion": "2099-01-01"}, meta=False)["result"]["protocolVersion"] == "2025-11-25"


# ------------------------------------------------------------------ submit_intent


def test_submit_success(client, log_path):
    res = client.submit("task.created", {"task_id": "t1", "title": "fix bug", "scope": ["src/a.py"]})
    assert res["isError"] is False
    sc = res["structuredContent"]
    assert sc["ok"] is True and sc["seq"] == 0 and len(sc["hash"]) == 64 and sc["actor"] == "mcp:worker-1"
    res = client.submit("task.claimed", {"task_id": "t1"})
    assert res["structuredContent"]["seq"] == 1
    evs = EventLog(log_path).events()
    assert [e.type for e in evs] == ["task.created", "task.claimed"]
    assert evs[0].hash == sc["hash"]
    assert EventLog(log_path).verify() == 2


def test_actor_prefix_configurable(log_path):
    c = Client(McpEdge(log_path, actor_prefix=""))
    assert c.submit("task.created", {"task_id": "t1"}, actor="planner")["structuredContent"]["actor"] == "planner"


def test_illegal_transition(client, log_path):
    res = client.submit("task.submitted", {"task_id": "nope"})
    assert res["isError"] is True
    err = res["structuredContent"]["error"]
    assert err["kind"] == "InvalidTransition" and "unknown task" in err["message"]
    client.submit("task.created", {"task_id": "t1"})
    res = client.submit("task.created", {"task_id": "t1"})
    assert res["structuredContent"]["error"]["kind"] == "InvalidTransition"
    res = client.submit("task.submitted", {"task_id": "t1"})  # not claimed yet
    assert res["structuredContent"]["error"]["kind"] == "InvalidTransition"
    assert len(EventLog(log_path).events()) == 1  # nothing illegal was stored


@pytest.mark.parametrize("type_", sorted(KERNEL_ONLY))
def test_kernel_only_events_are_forbidden(client, log_path, type_):
    client.submit("task.created", {"task_id": "t1"})
    client.submit("task.claimed", {"task_id": "t1"})
    client.submit("task.submitted", {"task_id": "t1"})
    payloads = {
        "gate.decided": {"task_id": "t1", "gate": "tests", "verdict": "pass"},
        "outcome.scored": {"task_id": "t1", "arm": "D", "solved": True},
        "route.decided": {"task_id": "t1", "action": "claude-opus-5-5"},
        "usage.recorded": {"task_id": "t1", "model": "claude-haiku-5-5", "input_tokens": 1},
        "fork.created": {"task_id": "t1", "arm": "control"},
    }
    res = client.submit(type_, payloads.get(type_, {"task_id": "t1"}))
    assert res["isError"] is True
    assert res["structuredContent"]["error"]["kind"] == "Forbidden"
    assert len(EventLog(log_path).events()) == 3


def test_required_forbidden_set():
    assert {"gate.decided", "outcome.scored", "route.decided", "usage.recorded"} <= set(KERNEL_ONLY)


def test_oversized_payload(client, log_path):
    res = client.submit("task.submitted", {"task_id": "t1", "blob": "x" * MAX_PAYLOAD_BYTES})
    assert res["isError"] is True
    err = res["structuredContent"]["error"]
    assert err["kind"] == "InvalidArguments" and "limit" in err["message"]
    assert not log_path.exists() or EventLog(log_path).events() == []


def test_oversized_line(client):
    line = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"pad":"' + b"x" * MAX_LINE_BYTES + b'"}}'
    assert client.raw(line)["error"]["code"] == -32600


@pytest.mark.parametrize(
    "args, needle",
    [
        ({"type": "task.created", "actor": "w", "payload": "{}"}, "payload must be an object"),
        ({"type": "task.created", "actor": 7, "payload": {"task_id": "t"}}, "actor must be a string"),
        ({"type": "task.created", "actor": "", "payload": {"task_id": "t"}}, "actor must be"),
        ({"type": "task.created", "actor": "a\nb", "payload": {"task_id": "t"}}, "control characters"),
        ({"type": "task.created", "actor": "x" * 500, "payload": {"task_id": "t"}}, "actor must be"),
        ({"type": "task.created", "actor": "w", "payload": {}}, "task_id is required"),
        ({"type": "task.created", "actor": "w", "payload": {"task_id": 3}}, "task_id must be a string"),
        ({"type": "task.created", "actor": "w", "payload": {"task_id": "t", "scope": "src"}}, "scope must be a list"),
        ({"type": "task.created", "actor": "w", "payload": {"task_id": "t", "max_attempts": "3"}}, "max_attempts"),
        ({"type": "task.created", "actor": "w", "payload": {"task_id": "t", "max_attempts": True}}, "max_attempts"),
        ({"type": "task.created", "actor": "w", "payload": {"task_id": "t", "supersedes": 1}}, "supersedes"),
        ({"type": "task.claimed", "actor": "w", "payload": {"task_id": "t", "worker": ""}}, "worker"),
        ({"type": "task.bogus", "actor": "w", "payload": {"task_id": "t"}}, "type must be one of"),
        ({"type": ["task.created"], "actor": "w", "payload": {"task_id": "t"}}, "type must be a string"),
        ({"actor": "w", "payload": {"task_id": "t"}}, "type is required"),
        ({"type": "task.created", "actor": "w", "payload": {"task_id": "t"}, "ts": TS}, "unexpected argument"),
        ({"type": "task.created", "actor": "w", "payload": {"task_id": "\ud800"}}, "Unicode"),
    ],
)
def test_submit_rejects_bad_arguments(client, log_path, args, needle):
    res = client.call("submit_intent", args)
    assert res["isError"] is True
    assert res["structuredContent"]["error"]["kind"] == "InvalidArguments"
    assert needle in res["structuredContent"]["error"]["message"]
    assert not log_path.exists() or EventLog(log_path).events() == []


def test_deeply_nested_payload_is_rejected(client):
    deep = "[" * 200 + "]" * 200
    line = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"_meta":%s,"name":"submit_intent","arguments":{"type":"task.created","actor":"w","payload":{"task_id":"t","x":%s}}}}' % (json.dumps(META), deep)
    assert client.raw(line)["error"]["code"] == -32600


def test_rate_limit(log_path):
    now = [0.0]
    c = Client(McpEdge(log_path, max_calls_per_minute=2, clock=lambda: now[0]))
    assert c.call("query_projection", {"view": "tasks"})["isError"] is False
    assert c.call("query_projection", {"view": "tasks"})["isError"] is False
    res = c.call("query_projection", {"view": "tasks"})
    assert res["isError"] is True and res["structuredContent"]["error"]["kind"] == "RateLimited"
    now[0] = 61.0
    assert c.call("query_projection", {"view": "tasks"})["isError"] is False


# ------------------------------------------------------------------ query_projection


def test_query_tasks(client):
    assert client.call("query_projection", {"view": "tasks"})["structuredContent"] == {"view": "tasks", "tasks": []}
    client.submit("task.created", {"task_id": "t1", "title": "a", "scope": ["src/a.py"]})
    client.submit("task.claimed", {"task_id": "t1"})
    tasks = client.call("query_projection", {"view": "tasks"})["structuredContent"]["tasks"]
    assert len(tasks) == 1
    t = tasks[0]
    assert (t["task_id"], t["status"], t["worker"], t["scope"]) == ("t1", "in_progress", "mcp:worker-1", ["src/a.py"])


def test_query_cost(client, log_path):
    # The kernel (not the MCP client) records usage.
    log = EventLog(log_path)
    log.append("task.created", "orchestrator", {"task_id": "t1"}, TS)
    log.append("usage.recorded", "runner", {"task_id": "t1", "role": "worker", "model": "claude-haiku-5-5", "input_tokens": 50_000}, TS)
    log.append("usage.recorded", "runner", {"task_id": "t1", "role": "orchestrator", "model": "claude-opus-5-5", "output_tokens": 1_000_000}, TS)
    sc = client.call("query_projection", {"view": "cost"})["structuredContent"]
    assert sc["by"] == "role" and sc["prices"] == "anthropic-2026-11"
    assert sc["totals"] == pytest.approx({"orchestrator": 20.0, "worker": 0.005})
    assert sc["total"] == pytest.approx(20.005)
    by_model = client.call("query_projection", {"view": "cost", "by": "model"})["structuredContent"]["totals"]
    assert set(by_model) == {"claude-haiku-5-5", "claude-opus-5-5"}


def test_query_rejects_bad_arguments(client):
    for args in (
        {"view": "secrets"},
        {},
        {"view": "cost", "by": "__class__"},
        {"view": "cost", "prices": "../../etc/passwd"},
        {"view": "tasks", "by": "role"},
        {"view": "tasks", "path": "/etc/passwd"},
    ):
        res = client.call("query_projection", args)
        assert res["isError"] is True, args
        assert res["structuredContent"]["error"]["kind"] == "InvalidArguments"


def test_query_verify(client, log_path):
    assert client.call("query_projection", {"view": "verify"})["structuredContent"] == {"view": "verify", "ok": True, "events": 0}
    client.submit("task.created", {"task_id": "t1"})
    client.submit("task.created", {"task_id": "t2"})
    assert client.call("query_projection", {"view": "verify"})["structuredContent"]["events"] == 2
    # Tamper with history: verify reports it as a structured error.
    lines = log_path.read_text().splitlines()
    ev = json.loads(lines[0])
    ev["payload"]["task_id"] = "evil"
    lines[0] = json.dumps(ev)
    log_path.write_text("\n".join(lines) + "\n")
    res = client.call("query_projection", {"view": "verify"})
    assert res["isError"] is True
    assert res["structuredContent"]["ok"] is False and res["structuredContent"]["error"]["kind"] == "ChainError"
    # Writes against a tampered log are refused too.
    res = client.submit("task.created", {"task_id": "t3"})
    assert res["isError"] is True and res["structuredContent"]["error"]["kind"] == "ChainError"


# ------------------------------------------------------------------ stdio loop


def _lines(*msgs):
    return b"".join(json.dumps(m).encode() + b"\n" for m in msgs)


def test_serve_stdio_stream(server):
    inp = io.BytesIO(
        _lines(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}},
        )
        + b"not json\n"
        + b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"pad":"' + b"x" * (MAX_LINE_BYTES + 10) + b'"}}\n'
        + _lines({"jsonrpc": "2.0", "id": 3, "method": "server/discover", "params": {"_meta": META}})
    )
    out = io.BytesIO()
    server.serve(inp, out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in responses] == [1, None, None, 3]
    assert responses[1]["error"]["code"] == -32700
    assert responses[2]["error"]["code"] == -32600
    assert "supportedVersions" in responses[3]["result"]


def test_entry_point_subprocess(log_path):
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    stdin = _lines(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"_meta": META, "name": "submit_intent", "arguments": {"type": "task.created", "actor": "cli", "payload": {"task_id": "t1"}}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"_meta": META, "name": "query_projection", "arguments": {"view": "verify"}}},
    )
    proc = subprocess.run(
        [sys.executable, "-m", "contraprova.mcp_edge", "--log", str(log_path)],
        input=stdin, capture_output=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    r1, r2 = [json.loads(line) for line in proc.stdout.splitlines()]
    assert r1["result"]["structuredContent"]["seq"] == 0
    assert r2["result"]["structuredContent"] == {"view": "verify", "ok": True, "events": 1}


def test_entry_point_rejects_directory_log(tmp_path):
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src}
    proc = subprocess.run(
        [sys.executable, "-m", "contraprova.mcp_edge", "--log", str(tmp_path)],
        input=b"", capture_output=True, env=env, timeout=30,
    )
    assert proc.returncode == 2
