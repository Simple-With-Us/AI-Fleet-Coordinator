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


class GoneHarness(StoreBase):
    """The harness the gone-report tests share:  a chat that holds a letter,  the reports the host makes,  and the
    row the assigner holds for that chat."""

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

    def touch_note(self, sid: str, seconds: float) -> None:
        """Make the note of a chat look as if the fleet addressed it that long ago."""
        path = PL.note_path(self.env(), "MM", sid)
        with open(path, encoding="utf-8") as fh:
            note = json.load(fh)
        note["updated_at"] = time.time() - seconds
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(note, fh)

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


class GoneChatsTests(GoneHarness):
    """An archived chat releases its letter (AFC-855).  Archiving reaches no hook, so the MiniMax row names the
    chats the store has archived (`minimax_gone`, from the store's own `archived` flag) and the library's gone
    report, made on a chat's start, has the assigner close them.  Every other chat is left alone."""

    def test_a_start_reports_the_archived_chats_and_their_letters_are_freed(self) -> None:
        self.add("m-0001")
        self.add("m-0002")
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
        self.add("m-0002")
        self.begin("m-0001", now=self.t0)
        with self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("m-0001"), "active")

    def test_a_chat_the_store_does_not_know_is_gone_when_nobody_has_addressed_it(self) -> None:
        # A chat that was deleted or pruned has no store row at all,  so the row the assigner holds for it
        # must be closed,  or its letter can never be freed (AFC-894).
        self.add("m-0002")
        first = time.time() - 600  # the chat registers,  and its note is fresh
        self.begin("m-0001", now=first)
        self.touch_note("m-0001", 7200)  # and the fleet has not addressed it since
        with self.reports() as sent:
            self.begin("m-0002", now=first + PL.GONE_REPORT_SECONDS + 1)
        self.assertEqual(sent, [{"platform": "minimax", "verified_host": "mac", "profile_scope": "default",
                                 "native_session_ids": ["m-0001"]}])
        self.assertEqual(self.row_state("m-0001"), "closed")

    def test_a_chat_the_store_does_not_know_but_that_was_addressed_lately_is_kept(self) -> None:
        # The store has not written the chat yet,  and the fleet addressed it a minute ago:  any doubt keeps it.
        self.add("m-0002")
        first = time.time() - 600
        self.begin("m-0001", now=first)
        self.touch_note("m-0001", 60)
        with self.reports() as sent:
            self.begin("m-0002", now=first + PL.GONE_REPORT_SECONDS + 1)
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
        label = self.label("m-0001")  # the letter the chat held, read before the report retires it
        out = io.StringIO()
        with self.reports() as sent:
            code = PL.gone_main(["--platform", "minimax"], self.env(), out, now=self.due, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().splitlines(), [
            "would report MiniMax %s (m-0001, mac)" % label,
            "MiniMax gone report, mac:  1 gone  (dry run:  nothing sent;  --apply reports them now)",
        ])
        self.assertNotIn("Secret Title", out.getvalue())
        self.assertEqual(sent, [])
        # --apply reports it now, and the row is closed.
        out = io.StringIO()
        code = PL.gone_main(["--platform", "minimax", "--apply"], self.env(), out, now=self.due, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("reported MiniMax %s (m-0001, mac)" % label, out.getvalue())
        self.assertEqual(self.row_state("m-0001"), "closed")
        # The report retires the chat locally:  its note holds no letter any more.
        self.assertEqual(self.label("m-0001"), "")

    def test_nothing_about_the_store_can_fail_a_start(self) -> None:
        self.add("m-0001")
        self.begin("m-0001", now=self.t0)
        self.archive("m-0001")
        with mock.patch.object(PM, "archived_sessions", side_effect=RuntimeError("the store changed")), \
                self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("m-0001"), "active")


# --------------------------------------------------------------------------------------------
# The app's own runs (AFC-894)
# --------------------------------------------------------------------------------------------

#: The runtime's own table with the columns that say what a session is.
MACHINE_DDL = (
    "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
    " parent_session_id TEXT, archived INTEGER NOT NULL DEFAULT 0, status TEXT, created_at_ms INTEGER,"
    " updated_at_ms INTEGER,"
    " workspace_dir TEXT, project_workspace_dir TEXT, purpose TEXT)"
)

#: A BotFleet bot run works in a workspace of its own.
BOTFLEET = "/tmp/fixture-home/.botfleet/workspaces/abc"
#: The owner's own chat works in a project folder.
OWNER = "/tmp/fixture-home/Code/Fixture-Coordinator"


class MachineRunBase(GoneHarness):
    """A session store that says what a session is:  a BotFleet bot run,  the runtime's own work,  or the owner's
    own chat."""

    def setUp(self) -> None:
        super().setUp()
        os.remove(self.store)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(MACHINE_DDL)
        conn.close()

    def add(
        self,
        sid: str,
        *,
        title: str = "",
        kind: str = "conversation",
        parent: str = "",
        archived: bool = False,
        status: str = "idle",
        updated: float | None = None,
        workspace: str = "",
        project_workspace: str = "",
        purpose: str = "",
    ) -> None:
        moment = time.time() if updated is None else updated
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id,"
                " archived, status, created_at_ms, updated_at_ms, workspace_dir, project_workspace_dir, purpose)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    title,
                    kind,
                    parent or None,
                    1 if archived else 0,
                    status,
                    int((moment - 3600) * 1000),
                    int(moment * 1000),
                    workspace,
                    project_workspace,
                    purpose,
                ),
            )
        conn.close()

    def update(self, sid: str, **fields: object) -> None:
        names = {"workspace": "workspace_dir", "project_workspace": "project_workspace_dir"}
        columns = [names.get(name, name) for name in fields]
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "UPDATE local_runtime_sessions SET %s WHERE session_id = ?"
                % ", ".join("%s = ?" % name for name in columns),
                (*fields.values(), sid),
            )
        conn.close()

    def kind(self, sid: str) -> str:
        return PM.minimax_kind({"session_id": sid}, self.env())


class MachineRunTests(MachineRunBase):
    """A machine run never takes a letter (AFC-894).  A BotFleet bot run and the runtime's own work are the app's
    own runs,  and the fleet's rule is that only interactive chats take one."""

    def test_a_botfleet_run_is_headless(self) -> None:
        self.add("m-0001", workspace=BOTFLEET)
        self.assertEqual(self.kind("m-0001"), "headless")

    def test_the_owners_own_chat_is_a_chat(self) -> None:
        self.add("m-0001", workspace=OWNER)
        self.assertEqual(self.kind("m-0001"), "chat")

    def test_a_cron_run_is_headless(self) -> None:
        # A cron row is headless because its own session kind is not a chat kind,  and not because of its
        # purpose:  the kind of a session is what says what it is.
        self.add("m-0001", kind="cron", workspace=OWNER, purpose="cron:mavis:poll-pr-513")
        self.assertEqual(self.kind("m-0001"), "headless")

    def test_a_task_of_the_owner_is_a_chat(self) -> None:
        # The runtime's own background work and a turn of a task are chats of their own,  and the ones that
        # carry a letter today (AW, CI, CL  and the rest) must keep taking one.  Only a BotFleet bot run is
        # the app's own run.
        self.add("m-0001", kind="task", parent="m-0000", workspace=OWNER, purpose="local-task:turn_abc:call_01")
        self.add("m-0002", kind="task", parent="m-0000", workspace=OWNER, purpose="local-background-task:bg_abc")
        self.assertEqual(self.kind("m-0001"), "subagent")
        self.assertEqual(self.kind("m-0002"), "subagent")

    def test_a_path_that_merely_carries_the_same_letters_is_the_owners_own(self) -> None:
        # The BotFleet folder is a segment of the path,  and a project folder of the owner's that carries the
        # same letters inside another name is not a bot run.
        self.add("m-0001", workspace="/tmp/fixture-home/Code/my.botfleet-notes")
        self.assertEqual(self.kind("m-0001"), "chat")
        self.assertEqual(PM.over_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_a_store_that_does_not_say_what_a_session_is_leaves_the_chat_a_chat(self) -> None:
        # A store from before the runtime wrote the columns says nothing at all,  and any doubt keeps the chat a
        # chat:  a letter is never taken from a chat the store cannot describe.
        os.remove(self.store)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(DDL)
            conn.execute(
                "INSERT INTO local_runtime_sessions VALUES ('m-0001', '', 'conversation', NULL, 0)"
            )
        conn.close()
        self.assertEqual(self.kind("m-0001"), "chat")

    def test_a_task_of_the_owner_still_takes_a_letter(self) -> None:
        # A sub-thread the owner started is a chat,  and it takes a letter after its parent's.
        self.add("m-0001", kind="task", parent="m-0000", workspace=OWNER, purpose="local-task:turn_abc:call_01")
        out = self.run_hook(
            PM.SPEC,
            {"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"},
        )
        self.assertNotEqual(out, "")
        self.assertIsNotNone(self.fake.lookup("minimax", "mac", PL.SCOPE, "m-0001"))

    def test_a_machine_run_never_gets_a_letter_in_the_hook(self) -> None:
        self.add("m-0001", workspace=BOTFLEET)
        out = self.run_hook(
            PM.SPEC,
            {"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"},
        )
        self.assertEqual(out, "")
        self.assertIsNone(self.fake.lookup("minimax", "mac", PL.SCOPE, "m-0001"))


class OverRunTests(MachineRunBase):
    """A machine run that is over frees the pair it held (AFC-894).  A pair held by a chat nobody addresses is a pair
    the owner's own chats cannot have."""

    def test_an_ended_machine_run_is_gone(self) -> None:
        self.add("m-0001", status="aborted", workspace=BOTFLEET)
        self.assertEqual(PM.over_sessions(self.env(), frozenset({"m-0001"})), {"m-0001"})

    def test_a_machine_run_that_is_still_at_work_is_not_gone(self) -> None:
        self.add("m-0001", status="started", workspace=BOTFLEET)
        self.assertEqual(PM.over_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_a_machine_run_that_has_rested_for_a_fortnight_is_gone(self) -> None:
        old = time.time() - PM.DEAD_QUIET_SECONDS - 60
        recent = time.time() - PM.DEAD_QUIET_SECONDS + 3600
        self.add("m-0001", status="idle", updated=old, workspace=BOTFLEET)
        self.add("m-0002", status="idle", updated=recent, workspace=BOTFLEET)
        self.assertEqual(PM.over_sessions(self.env(), frozenset({"m-0001", "m-0002"})), {"m-0001"})

    def test_the_owners_own_chat_is_never_gone(self) -> None:
        # An ended chat of the owner is his:  he archives it,  and the archive releases the pair (AFC-855).
        self.add("m-0001", status="aborted", workspace=OWNER)
        self.assertEqual(PM.over_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_the_gone_path_names_the_archived_and_the_over(self) -> None:
        self.add("m-0001", archived=True, workspace=OWNER)
        self.add("m-0002", status="aborted", workspace=BOTFLEET)
        self.add("m-0003", workspace=OWNER)
        ids = frozenset({"m-0001", "m-0002", "m-0003"})
        self.assertEqual(PM.minimax_gone(self.env(), ids), {"m-0001", "m-0002"})

    def test_a_store_that_cannot_be_read_names_nothing(self) -> None:
        with open(self.store, "wb") as fh:
            fh.write(b"this is not a session store" * 8)
        self.assertEqual(PM.over_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_a_store_without_the_columns_names_nothing(self) -> None:
        os.remove(self.store)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(DDL)
            conn.execute(
                "INSERT INTO local_runtime_sessions VALUES ('m-0001', '', 'conversation', NULL, 0)"
            )
        conn.close()
        self.assertEqual(PM.over_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_nothing_about_the_store_can_fail_a_start(self) -> None:
        # The chat holds a letter,  and the store turns out to be unreadable:  the report names nothing,  and the
        # row the assigner holds stays as it was.
        self.add("m-0001", workspace=OWNER)
        self.add("m-0002", workspace=OWNER)
        self.begin("m-0001", now=self.t0)
        self.update("m-0001", status="aborted", workspace=BOTFLEET)
        with mock.patch.object(PM, "over_sessions", side_effect=RuntimeError("the store changed")), \
                self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("m-0001"), "active")


class AbsentChatTests(MachineRunBase):
    """A lettered chat the store does not know at all,  and that nobody has addressed for an hour,  is gone
    (AFC-894):  a chat that was deleted or pruned has no store row,  and the row the assigner holds for it must
    be closed so its letter can be freed."""

    def write_note(self, sid: str, seconds: float) -> None:
        """The note of a chat,  as if the fleet addressed it that long ago."""
        path = PL.note_path(self.env(), "MM", sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "session_id": sid,
                    "state": "open",
                    "label": "AA",
                    "platform": "minimax",
                    "location": "mac",
                    "updated_at": time.time() - seconds,
                },
                fh,
            )

    def test_a_chat_the_store_does_not_know_is_absent_when_nobody_has_addressed_it(self) -> None:
        self.write_note("m-0001", 7200)
        self.assertEqual(PM.absent_sessions(self.env(), frozenset({"m-0001"})), {"m-0001"})

    def test_a_chat_the_store_does_not_know_but_that_was_addressed_lately_is_not_absent(self) -> None:
        self.write_note("m-0001", 60)
        self.assertEqual(PM.absent_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_a_chat_without_a_note_is_not_absent(self) -> None:
        # Any doubt keeps the chat:  a note that cannot be read says nothing about its quiet time.
        self.assertEqual(PM.absent_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_a_chat_the_store_knows_is_not_absent(self) -> None:
        self.add("m-0001", workspace=OWNER)
        self.write_note("m-0001", 7200)
        self.assertEqual(PM.absent_sessions(self.env(), frozenset({"m-0001"})), set())

    def test_a_caller_that_hands_over_an_iterator_is_still_answered(self) -> None:
        # The ids are read once:  a second pass over an iterator would find it empty,  and the gone chats
        # would be missed.
        self.write_note("m-0001", 7200)
        gone = PM.minimax_gone(self.env(), (sid for sid in ["m-0001"]))
        self.assertEqual(gone, {"m-0001"})

    def test_a_chat_the_store_knows_does_not_starve_the_ones_it_does_not(self) -> None:
        # The bound is spent on the chats the store does not know:  a first run of chats the store knows
        # must not leave the budget empty for the rest.
        known = ["k-%04d" % n for n in range(100)]
        for sid in known:
            self.add(sid, workspace=OWNER)
        unknown = ["m-%04d" % n for n in range(PM.ABSENT_PROBE_LIMIT + 10)]
        for sid in unknown:
            self.write_note(sid, 7200)
        gone = PM.absent_sessions(self.env(), known + unknown)
        self.assertEqual(gone, set(unknown[: PM.ABSENT_PROBE_LIMIT]))

    def test_the_probes_are_bounded(self) -> None:
        # The gone path runs on a chat's start,  and one probe is one file read,  so the number of chats it
        # asks about is bounded.  The client offers the longest quiet chats first,  so the bound keeps the ones
        # most likely to be gone.
        ids = ["m-%04d" % n for n in range(PM.ABSENT_PROBE_LIMIT + 20)]
        for sid in ids:
            self.write_note(sid, 7200)
        gone = PM.absent_sessions(self.env(), ids)
        self.assertEqual(len(gone), PM.ABSENT_PROBE_LIMIT)
        self.assertEqual(gone, set(ids[: PM.ABSENT_PROBE_LIMIT]))

    def test_only_the_ids_the_store_was_asked_about_are_named(self) -> None:
        # An id the store has not been asked about is not known to be absent:  naming it would free the
        # letter of a chat the store holds.
        known = ["k-%04d" % n for n in range(200)]
        for sid in known:
            self.add(sid, workspace=OWNER)
            self.write_note(sid, 7200)
        unknown = ["m-%04d" % n for n in range(100)]
        for sid in unknown:
            self.write_note(sid, 7200)
        gone = PM.absent_sessions(self.env(), known + unknown)
        self.assertEqual(gone, set(unknown[: PM.ABSENT_PROBE_LIMIT]))

    def test_the_known_check_is_bounded_and_stops_early(self) -> None:
        # The check reads the store in chunks and stops as soon as it has the budget it needs,  so a long
        # list does not cost a query per chunk of it.
        ids = ["m-%05d" % n for n in range(2000)]
        for sid in ids[:120]:
            self.write_note(sid, 7200)
        seen: list[str] = []
        real_connect = sqlite3.connect

        class Recording:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                seen.append(sql)
                return self._conn.execute(sql, params)

            def close(self):
                self._conn.close()

        def connect(*args, **kwargs):
            return Recording(real_connect(*args, **kwargs))

        with mock.patch.object(PM.sqlite3, "connect", side_effect=connect):
            gone = PM.absent_sessions(self.env(), ids)
        queries = [sql for sql in seen if "IN (" in sql]
        self.assertLessEqual(len(queries), PM.KNOWN_CHUNKS)
        self.assertEqual(len(gone), PM.ABSENT_PROBE_LIMIT)

    def test_a_store_that_cannot_be_read_names_nothing(self) -> None:
        self.write_note("m-0001", 7200)
        with open(self.store, "wb") as fh:
            fh.write(b"this is not a session store" * 8)
        self.assertEqual(PM.absent_sessions(self.env(), frozenset({"m-0001"})), set())


class ReleaseReportTests(MachineRunBase):
    """The report a chat's start makes frees the pair an ended machine run held (AFC-894)."""

    def test_a_start_reports_an_ended_machine_run_and_its_letter_is_freed(self) -> None:
        # The chat was a chat when the hook saw it,  and the store says now that it is a BotFleet run that is over.
        self.add("m-0001", workspace=OWNER)
        self.add("m-0002", workspace=OWNER)
        self.begin("m-0001", now=self.t0)
        label = self.label("m-0001")
        self.assertTrue(label)
        self.update("m-0001", status="aborted", workspace=BOTFLEET)
        with self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(
            sent,
            [
                {
                    "platform": "minimax",
                    "verified_host": "mac",
                    "profile_scope": "default",
                    "native_session_ids": ["m-0001"],
                }
            ],
        )
        self.assertEqual(self.row_state("m-0001"), "closed")
        # The letter is free:  the registry no longer resolves it to any live chat.
        self.assertFalse(
            registry_api.call(self.env(), "resolve", {"platform": "minimax", "letter": label})["found"]
        )

    def test_a_machine_run_that_is_still_at_work_keeps_its_letter(self) -> None:
        self.add("m-0001", workspace=OWNER)
        self.add("m-0002", workspace=OWNER)
        self.begin("m-0001", now=self.t0)
        self.update("m-0001", status="started", workspace=BOTFLEET)
        with self.reports() as sent:
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("m-0001"), "active")


class RepairScanTests(MachineRunBase):
    """The repair pass leaves the app's own runs alone (AFC-894):  a BotFleet run is not a chat the owner
    addresses,  and it must not take the scan's room from the chats that are."""

    def test_a_machine_run_is_not_named_by_the_scan(self) -> None:
        self.add("m-0001", workspace=BOTFLEET, title="")
        self.add("m-0002", workspace=OWNER, title="")
        named = PM.minimax_unlettered_sessions(self.env())
        self.assertIsNotNone(named)
        self.assertNotIn("m-0001", named)
        self.assertIn("m-0002", named)

    def test_a_machine_run_whose_title_lost_its_letter_is_not_named_by_the_scan(self) -> None:
        self.add("m-0001", workspace=BOTFLEET, title="New Chat")
        self.add("m-0002", workspace=OWNER, title="New Chat")
        named = PM.minimax_unlettered_sessions(self.env())
        self.assertIsNotNone(named)
        self.assertNotIn("m-0001", named)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
