# MCP edge

`contraprova.mcp_edge` is a thin [Model Context Protocol](https://modelcontextprotocol.io) façade
that lets outside harnesses (Claude Code, Hermes, openbot, any MCP client) propose task events and
read projections. MCP lives only at the edge: `mcp_edge` imports the kernel, the kernel never
imports `mcp_edge`, and nothing in the kernel knows MCP exists.

- Transport: stdio, newline-delimited JSON-RPC 2.0.
- Dependencies: the standard library only (no MCP SDK).
- Entry point: `python -m contraprova.mcp_edge --log PATH`.

## Protocol revision

**Target: MCP `2026-07-28`** (the current revision on the official versioning page), with
backward compatibility for handshake-based clients.

Checked against the official specification pages on 2026-11-11:

| Claim | Verified? | Source |
|---|---|---|
| `2026-07-28` is the current revision | Yes | `/specification/versioning` |
| Core is stateless; `initialize` / `notifications/initialized` removed | Yes | `/specification/2026-07-28/changelog` (SEP-2575) |
| Protocol-level sessions and `Mcp-Session-Id` removed | Yes | changelog (SEP-2567) |
| `Mcp-Method` / `Mcp-Name` headers required on Streamable HTTP POSTs | Yes | changelog, minor change 4 (SEP-2243); not relevant to stdio |
| Sampling (and Roots, Logging) deprecated | Yes | changelog, Deprecated 1 (SEP-2577) |
| Every request carries `_meta["io.modelcontextprotocol/protocolVersion"]` and `_meta["io.modelcontextprotocol/clientCapabilities"]`; missing either is `-32602` | Yes | `/specification/2026-07-28/basic/index` |
| `server/discover` is mandatory for servers | Yes | `/specification/2026-07-28/server/discover` |
| `UnsupportedProtocolVersionError` is `-32022` with `data.supported` / `data.requested` | Yes | `/specification/2026-07-28/basic/versioning` |
| Every result carries `resultType`; `tools/list` carries `ttlMs` and `cacheScope` | Yes | basic/index, server/tools, changelog (SEP-2322, SEP-2549) |
| `ping` removed | Yes | changelog, major change 5 |
| stdio framing: one message per line, no embedded newlines, nothing but MCP on stdout | Yes | `/specification/2026-07-28/basic/transports/stdio` |
| Dual-era servers may answer `initialize` with legacy semantics scoped to the stdio process | Yes | basic/versioning, "Backward Compatibility" |

Not verified: I did not run the server against a live Claude Code, Hermes or openbot build, so
which era each of those clients speaks today is not known here. I also did not validate messages
against the published `schema.json`; the shapes follow the prose and examples on the pages above.

### What the server does

It is a **dual-era** server in the specification's terms:

- **Modern (2026-07-28).** Any request whose `params._meta` has
  `io.modelcontextprotocol/protocolVersion` is served statelessly. Supported methods:
  `server/discover`, `tools/list`, `tools/call`. An unsupported version returns `-32022`; a
  missing `clientCapabilities` returns `-32602`. Results carry `resultType: "complete"` and
  `_meta["io.modelcontextprotocol/serverInfo"]`. Nothing from earlier requests is consulted.
- **Legacy (2025-11-25, 2025-06-18, 2025-03-26, 2024-11-05).** An `initialize` request switches the
  stdio process into legacy mode: the server echoes the requested version if it knows it, or
  answers `2025-11-25`, and then serves `tools/list`, `tools/call` and `ping` without per-request
  `_meta`. Modern requests on the same process are still served as modern.
- A request with neither modern `_meta` nor a prior `initialize` is rejected with `-32602`, naming
  the supported versions so either kind of client can show a useful message.
- Not implemented (not needed for two tools): resources, prompts, `subscriptions/listen`,
  multi round-trip requests, progress, cancellation (notifications are accepted and ignored),
  Streamable HTTP. JSON-RPC batches are rejected (MCP removed batching in 2025-06-18).

## Tools

### `submit_intent`

Propose one task lifecycle event. The server validates the arguments, then calls
`EventLog.append`, which re-reads the log under an exclusive lock, checks the event against the
task projection, and writes it only if it is legal.

```json
{
  "type": "object",
  "properties": {
    "type":    {"type": "string", "enum": ["task.created", "task.claimed", "task.submitted"]},
    "actor":   {"type": "string", "minLength": 1, "maxLength": 128},
    "payload": {
      "type": "object",
      "properties": {
        "task_id":      {"type": "string", "minLength": 1, "maxLength": 256},
        "title":        {"type": "string"},
        "kind":         {"type": "string"},
        "scope":        {"type": "array", "items": {"type": "string"}},
        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 1000},
        "supersedes":   {"type": ["string", "null"]},
        "worker":       {"type": "string"}
      },
      "required": ["task_id"]
    }
  },
  "required": ["type", "actor", "payload"],
  "additionalProperties": false
}
```

Success (`isError: false`, also serialised as a text block):

```json
{"ok": true, "seq": 3, "hash": "9f2c…", "type": "task.claimed", "actor": "mcp:worker-1", "ts": "2026-11-11T12:00:00.000Z"}
```

Failure (`isError: true`):

```json
{"ok": false, "error": {"kind": "InvalidTransition", "message": "task 't1' is todo, cannot be submitted"}}
```

Error kinds: `InvalidArguments`, `Forbidden`, `InvalidTransition`, `ValueError`, `ChainError`,
`LogError`, `RateLimited`.

**Kernel-only events.** `gate.decided`, `route.decided`, `fork.created`, `usage.recorded` and
`outcome.scored` are not in the enum and are refused with kind `Forbidden` if sent anyway. Only
the kernel decides gates, routes, forks, records usage and scores outcomes; an outside client can
propose work, never certify it.

### `query_projection`

```json
{
  "type": "object",
  "properties": {
    "view":   {"type": "string", "enum": ["tasks", "cost", "verify"]},
    "by":     {"type": "string", "enum": ["arm", "model", "role", "task_id"]},
    "prices": {"type": "string", "enum": ["anthropic-2026-11"]}
  },
  "required": ["view"],
  "additionalProperties": false
}
```

- `tasks` → `{"view": "tasks", "tasks": [TaskView as JSON, …]}` from `project_tasks`.
- `cost` → `{"view": "cost", "by": "role", "prices": "anthropic-2026-11", "totals": {…}, "total": 1.23}`
  from `pricing.cost_by` (defaults `by=role`, `prices=DEFAULT_TABLE`; the `prices` enum is the set
  of tables in `PRICE_TABLES`).
- `verify` → `{"view": "verify", "ok": true, "events": n}` from `EventLog.verify`, or
  `isError: true` with kind `ChainError` / `InvalidTransition` if the log is broken.

`by` and `prices` are rejected for views other than `cost`.

## Registering the server

### Claude Code

```bash
claude mcp add --transport stdio contraprova -- \
  python -m contraprova.mcp_edge --log /absolute/path/to/log.jsonl
```

Add `--scope project` to write it to `.mcp.json` for the whole repository, or `--scope user` for
all your projects. Everything after `--` is passed to the server untouched. Use the Python that has
contraprova installed (`pip install -e .`), or give its full path.

Equivalent `.mcp.json` entry:

```json
{
  "mcpServers": {
    "contraprova": {
      "command": "python",
      "args": ["-m", "contraprova.mcp_edge", "--log", "/absolute/path/to/log.jsonl"]
    }
  }
}
```

### Any stdio MCP client

Launch `python -m contraprova.mcp_edge --log PATH` as a subprocess, write one JSON-RPC message per
line to its stdin, read one response per request from its stdout. Close stdin to stop it.
Diagnostics go to stderr only.

A modern client can probe with `server/discover`:

```json
{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
```

A legacy client sends `initialize` first, as it always has.

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--log PATH` | required | The event log. Its directory must exist; it must be a regular file if it exists. |
| `--actor-prefix S` | `mcp:` | Prepended to every submitted `actor`, so the log shows which events came through the edge. `""` disables it. |
| `--max-calls-per-minute N` | 600 | Sliding-window limit on `tools/call`. |

## Why MCP at all: the trade-off with ESAA

[ESAA](https://github.com/elzobrito/ESAA-Core) (Elzo Brito dos Santos Filho,
[arXiv 2602.23193](https://arxiv.org/abs/2602.23193)), from which contraprova takes its separation of
proposals from a validating single writer, deliberately does **not** use MCP. Agents there emit
structured intentions that the orchestrator validates; there is no tool protocol between the model
and the log. That keeps the trusted surface tiny, removes a dependency on a fast-moving protocol
(this revision alone removed the handshake and sessions), and makes every write path explicit.

contraprova keeps that discipline in the kernel and adds MCP only as an optional, removable edge:

- **Gain:** any MCP harness can drive or inspect a run without bespoke glue, which matters when
  comparing orchestrators that already speak MCP.
- **Cost:** another input surface that must be treated as hostile, and a protocol that changes
  under it. The edge is one file with no SDK, so a protocol change touches only that file.
- **What does not change:** MCP clients reach the log only through `EventLog.append`, the same
  validating single writer everything else uses, and only for the three proposal events. They cannot
  write gate decisions, routing, usage or scores, so the measurements stay kernel-authored.

Measured runs do not need the edge at all; the kernel and runner call `EventLog` directly.

## Security notes

- **All input is untrusted.** Messages are parsed with `json.loads` only (no `eval`, no pickle);
  `NaN`/`Infinity` are rejected; lines over 1 MiB are discarded with `-32600`; nesting deeper than
  about 32 levels is rejected; `payload` is capped at 64 KB of canonical JSON; known payload
  fields are type- and length-checked; strings that are not valid Unicode and control characters in
  identifiers are refused; unknown tool arguments are refused.
- **No paths from clients.** No tool takes a file name. The only file the server opens is the log
  given on the command line, resolved once at start-up, and only through `EventLog`. Price tables
  and cost keys are enums, not free strings.
- **Kernel-only events cannot be submitted** (see above), and every accepted event still passes the
  kernel's transition rules; an illegal event is never stored.
- **Actors are self-asserted.** stdio has no authentication; whoever can start the process can write
  proposals as any actor name. The `mcp:` prefix marks provenance in the log but is not identity.
  Run the server only for clients you would let edit the log, and keep the log file's permissions
  tight.
- **Tampering is reported, not hidden.** `verify` returns the chain error, and writes against a
  broken chain are refused.
- **Errors do not leak internals.** Unexpected exceptions become `-32603 Internal error`; I/O
  failures become `LogError` without the path or traceback.
- **Rate limited** per process (default 600 tool calls per minute), as the tools page requires.
- **Nothing is executed and no network is used.** The server makes no outbound calls and runs no
  commands; deprecated features such as sampling are not implemented.
