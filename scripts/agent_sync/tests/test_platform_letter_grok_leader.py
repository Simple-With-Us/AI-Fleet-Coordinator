"""The Grok adapter of `platform-letter` (AFC-564): Operator session letters and "XX: Title" names for the Grok TUI and
Grok Build.  Grok's own store is a temporary `$GROK_HOME/sessions/<cwd>/<id>/summary.json` tree; the leader the hook asks
to rename a chat is a fake `grok` program (a real child process that speaks the ACP lines over pipes); the registry is a
temporary one in the double (pair) letter space, which main's registry has.  No test depends on which pair the registry
hands out first, only on its being a pair that the Operator, the note and the chat's title agree on."""

from __future__ import annotations

import dataclasses
import inspect
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import claude_letter as CL
from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL
from agent_sync import platform_letter_grok_leader as G

SCRIPTS = Path(__file__).resolve().parents[2]
ENTRY = SCRIPTS / "platform-letter"
PAIR = r"^[A-Z]{2}$"

FAKE_GROK = """#!%(python)s
import json, os, sys, time
log = os.environ.get("FAKE_GROK_LOG")
mode = os.environ.get("FAKE_GROK_MODE", "ok")
state = os.environ.get("FAKE_GROK_STATE", "")


def note(row):
    if log:
        with open(log, "a") as fh:
            fh.write(json.dumps(row) + "\\n")


names = ("GROK_SESSION_ID", "GROK_HOOK_EVENT", "GROK_HOOK_NAME", "GROK_HOME", "GROK_WORKSPACE_ROOT",
         "CLAUDE_PROJECT_DIR", "GROK_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "GROK_LEADER_SOCKET")
note({"argv": sys.argv[1:], "env": {k: os.environ[k] for k in names if k in os.environ}})
if mode == "exit":
    sys.exit(3)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if mode == "noise":
        print(json.dumps({"jsonrpc": "2.0", "method": "_x.ai/announcements/update", "params": {}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "method": "session/request_permission", "params": {}}), flush=True)
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1}}), flush=True)
    elif method == "_x.ai/session/rename":
        note({"rename": msg["params"]})
        if mode == "silent":
            time.sleep(30)
        elif mode == "error":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "Internal error"}}), flush=True)
        elif mode.startswith("notfound"):
            want = int(mode.split(":")[1])
            seen = int(open(state).read() or "0") if os.path.exists(state) else 0
            open(state, "w").write(str(seen + 1))
            if seen < want:
                err = {"code": -32600, "message": "Invalid request", "data": "session not found: " + msg["params"]["sessionId"]}
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": err}), flush=True)
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"success": True}}), flush=True)
        elif mode == "refused":
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"success": False}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"success": True}}), flush=True)
"""


def gid(n: int) -> str:
    return "01a00000-0000-7000-8000-%012d" % n


def listen_on(path: str) -> socket.socket:
    """A unix socket something listens on (nothing needs to accept: a connect lands in the backlog)."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    return server


class Base(unittest.TestCase):
    """A Mac-shaped home: a Grok home with a session store, and the Operator's registry under the state folder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.grok = os.path.join(self.home, ".grok")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.grok, "sessions"))
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()  # the Operator's registry exists, as it does on the Mac

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "AGENT_SYNC_STATE_DIR": self.state}
        env.update(extra)
        return env

    def exe(self, *parts: str, body: str = "#!/bin/sh\n") -> str:
        path = os.path.join(self.tmp.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(body)
        os.chmod(path, 0o755)
        return path

    def link_program(self) -> str:
        """The `platform-letter` link that scripts/agent_sync/install.sh puts in ~/.local/bin, which the hook file runs."""
        return self.exe("home", ".local", "bin", "platform-letter")

    # ---- Grok's store ----------------------------------------------------------------------

    def session(self, sid: str, **fields: object) -> str:
        """The session's summary.json, shaped like Grok's: a chat unless `session_kind` says otherwise."""
        folder = os.path.join(self.grok, "sessions", "%2Ftmp%2Fcwd", sid)
        os.makedirs(folder, exist_ok=True)
        doc = {"info": {"id": sid, "cwd": "/tmp/cwd"}, "agent_name": "grok-build-plan", "session_summary": ""}
        doc.update(fields)
        path = os.path.join(folder, "summary.json")
        Path(path).write_text(json.dumps(doc))
        return path

    def summary(self, sid: str) -> dict:
        return json.loads(Path(self.grok, "sessions", "%2Ftmp%2Fcwd", sid, "summary.json").read_text())

    def retitle(self, sid: str, title: str) -> None:
        self.session(sid, **dict(self.summary(sid), generated_title=title))

    # ---- the registry and the note ---------------------------------------------------------

    def lookup(self, sid: str) -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup(G.PLATFORM, "mac", PL.SCOPE, sid)
        finally:
            reg.close_db()

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "GROK", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def has_note(self, sid: str) -> bool:
        return os.path.exists(os.path.join(self.state, "GROK", "letters", sid + ".json"))

    def label(self, sid: str) -> str:
        return self.note(sid)["label"]


class AccessorTests(Base):
    def hook_env(self, sid: str, event: str = "session_start", **extra: str) -> dict[str, str]:
        return self.env(GROK_SESSION_ID=sid, GROK_HOOK_EVENT=event, **extra)

    def test_the_session_is_the_one_groks_hook_runner_names(self) -> None:
        sid = gid(1)
        payload = {"hook_event_name": "SessionStart", "sessionId": sid, "session_id": sid}
        self.assertEqual(G.grok_session_id(payload, self.hook_env(sid)), sid)
        # Not run by Grok's hook runner: no GROK_HOOK_EVENT, no GROK_SESSION_ID, or someone else's session.
        self.assertEqual(G.grok_session_id(payload, self.env(GROK_SESSION_ID=sid)), "")
        self.assertEqual(G.grok_session_id(payload, self.env(GROK_HOOK_EVENT="session_start")), "")
        self.assertEqual(G.grok_session_id(payload, self.hook_env(gid(2))), "")
        # A Claude Code or Cursor hook payload in a Claude Code environment.
        claude = self.env(CLAUDE_CODE_ENTRYPOINT="cli", CLAUDE_CODE_SESSION_ID=sid)
        self.assertEqual(G.grok_session_id({"hook_event_name": "SessionStart", "session_id": sid}, claude), "")
        # `platform-letter show` inside a Grok shell has no payload and no hook event.
        self.assertEqual(G.grok_session_id({}, self.env(GROK_SESSION_ID=sid)), sid)

    def test_the_kind_follows_the_store(self) -> None:
        cases = {
            gid(1): ({}, "chat"),
            gid(2): ({"session_kind": "worktree"}, "chat"),
            gid(3): ({"session_kind": "headless"}, "headless"),
            gid(4): ({"session_kind": "subagent"}, "subagent"),
            gid(5): ({"session_kind": "subagent_resume"}, "subagent"),
            gid(6): ({"session_kind": "something-new"}, "headless"),  # a kind not seen is not a chat
            gid(9): ({"session_kind": "fork", "parent_session_id": gid(1)}, "chat"),  # seen live after /fork
        }
        for sid, (fields, want) in cases.items():
            self.session(sid, **fields)
            payload = {"hook_event_name": "UserPromptSubmit", "sessionId": sid, "prompt": "hi"}
            self.assertEqual(G.grok_kind(payload, self.hook_env(sid, "user_prompt_submit")), want, fields)
        # A session the store does not show cannot be told from a `grok -p` run, so it is not a chat.
        payload = {"hook_event_name": "SessionStart", "sessionId": gid(7)}
        self.assertEqual(G.grok_kind(payload, self.hook_env(gid(7))), "headless")
        # An unreadable summary is the same.
        bad = self.session(gid(8))
        Path(bad).write_text("{not json")
        self.assertEqual(G.grok_kind({"hook_event_name": "SessionStart", "sessionId": gid(8)}, self.hook_env(gid(8))), "headless")
        # Not a Grok hook run at all.
        self.assertEqual(G.grok_kind({"hook_event_name": "SessionStart", "sessionId": gid(1)}, self.env()), "headless")

    def test_events_in_a_subagents_own_session_are_a_subagents(self) -> None:
        child = gid(2)
        # The sub-agent's own session may have no summary yet, and its events say what they are.
        for name, event in (("UserPromptSubmit", "user_prompt_submit"), ("SessionEnd", "session_end")):
            payload = {"hook_event_name": name, "sessionId": child, "subagentType": "code-scout"}
            self.assertEqual(G.grok_kind(payload, self.hook_env(child, event)), "subagent", name)
        self.session(child, session_kind="subagent")
        payload = {"hook_event_name": "UserPromptSubmit", "sessionId": child}
        self.assertEqual(G.grok_kind(payload, self.hook_env(child, "user_prompt_submit")), "subagent")

    def test_a_subagent_is_out_of_scope_whoever_started_it(self) -> None:
        """The spec reads no parent for a sub-agent, even a resumed one that names its source, so the library's in-scope
        rule leaves it out."""
        self.assertIs(G.SPEC.parent_id, G.grok_parent)
        child, parent = gid(2), gid(1)
        for fields in ({"session_kind": "subagent"}, {"session_kind": "subagent_resume", "parent_session_id": parent}):
            self.session(child, **fields)
            payload = {"hook_event_name": "UserPromptSubmit", "sessionId": child, "subagentType": "code-scout"}
            env = self.hook_env(child, "user_prompt_submit")
            self.assertEqual(G.grok_parent(payload, env), "", fields)
            self.assertFalse(PL.in_scope(G.SPEC, env, payload), fields)
        self.session(parent)
        self.assertTrue(PL.in_scope(G.SPEC, self.hook_env(parent), {"hook_event_name": "SessionStart", "sessionId": parent}))

    def test_the_current_title_is_the_one_grok_shows(self) -> None:
        sid = gid(1)
        payload = {"hook_event_name": "UserPromptSubmit", "sessionId": sid, "prompt": "x"}
        env = self.hook_env(sid, "user_prompt_submit")
        self.session(sid)
        self.assertEqual(G.grok_current_title(payload, env), "")
        # `session_summary` keeps the automatic title after a rename; `generated_title` is what is shown.
        self.session(sid, session_summary="Old Automatic Words", generated_title="AB:   Renamed  Chat ")
        self.assertEqual(G.grok_current_title(payload, env), "AB: Renamed Chat")

    def test_a_fork_names_its_source_as_its_parent(self) -> None:
        source, fork = gid(1), gid(2)
        payload = {"hook_event_name": "UserPromptSubmit", "sessionId": fork, "prompt": "x"}
        env = self.hook_env(fork, "user_prompt_submit")
        self.session(source)
        self.assertEqual(G.grok_parent({**payload, "sessionId": source}, self.hook_env(source, "user_prompt_submit")), "")
        for fields in (
            {"session_kind": "fork", "parent_session_id": source},  # seen live after /fork
            {"parent_session_id": source},  # a source with no kind is a sub-thread all the same
            {"session_kind": "worktree", "parent_session_id": source},
        ):
            self.session(fork, **fields)
            self.assertEqual(G.grok_parent(payload, env), source, fields)
        # Not a session id, the chat itself, nothing, or the store does not show it: no parent.
        for bad in ("../" + source, "a b", fork, "", None):
            self.session(fork, session_kind="fork", parent_session_id=bad)
            self.assertEqual(G.grok_parent(payload, env), "", bad)
        self.assertEqual(G.grok_parent({**payload, "sessionId": gid(5)}, self.hook_env(gid(5), "user_prompt_submit")), "")
        # Not a Grok hook run.
        self.session(fork, session_kind="fork", parent_session_id=source)
        self.assertEqual(G.grok_parent(payload, self.env()), "")

    def test_a_forks_title_comes_without_its_sources_letter(self) -> None:
        """Seen live: a fork's `generated_title` starts as the source chat's, "AA: ..." and all.  The letter is not the
        fork's, so the library must see the words alone; the fork's own letter, once it has one, stays."""
        source, fork = gid(1), gid(2)
        payload = {"hook_event_name": "SessionStart", "sessionId": fork, "source": "load"}
        env = self.hook_env(fork)
        self.session(fork, session_kind="fork", parent_session_id=source, generated_title="AA: Fix the Login Page Redirect")
        self.assertEqual(G.grok_current_title(payload, env), "Fix the Login Page Redirect")
        self.assertEqual(G.own_label(env, fork), "")

        def hold(label: str) -> None:
            folder = os.path.join(self.state, "GROK", "letters")
            os.makedirs(folder, exist_ok=True)
            Path(folder, fork + ".json").write_text(json.dumps({"label": label, "state": "open"}))

        hold("AB")  # the fork has its own letter now, and its title does not carry it yet (the rename failed)
        self.assertEqual(G.own_label(env, fork), "AB")
        self.assertEqual(G.grok_current_title(payload, env), "Fix the Login Page Redirect")
        self.retitle(fork, "AB: Fix the Login Page Redirect")  # and once it does, it is left as it is
        self.assertEqual(G.grok_current_title(payload, env), "AB: Fix the Login Page Redirect")
        # A source chat that never had a letter hands over a title that has none to take off.
        self.retitle(fork, "Fix Login")
        self.assertEqual(G.grok_current_title(payload, self.hook_env(fork)), "Fix Login")
        # A chat with no source keeps whatever it shows: the library knows how to read a lettered title.
        plain = gid(3)
        self.session(plain, generated_title="AA: Fix the Login Page Redirect")
        self.assertEqual(
            G.grok_current_title({**payload, "sessionId": plain}, self.hook_env(plain)), "AA: Fix the Login Page Redirect"
        )
        # A note that cannot be read is no letter.
        Path(self.state, "GROK", "letters", fork + ".json").write_text("{not json")
        self.assertEqual(G.own_label(env, fork), "")

    def test_a_session_id_that_is_not_one_never_reaches_a_path(self) -> None:
        self.session(gid(1))
        for bad in ("../" + gid(1), "*", "", "a/b", gid(1) + "/../x"):
            self.assertIsNone(G.summary_of(self.env(), bad), bad)
        self.assertIsNotNone(G.summary_of(self.env(), gid(1)))

    def test_the_store_is_the_grok_home_the_hook_runs_in(self) -> None:
        private = os.path.join(self.tmp.name, "private")
        folder = os.path.join(private, "sessions", "%2Ftmp%2Fother", gid(9))
        os.makedirs(folder)
        Path(folder, "summary.json").write_text(json.dumps({"session_kind": "headless"}))
        self.assertEqual(G.summary_of(self.env(GROK_HOME=private), gid(9))["session_kind"], "headless")
        self.assertIsNone(G.summary_of(self.env(), gid(9)))  # the default home does not hold it

    def test_the_spec_and_the_installer_agree_on_the_events(self) -> None:
        self.assertEqual(set(G.SPEC.events), set(G.HOOK_TIMEOUTS))
        self.assertEqual(G.SPEC.platform, "grok-leader")
        self.assertEqual(G.SPEC.seat, "GROK")
        self.assertEqual(G.SPEC.locations, ("mac",))
        for name, canonical in G.SPEC.events.items():
            self.assertEqual(PL.canonical_event(G.SPEC, {"hook_event_name": name}), canonical)
        self.assertEqual(G.SPEC.off_variable, "GROK_LETTER")
        self.assertIs(PL.load_spec("grok-leader"), G.SPEC)

    def test_the_claude_hook_is_not_in_scope_in_a_grok_hook_environment(self) -> None:
        """Grok imports Claude hook files; the Claude letter hook gates on CLAUDE_CODE_ENTRYPOINT, which Grok does not set
        itself.  A Grok that inherits one (started from a Claude Code Bash tool) is the gap `install` warns about."""
        grok = self.hook_env(gid(1), "session_start", CLAUDE_PROJECT_DIR="/tmp/cwd", GROK_WORKSPACE_ROOT="/tmp/cwd")
        self.assertFalse(CL.in_scope(grok))
        self.assertFalse(CL.in_scope(self.env(CLAUDE_CODE_ENTRYPOINT="sdk-cli")))
        self.assertTrue(CL.in_scope(self.env(CLAUDE_CODE_ENTRYPOINT="cli")))  # and a real Claude chat still is


class RenameTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.log = os.path.join(self.tmp.name, "fake-grok.log")
        self.fake = self.exe("bin", "grok", body=FAKE_GROK % {"python": sys.executable})
        self.sock = os.path.join(self.grok, "leader.sock")
        self.leader = listen_on(self.sock)  # stands in for the leader: the fake client never talks to it
        self.addCleanup(self.leader.close)

    def renv(self, **extra: str) -> dict[str, str]:
        env = self.env(GROK_LETTER_BIN=self.fake, FAKE_GROK_LOG=self.log, GROK_LEADER_SOCKET=self.sock)
        env.update(extra)
        return env

    def calls(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        return [json.loads(line) for line in Path(self.log).read_text().splitlines()]

    def test_it_renames_the_chat_through_the_leader_that_hosts_it(self) -> None:
        self.assertTrue(G.rename_session(gid(1), "AB: Fix  Login", self.renv()))
        first, second = self.calls()
        self.assertEqual(first["argv"], ["agent", "--leader", "--leader-socket", self.sock, "stdio"])
        self.assertEqual(second, {"rename": {"sessionId": gid(1), "title": "AB: Fix Login"}})

    def test_the_client_is_not_a_hook_of_the_chat(self) -> None:
        env = self.renv(
            GROK_SESSION_ID=gid(1), GROK_HOOK_EVENT="user_prompt_submit", GROK_HOOK_NAME="global/x", GROK_HOME=self.grok,
            GROK_WORKSPACE_ROOT="/tmp/cwd", CLAUDE_PROJECT_DIR="/tmp/cwd", GROK_PLUGIN_ROOT="/p", CLAUDE_PLUGIN_ROOT="/p",
        )
        self.assertTrue(G.rename_session(gid(1), "AB: Fix Login", env))
        seen = self.calls()[0]["env"]
        self.assertEqual(seen, {"GROK_HOME": self.grok, "GROK_LEADER_SOCKET": self.sock})

    def test_notifications_and_requests_of_the_leader_are_read_and_dropped(self) -> None:
        self.assertTrue(G.rename_session(gid(1), "AB: Fix Login", self.renv(FAKE_GROK_MODE="noise")))

    def test_a_title_longer_than_groks_limit_is_cut_to_it(self) -> None:
        """Seen live: the leader refuses more than 100 characters (code points, whatever the script)."""
        long = "AB: " + " ".join(["Reorganize"] * 12)  # 4 + 12 * 10 + 11 = 135
        self.assertTrue(G.rename_session(gid(1), long, self.renv()))
        sent = self.calls()[1]["rename"]["title"]
        self.assertLessEqual(len(sent), G.MAX_TITLE)
        self.assertTrue(sent.startswith("AB: Reorganize Reorganize") and sent.endswith("Reorganize"), sent)
        self.assertEqual(G.fit("x" * 150), "x" * 100)  # one long word is cut where the limit falls
        self.assertEqual(G.fit("AB: " + "y" * 150), "AB: " + "y" * 96)  # and a label is never all that is left
        self.assertEqual(G.fit("请" * 100), "请" * 100)
        self.assertEqual(G.fit("short"), "short")

    def test_a_session_the_leader_does_not_host_is_asked_once(self) -> None:
        """Seen live: for a session it does not host (a sub-agent's, say) the leader says "session not found" however
        long it is asked, so the hook asks once and does not hold the chat up."""
        env = self.renv(FAKE_GROK_MODE="notfound:99", FAKE_GROK_STATE=os.path.join(self.tmp.name, "count"))
        started = time.monotonic()
        self.assertFalse(G.rename_session(gid(1), "AB: Fix Login", env))
        self.assertEqual(sum(1 for c in self.calls() if "rename" in c), 1)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_an_error_or_a_refusal_is_a_failure(self) -> None:
        for mode in ("error", "refused", "exit"):
            self.assertFalse(G.rename_session(gid(1), "AB: Fix Login", self.renv(FAKE_GROK_MODE=mode)), mode)

    def test_a_leader_that_does_not_answer_costs_the_deadline_and_no_more(self) -> None:
        started = time.monotonic()
        with mock.patch.object(G, "RPC_SECONDS", 0.6):
            self.assertFalse(G.rename_session(gid(1), "AB: Fix Login", self.renv(FAKE_GROK_MODE="silent")))
        self.assertLess(time.monotonic() - started, 3.0)

    def test_nothing_is_started_without_a_program_a_leader_a_title_or_an_id(self) -> None:
        env = self.renv()
        self.assertFalse(G.rename_session(gid(1), "  ", env))
        self.assertFalse(G.rename_session("../x", "AB: Fix", env))
        self.assertFalse(G.rename_session(gid(1), "AB: Fix", self.renv(GROK_LETTER_BIN=os.path.join(self.tmp.name, "none"))))
        os.unlink(self.sock)  # no leader hosts the chat: a client of ours must not start one
        self.assertFalse(G.rename_session(gid(1), "AB: Fix", self.renv(GROK_LEADER_SOCKET=self.sock)))
        self.assertEqual(self.calls(), [])

    def test_a_socket_nobody_listens_on_is_not_a_leader(self) -> None:
        """Seen live: `grok agent --leader --leader-socket <missing>` starts a leader of its own.  A leader that died
        leaves its socket file behind, so the client is only started for a socket that accepts a connection."""
        self.leader.close()  # the file stays, as it does after a crash
        self.assertTrue(os.path.exists(self.sock))
        self.assertFalse(G.listening(self.sock))
        self.assertEqual(G.leader_socket(self.renv()), "")
        self.assertFalse(G.rename_session(gid(1), "AB: Fix", self.renv()))
        plain = os.path.join(self.tmp.name, "plain")
        Path(plain).write_text("")
        self.assertFalse(G.listening(plain))
        self.assertFalse(G.listening(os.path.join(self.tmp.name, "gone")))
        self.assertFalse(G.listening("x" * 300))  # too long for a socket address
        self.assertFalse(G.listening("a\0b"))
        self.assertEqual(self.calls(), [])

    def test_the_default_socket_is_under_the_grok_home(self) -> None:
        env = self.env()
        self.assertEqual(G.leader_socket(env), self.sock)
        self.assertEqual(G.leader_socket(self.env(GROK_LEADER_SOCKET=os.path.join(self.tmp.name, "gone"))), "")
        self.assertEqual(G.leader_socket(self.env(GROK_LEADER_SOCKET=self.sock)), self.sock)

    def test_the_program_is_found_on_the_path_or_where_grok_installs_itself(self) -> None:
        folder = os.path.dirname(self.fake)
        self.assertEqual(G.grok_binary(self.env(PATH=folder)), self.fake)
        self.assertEqual(G.grok_binary(self.env(PATH="/nonexistent")), "")
        installed = self.exe("home", ".grok", "bin", "grok")
        self.assertEqual(G.grok_binary(self.env(PATH="/nonexistent", HOME=os.path.join(self.tmp.name, "home"))), installed)

    def test_the_title_writer_names_the_chat_it_was_asked_about(self) -> None:
        req = PL.TitleRequest(
            platform=G.PLATFORM, session_id=gid(2), label="AB", title="AB: List Files", words="List Files",
            current="", reason="start", data={}, env=self.renv(),
        )
        self.assertTrue(G.grok_rename(req))
        self.assertEqual(self.calls()[1], {"rename": {"sessionId": gid(2), "title": "AB: List Files"}})


class FlowBase(Base):
    """The hook, in process, as the Mac runs it, with the leader replaced by a writer that updates the store."""

    def setUp(self) -> None:
        super().setUp()
        self.renames: list[tuple[str, str, str]] = []
        self.writer_works = True
        self.spec = dataclasses.replace(G.SPEC, title_writer=self.write_title)

    def write_title(self, req: PL.TitleRequest) -> bool:
        self.renames.append((req.session_id, req.title, req.reason))
        if self.writer_works:
            self.retitle(req.session_id, req.title)  # what the leader does: generated_title, title_is_manual
        return self.writer_works

    def run_hook(self, payload: dict, in_session: str, *, env: dict | None = None, **flags) -> dict | None:
        event = {
            "SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit", "SubagentStart": "subagent_start",
            "SubagentStop": "subagent_stop", "SessionEnd": "session_end",
        }[payload["hook_event_name"]]
        env = env if env is not None else self.env(GROK_SESSION_ID=in_session, GROK_HOOK_EVENT=event)
        out = io.StringIO()
        code = PL.hook(
            self.spec, env, io.StringIO(json.dumps(payload)), out, platform="darwin", stderr=io.StringIO(), **flags
        )
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def start(self, sid: str, source: str = "new", **kw) -> dict | None:
        return self.run_hook(
            {"hook_event_name": "SessionStart", "hookEventName": "session_start", "sessionId": sid, "session_id": sid,
             "source": source, "cwd": "/tmp/cwd"}, sid, **kw)

    def prompt(self, sid: str, text: str, **kw) -> dict | None:
        return self.run_hook(
            {"hook_event_name": "UserPromptSubmit", "sessionId": sid, "session_id": sid, "prompt": text, "promptId": "p"},
            sid, **kw)

    def end(self, sid: str, reason: str = "shutdown") -> dict | None:
        return self.run_hook({"hook_event_name": "SessionEnd", "sessionId": sid, "reason": reason}, sid)



class HookTests(FlowBase):
    def test_a_new_chat_gets_a_letter_then_a_title(self) -> None:
        sid = gid(1)
        self.session(sid)
        out = self.start(sid)
        label = self.label(sid)
        self.assertRegex(label, PAIR)
        self.assertEqual(self.lookup(sid)["label"], label)
        self.assertEqual(self.renames, [(sid, "%s: New Chat" % label, "start")])
        self.assertIn("Grok %s" % label, out["additionalContext"])  # Grok 1.0.70 does not read it; the title carries the letter
        self.assertNotIn("sessionTitle", out)  # there is no such field in Grok's output
        self.prompt(sid, "fix the login page redirect please")
        self.assertEqual(self.renames[-1], (sid, "%s: Fix the Login Page Redirect Please" % label, "prompt"))
        self.assertEqual(self.lookup(sid)["title"], "Fix the Login Page Redirect Please")
        self.assertEqual(self.summary(sid)["generated_title"], "%s: Fix the Login Page Redirect Please" % label)
        count = len(self.renames)
        self.prompt(sid, "and another thing")
        self.assertEqual(len(self.renames), count)  # steady state: nothing more to say

    def test_the_note_is_where_the_other_platforms_notes_are(self) -> None:
        sid = gid(1)
        self.session(sid)
        self.start(sid)
        path = os.path.join(self.state, "GROK", "letters", sid + ".json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(self.note(sid)["platform"], "grok-leader")
        out = io.StringIO()
        code = PL.show(self.spec, ["--session-id", sid], self.env(GROK_SESSION_ID=sid), out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["label"], self.label(sid))

    def test_a_resumed_chat_keeps_the_title_grok_stored(self) -> None:
        sid = gid(1)
        self.session(sid, generated_title="Fleet PR Sweep")
        self.start(sid, source="load")
        label = self.label(sid)
        self.assertEqual(self.lookup(sid)["title"], "Fleet PR Sweep")
        self.assertEqual(self.renames, [(sid, "%s: Fleet PR Sweep" % label, "start")])

    def test_a_resume_of_a_lettered_chat_changes_nothing(self) -> None:
        sid = gid(1)
        self.session(sid)
        self.start(sid)
        self.prompt(sid, "fix the login page")
        label = self.label(sid)
        count = len(self.renames)
        self.start(sid, source="load")
        self.assertEqual(self.label(sid), label)
        self.assertEqual(len(self.renames), count)
        self.assertEqual(self.lookup(sid)["title"], "Fix the Login Page")

    def test_a_chat_the_owner_renamed_gets_its_letter_back(self) -> None:
        sid = gid(1)
        self.session(sid)
        self.start(sid)
        label = self.label(sid)
        self.prompt(sid, "fix the login page")
        self.retitle(sid, "Billing Cleanup")  # /rename in the TUI dropped the letter
        self.prompt(sid, "next")
        self.assertEqual(self.renames[-1], (sid, "%s: Billing Cleanup" % label, "prompt"))
        self.assertEqual(self.lookup(sid)["title"], "Billing Cleanup")

    def test_a_title_the_leader_could_not_set_is_tried_on_the_first_prompt(self) -> None:
        sid = gid(1)
        self.session(sid)
        self.writer_works = False
        self.start(sid)
        self.assertEqual(self.note(sid)["state"], "open")  # the letter is in the registry whatever the leader said
        self.assertTrue(self.note(sid)["pending"])
        self.writer_works = True
        self.prompt(sid, "fix the login page")
        label = self.label(sid)
        self.assertEqual(self.summary(sid)["generated_title"], "%s: Fix the Login Page" % label)

    def test_a_headless_run_takes_no_letter(self) -> None:
        sid = gid(1)
        self.session(sid, session_kind="headless")
        self.assertIsNone(self.start(sid))
        self.assertIsNone(self.prompt(sid, "reply with ok"))
        self.assertIsNone(self.end(sid))
        self.assertFalse(self.has_note(sid))
        self.assertIsNone(self.lookup(sid))
        self.assertEqual(self.renames, [])

    def test_a_chat_the_store_does_not_show_takes_no_letter(self) -> None:
        self.assertIsNone(self.start(gid(1)))
        self.assertIsNone(self.lookup(gid(1)))

    def test_a_session_a_launcher_started_takes_no_letter(self) -> None:
        sid = gid(1)
        self.session(sid)
        for extra in ({"AGENT_LAUNCHER": "grok-bot"}, {"AGENT_LAUNCH_SEAT": "GROK-BOT"}):
            env = self.env(GROK_SESSION_ID=sid, GROK_HOOK_EVENT="session_start", **extra)
            self.assertIsNone(self.start(sid, env=env), extra)
        self.assertIsNone(self.lookup(sid))

    def test_the_off_switches(self) -> None:
        sid = gid(1)
        self.session(sid)
        for name in ("GROK_LETTER", "SESSION_LETTER"):
            env = self.env(GROK_SESSION_ID=sid, GROK_HOOK_EVENT="session_start", **{name: "0"})
            self.assertIsNone(self.start(sid, env=env), name)
        self.assertIsNone(self.lookup(sid))

    def leader_env(self, sid: str, event: str = "session_start", **extra: str) -> dict[str, str]:
        """What a hook of a leader-hosted chat sees: the leader's own environment (a socket it set, nothing of the
        client that asked for the chat), seen live on the Mac."""
        return self.env(GROK_SESSION_ID=sid, GROK_HOOK_EVENT=event, GROK_LEADER_SOCKET=os.path.join(self.grok, "leader.sock"), **extra)

    def test_the_gates_are_the_hosting_processs_environment(self) -> None:
        """The hook of a leader-hosted chat runs in the leader's environment, so what only the client that asked for the
        chat had set (a launcher's AGENT_LAUNCHER, GROK_LETTER=0 in a terminal) never reaches it, and what the leader
        itself carries reaches every chat on it.  Pinned both ways so the README's account stays the true one."""
        sid, other = gid(1), gid(2)
        self.session(sid)
        self.session(other)
        self.start(sid, env=self.leader_env(sid))
        self.assertRegex(self.label(sid), PAIR)  # nothing of the client is in the leader's environment: a letter
        for extra in ({"AGENT_LAUNCHER": "botfleet"}, {"GROK_LETTER": "0"}, {"SESSION_LETTER": "0"}):
            self.assertIsNone(self.start(other, env=self.leader_env(other, **extra)), extra)  # a leader started that way
        self.assertIsNone(self.lookup(other))

    def test_the_off_file_pauses_every_chat_whatever_the_environment_says(self) -> None:
        """The one switch that reaches a chat the leader hosts without restarting the leader."""
        live, paused = gid(1), gid(2)
        self.session(live)
        self.session(paused)
        self.start(live, env=self.leader_env(live))
        label = self.label(live)
        off = os.path.join(self.grok, G.OFF_FILE)
        Path(off).write_text("")
        self.assertTrue(G.switched_off(self.env()))
        self.assertEqual(G.grok_kind({"hook_event_name": "SessionStart", "sessionId": paused}, self.leader_env(paused)), "headless")
        before = len(self.renames)
        self.assertIsNone(self.start(paused, env=self.leader_env(paused)))
        self.assertIsNone(self.prompt(paused, "fix the login page", env=self.leader_env(paused, "user_prompt_submit")))
        self.assertIsNone(self.lookup(paused))
        self.assertFalse(self.has_note(paused))
        # A chat that already has its letter is left alone too: no title, no touch, no end.
        self.prompt(live, "fix the login page", env=self.leader_env(live, "user_prompt_submit"))
        self.end(live)
        self.assertEqual(len(self.renames), before)
        self.assertNotIn("ended", self.note(live))
        self.assertFalse(self.note(live)["titled"])  # the paused prompt named nothing
        os.unlink(off)  # and deleting the file puts it back
        self.assertFalse(G.switched_off(self.env()))
        self.start(paused, env=self.leader_env(paused))
        self.assertRegex(self.label(paused), PAIR)
        self.prompt(live, "fix the login page", env=self.leader_env(live, "user_prompt_submit"))
        self.assertEqual(self.label(live), label)
        self.assertEqual(self.summary(live)["generated_title"], "%s: Fix the Login Page" % label)

    def test_the_off_file_is_looked_for_in_the_grok_home_the_hook_runs_in(self) -> None:
        private = os.path.join(self.tmp.name, "private")
        os.makedirs(private)
        Path(private, G.OFF_FILE).write_text("")
        self.assertTrue(G.switched_off(self.env(GROK_HOME=private)))
        self.assertFalse(G.switched_off(self.env()))  # the default home has none

    def test_a_hook_that_is_not_groks_registers_nothing(self) -> None:
        sid = gid(1)
        self.session(sid)
        claude = self.env(CLAUDE_CODE_ENTRYPOINT="cli", CLAUDE_CODE_SESSION_ID=sid, CLAUDE_PROJECT_DIR="/tmp/cwd")
        self.assertIsNone(self.start(sid, env=claude))
        # The payload names a session other than the one Grok's runner is running.
        self.assertIsNone(self.start(sid, env=self.env(GROK_SESSION_ID=gid(2), GROK_HOOK_EVENT="session_start")))
        # A Cursor payload has no Grok environment.
        cursor = self.env(CURSOR_CONVERSATION_ID=sid)
        self.assertIsNone(self.run_hook({"hook_event_name": "SessionStart", "session_id": sid}, sid, env=cursor))
        self.assertIsNone(self.lookup(sid))
        self.assertFalse(self.has_note(sid))

    def test_the_server_takes_nothing(self) -> None:
        sid = gid(1)
        self.session(sid)
        env = self.env(GROK_SESSION_ID=sid, GROK_HOOK_EVENT="session_start", SESSION_LETTER_LOCATION="server", SESSION_LETTER_REMOTE="mac")
        out = io.StringIO()
        code = PL.hook(
            self.spec, env,
            io.StringIO(json.dumps({"hook_event_name": "SessionStart", "sessionId": sid, "source": "new"})), out,
            platform="linux", runner=mock.Mock(side_effect=AssertionError("no ssh for a platform that is not there")),
        )
        self.assertEqual((code, out.getvalue()), (0, ""))
        self.assertIsNone(self.lookup(sid))

    def test_a_worktree_chat_is_a_chat(self) -> None:
        sid = gid(1)
        self.session(sid, session_kind="worktree")
        self.start(sid)
        self.assertRegex(self.label(sid), PAIR)

    def test_a_quit_keeps_the_letter_and_records_why(self) -> None:
        sid = gid(1)
        self.session(sid)
        self.start(sid)
        self.end(sid, "shutdown")
        self.assertEqual(self.lookup(sid)["state"], "active")
        self.assertEqual(self.note(sid)["ended"]["reason"], "shutdown")
        label = self.label(sid)
        self.start(sid, source="load")  # --resume
        self.assertEqual(self.label(sid), label)

    def test_no_end_closes_a_chat(self) -> None:
        """Pinned on purpose: SessionEnd always has a reason (`shutdown`) and a quit chat is resumed, so even an end
        with no reason leaves the registration open, and the 36 hour sweep frees the pair of a chat nobody came back to."""
        self.assertEqual(G.SPEC.close_reasons, ())
        sid = gid(1)
        self.session(sid)
        self.start(sid)
        self.run_hook({"hook_event_name": "SessionEnd", "sessionId": sid}, sid)
        self.assertEqual(self.lookup(sid)["state"], "active")
        self.assertEqual(self.note(sid)["ended"]["reason"], "")


class ForkTests(FlowBase):
    """/fork copies a chat into a parallel one (seen live: `session_kind: fork`, `parent_session_id`, a SessionStart with
    source `load`, and a title that starts as the source chat's, letter and all).  It is a chat another chat started: a
    sub-thread, titled with its own letter and the source chat's words."""

    def setUp(self) -> None:
        super().setUp()
        self.argvs: list[list[str]] = []
        self.source = gid(1)
        self.session(self.source)
        self.start(self.source)
        self.prompt(self.source, "fix the login page redirect")
        self.source_label = self.label(self.source)
        self.title = self.summary(self.source)["generated_title"]
        self.words = self.title.split(": ", 1)[1]
        self.assertEqual(self.title, "%s: Fix the Login Page Redirect" % self.source_label)
        self.renames.clear()

    def recording(self, argv: list[str]) -> dict:
        """The registry transport: the real registry, and a record of what was asked of it."""
        self.argvs.append(list(argv))
        out = io.StringIO()
        PL.registry_main(argv, env=self.env(), stdout=out)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def fork(self, sid: str | None = None, **fields: object) -> str:
        sid = sid or gid(2)
        doc: dict[str, object] = {
            "session_kind": "fork", "parent_session_id": self.source, "forked_at": "2026-10-30T03:33:34.476160Z",
            "generated_title": self.title, "title_is_manual": True,
        }
        doc.update(fields)
        self.session(sid, **doc)
        return sid

    def test_a_fork_is_a_sub_thread_with_the_words_of_its_source(self) -> None:
        fork = self.fork()
        self.start(fork, source="load", transport=self.recording)
        registers = [a for a in self.argvs if a[0] == "register"]
        self.assertEqual(len(registers), 1)
        self.assertIn("--parent-session-id=" + self.source, registers[0])
        self.assertIn("--title=" + self.words, registers[0])  # the words, not "AA: Fix ..."
        label = self.label(fork)
        self.assertRegex(label, PAIR)
        self.assertNotEqual(label, self.source_label)
        self.assertEqual(self.note(fork)["parent"], self.source)
        self.assertEqual(self.lookup(fork)["title"], self.words)
        self.assertEqual(self.renames, [(fork, "%s: %s" % (label, self.words), "start")])  # never "AB: AA: Fix ..."
        self.assertEqual(self.summary(fork)["generated_title"], "%s: %s" % (label, self.words))
        self.assertEqual(self.lookup(self.source)["title"], self.words)  # and the source is as it was
        self.assertEqual(self.summary(self.source)["generated_title"], self.title)

    def test_a_fork_keeps_its_own_letter_through_its_prompts_and_a_resume(self) -> None:
        fork = self.fork()
        self.start(fork, source="load")
        label = self.label(fork)
        self.renames.clear()
        self.prompt(fork, "and now the logout page")
        self.start(fork, source="load")  # --resume
        self.prompt(fork, "one more")
        self.assertEqual(self.renames, [])
        self.assertEqual(self.label(fork), label)
        self.assertEqual(self.lookup(fork)["title"], self.words)
        self.assertEqual(self.summary(fork)["generated_title"], "%s: %s" % (label, self.words))

    def test_a_fork_whose_title_the_leader_could_not_set_is_not_doubled_later(self) -> None:
        """With the source's letter still in its title, the next prompt must not read "AA: Fix ..." as the fork's words."""
        fork = self.fork()
        self.writer_works = False
        self.start(fork, source="load")
        label = self.label(fork)
        self.assertEqual(self.summary(fork)["generated_title"], self.title)  # still the source's
        self.writer_works = True
        self.prompt(fork, "and now the logout page")
        self.assertEqual(self.summary(fork)["generated_title"], "%s: %s" % (label, self.words))
        self.assertEqual(self.lookup(fork)["title"], self.words)
        self.assertEqual(self.note(fork)["title"], self.words)
        self.assertEqual([r for r in self.renames if self.source_label + ": " in r[1] and r[0] == fork], [])

    def test_a_fork_of_a_chat_that_had_no_letter_is_a_sub_thread_too(self) -> None:
        fork = self.fork(generated_title="Fix Login")
        self.start(fork, source="load", transport=self.recording)
        label = self.label(fork)
        self.assertIn("--parent-session-id=" + self.source, [a for a in self.argvs if a[0] == "register"][0])
        self.assertEqual(self.renames, [(fork, "%s: Fix Login" % label, "start")])
        self.assertEqual(self.lookup(fork)["title"], "Fix Login")

    def test_a_source_with_no_session_kind_is_a_fork_all_the_same(self) -> None:
        fork = self.fork(session_kind=None)
        self.start(fork, source="load", transport=self.recording)
        self.assertIn("--parent-session-id=" + self.source, [a for a in self.argvs if a[0] == "register"][0])
        self.assertEqual(self.renames, [(fork, "%s: %s" % (self.label(fork), self.words), "start")])

    def test_a_fork_is_a_chat_whatever_its_source_is_doing(self) -> None:
        """The source may be closed, quit or unknown to the Operator: the fork still takes a letter, as a main chat."""
        self.end(self.source)
        stranger = gid(7)
        fork = self.fork(parent_session_id=stranger)
        self.start(fork, source="load", transport=self.recording)
        self.assertRegex(self.label(fork), PAIR)
        self.assertIn("--parent-session-id=" + stranger, [a for a in self.argvs if a[0] == "register"][0])

    @unittest.skipUnless(
        "parent_session_id" in inspect.signature(OR.register_session).parameters, "this registry cannot place a sub-thread"
    )
    def test_the_operator_gives_the_fork_the_pair_after_its_source(self) -> None:
        fork = self.fork()
        self.start(fork, source="load")
        label = self.label(fork)
        self.assertEqual(label[0], self.source_label[0])
        self.assertNotEqual(label, self.source_label)
        self.assertEqual(self.lookup(fork)["label"], label)


class SubagentTests(FlowBase):
    """A sub-agent's session is not hosted by the leader, so it can be neither titled nor listed: it takes no letter."""

    def setUp(self) -> None:
        super().setUp()
        self.parent, self.child = gid(1), gid(2)
        self.session(self.parent)
        self.start(self.parent)
        self.session(self.child, session_kind="subagent")
        self.before = len(self.renames)

    def child_event(self, name: str, **extra: object) -> dict | None:
        payload = {"hook_event_name": name, "sessionId": self.child, "subagentType": "code-scout"}
        payload.update(extra)
        return self.run_hook(payload, self.child)

    def nothing_for_the_child(self) -> None:
        self.assertIsNone(self.lookup(self.child))
        self.assertFalse(self.has_note(self.child))
        self.assertEqual(len(self.renames), self.before)

    def test_its_own_events_register_nothing(self) -> None:
        self.assertIsNone(self.child_event("UserPromptSubmit", prompt="List the file names in the current directory"))
        self.assertIsNone(self.child_event("SessionEnd", reason="shutdown"))
        self.nothing_for_the_child()

    def test_a_subagent_event_without_its_type_is_still_read_from_the_store(self) -> None:
        self.run_hook({"hook_event_name": "UserPromptSubmit", "sessionId": self.child, "prompt": "list"}, self.child)
        self.nothing_for_the_child()

    def test_a_resumed_subagent_that_names_its_parent_takes_none_either(self) -> None:
        resumed = gid(7)
        self.session(resumed, session_kind="subagent_resume", parent_session_id=self.parent)
        payload = {"hook_event_name": "UserPromptSubmit", "sessionId": resumed, "subagentType": "code-scout", "prompt": "carry on"}
        self.run_hook(payload, resumed)
        self.run_hook({"hook_event_name": "SessionStart", "sessionId": resumed, "source": "load"}, resumed)
        self.assertIsNone(self.lookup(resumed))
        self.assertFalse(self.has_note(resumed))

    def test_a_subagent_hook_event_that_is_wired_by_hand_changes_nothing(self) -> None:
        """SubagentStart and SubagentStop are not in the spec's events, so a file that wires them anyway is ignored."""
        self.run_hook(
            {"hook_event_name": "SubagentStart", "sessionId": self.parent, "subagentId": self.child, "subagentType": "code-scout"},
            self.parent,
        )
        self.run_hook(
            {"hook_event_name": "SubagentStop", "sessionId": self.child, "subagentId": self.child, "subagentType": "code-scout"},
            self.child,
        )
        self.nothing_for_the_child()
        self.assertEqual(self.lookup(self.parent)["state"], "active")

    def test_the_main_chat_is_untouched_by_its_subagents(self) -> None:
        label = self.label(self.parent)
        self.child_event("UserPromptSubmit", prompt="list")
        self.child_event("SessionEnd", reason="shutdown")
        self.assertEqual(self.label(self.parent), label)
        self.assertEqual(self.lookup(self.parent)["state"], "active")
        self.assertNotIn("ended", self.note(self.parent))


class InstallTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.link = self.link_program()  # what scripts/agent_sync/install.sh puts in ~/.local/bin

    def install(self, *argv: str, env: dict | None = None) -> tuple[int, dict]:
        out = io.StringIO()
        code = PL.main(["install", "--platform", "grok-leader", *argv], env=env if env is not None else self.env(), stdout=out)
        return code, json.loads(out.getvalue())

    def path(self) -> str:
        return os.path.join(self.grok, "hooks", "session-letter.json")

    def doc(self) -> dict:
        return json.loads(Path(self.path()).read_text())

    def test_it_writes_a_hook_file_of_its_own(self) -> None:
        code, answer = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(answer["changed"]), sorted(G.HOOK_TIMEOUTS))
        self.assertEqual(answer["path"], self.path())
        self.assertIn("nothing to approve", answer["trust"])
        self.assertEqual(os.stat(self.path()).st_mode & 0o777, 0o600)
        command = "%s/.local/bin/platform-letter hook --platform grok-leader" % self.home
        doc = self.doc()
        self.assertEqual(set(doc), {"hooks"})
        self.assertEqual(sorted(doc["hooks"]), ["SessionEnd", "SessionStart", "UserPromptSubmit"])  # no sub-agent hook
        for name, timeout in G.HOOK_TIMEOUTS.items():
            self.assertEqual(doc["hooks"][name], [{"hooks": [{"type": "command", "command": command, "timeout": timeout}]}], name)
        self.assertNotIn("--location", answer["command"])  # the Mac registers in process

    def test_it_is_idempotent_and_checkable(self) -> None:
        code, answer = self.install("--check")
        self.assertEqual(code, 1)
        self.assertFalse(any(answer["installed"].values()))
        self.assertFalse(os.path.exists(self.path()))  # a check writes nothing
        self.install()
        before = Path(self.path()).read_text()
        code, answer = self.install()
        self.assertEqual((code, answer["changed"]), (0, []))
        self.assertEqual(Path(self.path()).read_text(), before)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path())) if ".bak-letter-" in n], [])
        code, answer = self.install("--check")
        self.assertEqual(code, 0)
        self.assertTrue(all(answer["installed"].values()) and all(answer["current"].values()))

    def test_it_replaces_an_outdated_command_in_place_with_a_backup(self) -> None:
        self.install()
        doc = self.doc()
        doc["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] = "/old/place/platform-letter hook --platform grok-leader"
        Path(self.path()).write_text(json.dumps(doc))
        code, answer = self.install()
        self.assertEqual((code, answer["changed"]), (0, ["UserPromptSubmit"]))
        self.assertEqual(self.doc()["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "%s/.local/bin/platform-letter hook --platform grok-leader" % self.home)
        backup = answer["backup"]
        self.assertIn(".bak-letter-", backup)
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)

    def test_it_never_touches_a_hook_that_is_not_its_own(self) -> None:
        other = {"type": "command", "command": "/usr/local/bin/other-hook --x", "timeout": 5}
        Path(self.path()).parent.mkdir(parents=True)
        Path(self.path()).write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "new", "hooks": [other]}], "Stop": [{"hooks": [other]}]}}))
        self.install()
        doc = self.doc()
        self.assertEqual(doc["hooks"]["SessionStart"][0], {"matcher": "new", "hooks": [other]})  # kept, first
        self.assertEqual(len(doc["hooks"]["SessionStart"]), 2)
        self.assertEqual(doc["hooks"]["Stop"], [{"hooks": [other]}])
        code, answer = self.install("--remove")
        self.assertEqual(code, 0)
        doc = self.doc()
        self.assertEqual(doc["hooks"]["SessionStart"], [{"matcher": "new", "hooks": [other]}])
        self.assertEqual(doc["hooks"]["Stop"], [{"hooks": [other]}])
        self.assertNotIn("UserPromptSubmit", doc["hooks"])

    def test_remove_deletes_the_file_it_made_and_nothing_else(self) -> None:
        self.install()
        sibling = os.path.join(self.grok, "hooks", "seat-session.json")
        Path(sibling).write_text('{"hooks": {}}')
        code, answer = self.install("--remove")
        self.assertEqual(code, 0)
        self.assertTrue(answer.get("removed_file"))
        self.assertFalse(os.path.exists(self.path()))
        self.assertTrue(os.path.exists(sibling))
        code, answer = self.install("--remove")  # and again: nothing to do
        self.assertEqual((code, answer["changed"]), (0, []))

    def test_it_refuses_a_server_a_registry_host_and_a_host_without_grok(self) -> None:
        for argv in (["--location", "server"], ["--remote", "mac"], ["--location", "server", "--remote", "mac"]):
            code, answer = self.install(*argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("Mac only", answer["error"])
        shutil_home = os.path.join(self.tmp.name, "bare")
        os.makedirs(shutil_home)
        code, answer = self.install(env={"HOME": shutil_home, "AGENT_SYNC_STATE_DIR": self.state})
        self.assertEqual(code, 2)
        self.assertIn("no Grok home", answer["error"])
        self.assertEqual(os.listdir(shutil_home), [])  # no stray .grok folder
        code, _ = self.install("--check", env={"HOME": shutil_home})
        self.assertEqual(code, 1)
        self.assertEqual(os.listdir(shutil_home), [])
        self.assertFalse(os.path.exists(self.path()))

    def test_it_follows_grok_home(self) -> None:
        private = os.path.join(self.tmp.name, "private")
        os.makedirs(private)
        code, answer = self.install(env=self.env(GROK_HOME=private))
        self.assertEqual(code, 0)
        self.assertEqual(answer["path"], os.path.join(private, "hooks", "session-letter.json"))
        self.assertFalse(os.path.exists(self.path()))

    def test_it_refuses_a_file_that_is_not_a_hook_file(self) -> None:
        Path(self.path()).parent.mkdir(parents=True)
        Path(self.path()).write_text("[1, 2]")
        code, answer = self.install()
        self.assertEqual(code, 2)
        self.assertEqual(Path(self.path()).read_text(), "[1, 2]")


    # ---- the program the hooks run ----------------------------------------------------------

    def test_it_refuses_to_install_hooks_that_name_a_program_that_is_not_there(self) -> None:
        """Seen on the Mac: ~/.local/bin/platform-letter is only linked by scripts/agent_sync/install.sh.  Hooks that run
        a program that is not there fail with exit 127 on every Grok event and give no chat a letter."""
        for kind in ("missing", "dangling link", "not executable"):
            if os.path.lexists(self.link):
                os.unlink(self.link)
            if kind == "dangling link":
                os.symlink(os.path.join(self.tmp.name, "gone"), self.link)
            elif kind == "not executable":
                Path(self.link).write_text("#!/bin/sh\n")
                os.chmod(self.link, 0o644)
            code, answer = self.install()
            self.assertEqual(code, 2, kind)
            self.assertIn(self.link, answer["error"], kind)
            self.assertIn("install.sh", answer["error"], kind)
            self.assertFalse(os.path.exists(self.path()), kind)  # nothing was written
            self.assertNotIn("hooks", os.listdir(self.grok), kind)
        self.exe("home", ".local", "bin", "platform-letter")  # linked at last
        code, _ = self.install()
        self.assertEqual(code, 0)

    def test_a_check_says_whether_the_program_is_there_and_fails_when_it_is_not(self) -> None:
        self.install()
        code, answer = self.install("--check")
        self.assertEqual(code, 0)
        self.assertEqual((answer["program"], answer["program_ready"]), (self.link, True))
        os.unlink(self.link)
        code, answer = self.install("--check")
        self.assertEqual(code, 1)  # installed and current, and every event would fail with exit 127
        self.assertTrue(all(answer["installed"].values()) and all(answer["current"].values()))
        self.assertEqual((answer["program"], answer["program_ready"]), (self.link, False))

    def test_remove_does_not_need_the_program(self) -> None:
        self.install()
        os.unlink(self.link)
        code, answer = self.install("--remove")
        self.assertEqual(code, 0)
        self.assertTrue(answer.get("removed_file"))
        self.assertNotIn("program", answer)
        self.assertNotIn("warnings", answer)

    def test_the_answer_of_an_install_names_the_program(self) -> None:
        code, answer = self.install()
        self.assertEqual((code, answer["program"], answer["program_ready"]), (0, self.link, True))
        self.assertIn("install.sh", answer["trust"])

    # ---- what an owner should know about the host -------------------------------------------

    def config(self, text: str) -> None:
        Path(self.grok, "config.toml").write_text(text)

    def has_claude_warning(self, answer: dict) -> bool:
        return any("CLAUDE_CODE_ENTRYPOINT" in w for w in answer.get("warnings", []))

    def test_it_warns_while_grok_imports_the_hooks_of_claudes_settings(self) -> None:
        """Seen live with a stand-in for the Claude letter hook: with `[compat.claude] hooks` on and
        CLAUDE_CODE_ENTRYPOINT in Grok's environment, the imported hook ran on all three events and made a Claude row."""
        cases = {
            "no config": (None, True),  # Grok's default is on
            "on": ("[compat.claude]\nhooks = true\n", True),
            "unrelated cells only": ("[compat.claude]\nskills = true\n", True),
            "not a table": ("compat = 3\n", True),
            "a string is not false": ('[compat.claude]\nhooks = "false"\n', True),
            "cannot be read": ("[compat.claude\nhooks = false", True),
            "off": ("[compat.claude]\nhooks = false\n", False),  # the Mac's setting
            "off beside others": ("[compat.cursor]\nhooks = true\n\n[compat.claude]\nskills = true\nhooks = false\n", False),
        }
        for name, (text, warned) in cases.items():
            path = os.path.join(self.grok, "config.toml")
            if os.path.exists(path):
                os.unlink(path)
            if text is not None:
                self.config(text)
            for argv in (["--check"], []):
                _, answer = self.install(*argv)
                self.assertEqual(self.has_claude_warning(answer), warned, (name, argv))
                self.assertEqual(G.imports_claude_hooks(self.env()), warned, name)

    def test_it_says_when_the_hook_is_paused(self) -> None:
        self.config("[compat.claude]\nhooks = false\n")
        _, answer = self.install()
        self.assertNotIn("warnings", answer)  # nothing to say about a host that is as it should be
        Path(self.grok, G.OFF_FILE).write_text("")
        code, answer = self.install("--check")
        self.assertEqual(code, 0)
        self.assertEqual(len(answer["warnings"]), 1)
        self.assertIn(G.OFF_FILE, answer["warnings"][0])
        self.assertIn("paused", answer["warnings"][0])


class EntryScriptTests(Base):
    """The real `scripts/platform-letter`, as Grok's hook runner runs it: a payload on stdin, the environment, exit 0."""

    def run_entry(self, payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess:
        full = dict(os.environ)
        for name in list(full):
            if name.startswith(("GROK_", "CLAUDE_", "AGENT_", "SESSION_LETTER")):
                del full[name]
        full.update(env)
        return subprocess.run(
            [sys.executable, str(ENTRY), "hook", "--platform", "grok-leader", "--location", "mac"],
            input=json.dumps(payload), capture_output=True, text=True, env=full, timeout=60,
        )

    def test_a_start_registers_the_chat_and_exits_clean(self) -> None:
        sid = gid(1)
        self.session(sid)
        env = self.env(GROK_SESSION_ID=sid, GROK_HOOK_EVENT="session_start", GROK_LETTER_BIN=os.path.join(self.tmp.name, "none"))
        proc = self.run_entry({"hook_event_name": "SessionStart", "sessionId": sid, "session_id": sid, "source": "new"}, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(self.lookup(sid)["label"], PAIR)
        self.assertEqual(self.note(sid)["state"], "open")
        self.assertIn("Operator session letter", json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_a_hook_that_is_not_groks_is_silent_and_exits_clean(self) -> None:
        sid = gid(1)
        self.session(sid)
        proc = self.run_entry({"hook_event_name": "SessionStart", "session_id": sid}, self.env(CLAUDE_CODE_ENTRYPOINT="cli"))
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIsNone(self.lookup(sid))

    def test_garbage_on_stdin_is_not_a_chat_either(self) -> None:
        full = dict(os.environ, **self.env(GROK_SESSION_ID=gid(1), GROK_HOOK_EVENT="session_start"))
        proc = subprocess.run(
            [sys.executable, str(ENTRY), "hook", "--platform", "grok-leader", "--location", "mac"],
            input="{not json", capture_output=True, text=True, env=full, timeout=60,
        )
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_the_script_is_executable(self) -> None:
        self.assertTrue(os.stat(ENTRY).st_mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
