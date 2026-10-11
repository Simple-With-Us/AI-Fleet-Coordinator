"""The fallback notifier (`needs-owner notify`):  notices, caps, the pause, the lock, the log.

Every test runs the real notifier in process against the fake `board` and `agent-sync` (built on the real parsers), a
fake clock and fake K1 knobs, in a temp HOME.  Jay is Zulip user 7 (the pinned owner);  CODEX, BF-DIRECTOR and the
Grok Bot personas are the filing seats.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import unittest

from needs_owner import text as T

from .world import World

Q = "Revive PR 761 or close it?"
UID_SCOPE = "needs-owner:"


def body_with_ref(uid: str, question: str = Q) -> str:
    return "**Decision needed** from X:  %s\n\nRef:  `%s`\n" % (question, uid)


class NotifierCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.w = World(self)

    # ---- filing ------------------------------------------------------------------------------
    def env_for(self, seat: str) -> dict:
        return self.w.env(AGENT_SEAT=seat)

    def file(self, question: str = Q, *options: str, seat: str = "CODEX", low: str | None = "1", urgent: bool = False,
             extra: tuple = ()) -> dict:
        flags = list(extra)
        if low and not urgent:
            flags += ["--low-stakes", low]
        if urgent:
            flags += ["--urgent"]
        done = self.w.file(question, *(options or ("Revive it", "Close it", "Ask Codex")), seat=seat, extra=tuple(flags),
                           env=self.env_for(seat))
        self.assertEqual(done.code, 0, done.err)
        return done.json()

    def pending(self, question: str = Q, *, seat: str = "BF-DIRECTOR", urgent: bool = False, low: str | None = "1") -> dict:
        """An item whose filer's bot could not post:  notify_pending."""
        self.w.set_zulip(no_credential=sorted(set(self.w.zulip().get("no_credential", [])) | {seat}))
        record = self.file(question, seat=seat, urgent=urgent, low=low)
        self.assertEqual(record["state"], "notify_pending", record)
        return record

    def run_notify(self, *extra: str) -> dict:
        ran = self.w.notify(*extra)
        self.assertEqual(ran.code, 0, ran.err + ran.out)
        return ran.json()

    # ---- looking ---------------------------------------------------------------------------
    def posts(self, seat: str | None = None, *, went_out: bool = False) -> list[dict]:
        return [c for c in self.w.calls("agent-sync") if c["argv"][0] in ("post", "reply") and (seat is None or c["seat"] == seat)
                and (not went_out or c.get("code") == 0)]

    def sent_by_notifier(self) -> list[dict]:
        """The posts the notifier made and that went out (as CLAUDE, session label needsown)."""
        return self.posts("CLAUDE", went_out=True)

    def events(self, result: dict, word: str | None = None) -> list[dict]:
        return [e for e in result["events"] if word is None or e["event"] == word]

    def state_of(self, ident: str) -> dict:
        (row,) = [r for r in self.w.run("list", "--all", "--json").json() if r["identifier"] == ident]
        return row

    def issue_status(self, ident: str) -> str:
        return self.w.issue(ident)["state"]["name"]

    def created_at(self, ident: str) -> dt.datetime:
        return dt.datetime.fromisoformat(self.w.issue(ident)["createdAt"].replace("Z", "+00:00"))


class FallbackTests(NotifierCase):
    def test_a_pending_item_is_posted_from_claude_for_its_seat(self) -> None:
        record = self.pending()
        ident = record["identifier"]
        result = self.run_notify()
        (sent,) = self.events(result, "notified")
        self.assertEqual(sent["id"], ident)
        (post,) = self.sent_by_notifier()
        self.assertEqual(post["argv"][:2], ["post", "--as"])
        self.assertIn("--session", post["argv"])
        self.assertEqual(post["argv"][post["argv"].index("--session") + 1], "needsown")
        self.assertIn("--to-owner", post["argv"])
        self.assertEqual(post["argv"][post["argv"].index("--topic") + 1], T.topic_for(ident, Q))
        self.assertTrue(post["stdin"].startswith("**For BF-DIRECTOR** (filed by BF-DIRECTOR, not checked by CLAUDE)."), post["stdin"][:90])
        self.assertIn("Revive it", post["stdin"])
        self.assertIn("Ref:  `%s`" % record["uid"], post["stdin"])
        row = self.state_of(ident)
        self.assertEqual((row["state"], row["notified_msg_id"] is not None), ("notified", True))
        comments = self.w.comments(ident)
        self.assertTrue(any(c.startswith("[CLAUDE") and "[needs-owner] notified msg" in c for c in comments), comments)
        # nothing was closed, restated or edited
        self.assertEqual(self.issue_status(ident), "Todo")

    def test_only_notify_pending_items_are_posted_and_only_once(self) -> None:
        done = self.file("Already announced?", seat="CODEX")
        self.assertEqual(done["state"], "notified")
        record = self.pending()
        first = self.run_notify()
        self.assertEqual([e["id"] for e in self.events(first, "notified")], [record["identifier"]])
        again = self.run_notify()
        self.assertEqual(self.events(again, "notified"), [])
        self.assertEqual(len(self.sent_by_notifier()), 1)

    def test_an_item_younger_than_two_minutes_waits(self) -> None:
        record = self.pending()
        stamps = [self.created_at(record["identifier"])] + [
            dt.datetime.fromisoformat(c["createdAt"].replace("Z", "+00:00")) for c in self.w.issue(record["identifier"])["comments"]]
        created = max(stamps)  # the pending mark is what makes the item ready to announce
        self.w.now = created + dt.timedelta(seconds=90)
        self.assertEqual(self.sent_by_notifier(), [])
        self.assertEqual(self.events(self.run_notify(), "notified"), [])
        self.assertEqual(self.sent_by_notifier(), [])
        self.w.now = created + dt.timedelta(seconds=125)
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)

    def test_a_post_the_filers_bot_landed_but_did_not_record_is_not_posted_again(self) -> None:
        record = self.pending(seat="CODEX")
        self.w.set_zulip(no_credential=[])
        landed = self.w.add_message(T.topic_for(record["identifier"], Q), body_with_ref(record["uid"]), seat="CODEX")
        result = self.run_notify()
        (sent,) = self.events(result, "notified")
        self.assertEqual(sent["msg"], landed)
        self.assertEqual(self.sent_by_notifier(), [], "CLAUDE posted nothing:  the filer's own bot had landed it")
        self.assertEqual(self.state_of(record["identifier"])["notified_msg_id"], landed)

    def test_a_copy_of_the_uid_from_another_seat_or_a_person_does_not_hide_the_item(self) -> None:
        record = self.pending(seat="BF-DIRECTOR")
        topic = T.topic_for(record["identifier"], Q)
        self.w.add_message(topic, body_with_ref(record["uid"]), seat="CURSOR")
        self.w.add_message(topic, body_with_ref(record["uid"]))
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)
        self.assertEqual(len(self.sent_by_notifier()), 1)

    def test_a_failed_post_stays_pending_and_backs_off(self) -> None:
        record = self.pending()
        self.w.set_zulip(post_mode="exit3")
        failed = self.run_notify()
        (event,) = self.events(failed, "notice-failed")
        self.assertEqual((event["id"], event["fails"]), (record["identifier"], 1))
        self.assertEqual(self.state_of(record["identifier"])["state"], "notify_pending")
        calls = len(self.posts())
        self.w.set_zulip(post_mode="ok")
        self.assertEqual(self.events(self.run_notify(), "notified"), [], "five minutes have not passed")
        self.assertEqual(len(self.posts()), calls)
        self.w.now += dt.timedelta(minutes=6)
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)

    def test_an_item_adopted_with_no_notify_is_held_until_it_is_announced_for_real(self) -> None:
        ident = self.w.seed_issue("Revive PR 761?", reporter="CLAUDE")
        held = self.w.run("adopt", ident, "--by", "CLAUDE", "--option", "Yes", "--option", "No", "--no-notify", "--json")
        self.assertEqual(held.code, 0, held.err)
        self.assertTrue(any("notify_pending" in c and "reason: held" in c for c in self.w.comments(ident)))
        self.assertEqual(self.events(self.run_notify(), "notified"), [])
        self.assertEqual(self.sent_by_notifier(), [])
        real = self.w.run("adopt", ident, "--by", "CLAUDE", "--option", "Yes", "--option", "No", "--json")
        self.assertEqual(real.json()["state"], "notified")

    def test_a_failed_post_after_a_held_mark_is_marked_again_so_the_fallback_takes_it(self) -> None:
        ident = self.w.seed_issue("Revive PR 761?", reporter="CLAUDE")
        self.w.run("adopt", ident, "--by", "CLAUDE", "--option", "Yes", "--option", "No", "--no-notify", "--json")
        self.w.set_zulip(no_credential=["CLAUDE"])
        self.w.run("adopt", ident, "--by", "CLAUDE", "--option", "Yes", "--option", "No", "--json")
        self.assertEqual(self.state_of(ident)["state"], "notify_pending")
        self.assertTrue(any("reason: no_credential" in c for c in self.w.comments(ident)))
        self.w.set_zulip(no_credential=[])
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)

    def test_an_item_that_fails_revalidation_is_skipped_with_a_reason(self) -> None:
        edited = self.pending("Edited after filing?")
        mail = self.pending("Mail me a copy?", seat="BF-PLUMBER")
        unadopted = self.w.seed_issue("Labelled by hand")
        world = self.w.board_state()
        node = world["issues"][edited["identifier"]]
        node["description"] = node["description"].replace("Option 2: Close it", "Option 2: Delete everything")
        mail_node = world["issues"][mail["identifier"]]
        mail_node["description"] = mail_node["description"].replace("Option 1 (low-stakes): Revive it", "Option 1 (low-stakes): mail jay@example.org")
        self.w.save_board(world)
        result = self.run_notify()
        skips = {e["id"]: e["why"] for e in self.events(result, "skip")}
        self.assertEqual(skips[edited["identifier"]], "edited_after_filing")
        self.assertEqual(skips[mail["identifier"]], "unsafe_text")
        self.assertNotIn(unadopted, skips, "a label with no block is not ours to announce, and not worth a line every five minutes")
        self.assertEqual(self.sent_by_notifier(), [])
        self.assertEqual(self.state_of(unadopted)["state"], "unadopted")

    def test_an_item_whose_filer_cannot_be_checked_is_skipped(self) -> None:
        record = self.pending()
        world = self.w.board_state()
        node = world["issues"][record["identifier"]]
        node["description"] = node["description"].replace("[BF-DIRECTOR\\]", "[CODEX\\]").replace("Reported by BF-DIRECTOR", "Reported by CODEX")
        self.w.save_board(world)
        result = self.run_notify()
        self.assertEqual([e["why"] for e in self.events(result, "skip")], ["seat_not_verified"])
        self.assertEqual(self.sent_by_notifier(), [])


class CapsTests(NotifierCase):
    def caps(self, **limits: int) -> None:
        self.w.knobs.values["NEEDS_OWNER_CAPS"] = dict({"urgent_per_hour": 3, "per_hour": 6, "per_day": 30}, **limits)

    def test_urgent_items_go_first_and_have_their_own_hourly_cap(self) -> None:
        self.caps(urgent_per_hour=1, per_hour=3, per_day=30)
        normal = [self.pending("Normal %d?" % n, seat="BF-DIRECTOR") for n in range(3)]
        urgent = [self.pending("Urgent %d?" % n, urgent=True, seat="BF-PLUMBER", low=None) for n in range(2)]
        result = self.run_notify()
        sent = [e["id"] for e in self.events(result, "notified")]
        self.assertEqual(sent[0], urgent[0]["identifier"], "urgent first")
        self.assertEqual(len(sent), 3, "three posts an hour in all")
        self.assertEqual(sent[1:], [normal[0]["identifier"], normal[1]["identifier"]])
        deferred = {(e["id"], e["cap"]) for e in self.events(result, "deferred")}
        self.assertIn((urgent[1]["identifier"], "urgent_per_hour"), deferred)
        self.assertIn((normal[2]["identifier"], "per_hour"), deferred)

    def test_the_caps_carry_over_between_runs_and_reset_with_the_clock(self) -> None:
        self.caps(urgent_per_hour=3, per_hour=2, per_day=3)
        items = [self.pending("Decision %d?" % n) for n in range(5)]
        first = self.run_notify()
        self.assertEqual(len(self.events(first, "notified")), 2)
        second = self.run_notify()
        self.assertEqual(self.events(second, "notified"), [], "the hour's two are used")
        self.assertEqual(second["sent_last_hour"], 2)
        self.w.now += dt.timedelta(minutes=61)
        third = self.run_notify()
        self.assertEqual(len(self.events(third, "notified")), 1, "the day's cap of 3 allows one more")
        self.w.now += dt.timedelta(minutes=61)
        self.assertEqual(self.events(self.run_notify(), "notified"), [])
        self.w.now += dt.timedelta(hours=25)
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 2)
        self.assertEqual(len(self.sent_by_notifier()), 5)
        self.assertEqual(len(items), 5)

    def test_a_cap_of_zero_sends_nothing_and_a_bad_knob_takes_the_default(self) -> None:
        self.pending()
        self.caps(per_hour=0)
        self.assertEqual(self.events(self.run_notify(), "notified"), [])
        self.w.knobs.values["NEEDS_OWNER_CAPS"] = {"per_hour": "lots", "per_day": -1, "urgent_per_hour": True}
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1, "the defaults 3, 6 and 30 apply")
        self.w.knobs.values["NEEDS_OWNER_CAPS"] = "not a mapping"
        self.pending("A second one?", seat="BF-PLUMBER")
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)


class PauseTests(NotifierCase):
    def test_the_pause_knob_stops_every_post(self) -> None:
        self.pending()
        self.w.knobs.values["NEEDS_OWNER_NOTIFY_PAUSED"] = True
        result = self.run_notify()
        self.assertEqual(result["paused"], "knob")
        self.assertEqual(self.sent_by_notifier(), [])
        self.assertEqual(self.events(result, "notified"), [])
        self.w.knobs.values["NEEDS_OWNER_NOTIFY_PAUSED"] = False
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)

    def test_jays_verified_pause_in_an_owner_topic_pauses_for_a_day_or_until_resume(self) -> None:
        open_item = self.file("Which way?", seat="CODEX")
        topic = open_item["topic"]
        self.pending("Waiting to be announced?")
        self.w.add_message(topic, "pause")
        result = self.run_notify()
        self.assertEqual(result["paused"], "owner")
        self.assertEqual([e["event"] for e in result["events"] if e["event"] in ("pause", "notified")], ["pause"])
        self.assertEqual(self.sent_by_notifier(), [])
        # not an answer:  the item is still waiting on Jay
        self.assertEqual(self.state_of(open_item["identifier"])["state"], "notified")
        self.assertEqual(self.run_notify()["paused"], "owner")
        self.w.add_message(topic, "resume")
        resumed = self.run_notify()
        self.assertIsNone(resumed["paused"])
        self.assertEqual(len(self.events(resumed, "notified")), 1)

    def test_a_pause_lapses_after_a_day(self) -> None:
        open_item = self.file("Which way?", seat="CODEX")
        self.w.add_message(open_item["topic"], "Pause.")
        self.assertEqual(self.run_notify()["paused"], "owner")
        self.w.now += dt.timedelta(hours=23)
        self.assertEqual(self.run_notify()["paused"], "owner")
        self.w.now += dt.timedelta(hours=2)
        self.assertIsNone(self.run_notify()["paused"])

    def test_only_the_owner_on_a_human_client_can_pause(self) -> None:
        open_item = self.file("Which way?", seat="CODEX")
        topic = open_item["topic"]
        self.w.add_message(topic, "pause", client="ZulipPython")  # Jay's account through an API client
        self.w.add_message(topic, "pause", sender_id=8)  # another person
        self.w.add_message(topic, "pause", seat="CODEX")  # a bot
        self.w.add_message(topic, "I would pause here, because the lane is busy")  # a sentence, and an answer
        self.assertIsNone(self.run_notify()["paused"])

    def test_pause_does_not_stop_recording_a_reply_but_holds_its_note(self) -> None:
        open_item = self.file("Which way?", seat="CODEX")
        self.w.knobs.values["NEEDS_OWNER_NOTIFY_PAUSED"] = True
        self.w.add_message(open_item["topic"], "Option 2")
        result = self.run_notify()
        self.assertEqual(len(self.events(result, "replied")), 1)
        self.assertEqual(self.sent_by_notifier(), [], "no post while paused")
        self.assertEqual(self.state_of(open_item["identifier"])["state"], "replied")
        self.w.knobs.values["NEEDS_OWNER_NOTIFY_PAUSED"] = False
        later = self.run_notify()
        self.assertEqual(len(self.events(later, "note-sent")), 1)
        self.assertEqual(len([c for c in self.w.comments(open_item["identifier"]) if "owner replied:" in c]), 1)

    def test_paused_notifier_also_files_no_grok_bot_mirror(self) -> None:
        self.w.knobs.values["NEEDS_OWNER_NOTIFY_PAUSED"] = True
        self.w.add_message("OWNER GB-PLUMBER needs a key", "x", seat="GB-PLUMBER")
        self.run_notify()
        self.assertEqual([c for c in self.w.calls("agent-sync") if c["argv"][0] == "topics"], [])


class RunTests(NotifierCase):
    def test_a_failed_board_read_sends_nothing_and_exits_zero(self) -> None:
        self.pending()
        ran = self.w.notify(env=self.w.env(FAKE_BOARD_FAIL="list"))
        self.assertEqual(ran.code, 0, ran.err)
        result = ran.json()
        self.assertEqual([e["event"] for e in result["events"]], ["start", "board-read-failed"])
        self.assertEqual(self.sent_by_notifier(), [])

    def test_a_missing_board_is_a_failed_read_too(self) -> None:
        self.w.remove_tool("board")
        ran = self.w.notify()
        self.assertEqual(ran.code, 0, ran.err)
        self.assertIn("board-read-failed", [e["event"] for e in ran.json()["events"]])

    def test_a_refused_or_broken_knob_stops_the_run_before_anything_is_sent(self) -> None:
        self.pending()
        before = len(self.w.calls())
        self.w.knobs.fail = RuntimeError("the knobs are down")
        ran = self.w.notify()
        self.assertEqual(ran.code, 1)
        self.assertIn("nothing was sent", ran.err)
        self.assertNotIn("the knobs are down", ran.err, "an exception's words are never repeated")
        self.assertEqual(self.sent_by_notifier(), [])
        self.assertEqual(len(self.w.calls()), before, "not even a board read")

    def test_only_claude_runs_it(self) -> None:
        ran = self.w.notify(env=self.env_for("CODEX"))
        self.assertEqual(ran.code, 2)
        self.assertIn("does not match", ran.err + "only CLAUDE runs")
        ran = self.w.run("notify", "--by", "CODEX", "--json")
        self.assertEqual(ran.code, 2)

    def test_dry_run_changes_nothing_anywhere(self) -> None:
        record = self.pending()
        before_board = len(self.w.calls("board"))
        result = self.run_notify("--dry-run")
        self.assertEqual([e["id"] for e in self.events(result, "would-notify")], [record["identifier"]])
        self.assertEqual(self.sent_by_notifier(), [])
        self.assertEqual([c for c in self.w.calls("board")[before_board:] if c["argv"][0] in ("comment", "file", "label", "status")], [])
        self.assertFalse(self.w.notifier_dir.exists(), "no state, no lock")

    def test_the_state_files_are_private_and_hold_no_words(self) -> None:
        record = self.pending()
        self.run_notify()
        directory = self.w.notifier_dir
        self.assertEqual(oct(os.stat(directory).st_mode & 0o777), "0o700")
        path = directory / "notify-state.json"
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        text = path.read_text(encoding="utf-8")
        for word in ("Revive", "Close it", Q):
            self.assertNotIn(word, text)
        self.assertIn(record["identifier"], text)
        self.assertFalse((directory / "notify.lock").exists(), "the lock is released")

    def test_the_log_is_ids_and_words_never_a_question_an_option_or_a_message(self) -> None:
        record = self.pending()
        open_item = self.file("What colour should the badge be?", "Teal", "Plum", seat="CODEX")
        self.w.add_message(open_item["topic"], "Option 2, secretword")
        ran = self.w.run("notify", env=None)  # the human form:  one line per event
        self.assertEqual(ran.code, 0, ran.err)
        for word in ("Revive", "Close it", Q, "badge", "Teal", "Plum", "secretword"):
            self.assertNotIn(word, ran.out + ran.err)
        self.assertIn(record["identifier"], ran.out)
        self.assertRegex(ran.out.splitlines()[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00  start")
        self.assertEqual(self.w.leftover_temp_files(), [])

    def test_a_second_run_at_the_same_time_does_nothing(self) -> None:
        self.pending()
        directory = self.w.notifier_dir
        directory.mkdir(parents=True)
        (directory / "notify.lock").write_text("%d\n" % os.getpid())
        result = self.run_notify()
        self.assertEqual([e["event"] for e in result["events"]], ["locked"])
        self.assertEqual(self.sent_by_notifier(), [])
        self.assertTrue((directory / "notify.lock").exists(), "someone else's lock is left alone")

    def test_a_stale_lock_is_taken_over(self) -> None:
        self.pending()
        directory = self.w.notifier_dir
        directory.mkdir(parents=True)
        lock = directory / "notify.lock"
        lock.write_text("%d\n" % os.getpid())
        old = time.time() - 31 * 60
        os.utime(lock, (old, old))
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)
        self.assertFalse(lock.exists())

    def test_a_lock_whose_process_is_gone_is_taken_over(self) -> None:
        self.pending()
        directory = self.w.notifier_dir
        directory.mkdir(parents=True)
        (directory / "notify.lock").write_text("999999\n")
        self.assertEqual(len(self.events(self.run_notify(), "notified")), 1)

    def test_a_damaged_state_file_starts_fresh_and_says_so(self) -> None:
        self.pending()
        directory = self.w.notifier_dir
        directory.mkdir(parents=True)
        (directory / "notify-state.json").write_text("{not json")
        result = self.run_notify()
        self.assertIn("state-reset", [e["event"] for e in result["events"]])
        self.assertEqual(len(self.events(result, "notified")), 1)

    def test_stale_knobs_are_named_in_the_start_event(self) -> None:
        self.w.knobs.stale_copy = True
        result = self.run_notify()
        self.assertTrue(result["stale_knobs"])
        self.assertEqual(self.events(result, "start")[0]["stale_knobs"], True)

    def test_a_whoami_failure_still_runs_the_fallback(self) -> None:
        self.pending()
        self.w.set_zulip(whoami_fails=True)
        result = self.run_notify()
        self.assertIn("whoami-failed", [e["event"] for e in result["events"]])
        self.assertEqual(len(self.events(result, "notified")), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
