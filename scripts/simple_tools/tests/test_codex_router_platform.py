"""Server wrapper tests for the Codex wake router platform."""

import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from simple_tools import claude_wake as CLW
from simple_tools import codex_wake as CW
from simple_tools import platform_letter_codex as PLC
from simple_tools import wake_router as WR
from simple_tools import wake_service as SVC

SID = "11111111-1111-4111-8111-111111111111"


class CodexRouterPlatformTests(unittest.TestCase):
    def setUp(self):
        self.env = {"HOME": "/unused"}
        self.settings = {
            "delivery_enabled": True,
            "mac_ssh": "mac.example.test",
            "resume_timeout_seconds": CW.DEFAULTS["resume_timeout_seconds"],
        }
        self.target = WR.Target(
            SID, letter="BH", host="mac", title="ignored", generation=4
        )
        self.wake = WR.Wake(
            wake_id="wake-1",
            seat="CODEX",
            message_id=42,
            channel="Work-In-Progress",
            topic="AFC-694 Codex wake",
            zulip_link="https://zulip.example/42",
            sender="Jay",
            letter="BH",
            picked="most-recent",
            reason="most-recent",
            link_note=WR.LINK_NOTES["most-recent"],
        )

    def test_platform_metadata_and_defaults_match_shared_router(self):
        with patch.object(CLW, "CODEX_SESSION_RE", None, create=True):
            platform = CW.router_platform(self.env, self.settings)

        self.assertEqual(platform.seat, "CODEX")
        self.assertEqual(platform.platform, "codex")
        self.assertEqual(platform.mention, "Codex")
        self.assertIs(platform.target_re, CW.SESSION_RE)
        self.assertTrue(platform.target_re.fullmatch(SID))
        self.assertFalse(platform.target_re.fullmatch("not-a-uuid"))
        self.assertTrue(set(WR.ROUTER_DEFAULTS).issubset(CW.DEFAULTS))
        self.assertEqual(CW.DEFAULTS["mac_ssh"], "")
        self.assertEqual(
            CW.DEFAULTS["resume_timeout_seconds"],
            CLW.DEFAULTS["resume_timeout_seconds"],
        )

    def test_non_mac_target_is_refused_without_using_any_local_transport(self):
        target = WR.Target(SID, letter="BH", host="server", generation=4)

        with (
            patch.object(CLW, "deliver_remote") as remote,
            patch.object(
                CW.CT,
                "CodexChatTransport",
                side_effect=AssertionError("native transport used"),
            ),
            patch.object(PLC, "thread_row", side_effect=AssertionError("local store used")),
        ):
            platform = CW.router_platform(self.env, self.settings)
            result = platform.deliver(target, self.wake)

        self.assertEqual(result, WR.Delivery("refused", code="unknown-host"))
        remote.assert_not_called()

    def test_shared_service_prepares_codex_without_native_or_settings_reads(self):
        with (
            patch.object(CW.CT, "CodexChatTransport", side_effect=AssertionError("native used")),
            patch.object(PLC, "thread_row", side_effect=AssertionError("local store used")),
            patch.object(SVC.WS.knobs, "resolve", side_effect=AssertionError("settings read")),
        ):
            prepared = SVC.prepare(
                "CODEX", SVC.BUILDERS["CODEX"], self.env, importer=lambda name: CW
            )

        self.assertIs(prepared.make, CW.router_platform)
        self.assertEqual(prepared.builder.session_re, CW.SESSION_RE.pattern)
        self.assertIsInstance(prepared.never_ran, frozenset)
        self.assertIn("invalid-target", prepared.never_ran)
        self.assertIn("target-released", prepared.never_ran)
        self.assertTrue(
            {"queued", "duplicate", "observed", "rejected", "held", "delivery-unknown"}
            .isdisjoint(prepared.never_ran)
        )

    def test_mac_target_uses_shared_remote_delivery_and_preserves_results(self):
        # Include terminal, uncertain, and retryable cases with routing metadata.
        deliveries = (
            WR.Delivery("sent", "ssh", ack="confirmed", code="observed", note="ack"),
            WR.Delivery("sent", "ssh", ack="unknown", code="accepted"),
            WR.Delivery("busy", "ssh", code="ssh-preflight-timeout"),
            WR.Delivery("failed", "ssh", code="remote-needs-update"),
            WR.Delivery("refused", "ssh", code="remote-exit-126"),
        )

        for delivery in deliveries:
            with self.subTest(delivery=delivery):
                with (
                    patch.object(CLW, "deliver_remote", return_value=delivery) as remote,
                    patch.object(
                        CW.CT,
                        "CodexChatTransport",
                        side_effect=AssertionError("native transport used"),
                    ),
                    patch.object(PLC, "thread_row", side_effect=AssertionError("local store used")),
                ):
                    platform = CW.router_platform(self.env, self.settings)
                    result = platform.deliver(self.target, self.wake)

                remote.assert_called_once_with(
                    self.env, self.settings, self.target, self.wake, platform="codex"
                )
                self.assertIs(result, delivery)

    def _deliver_through_original_remote(self, generation):
        original = CLW.deliver_remote
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if len(calls) == 1:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            answer = {
                "wake_id": self.wake.wake_id,
                "session_id": self.target.session_id,
                "generation": generation,
                "delivery": {
                    "outcome": "sent",
                    "route": "codex-desktop",
                    "ack": "confirmed",
                    "code": "observed",
                    "note": "",
                },
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(answer), stderr="")

        def delegate(env, settings, target, wake, *, platform):
            return original(env, settings, target, wake, platform=platform, runner=runner)

        return patch.object(CLW, "deliver_remote", side_effect=delegate), calls

    def test_shared_transport_sends_correlated_codex_envelope(self):
        remote, calls = self._deliver_through_original_remote(self.target.generation)

        with (
            remote,
            patch.object(
                CW.CT,
                "CodexChatTransport",
                side_effect=AssertionError("native transport used"),
            ),
            patch.object(PLC, "thread_row", side_effect=AssertionError("local store used")),
        ):
            platform = CW.router_platform(self.env, self.settings)
            result = platform.deliver(self.target, self.wake)

        self.assertEqual(
            result,
            WR.Delivery("sent", "ssh:codex-desktop", ack="confirmed", code="observed"),
        )
        self.assertEqual(len(calls), 2)
        request = json.loads(calls[1][1]["input"])
        self.assertEqual(request["platform"], "codex")
        self.assertEqual(
            request["target"],
            {
                "session_id": SID,
                "letter": "BH",
                "host": "mac",
                "generation": 4,
            },
        )
        self.assertEqual(
            request["wake"], json.loads(json.dumps(asdict(self.wake)))
        )
        self.assertEqual(
            {key: request["wake"][key] for key in ("picked", "reason", "link_note")},
            {key: getattr(self.wake, key) for key in ("picked", "reason", "link_note")},
        )

    def test_shared_transport_rejects_string_generation_as_remote_mismatch(self):
        remote, calls = self._deliver_through_original_remote(str(self.target.generation))

        with (
            remote,
            patch.object(
                CW.CT,
                "CodexChatTransport",
                side_effect=AssertionError("native transport used"),
            ),
            patch.object(PLC, "thread_row", side_effect=AssertionError("local store used")),
        ):
            platform = CW.router_platform(self.env, self.settings)
            result = platform.deliver(self.target, self.wake)

        self.assertEqual(result, WR.Delivery("refused", "ssh", code="remote-mismatch"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
