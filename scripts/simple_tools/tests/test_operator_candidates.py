"""The Operator senders' shared delivery ledger (operator_candidates.py).

The candidate list this module was named for is gone:  the Operator asks nobody which session a
letter means (owner ruling, Nov 10, 2026).  What stays is the ledger the other Operator senders
(operator_send.py, handbook_service.py) reserve and record their sends in, and these tests pin it:

  - a reservation goes down before a send, so two sends of one key cannot both go out;
  - an `ok` row keeps its message id and blocks a resend inside the window;
  - an `unknown` row is kept, so a message that may exist is never replayed;
  - a definite refusal releases its key, so a clean retry still works;
  - rows older than the window are pruned;
  - a rejected credential is named by a masked preview only;
  - a malformed member record is refused before it is used.

The wake prompt's session-letter rule and the Operator identity helpers are pinned here too.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from simple_tools import operator_candidates as OC
from simple_tools import service_identity as SI
from simple_tools import wakes as W
from simple_tools import zulip as Z

# Synthetic fixture ids only:  no committed test carries a real realm user id.
OPERATOR_ID = 9101
OPERATOR_EMAIL = "operator-bot@zulip.test"
OWNER_ID = 12
WINDOW = 900.0


class LedgerTests(unittest.TestCase):
    """`pending` / `ok` / `unknown` / released, so a retry never resends and never loses a send."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="operator-ledger-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = str(self.tmp / "state" / "SEAT" / "ledger.json")

    def test_a_reservation_blocks_a_second_send_of_the_same_key(self) -> None:
        allowed, since, row = OC._reserve(self.path, "k", 1000.0, WINDOW, OWNER_ID)
        self.assertEqual((allowed, since, row), (True, 0.0, {}))
        allowed, since, row = OC._reserve(self.path, "k", 1060.0, WINDOW, OWNER_ID)
        self.assertFalse(allowed)
        self.assertEqual(since, 60.0)
        self.assertEqual(row["state"], OC.DELIVERY_PENDING)

    def test_an_ok_row_keeps_its_message_id(self) -> None:
        OC._reserve(self.path, "k", 1000.0, WINDOW, OWNER_ID)
        OC._record(self.path, "k", 1000.0, WINDOW, OWNER_ID, 4242, OC.DELIVERY_OK)
        allowed, _since, row = OC._reserve(self.path, "k", 1060.0, WINDOW, OWNER_ID)
        self.assertFalse(allowed, "an acknowledged send is never repeated")
        self.assertEqual((row["state"], row["message_id"]), (OC.DELIVERY_OK, 4242))

    def test_an_unknown_row_is_kept_and_blocks_a_replay(self) -> None:
        OC._reserve(self.path, "k", 1000.0, WINDOW, OWNER_ID)
        OC._record(self.path, "k", 1000.0, WINDOW, OWNER_ID, None, OC.DELIVERY_UNKNOWN)
        allowed, _since, row = OC._reserve(self.path, "k", 1060.0, WINDOW, OWNER_ID)
        self.assertFalse(allowed, "a message that may exist is never sent twice")
        self.assertEqual(row["state"], OC.DELIVERY_UNKNOWN)

    def test_a_released_key_may_be_sent_again(self) -> None:
        OC._reserve(self.path, "k", 1000.0, WINDOW, OWNER_ID)
        OC._release(self.path, "k", 1000.0, WINDOW)
        allowed, _since, _row = OC._reserve(self.path, "k", 1060.0, WINDOW, OWNER_ID)
        self.assertTrue(allowed)

    def test_rows_older_than_the_window_are_pruned(self) -> None:
        OC._reserve(self.path, "old", 1000.0, WINDOW, OWNER_ID)
        OC._record(self.path, "old", 1000.0, WINDOW, OWNER_ID, 1, OC.DELIVERY_OK)
        allowed, _since, _row = OC._reserve(self.path, "old", 1000.0 + WINDOW + 1.0, WINDOW, OWNER_ID)
        self.assertTrue(allowed, "a send outside the window goes again")
        self.assertEqual(set(OC._sends(self.path)), {"old"})
        self.assertEqual(OC._sends(self.path)["old"]["state"], OC.DELIVERY_PENDING)

    def test_prune_keeps_only_rows_inside_the_window_with_a_real_time(self) -> None:
        sends = {
            "fresh": {"at": 990.0},
            "stale": {"at": 1.0},
            "bool": {"at": True},
            "text": {"at": "990"},
            "junk": "not a row",
        }
        self.assertEqual(set(OC._prune(sends, 1000.0, WINDOW)), {"fresh"})

    def test_a_missing_or_malformed_ledger_reads_as_no_sends(self) -> None:
        self.assertEqual(OC._sends(self.path), {})
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        Path(self.path).write_text('{"sends": []}', encoding="utf-8")
        self.assertEqual(OC._sends(self.path), {})

    def test_the_ledger_file_is_private(self) -> None:
        OC._reserve(self.path, "k", 1000.0, WINDOW, OWNER_ID)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_the_delivery_states_are_distinct_tokens(self) -> None:
        states = {
            OC.DELIVERY_PENDING,
            OC.DELIVERY_OK,
            OC.DELIVERY_UNKNOWN,
            OC.DELIVERY_REFUSED,
            OC.DELIVERY_RETRYABLE,
        }
        self.assertEqual(states, {"pending", "ok", "unknown", "refused", "retryable"})


class MaskedTests(unittest.TestCase):
    def test_a_key_is_named_by_its_first_eight_and_last_four_only(self) -> None:
        key = secrets.token_hex(16)
        preview = OC._masked(key)
        self.assertEqual(preview, "%s…%s" % (key[:8], key[-4:]))
        self.assertNotIn(key, preview)

    def test_a_short_or_missing_key_shows_almost_nothing(self) -> None:
        self.assertEqual(OC._masked(""), "missing")
        self.assertEqual(OC._masked(None), "missing")
        self.assertEqual(OC._masked("abcdefgh"), "a…")


class MemberRecordTests(unittest.TestCase):
    def test_a_well_formed_record_is_accepted(self) -> None:
        self.assertTrue(OC._member_ok({"user_id": 12, "email": "jay@zulip.test", "is_bot": False}))
        self.assertTrue(OC._member_ok({"user_id": 13, "is_bot": True, "bot_type": OC.GENERIC_BOT}))

    def test_a_malformed_record_is_refused(self) -> None:
        for bad in (
            None,
            "x",
            {},
            {"user_id": True},
            {"user_id": "12"},
            {"user_id": 12, "is_active": "yes"},
            {"user_id": 12, "bot_type": True},
            {"user_id": 12, "full_name": 7},
        ):
            with self.subTest(bad=bad):
                self.assertFalse(OC._member_ok(bad))


class NothingIsAskedTests(unittest.TestCase):
    """The module no longer carries the candidate list or a way to send one."""

    def test_the_candidate_list_is_gone(self) -> None:
        for name in ("send_candidate_list", "render_body", "candidate_view", "resolve_recipient"):
            self.assertFalse(hasattr(OC, name), name)


class WakePromptTests(unittest.TestCase):
    """The wake prompt tells the seat the rule:  nobody is asked, nothing blocks."""

    def prompt(self, **overrides) -> str:
        pending = W.Pending(
            "w1",
            "MM",
            {"channel": Z.DEFAULT_CHANNEL, "topic": "AFC-412"},
            owner=False,
            now=1.0,
            due=2.0,
        )
        pending.add({"id": 5}, False)
        kwargs = dict(
            seat="MM",
            pending=pending,
            history=[],
            owner_user_id=OWNER_ID,
            is_bot=lambda uid: True,
            owner_of=lambda message: False,
            format_time=str,
            board_enabled=False,
            nonce="n",
        )
        kwargs.update(overrides)
        return W.build_prompt(**kwargs)

    def test_the_prompt_says_nothing_blocks_and_names_the_default(self) -> None:
        prompt = self.prompt()
        self.assertIn("nobody is asked", prompt)
        self.assertNotIn("candidate list", prompt)
        self.assertIn("Nothing here blocks", prompt)
        self.assertIn("your default letter", prompt)
        self.assertIn("name the letter you used", prompt)

    def test_the_prompt_names_the_letter_of_the_tagged_address(self) -> None:
        prompt = self.prompt(tagged_address="Z")
        self.assertIn('the session letter "Z"', prompt)
        self.assertIn("nobody is asked", prompt)
        self.assertNotIn("candidate list", prompt)
        self.assertIn("Nothing here blocks", prompt)

    def test_the_rule_sits_outside_the_untrusted_block(self) -> None:
        prompt = self.prompt()
        lines = prompt.splitlines()
        begin = lines.index("BEGIN_UNTRUSTED_ZULIP nonce=n")
        header = "\n".join(lines[:begin])
        self.assertIn("Nothing here blocks", header)


class ServiceIdentityTests(unittest.TestCase):
    """The Operator senders use the Operator helpers, not a home-grown identity."""

    def test_the_operator_identity_is_the_pinned_service_bot(self) -> None:
        env = {
            "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
            "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
        }
        self.assertEqual(SI.operator_identity(env), (OPERATOR_ID, OPERATOR_EMAIL))
        bot = {"user_id": OPERATOR_ID, "email": OPERATOR_EMAIL, "is_bot": True,
               "bot_owner_id": OWNER_ID}
        self.assertTrue(SI.operator_bot(bot, env, OWNER_ID))
        # Another bot with the same id but a different owner is not the Operator.
        self.assertFalse(SI.operator_bot(dict(bot, bot_owner_id=99), env, OWNER_ID))


if __name__ == "__main__":
    unittest.main()
