"""MAC_COLLAB_TOKEN rotation: Infisical is canonical, health exposes the source, 401 hints restart.

Never asserts on token values in health/401 bodies -- those must stay secret.  The Infisical read is
stubbed (`_fetch_vault_tokens`);  nothing here runs the real fleet-secrets or opens the old secrets folder.
"""

import importlib.util
import os
import stat
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "mac_collab_server_token", HERE / "mac-collab-server.py"
)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

board_cli = SourceFileLoader("board_cli", str(HERE / "board")).load_module()


class TestTokenStaleness(unittest.TestCase):
    def setUp(self):
        self.vault = {"file-token-aaa": None}
        self.fetch = lambda: dict(self.vault) if self.vault is not None else None
        self._orig_fetch = server._fetch_vault_tokens
        server._fetch_vault_tokens = lambda: self.fetch()
        server._VAULT_CACHE = (0.0, None)
        server._LOGGED_FILE_WINS = False
        self._env_was = os.environ.get("MAC_COLLAB_TOKEN")
        os.environ.pop("MAC_COLLAB_TOKEN", None)

    def tearDown(self):
        server._fetch_vault_tokens = self._orig_fetch
        server._VAULT_CACHE = (0.0, None)
        server._LOGGED_FILE_WINS = False
        if self._env_was is None:
            os.environ.pop("MAC_COLLAB_TOKEN", None)
        else:
            os.environ["MAC_COLLAB_TOKEN"] = self._env_was

    def test_vault_wins_over_env(self):
        os.environ["MAC_COLLAB_TOKEN"] = "env-token-bbb"
        tokens = server.load_tokens()
        self.assertIn("file-token-aaa", tokens)
        self.assertNotIn("env-token-bbb", tokens)

    def test_reload_after_rotation_once_the_ttl_passes(self):
        first = server.load_tokens()
        self.assertIn("file-token-aaa", first)
        self.vault = {"file-token-ccc": None}
        self.assertIn(
            "file-token-aaa", server.load_tokens(), "served from the cache inside the TTL"
        )
        at, cached = server._VAULT_CACHE
        server._VAULT_CACHE = (at - server.TOKEN_TTL_S - 1, cached)
        second = server.load_tokens()
        self.assertIn("file-token-ccc", second)
        self.assertNotIn("file-token-aaa", second)

    def test_env_only_when_the_vault_has_none(self):
        self.vault = {}
        os.environ["MAC_COLLAB_TOKEN"] = "env-token-bbb"
        tokens = server.load_tokens()
        self.assertIn("env-token-bbb", tokens)

    def test_a_failed_read_keeps_the_last_good_tokens(self):
        self.assertIn("file-token-aaa", server.load_tokens())
        self.vault = None  # the fetch now fails
        at, cached = server._VAULT_CACHE
        server._VAULT_CACHE = (at - server.TOKEN_TTL_S - 1, cached)
        self.assertIn("file-token-aaa", server.load_tokens())

    def test_auth_meta_has_source_not_token(self):
        meta = server.token_auth_meta()
        self.assertEqual(meta["token_source"], "infisical")
        self.assertEqual(meta["token_count"], 1)
        self.assertIsInstance(meta["vault_refreshed_at"], int)
        blob = str(meta)
        self.assertNotIn("file-token-aaa", blob)
        self.assertIn("pm2 restart mac-collab --update-env", meta["restart_hint"])

    def test_the_real_fetch_runs_fleet_secrets_run_and_parses_only_token_keys(self):
        """`_fetch_vault_tokens` itself, against a fake binary:  folder /mac-collab, only MAC_COLLAB_TOKEN* lines."""
        server._fetch_vault_tokens = self._orig_fetch
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fleet-secrets"
            fake.write_text(
                "#!/bin/sh\n"
                '[ "$1" = run ] && [ "$2" = --path ] && [ "$3" = /mac-collab ] && [ "$4" = -- ] || exit 2\n'
                "echo MAC_COLLAB_TOKEN=root-tok\necho MAC_COLLAB_TOKEN_CLAUDE=claude-tok\necho OTHER=nope\n"
            )
            fake.chmod(stat.S_IRWXU)
            with patch.object(server, "FLEET_SECRETS_BIN", fake):
                got = server._fetch_vault_tokens()
            self.assertEqual(got, {"root-tok": None, "claude-tok": "CLAUDE"})
            with patch.object(server, "FLEET_SECRETS_BIN", Path(tmp) / "missing"):
                self.assertIsNone(server._fetch_vault_tokens())

    def test_401_hint_omits_token_value(self):
        self.assertIn("rotated", server.AUTH_FAIL_HINT)
        self.assertNotIn("file-token-aaa", server.AUTH_FAIL_HINT)
        self.assertNotIn("env-token", server.AUTH_FAIL_HINT)

    def test_authorized_uses_passed_tokens_snapshot(self):
        from unittest.mock import MagicMock, patch

        handler = MagicMock()
        handler.headers = {"Authorization": "Bearer snap-token"}
        with patch.object(server, "load_tokens", side_effect=AssertionError("should not reload")):
            ident = server.authorized(handler, tokens={"snap-token": None})
        self.assertEqual(ident, "OWNER")

    def test_cookie_does_not_survive_empty_tokens(self):
        """HMAC key is derived from live tokens.  An unreadable file does not
        keep old cookies valid; checking the cookie first would still fail.
        """
        from http.server import BaseHTTPRequestHandler
        from unittest.mock import MagicMock

        cookie = server.mint_session_value("OWNER")
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Cookie": f"{server.SESSION_COOKIE}={cookie}"}
        self.assertEqual(server.cookie_authorized(handler), "OWNER")
        self.vault = {}
        at, cached = server._VAULT_CACHE
        server._VAULT_CACHE = (at - server.TOKEN_TTL_S - 1, cached)
        self.assertIsNone(server.cookie_authorized(handler))
        self.assertIsNone(server.authorized(handler))


class TestBoardCliToken(unittest.TestCase):
    def setUp(self):
        self._env_was = os.environ.get("MAC_COLLAB_TOKEN")
        os.environ.pop("MAC_COLLAB_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = Path(self.tmp.name) / "fleet-secrets"
        self.fake.write_text(
            "#!/bin/sh\n"
            '[ "$1" = get ] && [ "$2" = MAC_COLLAB_TOKEN ] && [ "$3" = --path ] && [ "$4" = /mac-collab ] || exit 3\n'
            "printf %s file-token-aaa\n"
        )
        self.fake.chmod(stat.S_IRWXU)
        self._orig = board_cli.FLEET_SECRETS
        board_cli.FLEET_SECRETS = str(self.fake)

    def tearDown(self):
        board_cli.FLEET_SECRETS = self._orig
        if self._env_was is None:
            os.environ.pop("MAC_COLLAB_TOKEN", None)
        else:
            os.environ["MAC_COLLAB_TOKEN"] = self._env_was
        self.tmp.cleanup()

    def test_board_prefers_infisical_over_env(self):
        os.environ["MAC_COLLAB_TOKEN"] = "env-token-bbb"
        self.assertEqual(board_cli.token(), "file-token-aaa")

    def test_board_falls_back_to_env_without_fleet_secrets(self):
        board_cli.FLEET_SECRETS = str(Path(self.tmp.name) / "missing")
        os.environ["MAC_COLLAB_TOKEN"] = "env-token-bbb"
        self.assertEqual(board_cli.token(), "env-token-bbb")

    def test_board_401_hint_omits_token(self):
        hint = board_cli.AUTH_FAIL_HINT
        self.assertIn("rotated", hint)
        self.assertIn("pm2 restart mac-collab --update-env", hint)
        self.assertNotIn("file-token-aaa", hint)


if __name__ == "__main__":
    unittest.main()
