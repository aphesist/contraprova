"""Temporary workspaces and a subprocess test runner for the gates.

This is NOT a security sandbox. The command runs as the current user, with the
current user's filesystem and network access; only the working directory is a
throwaway copy. It exists so a submission can be tested without touching the
real workspace, and so a hung test is killed after a timeout. Running untrusted
code safely needs real isolation (a container with no network, e.g. Docker),
which is a later option.

Stdlib only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

MAX_OUTPUT_BYTES = 2048
IGNORED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".hypothesis", ".venv", "node_modules"})
WORKSPACE_PLACEHOLDER = "<workspace>"


@dataclass(frozen=True)
class RunResult:
    """What a test command did, reduced to something small enough to log."""

    command: tuple[str, ...]
    returncode: int | None  # None when the command timed out or could not start
    timed_out: bool
    output: str  # combined stdout+stderr, tail-truncated to MAX_OUTPUT_BYTES
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def summary(self) -> dict[str, object]:
        """JSON-ready summary for a GateResult's evidence."""
        return {
            "command": list(self.command),
            "passed": self.passed,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "counts": dict(self.counts),
            "output": self.output,
        }


def safe_relpath(path: str) -> PurePosixPath:
    """Normalise a repo-relative path, refusing absolute paths and ``..``."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    pure = PurePosixPath(p)
    if not p or pure.is_absolute() or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise ValueError(f"unsafe path: {path!r}")
    return pure


def truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Keep the last ``limit`` UTF-8 bytes (test summaries are at the end)."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    marker = "...[truncated]\n"
    keep = data[-(limit - len(marker)) :].decode("utf-8", errors="ignore")
    return marker + keep


_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b")
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
_UNITTEST_FAILED = re.compile(r"^FAILED \(([^)]*)\)", re.MULTILINE)


def parse_counts(output: str) -> dict[str, int]:
    """Best-effort test counts from pytest or unittest output; empty if unrecognised."""
    counts: dict[str, int] = {}
    ran = _UNITTEST_RAN.findall(output)
    if ran:
        counts["ran"] = int(ran[-1])
        failed = _UNITTEST_FAILED.findall(output)
        if failed:
            for part in failed[-1].split(","):
                key, _, val = part.strip().partition("=")
                if val.isdigit():
                    counts[key] = int(val)
        return counts
    lines = [ln for ln in output.splitlines() if _PYTEST_COUNT.search(ln)]
    if lines:
        for num, key in _PYTEST_COUNT.findall(lines[-1]):
            counts["errors" if key.startswith("error") else key] = int(num)
    return counts


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in IGNORED_DIRS}


def materialize(workspace: Path | None, files: Mapping[str, str], dest: Path, *, overlay: Path | None = None) -> None:
    """Copy ``workspace`` into ``dest``, then ``overlay`` on top, then write ``files``."""
    if workspace is not None:
        shutil.copytree(workspace, dest, ignore=_ignore, dirs_exist_ok=True, symlinks=True)
    else:
        dest.mkdir(parents=True, exist_ok=True)
    if overlay is not None:
        shutil.copytree(overlay, dest, ignore=_ignore, dirs_exist_ok=True, symlinks=True)
    for rel, content in sorted(files.items()):
        target = dest.joinpath(*safe_relpath(rel).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        target.write_text(content, encoding="utf-8")


def run_command(command: Sequence[str], cwd: Path, *, timeout: float) -> RunResult:
    """Run ``command`` (no shell) in ``cwd`` with a timeout, capturing combined output."""
    cmd = tuple(command)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raw = exc.output or b""
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        return RunResult(cmd, None, True, _clean(text + f"\n[timed out after {timeout}s]", cwd))
    except OSError as exc:
        return RunResult(cmd, None, False, f"[could not start: {exc.strerror or exc}]")
    text = proc.stdout.decode("utf-8", errors="replace")
    return RunResult(cmd, proc.returncode, False, _clean(text, cwd), parse_counts(text))


def _clean(text: str, cwd: Path) -> str:
    # The temp path differs every run; hide it so evidence is reproducible.
    for p in {str(cwd), str(cwd.resolve())}:
        text = text.replace(p, WORKSPACE_PLACEHOLDER)
    return truncate(text)


def run_in_temp_workspace(
    workspace: Path | None,
    files: Mapping[str, str],
    command: Sequence[str],
    *,
    timeout: float = 60.0,
    overlay: Path | None = None,
    dir_name: str = "ws",
) -> RunResult:
    """Copy the workspace, apply ``files`` (and ``overlay``), run ``command``, delete the copy.

    ``dir_name`` names the copy's directory (some builds, e.g. Exercism's CMake files, derive the
    project name from it)."""
    with tempfile.TemporaryDirectory(prefix="contraprova-") as tmp:
        dest = Path(tmp) / safe_relpath(dir_name).name
        materialize(workspace, files, dest, overlay=overlay)
        return run_command(command, dest, timeout=timeout)
