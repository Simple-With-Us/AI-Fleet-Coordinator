"""Bracket-recipient wakes:  a seat named in a post's leading bracket intro wakes without an @,
when the seat has turned `bracket_recipient_wakes` on (owner request, Nov 12 2026:  "lets make it
optional in Muster app to configure the eavesdropper to consider recipients in the brackets intro
to posts to receive wakes even without an @").

Off by default, per seat.  Only the list after the header's arrow names a recipient:  the sender's
own tag before it, a bracket inside code or quotes, and a bracket anywhere but the very start of
the post never count.  Every wake guard (eligibility, the stale rule, the no-wake marker, the loop
guard and the budgets) applies exactly as it does to a mention.

Router level only:  `router.classify`, `prefilter`, `route` and `tags_seat`, the way
test_reply_wake.py exercises the reply classes.
"""
from __future__ import annotations

import unittest
from typing import Any

from simple_tools import router as R
from simple_tools.live import topic_key

NOW = 10_000.0
OWNER, CLAUDE, CURSOR, SENTRY = 12, 10, 13, 40
ME = R.SeatIdentity("CODEX", 11, "Codex", "codex-bot@zulip.test")
EMAIL = {
    CLAUDE: "claude-bot@zulip.test",
    11: "codex-bot@zulip.test",
    CURSOR: "cursor-bot@zulip.test",
    OWNER: "jay@zulip.test",
    SENTRY: "sentry-bot@zulip.test",
}

# CLAUDE posts, naming CODEX in the bracket recipient list, with no @ anywhere.
BRACKET = "[CLAUDE: AA.1.S 📁AFC → CODEX] the cutover is ready for review"
LEGACY = "[CLAUDE·4984a346→CODEX] the cutover is ready for review"


def msg(
    sender: int = CLAUDE,
    content: str = BRACKET,
    *,
    mid: int = 101,
    topic: str = "t",
    channel: str = "simple-tools",
    ts: float = NOW - 10,
    flags: tuple[str, ...] = (),
    dm: bool = False,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": mid,
        "sender_id": sender,
        "sender_email": EMAIL[sender],
        "content": content,
        "flags": list(flags),
        "timestamp": ts,
        "client": "website" if sender == OWNER else "ZulipPython",
        "sender_full_name": "x",
    }
    if dm:
        message.update(
            {"type": "private", "display_recipient": [{"id": sender}, {"id": 11}], "subject": ""}
        )
    else:
        message.update({"type": "stream", "display_recipient": channel, "subject": topic})
    return message


def ctx(bracket: bool = True, **kw: Any) -> R.Context:
    options: dict[str, Any] = dict(
        owner_user_id=OWNER,
        owner_clients=["website", "ZulipMobile"],
        eligible_user_ids=[11, CLAUDE, CURSOR],
        is_bot={11: True, CLAUDE: True, CURSOR: True, OWNER: False, SENTRY: True},
        now=NOW,
        stale_after=7200.0,
        presence=[("simple-tools", "roll call")],
        bracket_recipient_wakes=bracket,
    )
    options.update(kw)
    return R.Context(**options)


def routed(message: dict[str, Any], context: R.Context | None = None):
    return R.route(message, ME, context if context is not None else ctx(), [])


class BracketRecipientTest(unittest.TestCase):
    def test_off_by_default(self) -> None:
        c, d = routed(msg(), ctx(bracket=False))
        self.assertFalse(c.bracket_recipient)
        self.assertIsNone(d.wake)
        self.assertEqual(d.drop_reason, "chatter")

    def test_recipient_wakes(self) -> None:
        c, d = routed(msg())
        self.assertTrue(c.bracket_recipient)
        self.assertIn("bracket_recipient", c.labels())
        self.assertTrue(d.wake_worthy)
        self.assertEqual(d.wake, "peer")
        self.assertTrue(d.seat_inbox)

    def test_legacy_arrow_form_wakes(self) -> None:
        c, d = routed(msg(content=LEGACY))
        self.assertTrue(c.bracket_recipient)
        self.assertEqual(d.wake, "peer")

    def test_recipient_list_shapes(self) -> None:
        for listing in (
            "[CLAUDE 📁AFC → codex] x",  # any ASCII case
            "[CLAUDE 📁AFC → Codex] x",  # the mention name
            "[CLAUDE 📁AFC → @Codex] x",  # a plain @ in the list
            "[CLAUDE 📁AFC → @_**Codex**] x",  # the posting tools' silent mention
            "[CLAUDE 📁AFC → @**Codex**] x",  # a loud one
            "[CLAUDE 📁AFC → CODEX BB.1.M] x",  # with a session letter
            "[CLAUDE 📁AFC → CURSOR, CODEX] x",  # one of several
            "[CLAUDE: AA.1.S 📁AFC, OPS → CODEX] x",  # several repos
        ):
            with self.subTest(listing=listing):
                c, d = routed(msg(content=listing))
                self.assertTrue(c.bracket_recipient)
                self.assertEqual(d.wake, "peer")

    def test_header_without_arrow_names_nobody(self) -> None:
        for content in (
            "[CLAUDE 📁AFC] the cutover is ready",
            "[CLAUDE: AA.1.S 📁AFC] the cutover is ready",
            "[CODEX 📁AFC] a post that opens with this seat's own tag",  # the sender's tag is not a list
            "[CLAUDE 📁AFC → CURSOR] names another seat only",
            "[CLAUDE 📁AFC → CODEXX] a longer name is not this seat",
            "[CLAUDE 📁AFC → CO] a shorter one is not either",
        ):
            with self.subTest(content=content):
                c, d = routed(msg(content=content))
                self.assertFalse(c.bracket_recipient)
                self.assertIsNone(d.wake)

    def test_code_and_quotes_never_name(self) -> None:
        for content in (
            "```\n[CLAUDE 📁AFC → CODEX] inside a fenced block\n```",
            "~~~quote\n[CLAUDE 📁AFC → CODEX] inside a quote\n~~~",
            "> [CLAUDE 📁AFC → CODEX] a quoted line",
            "leading prose first\n[CLAUDE 📁AFC → CODEX] not at the start",
            " [CLAUDE 📁AFC → CODEX] indented is still the start, so this one wakes",
        ):
            with self.subTest(content=content):
                c, d = routed(msg(content=content))
                if content.startswith(" ["):
                    self.assertTrue(c.bracket_recipient)
                else:
                    self.assertFalse(c.bracket_recipient)
                    self.assertIsNone(d.wake)

    def test_stale_post_does_not_wake(self) -> None:
        c, d = routed(msg(ts=NOW - 7201))
        self.assertTrue(c.bracket_recipient)
        self.assertIsNone(d.wake)
        self.assertEqual(d.wake_block, "stale")

    def test_no_wake_marker_suppresses_the_wake_only(self) -> None:
        c, d = routed(msg(content="👀 [CLAUDE 📁AFC → CODEX] visibility only"))
        self.assertTrue(c.no_wake)
        self.assertIsNone(d.wake)
        self.assertEqual(d.wake_block, "no_wake")
        self.assertTrue(d.seat_inbox)

    def test_ineligible_sender_does_not_wake(self) -> None:
        # Exactly like a direct mention from a sender who is not eligible:  the inbox copy is
        # kept and nothing wakes.
        c, d = routed(msg(sender=SENTRY))
        self.assertFalse(c.eligible)
        self.assertIsNone(d.wake)
        self.assertEqual(d.wake_block, "not_eligible")
        self.assertTrue(d.seat_inbox)

    def test_owner_recipient_post_wakes_as_owner(self) -> None:
        c, d = routed(msg(sender=OWNER, content="[JAY 📁AFC → CODEX] please look"))
        self.assertTrue(c.bracket_recipient)
        self.assertEqual(d.wake, "owner")
        self.assertTrue(d.wake_owner)

    def test_muted_topic_still_wakes_like_a_mention(self) -> None:
        c, d = routed(msg(), ctx(muted=[topic_key("simple-tools", "t")]))
        self.assertTrue(c.bracket_recipient)
        self.assertEqual(d.wake, "peer")
        self.assertNotEqual(d.drop_reason, "muted")

    def test_dm_unchanged(self) -> None:
        c, d = routed(msg(dm=True, content="hello with no bracket at all"))
        self.assertFalse(c.bracket_recipient)
        self.assertEqual(d.wake, "peer")

    def test_tags_seat_edit_gate(self) -> None:
        # The edit path compares old and new text with tags_seat:  only with the seat's switch on
        # does a newly added bracket recipient count as newly tagged.
        self.assertFalse(R.tags_seat(BRACKET, ME))
        self.assertTrue(R.tags_seat(BRACKET, ME, bracket_ok=True))
        self.assertFalse(R.tags_seat("[CLAUDE 📁AFC] no list", ME, bracket_ok=True))
        # A loud mention still tags with the switch off, exactly as before.
        mention = "[CLAUDE 📁AFC] @**Codex** please look"
        self.assertTrue(R.tags_seat(mention, ME))
        self.assertTrue(R.tags_seat(mention, ME, bracket_ok=True))

    def test_mention_path_byte_identical_when_off(self) -> None:
        # A seat that never turns the switch on classifies a loud mention exactly as before.
        mention = "[CLAUDE 📁AFC] @**Codex** please look"
        c, d = routed(msg(content=mention, flags=("mentioned",)), ctx(bracket=False))
        self.assertTrue(c.direct)
        self.assertFalse(c.bracket_recipient)
        self.assertEqual(d.wake, "peer")


class BracketConfigTest(unittest.TestCase):
    """The per-seat switch in `config.py`:  absent is Off, true parses, anything else is a wake
    problem that leaves the seat capturing with the switch Off."""

    BASE = {
        "daemon": {"owner_user_id": 1},
        "seat": {
            "MM": {
                "bot": "MM",
                "wake": "http",
                "routine": {"url_env": "MM_ROUTINE_URL", "key_env": "MM_ROUTINE_KEY"},
            }
        },
    }

    def parse(self, *keys: tuple[str, object]):
        import copy

        from simple_tools import config as C

        raw = copy.deepcopy(self.BASE)
        for key, value in keys:
            raw["seat"]["MM"][key] = value
        cfg = C.from_dict(raw)
        return cfg, cfg.seats["MM"]

    def test_absent_is_off(self) -> None:
        cfg, seat = self.parse()
        self.assertFalse(seat.bracket_recipient_wakes)
        self.assertEqual(seat.config_problems, [])
        self.assertFalse(any("bracket" in w for w in cfg.warnings))

    def test_true_parses(self) -> None:
        cfg, seat = self.parse(("bracket_recipient_wakes", True))
        self.assertTrue(seat.bracket_recipient_wakes)
        self.assertEqual(seat.config_problems, [])

    def test_a_non_bool_is_a_wake_problem_and_off(self) -> None:
        cfg, seat = self.parse(("bracket_recipient_wakes", "yes"))
        self.assertFalse(seat.bracket_recipient_wakes)
        self.assertIn(
            "seat.MM.bracket_recipient_wakes must be true or false", seat.config_problems
        )


if __name__ == "__main__":
    unittest.main()
