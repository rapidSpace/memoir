# SPDX-License-Identifier: Apache-2.0
"""
Agent accounts (issue #189): `memoir login --agent`, the store-local token,
`memoir claim`, `memoir cloud open`, and the push-cap errors.

The conftest points XDG_CONFIG_HOME at an empty directory per test, so the
developer's real ~/.config/memoir/cloud.json is never read or written.

Run with: pytest tests/test_cloud_agent.py -v
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from memoir.cli.main import cli
from memoir.services import agent_account, cloud_auth, repo_meta, sync_service
from memoir.store import repo_mode
from tests.fake_cloud import AGENT_HANDLE, AGENT_KEY, API_KEY, HANDLE, FakeCloud


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def home(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="memoir_agent_")).resolve()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("MEMOIR_API_KEY", raising=False)
    monkeypatch.delenv("MEMOIR_CLOUD_URL", raising=False)
    monkeypatch.delenv("MEMOIR_STORE", raising=False)
    # No GitHub API call for the repo document: the local clone's fields only.
    monkeypatch.setattr(repo_meta, "github_fields", lambda owner, name: {})
    (root / ".memoir").mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def cloud(home, monkeypatch):
    (home / "repos").mkdir()
    with FakeCloud(repos_root=home / "repos") as fake:
        monkeypatch.setenv("MEMOIR_CLOUD_URL", fake.url)
        yield fake


@pytest.fixture
def runner():
    return CliRunner()


def _repo(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@x.io",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c1",
    )
    _git(
        path,
        "remote",
        "add",
        "origin",
        origin or f"git@github.com:someone/{path.name}.git",
    )
    return path


def _agent_login(runner, cloud, home, monkeypatch, name="widgets"):
    """A repo with its plugin store, one memory, and `memoir login --agent` run."""
    repo = _repo(home / "code" / name)
    store = repo_mode.store_for_repo(repo, home)
    runner.invoke(cli, ["new", str(store)], catch_exceptions=False)
    runner.invoke(
        cli,
        ["-s", str(store), "remember", "-p", "workflow.x", "y"],
        catch_exceptions=False,
    )
    monkeypatch.chdir(repo)
    res = runner.invoke(cli, ["login", "--agent"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return repo, store, res


class TestLoginAgent:
    def test_provisions_links_and_keeps_the_token_with_the_store(
        self, runner, cloud, home, monkeypatch
    ):
        _repo, store, res = _agent_login(runner, cloud, home, monkeypatch)
        # What the person sees: handle, address, caps, deadline, the claim hint; never the key.
        assert f"agent account {AGENT_HANDLE} owns {AGENT_HANDLE}/widgets" in res.output
        assert "5 branches" in res.output
        assert "2,000 memories per branch" in res.output
        assert "30 idle days" in res.output
        assert "memoir claim" in res.output
        assert AGENT_KEY not in res.output
        # The token lives with the store, 0600, and nowhere else.
        token = store / ".git" / "memoir-cloud" / "token"
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
        assert json.loads(token.read_text()) == {
            "gateway": cloud.url,
            "api_key": AGENT_KEY,
            "handle": AGENT_HANDLE,
            "claimed": False,
        }
        assert not cloud_auth.config_path().exists()
        assert AGENT_KEY not in (store / ".git" / "config").read_text()
        # origin is set exactly as `remote add` would set it.
        assert (
            _git(store, "remote", "get-url", "origin")
            == f"{cloud.url}/{AGENT_HANDLE}/widgets"
        )
        # The cloud got the client name, the store name and the repo document.
        [start] = cloud.state.agent_starts
        assert start["client_name"].endswith(":widgets")
        assert start["store_name"] == "widgets"
        assert start["repo"]["url"] == "https://github.com/someone/widgets"

    def test_json_output(self, runner, cloud, home, monkeypatch):
        repo = _repo(home / "code" / "widgets")
        monkeypatch.chdir(repo)
        res = runner.invoke(
            cli,
            ["--json", "login", "--agent", "--name", "my-widgets"],
            catch_exceptions=False,
        )
        assert res.exit_code == 0, res.output
        body = json.loads(res.output)
        assert body["handle"] == AGENT_HANDLE
        assert body["address"] == f"{AGENT_HANDLE}/my-widgets"
        assert body["limits"] == {"branches": 5, "keys": 2000}
        assert body["idle_days"] == 30
        assert AGENT_KEY not in res.output

    def test_refused_when_a_login_already_applies(
        self, runner, cloud, home, monkeypatch
    ):
        cloud_auth.save(cloud.url, API_KEY, HANDLE)
        repo = _repo(home / "code" / "widgets")
        monkeypatch.chdir(repo)
        res = runner.invoke(cli, ["login", "--agent"])
        assert res.exit_code == 1
        assert "already logged in" in res.output
        assert cloud.state.agent_starts == []

    def test_refused_when_the_store_has_a_remote(
        self, runner, cloud, home, monkeypatch
    ):
        _repo, store, _ = _agent_login(runner, cloud, home, monkeypatch)
        res = runner.invoke(cli, ["login", "--agent"])
        assert res.exit_code == 1
        assert "already links this store" in res.output
        _git(store, "remote", "remove", "origin")
        res = runner.invoke(cli, ["login", "--agent"])
        assert res.exit_code == 1
        assert "already holds an agent token" in res.output
        assert len(cloud.state.agent_starts) == 1

    def test_gateway_without_agent_accounts_and_rate_limit(
        self, runner, cloud, home, monkeypatch
    ):
        repo = _repo(home / "code" / "widgets")
        monkeypatch.chdir(repo)
        cloud.state.agent_accounts = False
        res = runner.invoke(cli, ["login", "--agent"])
        assert res.exit_code == 1
        assert "does not offer agent accounts" in res.output
        cloud.state.agent_accounts = True
        cloud.state.agent_rate_limited = True
        res = runner.invoke(cli, ["login", "--agent"])
        assert res.exit_code == 1
        assert "Too many agent accounts" in res.output
        assert not (
            repo_mode.store_for_repo(repo, home) / ".git" / "memoir-cloud" / "token"
        ).exists()


class TestResolution:
    def test_cloud_commands_use_the_store_token(self, runner, cloud, home, monkeypatch):
        _repo, _store, _ = _agent_login(runner, cloud, home, monkeypatch)
        res = runner.invoke(cli, ["push"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert f"pushed main to {AGENT_HANDLE}/widgets" in res.output
        pushes = [
            r for r in cloud.state.requests if r.path.endswith("git-receive-pack")
        ]
        assert pushes
        assert pushes[-1].headers["Authorization"] == f"Bearer {AGENT_KEY}"
        res = runner.invoke(cli, ["--json", "remote", "list"], catch_exceptions=False)
        [origin] = json.loads(res.output)["remotes"]
        assert origin["logged_in"] is True
        assert origin["agent_store"] is True
        # The machine has no login of its own.
        res = runner.invoke(cli, ["login", "--status"], catch_exceptions=False)
        assert "not logged in to any gateway" in res.output

    def test_other_stores_never_see_the_token(self, runner, cloud, home, monkeypatch):
        _repo, _store, _ = _agent_login(runner, cloud, home, monkeypatch)
        other = home / "other-store"
        runner.invoke(cli, ["new", str(other)], catch_exceptions=False)
        res = runner.invoke(
            cli, ["-s", str(other), "remote", "add", f"{AGENT_HANDLE}/widgets"]
        )
        assert res.exit_code == 1
        assert "memoir login" in res.output
        assert not any(
            r.headers.get("Authorization") == f"Bearer {AGENT_KEY}"
            for r in cloud.state.requests
            if r.path.startswith("/stores/by-name")
        )

    def test_token_bound_to_its_gateway(self, home):
        store = home / "s"
        (store / ".git").mkdir(parents=True)
        agent_account.save_token(store, "https://GW.example/", "mck_agent", "agent-x")
        assert sync_service.api_key("https://gw.example", store) == "mck_agent"
        assert sync_service.api_key("https://other.example", store) == ""
        assert sync_service.cloud_enabled(store) is True
        assert sync_service.cloud_enabled(home / "nowhere") is False
        # Precedence: env > store token > saved login.
        cloud_auth.save("https://gw.example", "mck_saved", "feng")
        assert sync_service.api_key("https://gw.example", store) == "mck_agent"
        assert sync_service.api_key("https://gw.example") == "mck_saved"
        os.environ["MEMOIR_API_KEY"] = "mck_env"
        try:
            assert sync_service.api_key("https://gw.example", store) == "mck_env"
        finally:
            del os.environ["MEMOIR_API_KEY"]
        assert (
            sync_service.redact("key mck_agent here", store_path=store)
            == "key *** here"
        )


class TestClaim:
    def test_moves_the_store_rewrites_origin_and_keeps_the_token(
        self, runner, cloud, home, monkeypatch
    ):
        _repo, store, _ = _agent_login(runner, cloud, home, monkeypatch)
        cloud_auth.save(cloud.url, API_KEY, HANDLE)
        res = runner.invoke(cli, ["claim"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert f"claimed: {HANDLE}/widgets" in res.output
        assert (
            _git(store, "remote", "get-url", "origin")
            == f"{cloud.url}/{HANDLE}/widgets"
        )
        token = json.loads((store / ".git" / "memoir-cloud" / "token").read_text())
        assert token == {
            "gateway": cloud.url,
            "api_key": AGENT_KEY,
            "handle": HANDLE,
            "claimed": True,
        }
        # remote list no longer calls it an agent store (nothing left to claim).
        res = runner.invoke(cli, ["--json", "remote", "list"], catch_exceptions=False)
        assert json.loads(res.output)["remotes"][0]["agent_store"] is False
        claims = [r for r in cloud.state.requests if r.path == "/auth/agent/claim"]
        assert claims[-1].headers["Authorization"] == f"Bearer {API_KEY}"
        assert json.loads(claims[-1].body) == {"agent_token": AGENT_KEY, "name": None}
        # The token still pushes, now to the claimed address.
        runner.invoke(
            cli, ["remember", "-p", "workflow.y", "z"], catch_exceptions=False
        )
        res = runner.invoke(cli, ["push"], catch_exceptions=False)
        assert res.exit_code == 0
        assert f"pushed main to {HANDLE}/widgets" in res.output
        # No second claim: the token is the human's now.
        res = runner.invoke(cli, ["claim"])
        assert res.exit_code == 1
        assert "not a live agent token" in res.output

    def test_name_taken_then_another_name(self, runner, cloud, home, monkeypatch):
        _repo, store, _ = _agent_login(runner, cloud, home, monkeypatch)
        cloud_auth.save(cloud.url, API_KEY, HANDLE)
        cloud.state.create_store("widgets")  # the human already has one
        res = runner.invoke(cli, ["--json", "claim"])
        assert res.exit_code == 1
        assert "--name" in res.output
        res = runner.invoke(
            cli, ["claim", "--name", "widgets-agent"], catch_exceptions=False
        )
        assert res.exit_code == 0, res.output
        assert (
            _git(store, "remote", "get-url", "origin")
            == f"{cloud.url}/{HANDLE}/widgets-agent"
        )

    def test_warns_when_the_repo_url_differs(self, runner, cloud, home, monkeypatch):
        repo, _store, _ = _agent_login(runner, cloud, home, monkeypatch)
        _git(
            repo, "remote", "set-url", "origin", "git@github.com:elsewhere/widgets.git"
        )
        cloud_auth.save(cloud.url, API_KEY, HANDLE)
        res = runner.invoke(cli, ["claim"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "provisioned for https://github.com/someone/widgets" in res.output
        assert "https://github.com/elsewhere/widgets" in res.output

    def test_logs_the_human_in_first(self, runner, cloud, home, monkeypatch):
        _repo, _store, _ = _agent_login(runner, cloud, home, monkeypatch)
        res = runner.invoke(cli, ["claim", "--no-browser"], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert "Open http://fake/cli/login and enter ABCD-EFGH" in res.output
        assert cloud_auth.entry(cloud.url)["api_key"] == API_KEY
        assert f"claimed: {HANDLE}/widgets" in res.output

    def test_without_a_token(self, runner, cloud, home):
        store = home / "plain"
        runner.invoke(cli, ["new", str(store)], catch_exceptions=False)
        res = runner.invoke(cli, ["-s", str(store), "claim"])
        assert res.exit_code == 1
        assert "holds no agent token" in res.output


class TestCloudOpen:
    def test_single_use_link(self, runner, cloud, home, monkeypatch):
        _repo, _store, _ = _agent_login(runner, cloud, home, monkeypatch)
        res = runner.invoke(
            cli, ["--json", "cloud", "open", "--no-browser"], catch_exceptions=False
        )
        assert res.exit_code == 0, res.output
        body = json.loads(res.output)
        assert body["url"] == f"{cloud.url}/auth/agent/open?id=code1&token=once1"
        assert body["expires_in"] == 300
        assert AGENT_KEY not in res.output
        res = runner.invoke(
            cli, ["cloud", "open", "--no-browser"], catch_exceptions=False
        )
        assert "open?id=code2" in res.output
        assert "single use" in res.output

    def test_after_a_claim_say_sign_in(self, runner, cloud, home, monkeypatch):
        _repo, _store, _ = _agent_login(runner, cloud, home, monkeypatch)
        cloud_auth.save(cloud.url, API_KEY, HANDLE)
        assert runner.invoke(cli, ["claim"]).exit_code == 0
        res = runner.invoke(cli, ["cloud", "open", "--no-browser"])
        assert res.exit_code == 1
        assert "sign in" in res.output


class TestPushCaps:
    def test_rejections_are_shown_verbatim_with_the_claim_hint(self):
        stderr = (
            "To http://gw/agent-k7x2m9/widgets\n"
            " ! [remote rejected] feat/x -> feat/x (branch limit (5) reached)\n"
            " ! [remote rejected] main -> main (memory limit (2000) exceeded on this branch)\n"
            " ! [rejected] dev -> dev (non-fast-forward)\n"
            "error: failed to push some refs to 'http://gw/agent-k7x2m9/widgets'\n"
        )
        assert sync_service.agent_cap_reasons(stderr) == [
            "branch limit (5) reached",
            "memory limit (2000) exceeded on this branch",
        ]
        assert sync_service.agent_cap_reasons("error: something else") == []

    def test_push_create_with_the_token_is_refused(
        self, runner, cloud, home, monkeypatch
    ):
        _repo, store, _ = _agent_login(runner, cloud, home, monkeypatch)
        _git(store, "remote", "remove", "origin")
        res = runner.invoke(cli, ["push", "--create", "another"])
        assert res.exit_code == 1
        assert "memoir claim" in res.output
