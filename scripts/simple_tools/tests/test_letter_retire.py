"""Retiring a chat's letter and its title (the shared letter library, `platform_letter`).

When the assigner closes a chat's row because the platform says the chat is gone for good (an archived chat), the
chat's note must hold no letter any more and the title the platform shows must lose the letter it carried:  an
archived chat that keeps its letter goes on showing a prefix the assigner has since given to another chat, and two
chats show the same one.

The MiniMax row is the one with a native title writer and an archive flag of its own, so the title is checked where
it is really written (the session store).  The assigner is the registry API served in process (FakeRegistry)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from unittest import mock

from simple_tools import platform_letter as PL
from simple_tools import platform_letter_minimax as PM
from simple_tools.tests.test_platform_letter_one_table import Base

#: The runtime's own table, with the columns this row reads:  the archive flag, the hook's own title and the
#: timestamps the repair scan reads.
DDL = (
    "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
    " parent_session_id TEXT, archived INTEGER NOT NULL DEFAULT 0, extra_data_json TEXT,"
    " created_at_ms INTEGER, updated_at_ms INTEGER)"
)


class RetireTests(Base):
    """A chat the platform says is gone for good releases its letter and its title."""

    def setUp(self) -> None:
        super().setUp()
        self.store = PM.store_file(self.env())
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(DDL)
        conn.close()
        # The assigner keeps a row its chat touched lately (its own rule, 45 minutes).  These tests report a chat
        # the platform says is archived, so that rule is turned off here.
        self.fake.service.gone_report_live_minutes = 0
        self.fake.service.gone_report_max_share = 1.0  # two chats are reported at once here
        self.now = time.time()

    # ---- the session store ------------------------------------------------------------------

    def add(self, sid: str, *, title: str = "", kind: str = "conversation", archived: bool = False) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id, archived,"
                " extra_data_json, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, title, kind, None, 1 if archived else 0, None,
                 int(self.now * 1000), int(self.now * 1000)),
            )
        conn.close()

    def archive(self, sid: str) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE local_runtime_sessions SET archived = 1 WHERE session_id = ?", (sid,))
        conn.close()

    def name(self, sid: str, title: str) -> None:
        """A name a person gave the chat:  the runtime writes it over the one the hook put there."""
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE local_runtime_sessions SET title = ? WHERE session_id = ?", (title, sid))
        conn.close()

    def title_of(self, sid: str) -> str:
        conn = sqlite3.connect(self.store)
        row = conn.execute("SELECT title FROM local_runtime_sessions WHERE session_id = ?", (sid,)).fetchone()
        conn.close()
        return str(row[0] or "")

    # ---- the chat and the report ------------------------------------------------------------

    def start(self, sid: str, at: float | None = None) -> str:
        """The chat's own start:  the MiniMax row's hook output is plain text (the context the model reads)."""
        return self.run_hook(
            PM.SPEC,
            {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"},
            now=self.now if at is None else at,
        )

    def note(self, sid: str) -> dict:
        return self.note_of(PM.SEAT, sid)

    def note_of(self, seat: str, sid: str) -> dict:
        with open(PL.note_path(self.env(), seat, sid), encoding="utf-8") as fh:
            return json.load(fh)

    def report(self, at: float | None = None) -> dict:
        """The gone report the host's hooks make:  the chats the platform says are gone, and what the assigner did."""
        return PL.report_gone(
            PM.SPEC, self.env(), PM.SEAT, "mac", now=self.now if at is None else at, force=True
        )

    # ---- the retire -------------------------------------------------------------------------

    def test_a_retired_chat_holds_no_letter_any_more(self) -> None:
        self.add("m-0001", title="Fix Login")
        self.start("m-0001")
        note = self.note("m-0001")
        self.assertTrue(PL.lettered(note))
        label = note["label"]

        self.archive("m-0001")
        done = self.report()
        self.assertEqual([row["session_id"] for row in done["gone"]], ["m-0001"])

        note = self.note("m-0001")
        self.assertFalse(PL.lettered(note))  # the chat holds no letter
        self.assertEqual(note["state"], "closed")  # the state a closed registration carries
        self.assertEqual(note["label"], "")  # the letter itself is gone
        self.assertIn(label, note["labels"])  # the history of the letters it held is kept
        self.assertIn("gone_at", note)  # and the mark that says it was reported gone

    def test_the_title_of_a_retired_chat_loses_its_letter(self) -> None:
        self.add("m-0002", title="Fix Login")
        self.start("m-0002")
        label = self.note("m-0002")["label"]
        self.assertEqual(self.title_of("m-0002"), "%s: Fix Login" % label)

        self.archive("m-0002")
        self.report()
        self.assertEqual(self.title_of("m-0002"), "Fix Login")

    def test_a_title_a_person_chose_is_left_alone(self) -> None:
        self.add("m-0003", title="Fix Login")
        self.start("m-0003")
        label = self.note("m-0003")["label"]
        self.name("m-0003", "My Own Name")  # a person named the chat

        self.archive("m-0003")
        self.report()
        self.assertEqual(self.title_of("m-0003"), "My Own Name")  # their words stay
        self.assertFalse(PL.lettered(self.note("m-0003")))  # the letter is released all the same

    def test_a_title_a_person_chose_that_carries_a_letter_is_left_alone(self) -> None:
        self.add("m-0004", title="Fix Login")
        self.start("m-0004")
        label = self.note("m-0004")["label"]
        self.name("m-0004", "%s: My Own Name" % label)  # a person named the chat, with a letter in it

        self.archive("m-0004")
        self.report()
        self.assertEqual(self.title_of("m-0004"), "%s: My Own Name" % label)  # their words stay
        self.assertFalse(PL.lettered(self.note("m-0004")))  # the letter is released all the same

    def test_an_archived_chat_is_retired_without_waiting_for_the_quiet_floor(self) -> None:
        # The chat's hook ran five minutes ago, so it is not quiet:  the platform says it is archived, and an
        # archived chat is gone for good whatever its quiet time is.
        self.add("m-0005", title="Fix Login")
        self.start("m-0005", at=self.now - 300)
        label = self.note("m-0005")["label"]
        self.assertEqual(self.title_of("m-0005"), "%s: Fix Login" % label)

        self.archive("m-0005")
        done = self.report()
        self.assertEqual([row["session_id"] for row in done["gone"]], ["m-0005"])
        self.assertFalse(PL.lettered(self.note("m-0005")))
        self.assertEqual(self.title_of("m-0005"), "Fix Login")

    def test_a_merely_quiet_chat_that_was_not_archived_is_not_retired(self) -> None:
        # Quiet is not proof of gone:  the platform does not say this chat is archived, so it keeps its letter.
        self.add("m-0006", title="Fix Login")
        self.start("m-0006", at=self.now - 7200)
        label = self.note("m-0006")["label"]

        done = self.report()
        self.assertEqual(done["gone"], [])
        note = self.note("m-0006")
        self.assertTrue(PL.lettered(note))
        self.assertEqual(note["label"], label)
        self.assertEqual(self.title_of("m-0006"), "%s: Fix Login" % label)

    def test_a_retired_chat_is_not_offered_to_the_repair_pass(self) -> None:
        # The pass replays the start of a chat that shows no letter, and a replay would register the chat again:
        # an archived chat must not be lettered once more.
        self.add("m-0007", title="Fix Login", kind="task")
        self.start("m-0007")
        self.archive("m-0007")
        self.report()
        self.assertFalse(PL.lettered(self.note("m-0007")))
        self.assertNotIn("m-0007", PM.minimax_unlettered_sessions(self.env()))

    def test_a_retired_chat_that_is_alive_after_all_takes_a_letter_once_more(self) -> None:
        # The chat was reported gone and is alive after all:  its next prompt asks the assigner again, and it
        # holds a letter once more.
        self.add("m-0008", title="Fix Login")
        self.start("m-0008")
        self.archive("m-0008")
        self.report()
        self.assertFalse(PL.lettered(self.note("m-0008")))

        out = self.run_hook(
            PM.SPEC,
            {"hook_event_name": "UserPromptSubmit", "session_id": "m-0008", "prompt": "go on"},
            now=self.now + 60,
        )
        note = self.note("m-0008")
        self.assertTrue(PL.lettered(note))
        self.assertNotIn("gone_at", note)
        self.assertEqual(self.title_of("m-0008"), "%s: Fix Login" % note["label"])
        self.assertIn('"@**MiniMax** %s"' % note["label"], out)


    def test_a_note_the_old_retire_path_left_behind_is_retired_without_a_report(self) -> None:
        # The shape the retire path used to leave:  the chat was reported gone, and its note still holds the letter
        # the assigner has already freed.  It is retired here, and no report is made for it again.
        self.add("m-0009", title="Fix Login")
        self.start("m-0009")
        label = self.note("m-0009")["label"]
        path = PL.note_path(self.env(), PM.SEAT, "m-0009")
        with open(path, encoding="utf-8") as fh:
            note = json.load(fh)
        note["gone_at"] = self.now  # the old path marked the chat and left the letter in place
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(note, fh)

        done = self.report()
        self.assertEqual(done["gone"], [])  # the platform says nothing about this chat
        self.assertIn("m-0009", done["retired"])  # and the note is retired all the same
        note = self.note("m-0009")
        self.assertFalse(PL.lettered(note))
        self.assertEqual(note["label"], "")
        self.assertIn(label, note["labels"])
        self.assertEqual(self.title_of("m-0009"), "Fix Login")  # and the title loses the letter

    def test_a_title_write_that_fails_is_tried_again_when_the_chat_comes_back(self) -> None:
        # The platform would not take the title, so the stale prefix stays on the chat.  The letter is released all
        # the same, and the mark says the title still has to lose it:  the next event of the chat tries again.
        self.add("m-0010", title="Fix Login")
        self.start("m-0010")
        label = self.note("m-0010")["label"]
        self.archive("m-0010")

        os.chmod(self.store, 0o444)  # the store takes no writes
        try:
            self.report()
        finally:
            os.chmod(self.store, 0o644)
        note = self.note("m-0010")
        self.assertFalse(PL.lettered(note))  # the letter is released
        self.assertTrue(note.get("title_pending"))  # and the title still has to lose it
        self.assertEqual(self.title_of("m-0010"), "%s: Fix Login" % label)  # the stale prefix is still there

        # The chat is here again:  the pending write is tried, and a compact start registers nothing.
        self.run_hook(
            PM.SPEC,
            {"hook_event_name": "SessionStart", "session_id": "m-0010", "source": "compact"},
            now=self.now + 60,
        )
        note = self.note("m-0010")
        self.assertNotIn("title_pending", note)  # the pending write is done
        self.assertEqual(self.title_of("m-0010"), "Fix Login")  # and the stale prefix is gone

    def test_a_lettered_archive_beyond_the_newest_notes_still_reaches_the_adapter(self) -> None:
        # The archive path asks the adapter about every lettered chat of this host, so a chat that is not among the
        # newest notes is still offered and its letter is freed.
        self.add("m-0011", title="Fix Login")
        self.start("m-0011")
        self.add("m-0012", title="Fix Signup")
        self.start("m-0012")
        self.archive("m-0011")
        self.archive("m-0012")

        with mock.patch.object(PL, "GONE_SCAN_NOTES", 1):  # the quiet path reads only the newest note
            done = self.report()
        self.assertEqual(sorted(row["session_id"] for row in done["gone"]), ["m-0011", "m-0012"])
        self.assertFalse(PL.lettered(self.note("m-0011")))
        self.assertFalse(PL.lettered(self.note("m-0012")))

    def test_a_chat_that_runs_after_the_scan_is_not_retired(self) -> None:
        # The note the old retire path left behind is only retired when it is still the note the scan saw:  a chat
        # that ran in between is in use, not gone.
        self.add("m-0013", title="Fix Login")
        self.start("m-0013")
        path = PL.note_path(self.env(), PM.SEAT, "m-0013")
        with open(path, encoding="utf-8") as fh:
            note = json.load(fh)
        note["gone_at"] = self.now
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(note, fh)

        real = PL._lettered_chats

        def scan_then_use(env, seat, platform, where, now, **kwargs):
            rows = real(env, seat, platform, where, now, **kwargs)
            self.start("m-0013", at=self.now + 1)  # the chat runs between the scan and the read
            return rows

        with mock.patch.object(PL, "_lettered_chats", scan_then_use):
            done = self.report()
        self.assertNotIn("m-0013", done["retired"])  # so it is not retired
        self.assertTrue(PL.lettered(self.note("m-0013")))  # and it keeps its letter

    def test_a_title_a_person_chose_leaves_no_pending_title(self) -> None:
        # The writer refuses a title a person chose for ever, so nothing is left pending:  the mark would ask for a
        # write that can never succeed.
        self.add("m-0014", title="Fix Login")
        self.start("m-0014")
        self.name("m-0014", "My Own Name")
        self.archive("m-0014")

        self.report()
        note = self.note("m-0014")
        self.assertFalse(PL.lettered(note))
        self.assertNotIn("title_pending", note)

    def test_the_retired_list_is_answered_on_the_stamp_failure_path(self) -> None:
        self.add("m-0015", title="Fix Login")
        self.start("m-0015")
        with mock.patch.object(PL, "_save_json", lambda path, data: False):  # the stamp cannot be written
            done = self.report()
        self.assertIn("retired", done)
        self.assertEqual(done["retired"], [])

    def test_the_notes_are_scanned_once_and_the_adapter_call_is_bounded(self) -> None:
        self.add("m-0016", title="Fix Login")
        self.start("m-0016")
        self.add("m-0017", title="Fix Signup")
        self.start("m-0017")
        self.archive("m-0016")
        self.archive("m-0017")

        calls = []
        real = PL._lettered_chats

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        with mock.patch.object(PL, "_lettered_chats", counting), mock.patch.object(PL, "GONE_REPORT_IDS", 1):
            done = self.report()
        self.assertEqual(len(calls), 1)  # one scan for the whole sweep
        self.assertEqual(len(done["gone"]), 1)  # and the adapter is asked about a bounded number of chats

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
