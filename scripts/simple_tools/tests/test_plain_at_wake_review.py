"""Plain @SEAT wakes (Jay, Nov 12 2026:  a plain @SEAT wakes the seat, in addition to the
@**Display Name** mention;  an @SEAT inside code or quoted text wakes nobody)."""

import unittest

from simple_tools import router as R


def me() -> R.SeatIdentity:
    return R.SeatIdentity(user_id=5, full_name="MC", seat="MC", email="mc-bot@example")


class PlainAtWakeTest(unittest.TestCase):
    def tags(self, content: str, **kw) -> bool:
        return R.tags_seat(content, me(), **kw)

    def test_plain_at_tag_wakes(self) -> None:
        self.assertTrue(self.tags("@MC please look at this"))
        self.assertTrue(self.tags("cc @MC"))
        self.assertTrue(self.tags("@MC"))

    def test_any_ascii_case(self) -> None:
        self.assertTrue(self.tags("@mc look"))
        self.assertTrue(self.tags("@Mc look"))

    def test_mid_line_and_line_start(self) -> None:
        self.assertTrue(self.tags("thanks, @MC, that works"))
        self.assertTrue(self.tags("first line\n@MC second line"))

    def test_possessive_parity_with_mentions(self) -> None:
        # A mention "@**MC**'s PR" wakes today;  the plain form keeps the same line.
        self.assertTrue(self.tags("@MC's PR is merged"))

    def test_not_a_tail(self) -> None:
        self.assertFalse(self.tags("mail jay@MC.example now"))
        self.assertFalse(self.tags("see /home/@MC/logs"))
        self.assertFalse(self.tags("word@MC"))
        self.assertFalse(self.tags("\\@MC escaped"))

    def test_domain_like_continuation_never_wakes(self) -> None:
        self.assertFalse(self.tags("visit @MC.example for docs"))
        self.assertFalse(self.tags("visit @MC.example.com/path"))
        self.assertFalse(self.tags("@MC.org"))

    def test_sentence_ending_dot_still_wakes(self) -> None:
        self.assertTrue(self.tags("look at this, @MC."))
        self.assertTrue(self.tags("@MC."))
        self.assertTrue(self.tags("@MC. Next sentence."))

    def test_not_a_longer_token(self) -> None:
        self.assertFalse(self.tags("@MCX is not MC"))
        self.assertFalse(self.tags("@MC-BUILD is not MC"))
        self.assertFalse(self.tags("@MC|123 is not the plain form"))

    def test_code_and_quotes_never_wake(self) -> None:
        self.assertFalse(self.tags("use `@MC` in the header"))
        self.assertFalse(self.tags("```\n@MC in a code block\n```"))
        self.assertFalse(self.tags("> @MC said this earlier"))

    def test_silent_mention_still_never_wakes(self) -> None:
        self.assertFalse(self.tags("@_**MC** was silent"))

    def test_plain_name_without_at_is_a_reference(self) -> None:
        self.assertFalse(self.tags("MC found the bug"))
        self.assertFalse(self.tags("ask MC about it"))

    def test_broadcast_and_mention_paths_unchanged(self) -> None:
        self.assertTrue(self.tags("@**MC** hi"))
        self.assertTrue(self.tags("@**MC|5** hi"))
        self.assertTrue(self.tags("@all hands"))

    def test_other_seat_not_tagged(self) -> None:
        self.assertFalse(self.tags("@CODEX please look"))

    def test_empty_seat_tag_never_matches(self) -> None:
        nobody = R.SeatIdentity(user_id=5, full_name="MC", seat="", email="mc-bot@example")
        self.assertFalse(R.tags_seat("@MC hi", nobody))

    def test_bracket_path_still_gated(self) -> None:
        post = "[CLAUDE: AA.1.S 📁AFC → MC] hi"
        self.assertFalse(self.tags(post))
        self.assertTrue(self.tags(post, bracket_ok=True))

    def test_helper_directly(self) -> None:
        self.assertTrue(R.plain_at_seat("cc @MC", "MC"))
        self.assertFalse(R.plain_at_seat("cc @MC", ""))
        self.assertFalse(R.plain_at_seat("no tag", "MC"))


if __name__ == "__main__":
    unittest.main()


OWNER, CLAUDE, CURSOR = 12, 10, 13
NOW = 10_000.0
ME2 = R.SeatIdentity("CODEX", 11, "Codex", "codex-bot@zulip.test")
EMAIL = {
    CLAUDE: "claude-bot@zulip.test",
    11: "codex-bot@zulip.test",
    CURSOR: "cursor-bot@zulip.test",
    OWNER: "jay@zulip.test",
}


def _msg(sender=CLAUDE, content="@CODEX please look", *, ts=NOW - 10, flags=(), mid=101):
    return {
        "id": mid,
        "sender_id": sender,
        "sender_email": EMAIL[sender],
        "content": content,
        "flags": list(flags),
        "timestamp": ts,
        "client": "website" if sender == OWNER else "ZulipPython",
        "sender_full_name": "x",
        "type": "stream",
        "display_recipient": "simple-tools",
        "subject": "t",
    }


def _ctx(**kw):
    options = dict(
        owner_user_id=OWNER,
        owner_clients=["website", "ZulipMobile"],
        eligible_user_ids=[11, CLAUDE, CURSOR],
        is_bot={11: True, CLAUDE: True, CURSOR: True, OWNER: False},
        now=NOW,
        stale_after=7200.0,
        presence=[("simple-tools", "roll call")],
    )
    options.update(kw)
    return R.Context(**options)


def _routed(message, context=None):
    return R.route(message, ME2, context if context is not None else _ctx(), [])


class PlainAtClassifyTest(unittest.TestCase):
    """The gap sentry caught on #2161:  plain @SEAT reached only tags_seat (the edit path),
    never classify/prefilter, so a new post tagged with a plain @SEAT was dropped as chatter."""

    def test_new_post_plain_at_wakes_peer(self) -> None:
        c, d = _routed(_msg())
        self.assertTrue(c.plain_at)
        self.assertIn("plain_at", c.labels())
        self.assertEqual(d.wake, "peer")
        self.assertTrue(d.seat_inbox)

    def test_owner_plain_at_wakes_as_owner(self) -> None:
        c, d = _routed(_msg(sender=OWNER))
        self.assertTrue(c.plain_at)
        self.assertEqual(d.wake, "owner")

    def test_stale_plain_at_does_not_wake(self) -> None:
        c, d = _routed(_msg(ts=NOW - 9000))
        self.assertTrue(c.plain_at)
        self.assertIsNone(d.wake)

    def test_code_and_quoted_plain_at_do_not_classify(self) -> None:
        for content in ("use `@CODEX` here", "```\n@CODEX\n```", "> @CODEX said so"):
            with self.subTest(content=content):
                c, d = _routed(_msg(content=content))
                self.assertFalse(c.plain_at)
                self.assertIsNone(d.wake)

    def test_plain_at_never_becomes_a_reply_wake(self) -> None:
        # The reply-to-chat class must stay out:  the post tags someone.
        c, d = _routed(_msg())
        self.assertIsNone(c.reply_to_chat)

    def test_other_seat_plain_at_is_chatter(self) -> None:
        c, d = _routed(_msg(content="@CURSOR please look"))
        self.assertFalse(c.plain_at)
        self.assertIsNone(d.wake)

    def test_dm_plain_at_not_double_counted(self) -> None:
        m = _msg()
        m.update(
            {"type": "private", "display_recipient": [{"id": CLAUDE}, {"id": 11}], "subject": ""}
        )
        c, d = _routed(m)
        self.assertFalse(c.plain_at)
        self.assertEqual(d.wake, "peer")  # the DM itself wakes, unchanged


KEY_T = "simple-tools/t"


def _lease(**kw):
    from typing import Any
    options: dict[str, Any] = dict(
        platform="claude-code", topics=[KEY_T], mentions=True, wake_capable=True,
        posted=[100], prefix=None,
    )
    options.update(kw)
    return R.LeaseView("L1", **options)


class PlainAtRouteParityTest(unittest.TestCase):
    """Route-level parity with the loud mention, requested by the #2161 reviewer:  new posts
    with no `mentioned` flag, the no-wake marker, stale, ineligible senders, muted topics,
    lease interrupts, and the negative tag shapes - all at route level, not tags_seat."""

    def test_peer_and_owner_new_post_without_mentioned_flag(self) -> None:
        # No flags at all:  Zulip's `mentioned` flag is never set for a plain @SEAT.
        _, dp = _routed(_msg(sender=CLAUDE, flags=()))
        _, do = _routed(_msg(sender=OWNER, flags=()))
        self.assertEqual((dp.wake, do.wake), ("peer", "owner"))

    def test_no_wake_marker_suppresses_only_the_wake(self) -> None:
        c, d = _routed(_msg(content=":eyes: @CODEX please look"))
        self.assertTrue(c.no_wake)
        self.assertIsNone(d.wake)
        self.assertEqual(d.wake_block, "no_wake")
        self.assertTrue(d.seat_inbox)  # the copy still lands, as for a muted mention

    def test_stale_never_wakes(self) -> None:
        _, d = _routed(_msg(ts=NOW - 9000))
        self.assertIsNone(d.wake)
        self.assertEqual(d.wake_block, "stale")

    def test_ineligible_sender_never_wakes(self) -> None:
        m = _msg(sender=CURSOR)
        m["sender_id"] = 99
        m["sender_email"] = "stranger-bot@zulip.test"
        c, d = _routed(m)
        self.assertFalse(c.eligible)
        self.assertIsNone(d.wake)

    def test_muted_topic_parity_with_the_mention(self) -> None:
        from simple_tools.live import topic_key
        context = _ctx(muted={topic_key("simple-tools", "t")})
        _, plain = _routed(_msg(), context)
        # A directed ask overrides the mute exactly like the loud mention does.
        m = _msg(content="@**Codex** please look", flags=("mentioned",))
        _, loud = _routed(m, context)
        self.assertEqual(plain.wake, loud.wake)
        self.assertEqual(plain.wake, "peer")

    def test_lease_interrupt_parity_with_the_mention(self) -> None:
        _, plain = R.route(_msg(), ME2, _ctx(), [_lease()])
        loud_msg = _msg(content="@**Codex** ok", flags=("mentioned",))
        _, loud = R.route(loud_msg, ME2, _ctx(), [_lease()])
        self.assertEqual(plain.lease_items, loud.lease_items)
        self.assertEqual(plain.lease_items, [("L1", R.INTERRUPT, "message")])

    def test_negative_tag_shapes_drop_as_chatter_at_route_level(self) -> None:
        for content in (
            "@CODEXX is not CODEX",
            "@CODEX-BUILD is not CODEX",
            "visit @CODEX.example for docs",
            "use `@CODEX` in code",
            "> @CODEX said so",
        ):
            with self.subTest(content=content):
                c, d = _routed(_msg(content=content))
                self.assertFalse(c.plain_at)
                self.assertIsNone(d.wake)
                self.assertEqual(d.drop_reason, "chatter")

    def test_edit_gate_inputs_unchanged(self) -> None:
        # The daemon's edit gate compares tags_seat(new) against tags_seat(old):  a plain
        # @SEAT added by an edit still satisfies exactly that comparison, and a post with no
        # tag never classifies plain_at.
        self.assertTrue(R.tags_seat("@CODEX hi", ME2))
        self.assertFalse(R.tags_seat("hi", ME2))
        c, _ = _routed(_msg(content="no tag here"))
        self.assertFalse(c.plain_at)
