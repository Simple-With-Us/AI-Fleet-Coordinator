"""Native Zulip inbox delivery for an owner channel broadcast (AFC-938).

The owner's `@**everyone**` must land UNREAD in every fleet bot's own Zulip inbox and must
wake nobody;  the owner's `@**all**` must land there too, with its existing wake path
untouched.  Zulip only lets a message be unread for a subscriber, so every bot is subscribed
(quiet) to the channel first and then has the message's read flag taken off.

The delivery runs offline here:  the credential fetch and the HTTP transport are injected.
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import inbox_delivery as IDEL
from agent_sync import subscribe_all as SUB
from agent_sync import zulip as Z
from agent_sync.tests.test_chat_fanout import FakeCatalog
from agent_sync.tests.test_daemon import DaemonHarness

CHANNEL = "work-in-progress"
OWNER_EMAIL = "jay@zulip.test"


def creds(seat: str) -> Z.Credentials:
    return Z.Credentials(
        email="%s-bot@zulip.test" % seat.lower(),
        key="secret-" + seat,
        site="https://zulip.test",
        source="test",
    )


def long_creds(seat: str) -> Z.Credentials:
    """Credentials with a realistic-length key, so the masked preview shows eight and four."""
    return Z.Credentials(
        email="%s-bot@zulip.test" % seat.lower(),
        key="abcdefghijklmnopqrstuvwxyz0123456789",
        site="https://zulip.test",
        source="test",
    )


def message(
    mid: int = 4242,
    *,
    content: str = "@**everyone** FYI",
    channel: str = CHANNEL,
    sender_email: str = OWNER_EMAIL,
) -> dict[str, Any]:
    return {
        "id": mid,
        "type": "stream",
        "display_recipient": channel,
        "subject": "fleet",
        "content": content,
        "sender_id": 12,
        "sender_email": sender_email,
        "timestamp": 1_790_000_000,
        "client": "website",
        "flags": ["stream_wildcard_mentioned"],
    }


class FakeTransport:
    """One bot's transport:  it records the calls and answers the two the delivery needs."""

    def __init__(
        self,
        seat: str,
        *,
        streams: list[dict[str, Any]] | None = None,
        ignored: list[str] | None = None,
        fail_on: str | None = None,
        errors: dict[str, Exception] | None = None,
        answers: dict[str, Any] | None = None,
    ) -> None:
        self.seat = seat
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.streams = (
            streams
            if streams is not None
            else [{"name": CHANNEL, "stream_id": 7, "invite_only": False}]
        )
        self.ignored = ignored or []
        self.fail_on = fail_on
        self.errors = errors or {}
        self.answers = answers or {}

    def __call__(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, dict(payload or {})))
        if self.fail_on == path:
            raise RuntimeError("synthetic failure")
        if path in self.errors:
            raise self.errors[path]
        if path in self.answers:
            return self.answers[path]
        if method == "GET" and path == "streams":
            return {"streams": [dict(s) for s in self.streams]}
        if path == "messages/flags":
            return {
                "messages": [],
                "ignored_because_not_subscribed_channels": list(self.ignored),
            }
        return {"result": "success"}

    def paths(self) -> list[str]:
        return [path for _method, path, _payload in self.calls]

    def payloads(self, path: str) -> list[dict[str, Any]]:
        return [payload for _method, seen, payload in self.calls if seen == path]


def recorder(events: list[tuple[str, dict[str, Any]]]):
    def log(event: str, **fields: Any) -> None:
        events.append((event, fields))

    return log


class InboxDeliveryTests(unittest.TestCase):
    """The delivery itself:  subscribe, unflag, and keep one bot's failure off the others."""

    def deliver(self, msg, targets, transport_for, **kw) -> tuple[dict[str, Any], list]:
        events: list[tuple[str, dict[str, Any]]] = []
        result = IDEL.deliver(
            msg,
            targets=targets,
            http_factory=transport_for,
            log=recorder(events),
            **kw,
        )
        return result, events

    def test_every_bot_is_subscribed_and_the_message_is_left_unread(self) -> None:
        transport = FakeTransport("CLAUDE")
        result, _events = self.deliver(
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c: transport
        )
        self.assertEqual(result["delivered"], 1)
        self.assertIn("users/me/subscriptions", transport.paths())
        self.assertIn("users/me/subscriptions/properties", transport.paths())
        self.assertEqual(
            transport.payloads("messages/flags"),
            [{"messages": [4242], "op": "remove", "flag": "read"}],
            "the read flag comes off, so the message is unread for this bot",
        )
        properties = transport.payloads("users/me/subscriptions/properties")
        self.assertEqual(len(properties), 1)
        settings = {row["property"]: row["value"] for row in properties[0]["subscription_data"]}
        self.assertEqual(settings, SUB.QUIET, "the bot stays quiet")

    def test_one_bot_raising_does_not_stop_the_others(self) -> None:
        broken = FakeTransport("CLAUDE", fail_on="streams")
        fine = FakeTransport("CODEX")
        result, events = self.deliver(
            message(),
            [("CLAUDE", creds("CLAUDE")), ("CODEX", creds("CODEX"))],
            lambda c: broken if c.email.startswith("claude") else fine,
        )
        self.assertEqual((result["delivered"], result["failed"]), (1, 1))
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        self.assertEqual([f.get("seat") for f in failures], ["CLAUDE"])
        self.assertEqual(failures[0].get("error"), "RuntimeError")
        self.assertEqual(failures[0].get("id"), 4242)
        self.assertNotIn("secret-", str(failures[0]), "a failure never logs a key")
        self.assertIn("messages/flags", fine.paths(), "the other bot still got the message")

    def test_the_sender_and_the_owner_are_skipped(self) -> None:
        transport = FakeTransport("CLAUDE")
        result, events = self.deliver(
            message(sender_email="claude-bot@zulip.test"),
            [("CLAUDE", creds("CLAUDE"))],
            lambda _c: transport,
            skip_emails=["jay@zulip.test"],
        )
        self.assertEqual((result["delivered"], result["skipped"]), (0, 1))
        self.assertEqual(transport.paths(), [], "the sender's own bot is never targeted")
        reasons = [fields.get("reason") for event, fields in events if event == "inbox-delivery-skipped"]
        self.assertEqual(reasons, ["sender"])

    def test_the_owner_account_is_skipped(self) -> None:
        owner = Z.Credentials(
            email="jay@zulip.test", key="secret-owner", site="https://zulip.test", source="test"
        )
        transport = FakeTransport("JAY")
        result, events = self.deliver(
            message(sender_email="codex-bot@zulip.test"),
            [("JAY", owner)],
            lambda _c: transport,
            skip_emails=["jay@zulip.test"],
        )
        self.assertEqual((result["delivered"], result["skipped"]), (0, 1))
        self.assertEqual(transport.paths(), [])
        reasons = [fields.get("reason") for event, fields in events if event == "inbox-delivery-skipped"]
        self.assertEqual(reasons, ["owner"])

    def test_a_private_channel_is_skipped_and_never_joined(self) -> None:
        transport = FakeTransport(
            "CLAUDE",
            streams=[{"name": CHANNEL, "stream_id": 7, "invite_only": True}],
        )
        result, events = self.deliver(
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c: transport
        )
        self.assertEqual((result["delivered"], result["skipped"]), (0, 1))
        self.assertNotIn("users/me/subscriptions", transport.paths(), "a private channel is never joined")
        reasons = [fields.get("reason") for event, fields in events if event == "inbox-delivery-skipped"]
        self.assertEqual(reasons, ["private-or-unknown-channel"])

    def test_an_unknown_channel_is_skipped_and_never_created(self) -> None:
        transport = FakeTransport("CLAUDE", streams=[])
        result, _events = self.deliver(
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c: transport
        )
        self.assertEqual((result["delivered"], result["skipped"]), (0, 1))
        self.assertNotIn("users/me/subscriptions", transport.paths(), "no channel is created")

    def test_zulip_ignoring_the_message_fails_that_bot(self) -> None:
        transport = FakeTransport("CLAUDE", ignored=[CHANNEL])
        result, events = self.deliver(
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c: transport
        )
        self.assertEqual((result["delivered"], result["failed"]), (0, 1))
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        self.assertEqual([f.get("seat") for f in failures], ["CLAUDE"])
        self.assertEqual(failures[0].get("id"), 4242)


class OutcomeClassificationTests(unittest.TestCase):
    """Every failed bot is classified:  retryable, terminal or unknown."""

    def deliver(self, transport, *, key_creds=None, targets=None):
        events: list[tuple[str, dict[str, Any]]] = []
        result = IDEL.deliver(
            message(),
            targets=targets if targets is not None else [("CLAUDE", key_creds or creds("CLAUDE"))],
            http_factory=lambda _c: transport,
            log=recorder(events),
        )
        return result, events

    def outcome(self, transport, **kw) -> tuple[str, dict[str, Any]]:
        result, events = self.deliver(transport, **kw)
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        return result["outcomes"]["CLAUDE"], (failures[0] if failures else {})

    def test_a_rate_limit_is_retryable(self) -> None:
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("rate limited", status=429)}
        )
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "retryable")

    def test_a_refused_connection_before_the_send_is_retryable(self) -> None:
        transport = FakeTransport(
            "CLAUDE",
            errors={"streams": Z.NetworkError("connection refused", maybe_sent=False)},
        )
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "retryable")

    def test_a_forbidden_key_is_terminal(self) -> None:
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("forbidden", status=403)}
        )
        outcome, row = self.outcome(transport)
        self.assertEqual(outcome, "terminal")
        self.assertEqual(row.get("outcome"), "terminal")

    def test_an_unauthorized_key_is_terminal(self) -> None:
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("unauthorized", status=401)}
        )
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "terminal")

    def test_a_server_error_on_a_read_is_retryable(self) -> None:
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("bad gateway", status=502)}
        )
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "retryable")

    def test_a_server_error_on_a_write_is_unknown(self) -> None:
        transport = FakeTransport(
            "CLAUDE",
            errors={"messages/flags": Z.ApiError("bad gateway", status=502)},
        )
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "unknown")

    def test_a_timeout_on_a_write_is_unknown(self) -> None:
        transport = FakeTransport(
            "CLAUDE",
            errors={
                "messages/flags": Z.NetworkError(
                    "timed out", timeout=True, maybe_sent=True
                )
            },
        )
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "unknown")

    def test_a_ignored_message_is_terminal(self) -> None:
        transport = FakeTransport("CLAUDE", ignored=[CHANNEL])
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "terminal")

    def test_a_malformed_streams_answer_is_terminal_and_never_subscribes(self) -> None:
        transport = FakeTransport(
            "CLAUDE",
            answers={"streams": {"streams": [{"name": CHANNEL, "stream_id": "7", "invite_only": False}]}},
        )
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "terminal")
        self.assertNotIn("users/me/subscriptions", transport.paths())

    def test_a_streams_answer_that_is_not_a_list_is_terminal(self) -> None:
        transport = FakeTransport("CLAUDE", answers={"streams": {"streams": {}}})
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "terminal")
        self.assertNotIn("users/me/subscriptions", transport.paths())

    def test_a_malformed_flags_answer_is_terminal(self) -> None:
        transport = FakeTransport("CLAUDE", answers={"messages/flags": ["nope"]})
        outcome, _row = self.outcome(transport)
        self.assertEqual(outcome, "terminal")

    def test_the_result_carries_every_outcome_and_its_count(self) -> None:
        broken = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("forbidden", status=403)}
        )
        fine = FakeTransport("CODEX")
        events: list[tuple[str, dict[str, Any]]] = []
        result = IDEL.deliver(
            message(),
            targets=[("CLAUDE", creds("CLAUDE")), ("CODEX", creds("CODEX"))],
            http_factory=lambda c: broken if c.email.startswith("claude") else fine,
            log=recorder(events),
        )
        self.assertEqual(result["outcomes"], {"CLAUDE": "terminal", "CODEX": "delivered"})
        self.assertEqual(result["counts"]["terminal"], 1)
        self.assertEqual(result["counts"]["delivered"], 1)
        self.assertEqual(result["counts"]["skipped"], 0)

    def test_a_refused_key_is_logged_as_a_preview_and_never_in_full(self) -> None:
        key = "abcdefghijklmnopqrstuvwxyz0123456789"
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("unauthorized", status=401)}
        )
        outcome, row = self.outcome(transport, key_creds=long_creds("CLAUDE"))
        self.assertEqual(outcome, "terminal")
        self.assertEqual(row.get("key_preview"), "abcdefgh…6789")
        self.assertNotIn(key, json.dumps(row), "the full key never reaches the log")

    def test_a_short_key_is_never_logged_in_full(self) -> None:
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("forbidden", status=403)}
        )
        _outcome, row = self.outcome(transport)
        preview = str(row.get("key_preview"))
        self.assertNotIn("secret-CLAUDE", preview)
        self.assertNotIn("secret-CLAUDE", json.dumps(row))

    def test_the_streams_list_is_fetched_once_per_realm(self) -> None:
        first = FakeTransport("CLAUDE")
        second = FakeTransport("CODEX")
        events: list[tuple[str, dict[str, Any]]] = []
        result = IDEL.deliver(
            message(),
            targets=[("CLAUDE", creds("CLAUDE")), ("CODEX", creds("CODEX"))],
            http_factory=lambda c: first if c.email.startswith("claude") else second,
            log=recorder(events),
        )
        self.assertEqual(result["delivered"], 2)
        self.assertEqual(first.paths().count("streams"), 1)
        self.assertEqual(second.paths().count("streams"), 0, "the second bot reuses the list")

    def test_a_failed_streams_fetch_is_not_cached_for_the_next_bot(self) -> None:
        broken = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("unauthorized", status=401)}
        )
        fine = FakeTransport("CODEX")
        events: list[tuple[str, dict[str, Any]]] = []
        result = IDEL.deliver(
            message(),
            targets=[("CLAUDE", creds("CLAUDE")), ("CODEX", creds("CODEX"))],
            http_factory=lambda c: broken if c.email.startswith("claude") else fine,
            log=recorder(events),
        )
        self.assertEqual(result["outcomes"], {"CLAUDE": "terminal", "CODEX": "delivered"})
        self.assertEqual(fine.paths().count("streams"), 1, "the next bot fetches with its own key")


class InfisicalCredentialTests(unittest.TestCase):
    """The bot list and the credentials come from Infisical only:  no fallback, no pretend."""

    def test_no_credentials_at_all_is_an_explicit_failure(self) -> None:
        with mock.patch.object(Z, "infisical_seats", return_value=frozenset()):
            with self.assertRaises(IDEL.DeliveryError) as caught:
                IDEL.infisical_targets(env={}, home=Path("/nonexistent"))
        self.assertIn("Infisical", str(caught.exception))

    def test_one_seat_without_credentials_is_skipped_and_the_others_survive(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        with (
            mock.patch.object(Z, "infisical_seats", return_value=frozenset({"CLAUDE", "CODEX"})),
            mock.patch.object(Z, "prefetch_infisical"),
            mock.patch.object(
                Z,
                "infisical_credentials",
                side_effect=[creds("CLAUDE"), Z.CredentialError("not set")],
            ),
        ):
            targets = IDEL.infisical_targets(env={}, home=Path("/nonexistent"), log=recorder(events))
        self.assertEqual([seat for seat, _ in targets.targets], ["CLAUDE"])
        self.assertEqual(list(targets.skipped), ["CODEX"])
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        self.assertEqual([f.get("seat") for f in failures], ["CODEX"])

    def test_a_missing_credential_names_the_variable_and_never_a_value(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        message_text = (
            "Infisical Fleet-OPS /zulip (prod) keys CLAUDE_ZULIP_KEY: "
            "not set: CLAUDE_ZULIP_KEY"
        )
        with (
            mock.patch.object(Z, "infisical_seats", return_value=frozenset({"CLAUDE"})),
            mock.patch.object(Z, "prefetch_infisical"),
            mock.patch.object(
                Z, "infisical_credentials", side_effect=Z.CredentialError(message_text)
            ),
        ):
            with self.assertRaises(IDEL.DeliveryError):
                IDEL.infisical_targets(
                    env={}, home=Path("/nonexistent"), log=recorder(events)
                )
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        self.assertEqual(failures[0].get("missing"), "CLAUDE_ZULIP_KEY")
        self.assertNotIn("secret", json.dumps(failures), "a value never reaches the log")


class OwnerInboxDeliveryTests(DaemonHarness):
    """The daemon wires the delivery:  the owner's `@**everyone**` reaches every bot's inbox and
    wakes nobody;  the owner's `@**all**` reaches them too and keeps its one wake."""

    def setUp(self) -> None:
        super().setUp()
        self.transports: dict[str, FakeTransport] = {}
        self.targets = IDEL.Targets(
            targets=[("CLAUDE", creds("CLAUDE")), ("CODEX", creds("CODEX"))]
        )

    def transport_for(self, c: Z.Credentials) -> FakeTransport:
        seat = c.email.split("-bot@")[0].upper()
        return self.transports.setdefault(seat, FakeTransport(seat))

    def wait_for(self, predicate, seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def wakes_for(self, daemon, message_id: int) -> list[str]:
        return sorted(
            {
                str(w.get("wake_id"))
                for w in self.ledger()
                if message_id in (w.get("trigger_ids") or [])
            }
        )

    def log_rows(self, daemon) -> list[dict[str, Any]]:
        from agent_sync import live as L

        return L.read_jsonl(daemon.log.path)

    def delivery(self, daemon, message_id: int):
        """Pump until the message is routed and the delivery thread has run."""
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=self.targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            for _ in range(3):
                daemon.pump("CLAUDE", timeout=0.5)
            self.assertTrue(
                self.wait_for(lambda: len(self.transports) == len(self.targets.targets)),
                "the inbox delivery never ran",
            )
        return message_id

    def test_everyone_reaches_every_bot_and_wakes_nobody(self) -> None:
        daemon = self.started()
        message_id = self.fake.add_message(
            "Jay Wedgeworth", CHANNEL, "fleet", "@**everyone** FYI"
        )
        self.delivery(daemon, message_id)
        daemon.tick()
        for seat, transport in self.transports.items():
            self.assertIn("users/me/subscriptions", transport.paths(), seat)
            self.assertEqual(
                transport.payloads("messages/flags"),
                [{"messages": [message_id], "op": "remove", "flag": "read"}],
                seat,
            )
        self.assertEqual(self.wakes_for(daemon, message_id), [], "@everyone wakes nobody")

    def test_all_reaches_every_bot_and_keeps_its_one_wake(self) -> None:
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.fake.add_message(
            "Jay Wedgeworth", CHANNEL, "fleet", "@**all** deploy"
        )
        self.delivery(daemon, message_id)
        for _ in range(10):
            daemon.tick()
            if self.wakes_for(daemon, message_id):
                break
            time.sleep(0.02)
        self.assertEqual(len(self.wakes_for(daemon, message_id)), 1, "the @all wake is unchanged")
        for seat, transport in self.transports.items():
            self.assertIn("messages/flags", transport.paths(), seat)

    def test_one_message_is_delivered_once(self) -> None:
        daemon = self.started()
        msg = message()
        classes = _classes()
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=self.targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            daemon.start_inbox_delivery(msg, classes)
            daemon.start_inbox_delivery(msg, classes)
            self.assertTrue(
                self.wait_for(lambda: len(self.transports) == len(self.targets.targets)),
                "the inbox delivery never ran",
            )
            time.sleep(0.05)
        for seat, transport in self.transports.items():
            self.assertEqual(len(transport.payloads("messages/flags")), 1, seat)

    def test_a_peer_broadcast_reaches_no_inbox(self) -> None:
        """A peer bot's `@**everyone**` and `@**all**` are not the owner's:  no bot is addressed."""
        daemon = self.started()
        for content in ("@**everyone** FYI", "@**all** deploy"):
            with self.subTest(content=content):
                self.transports.clear()
                message_id = self.fake.add_message("Codex", CHANNEL, "fleet", content)
                with (
                    mock.patch.object(IDEL, "infisical_targets", return_value=self.targets),
                    mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
                ):
                    self.pump_until(daemon, lambda: self.routed(daemon, message_id))
                    time.sleep(0.1)
                self.assertEqual(self.transports, {}, "a peer broadcast addresses no bot")
                for transport in self.transports.values():
                    self.assertNotIn("messages/flags", transport.paths())

    def routed(self, daemon, message_id: int) -> bool:
        return any(
            row.get("event") == "route" and row.get("id") == message_id
            for row in self.log_rows(daemon)
        )

    def test_an_infisical_failure_is_logged_and_never_pretends(self) -> None:
        daemon = self.started()
        msg = message()
        with mock.patch.object(
            IDEL, "infisical_targets", side_effect=IDEL.DeliveryError("Infisical returned nothing")
        ):
            daemon._deliver_inbox(msg)
        rows = [r for r in self.log_rows(daemon) if r.get("event") == "inbox-delivery-failed"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("id"), msg["id"])
        self.assertIn("Infisical", str(rows[0].get("reason")))
        self.assertEqual(self.transports, {}, "no bot was addressed without credentials")

    def test_a_seat_without_credentials_is_named_and_skipped(self) -> None:
        daemon = self.started()
        msg = message()
        targets = IDEL.Targets(
            targets=[("CLAUDE", creds("CLAUDE"))],
            skipped={"CODEX": "CODEX_ZULIP_KEY"},
        )
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            daemon._deliver_inbox(msg)
        rows = [
            r
            for r in self.log_rows(daemon)
            if r.get("event") == "inbox-delivery-skipped-credentials"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("seat"), "CODEX")
        self.assertEqual(rows[0].get("missing"), "CODEX_ZULIP_KEY")
        self.assertNotIn("CODEX", self.transports, "the seat without credentials is skipped")

    def test_a_retryable_bot_releases_the_inbox_marker(self) -> None:
        daemon = self.started()
        msg = message()
        transport = FakeTransport(
            "CLAUDE",
            errors={
                "messages/flags": Z.NetworkError("timed out", timeout=True, maybe_sent=True)
            },
        )
        with (
            mock.patch.object(
                IDEL,
                "infisical_targets",
                return_value=IDEL.Targets(targets=[("CLAUDE", creds("CLAUDE"))]),
            ),
            mock.patch.object(IDEL, "zulip_http", return_value=transport),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            self.assertTrue(
                self.wait_for(
                    lambda: any(
                        row.get("event") == "inbox-delivery-retry-pending"
                        for row in self.log_rows(daemon)
                    )
                ),
                "the retry was never logged",
            )
        self.assertNotIn(4242, daemon._inbox_seen, "the marker is released so a later seat retries")
        rows = [
            r
            for r in self.log_rows(daemon)
            if r.get("event") == "inbox-delivery-retry-pending"
        ]
        self.assertEqual(rows[0].get("id"), 4242)
        self.assertEqual(rows[0].get("seats"), ["CLAUDE"])

    def test_a_terminal_bot_keeps_the_inbox_marker(self) -> None:
        daemon = self.started()
        msg = message()
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("forbidden", status=403)}
        )
        with (
            mock.patch.object(
                IDEL,
                "infisical_targets",
                return_value=IDEL.Targets(targets=[("CLAUDE", creds("CLAUDE"))]),
            ),
            mock.patch.object(IDEL, "zulip_http", return_value=transport),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            self.assertTrue(
                self.wait_for(
                    lambda: any(
                        row.get("event") == "inbox-delivery-failed"
                        for row in self.log_rows(daemon)
                    )
                ),
                "the failure was never logged",
            )
        self.assertIn(4242, daemon._inbox_seen, "a terminal failure is not retried")
        rows = [
            r for r in self.log_rows(daemon) if r.get("event") == "inbox-delivery-retry-pending"
        ]
        self.assertEqual(rows, [])

    def test_a_delivery_thread_that_cannot_start_leaves_no_id_behind(self) -> None:
        import threading

        daemon = self.started()
        real_start = threading.Thread.start

        def boom(thread, *args, **kwargs):
            if thread.name == "inbox-delivery":
                raise RuntimeError("can't start new thread")
            return real_start(thread, *args, **kwargs)

        with mock.patch.object(threading.Thread, "start", boom):
            daemon.start_inbox_delivery(message(), _classes())
        self.assertNotIn(4242, daemon._inbox_seen, "the id must not stay seen")


def _classes():
    """A classified owner `@**everyone**` message:  the gate the daemon uses."""
    from agent_sync import router as R

    c = R.Classes()
    c.owner, c.everyone_inert, c.eligible = True, True, True
    c.channel, c.topic, c.key = CHANNEL, "fleet", "work-in-progress:fleet"
    return c


if __name__ == "__main__":
    unittest.main()
