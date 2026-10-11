# SPDX-License-Identifier: Apache-2.0
"""``--branch`` routing after issue #179: reads open the branch as a snapshot
and never move HEAD; writes check the branch out under the store's write
lock, so a concurrent writer waits instead of landing on the routed branch."""

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from memoir.cli.main import cli
from memoir.services.memory_service import MemoryService
from memoir.store.prolly_adapter import ProllyTreeStore, ReadOnlyStoreError
from memoir.store.write_lock import StoreBusyError, store_write_lock


def _head(store: str) -> str:
    return subprocess.run(
        ["git", "-C", store, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _run(store: str, *args, env=None):
    result = CliRunner().invoke(cli, ["--json", "-s", store, *args], env=env)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def store(tmp_path):
    """main holds a.b.c; agents/x holds a.b.c (other value) and a.b.x."""
    path = str(tmp_path / "store")
    _run(path, "new", path)
    _run(path, "remember", "on main", "-p", "a.b.c", "--replace")
    _run(
        path,
        "remember",
        "on the agent branch",
        "-p",
        "a.b.c",
        "--replace",
        "--branch",
        "agents/x",
    )
    _run(
        path,
        "remember",
        "agent only",
        "-p",
        "a.b.x",
        "--replace",
        "--branch",
        "agents/x",
    )
    assert _head(path) == "main"
    return path


def test_routed_reads_are_snapshots_that_never_touch_head(store):
    head_file = Path(store) / ".git" / "HEAD"
    before = (head_file.stat().st_mtime_ns, head_file.read_text())

    got = _run(store, "get", "a.b.c", "a.b.x", "--branch", "agents/x")
    by_key = {i["key"]: i for i in got["items"]}
    assert by_key["a.b.c"]["value"]["content"] == "on the agent branch"
    assert by_key["a.b.x"]["found"] is True

    on_main = _run(store, "get", "a.b.c")
    assert on_main["items"][0]["value"]["content"] == "on main"

    # The snapshot path leaves .git/HEAD untouched, byte for byte and mtime.
    assert (head_file.stat().st_mtime_ns, head_file.read_text()) == before


def test_snapshot_store_is_read_only_and_rejects_unknown_branches(store):
    view = ProllyTreeStore(store, ref="agents/x")
    assert view.get(("default",), "a.b.x")["content"] == "agent only"
    assert view.get(("default",), "nope") is None
    with pytest.raises(ReadOnlyStoreError):
        view.put(("default",), "k", {"content": "x"})
    with pytest.raises(ReadOnlyStoreError):
        view.delete(("default",), "a.b.x")
    with pytest.raises(ValueError, match="Branch 'typo' does not exist"):
        ProllyTreeStore(store, ref="typo")

    result = CliRunner().invoke(cli, ["-s", store, "get", "a.b.c", "--branch", "typo"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_service_reads_through_a_ref(store):
    service = MemoryService(store, ref="agents/x")
    result = service.get(["a.b.c"], "default")
    assert result.items[0]["value"]["content"] == "on the agent branch"
    assert _head(store) == "main"


def test_routed_write_holds_the_lock_so_a_concurrent_writer_waits(store):
    """The #179 race: a plain `remember` during a routed write must land on
    the writer's own branch (main), never on the routed one."""
    routed_started = threading.Event()
    release = threading.Event()
    plain_done = threading.Event()
    errors: list[BaseException] = []

    def routed_writer():
        try:
            from memoir.services.branch_service import BranchService

            with BranchService(store).routed_to("agents/x", auto_create=False):
                routed_started.set()
                assert _head(store) == "agents/x"
                release.wait(10)
                asyncio.run(
                    MemoryService(store).remember(
                        "slow routed write", "default", paths=["a.b.slow"]
                    )
                )
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    def plain_writer():
        try:
            routed_started.wait(10)
            asyncio.run(
                MemoryService(store).remember(
                    "plain write", "default", paths=["a.b.plain"]
                )
            )
            plain_done.set()
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    t1 = threading.Thread(target=routed_writer)
    t2 = threading.Thread(target=plain_writer)
    t1.start()
    t2.start()
    assert routed_started.wait(10)
    # While the routed block holds the lock the plain writer is blocked.
    time.sleep(0.5)
    assert not plain_done.is_set()
    release.set()
    t1.join(30)
    t2.join(30)
    assert not errors, errors
    assert plain_done.is_set()
    assert _head(store) == "main"

    on_main = {
        i["key"]: i["found"]
        for i in _run(store, "get", "a.b.plain", "a.b.slow")["items"]
    }
    assert on_main == {"a.b.plain": True, "a.b.slow": False}
    on_branch = {
        i["key"]: i["found"]
        for i in _run(store, "get", "a.b.plain", "a.b.slow", "--branch", "agents/x")[
            "items"
        ]
    }
    assert on_branch == {"a.b.plain": False, "a.b.slow": True}


def test_write_lock_is_reentrant_per_thread_and_times_out_across_threads(store):
    with store_write_lock(store):
        with store_write_lock(store):  # same thread: re-enters
            pass
        outcome: list[str] = []

        def other():
            try:
                with store_write_lock(store, timeout=0.3):
                    outcome.append("acquired")
            except StoreBusyError:
                outcome.append("busy")

        t = threading.Thread(target=other)
        t.start()
        t.join(5)
        assert outcome == ["busy"]
    # Released: the next holder gets in at once.
    with store_write_lock(store, timeout=0.3):
        pass
