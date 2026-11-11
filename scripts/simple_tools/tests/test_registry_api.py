"""The registry API transport for the package's own registry callers (registry_api.py).

The registry API is the only store a client reaches (AFC-720).  These tests run the
operator-registry CLI and the platform-letter registry side against a loopback stub that
serves the real RegistryService over a server-side store, with no local registry present,
and prove the operations land in the server store while no local file is created.  They
also hold the hard cut in place:  a host with no URL or no key answers not_configured for
every op and touches no file, the key has one name, and registry_api itself has no way to
open a store of its own.  tests/registry_fake.py serves the same API in process, for the
client tests that move off local-file fixtures.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_registry_api
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

from simple_tools import listener_registry as LREG
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import registry_api
from simple_tools.tests.registry_fake import FakeRegistry

KEY = "test-registry-key-0123456789abcdef"  # a test key, long enough for the API's floor
#: registry_api's own knob lookup, captured before any test replaces it.
_ORIGINAL_KNOB_VALUE = registry_api._knob_value


class _StubHandler(BaseHTTPRequestHandler):
    """Forward every request to the RegistryService this class is bound to."""

    service: Any = None

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload = self.service.handle(
            self.command, self.path, dict(self.headers.items()), body
        )
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *args: Any) -> None:
        pass


def _no_knobs(name: str) -> str:
    return ""


class ApiBase(unittest.TestCase):
    def setUp(self) -> None:
        # No test reaches Infisical:  the knob fallback reads as unset unless a test says so.
        saved = registry_api._knob_value
        registry_api._knob_value = _no_knobs  # type: ignore[assignment]
        self.addCleanup(setattr, registry_api, "_knob_value", saved)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The server's store:  the one file the API serves.
        self.server_db = os.path.join(self.tmp.name, "server", "registry.sqlite3")
        os.makedirs(os.path.dirname(self.server_db))
        OR.SessionRegistry(self.server_db).close_db()
        service = LREG.RegistryService(self.server_db, {"OPERATOR": KEY})
        handler = type("BoundStub", (_StubHandler,), {"service": service})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)
        import threading

        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        # The client side:  a state root with NO local registry in it.
        self.state = os.path.join(self.tmp.name, "state")
        self.local_db = os.path.join(self.state, "operator", "registry.sqlite3")

    def api_env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": self.tmp.name,
            "SIMPLE_TOOLS_STATE_DIR": self.state,
            registry_api.URL_ENV: self.url,
            registry_api.KEY_VAR: KEY,
        }
        env.update(extra)
        return env

    def server_lookup(self, platform: str, location: str, sid: str) -> dict | None:
        reg = OR.SessionRegistry(self.server_db)
        try:
            return reg.lookup(platform, location, PL.SCOPE, sid)
        finally:
            reg.close_db()


class ClientTests(ApiBase):
    def test_call_round_trip_and_refusals(self) -> None:
        env = self.api_env()
        missing = registry_api.call(
            env, "resolve", {"platform": "minimax", "letter": "A"}
        )
        self.assertEqual(missing.get("found"), False)
        made = registry_api.call(
            env,
            "register",
            {
                "platform": "minimax",
                "location": "server",
                "scope": "default",
                "session_id": "mm-1",
                "seat": "MM",
                "source": "test",
            },
        )
        self.assertNotIn("error", made)
        self.assertTrue(made.get("label"))
        found = registry_api.call(
            env,
            "resolve",
            {"platform": "minimax", "letter": made["label"]},
        )
        self.assertEqual(found.get("native_session_id"), "mm-1")

    def test_update_takes_issues_and_the_reads_return_them(self) -> None:
        env = self.api_env()
        identity = {"platform": "minimax", "location": "server", "scope": "default", "session_id": "mm-issues-1"}
        made = registry_api.call(env, "register", {**identity, "seat": "MM", "source": "test"})
        self.assertNotIn("error", made)
        issues = [{"id": "AFC-1", "seen_at": "2026-11-10T15:00:00Z"}]
        updated = registry_api.call(env, "update", {**identity, "issues": issues + [{"id": "NOPE-1"}]})
        self.assertEqual((updated["changed"], updated["issues"]), (["issues"], issues))
        history = registry_api.call(env, "history", {"platform": "minimax"})
        self.assertEqual(history["items"][0]["issues"], issues)
        # The session's last activity rides on history too, and a touch moves it forward.
        conn = sqlite3.connect(self.server_db)
        with conn:
            conn.execute("UPDATE sessions SET last_seen_at = '2026-01-01T00:00:00.000Z'")
        conn.close()
        before = registry_api.call(env, "history", {"platform": "minimax"})["items"][0]["last_seen_at"]
        self.assertEqual(before, "2026-01-01T00:00:00.000Z")
        registry_api.call(env, "touch", identity)
        after = registry_api.call(env, "history", {"platform": "minimax"})["items"][0]["last_seen_at"]
        self.assertGreater(after, before)
        found = registry_api.call(env, "resolve", {"platform": "minimax", "letter": made["label"]})
        self.assertEqual(found["issues"], issues)
        touched = registry_api.call(env, "touch", identity)
        self.assertEqual((touched["outcome"], touched["issues"]), ("seen", issues))
        # An update without the field leaves it;  one that is not a list is refused.
        again = registry_api.call(env, "update", {**identity, "title": "Words"})
        self.assertEqual(again["issues"], issues)
        refused = registry_api.call(env, "update", {**identity, "issues": "AFC-1"})
        self.assertIn("must be a list", refused.get("error", ""))

    def test_a_malformed_url_and_a_truncated_answer_are_error_answers(self) -> None:
        env = self.api_env(**{registry_api.URL_ENV: "http://[::1"})
        answer = registry_api.call(env, "resolve", {"platform": "minimax", "letter": "A"})
        self.assertEqual(answer.get("code"), "not_configured")
        # A server that closes mid-body:  the read breaks off, and the call answers with
        # the transport error instead of raising out of http.client.
        import socket
        import threading

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve_once() -> None:
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{\"partial\":"
            )
            conn.close()
            listener.close()

        threading.Thread(target=serve_once, daemon=True).start()
        env = self.api_env(**{registry_api.URL_ENV: "http://127.0.0.1:%d" % port})
        answer = registry_api.call(env, "resolve", {"platform": "minimax", "letter": "A"})
        self.assertIn("error", answer)
        self.assertEqual(answer.get("code"), "timeout")

    def test_call_refuses_an_unknown_op_and_a_missing_key(self) -> None:
        env = self.api_env()
        self.assertEqual(registry_api.call(env, "frobnicate")["code"], "bad_op")
        no_key = self.api_env()
        del no_key[registry_api.KEY_VAR]
        self.assertEqual(
            registry_api.call(no_key, "resolve", {"platform": "minimax", "letter": "A"})[
                "code"
            ],
            "not_configured",
        )
        wrong_key = self.api_env(**{registry_api.KEY_VAR: "x" * 40})
        refused = registry_api.call(
            wrong_key, "resolve", {"platform": "minimax", "letter": "A"}
        )
        self.assertIn("error", refused)


class OperatorCliTests(ApiBase):
    def run_cli(self, argv: list[str], env: dict[str, str]) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = OR.main(argv, env=env)
        return code, json.loads(out.getvalue())

    def seed_minimax_pair(self) -> tuple[str, str]:
        old = OR.register_session(
            self.server_db,
            platform="minimax",
            verified_host="server",
            profile_scope="default",
            native_session_id="mm-old",
            seat="MA",
            source="test",
            issuer=OR.ISSUER_OPERATOR,
        )
        conn = sqlite3.connect(self.server_db)
        try:
            row = conn.execute(
                "SELECT last_seen_at FROM sessions WHERE native_session_id = 'mm-old'"
            ).fetchone()
            seen = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            backdated = (seen - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with conn:
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE native_session_id = 'mm-old'",
                    (backdated,),
                )
        finally:
            conn.close()
        new = OR.register_session(
            self.server_db,
            platform="minimax",
            verified_host="server",
            profile_scope="default",
            native_session_id="mm-new",
            seat="MA",
            source="test",
            issuer=OR.ISSUER_OPERATOR,
        )
        return str(old["record_id"]), str(new["record_id"])

    def test_sweep_runs_over_the_api_and_never_touches_a_local_file(self) -> None:
        old_id, _new_id = self.seed_minimax_pair()
        code, answer = self.run_cli(
            [
                "sweep",
                "--platform",
                "minimax",
                "--seat",
                "MA",
                "--stale-after-minutes",
                "120",
            ],
            self.api_env(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(answer.get("count"), 1)
        self.assertEqual(answer["ended"][0]["record_id"], old_id)
        # The end landed in the server's store.
        reg = OR.SessionRegistry(self.server_db)
        try:
            row = reg._get(old_id)
        finally:
            reg.close_db()
        self.assertIsNotNone(row["closed_at"])
        # And no local registry was created or opened for a write.
        self.assertFalse(os.path.exists(self.local_db))

    def test_register_and_resolve_over_the_api(self) -> None:
        code, made = self.run_cli(
            [
                "register",
                "--platform",
                "minimax",
                "--location",
                "server",
                "--scope",
                "default",
                "--session-id",
                "mm-cli-1",
                "--seat",
                "MA",
                "--source",
                "test",
            ],
            self.api_env(),
        )
        self.assertEqual(code, 0)
        self.assertTrue(made.get("label"))
        code, found = self.run_cli(
            [
                "resolve",
                "--platform",
                "minimax",
                "--letter",
                str(made["label"]),
            ],
            self.api_env(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(found.get("native_session_id"), "mm-cli-1")
        self.assertFalse(os.path.exists(self.local_db))

    def test_a_dead_api_is_a_visible_failure_and_writes_nothing(self) -> None:
        env = self.api_env(**{registry_api.URL_ENV: "http://127.0.0.1:1"})
        code, answer = self.run_cli(
            ["sweep", "--platform", "minimax", "--seat", "MA"], env
        )
        self.assertEqual(code, 2)
        self.assertIn("error", answer)
        self.assertFalse(os.path.exists(self.local_db))

    def test_the_outbox_ops_run_over_the_api_without_a_db_flag(self) -> None:
        # pending-outbox and mark-exported once demanded --db, which argparse enforced
        # before the API branch could run:  without the flag they never reached the API.
        code, _made = self.run_cli(
            [
                "register",
                "--platform",
                "minimax",
                "--location",
                "server",
                "--scope",
                "default",
                "--session-id",
                "mm-outbox-1",
                "--seat",
                "MA",
                "--source",
                "test",
            ],
            self.api_env(),
        )
        self.assertEqual(code, 0)
        code, pending = self.run_cli(["pending-outbox"], self.api_env())
        self.assertEqual(code, 0)
        ids = [str(item["outbox_id"]) for item in pending.get("items", [])]
        self.assertTrue(ids)
        code, _marked = self.run_cli(
            ["mark-exported", *ids], self.api_env()
        )
        self.assertEqual(code, 0)
        code, drained = self.run_cli(["pending-outbox"], self.api_env())
        self.assertEqual(code, 0)
        self.assertEqual(drained.get("items"), [])
        self.assertFalse(os.path.exists(self.local_db))


class PlatformLetterTests(ApiBase):
    def registry(
        self, argv: list[str], env: dict[str, str] | None = None
    ) -> tuple[int, dict]:
        out = io.StringIO()
        code = PL.registry_main(
            list(argv), env=env or self.api_env(), stdout=out
        )
        return code, json.loads(out.getvalue())

    def reg_argv(self, op: str, sid: str, **flags: str) -> list[str]:
        argv = [
            op,
            "--platform",
            "codex",
            "--location",
            "server",
            "--session-id",
            sid,
            "--seat",
            "CODEX",
            "--source",
            "codex-test",
        ]
        for name, value in flags.items():
            argv.append("--%s=%s" % (name.replace("_", "-"), value))
        return argv

    def test_a_touch_over_the_api_names_the_letter_the_server_holds_now(self) -> None:
        code, made = self.registry(self.reg_argv("register", "s-0001", letter_pick="lowest"))
        self.assertEqual(code, 0)
        # The API's own touch answer keeps its old keys and adds the letter and generation.
        raw = registry_api.call(
            self.api_env(),
            "touch",
            {"platform": "codex", "location": "server", "scope": PL.SCOPE, "session_id": "s-0001"},
        )
        self.assertEqual(
            raw,
            {
                "record_id": made["record_id"],
                "outcome": "seen",
                "label": made["label"],
                "generation": made["generation"],
            },
        )
        conn = sqlite3.connect(self.server_db)
        with conn:
            conn.execute("UPDATE alias_assignments SET letter = 'QX' WHERE retired_at IS NULL")
        conn.close()
        code, touched = self.registry(self.reg_argv("touch", "s-0001"))
        self.assertEqual(code, 0)
        self.assertEqual(
            touched,
            {"ok": True, "record_id": made["record_id"], "label": "QX", "generation": made["generation"]},
        )
        # A title call's answer names the record too, so a move learned there refreshes the note's id.
        code, titled = self.registry(self.reg_argv("title", "s-0001", title="New words"))
        self.assertEqual(code, 0)
        self.assertEqual(
            titled,
            {"ok": True, "record_id": made["record_id"], "label": "QX", "generation": made["generation"]},
        )

    def test_the_whole_letter_life_runs_over_the_api(self) -> None:
        code, made = self.registry(
            self.reg_argv("register", "s-0001", letter_pick="lowest")
        )
        self.assertEqual(code, 0)
        self.assertEqual(made.get("outcome"), "registered")
        label = made["label"]
        self.assertEqual(self.server_lookup("codex", "server", "s-0001")["label"], label)

        code, again = self.registry(
            self.reg_argv("register", "s-0001", letter_pick="lowest")
        )
        self.assertEqual(code, 0)
        self.assertEqual(again.get("outcome"), "resumed")
        self.assertEqual(again["label"], label)

        code, titled = self.registry(
            self.reg_argv("title", "s-0001", title="Fix the thing")
        )
        self.assertEqual(code, 0)
        self.assertEqual(titled.get("ok"), True)
        self.assertEqual(
            self.server_lookup("codex", "server", "s-0001")["title"], "Fix the thing"
        )

        code, touched = self.registry(self.reg_argv("touch", "s-0001"))
        self.assertEqual(code, 0)
        self.assertEqual(touched.get("ok"), True)

        code, shown = self.registry(self.reg_argv("show", "s-0001"))
        self.assertEqual(code, 0)
        self.assertEqual(shown.get("found"), True)
        self.assertEqual(shown.get("label"), label)

        code, closed = self.registry(self.reg_argv("close", "s-0001"))
        self.assertEqual(code, 0)
        self.assertEqual(closed.get("closed"), True)
        self.assertEqual(closed.get("label"), label)

        # A touch on the closed registration reads as retired, as on the local path.
        code, retired = self.registry(self.reg_argv("touch", "s-0001"))
        self.assertEqual(code, 0)
        self.assertEqual(retired.get("retired"), True)

        # Registering again reopens it and gives a letter back.
        code, back = self.registry(
            self.reg_argv("register", "s-0001", letter_pick="lowest")
        )
        self.assertEqual(code, 0)
        self.assertEqual(back.get("outcome"), "reopened")
        self.assertTrue(back.get("label"))

        # A chat the registry does not hold is NO_REGISTRATION, as on the local path.
        code, missing = self.registry(self.reg_argv("touch", "s-9999"))
        self.assertEqual(code, 5)
        self.assertEqual(missing.get("error"), PL.NO_REGISTRATION)

        # Reopening with a title carries the title over, as the local path's register
        # does:  the record the registry holds ends up titled, not just the answer.
        code, _closed = self.registry(self.reg_argv("close", "s-0001"))
        self.assertEqual(code, 0)
        code, back_titled = self.registry(
            self.reg_argv("register", "s-0001", letter_pick="lowest", title="Back again")
        )
        self.assertEqual(code, 0)
        self.assertEqual(back_titled.get("outcome"), "reopened")
        self.assertEqual(back_titled.get("title"), "Back again")
        self.assertEqual(
            self.server_lookup("codex", "server", "s-0001")["title"], "Back again"
        )

        # Through all of it, no local registry ever came into being.
        self.assertFalse(os.path.exists(self.local_db))

    def test_the_not_registered_code_folds_to_no_registration(self) -> None:
        # The API names the refusal with the code as well as the registry's sentence,
        # and its record-id sentences carry no mark:  both forms fold.
        self.assertEqual(
            PL._api_error(
                {"error": "no session with this identity", "code": "not_registered"}
            ),
            {"error": PL.NO_REGISTRATION},
        )
        self.assertEqual(
            PL._api_error({"error": "something else", "code": "http_500"}),
            {"error": "something else", "code": "http_500"},
        )
        self.assertIsNone(PL._api_error({"ok": True}))

    def test_a_short_api_answer_is_an_error_answer_not_a_traceback(self) -> None:
        # A register answer naming the resumed-closed outcome but no record_id:  the
        # reopen merge reads the id, and the read must fold to an error answer.
        original = registry_api.call
        registry_api.call = lambda env, op, params=None, **kw: {  # type: ignore[assignment]
            "outcome": "resumed_closed"
        }
        try:
            code, answer = self.registry(
                self.reg_argv("register", "s-0001", letter_pick="lowest")
            )
        finally:
            registry_api.call = original  # type: ignore[assignment]
        self.assertEqual(code, 5)
        self.assertIn("error", answer)

    def test_hook_calls_carry_a_timeout_inside_the_hook_budgets(self) -> None:
        # The hooks run this side inside 15-20 second budgets (HOOK_TIMEOUTS) and a
        # register chains at most two calls, so the per-call timeout the platform path
        # passes must keep the worst chain inside the tightest budget.
        seen: dict[str, Any] = {}

        def fake_call(env: Any, op: str, params: Any = None, *, timeout: int = 25) -> dict:
            seen["timeout"] = timeout
            return {
                "record_id": "rec-1",
                "label": "A",
                "generation": 1,
                "outcome": "registered",
                "label_space": "double",
            }

        original = registry_api.call
        registry_api.call = fake_call  # type: ignore[assignment]
        try:
            code, _answer = self.registry(
                self.reg_argv("register", "s-0001", letter_pick="lowest")
            )
        finally:
            registry_api.call = original  # type: ignore[assignment]
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("timeout"), PL._API_TIMEOUT)
        self.assertLessEqual(2 * PL._API_TIMEOUT, min(PL.HOOK_TIMEOUTS.values()))

    def test_a_dead_api_is_an_error_answer_and_writes_nothing(self) -> None:
        env = self.api_env(**{registry_api.URL_ENV: "http://127.0.0.1:1"})
        code, answer = self.registry(
            self.reg_argv("register", "s-0001", letter_pick="lowest"), env=env
        )
        self.assertEqual(code, 5)
        self.assertIn("error", answer)
        self.assertFalse(os.path.exists(self.local_db))
        self.assertIsNone(self.server_lookup("codex", "server", "s-0001"))

    def test_without_a_url_or_a_key_nothing_is_allocated_anywhere(self) -> None:
        # The hard cut seen from the registry side:  a host with no URL (or no key) gets
        # an error answer, the server store holds nothing, and no local file appears.
        for env in (
            {"HOME": self.tmp.name, "SIMPLE_TOOLS_STATE_DIR": self.state},
            {
                "HOME": self.tmp.name,
                "SIMPLE_TOOLS_STATE_DIR": self.state,
                registry_api.URL_ENV: self.url,
            },
        ):
            answer = registry_api.call(
                env,
                "register",
                {"platform": "codex", "location": "server", "scope": PL.SCOPE,
                 "session_id": "s-0001", "seat": "CODEX", "source": "codex-test"},
            )
            self.assertEqual(answer.get("code"), "not_configured")
        self.assertIsNone(self.server_lookup("codex", "server", "s-0001"))
        self.assertFalse(os.path.exists(self.local_db))


class HardCutTests(unittest.TestCase):
    """AFC-720:  no URL or no key is not_configured for every op, never local behaviour."""

    def setUp(self) -> None:
        saved = (registry_api._knob_value, registry_api._open)
        self.addCleanup(self._restore, saved)
        registry_api._knob_value = _no_knobs  # type: ignore[assignment]

        def no_network(request: Any, timeout: int) -> bytes:
            raise AssertionError("an unconfigured call reached the network")

        registry_api._open = no_network  # type: ignore[assignment]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)

    @staticmethod
    def _restore(saved: tuple[Any, Any]) -> None:
        registry_api._knob_value, registry_api._open = saved  # type: ignore[assignment]

    def assert_every_op_not_configured(self, env: dict[str, str]) -> None:
        for op in sorted(LREG.OPS) + ["frobnicate"]:
            with self.subTest(op=op):
                answer = registry_api.call(env, op, {"platform": "codex", "seat": "CODEX"})
                self.assertEqual(answer.get("code"), "not_configured", answer)
        # registry_api itself made nothing under the caller's home or state.  This is a
        # module-level promise only:  callers keep their own local branches until P4/P6.
        self.assertEqual(os.listdir(self.home), [])

    def test_no_url_is_not_configured_for_every_op(self) -> None:
        env = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.home, registry_api.KEY_VAR: KEY}
        self.assertEqual(registry_api.base_url(env), "")
        self.assert_every_op_not_configured(env)

    def test_no_key_is_not_configured_for_every_op(self) -> None:
        env = {
            "HOME": self.home,
            "SIMPLE_TOOLS_STATE_DIR": self.home,
            registry_api.URL_ENV: "https://registry.example.invalid",
        }
        self.assert_every_op_not_configured(env)

    def test_the_key_has_one_name_and_no_pointer_is_followed(self) -> None:
        # The retired pointer variable named another variable that held the key.  It is
        # not read any more:  a host that sets only the pointer has no key.
        env = {
            "HOME": self.home,
            registry_api.URL_ENV: "https://registry.example.invalid",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY_ENV": "SOME_OTHER_KEY",
            "SOME_OTHER_KEY": KEY,
        }
        self.assertEqual(registry_api.signing_key(env), "")
        self.assert_every_op_not_configured(env)

    def _process_env_without_registry(self) -> None:
        """os.environ itself, with the registry names removed for this test."""
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (registry_api.URL_ENV, registry_api.KEY_VAR, registry_api.PRINCIPAL_ENV):
            os.environ.pop(name, None)

    def test_the_environment_wins_over_the_knob_and_the_knob_fills_a_gap(self) -> None:
        knobs = {
            registry_api.URL_KNOB: "https://knob.example.invalid",
            registry_api.KEY_KNOB: "k" * 40,
        }
        registry_api._knob_value = lambda name: knobs.get(name, "")  # type: ignore[assignment]
        self._process_env_without_registry()
        # The process environment (env None, or os.environ itself) falls back to the knob.
        self.assertEqual(registry_api.base_url(), "https://knob.example.invalid")
        self.assertEqual(registry_api.signing_key(os.environ), "k" * 40)
        os.environ[registry_api.URL_ENV] = "https://env.example.invalid"
        os.environ[registry_api.KEY_VAR] = KEY
        self.assertEqual(registry_api.base_url(), "https://env.example.invalid")
        self.assertEqual(registry_api.signing_key(), KEY)
        # One name for each value:  the knob is looked up under the environment's name.
        self.assertEqual(
            (registry_api.URL_KNOB, registry_api.KEY_KNOB),
            (registry_api.URL_ENV, registry_api.KEY_VAR),
        )

    def test_an_explicit_mapping_never_consults_the_knob(self) -> None:
        # A caller's own mapping is authoritative:  no URL in it means no API, whatever
        # Infisical holds, so a local test can never reach the live registry.
        def must_not_run(name: str) -> str:
            raise AssertionError("an explicit mapping consulted the knob %s" % name)

        registry_api._knob_value = must_not_run  # type: ignore[assignment]
        self.assertEqual(registry_api.base_url({}), "")
        self.assertEqual(registry_api.signing_key({"HOME": self.home}), "")
        self.assertEqual(registry_api.base_url(dict(os.environ, **{registry_api.URL_ENV: ""})), "")
        answer = registry_api.call({"HOME": self.home}, "resolve", {"platform": "codex"})
        self.assertEqual(answer.get("code"), "not_configured", answer)
        self.assertIn("explicit mapping", answer["error"])

    def test_a_knob_failure_reason_rides_on_not_configured_and_is_read_once(self) -> None:
        registry_api._knob_value = _ORIGINAL_KNOB_VALUE  # type: ignore[assignment]
        registry_api.reset_knob_cache()
        self.addCleanup(registry_api.reset_knob_cache)
        self._process_env_without_registry()

        class Unavailable(Exception):
            reason = "service-token-required"

        calls: list[str] = []

        def failing_get(name: str) -> str:
            calls.append(name)
            raise Unavailable("secret-value-must-not-appear")

        fake_knobs = types.SimpleNamespace(get=failing_get)
        fake_registry = types.SimpleNamespace(
            BY_NAME={registry_api.URL_KNOB: object(), registry_api.KEY_KNOB: object()}
        )
        fake_pkg = types.ModuleType("fleet_knobs")
        fake_pkg.knobs = fake_knobs  # type: ignore[attr-defined]
        fake_pkg.registry = fake_registry  # type: ignore[attr-defined]
        with mock.patch.dict(
            sys.modules,
            {"fleet_knobs": fake_pkg, "fleet_knobs.knobs": fake_knobs, "fleet_knobs.registry": fake_registry},
        ):
            for _ in range(3):
                self.assertEqual(registry_api.base_url(), "")
            answer = registry_api.call(None, "resolve", {"platform": "codex"})
        self.assertEqual(answer.get("code"), "not_configured", answer)
        self.assertIn("service-token-required", answer["error"])
        self.assertIn(registry_api.URL_ENV, answer["error"])
        self.assertNotIn("secret-value-must-not-appear", answer["error"])
        # One knob read per name per process, however often base_url() is asked.
        self.assertEqual(calls, [registry_api.URL_KNOB])

    def test_an_unregistered_knob_reads_as_unset(self) -> None:
        real = _ORIGINAL_KNOB_VALUE
        registry_api.reset_knob_cache()
        self.addCleanup(registry_api.reset_knob_cache)
        # A name the knob registry does not hold reads as unset, before any fetch.
        self.assertEqual(real("NO_SUCH_REGISTRY_KNOB_AFC_720"), "")
        try:
            from fleet_knobs.registry import BY_NAME
        except Exception:  # noqa: BLE001 - a host without fleet_knobs reads every knob as unset
            BY_NAME = {}
        for name in (registry_api.URL_KNOB, registry_api.KEY_KNOB):
            if name not in BY_NAME:
                self.assertEqual(real(name), "")

    def test_registry_api_has_no_store_of_its_own(self) -> None:
        # The guard that keeps a second writer from coming back through this module:  it
        # imports no store, opens no file and names no local registry path.
        path = os.path.join(os.path.dirname(registry_api.__file__), "registry_api.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(node.func.id, "open", "registry_api opens a file")
        for banned in ("sqlite3", "operator_registry", "shutil", "tempfile", "pathlib"):
            self.assertNotIn(banned, imported)
        for text in ("registry.sqlite3", "SessionRegistry", "local behaviour", "with_registry_env("):
            self.assertNotIn(text, source)


class FakeRegistryTests(unittest.TestCase):
    """tests/registry_fake.py serves the real API in process for the client tests."""

    def test_the_fake_serves_signed_calls_in_process(self) -> None:
        with FakeRegistry() as fake:
            identity = {
                "platform": "codex",
                "location": "server",
                "scope": PL.SCOPE,
                "session_id": "s-0001",
            }
            made = registry_api.call(
                fake.env(), "register", {**identity, "seat": "CODEX", "source": "test"}
            )
            self.assertEqual(made.get("outcome"), "registered", made)
            self.assertEqual(fake.lookup("codex", "server", PL.SCOPE, "s-0001")["label"], made["label"])
            lookup = {"platform": "codex", "letter": made["label"]}
            found = registry_api.call(fake.env(), "resolve", lookup)
            self.assertEqual(found.get("native_session_id"), "s-0001", found)
            self.assertEqual([method for method, _ in fake.requests], ["POST", "GET"])
            # A wrong key is the API's own refusal, folded to an http_ code.
            refused = registry_api.call(
                fake.env(**{registry_api.KEY_VAR: "x" * 40}), "resolve", lookup
            )
            self.assertIn("error", refused)
            self.assertTrue(str(refused.get("code", "")).startswith("http_"), refused)
            # Any other origin is a dead host, and a dead host is a visible failure.
            dead = registry_api.call(
                fake.env(**{registry_api.URL_ENV: "https://elsewhere.invalid"}), "resolve", lookup
            )
            self.assertEqual(dead.get("code"), "timeout")
            # A principal the fake has no key for is the API's refusal, not a KeyError.
            stranger = registry_api.call(fake.env("NOBODY"), "resolve", lookup)
            self.assertTrue(str(stranger.get("code", "")).startswith("http_"), stranger)
        # The fake's URL is a bare origin, as the registry URL is by contract.
        with self.assertRaises(ValueError):
            FakeRegistry(url="https://registry.example.invalid/some/path")
        # Uninstalled, the module's seams are its own again.
        self.assertIs(registry_api._knob_value, _ORIGINAL_KNOB_VALUE)
        self.assertEqual(registry_api._open.__name__, "_open")


class KnobLatencyTests(unittest.TestCase):
    """The per-hook cost of reading the URL and key from Infisical (owner decision 2).

    NOT YET MEASURED:  it runs only where it can be, the knob names are registered, this
    process holds a fleet_knobs reader token and REGISTRY_API_LIVE_LATENCY=1 opts in to a
    live Infisical read.  None of that holds anywhere today, so it skips in CI and locally,
    and owner decision 2 stays open until someone runs it.  It prints no value."""

    def test_a_knob_sourced_configuration_read_is_under_three_seconds(self) -> None:
        try:
            from fleet_knobs import knobs
            from fleet_knobs.registry import BY_NAME
        except Exception:  # noqa: BLE001
            self.skipTest("fleet_knobs is not importable here")
        if registry_api.URL_KNOB not in BY_NAME or registry_api.KEY_KNOB not in BY_NAME:
            self.skipTest("the registry URL and key knobs are not registered yet")
        if not (os.environ.get("FLEET_KNOBS_INFISICAL_TOKEN") or "").strip():
            self.skipTest("no fleet_knobs reader token in this environment")
        if os.environ.get("REGISTRY_API_LIVE_LATENCY") != "1":
            self.skipTest("live Infisical probe is opt-in: set REGISTRY_API_LIVE_LATENCY=1")
        knobs.reset_for_tests()
        registry_api.reset_knob_cache()
        self.addCleanup(knobs.reset_for_tests)
        self.addCleanup(registry_api.reset_knob_cache)
        started = time.monotonic()
        url = registry_api._knob_value(registry_api.URL_KNOB)
        key = registry_api._knob_value(registry_api.KEY_KNOB)
        elapsed = time.monotonic() - started
        self.assertTrue(url and key, "the knobs did not answer")
        self.assertLess(elapsed, 3.0, "knob-sourced configuration took %.2fs" % elapsed)


if __name__ == "__main__":
    unittest.main()
