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


class TitleTests(unittest.TestCase):
    def test_title_from_prompt(self) -> None:
        self.assertEqual(
            CL.title_from_prompt("help get all PR issues addressed from review findings."),
            "Help Get All PR Issues Addressed",
        )
        self.assertEqual(CL.title_from_prompt("```\nx = 1\n```\nfix the iOS build for AFC-564"), "Fix the iOS Build for AFC-564")
        self.assertEqual(CL.title_from_prompt("see https://example.com/a/b and the docs"), "See and the Docs")
        self.assertEqual(CL.title_from_prompt("   \n"), "")

    def test_bare_and_clean(self) -> None:
        self.assertEqual(CL.bare("B: Fix Login"), "Fix Login")
        self.assertEqual(CL.bare("Fix: Login"), "Fix: Login")
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
        self.assertEqual((first["label"], first["outcome"], first["created"]), ("A", "registered", True))
        again = self.register("s-0001")
        self.assertEqual((again["label"], again["outcome"]), ("A", "resumed"))
        code, out = self.registry("title", "--location", "server", "--session-id", "s-0001", "--title", "Renamed")
        self.assertEqual((code, out["label"]), (0, "A"))
        self.assertEqual(self.lookup("s-0001")["title"], "Renamed")
        code, out = self.registry("close", "--location", "server", "--session-id", "s-0001")
        self.assertEqual((code, out["closed"]), (0, True))
        code, out = self.registry("touch", "--location", "server", "--session-id", "s-0001")
        self.assertTrue(out["retired"])
        back = self.register("s-0001", title="Back Again")
        self.assertEqual((back["outcome"], back["label"], back["generation"], back["previous"]), ("reopened", "A", 2, "A"))
        self.assertEqual(self.lookup("s-0001")["title"], "Back Again")

    def test_reopen_takes_the_lowest_free_letter(self) -> None:
        self.register("s-0001")
        self.registry("close", "--location", "server", "--session-id", "s-0001")
        self.assertEqual(self.register("s-0002")["label"], "A")  # A was free, so another chat holds it now
        back = self.register("s-0001")
        self.assertEqual((back["outcome"], back["label"], back["previous"]), ("reopened", "B", "A"))

    def test_sweep_closes_only_stale_claude_chats(self) -> None:
        self.register("s-0001")
        OR.register_session(
            self.db, platform="minimax", verified_host="server", profile_scope="default",
            native_session_id="mm-0001", seat="MM", source="test", issuer=OR.ISSUER_OPERATOR,
        )
        self.set_seen("s-0001", "2026-01-01T00:00:00.000Z")
        self.set_seen("mm-0001", "2026-01-01T00:00:00.000Z")
        out = self.register("s-0002")
        self.assertEqual((out["swept"], out["label"]), (1, "A"))
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
        self.assertEqual(self.register("s-0001")["swept"], 0)
        for i in range(2, 25):  # D to Z:  with A, B and C held, these fill the alphabet
            self.register("s-%04d" % i)
        self.set_seen("s-0001", "2026-10-28T09:00:00.000Z")
        out = self.register("s-0099", now=OR.datetime(2026, 10, 28, 12, tzinfo=OR.UTC).timestamp())
        self.assertEqual((out["evicted"], out["label"]), (1, "C"))  # s-0001 held C;  A and B stay with the Operator's
        for sid in ("op-0001", "op-0002"):
            self.assertEqual(self.lookup(sid, "mac")["state"], "active")

    def test_full_letters_evict_the_least_recently_seen(self) -> None:
        for i in range(26):
            self.register("s-%04d" % i)
            self.set_seen("s-%04d" % i, "2026-10-28T%02d:%02d:00.000Z" % (10 + i // 60, i % 60))
        self.set_seen("s-0007", "2026-10-28T09:00:00.000Z")  # H is the oldest
        now = OR.datetime(2026, 10, 28, 12, tzinfo=OR.UTC).timestamp()
        out = self.register("s-0099", now=now)
        self.assertEqual((out["evicted"], out["label"], out["generation"]), (1, "H", 2))
        self.assertEqual(self.lookup("s-0007")["state"], "closed")


class FakeSsh:
    """Stands in for `ssh mac ...`:  checks the command's shape and runs the remote registry side in process."""

    def __init__(self, test: Base, fail: Exception | None = None):
        self.test, self.fail, self.calls = test, fail, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.fail is not None:
            raise self.fail
        self.test.assertEqual(cmd[:6], ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"])
        self.test.assertEqual(cmd[6], "mac")
        words = shlex.split(cmd[7])
        self.test.assertEqual(words[:3], ["$HOME/.local/bin/simple-chat", "letter", "registry"])
        out = io.StringIO()
        code = CL.registry_main(words[3:], env=self.test.env(), stdout=out)
        return SimpleNamespace(stdout=out.getvalue(), returncode=code)


class HookTests(Base):
    def hook(self, payload: dict, *, env: dict | None = None, runner=None, now: float | None = None,
             platform: str = "linux") -> dict | None:
        out = io.StringIO()
        env = env if env is not None else self.env(CLAUDE_LETTER_LOCATION="server")
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
        self.assertEqual(out["sessionTitle"], "A: New Chat")
        self.assertIn("Claude A", out["additionalContext"])
        out = self.prompt("s-0001", "help get all PR issues addressed from review findings.",
                          session_title="A: New Chat")
        self.assertEqual(out["sessionTitle"], "A: Help Get All PR Issues Addressed")
        self.assertEqual(self.lookup("s-0001")["title"], "Help Get All PR Issues Addressed")
        self.assertIsNone(self.prompt("s-0001", "next", session_title="A: Help Get All PR Issues Addressed"))

    def test_mac_registers_in_process(self) -> None:
        def no_ssh(*a, **k):
            raise AssertionError("the Mac never uses ssh")

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"},
                        env=self.env(), runner=no_ssh, platform="darwin")
        self.assertEqual(out["sessionTitle"], "A: New Chat")
        self.assertEqual(self.lookup("m-0001", "mac")["label"], "A")

    def test_a_named_chat_keeps_its_words(self) -> None:
        out = self.start("s-0001", source="resume", session_title="Fleet PR Sweep")
        self.assertEqual(out["sessionTitle"], "A: Fleet PR Sweep")
        self.assertIsNone(self.prompt("s-0001", "anything", session_title="A: Fleet PR Sweep"))

    def test_resume_titles_from_the_transcript(self) -> None:
        path = os.path.join(self.home, ".claude", "projects", "s-0001.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "user", "isMeta": True, "message": {"content": "<local-command>"}}) + "\n")
            fh.write(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "rotate the infisical tokens"}]}}) + "\n")
        out = self.start("s-0001", source="resume", transcript_path=path)
        self.assertEqual(out["sessionTitle"], "A: Rotate the Infisical Tokens")

    def test_outside_paths_are_not_read(self) -> None:
        stray = os.path.join(self.tmp.name, "stray.jsonl")
        with open(stray, "w") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "secret plans"}}) + "\n")
        self.assertEqual(CL.title_from_transcript(stray, self.home), "")

    def test_clear_closes_and_reopen_checks_again(self) -> None:
        self.start("s-0001", session_title="Old Name")
        self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0001", "reason": "clear"})
        self.assertEqual(self.lookup("s-0001")["state"], "closed")
        self.assertEqual(self.note("s-0001")["ended"]["reason"], "clear")
        self.start("s-0002")  # takes A, the letter /clear freed
        out = self.start("s-0001", source="resume", session_title="A: Old Name")
        self.assertEqual(out["sessionTitle"], "B: Old Name")
        self.assertIn("earlier letter A was retired", out["additionalContext"])

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
        env = self.env(CLAUDE_LETTER_LOCATION="server", CLAUDE_LETTER_CLOSE_ON="clear")
        self.hook({"hook_event_name": "SessionEnd", "session_id": "s-0002", "reason": "other"}, env=env)
        self.assertEqual(self.lookup("s-0002")["state"], "active")

    def test_clear_start_ignores_the_inherited_name(self) -> None:
        out = self.start("s-0001", source="clear", session_title="Z: Previous Chat")
        self.assertNotIn("sessionTitle", out)  # Claude Code ignores a title from a clear start
        out = self.prompt("s-0001", "write the tests", session_title="Z: Previous Chat")
        self.assertEqual(out["sessionTitle"], "A: Write the Tests")

    def test_a_letter_retired_while_open_is_reissued(self) -> None:
        self.start("s-0001")
        self.prompt("s-0001", "first task", session_title="A: New Chat")
        reg = OR.SessionRegistry(self.db)
        try:
            reg.close(self.lookup("s-0001")["record_id"], source="test")
        finally:
            reg.close_db()
        self.start("s-0002")  # A goes to another chat
        later = time.time() + CL.TOUCH_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                         "session_title": "A: First Task"}, now=later)
        self.assertEqual(out["sessionTitle"], "B: First Task")
        self.assertEqual(self.lookup("s-0001")["label"], "B")

    def test_compact_only_reminds(self) -> None:
        self.start("s-0001")
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "compact"}, runner=ssh)
        self.assertEqual(ssh.calls, [])
        self.assertNotIn("sessionTitle", out)
        self.assertIn("Claude A", out["additionalContext"])

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
        self.assertEqual(out["sessionTitle"], "A: Fix the Build")

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

        code, out = run("--location", "server")
        self.assertEqual(sorted(out["changed"]), ["SessionEnd", "SessionStart", "UserPromptSubmit", "env"])
        self.assertTrue(os.path.exists(out["backup"]))
        with open(path) as fh:
            settings = json.load(fh)
        self.assertEqual(settings["env"], {"AGENT_SEAT": "CLAUDE", "CLAUDE_LETTER_LOCATION": "server"})
        self.assertEqual(settings["hooks"]["SessionStart"][0], other)
        ours = settings["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(ours["command"], os.path.join(self.home, ".local", "bin", "simple-chat") + " letter hook")
        self.assertEqual(ours["timeout"], 15)
        self.assertEqual(run("--location", "server")[1]["changed"], [])
        self.assertEqual(run("--check")[0], 0)
        run("--remove")
        with open(path) as fh:
            settings = json.load(fh)
        self.assertEqual(settings["hooks"], {"SessionStart": [other]})
        self.assertEqual(run("--check")[0], 1)

    def test_main_never_fails_a_hook(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = CL.main(["hook"], env=self.env(CLAUDE_LETTER_LOCATION="server"), stdin=io.StringIO("not json"),
                       stdout=out, stderr=err)
        self.assertEqual((code, out.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
