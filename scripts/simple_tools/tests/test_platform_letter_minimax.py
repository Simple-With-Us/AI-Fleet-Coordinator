"""The MiniMax row of the letter table (`platform_letter_minimax`):  what it reads from MiniMax Code's own session
store.  Which chats the app has archived (the hooks' gone report, and `platform-letter gone`):  archiving a chat
reaches no hook event at all, so the store's own `archived` column is the only record of it (AFC-855).  The store
is a temporary one laid out the way the runtime lays out its own, and the assigner is the registry API served in
process (FakeRegistry)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from simple_tools import platform_letter as PL
from simple_tools import platform_letter_minimax as PM
from simple_tools import registry_api
from simple_tools.tests.test_platform_letter_one_table import Base

#: The runtime's own table, with the columns this row reads (the archive flag among them).
DDL = (
    "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
    " parent_session_id TEXT, archived INTEGER NOT NULL DEFAULT 0)"
)


class StoreBase(Base):
    """A MiniMax Code session store in this test's home (the Mac's)."""

    def setUp(self) -> None:
        super().setUp()
        self.store = PM.store_file(self.env())
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(DDL)
        conn.close()

    def add(self, sid: str, *, title: str = "", kind: str = "conversation", parent: str = "",
            archived: bool = False) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id, archived)"
                " VALUES (?, ?, ?, ?, ?)",
                (sid, title, kind, parent or None, 1 if archived else 0),
            )
        conn.close()

    def archive(self, sid: str) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE local_runtime_sessions SET archived = 1 WHERE session_id = ?", (sid,))
        conn.close()


class ArchiveFlagTests(StoreBase):
    """`minimax_gone`, the spec's `gone_sessions`:  which of these chats MiniMax Code has archived."""

    def test_the_minimax_row_names_its_gone_chats(self) -> None:
        self.assertIs(PM.SPEC.gone_sessions, PM.minimax_gone)

    def test_only_an_archived_chat_is_gone(self) -> None:
        ids = frozenset("m-%04d" % n for n in range(1, 6))
        env = self.env()
        self.assertEqual(PM.minimax_gone(env, ids), set())  # no store yet
        self.add("m-0001", archived=True)
        self.add("m-0002")  # a chat nobody archived
        self.add("m-0003", kind="cron")  # a run, not a chat
        # m-0004 is in no store at all, and m-0005 was never asked about.
        self.assertEqual(PM.minimax_gone(env, ids), {"m-0001"})
        self.assertEqual(PM.minimax_gone(env, frozenset()), set())

    def test_a_store_without_the_archive_flag_names_no_gone_chats(self) -> None:
        # A store from before the runtime wrote the column says nothing about an archive.
        os.remove(self.store)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT,"
                " session_kind TEXT, parent_session_id TEXT)"
            )
        conn.close()
        self.assertEqual(PM.minimax_gone(self.env(), frozenset({"m-0001"})), set())

    def test_a_store_that_cannot_be_read_names_no_gone_chats(self) -> None:
        with open(self.store, "wb") as fh:
            fh.write(b"this is not a session store" * 8)
        self.assertEqual(PM.minimax_gone(self.env(), frozenset({"m-0001"})), set())

    def test_a_store_whose_path_carries_a_question_mark_is_still_read(self) -> None:
        # The path goes into the SQLite URI, so a `?` or a `#` in it would end the file name
        # early and name another file, or none.  The path is percent-encoded, so the row is found.
        home = os.path.join(self.tmp.name, "we?ird #home")
        os.makedirs(os.path.join(home, ".minimax", "v2", "sqlite"))
        path = os.path.join(home, ".minimax", "v2", "sqlite", "runtime-state.sqlite")
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(DDL)
            conn.execute("INSERT INTO local_runtime_sessions VALUES ('m-0001', 'The Chat', 'conversation', NULL, 1)")
        conn.close()
        env = self.env(HOME=home)
        self.assertEqual(PM.minimax_gone(env, frozenset({"m-0001"})), {"m-0001"})
        self.assertEqual(PM.minimax_title({"session_id": "m-0001"}, env), "The Chat")

    def test_a_path_that_cannot_be_resolved_is_an_unreadable_store(self) -> None:
        # The path's own failure is the store's failure:  the readers answer for an unreadable
        # store, so a path that cannot be resolved is no crash.
        with mock.patch.object(PM.Path, "resolve", side_effect=OSError("no such path")):
            self.assertEqual(PM.minimax_gone(self.env(), frozenset({"m-0001"})), set())
            self.assertEqual(PM.minimax_title({"session_id": "m-0001"}, self.env()), "")

    def test_the_data_dir_moves_the_store(self) -> None:
        # MINIMAX_DATA_DIR moves ~/.minimax, so the store is read where the runtime wrote it.
        other = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(os.path.join(other, "v2", "sqlite"))
        path = os.path.join(other, "v2", "sqlite", "runtime-state.sqlite")
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(DDL)
            conn.execute("INSERT INTO local_runtime_sessions VALUES ('m-0001', '', 'conversation', NULL, 1)")
        conn.close()
        env = self.env(MINIMAX_DATA_DIR=other)
        self.assertEqual(PM.minimax_gone(env, frozenset({"m-0001"})), {"m-0001"})


class GoneChatsTests(StoreBase):
    """An archived chat releases its letter (AFC-855).  Archiving reaches no hook, so the MiniMax row names the
    chats the store has archived (`minimax_gone`, from the store's own `archived` flag) and the library's gone
    report, made on a chat's start, has the assigner close them.  Every other chat is left alone."""

    def setUp(self) -> None:
        super().setUp()
        self.t0 = time.time() - 5 * 3600
        self.due = self.t0 + PL.GONE_REPORT_SECONDS + 1
        # These tests report rows moments after they were registered.  The assigner keeps a row its chat
        # touched lately, so that rule is turned off here.
        self.fake.service.gone_report_live_minutes = 0

    def begin(self, sid: str, *, now: float | None = None, source: str = "startup") -> str:
        return self.run_hook(
            PM.SPEC,
            {"hook_event_name": "SessionStart", "session_id": sid, "source": source},
            now=now,
        )

    def label(self, sid: str) -> str:
        return self.note("MM", sid)["label"]

    def row(self, sid: str, location: str = "mac") -> dict | None:
        return self.fake.lookup("minimax", location, PL.SCOPE, sid)

    def row_state(self, sid: str, location: str = "mac") -> str:
        return self.row(sid, location)["state"]

    @contextmanager
    def reports(self):
        """The body of every report-gone request the client makes while this is open."""
        seen: list[dict] = []
        real = registry_api._open

        def capture(request, timeout):
            if request.full_url.endswith("/report-gone"):
                seen.append(json.loads(request.data.decode("utf-8")))
            return real(request, timeout)

        with mock.patch.object(registry_api, "_open", capture):
            yield seen

    def test_a_start_reports_the_archived_chats_and_their_letters_are_freed(self) -> None:
        self.add("m-0001")
        self.begin("m-0001", now=self.t0)
        label = self.label("m-0001")
        self.assertEqual(self.row_state("m-0001"), "active")
        self.archive("m-0001")
        with self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [{"platform": "minimax", "verified_host": "mac", "profile_scope": "default",
                                 "native_session_ids": ["m-0001"]}])
        self.assertEqual(self.row_state("m-0001"), "closed")
        # The letter is free:  the registry no longer resolves it to any live chat.
        self.assertFalse(registry_api.call(self.env(), "resolve", {"platform": "minimax", "letter": label})["found"])

    def test_a_chat_that_was_not_archived_keeps_its_letter(self) -> None:
        self.add("m-0001")
        self.begin("m-0001", now=self.t0)
        with self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("m-0001"), "active")

    def test_a_chat_the_store_does_not_know_keeps_its_letter(self) -> None:
        # A chat of another host's store, or one the store has not written yet:  any doubt keeps it.
        self.begin("m-0001", now=self.t0)
        with self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("m-0001"), "active")

    def test_a_chat_that_is_still_in_use_is_kept(self) -> None:
        # The assigner keeps a row its chat touched lately, whatever the store says.
        self.fake.service.gone_report_live_minutes = 60
        self.add("m-0001")
        self.begin("m-0001", now=self.t0)
        self.archive("m-0001")
        with self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.row_state("m-0001"), "active")

    def test_the_verb_shows_what_would_be_reported_and_never_a_title(self) -> None:
        self.add("m-0001", title="Secret Title")
        self.begin("m-0001", now=self.t0)
        self.archive("m-0001")
        out = io.StringIO()
        with self.reports() as sent:
            code = PL.gone_main(["--platform", "minimax"], self.env(), out, now=self.due, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().splitlines(), [
            "would report MiniMax %s (m-0001, mac)" % self.label("m-0001"),
            "MiniMax gone report, mac:  1 gone  (dry run:  nothing sent;  --apply reports them now)",
        ])
        self.assertNotIn("Secret Title", out.getvalue())
        self.assertEqual(sent, [])
        # --apply reports it now, and the row is closed.
        out = io.StringIO()
        code = PL.gone_main(["--platform", "minimax", "--apply"], self.env(), out, now=self.due, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("reported MiniMax %s (m-0001, mac)" % self.label("m-0001"), out.getvalue())
        self.assertEqual(self.row_state("m-0001"), "closed")

    def test_nothing_about_the_store_can_fail_a_start(self) -> None:
        self.add("m-0001")
        self.begin("m-0001", now=self.t0)
        self.archive("m-0001")
        with mock.patch.object(PM, "archived_sessions", side_effect=RuntimeError("the store changed")), \
                self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("m-0001"), "active")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
