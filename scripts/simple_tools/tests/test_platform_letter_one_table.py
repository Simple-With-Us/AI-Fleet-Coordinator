"""The one letter table (owner, Nov 11, 2026:  "one operator that has one tool/process for all platforms"):  Claude
Code and MiniMax Code are rows of `platform_letter` like every other platform, and the shared behaviour the PR #1838
review asked for holds for every row.  The assigner is the registry API served in process (FakeRegistry)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import unittest

from simple_tools import platform_letter as PL
from simple_tools import platform_letter_claude as PC
from simple_tools import platform_letter_minimax as PM
from simple_tools.tests.registry_fake import FakeRegistry
from simple_tools.tests.test_platform_letter import Calls, FlakyCalls, make_spec

PAIR = r"^[A-Z]{2}$"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.home)
        self.fake = FakeRegistry(os.path.join(self.tmp.name, "assigner")).install()
        self.addCleanup(self.fake.uninstall)

    def env(self, **extra: str) -> dict[str, str]:
        out = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.state, **self.fake.env()}
        out.update(extra)
        return out

    def run_hook(self, spec, payload, *, env=None, now=None, transport=None) -> str:
        out = io.StringIO()
        code = PL.hook(spec, env if env is not None else self.env(), io.StringIO(json.dumps(payload)), out,
                       now=time.time() if now is None else now, platform="darwin", transport=transport)
        self.assertEqual(code, 0)
        return out.getvalue()

    def note(self, seat: str, sid: str) -> dict:
        with open(os.path.join(self.state, seat, "letters", sid + ".json"), encoding="utf-8") as fh:
            return json.load(fh)


def hook_answer(text: str) -> dict:
    return json.loads(text)["hookSpecificOutput"] if text.strip() else {}


class ClaudeRow(Base):
    def claude_env(self, **extra: str) -> dict[str, str]:
        return self.env(CLAUDE_CODE_ENTRYPOINT="claude-desktop", **extra)

    def test_the_row_is_in_the_table(self) -> None:
        self.assertIs(PL.load_spec("claude"), PC.SPEC)
        self.assertEqual((PC.SPEC.seat, PC.SPEC.events["Stop"], PC.SPEC.close_reasons), ("CLAUDE", "turn", ("clear", "logout")))

    def test_only_interactive_chats_take_part(self) -> None:
        self.assertEqual(PC.claude_kind({}, {"CLAUDE_CODE_ENTRYPOINT": "cli"}), "chat")
        self.assertEqual(PC.claude_kind({}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}), "headless")
        self.assertEqual(PC.claude_kind({}, {"CLAUDE_CODE_ENTRYPOINT": "cli", "SIMPLE_TOOLS_ATTACH": "0"}), "headless")
        out = self.run_hook(PC.SPEC, {"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "startup"},
                            env=self.env(CLAUDE_CODE_ENTRYPOINT="sdk-cli"))
        self.assertEqual(out, "")

    def test_a_chat_gets_a_letter_and_a_title_in_the_hook_answer(self) -> None:
        out = hook_answer(self.run_hook(PC.SPEC, {"hook_event_name": "SessionStart", "session_id": "c-0001",
                                                   "source": "startup"}, env=self.claude_env()))
        label = self.note("CLAUDE", "c-0001")["label"]
        self.assertRegex(label, PAIR)
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % label)
        self.assertIn('"@**Claude** %s"' % label, out["additionalContext"])

    def test_a_chat_with_no_letter_is_titled_with_the_mark(self) -> None:
        env = self.claude_env()
        env.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL")
        out = hook_answer(self.run_hook(PC.SPEC, {"hook_event_name": "SessionStart", "session_id": "c-0002",
                                                   "source": "startup", "session_title": "Fix the build"}, env=env))
        self.assertEqual(out["sessionTitle"], "??: Fix the build")
        self.assertIn("No Operator letter yet", out["additionalContext"])

    def test_the_parent_comes_from_the_desktop_apps_own_records(self) -> None:
        root = os.path.join(self.home, *PC.STORE, "acct", "org")
        os.makedirs(root)
        for host, cli, spawned in (("local_parent", "p-0001", None), ("local_child", "c-0001", "local_parent")):
            record = {"cliSessionId": cli}
            if spawned:
                record["spawnedFrom"] = {"sessionId": spawned}
            with open(os.path.join(root, host + ".json"), "w", encoding="utf-8") as fh:
                json.dump(record, fh)
        env = {"HOME": self.home, "CLAUDE_CODE_HOST_SESSION_ID": "local_child"}
        self.assertEqual(PC.claude_parent({"session_id": "c-0001"}, env), "p-0001")
        # A `claude` started inside that chat inherits its app id, and the record is not its own:  no parent.
        self.assertEqual(PC.claude_parent({"session_id": "x-0009"}, env), "")
        self.assertEqual(PC.claude_parent({"session_id": "p-0001"}, env), "")  # the parent's own lookup:  not a loop

    def test_the_installer_replaces_the_retired_claude_hooks_in_place(self) -> None:
        path = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(path))
        old = {"hooks": {ev: [{"hooks": [{"type": "command", "command": "simple-chat letter hook", "timeout": 20}]}]
                         for ev in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        out = io.StringIO()
        self.assertEqual(PL.install_main(["--platform", "claude"], self.env(), out), 0)
        hooks = json.load(open(path, encoding="utf-8"))["hooks"]
        for ev in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
            commands = [h["command"] for group in hooks[ev] for h in group["hooks"]]
            self.assertEqual(len(commands), 1, ev)
            self.assertTrue(commands[0].endswith("platform-letter hook --platform claude"), commands[0])


class MiniMaxRow(Base):
    DDL = ("CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
           " parent_session_id TEXT)")

    def setUp(self) -> None:
        super().setUp()
        self.store = os.path.join(self.home, ".minimax", "v2", "sqlite", "runtime-state.sqlite")
        os.makedirs(os.path.dirname(self.store))
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(self.DDL)
        conn.close()

    def add(self, sid: str, title: str = "", kind: str = "conversation", parent: str = "") -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("INSERT INTO local_runtime_sessions VALUES (?, ?, ?, ?)", (sid, title, kind, parent or None))
        conn.close()

    def title(self, sid: str) -> str:
        conn = sqlite3.connect(self.store)
        row = conn.execute("SELECT title FROM local_runtime_sessions WHERE session_id = ?", (sid,)).fetchone()
        conn.close()
        return row[0]

    def set_title(self, sid: str, title: str) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE local_runtime_sessions SET title = ? WHERE session_id = ?", (title, sid))
        conn.close()

    def test_the_row_is_in_the_table(self) -> None:
        self.assertIs(PL.load_spec("minimax"), PM.SPEC)
        self.assertEqual((PM.SPEC.seat, PM.SPEC.output, PM.SPEC.events["Stop"]), ("MM", "text", "turn"))

    def test_kinds_parents_and_titles_come_from_the_store(self) -> None:
        self.add("m-0001", "Memory sync")
        self.add("m-0002", "", "conversation", "m-0001")
        self.add("m-0003", "", "cron")
        env = self.env()
        self.assertEqual(PM.minimax_kind({"session_id": "m-0001"}, env), "chat")
        self.assertEqual(PM.minimax_kind({"session_id": "m-0002"}, env), "subagent")
        self.assertEqual(PM.minimax_kind({"session_id": "m-0003"}, env), "headless")
        self.assertEqual(PM.minimax_kind({"session_id": "m-9999"}, env), "chat")
        self.assertEqual(PM.minimax_parent({"session_id": "m-0002"}, env), "m-0001")
        self.assertEqual(PM.minimax_title({"session_id": "m-0001"}, env), "Memory sync")

    def test_a_start_letters_the_stored_title_and_tells_the_model_in_plain_text(self) -> None:
        self.add("m-0001", "Memory sync")
        out = self.run_hook(PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"})
        label = self.note("MM", "m-0001")["label"]
        self.assertEqual(self.title("m-0001"), "%s: Memory sync" % label)
        self.assertTrue(out.startswith("Operator session letter: MiniMax %s" % label), out)

    def test_a_turn_end_puts_the_letter_back_on_a_regenerated_title(self) -> None:
        self.add("m-0001", "")
        t0 = time.time()
        self.run_hook(PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"}, now=t0)
        label = self.note("MM", "m-0001")["label"]
        self.assertEqual(self.title("m-0001"), "%s: New Chat" % label)
        self.set_title("m-0001", "Fix the wake path")
        self.assertEqual(self.run_hook(PM.SPEC, {"hook_event_name": "Stop", "session_id": "m-0001"}, now=t0 + 5), "")
        self.assertEqual(self.title("m-0001"), "%s: Fix the wake path" % label)
        self.assertEqual(self.fake.lookup("minimax", "mac", "default", "m-0001")["title"], "Fix the wake path")

    def test_no_letter_is_the_mark_and_a_turn_end_asks_again(self) -> None:
        self.add("m-0001", "Memory sync")
        env = self.env()
        env.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL")
        t0 = time.time()
        self.run_hook(PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"},
                      env=env, now=t0)
        self.assertEqual(self.title("m-0001"), "??: Memory sync")
        self.run_hook(PM.SPEC, {"hook_event_name": "Stop", "session_id": "m-0001"}, now=t0 + PL.RETRY_SECONDS + 1)
        label = self.note("MM", "m-0001")["label"]
        self.assertEqual(self.title("m-0001"), "%s: Memory sync" % label)

    def test_the_installer_replaces_the_retired_minimax_hooks_in_place(self) -> None:
        path = os.path.join(self.home, ".minimax", "hooks.json")
        old = {"enabled": True, "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "/x/mm-session-letter hook-session-start"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/x/mm-session-letter hook-stop"}]}],
        }}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        self.assertEqual(PL.install_main(["--platform", "minimax"], self.env(), io.StringIO()), 0)
        doc = json.load(open(path, encoding="utf-8"))
        self.assertTrue(doc["enabled"])
        for ev in ("SessionStart", "Stop"):
            commands = [h["command"] for group in doc["hooks"][ev] for h in group["hooks"]]
            self.assertEqual(len(commands), 1, ev)
            self.assertTrue(commands[0].endswith("platform-letter hook --platform minimax"), commands[0])


class MiniMaxRetiredClient(Base):
    """What the retired MiniMax client (`mm-session-letter`) left on a host:  its hooks in ~/.minimax/hooks.json, its
    wrappers, and its own cache of each chat's letter."""

    add, title = MiniMaxRow.add, MiniMaxRow.title

    def setUp(self) -> None:
        super().setUp()
        self.store = os.path.join(self.home, ".minimax", "v2", "sqlite", "runtime-state.sqlite")
        os.makedirs(os.path.dirname(self.store))
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(MiniMaxRow.DDL)
        conn.close()

    def hooks_file(self) -> str:
        return os.path.join(self.home, ".minimax", "hooks.json")

    def old_hooks(self) -> None:
        def entry(command: str, **extra) -> dict:
            return {"hooks": [{"type": "command", "command": command, **extra}]}

        hooks = {
            "SessionStart": [entry("mm-session-letter hook-session-start", timeout=20)],
            "PreToolUse": [{"matcher": "Bash", "description": "Letter In Posts",
                            "hooks": [{"type": "command", "command": "mm-session-letter hook-pre-tool-use"}]},
                           {"matcher": "Write", "hooks": [{"type": "command", "command": "another-tool guard"}]}],
            "Stop": [entry("mm-session-letter hook-stop", timeout=15)],
        }
        with open(self.hooks_file(), "w", encoding="utf-8") as fh:
            json.dump({"hooks": hooks}, fh)

    def install(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        code = PL.install_main(["--platform", "minimax", *argv], self.env(), out)
        return code, json.loads(out.getvalue())

    def test_install_replaces_the_retired_hooks_and_takes_out_the_pre_tool_use_entry(self) -> None:
        self.old_hooks()
        code, answer = self.install("--check")
        self.assertEqual((code, answer["retired"]["hooks"]), (1, ["PreToolUse"]))
        code, answer = self.install()
        self.assertEqual((code, sorted(answer["changed"])), (0, ["PreToolUse", "SessionStart", "Stop"]))
        with open(self.hooks_file(), encoding="utf-8") as fh:
            hooks = json.load(fh)["hooks"]
        self.assertNotIn("mm-session-letter", json.dumps(hooks))
        self.assertEqual(hooks["PreToolUse"], [{"matcher": "Write", "hooks": [{"type": "command",
                                                                              "command": "another-tool guard"}]}])
        for event in ("SessionStart", "Stop"):
            commands = [h["command"] for group in hooks[event] for h in group["hooks"]]
            self.assertEqual(len(commands), 1, event)
            self.assertTrue(commands[0].endswith("platform-letter hook --platform minimax"), commands[0])
        self.assertEqual(self.install("--check")[0], 0)
        # The same file, with the retired hooks back, comes out of a remove with only the other tool's hook.
        self.old_hooks()
        self.assertEqual(self.install("--remove")[0], 0)
        with open(self.hooks_file(), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["hooks"], {"PreToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "another-tool guard"}]}]})

    def test_check_names_the_wrappers_that_are_still_on_the_host(self) -> None:
        self.assertEqual(self.install()[0], 0)
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        wrapper = os.path.join(self.home, ".local", "bin", "mm-session-letter")
        with open(wrapper, "w") as fh:
            fh.write("#!/bin/sh\n")
        code, answer = self.install("--check")
        self.assertEqual((code, answer["retired"]), (1, {"hooks": [], "files": [wrapper]}))
        os.remove(wrapper)
        self.assertEqual(self.install("--check")[0], 0)

    def cache(self, sid: str, body: object, *scope: str) -> str:
        folder = os.path.join(self.home, ".minimax", *(scope or ("hook-state",)), "session-letters")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body if isinstance(body, str) else json.dumps(body))
        return path

    def test_the_retired_clients_cache_is_read_where_it_kept_it(self) -> None:
        env = self.env()
        self.assertIsNone(PM.minimax_retired_note(env, "m-0001"))
        path = self.cache("m-0001", {"letter": "QQ", "topic": "Memory sync", "host": "mac", "space": "double"})
        self.assertEqual(PM.minimax_retired_note(env, "m-0001"),
                         {"label": "QQ", "location": "mac", "title": "Memory sync", "seen": os.stat(path).st_mtime})
        # The scopes are read in the retired client's own order:  the user scope, the project scopes, then the old one.
        self.cache("m-0001", {"letter": "QR", "host": "mac"}, "v2", "hook-data", "projects", "abc123")
        self.assertEqual(PM.minimax_retired_note(env, "m-0001")["label"], "QR")
        self.cache("m-0001", {"letter": "QS", "host": "mac"}, "v2", "hook-data", "user")
        self.assertEqual(PM.minimax_retired_note(env, "m-0001")["label"], "QS")
        # A hook is told its own scope, and reads that one alone.
        scope = os.path.join(self.tmp.name, "scope")
        os.makedirs(os.path.join(scope, "session-letters"))
        with open(os.path.join(scope, "session-letters", "m-0001.json"), "w") as fh:
            json.dump({"letter": "QT", "host": "server"}, fh)
        told = self.env(MINIMAX_HOOKS_DATA=scope)
        self.assertEqual((PM.minimax_retired_note(told, "m-0001")["label"],
                          PM.minimax_retired_note(told, "m-0001")["location"]), ("QT", "server"))
        self.assertIsNone(PM.minimax_retired_note(told, "m-0002"))
        # Anything that is not the cache's shape is no record, and a symlink is never followed.
        for n, body in enumerate(("", "not json", "[1]", {"topic": "no letter"}, {"letter": 7}, {"letter": ""})):
            self.cache("m-01%02d" % n, body)
            self.assertIsNone(PM.minimax_retired_note(env, "m-01%02d" % n), body)
        outside = os.path.join(self.tmp.name, "outside.json")
        with open(outside, "w") as fh:
            json.dump({"letter": "ZZ", "host": "mac"}, fh)
        os.symlink(outside, os.path.join(self.home, ".minimax", "hook-state", "session-letters", "m-0200.json"))
        self.assertIsNone(PM.minimax_retired_note(env, "m-0200"))
        self.assertIs(PM.SPEC.retired_note, PM.minimax_retired_note)

    def test_a_chat_open_across_the_change_keeps_its_letter_at_its_next_turn_end(self) -> None:
        # The retired client registered the chat and cached its letter.  No start event comes for an open chat:
        # the row's first event for it is the end of a turn.
        out = io.StringIO()
        code = PL.registry_main(["register", "--platform", "minimax", "--location", "mac", "--session-id", "m-0001",
                                 "--seat", "MM", "--source", "mm-session-letter", "--title=Memory sync"],
                                env=self.env(), stdout=out)
        self.assertEqual(code, 0, out.getvalue())
        label = json.loads(out.getvalue())["label"]
        self.cache("m-0001", {"letter": label, "topic": "Memory sync", "host": "mac", "space": "double"})
        self.add("m-0001", "Memory sync")  # MiniMax Code regenerated the title without the letter
        before = len(self.fake.requests)
        self.assertEqual(self.run_hook(PM.SPEC, {"hook_event_name": "Stop", "session_id": "m-0001"}), "")
        note = self.note("MM", "m-0001")
        self.assertEqual((note["state"], note["label"], note["platform"], note["location"]),
                         ("open", label, "minimax", "mac"))
        self.assertEqual(self.title("m-0001"), "%s: Memory sync" % label)
        # It was not registered a second time:  the registration is the one the retired client made.
        self.assertFalse([path for method, path in self.fake.requests[before:] if path.endswith("/register")])
        self.assertEqual(self.fake.lookup("minimax", "mac", PL.SCOPE, "m-0001")["label"], label)
        # A chat the retired client never lettered has nothing to adopt:  its turn end does nothing, as before.
        self.assertEqual(self.run_hook(PM.SPEC, {"hook_event_name": "Stop", "session_id": "m-0002"}), "")
        self.assertFalse(os.path.exists(os.path.join(self.state, "MM", "letters", "m-0002.json")))


class SharedBehaviour(Base):
    """What the review found, on a generic row:  it holds for every platform in the table."""

    spec = make_spec()

    def hook(self, payload: dict, *, now: float, transport=None) -> dict:
        return hook_answer(self.run_hook(self.spec, payload, env=self.env(SESSION_LETTER_LOCATION="server"),
                                         now=now, transport=transport or Calls(self)))

    def test_a_lettered_chat_keeps_its_letter_when_a_resume_gets_no_answer(self) -> None:
        t0 = time.time()
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup",
                   "session_title": "Fix the Login Bug"}, now=t0)
        label = self.note("CODEX", "s-0001")["label"]
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "resume",
                         "session_title": "%s: Fix the Login Bug" % label}, now=t0 + 60,
                        transport=FlakyCalls(self, ("register",)))
        self.assertNotIn("sessionTitle", out)  # the title already carries the letter:  nothing to change
        self.assertIn("Operator session letter: Codex %s" % label, out["additionalContext"])
        self.assertEqual(self.note("CODEX", "s-0001")["label"], label)

    def test_a_refusal_takes_the_letter_off(self) -> None:
        note = {"state": "open", "label": "CC"}
        chat = PL.Chat(self.spec, self.env(), "server", "s-0001", now=1.0)
        self.assertEqual(PL._apply(chat, {"error": PL.refused_text(None, "codex")}, dict(note))["label"], "")
        self.assertEqual(PL._apply(chat, {"error": PL.NO_REGISTRATION}, dict(note))["label"], "")
        self.assertEqual(PL._apply(chat, {"error": "the registry did not answer"}, dict(note))["label"], "CC")

    def test_a_rename_of_an_unlettered_chat_keeps_the_mark(self) -> None:
        t0 = time.time()
        down = self.env(SESSION_LETTER_LOCATION="server")
        down.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL")
        self.run_hook(self.spec, {"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "startup",
                                  "session_title": "Write the Report"}, env=down, now=t0)
        spec = make_spec(events={"SessionStart": "start", "SessionRename": "rename"})
        out = hook_answer(self.run_hook(spec, {"hook_event_name": "SessionRename", "session_id": "s-0002",
                                               "session_title": "Quarterly Report"}, env=down, now=t0 + 5))
        self.assertEqual(out["sessionTitle"], "??: Quarterly Report")

    def test_a_letter_on_a_prompt_with_no_words_drops_the_mark(self) -> None:
        t0 = time.time()
        down = self.env(SESSION_LETTER_LOCATION="server")
        down.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL")
        self.run_hook(self.spec, {"hook_event_name": "SessionStart", "session_id": "s-0003", "source": "startup"},
                      env=down, now=t0)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0003", "prompt": "/review",
                         "session_title": "??: New Chat"}, now=t0 + PL.RETRY_SECONDS + 1)
        label = self.note("CODEX", "s-0003")["label"]
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % label)

    def test_the_retry_stamp_is_written_before_the_call(self) -> None:
        t0 = time.time()
        down = self.env(SESSION_LETTER_LOCATION="server")
        down.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL")
        self.run_hook(self.spec, {"hook_event_name": "SessionStart", "session_id": "s-0004", "source": "startup"},
                      env=down, now=t0)
        seen = []

        def hangs(argv):  # a hung assigner:  the hook is killed inside the call
            seen.append(self.note("CODEX", "s-0004").get("failed_at"))
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            PL._run_hook(self.spec, self.env(SESSION_LETTER_LOCATION="server"),
                         io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s-0004",
                                                 "prompt": "hi"})),
                         io.StringIO(), t0 + PL.RETRY_SECONDS + 1, "linux", hangs, "")
        self.assertEqual(seen, [t0 + PL.RETRY_SECONDS + 1])

    def test_a_mac_only_platform_on_a_host_that_does_not_say_is_left_alone(self) -> None:
        mac_only = make_spec(platform="grok", seat="GROK", locations=("mac",))
        out = io.StringIO()
        PL.hook(mac_only, self.env(), io.StringIO(json.dumps({"hook_event_name": "SessionStart",
                                                               "session_id": "g-0001", "source": "startup"})),
                out, platform="linux")
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(os.path.exists(os.path.join(self.state, "GROK")))

    def test_the_no_letter_line_says_what_the_platform_can_do(self) -> None:
        note = {"state": "failed", "error": "not configured"}
        self.assertIn('its title starts with "??: "', PL.no_letter(self.spec, note))
        untitled = make_spec(title_in_output=False, output="hook")
        self.assertIn("gives a hook no way to set a title", PL.no_letter(untitled, note))


class RequestCommand(Base):
    """`simple-chat letter request` is one command on every platform, Claude and MiniMax included."""

    def test_a_claude_chat_is_found_by_its_own_variable_and_by_its_id(self) -> None:
        env = self.env(CLAUDE_CODE_ENTRYPOINT="cli")
        env.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL")
        self.run_hook(PC.SPEC, {"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "startup"},
                      env=env)
        self.assertEqual(self.note("CLAUDE", "c-0001")["label"], "")
        out = io.StringIO()
        self.assertEqual(PL.request_main([], self.env(CLAUDE_CODE_SESSION_ID="c-0001"), out, platform="darwin"), 0)
        self.assertRegex(out.getvalue(), r"^letter [A-Z]{2}: Claude chat c-0001\n$")
        out = io.StringIO()
        self.assertEqual(PL.request_main(["--session-id", "c-0001"], self.env(), out, platform="darwin"), 0)
        self.assertTrue(out.getvalue().startswith("letter "))

    def test_the_fleet_session_variable_finds_any_platform(self) -> None:
        env = self.env(CLAUDE_CODE_ENTRYPOINT="cli")
        self.run_hook(PC.SPEC, {"hook_event_name": "SessionStart", "session_id": "c-0002", "source": "startup"},
                      env=env)
        out = io.StringIO()
        self.assertEqual(PL.request_main([], self.env(AGENT_SESSION="c-0002"), out, platform="darwin"), 0)
        self.assertIn("Claude chat c-0002", out.getvalue())

    def test_a_malformed_note_is_named_not_used(self) -> None:
        path = os.path.join(self.state, "CLAUDE", "letters", "c-0009.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"platform": "claude", "state": "failed", "failed_at": "soon"}, fh)
        out = io.StringIO()
        self.assertEqual(PL.request_main(["--session-id", "c-0009"], self.env(), out, platform="darwin"), 1)
        self.assertEqual(out.getvalue(),
                         "no letter: invalid letter note for chat c-0009: failed_at must be int or float\n")

    def test_the_title_writer_is_given_the_chats_own_payload(self) -> None:
        seen = []

        def writer(request):
            seen.append(dict(request.data))
            return True

        spec = make_spec(platform="faux", seat="CODEX", title_in_output=False, title_writer=writer,
                         chat_payload=lambda sid: {"session_id": sid, "source": "startup"})
        PL.register_spec(spec)
        self.addCleanup(PL.SPECS.pop, "faux", None)
        down = self.env(SESSION_LETTER_LOCATION="server")
        down.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL")
        PL.hook(spec, down, io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "f-0001",
                                                    "source": "startup"})), io.StringIO(), platform="linux")
        seen.clear()
        out = io.StringIO()
        code = PL.request_main(["--session-id", "f-0001"], self.env(SESSION_LETTER_LOCATION="server"), out,
                               platform="linux")
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(seen[-1], {"session_id": "f-0001", "source": "startup"})

    def test_simple_chat_letter_is_the_same_command(self) -> None:
        out = io.StringIO()
        self.assertEqual(PL.main(["request", "--session-id", "n-0001"], env=self.env(), stdout=out), 1)
        self.assertTrue(out.getvalue().startswith("no letter: "))


if __name__ == "__main__":
    unittest.main()
