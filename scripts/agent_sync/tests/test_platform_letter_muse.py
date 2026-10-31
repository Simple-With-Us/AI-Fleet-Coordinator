"""The Muse Code adapter of `platform-letter` (AFC-564):  the spec, the parent-process rules, the title queue, the rename
helper and the plugin bundle.  No test starts a real Muse:  the parent process is a fake /proc tree, the MSP host is a
small script that answers like `muse serve` on stdio, and the registry is either a fake transport or a temporary
registry file (which main's registry and the pair registry both accept)."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_sync import muse_session_rename as MR
from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL
from agent_sync import platform_letter_muse as PLM

SCRIPTS = Path(__file__).resolve().parents[2]
SID = "01a18a33-a850-7e22-82f5-a9ce847c65af"
PARENT_SID = "01a18a2a-6a35-7552-974f-9a62d8ddac1b"
CHILD_SID = "00000000-0000-75af-9e7d-5afb03ee70b8"
MUSE_EXE = "/opt/muse-fixture/bin/muse-bin-1.4.14-R7309.1"  # not a file on any host: the fake exe stays fake


def fake_proc(root: str, pid: int, *, ppid: int = 1, argv: tuple = (), env: dict | None = None, exe: str = "", state: str = "S") -> None:
    """One entry of a fake /proc:  cmdline, stat, environ and the exe link."""
    base = os.path.join(root, str(pid))
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "cmdline"), "wb") as fh:
        fh.write(b"".join(a.encode() + b"\0" for a in argv))
    with open(os.path.join(base, "stat"), "w") as fh:
        fh.write("%d (%s) %s %d 0 0 0 0\n" % (pid, "muse (x)" if "muse" in " ".join(argv) else "sh", state, ppid))
    with open(os.path.join(base, "environ"), "wb") as fh:
        fh.write(b"".join(("%s=%s" % kv).encode() + b"\0" for kv in (env or {}).items()))
    link = os.path.join(base, "exe")
    if os.path.lexists(link):
        os.unlink(link)  # a test makes the same pid a different program, one after another
    if exe:
        os.symlink(exe, link)


class Tree(unittest.TestCase):
    """A temp home and state folder, a fake /proc, and the module switches that point the adapter at them."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        self.proc = os.path.join(self.tmp.name, "proc")
        os.makedirs(self.home)
        os.makedirs(self.proc)
        self.hook_pid, self.muse_pid = 200, 100
        fake_proc(self.proc, 1, argv=("init",))
        for patch in (
            mock.patch.object(PLM, "PROC_ROOT", self.proc),
            mock.patch.object(PLM, "_ppid", lambda: self.muse_pid),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "AGENT_SYNC_STATE_DIR": self.state, "PATH": "/usr/bin:/bin"}
        env.update(extra)
        return env

    def muse(self, *words: str, env: dict | None = None, exe: str = MUSE_EXE) -> None:
        """Make the hook's parent a Muse process started with `words` (after the program name)."""
        fake_proc(self.proc, self.muse_pid, ppid=50, argv=(exe, *words), env=env, exe=exe)

    def store(self, sid: str, first: dict | None = None, *, lock: str = "") -> str:
        """A stored session folder under the home, with an optional first record and lock file."""
        d = os.path.join(self.home, ".local", "share", "muse", "sessions", "2026", "10", "30", sid)
        os.makedirs(d, exist_ok=True)
        if first is not None:
            with open(os.path.join(d, "session.jsonl"), "w") as fh:
                fh.write(json.dumps(first) + "\n")
        with open(os.path.join(d, ".session.lock"), "w") as fh:
            fh.write(lock)
        return d


class ProcessTests(Tree):
    def test_read_proc_keeps_only_the_identity_variables(self) -> None:
        fake_proc(self.proc, 7, ppid=3, argv=(MUSE_EXE, "exec"), exe=MUSE_EXE,
                  env={"AGENT_LAUNCHER": "botfleet", "GITHUB_TOKEN": "x" * 12, "MUSE_LETTER": "0", "PATH": "/bin"})
        proc = MR.read_proc(7, self.proc)
        self.assertEqual((proc.pid, proc.ppid, proc.exe), (7, 3, MUSE_EXE))
        self.assertEqual(dict(proc.env), {"AGENT_LAUNCHER": "botfleet", "MUSE_LETTER": "0"})
        self.assertNotIn("x" * 12, repr(proc))

    def test_a_missing_process_a_zombie_and_pid_one_are_none(self) -> None:
        fake_proc(self.proc, 8, argv=("muse",), state="Z")
        self.assertIsNone(MR.read_proc(8, self.proc))
        self.assertIsNone(MR.read_proc(9, self.proc))
        self.assertIsNone(MR.read_proc(1, self.proc))

    def test_the_muse_ancestor_is_found_through_a_wrapper(self) -> None:
        fake_proc(self.proc, 300, ppid=100, argv=("/usr/bin/env", "sh"))
        fake_proc(self.proc, 100, ppid=50, argv=(MUSE_EXE,), exe=MUSE_EXE)
        self.assertEqual(MR.muse_ancestor(300, self.proc).pid, 100)
        fake_proc(self.proc, 400, ppid=1, argv=("bash",))
        self.assertIsNone(MR.muse_ancestor(400, self.proc))

    def test_the_desktop_app_counts_as_muse(self) -> None:
        fake_proc(self.proc, 100, argv=("/Applications/Muse.app/Contents/MacOS/Muse",), exe="/Applications/Muse.app/Contents/MacOS/Muse")
        self.assertEqual(MR.muse_ancestor(100, self.proc).name, "Muse")

    def test_the_subcommand(self) -> None:
        cases = [
            ((), ""), (("muse",), ""), (("muse", "exec", "hi"), "exec"), (("muse", "--provider", "echo", "exec"), "exec"),
            (("muse", "--provider", "echo"), ""), (("muse", "--provider=echo", "serve"), "serve"),
            (("muse", "resume", SID), "resume"), (("muse", "--trust-workspace", "resume"), "resume"),
            (("muse", "how do I serve this"), ""), (("muse", "serve"), "serve"), (("muse", "-w", "create", "serve"), "serve"),
            (("muse", "-w", "serve"), "serve"), (("muse", "-c", "x=1", "plugins", "hook"), "plugins"),
            (("muse", "--model", "exec"), ""),
        ]
        for argv, want in cases:
            self.assertEqual(MR.subcommand(argv), want, argv)

    def test_the_mac_reads_each_argument_whole_from_the_kernel(self) -> None:
        # macOS has no /proc.  `ps` joins the arguments with spaces, so a prompt that opens with a subcommand word
        # (muse "login page redirects to the wrong host") looked like the subcommand `login`; the kernel keeps it whole.
        def runner(argv, **kw):
            flat = "/opt/homebrew/bin/muse login page redirects to the wrong host"
            if argv[:3] == ["ps", "-o", "ppid=,comm="]:
                return SimpleNamespace(stdout="  4242 /opt/homebrew/bin/muse\n")
            if argv[:3] == ["ps", "-o", "command="]:
                return SimpleNamespace(stdout=flat + "\n")
            return SimpleNamespace(stdout=flat + " MUSE_LETTER=0 GITHUB_TOKEN=" + "x" * 12 + "\n")  # ps eww

        kernel = ("/opt/homebrew/bin/muse", ("/opt/homebrew/bin/muse", "login page redirects to the wrong host"),
                  ["PATH=/bin", "MUSE_LETTER=0", "GITHUB_TOKEN=" + "x" * 12])
        with mock.patch.object(MR, "default_proc_root", lambda: None), mock.patch.object(MR, "kernel_args", lambda pid: kernel):
            proc = MR.read_proc(9999, None, runner)
        self.assertEqual((proc.pid, proc.ppid, proc.exe), (9999, 4242, "/opt/homebrew/bin/muse"))
        self.assertEqual(proc.argv, kernel[1])
        self.assertEqual(MR.subcommand(proc.argv), "")
        self.assertEqual(dict(proc.env), {"MUSE_LETTER": "0"})
        self.assertNotIn("x" * 12, repr(proc))
        # Where the kernel will not say, `ps` alone answers, with the words flattened as before.
        with mock.patch.object(MR, "default_proc_root", lambda: None), mock.patch.object(MR, "kernel_args", lambda pid: None):
            flat = MR.read_proc(9999, None, runner)
        self.assertEqual(flat.argv[:2], ("/opt/homebrew/bin/muse", "login"))
        self.assertEqual(dict(flat.env), {"MUSE_LETTER": "0"})
        # A process that is gone is None, whichever way it is read.
        gone = lambda argv, **kw: SimpleNamespace(stdout="")
        with mock.patch.object(MR, "default_proc_root", lambda: None), mock.patch.object(MR, "kernel_args", lambda pid: kernel):
            self.assertIsNone(MR.read_proc(9999, None, gone))

    def test_a_kernel_argument_buffer_is_split_into_path_arguments_and_environment(self) -> None:
        def buffer(argc: int, path: bytes, args: list, env: list, pad: int = 3) -> bytes:
            return argc.to_bytes(4, sys.byteorder) + path + b"\0" * pad + b"".join(a + b"\0" for a in args) + b"".join(e + b"\0" for e in env)

        raw = buffer(2, b"/opt/muse", [b"muse", b"login page"], [b"PATH=/bin", b"MUSE_LETTER=0"])
        self.assertEqual(MR.parse_procargs(raw), ("/opt/muse", ("muse", "login page"), ["PATH=/bin", "MUSE_LETTER=0"]))
        # An empty argument stays, and a process with no environment is fine.
        self.assertEqual(MR.parse_procargs(buffer(3, b"/opt/muse", [b"muse", b"", b"x"], [])), ("/opt/muse", ("muse", "", "x"), []))
        for bad in (b"", b"\0\0\0\0", buffer(0, b"/x", [], []), buffer(9, b"/x", [b"a"], []), b"\2\0\0\0/no-nul"):
            self.assertIsNone(MR.parse_procargs(bad), bad)

    def test_the_kernel_call_is_only_for_macos(self) -> None:
        got = MR.kernel_args(os.getpid())
        if sys.platform == "darwin":
            self.assertIsNotNone(got)
            self.assertTrue(any("python" in w.lower() for w in got[1]), got[1])
        else:
            self.assertIsNone(got)

    def test_uuid7_is_a_version_seven_uuid(self) -> None:
        for _ in range(20):
            self.assertRegex(MR.uuid7(), r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class KindTests(Tree):
    def kind(self, event: str = "SessionStart", sid: str = SID) -> str:
        return PLM.muse_kind({"hook_event_name": event, "session_id": sid}, self.env())

    def test_a_chat_in_a_terminal_is_a_chat(self) -> None:
        self.muse("--provider", "echo")
        self.assertEqual(self.kind(), "chat")
        self.muse("resume", SID)
        self.assertEqual(self.kind(), "chat")
        self.muse("a prompt as an argument")
        self.assertEqual(self.kind(), "chat")

    def test_headless_runs_and_hosts_take_no_letter(self) -> None:
        for words in (("exec", "-c", "run.toolset=[]", "--prompt-file", "p"), ("serve", "--provider", "echo"),
                      ("plugins", "hook", "test", "session-letter:letter-start"), ("skills", "list"), ("acp",)):
            self.muse(*words)
            self.assertEqual(self.kind(), "headless", words)

    def test_a_launcher_the_opt_outs_and_the_helper_mark_take_no_letter(self) -> None:
        for env in ({"AGENT_LAUNCHER": "botfleet"}, {"AGENT_LAUNCH_SEAT": "CODEX"}, {"AGENT_LAUNCHER": "agent-sync-wake"},
                    {"SESSION_LETTER": "0"}, {"MUSE_LETTER": "0"}, {MR.HELPER_MARK: "1"}):
            self.muse(env=env)
            self.assertEqual(self.kind(), "headless", env)
        self.muse(env={"SESSION_LETTER": "1", "AGENT_SEAT": "MC"})
        self.assertEqual(self.kind(), "chat")

    def test_a_sub_agent_session_is_not_a_chat(self) -> None:
        # Muse keeps a sub-agent's session inside its parent chat's folder;  the chat itself has a top-level folder.
        self.muse("--provider", "echo")
        self.store(SID)
        nested = os.path.join(self.home, ".local", "share", "muse", "sessions", "2026", "10", "30", SID, "subagent", CHILD_SID)
        os.makedirs(nested)
        self.assertEqual(self.kind(sid=SID), "chat")
        self.assertEqual(self.kind(sid=CHILD_SID), "subagent")
        self.assertEqual(self.kind(sid=PARENT_SID), "chat")  # an id the store does not hold is not taken for a sub-agent
        self.assertFalse(MR.is_subagent(PLM._data_root(self.env()), "../../etc"))

    def test_the_guard_shell_between_the_hook_and_muse_is_walked_through(self) -> None:
        self.muse_pid = 150  # the hook's parent is the `sh` of PLM.GUARD, and Muse is its parent
        fake_proc(self.proc, 150, ppid=100, argv=("/bin/sh", "-c", '"$@" || exit 0', "sh", "/h/.local/bin/platform-letter", "hook"))
        fake_proc(self.proc, 100, ppid=50, argv=(MUSE_EXE, "--provider", "echo"), exe=MUSE_EXE)
        self.assertEqual(self.kind(), "chat")
        fake_proc(self.proc, 100, ppid=50, argv=(MUSE_EXE, "exec", "hi"), exe=MUSE_EXE)
        self.assertEqual(self.kind(), "headless")

    def test_a_parent_that_is_not_muse_takes_no_letter(self) -> None:
        fake_proc(self.proc, self.muse_pid, ppid=1, argv=("bash",))
        self.assertEqual(self.kind(), "headless")
        self.assertEqual(self.kind("SessionEnd"), "headless")

    def test_the_end_of_a_known_chat_is_a_chat_even_when_muse_is_gone(self) -> None:
        # No process at all stands for the parent (Muse exited before the hook read it).
        self.muse_pid = 999
        self.assertEqual(self.kind("SessionEnd"), "headless")
        note = PL.note_path(self.env(), PLM.SEAT, SID)
        os.makedirs(os.path.dirname(note))
        Path(note).write_text("{}")
        self.assertEqual(self.kind("SessionEnd"), "chat")
        self.assertEqual(self.kind("SessionStart"), "headless")


class SpecTests(unittest.TestCase):
    def test_the_adapter_loads_by_name(self) -> None:
        spec = PL.load_spec("muse")
        self.assertIs(spec, PLM.SPEC)
        self.assertEqual((spec.platform, spec.seat, spec.source, spec.display), ("muse", "MC", "muse-letter-hook", "Muse Code"))
        self.assertEqual(spec.off_variable, "MUSE_LETTER")
        self.assertEqual(spec.close_reasons, ())
        self.assertFalse(spec.title_in_output)
        self.assertEqual(spec.events["SessionStart"], "start")

    def test_the_session_id_comes_from_the_payload_then_the_environment(self) -> None:
        self.assertEqual(PLM.SPEC.session_id({"session_id": "a-1234"}, {"MUSE_SESSION_ID": "b-1234"}), "a-1234")
        self.assertEqual(PLM.SPEC.session_id({}, {"MUSE_SESSION_ID": "b-1234"}), "b-1234")
        self.assertEqual(PLM.SPEC.session_id({}, {}), "")


class StoreTests(Tree):
    def fork_record(self, parent: str) -> dict:
        return {"record_type": "session.fork.created", "payload": {"fork_session_id": CHILD_SID, "source_session_id": parent}}

    def test_a_fork_names_its_parent(self) -> None:
        self.store(CHILD_SID, self.fork_record(PARENT_SID))
        self.assertEqual(PLM.muse_parent({"session_id": CHILD_SID}, self.env()), PARENT_SID)

    def test_a_chat_with_no_fork_record_has_no_parent(self) -> None:
        self.store(SID, {"record_type": "session.started", "payload": {"x": 1}})
        self.assertEqual(PLM.muse_parent({"session_id": SID}, self.env()), "")
        self.assertEqual(PLM.muse_parent({"session_id": "no-such-session"}, self.env()), "")
        self.assertEqual(PLM.muse_parent({}, self.env()), "")

    def test_a_bad_first_record_or_parent_is_ignored(self) -> None:
        d = self.store(CHILD_SID)
        Path(d, "session.jsonl").write_text("not json\n")
        self.assertEqual(PLM.muse_parent({"session_id": CHILD_SID}, self.env()), "")
        Path(d, "session.jsonl").write_text(json.dumps(self.fork_record("../x")) + "\n")
        self.assertEqual(PLM.muse_parent({"session_id": CHILD_SID}, self.env()), "")
        Path(d, "session.jsonl").write_text(json.dumps(self.fork_record(CHILD_SID)) + "\n")
        self.assertEqual(PLM.muse_parent({"session_id": CHILD_SID}, self.env()), "")

    def test_the_session_folder_is_found_only_for_a_plain_id(self) -> None:
        d = self.store(SID)
        root = MR.data_root(self.home)
        self.assertEqual(MR.find_session_dir(root, SID), d)
        for bad in ("", "../" + SID, "a/b", "short", SID + "/.."):
            self.assertIsNone(MR.find_session_dir(root, bad), bad)

    def test_the_data_root_follows_xdg(self) -> None:
        self.assertEqual(MR.data_root("/h"), "/h/.local/share/muse")
        self.assertEqual(MR.data_root("/h", "/x/data"), "/x/data/muse")

    def test_the_lock_names_its_holder(self) -> None:
        d = self.store(SID, lock="pid=42\nhost=h\nrole=serve\n")
        self.assertEqual(MR.lock_holder(d), (42, "serve"))
        Path(d, ".session.lock").write_text("")
        self.assertEqual(MR.lock_holder(d), (None, ""))
        Path(d, ".session.lock").write_text("pid=42\nhost=h\n")
        fake_proc(self.proc, 42, argv=("muse",))
        self.assertTrue(MR.lease_held(d, None, self.proc))
        self.assertFalse(MR.lease_held(d, None, os.path.join(self.tmp.name, "nowhere")))
        self.assertFalse(MR.lease_held(None, None, self.proc))
        self.assertTrue(MR.lease_held(None, 42, self.proc))

    def test_a_resumed_chat_takes_the_stored_title_of_its_note_or_its_parent(self) -> None:
        env = self.env()
        for sid, title in ((SID, "Fix Login"), (PARENT_SID, "Parent Words")):
            path = PL.note_path(env, PLM.SEAT, sid)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            Path(path).write_text(json.dumps({"title": title}))
        self.assertEqual(PLM.muse_stored_title({"session_id": SID}, env), "Fix Login")
        self.store(CHILD_SID, self.fork_record(PARENT_SID))
        self.assertEqual(PLM.muse_stored_title({"session_id": CHILD_SID}, env), "Parent Words")
        self.assertEqual(PLM.muse_stored_title({"session_id": "unknown-chat"}, env), "")


class FakeMuse:
    """A host that answers like `muse serve` on stdio, as a script run by this interpreter.  FAKE_MODE chooses the
    answers;  FAKE_LOG names a file that collects the methods it was asked."""

    SCRIPT = r'''
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
name = os.environ.get("FAKE_NAME", "")
origin = os.environ.get("FAKE_ORIGIN", "")
log = os.environ.get("FAKE_LOG")
for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps([method, params]) + "\n")
    if rid is None:
        continue
    print(json.dumps({"jsonrpc": "2.0", "method": "session/started", "params": {}}), flush=True)
    if mode == "silent" and method == "initialize":
        time.sleep(3)
        continue
    if mode == "exit" and method == "initialize":
        sys.exit(3)
    if method == "initialize":
        if params.get("clientInfo", {}).get("name", "").replace("_", "").isalnum() is False:
            out = {"error": {"code": -32602, "message": "bad client name", "data": {"kind": "invalidParams"}}}
        else:
            out = {"result": {"museHome": "/x"}}
    elif method == "session/resume":
        if mode == "in_use":
            out = {"error": {"code": -32021, "message": "in use", "data": {"kind": "sessionInUse"}}}
        else:
            out = {"result": {"session": {"name": name}}}
    elif method == "session/read":
        out = {"result": {"session": {"name": name, "nameOrigin": origin, "providerId": os.environ.get("FAKE_PROVIDER", "echo"),
                                      "modelId": os.environ.get("FAKE_MODEL") or None}}}
    elif method == "session/rename":
        if mode == "invalid":
            out = {"error": {"code": -32030, "message": "rejected", "data": {"kind": "commandRejected", "reason": "name_invalid"}}}
        else:
            out = {"result": {"commandId": params["commandId"], "name": params["name"].lower(), "status": "accepted"}}
    else:
        out = {"result": {"status": "accepted"}}
    print(json.dumps(dict(out, jsonrpc="2.0", id=rid)), flush=True)
'''

    def __init__(self, test: unittest.TestCase, **env: str):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.script = os.path.join(self.dir.name, "fake_muse.py")
        Path(self.script).write_text(self.SCRIPT)
        self.log = os.path.join(self.dir.name, "log.jsonl")
        self.env = dict(os.environ, FAKE_LOG=self.log, **env)

    def host(self, timeout: float = 10.0) -> MR.MspHost:
        return MR.MspHost([sys.executable, self.script], self.env, "/", timeout=timeout)

    def calls(self) -> list[list]:
        try:
            return [json.loads(line) for line in Path(self.log).read_text().splitlines()]
        except OSError:
            return []

    def methods(self) -> list[str]:
        return [c[0] for c in self.calls()]


class ChooseNameTests(unittest.TestCase):
    def test_a_chat_with_no_name_or_an_automatic_one_takes_the_title(self) -> None:
        self.assertEqual(MR.choose_name("AA", "AA: Fix Login", [], "", ""), "AA: Fix Login")
        self.assertEqual(MR.choose_name("AA", "AA: Fix Login", [], "glowing-halo", "automatic"), "AA: Fix Login")

    def test_a_name_the_helper_set_is_replaced(self) -> None:
        self.assertEqual(MR.choose_name("AA", "AA: Fix Login Page", ["aa: new chat"], "aa: new chat", "custom"), "AA: Fix Login Page")

    def test_the_title_the_chat_already_carries_is_left_alone(self) -> None:
        self.assertIsNone(MR.choose_name("AA", "AA: Fix Login", ["aa: fix login"], "aa: fix login", "custom"))
        self.assertIsNone(MR.choose_name("AA", "AA: Fix Login", [], "aa: fix login", "custom"))

    def test_a_name_given_by_hand_keeps_its_words(self) -> None:
        self.assertEqual(MR.choose_name("AA", "AA: Fix Login", [], "my own name", "custom"), "AA: my own name")
        self.assertIsNone(MR.choose_name("AA", "AA: Fix Login", [], "aa: my own name", "custom"))

    def test_a_long_name_given_by_hand_is_never_cut(self) -> None:
        # Muse takes names of well over a thousand characters, so there is no reason to cut someone's words.
        words = "q4 launch plan for the new pricing tiers and the billing migration schedule"
        self.assertEqual(MR.choose_name("AA", "AA: Fix", [], words, "custom"), "AA: " + words)
        self.assertEqual(MR.choose_name("AA", "AA: Fix", [], "x" * 1500, "custom"), "AA: " + "x" * 1500)
        self.assertFalse(hasattr(MR, "MAX_NAME"))

    def test_a_name_wrapped_around_someones_words_is_theirs_still(self) -> None:
        # The helper set "aa: q4 launch plan" around words the owner typed.  The title a resumed chat queues again is the
        # stale automatic one, and must not take the name back.
        wrapped = ["aa: q4 launch plan"]
        applied = ["aa: fix login page"]
        self.assertIsNone(MR.choose_name("AA", "AA: Fix Login Page", applied, "aa: q4 launch plan", "custom", wrapped))
        self.assertIsNone(MR.choose_name("AA", "AA: A Newer Title", applied, "AA: Q4 Launch Plan", "custom", wrapped))
        # A letter that changed goes in place of the old one, and the words stay.
        self.assertEqual(MR.choose_name("AB", "AB: Fix Login Page", applied, "aa: q4 launch plan", "custom", wrapped),
                         "AB: q4 launch plan")
        # The same text with no record of the wrap is the helper's own automatic title, which the library may replace.
        self.assertEqual(MR.choose_name("AA", "AA: Fix Login Page", ["aa: q4 launch plan"], "aa: q4 launch plan", "custom"),
                         "AA: Fix Login Page")

    def test_plan_name_says_whether_the_name_wraps_someones_words(self) -> None:
        self.assertEqual(MR.plan_name("AA", "AA: Fix", [], [], "my own name", "custom"), ("AA: my own name", True))
        self.assertEqual(MR.plan_name("AA", "AA: Fix", ["aa: new chat"], [], "aa: new chat", "custom"), ("AA: Fix", False))
        self.assertEqual(MR.plan_name("AA", "AA: Fix", [], [], "", ""), ("AA: Fix", False))
        self.assertEqual(MR.plan_name("AA", "AA: Fix", [], [], "   ", "custom"), ("AA: Fix", False))  # spaces are no words
        self.assertEqual(MR.plan_name("AA", "AA: Fix", [], [], "aa: mine", "custom"), (None, False))

    def test_a_name_is_filed_in_one_list_and_the_newest_is_last(self) -> None:
        self.assertEqual(MR.remember(["aa: a"], [], "aa: b", False), (["aa: a", "aa: b"], []))
        self.assertEqual(MR.remember(["aa: a"], [], "AA: A", True), ([], ["AA: A"]))
        self.assertEqual(MR.remember([], ["aa: w"], "aa: w", False), (["aa: w"], []))
        self.assertEqual(MR.remember(["x", "y"], [], "X", False), (["y", "X"], []))


class RenameSessionTests(unittest.TestCase):
    def test_the_wire_conversation_renames_and_leaves(self) -> None:
        fake = FakeMuse(self, FAKE_NAME="glowing-halo", FAKE_ORIGIN="automatic")
        host = fake.host()
        try:
            result = MR.rename_session(host, SID, "AA", "AA: Fix Login", [])
        finally:
            host.close()
        self.assertEqual(result, {"name": "aa: fix login", "set": True, "hand": False})
        self.assertEqual(fake.methods(), ["initialize", "initialized", "session/resume", "session/read", "session/rename", "session/leave"])
        rename = fake.calls()[4][1]
        self.assertEqual((rename["sessionId"], rename["name"]), (SID, "AA: Fix Login"))
        self.assertRegex(rename["commandId"], r"^[0-9a-f-]{36}$")
        init = fake.calls()[0][1]
        self.assertRegex(init["clientInfo"]["name"], r"^[a-z][a-z0-9_]*$")

    def test_a_name_that_is_right_is_not_renamed(self) -> None:
        fake = FakeMuse(self, FAKE_NAME="aa: fix login", FAKE_ORIGIN="custom")
        host = fake.host()
        try:
            result = MR.rename_session(host, SID, "AA", "AA: Fix Login", ["aa: fix login"])
        finally:
            host.close()
        self.assertEqual(result, {"name": "aa: fix login", "set": False, "hand": False})
        self.assertNotIn("session/rename", fake.methods())
        self.assertEqual(fake.methods()[-1], "session/leave")

    def test_a_name_given_by_hand_goes_over_the_wire_with_all_its_words(self) -> None:
        words = "q4 launch plan for the new pricing tiers and the billing migration schedule"
        fake = FakeMuse(self, FAKE_NAME=words, FAKE_ORIGIN="custom")
        host = fake.host()
        try:
            result = MR.rename_session(host, SID, "AA", "AA: Fix Pricing", [])
        finally:
            host.close()
        self.assertEqual(fake.calls()[4][1]["name"], "AA: " + words)
        self.assertEqual(result, {"name": "aa: " + words, "set": True, "hand": True})

    def test_the_names_wrapped_around_someones_words_are_passed_in(self) -> None:
        fake = FakeMuse(self, FAKE_NAME="aa: q4 launch plan", FAKE_ORIGIN="custom")
        host = fake.host()
        try:
            result = MR.rename_session(host, SID, "AA", "AA: Fix Login Page", ["aa: fix login page"], ["aa: q4 launch plan"])
        finally:
            host.close()
        self.assertEqual(result, {"name": "aa: q4 launch plan", "set": False, "hand": False})
        self.assertNotIn("session/rename", fake.methods())

    def test_the_recorded_provider_is_read_without_resuming(self) -> None:
        cases = [
            ({"FAKE_PROVIDER": "meta", "FAKE_MODEL": "muse-wonder-1.0"}, ("meta", "muse-wonder-1.0")),
            ({"FAKE_PROVIDER": "muse", "FAKE_MODEL": ""}, ("meta", "")), # what a default `muse serve` host recorded
            ({"FAKE_PROVIDER": "local", "FAKE_MODEL": "qwen3:8b"}, ("local", "qwen3:8b")),
            ({"FAKE_PROVIDER": "echo", "FAKE_MODEL": "ignored"}, ("echo", "")),
            ({"FAKE_PROVIDER": "", "FAKE_MODEL": ""}, ("echo", "")),
        ]
        for env, want in cases:
            fake = FakeMuse(self, FAKE_NAME="x", FAKE_ORIGIN="custom", **env)
            host = fake.host()
            try:
                self.assertEqual(MR.read_stored_provider(host, SID), want, env)
            finally:
                host.close()
            self.assertEqual(fake.methods(), ["initialize", "initialized", "session/read"], env)  # no lease taken, nothing left

    def test_a_provider_without_a_name_or_a_strange_model_is_a_final_refusal(self) -> None:
        for env in ({"FAKE_PROVIDER": "mystery"}, {"FAKE_PROVIDER": "meta", "FAKE_MODEL": "--disable-sandbox"},
                    {"FAKE_PROVIDER": "meta", "FAKE_MODEL": "a b"}):
            fake = FakeMuse(self, FAKE_NAME="x", FAKE_ORIGIN="custom", **env)
            host = fake.host()
            try:
                with self.assertRaises(MR.Refused) as ctx:
                    MR.read_stored_provider(host, SID)
            finally:
                host.close()
            self.assertEqual(ctx.exception.reason, "unsupported_provider", env)
            self.assertTrue(ctx.exception.final)

    def test_a_session_still_held_is_refused_as_in_use(self) -> None:
        fake = FakeMuse(self, FAKE_MODE="in_use")
        host = fake.host()
        try:
            with self.assertRaises(MR.Refused) as ctx:
                MR.rename_session(host, SID, "AA", "AA: Fix", [])
        finally:
            host.close()
        self.assertTrue(ctx.exception.in_use)
        self.assertFalse(ctx.exception.final)
        self.assertNotIn("session/leave", fake.methods())  # it never held the session

    def test_a_rejected_name_is_final(self) -> None:
        fake = FakeMuse(self, FAKE_MODE="invalid", FAKE_NAME="", FAKE_ORIGIN="")
        host = fake.host()
        try:
            with self.assertRaises(MR.Refused) as ctx:
                MR.rename_session(host, SID, "AA", "AA: Fix", [])
        finally:
            host.close()
        self.assertEqual(ctx.exception.reason, "name_invalid")
        self.assertTrue(ctx.exception.final)
        self.assertEqual(fake.methods()[-1], "session/leave")  # it left the session it had resumed

    def test_a_host_that_goes_quiet_or_exits_is_a_host_error(self) -> None:
        for mode in ("silent", "exit"):
            fake = FakeMuse(self, FAKE_MODE=mode)
            host = fake.host(timeout=0.6)
            try:
                with self.assertRaises(MR.HostError):
                    MR.rename_session(host, SID, "AA", "AA: Fix", [])
            finally:
                host.close()

    def test_a_missing_program_is_a_host_error(self) -> None:
        with self.assertRaises(MR.HostError):
            MR.MspHost(["/no/such/muse"], {}, "/")

    def test_the_default_host_runs_the_provider_and_model_the_session_was_recorded_with(self) -> None:
        # A host on another provider writes its own into the session on resume, and `muse resume` keeps it: a chat
        # renamed through an echo host came back on the echo provider.  So the helper's host matches the record.
        hosts: list = []

        class Capture:
            recorded: dict = {}

            def __init__(self, argv, env, cwd, **kw):
                self.argv, self.env, self.cwd, self.closed, self.calls = argv, env, cwd, False, []
                hosts.append(self)

            def request(self, method, params):
                self.calls.append(method)
                return {"session": dict(Capture.recorded)} if method == "session/read" else {}

            def notify(self, method):
                self.calls.append(method)

            def close(self):
                self.closed = True

        rec = {"session_id": SID, "exe": sys.executable, "home": "/h", "cwd": "/no/such/dir", "xdg_data_home": "/x"}
        base = {"HOME": "/other", "PATH": "/bin", "GITHUB_TOKEN": "t" * 10, "AGENT_SEAT": "CLAUDE"}
        inert = ["--disable-shell", "--disable-write"]

        def make() -> tuple:
            hosts.clear()
            with mock.patch.object(MR, "MspHost", Capture):
                host = MR.default_host(rec, base)
            self.assertEqual(len(hosts), 2)
            self.assertIs(host, hosts[1])
            return hosts[0], hosts[1]

        Capture.recorded = {"providerId": "meta", "modelId": "muse-wonder-1.0"}
        probe, real = make()
        self.assertEqual(probe.argv, [sys.executable, "serve", "--provider", "echo", *inert])
        self.assertEqual(probe.calls, ["initialize", "initialized", "session/read"])
        self.assertTrue(probe.closed)
        self.assertFalse(real.closed)
        self.assertEqual(real.argv, [sys.executable, "serve", "--provider", "meta", "--model=muse-wonder-1.0", *inert])
        for host in (probe, real):
            self.assertEqual(host.cwd, "/")
            self.assertEqual(host.env["HOME"], "/h")
            self.assertEqual(host.env[MR.HELPER_MARK], "1")
            self.assertEqual(host.env["XDG_DATA_HOME"], "/x")
            self.assertNotIn("GITHUB_TOKEN", host.env)
            self.assertNotIn("AGENT_SEAT", host.env)
        Capture.recorded = {"providerId": "muse", "modelId": None}  # a session Muse's own default host started
        self.assertEqual(make()[1].argv, [sys.executable, "serve", "--provider", "meta", *inert])
        Capture.recorded = {"providerId": "echo", "modelId": None}
        self.assertEqual(make()[1].argv, [sys.executable, "serve", "--provider", "echo", *inert])
        Capture.recorded = {"providerId": "mystery"}
        hosts.clear()
        with mock.patch.object(MR, "MspHost", Capture), self.assertRaises(MR.Refused) as ctx:
            MR.default_host(rec, base)
        self.assertTrue(ctx.exception.final)
        self.assertEqual(len(hosts), 1)
        self.assertTrue(hosts[0].closed)  # the probe is closed even when its answer is refused


class WorkerTests(Tree):
    """`run_worker` with a fake clock, a fake holder and a fake host."""

    def setUp(self) -> None:
        super().setUp()
        self.spool = os.path.join(self.state, "MC", "renames", SID + ".json")
        self.sleeps: list[float] = []
        self.clock = [1000.0]
        self.calls: list[tuple] = []
        self.results: list = []

    def record(self, **over) -> dict:
        rec = dict(session_id=SID, name="AA: Fix Login", label="AA", applied=[], cwd="/", home=self.home, exe="",
                   xdg_data_home="", holder_pid=0, queued_at=self.clock[0], state="queued", attempts=0, error="", worker_pid=0)
        rec.update(over)
        self.assertTrue(MR.write_record(self.spool, rec))
        return rec

    def read(self) -> dict:
        return MR.read_record(self.spool)

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.clock[0] += secs
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))

    on_sleep = None

    def factory(self, rec):
        host = SimpleNamespace(close=lambda: None)
        self.calls.append(("host", rec["name"]))
        return host

    def run_it(self, **kw) -> str:
        def rename_session(host, sid, label, want, applied, wrapped=()):
            result = self.results.pop(0) if self.results else {"name": want.lower(), "set": True}
            if isinstance(result, Exception):
                raise result
            self.calls.append(("rename", want))
            return result

        with mock.patch.object(MR, "rename_session", rename_session):
            return MR.run_worker(self.spool, now=lambda: self.clock[0], sleep=self.sleep, host_factory=self.factory,
                                 proc_root=self.proc, pid=555, **kw)

    def test_it_waits_for_the_holder_then_renames(self) -> None:
        self.store(SID, lock="pid=42\nhost=h\n")
        fake_proc(self.proc, 42, argv=("muse",))
        self.record()

        def holder_exits(n: int) -> None:
            if n == 3:
                Path(self.proc, "42", "stat").unlink()  # the process is gone

        self.on_sleep = holder_exits
        self.assertEqual(self.run_it(), "done")
        self.assertEqual(self.sleeps, [MR.LEASE_POLL] * 3)
        self.assertEqual([c for c in self.calls if c[0] == "rename"], [("rename", "AA: Fix Login")])
        rec = self.read()
        self.assertEqual((rec["state"], rec["worker_pid"], rec["applied"]), ("done", 0, ["aa: fix login"]))
        self.assertEqual(rec["result"], {"name": "aa: fix login", "set": True})

    def test_an_empty_lock_means_no_wait(self) -> None:
        self.store(SID, lock="")
        self.record()
        self.assertEqual(self.run_it(), "done")
        self.assertEqual(self.sleeps, [])

    def test_it_asks_again_while_the_host_says_in_use_and_then_gives_up(self) -> None:
        self.store(SID, lock="")
        self.record()
        self.results = [MR.Refused(-32021, "sessionInUse", "", "in use")] * 3 + [{"name": "aa: fix login", "set": True}]
        self.assertEqual(self.run_it(), "done")
        self.assertEqual(len(self.sleeps), 3)
        self.record(state="queued")
        self.results = [MR.Refused(-32021, "sessionInUse", "", "in use")] * (MR.MAX_BUSY + 2)
        self.sleeps.clear()
        self.assertEqual(self.run_it(), "failed")
        self.assertIn("in use", self.read()["error"])

    def test_a_final_refusal_fails_at_once(self) -> None:
        self.store(SID, lock="")
        self.record()
        self.results = [MR.Refused(-32030, "commandRejected", "name_invalid", "rejected")]
        self.assertEqual(self.run_it(), "failed")
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.read()["state"], "failed")
        self.assertIn("name_invalid", self.read()["error"])

    def test_a_host_that_keeps_failing_fails_after_the_attempts(self) -> None:
        self.store(SID, lock="")
        self.record()
        self.results = [MR.HostError("down")] * 10
        self.assertEqual(self.run_it(), "failed")
        self.assertEqual(self.read()["attempts"], MR.ATTEMPTS)
        self.assertEqual(self.sleeps, [MR.RETRY_PAUSE] * (MR.ATTEMPTS - 1))

    def test_a_host_that_recovers_is_used(self) -> None:
        self.store(SID, lock="")
        self.record()
        self.results = [MR.HostError("down"), {"name": "aa: fix login", "set": True}]
        self.assertEqual(self.run_it(), "done")
        self.assertEqual(self.read()["attempts"], 1)

    def test_a_title_queued_while_it_works_is_applied_too(self) -> None:
        self.store(SID, lock="")
        self.record(name="AA: New Chat")
        queued = {"done": False}

        def rename_session(host, sid, label, want, applied, wrapped=()):
            if not queued["done"]:
                queued["done"] = True
                MR.write_record(self.spool, dict(self.read(), name="AA: Fix Login"))  # the hook queues a better title
            self.calls.append(("rename", want))
            return {"name": want.lower(), "set": True}

        with mock.patch.object(MR, "rename_session", rename_session):
            state = MR.run_worker(self.spool, now=lambda: self.clock[0], sleep=self.sleep, host_factory=self.factory,
                                  proc_root=self.proc, pid=555)
        self.assertEqual(state, "done")
        self.assertEqual([c[1] for c in self.calls if c[0] == "rename"], ["AA: New Chat", "AA: Fix Login"])
        self.assertEqual(self.read()["applied"], ["aa: new chat", "aa: fix login"])

    def test_a_name_set_around_someones_words_is_filed_apart_from_the_automatic_ones(self) -> None:
        self.store(SID, lock="")
        self.record(applied=["aa: new chat"], wrapped=["aa: older words"])
        self.results = [{"name": "aa: q4 launch plan", "set": True, "hand": True}]
        self.assertEqual(self.run_it(), "done")
        rec = self.read()
        self.assertEqual((rec["applied"], rec["wrapped"]), (["aa: new chat"], ["aa: older words", "aa: q4 launch plan"]))
        self.record(applied=[], wrapped=[])
        self.results = [{"name": "aa: fix login", "set": True, "hand": False}, ]
        self.assertEqual(self.run_it(), "done")
        self.assertEqual((self.read()["applied"], self.read()["wrapped"]), (["aa: fix login"], []))

    def test_the_worker_hands_the_wrapped_names_to_the_rename(self) -> None:
        self.store(SID, lock="")
        self.record(applied=["aa: fix"], wrapped=["aa: q4 launch plan"])
        seen = {}

        def rename_session(host, sid, label, want, applied, wrapped=()):
            seen.update(applied=list(applied), wrapped=list(wrapped))
            return {"name": "aa: q4 launch plan", "set": False, "hand": False}

        with mock.patch.object(MR, "rename_session", rename_session):
            MR.run_worker(self.spool, now=lambda: self.clock[0], sleep=self.sleep, host_factory=self.factory,
                          proc_root=self.proc, pid=555)
        self.assertEqual(seen, {"applied": ["aa: fix"], "wrapped": ["aa: q4 launch plan"]})
        self.assertEqual(self.read()["wrapped"], ["aa: q4 launch plan"])  # kept, so the next run still knows

    def test_a_record_that_is_gone_finished_or_taken_is_left_alone(self) -> None:
        self.assertEqual(self.run_it(), "stale")
        self.record(state="done")
        self.assertEqual(self.run_it(), "stale")
        self.record(worker_pid=77)
        fake_proc(self.proc, 77, argv=(sys.executable, MR.__file__, "run", "--spool", self.spool))
        self.assertEqual(self.run_it(), "stale")
        self.assertEqual(self.read()["worker_pid"], 77)

    def test_a_chat_still_open_after_a_week_is_given_up(self) -> None:
        self.store(SID, lock="pid=42\nhost=h\n")
        fake_proc(self.proc, 42, argv=("muse",))
        self.record()
        self.on_sleep = lambda n: self.clock.__setitem__(0, self.clock[0] + MR.WAIT_MAX) if n == 1 else None
        self.assertEqual(self.run_it(), "failed")
        self.assertIn("week", self.read()["error"])

    def test_a_job_with_no_session_folder_uses_the_recorded_holder(self) -> None:
        fake_proc(self.proc, 42, argv=("muse",))
        self.record(holder_pid=42)
        self.on_sleep = lambda n: Path(self.proc, "42", "stat").unlink() if n == 2 else None
        self.assertEqual(self.run_it(), "done")
        self.assertEqual(len(self.sleeps), 2)

    def test_the_script_runs_as_a_program_and_leaves_a_missing_job_alone(self) -> None:
        proc = subprocess.run([sys.executable, str(SCRIPTS / "agent_sync" / "muse_session_rename.py"), "run", "--spool", self.spool],
                              capture_output=True, text=True, timeout=30, cwd="/")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))  # stderr is not checked:  a host's tracer may write to it
        bad = subprocess.run([sys.executable, str(SCRIPTS / "agent_sync" / "muse_session_rename.py"), "nope"],
                             capture_output=True, text=True, timeout=30, cwd="/")
        self.assertEqual(bad.returncode, 2)

    def test_start_worker_detaches_the_helper(self) -> None:
        seen = {}

        def popen(argv, **kw):
            seen.update(argv=argv, **kw)
            return SimpleNamespace(pid=4321)

        pid = MR.start_worker(self.spool, self.home, {"PATH": "/bin", "AGENT_SYNC_STATE_DIR": self.state, "GITHUB_TOKEN": "t"}, popen)
        self.assertEqual(pid, 4321)
        self.assertEqual(seen["argv"][1:], [os.path.abspath(MR.__file__), "run", "--spool", self.spool])
        self.assertTrue(seen["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(seen[stream], subprocess.DEVNULL)
        self.assertEqual(seen["env"], {"HOME": self.home, "PATH": "/bin", "LANG": "C.UTF-8", "AGENT_SYNC_STATE_DIR": self.state})

        def broken(argv, **kw):
            raise OSError("no")

        self.assertEqual(MR.start_worker(self.spool, self.home, {}, broken), 0)

    def test_the_record_is_private_and_atomic(self) -> None:
        self.record()
        self.assertEqual(stat.S_IMODE(os.stat(self.spool).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.spool)).st_mode), 0o700)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.spool)) if n.endswith(".tmp")], [])
        self.assertEqual(MR.read_record(os.path.join(self.tmp.name, "missing.json")), {})


class WriterTests(Tree):
    def request(self, title: str = "AA: New Chat", **data) -> PL.TitleRequest:
        return PL.TitleRequest(platform="muse", session_id=SID, label="AA", title=title, words=PL.bare(title, ("AA",)),
                               current="", reason="start", data=dict({"cwd": "/work"}, **data), env=self.env())

    def setUp(self) -> None:
        super().setUp()
        self.started: list[tuple] = []
        self.next_pid = 4242
        self.muse("--provider", "echo")

        def start(spool, home, base, popen=None):
            self.started.append((spool, home))
            return self.next_pid

        for patch in (mock.patch.object(MR, "start_worker", start),
                      mock.patch.object(MR, "worker_alive", lambda rec, root=None: bool(rec.get("worker_pid")) and rec["worker_pid"] == 4242)):
            patch.start()
            self.addCleanup(patch.stop)

    def spool(self) -> dict:
        return MR.read_record(PLM.spool_path(self.env(), SID))

    def test_the_title_is_queued_and_a_helper_started(self) -> None:
        self.assertTrue(PLM.muse_rename(self.request()))
        rec = self.spool()
        self.assertEqual((rec["name"], rec["label"], rec["session_id"], rec["state"]), ("AA: New Chat", "AA", SID, "queued"))
        self.assertEqual((rec["cwd"], rec["home"], rec["holder_pid"], rec["worker_pid"]), ("/work", self.home, self.muse_pid, 4242))
        self.assertEqual(rec["exe"], "")  # the fake exe is not a real file
        self.assertEqual(self.started, [(PLM.spool_path(self.env(), SID), self.home)])
        self.assertEqual(os.path.dirname(PLM.spool_path(self.env(), SID)), os.path.join(self.state, "MC", "renames"))

    def test_a_newer_title_updates_the_record_and_starts_no_second_helper(self) -> None:
        PLM.muse_rename(self.request())
        self.assertTrue(PLM.muse_rename(self.request("AA: Fix Login")))
        self.assertEqual(self.spool()["name"], "AA: Fix Login")
        self.assertEqual(len(self.started), 1)

    def test_a_title_the_chat_already_carries_is_not_queued_again(self) -> None:
        PLM.muse_rename(self.request("AA: Fix Login"))
        MR.write_record(PLM.spool_path(self.env(), SID), dict(self.spool(), state="done", worker_pid=0, applied=["aa: fix login"]))
        self.started.clear()
        self.assertTrue(PLM.muse_rename(self.request("AA: Fix Login")))
        self.assertEqual(self.started, [])
        self.assertEqual(self.spool()["state"], "done")
        self.assertTrue(PLM.muse_rename(self.request("AA: Another Title")))  # a different title is queued
        self.assertEqual((self.spool()["state"], len(self.started)), ("queued", 1))

    def test_the_record_is_written_before_the_helper_starts(self) -> None:
        # A helper that read the file before it was written would find no job (or the last one's, done), leave as
        # `stale`, and the hook would still say the title was queued.
        seen = []

        def start(spool, home, base, popen=None):
            seen.append(MR.read_record(spool))
            return 4242

        with mock.patch.object(MR, "start_worker", start):
            self.assertTrue(PLM.muse_rename(self.request()))
            MR.write_record(PLM.spool_path(self.env(), SID), dict(self.spool(), state="done", worker_pid=0, name="AA: Old"))
            self.assertTrue(PLM.muse_rename(self.request("AA: Fix Login")))  # a resume with a new title
        self.assertEqual([(r.get("name"), r.get("state"), r.get("worker_pid")) for r in seen],
                         [("AA: New Chat", "queued", 0), ("AA: Fix Login", "queued", 0)])
        self.assertEqual(self.spool()["worker_pid"], 4242)

    def test_a_helper_that_has_already_claimed_or_finished_the_job_is_not_overwritten(self) -> None:
        spool = PLM.spool_path(self.env(), SID)

        def finished(path, home, base, popen=None):
            MR.write_record(path, dict(MR.read_record(path), state="done", worker_pid=0, done_at=1.0))
            return 4242

        with mock.patch.object(MR, "start_worker", finished):
            self.assertTrue(PLM.muse_rename(self.request()))
        self.assertEqual((self.spool()["state"], self.spool()["worker_pid"]), ("done", 0))

        def claimed(path, home, base, popen=None):
            MR.write_record(path, dict(MR.read_record(path), state="waiting", worker_pid=777))
            return 777

        with mock.patch.object(MR, "start_worker", claimed):
            self.assertTrue(PLM.muse_rename(self.request("AA: Fix Login")))
        self.assertEqual((MR.read_record(spool)["state"], MR.read_record(spool)["worker_pid"]), ("waiting", 777))

    def test_a_record_that_cannot_be_saved_starts_no_helper(self) -> None:
        with mock.patch.object(MR, "write_record", lambda path, rec: False):
            self.assertFalse(PLM.muse_rename(self.request()))
        self.assertEqual(self.started, [])

    def test_a_helper_that_cannot_start_leaves_the_title_pending(self) -> None:
        self.next_pid = 0
        self.assertFalse(PLM.muse_rename(self.request()))
        self.assertEqual(self.spool()["name"], "AA: New Chat")  # queued all the same, for a later revive

    def test_the_cli_program_is_the_chats_own_muse(self) -> None:
        exe = os.path.join(self.tmp.name, "muse-bin-1.4.14")
        Path(exe).write_text("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        self.muse("--provider", "echo", exe=exe)
        PLM.muse_rename(self.request())
        self.assertEqual(self.spool()["exe"], exe)
        launcher = os.path.join(self.home, ".local", "bin", "muse")
        os.makedirs(os.path.dirname(launcher))
        Path(launcher).write_text("#!/bin/sh\n")
        os.chmod(launcher, 0o755)
        fake_proc(self.proc, self.muse_pid, ppid=50, argv=("/Applications/Muse.app/Contents/MacOS/Muse",), exe="/Applications/Muse.app/Contents/MacOS/Muse")
        self.assertEqual(PLM._cli(MR.muse_ancestor(self.muse_pid, self.proc), self.home, self.env()), launcher)

    def test_a_job_whose_helper_is_gone_is_revived(self) -> None:
        PLM.muse_rename(self.request())
        other = os.path.join(PLM.spool_dir(self.env()), "other-session-0001.json")
        MR.write_record(other, dict(session_id="other-session-0001", name="AB: Other", label="AB", home=self.home, state="waiting",
                                    queued_at=time.time(), worker_pid=9999))
        done = os.path.join(PLM.spool_dir(self.env()), "done-session-0001.json")
        MR.write_record(done, dict(session_id="done-session-0001", name="AC: Done", state="done", queued_at=time.time(), worker_pid=0))
        old = os.path.join(PLM.spool_dir(self.env()), "old-session-0001.json")
        MR.write_record(old, dict(session_id="old-session-0001", name="AD: Old", state="queued", queued_at=time.time() - MR.WAIT_MAX - 5))
        self.started.clear()
        self.assertEqual(PLM.revive(self.env(), self.home, skip=SID), 1)
        self.assertEqual(self.started, [(other, self.home)])
        self.assertEqual(MR.read_record(other)["worker_pid"], 4242)

    def test_a_hook_scans_the_spool_at_most_once_in_the_gap(self) -> None:
        PLM.muse_rename(self.request())  # queues the title and scans the spool once, which makes the stamp
        stamp = os.path.join(PLM.spool_dir(self.env()), ".revived")
        self.assertTrue(os.path.exists(stamp))
        later = os.path.join(PLM.spool_dir(self.env()), "later-session-0002.json")
        MR.write_record(later, dict(session_id="later-session-0002", name="AC: Later", label="AC", home=self.home,
                                    state="waiting", queued_at=time.time(), worker_pid=9998))
        self.started.clear()
        self.assertEqual(PLM.revive(self.env(), self.home, skip=SID, min_gap=PLM.REVIVE_EVERY), 0, "scanned a moment ago")
        self.assertEqual(self.started, [], "a job whose helper is gone waits for the next scan")
        long_ago = time.time() - PLM.REVIVE_EVERY - 5
        os.utime(stamp, (long_ago, long_ago))
        PLM.revive(self.env(), self.home, skip=SID, min_gap=PLM.REVIVE_EVERY)
        self.assertIn(later, [spool for spool, _ in self.started], "the gap is over: the scan runs again")

    def test_a_session_id_that_is_not_a_muse_one_makes_no_spool_path(self) -> None:
        for bad in ("../../x", "a/b/c/d/e/f/g", "..", "", "x" * 80, "short"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                PLM.spool_path(self.env(), bad)
        escaping = PL.TitleRequest(platform="muse", session_id="../../escape-session", label="AA", title="AA: New Chat",
                                   words="New Chat", current="", reason="start", data={"cwd": "/work"}, env=self.env())
        self.assertIs(PLM.muse_rename(escaping), False)
        self.assertFalse(os.path.exists(os.path.join(PLM.spool_dir(self.env()), "..", "..", "escape-session.json")))
        self.assertEqual(PLM.spool_path(self.env(), SID), os.path.join(PLM.spool_dir(self.env()), SID + ".json"))


class FakeStore:
    """The Muse side of one stored session, as the helper meets it: read, rename (stored in lower case, as the runtime
    settles it) and the calls that change nothing."""

    def __init__(self, name: str = "", origin: str = "") -> None:
        self.name, self.origin, self.calls = name, origin, []

    def request(self, method: str, params: dict) -> dict:
        self.calls.append(method)
        if method == "session/read":
            return {"session": {"name": self.name, "nameOrigin": self.origin}}
        if method == "session/rename":
            self.name, self.origin = params["name"].lower(), "custom"
            return {"name": self.name}
        return {}

    def notify(self, method: str) -> None:
        self.calls.append(method)

    def close(self) -> None:
        pass


class HandNamedTests(Tree):
    """The whole round trip for a chat the owner renamed by hand: the hook's title writer queues, the helper renames
    once the chat has exited, and a resume queues the old automatic title again."""

    def setUp(self) -> None:
        super().setUp()
        self.muse("--provider", "echo")
        self.store(SID, lock="")  # no holder: the helper need not wait
        self.session = FakeStore("glowing-halo", "automatic")
        for patch in (mock.patch.object(MR, "start_worker", lambda spool, home, base, popen=None: 4242),
                      mock.patch.object(MR, "worker_alive", lambda rec, root=None: rec.get("worker_pid") == 4242)):
            patch.start()
            self.addCleanup(patch.stop)

    def queue(self, title: str, label: str = "AA") -> bool:
        req = PL.TitleRequest(platform="muse", session_id=SID, label=label, title=title, words=PL.bare(title, (label,)),
                              current="", reason="start", data={"cwd": "/work"}, env=self.env())
        return PLM.muse_rename(req)

    def spool(self) -> dict:
        return MR.read_record(PLM.spool_path(self.env(), SID))

    def exit_chat(self) -> str:
        """The chat lets go and the helper runs (its pid is the one the hook recorded)."""
        return MR.run_worker(PLM.spool_path(self.env(), SID), now=time.time, sleep=lambda s: None,
                             host_factory=lambda rec: self.session, proc_root=self.proc, pid=4242)

    def test_a_name_given_by_hand_keeps_its_words_through_a_resume_that_queues_the_old_title(self) -> None:
        self.queue("AA: New Chat")
        self.queue("AA: Fix Login Page")
        self.session.name, self.session.origin = "Q4 launch plan", "custom"  # the owner renamed the chat mid-chat
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "aa: q4 launch plan")
        self.assertEqual((self.spool()["applied"], self.spool()["wrapped"]), ([], ["aa: q4 launch plan"]))
        # `muse resume`: the SessionStart hook queues the title it has in the note, which the owner has replaced.
        self.assertTrue(self.queue("AA: Fix Login Page"))
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "aa: q4 launch plan")  # not "aa: fix login page"
        self.assertEqual(self.session.calls.count("session/rename"), 1)

    def test_a_hand_given_name_replaces_an_automatic_title_the_helper_applied_and_stays(self) -> None:
        self.queue("AA: Fix Login Page")
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "aa: fix login page")
        self.session.name = "Q4 launch plan"  # renamed by hand while the chat was resumed
        self.assertTrue(self.queue("AA: Another Title"))
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "aa: q4 launch plan")
        # The old automatic title is in `applied` still.  Queued again by a later resume, it must not take the name back.
        self.assertEqual(self.spool()["applied"], ["aa: fix login page"])
        self.assertTrue(self.queue("AA: Fix Login Page"))
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "aa: q4 launch plan")

    def test_a_letter_that_changes_is_put_in_place_of_the_old_one_around_the_same_words(self) -> None:
        self.queue("AA: Fix Login Page")
        self.session.name, self.session.origin = "Q4 launch plan", "custom"
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "aa: q4 launch plan")
        self.assertTrue(self.queue("AB: Fix Login Page", label="AB"))  # the Operator reissued the chat a new pair
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "ab: q4 launch plan")  # not "ab: aa: q4 launch plan"

    def test_a_long_hand_given_name_is_kept_whole(self) -> None:
        words = "q4 launch plan for the new pricing tiers and the billing migration schedule"
        self.queue("AA: Fix Pricing")
        self.session.name, self.session.origin = words, "custom"
        self.assertEqual(self.exit_chat(), "done")
        self.assertEqual(self.session.name, "aa: " + words)


class FakeTransport:
    """The registry side as a function of the argv the hook sends, with a record of every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.labels = iter(["AA", "AB", "AC", "AD"])
        self.by_sid: dict[str, str] = {}

    def __call__(self, argv: list[str]) -> dict:
        self.calls.append(argv)
        sid = argv[argv.index("--session-id") + 1]
        if argv[0] == "register":
            label = self.by_sid.setdefault(sid, next(self.labels))
            return {"outcome": "registered", "label": label, "generation": 1, "record_id": "r-" + label}
        return {"ok": True, "label": self.by_sid.get(sid, "AA"), "generation": 1}

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]


class HookFlowTests(Tree):
    def setUp(self) -> None:
        super().setUp()
        self.muse("--provider", "echo", env={"AGENT_SEAT": "MC"})
        self.started: list[str] = []
        self.transport = FakeTransport()

        def start(spool, home, base, popen=None):
            self.started.append(spool)
            return 4242

        for patch in (mock.patch.object(MR, "start_worker", start),
                      mock.patch.object(MR, "worker_alive", lambda rec, root=None: rec.get("worker_pid") == 4242)):
            patch.start()
            self.addCleanup(patch.stop)

    def hook(self, payload: dict, *, env: dict | None = None) -> dict | None:
        out = io.StringIO()
        base = dict(self.env(), SESSION_LETTER_LOCATION="server", SESSION_LETTER_REMOTE="mac")
        code = PL.hook(PLM.SPEC, env or base, io.StringIO(json.dumps(payload)), out, transport=self.transport, platform="linux")
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def start(self, sid: str = SID, source: str = "startup") -> dict | None:
        return self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": source, "cwd": "/work"})

    def prompt(self, text: str, sid: str = SID) -> dict | None:
        return self.hook({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text, "cwd": "/work"})

    def spool(self, sid: str = SID) -> dict:
        return MR.read_record(PLM.spool_path(self.env(), sid))

    def note(self, sid: str = SID) -> dict:
        return json.loads(Path(PL.note_path(self.env(), "MC", sid)).read_text())

    def test_a_chat_gets_a_letter_then_a_title_and_the_model_is_told(self) -> None:
        out = self.start()
        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertNotIn("sessionTitle", out)
        self.assertIn("Muse Code AA", out["additionalContext"])
        self.assertIn('"@**Muse Code** AA"', out["additionalContext"])
        register = self.transport.calls[0]
        for flag, value in (("--platform", "muse"), ("--seat", "MC"), ("--source", "muse-letter-hook"), ("--location", "server")):
            self.assertEqual(register[register.index(flag) + 1], value)
        self.assertNotIn("--parent-session-id", " ".join(register))
        self.assertEqual(self.spool()["name"], "AA: New Chat")
        self.assertEqual(len(self.started), 1)
        self.prompt("fix the login page redirect please")
        self.assertEqual(self.spool()["name"], "AA: Fix the Login Page Redirect Please")
        self.assertEqual(self.transport.ops(), ["register", "title"])
        self.assertEqual(len(self.started), 1)  # the helper that waits for the chat is the one that applies it
        self.prompt("and another thing")
        self.assertEqual(self.transport.ops(), ["register", "title"])  # steady state costs no hop
        self.assertEqual(self.note()["label"], "AA")

    def test_nothing_is_closed_when_the_chat_ends(self) -> None:
        self.start()
        self.hook({"hook_event_name": "SessionEnd", "session_id": SID, "reason": "other"})
        self.assertEqual(self.transport.ops(), ["register"])
        self.assertEqual(self.note()["state"], "open")
        self.assertEqual(self.note()["ended"]["reason"], "other")

    def test_a_headless_run_a_launcher_run_and_an_opt_out_leave_no_trace(self) -> None:
        for words, env in ((("exec", "hi"), {}), (("serve",), {}), ((), {"AGENT_LAUNCHER": "botfleet"}), ((), {"MUSE_LETTER": "0"}),
                           ((), {MR.HELPER_MARK: "1"})):
            self.muse(*words, env=env)
            self.assertIsNone(self.start())
            self.assertIsNone(self.prompt("hello there"))
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "MC")))
        self.assertEqual(self.started, [])

    def test_a_fixture_piped_in_by_hand_registers_nothing(self) -> None:
        fake_proc(self.proc, self.muse_pid, ppid=1, argv=("python3", "fixture-runner"))
        self.assertIsNone(self.start())
        self.assertEqual(self.transport.calls, [])

    def test_a_resumed_chat_keeps_its_letter_and_its_title(self) -> None:
        self.start()
        self.prompt("fix the login page")
        MR.write_record(PLM.spool_path(self.env(), SID), dict(self.spool(), state="done", worker_pid=0, applied=["aa: fix the login page"]))
        self.started.clear()
        out = self.start(source="resume")
        self.assertIn("Muse Code AA", out["additionalContext"])
        self.assertEqual(self.transport.calls[-1][0], "register")
        self.assertIn("--title=Fix the Login Page", self.transport.calls[-1])
        self.assertEqual(self.started, [])  # the name is already stored:  nothing is queued
        self.assertEqual(self.spool()["state"], "done")

    def test_a_fork_asks_for_the_pair_after_its_parent_and_keeps_the_words(self) -> None:
        self.start()
        self.prompt("fix the login page")
        self.store(CHILD_SID, {"record_type": "session.fork.created", "payload": {"source_session_id": SID}})
        out = self.start(sid=CHILD_SID, source="fork")
        register = self.transport.calls[-1]
        self.assertIn("--parent-session-id=" + SID, register)
        self.assertIn("--title=Fix the Login Page", register)
        self.assertIn("Muse Code AB", out["additionalContext"])
        self.assertEqual(self.spool(CHILD_SID)["name"], "AB: Fix the Login Page")
        self.assertEqual(self.note(CHILD_SID)["parent"], SID)

    def test_the_end_of_a_known_chat_is_noted_even_when_muse_is_already_gone(self) -> None:
        self.start()
        self.muse_pid = 999  # no such process
        self.hook({"hook_event_name": "SessionEnd", "session_id": SID, "reason": "other"})
        self.assertEqual(self.note()["ended"]["reason"], "other")


class RegistryTests(Tree):
    """The hook against a real temporary registry (the Mac's in process), main's or the pair-aware one."""

    def test_a_chat_and_its_fork_are_registered_as_muse_chats_of_seat_mc(self) -> None:
        db = OR.registry_db_path(self.state, {})
        os.makedirs(os.path.dirname(db))
        OR.SessionRegistry(db).close_db()
        self.muse("--provider", "echo")
        env = self.env()
        started = []
        with mock.patch.object(MR, "start_worker", lambda *a, **k: started.append(a) or 4242), \
                mock.patch.object(MR, "worker_alive", lambda rec, root=None: rec.get("worker_pid") == 4242):
            for payload in (
                {"hook_event_name": "SessionStart", "session_id": SID, "source": "startup", "cwd": "/work"},
                {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "fix the login page", "cwd": "/work"},
            ):
                self.assertEqual(PL.hook(PLM.SPEC, env, io.StringIO(json.dumps(payload)), io.StringIO(), platform="darwin"), 0)
            self.store(CHILD_SID, {"record_type": "session.fork.created", "payload": {"source_session_id": SID}})
            payload = {"hook_event_name": "SessionStart", "session_id": CHILD_SID, "source": "fork", "cwd": "/work"}
            self.assertEqual(PL.hook(PLM.SPEC, env, io.StringIO(json.dumps(payload)), io.StringIO(), platform="darwin"), 0)
        reg = OR.SessionRegistry(db, read_only=True)
        try:
            parent = reg.lookup("muse", "mac", PL.SCOPE, SID)
            child = reg.lookup("muse", "mac", PL.SCOPE, CHILD_SID)
        finally:
            reg.close_db()
        for rec in (parent, child):
            self.assertRegex(rec["label"], r"^[A-Z]{2}$")
            self.assertEqual(rec["seat"], "MC")
        self.assertNotEqual(parent["label"], child["label"])
        self.assertEqual(parent["title"], "Fix the Login Page")
        want = MR.read_record(PLM.spool_path(env, CHILD_SID))["name"]
        self.assertEqual(want, "%s: Fix the Login Page" % child["label"])


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.env = {"HOME": self.home, "PATH": "/usr/bin:/bin"}
        self.manifest = PLM.manifest_path(self.home)
        patch = mock.patch.object(PLM, "_on_mac", lambda: True)  # as on the Mac: the hook needs no location flags
        patch.start()
        self.addCleanup(patch.stop)

    def install(self, **kw) -> tuple[int, dict]:
        out = io.StringIO()
        with mock.patch.object(PLM, "find_muse", lambda home, env: ""):
            code = PLM.install(PL.InstallOptions("muse", **kw), self.env, out)
        return code, json.loads(out.getvalue())

    def doc(self) -> dict:
        return json.loads(Path(self.manifest).read_text())

    def test_it_writes_a_bundle_in_the_shape_of_the_lane_guard(self) -> None:
        code, answer = self.install()
        self.assertEqual((code, answer["changed"], answer["platform"]), (0, ["bundle"], "muse"))
        doc = self.doc()
        self.assertEqual((doc["schemaVersion"], doc["name"], doc["compat"]), (1, "session-letter", {"source": "native", "manifestDir": ".muse-plugin"}))
        hooks = doc["capabilities"]["hooks"]
        self.assertEqual([(h["id"], h["event"]) for h in hooks],
                         [("letter-start", "SessionStart"), ("letter-prompt", "UserPromptSubmit"), ("letter-end", "SessionEnd")])
        self.assertEqual([h["timeoutMs"] for h in hooks], [20000, 15000, 15000])
        for h in hooks:
            self.assertNotIn("matcher", h)
            self.assertEqual(h["command"], [*PLM.GUARD, os.path.join(self.home, ".local", "bin", "platform-letter"), "hook", "--platform", "muse"])
        self.assertIn("statusMessage", hooks[0])
        self.assertNotIn("statusMessage", hooks[1])
        for key in ("skills", "commands", "mcpServers", "reminders"):
            self.assertEqual(doc["capabilities"][key], [])
        self.assertEqual(answer["steps"], ["muse plugins install %s --scope user" % PLM.bundle_dir(self.home), "muse plugins approve session-letter"])
        self.assertEqual(os.listdir(os.path.dirname(self.manifest)), ["plugin.json"])  # nothing else ships in the bundle

    def test_it_is_idempotent_and_keeps_the_location_flags(self) -> None:
        self.install(location="server", remote="mac")
        before = Path(self.manifest).read_bytes()
        code, answer = self.install()
        self.assertEqual((code, answer["changed"]), (0, []))
        self.assertEqual(Path(self.manifest).read_bytes(), before)
        command = self.doc()["capabilities"]["hooks"][0]["command"]
        self.assertEqual(command[-4:], ["--location", "server", "--remote", "mac"])
        self.assertEqual(os.listdir(self.tmp.name + "/home/.local/share/platform-letter/muse"), ["session-letter"])  # no backup yet

    def test_a_changed_bundle_is_backed_up_beside_it(self) -> None:
        self.install()
        code, answer = self.install(location="server", remote="mac")
        self.assertEqual((code, answer["changed"]), (0, ["bundle"]))
        backup = answer["backup"]
        self.assertTrue(os.path.basename(backup).startswith("session-letter.bak-letter-"))
        self.assertEqual(os.path.dirname(backup), os.path.dirname(PLM.bundle_dir(self.home)))
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
        self.assertNotIn("--location", json.loads(Path(backup).read_text())["capabilities"]["hooks"][0]["command"])
        self.assertEqual(os.listdir(os.path.dirname(self.manifest)), ["plugin.json"])

    def test_bad_flags_are_refused_before_anything_is_written(self) -> None:
        for kw in ({"remote": "-oProxyCommand=x"}, {"location": "cloud"}):
            code, answer = self.install(**kw)
            self.assertEqual(code, 2, kw)
            self.assertIn("error", answer)
        self.assertFalse(os.path.exists(self.manifest))

    def test_remove_takes_the_bundle_out_after_a_backup(self) -> None:
        self.install()
        code, answer = self.install(remove=True)
        self.assertEqual((code, answer["changed"], answer["steps"]), (0, ["bundle"], ["muse plugins remove session-letter"]))
        self.assertFalse(os.path.exists(PLM.bundle_dir(self.home)))
        self.assertTrue(os.path.exists(answer["backup"]))
        self.assertEqual(self.install(remove=True)[1]["changed"], [])

    HOOK_IDS = ("letter-start", "letter-prompt", "letter-end")

    def inspect_output(self, status: str = "trusted_enabled", cache: str | None = None, hooks: tuple = HOOK_IDS) -> str:
        """What `muse plugins inspect session-letter` prints: the plugin's line (with the cache folder) and a line per hook."""
        head = "session-letter\t1.0.0\tenabled=true active=true trust=user-local valid=true hooks=3"
        head += " cache=%s\n" % cache if cache else "\n"
        return head + "".join("runtime-capability\tplugin:session-letter:hook:%s\tstatus=%s\n" % (h, status) for h in hooks)

    def cache_copy(self, text: str | None = None) -> str:
        """A folder like the one Muse copies a bundle into on `plugins install` (package/.muse-plugin/plugin.json)."""
        package = os.path.join(self.tmp.name, "cache", "package")
        os.makedirs(os.path.join(package, ".muse-plugin"), exist_ok=True)
        Path(package, ".muse-plugin", "plugin.json").write_text(Path(self.manifest).read_text() if text is None else text)
        return package

    def muse_says(self, output: str, code: int = 0):
        return mock.patch.object(PLM, "find_muse", lambda home, env: "/bin/muse"), \
            mock.patch.object(subprocess, "run", lambda argv, **kw: SimpleNamespace(stdout=output, stderr="", returncode=code))

    def check(self, output: str, code: int = 0) -> tuple[int, dict]:
        out = io.StringIO()
        found, run = self.muse_says(output, code)
        with found, run:
            rc = PLM.install(PL.InstallOptions("muse", check=True), self.env, out)
        return rc, json.loads(out.getvalue())

    def test_check_reports_what_muse_says_and_the_exit_follows_it(self) -> None:
        self.install()
        cache = self.cache_copy()
        rc, answer = self.check(self.inspect_output(cache=cache))
        self.assertEqual((rc, answer["current"], answer["cached"], answer["muse"]["approved"]), (0, True, True, True))
        self.assertNotIn("steps", answer)
        rc, answer = self.check(self.inspect_output("review_needed", cache))
        self.assertEqual((rc, answer["muse"]["approved"], answer["muse"]["hooks"]["letter-end"]), (1, False, "review_needed"))
        self.assertEqual(answer["steps"], ["muse plugins approve session-letter"])
        rc, answer = self.check("", 1)
        self.assertEqual((rc, answer["installed"]), (1, {"bundle": True, "muse": False}))
        self.assertEqual(answer["steps"][0], "muse plugins install %s --scope user" % PLM.bundle_dir(self.home))
        rc, answer = self.check(self.inspect_output(cache=cache, hooks=("letter-start",)))  # a hook is missing
        self.assertEqual((rc, answer["muse"]["approved"]), (1, False))

    def test_check_reads_the_copy_muse_runs_and_not_only_the_bundle(self) -> None:
        # `muse plugins install` copies the bundle into a cache package.  A bundle changed afterwards (a new --remote,
        # a new timeout) stays unapplied there, with every hook still trusted_enabled, until `muse plugins update`.
        self.install(location="server", remote="mac")
        cache = self.cache_copy()
        self.install(location="server", remote="mac2")
        rc, answer = self.check(self.inspect_output(cache=cache))
        self.assertEqual((rc, answer["current"], answer["cached"], answer["muse"]["approved"]), (1, True, False, True))
        self.assertEqual(answer["steps"], ["muse plugins update session-letter", "muse plugins approve session-letter"])
        # The next install says so as well, instead of "changed: []" and nothing more.
        out = io.StringIO()
        found, run = self.muse_says(self.inspect_output(cache=cache))
        with found, run:
            PLM.install(PL.InstallOptions("muse"), self.env, out)
        again = json.loads(out.getvalue())
        self.assertEqual((again["changed"], again["steps"]), ([], ["muse plugins update session-letter", "muse plugins approve session-letter"]))
        self.cache_copy()  # `muse plugins update` copied the new bundle
        rc, answer = self.check(self.inspect_output(cache=cache))
        self.assertEqual((rc, answer["cached"]), (0, True))
        out = io.StringIO()
        found, run = self.muse_says(self.inspect_output(cache=cache))
        with found, run:
            PLM.install(PL.InstallOptions("muse"), self.env, out)
        self.assertEqual(json.loads(out.getvalue()), {"platform": "muse", "changed": [], "path": self.manifest})

    def test_a_copy_that_cannot_be_found_is_not_a_current_one(self) -> None:
        self.install()
        for cache in (None, "/no/such/folder", self.tmp.name):
            rc, answer = self.check(self.inspect_output(cache=cache))
            self.assertEqual((rc, answer["cached"], answer["muse"]["approved"]), (1, None, True), cache)

    def test_check_with_no_bundle_and_no_muse_fails_without_writing(self) -> None:
        out = io.StringIO()
        with mock.patch.object(PLM, "find_muse", lambda home, env: ""):
            rc = PLM.install(PL.InstallOptions("muse", check=True), self.env, out)
        answer = json.loads(out.getvalue())
        self.assertEqual((rc, answer["installed"]["bundle"], answer["current"]), (1, False, False))
        self.assertFalse(os.path.exists(self.manifest))

    def test_install_never_runs_muse_install_or_approve(self) -> None:
        calls = []
        out = io.StringIO()
        with mock.patch.object(PLM, "find_muse", lambda home, env: "/bin/muse"), \
                mock.patch.object(subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(stdout="", stderr="", returncode=1)):
            PLM.install(PL.InstallOptions("muse"), self.env, out)
            PLM.install(PL.InstallOptions("muse", remove=True), self.env, io.StringIO())
        for argv in calls:
            self.assertEqual(argv[1:3], ["plugins", "inspect"], argv)

    def test_off_the_mac_install_refuses_flags_the_hook_cannot_do_without(self) -> None:
        # The hook runs with a cleared environment, so a server hook with no --location and --remote never registers.
        with mock.patch.object(PLM, "_on_mac", lambda: False):
            code, answer = self.install()
            self.assertEqual(code, 2)
            self.assertIn("--location server --remote", answer["error"])
            code, answer = self.install(location="server")
            self.assertEqual(code, 2)
            self.assertIn("needs --remote", answer["error"])
            self.assertFalse(os.path.exists(self.manifest))
            code, answer = self.install(location="server", remote="mac")
            self.assertEqual((code, answer["changed"]), (0, ["bundle"]))
            self.assertEqual(self.install()[1]["changed"], [])  # the flags of the bundle already there count

    def test_check_off_the_mac_fails_for_a_bundle_that_never_registers(self) -> None:
        self.install()  # written on the Mac: no location in it
        cache = self.cache_copy()
        rc, answer = self.check(self.inspect_output(cache=cache))
        self.assertEqual(rc, 0)
        with mock.patch.object(PLM, "_on_mac", lambda: False):
            rc, answer = self.check(self.inspect_output(cache=cache))
            self.assertEqual(rc, 1)
            self.assertIn("--location", answer["problem"])
            self.assertEqual((answer["current"], answer["cached"], answer["muse"]["approved"]), (True, True, True))
            self.assertEqual(self.install(remove=True)[0], 0)  # taking it out asks for nothing

    def test_the_hook_runs_under_sh_so_that_no_failure_of_the_entry_point_blocks_a_prompt(self) -> None:
        # Muse reads exit 2 of a UserPromptSubmit plugin hook as "block this prompt".  The server wrapper exits 2 when the
        # current fleet-cli release has no scripts/platform-letter (a rollback target), so the manifest guards the command.
        self.install(location="server", remote="mac")
        argv = self.doc()["capabilities"]["hooks"][1]["command"]
        self.assertEqual(argv[: len(PLM.GUARD)], PLM.GUARD)
        entry = argv[len(PLM.GUARD)]
        self.assertEqual(argv[len(PLM.GUARD) + 1 :], ["hook", "--platform", "muse", "--location", "server", "--remote", "mac"])
        os.makedirs(os.path.dirname(entry))

        def run(command: list, script: str | None) -> subprocess.CompletedProcess:
            if script is None:
                if os.path.exists(entry):
                    os.unlink(entry)
            else:
                Path(entry).write_text(script)
                os.chmod(entry, 0o755)
            return subprocess.run(command, input='{"event": 1}', capture_output=True, text=True, timeout=30)

        for script in ("#!/bin/sh\nexit 2\n", "#!/bin/sh\nexit 1\n", None):
            guarded = run(argv, script)
            self.assertEqual((guarded.returncode, guarded.stdout), (0, ""), script)
        bare = run(argv[len(PLM.GUARD) :], "#!/bin/sh\nexit 2\n")
        self.assertEqual(bare.returncode, 2)  # the exit the guard hides
        passed = run(argv, "#!/bin/sh\ncat\n")
        self.assertEqual((passed.returncode, passed.stdout), (0, '{"event": 1}'))  # stdin in, stdout out

    def test_the_known_plugin_is_updated_not_installed_again(self) -> None:
        out = io.StringIO()
        inspect = "session-letter\t1.0.0\tenabled=true\n"
        with mock.patch.object(PLM, "find_muse", lambda home, env: "/bin/muse"), \
                mock.patch.object(subprocess, "run", lambda argv, **kw: SimpleNamespace(stdout=inspect, stderr="", returncode=0)):
            PLM.install(PL.InstallOptions("muse"), self.env, out)
        self.assertEqual(json.loads(out.getvalue())["steps"], ["muse plugins update session-letter", "muse plugins approve session-letter"])

    def test_the_entry_point_dispatches_to_this_installer(self) -> None:
        out = io.StringIO()
        with mock.patch.object(PLM, "find_muse", lambda home, env: ""):
            code = PL.install_main(["--platform", "muse", "--location", "server", "--remote", "mac"], self.env, out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["changed"], ["bundle"])
        self.assertEqual(self.doc()["capabilities"]["hooks"][2]["command"][-1], "mac")

    @unittest.skipUnless(os.environ.get("PLATFORM_LETTER_LIVE_MUSE") == "1", "set PLATFORM_LETTER_LIVE_MUSE=1 to run the real muse")
    def test_muse_validates_the_bundle(self) -> None:
        self.install(location="server", remote="mac")
        proc = subprocess.run(["muse", "plugins", "validate", PLM.bundle_dir(self.home)], capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, HOME=self.home), cwd=self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"^valid\tsession-letter\tnative\t.*hooks=3 .*diagnostics=0")


class EntryTests(unittest.TestCase):
    def test_the_entry_script_runs_the_muse_hook_and_ignores_a_stranger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "PATH": os.environ.get("PATH", "/usr/bin"), "AGENT_SYNC_STATE_DIR": os.path.join(tmp, "s")}
            payload = json.dumps({"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"})
            proc = subprocess.run([sys.executable, str(SCRIPTS / "platform-letter"), "hook", "--platform", "muse"],
                                  input=payload, capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""))  # its parent is not Muse:  no letter, no output
            self.assertFalse(os.path.exists(os.path.join(tmp, "s", "MC")))


if __name__ == "__main__":
    unittest.main()
