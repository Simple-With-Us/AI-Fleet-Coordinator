"""Native Zulip inbox delivery for an owner channel broadcast (AFC-938).

The owner's `@**everyone**` must land UNREAD in every fleet bot's own Zulip inbox and must
wake nobody;  the owner's `@**all**` must land there too, with its existing wake path
untouched.  Zulip only lets a message be unread for a subscriber, so every bot is subscribed
(quiet) to the channel first and then has the message's read flag taken off.

The delivery runs offline here:  the credential fetch and the HTTP transport are injected.
"""
from __future__ import annotations

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
    """One bot's transport:  it records the calls and answers the two the delivery needs."""

    def __init__(
        self,
        seat: str,
        *,
        streams: list[dict[str, Any]] | None = None,
        ignored: list[str] | None = None,
        fail_on: str | None = None,
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

    def __call__(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, dict(payload or {})))
        if self.fail_on == path:
            raise RuntimeError("synthetic failure")
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
    """The delivery itself:  subscribe, unflag, and keep one bot's failure off the others."""

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


class InfisicalCredentialTests(unittest.TestCase):
    """The bot list and the credentials come from Infisical only:  no fallback, no pretend."""

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
        self.assertEqual([seat for seat, _ in targets], ["CLAUDE"])
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        self.assertEqual([f.get("seat") for f in failures], ["CODEX"])


class OwnerInboxDeliveryTests(DaemonHarness):
    """The daemon wires the delivery:  the owner's `@**everyone**` reaches every bot's inbox and
    wakes nobody;  the owner's `@**all**` reaches them too and keeps its one wake."""

    def setUp(self) -> None:
        super().setUp()
        self.transports: dict[str, FakeTransport] = {}
        self.targets = [("CLAUDE", creds("CLAUDE")), ("CODEX", creds("CODEX"))]

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
                self.wait_for(lambda: len(self.transports) == len(self.targets)),
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
                self.wait_for(lambda: len(self.transports) == len(self.targets)),
                "the inbox delivery never ran",
            )
            time.sleep(0.05)
        for seat, transport in self.transports.items():
            self.assertEqual(len(transport.payloads("messages/flags")), 1, seat)

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
    """A classified owner `@**everyone**` message:  the gate the daemon uses."""
    from agent_sync import router as R

    c = R.Classes()
    c.owner, c.everyone_inert, c.eligible = True, True, True
    c.channel, c.topic, c.key = CHANNEL, "fleet", "work-in-progress:fleet"
    return c


if __name__ == "__main__":
    unittest.main()
