"""Board CLI tests for Infisical (AFC-348):  Linear client pairs from AI Fleet Coordinator prod /linear
before the local files, and one shared minted token per app (LINEAR_<SEAT>_CACHE_TOKEN).

A mock Infisical runs beside test_board.py's mock Linear;  nothing reaches the network.

    python3 scripts/linear-board/test_board_infisical.py
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_board as T


class MockInfisical:
    """Just enough of Infisical for the board's /linear reads and writes (AFC-348)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.secrets: dict = {}
        self.requests: list = []
        self.down = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                data = json.loads(body) if body else {}
                path = urllib.parse.urlsplit(self.path).path
                with mock.lock:
                    mock.requests.append((method, path))
                    if mock.down:
                        return self._send(503, {"message": "down"})
                    if path == "/api/v1/auth/universal-auth/login":
                        ok = (
                            data.get("clientId") == "inf-id"
                            and data.get("clientSecret") == "inf-secret"
                        )
                        return (
                            self._send(200, {"accessToken": "inf-token"})
                            if ok
                            else self._send(401, {})
                        )
                    if self.headers.get("Authorization") != "Bearer inf-token":
                        return self._send(401, {})
                    if path == "/api/v1/projects/slug/ai-fleet-coordinator":
                        return self._send(
                            200, {"project": {"id": "proj-afc", "slug": "ai-fleet-coordinator"}}
                        )
                    if path == "/api/v3/secrets/raw" and method == "GET":
                        return self._send(
                            200,
                            {
                                "secrets": [
                                    {"secretKey": k, "secretValue": v}
                                    for k, v in mock.secrets.items()
                                ]
                            },
                        )
                    if path.startswith("/api/v3/secrets/raw/"):
                        name = urllib.parse.unquote(path.rsplit("/", 1)[1])
                        if (
                            data.get("workspaceId") != "proj-afc"
                            or data.get("secretPath") != "/linear"
                        ):
                            return self._send(400, {"message": "wrong place"})
                        if method == "PATCH" and name not in mock.secrets:
                            return self._send(404, {"message": "not found"})
                        mock.secrets[name] = data["secretValue"]
                        return self._send(200, {"secret": {"secretKey": name}})
                    return self._send(404, {})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        return H


class InfisicalTests(T.BoardTest):
    """Credentials from Infisical first, and one shared token per seat through LINEAR_<SEAT>_CACHE_TOKEN."""

    seat_app = T.SeatAppTests.seat_app
    cache = T.SeatAppTests.cache
    as_seat = T.SeatAppTests.as_seat

    def setUp(self):
        super().setUp()
        self.vault = MockInfisical()
        self.addCleanup(self.vault.close)
        self.env.update(
            {
                "INFISICAL_AUTOMATION_CLIENT_ID": "inf-id",
                "INFISICAL_AUTOMATION_CLIENT_SECRET": "inf-secret",
                "LINEAR_BOARD_INFISICAL_API": self.vault.url,
            }
        )

    def legacy_host(self):
        """A server or cloud host:  no fleet-secrets, only an INFISICAL_AUTOMATION identity (AFC-348)."""
        return {"LINEAR_BOARD_FLEET_SECRETS": str(self.tmp / "no-such-fleet-secrets")}

    def pair_in_old_folder(self):
        cid, secret = self.seat_app("CLAUDE", "Claude", file=False)
        self.vault.secrets.update(
            {"LINEAR_CLAUDE_CLIENT_ID": cid, "LINEAR_CLAUDE_CLIENT_SECRET": secret}
        )

    def test_legacy_host_reads_the_pair_and_shares_the_token_through_the_old_folder(self):
        self.pair_in_old_folder()
        p = self.as_seat("CLAUDE", "whoami", extra=self.legacy_host())
        self.assertIn("viewer:      Claude (app user)", p.stdout)
        self.assertIn(
            "credentials: Infisical AI Fleet Coordinator prod /linear (LINEAR_CLAUDE_CLIENT_ID",
            p.stdout,
        )
        self.assertIn(
            "token store: Infisical AI Fleet Coordinator prod /linear LINEAR_CLAUDE_CACHE_TOKEN (no token on disk)",
            p.stdout,
        )
        shared = json.loads(self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"])
        self.assertIn(shared["access_token"], self.mock.tokens)
        # a second process on the same host (or another host) reads the shared copy and mints nothing
        self.as_seat("CLAUDE", "whoami", extra=self.legacy_host())
        self.assertEqual(self.mock.mints, 1)
        self.assertFalse(
            list((self.tmp / "home").rglob("*.token")), "no token file anywhere under HOME"
        )

    def test_legacy_host_remints_a_refused_shared_token_and_shares_it_again(self):
        self.pair_in_old_folder()
        self.as_seat("CLAUDE", "whoami", extra=self.legacy_host())
        first = json.loads(self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"])
        first["minted_at"] -= 2 * 86400
        self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"] = json.dumps(first)
        self.mock.tokens.clear()  # Linear now refuses the shared token
        self.as_seat("CLAUDE", "whoami", extra=self.legacy_host())
        self.assertEqual(self.mock.mints, 2)
        second = json.loads(self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"])
        self.assertNotEqual(second["access_token"], first["access_token"])

    def test_infisical_outage_keeps_fleet_ops_credentials(self):
        self.seat_app("CLAUDE", "Claude")  # CLAUDE's pair in Fleet-OPS /linear
        self.vault.down = True
        p = self.as_seat("CLAUDE", "whoami")
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (CLAUDE_LINEAR_CLIENT_ID", p.stdout
        )
        self.assertIn("viewer:      Claude (app user)", p.stdout)

    def test_identity_from_fleet_secrets_when_not_in_the_environment(self):
        cid, secret = self.seat_app("CLAUDE", "Claude", file=False)
        self.vault.secrets.update(
            {"LINEAR_CLAUDE_CLIENT_ID": cid, "LINEAR_CLAUDE_CLIENT_SECRET": secret}
        )
        stub = self.tmp / "fleet-secrets"
        stub.write_text(
            "#!/bin/sh\n"
            '[ "$1" = get ] || exit 2\n'
            'case "$2" in INFISICAL_AUTOMATION_CLIENT_ID) echo inf-id;; INFISICAL_AUTOMATION_CLIENT_SECRET) echo inf-secret;; *) exit 3;; esac\n'
        )
        stub.chmod(0o700)
        p = self.as_seat(
            "CLAUDE",
            "whoami",
            drop=("INFISICAL_AUTOMATION_CLIENT_ID", "INFISICAL_AUTOMATION_CLIENT_SECRET"),
            extra={"LINEAR_BOARD_FLEET_SECRETS": str(stub)},
        )
        self.assertIn(
            "credentials: Infisical AI Fleet Coordinator prod /linear (LINEAR_CLAUDE_CLIENT_ID",
            p.stdout,
        )

    def test_no_identity_means_no_infisical_calls(self):
        cid, secret = self.seat_app(
            "CLAUDE", "Claude", file=False
        )  # the pair comes from the environment
        self.as_seat(
            "CLAUDE",
            "whoami",
            drop=("INFISICAL_AUTOMATION_CLIENT_ID", "INFISICAL_AUTOMATION_CLIENT_SECRET"),
            extra={
                "LINEAR_BOARD_FLEET_SECRETS": str(self.tmp / "no-such-fleet-secrets"),
                "LINEAR_CLAUDE_CLIENT_ID": cid,
                "LINEAR_CLAUDE_CLIENT_SECRET": secret,
            },
        )
        self.assertEqual(self.vault.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
