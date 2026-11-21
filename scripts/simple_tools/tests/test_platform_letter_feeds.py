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
#: row.  The exemption only covers a row with neither a list nor a reason:  once the pull request lands, its row
#: takes the first branch below and is held to it like any other.  After it merges, drop its name here and add it
#: to the expected set in `test_the_rows_that_list_their_chats`.
PENDING_FEEDS: set[str] = set()


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
        self.assertTrue({"antigravity", "claude", "codex", "minimax", "zcode"} <= listed, listed)


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

    def test_a_spawn_that_fails_gives_the_claim_back(self) -> None:
        # A spawn that never started must not hold the next start off for the whole
        # claim TTL (Kody, PR 2307).
        chat = self.chat()
        self.assertFalse(PL.repair_after_start(chat, lambda argv, env: False))
        stamp = PL.repair_stamp_path(chat.env, chat.seat, chat.spec.platform, chat.location)
        self.assertFalse(os.path.exists(stamp + ".spawn"))
        # So the very next start runs.
        self.assertTrue(PL.repair_after_start(chat, self.spawn))


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

    def test_a_claude_terminal_chat_asks_for_itself_with_the_real_row(self) -> None:
        """Claude's own `chat_payload` asks about a chat this hook did not fire for, and takes only a desktop chat
        with a record here;  a terminal chat naming itself through its own variables starts on the plain payload."""
        sid = "0b7c2f2e-1111-4222-8333-944455550001"
        with mock.patch.dict(PL.SPECS, {"codex": PL.load_spec("codex")}):
            env = self.server_env(CLAUDE_CODE_SESSION_ID=sid, CLAUDE_CODE_ENTRYPOINT="cli")
            code, line = self.request(env=env)
        self.assertEqual(code, 0, line)
        held = self.lookup(sid, platform="claude")
        self.assertIsNotNone(held)
        self.assertEqual(line, "letter %s: Claude chat %s\n" % (held["label"], sid))

    def test_a_claude_chat_named_only_by_id_still_needs_its_record_here(self) -> None:
        """An id that is not this shell's own is the question about another chat:  Claude's row answers it only
        for a desktop chat this host runs, so a typo never registers a phantom Claude chat."""
        sid = "0b7c2f2e-1111-4222-8333-944455550002"
        env = self.server_env(CLAUDE_CODE_ENTRYPOINT="cli")
        code, line = self.request("--session-id", sid, "--platform", "claude", env=env)
        self.assertEqual(code, 1, line)
        self.assertIn("does not take a letter", line)
        self.assertIsNone(self.lookup(sid, platform="claude"))

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


class ClaudeRepairTests(Base):
    """The repair on the real Claude row:  a "??" desktop chat with no note registers once, and one whose note
    already holds a letter is never replayed, though the app's record still says "??: " (the row has no writer)."""

    SID = "0b7c2f2e-1111-4222-8333-944455556666"

    def setUp(self) -> None:
        super().setUp()
        org = os.path.join(CL.store_root(self.home), "account", "org")
        os.makedirs(org)
        path = os.path.join(org, "local_a.json")
        with open(path, "w") as fh:
            json.dump({"cliSessionId": self.SID, "title": "??: Fix Login"}, fh)
        os.utime(path, (_ago(3600), _ago(3600)))
        self.claude_env = self.env(CLAUDE_CODE_ENTRYPOINT="claude-desktop")

    def repair(self, now: float) -> dict:
        return PL.repair_unlettered(CL.SPEC, self.claude_env, "CLAUDE", "mac", now=now, force=True)

    def test_a_chat_with_a_letter_is_not_replayed_every_pass(self) -> None:
        first = self.repair(time.time())
        self.assertEqual(first["asked"], [self.SID], first)
        self.assertEqual(first["repaired"], [self.SID], first)
        held = self.lookup(self.SID, location="mac", platform="claude")
        self.assertIsNotNone(held)
        seen = held.get("last_seen_at")
        # The app's record still says "??: Fix Login":  the next pass lists it again and leaves it alone.
        self.assertEqual(CL.claude_unlettered_sessions(self.claude_env), [self.SID])
        later = self.repair(time.time() + PL.REPAIR_SWEEP_SECONDS)
        self.assertEqual(later["asked"], [], later)
        self.assertEqual(self.lookup(self.SID, location="mac", platform="claude").get("last_seen_at"), seen)


class RepairClaimTests(Base):
    """The stamp is read and written as one step, and a burst of chat starts sets off one run."""

    def setUp(self) -> None:
        super().setUp()
        self.listed = 0

        def listing(env):
            self.listed += 1
            return []

        self.spec = make_spec(
            unlettered_sessions=listing,
            chat_payload=lambda sid: {"hook_event_name": "SessionStart", "source": "resume", "session_id": sid},
        )

    def test_a_held_lock_skips_the_pass(self) -> None:
        path = PL.repair_stamp_path(self.server_env(), "CODEX", "codex", "server")
        self.assertTrue(PL._claim(path + ".lock", PL.REPAIR_LOCK_SECONDS))
        done = PL.repair_unlettered(self.spec, self.server_env(), "CODEX", "server", now=time.time())
        self.assertEqual(done, {"skipped": "another pass holds the stamp"})
        self.assertEqual(self.listed, 0)
        PL._release(path + ".lock")
        done = PL.repair_unlettered(self.spec, self.server_env(), "CODEX", "server", now=time.time())
        self.assertEqual(self.listed, 1, done)
        self.assertFalse(os.path.exists(path + ".lock"))  # released once the stamp is written

    def test_a_dead_holders_claim_is_taken_over(self) -> None:
        path = os.path.join(self.tmp.name, "claims", "x.lock")
        self.assertTrue(PL._claim(path, 30))
        self.assertFalse(PL._claim(path, 30))
        os.utime(path, (_ago(60), _ago(60)))
        self.assertTrue(PL._claim(path, 30))

    def test_a_burst_of_starts_sets_off_one_run(self) -> None:
        spawned: list[list[str]] = []

        def spawn(argv, env) -> bool:
            spawned.append(list(argv))
            return True

        for n in range(4):
            chat = PL.Chat(self.spec, self.server_env(), "server", "s-%04d" % n, now=time.time())
            PL.repair_after_start(chat, spawn)
        self.assertEqual(len(spawned), 1)


class StoreIdTests(Base):
    """A listed id that is not a session id never becomes a note path, whatever the store holds."""

    def test_the_pass_drops_an_id_that_would_leave_the_letters_folder(self) -> None:
        spec = make_spec(
            unlettered_sessions=lambda env: ["../../escape", "/etc/x", "s-0301"],
            chat_payload=lambda sid: {"hook_event_name": "SessionStart", "source": "resume", "session_id": sid},
        )
        done = PL.repair_unlettered(spec, self.server_env(), "CODEX", "server", now=time.time())
        self.assertEqual(done["asked"], ["s-0301"], done)
        for root, _dirs, files in os.walk(self.tmp.name):
            for name in files:
                self.assertNotIn("escape", name, root)


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
        rows.append(("../escape", "??: Odd", "interactive", None, None, ms(3600)))
        conn.executemany("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        self.assertEqual(ZC.zcode_unlettered_sessions(self.where), ["z-old-enough"])

    def test_zcode_age_filter_precedes_result_cap(self) -> None:
        path = os.path.join(self.home, ZC.STORE_SUBPATH)
        os.makedirs(os.path.dirname(path))
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE session (id text, title text, time_created integer)")
        rows = [("z-new-%d" % i, "??: Starting", int(_ago(60) * 1000))
                for i in range(ZC.UNLETTERED_SCAN + 5)]
        rows += [("z-ready-%02d" % i, "??: Ready", int(_ago(3600 + i) * 1000))
                 for i in range(ZC.UNLETTERED_SCAN + 5)]
        conn.executemany("INSERT INTO session VALUES (?, ?, ?)", rows)
        conn.commit()
        conn.close()
        self.assertEqual(ZC.zcode_unlettered_sessions(self.where),
                         ["z-ready-%02d" % i for i in range(ZC.UNLETTERED_SCAN)])

    def test_zcode_without_age_signal_skips_repair_pass(self) -> None:
        path = os.path.join(self.home, ZC.STORE_SUBPATH)
        os.makedirs(os.path.dirname(path))
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE session (id text, title text, task_type text, "
                     "time_archived integer, time_deleted integer)")
        conn.execute("INSERT INTO session VALUES (?, ?, ?, NULL, NULL)",
                     ("z-new-unknown", "??: Starting", "interactive"))
        conn.commit()
        conn.close()
        self.assertIsNone(ZC.zcode_unlettered_sessions(self.where))

    def test_zcode_store_under_a_home_whose_path_needs_escaping(self) -> None:
        self.where = {"HOME": os.path.join(self.home, "a b?c#d%")}
        path = os.path.join(self.where["HOME"], ZC.STORE_SUBPATH)
        os.makedirs(os.path.dirname(path))
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE session (id text primary key, title text, task_type text, time_archived integer, "
            "time_deleted integer, time_created integer)"
        )
        conn.execute(
            "INSERT INTO session VALUES ('z-one', '??: One', 'interactive', NULL, NULL, ?)",
            (int(_ago(3600) * 1000),),
        )
        conn.commit()
        conn.close()
        self.assertEqual(ZC.zcode_unlettered_sessions(self.where), ["z-one"])

    def lettered_note_for(self, sid: str) -> None:
        """A parent's own note, as the hook writes it when the assigner issued a letter."""
        path = os.path.join(self.home, ".simple-tools", "ZC", "letters", sid + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"state": "open", "label": "CC"}, fh)

    def zcode_store_with_parents(self) -> None:
        path = os.path.join(self.home, ZC.STORE_SUBPATH)
        os.makedirs(os.path.dirname(path))
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE session (id text primary key, title text, task_type text, parent_id text, "
            "time_archived integer, time_deleted integer, time_created integer)"
        )
        return conn

    def test_zcode_children_of_a_lettered_parent_join_the_repair_pass(self) -> None:
        conn = self.zcode_store_with_parents()

        def ms(seconds: float) -> int:
            return int(_ago(seconds) * 1000)

        self.lettered_note_for("p-lettered")
        self.lettered_note_for("k-done")
        rows = [
            ("z-chat", "??: Chat work", "interactive", None, None, None, ms(3600)),
            ("k-newer", "", "subagent_child", "p-lettered", None, None, ms(1800)),
            ("k-older", "", "subagent_child", "p-lettered", None, None, ms(5400)),
            ("k-done", "", "subagent_child", "p-lettered", None, None, ms(1200)),
            ("k-orphan", "", "subagent_child", "p-gone", None, None, ms(3600)),
            ("k-young", "", "subagent_child", "p-lettered", None, None, ms(60)),
            ("k-ancient", "", "subagent_child", "p-lettered", None, None, ms(30 * 86400)),
            ("k-archived", "", "subagent_child", "p-lettered", ms(100), None, ms(3600)),
            ("k-parentless", "", "subagent_child", None, None, None, ms(3600)),
        ]
        conn.executemany("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
        # Newest first, "??" chats and a lettered parent's children together;  a child that already
        # holds a letter, one whose parent holds none, one too young, one too old, an archived one and
        # one with no parent named at all are left out.
        self.assertEqual(ZC.zcode_unlettered_sessions(self.where), ["k-newer", "z-chat", "k-older"])

    def test_zcode_child_sessions_of_one_parent(self) -> None:
        conn = self.zcode_store_with_parents()

        def ms(seconds: float) -> int:
            return int(_ago(seconds) * 1000)

        rows = [
            ("k-first", "p-mine", "subagent_child", ms(1800)),
            ("k-second", "p-mine", "subagent_child", ms(5400)),
            ("k-ancient", "p-mine", "subagent_child", ms(30 * 86400)),
            ("k-elsewhere", "other-parent", "subagent_child", ms(600)),
            ("z-chat", None, "interactive", ms(600)),
        ]
        conn.executemany(
            "INSERT INTO session (id, parent_id, task_type, time_created) VALUES (?, ?, ?, ?)", rows
        )
        conn.commit()
        conn.close()
        # Only this parent's children, newest first, inside the pass's own age window.
        self.assertEqual(ZC.zcode_child_sessions(self.where, "p-mine"), ["k-first", "k-second"])
        self.assertEqual(ZC.zcode_child_sessions(self.where, "nobody"), [])
        self.assertIsNone(ZC.zcode_child_sessions(self.where, ""))

    def test_zcode_child_sessions_need_the_parent_columns(self) -> None:
        path = os.path.join(self.home, ZC.STORE_SUBPATH)
        os.makedirs(os.path.dirname(path))
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE session (id text primary key, title text, task_type text, time_created integer)")
        conn.execute(
            "INSERT INTO session VALUES ('k-one', '', 'subagent_child', ?)", (int(_ago(1800) * 1000),)
        )
        conn.commit()
        conn.close()
        self.assertIsNone(ZC.zcode_child_sessions(self.where, "p-mine"))  # no parent_id column
        self.assertEqual(ZC.zcode_unlettered_sessions(self.where), [])  # the child joins no pass

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


class RequestSelectorTests(Base):
    """`request`'s own selectors:  `--platform` narrows every answer, `--location` names the host
    on the no-note path, and an adapter the table does not hold is a bad argument."""

    def setUp(self) -> None:
        super().setUp()
        self.spec = make_spec(session_id=PL.either(PL.from_payload("session_id"), PL.from_env("FAUX_SESSION_ID")))
        faux = make_spec(platform="faux", source="faux-hook", display="Faux", session_env=("FAUX_NOTE",))
        patcher = mock.patch.dict(PL.SPECS, {"codex": self.spec, "faux": faux})
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, *argv: str, env: dict | None = None) -> tuple[int, str]:
        out = io.StringIO()
        code = PL.request_main(list(argv), env if env is not None else self.server_env(), out, platform="linux")
        return code, out.getvalue()

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "CODEX", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def test_an_unknown_platform_is_a_bad_argument_not_a_failed_request(self) -> None:
        # Exit 2, as `repair` answers the same case;  exit 1 is a request that found no letter
        # (Kody, PR 2307).
        code, line = self.request("--session-id", "s-0301", "--platform", "no-such-platform")
        self.assertEqual(code, 2)
        self.assertIn("usage:", line)
        self.assertIn("no adapter", line)
        self.assertIsNone(self.lookup("s-0301"))

    def test_a_platform_selector_refuses_another_platforms_note(self) -> None:
        # A note of another platform is a refusal, never a registration under the wrong
        # adapter (Kody, PR 2307).
        code, _ = self.request("--session-id", "s-0302", "--platform", "codex")
        self.assertEqual(code, 0)
        code, line = self.request("--session-id", "s-0302", "--platform", "faux")
        self.assertEqual(code, 1)
        self.assertIn("is a codex chat, not the faux chat", line)
        # Without the selector the note answers as it always did.
        self.assertEqual(self.request("--session-id", "s-0302")[0], 0)

    def test_location_flag_names_the_host_on_the_no_note_path(self) -> None:
        # The flag reaches the no-note start the way the hook's own `--location server` does
        # (Kody, PR 2307):  a server chat whose shell says nothing still asks.
        env = self.server_env()
        env.pop("SESSION_LETTER_LOCATION", None)
        code, line = self.request(
            "--session-id", "s-0303", "--platform", "codex", "--location", "server", env=env
        )
        self.assertEqual(code, 0, line)
        self.assertEqual(self.note("s-0303")["location"], "server")
        # A bogus host is bad arguments, not a registry error later;  a wrong-cased one is
        # lower-cased and accepted (Kody, PR 2307 round 2).
        code, line = self.request(
            "--session-id", "s-0306", "--platform", "codex", "--location", "bogus", env=env
        )
        self.assertEqual(code, 2)
        self.assertIn("--location", line)
        self.assertIsNone(self.lookup("s-0306"))
        code, line = self.request(
            "--session-id", "s-0307", "--platform", "codex", "--location", "Server", env=env
        )
        self.assertEqual(code, 0, line)
        self.assertEqual(self.note("s-0307")["location"], "server")

    def test_one_platform_naming_two_ids_is_told_to_name_one(self) -> None:
        # Every id the platform names counts, never only the first (Cursor names two);
        # one platform holding two chats asks for --session-id (Kody, PR 2307).
        second = make_spec(
            platform="codex",
            source="faux-hook",
            display="Codex",
            session_id=PL.from_env("FAUX_SESSION_ID"),
            session_env=("FAUX_SESSION_ID", "FAUX_SESSION_ID_2"),
        )
        with mock.patch.dict(PL.SPECS, {"codex": second}):
            code, line = self.request(
                env=self.server_env(FAUX_SESSION_ID="s-0304", FAUX_SESSION_ID_2="s-0305")
            )
        self.assertEqual(code, 1)
        self.assertIn("more than one codex chat", line)
        self.assertIn("s-0304", line)
        self.assertIn("--session-id S", line)
        self.assertIsNone(self.lookup("s-0304"))


class RepairStampTests(unittest.TestCase):
    """The repair stamp's `at` is the throttle:  an outcome never moves it backward."""

    def test_an_outcome_never_moves_the_stamp_backward(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stamp.json")
            self.assertTrue(PL._save_json(path, {"at": 5_000.0}))
            # An older pass finishing late keeps the newer pass's time (Kody, PR 2307).
            self.assertTrue(PL._record_outcome(path, 4_000.0, {"found": 0}))
            stamp = PL._read_json(path)
            self.assertEqual(stamp["at"], 5_000.0)
            self.assertEqual(stamp["outcome"], {"found": 0})
            # And a newer outcome moves it forward as usual.
            self.assertTrue(PL._record_outcome(path, 6_000.0, {"found": 1}))
            self.assertEqual(PL._read_json(path)["at"], 6_000.0)


if __name__ == "__main__":
    unittest.main()
