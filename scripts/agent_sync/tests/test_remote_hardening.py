"""Cross-transport hardening tests for the three opt-in remote transports (simple-chat, simple-board, recall).

The same cases run against each transport's own copy of the helpers (they are separate files by design).
Hermetic: the network function is replaced in process, except the redirect, byte-cap and deadline cases, which run
the real `_remote_post` against two loopback servers started here.  Nothing reaches the hosted endpoint.
Run:  cd scripts && python3 -m unittest agent_sync.tests.test_remote_hardening
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from agent_sync import cli

SCRIPTS = Path(__file__).resolve().parents[2]


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


recall = _load("recall_hardening_under_test", SCRIPTS / "recall")
board = _load("board_hardening_under_test", SCRIPTS / "linear-board" / "board")

BEARER = "sbt_codex_" + "H" * 43
URL = "https://agents.simplewithus.com/mcp"
GH = "ghp_" + "A1b2C3d4E5" * 4


class Adapter:
    """One transport behind one call shape:  call(tool, args, write, post) -> (result_or_None, error_text)."""

    def __init__(self, name):
        self.name = name
        self.module = {"cli": cli, "recall": recall, "board": board}[name]

    def call(self, tool, args, write, post):
        module = self.module
        if self.name == "cli":
            with mock.patch.object(cli, "_remote_post", post):
                try:
                    return cli._remote_call({"SIMPLE_TOOLS_BEARER": BEARER}, tool, args, write), ""
                except cli.RemoteError as exc:
                    return None, str(exc)
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"SIMPLE_TOOLS_BEARER": BEARER}),
            mock.patch.object(module, "_remote_post", post),
            contextlib.redirect_stderr(err),
        ):
            try:
                result = module.remote_call(tool, args, write)
            except recall.FleetRagError as exc:
                return None, str(exc)
            except SystemExit:
                return None, err.getvalue()
        if self.name == "board":
            return {"structuredContent": result["structured"], "content": result["text"]}, ""
        return result, ""


ADAPTERS = [Adapter("cli"), Adapter("recall"), Adapter("board")]


def rpc(result=None, *, error=None, mid=1):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return json.dumps(msg)


GOOD = {
    "structuredContent": {"seat": "CODEX"},
    "isError": False,
    "content": [{"type": "text", "text": "ok"}],
}


def answer(body, ctype="application/json", status=200):
    return lambda *a: (status, ctype, body.encode() if isinstance(body, str) else body)


class Parsing(unittest.TestCase):
    def each(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                yield ad

    def test_sse_picks_the_response_not_a_leading_notification_and_joins_data_lines(self):
        pretty = json.dumps(json.loads(rpc(GOOD)), indent=1)
        data = "\n".join("data: " + line for line in pretty.split("\n"))
        stream = (
            'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
            ": keepalive\r\n" + data + "\r\n\r\n"
        )
        for ad in self.each():
            result, err = ad.call("whoami", {}, False, answer(stream, "text/event-stream"))
            self.assertEqual(err, "")
            self.assertEqual(result["structuredContent"], {"seat": "CODEX"})

    def test_sse_with_no_response_event_is_an_error_not_a_guess(self):
        stream = 'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        for ad in self.each():
            result, err = ad.call("whoami", {}, False, answer(stream, "text/event-stream"))
            self.assertIsNone(result)
            self.assertIn("no readable answer", err)

    def test_deep_and_heavy_json_are_refused_before_use(self):
        deep = "[" * 100_000 + "]" * 100_000
        heavy = rpc(
            {"content": [], "structuredContent": {"v": list(range(150_000))}, "isError": False}
        )
        for ad in self.each():
            _, err = ad.call("whoami", {}, False, answer(deep))
            self.assertIn("nests too deeply", err)
            _, err = ad.call("whoami", {}, False, answer(heavy))
            self.assertIn("oversized", err)

    def test_malformed_envelopes_are_refused(self):
        bad = [
            rpc({"content": "x", "isError": False}),
            rpc({"content": [{"type": "text", "text": 5}], "isError": False}),
            rpc({"content": [], "structuredContent": [1], "isError": False}),
            rpc({"content": [], "isError": "yes"}),
            rpc("nope"),
            rpc(GOOD, mid=2),
            json.dumps({"jsonrpc": "garbage", "id": True, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "id": True, "result": GOOD}),
            json.dumps({"jsonrpc": "2.0", "id": 1.0, "result": GOOD}),
            json.dumps({"jsonrpc": "2.0", "id": "1", "result": GOOD}),
            json.dumps({"id": 1, "result": GOOD}),
            json.dumps({"jsonrpc": "1.0", "id": 1, "result": GOOD}),
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": GOOD, "error": {"code": -1}}),
            json.dumps({"jsonrpc": "2.0", "id": 1}),
        ]
        for ad in self.each():
            for body in bad:
                result, err = ad.call("whoami", {}, False, answer(body))
                self.assertIsNone(result, body)
                self.assertTrue(err, body)

    def test_the_servers_real_error_shape_has_no_structured_content_and_its_code_still_shows(self):
        # contract.js errorResult:  isError, no structuredContent, the object in text (JSON) and in _meta.
        secret = "ghp_" + "Z9y8X7w6V5" * 4
        obj = {"code": "identity_required", "message": secret, "retryable": False}
        text_only = rpc({"isError": True, "content": [{"type": "text", "text": json.dumps(obj)}]})
        meta_only = rpc(
            {
                "isError": True,
                "content": [{"type": "text", "text": "x"}],
                "_meta": {"agent-sync/error": obj},
            }
        )
        limited = rpc(
            {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"code": "rate_limited", "message": secret, "retry_after_s": 9}
                        ),
                    }
                ],
            }
        )
        unknown = rpc(
            {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"code": "safe_looking_unknown", "message": secret}),
                    }
                ],
            }
        )
        not_json = rpc({"isError": True, "content": [{"type": "text", "text": secret}]})
        later = rpc(
            {
                "isError": True,
                "content": [
                    {"type": "text", "text": "not json at all"},
                    {"type": "text", "text": "5"},
                    {"type": "text", "text": json.dumps({"code": "paused", "message": secret})},
                ],
            }
        )
        fractional = rpc(
            {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"code": "rate_limited", "retry_after_s": 2.5}),
                    }
                ],
            }
        )
        bad_retry = [
            rpc(
                {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"code": "rate_limited", "retry_after_s": v}),
                        }
                    ],
                }
            )
            for v in (True, -1, 86401, 86400.5, "3")
        ]
        for ad in self.each():
            for body in bad_retry:
                _, err = ad.call("zulip_post", {}, False, answer(body))
                self.assertNotIn("retry after", err)
            _, err = ad.call(
                "zulip_post",
                {},
                False,
                answer(
                    rpc(
                        {
                            "isError": True,
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {"code": "rate_limited", "retry_after_s": 86400}
                                    ),
                                }
                            ],
                        }
                    )
                ),
            )
            self.assertIn("retry after 86400s", err)
            for body, expect in (
                (later, "failed on the hosted server: paused"),
                (fractional, "rate_limited, retry after 3s"),
                (text_only, "zulip_post failed on the hosted server: identity_required"),
                (meta_only, "zulip_post failed on the hosted server: identity_required"),
                (limited, "rate_limited, retry after 9s"),
                (unknown, "hosted server: error"),
                (not_json, "hosted server: error"),
            ):
                result, err = ad.call("zulip_post", {}, False, answer(body))
                self.assertIsNone(result)
                self.assertIn(expect, err)
                self.assertNotIn(secret, err)
                self.assertNotIn("may or may not", err)

    def test_error_text_from_the_server_is_never_copied_only_an_allow_listed_code(self):
        secret = "ghp_" + "Z9y8X7w6V5" * 4
        cases = [
            (
                rpc(
                    {
                        "isError": True,
                        "structuredContent": {
                            "code": "rate_limited",
                            "message": secret,
                            "retry_after": 7,
                        },
                        "content": [{"type": "text", "text": secret}],
                    }
                ),
                "rate_limited, retry after 7s",
            ),
            (
                rpc({"isError": True, "structuredContent": {"code": {"x": secret}}}),
                "failed on the hosted server: error",
            ),
            (
                rpc({"isError": True, "structuredContent": {"code": "bad code\x1b[31m"}}),
                "hosted server: error",
            ),
            (
                rpc({"isError": True, "structuredContent": {"code": "x" * 200}}),
                "hosted server: error",
            ),
            (
                rpc({"isError": True, "structuredContent": {"code": "ghp_" + "A1b2C3d4E5" * 4}}),
                "hosted server: error",
            ),
            (
                rpc({"isError": True, "structuredContent": {"code": "FULLSECRET"}}),
                "hosted server: error",
            ),
            (
                rpc({"isError": True, "structuredContent": {"code": "safe_looking_unknown"}}),
                "hosted server: error",
            ),
            (rpc({"isError": True, "structuredContent": {"code": 4242}}), "hosted server: error"),
            (rpc(error={"code": -32000, "message": secret}), "refused the call"),
        ]
        for ad in self.each():
            for body, expect in cases:
                result, err = ad.call("zulip_post", {}, False, answer(body))
                self.assertIsNone(result)
                self.assertIn(expect, err)
                self.assertNotIn(secret, err)
                self.assertNotIn("\x1b", err)
                self.assertNotIn("FULLSECRET", err)
                self.assertNotIn("ghp_", err)
                self.assertNotIn("safe_looking_unknown", err)

    def test_the_bearer_is_never_in_an_error(self):
        for ad in self.each():
            for status in (401, 403, 500):
                _, err = ad.call("whoami", {}, False, answer("", status=status))
                self.assertNotIn(BEARER, err)
                self.assertNotIn(BEARER[:8], err)
                self.assertNotIn(BEARER[-4:], err)


class WriteOutcome(unittest.TestCase):
    def test_a_write_that_may_have_been_sent_says_so_and_a_read_does_not(self):
        sent = lambda *a: (_ for _ in ()).throw(ad.module._TransportError(True))  # noqa: E731
        unsent = lambda *a: (_ for _ in ()).throw(ad.module._TransportError(False))  # noqa: E731
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                _, err = ad.call("zulip_post", {"text": "hi"}, True, sent)
                self.assertIn("may or may not have been applied", err)
                _, err = ad.call("whoami", {}, False, sent)
                self.assertNotIn("may or may not", err)
                _, err = ad.call("zulip_post", {"text": "hi"}, True, unsent)
                self.assertNotIn("may or may not", err)
                self.assertIn("could not be reached", err)
                _, err = ad.call("zulip_post", {"text": "hi"}, True, answer("", status=502))
                self.assertIn("may or may not have been applied", err)
                _, err = ad.call("zulip_post", {"text": "hi"}, True, answer("", status=404))
                self.assertNotIn("may or may not", err)
                _, err = ad.call("zulip_post", {"text": "hi"}, True, answer("not json"))
                self.assertIn("may or may not have been applied", err)
                body = rpc({"isError": True, "structuredContent": {"code": "outcome_unknown"}})
                _, err = ad.call("zulip_post", {"text": "hi"}, True, answer(body))
                self.assertIn("may or may not have been applied", err)


class SecretScan(unittest.TestCase):
    def test_credential_shaped_arguments_are_refused_before_any_send(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                sent = []

                def post(*a):
                    sent.append(a)
                    return 200, "application/json", rpc(GOOD).encode()

                for args, field in (
                    ({"query": "rotate " + GH}, "query"),
                    ({"text": "ok", "to": ["a@b.example", GH]}, "to[1]"),
                    ({"nest": {"deep": [{"v": GH}]}}, "nest.deep[0].v"),
                    ({"text": "pasted " + BEARER}, "text"),
                ):
                    result, err = ad.call("zulip_post", args, True, post)
                    self.assertIsNone(result)
                    self.assertIn("secret_scan:" + field, err)
                    for part in (GH, GH[:12], BEARER, BEARER[:12]):
                        self.assertNotIn(part, err)
                self.assertEqual(sent, [])

    def test_legitimate_arguments_still_go_through(self):
        ok = {
            "query": "how do I rotate a token and reset a password?",
            "limit": 5,
            "flag": True,
            "text": "pm2 start does not re-read env; use --update-env on restart.",
        }
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                result, err = ad.call("recall_search", ok, False, answer(rpc(GOOD)))
                self.assertEqual(err, "")
                self.assertIsNotNone(result)

    def test_the_scanner_loads_from_the_real_layout_and_is_a_required_release_file(self):
        self.assertTrue(callable(recall._remote_scanner().scan))
        self.assertTrue(callable(board._remote_scanner().scan))
        release = _load("release_under_test_hardening", SCRIPTS / "fleet-cli-release.py")
        self.assertIn("scripts/agent_sync/secretscan.py", release.TOOL_REQUIRED)
        self.assertTrue(release.allowed_path("scripts/agent_sync/secretscan.py"))

    def test_an_unloadable_scanner_means_nothing_is_sent(self):
        for module, name in ((recall, "recall"), (board, "board")):
            with self.subTest(transport=name):
                sent = []

                def post(*a):
                    sent.append(a)
                    return 200, "application/json", rpc(GOOD).encode()

                with mock.patch(
                    "importlib.util.spec_from_file_location", side_effect=OSError("gone")
                ):
                    result, err = Adapter(name).call("recall_search", {"query": "x"}, False, post)
                self.assertIsNone(result)
                self.assertIn("secret scanner is not available", err)
                self.assertEqual(sent, [])


class _Recorder(BaseHTTPRequestHandler):
    log: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.log.append((self.path, self.headers.get("Authorization")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def _serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class RealTransport(unittest.TestCase):
    def test_a_redirect_is_never_followed_and_the_bearer_never_leaves_the_first_origin(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                seen = []

                class Target(_Recorder):
                    log = seen

                target = _serve(Target)

                class Redirect(BaseHTTPRequestHandler):
                    def log_message(self, *a):
                        pass

                    def do_POST(self):
                        self.send_response(307)
                        self.send_header(
                            "Location", f"http://127.0.0.1:{target.server_port}/stolen"
                        )
                        self.end_headers()

                origin = _serve(Redirect)
                try:
                    status, _, _ = ad.module._remote_post(
                        f"http://127.0.0.1:{origin.server_port}/mcp",
                        {"Authorization": "Bearer " + BEARER},
                        b"{}",
                        5,
                    )
                finally:
                    origin.shutdown()
                    target.shutdown()
                self.assertEqual(status, 307)
                self.assertEqual(seen, [], "the redirect target received a request")

    def test_the_body_is_capped_and_read_under_one_total_deadline(self):
        class Big(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    for _ in range(70):
                        self.wfile.write(b"x" * 65536)
                except OSError:
                    pass

        class Drip(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.15)
                except OSError:
                    pass

        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                big, drip = _serve(Big), _serve(Drip)
                try:
                    with self.assertRaises(ad.module._TransportError) as ctx:
                        ad.module._remote_post(
                            f"http://127.0.0.1:{big.server_port}/", {}, b"{}", 10
                        )
                    self.assertTrue(ctx.exception.sent)
                    started = time.monotonic()
                    with self.assertRaises(ad.module._TransportError):
                        ad.module._remote_post(
                            f"http://127.0.0.1:{drip.server_port}/", {}, b"{}", 1
                        )
                    self.assertLess(time.monotonic() - started, 1.6)
                finally:
                    big.shutdown()
                    drip.shutdown()

    def test_a_refused_connection_is_proved_unsent(self):
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                with self.assertRaises(ad.module._TransportError) as ctx:
                    ad.module._remote_post(f"http://127.0.0.1:{port}/", {}, b"{}", 2)
                self.assertFalse(ctx.exception.sent)


class Stall(unittest.TestCase):
    def test_a_server_that_stalls_after_the_headers_is_cut_off_at_the_deadline(self):
        class Stall(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.flush()
                time.sleep(4)

        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                server = _serve(Stall)
                try:
                    started = time.monotonic()
                    with self.assertRaises(ad.module._TransportError) as ctx:
                        ad.module._remote_post(
                            f"http://127.0.0.1:{server.server_port}/", {}, b"{}", 1
                        )
                    self.assertTrue(ctx.exception.sent)
                    self.assertLess(time.monotonic() - started, 1.6)
                finally:
                    server.shutdown()


class DeadlineArithmetic(unittest.TestCase):
    """The total budget covers the open and every read, including the final empty (EOF) read, on a mocked clock."""

    class Sock:
        def settimeout(self, value):
            self.last = value

    class Raw:
        def __init__(self):
            self._sock = DeadlineArithmetic.Sock()

    class Fp:
        def __init__(self):
            self.raw = DeadlineArithmetic.Raw()

    class Resp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self, chunks):
            self.fp = DeadlineArithmetic.Fp()
            self.chunks = list(chunks)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read1(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    def run_with_clock(self, module, clock, chunks, post=None):
        resp = self.Resp(chunks)

        class Op:
            def open(self, *a, **k):
                return resp

        with (
            mock.patch.object(module.urllib.request, "build_opener", return_value=Op()),
            mock.patch.object(module.time, "monotonic", side_effect=clock),
        ):
            return (post or module._remote_post)(
                "https://agents.simplewithus.com/mcp", {}, b"{}", 1
            )

    def test_an_eof_that_arrives_after_the_deadline_is_a_failure_not_an_empty_success(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                # start 0 (deadline 1), loop remaining check 0.8, EOF read returns at 1.5
                with self.assertRaises(ad.module._TransportError) as ctx:
                    self.run_with_clock(ad.module, [0.0, 0.8, 1.5], [])
                self.assertTrue(ctx.exception.sent)

    def test_an_open_that_used_the_whole_budget_is_a_failure_before_any_read(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                with self.assertRaises(ad.module._TransportError):
                    self.run_with_clock(ad.module, [0.0, 1.2], [b"{}"])

    def test_a_response_with_no_socket_to_bound_is_refused_not_read_unbounded(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                resp = self.Resp([b"{}"])
                resp.fp = None

                class Op:
                    def open(self, *a, **k):
                        return resp

                with mock.patch.object(ad.module.urllib.request, "build_opener", return_value=Op()):
                    with self.assertRaises(ad.module._TransportError) as ctx:
                        ad.module._remote_post("https://agents.simplewithus.com/mcp", {}, b"{}", 1)
                self.assertTrue(ctx.exception.sent)
                self.assertEqual(resp.chunks, [b"{}"], "the body was read without a bounded socket")

    def test_a_read_that_finishes_inside_the_budget_succeeds(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                status, _, body = self.run_with_clock(ad.module, [0.0, 0.1, 0.2, 0.3, 0.4], [b"{}"])
                self.assertEqual((status, body), (200, b"{}"))

    def test_a_late_failure_on_a_write_reports_the_unknown_outcome(self):
        for ad in ADAPTERS:
            with self.subTest(transport=ad.name):
                real = ad.module._remote_post

                def post(*a):
                    return self.run_with_clock(ad.module, [0.0, 0.8, 1.5], [], post=real)

                _, err = ad.call("zulip_post", {"text": "hi"}, True, post)
                self.assertIn("may or may not have been applied", err)


@contextlib.contextmanager
def scanner_missing():
    """`from . import secretscan` fails as it would if the file were absent from the release."""
    import agent_sync

    saved = getattr(agent_sync, "secretscan", None)
    if saved is not None:
        del agent_sync.secretscan
    try:
        with mock.patch.dict("sys.modules", {"agent_sync.secretscan": None}):
            yield
    finally:
        if saved is not None:
            agent_sync.secretscan = saved


class ScannerImport(unittest.TestCase):
    def test_a_missing_scanner_in_simple_chat_is_a_clean_refusal_not_a_traceback(self):
        sent = []
        with scanner_missing(), mock.patch.object(cli, "_remote_post", lambda *a: sent.append(a)):
            with self.assertRaises(cli.RemoteError) as ctx:
                cli._remote_call({"SIMPLE_TOOLS_BEARER": BEARER}, "whoami", {}, False)
        self.assertIn("secret scanner is not available", str(ctx.exception))
        self.assertEqual(sent, [])

    def test_the_cli_entrypoint_exits_2_with_no_traceback_when_the_scanner_is_missing(self):
        import tempfile

        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as home, scanner_missing():
            code = cli.main(
                ["whoami"],
                prog="simple-chat",
                env={"SIMPLE_CHAT_REMOTE": "1", "SIMPLE_TOOLS_BEARER": BEARER},
                stdout=out,
                stderr=err,
                stdin=io.StringIO(),
                home=Path(home),
            )
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertIn("secret scanner is not available", err.getvalue())


class CodeAllowList(unittest.TestCase):
    def test_every_hosted_error_code_is_allowed_and_nothing_else(self):
        src = SCRIPTS / "agent-sync-mcp" / "src"
        found = set()
        for path in src.glob("*.js"):
            found.update(re.findall(r'new ToolError\("([a-z_]+)"', path.read_text()))
            found.update(re.findall(r'(?<![A-Za-z])ToolError\("([a-z_]+)"', path.read_text()))
        self.assertTrue(found)
        for module in (cli, recall, board):
            with self.subTest(module=module.__name__):
                self.assertEqual(found - set(module._REMOTE_CODES), set())
                self.assertEqual(module._remote_code("rate_limited"), "rate_limited")
                self.assertEqual(module._remote_code("ghp_" + "A" * 40), "error")
                self.assertEqual(module._remote_code(5), "error")


class NoFallbackPattern(unittest.TestCase):
    def test_the_three_copies_of_the_helpers_agree_on_their_limits(self):
        for module in (cli, recall, board):
            self.assertEqual(module.REMOTE_MAX_BYTES, 4_000_000)
            self.assertEqual(module.REMOTE_MAX_DEPTH, 64)
            self.assertEqual(module.REMOTE_MAX_NODES, 100_000)


if __name__ == "__main__":
    unittest.main()
