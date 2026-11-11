"""The registry API transport for the package's own registry callers (registry_api.py).

The operator-registry CLI (which the letter sweep drives) and the platform-letter registry
side both speak the registry CLI's dialect against a local file.  With the registry API
URL configured they must speak the same dialect to the server's store instead, and never
open the local file:  these tests run both callers against a loopback stub that serves
the real RegistryService over a server-side store, with no local registry present at all,
and prove the operations land in the server store while no local file is created.  The
contrast case (no URL configured) keeps the pre-cutover behaviour:  the same call writes
the local store.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_registry_api
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from simple_tools import listener_registry as LREG
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import registry_api

KEY = "test-registry-key-0123456789abcdef"  # a test key, long enough for the API's floor


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


class ApiBase(unittest.TestCase):
    def setUp(self) -> None:
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
            registry_api.KEY_ENV: KEY,
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
        issues = [{"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"}]
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
        refused = registry_api.call(env, "update", {**identity, "issues": "AFC-694"})
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
        del no_key[registry_api.KEY_ENV]
        self.assertEqual(
            registry_api.call(no_key, "resolve", {"platform": "minimax", "letter": "A"})[
                "code"
            ],
            "not_configured",
        )
        wrong_key = self.api_env(**{registry_api.KEY_ENV: "x" * 40})
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

    def test_without_the_url_the_same_call_writes_the_local_store(self) -> None:
        # The pre-cutover behaviour, kept:  no URL configured, the local file is the
        # store and the registration lands in it (and not in the server's).
        os.makedirs(os.path.dirname(self.local_db))
        OR.SessionRegistry(self.local_db).close_db()
        env = {"HOME": self.tmp.name, "SIMPLE_TOOLS_STATE_DIR": self.state}
        with open(self.local_db, "rb") as fh:
            before = hashlib.sha256(fh.read()).hexdigest()
        code, made = self.registry(
            self.reg_argv("register", "s-0001", letter_pick="lowest"), env=env
        )
        self.assertEqual(code, 0)
        self.assertEqual(made.get("outcome"), "registered")
        with open(self.local_db, "rb") as fh:
            after = hashlib.sha256(fh.read()).hexdigest()
        self.assertNotEqual(before, after)
        self.assertIsNone(self.server_lookup("codex", "server", "s-0001"))


if __name__ == "__main__":
    unittest.main()
