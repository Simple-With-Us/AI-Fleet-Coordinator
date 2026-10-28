"""The peer-broadcast audit (Jay, Oct 27 2026):  a fleet bot's `@all` must be explained to Jay.

The transport and the clock are injected, so every outcome runs offline here.
"""
from __future__ import annotations

import unittest

from agent_sync import broadcast_audit as BA
from agent_sync import secretscan
from agent_sync import zulip as Z

OWNER = 12
NOW = 1_700_000_000.0
PERMALINK = "https://simplewithus.zulipchat.com/#narrow/near/500"


def broadcast(mid: int = 500, seat: str = "GB-ENGINEER", **kw) -> BA.Broadcast:
    options = dict(
        message_id=mid,
        sender_seat=seat,
        sender_email="gb-engineer-bot@zulip.test",
        channel="work-in-progress",
        topic="fleet",
        timestamp=NOW,
    )
    options.update(kw)
    return BA.Broadcast(**options)


def message(mid: int, sender: int, *, channel="work-in-progress", topic="fleet", ts=NOW, kind="stream"):
    return {
        "id": mid,
        "sender_id": sender,
        "type": kind,
        "display_recipient": channel,
        "subject": topic,
        "timestamp": ts,
    }


class FakeHttp:
    """A transport that answers the two pages the audit asks for."""

    def __init__(self, topic=None, dm=None, error=None):
        self.topic = list(topic or [])
        self.dm = list(dm or [])
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, dict(payload or {})))
        if self.error is not None:
            raise self.error
        narrow = payload.get("narrow") or []
        operators = [item.get("operator") for item in narrow]
        if "dm" in operators:
            return {"messages": list(self.dm)}
        return {"messages": list(self.topic)}

    def narrow_of(self, index: int) -> list[dict]:
        return self.calls[index][2].get("narrow") or []


class PagedHttp:
    """A transport that pages back through a topic's history the way Zulip does."""

    def __init__(self, topic):
        self.topic = sorted(topic, key=lambda m: m["id"])
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, dict(payload or {})))
        narrow = payload.get("narrow") or []
        if any(item.get("operator") == "dm" for item in narrow):
            return {"messages": []}
        anchor = int(payload.get("anchor") or 0)
        before = int(payload.get("num_before") or 0)
        rows = [m for m in self.topic if m["id"] < anchor]
        return {"messages": rows[-before:] if before else rows}


class BusyHttp:
    """A transport that always answers with a full page of busy history."""

    def __init__(self, count: int = BA.OWNER_CONTEXT_PAGE, ts: float = NOW - 3000):
        self.count = count
        self.ts = ts
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, dict(payload or {})))
        narrow = payload.get("narrow") or []
        if any(item.get("operator") == "dm" for item in narrow):
            return {"messages": []}
        anchor = int(payload.get("anchor") or 0)
        ids = [anchor - 1 - i for i in range(self.count)]
        return {"messages": [message(i, 99, ts=self.ts) for i in ids]}


class OwnerContextTests(unittest.TestCase):
    def test_jay_in_the_topic_just_before_the_broadcast_is_owner_context(self) -> None:
        http = FakeHttp(topic=[message(400, OWNER, ts=NOW - 60)])
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.OWNER_CONTEXT)
        self.assertEqual(verdict.text, "", "nothing is sent when Jay asked for it")

    def test_a_post_after_the_broadcast_is_not_owner_context(self) -> None:
        http = FakeHttp(topic=[message(600, OWNER, ts=NOW + 60)])
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertNotEqual(verdict.outcome, BA.OWNER_CONTEXT)

    def test_a_post_in_another_topic_is_not_owner_context(self) -> None:
        http = FakeHttp(topic=[message(400, OWNER, topic="other", ts=NOW - 60)])
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertNotEqual(verdict.outcome, BA.OWNER_CONTEXT)

    def test_a_post_too_long_before_the_broadcast_is_not_owner_context(self) -> None:
        http = FakeHttp(topic=[message(400, OWNER, ts=NOW - 61 * 60)])
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertNotEqual(verdict.outcome, BA.OWNER_CONTEXT)


class ExplainedTests(unittest.TestCase):
    def test_a_dm_after_the_broadcast_explains_it(self) -> None:
        http = FakeHttp(dm=[message(501, 99, kind="private", ts=NOW + 5)])
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.EXPLAINED)
        self.assertEqual(verdict.text, "")

    def test_a_dm_before_the_broadcast_explains_nothing(self) -> None:
        http = FakeHttp(dm=[message(499, 99, kind="private", ts=NOW - 5)])
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.REMIND)


class ReminderTests(unittest.TestCase):
    def test_nothing_explained_the_broadcast_reminds_jay(self) -> None:
        http = FakeHttp()
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.REMIND)
        self.assertIn("[MA\u00b7note]", verdict.text)
        self.assertIn("GB-ENGINEER", verdict.text)
        self.assertIn("#work-in-progress", verdict.text)
        self.assertIn("fleet", verdict.text)
        self.assertIn(PERMALINK, verdict.text)
        self.assertIn("within 10 minutes", verdict.text)

    def test_the_reminder_never_carries_the_message_content(self) -> None:
        http = FakeHttp()
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertNotIn("deploy", verdict.text)

    def test_the_dm_check_asks_for_the_senders_own_dms(self) -> None:
        http = FakeHttp()
        BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        dm_call = http.calls[-1]
        self.assertEqual(dm_call[0], "GET")
        self.assertEqual(dm_call[1], "messages")
        narrow = dm_call[2].get("narrow")
        self.assertIn({"operator": "dm", "operand": [OWNER]}, narrow)
        self.assertIn({"operator": "sender", "operand": "me"}, narrow)
        self.assertEqual(dm_call[2].get("anchor"), "newest")
        self.assertEqual(dm_call[2].get("num_before"), 20)
        self.assertEqual(dm_call[2].get("num_after"), 0)


class CouldNotCheckTests(unittest.TestCase):
    def test_a_check_that_could_not_run_still_reminds_jay_and_says_why(self) -> None:
        http = FakeHttp(error=RuntimeError("the vault is away"))
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.REMIND)
        self.assertIn("Could not check its DMs", verdict.text)
        self.assertIn("RuntimeError", verdict.text)

    def test_a_malformed_answer_is_a_reason_and_not_a_pass(self) -> None:
        def broken(method, path, payload=None):
            return {"messages": "not a list"}

        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=broken,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.REMIND)
        self.assertIn("Could not check its DMs", verdict.text)

    def test_an_unknown_sender_seat_is_a_reason(self) -> None:
        """A mail from outside the fleet is not a seat:  there is no account to read."""
        self.assertEqual(BA.sender_seat_for("", seats=("CODEX",)), "")
        self.assertEqual(
            BA.sender_seat_for("nobody@zulip.test", seats=("CODEX",)), ""
        )
        self.assertEqual(BA.sender_seat_for("codex-bot@zulip.test", seats=("CODEX",)), "CODEX")


class RateLimitTests(unittest.TestCase):
    def test_a_seat_gets_at_most_three_reminders_in_an_hour(self) -> None:
        rate = BA.RateLimiter()
        self.assertTrue(rate.allow("GB-ENGINEER", NOW))
        self.assertTrue(rate.allow("GB-ENGINEER", NOW + 60))
        self.assertTrue(rate.allow("GB-ENGINEER", NOW + 120))
        self.assertFalse(rate.allow("GB-ENGINEER", NOW + 180), "the fourth is refused")
        # The window rolls:  an hour later the seat may be reminded again.
        self.assertTrue(rate.allow("GB-ENGINEER", NOW + 3601))

    def test_the_rate_limited_audit_sends_nothing(self) -> None:
        rate = BA.RateLimiter()
        for _ in range(3):
            rate.allow("GB-ENGINEER", NOW)
        http = FakeHttp()
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=rate,
            now=NOW + 200,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.RATE_LIMITED)
        self.assertEqual(verdict.text, "")


class TextTests(unittest.TestCase):
    def test_the_sentences_are_separated_by_a_no_break_space_and_a_space(self) -> None:
        text = BA.reminder_text(
            "MA", "GB-ENGINEER", "work-in-progress", "fleet", PERMALINK,
            reason="its credentials could not be read (CredentialError)",
        )
        self.assertIn("\u00a0 ", text, "a real no-break space and a normal space")
        self.assertNotIn("&nbsp;", text)
        self.assertNotIn(".  ", text, "never two plain spaces between sentences")

    def test_the_topic_goes_through_the_usual_escaping(self) -> None:
        text = BA.reminder_text(
            "MA", "GB-ENGINEER", "work-in-progress", "a\nb", PERMALINK
        )
        self.assertNotIn("\n", text.split("]")[1], "a topic never breaks the line")

    def test_a_topic_that_carries_a_credential_is_refused_by_the_scanner(self) -> None:
        """The reminder is built from the topic, so the scanner has the last word on it."""
        clean = BA.reminder_text("MA", "GB-ENGINEER", "work-in-progress", "fleet", PERMALINK)
        self.assertIsNone(secretscan.scan(clean))
        planted = BA.reminder_text(
            "MA", "GB-ENGINEER", "work-in-progress", "sk-ant-" + "a" * 32, PERMALINK
        )
        self.assertIsNotNone(secretscan.scan(planted), "the scanner refuses it, so the DM is not sent")


class OwnerContextWindowTests(unittest.TestCase):
    """The window is the sixty minutes before the `@all`:  the history has to reach that far
    back, and a history that stops inside it is a check that could not run."""

    def test_jay_is_found_further_back_than_one_page(self) -> None:
        """A busy topic:  Jay's ask is 250 messages back, so a fixed page of twenty misses it."""
        rows = [message(2 + i, 99, ts=NOW - 3000) for i in range(250)]
        rows.append(message(1, OWNER, ts=NOW - 3600))
        http = PagedHttp(rows)
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.OWNER_CONTEXT)

    def test_a_history_that_stops_inside_the_window_is_a_reason_and_not_a_pass(self) -> None:
        """The history never reaches the window:  the reminder says the check could not run."""
        http = BusyHttp()
        verdict = BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        self.assertEqual(verdict.outcome, BA.REMIND)
        self.assertIn("Could not check", verdict.text)
        self.assertIn("far enough back", verdict.text)

    def test_the_dm_check_still_runs_after_the_topic_history(self) -> None:
        http = BusyHttp()
        BA.evaluate(
            broadcast(),
            owner_user_id=OWNER,
            http=http,
            permalink=PERMALINK,
            rate=BA.RateLimiter(),
            now=NOW,
            alert_seat="MA",
        )
        dm_call = http.calls[-1]
        narrow = dm_call[2].get("narrow")
        self.assertIn({"operator": "dm", "operand": [OWNER]}, narrow)


class DmFailureTests(unittest.TestCase):
    """A failed DM to the owner is classified:  a retryable one is kept, a permanent one is
    dropped, and a write that may have landed is never sent twice."""

    def test_a_rate_limit_is_retryable(self) -> None:
        self.assertEqual(BA.dm_failure_outcome(Z.ApiError("slow down", status=429)), BA.RETRY)

    def test_a_server_error_is_retryable(self) -> None:
        self.assertEqual(BA.dm_failure_outcome(Z.ApiError("oops", status=503)), BA.RETRY)

    def test_a_permanent_refusal_is_not_retryable(self) -> None:
        self.assertEqual(BA.dm_failure_outcome(Z.ApiError("nope", status=400)), BA.PERMANENT)

    def test_a_connection_that_never_opened_is_retryable(self) -> None:
        self.assertEqual(BA.dm_failure_outcome(Z.NetworkError("no route")), BA.RETRY)

    def test_a_write_that_may_have_landed_is_never_sent_twice(self) -> None:
        self.assertEqual(
            BA.dm_failure_outcome(Z.NetworkError("timeout", maybe_sent=True)), BA.UNKNOWN
        )


if __name__ == "__main__":
    unittest.main()
