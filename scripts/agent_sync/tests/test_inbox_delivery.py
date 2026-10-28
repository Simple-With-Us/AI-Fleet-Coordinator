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


def creds(seat: str, *, site: str = "https://zulip.test") -> Z.Credentials:
    return Z.Credentials(
        email="%s-bot@zulip.test" % seat.lower(),
        key="secret-" + seat,
        site=site,
        source="test",
    )


def long_creds(seat: str, *, site: str = "https://zulip.test") -> Z.Credentials:
    """Credentials with a realistic-length key, so the masked preview shows eight and four."""
    return Z.Credentials(
        email="%s-bot@zulip.test" % seat.lower(),
        key="abcdefghijklmnopqrstuvwxyz0123456789",
        site=site,
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
        flaky: dict[str, list[Exception]] | None = None,
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
        # path -> the exceptions to raise, one per call:  the first call fails, the next works.
        self.flaky = {path: list(excs) for path, excs in (flaky or {}).items()}

    def __call__(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, dict(payload or {})))
        if self.fail_on == path:
            raise RuntimeError("synthetic failure")
        if path in self.errors:
            raise self.errors[path]
        if self.flaky.get(path):
            raise self.flaky[path].pop(0)
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
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c, _realm: transport
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

    def test_unread_is_reapplied_and_streams_not_refetched_when_only_unread_is_missing(
        self,
    ) -> None:
        transport = FakeTransport("CLAUDE")
        done: dict[str, set[str]] = {"CLAUDE": set()}
        for _ in range(2):
            result, _events = self.deliver(
                message(),
                [("CLAUDE", creds("CLAUDE"))],
                lambda _c, _realm: transport,
                done=done,
            )
            self.assertEqual(result["outcomes"]["CLAUDE"], "delivered")
        self.assertEqual(len(transport.payloads("messages/flags")), 2)
        self.assertEqual(transport.paths().count("users/me/subscriptions"), 1)
        self.assertEqual(transport.paths().count("streams"), 1)

    def test_only_unread_missing_does_not_fetch_streams(self) -> None:
        transport = FakeTransport("CLAUDE")
        done: dict[str, set[str]] = {"CLAUDE": {"subscribe", "quiet"}}
        result, _events = self.deliver(
            message(),
            [("CLAUDE", creds("CLAUDE"))],
            lambda _c, _realm: transport,
            done=done,
        )
        self.assertEqual(result["outcomes"]["CLAUDE"], "delivered")
        self.assertEqual(transport.paths().count("streams"), 0)
        self.assertEqual(len(transport.payloads("messages/flags")), 1)

    def test_one_bot_raising_does_not_stop_the_others(self) -> None:
        broken = FakeTransport("CLAUDE", fail_on="streams")
        fine = FakeTransport("CODEX")
        result, events = self.deliver(
            message(),
            [("CLAUDE", creds("CLAUDE")), ("CODEX", creds("CODEX"))],
            lambda c, _realm: broken if c.email.startswith("claude") else fine,
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
            lambda _c, _realm: transport,
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
            lambda _c, _realm: transport,
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
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c, _realm: transport
        )
        self.assertEqual((result["delivered"], result["skipped"]), (0, 1))
        self.assertNotIn("users/me/subscriptions", transport.paths(), "a private channel is never joined")
        reasons = [fields.get("reason") for event, fields in events if event == "inbox-delivery-skipped"]
        self.assertEqual(reasons, ["private-or-unknown-channel"])

    def test_an_unknown_channel_is_skipped_and_never_created(self) -> None:
        transport = FakeTransport("CLAUDE", streams=[])
        result, _events = self.deliver(
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c, _realm: transport
        )
        self.assertEqual((result["delivered"], result["skipped"]), (0, 1))
        self.assertNotIn("users/me/subscriptions", transport.paths(), "no channel is created")

    def test_zulip_ignoring_the_message_fails_that_bot(self) -> None:
        transport = FakeTransport("CLAUDE", ignored=[CHANNEL])
        result, events = self.deliver(
            message(), [("CLAUDE", creds("CLAUDE"))], lambda _c, _realm: transport
        )
        self.assertEqual((result["delivered"], result["failed"]), (0, 1))
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        self.assertEqual([f.get("seat") for f in failures], ["CLAUDE"])
        self.assertEqual(failures[0].get("id"), 4242)

    def test_a_bot_on_another_realm_is_skipped(self) -> None:
        """A key is never sent to a server it does not belong to."""
        transport = FakeTransport("CLAUDE")
        result, events = self.deliver(
            message(),
            [("CLAUDE", creds("CLAUDE"))],
            lambda _c, _realm: transport,
            realm="https://other-realm.test",
        )
        self.assertEqual((result["delivered"], result["skipped"]), (0, 1))
        self.assertEqual(result["outcomes"]["CLAUDE"], "skipped")
        self.assertEqual(transport.paths(), [], "the bot is not addressed on another realm")
        reasons = [
            fields.get("reason") for event, fields in events if event == "inbox-delivery-skipped"
        ]
        self.assertEqual(reasons, ["other-realm"])
        self.assertNotIn(
            "claude-bot@zulip.test", json.dumps(events), "the log never names the bot's email"
        )

    def test_the_transport_addresses_the_source_realm(self) -> None:
        http = IDEL.zulip_http(creds("CLAUDE"), "https://zulip.test")
        self.assertEqual(http.client.base, "https://zulip.test/api/v1/")
        other = IDEL.zulip_http(
            creds("CLAUDE", site="https://other-realm.test"), "https://zulip.test"
        )
        self.assertEqual(
            other.client.base,
            "https://zulip.test/api/v1/",
            "the transport addresses the source realm, not the credential's own site",
        )


class RealmMismatchMessageTests(unittest.TestCase):
    def test_realm_mismatch_names_only_hosts(self) -> None:
        secret_creds = Z.Credentials(
            email="claude-bot@zulip.test",
            key="k",
            site="https://other-realm.test",
            source="CLAUDE_ZULIP_KEY",
        )
        with self.assertRaises(Z.RealmMismatch) as caught:
            Z.verify_realm(secret_creds, "https://zulip.test")
        text = str(caught.exception)
        self.assertIn("other-realm.test", text)
        self.assertIn("zulip.test", text)
        self.assertNotIn("claude-bot@zulip.test", text)
        self.assertNotIn("CLAUDE_ZULIP_KEY", text)


class OutcomeClassificationTests(unittest.TestCase):
    """Every failed bot is classified:  retryable, terminal or unknown."""

    def deliver(self, transport, *, key_creds=None, targets=None):
        events: list[tuple[str, dict[str, Any]]] = []
        result = IDEL.deliver(
            message(),
            targets=targets if targets is not None else [("CLAUDE", key_creds or creds("CLAUDE"))],
            http_factory=lambda _c, _realm: transport,
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
            http_factory=lambda c, _realm: broken if c.email.startswith("claude") else fine,
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
            http_factory=lambda c, _realm: first if c.email.startswith("claude") else second,
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
            http_factory=lambda c, _realm: broken if c.email.startswith("claude") else fine,
            log=recorder(events),
        )
        self.assertEqual(result["outcomes"], {"CLAUDE": "terminal", "CODEX": "delivered"})
        self.assertEqual(fine.paths().count("streams"), 1, "the next bot fetches with its own key")


class InfisicalCredentialTests(unittest.TestCase):
    """The bot list and the credentials come from Infisical only:  no fallback, no pretend."""

    def test_no_credentials_at_all_is_an_explicit_failure(self) -> None:
        with mock.patch.object(Z, "infisical_roster", return_value={}):
            with self.assertRaises(IDEL.DeliveryError) as caught:
                IDEL.infisical_targets(env={}, home=Path("/nonexistent"))
        self.assertIn("Infisical", str(caught.exception))

    def test_one_seat_without_credentials_is_skipped_and_the_others_survive(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        with (
            mock.patch.object(
                Z, "infisical_roster", return_value={"CLAUDE": (), "CODEX": ()}
            ),
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
            mock.patch.object(Z, "infisical_roster", return_value={"CLAUDE": ()}),
            mock.patch.object(Z, "prefetch_infisical"),
            mock.patch.object(
                Z, "infisical_credentials", side_effect=Z.CredentialError(message_text)
            ),
        ):
            targets = IDEL.infisical_targets(
                env={}, home=Path("/nonexistent"), log=recorder(events)
            )
        self.assertEqual(targets.skipped, {"CLAUDE": "CLAUDE_ZULIP_KEY"})
        self.assertEqual(targets.targets, [], "a seat without a key is never addressed")
        failures = [fields for event, fields in events if event == "inbox-delivery-failed"]
        self.assertEqual(failures[0].get("missing"), "CLAUDE_ZULIP_KEY")
        self.assertNotIn("secret", json.dumps(failures), "a value never reaches the log")

    def test_a_seat_with_some_of_its_names_is_named_and_skipped(self) -> None:
        """A seat with an email and a site but no key name never reaches `infisical_seats`.

        The roster is the wider enumeration, so the seat is named instead of vanishing."""
        with (
            mock.patch.object(
                Z,
                "infisical_roster",
                return_value={"CLAUDE": (), "CODEX": ("CODEX_ZULIP_KEY",)},
            ),
            mock.patch.object(Z, "prefetch_infisical"),
            mock.patch.object(Z, "infisical_credentials", return_value=creds("CLAUDE")),
        ):
            targets = IDEL.infisical_targets(env={}, home=Path("/nonexistent"))
        self.assertEqual(targets.skipped, {"CODEX": "CODEX_ZULIP_KEY"})
        self.assertEqual([seat for seat, _ in targets.targets], ["CLAUDE"])


class InfisicalRosterBoundaryTests(unittest.TestCase):
    """The real enumeration boundary:  only the Infisical reader is stubbed.

    `Z.infisical_roster` runs for real here, so the seat that has only two of its three
    names is found by the same code the daemon uses."""

    NAMES = (
        "GOOD_ZULIP_EMAIL",
        "GOOD_ZULIP_KEY",
        "GOOD_ZULIP_SITE",
        "MISSING_ZULIP_EMAIL",
        "MISSING_ZULIP_SITE",
    )
    VALUES = {
        "GOOD_ZULIP_EMAIL": "good-bot@zulip.test",
        "GOOD_ZULIP_KEY": "good-key-value-0123456789",
        "GOOD_ZULIP_SITE": "https://zulip.test",
        "MISSING_ZULIP_EMAIL": "missing-bot@zulip.test",
        "MISSING_ZULIP_SITE": "https://zulip.test",
    }

    def stub(self):
        def names(_env, _home):
            return list(self.NAMES)

        def fetch(wanted, _env, _home):
            return {name: self.VALUES.get(name, "") for name in wanted}

        return (
            mock.patch.object(Z, "_default_infisical_names", names),
            mock.patch.object(Z, "_default_infisical_fetch", fetch),
        )

    def test_the_roster_names_a_seat_whose_key_name_is_absent(self) -> None:
        with self.stub()[0], self.stub()[1]:
            roster = Z.infisical_roster(env={}, home=Path("/nonexistent"))
        self.assertEqual(roster.get("GOOD"), ())
        self.assertEqual(roster.get("MISSING"), ("MISSING_ZULIP_KEY",))

    def test_a_seat_without_a_key_is_delivered_as_missing_credentials(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        names, fetch = self.stub()
        with names, fetch:
            targets = IDEL.infisical_targets(env={}, home=Path("/nonexistent"))
            result = IDEL.deliver(
                message(),
                targets=targets,
                http_factory=lambda _c, _realm: FakeTransport("GOOD"),
                log=recorder(events),
            )
        self.assertEqual([seat for seat, _ in targets.targets], ["GOOD"])
        self.assertEqual(targets.skipped, {"MISSING": "MISSING_ZULIP_KEY"})
        self.assertEqual(result["outcomes"].get("MISSING"), "missing-credentials")
        self.assertEqual(result["counts"]["missing-credentials"], 1)
        self.assertEqual(result["outcomes"].get("GOOD"), "delivered")


class OwnerInboxDeliveryTests(DaemonHarness):
    """The daemon wires the delivery:  the owner's `@**everyone**` reaches every bot's inbox and
    wakes nobody;  the owner's `@**all**` reaches them too and keeps its one wake."""

    def setUp(self) -> None:
        super().setUp()
        self.transports: dict[str, FakeTransport] = {}
        # The bots sit on the daemon's own realm, so the delivery addresses them there.
        self.targets = IDEL.Targets(
            targets=[
                ("CLAUDE", creds("CLAUDE", site=self.fake.url)),
                ("CODEX", creds("CODEX", site=self.fake.url)),
            ]
        )

    def transport_for(self, c: Z.Credentials, realm: str | None = None) -> FakeTransport:
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

    def test_a_peer_everyone_reaches_no_inbox(self) -> None:
        """A peer bot's `@**everyone**` is not a broadcast:  it wakes nobody, so no bot is
        addressed."""
        daemon = self.started()
        self.transports.clear()
        message_id = self.fake.add_message("Codex", CHANNEL, "fleet", "@**everyone** FYI")
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=self.targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            self.pump_until(daemon, lambda: self.routed(daemon, message_id))
            time.sleep(0.1)
        self.assertEqual(self.transports, {}, "a peer `@everyone` addresses no bot")

    def test_a_peer_all_lands_unread_in_every_bot_inbox(self) -> None:
        """Jay's Oct 27 2026:  a fleet bot's `@all` lands unread in every bot's inbox, like the
        owner's."""
        daemon = self.started()
        self.transports.clear()
        message_id = self.fake.add_message("Codex", CHANNEL, "fleet", "@**all** deploy")
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=self.targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            self.pump_until(daemon, lambda: self.routed(daemon, message_id))
            self.assertTrue(
                self.wait_for(lambda: bool(self.transports)),
                "the inbox delivery never ran",
            )
            time.sleep(0.05)
        self.assertTrue(self.transports, "a peer `@all` addresses every bot")
        for seat, transport in self.transports.items():
            self.assertIn("messages/flags", transport.paths(), seat)



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
            targets=[("CLAUDE", creds("CLAUDE", site=self.fake.url))],
            skipped={"CODEX": "CODEX_ZULIP_KEY"},
        )
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            daemon._deliver_inbox(msg)
        rows = [
            r for r in self.log_rows(daemon) if r.get("event") == "inbox-delivery-missing-credentials"
        ]
        self.assertEqual(len(rows), 1, "one event names every seat without credentials")
        self.assertEqual(rows[0].get("seats"), {"CODEX": "CODEX_ZULIP_KEY"})
        self.assertNotIn("CODEX", self.transports, "the seat without credentials is skipped")

    def test_a_retryable_bot_stays_on_the_retry_queue(self) -> None:
        """The id stays seen:  one owner per message, and the retry queue drives it."""
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
                return_value=IDEL.Targets(targets=[("CLAUDE", creds("CLAUDE", site=self.fake.url))]),
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
        self.assertIn(4242, daemon._inbox_seen, "the id stays seen:  one owner per message")
        pending = daemon._inbox_pending.get(4242)
        self.assertIsNotNone(pending, "the message is on the retry queue")
        self.assertEqual(pending.seats, {"CLAUDE"}, "only the pending seat is queued")
        rows = [
            r
            for r in self.log_rows(daemon)
            if r.get("event") == "inbox-delivery-retry-pending"
        ]
        self.assertEqual(rows[0].get("id"), 4242)
        self.assertEqual(rows[0].get("seats"), ["CLAUDE"])

    def test_a_retry_runs_only_the_steps_that_are_missing(self) -> None:
        """The delivered seat never runs again, and the pending seat does not repeat the
        steps that already worked."""
        daemon = self.started()
        msg = message()
        good = FakeTransport("CLAUDE")
        broken = FakeTransport(
            "CODEX",
            flaky={
                "messages/flags": [
                    Z.NetworkError("timed out", timeout=True, maybe_sent=True)
                ]
            },
        )
        self.transports["CLAUDE"] = good
        self.transports["CODEX"] = broken
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=self.targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            self.assertTrue(
                self.wait_for(
                    lambda: any(
                        row.get("event") == "inbox-delivery-retry-pending"
                        for row in self.log_rows(daemon)
                    )
                ),
                "the retry was never scheduled",
            )
            self.assertEqual(good.paths().count("messages/flags"), 1)
            self.assertEqual(broken.paths().count("users/me/subscriptions"), 1)
            self.assertEqual(broken.paths().count("users/me/subscriptions/properties"), 1)
            self.assertEqual(broken.paths().count("messages/flags"), 1, "the read flag failed")
            self.clock.advance(30)
            daemon.tick()
            self.assertTrue(
                self.wait_for(
                    lambda: broken.paths().count("messages/flags") == 2,
                    seconds=5.0,
                ),
                "the retry never ran",
            )
        self.assertEqual(
            good.paths().count("messages/flags"),
            1,
            "the delivered seat never runs again",
        )
        self.assertEqual(
            broken.paths().count("users/me/subscriptions"),
            1,
            "the subscription is not repeated",
        )
        self.assertEqual(
            broken.paths().count("users/me/subscriptions/properties"),
            1,
            "the quiet settings are not repeated",
        )
        self.assertEqual(broken.paths().count("messages/flags"), 2, "only the read flag ran again")
        self.assertNotIn(4242, daemon._inbox_pending, "the message is delivered")

    def test_the_delivery_gives_up_after_the_last_attempt(self) -> None:
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
                return_value=IDEL.Targets(targets=[("CLAUDE", creds("CLAUDE", site=self.fake.url))]),
            ),
            mock.patch.object(IDEL, "zulip_http", return_value=transport),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            for index, delay in enumerate((30, 60, 120, 240, 480)):
                self.assertTrue(
                    self.wait_for(lambda: len(self.retry_rows(daemon)) == index + 1),
                    "the retry was never scheduled",
                )
                self.clock.advance(delay)
                daemon.tick()
            self.assertTrue(
                self.wait_for(lambda: self.gave_up_rows(daemon)),
                "the delivery never gave up",
            )
        rows = self.gave_up_rows(daemon)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("id"), 4242)
        self.assertEqual(rows[0].get("seats"), ["CLAUDE"])
        self.assertEqual(rows[0].get("outcomes"), {"CLAUDE": "unknown"})
        self.assertNotIn(4242, daemon._inbox_pending, "the message is dropped after the last try")

    def test_a_broken_delivery_is_unknown_and_goes_back_on_the_queue(self) -> None:
        """The delivery itself broke:  every seat without an outcome is unknown, and unknown
        is retried, so the message is never silently lost."""
        daemon = self.started()
        msg = message()
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=self.targets),
            mock.patch.object(IDEL, "deliver", side_effect=RuntimeError("synthetic failure")),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            self.assertTrue(
                self.wait_for(lambda: len(self.retry_rows(daemon)) == 1),
                "the retry was never scheduled",
            )
        failures = [
            r
            for r in self.log_rows(daemon)
            if r.get("event") == "inbox-delivery-failed" and r.get("outcome") == "unknown"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].get("error"), "RuntimeError")
        pending = daemon._inbox_pending.get(4242)
        self.assertIsNotNone(pending, "the message is still owed to the fleet")
        self.assertEqual(pending.seats, {"CLAUDE", "CODEX"})
        self.assertEqual(
            pending.outcomes, {"CLAUDE": "unknown", "CODEX": "unknown"}, "no seat is left silent"
        )

    def test_an_unreadable_infisical_retries_the_whole_message(self) -> None:
        daemon = self.started()
        msg = message()
        with mock.patch.object(
            IDEL,
            "infisical_targets",
            side_effect=IDEL.DeliveryError("Infisical returned nothing"),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            self.assertTrue(
                self.wait_for(lambda: len(self.retry_rows(daemon)) == 1),
                "the retry was never scheduled",
            )
        failures = [
            r for r in self.log_rows(daemon) if r.get("event") == "inbox-delivery-failed"
        ]
        self.assertEqual(len(failures), 1)
        self.assertIn("Infisical", str(failures[0].get("reason")))
        pending = daemon._inbox_pending.get(4242)
        self.assertIsNotNone(pending)
        self.assertIsNone(pending.seats, "every seat waits:  the roster could not be read")
        self.assertEqual(pending.attempts, 1)

    def test_a_seat_without_a_key_is_named_once_and_kept_for_a_retry(self) -> None:
        """The real enumeration boundary:  only the Infisical reader is stubbed.

        A seat with an email and a site but no key name never reaches `infisical_seats`, so
        it would vanish from the fleet without a word.  The roster names it instead, and the
        seat stays on the retry queue, so a repaired key is picked up."""
        daemon = self.started()
        msg = message()
        names = {
            "GOOD_ZULIP_EMAIL",
            "GOOD_ZULIP_KEY",
            "GOOD_ZULIP_SITE",
            "MISSING_ZULIP_EMAIL",
            "MISSING_ZULIP_SITE",
        }
        values = {
            "GOOD_ZULIP_EMAIL": "good-bot@zulip.test",
            "GOOD_ZULIP_KEY": "good-key-value-0123456789",
            "GOOD_ZULIP_SITE": self.fake.url,
            "MISSING_ZULIP_EMAIL": "missing-bot@zulip.test",
            "MISSING_ZULIP_SITE": self.fake.url,
        }
        with (
            mock.patch.object(
                Z, "_default_infisical_names", lambda _env, _home: sorted(names)
            ),
            mock.patch.object(
                Z,
                "_default_infisical_fetch",
                lambda wanted, _env, _home: {name: values.get(name, "") for name in wanted},
            ),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            self.assertTrue(
                self.wait_for(lambda: len(self.retry_rows(daemon)) == 1),
                "the seat without a key was never queued",
            )
        rows = [
            r
            for r in self.log_rows(daemon)
            if r.get("event") == "inbox-delivery-missing-credentials"
        ]
        self.assertEqual(len(rows), 1, "one event names every seat without credentials")
        self.assertEqual(rows[0].get("seats"), {"MISSING": "MISSING_ZULIP_KEY"})
        self.assertNotIn("good-key-value", json.dumps(rows), "a value never reaches the log")
        pending = daemon._inbox_pending.get(4242)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.seats, {"MISSING"}, "the seat without a key waits for a repair")
        self.assertIn("GOOD", self.transports, "the other seat is delivered with its own key")

    def retry_rows(self, daemon) -> list[dict[str, Any]]:
        return [r for r in self.log_rows(daemon) if r.get("event") == "inbox-delivery-retry-pending"]
    def gave_up_rows(self, daemon) -> list[dict[str, Any]]:
        return [r for r in self.log_rows(daemon) if r.get("event") == "inbox-delivery-gave-up"]

    def test_a_terminal_bot_is_reported_once_and_not_retried(self) -> None:
        daemon = self.started()
        msg = message()
        transport = FakeTransport(
            "CLAUDE", errors={"streams": Z.ApiError("forbidden", status=403)}
        )
        with (
            mock.patch.object(
                IDEL,
                "infisical_targets",
                return_value=IDEL.Targets(targets=[("CLAUDE", creds("CLAUDE", site=self.fake.url))]),
            ),
            mock.patch.object(IDEL, "zulip_http", return_value=transport),
        ):
            daemon.start_inbox_delivery(msg, _classes())
            self.assertTrue(
                self.wait_for(
                    lambda: any(
                        row.get("event") == "inbox-delivery-incomplete"
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
        incomplete = [
            r for r in self.log_rows(daemon) if r.get("event") == "inbox-delivery-incomplete"
        ]
        self.assertEqual(len(incomplete), 1, "a terminal seat is reported once")
        self.assertEqual(incomplete[0].get("outcomes"), {"CLAUDE": "terminal"})

    def test_missing_credentials_event_is_not_repeated_on_retry(self) -> None:
        from agent_sync.daemon import InboxPending

        daemon = self.started()
        msg = message()
        daemon._inbox_pending[4242] = InboxPending(message=msg, seats={"CODEX"})
        targets = IDEL.Targets(
            targets=[("CLAUDE", creds("CLAUDE", site=self.fake.url))],
            skipped={"CODEX": "CODEX_ZULIP_KEY", "OTHER": "OTHER_ZULIP_KEY"},
        )
        with (
            mock.patch.object(IDEL, "infisical_targets", return_value=targets),
            mock.patch.object(IDEL, "zulip_http", side_effect=self.transport_for),
        ):
            daemon._deliver_inbox(msg, only_seats={"CODEX"})
            daemon._deliver_inbox(msg, only_seats={"CODEX"})
        rows = [
            r
            for r in self.log_rows(daemon)
            if r.get("event") == "inbox-delivery-missing-credentials"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("seats"), {"CODEX": "CODEX_ZULIP_KEY"})

    def test_whole_retry_pending_event_lists_no_delivered_seat(self) -> None:
        from agent_sync.daemon import InboxPending

        daemon = self.started()
        daemon._inbox_pending[4242] = InboxPending(
            message=message(),
            outcomes={"CLAUDE": "delivered", "CODEX": "unknown"},
        )
        daemon._inbox_finish(4242, outcomes={}, whole=True)
        rows = self.retry_rows(daemon)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("seats"), [])

    def test_inbox_finish_logs_after_releasing_fanout_lock(self) -> None:
        from agent_sync.daemon import InboxPending

        daemon = self.started()
        daemon._inbox_pending[4242] = InboxPending(
            message=message(), outcomes={"CLAUDE": "unknown"}
        )
        seen: list[tuple[str, bool]] = []
        real_log = daemon.log

        class StubLog:
            path = real_log.path

            def write(self, event, **fields):
                acquired = daemon._fanout_lock.acquire(blocking=False)
                seen.append((event, acquired))
                if acquired:
                    daemon._fanout_lock.release()

        daemon.log = StubLog()  # type: ignore[assignment]
        try:
            daemon._inbox_finish(4242, outcomes={"CLAUDE": "unknown"})
        finally:
            daemon.log = real_log  # type: ignore[assignment]
        self.assertEqual(seen, [("inbox-delivery-retry-pending", True)])

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
