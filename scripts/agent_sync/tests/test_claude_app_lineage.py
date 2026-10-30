"""`claude_app_lineage` (AFC-564):  which chat started this one, read from the desktop app's record of the chat.
The store is a temporary one laid out the way the app lays out its own."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from agent_sync import claude_app_lineage as AL

PARENT_APP = "local_11111111-1111-4111-8111-111111111111"
PARENT_CLI = "33333333-3333-4333-8333-333333333333"
CHILD_APP = "local_22222222-2222-4222-8222-222222222222"
CHILD_CLI = "44444444-4444-4444-8444-444444444444"  # a chat's app id is not its Claude Code UUID


def write(home: str, host: str, body: dict | str, account: str = "acct", org: str = "org") -> str:
    folder = os.path.join(AL.store_root(home), account, org)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, host + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body if isinstance(body, str) else json.dumps(body))
    return path


class Base(unittest.TestCase):
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
        return AL.parent_of(self.home, **kw)


class ParentOfTests(Base):
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
        root = AL.store_root(self.home)
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI})  # make the store (and an org folder) exist
        # Proof the file is reachable that way:  with the id check switched off, the hostile id finds it.
        with mock.patch.object(AL, "HOST_ID", re.compile(".*")):
            self.assertTrue(os.path.samefile(AL._find(root, hostile), outside))
        for bad in (hostile, "local_" + hostile, "local_/../../../../../outside", "local_a/b", "/etc/passwd", "local_" + "a" * 200, ""):
            self.assertEqual(AL._find(root, bad), "", bad)
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": "local_" + hostile}})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.PARENT_GONE))
        with mock.patch.object(AL, "HOST_ID", re.compile(".*")):  # and the same record, were the check off, would leak
            write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": hostile}})
            self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), (PARENT_CLI, ""))

    def test_a_parent_uuid_that_is_not_a_session_id_is_ignored(self) -> None:
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP, "cliSessionId": "../../etc"})
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}})
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP, "cliSessionId": 7})
        self.assertEqual(self.parent(host_session_id=CHILD_APP), "")

    def test_malformed_records_answer_no_parent(self) -> None:
        for body in ("", "not json", "[1, 2]", "null", '{"spawnedFrom": "x"}', '{"spawnedFrom": {"sessionId": 5}}'):
            write(self.home, CHILD_APP, body)
            self.assertEqual(self.parent(host_session_id=CHILD_APP), "", body)

    def test_a_record_caught_mid_write_is_read_again(self) -> None:
        self.family()
        real = AL._read
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            return '{"cliSes' if calls["n"] == 1 else real(path)

        with mock.patch.object(AL, "_read", flaky):
            self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)
        self.assertGreaterEqual(calls["n"], 3)

    def test_a_record_that_stays_broken_is_given_up_on(self) -> None:
        self.family()
        with mock.patch.object(AL, "_read", lambda path: "{"):
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
        with mock.patch.object(AL, "RECORD_BYTES", 300):
            self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.UNREADABLE))
            # the same size rule keeps the scan from reading it, and the scan says nothing was found
            self.assertEqual(AL.lookup(self.home, cli_session_id=CHILD_CLI), ("", AL.NO_RECORD))

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
        child_path = AL._find(AL.store_root(self.home), CHILD_APP)
        old = time.time() - 86400
        os.utime(child_path, (old, old))
        for i in range(3):
            write(self.home, "local_0000000%d-0000" % i, {"cliSessionId": "other-%d-session" % i})
        with mock.patch.object(AL, "SCAN_RECORDS", 2):
            self.assertEqual(self.parent(cli_session_id=CHILD_CLI), "")  # the child's record is past the newest two
        self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_the_reason_no_parent_is_known_is_named(self) -> None:
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.NO_STORE))
        write(self.home, "local_00000000-0000", {"cliSessionId": "other-0000"})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.NO_RECORD))
        self.assertEqual(AL.lookup(self.home), ("", AL.NO_RECORD))
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.NOT_STARTED))
        self.family(spawnedFrom={"sessionId": PARENT_APP, "deletedParent": True})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.PARENT_GONE))
        self.family(spawnedFrom={"sessionId": "local_deadbeef-0000"})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.PARENT_GONE))
        self.family()
        write(self.home, PARENT_APP, {"sessionId": PARENT_APP})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.PARENT_NEVER_RAN))
        write(self.home, PARENT_APP, {"cliSessionId": CHILD_CLI})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.SAME_CHAT))
        write(self.home, PARENT_APP, "{")
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.UNREADABLE))
        self.family()
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), (PARENT_CLI, ""))

    def test_only_three_fields_leave_a_record(self) -> None:
        private = "my private title"
        write(
            self.home, CHILD_APP,
            {
                "cliSessionId": CHILD_CLI, "title": private, "messages": ["hello"],
                "spawnedFrom": {"sessionId": PARENT_APP, "title": private, "taskId": "t", "deletedParent": False},
                "remoteMcpServersConfig": {"token": "do-not-leak"},
            },
        )
        record = AL._load(AL._find(AL.store_root(self.home), CHILD_APP))
        self.assertEqual(
            record, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP, "deletedParent": False}}
        )
        self.assertNotIn(private, json.dumps(record))
        self.assertNotIn("do-not-leak", json.dumps(record))

    def test_a_deeply_nested_record_never_raises(self) -> None:
        # Older Pythons refuse this with RecursionError (unreadable);  3.15 parses it, and it names no parent.
        write(self.home, CHILD_APP, '{"a":' * 100000 + "1" + "}" * 100000)
        parent, why = AL.lookup(self.home, host_session_id=CHILD_APP)
        self.assertEqual(parent, "")
        self.assertIn(why, (AL.UNREADABLE, AL.NOT_STARTED))

    def test_a_record_the_parser_refuses_as_too_deep_is_unreadable_not_an_error(self) -> None:
        self.family()
        with mock.patch.object(AL.json, "loads", side_effect=RecursionError("maximum recursion depth exceeded")):
            self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.UNREADABLE))

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

        with mock.patch.object(AL.os, "scandir", scandir):
            self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_a_symlinked_record_is_not_scanned(self) -> None:
        write(self.home, PARENT_APP, {"cliSessionId": PARENT_CLI})
        real = write(self.home, "elsewhere", {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}})
        os.symlink(real, os.path.join(os.path.dirname(real), CHILD_APP + ".json"))
        self.assertEqual(AL.lookup(self.home, cli_session_id=CHILD_CLI), ("", AL.NO_RECORD))

    def test_a_symlinked_folder_is_not_followed(self) -> None:
        self.family()
        elsewhere = os.path.join(self.tmp.name, "elsewhere-org")
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, "local_99999999-0000.json"), "w", encoding="utf-8") as fh:
            json.dump({"cliSessionId": "ghost-0001", "spawnedFrom": {"sessionId": PARENT_APP}}, fh)
        os.symlink(elsewhere, os.path.join(AL.store_root(self.home), "acct", "org2"))
        self.assertEqual(AL.lookup(self.home, cli_session_id="ghost-0001"), ("", AL.NO_RECORD))
        self.assertEqual(AL._find(AL.store_root(self.home), "local_99999999-0000"), "")

    def test_the_scan_stops_at_its_byte_budget(self) -> None:
        self.family()
        old = time.time() - 86400
        os.utime(AL._find(AL.store_root(self.home), CHILD_APP), (old, old))
        for i in range(3):
            write(self.home, "local_0000000%d-0000" % i, {"cliSessionId": "other-%d-session" % i, "padding": "x" * 500})
        with mock.patch.object(AL, "SCAN_BYTES", 600):
            self.assertEqual(AL.lookup(self.home, cli_session_id=CHILD_CLI), ("", AL.NO_RECORD))
        self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)

    def test_files_too_big_to_read_still_count_toward_the_scan_budget(self) -> None:
        write(self.home, PARENT_APP, {"cliSessionId": PARENT_CLI})
        write(self.home, CHILD_APP, {"cliSessionId": CHILD_CLI, "spawnedFrom": {"sessionId": PARENT_APP}})
        old = time.time() - 86400
        os.utime(AL._find(AL.store_root(self.home), CHILD_APP), (old, old))
        for i in range(3):
            write(self.home, "local_0000000%d-0000" % i, {"cliSessionId": "other-%d-session" % i, "padding": "x" * 500})
        with mock.patch.object(AL, "RECORD_BYTES", 200), mock.patch.object(AL, "SCAN_BYTES", 350):
            self.assertEqual(AL.lookup(self.home, cli_session_id=CHILD_CLI), ("", AL.NO_RECORD))
        with mock.patch.object(AL, "RECORD_BYTES", 200):
            self.assertEqual(AL.lookup(self.home, cli_session_id=CHILD_CLI), (PARENT_CLI, ""))  # no budget in the way

    def test_a_host_id_whose_record_is_another_chats_is_not_used(self) -> None:
        # A claude started inside the child inherits CHILD_APP, so the record it names is the child's, not the nested
        # run's:  the nested run (its own session uuid) is not the child's sibling.
        self.family()
        self.assertEqual(
            AL.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="nested-0001"), ("", AL.NO_RECORD)
        )
        self.assertEqual(AL.parent_of(self.home, host_session_id=CHILD_APP, cli_session_id="nested-0001"), "")

    def test_a_nested_run_with_a_record_of_its_own_is_judged_by_that_record(self) -> None:
        self.family()
        write(self.home, "local_eeeeeeee-0005", {"cliSessionId": "mine-0001", "spawnedFrom": {"sessionId": PARENT_APP}})
        self.assertEqual(
            AL.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="mine-0001"), (PARENT_CLI, "")
        )
        write(self.home, "local_eeeeeeee-0005", {"cliSessionId": "mine-0001"})  # a record of a chat nobody started
        self.assertEqual(
            AL.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="mine-0001"), ("", AL.NOT_STARTED)
        )

    def test_a_host_id_with_a_matching_or_missing_session_uuid_is_used(self) -> None:
        self.family()
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP, cli_session_id=CHILD_CLI), (PARENT_CLI, ""))
        # a record that holds no session uuid (yet) cannot be told to be another chat's
        write(self.home, CHILD_APP, {"spawnedFrom": {"sessionId": PARENT_APP}})
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="anything-0001"), (PARENT_CLI, ""))

    def test_a_record_that_lags_after_a_clear_is_used_when_the_caller_allows_it(self) -> None:
        self.family()  # the record still holds CHILD_CLI, but the chat now has a new session uuid
        self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="cleared-0001")[0], "")
        self.assertEqual(
            AL.lookup(self.home, host_session_id=CHILD_APP, cli_session_id="cleared-0001", allow_lag=True),
            (PARENT_CLI, ""),
        )

    def test_a_record_read_half_written_is_read_again_after_a_pause(self) -> None:
        self.family()
        real = AL._read
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            return "" if calls["n"] <= 2 else real(path)  # empty twice, as a truncate-then-write leaves it

        with mock.patch.object(AL, "_read", flaky), mock.patch.object(AL.time, "sleep") as pause:
            self.assertEqual(self.parent(host_session_id=CHILD_APP), PARENT_CLI)
        self.assertEqual(pause.call_count, 2)  # one pause before each re-read
        pause.assert_called_with(AL.RETRY_PAUSE)

    def test_a_record_that_stays_empty_through_every_attempt_is_unreadable(self) -> None:
        self.family()
        with mock.patch.object(AL, "_read", lambda path: ""), mock.patch.object(AL.time, "sleep") as pause:
            self.assertEqual(AL.lookup(self.home, host_session_id=CHILD_APP), ("", AL.UNREADABLE))
        self.assertEqual(pause.call_count, AL.LOAD_ATTEMPTS - 1)

    def test_the_scan_does_not_trust_a_record_that_only_mentions_the_uuid(self) -> None:
        self.family()
        write(self.home, "local_99999999-0000", {"cliSessionId": "x" * 8, "notes": CHILD_CLI,
                                                  "spawnedFrom": {"sessionId": "local_deadbeef-0000"}})
        self.assertEqual(self.parent(cli_session_id=CHILD_CLI), PARENT_CLI)


if __name__ == "__main__":
    unittest.main()
