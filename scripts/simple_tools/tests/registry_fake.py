"""An in-process registry API for tests:  the real RegistryService over a temp store.

Clients reach the Operator's registry only through registry_api (AFC-720), so a test of a
client needs the API, not a local registry file.  FakeRegistry gives it one without a
socket:  while installed it replaces registry_api's one network seam (`_open`) with a
function that hands the signed request to listener_registry.RegistryService over a store
in a temp folder, so the signing, the paths, the query and body encoding and the error
dialect are the real ones.  It also pins registry_api's knob lookup to "unset", so a test
never reaches Infisical.

    with FakeRegistry() as fake:
        answer = registry_api.call(fake.env(), "register", {...})
        row = fake.lookup("codex", "server", "default", "s-1")

A request for any other origin fails as a dead host does (URLError, which the client
answers as `timeout`).  This file is repo-only:  fleet-cli ships no tests.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from typing import Any

from simple_tools import listener_registry as LREG
from simple_tools import operator_registry as OR
from simple_tools import registry_api

#: A test key, long enough for the API's floor (listener_registry.MIN_KEY_LEN).
KEY = "fake-registry-key-0123456789abcdef"
#: An origin no resolver answers, so a leak past the fake can never reach a real host.
URL = "https://registry.fake.invalid"


#: The key an unregistered principal signs with:  long enough to be a key, held by no one.
UNKNOWN_PRINCIPAL_KEY = "u" * 40


class FakeRegistry:
    """The registry API in process.  Use as a context manager, or call install() and
    uninstall() (an addCleanup-friendly pair)."""

    def __init__(
        self,
        root: str | None = None,
        *,
        keys: dict[str, str] | None = None,
        url: str = URL,
    ) -> None:
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        if root is None:
            self._tmp = tempfile.TemporaryDirectory()
            root = self._tmp.name
        self.url = url.rstrip("/")
        parts = urllib.parse.urlsplit(self.url)
        if parts.path or parts.query or parts.fragment:
            # The registry URL is the bare origin by contract (registry_api adds
            # /registry/v1/ itself), so a path here could never be served.
            raise ValueError("FakeRegistry url must be a bare origin, not %r" % url)
        self.db = os.path.join(root, "server", "registry.sqlite3")
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        OR.SessionRegistry(self.db).close_db()
        self.keys = dict(keys) if keys else {"OPERATOR": KEY}
        self.service = LREG.RegistryService(self.db, self.keys)
        #: Every request served, as (method, path with query), in order.
        self.requests: list[tuple[str, str]] = []
        self._saved: tuple[Any, Any] | None = None
        self._servers: list[Any] = []

    # ---- the client's view ------------------------------------------------

    def env(self, principal: str = "OPERATOR", **extra: str) -> dict[str, str]:
        """An environment that points registry_api at this fake, signing as `principal`."""
        env = {
            registry_api.URL_ENV: self.url,
            # A principal the fake holds no key for signs with a key the service will
            # refuse, so the test exercises the API's own refusal, not a KeyError here.
            registry_api.KEY_VAR: self.keys.get(principal, UNKNOWN_PRINCIPAL_KEY),
            registry_api.PRINCIPAL_ENV: principal,
        }
        env.update(extra)
        return env

    def lookup(self, platform: str, location: str, scope: str, session_id: str) -> dict | None:
        """The store's row for one identity, read straight from the fake's own file."""
        reg = OR.SessionRegistry(self.db, read_only=True)
        try:
            return reg.lookup(platform, location, scope, session_id)
        finally:
            reg.close_db()

    def bootstrap_env(self, registry_url: str, *, home: str | None = None) -> dict[str, str]:
        """A real scoped-token transport for entry-script tests, never ambient credentials.

        The optional home exercises the launcher's protected store.  Other callers bind
        their synthetic token explicitly; both use the real direct reader's HTTP checks.
        """
        import http.server
        from pathlib import Path
        import threading
        from types import SimpleNamespace

        from fleet_knobs.source import PROJECT_ID
        from simple_tools.platform_letter_bootstrap import TOKEN_ENV

        token = "st." + secrets.token_hex(24)
        values = {
            registry_api.URL_ENV: registry_url,
            registry_api.KEY_VAR: self.keys["OPERATOR"],
        }
        calls = []
        self.bootstrap_requests = calls

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                calls.append(parts.path)
                status, answer = 403, {}
                if self.headers.get("Authorization") == "Bearer " + token:
                    if parts.path == "/api/v2/service-token":
                        status, answer = 200, {
                            "projectId": PROJECT_ID,
                            "workspace": PROJECT_ID,
                            "permissions": ["read"],
                            "scopes": [{"environment": "prod", "secretPath": "/operator-registry"}],
                            "expiresAt": "2099-01-01T00:00:00Z",
                        }
                    elif parts.path.startswith("/api/v3/secrets/raw/"):
                        name = urllib.parse.unquote(parts.path.rsplit("/", 1)[-1])
                        query = urllib.parse.parse_qs(parts.query)
                        if (
                            query.get("workspaceId") == [PROJECT_ID]
                            and query.get("environment") == ["prod"]
                            and query.get("secretPath") == ["/operator-registry"]
                            and name in values
                        ):
                            status, answer = 200, {
                                "secret": {
                                    "secretKey": name,
                                    "secretValue": values[name],
                                    "type": "shared",
                                    "workspace": PROJECT_ID,
                                    "environment": "prod",
                                    "secretPath": "/operator-registry",
                                }
                            }
                body = json.dumps(answer).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self._servers.append(SimpleNamespace(close=close))
        env = {"FLEET_SECRETS_API": "http://127.0.0.1:%d/api" % server.server_address[1]}
        if home is None:
            env[TOKEN_ENV] = token
        else:
            directory = Path(home) / ".config" / "platform-letter"
            directory.mkdir(parents=True, mode=0o700)
            directory.chmod(0o700)
            store = directory / "infisical-registry.token"
            store.write_text(token + "\n", encoding="ascii")
            store.chmod(0o600)
        return env

    def serve(self, state_dir: str) -> str:
        """The same store behind a real listener on loopback, for a test that runs the client in a subprocess (the
        patched seam reaches only this process).  Answers the base URL;  uninstall() stops the listener."""
        from simple_tools import listener_api as API

        server = API.ListenerReadApi("127.0.0.1", 0, state_dir, {}, registry=self.service)
        server.start()
        self._servers.append(server)
        host, port = server.address
        return "http://%s:%d" % (host, port)

    # ---- install --------------------------------------------------------------

    def _open(self, request: urllib.request.Request, timeout: int) -> bytes:
        parts = urllib.parse.urlsplit(request.full_url)
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        if origin != self.url:
            raise urllib.error.URLError("no fake registry at %s" % origin)
        target = parts.path + (("?" + parts.query) if parts.query else "")
        method = request.get_method()
        self.requests.append((method, target))
        body = request.data if isinstance(request.data, bytes) else b""
        status, payload = self.service.handle(method, target, dict(request.header_items()), body)
        raw = json.dumps(payload).encode("utf-8")
        if 200 <= status < 300:
            return raw
        raise urllib.error.HTTPError(request.full_url, status, "fake", Message(), io.BytesIO(raw))

    def install(self) -> FakeRegistry:
        """Replace registry_api's two seams (_open and _knob_value).  Install the fake as
        the outermost patch:  a test that patches either seam itself must do so after
        install() and restore it before uninstall(), or uninstall() restores the seams
        the fake saved over the test's own."""
        if self._saved is None:
            self._saved = (registry_api._open, registry_api._knob_value)
            registry_api._open = self._open  # type: ignore[assignment]
            registry_api._knob_value = lambda name: ""  # type: ignore[assignment]
        return self

    def uninstall(self) -> None:
        while self._servers:
            self._servers.pop().close()
        if self._saved is not None:
            registry_api._open, registry_api._knob_value = self._saved  # type: ignore[assignment]
            self._saved = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def __enter__(self) -> FakeRegistry:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.uninstall()
