# SPDX-License-Identifier: Apache-2.0
"""`BranchService.diff_keys` and `memoir diff`: key-level changes between two
refs, read from the prolly tree without checking either branch out."""

import json
import subprocess

import pytest
from click.testing import CliRunner

from memoir.cli.main import cli
from memoir.services.branch_service import BranchService


def _head(store: str) -> str:
    return subprocess.run(
        ["git", "-C", store, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def branched_store(tmp_path):
    """main holds a.b.c / a.b.g / a.b.x; feat rewrites a.b.c, adds a.b.d, forgets a.b.x."""
    store = str(tmp_path / "store")
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli, ["-s", store, *args])
        assert result.exit_code == 0, result.output
        return result

    run("new", store)
    run("remember", "alpha on main", "-p", "a.b.c", "--replace")
    run("remember", "gamma stays", "-p", "a.b.g", "--replace")
    run("remember", "to be deleted", "-p", "a.b.x", "--replace")
    run("checkout", "-b", "feat")
    run("remember", "alpha changed on feat", "-p", "a.b.c", "--replace")
    run("remember", "delta new on feat", "-p", "a.b.d", "--replace")
    run("forget", "a.b.x", "--force")
    return store


def test_diff_keys_reports_every_change_with_both_values(branched_store):
    changes = BranchService(branched_store).diff_keys("main", "feat")

    by_key = {c["key"]: c for c in changes}
    assert [c["key"] for c in changes] == ["a.b.c", "a.b.d", "a.b.x"]
    assert all(c["namespace"] == "default" for c in changes)

    assert by_key["a.b.c"]["operation"] == "modified"
    assert by_key["a.b.c"]["old"]["content"] == "alpha on main"
    assert by_key["a.b.c"]["new"]["content"] == "alpha changed on feat"

    assert by_key["a.b.d"]["operation"] == "added"
    assert by_key["a.b.d"]["old"] is None
    assert by_key["a.b.d"]["new"]["content"] == "delta new on feat"

    assert by_key["a.b.x"]["operation"] == "removed"
    assert by_key["a.b.x"]["old"]["content"] == "to be deleted"
    assert by_key["a.b.x"]["new"] is None


def test_diff_keys_never_moves_head(branched_store):
    assert _head(branched_store) == "feat"
    BranchService(branched_store).diff_keys("main", "feat")
    BranchService(branched_store).diff_keys("feat", "main")
    assert _head(branched_store) == "feat"


def test_diff_keys_resolves_revisions_and_rejects_unknown_refs(branched_store):
    service = BranchService(branched_store)
    # HEAD~1 is the commit before `forget`: only the deletion separates them.
    assert [c["operation"] for c in service.diff_keys("HEAD~1", "HEAD")] == ["removed"]
    assert service.diff_keys("feat", "HEAD") == []
    with pytest.raises(ValueError, match="Unknown ref"):
        service.diff_keys("no-such-branch", "feat")


def test_diff_command_lists_changed_keys(branched_store):
    runner = CliRunner()

    result = runner.invoke(
        cli, ["--json", "-s", branched_store, "diff", "main", "feat"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["from"] == "main"
    assert data["to"] == "feat"
    assert [(c["key"], c["operation"]) for c in data["changes"]] == [
        ("a.b.c", "modified"),
        ("a.b.d", "added"),
        ("a.b.x", "removed"),
    ]

    result = runner.invoke(cli, ["-s", branched_store, "diff", "main", "feat"])
    assert result.exit_code == 0, result.output
    assert "~ default:a.b.c" in result.output
    assert "+ default:a.b.d" in result.output
    assert "- default:a.b.x" in result.output
    assert _head(branched_store) == "feat"
