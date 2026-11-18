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
        if os.path.islink(self.home):
            raise RuntimeError("refusing a symlinked directory: %s" % self.home)
        os.makedirs(self.home, mode=0o700)
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

    def slow_transport(self, seconds: float):
        """A registry that takes its time:  the walk's budget is spent on the calls it really makes."""

        def transport(argv):
            time.sleep(seconds)
            out = io.StringIO()
            PL.registry_main(list(argv), env=getattr(self, "env")(), stdout=out)
            return json.loads(out.getvalue())

        return transport

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
           " parent_session_id TEXT, archived INTEGER DEFAULT 0, created_at_ms INTEGER)")

    def setUp(self) -> None:
        super().setUp()
        self.store = os.path.join(self.home, ".minimax", "v2", "sqlite", "runtime-state.sqlite")
        os.makedirs(os.path.dirname(self.store))
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(self.DDL)
        conn.close()

    def add(self, sid: str, title: str = "", kind: str = "conversation", parent: str = "",
            archived: int = 0, age: float = 3600.0) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id,"
                " archived, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (sid, title, kind, parent or None, archived, int((time.time() - age) * 1000)),
            )
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

    def test_the_prompt_event_names_the_chat_from_its_first_message(self) -> None:
        # The hook does not take a brand-new chat's title before the chat has a descriptive one.  MiniMax Code
        # names a chat itself, and a placeholder the hook put there first was put over by the runtime's own name,
        # which took the letter with it.  So the start leaves the name to the runtime, and the letter goes on at
        # the chat's first prompt, on the words the chat has by then.
        self.add("m-0001")
        self.run_hook(PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"})
        label = self.note("MM", "m-0001")["label"]
        self.assertEqual(self.title("m-0001"), "")
        self.run_hook(PM.SPEC, {"hook_event_name": "UserPromptSubmit", "session_id": "m-0001",
                                "prompt": "Coordinate P-BF-6 completion via Zulip"})
        title = self.title("m-0001")
        self.assertTrue(title.startswith(label + ": "), title)
        self.assertNotIn("New Chat", title)
        self.assertIn("P-BF-6", title)

    def test_a_subagent_takes_its_own_letter_and_title(self) -> None:
        # MiniMax Code sends the PARENT's session id for a subagent event, so the child's own id has to be read
        # from the payload, and the child takes the pair after its parent's.  The letter goes on the name the
        # runtime gave the child.
        self.add("m-0200")
        self.run_hook(PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0200", "source": "startup"})
        parent = self.note("MM", "m-0200")
        self.add("m-0201", "Child work", kind="task", parent="m-0200")
        self.run_hook(PM.SPEC, {"hook_event_name": "SubagentStart", "session_id": "m-0200",
                                "childSessionId": "m-0201"})
        child = self.note("MM", "m-0201")
        self.assertRegex(child["label"], PAIR)
        self.assertNotEqual(child["label"], parent["label"])
        self.assertEqual(self.title("m-0201"), "%s: Child work" % child["label"])

    def test_a_subagent_with_no_name_yet_is_left_to_the_runtime(self) -> None:
        # The same rule as a chat:  a subagent with no descriptive name yet keeps the name MiniMax Code gives it,
        # and the letter goes on that name at the subagent's first turn end.
        self.add("m-0200")
        self.run_hook(PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0200", "source": "startup"})
        self.add("m-0201", kind="task", parent="m-0200")
        self.run_hook(PM.SPEC, {"hook_event_name": "SubagentStart", "session_id": "m-0200",
                                "childSessionId": "m-0201"})
        child = self.note("MM", "m-0201")
        self.assertEqual(self.title("m-0201"), "")
        self.set_title("m-0201", "Child work")
        self.run_hook(PM.SPEC, {"hook_event_name": "SubagentStop", "session_id": "m-0200",
                                "childSessionId": "m-0201"})
        self.assertEqual(self.title("m-0201"), "%s: Child work" % child["label"])

    def test_the_hook_timeouts_stay_inside_the_runtime_cap(self) -> None:
        # MiniMax Code's runtime takes a command hook's timeout from 1 to 10 seconds and refuses anything longer with
        # HOOK_SCHEMA_INVALID, so an entry over the cap is never run:  the hook file reads as installed and no letter
        # note is written and no title is renamed.  The cap is the runtime's, so the timeouts stay under it.
        self.assertEqual(PM.HOOK_TIMEOUT_CAP, 10)
        for event, timeout in PM.HOOK_TIMEOUTS.items():
            self.assertGreaterEqual(timeout, 1, event)
            self.assertLessEqual(timeout, PM.HOOK_TIMEOUT_CAP, event)
        self.assertEqual(PM.INSTALL.timeout_cap, PM.HOOK_TIMEOUT_CAP)
        self.assertEqual(PM.INSTALL.events, PM.HOOK_TIMEOUTS)
        # And the chain fits inside the budget:  the chat's own calls at the row's own deadlines, and
        # the ancestor walk may spend what is left of the budget after them.  An ancestor's
        # registration chains three calls (the register, the reopen and the title update), so what
        # bounds the walk is the time it may spend, not a count of calls.
        self.assertLessEqual(
            PL.REGISTER_CALLS * PM.CALL_TIMEOUT + PL.walk_allowance(PM.SPEC),
            min(PM.HOOK_TIMEOUTS.values()),
        )
        self.assertLess(PM.CALL_TIMEOUT, PL._API_TIMEOUT)
        self.assertEqual(PM.SPEC.call_timeout, PM.CALL_TIMEOUT)
        self.assertEqual(PM.SPEC.parent_call_timeout, PM.PARENT_CALL_TIMEOUT)
        self.assertEqual(PM.SPEC.hook_budget, PM.HOOK_BUDGET)
        self.assertEqual(PL.walk_allowance(PM.SPEC), PM.HOOK_BUDGET - PL.REGISTER_CALLS * PM.CALL_TIMEOUT)
        # The rows that keep the shared depth and the shared deadline are bounded the same way:  the
        # walk may spend what their budget leaves, and no row's ancestor registration outlives its
        # hook.
        self.assertEqual(PL.PARENT_DEPTH, 3)
        self.assertEqual(PC.SPEC.hook_budget, 0)
        self.assertEqual(
            PL.walk_allowance(PC.SPEC), PL.SHARED_HOOK_BUDGET - PL.REGISTER_CALLS * PL._API_TIMEOUT
        )
        self.assertLessEqual(
            PL.REGISTER_CALLS * PL._API_TIMEOUT + PL.walk_allowance(PC.SPEC),
            min(PL.HOOK_TIMEOUTS.values()),
        )

    def test_the_parent_walk_registers_the_ancestors_the_budget_allows(self) -> None:
        # The row's budget is ten seconds, the chat's own chain is six of them, and the walk may spend
        # the four that are left:  a parent and a grandparent are registered while that lasts.
        self.add("m-0001", "Grandparent")
        self.add("m-0002", "Parent", parent="m-0001")
        self.add("m-0003", "Child", parent="m-0002")
        self.run_hook(
            PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0003", "source": "startup"}
        )
        self.assertTrue(self.note("MM", "m-0002").get("label"))
        self.assertTrue(self.note("MM", "m-0001").get("label"))
        self.assertTrue(self.note("MM", "m-0003").get("label"))

    def test_the_parent_walk_stops_when_its_budget_is_spent(self) -> None:
        # A slow registry can cost a parent its registration, but not the chat's own letter:  the walk
        # may spend only what the row's budget leaves after the chat's own chain, and it stops when
        # less than one ancestor's chain is left.  Three seconds of allowance fit one generation
        # (the register, the reopen that may follow it, the title update) and no more.
        import dataclasses

        spec = dataclasses.replace(PM.SPEC, hook_budget=12, call_timeout=0, parent_call_timeout=0)
        self.assertEqual(PL.walk_allowance(spec), 3.0)
        self.add("m-0001", "Grandparent")
        self.add("m-0002", "Parent", parent="m-0001")
        self.add("m-0003", "Child", parent="m-0002")
        self.run_hook(
            spec,
            {"hook_event_name": "SessionStart", "session_id": "m-0003", "source": "startup"},
            transport=self.slow_transport(0.6),
        )
        self.assertTrue(self.note("MM", "m-0001").get("label"), "the first ancestor fits in the budget")
        self.assertFalse(
            os.path.exists(os.path.join(self.state, "MM", "letters", "m-0002.json")),
            "the walk stops when its budget is spent",
        )
        self.assertTrue(self.note("MM", "m-0003").get("label"), "the chat's own letter is not the walk's to lose")

    def test_the_ancestors_chain_calls_share_what_is_left_of_the_budget(self) -> None:
        # The chain is the register, the reopen that may follow it, and the title update, so each of
        # its calls gets a share of what is left of the walk's budget:  the chain cannot outlast the
        # allowance, however slow the registry is.
        import dataclasses
        from unittest import mock

        from simple_tools import registry_api

        spec = dataclasses.replace(PM.SPEC, hook_budget=14, call_timeout=0, parent_call_timeout=0)
        self.assertEqual(PL.walk_allowance(spec), 5.0)
        self.add("m-0001", "Grandparent")
        self.add("m-0002", "Parent", parent="m-0001")
        self.add("m-0003", "Child", parent="m-0002")
        seen: list[tuple[str, str, int]] = []
        real = registry_api.call

        def capture(env, op, params, timeout=25):
            seen.append((op, str(params.get("session_id") or ""), timeout))
            return real(env, op, params, timeout=timeout)

        with mock.patch.object(registry_api, "call", capture):
            self.run_hook(
                spec, {"hook_event_name": "SessionStart", "session_id": "m-0003", "source": "startup"}
            )
        self.assertTrue(self.note("MM", "m-0002").get("label"))
        # The chat's own registration is not the walk's, so only the ancestors' calls are read here:
        # the chat's call would satisfy this assertion even if the walk's share were wrong.
        ancestors = [timeout for op, sid, timeout in seen if sid != "m-0003"]
        self.assertTrue(ancestors, "the walk made no call of its own")
        self.assertLessEqual(max(ancestors), 1, "an ancestor's chain call outlasted the allowance")

    def test_an_ancestor_with_no_deadline_of_its_own_gets_the_shared_one(self) -> None:
        # A row that declares no deadline of its own gets the shared one, not a one-second clamp:
        # the sentinel of zero means "not declared".
        import dataclasses
        from unittest import mock

        from simple_tools import registry_api

        spec = dataclasses.replace(PM.SPEC, hook_budget=30, call_timeout=0, parent_call_timeout=0)
        self.assertEqual(PL.walk_allowance(spec), 21.0)
        self.add("m-0001", "Grandparent")
        self.add("m-0002", "Parent", parent="m-0001")
        self.add("m-0003", "Child", parent="m-0002")
        seen: list[tuple[str, str, int]] = []
        real = registry_api.call

        def capture(env, op, params, timeout=25):
            seen.append((op, str(params.get("session_id") or ""), timeout))
            return real(env, op, params, timeout=timeout)

        with mock.patch.object(registry_api, "call", capture):
            self.run_hook(
                spec, {"hook_event_name": "SessionStart", "session_id": "m-0003", "source": "startup"}
            )
        # Only the ancestors' calls are read:  the chat's own registration carries the shared
        # deadline by itself, so an assertion over every call would pass even if the ancestor's
        # call were clamped to a second.
        ancestors = [timeout for op, sid, timeout in seen if sid != "m-0003"]
        self.assertTrue(ancestors, "the walk made no call of its own")
        self.assertIn(PL._API_TIMEOUT, ancestors, "the shared deadline was clamped away")

    def test_a_failing_ancestor_call_is_charged_to_the_walk(self) -> None:
        # A failing call that took its time is spent time:  the walk must not be told its budget is
        # untouched, or the next generation starts with the whole allowance again.
        import dataclasses
        from unittest import mock

        spec = dataclasses.replace(PM.SPEC, hook_budget=13, call_timeout=0, parent_call_timeout=0)
        self.assertEqual(PL.walk_allowance(spec), 4.0)
        self.add("m-0001", "Grandparent")
        self.add("m-0002", "Parent", parent="m-0001")
        self.add("m-0003", "Child", parent="m-0002")
        real = PL._apply

        def failing(chat, answer, note):
            if chat.session_id == "m-0003":
                return real(chat, answer, note)
            time.sleep(1.2)
            raise RuntimeError("the registry call failed")

        with mock.patch.object(PL, "_apply", failing):
            self.run_hook(
                spec, {"hook_event_name": "SessionStart", "session_id": "m-0003", "source": "startup"}
            )
        self.assertFalse(
            os.path.exists(os.path.join(self.state, "MM", "letters", "m-0002.json")),
            "the walk started another generation with a budget it had already spent",
        )
        self.assertTrue(self.note("MM", "m-0003").get("label"), "the chat's own letter is not the walk's to lose")

    def test_an_ancestor_that_is_not_a_chat_does_not_break_the_walk(self) -> None:
        # An ancestor whose own kind is neither a chat nor a subagent ends that branch with a number,
        # so the generation above it is still registered.
        self.add("m-0001", "Grandparent", kind="cron")
        self.add("m-0002", "Parent", parent="m-0001")
        self.add("m-0003", "Child", parent="m-0002")
        self.run_hook(
            PM.SPEC, {"hook_event_name": "SessionStart", "session_id": "m-0003", "source": "startup"}
        )
        self.assertTrue(self.note("MM", "m-0002").get("label"))
        self.assertTrue(self.note("MM", "m-0003").get("label"))

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
        # The start leaves the name to the runtime, so the runtime's own title is the one that shows.
        self.assertEqual(self.title("m-0001"), "")
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


class UnletteredSessionsTests(MiniMaxRow):
    """`minimax_unlettered_sessions`:  the repair pass's candidates are this host's chats that hold no letter
    note, old enough that their own start has finished and young enough to be a live loss, newest first.  A chat
    that started before the hook was installed has no note at all, and the pass that looked for the "??: "
    placeholder could not see it."""

    def write_note(self, sid: str) -> None:
        """A lettered chat, written the way the library writes a note:  owner-only folder, the file
        whole, and a rename in place of a partial write."""
        path = PL.note_path(self.env(), PM.SEAT, sid)
        self.assertTrue(
            PL._save_json(path, {"label": "AA", "generation": 1, "location": "mac"}), path
        )

    def test_names_the_chats_that_hold_no_note(self) -> None:
        self.add("m-0001", "A Chat With No Note")
        self.add("m-0002", "A Lettered Chat")
        self.write_note("m-0002")
        self.add("m-0003", "Brand New", age=60)
        self.add("m-0004", "Ancient", age=365 * 86400)
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), ["m-0001"])

    def test_names_the_newest_first(self) -> None:
        self.add("m-0005", "Older", age=7200)
        self.add("m-0006", "Newer", age=3600)
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), ["m-0006", "m-0005"])

    def test_a_sub_thread_is_not_a_candidate(self) -> None:
        self.add("m-0007", "A Chat", kind="conversation")
        self.add("m-0008", "Its Sub-Thread", kind="task", parent="m-0007")
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), ["m-0007"])

    def test_an_archived_chat_is_not_a_candidate(self) -> None:
        self.add("m-0009", "Put Away", archived=1)
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), [])

    def test_a_cron_run_is_not_a_candidate(self) -> None:
        self.add("m-0010", "A Cron Run", kind="cron")
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), [])

    def test_no_store_is_doubt_and_doubt_answers_none(self) -> None:
        os.unlink(self.store)
        self.assertIsNone(PM.minimax_unlettered_sessions(self.env()))

    def test_a_store_without_the_columns_it_needs_answers_none(self) -> None:
        os.unlink(self.store)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
                " parent_session_id TEXT)"
            )
            conn.execute(
                "INSERT INTO local_runtime_sessions VALUES (?, ?, ?, ?)",
                ("m-0011", "Old Store", "conversation", None),
            )
        conn.close()
        self.assertIsNone(PM.minimax_unlettered_sessions(self.env()))

    def test_a_malformed_session_id_is_skipped_and_not_silent(self) -> None:
        """One corrupt row neither stops the pass nor passes unnoticed:  it is skipped and said
        out loud, so the inventory that was taken is never read as a complete one."""
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id,"
                " archived, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                (None, "A Broken Row", "conversation", None, 0, int((time.time() - 3600) * 1000)),
            )
        conn.close()
        self.add("m-4100", "The One That Needs A Letter", age=7200)
        import sys
        from unittest import mock

        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            found = PM.minimax_unlettered_sessions(self.env())
        self.assertEqual(found, ["m-4100"])
        self.assertIn("malformed", stderr.getvalue())

    def test_a_row_with_no_timestamp_is_of_unknown_age_and_is_a_candidate(self) -> None:
        """The chat that predates the hook is the one with no timestamp, so the age window must
        not drop it."""
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id,"
                " archived, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                ("m-0020", "Untimed", "conversation", None, 0, None),
            )
        conn.close()
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), ["m-0020"])

    def test_the_scan_limit_counts_candidates_and_not_rows(self) -> None:
        """The chats that are already lettered are the newest ones, so a scan that stops at them
        never reaches the chat that needs a letter."""
        for n in range(25):
            sid = "m-1%03d" % n
            self.add(sid, "Lettered", age=3600 + n)
            self.write_note(sid)
        self.add("m-2000", "The One That Needs A Letter", age=7200)
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), ["m-2000"])

    def test_the_rows_of_unknown_age_come_first(self) -> None:
        """A row with no timestamp is the chat that predates the hook, and it is the one a repair
        pass can least afford to miss:  it is read before the dated rows, so the scan's limit of
        candidates cannot be spent on the dated ones first."""
        for n in range(25):
            sid = "m-2%03d" % n
            self.add(sid, "Dated", age=3600 + n)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id,"
                " archived, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                ("m-untimed", "From Before The Hook", "conversation", None, 0, None),
            )
        conn.close()
        found = PM.minimax_unlettered_sessions(self.env())
        self.assertEqual(found[0], "m-untimed")
        self.assertIn("m-untimed", found)
        # The scan still stops at its own limit of candidates.
        self.assertLessEqual(len(found), PM.UNLETTERED_SCAN)

    def test_the_scan_reads_bounded_batches(self) -> None:
        """The scan asks the store for one bounded batch at a time, so a busy host is not read
        whole, and it stops as soon as it holds the candidates it needs."""
        for n in range(25):
            sid = "m-3%03d" % n
            self.add(sid, "Lettered", age=3600 + n)
            self.write_note(sid)
        self.add("m-3999", "The One That Needs A Letter", age=7200)
        from unittest import mock

        real_connect = sqlite3.connect
        seen: list[str] = []

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
            found = PM.minimax_unlettered_sessions(self.env())
        self.assertEqual(found, ["m-3999"])
        self.assertTrue(seen, "the scan asked the store for nothing")
        for sql in seen:
            self.assertIn("LIMIT", sql, sql)

    def test_a_malformed_session_id_is_skipped_and_said(self) -> None:
        """One corrupt row does not silence the pass:  the chat that needs a letter is still
        found, and the corrupt row is said out loud so it never reads as a chat that was
        considered and left alone."""
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id,"
                " archived, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                ("", "Corrupt", "conversation", None, 0, int((time.time() - 3600) * 1000)),
            )
        conn.close()
        self.add("m-4000", "The One That Needs A Letter", age=7200)
        import sys
        from unittest import mock

        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            found = PM.minimax_unlettered_sessions(self.env())
        self.assertEqual(found, ["m-4000"])
        self.assertIn("malformed", stderr.getvalue())

    def test_a_store_path_with_punctuation_is_still_read(self) -> None:
        """The store is opened by a URI that percent-encodes the path, so a folder whose name
        holds a question mark or a hash is read and not skipped."""
        home = os.path.join(self.tmp.name, "home?with#punctuation")
        if os.path.islink(home):
            raise RuntimeError("refusing a symlinked directory: %s" % home)
        os.makedirs(home, mode=0o700)
        store = os.path.join(home, ".minimax", "v2", "sqlite", "runtime-state.sqlite")
        os.makedirs(os.path.dirname(store))
        conn = sqlite3.connect(store)
        with conn:
            conn.execute(
                "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT,"
                " session_kind TEXT, parent_session_id TEXT, archived INTEGER DEFAULT 0,"
                " created_at_ms INTEGER)"
            )
            conn.execute(
                "INSERT INTO local_runtime_sessions (session_id, title, session_kind, parent_session_id,"
                " archived, created_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                ("m-0030", "Punctuated", "conversation", None, 0, int((time.time() - 3600) * 1000)),
            )
        conn.close()
        env = {"HOME": home, "SIMPLE_TOOLS_STATE_DIR": self.state}
        self.assertEqual(PM.minimax_unlettered_sessions(env), ["m-0030"])

    def test_the_repair_pass_letters_a_chat_that_started_before_the_hook(self) -> None:
        """The chat that predates the hook has no note and no placeholder, and the pass gives it a letter and
        puts it on its title:  the repair the issue asks for, proved."""
        self.add("m-0012", "A Chat From Before The Hook")
        out = PL.repair_unlettered(PM.SPEC, self.env(), PM.SEAT, "mac", now=time.time())
        self.assertEqual(out["asked"], ["m-0012"])
        self.assertEqual(out["repaired"], ["m-0012"])
        note = self.note(PM.SEAT, "m-0012")
        self.assertEqual(len(note["label"]), 2, note)
        self.assertTrue(self.title("m-0012").startswith(note["label"] + ": "), self.title("m-0012"))
        # And a second pass finds nothing:  the chat is lettered now.
        self.assertEqual(PM.minimax_unlettered_sessions(self.env()), [])


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
        self.assertEqual((code, sorted(answer["changed"])),
                         (0, ["PreToolUse", "SessionStart", "Stop", "SubagentStart", "SubagentStop", "UserPromptSubmit"]))
        with open(self.hooks_file(), encoding="utf-8") as fh:
            hooks = json.load(fh)["hooks"]
        self.assertNotIn("mm-session-letter", json.dumps(hooks))
        self.assertEqual(hooks["PreToolUse"], [{"matcher": "Write", "hooks": [{"type": "command",
                                                                              "command": "another-tool guard"}]}])
        for event in ("SessionStart", "UserPromptSubmit", "SubagentStart", "SubagentStop", "Stop"):
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
        # The cache is in several places (the old layout, the project scopes, the user scope).  The file written last is
        # the one adopted, whichever place it is in, and never more than one.
        os.utime(path, (1000, 1000))
        older = self.cache("m-0001", {"letter": "QR", "host": "mac"}, "v2", "hook-data", "projects", "abc123")
        os.utime(older, (2000, 2000))
        newest_user = self.cache("m-0001", {"letter": "QS", "host": "mac"}, "v2", "hook-data", "user")
        os.utime(newest_user, (1500, 1500))
        self.assertEqual(PM.minimax_retired_note(env, "m-0001")["label"], "QR")  # 2000 beats 1500 and 1000
        os.utime(newest_user, (3000, 3000))
        self.assertEqual(PM.minimax_retired_note(env, "m-0001")["label"], "QS")
        os.utime(path, (4000, 4000))  # the old layout's file, written last, is the one that holds the letter now
        note = PM.minimax_retired_note(env, "m-0001")
        self.assertEqual((note["label"], note["seen"]), ("QQ", 4000.0))
        for stamped in (path, older, newest_user):  # the same time:  the retired client's own order breaks the tie
            os.utime(stamped, (5000, 5000))
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

    def test_every_field_of_the_retired_cache_is_typed_and_a_bad_one_is_ignored(self) -> None:
        env = self.env()
        good = {"letter": "QQ", "topic": "Memory sync", "host": "mac", "space": "double"}
        for n, (body, want) in enumerate((
            # a field that is not a string, or too long, is ignored (as if absent);  the note still stands
            (dict(good, host=7), {"label": "QQ", "location": None, "title": "Memory sync"}),
            (dict(good, host=["mac"]), {"label": "QQ", "location": None, "title": "Memory sync"}),
            (dict(good, host="m" * 17), {"label": "QQ", "location": None, "title": "Memory sync"}),
            (dict(good, topic={"a": 1}), {"label": "QQ", "location": "mac", "title": None}),
            (dict(good, topic="t" * 501), {"label": "QQ", "location": "mac", "title": None}),
            (dict(good, topic=None, space=3), {"label": "QQ", "location": "mac", "title": None}),
            (dict(good, topic=""), {"label": "QQ", "location": "mac", "title": None}),
            # a letter that is missing or not a letter rejects the whole note
            (dict(good, letter=None), None), (dict(good, letter=["Q"]), None), (dict(good, letter="qq"), None),
            (dict(good, letter="QQQ"), None), (dict(good, letter="Q" * 5000), None), (dict(good, letter=1.5), None),
            ({"topic": "only a topic"}, None), ([good], None), ("QQ", None), (7, None), (None, None),
        )):
            sid = "m-03%02d" % n
            self.cache(sid, body)
            got = PM.minimax_retired_note(env, sid)  # never raises
            if want is None:
                self.assertIsNone(got, body)
            else:
                self.assertEqual({k: got[k] for k in want}, want, body)
                self.assertIsInstance(got["seen"], float)
        # What the library adopts of such a note is typed too:  a bad location is the chat's own, a bad title is none.
        self.cache("m-0400", dict(good, host=7, topic=["x"]))
        note = PL.adopted_note(PM.SPEC, env, "m-0400", "mac")
        self.assertEqual((note["label"], note["location"], note["title"]), ("QQ", "mac", ""))

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

    def test_thread_link_capability_scans_before_registry_configuration_or_transport(self) -> None:
        import secrets
        from unittest import mock
        from simple_tools import registry_api as RA

        private = "synthetic-" + secrets.token_hex(20)
        env = self.env(SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=private)
        original = dict(env)
        record = PL._thread_link_recorder(PL.Chat(self.spec, env, "server", "scan-1", now=1.0))
        unsafe = "sk-" + secrets.token_urlsafe(32)  # Fresh synthetic shape, never a credential.
        for case, value in (("pattern", unsafe), ("parent binding", private)):
            link = "https://example.test/thread?key=" + value
            with self.subTest(case=case), mock.patch.object(RA, "base_url") as configured, mock.patch.object(RA, "call") as call:
                answer = record(link)
                self.assertEqual(answer, {"code": "thread_link_secret_detected", "error": "thread_link_secret_detected"})
                self.assertNotIn(link, json.dumps(answer))
                self.assertNotIn(private, json.dumps(answer))
                configured.assert_not_called()
                call.assert_not_called()
        self.assertEqual(env, original)

    def test_thread_link_capability_uses_parent_configuration_and_bound_location(self) -> None:
        from unittest import mock
        from simple_tools import registry_api as RA

        env = self.env(SESSION_LETTER_LOCATION="mac")
        chat = PL.Chat(self.spec, env, "server", "bound-1", now=1.0)
        record = PL._thread_link_recorder(chat)
        with mock.patch.object(RA, "call", return_value={"ok": True}) as call:
            self.assertEqual(record("zcode://session/bound-1"), {"ok": True})
        self.assertIs(call.call_args.args[0], env)
        self.assertEqual(call.call_args.args[2]["location"], "server")
        self.assertNotIn(RA.KEY_VAR, PL.child_process_env(env))

    def test_optional_thread_link_callback_without_parent_url_is_quiet(self) -> None:
        import contextlib
        from unittest import mock
        from simple_tools import platform_letter_zcode as PZ, registry_api as RA

        env = self.env()
        env.pop(RA.URL_ENV)
        original = dict(env)
        record = PL._thread_link_recorder(PL.Chat(self.spec, env, "server", "optional-1", now=1.0))
        error = io.StringIO()
        with mock.patch.object(RA, "call") as call, contextlib.redirect_stderr(error):
            PZ._record_thread_link(PL.child_process_env(env), "optional-1", record=record)
        call.assert_not_called()
        self.assertEqual(error.getvalue(), "")
        self.assertEqual(env, original)

    def test_advice_receives_only_the_child_environment_and_preserves_parent_context(self) -> None:
        seen = []

        def advise(request):
            seen.append(request)
            return "Keep the assigned title."

        spec = make_spec(title_advice=advise)
        private = {
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN": "fake.bootstrap",
            "FLEET_KNOBS_INFISICAL_TOKEN": "fake.knobs",
            "FLEET_KNOBS_WRITE_TOKEN": "fake.writer",
            "INFISICAL_TOKEN": "fake.project",
            "INFISICAL_AI_FLEET_COORDINATOR_TOKEN": "fake.afc",
            "FLEET_RECALL_INFISICAL_TOKEN": "fake.recall",
        }
        env = self.env(AGENT_LAUNCHER="platform-launcher", **private)
        original = dict(env)
        chat = PL.Chat(spec, env, "server", "advice-1", now=1.0)
        note = {"state": "open", "label": "AA", "title": "Words"}
        self.assertEqual(PL.advice(chat, note, "Words", {}, "start"), "Keep the assigned title.")
        request = seen[-1]
        self.assertEqual(request.env["AGENT_LAUNCHER"], "platform-launcher")
        for name in (*private, "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"):
            self.assertNotIn(name, request.env)
        self.assertTrue(callable(request.record_thread_link))
        self.assertEqual(env, original)
        self.assertNotIn("fake.bootstrap", repr(request))
        from dataclasses import asdict
        serialized = json.dumps(asdict(request), default=str)
        for value in private.values():
            self.assertNotIn(value, serialized)

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
        title_envs = []

        def writer(request):
            seen.append(dict(request.data))
            title_envs.append(dict(request.env))
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
        env = self.env(SESSION_LETTER_LOCATION="server", AGENT_LAUNCHER="platform-launcher",
                       SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN="fake.bootstrap",
                       FLEET_KNOBS_INFISICAL_TOKEN="fake.old", FLEET_KNOBS_WRITE_TOKEN="fake.writer")
        original = dict(env)
        code = PL.request_main(["--session-id", "f-0001"], env, out,
                               platform="linux")
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(seen[-1], {"session_id": "f-0001", "source": "startup"})
        self.assertEqual(title_envs[-1]["AGENT_LAUNCHER"], "platform-launcher")
        for name in ("SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN", "FLEET_KNOBS_INFISICAL_TOKEN",
                     "FLEET_KNOBS_WRITE_TOKEN", "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"):
            self.assertNotIn(name, title_envs[-1])
        self.assertEqual(env, original)  # The registry successfully used the parent's own signing key.

    def test_simple_chat_letter_is_the_same_command(self) -> None:
        out = io.StringIO()
        self.assertEqual(PL.main(["request", "--session-id", "n-0001"], env=self.env(), stdout=out), 1)
        self.assertTrue(out.getvalue().startswith("no letter: "))


if __name__ == "__main__":
    unittest.main()


class MiniMaxTitleSource(Base):
    """The real runtime's store shape:  it carries the extra data column the title source lives in."""

    DDL = ("CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
           " parent_session_id TEXT, extra_data_json TEXT)")

    def setUp(self) -> None:
        super().setUp()
        self.store = os.path.join(self.home, ".minimax", "v2", "sqlite", "runtime-state.sqlite")
        os.makedirs(os.path.dirname(self.store))
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(self.DDL)
        conn.close()

    def add(self, sid: str, title: str = "", extra: object = None) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("INSERT INTO local_runtime_sessions VALUES (?, ?, ?, ?, ?)",
                         (sid, title, "conversation", None, json.dumps(extra) if extra is not None else None))
        conn.close()

    def row(self, sid: str):
        conn = sqlite3.connect(self.store)
        row = conn.execute("SELECT title, extra_data_json FROM local_runtime_sessions WHERE session_id = ?",
                            (sid,)).fetchone()
        conn.close()
        return row

    def request(self, sid: str, title: str, shown: str = "", reason: str = "start") -> PL.TitleRequest:
        # The words are what the library puts in a request:  the title with the letter taken off.
        words = title.split(": ", 1)[1] if ": " in title else ""
        return PL.TitleRequest(platform="minimax", session_id=sid, label="YY", title=title, words=words,
                                current="", reason=reason, data={}, env=self.home_env(), shown=shown, held=())

    def home_env(self) -> dict:
        return {"HOME": self.home}

    def test_the_rename_marks_the_title_as_the_hook_own(self) -> None:
        # The runtime only auto-titles a chat whose titleSource is still "default", and it put its own title over
        # the lettered one:  the hook's write carries the source, so the runtime leaves the lettered title alone.
        # The write also remembers the title the hook put there, so the hook's own title is told apart from a name
        # a person gave even when the note lost it.
        self.add("m-0100")
        self.assertTrue(PM.minimax_rename(self.request("m-0100", "YY: Work")))
        title, extra = self.row("m-0100")
        self.assertEqual(title, "YY: Work")
        self.assertEqual(json.loads(extra)["titleSource"], "manual")
        self.assertEqual(json.loads(extra)["hookTitle"], "YY: Work")

    def test_a_title_a_person_chose_is_left_alone(self) -> None:
        # The runtime marks a name a person set as "manual" too, so a manual title that is not the hook's own is
        # the person's words, and the hook never overwrites them.  The hook also says it did not write it:  a
        # writer that returns True has put the title on the chat, and this one did not.
        self.add("m-0101", "YY: My Own Name", {"titleSource": "manual", "effectiveModel": "m-1"})
        self.assertFalse(PM.minimax_rename(self.request("m-0101", "YY: Coordinate Work", shown="YY: Coordinate Work")))
        title, extra = self.row("m-0101")
        self.assertEqual(title, "YY: My Own Name")
        self.assertEqual(json.loads(extra)["effectiveModel"], "m-1")

    def test_a_placeholder_title_is_never_written(self) -> None:
        # A chat with no descriptive name yet keeps the name MiniMax Code gives it.  The hook does not put
        # "New Chat" on the thread, because the runtime then puts its own name over it and the letter goes with
        # it:  the letter goes on at the chat's first prompt instead.
        self.add("m-0102")
        self.assertFalse(PM.minimax_rename(self.request("m-0102", "YY: New Chat")))
        title, extra = self.row("m-0102")
        self.assertEqual(title, "")

    def test_a_title_the_hook_cannot_claim_is_left_alone(self) -> None:
        # A person named the chat and the runtime marked it as a person's:  the hook has no record of a title of
        # its own on that row, so it does not write over the name, and it says it did not write it.
        self.add("m-0105", "YY: P-BF-6 Work", {"titleSource": "manual"})
        self.assertFalse(PM.minimax_rename(self.request("m-0105", "YY: Coordinate Work")))
        title, extra = self.row("m-0105")
        self.assertEqual(title, "YY: P-BF-6 Work")

    def test_the_hook_writes_its_own_title_again_when_the_note_lost_it(self) -> None:
        # The note that holds the hook's own last title can be lost, and the hook's own title is remembered in
        # the row itself:  a row the hook wrote is written again, and is not taken for a name a person gave.
        self.add("m-0103", "YY: Work", {"titleSource": "manual", "hookTitle": "YY: Work"})
        self.assertTrue(PM.minimax_rename(self.request("m-0103", "YY: Work")))
        title, extra = self.row("m-0103")
        self.assertEqual(title, "YY: Work")
        self.assertEqual(json.loads(extra)["hookTitle"], "YY: Work")

    def test_the_letter_goes_on_the_name_the_runtime_gave_the_chat(self) -> None:
        # MiniMax Code names a chat itself:  the letter goes on that name, under the letter.
        self.add("m-0104", "Coordinate P-BF-6")
        self.assertTrue(PM.minimax_rename(self.request("m-0104", "YY: Coordinate P-BF-6", reason="prompt")))
        title, extra = self.row("m-0104")
        self.assertEqual(title, "YY: Coordinate P-BF-6")
        self.assertEqual(json.loads(extra)["titleSource"], "manual")
