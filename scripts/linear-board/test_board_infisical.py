"""Board CLI tests for the direct scoped-Infisical contract:  every seat's Linear client
pair and the shared minted-token copies live in the AI Fleet Coordinator prod /linear
folder, read and written directly with the run's own scoped service tokens, and the
bearer verify key is read from Fleet-OPS prod /tools the same way.  The retired
fleet-secrets wrapper is gone:  a missing or mis-scoped token fails closed, with no
other loader and no fallback.

A mock Infisical (test_board.MockInfisical) runs beside test_board.py's mock Linear;
nothing reaches the network.

    python3 scripts/linear-board/test_board_infisical.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_board as T


class InfisicalTests(T.BoardTest):
    """Reads and writes use separate scoped service tokens, verified against their own
    metadata before any secret is touched;  no Universal Auth login is available."""

    seat_app = T.SeatAppTests.seat_app
    cache = T.SeatAppTests.cache
    as_seat = T.SeatAppTests.as_seat

    def test_scoped_read_and_write_tokens_share_the_cache(self):
        self.seat_app("CLAUDE", "Claude")
        p = self.as_seat("CLAUDE", "whoami")
        self.assertIn("viewer:      Claude (app user)", p.stdout)
        self.assertIn(
            "token store: Infisical AI Fleet Coordinator prod /linear LINEAR_CLAUDE_CACHE_TOKEN (no token on disk)",
            p.stdout,
        )
        shared = json.loads(self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"])
        self.assertIn(shared["access_token"], self.mock.tokens)
        self.assertEqual(shared["viewer"]["name"], "Claude")  # the viewer is kept too
        self.assertFalse(any("universal-auth/login" in path for _, path in self.vault.requests))
        self.assertTrue(
            all(
                authorization == "Bearer " + self.vault.read_token
                for method, path, authorization in self.vault.authorizations
                if method == "GET" and path in ("/api/v3/secrets/raw",)
            )
        )
        metadata_auths = [
            authorization
            for method, path, authorization in self.vault.authorizations
            if method == "GET" and path == "/api/v2/service-token"
        ]
        self.assertIn("Bearer " + self.vault.read_token, metadata_auths)
        self.assertIn("Bearer " + self.vault.write_token, metadata_auths)
        self.assertTrue(
            all(
                authorization == "Bearer " + self.vault.write_token
                for method, _path, authorization in self.vault.authorizations
                if method in ("PATCH", "POST")
            )
        )
        # a second process on the same host (or another host) reads the shared copy and mints nothing
        self.as_seat("CLAUDE", "whoami")
        self.assertEqual(self.mock.mints, 1)
        self.assertFalse(
            list((self.tmp / "home").rglob("*.token")), "no token file anywhere under HOME"
        )

    def test_a_refused_shared_token_is_reminted_and_shared_again(self):
        self.seat_app("CLAUDE", "Claude")
        self.as_seat("CLAUDE", "whoami")
        first = json.loads(self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"])
        first["minted_at"] -= 2 * 86400
        self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"] = json.dumps(first)
        self.mock.tokens.clear()  # Linear now refuses the shared token
        self.as_seat("CLAUDE", "whoami")
        self.assertEqual(self.mock.mints, 2)
        second = json.loads(self.vault.secrets["LINEAR_CLAUDE_CACHE_TOKEN"])
        self.assertNotEqual(second["access_token"], first["access_token"])

    def test_no_read_token_fails_closed_without_reading_the_folder(self):
        self.seat_app("CLAUDE", "Claude")  # the pair exists only in the /linear folder
        p = self.as_seat(
            "CLAUDE",
            "whoami",
            drop=("LINEAR_BOARD_INFISICAL_READ_TOKEN",),
            code=4,
        )
        self.assertIn("no Linear app credentials", p.stderr)
        self.assertNotIn(("GET", "/api/v3/secrets/raw"), self.vault.requests)
        self.assertEqual(self.mock.mints, 0)

    def test_malformed_permission_metadata_fails_closed(self):
        self.seat_app("CLAUDE", "Claude")
        self.vault.metadata_override = {
            "projectId": "proj-afc",
            "workspace": "proj-afc",
            "permissions": [{"read": True}],
            "scopes": [{"environment": "prod", "secretPath": "/linear"}],
            "expiresAt": None,
        }
        p = self.as_seat("CLAUDE", "whoami", code=4)
        self.assertIn("no Linear app credentials", p.stderr)
        self.assertNotIn("LINEAR_CLAUDE_CACHE_TOKEN", self.vault.secrets)
        self.assertTrue(
            any(path == "/api/v2/service-token" for _method, path in self.vault.requests)
        )
        self.assertNotIn(("GET", "/api/v3/secrets/raw"), self.vault.requests)

    def test_workspace_only_metadata_supports_read_and_write_grants(self):
        self.seat_app("CLAUDE", "Claude")
        self.vault.metadata_override = {
            "workspace": "proj-afc",
            "permissions": ["read"],
            "scopes": [{"environment": "prod", "secretPath": "/linear"}],
            "expiresAt": None,
        }
        self.as_seat("CLAUDE", "whoami")
        self.assertIn("LINEAR_CLAUDE_CACHE_TOKEN", self.vault.secrets)
        self.assertTrue(any(method == "POST" for method, _ in self.vault.requests))

    def test_contradictory_project_or_expiry_metadata_stops_before_secret_read(self):
        self.seat_app("CLAUDE", "Claude")
        valid = {
            "workspace": "proj-afc",
            "permissions": ["read"],
            "scopes": [{"environment": "prod", "secretPath": "/linear"}],
            "expiresAt": None,
        }
        for patch in (
            {"projectId": "wrong-project"},
            {"projectId": None},
            {"workspace": "wrong-project"},
            {"expiresAt": "2000-01-01T00:00:00Z"},
            {"expiresAt": "2099-01-01T00:00:00"},
            {"expiresAt": "invalid"},
            {"expiresAt": 123},
        ):
            with self.subTest(patch=patch):
                self.vault.requests.clear()
                self.vault.metadata_override = dict(valid, **patch)
                self.as_seat("CLAUDE", "whoami", code=4)
                self.assertTrue(self.vault.requests)
                # metadata was fetched, but the /linear folder read never happened
                self.assertTrue(
                    any(path == "/api/v2/service-token" for _, path in self.vault.requests)
                )
                self.assertNotIn(("GET", "/api/v3/secrets/raw"), self.vault.requests)

    def test_the_pair_is_read_directly_and_no_wrapper_is_ever_run(self):
        # A stub executable at the old wrapper's env path records any invocation:
        # the CLI must never spawn it, whatever the variable says.
        marker = self.tmp / "wrapper-was-run"
        stub = self.tmp / "fleet-secrets"
        stub.write_text(f"#!/bin/sh\ntouch {marker}\nexit 9\n")
        stub.chmod(0o700)
        cid, secret = self.seat_app("CLAUDE", "Claude", file=False)
        self.afc_put("LINEAR_CLAUDE_CLIENT_ID", cid)
        self.afc_put("LINEAR_CLAUDE_CLIENT_SECRET", secret)
        p = self.as_seat(
            "CLAUDE", "whoami", extra={"LINEAR_BOARD_FLEET_SECRETS": str(stub)}
        )
        self.assertIn(
            "credentials: Infisical AI Fleet Coordinator prod /linear (LINEAR_CLAUDE_CLIENT_ID",
            p.stdout,
        )
        gets = [
            a for a in self.vault.authorizations if a[:2] == ("GET", "/api/v3/secrets/raw")
        ]
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0][2], "Bearer " + self.vault.read_token)
        self.assertFalse(marker.exists(), "the retired wrapper was spawned")

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
        self.assertNotIn(
            "credentials: Infisical AI Fleet Coordinator prod /linear (LINEAR_CLAUDE_CLIENT_ID",
            p.stdout,
        )

    def test_infisical_outage_fails_closed_without_a_fallback(self):
        # The pair exists only in the /linear folder:  with Infisical down there is no
        # other loader, so the run fails closed instead of reaching for a wrapper.
        self.seat_app("CLAUDE", "Claude")
        self.vault.down_linear = True  # the /linear project is down;  /tools stays up
        p = self.as_seat("CLAUDE", "whoami", code=4)
        self.assertIn("no Linear app credentials", p.stderr)
        self.assertEqual(self.mock.mints, 0)

    def test_missing_scoped_tokens_skip_the_shared_store(self):
        cid, secret = self.seat_app(
            "CLAUDE", "Claude", file=False
        )  # the pair comes from the environment
        self.as_seat(
            "CLAUDE",
            "whoami",
            drop=(
                "LINEAR_BOARD_INFISICAL_READ_TOKEN",
                "LINEAR_BOARD_INFISICAL_WRITE_TOKEN",
            ),
            extra={
                "LINEAR_CLAUDE_CLIENT_ID": cid,
                "LINEAR_CLAUDE_CLIENT_SECRET": secret,
            },
        )
        linear = [
            r
            for r in self.vault.requests
            if r[1] == "/api/v3/secrets/raw" or "LINEAR_" in r[1]
        ]
        self.assertEqual(linear, [])

    def test_tokens_can_come_from_private_files(self):
        read_file = self.private("inf-read-token", self.vault.read_token + "\n")
        write_file = self.private("inf-write-token", self.vault.write_token + "\n")
        self.seat_app("CLAUDE", "Claude")
        p = self.as_seat(
            "CLAUDE",
            "whoami",
            drop=(
                "LINEAR_BOARD_INFISICAL_READ_TOKEN",
                "LINEAR_BOARD_INFISICAL_WRITE_TOKEN",
            ),
            extra={
                "LINEAR_BOARD_INFISICAL_READ_TOKEN_FILE": read_file,
                "LINEAR_BOARD_INFISICAL_WRITE_TOKEN_FILE": write_file,
            },
        )
        self.assertIn("viewer:      Claude (app user)", p.stdout)
        self.assertIn("LINEAR_CLAUDE_CACHE_TOKEN", self.vault.secrets)

    def test_a_loose_token_file_fails_closed(self):
        loose = self.private("inf-read-token", self.vault.read_token + "\n", mode=0o644)
        self.seat_app("CLAUDE", "Claude")  # the pair exists only in the /linear folder
        p = self.as_seat(
            "CLAUDE",
            "whoami",
            drop=("LINEAR_BOARD_INFISICAL_READ_TOKEN",),
            extra={"LINEAR_BOARD_INFISICAL_READ_TOKEN_FILE": loose},
            code=4,
        )
        self.assertIn("no Linear app credentials", p.stderr)
        self.assertNotIn(("GET", "/api/v3/secrets/raw"), self.vault.requests)

    def test_the_verify_key_requires_an_exactly_scoped_tools_token(self):
        for wrong_grant in (
            ("proj-tools", "/wrong-folder", ["read"]),
            ("proj-other", "/tools", ["read"]),
            ("proj-tools", "/tools", ["read", "write"]),
        ):
            with self.subTest(wrong_grant=wrong_grant):
                self.vault.grants[self.vault.tools_token] = wrong_grant
                self.vault.requests.clear()
                p = self.board("stats", code=None)
                self.assertEqual(p.returncode, 6, f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}")
                self.assertNotIn(
                    ("GET", "/api/v3/secrets/raw/GR_BEARER_VERIFY_KEY"),
                    self.vault.requests,
                    "a mis-scoped token never reaches the secret read",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
