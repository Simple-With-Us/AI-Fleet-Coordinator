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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
