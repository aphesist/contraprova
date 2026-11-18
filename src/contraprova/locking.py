"""One portable exclusive lock for the single writer.

``exclusive_lock`` is the only place the kernel touches an OS locking primitive:

- POSIX: ``fcntl.flock(LOCK_EX)`` on the open log file itself, exactly what ``EventLog.append``
  always did.
- Windows: ``msvcrt.locking`` over one byte of a sidecar lock file (``<log>.lock``). The log file
  is not locked itself because Windows byte-range locks are mandatory and would block the readers.

Either way the lock is exclusive, waits (forever, or up to ``timeout`` seconds) and is released in
a ``finally``. Exceeding the timeout raises ``LockTimeout``; nothing is ever written without the lock.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import IO, Any, Iterator

try:  # POSIX only
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows only
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

POLL_SECONDS = 0.01


class LockTimeout(TimeoutError):
    """The exclusive lock could not be taken within the timeout."""


def _deadline(timeout: float | None) -> float | None:
    return None if timeout is None else time.monotonic() + timeout


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


@contextmanager
def _flock(f: IO[Any], timeout: float | None, what: str) -> Iterator[None]:
    assert fcntl is not None
    if timeout is None:
        fcntl.flock(f, fcntl.LOCK_EX)
    else:
        deadline = _deadline(timeout)
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, PermissionError):
                if _expired(deadline):
                    raise LockTimeout(f"could not lock {what} within {timeout}s") from None
                time.sleep(POLL_SECONDS)
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def _msvcrt_lock(lock_path: str | os.PathLike, timeout: float | None) -> Iterator[None]:
    assert msvcrt is not None
    with open(lock_path, "a+b") as lf:
        deadline = _deadline(timeout)
        while True:
            lf.seek(0)
            try:
                msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if _expired(deadline):
                    raise LockTimeout(f"could not lock {os.fspath(lock_path)} within {timeout}s") from None
                time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def exclusive_lock(f: IO[Any], lock_path: str | os.PathLike, *, timeout: float | None = None) -> Iterator[None]:
    """Hold an exclusive lock for the ``with`` body, released on any exit.

    ``f`` is the open log file (locked itself on POSIX). ``lock_path`` is the sidecar file used on
    Windows (created if missing, never read). ``timeout`` is in seconds; ``None`` waits forever,
    which is the behaviour ``EventLog`` has always had. Raises ``LockTimeout`` on expiry.
    """
    if fcntl is not None:
        with _flock(f, timeout, getattr(f, "name", "the log")):
            yield
    elif msvcrt is not None:
        with _msvcrt_lock(lock_path, timeout):
            yield
    else:  # pragma: no cover - no known platform
        raise RuntimeError("no file locking primitive (fcntl or msvcrt) on this platform")
