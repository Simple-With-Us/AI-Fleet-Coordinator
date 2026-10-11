"""Answers:  a tap, a typed reply, a Linear comment, the note that wakes the filer, and what must never count."""
from __future__ import annotations

import datetime as dt
import json
import unittest

from needs_owner import notifier
from needs_owner import replies as R

from .test_notifier import NotifierCase
from .world import KEYCAP, OWNER_ID

OTHER_PERSON = 8
LINEAR_OWNER = "0b1c2d3e-0000-4000-8000-00000000aaaa"
LINEAR_OTHER = "0b1c2d3e-0000-4000-8000-00000000cccc"
LINEAR_APP = "0b1c2d3e-0000-4000-8000-00000000dddd"


class RepliesCase(NotifierCase):
    def item(self, question: str = "Revive PR 761 or close it?", *, seat: str = "CODEX", low: str | None = "1") -> dict:
        return self.file(question, "Revive it", "Close it", "Ask Codex", seat=seat, low=low)

    def tap(self, record: dict, option: int, user: int = OWNER_ID, **kw) -> None:
        self.w.react(record["notified_msg_id"], user, kw.pop("code", KEYCAP[option]), **kw)

    def replied(self, ident: str) -> list[str]:
        """The reply records the notifier wrote (CLAUDE's comments), not anything a test or a person typed."""
        return [c for c in self.w.comments(ident) if c.startswith("[CLAUDE") and "owner replied:" in c]

    def notes(self) -> list[dict]:
        return [c for c in self.sent_by_notifier() if "--to" in c["argv"]]


class TapTests(RepliesCase):
    def test_jays_tap_on_a_low_stakes_option_is_recorded_and_the_filer_is_told(self) -> None:
        record = self.item()
        self.tap(record, 1)
        result = self.run_notify()
        (replied,) = self.events(result, "replied")
        self.assertEqual((replied["id"], replied["kind"], replied["option"], replied["ref"]),
                         (record["identifier"], "reaction", 1, record["notified_msg_id"]))
        (comment,) = self.replied(record["identifier"])
        self.assertIn("[CLAUDE", comment)
        self.assertIn("owner replied:  reaction 1 on msg %d" % record["notified_msg_id"], comment)
        self.assertNotIn("Revive", comment, "the record never holds the answer's words")
        # the filer is woken by one directed note in the item's own topic
        (note,) = self.notes()
        self.assertEqual(note["argv"][note["argv"].index("--to") + 1], "CODEX")
        self.assertEqual(note["argv"][note["argv"].index("--topic") + 1], record["topic"])
        self.assertNotIn("--to-owner", note["argv"])
        for needle in (record["identifier"], record["url"], "reaction on message %d" % record["notified_msg_id"], "Verify it before you act",
                       "stays open"):
            self.assertIn(needle, note["stdin"])
        for word in ("Revive", "Close it", "option 1"):
            self.assertNotIn(word, note["stdin"], "the note never restates the answer")
        # nothing is closed, edited or acted on
        self.assertEqual(self.issue_status(record["identifier"]), "Todo")
        self.assertIn("needs-owner", [x["name"] for x in self.w.issue(record["identifier"])["labels"]["nodes"]])
        self.assertEqual(self.state_of(record["identifier"])["replied"], {"kind": "reaction", "id": str(record["notified_msg_id"]), "option": 1, "by": "CLAUDE"})

    def test_a_reply_is_recorded_once_and_the_note_goes_out_once(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.run_notify()
        again = self.run_notify()
        self.assertEqual(self.events(again, "replied"), [])
        self.assertEqual(len(self.replied(record["identifier"])), 1)
        self.assertEqual(len(self.notes()), 1)
        self.tap(record, 3)  # a later reaction does not undo the first answer:  the filer decides
        self.run_notify()
        self.assertEqual(len(self.replied(record["identifier"])), 1)

    def test_taps_by_anyone_else_or_of_another_kind_are_ignored(self) -> None:
        record = self.item()
        self.tap(record, 1, user=OTHER_PERSON)  # someone else
        self.tap(record, 1, user=999)  # a bot
        self.w.react(record["notified_msg_id"], OWNER_ID, "12", emoji_name="one", reaction_type="realm_emoji")  # a custom emoji named one
        self.w.react(record["notified_msg_id"], OWNER_ID, "1f44d", emoji_name="thumbs_up")  # a thumbs up
        self.w.react(record["notified_msg_id"], OWNER_ID, KEYCAP[5], emoji_name="five")  # an option that does not exist
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        self.assertEqual(self.replied(record["identifier"]), [])
        self.assertEqual(self.sent_by_notifier(), [])
        self.assertEqual(self.state_of(record["identifier"])["state"], "notified")

    def test_the_emoji_with_the_variation_selector_counts_too(self) -> None:
        record = self.item()
        self.tap(record, 1, code="0031-fe0f-20e3")
        self.assertEqual(len(self.events(self.run_notify(), "replied")), 1)

    def test_a_tap_on_a_risky_option_is_not_an_answer_and_the_topic_says_so_once(self) -> None:
        record = self.item()
        self.tap(record, 2)
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        (nudge,) = self.events(result, "nudged")
        self.assertEqual(nudge["kind"], "typed-reply")
        (post,) = self.sent_by_notifier()
        self.assertIn("option 2, which needs a typed reply", post["stdin"])
        self.assertEqual(post["argv"][post["argv"].index("--topic") + 1], record["topic"])
        self.assertIn("--to-owner", post["argv"])
        self.assertEqual(self.replied(record["identifier"]), [])
        self.run_notify()
        self.assertEqual(len(self.sent_by_notifier()), 1, "said once")

    def test_two_different_taps_ask_again_once_and_a_single_tap_then_counts(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.tap(record, 2)
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        (nudge,) = self.events(result, "nudged")
        self.assertEqual(nudge["kind"], "ask-again")
        (post,) = self.sent_by_notifier()
        self.assertIn("I counted neither", post["stdin"])
        self.run_notify()
        self.assertEqual(len(self.sent_by_notifier()), 1, "asked once")
        self.w.unreact(record["notified_msg_id"], OWNER_ID)
        self.tap(record, 1)
        done = self.run_notify()
        self.assertEqual([e["option"] for e in self.events(done, "replied")], [1])

    def test_an_item_without_low_stakes_options_never_takes_a_tap(self) -> None:
        record = self.item(low=None)
        self.tap(record, 1)
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        self.assertEqual(self.events(result, "nudged")[0]["kind"], "typed-reply")

    def test_an_urgent_item_never_takes_a_tap(self) -> None:
        record = self.file("Urgent one?", "Yes", "No", seat="CODEX", urgent=True)
        self.tap(record, 1)
        self.assertEqual(self.events(self.run_notify(), "replied"), [])

    def test_taps_are_ignored_when_the_owner_is_not_pinned(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.w.set_zulip(owner_user_id=0)
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        self.assertEqual(self.events(result, "taps-skipped")[0]["why"], "owner_unknown")

    def test_the_reaction_is_read_off_the_notice_even_when_the_notifier_itself_posted_it(self) -> None:
        record = self.pending(low="1")
        self.run_notify()  # CLAUDE posts the notice for BF-DIRECTOR
        notice = self.state_of(record["identifier"])["notified_msg_id"]
        self.w.react(notice, OWNER_ID, KEYCAP[1])
        result = self.run_notify()
        self.assertEqual([(e["kind"], e["ref"]) for e in self.events(result, "replied")], [("reaction", notice)])
        (note,) = [n for n in self.notes() if "BF-DIRECTOR" in n["argv"]]
        self.assertEqual(note["argv"][note["argv"].index("--to") + 1], "BF-DIRECTOR")

    def test_a_failed_read_records_nothing_and_says_so(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.w.set_zulip(read_fails=True)
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        self.assertEqual(self.events(result, "read-failed")[0]["id"], record["identifier"])
        self.w.set_zulip(read_fails=False)
        self.assertEqual(len(self.events(self.run_notify(), "replied")), 1)


class TypedReplyTests(RepliesCase):
    def test_a_message_that_verifies_is_recorded_and_the_note_is_sent(self) -> None:
        record = self.item()
        reply = self.w.add_message(record["topic"], "Option 2, please")
        result = self.run_notify()
        (replied,) = self.events(result, "replied")
        self.assertEqual((replied["kind"], replied["ref"]), ("msg", reply))
        (comment,) = self.replied(record["identifier"])
        self.assertIn("owner replied:  msg %d" % reply, comment)
        self.assertNotIn("please", comment)
        (note,) = self.notes()
        self.assertIn("message %d in this topic" % reply, note["stdin"])
        self.assertNotIn("Option 2", note["stdin"])
        verifies = [c for c in self.w.calls("agent-sync") if c["argv"][0] == "verify-owner-msg"]
        self.assertEqual(len(verifies), 1)
        self.assertEqual(verifies[0]["argv"][verifies[0]["argv"].index("--after") + 1], str(record["notified_msg_id"]))
        self.assertEqual(verifies[0]["argv"][verifies[0]["argv"].index("--topic") + 1], record["topic"])

    def test_messages_that_do_not_verify_are_ignored_and_not_asked_about_twice(self) -> None:
        record = self.item()
        self.w.add_message(record["topic"], "Option 1", client="ZulipPython")  # Jay's account through an API client
        self.w.add_message(record["topic"], "Option 1", sender_id=OTHER_PERSON)  # another person
        self.w.add_message(record["topic"], "Option 1", seat="CODEX")  # a bot
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        self.assertEqual(self.sent_by_notifier(), [])
        verifies = [c for c in self.w.calls("agent-sync") if c["argv"][0] == "verify-owner-msg"]
        self.assertEqual(len(verifies), 1, "only the owner's own id is asked about, and a bot or another person is not")
        self.run_notify()
        self.assertEqual(len([c for c in self.w.calls("agent-sync") if c["argv"][0] == "verify-owner-msg"]), 1, "a rejection is remembered")
        # a real message after them still counts
        good = self.w.add_message(record["topic"], "Option 1")
        self.assertEqual([e["ref"] for e in self.events(self.run_notify(), "replied")], [good])

    def test_a_burst_of_rejected_messages_never_hides_the_answer_behind_them(self) -> None:
        record = self.item()
        for _ in range(notifier.VERIFY_PER_TOPIC + 3):
            self.w.add_message(record["topic"], "noise", client="ZulipPython")
        good = self.w.add_message(record["topic"], "Option 1")
        first = self.run_notify()
        self.assertEqual(self.events(first, "replied"), [], "ten calls a run at the most")
        asked = len([c for c in self.w.calls("agent-sync") if c["argv"][0] == "verify-owner-msg"])
        self.assertEqual(asked, notifier.VERIFY_PER_TOPIC)
        second = self.run_notify()
        self.assertEqual([e["ref"] for e in self.events(second, "replied")], [good], "the rejected ten are remembered:  three more, then the answer")

    def test_the_first_verified_message_wins_and_a_pause_before_it_is_not_an_answer(self) -> None:
        record = self.item()
        self.w.add_message(record["topic"], "pause")
        first = self.w.add_message(record["topic"], "Option 1")
        self.w.add_message(record["topic"], "No wait, option 2")
        result = self.run_notify()
        self.assertEqual([e["ref"] for e in self.events(result, "replied")], [first])
        self.assertEqual(result["paused"], "owner")

    def test_a_message_in_another_topic_is_not_an_answer(self) -> None:
        record = self.item()
        self.w.add_message("OWNER AFC-999 something else", "Option 1")
        self.assertEqual(self.events(self.run_notify(), "replied"), [])
        self.assertEqual(self.state_of(record["identifier"])["state"], "notified")

    def test_a_verify_that_cannot_answer_leaves_the_item_open_for_the_next_run(self) -> None:
        record = self.item()
        reply = self.w.add_message(record["topic"], "Option 1")
        self.w.set_zulip(verify_fails=True)
        self.assertEqual(self.events(self.run_notify(), "replied"), [])
        self.w.set_zulip(verify_fails=False)
        self.assertEqual([e["ref"] for e in self.events(self.run_notify(), "replied")], [reply])

    def test_a_record_that_cannot_be_written_sends_no_note_and_is_tried_again(self) -> None:
        record = self.item()
        self.w.add_message(record["topic"], "Option 1")
        failing = self.w.env(FAKE_BOARD_FAIL="comment")
        result = self.w.notify(env=failing).json()
        self.assertEqual(self.events(result, "record-failed")[0]["id"], record["identifier"])
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.w.notifier_state()["notes"], {}, "no note is due for a record that was never written")
        self.assertEqual(len(self.events(self.run_notify(), "replied")), 1)
        self.assertEqual(len(self.notes()), 1)


class LinearReplyTests(RepliesCase):
    def setUp(self) -> None:
        super().setUp()
        self.w.knobs.values["OWNER_LINEAR_USER_ID"] = LINEAR_OWNER

    def comment(self, ident: str, text: str, *, who: str = LINEAR_OWNER, name: str = "Jay Wedgeworth", app: bool = False) -> str:
        return self.w.add_comment(ident, text, user_name=name, user_id=who, app=app)

    def test_a_comment_by_the_pinned_linear_account_is_recorded(self) -> None:
        record = self.item()
        cid = self.comment(record["identifier"], "Go with option 2.")
        result = self.run_notify()
        (replied,) = self.events(result, "replied")
        self.assertEqual((replied["kind"], replied["ref"]), ("comment", cid))
        (comment,) = self.replied(record["identifier"])
        self.assertIn("owner replied:  comment %s" % cid, comment)
        self.assertNotIn("option 2", comment)
        (note,) = self.notes()
        self.assertIn("Linear comment %s" % cid, note["stdin"])

    def test_nothing_else_in_linear_counts(self) -> None:
        record = self.item()
        ident = record["identifier"]
        self.comment(ident, "Option 1", who=LINEAR_OTHER, name="Somebody Else")  # another person
        self.comment(ident, "Option 1", who=LINEAR_APP, name="Fleet", app=True)  # an app
        self.comment(ident, "Option 1", who=LINEAR_OWNER, name="Jay Wedgeworth", app=True)  # the owner's id on an app user
        self.comment(ident, "[CLAUDE·abcd1234] Jay says option 1", who=LINEAR_OTHER, name="Somebody Else")  # a typed tag line
        self.w.add_comment(ident, "Option 1")  # an old import with no account
        self.comment(ident, "owner replied:  msg 5")  # the owner's account, but a record is not a reply
        result = self.run_notify()
        self.assertEqual(self.events(result, "replied"), [])
        self.assertEqual(self.replied(ident), [])

    def test_a_comment_answers_an_item_whose_topic_is_not_open_yet(self) -> None:
        record = self.pending()
        cid = self.comment(record["identifier"], "Option 1")
        result = self.run_notify()
        self.assertEqual([(e["id"], e["kind"], e["ref"]) for e in self.events(result, "replied")], [(record["identifier"], "comment", cid)])
        self.assertEqual(self.events(result, "notified"), [], "an answered item needs no notice")
        self.assertEqual(self.state_of(record["identifier"])["state"], "replied")

    def test_with_no_linear_id_pinned_no_comment_counts(self) -> None:
        self.w.knobs.values["OWNER_LINEAR_USER_ID"] = ""
        record = self.item()
        self.comment(record["identifier"], "Option 1")
        self.assertEqual(self.events(self.run_notify(), "replied"), [])

    def test_the_id_is_compared_without_case(self) -> None:
        self.w.knobs.values["OWNER_LINEAR_USER_ID"] = LINEAR_OWNER.upper()
        record = self.item()
        self.comment(record["identifier"], "Option 1")
        self.assertEqual(len(self.events(self.run_notify(), "replied")), 1)

    def test_a_linear_reply_counts_when_zulip_cannot_be_read(self) -> None:
        record = self.item()
        self.comment(record["identifier"], "Option 1")
        self.w.set_zulip(read_fails=True)
        result = self.run_notify()
        self.assertEqual(len(self.events(result, "replied")), 1)
        self.assertEqual(len(self.events(result, "read-failed")), 1)


class NoteTests(RepliesCase):
    def test_a_reply_to_claudes_own_item_is_left_in_a_file_and_nothing_is_posted(self) -> None:
        record = self.file("Mine?", "Yes", "No", seat="CLAUDE", low="1")
        self.w.react(record["notified_msg_id"], OWNER_ID, KEYCAP[1])
        result = self.run_notify()
        self.assertEqual(len(self.events(result, "replied")), 1)
        self.assertEqual(self.events(result, "pending-claude")[0]["id"], record["identifier"])
        self.assertEqual(self.notes(), [])
        path = self.w.notifier_dir / "pending-CLAUDE.json"
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        document = json.loads(path.read_text(encoding="utf-8"))
        (row,) = document["items"]
        self.assertEqual((row["identifier"], row["kind"], row["option"], row["id"]),
                         (record["identifier"], "reaction", 1, str(record["notified_msg_id"])))
        self.assertEqual(row["url"], record["url"])
        self.assertNotIn("Yes", json.dumps(document), "ids and links only")
        # it is dropped once the item is closed
        self.w.close(record["identifier"])
        self.run_notify()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"], [])

    def test_the_file_for_claude_is_written_even_while_paused(self) -> None:
        record = self.file("Mine?", "Yes", "No", seat="CLAUDE", low="1")
        self.w.react(record["notified_msg_id"], OWNER_ID, KEYCAP[1])
        self.w.knobs.values["NEEDS_OWNER_NOTIFY_PAUSED"] = True
        self.run_notify()
        self.assertEqual(len(json.loads((self.w.notifier_dir / "pending-CLAUDE.json").read_text(encoding="utf-8"))["items"]), 1)

    def test_a_note_that_cannot_go_out_stays_due_and_is_sent_when_it_can(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.w.set_zulip(no_bot_for=["CODEX"])
        failed = self.run_notify()
        self.assertEqual(self.events(failed, "note-failed")[0]["tries"], 1)
        self.assertEqual(len(self.replied(record["identifier"])), 1)
        self.assertIn(record["identifier"], self.w.notifier_state()["notes"])
        self.w.set_zulip(no_bot_for=[])
        self.assertEqual(self.events(self.run_notify(), "note-sent"), [], "not before the backoff")
        self.w.now += dt.timedelta(minutes=6)
        sent = self.run_notify()
        self.assertEqual(len(self.events(sent, "note-sent")), 1)
        self.assertEqual(self.w.notifier_state()["notes"], {})
        self.assertEqual(len(self.replied(record["identifier"])), 1, "the record was written once")

    def test_a_note_is_given_up_after_a_dozen_tries(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.w.set_zulip(no_bot_for=["CODEX"])
        for _ in range(notifier.NOTE_TRIES + 1):
            self.run_notify()
            self.w.now += dt.timedelta(hours=2)
        self.assertEqual(self.w.notifier_state()["notes"], {})
        self.assertEqual(self.notes(), [])

    def test_a_note_waits_for_the_caps(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.w.knobs.values["NEEDS_OWNER_CAPS"] = {"urgent_per_hour": 3, "per_hour": 0, "per_day": 30}
        result = self.run_notify()
        self.assertEqual(self.events(result, "deferred")[0]["cap"], "per_hour")
        self.assertEqual(self.notes(), [])
        self.w.knobs.values["NEEDS_OWNER_CAPS"] = {"urgent_per_hour": 3, "per_hour": 6, "per_day": 30}
        self.assertEqual(len(self.events(self.run_notify(), "note-sent")), 1)

    def test_a_note_is_dropped_when_the_item_is_closed_first(self) -> None:
        record = self.item()
        self.tap(record, 1)
        self.w.set_zulip(no_bot_for=["CODEX"])
        self.run_notify()
        self.w.close(record["identifier"])
        self.w.set_zulip(no_bot_for=[])
        self.w.now += dt.timedelta(minutes=10)
        result = self.run_notify()
        self.assertEqual(self.events(result, "note-dropped")[0]["id"], record["identifier"])
        self.assertEqual(self.notes(), [])

    def test_a_note_for_a_record_that_does_not_exist_is_never_sent(self) -> None:
        record = self.item()
        state_dir = self.w.notifier_dir
        state_dir.mkdir(parents=True)
        (state_dir / "notify-state.json").write_text(json.dumps({"schema": 1, "notes": {record["identifier"]: {
            "kind": "msg", "id": "5", "option": None, "since": 0, "tries": 0, "next": 0}}}))
        result = self.run_notify()
        self.assertEqual(self.events(result, "note-dropped")[0]["why"], "item_closed_or_unrecorded")
        self.assertEqual(self.notes(), [])


class RecordAuthorTests(RepliesCase):
    """A record counts only from who may write it, so a peer cannot hide an item or point the taps somewhere else."""

    def test_a_peer_cannot_mark_an_item_replied(self) -> None:
        record = self.item()
        self.w.add_comment(record["identifier"], "[CODEX·abcd1234] owner replied:  msg 5")
        self.assertEqual(self.state_of(record["identifier"])["state"], "notified")
        self.assertIsNone(self.state_of(record["identifier"])["replied"])

    def test_a_peer_cannot_name_the_message_the_taps_are_read_from(self) -> None:
        record = self.file("Mine?", "Yes", "No", seat="CLAUDE", low="1")
        self.w.add_comment(record["identifier"], "[CODEX·abcd1234] [needs-owner] notified msg 5\ntopic: OWNER AFC-1 x")
        row = self.state_of(record["identifier"])
        self.assertEqual(row["notified_msg_id"], record["notified_msg_id"])

    def test_the_filer_and_claude_may_write_notices_and_only_claude_replies(self) -> None:
        record = self.item(seat="CODEX")
        self.w.add_comment(record["identifier"], "[CLAUDE·notify01] owner replied:  msg 5")
        self.assertEqual(self.state_of(record["identifier"])["state"], "replied")
        pending = self.pending(seat="BF-DIRECTOR")
        self.run_notify()
        self.assertEqual(self.state_of(pending["identifier"])["state"], "notified", "CLAUDE's notice record is trusted")


class UnitTests(unittest.TestCase):
    def test_which_reactions_are_taps(self) -> None:
        def reaction(user, code, kind="unicode_emoji"):
            return {"user_id": user, "emoji_code": code, "reaction_type": kind}

        mixed = [reaction(7, "0031-20e3"), reaction(7, "0033-20e3"), reaction(8, "0002-20e3"), reaction(7, "1f44d"),
                 reaction(7, "0032-20e3", "realm_emoji"), {"user_id": 7}, "junk", reaction(7, "0035-20e3")]
        self.assertEqual(R.owner_taps(mixed, 7, 3), [1, 3])
        self.assertEqual(R.owner_taps(mixed, 7, 5), [1, 3, 5])
        for bad_owner in (None, 0, -1, True, "7"):
            self.assertEqual(R.owner_taps(mixed, bad_owner, 5), [])
        self.assertEqual(R.owner_taps(None, 7, 5), [])

    def test_judging_taps(self) -> None:
        self.assertEqual(R.judge_taps([], [1]).kind, "none")
        self.assertEqual(R.judge_taps([1], [1, 2]).kind, "answer")
        self.assertEqual(R.judge_taps([3], [1, 2]).kind, "risky")
        self.assertEqual(R.judge_taps([1, 2], [1, 2]).kind, "conflict")
        self.assertEqual(R.judge_taps([1, 3], [1]).kind, "conflict", "a tap on a risky option beside a low-stakes one is still two taps")

    def test_control_words(self) -> None:
        for text in ("pause", "Pause", "  PAUSE.  ", "pause!", "@**Claude** pause", "@_**Jay|7** resume", "resume"):
            self.assertIn(R.control_word(text), ("pause", "resume"), text)
        for text in ("pause the rollout", "I will pause", "", "please pause", "paused", "pause\nresume", None):
            self.assertIsNone(R.control_word(text), text)

    def test_the_notifiers_words_hold_no_filer_text(self) -> None:
        self.assertIn("1️⃣ and 2️⃣", R.ask_again_text([1, 2]))
        note = R.note_text("AFC-12", "https://linear.app/x/issue/AFC-12", "msg", "630000001")
        self.assertIn("AFC-12", note)
        self.assertIn("message 630000001 in this topic", note)
        self.assertEqual(R.pending_header("GB-PLUMBER", None), "**For GB-PLUMBER** (filed by an unknown seat, not checked by CLAUDE).")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
