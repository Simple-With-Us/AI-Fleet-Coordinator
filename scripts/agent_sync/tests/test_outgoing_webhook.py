from __future__ import annotations

import json
import secrets
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_sync import config as C
from agent_sync import outgoing_webhook as OW
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.listener_harness import ListenerHarness


class OutgoingWebhookReceiverTests(ListenerHarness):
    def _post(self, url: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read().decode("utf-8"))

    def test_auth_payload_and_response(self):
        token = secrets.token_hex(24)
        accepted: list[tuple[str, str, int]] = []
        logs: list[tuple[str, dict[str, Any]]] = []
        receiver = OW.OutgoingWebhookServer("127.0.0.1", 0, {"CODEX": token},
                                            lambda *args: accepted.append(args) or "accepted",
                                            lambda event, **fields: logs.append((event, fields)))
        receiver.start()
        self.addCleanup(receiver.close)
        url = "http://%s:%d/zulip/outgoing/CODEX" % receiver.address
        payload = {"token": token, "bot_email": "codex-bot@zulip.test", "trigger": "mention",
                   "message": {"id": 123, "content": "untrusted and ignored"}}

        status, result = self._post(url, payload)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"response_not_required": True})
        self.assertEqual(accepted, [("CODEX", "codex-bot@zulip.test", 123)])
        self.assertNotIn(token, repr(logs))

        bad = {**payload, "token": secrets.token_hex(24)}
        status, _ = self._post(url, bad)
        self.assertEqual(status, 401)
        status, _ = self._post(url.replace("CODEX", "NOPE"), payload)
        self.assertEqual(status, 404)
        malformed = {**payload, "message": {"id": True}}
        status, _ = self._post(url, malformed)
        self.assertEqual(status, 400)
        self.assertEqual(len(accepted), 1)

    def test_unavailable_identity_is_not_acknowledged(self):
        token = secrets.token_hex(24)
        receiver = OW.OutgoingWebhookServer("127.0.0.1", 0, {"CLAUDE": token},
                                            lambda *_args: "unavailable")
        receiver.start()
        self.addCleanup(receiver.close)
        url = "http://%s:%d/zulip/outgoing/CLAUDE" % receiver.address
        payload = {"token": token, "bot_email": BOT_EMAIL, "trigger": "private_message",
                   "message": {"id": 9}}
        status, _ = self._post(url, payload)
        self.assertEqual(status, 503)

    def test_enabled_receiver_binds_with_no_tokens_and_denies_all_routes(self):
        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        daemon = self.daemon()
        daemon.config.outgoing_webhook_enabled = True
        daemon.config.outgoing_webhook_bind = "127.0.0.1"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            daemon.config.outgoing_webhook_port = probe.getsockname()[1]

        daemon._configure_outgoing_webhook()
        self.assertIsNotNone(daemon.outgoing_webhook_server)
        url = "http://%s:%d/zulip/outgoing/CLAUDE" % daemon.outgoing_webhook_server.address
        payload = {"token": "unconfigured", "bot_email": BOT_EMAIL, "trigger": "mention",
                   "message": {"id": 10}}
        status, _ = self._post(url, payload)
        self.assertEqual(status, 404)

    def test_spool_survives_recreation_and_deduplicates_pending_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            first = OW.DurableSpool(directory)
            self.assertTrue(first.add("BF-BUILDER", 456))
            self.assertFalse(first.add("BF-BUILDER", 456))
            raw = next(Path(directory).glob("*.json")).read_text()
            self.assertNotIn("token", raw)

            restarted = OW.DurableSpool(directory)
            self.assertEqual(restarted.pending(), [{"seat": "BF-BUILDER", "message_id": 456}])
            restarted.remove("BF-BUILDER", 456)
            self.assertEqual(restarted.pending(), [])

    def test_redelivery_repairs_torn_final_file_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = OW.DurableSpool(directory)
            torn = Path(directory) / "CLAUDE-789.json"
            torn.write_bytes(b'{"seat":"CLAUDE",')

            self.assertTrue(spool.add("CLAUDE", 789))
            restarted = OW.DurableSpool(directory)
            self.assertEqual(restarted.pending(), [{"seat": "CLAUDE", "message_id": 789}])
            self.assertFalse(restarted.add("CLAUDE", 789))

    def test_duplicate_webhook_deliveries_are_acknowledged_without_duplicate_spool_rows(self):
        token = secrets.token_hex(24)
        with tempfile.TemporaryDirectory() as directory:
            spool = OW.DurableSpool(directory)

            def accept(seat: str, _email: str, message_id: int) -> str:
                return "accepted" if spool.add(seat, message_id) else "duplicate"

            receiver = OW.OutgoingWebhookServer("127.0.0.1", 0, {"CLAUDE": token}, accept)
            receiver.start()
            self.addCleanup(receiver.close)
            url = "http://%s:%d/zulip/outgoing/CLAUDE" % receiver.address
            payload = {"token": token, "bot_email": BOT_EMAIL, "trigger": "mention",
                       "message": {"id": 321}}
            self.assertEqual(self._post(url, payload), (200, {"response_not_required": True}))
            self.assertEqual(self._post(url, payload), (200, {"response_not_required": True}))
            self.assertEqual(spool.pending(), [{"seat": "CLAUDE", "message_id": 321}])

    def test_config_ingress_disabled_by_default_and_seat_allowlist_parses(self):
        cfg = C.from_dict({"daemon": {"outgoing_webhook_seats": ["codex"],
                                       "owner_user_id": 12},
                           "seat": {"CODEX": {"bot": "Codex", "wake": "inbox"}}})
        self.assertFalse(cfg.outgoing_webhook_enabled)
        self.assertEqual(cfg.outgoing_webhook_bind, "127.0.0.1")
        self.assertEqual(cfg.outgoing_webhook_seats, frozenset({"CODEX"}))
        self.assertFalse(cfg.errors)

    def test_authoritative_rest_message_converges_with_native_event_once(self):
        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        runner = daemon.seats["CLAUDE"]
        msg_id = self.fake.add_message("Jay Wedgeworth", "agent-sync", "AFC-309 webhook test", "wake Codex",
                                       deliver=False)
        original_cursor = runner.cursor

        self.assertEqual(daemon._accept_outgoing_webhook("CLAUDE", BOT_EMAIL, msg_id), "accepted")
        pending_path = Path(daemon.webhook_spool.directory) / ("CLAUDE-%d.json" % msg_id)
        self.assertTrue(pending_path.exists())
        daemon._drain_outgoing_webhook_spool()
        self.assertFalse(pending_path.exists())
        self.assertEqual(runner.cursor, original_cursor, "webhook must not advance native event cursor")
        self.assertEqual([row["id"] for row in self.inbox("CLAUDE")].count(msg_id), 1)

        actual = self.fake._find(msg_id)
        daemon.handle_events(runner, [{"type": "message", "message": actual}])
        self.assertEqual(runner.cursor, msg_id)
        self.assertEqual([row["id"] for row in self.inbox("CLAUDE")].count(msg_id), 1)
        self.assertEqual(daemon._accept_outgoing_webhook("CLAUDE", BOT_EMAIL, msg_id), "duplicate")
        self.assertEqual(daemon.webhook_spool.pending(), [])

    def test_spool_remove_io_error_is_deferred_and_native_event_still_routes(self):
        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        runner = daemon.seats["CLAUDE"]
        msg_id = self.fake.add_message("Jay Wedgeworth", "agent-sync", "AFC-309 spool retry",
                                       "wake Codex", deliver=False)
        self.assertEqual(daemon._accept_outgoing_webhook("CLAUDE", BOT_EMAIL, msg_id), "accepted")
        original_remove = daemon.webhook_spool.remove

        def fail_remove(_seat: str, _message_id: int) -> None:
            raise OSError("simulated fsync failure")

        daemon.webhook_spool.remove = fail_remove
        daemon._drain_outgoing_webhook_spool(limit=1)
        self.assertEqual(daemon.webhook_spool.pending(), [{"seat": "CLAUDE", "message_id": msg_id}])
        self.assertGreater(daemon.webhook_retry_at[("CLAUDE", msg_id)], daemon.clock.monotonic())
        daemon.webhook_spool.remove = original_remove

        actual = self.fake._find(msg_id)
        daemon.handle_events(runner, [{"type": "message", "message": actual}])
        self.assertEqual(runner.cursor, msg_id)

    def test_disconnected_and_backoff_heads_do_not_starve_ready_spool_rows(self):
        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        runner = daemon.seats["CLAUDE"]
        first_id = self.fake.add_message("Jay Wedgeworth", "agent-sync", "AFC-309 retry head",
                                         "older deferred event", deliver=False)
        later_id = self.fake.add_message("Jay Wedgeworth", "agent-sync", "AFC-309 ready tail",
                                         "ready event", deliver=False)
        daemon.webhook_spool.add("GONE", 1)
        daemon.webhook_spool.add("CLAUDE", first_id)
        daemon.webhook_spool.add("CLAUDE", later_id)
        daemon.webhook_retry_at[("CLAUDE", first_id)] = daemon.clock.monotonic() + 5.0

        daemon._drain_outgoing_webhook_spool(limit=1)

        self.assertFalse((Path(daemon.webhook_spool.directory) / ("CLAUDE-%d.json" % later_id)).exists())
        self.assertTrue((Path(daemon.webhook_spool.directory) / ("CLAUDE-%d.json" % first_id)).exists())
        self.assertTrue(any(row["id"] == later_id for row in self.inbox("CLAUDE")))

    def test_webhook_wrong_bot_email_is_rejected_before_spooling(self):
        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        msg_id = self.fake.add_message("Jay Wedgeworth", "agent-sync", "AFC-309 spoof", "ignore this body",
                                       deliver=False)
        self.assertEqual(daemon._accept_outgoing_webhook("CLAUDE", "other@zulip.test", msg_id), "rejected")
        self.assertEqual(daemon.webhook_spool.pending(), [])

    def test_outgoing_webhook_bot_type_requires_explicit_seat_allowlist(self):
        self.fake.me["bot_type"] = 3
        self.write_config()
        denied = self.daemon()
        self.assertFalse(denied.connect_seat("CLAUDE"))

        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        allowed = self.daemon()
        self.assertTrue(allowed.connect_seat("CLAUDE"))

    def test_a_marker_message_through_the_outgoing_webhook_spool_is_suppressed(self):
        # The spool stores authenticated metadata only:  the daemon re-fetches the authoritative
        # message and routes it, so the router's no-wake gate covers this path too (AFC-468).
        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        msg_id = self.fake.add_message(
            "Jay Wedgeworth", "agent-sync", "AFC-309 no-wake", ":eyes: @**Codex** wake up",
            deliver=False,
        )
        self.assertEqual(daemon._accept_outgoing_webhook("CLAUDE", BOT_EMAIL, msg_id), "accepted")
        daemon._drain_outgoing_webhook_spool()
        self.assertEqual(
            [row["id"] for row in self.inbox("CLAUDE") if row["id"] == msg_id],
            [],
            "the marker message never reaches the seat inbox",
        )

    def test_an_unmarked_message_through_the_outgoing_webhook_spool_still_routes(self):
        # The control:  the same path without the marker does deliver, so the test above is
        # measuring the marker and not a broken spool.
        self.write_config(extra_daemon='outgoing_webhook_seats = ["CLAUDE"]')
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        msg_id = self.fake.add_message(
            "Jay Wedgeworth", "agent-sync", "AFC-309 control", "@**Codex** wake up",
            deliver=False,
        )
        self.assertEqual(daemon._accept_outgoing_webhook("CLAUDE", BOT_EMAIL, msg_id), "accepted")
        daemon._drain_outgoing_webhook_spool()
        self.assertEqual(
            [row["id"] for row in self.inbox("CLAUDE") if row["id"] == msg_id],
            [msg_id],
            "the unmarked message reaches the seat inbox",
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
