"""Docker plumbing for SWE-bench Pro: the daemon, a disk-capped image cache, file extraction and
the official verifier run with plain ``docker run --network none``.

Nothing here runs repository code on the host. Model output and tests run only inside a task's
container, started with ``--network none`` and removed when it exits (``--rm``).

Daemon
    In the cloud sessions this project runs in, ``dockerd`` is installed but not running at
    session start. :func:`ensure_dockerd` checks ``docker info``; if the daemon is down and the
    process is root with ``dockerd`` on PATH, it starts ``dockerd`` in the background (log in
    ``$TMPDIR/contraprova-dockerd.log``), waits for it, and returns the process so the caller can
    stop it with :func:`stop_dockerd` when done. A daemon that was already running is never touched.

Image cache
    Pro images are 2-6 GB each (py/js repos) and the disk is small, so :class:`ImageCache` pulls
    on demand, keeps a least-recently-used record of the images *it* pulled (in a JSON state
    file), and deletes the least recently used ones before a pull so the images it holds stay
    under ``max_bytes`` (default 20 GB). Pulls are serialised with a lock file, so at most one
    pull runs at a time even across processes. Images it did not pull are never deleted.

Verifier
    :func:`run_verifier` mounts the task's verifier directory (``test.sh``, ``run_script.sh``,
    ``parser.py``, ``config.json``, ``test_patch.patch``) at ``/tests``, the submission's files at
    ``/submission`` and an empty ``/logs``, copies the submission over ``/app`` and runs
    ``bash /tests/test.sh``. ``test.sh`` resets test files, applies the test patch, runs the
    selected tests, parses them with ``parser.py`` and writes ``/logs/verifier/reward.txt`` and
    ``output.json`` (per-test statuses). This is the approach validated in the feasibility study.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

GB = 1_000_000_000
DEFAULT_MAX_IMAGE_BYTES = 20 * GB
DEFAULT_PULL_TIMEOUT = 1800.0
DEFAULT_TEST_TIMEOUT = 1800.0

Runner = Callable[..., subprocess.CompletedProcess]


def _run(cmd: Sequence[str], *, timeout: float | None = None, input: bytes | None = None,
         check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=check)


# ------------------------------------------------------------------ daemon


def docker_available(runner: Runner = _run) -> bool:
    """True if the docker CLI is installed and the daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        return runner(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_dockerd(timeout: float = 60.0, log_path: str | Path | None = None) -> subprocess.Popen | None:
    """Make sure a Docker daemon is running. Returns the ``dockerd`` process if this call started
    one (stop it with :func:`stop_dockerd`), None if one was already running.

    Raises RuntimeError if no daemon is running and one cannot be started here."""
    if docker_available():
        return None
    if shutil.which("dockerd") is None or shutil.which("docker") is None:
        raise RuntimeError("docker is not installed (need the docker CLI and dockerd)")
    if os.geteuid() != 0:
        raise RuntimeError("the Docker daemon is not running and starting it needs root")
    log = Path(log_path) if log_path else Path(tempfile.gettempdir()) / "contraprova-dockerd.log"
    fh = open(log, "ab")
    proc = subprocess.Popen(["dockerd"], stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"dockerd exited with code {proc.returncode}; see {log}")
        if docker_available():
            return proc
        time.sleep(1.0)
    proc.terminate()
    raise RuntimeError(f"dockerd did not become ready within {timeout:.0f}s; see {log}")


def stop_dockerd(proc: subprocess.Popen | None, timeout: float = 60.0) -> None:
    """Stop a daemon started by :func:`ensure_dockerd` (no-op for None)."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


# ------------------------------------------------------------------ image cache


@dataclass
class PullRecord:
    image: str
    seconds: float
    bytes: int
    pulled: bool  # False if it was already present


class ImageCache:
    """Pull images on demand and keep the images this cache pulled under ``max_bytes``.

    ``state_path`` holds ``{image: {"bytes": n, "last_used": t}}`` for images this cache pulled,
    so the LRU order survives restarts. ``reserve_bytes`` is the space kept free for the next
    pull before it starts (its size is unknown until it lands): the default is the largest image
    in the supported py/js subset, about 6.1 GB, rounded up.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_IMAGE_BYTES, *, state_path: str | Path | None = None,
                 reserve_bytes: int = 7 * GB, runner: Runner = _run, pull_timeout: float = DEFAULT_PULL_TIMEOUT,
                 clock: Callable[[], float] = time.time):
        self.max_bytes = int(max_bytes)
        self.reserve_bytes = min(int(reserve_bytes), self.max_bytes)
        self.state_path = Path(state_path) if state_path else Path(tempfile.gettempdir()) / "contraprova-images.json"
        self.lock_path = self.state_path.with_suffix(".lock")
        self.runner = runner
        self.pull_timeout = pull_timeout
        self.clock = clock

    # -- state

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # -- docker queries

    def present(self, image: str) -> bool:
        return self.runner(["docker", "image", "inspect", image], timeout=60).returncode == 0

    def size(self, image: str) -> int:
        r = self.runner(["docker", "image", "inspect", "--format", "{{.Size}}", image], timeout=60)
        try:
            return int(r.stdout.decode().strip() or 0) if r.returncode == 0 else 0
        except ValueError:
            return 0

    def held_bytes(self) -> int:
        return sum(int(v.get("bytes", 0)) for v in self._load().values())

    # -- operations

    def remove(self, image: str) -> bool:
        """Delete ``image`` if this cache pulled it. Returns True if it was removed."""
        with self._locked():
            state = self._load()
            if image not in state:
                return False
            self.runner(["docker", "image", "rm", "-f", image], timeout=300)
            state.pop(image, None)
            self._save(state)
            return True

    def _evict_for(self, state: dict[str, dict[str, Any]], incoming: int, keep: str) -> list[str]:
        removed = []
        for img in sorted(state, key=lambda k: state[k].get("last_used", 0)):
            total = sum(int(v.get("bytes", 0)) for k, v in state.items())
            if total + incoming <= self.max_bytes:
                break
            if img == keep:
                continue
            self.runner(["docker", "image", "rm", "-f", img], timeout=300)
            state.pop(img)
            removed.append(img)
        return removed

    def ensure(self, image: str) -> PullRecord:
        """Make ``image`` available, pulling it if needed (one pull at a time), and mark it used."""
        with self._locked():
            state = self._load()
            if self.present(image):
                if image in state:
                    state[image]["last_used"] = self.clock()
                    self._save(state)
                return PullRecord(image, 0.0, self.size(image), False)
            self._evict_for(state, self.reserve_bytes, keep=image)
            self._save(state)
            t0 = time.monotonic()
            r = self.runner(["docker", "pull", "--quiet", image], timeout=self.pull_timeout)
            secs = time.monotonic() - t0
            if r.returncode != 0:
                raise RuntimeError(f"docker pull {image} failed: {r.stderr.decode(errors='replace')[-500:]}")
            n = self.size(image)
            state[image] = {"bytes": n, "last_used": self.clock()}
            self._evict_for(state, 0, keep=image)
            self._save(state)
            return PullRecord(image, secs, n, True)

    def clear(self) -> list[str]:
        """Delete every image this cache pulled."""
        with self._locked():
            state = self._load()
            for img in list(state):
                self.runner(["docker", "image", "rm", "-f", img], timeout=300)
            self._save({})
            return sorted(state)


# ------------------------------------------------------------------ files and verifier


def read_files(image: str, paths: Sequence[str], dest: str | Path, *, workdir: str = "/app",
               runner: Runner = _run) -> dict[str, bool]:
    """Copy ``paths`` (relative to ``workdir``) out of ``image`` into ``dest`` without running
    anything in it: ``docker create`` (the container never starts), ``docker cp``, ``docker rm``.
    Returns path -> whether it existed."""
    dest = Path(dest)
    r = runner(["docker", "create", "--network", "none", image, "true"], timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"docker create {image} failed: {r.stderr.decode(errors='replace')[-300:]}")
    cid = r.stdout.decode().strip()
    found: dict[str, bool] = {}
    try:
        for p in paths:
            target = dest / p
            target.parent.mkdir(parents=True, exist_ok=True)
            c = runner(["docker", "cp", f"{cid}:{workdir}/{p}", str(target)], timeout=120)
            found[p] = c.returncode == 0 and target.is_file()
            if c.returncode == 0 and target.is_dir():  # never expected for an oracle file
                shutil.rmtree(target)
                found[p] = False
    finally:
        runner(["docker", "rm", "-f", cid], timeout=120)
    return found


def image_head(image: str, *, workdir: str = "/app", runner: Runner = _run) -> str:
    """``git rev-parse HEAD`` of the repository in ``image`` (run offline, removed after)."""
    r = runner(["docker", "run", "--rm", "--network", "none", "--entrypoint", "git", "-w", workdir, image,
                "rev-parse", "HEAD"], timeout=120)
    return r.stdout.decode().strip() if r.returncode == 0 else ""


@dataclass(frozen=True)
class VerifierResult:
    reward: int | None  # 1/0 from reward.txt, None if it was not written
    statuses: Mapping[str, str]  # test id -> PASSED/FAILED/SKIPPED/ERROR from output.json
    returncode: int | None
    timed_out: bool
    seconds: float
    output_tail: str = ""  # last part of test.sh's own output (harness evidence; never shown to models)
    error: str = ""

    def passed(self, tests: Sequence[str]) -> list[str]:
        return [t for t in tests if self.statuses.get(t) == "PASSED"]

    def failed(self, tests: Sequence[str]) -> list[str]:
        return [t for t in tests if self.statuses.get(t) != "PASSED"]


def run_verifier(image: str, tests_dir: str | Path, files: Mapping[str, str], *, timeout: float = DEFAULT_TEST_TIMEOUT,
                 runner: Runner = _run, workdir: str = "/app", memory: str | None = "8g", cpus: str | None = None,
                 tag: str = "") -> VerifierResult:
    """Run the official ``test.sh`` in a fresh, offline container with ``files`` copied over ``workdir``.

    ``files`` maps repo-relative paths to full new contents (an empty mapping runs the base
    commit unchanged). The container is named so a timed-out run can be killed."""
    t0 = time.monotonic()
    name = f"contraprova-verify-{os.getpid()}-{int(t0 * 1000) % 10**9}{('-' + tag) if tag else ''}"
    with tempfile.TemporaryDirectory(prefix="contraprova-verify-") as d:
        sub = Path(d) / "submission"
        logs = Path(d) / "logs"
        sub.mkdir()
        logs.mkdir()
        for rel, text in sorted(files.items()):
            parts = Path(rel).parts
            if not parts or rel.startswith("/") or ".." in parts:
                raise ValueError(f"unsafe path {rel!r}")
            p = sub.joinpath(*parts)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8"))
        # test.sh copies output.json into /logs/verifier before its exit trap creates that
        # directory, so it must exist up front or the per-test statuses are lost.
        (logs / "verifier").mkdir()
        os.chmod(logs, 0o777)
        os.chmod(logs / "verifier", 0o777)
        # Plain ``cp -r`` writes into existing files, so they keep their mode and owner.
        script = f"cp -r /submission/. {workdir}/ && exec bash /tests/test.sh"
        cmd = ["docker", "run", "--rm", "--name", name, "--network", "none",
               "-v", f"{Path(tests_dir).resolve()}:/tests:ro", "-v", f"{sub}:/submission:ro", "-v", f"{logs}:/logs",
               "--entrypoint", "/bin/bash", "-w", workdir]
        if memory:
            cmd += ["--memory", memory]
        if cpus:
            cmd += ["--cpus", cpus]
        cmd += [image, "-c", script]
        timed_out = False
        rc: int | None = None
        out = ""
        try:
            r = runner(cmd, timeout=timeout)
            rc = r.returncode
            out = (r.stdout + b"\n" + r.stderr).decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            timed_out = True
            runner(["docker", "rm", "-f", name], timeout=120)
        reward: int | None = None
        rp = logs / "verifier" / "reward.txt"
        if rp.is_file():
            txt = rp.read_text(errors="replace").strip()
            reward = 1 if txt == "1" else 0
        statuses: dict[str, str] = {}
        op = logs / "verifier" / "output.json"
        error = ""
        if op.is_file():
            try:
                for t in json.loads(op.read_text(errors="replace")).get("tests", []):
                    if isinstance(t, dict) and "name" in t:
                        statuses[str(t["name"])] = str(t.get("status", ""))
            except ValueError:
                error = "output.json did not parse"
        return VerifierResult(reward if not timed_out else 0, statuses, rc, timed_out, time.monotonic() - t0,
                              out[-4000:], error)


def system_df(runner: Runner = _run) -> str:
    r = runner(["docker", "system", "df"], timeout=60)
    return r.stdout.decode(errors="replace")
