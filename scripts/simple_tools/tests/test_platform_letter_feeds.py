"""The letter client's own ways back to a letter (AFC-694):  `request` for a chat that has no note yet, the repair
a chat's start sets off (one detached run, no host timer), and each row's word on where its "??" chats come from."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import UTC, datetime
from unittest import mock

from simple_tools import platform_letter as PL
from simple_tools import platform_letter_antigravity as AG
from simple_tools import platform_letter_claude as CL
from simple_tools import platform_letter_zcode as ZC
from simple_tools import registry_api
from simple_tools.tests.test_platform_letter import PAIR, Base, make_spec

#: Rows whose `unlettered_sessions` is in an open pull request by its own seat, so this change does not edit the
#: row:  MiniMax's (#2292, `minimax_unlettered_sessions`).  The entry is harmless once that lands.
PENDING_FEEDS = {"minimax"}


class FeedTableTests(unittest.TestCase):
    def test_every_row_names_its_unlettered_chats_or_says_why_it_has_none(self) -> None:
        for platform in PL.adapter_platforms():
            spec = PL.load_spec(platform)
            with self.subTest(platform=platform):
                self.assertIsNotNone(spec)
                if spec.unlettered_sessions is not None:
                    # The repair replays a chat's start, so a row that names chats can make that start.
                    self.assertIsNotNone(spec.chat_payload)
                    self.assertEqual(spec.unlettered_feed, "", "a row names its chats or says why not, not both")
                elif platform not in PENDING_FEEDS:
                    self.assertTrue(spec.unlettered_feed.strip(), "%s says nothing about its ?? chats" % platform)

    def test_the_rows_that_list_their_chats(self) -> None:
        listed = {p for p in PL.adapter_platforms() if PL.load_spec(p).unlettered_sessions is not None}
        self.assertTrue({"antigravity", "claude", "codex", "zcode"} <= listed, listed)


class RepairAfterStartTests(Base):
    """A chat's start sets off one detached `platform-letter repair` run when a pass is due, and nothing else."""

    def setUp(self) -> None:
        super().setUp()
        self.spawned: list[list[str]] = []
        self.spec = make_spec(
            unlettered_sessions=lambda env: [],
            chat_payload=lambda sid: {"hook_event_name": "SessionStart", "source": "resume", "session_id": sid},
        )

    def spawn(self, argv, env) -> bool:
        self.spawned.append(list(argv))
        return True

    def chat(self, *, where: str = "server", transport=None, spec=None) -> PL.Chat:
        return PL.Chat(spec or self.spec, self.server_env(), where, "s-0001", now=time.time(), transport=transport)

    def test_a_due_pass_starts_one_detached_run_of_this_checkouts_repair(self) -> None:
        self.assertTrue(PL.repair_after_start(self.chat(), self.spawn))
        (argv,) = self.spawned
        self.assertEqual(argv[3:], ["repair", "--platform", "codex", "--location", "server"])
        self.assertEqual(argv[1], "-B")
        self.assertTrue(os.path.isfile(argv[2]) and os.path.basename(argv[2]) == "platform-letter")

    def test_nothing_starts_inside_the_window_or_without_a_row_or_a_place(self) -> None:
        chat = self.chat()
        stamp = PL.repair_stamp_path(chat.env, chat.seat, "codex", "server")
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w") as fh:
            json.dump({"at": chat.now - 60}, fh)
        self.assertFalse(PL.repair_after_start(chat, self.spawn))  # the run of a minute ago took the stamp
        chat.now += PL.REPAIR_SWEEP_SECONDS
        self.assertTrue(PL.repair_after_start(chat, self.spawn))  # the window is over
        self.assertFalse(PL.repair_after_start(self.chat(where=""), self.spawn))  # a host that says no place
        self.assertFalse(PL.repair_after_start(self.chat(spec=make_spec()), self.spawn))  # a row with no list
        self.assertFalse(PL.repair_after_start(self.chat(transport=lambda argv: {}), self.spawn))  # its own registry
        self.assertEqual(len(self.spawned), 1)

    def test_a_start_hook_sets_it_off_and_a_compact_start_does_not(self) -> None:
        # The hook as a platform runs it:  on the process's own environment.
        with mock.patch.object(PL, "_spawn_detached", side_effect=self.spawn), mock.patch.dict(
            os.environ, self.server_env(), clear=True
        ):
            for source in ("startup", "compact"):
                payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": source}
                PL.hook(self.spec, os.environ, io.StringIO(json.dumps(payload)), io.StringIO(), platform="linux")
        self.assertEqual(len(self.spawned), 1)

    def test_a_hook_on_a_mapping_of_its_own_starts_nothing(self) -> None:
        """A caller's own environment mapping (every test, an embedding caller) is not what a child would read."""
        with mock.patch.object(PL, "_spawn_detached", side_effect=self.spawn):
            payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}
            PL.hook(self.spec, self.server_env(), io.StringIO(json.dumps(payload)), io.StringIO(), platform="linux")
        self.assertEqual(self.spawned, [])

    def test_a_spawn_that_fails_never_reaches_the_chat(self) -> None:
        def broken(argv, env):
            raise OSError("no fork")

        self.assertFalse(PL.repair_after_start(self.chat(), broken))


class RequestNewChatTests(Base):
    """`request` for a chat with no note:  its platform's own session id says which chat, and its start runs now."""

    def setUp(self) -> None:
        super().setUp()
        self.spec = make_spec(session_id=PL.either(PL.from_payload("session_id"), PL.from_env("FAUX_SESSION_ID")))
        patcher = mock.patch.dict(PL.SPECS, {"codex": self.spec})
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, *argv: str, env: dict | None = None) -> tuple[int, str]:
        out = io.StringIO()
        code = PL.request_main(list(argv), env if env is not None else self.server_env(), out, platform="linux")
        return code, out.getvalue()

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "CODEX", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def test_a_chat_with_no_note_asks_from_its_own_shell(self) -> None:
        code, line = self.request(env=self.server_env(FAUX_SESSION_ID="s-0101"))
        self.assertEqual(code, 0, line)
        label = self.note("s-0101")["label"]
        self.assertRegex(label, PAIR)
        self.assertEqual(line, "letter %s: Codex chat s-0101\n" % label)
        self.assertEqual(self.lookup("s-0101")["label"], label)
        self.assertEqual(self.request(env=self.server_env(FAUX_SESSION_ID="s-0101")), (0, line))  # now it has a note

    def test_a_bare_session_id_with_its_platform(self) -> None:
        code, line = self.request("--session-id", "s-0102", "--platform", "codex")
        self.assertEqual(code, 0, line)
        self.assertEqual(self.lookup("s-0102")["label"], self.note("s-0102")["label"])

    def test_a_bare_session_id_no_platform_names_is_refused(self) -> None:
        code, line = self.request("--session-id", "s-0103")
        self.assertEqual(code, 1)
        self.assertIn("no chat with the session id s-0103", line)
        self.assertIn("--platform", line)
        self.assertIsNone(self.lookup("s-0103"))

    def test_the_shared_variable_alone_never_picks_a_platform(self) -> None:
        code, line = self.request(env=self.server_env(AGENT_SESSION="s-0104"))
        self.assertEqual(code, 1)
        self.assertIsNone(self.lookup("s-0104"))
        code, line = self.request("--platform", "codex", env=self.server_env(AGENT_SESSION="s-0104"))
        self.assertEqual(code, 0, line)

    def test_a_shell_that_names_two_platforms_chats_is_ambiguous(self) -> None:
        other = make_spec(platform="faux", source="faux-hook", display="Faux", session_env=("FAUX2",))
        with mock.patch.dict(PL.SPECS, {"faux": other}):
            code, line = self.request(env=self.server_env(FAUX_SESSION_ID="s-0105", FAUX2="s-0106"))
        self.assertEqual(code, 1)
        self.assertIn("more than one platform", line)
        self.assertIsNone(self.lookup("s-0105"))

    def test_the_hooks_own_refusals_hold(self) -> None:
        with self.subTest("a launcher's session"):
            launched = self.server_env(FAUX_SESSION_ID="s-0107", AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="CODEX")
            self.assertEqual(self.request(env=launched)[0], 1)
            self.assertIsNone(self.lookup("s-0107"))
        with self.subTest("a host that does not say where it is"):
            code, line = self.request(env=self.env(FAUX_SESSION_ID="s-0108"))
            self.assertEqual(code, 1)
            self.assertIn(PL.NO_LOCATION, line)
        with self.subTest("a platform that does not run there"):
            mac_only = make_spec(session_env=("FAUX_SESSION_ID",), locations=("mac",))
            with mock.patch.dict(PL.SPECS, {"codex": mac_only}):
                code, line = self.request(env=self.server_env(FAUX_SESSION_ID="s-0109"))
            self.assertEqual(code, 1)
            self.assertIn("does not run on the server", line)

    def test_the_client_never_reads_a_host_registry_settings_file(self) -> None:
        """Every host reads the registry the same way (its environment, else fleet_knobs):  a retired settings file
        in the home is never sourced, so a chat on a host that has only that file gets no letter."""
        bin_dir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bin_dir)
        settings = self.fake.env()
        with open(os.path.join(bin_dir, "operator-registry-env"), "w") as fh:
            fh.writelines("export %s=%s\n" % (k, v) for k, v in settings.items())
        shell = {k: v for k, v in self.server_env(FAUX_SESSION_ID="s-0110").items() if k not in settings}
        code, line = self.request(env=shell)
        self.assertEqual(code, 1)
        self.assertIn(registry_api.URL_ENV, line)
        self.assertIsNone(self.lookup("s-0110"))


def _ago(seconds: float) -> float:
    return time.time() - seconds


class UnletteredListingTests(unittest.TestCase):
    """The rows' own listings of their "??" chats:  read only, inside the age window, and nothing they doubt."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.where = {"HOME": self.home}

    def test_no_store_is_no_pass(self) -> None:
        self.assertIsNone(AG.antigravity_unlettered_sessions(self.where))
        self.assertIsNone(ZC.zcode_unlettered_sessions(self.where))
        self.assertIsNone(CL.claude_unlettered_sessions(self.where))

    def test_antigravity(self) -> None:
        folder = os.path.join(self.home, ".gemini", "antigravity")
        os.makedirs(folder)
        conn = sqlite3.connect(os.path.join(folder, "conversation_summaries.db"))
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id text primary key, title text, "
            "last_modified_time datetime, killed numeric)"
        )

        def when(seconds: float) -> str:
            return datetime.fromtimestamp(_ago(seconds), UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")

        rows = [
            ("c-old-enough", "??: Fix Login", when(3600), 0),
            ("c-newer", "??: Other", when(1800), 0),
            ("c-in-flight", "??: New Chat", when(60), 0),
            ("c-killed", "??: Gone", when(3600), 1),
            ("c-lettered", "AA: Fine", when(3600), 0),
            ("c-ancient", "??: History", when(30 * 86400), 0),
            ("c-no-time", "??: Odd", "not a time", 0),
        ]
        conn.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        self.assertEqual(AG.antigravity_unlettered_sessions(self.where), ["c-newer", "c-old-enough"])

    def test_zcode(self) -> None:
        path = os.path.join(self.home, ZC.STORE_SUBPATH)
        os.makedirs(os.path.dirname(path))
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE session (id text primary key, title text, task_type text, time_archived integer, "
            "time_deleted integer, time_created integer)"
        )

        def ms(seconds: float) -> int:
            return int(_ago(seconds) * 1000)

        rows = [
            ("z-old-enough", "??: Fix Login", "interactive", None, None, ms(3600)),
            ("z-in-flight", "??: New Chat", "interactive", None, None, ms(60)),
            ("z-child", "??: Child", "subagent_child", None, None, ms(3600)),
            ("z-archived", "??: Put Away", "interactive", ms(100), None, ms(3600)),
            ("z-deleted", "??: Deleted", "interactive", None, ms(100), ms(3600)),
            ("z-lettered", "AA: Fine", "interactive", None, None, ms(3600)),
        ]
        conn.executemany("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        self.assertEqual(ZC.zcode_unlettered_sessions(self.where), ["z-old-enough"])

    def test_claude_desktop_records(self) -> None:
        org = os.path.join(CL.store_root(self.home), "account", "org")
        os.makedirs(org)

        def record(name: str, age: float, **fields) -> None:
            path = os.path.join(org, "local_%s.json" % name)
            with open(path, "w") as fh:
                json.dump(fields, fh)
            os.utime(path, (_ago(age), _ago(age)))

        sid = "0b7c2f2e-1111-4222-8333-944455556666"
        record("a", 3600, cliSessionId=sid, title="??: Fix Login")
        record("b", 60, cliSessionId="0b7c2f2e-1111-4222-8333-000000000001", title="??: In Flight")
        record("c", 3600, cliSessionId="0b7c2f2e-1111-4222-8333-000000000002", title="??: Archived", isArchived=True)
        record("d", 3600, cliSessionId="0b7c2f2e-1111-4222-8333-000000000003", title="??: Remote", sshConfig={"h": 1})
        record("e", 3600, cliSessionId="0b7c2f2e-1111-4222-8333-000000000004", title="AA: Fine")
        record("f", 3600, cliSessionId="0b7c2f2e-1111-4222-8333-000000000005", title="??: Odd", isArchived="yes")
        self.assertEqual(CL.claude_unlettered_sessions(self.where), [sid])


if __name__ == "__main__":
    unittest.main()
