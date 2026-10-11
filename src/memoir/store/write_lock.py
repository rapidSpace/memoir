# SPDX-License-Identifier: Apache-2.0
"""One exclusive write lock per store, shared by every writer process.

Memoir's prolly tree commits onto whatever branch the store has checked
out, and ``--branch`` routing works by checking the target out for the
duration of a write (:meth:`BranchService.routed_to`). Without a lock a
second writer landing in that window commits onto the routed branch
(issue #179). Every write path takes this lock — the adapter's ``put`` /
``delete`` / ``commit`` and the routed block as a whole — so a concurrent
writer waits for the restore instead.

The lock is a ``flock`` on ``<store>/.git/memoir-write.lock``, so it works
across processes (the CLI, the plugin's hooks, the UI server) and vanishes
with the process that holds it. Within a process it is re-entrant per
thread: a routed write holds it across checkout → write → restore and the
nested ``put`` re-enters.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

LOCK_FILE = "memoir-write.lock"
DEFAULT_TIMEOUT_S = 120.0

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class StoreBusyError(TimeoutError):
    """Another writer held the store's write lock for longer than the timeout."""


class _Held:
    __slots__ = ("depth", "fd")

    def __init__(self, fd: int):
        self.fd = fd
        self.depth = 1


_local = threading.local()


def _held() -> dict[str, _Held]:
    held = getattr(_local, "held", None)
    if held is None:
        held = {}
        _local.held = held
    return held


def lock_path(store_path: str | os.PathLike[str]) -> Path:
    return Path(store_path) / ".git" / LOCK_FILE


@contextlib.contextmanager
def store_write_lock(
    store_path: str | os.PathLike[str], timeout: float = DEFAULT_TIMEOUT_S
) -> Iterator[None]:
    """Hold the store's exclusive write lock for the block.

    Re-entrant within a thread. Raises :class:`StoreBusyError` when another
    holder keeps it beyond ``timeout`` seconds. A store without a ``.git``
    directory (memory mode, a worktree) is left unlocked.
    """
    path = lock_path(store_path)
    key = str(path)
    held = _held()
    if key in held:
        held[key].depth += 1
        try:
            yield
        finally:
            held[key].depth -= 1
            if held[key].depth == 0:
                _release(held.pop(key))
        return

    if fcntl is None or not path.parent.is_dir():
        yield
        return

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    delay = 0.02
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreBusyError(
                        f"Another memoir process has been writing to {Path(store_path)} "
                        f"for over {timeout:.0f}s (lock: {path}). Retry when it finishes."
                    ) from None
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
    except BaseException:
        os.close(fd)
        raise

    held[key] = _Held(fd)
    try:
        yield
    finally:
        if held[key].depth == 1:
            _release(held.pop(key))
        else:
            held[key].depth -= 1


def _release(entry: _Held) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(entry.fd, fcntl.LOCK_UN)
    finally:
        os.close(entry.fd)
