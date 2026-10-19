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
        server._reset_vault()
        server._LOGGED_FILE_WINS = False
        self._env_was = os.environ.get("MAC_COLLAB_TOKEN")
        os.environ.pop("MAC_COLLAB_TOKEN", None)

    def tearDown(self):
        server._fetch_vault_tokens = self._orig_fetch
        server._reset_vault()
        server._LOGGED_FILE_WINS = False
        if self._env_was is None:
            os.environ.pop("MAC_COLLAB_TOKEN", None)
        else:
            os.environ["MAC_COLLAB_TOKEN"] = self._env_was

    def expire(self):
        """Make the next read due and let the background refresh finish."""
        with server._VAULT_LOCK:
            server._VAULT["next_try"] = 0.0
        server._vault_tokens()
        server._vault_wait()

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
        self.expire()
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
        self.expire()
        self.assertIn("file-token-aaa", server.load_tokens())

    def test_a_hung_refresh_does_not_block_requests_and_serves_the_last_good_tokens(self):
        import threading
        import time

        self.assertIn("file-token-aaa", server.load_tokens())  # first load, inline
        started, release = threading.Event(), threading.Event()

        def hung():
            started.set()
            release.wait(30)
            return {"file-token-new": None}

        server._fetch_vault_tokens = hung
        with server._VAULT_LOCK:
            server._VAULT["next_try"] = 0.0
        try:
            t0 = time.time()
            self.assertIn("file-token-aaa", server.load_tokens())  # starts the background fetch
            self.assertTrue(started.wait(5), "the refresh did not start")
            results = []

            def request():
                results.append(server.load_tokens())
                results.append(server.token_auth_meta())

            workers = [threading.Thread(target=request) for _ in range(5)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(5)
            self.assertFalse(any(w.is_alive() for w in workers), "a request was stalled by the hung fetch")
            self.assertLess(time.time() - t0, 5)
            self.assertTrue(all("file-token-aaa" in r for r in results[0::2]))
            self.assertEqual(results[1]["token_source"], "infisical")
        finally:
            release.set()
            server._vault_wait()
        self.assertIn("file-token-new", server.load_tokens())  # the finished refresh is swapped in

    def test_a_failed_refresh_backs_off_and_keeps_refreshed_at(self):
        server.load_tokens()
        before = server.token_auth_meta()["vault_refreshed_at"]
        calls = []
        server._fetch_vault_tokens = lambda: calls.append(1)  # returns None:  a failed read
        self.expire()
        self.assertEqual(len(calls), 1)
        self.assertEqual(server.token_auth_meta()["vault_refreshed_at"], before)
        server.load_tokens()
        server._vault_wait()
        self.assertEqual(len(calls), 1, "no retry inside the back-off window")
        self.assertGreater(server._VAULT["next_try"], __import__("time").time())

    def test_the_fetch_scrubs_inherited_token_variables(self):
        server._fetch_vault_tokens = self._orig_fetch
        os.environ["MAC_COLLAB_TOKEN_STALE"] = "inherited-value"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fake = Path(tmp) / "fleet-secrets"
                # Runs the real command after `--` with the environment it was given, as fleet-secrets does.
                fake.write_text("#!/bin/sh\nshift 4\nMAC_COLLAB_TOKEN=vault-tok exec \"$@\"\n")
                fake.chmod(stat.S_IRWXU)
                with patch.object(server, "FLEET_SECRETS_BIN", fake):
                    got = server._fetch_vault_tokens()
        finally:
            os.environ.pop("MAC_COLLAB_TOKEN_STALE", None)
        self.assertEqual(got, {"vault-tok": None})

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
        self.expire()
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
