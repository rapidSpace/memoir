# SPDX-License-Identifier: Apache-2.0
"""
Agent accounts (issue #189): a throwaway memoir-cloud account that owns one
store for one code repo, provisioned over HTTP with no human login.

``memoir login --agent`` asks the gateway for one; the token it returns is
kept **with the store**, under ``<store>/.git/memoir-cloud/token`` (mode
0600), never in ``~/.config/memoir/cloud.json``, so another repo on the
machine never picks it up. A human can later ``memoir claim`` the store into
their real account; the same token keeps working, now scoped to the claimed
store. ``memoir cloud open`` turns the token into a one-time browser link,
the agent account's only way into the web UI.

Resolution used by every cloud command, for gateway G: ``MEMOIR_API_KEY``
→ the store-local token when its gateway is G → the login saved for G. A
store-local token is never sent to another gateway.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

from memoir.services.base import ServiceError

logger = logging.getLogger(__name__)

TOKEN_FILE = Path(".git") / "memoir-cloud" / "token"
TIMEOUT = 15.0
CLIENT_NAME_MAX = 64


class AgentError(ServiceError):
    """A failure of one of the agent-account calls, already worded for the user."""


# --------------------------------------------------------------------------
# The store-local token
# --------------------------------------------------------------------------


def token_path(store_path: str | Path) -> Path:
    return Path(store_path) / TOKEN_FILE


def load_token(store_path: str | Path | None) -> dict[str, Any] | None:
    """``{"gateway", "api_key", "handle", "claimed"}`` from the store, or None
    when the store holds no (readable, well-formed) token. ``claimed`` is set
    once ``memoir claim`` moved the store: the token is the human's then."""
    if not store_path:
        return None
    path = token_path(store_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("api_key") or not data.get("gateway"):
        logger.debug("ignoring malformed agent token at %s", path)
        return None
    return {
        "gateway": str(data["gateway"]),
        "api_key": str(data["api_key"]),
        "handle": data.get("handle"),
        "claimed": bool(data.get("claimed")),
    }


def save_token(
    store_path: str | Path,
    gateway: str,
    api_key: str,
    handle: str | None,
    *,
    claimed: bool = False,
) -> Path:
    """Write the token file (0600; the directory exists after any cloud
    command, created here otherwise)."""
    from memoir.services.cloud_auth import normalize_gateway

    path = token_path(store_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "gateway": normalize_gateway(gateway),
            "api_key": api_key,
            "handle": handle,
            "claimed": claimed,
        }
    )
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return path


def token_for(store_path: str | Path | None, gateway: str) -> str:
    """The store-local key for exactly this gateway, else ``""``."""
    from memoir.services.cloud_auth import normalize_gateway

    found = load_token(store_path)
    if found and normalize_gateway(found["gateway"]) == normalize_gateway(gateway):
        return found["api_key"]
    return ""


def client_name(repo_name: str) -> str:
    """``<host>:<repo dir name>``: what the cloud names the token after."""
    host = socket.gethostname() or "memoir-cli"
    return f"{host}:{repo_name}"[:CLIENT_NAME_MAX]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _detail(resp: Any) -> str:
    """The gateway's ``detail`` as text: a string, ``{"message"}``, or the
    first validation error's ``msg``."""
    try:
        detail = resp.json().get("detail")
    except Exception:
        return (resp.text or "").strip()[:200]
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)
    if isinstance(detail, list) and detail:
        first = detail[0]
        return str(first.get("msg", first) if isinstance(first, dict) else first)
    return str(detail or "").strip()


def _post(gateway: str, path: str, body: dict[str, Any], key: str | None) -> Any:
    import httpx

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        return httpx.post(
            f"{gateway.rstrip('/')}{path}", json=body, headers=headers, timeout=TIMEOUT
        )
    except httpx.HTTPError as e:
        from memoir.services.sync_service import redact

        raise AgentError(f"{gateway}{path}: {redact(str(e), key)}") from None


def start(
    gateway: str, *, client: str, store_name: str, repo: dict[str, Any] | None
) -> dict[str, Any]:
    """``POST /auth/agent/start`` (no auth): a new agent account with one
    store named ``store_name``. The answer carries the token exactly once."""
    resp = _post(
        gateway,
        "/auth/agent/start",
        {"client_name": client, "store_name": store_name, "repo": repo},
        None,
    )
    if resp.status_code in (200, 201):
        return dict(resp.json())
    if resp.status_code == 404:
        raise AgentError(
            f"{gateway} does not offer agent accounts; run `memoir login` instead"
        )
    if resp.status_code == 429:
        raise AgentError(
            _detail(resp) or "too many agent accounts from this address today"
        )
    if resp.status_code == 422:
        raise AgentError(f'"{store_name}" is not a valid store name: {_detail(resp)}')
    raise AgentError(f"POST /auth/agent/start → {resp.status_code}: {_detail(resp)}")


class StoreNameTaken(AgentError):
    """The claimer already owns a store of that name (``claim --name`` picks another)."""

    def __init__(self, message: str):
        super().__init__(message, code=1)


def claim(
    gateway: str, *, human_key: str, agent_token: str, name: str | None
) -> dict[str, Any]:
    """``POST /auth/agent/claim`` as the human: the agent's store moves to
    them and the token is rebound (still scoped to that store)."""
    resp = _post(
        gateway,
        "/auth/agent/claim",
        {"agent_token": agent_token, "name": name},
        human_key,
    )
    if resp.status_code == 200:
        return dict(resp.json())
    if resp.status_code == 409:
        raise StoreNameTaken(_detail(resp) or "you already have a store of that name")
    if resp.status_code == 403:
        raise AgentError(
            "the saved login belongs to an agent account itself; "
            "`memoir login` as yourself first"
        )
    if resp.status_code == 401:
        from memoir.services.sync_service import NOT_SIGNED_IN_MESSAGE

        raise AgentError(NOT_SIGNED_IN_MESSAGE)
    if resp.status_code == 404:
        raise AgentError(f"{gateway} does not offer agent accounts")
    raise AgentError(_detail(resp) or f"POST /auth/agent/claim → {resp.status_code}")


def session_url(gateway: str, *, agent_token: str) -> dict[str, Any]:
    """``POST /auth/agent/session``: a single-use browser link (5 minutes)."""
    resp = _post(gateway, "/auth/agent/session", {}, agent_token)
    if resp.status_code == 200:
        return dict(resp.json())
    if resp.status_code == 403:
        raise AgentError(
            "this store's token no longer belongs to an agent account (it was "
            f"claimed); sign in at {gateway.rstrip('/')}/login instead"
        )
    if resp.status_code == 401:
        raise AgentError(
            "the store's agent token was not accepted; it may have expired"
        )
    raise AgentError(_detail(resp) or f"POST /auth/agent/session → {resp.status_code}")
