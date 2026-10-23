import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from whatsapp_bridge.bridge import (
    Bridge,
    BridgeError,
    State,
    StopBridge,
    bounded_tree,
    digest,
    normalize,
)


def payload(text="hello", offset=9223372036854775807):
    return {
        "object": "whatsapp_agent_platform",
        "entry": [
            {
                "id": "123456",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "id": "wamid.1234",
                                    "from": "user:owner",
                                    "timestamp": "1792700000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                            "statuses": [],
                        },
                    }
                ],
            }
        ],
        "next_offset": offset,
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"WA_SENDER_HASH_JET": digest("user:owner", "k" * 64)})
        self.env.start()
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(self.directory.name)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()
        self.env.stop()

    def test_persist_only_exact_ack_and_sign_raw_body(self):
        calls = []

        def request(url, headers, body=None):
            calls.append((url, headers, body))
            if body is None:
                return payload("👀 silent")
            decoded = json.loads(body)
            self.assertEqual(
                headers["X-WA-Bridge-Signature"],
                hmac.new(b"k" * 64, body, hashlib.sha256).hexdigest(),
            )
            self.assertEqual(decoded["messages"][0]["text"], "👀 silent")
            self.assertEqual(decoded["messages"][0]["sender_hash"], digest("user:owner", "k" * 64))
            self.assertNotIn("user:owner", body.decode())
            return {"ok": True, "processed": True, "next_offset": decoded["next_offset"]}

        Bridge(self.state, "synthetic", "k" * 64, "https://worker.example", request).step()
        self.assertEqual(self.state.offset(), 9223372036854775807)
        self.assertEqual(len(calls), 2)
        self.assertIn("timeout=25", calls[0][0])
        with open(os.path.join(self.directory.name, "state.sqlite3"), "rb") as f:
            data = f.read()
        self.assertNotIn("👀 silent".encode(), data)
        self.assertNotIn(b"synthetic", data)

    def test_bad_ack_never_advances(self):
        for answer in [
            None,
            {},
            {"ok": True, "next_offset": "9223372036854775807"},
            {"ok": True, "processed": True, "next_offset": 9223372036854775807},
        ]:
            bridge = Bridge(
                self.state,
                "synthetic",
                "k" * 64,
                "https://worker.example",
                lambda url, headers, body=None, answer=answer: (
                    payload() if body is None else answer
                ),
            )
            with self.assertRaises(BridgeError):
                bridge.step()
            self.assertEqual(self.state.offset(), 0)

    def test_restart_and_single_local_poller(self):
        self.state.save(123)
        with self.assertRaisesRegex(StopBridge, "poller_already_running"):
            State(self.directory.name)
        self.state.close()
        self.state = State(self.directory.name)
        self.assertEqual(self.state.offset(), 123)

    def test_strict_batch_validation_and_unknown_type(self):
        for change in [
            {"next_offset": 1.0},
            {"next_offset": -1},
            {"next_offset": 2**63},
            {"object": "wrong"},
            {"entry": []},
        ]:
            with self.assertRaises(BridgeError):
                normalize({**payload(), **change}, 0, "k" * 64)
        p = payload()
        p["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "new_type"
        self.assertIsNone(normalize(p, 0, "k" * 64)["messages"][0]["text"])
        with self.assertRaises(BridgeError):
            normalize(payload(offset=5), 6, "k" * 64)
        with self.assertRaises(BridgeError):
            normalize(payload("x" * 4097), 0, "k" * 64)

    def test_idle_poll_heartbeats(self):
        self.state.save(99)

        def request(url, headers, body=None):
            if body is None:
                return None
            decoded = json.loads(body)
            self.assertEqual(decoded["messages"], [])
            self.assertEqual(decoded["agent_hash"], "a" * 64)
            return {"ok": True, "processed": True, "next_offset": "99"}

        with patch.dict(os.environ, {"WA_AGENT_HASH_JET": "a" * 64}):
            Bridge(self.state, "synthetic", "k" * 64, "https://worker.example", request).step()
        self.assertEqual(self.state.offset(), 99)

    def test_limits_and_urls(self):
        nested = {}
        child = nested
        for _ in range(18):
            child["x"] = {}
            child = child["x"]
        with self.assertRaises(BridgeError):
            bounded_tree(nested)
        for url in [
            "http://worker.example",
            "https://user:pass@worker.example",
            "https://worker.example/path",
            "https://worker.example?x=1",
        ]:
            with self.assertRaises(StopBridge):
                Bridge(self.state, "synthetic", "k" * 64, url)


class BindingTests(unittest.TestCase):
    def test_code_and_creator_check_both_required(self):
        from whatsapp_bridge.binding import verify_candidate

        code = "JET-0123456789ABCDEF"
        p = payload(code)
        calls = []

        def request(url, headers, body):
            calls.append(json.loads(body))
            return {"success": True}

        pin = verify_candidate(
            p,
            code,
            "synthetic",
            1792699999,
            1792700100,
            request,
            now=lambda: 1792700001,
            hash_key="k" * 64,
        )
        self.assertEqual(pin["verified_via"], "code+creator_check")
        self.assertEqual(pin["sender_hash"], digest("user:owner", "k" * 64))
        self.assertEqual(calls[0]["status"], "read")
        with self.assertRaises(StopBridge):
            verify_candidate(
                p,
                "JET-FFFFFFFFFFFFFFFF",
                "synthetic",
                1792699999,
                1792700100,
                request,
                now=lambda: 1792700001,
                hash_key="k" * 64,
            )
        with self.assertRaises(StopBridge):
            verify_candidate(
                p,
                code,
                "synthetic",
                1792699999,
                1792700100,
                lambda *a: {"success": False},
                now=lambda: 1792700001,
                hash_key="k" * 64,
            )

    def test_supervised_binding_shares_lock_and_preserves_live_cursor(self):
        from whatsapp_bridge.binding import bind_once

        code = "JET-0123456789ABCDEF"
        responses = [payload("old", offset=20), payload(code, offset=21), {"success": True}]
        calls = []

        def request(url, headers, body=None):
            calls.append(url)
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                state.save(10)
                state.begin_binding("k" * 64, 1792699999, code=code)
                pin = bind_once(
                    state,
                    "synthetic",
                    code,
                    1792699999,
                    1792700100,
                    request,
                    now=lambda: 1792700001,
                    hash_key="k" * 64,
                )
                self.assertEqual(pin["verified_via"], "code+creator_check")
                self.assertEqual(state.offset(), 10, "do not lose messages in the binding batch")
                self.assertIn("offset=20", calls[1])
            finally:
                state.close()


class ReviewRegressionTests(unittest.TestCase):
    def test_large_utf8_batch_filters_foreign_messages_and_keeps_caption(self):
        p = payload("😀" * 4096)
        messages = p["entry"][0]["changes"][0]["value"]["messages"]
        messages[:] = [{**messages[0], "id": f"wamid.{i:04}"} for i in range(99)]
        messages.append(
            {
                "id": "wamid.9999",
                "from": "user:stranger",
                "type": "text",
                "text": {"body": "foreign body"},
                "timestamp": "1792700000",
            }
        )
        captured = []

        def request(url, headers, body=None):
            if body is None:
                return p
            captured.append(body)
            return {"ok": True, "processed": True, "next_offset": str(p["next_offset"])}

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WA_SENDER_HASH_JET": digest("user:owner", "k" * 64)}),
        ):
            state = State(directory)
            try:
                Bridge(state, "synthetic", "k" * 64, "https://worker.example", request).step()
            finally:
                state.close()
        self.assertGreater(len(captured[0]), 1024 * 1024)
        self.assertLess(len(captured[0]), 8 * 1024 * 1024)
        self.assertNotIn(b"foreign body", captured[0])
        m = payload()["entry"][0]["changes"][0]["value"]["messages"][0]
        m.update(type="image", image={"caption": " \n👀 private photo"})
        p["entry"][0]["changes"][0]["value"]["messages"] = [m]
        self.assertEqual(normalize(p, 0, "k" * 64)["messages"][0]["text"], " \n👀 private photo")

    def test_generated_binding_code_is_hashed_single_use_and_expiring(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                code = state.begin_binding("k" * 64, 1000)
                self.assertRegex(code, r"^JET-[A-F0-9]{16}$")
                self.assertEqual(state.binding_window(code, "k" * 64, 1001), (1000, 1600))
                with self.assertRaises(StopBridge):
                    state.binding_window(code, "k" * 64, 1601)
                state.spend_binding(code, "k" * 64, 1002)
                with self.assertRaises(StopBridge):
                    state.binding_window(code, "k" * 64, 1003)
                with open(os.path.join(directory, "state.sqlite3"), "rb") as file:
                    self.assertNotIn(code.encode(), file.read())
            finally:
                state.close()

    def test_creator_403_and_expired_binding_never_produce_pin(self):
        from whatsapp_bridge.binding import verify_candidate

        code = "JET-0123456789ABCDEF"

        def refused(*args):
            raise StopBridge("http_403")

        with self.assertRaisesRegex(StopBridge, "http_403"):
            verify_candidate(
                payload(code),
                code,
                "synthetic",
                1792699999,
                1792700100,
                refused,
                now=lambda: 1792700001,
                hash_key="k" * 64,
            )
        with self.assertRaisesRegex(StopBridge, "binding_window_closed"):
            verify_candidate(
                payload(code),
                code,
                "synthetic",
                1792699999,
                1792700100,
                refused,
                now=lambda: 1792700101,
                hash_key="k" * 64,
            )


class LaunchTests(unittest.TestCase):
    def test_dedicated_identity_and_explicit_project_without_shared_fallback(self):
        from pathlib import Path

        from whatsapp_bridge.launch import POLL_NAMES, arguments, prepare

        env = prepare(
            {
                "WA_INFISICAL_PROJECT_ID": "synthetic-project",
                "INFISICAL_AUTOMATION_CLIENT_SECRET": "shared-forbidden",
            },
            {"client_id": "synthetic-id", "client_secret": "synthetic-secret"},
        )
        self.assertEqual(env["INFISICAL_AUTOMATION_CLIENT_SECRET"], "synthetic-secret")
        self.assertEqual(env["INFISICAL_FLEET_OPS_PROJECT_ID"], "synthetic-project")
        self.assertNotIn("INFISICAL_TOKEN", env)
        self.assertEqual(arguments("poll", Path("/release"))[3], ",".join(POLL_NAMES))
        with self.assertRaises(ValueError):
            prepare(
                {"WA_INFISICAL_PROJECT_ID": "x", "INFISICAL_TOKEN": "forbidden"},
                {"client_id": "x", "client_secret": "y"},
            )
        with self.assertRaises(ValueError):
            prepare({}, {"client_id": "x", "client_secret": "y"})


class BoundaryRegressionTests(unittest.TestCase):
    def test_binding_cli_normalizes_key_before_hashing_owner_identity(self):
        import contextlib
        import io
        import runpy

        code = "JET-0123456789ABCDEF"
        output = io.StringIO()

        def request(url, headers, body=None):
            return {"success": True} if body is not None else payload(code)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "WHATSAPP_API_KEY": "synthetic-api-key",
                    "WA_BRIDGE_HMAC_KEY_JET": " \n" + "k" * 64 + "\n ",
                    "WA_STATE_DIRECTORY": directory,
                },
            ),
            patch("whatsapp_bridge.bridge.request_json", request),
            patch("secrets.token_hex", return_value="0123456789abcdef"),
            patch("time.time", return_value=1792700000),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            runpy.run_module("whatsapp_bridge.binding", run_name="__main__")
        pin = json.loads(output.getvalue())
        self.assertEqual(pin["agent_hash"], digest("123456", "k" * 64))
        self.assertEqual(pin["sender_hash"], digest("user:owner", "k" * 64))

    def test_http_classification_user_agent_and_no_redirect(self):
        import urllib.error
        from email.message import Message

        from whatsapp_bridge.bridge import NoRedirect, request_json

        self.assertIsNone(
            NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example")
        )
        for status in (302, 418, 429, 503):
            with patch("urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError(
                    "https://test", status, "test", Message(), None
                )
                with self.assertRaises(BridgeError) as caught:
                    request_json("https://api.whatsapp.com/test", {})
                self.assertEqual(isinstance(caught.exception, StopBridge), status in (302, 418))
                req = opener.return_value.open.call_args.args[0]
                self.assertEqual(req.get_header("User-agent"), "AFC-WhatsApp-Bridge/1")
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_schema_counter_is_consecutive_and_separate(self):
        import contextlib
        import io

        from whatsapp_bridge.bridge import main

        with tempfile.TemporaryDirectory() as directory:
            env = {
                name: "synthetic"
                for name in (
                    "WHATSAPP_API_KEY",
                    "WA_BRIDGE_HMAC_KEY_JET",
                    "WA_WORKER_URL",
                    "WA_AGENT_HASH_JET",
                    "WA_SENDER_HASH_JET",
                )
            }
            env.update(WA_STATE_DIRECTORY=directory, WHATSAPP_BRIDGE_ENABLED="on")
            with (
                patch.dict(os.environ, env),
                patch("whatsapp_bridge.bridge.Bridge") as bridge,
                patch("time.sleep"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                bridge.return_value.step.side_effect = [
                    BridgeError("network_error"),
                    BridgeError("network_error"),
                    BridgeError("schema_error"),
                    StopBridge("test_exit"),
                ]
                with self.assertRaisesRegex(StopBridge, "test_exit"):
                    main()
                bridge.return_value.step.side_effect = [BridgeError("schema_error")] * 3
                with self.assertRaisesRegex(StopBridge, "schema_stop"):
                    main()

    def test_state_symlinks_are_rejected_and_numeric_ack_is_not_boolean(self):
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actual").mkdir()
            (root / "link").symlink_to(root / "actual", target_is_directory=True)
            with self.assertRaisesRegex(StopBridge, "unsafe_state_directory"):
                State(root / "link")
            state = State(root / "actual")
            try:

                def request(url, headers, body=None):
                    return (
                        payload()
                        if body is None
                        else {"ok": 1, "processed": 1, "next_offset": str(payload()["next_offset"])}
                    )

                with (
                    patch.dict(os.environ, {"WA_SENDER_HASH_JET": digest("user:owner", "k" * 64)}),
                    self.assertRaisesRegex(BridgeError, "ingest_not_acknowledged"),
                ):
                    Bridge(state, "synthetic", "k" * 64, "https://worker.example", request).step()
            finally:
                state.close()

    def test_loaded_credentials_are_redacted_before_worker_relay(self):
        def request(url, headers, body=None):
            if body is None:
                return payload("👀 keep synthetic private")
            self.assertNotIn(b"synthetic", body)
            self.assertIn(b"[redacted:loaded credential]", body)
            return {"ok": True, "processed": True, "next_offset": str(payload()["next_offset"])}

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WA_SENDER_HASH_JET": digest("user:owner", "k" * 64)}),
        ):
            state = State(directory)
            try:
                Bridge(state, "synthetic", "k" * 64, "https://worker.example", request).step()
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
