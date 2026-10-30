"""Antigravity on `platform-letter` (AFC-564): the hook's payload reading, the one-shot test, the stored title, the
injected-step answer, the quick answer the entry script gives a later model call, and the hooks.json installer (that it
writes only a hook that can run, and that its check says when one cannot).  The registry side runs against a temporary
registry in the double (pair) letter space, which main's registry has; the hook runs in process on the Mac's rules
(SESSION_LETTER_LOCATION), with the process lookup replaced.  No test depends on which pair the registry hands out first
(main takes the lowest free pair, the pair registry takes the main-chat order), only on the letter being a pair that the
Operator, the note and the injected text agree on."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL
from agent_sync import platform_letter_antigravity as PA

REAL_AGY_ARGUMENTS = PA.agy_arguments  # the tests below replace it on the module; this keeps the real one
SCRIPTS = Path(__file__).resolve().parents[2]
ENTRY = SCRIPTS / "platform-letter"
PAIR = r"^[A-Z]{2}$"
CID = "4aade78e-6a38-40ed-9fc2-11a6eeac6189"
PARENT = "cd6b94bc-8497-4f06-974b-9e247440c923"
CLI_ARGS = ["/Users/x/.local/bin/agy", "--settings", "/tmp/s.json", "--conversation", CID]
PRINT_ARGS = ["/Users/x/.local/bin/agy", "-p", "probe", "two", "--output-format", "json"]

# hooks.json as agy 1.4.10 and the owner's tools leave it (the two groups the Mac has today): flat handlers for the
# invocation events, a matcher group for a tool event, and `enabled` last.
OWNERS_HOOKS = {
    "agent-hook-otlp": {
        "PostInvocation": [
            {"command": "'/opt/homebrew/bin/node' '/x/agent-hook-otlp.mjs' antigravity PostInvocation", "timeout": 3, "type": "command"}
        ],
        "Stop": [{"command": "'/opt/homebrew/bin/node' '/x/agent-hook-otlp.mjs' antigravity Stop", "timeout": 3, "type": "command"}],
        "enabled": True,
    },
    "lane-guard": {
        "PreToolUse": [
            {"matcher": "run_command", "command": "/x/lane-guard-hook --format antigravity", "timeout": 5, "type": "command"}
        ],
        "enabled": True,
    },
}


def start_payload(cid: str = CID, **extra) -> dict:
    """What agy sends a SessionStart hook (captured on agy 1.4.10): the common fields only."""
    base = {
        "artifactDirectoryPath": "/Users/x/.gemini/antigravity-cli/brain/%s" % cid,
        "conversationId": cid,
        "lastUserInput": "fix the failing login test for AFC-564",
        "modelName": "gemini-4-argon-medium",
        "transcriptPath": "/Users/x/.gemini/antigravity-cli/brain/%s/.system_generated/logs/transcript_full.jsonl" % cid,
        "workspacePaths": ["/Users/x/Code"],
    }
    base.update(extra)
    return base


def pre_payload(cid: str = CID, number: int = 0, **extra) -> dict:
    """What agy sends a PreInvocation hook: the common fields plus the invocation counters."""
    return start_payload(cid, invocationNum=number, initialNumSteps=5, **extra)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.home)
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()  # the Operator's registry exists, as it does on the Mac
        self.calls: list[list[str]] = []
        patcher = mock.patch.object(PA, "agy_arguments", side_effect=lambda *a, **k: list(self.agy))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agy = CLI_ARGS

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "AGENT_SYNC_STATE_DIR": self.state, "SESSION_LETTER_LOCATION": "mac"}
        env.update(extra)
        return env

    def transport(self, argv: list[str]) -> dict:
        """The Mac's registry side, in process, remembering what it was asked."""
        self.calls.append(list(argv))
        out = io.StringIO()
        PL.registry_main(list(argv), env=self.env(), stdout=out)
        return json.loads(out.getvalue())

    def ops(self) -> list[str]:
        return [argv[0] for argv in self.calls]

    def hook(self, payload, *, env: dict | None = None, transport=None, raw: str | None = None) -> dict:
        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(raw if raw is not None else json.dumps(payload))
        code = PA.hook_main(
            env if env is not None else self.env(), stdin, out, transport=transport or self.transport, stderr=err
        )
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, out.getvalue())  # agy reads one answer from every hook
        answer = json.loads(lines[0])
        self.assertIsInstance(answer, dict)
        return answer

    def said(self, answer: dict) -> str:
        """The text of the one injected step in an answer."""
        steps = answer["injectSteps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(list(steps[0]), ["systemMessage"])
        self.assertEqual(list(steps[0]["systemMessage"]), ["systemMessage"])  # no metadata: user_markdown fails in agy
        return steps[0]["systemMessage"]["systemMessage"]

    def note(self, cid: str = CID) -> dict:
        with open(os.path.join(self.state, "AG", "letters", cid + ".json")) as fh:
            return json.load(fh)

    def lookup(self, cid: str = CID) -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("antigravity", "mac", PL.SCOPE, cid)
        finally:
            reg.close_db()

    def summaries(self, titles: dict[str, str], product: str = "antigravity-cli") -> str:
        """A product root with a summaries database holding these titles; returns the root."""
        root = os.path.join(self.tmp.name, ".gemini", product)
        os.makedirs(os.path.join(root, "brain"), exist_ok=True)
        conn = sqlite3.connect(os.path.join(root, "conversation_summaries.db"))
        with conn:
            conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT)")
            for cid, title in titles.items():
                conn.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", (cid, title, title))
        conn.close()
        return root

    def set_title(self, root: str, cid: str, title: str) -> None:
        conn = sqlite3.connect(os.path.join(root, "conversation_summaries.db"))
        with conn:
            conn.execute("UPDATE conversation_summaries SET title = ?, preview = ? WHERE conversation_id = ?", (title, title, cid))
        conn.close()

    def payloads(self, root: str, cid: str = CID) -> tuple[dict, dict]:
        art = os.path.join(root, "brain", cid)
        return start_payload(cid, artifactDirectoryPath=art), pre_payload(cid, artifactDirectoryPath=art)


class NormalizeTests(unittest.TestCase):
    def test_a_payload_with_only_the_common_fields_is_a_session_start(self) -> None:
        data = PA.normalize(start_payload())
        self.assertEqual(data["hook_event_name"], "SessionStart")
        self.assertEqual((data["session_id"], data["prompt"]), (CID, "fix the failing login test for AFC-564"))
        self.assertEqual(data["workspacePaths"], ["/Users/x/Code"])  # everything the platform sent is kept

    def test_the_first_model_call_of_a_prompt_is_the_prompt_event(self) -> None:
        self.assertEqual(PA.normalize(pre_payload())["hook_event_name"], "PreInvocation")
        # An encoder that leaves a zero out still sends the step count.
        data = start_payload(initialNumSteps=5)
        self.assertEqual(PA.normalize(data)["hook_event_name"], "PreInvocation")

    def test_a_later_model_call_of_the_same_prompt_is_nothing(self) -> None:
        for number in (1, 2, 37):
            self.assertIsNone(PA.normalize(pre_payload(number=number)))
        for number in (True, "0", -1):
            self.assertIsNone(PA.normalize(pre_payload(number=number)), number)

    def test_events_this_adapter_does_not_hook_are_nothing(self) -> None:
        stop = start_payload(executionNum=0, fullyIdle=True, terminationReason="NO_TOOL_CALL")
        post = pre_payload(modelOutput="done")
        tool = start_payload(toolCall={"name": "run_command", "args": {}}, stepIdx=3)
        for data in (stop, post, tool):
            self.assertIsNone(PA.normalize(data))

    def test_a_payload_with_no_conversation_is_nothing(self) -> None:
        for data in ({}, {"conversationId": ""}, {"conversationId": 5}, {"lastUserInput": "hello"}):
            self.assertIsNone(PA.normalize(data))


class KindTests(Base):
    def test_one_shot_runs_are_found_by_their_flags(self) -> None:
        shot = [
            ["agy", "-p", "hello"],
            ["agy", "--print", "hello"],
            ["agy", "--prompt", "hello", "--output-format", "json"],
            ["agy", "--model", "x", "-p=hello"],
            ["agy", "-p", "fix", "the", "--input-format", "x"],
        ]
        chats = [
            ["agy"],
            ["agy", "--conversation", CID],
            ["agy", "-c"],
            ["agy", "-i", "fix", "-p", "handling"],  # the words after -i are its prompt
            ["agy", "--prompt-interactive", "go"],
            ["agy", "-p", "--input-format", "stream-json", "--output-format", "stream-json"],
            ["agy", "-p", "--input-format=stream-json"],
            [],
        ]
        for words in shot:
            self.assertTrue(PA.one_shot(words), words)
        for words in chats:
            self.assertFalse(PA.one_shot(words), words)

    def test_the_agy_process_is_found_above_the_shell(self) -> None:
        rows = {
            900: (800, ["sh", "-c", "platform-letter hook"]),
            800: (700, ["/Users/x/.local/bin/agy", "-p", "go"]),
            700: (1, ["-zsh"]),
        }
        with mock.patch.object(PA, "process_row", side_effect=lambda pid: rows.get(pid, (0, []))):
            self.assertEqual(REAL_AGY_ARGUMENTS(900), ["/Users/x/.local/bin/agy", "-p", "go"])
            self.assertEqual(REAL_AGY_ARGUMENTS(700), [])  # a shell with no agy above it
            self.assertEqual(REAL_AGY_ARGUMENTS(900, hops=1), [])  # not far enough
            self.assertEqual(REAL_AGY_ARGUMENTS(5), [])  # ps could not say

    def test_ps_output_is_read_as_a_parent_and_words(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="  412 /Users/x/.local/bin/agy -p hello world\n")
        with mock.patch.object(PA.subprocess, "run", return_value=done):
            self.assertEqual(PA.process_row(5), (412, ["/Users/x/.local/bin/agy", "-p", "hello", "world"]))
        for bad in (subprocess.CompletedProcess([], 1, stdout=""), subprocess.CompletedProcess([], 0, stdout="x y\n")):
            with mock.patch.object(PA.subprocess, "run", return_value=bad):
                self.assertEqual(PA.process_row(5), (0, []))
        with mock.patch.object(PA.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(PA.process_row(5), (0, []))

    def test_kind(self) -> None:
        self.agy = CLI_ARGS
        self.assertEqual(PA.antigravity_kind(start_payload(), {}), "chat")
        self.assertEqual(PA.antigravity_kind(start_payload(parentConversationId=PARENT), {}), "subagent")
        self.agy = PRINT_ARGS
        self.assertEqual(PA.antigravity_kind(start_payload(), {}), "headless")
        self.assertEqual(PA.antigravity_kind(start_payload(parentConversationId=PARENT), {}), "headless")
        self.agy = []  # the app and the IDE run hooks from a language server
        self.assertEqual(PA.antigravity_kind(start_payload(), {}), "chat")


class StoredTitleTests(Base):
    def test_the_title_is_read_from_the_products_summaries_database(self) -> None:
        root = self.summaries({CID: "Audio Check  Probe", "other": "Not Mine"})
        self.assertEqual(PA.stored_title(self.payloads(root)[0]), "Audio Check Probe")
        self.assertEqual(PA.stored_title(self.payloads(root, "unknown-id")[0]), "")

    def test_the_database_is_only_read(self) -> None:
        root = self.summaries({CID: "Probe"})
        db = os.path.join(root, "conversation_summaries.db")
        before = (os.path.getmtime(db), Path(db).read_bytes())
        PA.stored_title(self.payloads(root)[0])
        self.assertEqual((os.path.getmtime(db), Path(db).read_bytes()), before)

    def test_a_product_with_no_database_or_a_broken_one_has_no_title(self) -> None:
        ide = os.path.join(self.tmp.name, ".gemini", "antigravity-ide")
        os.makedirs(os.path.join(ide, "brain"))
        self.assertEqual(PA.stored_title(start_payload(artifactDirectoryPath=os.path.join(ide, "brain", CID))), "")
        root = self.summaries({CID: "Probe"})
        Path(root, "conversation_summaries.db").write_bytes(b"not a database")
        self.assertEqual(PA.stored_title(self.payloads(root)[0]), "")
        for art in ("", "relative", "/x/notbrain/%s" % CID, "/x/other/brain/%s" % CID):
            self.assertEqual(PA.stored_title(start_payload(artifactDirectoryPath=art)), "", art)


class HookTests(Base):
    def test_a_new_conversation_gets_a_letter_and_is_told_it(self) -> None:
        answer = self.hook(start_payload())
        label = self.note()["label"]
        self.assertRegex(label, PAIR)
        text = self.said(answer)
        self.assertIn("Operator session letter: Antigravity %s, on the Mac." % label, text)
        self.assertIn('"@**Antigravity** %s"' % label, text)
        self.assertIn("no way to set a chat's title", text)
        self.assertIn('should start with "%s: "' % label, text)
        self.assertNotIn("title starts with", text)  # the library's line is true only where a title can be set
        record = self.lookup()
        self.assertEqual((record["label"], record["seat"]), (label, "AG"))
        self.assertEqual(self.note()["state"], "open")
        self.assertEqual(os.stat(os.path.join(self.state, "AG", "letters", CID + ".json")).st_mode & 0o777, 0o600)

    def test_the_first_call_of_the_first_prompt_gives_the_operator_the_words(self) -> None:
        self.hook(start_payload())
        self.calls.clear()
        self.assertEqual(self.hook(pre_payload()), {})  # already told at start
        self.assertEqual(self.lookup()["title"], "Fix the Failing Login Test for AFC-564")
        self.assertEqual(self.ops(), ["title"])
        self.assertEqual(self.note()["titled"], True)

    def test_a_later_prompt_and_a_later_model_call_cost_no_hop(self) -> None:
        self.hook(start_payload())
        self.hook(pre_payload())
        self.calls.clear()
        self.assertEqual(self.hook(pre_payload(lastUserInput="and now the docs")), {})
        self.assertEqual(self.hook(pre_payload(number=3)), {})
        self.assertEqual(self.calls, [])

    def test_the_title_agy_generated_is_what_the_operator_keeps(self) -> None:
        root = self.summaries({CID: "Audio Check Probe"})
        start, pre = self.payloads(root)
        self.hook(start)
        self.hook(pre)
        self.assertEqual(self.lookup()["title"], "Audio Check Probe")
        # The title agy makes changes as the conversation grows; each new one goes to the Operator once.
        self.set_title(root, CID, "Login Test Repair")
        self.calls.clear()
        self.assertEqual(self.hook(pre), {})
        self.assertEqual((self.ops(), self.lookup()["title"]), (["title"], "Login Test Repair"))
        self.calls.clear()
        self.hook(pre)
        self.assertEqual(self.calls, [])

    def test_a_person_who_renames_the_chat_with_its_letter_keeps_their_words(self) -> None:
        root = self.summaries({CID: "Audio Check Probe"})
        start, pre = self.payloads(root)
        self.hook(start)
        label = self.note()["label"]
        self.set_title(root, CID, "%s: Own Words" % label)
        self.assertEqual(self.hook(pre), {})
        self.assertEqual(self.lookup()["title"], "Own Words")  # the letter comes off what the Operator keeps

    def test_a_resumed_conversation_is_registered_on_its_first_prompt(self) -> None:
        # A conversation from before the install, resumed: agy sends no SessionStart.
        answer = self.hook(pre_payload())
        label = self.note()["label"]
        self.assertIn("Antigravity %s" % label, self.said(answer))
        self.assertRegex(label, PAIR)
        self.assertEqual(self.hook(pre_payload()), {})  # told once

    def test_a_one_shot_run_takes_no_letter(self) -> None:
        self.agy = PRINT_ARGS
        for data in (start_payload(), pre_payload()):
            self.assertEqual(self.hook(data), {})
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "AG")))

    def test_a_streaming_bridge_is_a_chat(self) -> None:
        self.agy = ["/x/agy", "-p", "--input-format", "stream-json", "--output-format", "stream-json"]
        self.assertIn("injectSteps", self.hook(start_payload()))

    def test_a_sub_agent_conversation_is_registered_after_its_parent(self) -> None:
        self.hook(start_payload(PARENT))
        self.calls.clear()
        answer = self.hook(start_payload(CID, parentConversationId=PARENT, agentName="self"))
        self.assertIn("injectSteps", answer)
        register = self.calls[0]
        self.assertEqual(register[0], "register")
        self.assertIn("--parent-session-id=%s" % PARENT, register)
        self.assertRegex(self.note()["label"], PAIR)
        self.assertEqual(self.note()["parent"], PARENT)
        # Under main's registry the child is picked like a main chat; under the pair registry it follows the parent.
        if PL._accepts(OR.register_session, "parent_session_id"):
            parent = self.note(PARENT)["label"]
            self.assertEqual(self.note()["label"], parent[0] + chr(ord(parent[1]) + 1))

    def test_a_registry_that_does_not_answer_is_a_line_of_context(self) -> None:
        def down(argv):
            return {"error": "the registry host did not answer (TimeoutExpired)"}

        text = self.said(self.hook(start_payload(), transport=down))
        self.assertIn("No Operator letter yet (the registry host did not answer (TimeoutExpired))", text)
        self.assertEqual(self.note()["state"], "failed")
        self.assertEqual(self.hook(pre_payload(), transport=down), {})  # held back for ten minutes
        # Once the Operator answers, the prompt registers the chat and tells it the letter.
        with mock.patch.object(PL.time, "time", return_value=self.note()["failed_at"] + PL.RETRY_SECONDS + 1):
            answer = self.hook(pre_payload())
        self.assertRegex(self.note()["label"], PAIR)
        self.assertIn("Operator session letter: Antigravity", self.said(answer))

    def test_whatever_goes_wrong_the_answer_is_one_empty_object(self) -> None:
        for raw in ("", "not json", "[]", "null", "{}", json.dumps({"conversationId": CID, "invocationNum": 4})):
            self.assertEqual(self.hook(None, raw=raw), {}, raw)

        def boom(argv):
            raise RuntimeError("anything at all")

        text = self.said(self.hook(start_payload("9" * 36), transport=boom))
        self.assertIn("No Operator letter yet (the registry call failed (RuntimeError))", text)
        with mock.patch.object(PL, "hook", side_effect=KeyError("a bug in the library")):
            err = io.StringIO()
            out = io.StringIO()
            code = PA.hook_main(self.env(), io.StringIO(json.dumps(start_payload())), out, stderr=err)
        self.assertEqual((code, json.loads(out.getvalue())), (0, {}))
        self.assertIn("KeyError", err.getvalue())

    def test_scope_rules_of_the_library_still_apply(self) -> None:
        for env in (
            self.env(AGENT_LAUNCHER="botfleet"),
            self.env(AGENT_LAUNCH_SEAT="BOTFLEET"),
            self.env(SESSION_LETTER="0"),
            self.env(ANTIGRAVITY_LETTER="0"),
            self.env(SESSION_LETTER_LOCATION="server"),  # Antigravity runs on the Mac only
        ):
            self.assertEqual(self.hook(start_payload(), env=env), {}, env)
        self.assertEqual(self.calls, [])

    def test_the_session_id_is_the_conversation_id_from_the_payload_or_the_environment(self) -> None:
        self.assertEqual(PA.SPEC.session_id({"conversationId": CID}, {}), CID)
        self.assertEqual(PA.SPEC.session_id({}, {PA.ENV_CONVERSATION: CID}), CID)  # what `show` has in a shell
        self.assertEqual(PA.SPEC.session_id({"session_id": PARENT}, {PA.ENV_CONVERSATION: CID}), PARENT)

    def test_a_secret_in_the_first_prompt_never_reaches_the_operator(self) -> None:
        key = "ghp_" + "A1b2C3d4" * 5  # built here so the source holds no key-looking literal
        self.hook(start_payload())
        self.hook(pre_payload(lastUserInput="rotate the token %s today" % key))
        record = self.lookup()
        self.assertNotIn(key, record["title"])
        self.assertTrue(record["title"])

    def test_the_library_entry_hands_the_hook_to_this_adapter(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        code = PL.main(
            ["hook", "--platform", "antigravity"],
            env=self.env(),
            stdin=io.StringIO(json.dumps(start_payload())),
            stdout=out,
            stderr=err,
            transport=self.transport,
        )
        self.assertEqual(code, 0)
        self.assertIn("injectSteps", json.loads(out.getvalue()))
        out = io.StringIO()
        PL.main(["hook", "--platform", "antigravity"], env=self.env(), stdin=io.StringIO("junk"), stdout=out, stderr=err)
        self.assertEqual(json.loads(out.getvalue()), {})


class EntryScriptTests(Base):
    def test_the_installed_command_runs_end_to_end(self) -> None:
        """`platform-letter hook --platform antigravity`, as hooks.json runs it, with the payload on stdin."""
        env = dict(os.environ, **self.env())
        for name in ("AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "SESSION_LETTER", "ANTIGRAVITY_LETTER"):
            env.pop(name, None)
        proc = subprocess.run(
            [sys.executable, str(ENTRY), "hook", "--platform", "antigravity"],
            input=json.dumps(start_payload()),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        answer = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertIn("Operator session letter: Antigravity", self.said(answer))
        self.assertRegex(self.note()["label"], PAIR)
        again = subprocess.run(
            [sys.executable, str(ENTRY), "hook", "--platform", "antigravity"],
            input="{}", capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual((again.returncode, json.loads(again.stdout)), (0, {}))


# A stand-in for the library, next to a copy of the entry script: it records that it was imported and echoes the arguments
# and the standard input it was given, so a test can tell what reached the library and whether it came whole.
STUB_LIBRARY = """\
import json
import os
import sys

open(os.path.join(os.path.dirname(__file__), "..", "imported"), "w").close()


def main(argv):
    data = sys.stdin.read()
    sys.stdout.write(json.dumps({"argv": argv, "length": len(data), "head": data[:40], "tail": data[-40:]}) + "\\n")
    return 0
"""

# The hand-over the entry script makes under Python 3.9 (run_again_under), with the interpreter that runs these tests as
# the new one: it reads the payload from standard input first, as the script does, then hands it on.
HAND_OVER = """\
import sys

path = sys.argv[1]
names = {"__name__": "hand_over", "__file__": path}
exec(compile(open(path).read(), path, "exec"), names)
sys.argv = [path, "hook", "--platform", "antigravity"]
names["run_again_under"](sys.executable, sys.stdin.buffer.read())
"""

LONG_WORDS = "é語 " * 120000  # about 720 KB of UTF-8, far more than a pipe holds


def load_entry() -> dict:
    """The entry script's own names (its functions), without running it."""
    names = {"__name__": "platform_letter_entry", "__file__": str(ENTRY)}
    exec(compile(ENTRY.read_text(), str(ENTRY), "exec"), names)
    return names


def dumps_in_every_layout(payload: dict) -> list[str]:
    return [
        json.dumps(payload),
        json.dumps(payload, indent=2),
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        json.dumps(payload, ensure_ascii=False),
    ]


class QuickAnswerTests(Base):
    """`platform-letter hook --platform antigravity` answers a later model call before it imports the library: agy
    runs PreInvocation before every model call, and a later call used to pay for the whole library import to print `{}`
    (a median of 85 ms on the server and 71 ms on the Mac through /usr/bin/python3, against 24 and 30 ms now)."""

    def setUp(self) -> None:
        super().setUp()
        self.checkout = os.path.join(self.tmp.name, "checkout", "scripts")
        package = os.path.join(self.checkout, "agent_sync")
        os.makedirs(package)
        self.entry = os.path.join(self.checkout, "platform-letter")
        shutil.copy(ENTRY, self.entry)
        os.chmod(self.entry, 0o755)
        Path(package, "__init__.py").write_text("")
        Path(package, "platform_letter.py").write_text(STUB_LIBRARY)
        self.imported = os.path.join(self.checkout, "imported")

    def child_env(self) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONUTF8": "1"}

    def run_entry(self, payload: str, *argv: str) -> subprocess.CompletedProcess:
        words = argv or ("--platform", "antigravity")
        return subprocess.run(
            [sys.executable, "-I", "-B", self.entry, "hook", *words],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=60,
            env=self.child_env(),
        )

    def echoed(self, proc: subprocess.CompletedProcess) -> dict:
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def assert_whole(self, echo: dict, text: str) -> None:
        self.assertEqual((echo["length"], echo["head"], echo["tail"]), (len(text), text[:40], text[-40:]))

    def test_a_later_model_call_is_answered_before_the_library_is_imported(self) -> None:
        for number in (1, 2, 40):
            proc = self.run_entry(json.dumps(pre_payload(number=number)))
            self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, b"{}\n", b""), number)
        self.assertFalse(os.path.exists(self.imported))

    def test_the_platform_flag_is_found_in_either_form_among_the_others(self) -> None:
        later = json.dumps(pre_payload(number=3))
        for argv in (
            ["--platform=antigravity"],
            ["--location", "mac", "--platform", "antigravity"],
            ["--platform", "antigravity", "--remote", "mac"],
        ):
            self.assertEqual(self.run_entry(later, *argv).stdout, b"{}\n", argv)
        self.assertFalse(os.path.exists(self.imported))

    def test_every_other_payload_reaches_the_library_whole(self) -> None:
        for payload in (pre_payload(number=0), start_payload(), pre_payload(number=0, lastUserInput=LONG_WORDS)):
            text = json.dumps(payload, ensure_ascii=False)
            echo = self.echoed(self.run_entry(text))
            self.assert_whole(echo, text)
            self.assertEqual(echo["argv"], ["hook", "--platform", "antigravity"])
            self.assertTrue(os.path.exists(self.imported))
            os.unlink(self.imported)

    def test_another_platforms_hook_is_never_read_by_the_script(self) -> None:
        later = json.dumps(pre_payload(number=3))  # the same bytes, for a platform with no quick answer
        echo = self.echoed(self.run_entry(later, "--platform", "codex"))
        self.assert_whole(echo, later)
        self.assertTrue(os.path.exists(self.imported))

    def test_a_checkout_that_cannot_be_imported_still_answers_agy(self) -> None:
        os.unlink(os.path.join(self.checkout, "agent_sync", "platform_letter.py"))
        first = json.dumps(pre_payload(number=0))
        proc = self.run_entry(first)
        self.assertEqual((proc.returncode, proc.stdout), (0, b"{}\n"))  # agy reads an answer from every hook
        proc = self.run_entry(first, "--platform", "codex")
        self.assertEqual((proc.returncode, proc.stdout), (0, b""))  # and the others still say nothing

    def test_a_hook_with_no_standard_input_is_an_empty_payload(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-I", "-B", self.entry, "hook", "--platform", "antigravity"],
            capture_output=True,
            timeout=60,
            env=self.child_env(),
            preexec_fn=lambda: os.close(0),  # sys.stdin is None in the child
        )
        self.assertEqual(self.echoed(proc)["length"], 0)

    def test_the_hand_over_to_a_newer_python_feeds_the_payload_through_a_pipe(self) -> None:
        text = json.dumps(pre_payload(number=0, lastUserInput=LONG_WORDS), ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", HAND_OVER, self.entry],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
            env=self.child_env(),
        )
        self.assert_whole(self.echoed(proc), text)

    def test_the_quick_check_looks_only_at_a_key_with_a_number_above_zero(self) -> None:
        later_model_call = load_entry()["later_model_call"]
        for raw, expected in (
            (b'{"invocationNum": 3}', True),
            (b'{"invocationNum":3}', True),
            (b'{"invocationNum" :\n 12, "x": 0}', True),
            (b'{"x": "invocationNum", "invocationNum": 4}', True),  # the first one is a value, not the key
            (b'{"invocationNum": 1.5}', True),  # not an integer: normalize() turns it away too
            (b'{"invocationNum": 0}', False),
            (b'{"invocationNum": 0, "n": 5}', False),
            (b'{"n": 5, "invocationNum":0}', False),
            (b'{"invocationNum": -1}', False),
            (b'{"invocationNum": "3"}', False),
            (b'{"invocationNum": null}', False),
            (b'{"x": "invocationNum"}', False),
            (b'{"invocationNum"', False),
            (b'{"invocationNum":', False),
            (b"", False),
            (b"junk", False),
        ):
            self.assertEqual(later_model_call(raw), expected, raw)
            if expected:  # whatever it turns away, the adapter turns away
                self.assertIsNone(PA.normalize(dict(start_payload(), **json.loads(raw))), raw)

    def test_a_quick_answer_is_only_given_where_the_adapter_would_answer_nothing(self) -> None:
        """The quick check is a subset of the adapter's own: it never skips a payload that would have registered a chat,
        and the payload text that merely mentions the key (a prompt about hooks) is not mistaken for the key."""
        later_model_call = load_entry()["later_model_call"]
        mentions = (
            {},
            {"lastUserInput": 'say "invocationNum": 7 again'},
            {"lastUserInput": '{"invocationNum": 5}'},
            {"lastUserInput": "invocationNum"},
            {"modelName": "invocationNum"},
            {"workspacePaths": ["/x/invocationNum"]},
        )
        for number in (0, 1, 2, 9, 10, 250):
            for extra in mentions:
                payload = pre_payload(number=number, **extra)
                for text in dumps_in_every_layout(payload):
                    quick = later_model_call(text.encode("utf-8"))
                    self.assertEqual(quick, number > 0, text)
                    if quick:
                        self.calls.clear()
                        self.assertEqual(self.hook(payload), {})
                        self.assertEqual(self.calls, [])  # not even a hop to the registry
        for extra in mentions:  # a SessionStart that mentions the key is still a SessionStart
            for text in dumps_in_every_layout(start_payload(**extra)):
                self.assertFalse(later_model_call(text.encode("utf-8")), text)
        for payload in (dict(pre_payload(number=4), toolCall={"n": 1}), dict(pre_payload(number=4), conversationId="")):
            self.assertTrue(later_model_call(json.dumps(payload).encode("utf-8")))
            self.assertIsNone(PA.normalize(payload))


class MessageTests(unittest.TestCase):
    def test_a_reissued_letter_keeps_the_librarys_first_sentence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENT_SYNC_STATE_DIR": tmp, "HOME": tmp}
            os.makedirs(os.path.join(tmp, "AG", "letters"))
            with open(os.path.join(tmp, "AG", "letters", CID + ".json"), "w") as fh:
                json.dump({"generation": 2, "location": "mac"}, fh)
            body = {
                "label": "KL",
                "context": "This chat's earlier letter CC went to another chat while this one was closed, so the "
                "Operator issued KL.  Operator session letter: Antigravity KL (gen 2), on the Mac.  more",
            }
            text = PA.message_for(body, env, CID)
        self.assertTrue(text.startswith("This chat's earlier letter CC went to another chat"))
        self.assertIn("Operator session letter: Antigravity KL (gen 2), on the Mac.", text)

    def test_a_line_without_a_letter_is_passed_on(self) -> None:
        body = {"context": "No Operator letter yet (x).  A later prompt tries again, at most every ten minutes."}
        self.assertEqual(PA.message_for(body, {}, CID), body["context"])
        self.assertEqual(PA.injection("", {}, CID), {})
        self.assertEqual(PA.injection('{"platform_letter": {"event": "start", "platform": "antigravity"}}', {}, CID), {})


class InstallTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.link_command()  # the Mac has ~/.local/bin/platform-letter once agent_sync/install.sh has run

    def command(self) -> str:
        return os.path.join(self.home, ".local", "bin", "platform-letter")

    def link_command(self, mode: int = 0o755) -> None:
        os.makedirs(os.path.dirname(self.command()), exist_ok=True)
        if os.path.lexists(self.command()):
            os.unlink(self.command())
        Path(self.command()).write_text("#!/bin/sh\nexit 0\n")
        os.chmod(self.command(), mode)

    def hooks_path(self) -> str:
        return os.path.join(self.home, ".gemini", "config", "hooks.json")

    def seed(self, doc: dict | None = None, mode: int = 0o644) -> str:
        os.makedirs(os.path.dirname(self.hooks_path()), exist_ok=True)
        text = json.dumps(OWNERS_HOOKS if doc is None else doc, indent=2) + "\n"
        with open(self.hooks_path(), "w") as fh:
            fh.write(text)
        os.chmod(self.hooks_path(), mode)
        return text

    def install(self, **opts) -> tuple[int, dict]:
        out = io.StringIO()
        code = PA.install(PL.InstallOptions(**dict({"platform": "antigravity"}, **opts)), {"HOME": self.home}, out)
        return code, json.loads(out.getvalue())

    def load(self) -> dict:
        with open(self.hooks_path()) as fh:
            return json.load(fh)

    def test_install_adds_one_group_and_leaves_the_others_as_they_are(self) -> None:
        original = self.seed()
        code, out = self.install()
        self.assertEqual((code, sorted(out["changed"])), (0, ["PreInvocation", "SessionStart"]))
        self.assertNotIn("trust", out)  # agy has no hook trust step
        self.assertEqual(os.stat(out["backup"]).st_mode & 0o777, 0o600)
        self.assertEqual(Path(out["backup"]).read_text(), original)
        self.assertEqual(os.stat(self.hooks_path()).st_mode & 0o777, 0o644)
        doc = self.load()
        self.assertEqual(list(doc), ["agent-hook-otlp", "lane-guard", PA.GROUP])  # appended, nothing reordered
        for name, group in OWNERS_HOOKS.items():
            self.assertEqual(doc[name], group)
        group = doc[PA.GROUP]
        self.assertIs(group["enabled"], True)
        command = "%s hook --platform antigravity" % os.path.join(self.home, ".local", "bin", "platform-letter")
        self.assertEqual(group["SessionStart"], [{"type": "command", "command": command, "timeout": 20}])
        self.assertEqual(group["PreInvocation"], [{"type": "command", "command": command, "timeout": 15}])
        self.assertNotIn("Stop", group)  # nothing is hooked on a turn's end

    def test_install_is_idempotent_and_checkable(self) -> None:
        self.seed()
        self.assertEqual(self.install(check=True)[0], 1)
        self.install()
        before = Path(self.hooks_path()).read_text()
        again = self.install()[1]
        self.assertEqual(again["changed"], [])
        self.assertEqual(Path(self.hooks_path()).read_text(), before)
        code, check = self.install(check=True)
        self.assertEqual((code, all(check["installed"].values()), all(check["current"].values())), (0, True, True))
        backups = [n for n in os.listdir(os.path.dirname(self.hooks_path())) if ".bak-letter-" in n]
        self.assertEqual(len(backups), 1)  # the second run rewrote nothing

    def test_an_outdated_command_is_replaced_in_place(self) -> None:
        self.seed()
        self.install()
        doc = self.load()
        doc[PA.GROUP]["SessionStart"][0]["timeout"] = 1
        self.seed(doc)
        self.assertEqual(self.install()[1]["changed"], ["SessionStart"])
        self.assertEqual(self.load()[PA.GROUP]["SessionStart"][0]["timeout"], 20)

    def test_a_hook_of_the_owners_in_the_same_group_is_never_touched(self) -> None:
        self.seed()
        self.install()
        doc = self.load()
        mine = {"type": "command", "command": "/x/theirs.sh", "timeout": 5}
        doc[PA.GROUP]["SessionStart"].insert(0, mine)
        self.seed(doc)
        self.assertEqual(self.install()[1]["changed"], [])
        self.assertEqual(self.load()[PA.GROUP]["SessionStart"][0], mine)
        self.install(remove=True)
        doc = self.load()
        self.assertEqual(doc[PA.GROUP]["SessionStart"], [mine])  # the group stays: it still holds a hook that is not ours
        self.assertNotIn("PreInvocation", doc[PA.GROUP])

    def test_remove_restores_the_file_the_owner_had(self) -> None:
        original = self.seed()
        self.install()
        code, out = self.install(remove=True)
        self.assertEqual((code, sorted(out["changed"]), out["removed_group"]), (0, ["PreInvocation", "SessionStart"], PA.GROUP))
        self.assertEqual(Path(self.hooks_path()).read_text(), original)  # not a byte different
        self.assertEqual(self.install(remove=True)[1]["changed"], [])
        self.assertEqual(self.install(check=True)[0], 1)

    def test_a_missing_file_is_created_and_removed_again(self) -> None:
        code, out = self.install(location="mac")
        self.assertEqual((code, out["backup"]), (0, ""))
        self.assertEqual(os.stat(self.hooks_path()).st_mode & 0o777, 0o600)
        self.assertIn("--location mac", self.load()[PA.GROUP]["SessionStart"][0]["command"])
        self.install(remove=True)
        self.assertEqual(self.load(), {})

    def test_a_file_that_is_not_the_expected_shape_is_refused_untouched(self) -> None:
        for text in ("not json", "[]", json.dumps({PA.GROUP: []}), json.dumps({PA.GROUP: {"SessionStart": {}}})):
            os.makedirs(os.path.dirname(self.hooks_path()), exist_ok=True)
            Path(self.hooks_path()).write_text(text)
            code, out = self.install()
            self.assertEqual(code, 2, text)
            self.assertEqual(Path(self.hooks_path()).read_text(), text)

    def test_a_healthy_check_says_so(self) -> None:
        self.seed()
        self.install()
        code, check = self.install(check=True)
        self.assertEqual((code, check["healthy"]), (0, True))
        self.assertNotIn("problems", check)

    def test_install_writes_nothing_while_the_hook_command_is_not_there(self) -> None:
        """The installer used to write a command that is not there and then call the install healthy.  agy runs the
        command before every model call, so nothing is written until it exists and can be run."""
        original = self.seed()
        for how in ("missing", "dangling link", "not executable"):
            if os.path.lexists(self.command()):
                os.unlink(self.command())
            if how == "dangling link":
                os.symlink(os.path.join(self.home, "nowhere"), self.command())
            elif how == "not executable":
                self.link_command(0o644)
            code, out = self.install()
            self.assertEqual((code, out["changed"]), (2, []), how)
            self.assertIn("nothing written", out["error"], how)
            self.assertIn(self.command(), out["error"], how)
            self.assertEqual(Path(self.hooks_path()).read_text(), original, how)
        names = os.listdir(os.path.dirname(self.hooks_path()))
        self.assertEqual([n for n in names if ".bak-letter-" in n or n.endswith(".tmp")], [])

    def test_install_creates_no_file_when_the_command_is_not_there(self) -> None:
        os.unlink(self.command())
        code, out = self.install()
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(os.path.dirname(self.hooks_path())))

    def test_check_reports_a_command_that_went_missing_after_the_install(self) -> None:
        self.seed()
        self.install()
        os.unlink(self.command())
        code, out = self.install(check=True)
        self.assertEqual((code, out["healthy"]), (1, False))
        self.assertEqual(list(out["installed"].values()), [True, True])  # the file is as the installer wrote it
        self.assertEqual(list(out["current"].values()), [False, False])  # and is still not a healthy install
        self.assertEqual(
            out["problems"], ["the hook command %s does not exist (SessionStart, PreInvocation)" % self.command()]
        )
        self.link_command(0o600)
        code, out = self.install(check=True)
        self.assertEqual((code, out["healthy"]), (1, False))
        self.assertIn("is not executable", out["problems"][0])
        self.link_command()
        self.assertEqual(self.install(check=True)[0], 0)

    def test_check_looks_at_the_command_that_is_installed(self) -> None:
        """An older install can point at another place; that command is the one agy runs."""
        self.seed()
        self.install()
        doc = self.load()
        for event in ("SessionStart", "PreInvocation"):
            doc[PA.GROUP][event][0]["command"] = "/old/place/platform-letter hook --platform antigravity"
        self.seed(doc)
        code, out = self.install(check=True)
        self.assertEqual((code, out["healthy"]), (1, False))
        self.assertIn("/old/place/platform-letter does not exist", out["problems"][0])

    def test_a_group_that_is_switched_off_is_reported_and_switched_on_again(self) -> None:
        self.seed()
        self.install()
        doc = self.load()
        doc[PA.GROUP]["enabled"] = False
        off = self.seed(doc, mode=0o640)
        code, out = self.install(check=True)
        self.assertEqual((code, out["healthy"], list(out["current"].values())), (1, False, [False, False]))
        self.assertEqual(list(out["installed"].values()), [True, True])
        self.assertIn("switched off", out["problems"][0])
        self.assertEqual(Path(self.hooks_path()).read_text(), off)  # a check changes nothing
        code, out = self.install()
        self.assertEqual((code, out["changed"], out["enabled_group"]), (0, [], PA.GROUP))
        self.assertEqual(Path(out["enable_backup"]).read_text(), off)
        self.assertEqual(os.stat(out["enable_backup"]).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.hooks_path()).st_mode & 0o777, 0o640)
        doc = self.load()
        self.assertIs(doc[PA.GROUP]["enabled"], True)
        self.assertEqual(list(doc), ["agent-hook-otlp", "lane-guard", PA.GROUP])
        for name, group in OWNERS_HOOKS.items():
            self.assertEqual(doc[name], group)
        code, out = self.install(check=True)
        self.assertEqual((code, out["healthy"]), (0, True))
        self.assertNotIn("enabled_group", self.install()[1])  # on already: nothing more to write

    def test_an_install_into_a_group_that_is_off_adds_the_hooks_and_switches_it_on(self) -> None:
        self.seed(dict(OWNERS_HOOKS, **{PA.GROUP: {"enabled": False}}))
        code, out = self.install()
        self.assertEqual((code, sorted(out["changed"])), (0, ["PreInvocation", "SessionStart"]))
        self.assertEqual(out["enabled_group"], PA.GROUP)
        self.assertNotEqual(out["backup"], out["enable_backup"])
        doc = self.load()
        self.assertIs(doc[PA.GROUP]["enabled"], True)
        self.assertEqual(sorted(k for k in doc[PA.GROUP] if k != "enabled"), ["PreInvocation", "SessionStart"])

    def test_remove_does_not_need_the_command(self) -> None:
        original = self.seed()
        self.install()
        os.unlink(self.command())
        code, out = self.install(remove=True)
        self.assertEqual((code, out["removed_group"]), (0, PA.GROUP))
        self.assertEqual(Path(self.hooks_path()).read_text(), original)

    def test_a_check_with_nothing_installed_has_no_problems_to_report(self) -> None:
        os.unlink(self.command())
        self.assertEqual(PA.hook_problems(self.hooks_path()), [])  # no file
        self.seed()
        code, out = self.install(check=True)
        self.assertEqual((code, out["healthy"]), (1, False))
        self.assertNotIn("problems", out)  # the group is not there: that is `installed: false`, not a problem

    def test_the_library_installer_finds_this_adapter(self) -> None:
        out = io.StringIO()
        code = PL.install_main(["--platform", "antigravity", "--check"], {"HOME": self.home}, out)
        self.assertEqual((code, json.loads(out.getvalue())["installed"]), (1, {"SessionStart": False, "PreInvocation": False}))


if __name__ == "__main__":
    unittest.main()
