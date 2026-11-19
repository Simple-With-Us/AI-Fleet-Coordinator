import ast
import contextlib
import io
import sys
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "service_zulip", Path(__file__).with_name("service_zulip.py")
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ServiceSenderTests(unittest.TestCase):
    def env(self, service="doctor"):
        prefix = m.SERVICES[service][0]
        return {
            prefix + "_ZULIP_EMAIL": m.SERVICE_EMAIL,
            prefix + "_ZULIP_API_KEY": "synthetic-test-only",
            prefix + "_ZULIP_USER_ID": str(m.SERVICE_USER_ID),
        }

    def fake(self, calls, mismatch=None, old=False, error=None):
        def request(method, path, fields=None):
            calls.append((method, path, fields))
            if path.endswith("/users/me"):
                d = dict(
                    result="success",
                    user_id=m.SERVICE_USER_ID,
                    email=m.SERVICE_EMAIL,
                    is_bot=True,
                )
                d.update(mismatch or {})
                return d
            if method == "GET":
                return {
                    "message": dict(
                        sender_id=999 if old else m.SERVICE_USER_ID,
                        type="stream",
                        display_recipient="Work-In-Progress",
                        subject="fleet tools doctor",
                    )
                }
            if error:
                raise error
            return dict(result="success", id=456)

        return request

    def test_missing_binding_never_requests(self):
        for service in m.SERVICES:
            calls = []
            with self.assertRaises(m.Refused):
                m.send(service, "body", env={}, request=self.fake(calls))
            self.assertEqual(calls, [])

    def test_bound_key_in_message_refused_before_request(self):
        calls = []
        with self.assertRaises(m.Refused):
            m.send(
                "doctor",
                "synthetic-test-only",
                env=self.env(),
                request=self.fake(calls),
            )
        self.assertEqual(calls, [])

    def test_agent_email_refused_even_with_service_prefix(self):
        env = self.env()
        env["FLEET_DOCTOR_ZULIP_EMAIL"] = "claude-bot@simplewithus.zulipchat.com"
        with self.assertRaises(m.Refused):
            m.send("doctor", "body", env=env)

    def test_nonagent_bot_not_in_positive_allowlist_refused(self):
        env = self.env()
        env["FLEET_DOCTOR_ZULIP_EMAIL"] = "unknown-bot@simplewithus.zulipchat.com"
        env["FLEET_DOCTOR_ZULIP_USER_ID"] = "123"
        with self.assertRaises(m.Refused):
            m.binding("doctor", env)

    def test_registry_missing_fails_closed(self):
        with patch.object(m.Path, "read_text", side_effect=OSError):
            with self.assertRaises(m.Refused):
                m.binding("doctor", self.env())

    def test_identity_mismatch_prevents_mutation(self):
        for mismatch in [
            {"user_id": 999},
            {"email": "other@simplewithus.zulipchat.com"},
            {"is_bot": False},
            {"result": "error"},
        ]:
            calls = []
            with self.assertRaises(m.Refused):
                m.send("doctor", "body", env=self.env(), request=self.fake(calls, mismatch))
            self.assertEqual([c[0] for c in calls], ["GET"])

    def test_all_audiences_fixed(self):
        for service, (_, channel, topic) in m.SERVICES.items():
            calls = []
            result = m.send(service, "body", env=self.env(service), request=self.fake(calls))
            self.assertEqual(result, {"id": 456})
            self.assertEqual(
                calls[-1],
                (
                    "POST",
                    "/api/v1/messages",
                    {
                        "type": "stream",
                        "to": channel,
                        "topic": topic,
                        "content": "body",
                    },
                ),
            )

    def test_edit_agent_message_refused(self):
        calls = []
        with self.assertRaises(m.Refused):
            m.send("doctor", "body", 17, env=self.env(), request=self.fake(calls, old=True))
        self.assertEqual([c[0] for c in calls], ["GET", "GET"])

    def test_edit_own_message(self):
        calls = []
        self.assertEqual(
            m.send("doctor", "body", 17, env=self.env(), request=self.fake(calls)),
            {"id": 17, "edited": True},
        )
        self.assertEqual(calls[-1][0], "PATCH")

    def test_uncertain_send_is_not_retried(self):
        calls = []
        with self.assertRaises(m.Unknown):
            m.send(
                "doctor",
                "body",
                env=self.env(),
                request=self.fake(calls, error=m.Unknown()),
            )
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])

    def test_redirect_never_follows_credentials(self):
        with self.assertRaises(m.Unknown):
            m.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")

    def test_unrelated_agent_env_not_used(self):
        env = self.env()
        env.update(AGENT_SEAT="CLAUDE", ZULIP_API_KEY="unused", IOS_TF_FRESHNESS_SEAT="CLAUDE")
        calls = []
        m.send("doctor", "body", env=env, request=self.fake(calls))
        self.assertEqual(len(calls), 2)


class CliTests(unittest.TestCase):
    def invoke(self, arguments):
        with (
            patch.object(sys, "argv", ["service_zulip.py"] + arguments),
            patch.object(m, "send", return_value={"id": 17}) as send,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = m.main()
        return result, send

    def test_edit_interleaved_id_preserves_body_and_message_id(self):
        body = "same check, changed detail\nsecond line"
        result, send = self.invoke(["--service", "doctor", "edit", "--id", "17", body])
        self.assertEqual(result, 0)
        send.assert_called_once_with("doctor", body, 17)

    def test_post_positional_body_is_unchanged(self):
        for service in m.SERVICES:
            result, send = self.invoke(["--service", service, "post", "alert body"])
            self.assertEqual(result, 0)
            send.assert_called_once_with(service, "alert body", None)

    def test_edit_body_file_after_id_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "body.txt"
            body_file.write_text("file body")
            result, send = self.invoke(
                [
                    "--service",
                    "doctor",
                    "edit",
                    "--id",
                    "17",
                    "--body-file",
                    str(body_file),
                ]
            )
        self.assertEqual(result, 0)
        send.assert_called_once_with("doctor", "file body", 17)

    def test_invalid_id_action_and_extra_argument_never_send(self):
        cases = [
            ["edit", "body"],
            ["edit", "--id", "0", "body"],
            ["post", "--id", "17", "body"],
            ["post", "body", "extra"],
            ["edit", "--id", "17", "--unknown", "body"],
        ]
        for arguments in cases:
            with (
                self.subTest(arguments=arguments),
                patch.object(sys, "argv", ["service_zulip.py", "--service", "doctor"] + arguments),
                patch.object(m, "send") as send,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as error:
                    m.main()
                self.assertEqual(error.exception.code, 2)
                send.assert_not_called()


class BindingFileTests(unittest.TestCase):
    def test_protected_file_load_and_unsafe_file_refusal(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(m.Path, "home", return_value=Path(tmp)),
        ):
            directory = Path(tmp) / ".config/fleet-services"
            directory.mkdir(parents=True, mode=0o700)
            p = directory / "doctor.json"
            p.write_text(
                json.dumps(
                    {
                        "email": m.SERVICE_EMAIL,
                        "api_key": "synthetic-test-only",
                        "user_id": m.SERVICE_USER_ID,
                    }
                )
            )
            p.chmod(0o600)
            self.assertEqual(m.binding("doctor", {})[0], m.SERVICE_EMAIL)
            p.chmod(0o644)
            with self.assertRaises(m.Refused):
                m.binding("doctor", {})
            p.chmod(0o600)
            data = p.read_text()
            p.unlink()
            target = directory / "other.json"
            target.write_text(data)
            target.chmod(0o600)
            p.symlink_to(target)
            with self.assertRaises(m.Refused):
                m.binding("doctor", {})

    def test_partial_injected_binding_never_reads_file(self):
        with patch.object(m.os, "open", side_effect=AssertionError("must not mix sources")):
            with self.assertRaises(m.Refused):
                m.binding("doctor", {"FLEET_DOCTOR_ZULIP_EMAIL": m.SERVICE_EMAIL})


class WiringTests(unittest.TestCase):
    def test_consumers_use_fixed_service_helper(self):
        root = Path(__file__).parent
        doctor = (root / "fleet-tools-doctor.py").read_text()
        reaper = (root / "sim-idle-reaper.py").read_text()
        tf = (root / "ios-fleet/ios_testflight_freshness.mjs").read_text()
        for source, service in [(doctor, "doctor"), (reaper, "sim-reaper")]:
            lists = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.List)]
            self.assertTrue(
                any(
                    any(
                        [getattr(value, "value", None) for value in node.elts[i : i + 3]]
                        == ["--service", service, "post"]
                        for i in range(len(node.elts) - 2)
                    )
                    for node in lists
                ),
                service,
            )
        self.assertIn('"--service", "testflight", "post"', tf)
        self.assertNotIn("IOS_TF_FRESHNESS_SEAT ||", tf)
        self.assertNotIn('[chat, "--as", "MA"', reaper)
        self.assertNotIn(
            "Environment=AGENT_SEAT=",
            (root / "systemd/fleet-tools-doctor.service").read_text(),
        )
        self.assertNotIn("--seats", (root / "systemd/fleet-tools-doctor.service").read_text())


if __name__ == "__main__":
    unittest.main()
