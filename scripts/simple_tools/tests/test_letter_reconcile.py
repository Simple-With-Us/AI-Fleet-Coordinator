"""The letter reconcile pass (the shared letter library, `platform_letter`).

The retire path covers a chat the platform says is gone for good.  It does not cover a chat whose registration the
assigner closed by its own stale sweep:  that chat never enters the gone path,  so its title keeps the letter prefix
until the chat has its next event,  and the sidebar goes on showing a letter the registry has already given to
another chat.

This pass asks the assigner about each of this host's lettered chats,  by the chat's own identity,  and retires the
ones the assigner no longer holds.  It is read-only against the registry.

The MiniMax row is the one with a native title writer,  so the title is checked where it is really written (the
session store).  The assigner is the registry API served in process (FakeRegistry)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from simple_tools import listener_registry as LREG
from simple_tools import platform_letter as PL
from simple_tools import platform_letter_minimax as PM
from simple_tools import registry_api
from simple_tools.tests.test_platform_letter_one_table import Base

#: The runtime's own table, with the columns this row reads:  the archive flag, the hook's own title and the
#: timestamps the repair scan reads.
DDL = (
    "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
    " parent_session_id TEXT, archived INTEGER NOT NULL DEFAULT 0, extra_data_json TEXT,"
    " created_at_ms INTEGER, updated_at_ms INTEGER)"
)


class ReconcileTests(Base):
    """A chat whose letter the assigner no longer holds loses its prefix,  and nothing else does."""

    def setUp(self) -> None:
        super().setUp()
        self.store = PM.store_file(self.env())
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(DDL)
        conn.close()
        self.now = time.time()
        # The chats of these tests are quiet:  the pass only offers a chat that has been quiet for an hour,  the
        # same floor the gone sweep uses.
        self.old = self.now - 7200.0
        # The assigner's own sweep,  with its own window and its own share turned to what these tests need:  a row
        # that is a minute old and is replaced by a newer one is the row the sweep closes.
        self.sweeper = LREG.Sweeper(
            self.fake.service, interval_minutes=15, stale_after_hours=1.0 / 60.0, max_share=1.0
        )

    # ---- the platform's own store ---------------------------------------------------------

    def add(self, sid: str, *, title: str = "") -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id, archived,"
                " extra_data_json, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, title, "conversation", None, 0, None, int(self.now * 1000), int(self.now * 1000)),
            )
        conn.close()

    def name(self, sid: str, title: str) -> None:
        """A name a person gave the chat:  the runtime writes it over the one the hook put there."""
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE local_runtime_sessions SET title = ? WHERE session_id = ?", (title, sid))
        conn.close()

    def title_of(self, sid: str) -> str:
        conn = sqlite3.connect(self.store)
        row = conn.execute(
            "SELECT title FROM local_runtime_sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        conn.close()
        return str(row[0] or "")

    # ---- the chat and the assigner -------------------------------------------------------

    def start(self, sid: str, at: float | None = None) -> str:
        """The chat's own start:  the MiniMax row's hook output is plain text (the context the model reads)."""
        return self.run_hook(
            PM.SPEC,
            {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"},
            now=self.now if at is None else at,
        )

    def note(self, sid: str) -> dict:
        with open(PL.note_path(self.env(), PM.SEAT, sid), encoding="utf-8") as fh:
            return json.load(fh)

    def age(self, sid: str, when: float) -> None:
        """Make the assigner's row for this chat look old:  the sweep's own test is a row's age,  and a row that
        is old and replaced is the row the sweep closes."""
        stamp = datetime.fromtimestamp(when, tz=timezone.utc).isoformat()
        conn = sqlite3.connect(self.fake.db)
        with conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ?, registered_at = ? WHERE native_session_id = ?",
                (stamp, stamp, sid),
            )
        conn.close()

    def issue_letter(self, sid: str, letter: str) -> None:
        """Hand this chat the letter named, the way a later registration does when the space runs out:  the
        chat's own letter is retired and the named one is issued to it as the next generation."""
        stamp = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.fake.db)
        with conn:
            row = conn.execute(
                "SELECT record_id FROM sessions WHERE native_session_id = ?", (sid,)
            ).fetchone()
            record_id = str(row[0])
            conn.execute(
                "UPDATE alias_assignments SET retired_at = ? WHERE record_id = ? AND retired_at IS NULL",
                (stamp, record_id),
            )
            generation = conn.execute(
                "SELECT MAX(generation) FROM alias_assignments WHERE platform = ? AND letter = ?",
                (PM.SPEC.platform, letter),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (PM.SPEC.platform, letter, int(generation or 0) + 1, record_id, stamp),
            )
        conn.close()

    def show_letter(self, sid: str, letter: str, title: str = "") -> None:
        """The chat's own note and the platform's title carry the letter, the way the hook wrote them."""
        path = PL.note_path(self.env(), PM.SEAT, sid)
        with open(path, encoding="utf-8") as fh:
            note = json.load(fh)
        note["label"] = letter
        note["labels"] = [letter]
        note["shown"] = "%s: %s" % (letter, title or "Fix Billing")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(note, fh, sort_keys=True)
        self.name(sid, "%s: %s" % (letter, title or "Fix Billing"))

    def sweep(self) -> dict:
        """The assigner's own stale sweep:  it closes the rows that are old and replaced."""
        return self.sweeper.sweep(PM.SPEC.platform, PM.SEAT)

    def assignments(self) -> list[tuple]:
        conn = sqlite3.connect(self.fake.db)
        rows = conn.execute(
            "SELECT platform, letter, generation, record_id, retired_at FROM alias_assignments"
            " ORDER BY platform, letter, generation"
        ).fetchall()
        conn.close()
        return [tuple(row) for row in rows]

    def sessions(self) -> list[tuple]:
        conn = sqlite3.connect(self.fake.db)
        rows = conn.execute(
            "SELECT record_id, native_session_id, closed_at FROM sessions ORDER BY record_id"
        ).fetchall()
        conn.close()
        return [tuple(row) for row in rows]

    def reconcile(self, at: float | None = None) -> dict:
        return PL.reconcile_letters(
            PM.SPEC, self.env(), PM.SEAT, "mac", now=self.now if at is None else at, force=True
        )

    # ---- the pass -------------------------------------------------------------------------

    def test_a_chat_the_sweep_closed_loses_its_letter_prefix(self) -> None:
        self.add("m-0001", title="Fix Login")
        self.start("m-0001", at=self.old)
        self.add("m-0002", title="Fix Signup")
        self.start("m-0002", at=self.old)
        old_label = self.note("m-0001")["label"]
        new_label = self.note("m-0002")["label"]
        self.assertNotEqual(old_label, new_label)
        self.assertEqual(self.title_of("m-0001"), "%s: Fix Login" % old_label)

        # The assigner's own sweep closes the old row,  and the chat never enters the gone path.
        self.age("m-0001", self.old)
        ended = self.sweep()
        self.assertEqual([row["native_session_id"] for row in ended["ended"]], ["m-0001"])

        done = self.reconcile()
        self.assertIn("m-0001", done["retired"])
        self.assertEqual(self.title_of("m-0001"), "Fix Login")
        note = self.note("m-0001")
        self.assertFalse(PL.lettered(note))  # the chat holds no letter
        self.assertEqual(note["state"], "closed")  # the state a closed registration carries
        self.assertIn(old_label, note["labels"])  # the history of the letters it held is kept

    def test_a_chat_whose_letter_is_still_live_is_left_alone(self) -> None:
        self.add("m-0003", title="Fix Login")
        self.start("m-0003", at=self.old)
        label = self.note("m-0003")["label"]

        done = self.reconcile()
        self.assertEqual(done["retired"], [])
        self.assertEqual(self.title_of("m-0003"), "%s: Fix Login" % label)
        self.assertTrue(PL.lettered(self.note("m-0003")))

    def test_a_title_a_person_chose_is_left_alone(self) -> None:
        self.add("m-0004", title="Fix Login")
        self.start("m-0004", at=self.old)
        self.name("m-0004", "Fix Login")

        done = self.reconcile()
        self.assertEqual(done["retired"], [])
        self.assertEqual(self.title_of("m-0004"), "Fix Login")

    def test_the_chat_that_holds_a_reused_letter_is_left_alone(self) -> None:
        self.add("m-0005", title="Fix Login")
        self.start("m-0005", at=self.old)
        first = self.note("m-0005")["label"]
        self.add("m-0006", title="Fix Signup")
        self.start("m-0006", at=self.old)
        self.age("m-0005", self.old)
        ended = self.sweep()
        self.assertEqual([row["native_session_id"] for row in ended["ended"]], ["m-0005"])

        # The letter the sweep freed is handed to a later chat,  so two chats show the same prefix and only one
        # of them holds it.
        self.add("m-0007", title="Fix Billing")
        self.start("m-0007", at=self.old)
        self.issue_letter("m-0007", first)
        self.show_letter("m-0007", first)
        self.assertEqual(self.title_of("m-0005"), "%s: Fix Login" % first)
        self.assertEqual(self.title_of("m-0007"), "%s: Fix Billing" % first)

        done = self.reconcile()
        self.assertIn("m-0005", done["retired"])
        self.assertNotIn("m-0007", done["retired"])
        self.assertEqual(self.title_of("m-0005"), "Fix Login")
        self.assertEqual(self.title_of("m-0007"), "%s: Fix Billing" % first)
        self.assertTrue(PL.lettered(self.note("m-0007")))

    def test_a_letter_under_another_seat_is_still_a_letter_this_chat_does_not_hold(self) -> None:
        # The assigner refuses to name the holder of a letter that is live under another seat,  and that refusal
        # is an answer:  the letter is live and it is not ours.
        with mock.patch.object(PL, "_call_api", return_value={"error": "forbidden"}):
            self.assertEqual(PL._letter_holder(self.env(), PM.SPEC, "AA"), ("another-seat", ""))
        # A call that was not answered at all is no answer at all.
        with mock.patch.object(PL, "_call_api", return_value={"error": "timeout"}):
            self.assertEqual(PL._letter_holder(self.env(), PM.SPEC, "AA"), ("unknown", ""))

    def test_a_chat_the_registry_relabelled_in_place_is_not_retired(self) -> None:
        # The registry relabels a chat in place:  the same record, a new letter, and the note still remembers the
        # old one.  The chat holds its new letter,  so it is not retired for a prefix its own title happens to
        # carry:  the letter to ask about is the one the registry answers with.
        self.add("m-0013", title="Fix Login")
        self.start("m-0013", at=self.old)
        first = self.note("m-0013")["label"]
        self.issue_letter("m-0013", "BB")
        self.assertEqual(self.note("m-0013")["label"], first)  # the note is behind
        self.assertEqual(self.title_of("m-0013"), "%s: Fix Login" % first)

        done = self.reconcile()
        self.assertEqual(done["retired"], [])
        self.assertEqual(self.title_of("m-0013"), "%s: Fix Login" % first)
        self.assertTrue(PL.lettered(self.note("m-0013")))

    def test_a_chat_start_never_runs_the_pass_itself(self) -> None:
        # The pass asks the assigner about every lettered chat of this host,  so it must not run inside a chat's
        # start:  a hook has a budget and a run of API calls does not fit in one.
        self.add("m-0014", title="Fix Login")
        self.start("m-0014", at=self.old)
        stamp = PL.reconcile_stamp_path(self.env(), PM.SEAT, PM.SPEC.platform, "mac")
        self.assertFalse(os.path.exists(stamp), "the pass must not run inside a chat's start")

    def test_the_detached_run_is_this_checkouts_own_reconcile_verb(self) -> None:
        argv = PL.reconcile_command(PM.SPEC.platform, "mac")
        self.assertIn("reconcile", argv)
        self.assertIn("--platform", argv)
        self.assertIn("--location", argv)
        self.assertIn("mac", argv)

    def test_one_failing_chat_does_not_stop_the_pass(self) -> None:
        # Two stale chats,  and one of them makes the assigner call fail.  The other is still retired:  one
        # chat's failure never stops the pass,  and the failure is recorded with its session id and the
        # exception's type alone.
        self.add("m-0016", title="Fix Login")
        self.start("m-0016", at=self.old)
        self.add("m-0017", title="Fix Signup")
        self.start("m-0017", at=self.old)
        self.add("m-0018", title="Fix Billing")
        self.start("m-0018", at=self.old)
        for sid in ("m-0016", "m-0017"):
            self.age(sid, self.old)
        ended = self.sweep()
        self.assertEqual([row["native_session_id"] for row in ended["ended"]], ["m-0016", "m-0017"])

        original = PL._reconcile_answer

        def flaky(env, spec, where, sid, timeout=0):
            if sid == "m-0016":
                raise RuntimeError("the assigner call failed")
            return original(env, spec, where, sid, timeout=timeout)

        with mock.patch.object(PL, "_reconcile_answer", flaky):
            done = self.reconcile()
        self.assertNotIn("m-0016", done["retired"])
        self.assertIn("m-0017", done["retired"])
        self.assertEqual([row["sid"] for row in done["failures"]], ["m-0016"])
        self.assertEqual(done["failures"][0]["error"], "RuntimeError")

    def test_a_title_write_that_is_still_owed_is_retried(self) -> None:
        # A retire path that could not write the title leaves the mark that says so,  and the title still carries
        # the prefix.  The pass retries that write,  so a chat whose letter is already gone does not wait for the
        # gone sweep to notice it.
        self.add("m-0019", title="Fix Login")
        self.start("m-0019", at=self.old)
        label = self.note("m-0019")["label"]
        path = PL.note_path(self.env(), PM.SEAT, "m-0019")
        with open(path, encoding="utf-8") as fh:
            note = json.load(fh)
        note.update(state="closed", label="", labels=[label], title_pending=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(note, fh, sort_keys=True)
        self.assertEqual(self.title_of("m-0019"), "%s: Fix Login" % label)

        done = self.reconcile()
        self.assertEqual(self.title_of("m-0019"), "Fix Login")
        self.assertNotIn("title_pending", self.note("m-0019"))
        # The write that landed is a retire,  and the callers read this list.
        self.assertIn("m-0019", done["retired"])

    def forget_letters(self, sid: str) -> None:
        """Take the record's letters out of the assigner,  so it holds the chat and no letter for it."""
        conn = sqlite3.connect(self.fake.db)
        with conn:
            row = conn.execute(
                "SELECT record_id FROM sessions WHERE native_session_id = ?", (sid,)
            ).fetchone()
            conn.execute("DELETE FROM alias_assignments WHERE record_id = ?", (str(row[0]),))
        conn.close()

    def test_a_label_the_assigner_did_not_name_is_not_taken_from_the_history(self) -> None:
        # The assigner answers with the letters the record holds,  and with the one it holds now.  A label that is
        # missing,  or that does not look like one,  is no label at all:  the pass must not pick a letter out of the
        # history on its own,  for the answer about that letter is about another chat's letter.
        with mock.patch.object(
            PL,
            "_call_api",
            return_value={"record_id": "rec-1", "letters": ["YY", "AB"], "label": ""},
        ):
            answer = PL._reconcile_answer(self.env(), PM.SPEC, "mac", "m-0001")
        self.assertEqual(answer["label"], "")
        self.assertFalse(answer["holds_no_letter"])

    def test_a_chat_the_assigner_holds_without_a_letter_loses_its_prefix(self) -> None:
        # The assigner holds the chat and no letter for it:  the prefix in its title belongs to nobody,  and the
        # pass takes it off.
        self.add("m-0020", title="Fix Login")
        self.start("m-0020", at=self.old)
        label = self.note("m-0020")["label"]
        self.assertEqual(self.title_of("m-0020"), "%s: Fix Login" % label)
        self.forget_letters("m-0020")

        done = self.reconcile()
        self.assertIn("m-0020", done["retired"])
        self.assertEqual(self.title_of("m-0020"), "Fix Login")
        self.assertFalse(PL.lettered(self.note("m-0020")))

    def test_an_answer_with_no_usable_label_leaves_the_title_alone(self) -> None:
        # The assigner holds the chat and its letter history,  but names no label this pass can trust.  That is no
        # evidence,  and the title is left exactly as it is.
        self.add("m-0021", title="Fix Login")
        self.start("m-0021", at=self.old)
        label = self.note("m-0021")["label"]
        answer = {
            "registered": True,
            "record_id": "rec-1",
            "letters": [label],
            "label": "",
            "holds_no_letter": False,
        }
        with mock.patch.object(PL, "_reconcile_answer", return_value=answer):
            done = self.reconcile()
        self.assertEqual(done["retired"], [])
        self.assertEqual(self.title_of("m-0021"), "%s: Fix Login" % label)
        self.assertTrue(PL.lettered(self.note("m-0021")))

    def test_an_assigner_that_does_not_answer_changes_nothing(self) -> None:
        self.add("m-0008", title="Fix Login")
        self.start("m-0008", at=self.old)
        label = self.note("m-0008")["label"]
        env = self.env()
        env.pop(registry_api.URL_ENV, None)  # no registry URL:  the call answers not_configured

        done = PL.reconcile_letters(PM.SPEC, env, PM.SEAT, "mac", now=self.now, force=True)
        self.assertEqual(done["retired"], [])
        self.assertEqual(self.title_of("m-0008"), "%s: Fix Login" % label)
        self.assertTrue(PL.lettered(self.note("m-0008")))

    def test_a_chat_that_ran_after_the_scan_is_left_alone(self) -> None:
        self.add("m-0009", title="Fix Login")
        self.start("m-0009", at=self.old)
        original = PL._lettered_chats
        state = {"again": True}

        def scan_then_use(env, seat, platform, where, now, **kwargs):
            rows = original(env, seat, platform, where, now, **kwargs)
            if state["again"]:
                state["again"] = False
                self.start("m-0009", at=self.now)  # the chat runs after the scan
            return rows

        with mock.patch.object(PL, "_lettered_chats", scan_then_use):
            done = self.reconcile()
        self.assertEqual(done["retired"], [])
        self.assertTrue(PL.lettered(self.note("m-0009")))

    def test_the_registry_is_not_changed_by_the_pass(self) -> None:
        self.add("m-0010", title="Fix Login")
        self.start("m-0010", at=self.old)
        self.add("m-0011", title="Fix Signup")
        self.start("m-0011", at=self.old)
        self.age("m-0010", self.old)
        self.sweep()
        assignments_before = self.assignments()
        sessions_before = self.sessions()

        done = self.reconcile()
        self.assertIn("m-0010", done["retired"])
        # The pass reads the assigner and changes nothing there:  no row,  no letter,  no assignment.
        self.assertEqual(self.assignments(), assignments_before)
        self.assertEqual(self.sessions(), sessions_before)

    def test_the_pass_is_throttled_by_its_own_stamp(self) -> None:
        self.add("m-0012", title="Fix Login")
        self.start("m-0012", at=self.old)
        label = self.note("m-0012")["label"]
        # The first pass has no stamp yet,  so it runs:  it writes one,  and the next pass waits for its window.
        first = PL.reconcile_letters(PM.SPEC, self.env(), PM.SEAT, "mac", now=self.now)
        self.assertNotIn("skipped", first)
        again = PL.reconcile_letters(PM.SPEC, self.env(), PM.SEAT, "mac", now=self.now + 60.0)
        self.assertEqual(again.get("skipped"), "not due")
        self.assertEqual(self.title_of("m-0012"), "%s: Fix Login" % label)

    def test_a_platform_that_writes_no_title_is_skipped(self) -> None:
        spec = PL.PlatformSpec(
            platform=PM.SPEC.platform,
            seat=PM.SPEC.seat,
            events=PM.SPEC.events,
            source=PM.SPEC.source,
            display=PM.SPEC.display,
            title_writer=None,
        )
        done = PL.reconcile_letters(spec, self.env(), PM.SEAT, "mac", now=self.now, force=True)
        self.assertIn("skipped", done)


if __name__ == "__main__":
    unittest.main()
