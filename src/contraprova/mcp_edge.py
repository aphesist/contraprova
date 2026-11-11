"""MCP edge: a thin stdio façade over ``submit_intent`` and ``query_projection``.

Outside harnesses (Claude Code, Hermes, openbot, ...) talk to contraprova
through this module only. The kernel never imports it; it imports the kernel.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout, stdlib only.

Protocol: the server is *dual-era* in the sense of MCP revision 2026-07-28
(see docs/mcp.md):

* A request whose ``params._meta`` carries ``io.modelcontextprotocol/protocolVersion``
  is served statelessly under 2026-07-28 (``server/discover``, ``tools/list``,
  ``tools/call``). Nothing from earlier requests is consulted.
* An ``initialize`` request selects legacy (2025-11-25 and earlier) semantics
  for the rest of this stdio process, so clients that still use the handshake
  keep working.

Every input is untrusted. No tool takes a path; the only file ever opened is
the log configured at start-up, and it is only touched through
:class:`contraprova.log.EventLog`.

Run: ``python -m contraprova.mcp_edge --log PATH``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping

from . import __version__
from .events import EVENT_TYPES, ChainError, canonical_json
from .log import EventLog
from .pricing import DEFAULT_TABLE, PRICE_TABLES, cost_by
from .projections import InvalidTransition, project_tasks, usage_records

# ------------------------------------------------------------------ protocol constants

MODERN_VERSION = "2026-07-28"
SUPPORTED_MODERN = (MODERN_VERSION,)
# Handshake-based revisions this server will negotiate through ``initialize``.
SUPPORTED_LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

SERVER_INFO = {"name": "contraprova", "version": __version__}

# ------------------------------------------------------------------ input limits

MAX_LINE_BYTES = 1024 * 1024  # one JSON-RPC message
MAX_PAYLOAD_BYTES = 64 * 1024  # canonical JSON of an intent payload
MAX_DEPTH = 32  # nesting of any incoming JSON value
MAX_ID_CHARS = 256  # task ids, supersedes, worker
MAX_ACTOR_CHARS = 128
MAX_TEXT_CHARS = 4096  # title, kind
MAX_SCOPE_ITEMS = 1024
MAX_ATTEMPTS_LIMIT = 1000
DEFAULT_CALLS_PER_MINUTE = 600

# Only the task lifecycle a worker or planner proposes. gate.decided, route.decided,
# fork.created, usage.recorded and outcome.scored are written by the kernel alone.
SUBMITTABLE = ("task.created", "task.claimed", "task.submitted")
KERNEL_ONLY = tuple(sorted(EVENT_TYPES - set(SUBMITTABLE)))

COST_KEYS = ("arm", "model", "role", "task_id")
VIEWS = ("tasks", "cost", "verify")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "submit_intent",
        "title": "Submit a task lifecycle intent",
        "description": (
            "Propose one task lifecycle event (task.created, task.claimed or task.submitted). "
            "The kernel validates it against the current task state and appends it to the "
            "hash-chained log, or rejects it. Gate decisions, routing, usage and outcomes are "
            "written only by the kernel and cannot be submitted here. Returns the event's seq and hash."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(SUBMITTABLE)},
                "actor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_ACTOR_CHARS,
                    "description": "Who proposes this (self-asserted; recorded with the server's actor prefix).",
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "Event payload, at most 64 KB as canonical JSON. Always needs task_id. "
                        "task.created also takes title, kind, scope (list of paths), max_attempts, supersedes; "
                        "task.claimed takes worker."
                    ),
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1, "maxLength": MAX_ID_CHARS},
                        "title": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                        "kind": {"type": "string", "maxLength": MAX_TEXT_CHARS},
                        "scope": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_SCOPE_ITEMS},
                        "max_attempts": {"type": "integer", "minimum": 1, "maximum": MAX_ATTEMPTS_LIMIT},
                        "supersedes": {"type": ["string", "null"], "maxLength": MAX_ID_CHARS},
                        "worker": {"type": "string", "minLength": 1, "maxLength": MAX_ID_CHARS},
                    },
                    "required": ["task_id"],
                },
            },
            "required": ["type", "actor", "payload"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    },
    {
        "name": "query_projection",
        "title": "Read a projection of the log",
        "description": (
            "Read state derived from the event log. view=tasks: every task and its status. "
            "view=cost: recorded usage re-priced in USD, grouped by `by` under price table `prices`. "
            "view=verify: check the hash chain and replay every transition."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": list(VIEWS)},
                "by": {"type": "string", "enum": list(COST_KEYS), "description": "cost only; default role"},
                "prices": {
                    "type": "string",
                    "enum": sorted(PRICE_TABLES),
                    "description": f"cost only; default {DEFAULT_TABLE}",
                },
            },
            "required": ["view"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
]

INSTRUCTIONS = (
    "contraprova event log. Use submit_intent to create, claim or submit a task; "
    "use query_projection to read tasks, cost or verify the log. Only the kernel decides gates."
)


# ------------------------------------------------------------------ helpers


class ToolInputError(Exception):
    """Arguments failed validation; reported as a tool execution error."""


class _RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant)


def _depth_ok(value: Any, limit: int = MAX_DEPTH) -> bool:
    stack = [(value, 1)]
    while stack:
        v, d = stack.pop()
        if isinstance(v, (dict, list)):
            if d > limit:
                return False
            children = v.values() if isinstance(v, dict) else v
            stack.extend((c, d + 1) for c in children)
    return True


def _check_text(name: str, value: Any, max_chars: int, *, min_chars: int = 0) -> str:
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string")
    if not (min_chars <= len(value) <= max_chars):
        raise ToolInputError(f"{name} must be {min_chars}..{max_chars} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ToolInputError(f"{name} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolInputError(f"{name} is not valid Unicode") from None
    return value


def _validate_payload(type_: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ToolInputError("payload must be an object")
    try:
        size = len(canonical_json(payload).encode("utf-8"))
    except (ValueError, TypeError):
        raise ToolInputError("payload is not valid JSON text (bad number or Unicode)") from None
    if size > MAX_PAYLOAD_BYTES:
        raise ToolInputError(f"payload is {size} bytes; the limit is {MAX_PAYLOAD_BYTES}")
    if "task_id" not in payload:
        raise ToolInputError("payload.task_id is required")
    _check_text("payload.task_id", payload["task_id"], MAX_ID_CHARS, min_chars=1)
    if type_ == "task.created":
        for key in ("title", "kind"):
            if key in payload:
                _check_text(f"payload.{key}", payload[key], MAX_TEXT_CHARS)
        if "scope" in payload:
            scope = payload["scope"]
            if not isinstance(scope, list) or len(scope) > MAX_SCOPE_ITEMS:
                raise ToolInputError(f"payload.scope must be a list of at most {MAX_SCOPE_ITEMS} paths")
            for i, item in enumerate(scope):
                _check_text(f"payload.scope[{i}]", item, MAX_TEXT_CHARS, min_chars=1)
        if "max_attempts" in payload:
            m = payload["max_attempts"]
            if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= MAX_ATTEMPTS_LIMIT:
                raise ToolInputError(f"payload.max_attempts must be an integer 1..{MAX_ATTEMPTS_LIMIT}")
        if payload.get("supersedes") is not None:
            _check_text("payload.supersedes", payload["supersedes"], MAX_ID_CHARS, min_chars=1)
    elif type_ == "task.claimed" and "worker" in payload:
        _check_text("payload.worker", payload["worker"], MAX_ID_CHARS, min_chars=1)
    return payload


def _only_keys(args: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ToolInputError(f"unexpected argument(s): {', '.join(map(str, extra))}")


class RateLimiter:
    """At most ``max_calls`` tool calls in any sliding ``window`` seconds."""

    def __init__(self, max_calls: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.max_calls, self.window, self.clock = max_calls, window, clock
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        while self._calls and now - self._calls[0] >= self.window:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(now)
        return True


# ------------------------------------------------------------------ server


class McpEdge:
    """Handles one JSON-RPC message at a time. ``handle_line`` is the whole protocol."""

    def __init__(
        self,
        log_path: str | Path,
        *,
        actor_prefix: str = "mcp:",
        max_calls_per_minute: int = DEFAULT_CALLS_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.log = EventLog(Path(log_path))
        self.actor_prefix = actor_prefix
        self.limiter = RateLimiter(max_calls_per_minute, 60.0, clock)
        self.legacy_version: str | None = None  # set by ``initialize``; legacy clients only

    # ---- framing

    def handle_line(self, line: str | bytes) -> str | None:
        """Process one message; return the response line (no newline) or None for notifications."""
        if isinstance(line, bytes):
            if len(line) > MAX_LINE_BYTES:
                return self._error_line(None, INVALID_REQUEST, f"message exceeds {MAX_LINE_BYTES} bytes")
            try:
                line = line.decode("utf-8")
            except UnicodeDecodeError:
                return self._error_line(None, PARSE_ERROR, "Parse error: message is not UTF-8")
        elif len(line.encode("utf-8", "surrogatepass")) > MAX_LINE_BYTES:
            return self._error_line(None, INVALID_REQUEST, f"message exceeds {MAX_LINE_BYTES} bytes")
        if not line.strip():
            return None
        try:
            msg = _loads(line)
        except (ValueError, RecursionError):
            return self._error_line(None, PARSE_ERROR, "Parse error")
        return self.handle_message(msg)

    def handle_message(self, msg: Any) -> str | None:
        if isinstance(msg, list):
            return self._error_line(None, INVALID_REQUEST, "JSON-RPC batches are not supported by MCP")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._error_line(None, INVALID_REQUEST, "Invalid Request: expected a JSON-RPC 2.0 object")
        if not _depth_ok(msg, MAX_DEPTH + 4):
            return self._error_line(None, INVALID_REQUEST, "Invalid Request: nesting too deep")
        method = msg.get("method")
        if not isinstance(method, str):
            # A response from the client, or junk. The server never sends requests, so ignore responses.
            if "method" not in msg and ("result" in msg or "error" in msg):
                return None
            return self._error_line(_safe_id(msg), INVALID_REQUEST, "Invalid Request: method must be a string")
        is_notification = "id" not in msg
        req_id = msg.get("id")
        if not is_notification and (isinstance(req_id, bool) or not isinstance(req_id, (str, int))):
            return self._error_line(None, INVALID_REQUEST, "Invalid Request: id must be a string or integer")
        params = msg.get("params", {})
        if not isinstance(params, dict):
            if is_notification:
                return None
            return self._error_line(req_id, INVALID_PARAMS, "params must be an object")
        if is_notification:
            return None  # notifications/initialized, notifications/cancelled, ...: nothing to answer
        try:
            result = self._dispatch(method, params)
        except _RpcError as e:
            return self._error_line(req_id, e.code, e.message, e.data)
        except Exception:  # never leak internals to an untrusted client
            print("contraprova.mcp_edge: internal error", file=sys.stderr)
            return self._error_line(req_id, INTERNAL_ERROR, "Internal error")
        return _dump({"jsonrpc": "2.0", "id": req_id, "result": result})

    # ---- dispatch

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        meta = params.get("_meta")
        modern = isinstance(meta, dict) and META_VERSION in meta
        if modern:
            self._check_modern_meta(meta)
        elif method == "initialize":
            return self._initialize(params)
        elif self.legacy_version is None:
            raise _RpcError(
                INVALID_PARAMS,
                f"missing required _meta field {META_VERSION}: this server speaks MCP {MODERN_VERSION} "
                f"per request, or send initialize for one of {', '.join(SUPPORTED_LEGACY)}",
                {"supported": list(SUPPORTED_MODERN), "legacy": list(SUPPORTED_LEGACY)},
            )

        if method == "server/discover":
            return self._result(
                {
                    "supportedVersions": list(SUPPORTED_MODERN),
                    "capabilities": {"tools": {}},
                    "instructions": INSTRUCTIONS,
                    "ttlMs": 3_600_000,
                    "cacheScope": "public",
                }
            )
        if method == "tools/list":
            return self._result({"tools": TOOLS, "ttlMs": 3_600_000, "cacheScope": "public"})
        if method == "tools/call":
            return self._result(self._call_tool(params))
        if method == "ping" and not modern:
            return {}
        if method == "initialize":  # modern _meta on initialize: era mismatch
            raise _RpcError(METHOD_NOT_FOUND, f"initialize is not part of MCP {MODERN_VERSION}")
        raise _RpcError(METHOD_NOT_FOUND, f"Method not found: {method[:100]}")

    def _check_modern_meta(self, meta: dict[str, Any]) -> None:
        version = meta.get(META_VERSION)
        if not isinstance(version, str):
            raise _RpcError(INVALID_PARAMS, f"{META_VERSION} must be a string")
        if version not in SUPPORTED_MODERN:
            raise _RpcError(
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": list(SUPPORTED_MODERN), "requested": version[:64]},
            )
        if not isinstance(meta.get(META_CLIENT_CAPS), dict):
            raise _RpcError(INVALID_PARAMS, f"missing required _meta field {META_CLIENT_CAPS}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_LEGACY else SUPPORTED_LEGACY[0]
        self.legacy_version = version
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    @staticmethod
    def _result(body: dict[str, Any]) -> dict[str, Any]:
        return {"resultType": "complete", **body, "_meta": {META_SERVER_INFO: SERVER_INFO}}

    # ---- tools

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in ("submit_intent", "query_projection"):
            shown = name[:100] if isinstance(name, str) else repr(type(name).__name__)
            raise _RpcError(INVALID_PARAMS, f"Unknown tool: {shown}")
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            raise _RpcError(INVALID_PARAMS, "arguments must be an object")
        if not self.limiter.allow():
            return _tool_error("RateLimited", "too many tool calls; try again later")
        try:
            if name == "submit_intent":
                return self._submit_intent(args)
            return self._query_projection(args)
        except ToolInputError as e:
            return _tool_error("InvalidArguments", str(e))

    def _submit_intent(self, args: dict[str, Any]) -> dict[str, Any]:
        _only_keys(args, ("type", "actor", "payload"))
        for key in ("type", "actor", "payload"):
            if key not in args:
                raise ToolInputError(f"{key} is required")
        type_ = args["type"]
        if not isinstance(type_, str):
            raise ToolInputError("type must be a string")
        if type_ in KERNEL_ONLY:
            return _tool_error("Forbidden", f"{type_} is written only by the kernel and cannot be submitted")
        if type_ not in SUBMITTABLE:
            raise ToolInputError(f"type must be one of {', '.join(SUBMITTABLE)}")
        actor = _check_text("actor", args["actor"], MAX_ACTOR_CHARS, min_chars=1)
        payload = _validate_payload(type_, args["payload"])
        try:
            ev = self.log.append(type_, self.actor_prefix + actor, payload)
        except InvalidTransition as e:
            return _tool_error("InvalidTransition", str(e))
        except ValueError as e:
            return _tool_error("ValueError", str(e))
        except ChainError as e:
            return _tool_error("ChainError", f"the log failed verification: {e}")
        except (OSError, KeyError, TypeError):
            return _tool_error("LogError", "the event log could not be read or written")
        return _tool_ok({"ok": True, "seq": ev.seq, "hash": ev.hash, "type": ev.type, "actor": ev.actor, "ts": ev.ts})

    def _query_projection(self, args: dict[str, Any]) -> dict[str, Any]:
        _only_keys(args, ("view", "by", "prices"))
        view = args.get("view")
        if view not in VIEWS:
            raise ToolInputError(f"view must be one of {', '.join(VIEWS)}")
        if view != "cost" and ("by" in args or "prices" in args):
            raise ToolInputError("by and prices apply only to view=cost")
        try:
            if view == "verify":
                try:
                    n = self.log.verify()
                except (ChainError, InvalidTransition) as e:
                    return _tool_error(type(e).__name__, str(e), {"view": "verify", "ok": False})
                return _tool_ok({"view": "verify", "ok": True, "events": n})
            events = self.log.events()
            if view == "tasks":
                tasks = [_task_dict(t) for t in project_tasks(events).values()]
                return _tool_ok({"view": "tasks", "tasks": tasks})
            by = args.get("by", "role")
            prices = args.get("prices", DEFAULT_TABLE)
            if by not in COST_KEYS:
                raise ToolInputError(f"by must be one of {', '.join(COST_KEYS)}")
            if prices not in PRICE_TABLES:
                raise ToolInputError(f"prices must be one of {', '.join(sorted(PRICE_TABLES))}")
            try:
                totals = cost_by(usage_records(events), PRICE_TABLES[prices], by)
            except KeyError as e:
                return _tool_error("PricingError", str(e.args[0]) if e.args else "missing price")
            ordered = dict(sorted(totals.items()))
            return _tool_ok({"view": "cost", "by": by, "prices": prices, "totals": ordered, "total": sum(ordered.values())})
        except (ChainError, InvalidTransition) as e:
            return _tool_error(type(e).__name__, str(e))
        except (OSError, ValueError, KeyError, TypeError):
            return _tool_error("LogError", "the event log could not be read")

    # ---- output

    @staticmethod
    def _error_line(req_id: Any, code: int, message: str, data: Any = None) -> str:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return _dump({"jsonrpc": "2.0", "id": req_id, "error": err})

    # ---- stdio loop

    def serve(self, instream: BinaryIO, outstream: BinaryIO) -> None:
        """Read newline-delimited messages until EOF; write one response line per request."""
        while True:
            raw = instream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                while True:  # discard the rest of the oversized line
                    rest = instream.readline(MAX_LINE_BYTES)
                    if not rest or rest.endswith(b"\n"):
                        break
                out = self._error_line(None, INVALID_REQUEST, f"message exceeds {MAX_LINE_BYTES} bytes")
            else:
                out = self.handle_line(raw.rstrip(b"\r\n"))
            if out is not None:
                outstream.write(out.encode("utf-8") + b"\n")
                outstream.flush()


def _safe_id(msg: dict[str, Any]) -> Any:
    i = msg.get("id")
    return i if isinstance(i, (str, int)) and not isinstance(i, bool) else None


def _dump(obj: Any) -> str:
    # json.dumps escapes control characters, so the line never contains a raw newline.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _task_dict(t: Any) -> dict[str, Any]:
    d = asdict(t)
    d["scope"] = list(d["scope"])
    return d


def _tool_ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": _dump(data)}], "structuredContent": data, "isError": False}


def _tool_error(kind: str, message: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = {**(extra or {}), "ok": False, "error": {"kind": kind, "message": message}}
    return {"content": [{"type": "text", "text": _dump(data)}], "structuredContent": data, "isError": True}


# ------------------------------------------------------------------ entry point


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m contraprova.mcp_edge", description="contraprova MCP edge (stdio)")
    ap.add_argument("--log", required=True, help="path to the event log (JSON lines); the only file this server touches")
    ap.add_argument("--actor-prefix", default="mcp:", help="prepended to every submitted actor (default: mcp:)")
    ap.add_argument("--max-calls-per-minute", type=int, default=DEFAULT_CALLS_PER_MINUTE)
    args = ap.parse_args(argv)

    path = Path(args.log).expanduser().resolve()
    if path.exists() and not path.is_file():
        print(f"error: {path} is not a regular file", file=sys.stderr)
        return 2
    if not path.parent.is_dir():
        print(f"error: directory {path.parent} does not exist", file=sys.stderr)
        return 2
    if args.max_calls_per_minute < 1:
        print("error: --max-calls-per-minute must be at least 1", file=sys.stderr)
        return 2
    server = McpEdge(path, actor_prefix=args.actor_prefix, max_calls_per_minute=args.max_calls_per_minute)
    try:
        server.serve(sys.stdin.buffer, sys.stdout.buffer)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
