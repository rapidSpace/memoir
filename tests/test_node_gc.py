# SPDX-License-Identifier: Apache-2.0
"""`memoir gc`: dead prolly node files are found by walking every ref's root,
and deleting them leaves every key on every branch readable."""

import json
import os
import subprocess

import pytest
from click.testing import CliRunner

from memoir.cli.main import cli
from memoir.store.node_gc import (
    collect_garbage,
    local_nodes,
    nodes_dir,
    reachable_nodes,
)
from memoir.store.prolly_adapter import ProllyTreeStore


@pytest.fixture
def busy_store(tmp_path):
    """300 keys on main, 50 more on feat, written one put at a time so the
    node store is mostly dead nodes."""
    path = str(tmp_path / "store")
    res = CliRunner().invoke(cli, ["new", path])
    assert res.exit_code == 0, res.output
    store = ProllyTreeStore(path, auto_commit=False)
    for i in range(300):
        store.put(("default",), f"k.{i:04d}.x", {"content": "v" * 500 + str(i)})
    store.commit("bulk main")
    store.tree.create_branch("feat")
    for i in range(300, 350):
        store.put(("default",), f"k.{i:04d}.x", {"content": "w" * 500 + str(i)})
    store.commit("bulk feat")
    store.tree.checkout("main")
    del store
    return path


def _readable(path, branch, keys):
    store = ProllyTreeStore(path)
    store.tree.checkout(branch)
    try:
        return all(store.get(("default",), k) is not None for k in keys)
    finally:
        store.tree.checkout("main")


def test_reachable_nodes_is_a_small_subset(busy_store):
    files = local_nodes(busy_store)
    live = reachable_nodes(busy_store)
    assert live <= files
    assert len(live) < len(files) / 10
    assert len(live) >= 3  # at least one node per commit root


def test_dry_run_reports_without_deleting(busy_store):
    before = local_nodes(busy_store)
    result = collect_garbage(busy_store)
    assert result.applied is False
    assert result.deleted == 0
    assert result.orphans == len(before) - result.reachable
    assert result.bytes_orphaned > 0
    assert local_nodes(busy_store) == before


def test_apply_deletes_only_dead_nodes_and_every_key_still_reads(busy_store):
    result = collect_garbage(busy_store, apply=True)
    assert result.applied is True
    assert result.deleted == result.orphans > 0
    assert local_nodes(busy_store) == reachable_nodes(busy_store)
    assert len(local_nodes(busy_store)) == result.reachable
    main_keys = [f"k.{i:04d}.x" for i in range(300)]
    feat_keys = [f"k.{i:04d}.x" for i in range(350)]
    assert _readable(busy_store, "main", main_keys)
    assert _readable(busy_store, "feat", feat_keys)
    # The feat-only keys are still absent on main: nothing crossed over.
    store = ProllyTreeStore(busy_store)
    assert store.get(("default",), "k.0320.x") is None


def test_gc_command_json_and_text(busy_store):
    runner = CliRunner()
    res = runner.invoke(cli, ["--json", "-s", busy_store, "gc"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["applied"] is False
    assert data["orphans"] > 0
    res = runner.invoke(cli, ["-s", busy_store, "gc"])
    assert res.exit_code == 0, res.output
    assert "dead node files" in res.output
    assert "--apply" in res.output
    res = runner.invoke(cli, ["--json", "-s", busy_store, "gc", "--apply"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["deleted"] == data["orphans"]
    res = runner.invoke(cli, ["-s", busy_store, "gc"])
    assert "nothing to collect" in res.output


def test_gc_leaves_other_files_alone(busy_store):
    nodes = nodes_dir(busy_store)
    (nodes / "not-a-node.txt").write_text("keep me")
    (nodes.parent / "prolly_config_tree_config").write_text("{}")
    collect_garbage(busy_store, apply=True)
    assert (nodes / "not-a-node.txt").exists()
    assert (nodes.parent / "prolly_config_tree_config").exists()
    # git's own state is untouched.
    assert (
        subprocess.run(
            ["git", "-C", busy_store, "fsck", "--no-dangling"], capture_output=True
        ).returncode
        == 0
    )
    assert os.path.exists(os.path.join(busy_store, "data", "prolly_config_tree_config"))
