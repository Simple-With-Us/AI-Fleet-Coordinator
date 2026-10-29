"""`simple-chat letter` (AFC-564):  Operator session letters and "<letter>: <Title>" titles for Claude Code chats.
The registry side runs against a temporary registry;  the hook runs in process, with the ssh leg faked by a runner
that hands the remote command to the same registry side."""
from __future__ import annotations

import io
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from unittest import mock
from types import SimpleNamespace

from agent_sync import claude_letter as CL
from agent_sync import operator_registry as OR


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.home, ".claude", "projects"))
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()  # the Operator's registry exists, as it does on the Mac

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "AGENT_SYNC_STATE_DIR": self.state, "CLAUDE_CODE_ENTRYPOINT": "cli"}
        env.update(extra)
        return env

    def server_env(self, **extra: str) -> dict[str, str]:
        return self.env(CLAUDE_LETTER_LOCATION="server", CLAUDE_LETTER_REMOTE="mac", **extra)

    def registry(self, *argv: str, now: float | None = None) -> tuple[int, dict]:
        out = io.StringIO()
        code = CL.registry_main(list(argv), env=self.env(), stdout=out, now=now)
        return code, json.loads(out.getvalue())

    def register(self, sid: str, location: str = "server", title: str = "", now: float | None = None) -> dict:
        argv = ["register", "--location", location, "--session-id", sid]
        if title:
            argv += ["--title", title]
        return self.registry(*argv, now=now)[1]

    def lookup(self, sid: str, location: str = "server") -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup(CL.PLATFORM, location, CL.SCOPE, sid)
        finally:
            reg.close_db()

    def set_seen(self, sid: str, iso: str) -> None:
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE native_session_id = ?", (iso, sid))
        conn.close()

    def ago(self, seconds: float) -> str:
        """An ISO time this many seconds back, in the registry's own form, for last_seen_at."""
        return datetime.fromtimestamp(time.time() - seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def small_space(self, *pairs: str):
        """Shrink the pair space, so reuse and eviction can be reached without 676 chats."""
        return mock.patch.dict(OR.LETTER_SPACES, {"double": tuple(pairs)})


class TitleTests(unittest.TestCase):
    def test_title_from_prompt(self) -> None:
        self.assertEqual(
            CL.title_from_prompt("help get all PR issues addressed from review findings."),
            "Help Get All PR Issues Addressed",
        )
        self.assertEqual(CL.title_from_prompt("```\nx = 1\n```\nfix the iOS build for AFC-564"), "Fix the iOS Build for AFC-564")
        self.assertEqual(CL.title_from_prompt("see https://example.com/a/b and the docs"), "See and the Docs")
        self.assertEqual(CL.title_from_prompt("   \n"), "")

    def test_slash_commands_and_secrets_never_name_a_chat(self) -> None:
        self.assertEqual(CL.title_from_prompt("/review"), "")
        self.assertEqual(CL.title_from_prompt("/review fix the login page"), "Fix the Login Page")
        key = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"  # Zulip-shaped:  32 mixed-case letters and digits
        self.assertEqual(CL.title_from_prompt("rotate %s today" % key), "Rotate Today")
        self.assertEqual(CL.clean_title("Rotate %s Today" % key), "Rotate Today")

    def test_bare_and_clean(self) -> None:
        self.assertEqual(CL.bare("B: Fix Login"), "Fix Login")
        self.assertEqual(CL.bare("Fix: Login"), "Fix: Login")
        self.assertEqual(CL.bare("BC: Fix Login"), "Fix Login")  # a pair prefix, with no label known
        self.assertEqual(CL.bare("BC: Fix Login", "BC"), "Fix Login")
        self.assertEqual(CL.bare("PR: Review", "BC"), "PR: Review")  # only the chat's own label is dropped
        self.assertEqual(CL.bare("BC: PR: Review", "BC"), "PR: Review")
        self.assertEqual(CL.bare("BD: Fix Login", "BC"), "BD: Fix Login")  # someone else's label is not ours to drop
        long = "word " * 30
        self.assertLessEqual(len(CL.clean_title(long)), CL.TITLE_CHARS)
        self.assertEqual(CL.clean_title("a\nb"), "a b")

    def test_location_and_scope(self) -> None:
        self.assertEqual(CL.location({}, "darwin"), "mac")
        self.assertIsNone(CL.location({}, "linux"))
        self.assertEqual(CL.location({CL.ENV_LOCATION: "Server"}, "linux"), "server")
        self.assertIsNone(CL.location({CL.ENV_LOCATION: "my-laptop"}, "linux"))
        self.assertTrue(CL.in_scope({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}))
        self.assertFalse(CL.in_scope({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}))
        self.assertFalse(CL.in_scope({"CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_LETTER": "0"}))
        self.assertFalse(CL.in_scope({"CLAUDE_CODE_ENTRYPOINT": "cli", "AGENT_LAUNCHER": "muster"}))

    def test_a_platform_that_runs_claude_as_its_engine_takes_no_claude_letter(self) -> None:
        # What BotFleet hands its Claude engine (the BotFleet repo's docs/launch-identity.md, and this repo's
        # docs/protocols/zulip-fleet-guide.md, Launcher Contract):  a launcher, its seat, its own thread id, and the
        # attach opt-out.  The first loop checks the whole set against every entrypoint;  the second checks that each
        # of the three markers that matter is enough on its own, which is what exercises the AGENT_SYNC_ATTACH=0 line.
        botfleet = {
            "AGENT_LAUNCHER": "botfleet",
            "AGENT_LAUNCH_SEAT": "BF-MONITOR",
            "AGENT_SEAT": "BF-MONITOR",
            "AGENT_SESSION": "bf-thread-1",
            "AGENT_SYNC_ATTACH": "0",
        }
        for entry in ("cli", "claude-desktop", "sdk-cli"):
            self.assertFalse(CL.in_scope(dict(botfleet, CLAUDE_CODE_ENTRYPOINT=entry)), entry)
        for key in ("AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "AGENT_SYNC_ATTACH"):
            self.assertFalse(CL.in_scope({"CLAUDE_CODE_ENTRYPOINT": "cli", key: botfleet[key]}), key)
        # The owner's own chat sets none of them.
        self.assertTrue(CL.in_scope({"CLAUDE_CODE_ENTRYPOINT": "cli", "AGENT_SEAT": "CLAUDE"}))
        self.assertTrue(CL.in_scope({"CLAUDE_CODE_ENTRYPOINT": "cli", "AGENT_SYNC_ATTACH": "1"}))


class RegistrySideTests(Base):
    def test_refuses_a_host_without_a_registry(self) -> None:
        other = os.path.join(self.tmp.name, "elsewhere")
        out = io.StringIO()
        code = CL.registry_main(
            ["register", "--location", "server", "--session-id", "s-0001"],
            env={"HOME": self.home, "AGENT_SYNC_STATE_DIR": other},
            stdout=out,
        )
        self.assertEqual(code, 3)
        self.assertIn("no Operator registry", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(other, "operator")))

    def test_register_resume_title_close_reopen(self) -> None:
        first = self.register("s-0001", title="First Chat")
        self.assertEqual((first["label"], first["outcome"], first["created"]), ("AA", "registered", True))
        again = self.register("s-0001")
        self.assertEqual((again["label"], again["outcome"]), ("AA", "resumed"))
        code, out = self.registry("title", "--location", "server", "--session-id", "s-0001", "--title", "Renamed")
        self.assertEqual((code, out["label"]), (0, "AA"))
        self.assertEqual(self.lookup("s-0001")["title"], "Renamed")
        code, out = self.registry("close", "--location", "server", "--session-id", "s-0001")
        self.assertEqual((code, out["closed"]), (0, True))
        code, out = self.registry("touch", "--location", "server", "--session-id", "s-0001")
        self.assertTrue(out["retired"])
        back = self.register("s-0001", title="Back Again")
        self.assertEqual((back["outcome"], back["label"], back["generation"], back["previous"]), ("reopened", "AA", 2, "AA"))
        self.assertEqual(self.lookup("s-0001")["title"], "Back Again")

    def test_new_chats_take_pairs_and_a_chat_that_holds_a_single_letter_keeps_it(self) -> None:
        self.assertEqual([self.register("s-%04d" % i)["label"] for i in (1, 2, 3)], ["AA", "AB", "AC"])
        # The first Claude chat was registered by hand, before pairs, and holds a single letter.
        OR.register_session(
            self.db, platform="claude", verified_host="server", profile_scope="default",
            native_session_id="first-chat", seat="CLAUDE", source="claude-chat-start", issuer=OR.ISSUER_OPERATOR,
        )
        self.assertEqual(self.lookup("first-chat")["label"], "A")
        self.registry("close", "--location", "server", "--session-id", "first-chat")
        back = self.register("first-chat")
        self.assertEqual((back["outcome"], back["label"], back["previous"]), ("reopened", "A", "A"))

    def test_reopen_prefers_the_letter_it_held(self) -> None:
        for sid in ("s-0001", "s-0002", "s-0003"):
            self.register(sid)
        for sid in ("s-0002", "s-0001"):
            self.registry("close", "--location", "server", "--session-id", sid)
        back = self.register("s-0002")  # AA is free and lower, but AB is still free and was this chat's
        self.assertEqual((back["outcome"], back["label"], back["generation"]), ("reopened", "AB", 2))

    def test_one_bad_record_does_not_stop_the_others_closing(self) -> None:
        self.register("s-0001")
        rid = self.lookup("s-0001")["record_id"]
        self.assertEqual(CL._close_records(OR, self.db, ["no-such-record", rid], "test"), 1)
        self.assertEqual(self.lookup("s-0001")["state"], "closed")

    def test_a_retired_pair_waits_while_pairs_nobody_used_remain(self) -> None:
        self.register("s-0001")
        self.registry("close", "--location", "server", "--session-id", "s-0001")
        # AA was used and retired, AB never was:  the new chat takes the pair that has gone unused the longest.
        self.assertEqual(self.register("s-0002")["label"], "AB")
        back = self.register("s-0001")  # AA is still free and was this chat's, so it comes back to it
        self.assertEqual((back["outcome"], back["label"], back["generation"], back["previous"]), ("reopened", "AA", 2, "AA"))

    def test_the_pair_unused_the_longest_goes_first_once_every_pair_has_been_used(self) -> None:
        with self.small_space("AA", "AB", "AC"):
            for sid in ("s-0001", "s-0002", "s-0003"):
                self.register(sid)  # AA, AB, AC
            reg = OR.SessionRegistry(self.db)
            try:
                for step, sid in enumerate(("s-0003", "s-0001", "s-0002")):  # retired in this order:  AC first, AB last
                    reg.close(self.lookup(sid)["record_id"], at="2026-10-28T00:00:0%d.000Z" % step)
            finally:
                reg.close_db()
            self.assertEqual(self.register("s-0004")["label"], "AC")  # retired longest ago, not the lowest (AA)
            self.assertEqual(self.register("s-0005")["label"], "AA")
            self.assertEqual(self.register("s-0006")["label"], "AB")

    def test_a_reopened_chat_whose_pair_went_elsewhere_gets_another_pair(self) -> None:
        with self.small_space("AA", "AB", "AC"):
            self.register("s-0001")  # AA
            self.registry("close", "--location", "server", "--session-id", "s-0001")
            self.assertEqual(self.register("s-0002")["label"], "AB")
            self.assertEqual(self.register("s-0003")["label"], "AC")
            self.assertEqual(self.register("s-0004")["label"], "AA")  # the only pair left, generation 2
            self.set_seen("s-0002", self.ago(300))
            back = self.register("s-0001")
        # AA is held by s-0004 and every pair is live, so the least recently seen chat (s-0002) gives up AB.
        self.assertEqual((back["outcome"], back["label"], back["previous"], back["evicted"]), ("reopened", "AB", "AA", 1))
        self.assertEqual(self.lookup("s-0002")["state"], "closed")

    def test_sweep_closes_only_stale_claude_chats(self) -> None:
        self.register("s-0001")
        OR.register_session(
            self.db, platform="minimax", verified_host="server", profile_scope="default",
            native_session_id="mm-0001", seat="MM", source="test", issuer=OR.ISSUER_OPERATOR,
        )
        self.set_seen("s-0001", "2026-01-01T00:00:00.000Z")
        self.set_seen("mm-0001", "2026-01-01T00:00:00.000Z")
        out = self.register("s-0002")
        self.assertEqual((out["swept"], out["label"]), (1, "AB"))  # AA is retired and waits;  AB was never used
        self.assertEqual(self.lookup("s-0001")["state"], "closed")
        reg = OR.SessionRegistry(self.db)
        try:
            self.assertEqual(reg.lookup("minimax", "server", "default", "mm-0001")["state"], "active")
        finally:
            reg.close_db()

    def test_chats_the_operator_registered_another_way_are_never_swept_or_evicted(self) -> None:
        for sid, source in (("op-0001", "operator-registration"), ("op-0002", "chat-topic-answer")):
            OR.register_session(
                self.db, platform="claude", verified_host="mac", profile_scope="default",
                native_session_id=sid, seat="CLAUDE", source=source, issuer=OR.ISSUER_OPERATOR,
            )
            self.set_seen(sid, "2026-01-01T00:00:00.000Z")
        with self.small_space("AA", "AB"):
            # The Operator's rows hold the single letters A and B, outside the pair space.
            self.assertEqual(self.register("s-0001")["swept"], 0)
            self.register("s-0002")
            self.set_seen("s-0001", self.ago(300))
            out = self.register("s-0099")
        # The pairs are all live:  s-0001, the least recently seen of this hook's chats, gives up AA.
        self.assertEqual((out["evicted"], out["label"]), (1, "AA"))
        for sid in ("op-0001", "op-0002"):
            self.assertEqual(self.lookup(sid, "mac")["state"], "active")

    def test_full_letters_evict_the_least_recently_seen(self) -> None:
        with self.small_space("AA", "AB", "AC", "AD"):
            for i in range(4):
                self.register("s-%04d" % i)
                self.set_seen("s-%04d" % i, self.ago(100 - i))
            self.set_seen("s-0002", self.ago(900))  # AC is the oldest
            out = self.register("s-0099")
        self.assertEqual((out["evicted"], out["label"], out["generation"]), (1, "AC", 2))
        self.assertEqual(self.lookup("s-0002")["state"], "closed")

    def test_when_the_operator_holds_every_pair_the_error_counts_the_pairs(self) -> None:
        with self.small_space("AA"):
            OR.register_session(
                self.db, platform="claude", verified_host="mac", profile_scope="default",
                native_session_id="op-0001", seat="CLAUDE", source="operator-registration",
                issuer=OR.ISSUER_OPERATOR, letter_space="double",
            )
            code, out = self.registry("register", "--location", "server", "--session-id", "s-0001")
        self.assertEqual(code, 5)  # nothing this hook manages can be evicted, so there is no room
        self.assertEqual(out["error"], "all 1 Claude letters are in use")
        self.assertIsNone(self.lookup("s-0001"))

    def single(self, sid: str, source: str) -> dict:
        """A Claude registration in the single space, as the hand-registered first chat has."""
        return OR.register_session(
            self.db, platform="claude", verified_host="server", profile_scope="default", native_session_id=sid,
            seat="CLAUDE", source=source, issuer=OR.ISSUER_OPERATOR,
        )

    def test_a_full_pair_space_never_evicts_a_chat_that_holds_a_single_letter(self) -> None:
        with self.small_space("AA"):
            legacy = self.single("first-chat", "claude-chat-start")  # managed, single letter A, the oldest chat
            self.assertEqual(legacy["label"], "A")
            self.register("s-0001")  # AA
            self.set_seen("first-chat", self.ago(900))
            self.set_seen("s-0001", self.ago(100))
            out = self.register("s-0002")
        # Closing the single-letter chat would free no pair, so the pair chat is the one that gives its pair up.
        self.assertEqual((out["evicted"], out["label"]), (1, "AA"))
        self.assertEqual(self.lookup("first-chat")["state"], "active")
        self.assertEqual(self.lookup("s-0001")["state"], "closed")

    def test_a_full_single_space_never_evicts_a_chat_that_holds_a_pair(self) -> None:
        with mock.patch.dict(OR.LETTER_SPACES, {"single": ("A",), "double": ("AA", "AB")}):
            first = self.single("first-chat", "claude-chat-start")  # A
            self.register("s-0001")  # AA
            self.registry("close", "--location", "server", "--session-id", "first-chat")
            self.single("op-single", "operator-registration")  # takes A, and this hook does not manage it
            self.set_seen("s-0001", self.ago(900))
            back = self.register("first-chat")  # needs a single letter:  none is free, none can be freed
        self.assertEqual(first["label"], "A")
        self.assertEqual(back["error"], "all 1 Claude letters are in use")
        self.assertEqual(self.lookup("s-0001")["state"], "active")

    def test_a_chat_that_holds_a_single_letter_gets_a_single_letter_when_its_own_went_elsewhere(self) -> None:
        with mock.patch.dict(OR.LETTER_SPACES, {"single": ("A", "B"), "double": ("AA", "AB")}):
            self.single("first-chat", "claude-chat-start")  # A
            self.registry("close", "--location", "server", "--session-id", "first-chat")
            self.assertEqual(self.single("op-single", "operator-registration")["label"], "A")
            back = self.register("first-chat")
        self.assertEqual((back["outcome"], back["label"], back["previous"]), ("reopened", "B", "A"))

    def test_an_answer_label_is_one_or_two_capital_letters(self) -> None:
        for label in ("", "A", "AB", "ZZ"):
            self.assertNotIn("error", CL._answer({"label": label, "outcome": "registered"}), label)
        for label in ("ABC", "a", "aB", "A1", "A B", "A:"):
            self.assertIn("malformed label", CL._answer({"label": label, "outcome": "registered"})["error"], label)


class FakeSsh:
    """Stands in for the SSH call to the Mac:  checks the command's shape and runs the remote registry side in process."""

    def __init__(self, test: Base, fail: Exception | None = None):
        self.test, self.fail, self.calls = test, fail, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.fail is not None:
            raise self.fail
        self.test.assertEqual(cmd[:7], ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "--"])
        self.test.assertEqual(cmd[7], "mac")
        self.test.assertEqual(kwargs["timeout"], CL.REMOTE_TIMEOUT)
        words = shlex.split(cmd[8])
        self.test.assertEqual(words[:3], ["$HOME/.local/bin/simple-chat", "letter", "registry"])
        out = io.StringIO()
        code = CL.registry_main(words[3:], env=self.test.env(), stdout=out)
        return SimpleNamespace(stdout=out.getvalue(), returncode=code)


class HookTests(Base):
    def hook(self, payload: dict, *, env: dict | None = None, runner=None, now: float | None = None,
             platform: str = "linux") -> dict | None:
        out = io.StringIO()
        env = env if env is not None else self.server_env()
        code = CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=runner or FakeSsh(self), now=now,
                       platform=platform)
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def start(self, sid: str, source: str = "startup", **extra) -> dict | None:
        return self.hook(dict({"hook_event_name": "SessionStart", "session_id": sid, "source": source}, **extra))

    def prompt(self, sid: str, text: str, **extra) -> dict | None:
        return self.hook(dict({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text}, **extra))

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "CLAUDE", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def test_new_chat_gets_a_letter_then_a_title(self) -> None:
        out = self.start("s-0001")
        self.assertEqual(out["sessionTitle"], "AA: New Chat")
        self.assertIn("Claude AA", out["additionalContext"])
        out = self.prompt("s-0001", "help get all PR issues addressed from review findings.",
                          session_title="AA: New Chat")
        self.assertEqual(out["sessionTitle"], "AA: Help Get All PR Issues Addressed")
        self.assertEqual(self.lookup("s-0001")["title"], "Help Get All PR Issues Addressed")
        self.assertIsNone(self.prompt("s-0001", "next", session_title="AA: Help Get All PR Issues Addressed"))

    def test_mac_registers_in_process(self) -> None:
        def no_ssh(*a, **k):
            raise AssertionError("the Mac never uses ssh")

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"},
                        env=self.env(), runner=no_ssh, platform="darwin")
        self.assertEqual(out["sessionTitle"], "AA: New Chat")
        self.assertEqual(self.lookup("m-0001", "mac")["label"], "AA")

    def test_a_named_chat_keeps_its_words(self) -> None:
        out = self.start("s-0001", source="resume", session_title="Fleet PR Sweep")
        self.assertEqual(out["sessionTitle"], "AA: Fleet PR Sweep")
        self.assertIsNone(self.prompt("s-0001", "anything", session_title="AA: Fleet PR Sweep"))

    def test_resume_titles_from_the_transcript(self) -> None:
        path = os.path.join(self.home, ".claude", "projects", "s-0001.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "user", "isMeta": True, "message": {"content": "<local-command>"}}) + "\n")
            fh.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "rotate the infisical tokens"}]}}) + "\n")
        out = self.start("s-0001", source="resume", transcript_path=path)
        self.assertEqual(out["sessionTitle"], "AA: Rotate the Infisical Tokens")

    def test_outside_paths_are_not_read(self) -> None:
        stray = os.path.join(self.tmp.name, "stray.jsonl")
        with open(stray, "w") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "secret plans"}}) + "\n")
        self.assertEqual(CL.title_from_transcript(stray, self.home), "")

    def test_clear_closes_and_reopen_checks_again(self) -> None:
        with self.small_space("AA", "AB"):
            self.start("s-0001", session_title="Old Name")  # AA
            self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0001", "reason": "clear"})
            self.assertEqual(self.lookup("s-0001")["state"], "closed")
            self.assertEqual(self.note("s-0001")["ended"]["reason"], "clear")
            self.start("s-0002")  # AB:  AA was used, AB never was
            self.start("s-0003")  # AA again, the only pair left
            self.set_seen("s-0002", self.ago(300))
            out = self.start("s-0001", source="resume", session_title="AA: Old Name")
        # AA is s-0003's now and every pair is live, so the least recently seen chat (s-0002) gives up AB.
        self.assertEqual(out["sessionTitle"], "AB: Old Name")
        self.assertIn("earlier letter AA went to another chat", out["additionalContext"])
        self.assertIn("  ", out["additionalContext"])  # rendered copy takes U+00A0 sentence gaps

    def test_clear_closes_and_the_same_pair_comes_back_when_it_is_free(self) -> None:
        self.start("s-0001", session_title="Old Name")
        self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0001", "reason": "clear"})
        self.start("s-0002")  # AB:  the freed AA waits behind the pairs nobody used
        out = self.start("s-0001", source="resume", session_title="AA: Old Name")
        self.assertNotIn("sessionTitle", out)  # the title already reads "AA: Old Name"
        self.assertNotIn("went to another chat", out["additionalContext"])
        self.assertEqual(self.lookup("s-0001")["label"], "AA")

    def test_exit_and_resume_keep_the_letter(self) -> None:
        self.start("s-0001")
        for reason in ("prompt_input_exit", "resume"):
            self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0001", "reason": reason})
            self.assertEqual(self.lookup("s-0001")["state"], "active")
        self.assertEqual(self.note("s-0001")["ended"]["reason"], "resume")

    def test_archive_closes_and_a_host_can_name_its_reasons(self) -> None:
        self.start("s-0001")
        self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0001", "reason": "other"})
        self.assertEqual(self.lookup("s-0001")["state"], "closed")
        self.start("s-0002")
        env = self.server_env(CLAUDE_LETTER_CLOSE_ON="clear")
        self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0002", "reason": "other"}, env=env)
        self.assertEqual(self.lookup("s-0002")["state"], "active")

    def test_clear_start_ignores_the_inherited_name(self) -> None:
        out = self.start("s-0001", source="clear", session_title="ZZ: Previous Chat")
        self.assertNotIn("sessionTitle", out)  # Claude Code ignores a title from a clear start
        out = self.prompt("s-0001", "write the tests", session_title="ZZ: Previous Chat")
        self.assertEqual(out["sessionTitle"], "AA: Write the Tests")

    def test_a_letter_retired_while_open_is_reissued(self) -> None:
        with self.small_space("AA", "AB"):
            self.start("s-0001")  # AA
            self.prompt("s-0001", "first task", session_title="AA: New Chat")
            reg = OR.SessionRegistry(self.db)
            try:
                reg.close(self.lookup("s-0001")["record_id"], source="test")
            finally:
                reg.close_db()
            self.start("s-0002")  # AB:  AA was used, AB never was
            self.start("s-0003")  # AA goes to another chat
            self.set_seen("s-0002", self.ago(300))
            later = time.time() + CL.TOUCH_SECONDS + 5
            out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                             "session_title": "AA: First Task"}, now=later)
        self.assertEqual(out["sessionTitle"], "AB: First Task")
        self.assertEqual(self.lookup("s-0001")["label"], "AB")

    def test_a_placeholder_is_never_taken_for_a_title(self) -> None:
        self.start("s-0001")
        out = self.start("s-0001", source="resume", session_title="AA: New Chat")
        self.assertNotIn("sessionTitle", out)
        self.assertFalse(self.note("s-0001")["titled"])
        out = self.prompt("s-0001", "fix the build", session_title="AA: New Chat")
        self.assertEqual(out["sessionTitle"], "AA: Fix the Build")

    def test_a_bare_slash_command_waits_for_the_next_prompt(self) -> None:
        self.start("s-0001")
        self.assertIsNone(self.prompt("s-0001", "/review", session_title="AA: New Chat"))
        self.assertFalse(self.note("s-0001")["titled"])
        out = self.prompt("s-0001", "fix the login bug", session_title="AA: New Chat")
        self.assertEqual(out["sessionTitle"], "AA: Fix the Login Bug")

    # ---- renames:  the Operator's title follows the thread ----

    def titled(self, sid: str = "s-0001") -> None:
        """A chat that has its pair and its first title, "AA: Help Get All PR Issues Addressed"."""
        self.start(sid)
        self.prompt(sid, "help get all PR issues addressed", session_title="AA: New Chat")

    def renamed(self, shown: str, *, runner=None, sid: str = "s-0001", now: float | None = None) -> dict | None:
        return self.hook(
            {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "go", "session_title": shown},
            runner=runner, now=now,
        )

    def test_a_rename_that_keeps_the_pair_reaches_the_registry(self) -> None:
        self.titled()
        ssh = FakeSsh(self)
        self.assertIsNone(self.renamed("AA: Release Notes", runner=ssh))  # the title already reads right
        self.assertEqual(self.lookup("s-0001")["title"], "Release Notes")
        self.assertEqual(self.note("s-0001")["title"], "Release Notes")
        self.assertEqual(len(ssh.calls), 1)

    def test_a_rename_that_drops_the_pair_gets_it_back(self) -> None:
        self.titled()
        out = self.renamed("Release Notes")
        self.assertEqual(out["sessionTitle"], "AA: Release Notes")
        self.assertEqual(self.lookup("s-0001")["title"], "Release Notes")

    def test_a_name_that_opens_with_an_acronym_keeps_it(self) -> None:
        self.titled()
        out = self.renamed("PR: Review")
        self.assertEqual(out["sessionTitle"], "AA: PR: Review")
        self.assertEqual(self.lookup("s-0001")["title"], "PR: Review")
        ssh = FakeSsh(self)
        self.assertIsNone(self.renamed("AA: PR: Review", runner=ssh))  # the hook's own title is not a rename
        self.assertEqual(ssh.calls, [])

    def test_the_hooks_own_title_and_the_placeholder_are_not_renames(self) -> None:
        self.titled()
        ssh = FakeSsh(self)
        for shown in ("AA: Help Get All PR Issues Addressed", "AA: New Chat"):
            self.assertIsNone(self.renamed(shown, runner=ssh), shown)
        self.assertEqual(ssh.calls, [])
        self.assertEqual(self.lookup("s-0001")["title"], "Help Get All PR Issues Addressed")

    def test_a_rename_is_retried_when_the_registry_did_not_answer(self) -> None:
        self.titled()
        down = FakeSsh(self, fail=subprocess.TimeoutExpired("ssh", 12))
        out = self.renamed("Release Notes", runner=down)
        self.assertEqual(out["sessionTitle"], "AA: Release Notes")  # the pair goes back in front whatever the registry says
        self.assertEqual(self.note("s-0001")["title"], "Help Get All PR Issues Addressed")  # not taken yet
        self.assertEqual(self.lookup("s-0001")["title"], "Help Get All PR Issues Addressed")
        later = time.time() + CL.RETRY_SECONDS + 5
        self.assertIsNone(self.renamed("AA: Release Notes", now=later))  # once the wait is over it tries again, and lands
        self.assertEqual(self.lookup("s-0001")["title"], "Release Notes")
        self.assertEqual(self.note("s-0001")["title"], "Release Notes")
        self.assertNotIn("rename_failed_at", self.note("s-0001"))

    def test_a_name_that_starts_with_a_dash_is_a_title_not_an_option(self) -> None:
        self.titled()
        for name in ("-v release", "--db /tmp/other release", "-1 fix"):
            out = self.renamed(name)
            self.assertEqual(out["sessionTitle"], "AA: %s" % name, name)
            self.assertEqual(self.lookup("s-0001")["title"], name, name)
        # and over ssh the title is one shell word
        ssh = FakeSsh(self)
        self.renamed("--help me", runner=ssh)
        self.assertIn("--title=", ssh.calls[-1][8])

    def test_a_secret_in_a_new_name_never_reaches_the_registry(self) -> None:
        self.titled()
        key = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"  # Zulip-shaped:  32 mixed-case letters and digits
        out = self.renamed("Rotate %s Today" % key)
        # The owner's own words stay as they wrote them;  the Operator and the note hold only the scanned copy.
        self.assertEqual(out["sessionTitle"], "AA: Rotate %s Today" % key)
        self.assertEqual(self.lookup("s-0001")["title"], "Rotate Today")
        self.assertNotIn(key, json.dumps(self.note("s-0001")))
        self.assertIsNone(self.renamed("AA: Rotate %s Today" % key))  # and it is not a rename again

    def test_a_rename_on_a_retired_pair_registers_again_under_the_new_words(self) -> None:
        self.titled()
        reg = OR.SessionRegistry(self.db)
        try:
            reg.close(self.lookup("s-0001")["record_id"], source="test")
        finally:
            reg.close_db()
        out = self.renamed("Release Notes")
        self.assertEqual(out["sessionTitle"], "AA: Release Notes")  # AA is free, so the reopened chat has it back
        self.assertEqual(self.lookup("s-0001")["title"], "Release Notes")
        self.assertEqual(self.lookup("s-0001")["state"], "active")

    def test_deleting_only_the_pair_from_the_title_puts_it_back_without_the_registry(self) -> None:
        self.titled()
        ssh = FakeSsh(self)
        out = self.renamed("Help Get All PR Issues Addressed", runner=ssh)  # same words, the "AA: " gone
        self.assertEqual(out["sessionTitle"], "AA: Help Get All PR Issues Addressed")
        self.assertEqual(ssh.calls, [])  # nothing for the Operator to learn
        self.assertEqual(self.lookup("s-0001")["title"], "Help Get All PR Issues Addressed")

    def test_a_name_cleared_down_to_the_pair_changes_nothing(self) -> None:
        self.titled()
        ssh = FakeSsh(self)
        self.assertIsNone(self.renamed("AA:", runner=ssh))
        self.assertEqual(ssh.calls, [])
        self.assertEqual(self.lookup("s-0001")["title"], "Help Get All PR Issues Addressed")
        self.assertEqual(CL.bare("AA:", "AA"), "")
        self.assertEqual(CL.bare("AA:"), "")

    def test_a_long_name_is_kept_whole_and_only_the_registry_copy_is_cut(self) -> None:
        self.titled()
        long = "Investigate the flaky checkout integration test failures on staging and prod today"
        self.assertGreater(len(long), CL.TITLE_CHARS)
        ssh = FakeSsh(self)
        self.assertIsNone(self.renamed("AA: " + long, runner=ssh))  # the owner's own title is left alone
        self.assertEqual(len(ssh.calls), 1)
        stored = self.lookup("s-0001")["title"]
        self.assertLessEqual(len(stored), CL.TITLE_CHARS)
        self.assertTrue(long.startswith(stored))
        self.assertIsNone(self.renamed("AA: " + long, runner=ssh))  # and it is not a rename again on the next prompt
        self.assertEqual(len(ssh.calls), 1)
        out = self.renamed(long, runner=ssh)  # with the pair dropped it comes back in front of the whole name
        self.assertEqual(out["sessionTitle"], "AA: " + long)
        self.assertEqual(len(ssh.calls), 1)

    def test_a_rename_the_registry_did_not_take_waits_before_it_asks_again(self) -> None:
        self.titled()
        down = FakeSsh(self, fail=subprocess.TimeoutExpired("ssh", 12))
        first = self.renamed("Release Notes", runner=down)
        self.assertEqual(first["sessionTitle"], "AA: Release Notes")
        self.assertEqual(len(down.calls), 1)
        for _ in range(3):  # prompt after prompt:  no more ssh while it waits, and nothing to say about the title
            self.assertIsNone(self.renamed("AA: Release Notes", runner=down))
        self.assertEqual(len(down.calls), 1)
        later = time.time() + CL.RETRY_SECONDS + 5
        self.assertIsNone(self.renamed("AA: Release Notes", runner=down, now=later))
        self.assertEqual(len(down.calls), 2)  # the wait is over
        # A different name is not the one that failed, so it is tried at once.
        self.renamed("AA: Another Name", runner=down, now=later + 1)
        self.assertEqual(len(down.calls), 3)

    def test_a_chat_that_took_a_new_letter_after_a_failed_start_drops_the_old_one_from_its_title(self) -> None:
        down = FakeSsh(self, fail=subprocess.TimeoutExpired("ssh", 12))
        with self.small_space("AA", "AB"):
            self.start("s-0001", session_title="Fix Login")  # AA
            self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0001", "reason": "clear"})
            self.start("s-0002")  # AB
            self.start("s-0003")  # AA again
            self.set_seen("s-0002", self.ago(300))
            # The resume cannot reach the registry, so the chat keeps showing "AA: Fix Login".
            out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "resume",
                             "session_title": "AA: Fix Login"}, runner=down)
            self.assertIn("No Operator letter yet", out["additionalContext"])
            later = time.time() + CL.RETRY_SECONDS + 5
            out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go",
                             "session_title": "AA: Fix Login"}, now=later)
        self.assertEqual(out["sessionTitle"], "AB: Fix Login")  # not "AB: AA: Fix Login"
        self.assertEqual(self.lookup("s-0001")["title"], "Fix Login")

    def test_a_secret_in_a_name_given_before_the_first_prompt_is_not_kept_in_the_note(self) -> None:
        key = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"  # Zulip-shaped:  32 mixed-case letters and digits
        self.start("s-0001", source="resume", session_title="Deploy %s now" % key)
        self.assertNotIn(key, json.dumps(self.note("s-0001")))
        self.assertEqual(self.lookup("s-0001")["title"], "Deploy now")

    def test_the_acronym_tradeoff_is_pinned_for_a_chat_with_no_note(self) -> None:
        # With no note there is no letter of its own to go by:  two capitals and a colon are taken for one.
        out = self.start("s-0001", source="resume", session_title="PR: Review Sweep")
        self.assertEqual(out["sessionTitle"], "AA: Review Sweep")
        out = self.start("s-0002", source="resume", session_title="BC: Fix Login")  # a letter from another machine
        self.assertEqual(out["sessionTitle"], "AB: Fix Login")
        # With its own letter known, an acronym stays.
        self.assertEqual(CL.own_words("PR: Review Sweep", {"label": "AA"}), "PR: Review Sweep")

    def test_a_dash_name_reaches_the_registry_from_the_mac_and_on_a_first_registration(self) -> None:
        def no_ssh(*a, **k):
            raise AssertionError("the Mac never uses ssh")

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "resume",
                         "session_title": "-v notes"}, env=self.env(), runner=no_ssh, platform="darwin")
        self.assertEqual(out["sessionTitle"], "AA: -v notes")
        self.assertEqual(self.lookup("m-0001", "mac")["title"], "-v notes")

    def test_a_retired_pair_that_cannot_be_replaced_leaves_the_old_pair_off_the_title(self) -> None:
        with self.small_space("AA"):
            self.titled()
            reg = OR.SessionRegistry(self.db)
            try:
                reg.close(self.lookup("s-0001")["record_id"], source="test")
            finally:
                reg.close_db()
            OR.register_session(  # the only pair goes to a chat this hook does not manage
                self.db, platform="claude", verified_host="mac", profile_scope="default", native_session_id="op-0001",
                seat="CLAUDE", source="operator-registration", issuer=OR.ISSUER_OPERATOR, letter_space="double",
            )
            later = time.time() + CL.TOUCH_SECONDS + 5
            out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go",
                             "session_title": "AA: First Task"}, now=later)
        self.assertEqual(out["sessionTitle"], "First Task")  # the stale pair is taken off
        self.assertEqual(self.note("s-0001")["state"], "failed")

    # ---- a platform that runs Claude as its engine asks for its own letter ----

    def test_a_botfleet_turn_takes_no_claude_letter_and_touches_nothing(self) -> None:
        botfleet = dict(
            AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="BF-MONITOR", AGENT_SEAT="BF-MONITOR",
            AGENT_SESSION="bf-thread-1", AGENT_SYNC_ATTACH="0",
        )
        ssh = FakeSsh(self)
        events = (
            {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
            {"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build"},
            {"hook_event_name": "SessionEnd", "session_id": "s-0001", "reason": "other"},
        )
        for env in (botfleet, {"AGENT_SYNC_ATTACH": "0"}, {"AGENT_LAUNCHER": "opencode"}):
            for payload in events:
                self.assertIsNone(self.hook(payload, env=self.server_env(**env), runner=ssh), (env, payload))
        self.assertEqual(ssh.calls, [])
        self.assertIsNone(self.lookup("s-0001"))
        self.assertFalse(os.path.exists(os.path.join(self.state, "CLAUDE")))

    def test_the_remote_must_be_named_and_plain(self) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}
        ssh = FakeSsh(self)
        for remote in ("", "-oProxyCommand=touch /tmp/x"):
            out = self.hook(payload, env=self.env(CLAUDE_LETTER_LOCATION="server", CLAUDE_LETTER_REMOTE=remote),
                            runner=ssh)
            self.assertIn("CLAUDE_LETTER_REMOTE does not name an ssh destination", out["additionalContext"])
        self.assertEqual(ssh.calls, [])

    def test_a_malformed_answer_is_refused(self) -> None:
        def bad(cmd, **kwargs):
            return SimpleNamespace(stdout='{"outcome": "registered", "label": "AAA", "generation": 1}\n', returncode=0)

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=bad)
        self.assertIn("malformed label", out["additionalContext"])
        self.assertEqual(self.note("s-0001")["state"], "failed")

    def test_a_seat_with_a_separator_stays_under_the_state_root(self) -> None:
        self.assertEqual(
            CL.note_path(self.env(), "../../tmp/x", "s-0001"),
            os.path.join(self.state, "CLAUDE", "letters", "s-0001.json"),
        )

    def test_compact_only_reminds(self) -> None:
        self.start("s-0001")
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "compact"}, runner=ssh)
        self.assertEqual(ssh.calls, [])
        self.assertNotIn("sessionTitle", out)
        self.assertIn("Claude AA", out["additionalContext"])

    def test_unreachable_registry_never_blocks_and_retries_later(self) -> None:
        down = FakeSsh(self, fail=subprocess.TimeoutExpired("ssh", 12))
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=down)
        self.assertIn("No Operator letter yet", out["additionalContext"])
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x"},
                                    runner=down))
        self.assertEqual(len(down.calls), 1)  # inside the retry window nothing is tried
        later = time.time() + CL.RETRY_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build"},
                        now=later)
        self.assertEqual(out["sessionTitle"], "AA: Fix the Build")

    def test_out_of_scope_and_unknown_hosts(self) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}
        self.assertIsNone(self.hook(payload, env=self.env(CLAUDE_CODE_ENTRYPOINT="sdk-cli")))
        out = self.hook(payload, env=self.env())
        self.assertIn("No Operator letter", out["additionalContext"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "CLAUDE", "letters")))

    def test_install_is_idempotent_keeps_other_hooks_and_removes_cleanly(self) -> None:
        path = os.path.join(self.home, ".claude", "settings.json")
        other = {"hooks": [{"type": "command", "command": "/x/guard.py", "timeout": 5}]}
        with open(path, "w") as fh:
            json.dump({"env": {"AGENT_SEAT": "CLAUDE"}, "hooks": {"SessionStart": [other]}}, fh)

        def run(*argv: str) -> tuple[int, dict]:
            out = io.StringIO()
            return CL.install(list(argv), {"HOME": self.home}, out), json.loads(out.getvalue())

        os.chmod(path, 0o640)
        code, out = run("--location", "server", "--remote", "mac")
        self.assertEqual(
            sorted(out["changed"]),
            ["CLAUDE_LETTER_LOCATION", "CLAUDE_LETTER_REMOTE", "SessionEnd", "SessionStart", "UserPromptSubmit"],
        )
        self.assertEqual(os.stat(out["backup"]).st_mode & 0o777, 0o600)  # a backup is private whatever the source
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)  # settings.json keeps its own mode
        with open(path) as fh:
            settings = json.load(fh)
        self.assertEqual(
            settings["env"],
            {"AGENT_SEAT": "CLAUDE", "CLAUDE_LETTER_LOCATION": "server", "CLAUDE_LETTER_REMOTE": "mac"},
        )
        self.assertEqual(settings["hooks"]["SessionStart"][0], other)
        ours = settings["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(ours["command"], os.path.join(self.home, ".local", "bin", "simple-chat") + " letter hook")
        self.assertEqual(ours["timeout"], 15)
        self.assertEqual(run("--location", "server", "--remote", "mac")[1]["changed"], [])
        self.assertEqual(run("--check")[0], 0)
        self.assertEqual(run("--remote=-oProxyCommand=x")[0], 2)  # the form that gets past argparse
        run("--remove")
        with open(path) as fh:
            settings = json.load(fh)
        self.assertEqual(settings["hooks"], {"SessionStart": [other]})
        self.assertEqual(settings["env"], {"AGENT_SEAT": "CLAUDE"})  # --remove takes its two settings out too
        self.assertEqual(run("--check")[0], 1)

    def test_install_refuses_a_settings_env_that_is_not_an_object(self) -> None:
        path = os.path.join(self.home, ".claude", "settings.json")
        for bad in ({"env": ["x"]}, {"hooks": ["x"]}, {"hooks": {"SessionStart": "x"}}):
            with open(path, "w") as fh:
                json.dump(bad, fh)
            for argv in (["--location", "server"], ["--check"], ["--remove"]):
                out = io.StringIO()
                self.assertEqual(CL.install(argv, {"HOME": self.home}, out), 2)
                self.assertIn("error", json.loads(out.getvalue()))
            with open(path) as fh:
                self.assertEqual(json.load(fh), bad)  # refused, never edited

    def test_install_reads_a_null_env_as_empty(self) -> None:
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w") as fh:
            json.dump({"env": None}, fh)
        out = io.StringIO()
        self.assertEqual(CL.install(["--location", "server"], {"HOME": self.home}, out), 0)

    def test_a_long_prompt_scans_a_bounded_number_of_words(self) -> None:
        calls = []
        real = CL.secretscan.scan

        def counting(text, *a, **k):
            calls.append(text)
            return real(text, *a, **k)

        with mock.patch.object(CL.secretscan, "scan", side_effect=counting):
            title = CL.title_from_prompt("token=" * 200000 + " fix login")
        self.assertLessEqual(len(calls), CL.PROMPT_SCAN_WORDS)
        self.assertIsInstance(title, str)

    def test_install_creates_a_private_claude_folder(self) -> None:
        fresh = os.path.join(self.tmp.name, "fresh-home")
        out = io.StringIO()
        self.assertEqual(CL.install(["--location", "mac"], {"HOME": fresh}, out), 0)
        self.assertEqual(os.stat(os.path.join(fresh, ".claude")).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(fresh, ".claude", "settings.json")).st_mode & 0o777, 0o600)

    def test_main_never_fails_a_hook(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = CL.main(["hook"], env=self.server_env(), stdin=io.StringIO("not json"), stdout=out, stderr=err)
        self.assertEqual((code, out.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
