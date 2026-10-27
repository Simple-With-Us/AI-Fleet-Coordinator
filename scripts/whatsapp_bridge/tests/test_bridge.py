import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest.mock import mock_open, patch

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

        def request(url, headers, body=None, **_options):
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

        def request(url, headers, body=None, **_options):
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

        def request(url, headers, body, **_options):
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
                lambda *a, **kw: {"success": False},
                now=lambda: 1792700001,
                hash_key="k" * 64,
            )

    def test_future_binding_candidate_is_skipped_without_spending_the_code(self):
        from whatsapp_bridge.binding import bind_once, verify_candidate

        code = "JET-0123456789ABCDEF"
        future = payload(code, offset=21)
        future["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = "1792700120"
        with self.assertRaisesRegex(StopBridge, "binding_candidate_not_unique"):
            verify_candidate(
                future,
                code,
                "synthetic",
                1792699999,
                1792700500,
                request=lambda *a, **kw: self.fail("future candidate sent a read receipt"),
                now=lambda: 1792700001,
                hash_key="k" * 64,
            )
        answers = [future, payload(code, offset=22), {"success": True}]
        calls = []

        def request(url, headers, body=None, **options):
            calls.append(body is not None)
            return answers.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                state.begin_binding("k" * 64, 1792699999, code=code)
                pin = bind_once(
                    state,
                    "synthetic",
                    code,
                    1792699999,
                    1792700500,
                    request=request,
                    now=lambda: 1792700001,
                    hash_key="k" * 64,
                )
                self.assertEqual(pin["cutoff"], 1792700000)
                self.assertEqual(calls, [False, False, True])
                self.assertEqual(state.offset(), 0)
            finally:
                state.close()

    def test_supervised_binding_shares_lock_and_preserves_live_cursor(self):
        from whatsapp_bridge.binding import bind_once

        code = "JET-0123456789ABCDEF"
        responses = [payload("old", offset=20), payload(code, offset=21), {"success": True}]
        calls = []

        def request(url, headers, body=None, **_options):
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

        def request(url, headers, body=None, **_options):
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

        def refused(*args, **_options):
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
    # Synthetic token-shaped fixtures assembled at runtime; never real credentials.
    BRIDGE_TOKEN = "st." + "fixture-bridge" + "." + "not-a-live-token"
    OPERATOR_TOKEN = "st." + "fixture-operator" + "." + "not-a-live-token"
    PROJECT = "00000000-0000-4000-8000-000000000001"  # synthetic AFC project

    def test_dedicated_service_token_rejects_ambient_auth_and_machine_identity(self):
        from whatsapp_bridge.launch import AMBIENT_AUTH, prepare

        env = prepare(
            {"WA_INFISICAL_PROJECT_ID": self.PROJECT, "GH_TOKEN": "unused-shared"},
            self.BRIDGE_TOKEN,
        )
        self.assertEqual(env["INFISICAL_TOKEN"], self.BRIDGE_TOKEN)
        self.assertEqual(env["INFISICAL_FLEET_OPS_PROJECT_ID"], self.PROJECT)
        self.assertEqual(set(env), {"PATH", "INFISICAL_FLEET_OPS_PROJECT_ID", "INFISICAL_TOKEN"})
        for name in AMBIENT_AUTH:
            with self.assertRaisesRegex(ValueError, "ambient_credentials_refused"):
                prepare(
                    {"WA_INFISICAL_PROJECT_ID": self.PROJECT, name: "shared-forbidden"},
                    self.BRIDGE_TOKEN,
                )
        for token in (
            {"client_id": "old", "client_secret": "old"},
            "",
            "identity-access-token",
            "st.a. b",
        ):
            with self.assertRaises((TypeError, ValueError)):
                prepare({"WA_INFISICAL_PROJECT_ID": self.PROJECT}, token)
        with self.assertRaisesRegex(ValueError, "WA_INFISICAL_PROJECT_ID_missing"):
            prepare({}, self.BRIDGE_TOKEN)

    def test_mode_pins_folder_allowlist_credential_and_operator_argument(self):
        from pathlib import Path

        from whatsapp_bridge.launch import (
            BIND_NAMES,
            CREDENTIAL_NAMES,
            OPERATOR_FOLDER,
            OPERATOR_NAMES,
            POLL_NAMES,
            arguments,
        )

        request_id = "00000000-0000-4000-8000-000000000009"
        for mode, folder, names in (
            ("poll", "/jet", POLL_NAMES),
            ("bind", "/jet", BIND_NAMES),
            ("operator", OPERATOR_FOLDER, OPERATOR_NAMES),
        ):
            args = arguments(mode, Path("/release"), request_id if mode == "operator" else None)
            self.assertEqual(args[:4], ["--path", folder, "--only", ",".join(names)])
            self.assertNotIn("WHATSAPP_API_KEY", OPERATOR_NAMES)
        self.assertNotEqual(CREDENTIAL_NAMES["operator"], CREDENTIAL_NAMES["poll"])
        for invalid in ("not-an-id", "", None, True, request_id.upper() + " "):
            with self.assertRaisesRegex(ValueError, "^invalid_operator_request$"):
                arguments("operator", Path("/release"), invalid)
        with self.assertRaises(ValueError):
            arguments("poll", Path("/release"), request_id)

    def _launch(self, mode, token=None, denied=False):
        import contextlib
        import io
        import subprocess
        from pathlib import Path

        from fleet_secrets import cli, core

        from whatsapp_bridge import launch

        exports, children = [], []
        request_id = "00000000-0000-4000-8000-000000000009"
        folder = launch.OPERATOR_FOLDER if mode == "operator" else "/jet"
        expected_token = self.OPERATOR_TOKEN if mode == "operator" else self.BRIDGE_TOKEN
        token = expected_token if token is None else token
        source_names = (
            launch.OPERATOR_NAMES
            if mode == "operator"
            else launch.POLL_NAMES
            if mode == "poll"
            else launch.BIND_NAMES
        )
        values = {name: "synthetic-" + name for name in source_names}
        values["UNRELATED_SECRET"] = "not-in-child"
        if mode == "operator":
            values["WHATSAPP_API_KEY"] = "must-never-reach-operator"
        output = io.StringIO()

        def export(argv, **options):
            exports.append((argv, dict(options["env"])))
            self.assertEqual(argv[1], "export", "no login/session/machine fallback")
            self.assertIn("--env=prod", argv)
            self.assertIn("--projectId=" + self.PROJECT, argv)
            self.assertIn("--path=" + folder, argv)
            self.assertNotIn(token, " ".join(argv))
            # Simulates the server's exact-folder read-only token boundary.
            accepted = not denied and options["env"].get("INFISICAL_TOKEN") == expected_token
            return subprocess.CompletedProcess(
                argv,
                0 if accepted else 1,
                json.dumps(values).encode() if accepted else b"",
                b"forbidden",
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / launch.CREDENTIAL_NAMES[mode]
            path.write_text(token)
            path.chmod(0o600)
            environment = {
                "CREDENTIALS_DIRECTORY": directory,
                "WA_INFISICAL_PROJECT_ID": self.PROJECT,
            }
            with (
                patch.object(core, "find_cli", return_value="/fake/infisical"),
                patch.object(core.subprocess, "run", side_effect=export),
                patch.object(
                    cli.os,
                    "execvpe",
                    side_effect=lambda program, argv, env: children.append(
                        (program, argv, dict(env))
                    ),
                ),
                contextlib.redirect_stderr(output),
                contextlib.redirect_stdout(output),
            ):
                argv = [mode, request_id] if mode == "operator" else [mode]
                if denied or token != expected_token:
                    with self.assertRaises(core.SecretsError):
                        launch.main(argv, environment)
                else:
                    self.assertEqual(launch.main(argv, environment), 0)
        self.assertEqual(len(exports), 1)
        self.assertNotIn(token, output.getvalue())
        if denied or token != expected_token:
            self.assertEqual(children, [])
            return
        self.assertEqual(len(children), 1)
        child = children[0][2]
        self.assertTrue(set(core.AUTH_ENV_VARS).isdisjoint(child))
        self.assertEqual(set(child), {"PATH", "INFISICAL_FLEET_OPS_PROJECT_ID", *source_names})
        self.assertNotIn("CREDENTIALS_DIRECTORY", child)
        if mode == "operator":
            self.assertNotIn("WHATSAPP_API_KEY", child)
            self.assertNotIn("WA_BRIDGE_HMAC_KEY_JET", child)
            self.assertEqual(children[0][1][-1], request_id)

    def test_missing_send_switch_defaults_off_without_weakening_required_keys(self):
        from whatsapp_bridge import launch
        from fleet_secrets import core
        names = {n: "synthetic" for n in launch.POLL_NAMES if n != "WHATSAPP_BRIDGE_SEND_ENABLED"}
        command = launch.arguments("poll", __import__("pathlib").Path("/release"))
        children = []
        with patch.object(core, "fetch_all", return_value=names), patch.object(launch.os, "execvpe", side_effect=lambda *a: children.append(a)):
            launch.run_poll_with_optional_send(command, {"PATH": "/usr/bin", "INFISICAL_TOKEN": "synthetic"})
        self.assertEqual(children[0][2]["WHATSAPP_BRIDGE_SEND_ENABLED"], "off")
        self.assertNotIn("INFISICAL_TOKEN", children[0][2])
        del names["WHATSAPP_API_KEY"]
        with patch.object(core, "fetch_all", return_value=names), patch.object(launch.os, "execvpe") as execute:
            with self.assertRaises(core.SecretsError):
                launch.run_poll_with_optional_send(command, {})
            execute.assert_not_called()

    def test_poller_and_binder_strip_bootstrap_before_exec(self):
        self._launch("poll")
        self._launch("bind")

    def test_operator_has_own_token_folder_and_no_owner_credentials(self):
        self._launch("operator")

    def test_wrong_folder_or_expired_token_fails_closed_without_fallback(self):
        self._launch("poll", self.OPERATOR_TOKEN)
        self._launch("operator", self.BRIDGE_TOKEN)
        self._launch("poll", denied=True)

    def test_operator_boot_preserves_only_client_arguments(self):
        import subprocess
        import sys
        from pathlib import Path

        from whatsapp_bridge.launch import arguments

        request_id = "00000000-0000-4000-8000-000000000009"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "scripts" / "whatsapp_bridge"
            package.mkdir(mode=0o700, parents=True)
            (package / "__init__.py").write_text("")
            (package / "operator_wake.py").write_text(
                "import json,sys; print(json.dumps(sys.argv[1:]))"
            )
            args = arguments("operator", root, request_id)
            result = subprocess.run(
                [sys.executable, *args[6:]], capture_output=True, text=True, check=True, timeout=5
            )
        self.assertEqual(json.loads(result.stdout), [request_id])


class BoundaryRegressionTests(unittest.TestCase):
    def test_binding_requests_share_absolute_deadline(self):
        from whatsapp_bridge.binding import bind_once

        clock = [1599]
        calls = []

        def request(url, headers, body=None, **options):
            calls.append((url, options))
            clock[0] = 1601

        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            try:
                code = state.begin_binding("k" * 64, 1000)
                with self.assertRaisesRegex(StopBridge, "binding_window_closed"):
                    bind_once(
                        state,
                        "synthetic",
                        code,
                        1000,
                        1600,
                        request=request,
                        now=lambda: clock[0],
                        hash_key="k" * 64,
                    )
            finally:
                state.close()
        self.assertEqual(len(calls), 1)
        self.assertIn("timeout=1", calls[0][0])
        self.assertEqual(calls[0][1]["timeout"], 1)

    def test_binding_code_never_falls_back_to_logs_without_console(self):
        import contextlib
        import io

        from whatsapp_bridge.binding import show_binding_code

        output = io.StringIO()
        with (
            patch("builtins.open", side_effect=OSError("private detail")),
            contextlib.redirect_stderr(output),
            contextlib.redirect_stdout(output),
            self.assertRaisesRegex(StopBridge, "binding_console_unavailable"),
        ):
            show_binding_code("JET-0123456789ABCDEF")
        self.assertEqual(output.getvalue(), "")

    def test_credential_file_errors_identify_source_without_contents(self):
        from pathlib import Path

        from whatsapp_bridge.launch import CredentialSourceError, load_service_token

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(CredentialSourceError, "unavailable_systemd_service_token"):
                load_service_token(root, "infisical-jet")
            for content in (b"invalid synthetic secret", b"\xff"):
                (root / "infisical-jet").write_bytes(content)
                (root / "infisical-jet").chmod(0o600)
                with self.assertRaisesRegex(CredentialSourceError, "invalid_systemd_service_token"):
                    load_service_token(root, "infisical-jet")

    def test_non_object_media_is_metadata_only_without_poisoning_batch(self):
        for media in ([], ["provider metadata"], None, "opaque"):
            batch = payload()
            message = batch["entry"][0]["changes"][0]["value"]["messages"][0]
            message.update(type="image", image=media)
            self.assertIsNone(normalize(batch, 0, "k" * 64)["messages"][0]["text"])

    def test_operator_cli_preserves_refused_busy_and_unknown_outcomes(self):
        import contextlib
        import io

        from whatsapp_bridge.operator_wake import main

        env = {"WA_WORKER_URL": "https://worker.example", "WA_OPERATOR_HMAC_KEY_JET": "k" * 64}
        for code, expected in (
            ("network_error", "outcome_unknown"),
            ("http_408", "outcome_unknown"),
            ("http_503", "outcome_unknown"),
            ("invalid_json", "outcome_unknown"),
            ("http_403", "http_403"),
            ("operator_refused", "operator_refused"),
            ("rate_limited:" + "9" * 5000, "operator_busy"),
            ("rate_limited:123", "operator_busy"),
        ):
            output = io.StringIO()
            with (
                patch("whatsapp_bridge.operator_wake.wake", side_effect=BridgeError(code)) as call,
                contextlib.redirect_stderr(output),
            ):
                self.assertEqual(main(["synthetic-request"], env), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["reason"], expected)
            self.assertEqual(call.call_count, 1, "no automatic retries")
            if expected == "operator_busy":
                self.assertEqual(result["retry_after_s"], 123 if code == "rate_limited:123" else 30)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main([], {}), 1)
        self.assertEqual(json.loads(output.getvalue())["required"], "WA_WORKER_URL")

    def test_binding_failure_reports_only_fixed_source_and_detail(self):
        from whatsapp_bridge.binding import failure

        for code, source in (
            ("binding_configuration_missing", "approved_Infisical_runtime"),
            ("unsafe_state_directory", "durable_binding_state"),
            ("http_403", "WhatsApp_provider"),
            ("network_error", "WhatsApp_provider"),
            ("binding_console_unavailable", "supervised_binding"),
        ):
            result = failure(StopBridge(code))
            self.assertFalse(result["ok"])
            self.assertEqual(result["source"], source)
            self.assertEqual(result["detail"], code)
        unknown = failure(ValueError("synthetic-sensitive-provider-text"))
        self.assertEqual(unknown["detail"], "binding_failed")
        self.assertNotIn("synthetic-sensitive-provider-text", json.dumps(unknown))

    def test_binding_cli_normalizes_key_before_hashing_owner_identity(self):
        import contextlib
        import io
        import runpy

        code = "JET-0123456789ABCDEF"
        output = io.StringIO()

        def request(url, headers, body=None, **_options):
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
            patch("builtins.open", mock_open()) as console,
            patch("secrets.token_hex", return_value="0123456789abcdef"),
            patch("time.time", return_value=1792700000),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            runpy.run_module("whatsapp_bridge.binding", run_name="__main__")
            console.assert_called_once_with("/dev/tty", "w", encoding="utf-8")
        pin = json.loads(output.getvalue())
        self.assertEqual(pin["agent_hash"], digest("123456", "k" * 64))
        self.assertEqual(pin["sender_hash"], digest("user:owner", "k" * 64))

    def test_retry_after_is_bounded_before_integer_conversion(self):
        import urllib.error
        from email.message import Message

        from whatsapp_bridge.bridge import request_json, retry_delay

        for value, expected in (
            ("9" * 5000, 30),
            ("１２３", 30),
            ("-1", 30),
            ("0", 1),
            ("999999", 3600),
            ("123", 123),
        ):
            headers = Message()
            headers["Retry-After"] = value
            with patch("urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError(
                    "https://test", 429, "test", headers, None
                )
                with self.assertRaises(BridgeError) as caught:
                    request_json("https://api.whatsapp.com/test", {})
            self.assertEqual(str(caught.exception), f"rate_limited:{expected}")
            self.assertEqual(retry_delay(value), expected)

    def test_operator_negative_ack_is_refused_and_malformed_ack_stays_unknown(self):
        from whatsapp_bridge.operator_wake import wake

        request_id = "00000000-0000-4000-8000-000000000009"
        for answer, expected in (
            ({"ok": False, "reason": "synthetic-sensitive-text"}, "operator_refused"),
            ({"ok": 0}, "operator_not_acknowledged"),
            ({}, "operator_not_acknowledged"),
        ):
            calls = []

            def request(*args, calls=calls, answer=answer):
                calls.append(args)
                return answer

            with self.assertRaisesRegex(BridgeError, "^" + expected + "$"):
                wake("https://worker.example", "k" * 64, request_id, request)
            self.assertEqual(len(calls), 1, "no automatic retry")

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
            (root / "actual").mkdir(mode=0o700)
            (root / "link").symlink_to(root / "actual", target_is_directory=True)
            with self.assertRaisesRegex(StopBridge, "unsafe_state_directory"):
                State(root / "link")
            state = State(root / "actual")
            try:

                def request(url, headers, body=None, **_options):
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
        def request(url, headers, body=None, **_options):
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
