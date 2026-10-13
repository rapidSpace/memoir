# SPDX-License-Identifier: Apache-2.0
"""In-process fake of the memoir-cloud api-gateway for cloud-sync tests.

Implements the subset of the gateway contract that ``SyncService`` talks to,
with GitHub-style ``<owner>/<store>`` addressing:

- ``GET  /auth/whoami`` (``handle`` configurable, ``None`` → ``null``)
- ``POST /stores`` (422 on an invalid name, 409 on a duplicate),
  ``GET /stores``, ``GET /stores/by-name/{owner}/{store}`` (404 for unknown
  **or** not-owned)
- ``POST /{owner}/{store}/chunks/negotiate``, ``GET /{owner}/{store}/chunks``,
  ``PUT|GET /{owner}/{store}/chunks/{hash}``, ``POST /{owner}/{store}/chunks/batch``
  (multipart; ``stored`` / ``existing`` / ``rejected`` exactly like production)
- ``GET /{owner}/{store}/info/refs``, ``POST .../git-receive-pack``,
  ``POST .../git-upload-pack`` — delegated to the real ``git http-backend``
  CGI over a bare repo, so the git half of push/fetch/pull is exercised with
  the real ``git`` binary and no network.
- ``POST /auth/agent/start`` (no auth; 201 with ``AGENT_KEY`` owning one
  store under ``AGENT_HANDLE``; 404 with ``agent_accounts=False``, 429 with
  ``agent_rate_limited``), ``POST /auth/agent/claim`` (the human's key moves
  the store to them; 409 ``store_name_taken``; 400 bad token; 403 agent
  caller), ``POST /auth/agent/session`` (a one-time link; 403 once claimed).
  The agent key resolves only its own store; ``POST /stores`` with it is 403
  ``key_scoped``.

Every request is recorded (method, path, headers, body) so tests can assert
on ordering (chunk PUTs before receive-pack), batching, and that the bearer
header actually arrives. Fault injection: ``fail_puts`` (always 500 for
those hashes), ``flaky_puts`` (500 once, then succeed), ``page_size`` (cap on
chunk-listing pages, to exercise pagination), ``batch_fail_502`` (the next N
batch requests answer 502 "object store unavailable"; ``-1`` = forever),
``batch_fail_after_ok`` (502 forever once that many batches succeeded, to
simulate a network cut mid-push), ``batch_max_parts`` (413 above it, to
exercise splitting), ``reject_hashes`` (reported in ``rejected``).
"""

from __future__ import annotations

import email.parser
import email.policy
import gzip
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from pathlib import Path

API_KEY = "mck_test_secret_key_DO_NOT_LEAK"
HANDLE = "feng-zhang"
# Agent accounts (issue #189): what `POST /auth/agent/start` hands out.
AGENT_KEY = "mck_agent_token_DO_NOT_LEAK"
AGENT_HANDLE = "agent-k7x2m9"
OTHER_HANDLE = "someone-else"

_STORE_NAME_RE = re.compile(r"^(?!\.)[A-Za-z0-9._-]{1,100}$")
_STORE_NAME_RULES = (
    "store names are 1-100 letters, digits, '.', '_' or '-'; "
    "not starting with '.'; not ending in .git"
)


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""


@dataclass
class FakeCloudState:
    repos_root: Path
    api_key: str = API_KEY
    handle: str | None = HANDLE
    # store_id -> store object (incl. owner_handle)
    stores: dict[str, dict] = field(default_factory=dict)
    # store_id -> {hash: bytes}
    chunks: dict[str, dict[str, bytes]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    fail_puts: set[str] = field(default_factory=set)
    flaky_puts: set[str] = field(default_factory=set)
    page_size: int | None = None
    batch_fail_502: int = 0
    batch_fail_after_ok: int | None = None
    batch_max_parts: int | None = None
    reject_hashes: set[str] = field(default_factory=set)
    # PATCH /stores/by-name/<o>/<s>: last {"repo": ...} body per store id,
    # and an optional forced status for failure tests.
    repo_meta: dict[str, object] = field(default_factory=dict)
    # gzip batch bodies (issue #166): advertise on chunk routes and info/refs,
    # inflate Content-Encoding: gzip; `reject_gzip` answers 415 to gzip bodies
    # (a server that misadvertised); `advertise_gzip=False` is an old server.
    advertise_gzip: bool = True
    reject_gzip: bool = False
    # Device login (`memoir login`, issue #168): polls answer "pending" this
    # many times, then `login_outcome` ("approved" | "denied" | "expired").
    login_pending_polls: int = 1
    login_outcome: str = "approved"
    login_polls: int = 0
    login_starts: list[dict] = field(default_factory=list)
    revoked_keys: list[str] = field(default_factory=list)
    # Agent accounts: `agent_accounts=False` is a gateway with the feature
    # off (404); `agent_rate_limited` answers 429; `agent_starts` records
    # the bodies; `agent_claimed` flips once a human claimed the store (the
    # same token then acts as the human's, scoped to that store).
    agent_accounts: bool = True
    agent_rate_limited: bool = False
    agent_starts: list[dict] = field(default_factory=list)
    agent_store_id: str | None = None
    agent_claimed: bool = False
    agent_sessions: int = 0
    patch_status: int | None = None
    batches_ok: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def create_store(self, name: str, owner_handle: str | None = None) -> dict:
        owner_handle = owner_handle or self.handle or HANDLE
        store_id = f"str_{len(self.stores) + 1:04d}"
        repo = self.repos_root / store_id
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-q", str(repo)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "http.receivepack", "true"],
            check=True,
            capture_output=True,
        )
        store = {
            "id": store_id,
            "name": name,
            "owner_user_id": f"usr_{owner_handle}",
            "owner_handle": owner_handle,
            "created_at": "2026-10-03T00:00:00Z",
        }
        self.stores[store_id] = store
        self.chunks.setdefault(store_id, {})
        return store

    def lookup(self, owner: str, name: str, viewer: str | None = None) -> dict | None:
        """Owner-scoped, case-insensitive resolution (None if not the
        viewer's; the viewer is the human by default)."""
        viewer = viewer or self.handle
        for s in self.stores.values():
            if (
                s["owner_handle"].lower() == owner.lower()
                and s["name"].lower() == name.lower()
                and s["owner_handle"] == viewer
            ):
                return s
        return None

    def known_key(self, key: str | None) -> bool:
        return key == self.api_key or (
            key == AGENT_KEY and self.agent_store_id is not None
        )

    def viewer_for(self, key: str | None) -> str | None:
        """Whose handle a bearer key acts under: the human's (None until they
        chose one), the agent's (until the store is claimed, when the token
        becomes the human's), or nobody's."""
        if key == self.api_key:
            return self.handle
        if key == AGENT_KEY and self.agent_store_id is not None:
            return self.handle if self.agent_claimed else AGENT_HANDLE
        return None

    def repo_for(self, owner: str, name: str, viewer: str | None = None) -> Path:
        store = self.lookup(owner, name, viewer)
        assert store is not None
        return self.repos_root / store["id"]

    def chunks_for(
        self, owner: str, name: str, viewer: str | None = None
    ) -> dict[str, bytes]:
        store = self.lookup(owner, name, viewer)
        assert store is not None
        return self.chunks[store["id"]]

    def paths(self, method: str | None = None) -> list[str]:
        return [r.path for r in self.requests if method is None or r.method == method]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeCloudState  # set by the server factory

    def log_message(self, *_args):  # silence the default access log
        pass

    # ---- helpers -------------------------------------------------------
    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json"):
        self.send_response(status)
        if self.state.advertise_gzip and (
            "/chunks" in self.path or "/info/refs" in self.path
        ):
            self.send_header("Memoir-Accept-Encoding", "gzip")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode())

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        return auth[len("Bearer ") :] if auth.startswith("Bearer ") else None

    def _authed(self) -> bool:
        return self.state.known_key(self._bearer())

    def _viewer(self) -> str | None:
        return self.state.viewer_for(self._bearer())

    def _is_agent(self) -> bool:
        return self._bearer() == AGENT_KEY and not self.state.agent_claimed

    def _record(self, body: bytes) -> None:
        with self.state.lock:
            self.state.requests.append(
                Recorded(self.command, self.path, dict(self.headers.items()), body)
            )

    # ---- dispatch ------------------------------------------------------
    def do_GET(self):
        self._dispatch(b"")

    def do_POST(self):
        self._dispatch(self._read_body())

    def do_PUT(self):
        self._dispatch(self._read_body())

    def do_PATCH(self):
        self._dispatch(self._read_body())

    def _dispatch(self, body: bytes) -> None:
        self._record(body)
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)

        if path == "/auth/cli/start" and self.command == "POST":
            req = json.loads(body or b"{}")
            with self.state.lock:
                self.state.login_starts.append(req)
            self._json(
                200,
                {
                    "device_code": "dev-code-1",
                    "user_code": "ABCD-EFGH",
                    "verification_url": "http://fake/cli/login",
                    "verification_url_complete": "http://fake/cli/login?code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 0,
                },
            )
            return
        if path == "/auth/cli/poll" and self.command == "POST":
            req = json.loads(body or b"{}")
            if req.get("device_code") != "dev-code-1":
                self._json(404, {"detail": "unknown device code"})
                return
            with self.state.lock:
                self.state.login_polls += 1
                polls = self.state.login_polls
            if polls <= self.state.login_pending_polls:
                self._json(200, {"status": "pending"})
            elif self.state.login_outcome == "approved":
                self._json(
                    200,
                    {
                        "status": "approved",
                        "api_key": self.state.api_key,
                        "handle": self.state.handle,
                        "gateway": f"http://{self.headers.get('Host')}",
                    },
                )
            else:
                self._json(200, {"status": self.state.login_outcome})
            return
        if path == "/auth/agent/start" and self.command == "POST":
            self._agent_start(body)
            return
        if not self._authed():
            self._json(401, {"detail": "invalid or missing API key"})
            return
        if path == "/auth/agent/claim" and self.command == "POST":
            self._agent_claim(body)
            return
        if path == "/auth/agent/session" and self.command == "POST":
            if not self.state.agent_accounts:
                self._json(404, {"detail": "Not Found"})
                return
            if not self._is_agent():
                self._json(
                    403,
                    {
                        "detail": "only an agent token opens the UI this way; sign in at /login instead"
                    },
                )
                return
            with self.state.lock:
                self.state.agent_sessions += 1
                n = self.state.agent_sessions
            base = f"http://{self.headers.get('Host')}"
            self._json(
                200,
                {
                    "url": f"{base}/auth/agent/open?id=code{n}&token=once{n}",
                    "expires_in": 300,
                },
            )
            return
        if path == "/auth/keys/self/revoke" and self.command == "POST":
            with self.state.lock:
                self.state.revoked_keys.append(self.state.api_key)
            self._send(204)
            return

        if path == "/auth/whoami":
            self._json(
                200,
                {
                    "user_id": "usr_test",
                    "key_id": "k_test",
                    "scopes": ["read", "write"],
                    "expires_at": None,
                    "tier": "PRO",
                    "handle": self._viewer(),
                },
            )
            return

        if path == "/stores":
            if self._bearer() == AGENT_KEY:
                # A token scoped to one store: it can neither create another
                # nor see beyond its own.
                if self.command == "POST":
                    self._json(403, {"error": "key_scoped"})
                else:
                    self._json(
                        200,
                        [
                            s
                            for s in self.state.stores.values()
                            if s["id"] == self.state.agent_store_id
                        ],
                    )
                return
            if self.command == "POST":
                name = json.loads(body or b"{}").get("name", "")
                if (
                    not _STORE_NAME_RE.match(name)
                    or name.lower().endswith(".git")
                    or name in (".", "..")
                ):
                    self._json(
                        422,
                        {
                            "detail": [
                                {
                                    "type": "value_error",
                                    "loc": ["body", "name"],
                                    "msg": f"Value error, {_STORE_NAME_RULES}",
                                }
                            ]
                        },
                    )
                    return
                if any(
                    s["owner_handle"] == self.state.handle
                    and s["name"].lower() == name.lower()
                    for s in self.state.stores.values()
                ):
                    self._json(409, {"detail": "store name already exists"})
                    return
                self._json(201, self.state.create_store(name))
            else:
                self._json(
                    200,
                    [
                        s
                        for s in self.state.stores.values()
                        if s["owner_handle"] == self.state.handle
                    ],
                )
            return

        if path.startswith("/stores/by-name/"):
            segs = path[len("/stores/by-name/") :].split("/")
            store = (
                self.state.lookup(*segs, viewer=self._viewer())
                if len(segs) == 2
                else None
            )
            if store is None:
                self._json(404, {"detail": "store not found"})
                return
            if self.command == "PATCH":
                if self.state.patch_status:
                    self._json(self.state.patch_status, {"detail": "injected"})
                    return
                payload = json.loads(body or b"{}")
                repo = payload.get("repo")
                if repo is not None and not str(repo.get("url", "")).startswith(
                    ("http://", "https://")
                ):
                    self._json(422, {"detail": "repo.url must be http(s)"})
                    return
                with self.state.lock:
                    self.state.repo_meta[store["id"]] = repo
                self._json(200, {**store, "repo": repo})
                return
            self._json(200, store)
            return

        # /{owner}/{store}/...
        segs = path.strip("/").split("/", 2)
        if len(segs) == 3:
            owner, name, tail = segs
            if self.state.lookup(owner, name, viewer=self._viewer()) is None:
                self._json(404, {"detail": "store not found"})
                return
            if tail.startswith("chunks"):
                self._chunks(owner, name, tail, query, body)
            elif tail in ("info/refs", "git-receive-pack", "git-upload-pack"):
                self._git_cgi(owner, name, tail, parts.query, body)
            else:
                self._json(404, {"detail": "unknown route"})
            return

        self._json(404, {"detail": "not found"})

    # ---- agent accounts (issue #189) -----------------------------------
    def _agent_start(self, body: bytes) -> None:
        if not self.state.agent_accounts:
            self._json(404, {"detail": "Not Found"})
            return
        if self.state.agent_rate_limited:
            self._json(
                429,
                {
                    "detail": "Too many agent accounts were created from this address today."
                },
            )
            return
        req = json.loads(body or b"{}")
        name = str(req.get("store_name", ""))
        if (
            not _STORE_NAME_RE.match(name)
            or name.lower().endswith(".git")
            or name in (".", "..")
            or not req.get("client_name")
        ):
            self._json(
                422,
                {
                    "detail": [
                        {
                            "type": "value_error",
                            "loc": ["body", "store_name"],
                            "msg": f"Value error, {_STORE_NAME_RULES}",
                        }
                    ]
                },
            )
            return
        with self.state.lock:
            self.state.agent_starts.append(req)
            store = self.state.create_store(name, AGENT_HANDLE)
            store["repo"] = req.get("repo")
            self.state.agent_store_id = store["id"]
            self.state.agent_claimed = False
        self._json(
            201,
            {
                "api_key": AGENT_KEY,
                "handle": AGENT_HANDLE,
                "store": name,
                "address": f"{AGENT_HANDLE}/{name}",
                "gateway": f"http://{self.headers.get('Host')}",
                "limits": {"branches": 5, "keys": 2000},
                "idle_days": 30,
                "key_expires_at": "2027-10-12T00:00:00+00:00",
            },
        )

    def _agent_claim(self, body: bytes) -> None:
        if not self.state.agent_accounts:
            self._json(404, {"detail": "Not Found"})
            return
        if self._is_agent():
            self._json(403, {"detail": "sign in with a regular account to claim"})
            return
        req = json.loads(body or b"{}")
        store = self.state.stores.get(self.state.agent_store_id or "")
        if (
            req.get("agent_token") != AGENT_KEY
            or store is None
            or self.state.agent_claimed
        ):
            self._json(400, {"detail": "that is not a live agent token"})
            return
        name = req.get("name") or store["name"]
        taken = any(
            s["owner_handle"] == self.state.handle and s["name"].lower() == name.lower()
            for s in self.state.stores.values()
        )
        if taken:
            self._json(
                409,
                {
                    "detail": {
                        "error": "store_name_taken",
                        "message": f"you already have a store named {name!r}; pass another name",
                    }
                },
            )
            return
        with self.state.lock:
            store["name"] = name
            store["owner_handle"] = self.state.handle
            store["owner_user_id"] = f"usr_{self.state.handle}"
            self.state.agent_claimed = True
        repo = store.get("repo") or {}
        self._json(
            200,
            {
                "address": f"{self.state.handle}/{name}",
                "handle": self.state.handle,
                "store": name,
                "store_id": store["id"],
                "repo_url": repo.get("url") if isinstance(repo, dict) else None,
            },
        )

    # ---- chunk protocol ------------------------------------------------
    def _chunks(
        self, owner: str, name: str, tail: str, query: dict, body: bytes
    ) -> None:
        store_chunks = self.state.chunks_for(owner, name, self._viewer())
        if tail == "chunks/negotiate" and self.command == "POST":
            req = json.loads(body or b"{}")
            have = req.get("have", [])
            want = req.get("want", [])
            if len(have) + len(want) > 1000:
                self._json(400, {"detail": "batch too large"})
                return
            self._json(
                200,
                {
                    "missing_on_server": [h for h in have if h not in store_chunks],
                    "present_on_server": [h for h in want if h in store_chunks],
                },
            )
            return
        if tail == "chunks/batch" and self.command == "POST":
            self._batch(store_chunks, body)
            return
        if tail == "chunks" and self.command == "GET":
            limit = int(query.get("limit", ["1000"])[0])
            if self.state.page_size:
                limit = min(limit, self.state.page_size)
            after = query.get("after", [None])[0]
            keys = sorted(store_chunks)
            if after:
                keys = [k for k in keys if k > after]
            page = keys[:limit]
            self._json(
                200,
                {"hashes": page, "next": page[-1] if len(page) == limit else None},
            )
            return
        if tail.startswith("chunks/"):
            chunk_hash = tail[len("chunks/") :]
            if self.command == "PUT":
                if chunk_hash in self.state.fail_puts:
                    self._json(500, {"detail": "injected failure"})
                    return
                if chunk_hash in self.state.flaky_puts:
                    with self.state.lock:
                        self.state.flaky_puts.discard(chunk_hash)
                    self._json(503, {"detail": "injected transient failure"})
                    return
                if len(body) > 8 * 1024 * 1024:
                    self._json(413, {"detail": "too large"})
                    return
                with self.state.lock:
                    existed = chunk_hash in store_chunks
                    store_chunks[chunk_hash] = body
                self._send(200 if existed else 201)
                return
            if self.command == "GET":
                data = store_chunks.get(chunk_hash)
                if data is None:
                    self._json(404, {"detail": "chunk not found"})
                else:
                    self._send(200, data, "application/octet-stream")
                return
        self._json(404, {"detail": "unknown chunk route"})

    def _batch(self, store_chunks: dict[str, bytes], body: bytes) -> None:
        st = self.state
        if "Content-Length" not in self.headers:
            self._json(411, {"detail": "Content-Length required"})
            return
        encoding = (self.headers.get("Content-Encoding") or "").lower()
        if encoding:
            if encoding != "gzip" or st.reject_gzip or not st.advertise_gzip:
                self._json(415, {"detail": f"unsupported encoding {encoding}"})
                return
            try:
                body = gzip.decompress(body)
            except OSError:
                self._json(400, {"detail": "corrupt gzip stream"})
                return
        with st.lock:
            if st.batch_fail_502 != 0 or (
                st.batch_fail_after_ok is not None
                and st.batches_ok >= st.batch_fail_after_ok
            ):
                if st.batch_fail_502 > 0:
                    st.batch_fail_502 -= 1
                fail = True
            else:
                fail = False
        if fail:
            self._json(502, {"detail": "object store unavailable"})
            return
        ctype = self.headers.get("Content-Type", "")
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        parts = list(msg.iter_parts()) if msg.is_multipart() else []
        if len(parts) > (st.batch_max_parts or 500) or len(body) > 32 * 1024 * 1024:
            self._json(413, {"detail": "too many parts or bytes"})
            return
        stored: list[str] = []
        existing: list[str] = []
        rejected: dict[str, str] = {}
        for part in parts:
            name = part.get_param("name", header="content-disposition") or ""
            has_file = (
                part.get_param("filename", header="content-disposition") is not None
            )
            data = part.get_payload(decode=True) or b""
            if not has_file:
                rejected[name] = "not a file part"
            elif not re.match(r"^[0-9a-f]{16,128}$", name):
                rejected[name] = "bad hash"
            elif len(data) > 8 * 1024 * 1024:
                rejected[name] = "too large"
            elif name in st.reject_hashes:
                rejected[name] = "injected"
            else:
                with st.lock:
                    if name in store_chunks:
                        if name not in existing and name not in stored:
                            existing.append(name)
                    else:
                        store_chunks[name] = data
                        stored.append(name)
        with st.lock:
            st.batches_ok += 1
        self._json(200, {"stored": stored, "existing": existing, "rejected": rejected})

    # ---- git smart-HTTP via git http-backend ---------------------------
    def _git_cgi(
        self, owner: str, name: str, tail: str, query: str, body: bytes
    ) -> None:
        repo = self.state.repo_for(owner, name, self._viewer())
        env = {
            **os.environ,
            "GIT_PROJECT_ROOT": str(repo.parent),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": f"/{repo.name}/{tail}",
            "REQUEST_METHOD": self.command,
            "QUERY_STRING": query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_ADDR": "127.0.0.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "GATEWAY_INTERFACE": "CGI/1.1",
        }
        enc = self.headers.get("Content-Encoding")
        if enc:
            env["HTTP_CONTENT_ENCODING"] = enc
        proc = subprocess.run(
            ["git", "http-backend"],
            input=body,
            env=env,
            capture_output=True,
            check=False,
        )
        head, _, payload = proc.stdout.partition(b"\r\n\r\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.decode("latin-1").splitlines():
            key, _, value = line.partition(":")
            value = value.strip()
            if key.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((key, value))
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)


class FakeCloud:
    """Context manager: starts the fake gateway on a free localhost port."""

    def __init__(self, repos_root: Path):
        self.state = FakeCloudState(repos_root=repos_root)
        handler = type("BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeCloud:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
