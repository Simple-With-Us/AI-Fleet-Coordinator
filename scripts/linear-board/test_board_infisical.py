"""Board CLI tests for the Linear credentials (AFC-348, AFC-473):  the client pairs come from the
environment first and from `fleet-secrets get` second, and one shared minted token per app is kept in
the AI Fleet Coordinator prod /linear folder (LINEAR_<SEAT>_CACHE_TOKEN).

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
    """Credentials from the environment first and from `fleet-secrets get` second, and one shared token
    per app through LINEAR_<SEAT>_CACHE_TOKEN."""

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

    def legacy_host(self, **extra):
        """A server or cloud host:  no fleet-secrets, so the pair comes from the environment (AFC-473).  Such
        a host cannot verify a seat bearer, so it runs without an identity:  reads use the shared Fleet app."""
        return dict(
            {"LINEAR_BOARD_FLEET_SECRETS": str(self.tmp / "no-such-fleet-secrets")}, **extra
        )

    def fleet_env(self):
        """The shared Fleet app's pair in the environment, as a host with no fleet-secrets must export it."""
        return {
            "LINEAR_FLEET_CLIENT_ID": T.CLIENT_ID,
            "LINEAR_FLEET_CLIENT_SECRET": T.CLIENT_SECRET,
        }

    def test_a_host_without_fleet_secrets_takes_the_pair_from_the_environment(self):
        p = self.board("whoami", drop=("AGENT_SEAT",), extra=self.legacy_host(**self.fleet_env()))
        self.assertIn("viewer:      Fleet (app user)", p.stdout)
        self.assertIn(
            "credentials: environment (LINEAR_FLEET_CLIENT_ID, LINEAR_FLEET_CLIENT_SECRET)",
            p.stdout,
        )
        self.assertIn(
            "token store: Infisical AI Fleet Coordinator prod /linear LINEAR_FLEET_CACHE_TOKEN (no token on disk)",
            p.stdout,
        )
        shared = json.loads(self.vault.secrets["LINEAR_FLEET_CACHE_TOKEN"])
        self.assertIn(shared["access_token"], self.mock.tokens)
        self.assertEqual(shared["viewer"]["name"], "Fleet")  # the viewer is kept too
        # a second process on the same host (or another host) reads the shared copy and mints nothing
        self.board("whoami", drop=("AGENT_SEAT",), extra=self.legacy_host(**self.fleet_env()))
        self.assertEqual(self.mock.mints, 1)
        self.assertFalse(
            list((self.tmp / "home").rglob("*.token")), "no token file anywhere under HOME"
        )

    def test_a_host_without_fleet_secrets_remints_a_refused_shared_token_and_shares_it_again(self):
        self.board("whoami", drop=("AGENT_SEAT",), extra=self.legacy_host(**self.fleet_env()))
        first = json.loads(self.vault.secrets["LINEAR_FLEET_CACHE_TOKEN"])
        first["minted_at"] -= 2 * 86400
        self.vault.secrets["LINEAR_FLEET_CACHE_TOKEN"] = json.dumps(first)
        self.mock.tokens.clear()  # Linear now refuses the shared token
        self.board("whoami", drop=("AGENT_SEAT",), extra=self.legacy_host(**self.fleet_env()))
        self.assertEqual(self.mock.mints, 2)
        second = json.loads(self.vault.secrets["LINEAR_FLEET_CACHE_TOKEN"])
        self.assertNotEqual(second["access_token"], first["access_token"])

    def test_a_host_without_fleet_secrets_and_without_a_pair_fails_closed_naming_the_variables(
        self,
    ):
        p = self.board("whoami", drop=("AGENT_SEAT",), extra=self.legacy_host(), code=4)
        self.assertIn("FLEET_LINEAR_CLIENT_ID", p.stderr)
        self.assertIn("LINEAR_FLEET_CLIENT_ID", p.stderr)
        self.assertIn("LINEAR_CLIENT_ID", p.stderr)
        self.assertEqual(self.mock.mints, 0)

    def test_the_ai_fleet_pair_is_read_through_fleet_secrets_and_not_through_infisical(self):
        cid, secret = self.seat_app("CLAUDE", "Claude", file=False)
        self.afc_put("LINEAR_CLAUDE_CLIENT_ID", cid)
        self.afc_put("LINEAR_CLAUDE_CLIENT_SECRET", secret)
        p = self.as_seat("CLAUDE", "whoami")
        self.assertIn(
            "credentials: Infisical AI Fleet Coordinator prod /linear (LINEAR_CLAUDE_CLIENT_ID",
            p.stdout,
        )
        # the loader was asked for that project by name, and the direct Infisical call was never made
        self.assertIn("get LINEAR_CLAUDE_CLIENT_ID --project ai-fleet-coordinator", self.fs_verbs())
        self.assertEqual(
            [r for r in self.vault.requests if r[1].startswith("/api/v3/secrets/raw")], []
        )

    def test_the_environment_pair_beats_the_stored_pair(self):
        cid, secret = self.seat_app("CLAUDE", "Claude", file=False)
        self.afc_put("LINEAR_CLAUDE_CLIENT_ID", "stored-client-id")
        self.afc_put("LINEAR_CLAUDE_CLIENT_SECRET", "stored-client-secret")
        p = self.as_seat(
            "CLAUDE",
            "whoami",
            extra={
                "LINEAR_CLAUDE_CLIENT_ID": cid,
                "LINEAR_CLAUDE_CLIENT_SECRET": secret,
            },
        )
        self.assertIn(
            "credentials: environment (LINEAR_CLAUDE_CLIENT_ID, LINEAR_CLAUDE_CLIENT_SECRET)",
            p.stdout,
        )
        # the stored pair was never read:  the environment came first
        self.assertEqual(
            [v for v in self.fs_verbs() if v.startswith("get LINEAR_CLAUDE_CLIENT_ID")], []
        )

    def test_identity_from_fleet_secrets_when_not_in_the_environment(self):
        cid, secret = self.seat_app("CLAUDE", "Claude", file=False)
        self.afc_put("LINEAR_CLAUDE_CLIENT_ID", cid)
        self.afc_put("LINEAR_CLAUDE_CLIENT_SECRET", secret)
        p = self.as_seat(
            "CLAUDE",
            "whoami",
            drop=("INFISICAL_AUTOMATION_CLIENT_ID", "INFISICAL_AUTOMATION_CLIENT_SECRET"),
        )
        self.assertIn(
            "credentials: Infisical AI Fleet Coordinator prod /linear (LINEAR_CLAUDE_CLIENT_ID",
            p.stdout,
        )

    def test_infisical_outage_keeps_fleet_ops_credentials(self):
        self.seat_app("CLAUDE", "Claude")  # CLAUDE's pair in Fleet-OPS /linear
        self.vault.down = True
        p = self.as_seat("CLAUDE", "whoami")
        self.assertIn(
            "credentials: Infisical Fleet-OPS prod /linear (CLAUDE_LINEAR_CLIENT_ID", p.stdout
        )
        self.assertIn("viewer:      Claude (app user)", p.stdout)

    def test_no_identity_means_no_infisical_calls(self):
        cid, secret = self.seat_app(
            "CLAUDE", "Claude", file=False
        )  # the pair comes from the environment
        self.as_seat(
            "CLAUDE",
            "whoami",
            drop=("INFISICAL_AUTOMATION_CLIENT_ID", "INFISICAL_AUTOMATION_CLIENT_SECRET"),
            extra={
                "LINEAR_CLAUDE_CLIENT_ID": cid,
                "LINEAR_CLAUDE_CLIENT_SECRET": secret,
            },
        )
        self.assertEqual(self.vault.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
