#!/usr/bin/env python3
"""contraprova cascade gate: a Claude Code hook on the Agent tool (standard library only).

It mirrors arm D's gates in contraprova's harness: the orchestrator does not take a worker's word
for it. Wired as two hooks on the main session's ``Agent`` tool:

* ``PreToolUse``: when a cascade agent (worker, escalation-sonnet, escalation-opus) is about to be
  spawned, refuse a per-call ``model`` override (cascade agents run on the model in their own
  definition, so a call can never put them on another tier, Fable included) and snapshot the
  working tree's dirty files.
* ``PostToolUse``: when that agent returns, compare the tree with the snapshot and check
  1. **contract**: the brief has a ``scope:`` line;
  2. **scope**: every changed file matches a ``scope:`` glob;
  3. **protected**: no test file changed (the same patterns as the SWE-bench Pro harness);
  4. **tests**: ``$CONTRAPROVA_TEST_CMD`` exits 0, if that variable is set.
  The verdict goes back to the orchestrator as ``additionalContext`` next to the agent's result.

The test command comes only from the environment the user started Claude Code in, never from the
brief or the repository, so the hook never runs a command a model or a cloned repo chose.
The hook never fails the session: any error becomes a note and exit code 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path

CASCADE_AGENTS = ("worker", "escalation-sonnet", "escalation-opus")
# Same as contraprova.harness.swepro.TEST_PATTERNS (a test keeps the two in sync).
TEST_PATTERNS = (
    "tests/*", "*/tests/*", "test/*", "*/test/*", "__tests__/*", "*/__tests__/*", "spec/*", "*/spec/*",
    "specs/*", "*/specs/*", "conftest.py", "*/conftest.py", "test_*.py", "*/test_*.py", "*_test.py",
    "*_test.js", "*_test.ts", "*_test.tsx", "*_test.jsx", "*.test.*", "*.spec.*",
)
TEST_ENV = "CONTRAPROVA_TEST_CMD"
TEST_TIMEOUT_ENV = "CONTRAPROVA_TEST_TIMEOUT"
OUTPUT_TAIL_CHARS = 3000


def agent_base(subagent_type: str | None) -> str | None:
    """'contraprova-cascade:worker' or 'worker' -> 'worker'; anything else -> None."""
    if not subagent_type:
        return None
    base = subagent_type.rsplit(":", 1)[-1]
    return base if base in CASCADE_AGENTS else None


def parse_brief(prompt: str) -> tuple[list[str] | None, str | None]:
    """The brief's ``scope:`` globs (None if the line is missing) and its ``test:`` command."""
    scope = test = None
    for line in prompt.splitlines():
        m = re.match(r"^\s*[-*]?\s*(scope|test)\s*:\s*(.*)$", line, re.IGNORECASE)
        if not m:
            continue
        key, value = m.group(1).lower(), m.group(2).strip().strip("`")
        if key == "scope" and scope is None:
            scope = [p.strip().strip("`") for p in re.split(r"[,\s]+", value) if p.strip().strip("`")]
        elif key == "test" and test is None:
            test = value or None
    return scope, test


def in_scope(path: str, scope: list[str]) -> bool:
    for pat in scope:
        pat = pat[2:] if pat.startswith("./") else pat
        if pat.endswith("/") and path.startswith(pat):
            return True
        if fnmatchcase(path, pat) or path == pat.rstrip("/"):
            return True
    return False


def is_protected(path: str) -> bool:
    return any(fnmatchcase(path, p) for p in TEST_PATTERNS)


def git_root(cwd: str) -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def dirty_state(root: Path) -> dict[str, str]:
    """{repo-relative path: sha256 of content, or 'deleted'} for every changed or untracked file."""
    out = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root,
                         capture_output=True, timeout=60)
    entries = out.stdout.decode("utf-8", "replace").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        e = entries[i]
        if len(e) > 3:
            paths.append(e[3:])
            if e[0] in "RC":  # rename/copy: the next entry is the source path
                i += 1
                if i < len(entries) and entries[i]:
                    paths.append(entries[i])
        i += 1
    state = {}
    for p in paths:
        f = root / p
        try:
            state[p] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "deleted"
        except OSError:
            state[p] = "unreadable"
    return state


def changed_since(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def state_dir() -> Path:
    d = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or Path(tempfile.gettempdir()) / "contraprova-gate")
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_path(data: dict) -> Path:
    key = data.get("tool_use_id") or data.get("session_id") or "last"
    return state_dir() / ("snap-" + re.sub(r"[^A-Za-z0-9_.-]", "_", str(key)) + ".json")


def run_tests(cmd: str, cwd: Path) -> tuple[int | None, str]:
    timeout = int(os.environ.get(TEST_TIMEOUT_ENV, "540"))
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    out = (r.stdout + r.stderr).strip()
    return r.returncode, out[-OUTPUT_TAIL_CHARS:]


def evaluate(prompt: str, changed: list[str] | None, test_cmd: str | None, cwd: Path) -> tuple[str, list[str]]:
    """(verdict, findings). verdict is PASS or BLOCK; findings are one-line reasons plus test output."""
    scope, brief_test = parse_brief(prompt)
    findings: list[str] = []
    block = False
    if scope is None:
        block = True
        findings.append("BLOCK contract: the brief has no 'scope:' line, so scope could not be checked")
    if changed is None:
        findings.append("note: not a git repository, so changed files could not be checked")
    else:
        if not changed:
            findings.append("note: no file changed")
        prot = [p for p in changed if is_protected(p)]
        if prot:
            block = True
            findings.append("BLOCK protected: test files changed: " + ", ".join(prot))
        if scope is not None:
            out = [p for p in changed if not in_scope(p, scope) and not is_protected(p)]
            if out:
                block = True
                findings.append("BLOCK scope: changed outside scope: " + ", ".join(out))
            else:
                findings.append(f"ok scope: {len(changed)} changed file(s), all in scope")
    if test_cmd:
        code, output = run_tests(test_cmd, cwd)
        if code == 0:
            findings.append(f"ok tests: `{test_cmd}` exited 0")
        else:
            block = True
            status = "timed out" if code is None else f"exited {code}"
            findings.append(f"BLOCK tests: `{test_cmd}` {status}\n--- output (tail) ---\n{output}\n---")
    else:
        hint = f" The brief's test command was `{brief_test}`." if brief_test else ""
        findings.append(f"note: ${TEST_ENV} is not set, so the gate did not run tests; rely on the agent's "
                        f"own report.{hint}")
    return ("BLOCK" if block else "PASS"), findings


def emit(event: str, **fields) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}}))


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if data.get("tool_name") not in ("Agent", "Task"):
        return 0
    tool_input = data.get("tool_input") or {}
    agent = agent_base(tool_input.get("subagent_type"))
    if agent is None:
        return 0
    event = data.get("hook_event_name")
    cwd = data.get("cwd") or os.getcwd()
    try:
        root = git_root(cwd)
        if event == "PreToolUse":
            if tool_input.get("model"):
                emit("PreToolUse", permissionDecision="deny",
                     permissionDecisionReason=f"contraprova gate: do not pass a model to {agent}; cascade agents run "
                                              "on the model in their definition. Call it again without 'model'.")
                return 0
            if root is not None:
                snapshot_path(data).write_text(json.dumps({"root": str(root), "state": dirty_state(root)}))
            return 0
        if event == "PostToolUse":
            changed = None
            if root is not None:
                snap = snapshot_path(data)
                before = json.loads(snap.read_text())["state"] if snap.exists() else {}
                changed = changed_since(before, dirty_state(root))
                snap.unlink(missing_ok=True)
            verdict, findings = evaluate(tool_input.get("prompt") or "", changed,
                                         os.environ.get(TEST_ENV) or None, root or Path(cwd))
            emit("PostToolUse", additionalContext=f"contraprova gate ({agent}): {verdict}\n" + "\n".join(findings))
    except Exception as e:  # never break the session over the gate
        if event == "PostToolUse":
            emit("PostToolUse", additionalContext=f"contraprova gate ({agent}): ERROR {type(e).__name__}: {e}; "
                                                  "treat the result as unchecked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
