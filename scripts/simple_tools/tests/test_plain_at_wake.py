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
