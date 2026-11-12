"""The Claude row of the letter table (`platform_letter_claude`):  what it reads from the desktop app's own record
of a chat.  Which chat started this one (found by its app id, or by its session UUID), the parent registered ahead of
its sub-thread, the title the app shows over the hook's (the model is told to put the letter on it), and the chats
the app has archived (the hooks' gone report, and `platform-letter gone`).  The store is a temporary one laid out the way the app lays
out its own, and the assigner is the registry API served in process (FakeRegistry)."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from simple_tools import platform_letter as PL
from simple_tools import platform_letter_claude as PC
from simple_tools import registry_api
from simple_tools.tests.test_platform_letter import Calls
from simple_tools.tests.test_platform_letter_one_table import Base as TableBase

SCRIPTS = Path(__file__).resolve().parents[2]
PARENT_APP = "local_11111111-1111-4111-8111-111111111111"
PARENT_CLI = "33333333-3333-4333-8333-333333333333"
CHILD_APP = "local_22222222-2222-4222-8222-222222222222"
CHILD_CLI = "44444444-4444-4444-8444-444444444444"  # a chat's app id is not its Claude Code UUID
GAP = "  "


def write(home: str, host: str, body: dict | str, account: str = "acct", org: str = "org") -> str:
    folder = os.path.join(PC.store_root(home), account, org)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, host + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body if isinstance(body, str) else json.dumps(body))
    return path


def secret_word() -> str:
    """A GitHub-shaped token built at run time, so the source holds no key-looking literal."""
    return "ghp" + "_" + "a1B2" * 9


class StoreBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name

    def family(self, **child_extra) -> None:
        """A parent that ran, and a chat it started."""
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP, "cliSessionId": PARENT_CLI})
        child = {
            "sessionId": CHILD_APP,
            "cliSessionId": CHILD_CLI,
            "spawnedFrom": {"sessionId": PARENT_APP, "title": "t", "handOff": True},
        }
        child.update(child_extra)
        write(self.home, CHILD_APP, child)

    def parent(self, **kw) -> str:
        return PC.lookup(self.home, **kw)[0]


class LookupTests(StoreBase):
    """`lookup`:  the parent's session UUID, or why none is known."""

    def test_a_started_chat_names_its_parents_session_uuid(self) -> None:
        self.family()
        self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)
        self.assertEqual(self.parent(host_session_id=CHILD_APP, cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_the_apps_id_is_not_the_session_uuid(self) -> None:
        self.family()
        self.assertNotEqual(PARENT_APP[len("local_"):], PARENT_CLI)
        self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)  # never the text after local_

    def test_the_chat_is_found_by_its_session_uuid_when_its_app_id_is_not_known(self) -> None:
        self.family()
        self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)
        # or when the app id it gave names no record
        self.assertEqual(self.parent(host_session_id="local_00000000-0000", cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_a_chat_nobody_started_has_no_parent(self) -> None:
        write(self.home, CHILD_APP, {"sessionId": CHILD_APP, "cliSessionId": CHILD_CLI})
        self.assertEqual(self.parent(host_session_id=CHILD_APP, cli_session_id=CHILD_CLI), "")

    def test_a_user_fork_is_not_a_subthread(self) -> None:
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP, "cliSessionId": PARENT_CLI})
        write(
            self.home, CHILD_APP,
            {"sessionId": CHILD_APP, "cliSessionId": CHILD_CLI, "forkedFromSessionId": PARENT_APP},
        )
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")

    def test_no_store_means_no_parent(self) -> None:  # the server, or a Mac that never ran the app
        self.assertEqual(self.parent(host_session_id=CHILD_APP, cli_session_id=CHILD_CLI), "")
        self.assertEqual(self.parent(), "")

    def test_a_parent_the_app_deleted_or_never_ran_is_no_parent(self) -> None:
        self.family(spawnedFrom={"sessionId": PARENT_APP, "deletedParent": True})
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")
        self.family(spawnedFrom={"sessionId": "local_deadbeef-0000-0000-0000-000000000000"})  # no such record
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")
        self.family()
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP})  # a parent that never started a process
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")

    def test_a_chat_is_never_its_own_parent(self) -> None:
        write(
            self.home, CHILD_APP,
            {"sessionId": CHILD_APP, "cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": CHILD_APP}},
        )
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")
        # a parent record that holds the child's own UUID is the same chat
        self.family()
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP, "cliSessionId": CHILD_CLI})
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")

    def test_an_id_that_could_leave_the_store_never_builds_a_path(self) -> None:
        # The store folder is six levels below home, so six ".." from a record's folder land on this file.
        outside = os.path.join(self.home, "outside.json")
        with open(outside, "w") as fh:
            fh.write(json.dumps({"cliSessionId": PARENT_CLI}))
        hostile = "../" * 6 + "outside"
        root = PC.store_root(self.home)
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI})  # make the store (and an org folder) exist
        # Proof the file is reachable that way:  with the id check switched off, the hostile id finds it.
        with mock.patch.object(PC, "HOST_ID", re.compile(".*")):
            self.assertTrue(os.path.samefile(PC._find(root, hostile), outside))
        for bad in (hostile, "local_" + hostile, "local_/../../../../../outside", "local_a/b", "/etc/passwd",
                    "local_" + "a" * 200, "", None, 7):
            self.assertEqual(PC._find(root, bad), "", bad)
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": "local_" + hostile}})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.PARENT_GONE))
        with mock.patch.object(PC, "HOST_ID", re.compile(".*")):  # and the same record, were the check off, would leak
            write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": hostile}})
            self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), (PARENT_CLI, ""))

    def test_a_parent_uuid_that_is_not_a_session_id_is_ignored(self) -> None:
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP, "cliSessionId": "../../etc"})
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}})
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP, "cliSessionId": 7})
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")

    def test_malformed_records_answer_no_parent(self) -> None:
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            for body in ("", "not json", "[1, 2]", "null", '{"spawnedFrom": "x"}', '{"spawnedFrom": {"sessionId": 5}}'):
                write(self.home, CHILD_APP, body)
                self.assertEqual(self.parent(host_session_id=CHILD_APP), "", body)

    def test_a_record_caught_mid_write_is_read_again(self) -> None:
        self.family()
        real = PC._read
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            return '{"cliSes' if calls["n"] == 1 else real(path)

        with mock.patch.object(PC, "_read", flaky), mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)
        self.assertGreaterEqual(calls["n"], 3)

    def test_a_record_that_stays_broken_is_given_up_on(self) -> None:
        self.family()
        with mock.patch.object(PC, "_read", lambda path: "{"), mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.assertEqual(self.parent(host_session_id=CHILD_APP), "")

    def test_a_symlinked_record_is_not_followed(self) -> None:
        real = write(self.home, "elsewhere", {"cliSessionId": PARENT_CLI})
        folder = os.path.dirname(real)
        os.symlink(real, os.path.join(folder, PARENT_APP + ".json"))
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}})
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")

    def test_an_oversized_record_is_ignored_though_it_is_valid_json(self) -> None:
        self.family(padding="x" * 400)  # a valid record, padded past the limit below
        self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)  # fine at the real limit
        with mock.patch.object(PC, "RECORD_BYTES", 300):
            self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.UNREADABLE))
            # the same size rule keeps the scan from reading it, and the scan says nothing was found
            self.assertEqual(PC.lookup(self.home, cli_session_id=CHILD_CLI), ("", PC.NO_RECORD))

    def test_records_in_other_account_folders_are_found(self) -> None:
        write(self.home, PARENT_APP, {"cliSessionId": PARENT_CLI}, account="a1", org="o1")
        write(
            self.home, CHILD_APP,
            {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}}, account="a2", org="o2",
        )
        self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)

    def test_a_chat_that_was_started_by_a_started_chat_names_that_chat(self) -> None:
        self.family()
        grandchild_host, grandchild_cli = "local_11111111-2222-3333-4444-555555555555", "5e1f0c2a-7d3b-4a6e-8f90-aabbccddeeff"
        write(
            self.home, grandchild_host,
            {"cliSessionId": grandchild_cli, "spawnedFrom": {"sessionId": CHILD_APP}},
        )
        self.assertEqual(self.parent(host_session_id=grandchild_host), CHILD_CLI)

    def test_the_scan_by_session_uuid_looks_through_a_bounded_set_of_the_newest_records(self) -> None:
        self.family()
        child_path = PC._find(PC.store_root(self.home), CHILD_APP)
        old = time.time() - 86400
        os.utime(child_path, (old, old))
        for i in range(3):
            write(self.home, "local_0000000%d-0000" % i, {"cliSessionId": "other-%d-session" % i})
        with mock.patch.object(PC, "SCAN_RECORDS", 2):
            self.assertEqual(self.parent(cli_session_id=CHILD_CLI), "")  # the child's record is past the newest two
        self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_the_reason_no_parent_is_known_is_named(self) -> None:
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.NO_STORE))
        write(self.home, "local_00000000-0000", {"cliSessionId": "other-0000"})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.NO_RECORD))
        self.assertEqual(PC.lookup(self.home), ("", PC.NO_RECORD))
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.NOT_STARTED))
        self.family(spawnedFrom={"sessionId": PARENT_APP, "deletedParent": True})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.PARENT_GONE))
        self.family(spawnedFrom={"sessionId": "local_deadbeef-0000"})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.PARENT_GONE))
        self.family()
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.PARENT_NEVER_RAN))
        write(self.home, PARENT_APP, {"cliSessionId": CHILD_CLI})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.SAME_CHAT))
        write(self.home, PARENT_APP, "{")
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.UNREADABLE))
        self.family()
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), (PARENT_CLI, ""))

    def test_only_the_lineage_fields_leave_a_record(self) -> None:
        private = "my private title"
        write(
            self.home, CHILD_APP,
            {
                "cliSessionId": CHILD_CLI, "title": private, "messages": ["hello"],
                "spawnedFrom": {"sessionId": PARENT_APP, "title": private, "taskId": "t", "deletedParent": False},
                "remoteMcpServersConfig": {"token": "do-not-leak"}, "sshConfig": {"sshHost": "a-private-host"},
            },
        )
        record = PC._load(PC._find(PC.store_root(self.home), CHILD_APP))
        self.assertEqual(
            record,
            {"cliSessionId": CHILD_CLI, "remote": True,
             "spawnedFrom": {"sessionId": PARENT_APP, "deletedParent": False}},
        )
        for hidden in (private, "do-not-leak", "a-private-host"):
            self.assertNotIn(hidden, json.dumps(record))

    def test_a_deeply_nested_record_never_raises(self) -> None:
        # Older Pythons refuse this with RecursionError (unreadable);  3.15 parses it, and it names no parent.
        write(self.home, CHILD_APP, '{"a":' * 100000 + "1" + "}" * 100000)
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            parent, why = PC.lookup(self.home, host_session_id=CHILD_APP)
        self.assertEqual(parent, "")
        self.assertIn(why, (PC.UNREADABLE, PC.NOT_STARTED))

    def test_a_record_the_parser_refuses_as_too_deep_is_unreadable_not_an_error(self) -> None:
        self.family()
        with mock.patch.object(PC.json, "loads", side_effect=RecursionError("maximum recursion depth exceeded")), \
                mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.UNREADABLE))

    def test_one_file_that_vanishes_does_not_end_the_scan_of_its_folder(self) -> None:
        self.family()
        write(self.home, "local_0-vanishing", {"cliSessionId": "gone-0001"})
        real = os.scandir

        class Entry:
            def __init__(self, entry):
                self.entry, self.name, self.path = entry, entry.name, entry.path

            def is_dir(self, **kw):
                return self.entry.is_dir(**kw)

            def is_file(self, **kw):
                return self.entry.is_file(**kw)

            def stat(self):
                if self.name.startswith("local_0-vanishing"):
                    raise FileNotFoundError(self.name)  # listed, then gone before it was looked at
                return self.entry.stat()

        @contextmanager
        def scandir(path):
            with real(path) as entries:
                listed = sorted((Entry(e) for e in entries), key=lambda e: not e.name.startswith("local_0-vanishing"))
                yield iter(listed)  # the file that vanishes comes first

        with mock.patch.object(PC.os, "scandir", scandir):
            self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_a_symlinked_record_is_not_scanned(self) -> None:
        write(self.home, PARENT_APP, {"cliSessionId": PARENT_CLI})
        real = write(self.home, "elsewhere", {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}})
        os.symlink(real, os.path.join(os.path.dirname(real), CHILD_APP + ".json"))
        self.assertEqual(PC.lookup(self.home, cli_session_id=CHILD_CLI), ("", PC.NO_RECORD))

    def test_a_symlinked_folder_is_not_followed(self) -> None:
        self.family()
        elsewhere = os.path.join(self.tmp.name, "elsewhere-org")
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, "local_99999999-0000.json"), "w", encoding="utf-8") as fh:
            json.dump({"cliSessionId": "ghost-0001", "spawnedFrom": {"sessionId": PARENT_APP}}, fh)
        os.symlink(elsewhere, os.path.join(PC.store_root(self.home), "acct", "org2"))
        self.assertEqual(PC.lookup(self.home, cli_session_id="ghost-0001"), ("", PC.NO_RECORD))
        self.assertEqual(PC._find(PC.store_root(self.home), "local_99999999-0000"), "")

    def test_the_scan_stops_at_its_byte_budget(self) -> None:
        self.family()
        old = time.time() - 86400
        os.utime(PC._find(PC.store_root(self.home), CHILD_APP), (old, old))
        for i in range(3):
            write(self.home, "local_0000000%d-0000" % i, {"cliSessionId": "other-%d-session" % i, "padding": "x" * 500})
        with mock.patch.object(PC, "SCAN_BYTES", 600):
            self.assertEqual(PC.lookup(self.home, cli_session_id=CHILD_CLI), ("", PC.NO_RECORD))
        self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_files_too_big_to_read_still_count_toward_the_scan_budget(self) -> None:
        write(self.home, PARENT_APP, {"cliSessionId": PARENT_CLI})
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}})
        old = time.time() - 86400
        os.utime(PC._find(PC.store_root(self.home), CHILD_APP), (old, old))
        for i in range(3):
            write(self.home, "local_0000000%d-0000" % i, {"cliSessionId": "other-%d-session" % i, "padding": "x" * 500})
        with mock.patch.object(PC, "RECORD_BYTES", 200), mock.patch.object(PC, "SCAN_BYTES", 350):
            self.assertEqual(PC.lookup(self.home, cli_session_id=CHILD_CLI), ("", PC.NO_RECORD))
        with mock.patch.object(PC, "RECORD_BYTES", 200):
            self.assertEqual(PC.lookup(self.home, cli_session_id=CHILD_CLI), (PARENT_CLI, ""))  # no budget in the way

    def test_a_host_id_whose_record_is_another_chats_is_not_used(self) -> None:
        # A claude started inside the child inherits CHILD_APP, so the record it names is the child's, not the nested
        # run's:  the nested run (its own session uuid) is not the child's sibling.
        self.family()
        self.assertEqual(
            PC.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="nested-0001"), ("", PC.NO_RECORD)
        )

    def test_a_nested_run_with_a_record_of_its_own_is_judged_by_that_record(self) -> None:
        self.family()
        write(self.home, "local_eeeeeeee-0005", {"cliSessionId": "mine-0001", "spawnedFrom": {"sessionId": PARENT_APP}})
        self.assertEqual(
            PC.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="mine-0001"), (PARENT_CLI, "")
        )
        write(self.home, "local_eeeeeeee-0005", {"cliSessionId": "mine-0001"})  # a record of a chat nobody started
        self.assertEqual(
            PC.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="mine-0001"), ("", PC.NOT_STARTED)
        )

    def test_a_host_id_with_a_matching_or_missing_session_uuid_is_used(self) -> None:
        self.family()
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP, cli_session_id=CHILD_CLI), (PARENT_CLI, ""))
        # a record that holds no session uuid (yet) cannot be told to be another chat's
        write(self.home, CHILD_APP, {"spawnedFrom": {"sessionId": PARENT_APP}})
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="anything-0001"), (PARENT_CLI, ""))

    def test_a_record_that_lags_after_a_clear_is_used_when_the_caller_allows_it(self) -> None:
        self.family()  # the record still holds CHILD_CLI, but the chat now has a new session uuid
        self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="cleared-0001")[0], "")
        self.assertEqual(
            PC.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="cleared-0001", allow_lag=True),
            (PARENT_CLI, ""),
        )

    def test_a_record_read_half_written_is_read_again_after_a_pause(self) -> None:
        self.family()
        real = PC._read
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            return "" if calls["n"] <= 2 else real(path)  # empty twice, as a truncate-then-write leaves it

        with mock.patch.object(PC, "_read", flaky), mock.patch.object(PC.time, "sleep") as pause:
            self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)
        self.assertEqual(pause.call_count, 2)  # one pause before each re-read
        pause.assert_called_with(PC.RETRY_PAUSE)

    def test_a_record_that_stays_empty_through_every_attempt_is_unreadable(self) -> None:
        self.family()
        with mock.patch.object(PC, "_read", lambda path: ""), mock.patch.object(PC.time, "sleep") as pause:
            self.assertEqual(PC.lookup(self.home, host_session_id=CHILD_APP), ("", PC.UNREADABLE))
        self.assertEqual(pause.call_count, PC.LOAD_ATTEMPTS - 1)

    def test_the_scan_does_not_trust_a_record_that_only_mentions_the_uuid(self) -> None:
        self.family()
        write(self.home, "local_99999999-0000", {"cliSessionId": "x" * 8, "notes": CHILD_CLI,
                                                  "spawnedFrom": {"sessionId": "local_deadbeef-0000"}})
        self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)


class ParentAccessorTests(StoreBase):
    """`claude_parent`, the spec's accessor:  a parent is a non-empty session id that is not this chat's own, and
    anything else is no parent."""

    def env(self, host: str = CHILD_APP) -> dict[str, str]:
        return {"HOME": self.home, "CLAUDE_CODE_HOST_SESSION_ID": host} if host else {"HOME": self.home}

    def test_the_parent_is_found_by_the_app_id_and_by_the_session_uuid(self) -> None:
        self.family()
        self.assertEqual(PC.claude_parent({"session_id": CHILD_CLI}, self.env()), PARENT_CLI)
        # No app id in the environment, or one that names no record:  the chat is found by its own session UUID.
        self.assertEqual(PC.claude_parent({"session_id": CHILD_CLI}, self.env("")), PARENT_CLI)
        self.assertEqual(PC.claude_parent({"session_id": CHILD_CLI}, self.env("local_00000000-0000")), PARENT_CLI)

    def test_a_nested_run_never_takes_the_outer_chats_parent(self) -> None:
        # A `claude` started inside the child inherits its app id.  The record is not the nested run's own, so the
        # run is looked for by its own UUID:  it has no record, and so no parent.
        self.family()
        self.assertEqual(PC.claude_parent({"session_id": "nested-0001"}, self.env()), "")
        # A nested run that is itself a chat the app started is judged by its own record.
        write(self.home, "local_eeeeeeee-0005", {"cliSessionId": "mine-0001", "spawnedFrom": {"sessionId": CHILD_APP}})
        self.assertEqual(PC.claude_parent({"session_id": "mine-0001"}, self.env()), CHILD_CLI)
        # Asked about the parent itself with the child's app id (the library, registering the parent first):  the
        # parent's own record answers, and it was started by nobody.
        self.assertEqual(PC.claude_parent({"session_id": PARENT_CLI}, self.env()), "")

    def test_a_parent_is_never_this_chat_and_never_something_that_is_not_a_session_id(self) -> None:
        self.family()
        for answer in ((CHILD_CLI, ""), ("", ""), (None, ""), (7, ""), (["x"], ""), ("", PC.UNREADABLE)):
            with self.subTest(answer=answer), mock.patch.object(PC, "lookup", return_value=answer):
                self.assertEqual(PC.claude_parent({"session_id": CHILD_CLI}, self.env()), "")
        with mock.patch.object(PC, "lookup", return_value=(PARENT_CLI, "")):
            self.assertEqual(PC.claude_parent({"session_id": CHILD_CLI}, self.env()), PARENT_CLI)
        # A payload with no usable session id asks nothing of the store.
        with mock.patch.object(PC, "lookup", side_effect=AssertionError("not asked")):
            for payload in ({}, {"session_id": ""}, {"session_id": "../x"}, {"session_id": 7}):
                self.assertEqual(PC.claude_parent(payload, self.env()), "")

    def test_nothing_the_apps_file_can_do_fails_a_chats_start(self) -> None:
        self.family()
        with mock.patch.object(PC, "lookup", side_effect=RuntimeError("the app changed its file")):
            self.assertEqual(PC.claude_parent({"session_id": CHILD_CLI}, self.env()), "")
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            for body in ("", "not json", "[1]", '{"spawnedFrom": {"sessionId": 5}}', '{"cliSessionId": {"a": 1}}'):
                write(self.home, CHILD_APP, body)
                self.assertEqual(PC.claude_parent({"session_id": CHILD_CLI}, self.env()), "", body)

    def test_only_the_start_after_a_clear_lets_a_lagging_record_decide(self) -> None:
        self.family()  # the record still holds CHILD_CLI, but the chat now has a new session id
        payload = {"hook_event_name": "SessionStart", "session_id": "cleared-0001"}
        self.assertEqual(PC.claude_parent(dict(payload, source="clear"), self.env()), PARENT_CLI)
        self.assertEqual(PC.claude_parent(dict(payload, source="startup"), self.env()), "")
        self.assertEqual(PC.claude_parent(dict(payload, source="clear", hook_event_name="UserPromptSubmit"),
                                          self.env()), "")


class HookBase(TableBase):
    """Claude chats on the Mac, through the hook, against the assigner served in process."""

    PARENT, CHILD = "local_aaaaaaaa-0001", "local_bbbbbbbb-0002"

    def claude_env(self, host: str = "", **extra: str) -> dict[str, str]:
        if host:
            extra["CLAUDE_CODE_HOST_SESSION_ID"] = host
        return self.env(CLAUDE_CODE_ENTRYPOINT="claude-desktop", **extra)

    def app_record(self, host: str, cli: str = "", started_by: str = "", title: str = "", source: str = "",
                   **extra) -> str:
        """The desktop app's record of a chat, laid out as the app lays it out, in this test's home (the Mac's)."""
        body: dict = {"sessionId": host}
        if cli:
            body["cliSessionId"] = cli
        if started_by:
            body["spawnedFrom"] = {"sessionId": started_by}
        if title:
            body.update(title=title, titleSource=source)
        body.update(extra)
        return write(self.home, host, body)

    def answer(self, payload: dict, *, host: str = "", now: float | None = None, env=None, transport=None) -> dict:
        text = self.run_hook(PC.SPEC, payload, env=env if env is not None else self.claude_env(host), now=now,
                             transport=transport)
        return json.loads(text)["hookSpecificOutput"] if text.strip() else {}

    def begin(self, sid: str, *, host: str = "", source: str = "startup", **kw) -> dict:
        return self.answer({"hook_event_name": "SessionStart", "session_id": sid, "source": source}, host=host, **kw)

    def ask(self, sid: str, text: str = "go", *, host: str = "", **kw) -> dict:
        return self.answer({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text}, host=host, **kw)

    def label(self, sid: str) -> str:
        return self.note("CLAUDE", sid)["label"]

    def row(self, sid: str, location: str = "mac") -> dict | None:
        return self.fake.lookup("claude", location, PL.SCOPE, sid)


class SubThreadTests(HookBase):
    """A chat the app started from another chat takes the pair after its parent's, and its parent is registered
    first when this host's app runs it and it holds no letter yet."""

    def assert_placed_after(self, child: str, parent: str) -> None:
        self.assertNotEqual(child, parent)
        self.assertEqual(child[0], parent[0], (child, parent))  # the pair after its parent's, in the same row

    def test_a_chat_the_app_started_takes_the_pair_after_its_parents(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        parent = self.begin("p-0001", host=self.PARENT)
        child = self.begin("c-0001", host=self.CHILD)
        self.assert_placed_after(self.label("c-0001"), self.label("p-0001"))
        self.assertEqual(parent["sessionTitle"], "%s: New Chat" % self.label("p-0001"))
        self.assertEqual(child["sessionTitle"], "%s: New Chat" % self.label("c-0001"))
        self.assertEqual(self.note("CLAUDE", "c-0001")["parent"], "p-0001")

    def test_a_chat_with_no_app_id_in_its_environment_is_still_placed_by_its_session_uuid(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        self.begin("p-0001")
        self.begin("c-0001")
        self.assert_placed_after(self.label("c-0001"), self.label("p-0001"))

    def test_a_claude_started_inside_a_chat_is_a_main_chat(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        self.begin("p-0001", host=self.PARENT)
        self.begin("n-0001", host=self.CHILD)  # it inherited the child's app id, and has no record of its own
        self.assertEqual(self.note("CLAUDE", "n-0001")["parent"], "")
        nested, parent = self.label("n-0001"), self.label("p-0001")
        self.assertEqual(nested[0], nested[1], nested)  # a doubled pair:  picked as a main chat is
        self.assertNotEqual(nested, parent)

    def test_the_payload_for_a_chat_the_hook_did_not_fire_for_says_so(self) -> None:
        payload = PC.SPEC.chat_payload("p-0001")
        self.assertEqual(payload, {"hook_event_name": "SessionStart", "source": "resume", "session_id": "p-0001",
                                   PC.ASKED_ABOUT: True})
        env = self.claude_env(self.CHILD)
        # Asked about a chat:  only one this host's app holds a record of, and runs on this host, is one.
        self.assertEqual(PC.claude_kind(payload, env), "headless")  # no record at all
        self.app_record(self.PARENT, "p-0001")
        self.assertEqual(PC.claude_kind(payload, env), "chat")
        self.app_record(self.PARENT, "p-0001", sshConfig={"sshHost": "elsewhere"})
        self.assertEqual(PC.claude_kind(payload, env), "headless")  # the app runs it on another host
        self.app_record(self.PARENT, "p-0001", sshConfig={})
        self.assertEqual(PC.claude_kind(payload, env), "chat")
        with mock.patch.object(PC, "_record_for_cli_id", side_effect=RuntimeError("the app changed its file")):
            self.assertEqual(PC.claude_kind(payload, env), "headless")  # any doubt registers nothing
        # The hook's own payload carries no such mark, so the chat's own start never reads the store for its kind.
        with mock.patch.object(PC, "_record_for_cli_id", side_effect=AssertionError("not asked")):
            self.assertEqual(PC.claude_kind({"session_id": "c-0001"}, env), "chat")
        # And the rules every Claude chat is under still come first.
        self.assertEqual(PC.claude_kind(payload, dict(env, CLAUDE_CODE_ENTRYPOINT="sdk-cli")), "headless")

    def test_a_parent_with_no_letter_yet_is_registered_before_its_sub_thread(self) -> None:
        # The parent started before the hooks were installed (or its own registration failed):  no note, no row.
        self.app_record(self.PARENT, "p-0001", title="Parent Title", source="tool")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        calls = Calls(self)
        child = self.begin("c-0001", host=self.CHILD, transport=calls)
        self.assertEqual([(c[0], c[c.index("--session-id") + 1]) for c in calls.calls],
                         [("register", "p-0001"), ("register", "c-0001")])
        self.assert_placed_after(self.label("c-0001"), self.label("p-0001"))
        self.assertEqual(self.row("p-0001")["label"], self.label("p-0001"))
        # Nothing of the parent reaches the child's hook answer:  the title and the reminder are the child's own.
        self.assertEqual(child["sessionTitle"], "%s: New Chat" % self.label("c-0001"))
        self.assertIn('"@**Claude** %s"' % self.label("c-0001"), child["additionalContext"])
        self.assertNotIn(self.label("p-0001") + ":", child["additionalContext"])
        self.assertNotIn("Parent Title", json.dumps(child))
        # The parent's registration carries no title made up for it, and the parent hears of its letter, and is
        # titled, at its own next prompt.
        self.assertEqual(self.row("p-0001")["title"] or "", "")
        note = self.note("CLAUDE", "p-0001")
        self.assertEqual((note["state"], note.get("announce"), note.get("titled")), ("open", True, False))
        told = self.ask("p-0001", "review the parser", host=self.PARENT)
        self.assertEqual(told["sessionTitle"], "%s: Review the Parser" % self.label("p-0001"))
        self.assertIn('"@**Claude** %s"' % self.label("p-0001"), told["additionalContext"])

    def test_a_parent_the_app_runs_on_another_host_is_never_registered_from_here(self) -> None:
        # Its record is in this Mac's store, but the chat and its hook run over ssh, under the other location.
        self.app_record(self.PARENT, "p-0001", sshConfig={"sshHost": "the-server"})
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        calls = Calls(self)
        self.begin("c-0001", host=self.CHILD, transport=calls)
        self.assertEqual([(c[0], c[c.index("--session-id") + 1]) for c in calls.calls], [("register", "c-0001")])
        self.assertIsNone(self.row("p-0001"))
        self.assertIsNone(self.row("p-0001", "server"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "CLAUDE", "letters", "p-0001.json")))
        # The parent is still named to the assigner, which knows a parent on any host.
        self.assertIn("--parent-session-id=p-0001", calls.calls[0])

    def test_a_parent_registered_under_the_other_location_is_followed_without_a_second_registration(self) -> None:
        self.app_record(self.PARENT, "p-0001", sshConfig={"sshHost": "the-server"})
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        # The parent's own hook, on the server, where there is no app store.
        server = tempfile.mkdtemp(dir=self.tmp.name)
        out = io.StringIO()
        PL.hook(PC.SPEC, dict(self.claude_env(), HOME=server, SIMPLE_TOOLS_STATE_DIR=server),
                io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "p-0001",
                                        "source": "startup"})), out, platform="linux", location_flag="server")
        parent = self.row("p-0001", "server")["label"]
        self.begin("c-0001", host=self.CHILD)
        self.assert_placed_after(self.label("c-0001"), parent)
        self.assertIsNone(self.row("p-0001"))  # no second registration of the parent under this Mac's location

    def test_a_parent_that_holds_a_letter_costs_no_call(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        self.begin("p-0001", host=self.PARENT)
        calls = Calls(self)
        self.begin("c-0001", host=self.CHILD, transport=calls)
        self.assertEqual([(c[0], c[c.index("--session-id") + 1]) for c in calls.calls], [("register", "c-0001")])

    def test_a_grandparent_is_registered_first_too(self) -> None:
        grand = "local_cccccccc-0003"
        self.app_record(grand, "g-0001")
        self.app_record(self.PARENT, "p-0001", started_by=grand)
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        calls = Calls(self)
        self.begin("c-0001", host=self.CHILD, transport=calls)
        self.assertEqual([c[c.index("--session-id") + 1] for c in calls.calls], ["g-0001", "p-0001", "c-0001"])
        self.assert_placed_after(self.label("p-0001"), self.label("g-0001"))
        self.assertEqual(self.note("CLAUDE", "p-0001")["parent"], "g-0001")


class TitleAdviceTests(HookBase):
    """The app keeps the title another session gave a chat (`titleSource` "tool") over the hook's `sessionTitle`,
    so the chat's own model is told to put the letter on it, once."""

    def test_a_title_an_agent_set_gets_the_model_to_put_the_letter_back(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT, title="iPhone parity", source="tool")
        self.begin("p-0001", host=self.PARENT)
        child = self.begin("c-0001", host=self.CHILD)
        label = self.label("c-0001")
        context = child["additionalContext"]
        self.assertIn('title "%s: iPhone parity"' % label, context)
        self.assertIn("mcp__ccd_session_mgmt__set_session_title", context)
        self.assertIn('session_id "self"', context)
        self.assertIn("Claude %s" % label, context)  # the letter's own reminder is still there
        # One context, the reminder first, with the fleet's sentence gap between the lines and inside the advice.
        self.assertIn("renamed." + GAP + "The app is showing the title another session gave this chat", context)
        self.assertIn("from this hook." + GAP + "Before other work, call", context)
        self.assertTrue(context.endswith("deferred)." + GAP + "Do not mention this to the user."), context)

    def test_the_rename_is_asked_for_once_and_not_once_it_took(self) -> None:
        self.app_record(self.CHILD, "c-0001", title="Task", source="tool")
        t0 = time.time()
        start = self.begin("c-0001", host=self.CHILD, now=t0)
        self.assertIn("set_session_title", start["additionalContext"])
        first = self.ask("c-0001", host=self.CHILD, now=t0 + 1)
        self.assertNotIn("set_session_title", first.get("additionalContext", ""))
        self.assertEqual(self.ask("c-0001", host=self.CHILD, now=t0 + 2), {})  # nor on every prompt after it
        later = self.ask("c-0001", host=self.CHILD, now=t0 + PL.RETRY_SECONDS + 1)
        self.assertIn("set_session_title", later.get("additionalContext", ""))
        self.assertNotIn("sessionTitle", later)
        self.assertEqual(self.ask("c-0001", host=self.CHILD, now=t0 + PL.RETRY_SECONDS + 2), {})
        self.app_record(self.CHILD, "c-0001", title="%s: Task" % self.label("c-0001"), source="tool")
        done = self.ask("c-0001", host=self.CHILD, now=t0 + 3 * PL.RETRY_SECONDS)
        self.assertNotIn("set_session_title", done.get("additionalContext", ""))

    def test_only_an_agents_title_on_this_chats_own_record_asks_for_a_rename(self) -> None:
        for sid, record_cli, title, source in (
            ("a-0001", "a-0001", "Named By A Person", "user"),
            ("b-0001", "b-0001", "Generated", "auto"),
            ("d-0001", "someone-else", "Outer Chat", "tool"),
        ):
            with self.subTest(source=source):
                self.app_record(self.CHILD, record_cli, title=title, source=source)
                out = self.begin(sid, host=self.CHILD)
                self.assertNotIn("set_session_title", out["additionalContext"])
                self.assertEqual(self.ask(sid, host=self.CHILD).get("additionalContext", ""), "")

    def test_a_terminal_chat_is_asked_nothing(self) -> None:
        self.app_record(self.CHILD, "c-0001", title="Task", source="tool")
        out = self.begin("c-0001")  # no app id in its environment:  the hook's title is the one it shows
        self.assertNotIn("set_session_title", out["additionalContext"])

    def test_a_secret_in_an_agents_title_stops_the_rename_and_never_reaches_the_model(self) -> None:
        assigned = "pass" + "word=" + "Xk29dmQ7vLp3Zt8w"
        for sid, title in (("c-0001", "Rotate %s now" % secret_word()), ("c-0002", "Set %s today" % assigned)):
            with self.subTest(sid=sid):
                self.app_record(self.CHILD, sid, title=title, source="tool")
                out = self.begin(sid, host=self.CHILD)
                self.assertNotIn("set_session_title", out["additionalContext"])
                self.assertNotIn(title, json.dumps(out))
                self.assertNotIn(title, json.dumps(self.note("CLAUDE", sid)))

    def test_a_long_agent_title_is_cut_at_a_word(self) -> None:
        self.app_record(self.CHILD, "c-0001", title="word " * 100, source="tool")
        out = self.begin("c-0001", host=self.CHILD)
        asked = out["additionalContext"].split('title "', 1)[1].split('"', 1)[0]
        self.assertTrue(asked.endswith(" word"), asked[-12:])
        self.assertLessEqual(len(asked), len("AA: ") + PC.AGENT_TITLE_CHARS)

    def test_an_agent_title_of_new_chat_is_not_a_title(self) -> None:
        self.app_record(self.CHILD, "c-0001", title=PL.PLACEHOLDER, source="tool")
        out = self.begin("c-0001", host=self.CHILD)
        self.assertNotIn("set_session_title", out["additionalContext"])

    def test_an_earlier_letter_does_not_stack_and_a_parents_acronym_stays(self) -> None:
        self.app_record(self.CHILD, "c-0001", title="PR: Review", source="tool")
        out = self.begin("c-0001", host=self.CHILD)
        self.assertIn('title "%s: PR: Review"' % self.label("c-0001"), out["additionalContext"])

        def advice(title: str, label: str, held: tuple[str, ...]) -> str:
            self.app_record(self.CHILD, "c-0001", title=title, source="tool")
            return PC.claude_title_advice(PL.TitleRequest(
                platform="claude", session_id="c-0001", label=label, title="%s: Review" % label, words="Review",
                current="", reason="prompt", data={}, env=self.claude_env(self.CHILD), held=held,
            ))

        self.assertIn('title "QR: Review"', advice("QQ: Review", "QR", ("QR", "QQ")))
        self.assertIn('title "QR: Review"', advice("QQ: QP: Review", "QR", ("QR", "QQ", "QP")))
        self.assertIn('title "QR: Review"', advice("??: Review", "QR", ("QR",)))
        self.assertIn('title "QR: PR: Review"', advice("PR: Review", "QR", ("QR", "QQ")))
        self.assertEqual(advice("QR: Review", "QR", ("QR", "QQ")), "")  # the letter is on it already
        self.assertEqual(advice("QQ: Review", "", ()), "")  # a chat with no letter is asked nothing

    def test_a_chat_with_no_letter_is_asked_nothing_and_not_retitled_on_every_prompt(self) -> None:
        # The app shows the title another session gave the chat, and the assigner is down.
        self.app_record(self.CHILD, "c-0001", title="Task", source="tool")
        down = Calls(self, fail=TimeoutError("down"))
        t0 = time.time()
        start = self.answer({"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "startup",
                             "session_title": "Task"}, host=self.CHILD, transport=down, now=t0)
        self.assertEqual(start["sessionTitle"], "??: Task")
        self.assertNotIn("set_session_title", start["additionalContext"])
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "c-0001", "prompt": "go",
                  "session_title": "Task"}  # the app kept its own title over "??: Task"
        for n in range(1, 4):
            self.assertEqual(self.answer(prompt, host=self.CHILD, transport=down, now=t0 + n), {})
        # The letter that comes later goes on the title, and the model is told to put it on the app's.
        later = self.answer(prompt, host=self.CHILD, now=t0 + PL.RETRY_SECONDS + 1)
        label = self.label("c-0001")
        self.assertEqual(later["sessionTitle"], "%s: Task" % label)
        self.assertIn('title "%s: Task"' % label, later["additionalContext"])
        self.assertIn('"@**Claude** %s"' % label, later["additionalContext"])

    def test_a_chat_the_app_just_started_is_told_to_put_its_letter_on_the_apps_title(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, started_by=self.PARENT)  # the app has not written its session id yet
        parent = self.begin("p-0001", host=self.PARENT)
        child = self.begin("c-0001", host=self.CHILD)
        label = self.label("c-0001")
        self.assertIn("mcp__ccd_session_mgmt__get_session", child["additionalContext"])
        self.assertIn("mcp__ccd_session_mgmt__set_session_title", child["additionalContext"])
        self.assertIn('"%s: <that title>"' % label, child["additionalContext"])
        self.assertIn("renamed." + GAP + "Another chat started this one", child["additionalContext"])
        self.assertNotIn("set_session_title", parent["additionalContext"])
        # Its first prompt, once the record is its own, names the exact title.
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT, title="Port The Parser", source="tool")
        first = self.ask("c-0001", host=self.CHILD)
        self.assertIn('title "%s: Port The Parser"' % label, first["additionalContext"])

    def test_a_chat_the_app_started_with_a_record_of_its_own_is_told_too(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)  # its own record, no title in it yet
        self.begin("p-0001", host=self.PARENT)
        child = self.begin("c-0001", host=self.CHILD)
        self.assertIn('"%s: <that title>"' % self.label("c-0001"), child["additionalContext"])

    def test_a_resumed_chat_the_app_started_is_not_told_again(self) -> None:
        self.app_record(self.PARENT, "p-0001")
        self.app_record(self.CHILD, "c-0001", started_by=self.PARENT)
        self.begin("p-0001", host=self.PARENT)
        self.begin("c-0001", host=self.CHILD)
        label = self.label("c-0001")
        again = self.answer({"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "resume",
                             "session_title": "%s: Fix It" % label}, host=self.CHILD,
                            now=time.time() + 2 * PL.RETRY_SECONDS)
        self.assertIn("Claude %s" % label, again["additionalContext"])
        self.assertNotIn("set_session_title", again["additionalContext"])

    def test_the_title_reader_takes_only_the_title_fields_of_the_chats_own_record(self) -> None:
        self.app_record(self.CHILD, "c-0001", title="Task", source="tool", messages=["hello"],
                        remoteMcpServersConfig={"token": "do-not-leak"})
        record = PC._load(PC._find(PC.store_root(self.home), self.CHILD), PC._project_title)
        self.assertEqual(record, {"cliSessionId": "c-0001", "title": "Task", "titleSource": "tool"})
        self.assertEqual(PC.shown_title(self.home, host_session_id=self.CHILD, cli_session_id="c-0001"),
                         ("tool", "Task"))
        for host, cli in ((self.CHILD, "x-0001"), ("", "c-0001"), ("local_zzzzzzzz-0009", "c-0001"),
                          ("../../x", "c-0001"), (self.CHILD, "")):
            self.assertIsNone(PC.shown_title(self.home, host_session_id=host, cli_session_id=cli), (host, cli))
        with mock.patch.object(PC, "shown_title", side_effect=RuntimeError("the app changed its file")):
            self.assertNotIn("set_session_title", self.begin("c-0001", host=self.CHILD)["additionalContext"])


class ArchiveStatesTests(StoreBase):
    """`archive_states`:  which chats the app has archived.  Any doubt keeps a chat."""

    def record(self, host: str, cli: str, archived: object = True, **extra) -> str:
        return write(self.home, host, dict({"sessionId": host, "cliSessionId": cli, "isArchived": archived,
                                            "title": "Secret Title"}, **extra))

    def test_each_chat_the_app_holds_is_archived_or_not(self) -> None:
        self.record("local_aaaaaaaa-0001", "s-0001")
        self.record("local_aaaaaaaa-0002", "s-0002", archived=False)
        self.record("local_aaaaaaaa-0003", "s-0003")  # not asked about
        states = PC.archive_states(self.home, frozenset({"s-0001", "s-0002", "s-0009"}))
        self.assertEqual(states, {"s-0001": PC.ARCHIVED, "s-0002": PC.UNARCHIVED})  # s-0009 has no record:  left out

    def test_no_store_is_none_and_nothing_asked_is_nothing(self) -> None:
        self.assertIsNone(PC.archive_states(self.home, {"s-0001"}))
        self.assertIsNone(PC.archive_states(self.home, ()))
        self.record("local_aaaaaaaa-0001", "s-0001")
        self.assertEqual(PC.archive_states(self.home, ()), {})
        self.assertEqual(PC.archive_states(self.home, {"", None, 7}), {})

    def test_only_a_true_flag_is_an_archive(self) -> None:
        for n, flag in enumerate(("true", 1, None, "yes", [True], {"a": 1})):
            self.record("local_aaaaaaaa-%04d" % n, "s-%04d" % n, archived=flag)
        write(self.home, "local_aaaaaaaa-0099", {"cliSessionId": "s-0099"})  # no flag at all
        states = PC.archive_states(self.home, {"s-%04d" % n for n in range(6)} | {"s-0099"})
        self.assertEqual(set(states.values()), {PC.UNARCHIVED})
        self.assertEqual(len(states), 7)

    def test_one_unarchived_record_of_two_keeps_the_chat(self) -> None:
        self.record("local_aaaaaaaa-0001", "s-0001")
        self.record("local_aaaaaaaa-0009", "s-0001", archived=False)
        self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {"s-0001": PC.UNARCHIVED})
        self.record("local_aaaaaaaa-0009", "s-0001", archived=True)
        self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {"s-0001": PC.ARCHIVED})

    def test_an_unreadable_record_that_mentions_the_chat_keeps_it(self) -> None:
        self.record("local_aaaaaaaa-0001", "s-0001")
        write(self.home, "local_aaaaaaaa-0003", '{"cliSessionId": "s-0001", "isArchived": tr')  # never finished
        write(self.home, "local_aaaaaaaa-0004", '{"cliSessionId": "s-0004", "isArchived": tr')
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            states = PC.archive_states(self.home, {"s-0001", "s-0004"})
        self.assertEqual(states, {"s-0001": PC.UNREADABLE, "s-0004": PC.UNREADABLE})

    def test_a_record_caught_mid_write_is_read_again(self) -> None:
        path = self.record("local_aaaaaaaa-0001", "s-0001")
        with open(path, encoding="utf-8") as fh:
            whole = fh.read()
        reads = [whole[: whole.index('"isArchived"') + 5]]  # the id is written, the flag is not yet
        real = PC._read

        def read(p: str):
            return reads.pop(0) if reads and p == path else real(p)

        with mock.patch.object(PC, "_read", side_effect=read), mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {"s-0001": PC.ARCHIVED})

    def test_a_record_caught_before_its_session_id_was_written_still_keeps_a_chat_it_mentions(self) -> None:
        # A whole record names its chat as a `cliSessionId` value.  One caught mid-write may not have got that far,
        # so any mention of the id in it counts, and the chat is kept.
        self.record("local_aaaaaaaa-0001", "s-0001")
        write(self.home, "local_aaaaaaaa-0002", '{"sessionId": "local_aaaaaaaa-0002", "lastAssistantCliSessionId": "s-0001", "isArch')
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {"s-0001": PC.UNREADABLE})
        # The same words in a whole record of another chat say nothing of this one.
        write(self.home, "local_aaaaaaaa-0002", {"sessionId": "local_aaaaaaaa-0002", "cliSessionId": "s-0002",
                                                  "lastAssistantCliSessionId": "s-0001", "isArchived": False})
        self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {"s-0001": PC.ARCHIVED})

    def test_many_ids_cost_one_pass_over_each_record(self) -> None:
        for n in range(30):
            self.record("local_aaaaaaaa-%04d" % n, "s-%04d" % n, archived=n % 2 == 0)
        reads: list[str] = []
        real = PC._read
        with mock.patch.object(PC, "_read", side_effect=lambda path: reads.append(path) or real(path)):
            states = PC.archive_states(self.home, {"s-%04d" % n for n in range(200)})
        self.assertEqual(len(reads), 30)
        self.assertEqual(sum(1 for state in states.values() if state == PC.ARCHIVED), 15)
        self.assertEqual(len(states), 30)

    def test_a_record_that_only_mentions_the_chat_says_nothing_of_it(self) -> None:
        self.record("local_aaaaaaaa-0001", "s-0002", notes="see s-0001")  # another chat's, archived
        self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {})

    def test_the_bounds_can_only_keep_a_letter(self) -> None:
        old = time.time() - 86400
        os.utime(self.record("local_aaaaaaaa-0001", "s-0001"), (old, old))
        self.record("local_aaaaaaaa-0002", "s-0002", archived=False, padding="x" * 500)
        self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {"s-0001": PC.ARCHIVED})
        with mock.patch.object(PC, "ARCHIVE_SCAN_RECORDS", 1):
            self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {})  # past the bound:  not found, so kept
        with mock.patch.object(PC, "ARCHIVE_SCAN_BYTES", 100):
            self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {})
        with mock.patch.object(PC, "RECORD_BYTES", 300):  # a record too big to read is not read, and says nothing
            self.assertEqual(PC.archive_states(self.home, {"s-0001", "s-0002"}), {"s-0001": PC.ARCHIVED})

    def test_symlinks_are_not_followed_and_only_two_fields_leave_a_record(self) -> None:
        real = write(self.home, "elsewhere", {"cliSessionId": "s-0001", "isArchived": True})
        os.symlink(real, os.path.join(os.path.dirname(real), "local_aaaaaaaa-0001.json"))
        self.assertEqual(PC.archive_states(self.home, {"s-0001"}), {})
        path = self.record("local_aaaaaaaa-0002", "s-0002", remoteMcpServersConfig={"token": "do-not-leak"})
        self.assertEqual(PC._load(path, PC._project_archive), {"cliSessionId": "s-0002", "isArchived": True})


class GoneChatsTests(HookBase):
    """An archived chat releases its letter (AFC-694).  Archiving reaches no hook, so the Claude row names the chats
    the desktop app has archived (`claude_gone`, from the app's own `isArchived` flag) and the library's gone report,
    made on a chat's start, has the assigner close them.  Every other chat is left alone."""

    def setUp(self) -> None:
        super().setUp()
        self.t0 = time.time() - 5 * 3600
        self.due = self.t0 + PL.GONE_REPORT_SECONDS + 1

    def archived_record(self, host: str, cli: str, archived: bool = True, title: str = "Secret Title") -> str:
        return write(self.home, host, {"sessionId": host, "cliSessionId": cli, "isArchived": archived,
                                        "title": title})

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

    def test_the_claude_row_names_its_gone_chats(self) -> None:
        self.assertIs(PC.SPEC.gone_sessions, PC.claude_gone)

    def test_only_a_chat_every_record_calls_archived_is_gone(self) -> None:
        env = {"HOME": self.home}
        ids = frozenset("s-%04d" % n for n in range(1, 7))
        self.assertEqual(PC.claude_gone(env, ids), set())  # no app store here (the server, a terminal-only Mac)
        self.archived_record("local_aaaaaaaa-0001", "s-0001")
        self.archived_record("local_aaaaaaaa-0002", "s-0002", archived=False)
        # s-0003 has no record at all:  a terminal chat.
        write(self.home, "local_aaaaaaaa-0004", '{"cliSessionId": "s-0004", "isArchived": tr')  # never finished
        self.archived_record("local_aaaaaaaa-0005", "s-0005")
        self.archived_record("local_bbbbbbbb-0005", "s-0005", archived=False)  # two records, and one says no
        self.archived_record("local_aaaaaaaa-0006", "s-0006")
        self.archived_record("local_bbbbbbbb-0006", "s-0006")  # two records, and both say archived
        self.archived_record("local_aaaaaaaa-0007", "s-0007")  # archived, and not asked about
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            gone = PC.claude_gone(env, ids)
        self.assertEqual(gone, {"s-0001", "s-0006"})
        self.assertEqual(PC.claude_gone(env, ()), set())

    def test_a_start_reports_the_chats_the_app_has_archived_and_their_letters_are_freed(self) -> None:
        other = "local_cccccccc-0003"
        self.app_record(self.CHILD, "c-0001", title="Secret Title", source="auto")
        self.app_record(other, "c-0002")
        self.begin("c-0001", host=self.CHILD, now=self.t0)
        self.begin("c-0002", host=other, now=self.t0)
        label = self.label("c-0001")
        self.app_record(self.CHILD, "c-0001", title="Secret Title", source="auto", isArchived=True)
        with self.reports() as sent:
            out = self.begin("c-0003", now=self.due)
        self.assertEqual(sent, [{"platform": "claude", "verified_host": "mac", "profile_scope": "default",
                                 "native_session_ids": ["c-0001"]}])
        self.assertEqual((self.row_state("c-0001"), self.row_state("c-0002")), ("closed", "active"))
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % self.label("c-0003"))
        # The letter is free:  the registry no longer resolves it to any live chat.
        self.assertFalse(registry_api.call(self.env(), "resolve", {"platform": "claude", "letter": label})["found"])
        # Unarchived and resumed, the chat checks with the Operator again and holds a letter once more.
        self.app_record(self.CHILD, "c-0001", isArchived=False)
        self.begin("c-0001", host=self.CHILD, source="resume", now=self.due + 60)
        self.assertEqual(self.row_state("c-0001"), "active")
        self.assertRegex(self.label("c-0001"), r"^[A-Z]{2}$")
        self.assertNotIn("gone_at", self.note("CLAUDE", "c-0001"))

    def test_any_doubt_keeps_the_letter(self) -> None:
        for sid in ("s-0001", "s-0002", "s-0003", "s-0004"):
            self.begin(sid, now=self.t0)
        self.archived_record("local_aaaaaaaa-0001", "s-0001", archived=False)
        # s-0002 has no record at all:  a terminal chat.
        write(self.home, "local_aaaaaaaa-0003", '{"cliSessionId": "s-0003", "isArchived": tr')  # never finished
        self.archived_record("local_aaaaaaaa-0004", "s-0004")
        self.archived_record("local_aaaaaaaa-0009", "s-0004", archived=False)
        with self.reports() as sent, mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.begin("s-0005", now=self.due)
        self.assertEqual(sent, [])
        for sid in ("s-0001", "s-0002", "s-0003", "s-0004"):
            self.assertEqual(self.row_state(sid), "active", sid)
            self.assertNotIn("gone_at", self.note("CLAUDE", sid))

    def test_nothing_the_apps_records_can_do_fails_a_start(self) -> None:
        self.begin("s-0001", now=self.t0)
        self.archived_record("local_aaaaaaaa-0001", "s-0001")
        with mock.patch.object(PC, "archive_states", side_effect=RuntimeError("the app changed its file")), \
                self.reports() as sent:
            out = self.begin("s-0002", now=self.due)
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % self.label("s-0002"))
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("s-0001"), "active")

    def test_the_verb_shows_what_would_be_reported_and_never_a_title(self) -> None:
        sid = "0123456789abcdef-0001"
        self.begin(sid, now=self.t0)
        self.begin("s-0002", now=self.t0)
        self.archived_record("local_aaaaaaaa-0001", sid)
        out = io.StringIO()
        with self.reports() as sent:
            code = PL.gone_main(["--platform", "claude"], self.env(), out, now=self.due, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().splitlines(), [
            "would report Claude %s (01234567, mac)" % self.label(sid),
            "Claude gone report, mac:  1 gone  (dry run:  nothing sent;  --apply reports them now)",
            "Claude gone report, server:  0 gone  (dry run:  nothing sent;  --apply reports them now)",
        ])
        self.assertEqual(sent, [])
        out = io.StringIO()
        code = PL.gone_main(["--platform", "claude", "--apply"], self.env(), out, now=self.due, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("reported Claude %s (01234567, mac)" % self.label(sid), out.getvalue())
        self.assertNotIn("Secret Title", out.getvalue())
        self.assertEqual(self.row_state(sid), "closed")

    def test_simple_chat_letter_routes_the_verb_and_its_earlier_name(self) -> None:
        for script, verb in (("simple-chat", "gone"), ("simple-chat", "sweep-archived"), ("platform-letter", "gone")):
            argv = [sys.executable, "-I", "-B", str(SCRIPTS / script)]
            argv += (["letter"] if script == "simple-chat" else []) + [verb, "--platform", "claude", "--location", "mac"]
            with self.subTest(script=script, verb=verb), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run(
                    argv, capture_output=True, text=True, timeout=60,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "SIMPLE_TOOLS_STATE_DIR": tmp},
                )
                self.assertEqual(
                    (result.returncode, result.stdout),
                    (0, "Claude gone report, mac:  0 gone  (dry run:  nothing sent;  --apply reports them now)\n"
                        "Claude gone report, server:  0 gone  (dry run:  nothing sent;  --apply reports them now)\n"),
                    result.stderr,
                )
                self.assertEqual(os.listdir(tmp), [])


class GoneElsewhereTests(HookBase):
    """Most Claude chats run on the server through the Mac's desktop app.  Their notes are on the server and the
    app's records are on the Mac, so the Mac names the archived ones from its records (`claude_gone_elsewhere`) and
    reports them for the location they run at, `server`, in a report of their own."""

    def setUp(self) -> None:
        super().setUp()
        self.t0 = time.time() - 5 * 3600
        self.due = self.t0 + PL.GONE_REPORT_SECONDS + 1
        self.server_home = os.path.join(self.tmp.name, "server-home")
        os.makedirs(self.server_home)

    def record(self, host: str, cli: str, *, archived: object = True, ssh: object = True, age: float = 7200,
               **extra) -> str:
        """One record of the app, last changed `age` seconds ago."""
        body = {"sessionId": host, "cliSessionId": cli, "isArchived": archived, "title": "Secret Title", **extra}
        if ssh is not False:
            body["sshConfig"] = {"sshHost": "the-server"} if ssh is True else ssh
        path = write(self.home, host, body)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def server_chat(self, sid: str, now: float | None = None) -> str:
        """The chat's own hook, on the server, where there is no app store.  Answers its letter."""
        env = dict(self.claude_env(), HOME=self.server_home,
                   SIMPLE_TOOLS_STATE_DIR=os.path.join(self.server_home, "state"))
        PL.hook(PC.SPEC, env, io.StringIO(json.dumps(
            {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"})), io.StringIO(),
            now=self.t0 if now is None else now, platform="linux", location_flag="server")
        return self.row(sid, "server")["label"]

    def row_state(self, sid: str, location: str = "mac") -> str:
        return self.row(sid, location)["state"]

    def gone(self) -> set[str]:
        return PC.claude_gone_elsewhere({"HOME": self.home}, "server")

    @contextmanager
    def reports(self):
        seen: list[dict] = []
        real = registry_api._open

        def capture(request, timeout):
            if request.full_url.endswith("/report-gone"):
                seen.append(json.loads(request.data.decode("utf-8")))
            return real(request, timeout)

        with mock.patch.object(registry_api, "_open", capture):
            yield seen

    def test_the_claude_row_names_chats_that_run_elsewhere(self) -> None:
        self.assertIs(PC.SPEC.gone_elsewhere, PC.claude_gone_elsewhere)
        self.assertEqual(PC.REMOTE_LOCATION, "server")

    def test_a_mac_with_an_archived_ssh_chat_and_an_archived_local_chat_sends_two_reports(self) -> None:
        local, remote = "local_aaaaaaaa-0001", "local_bbbbbbbb-0002"
        self.begin("m-0001", host=local, now=self.t0)
        self.begin("m-0002", now=self.t0)
        for sid in ("r-0001", "r-0002", "r-0003"):
            self.server_chat(sid)
        self.record(local, "m-0001", ssh=False)  # archived, and it runs here
        self.record(remote, "r-0001")  # archived, and the app runs it over ssh
        self.record("local_cccccccc-0003", "r-0002", archived=False)  # run over ssh, not archived
        letter = self.row("r-0001", "server")["label"]
        with self.reports() as sent:
            out = self.begin("m-0003", now=self.due)
        self.assertEqual(sent, [
            {"platform": "claude", "verified_host": "mac", "profile_scope": "default",
             "native_session_ids": ["m-0001"]},
            {"platform": "claude", "verified_host": "server", "profile_scope": "default",
             "native_session_ids": ["r-0001"]},
        ])
        self.assertEqual((self.row_state("m-0001"), self.row_state("m-0002")), ("closed", "active"))
        self.assertEqual([self.row_state(sid, "server") for sid in ("r-0001", "r-0002", "r-0003")],
                         ["closed", "active", "active"])
        self.assertIsNone(self.row("r-0001"))  # nothing was made up under this Mac's location
        self.assertFalse(registry_api.call(self.env(), "resolve", {"platform": "claude", "letter": letter})["found"])
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % self.label("m-0003"))
        self.assertNotIn("Secret Title", json.dumps(sent))
        # The server chat, alive after all, hears of it on its next touch and registers again.
        env = dict(self.claude_env(), HOME=self.server_home,
                   SIMPLE_TOOLS_STATE_DIR=os.path.join(self.server_home, "state"))
        PL.hook(PC.SPEC, env, io.StringIO(json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": "r-0001", "prompt": "still here"})), io.StringIO(),
            now=self.due + PL.TOUCH_SECONDS, platform="linux", location_flag="server")
        self.assertEqual(self.row_state("r-0001", "server"), "active")

    def test_an_unarchived_or_doubtful_ssh_record_is_not_named(self) -> None:
        self.record("local_aaaaaaaa-0001", "r-0001")  # the one that is named
        self.record("local_aaaaaaaa-0002", "r-0002", archived=False)
        self.record("local_aaaaaaaa-0003", "r-0003", ssh=False)  # archived, and it runs on this Mac
        self.record("local_aaaaaaaa-0004", "r-0004", ssh={})  # no ssh host at all
        self.record("local_aaaaaaaa-0005", "r-0005", archived="true")  # only a true flag is an archive
        self.record("local_aaaaaaaa-0006", "../not-an-id")
        write(self.home, "local_aaaaaaaa-0007", {"isArchived": True, "sshConfig": {"sshHost": "x"}})  # never ran
        self.record("local_aaaaaaaa-0008", "r-0008")
        self.record("local_bbbbbbbb-0008", "r-0008", archived=False, ssh=False)  # two records, and one says no
        self.record("local_aaaaaaaa-0009", "r-0009")
        os.utime(write(self.home, "local_bbbbbbbb-0009", '{"cliSessionId": "r-0009", "isArchived": tr'),
                 (time.time() - 7200,) * 2)  # a second record of it, caught mid-write
        os.utime(write(self.home, "local_aaaaaaaa-0010", '{"cliSessionId": "r-0010", "sshConfig": {}, "isArchived": tr'),
                 (time.time() - 7200,) * 2)  # unreadable:  it names nothing
        self.record("local_aaaaaaaa-0011", "r-0011")
        self.record("local_bbbbbbbb-0011", "r-0011")  # two records, and both say archived
        with mock.patch.object(PC, "RETRY_PAUSE", 0):
            self.assertEqual(self.gone(), {"r-0001", "r-0011"})
            self.assertEqual(PC.claude_gone_elsewhere({"HOME": self.home}, "mac"), set())  # only the ssh location
        self.assertEqual(PC.claude_gone_elsewhere({"HOME": self.server_home}, "server"), set())  # no app store

    def test_how_far_back_it_looks_is_bounded(self) -> None:
        self.record("local_aaaaaaaa-0001", "r-0001", age=2 * 3600)
        self.record("local_aaaaaaaa-0002", "r-0002", age=600)  # changed ten minutes ago:  its chat may be working
        self.record("local_aaaaaaaa-0003", "r-0003", age=13 * 86400)
        self.record("local_aaaaaaaa-0004", "r-0004", age=15 * 86400)  # older than the window
        self.assertEqual(self.gone(), {"r-0001", "r-0003"})
        self.assertEqual((PC.REMOTE_SCAN_DAYS, PC.REMOTE_SCAN_RECORDS, PC.REMOTE_QUIET_SECONDS), (14, 300, 3600))
        with mock.patch.object(PC, "REMOTE_SCAN_RECORDS", 2):  # the newest two records:  r-0002 and r-0001
            self.assertEqual(self.gone(), {"r-0001"})
        with mock.patch.object(PC, "REMOTE_SCAN_DAYS", 1):
            self.assertEqual(self.gone(), {"r-0001"})
        with mock.patch.object(PC, "RECORD_BYTES", 10):  # a record too big to read names nothing
            self.assertEqual(self.gone(), set())

    def test_only_three_fields_leave_a_record(self) -> None:
        path = self.record("local_aaaaaaaa-0001", "r-0001", remoteMcpServersConfig={"token": "do-not-leak"})
        self.assertEqual(PC._load(path, PC._project_remote),
                         {"cliSessionId": "r-0001", "isArchived": True, "remote": True})

    def test_the_remote_report_is_throttled_by_its_own_stamp(self) -> None:
        for sid in ("r-0001", "r-0002", "r-0003", "r-0004"):
            self.server_chat(sid)
        with self.reports() as sent:
            self.begin("m-0001", now=self.t0)
            self.record("local_aaaaaaaa-0001", "r-0001")
            self.begin("m-0002", now=self.t0 + 60)  # inside the window of the first start's report
            self.assertEqual(sent, [])
            self.begin("m-0003", now=self.due)
            self.assertEqual([body["native_session_ids"] for body in sent], [["r-0001"]])
            self.record("local_aaaaaaaa-0002", "r-0002")
            self.begin("m-0004", now=self.due + 60)
            self.assertEqual(len(sent), 1)
            self.begin("m-0005", now=self.due + PL.GONE_REPORT_SECONDS + 1)
        # The second report names only the new one:  r-0001 is still archived, and it was reported.
        self.assertEqual([body["native_session_ids"] for body in sent], [["r-0001"], ["r-0002"]])
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "CLAUDE", "letters")))[:2],
                         [".gone-claude-mac", ".gone-claude-server"])

    def test_a_refused_remote_report_closes_nothing_and_is_cut_to_what_the_assigner_allows(self) -> None:
        for sid in ("r-0001", "r-0002", "r-0003", "r-0004"):
            self.server_chat(sid)
        for n in (1, 2, 3):
            self.record("local_aaaaaaaa-000%d" % n, "r-000%d" % n)
        with self.reports() as sent:
            out = self.begin("m-0001", now=self.t0)  # 3 of the server's 4 live rows:  over the share
            self.assertEqual(out["sessionTitle"], "%s: New Chat" % self.label("m-0001"))
            self.assertEqual(sent[-1]["native_session_ids"], ["r-0001", "r-0002", "r-0003"])
            self.assertEqual([self.row_state("r-000%d" % n, "server") for n in (1, 2, 3)], ["active"] * 3)
            self.begin("m-0002", now=self.due)
        self.assertEqual(sent[-1]["native_session_ids"], ["r-0001", "r-0002"])
        self.assertEqual([self.row_state("r-000%d" % n, "server") for n in (1, 2, 3)],
                         ["closed", "closed", "active"])

    def test_the_verb_shows_both_locations(self) -> None:
        self.server_chat("0123456789abcdef-0001")
        self.record("local_aaaaaaaa-0001", "0123456789abcdef-0001")
        out = io.StringIO()
        code = PL.gone_main(["--platform", "claude"], self.env(), out, now=self.due, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().splitlines(), [
            "Claude gone report, mac:  0 gone  (dry run:  nothing sent;  --apply reports them now)",
            "would report Claude - (01234567, server)",
            "Claude gone report, server:  1 gone  (dry run:  nothing sent;  --apply reports them now)",
        ])


if __name__ == "__main__":
    unittest.main()
