from __future__ import annotations
import contextlib
import io
import json
import os
import random
import shlex
import sqlite3
import string
import subprocess
import tempfile
import time
import unittest
from datetime import UTC, datetime
from unittest import mock
from types import SimpleNamespace
from simple_tools import claude_app_lineage as AL
from simple_tools import claude_letter as CL
from simple_tools import operator_registry as OR
from simple_tools.tests.test_claude_letter import Base


class RegistryRemoteTests(Base):
    """LETTER_REGISTRY_REMOTE:  the registry host named outright, decoupled from where the chat runs.

    A Mac chat (platform darwin, no location override) registers locally while no destination is
    named, and over the hop once one is:  the destination wins, a failure of it is an error answer
    and never a quiet local write, and this host keeps no registry file of its own."""

    class RemoteSsh:
        """The hop to the registry host:  checks the command's shape and runs the registry side
        in process against the REMOTE state dir, so the two sides never share a file."""

        def __init__(self, test: "RegistryRemoteTests", fail: Exception | None = None):
            self.test, self.fail, self.calls = test, fail, []

        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            if self.fail is not None:
                raise self.fail
            self.test.assertEqual(
                cmd[:7], ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "--"]
            )
            self.test.assertEqual(cmd[7], "registry-host")
            words = shlex.split(cmd[8])
            self.test.assertEqual(words[:3], ["$HOME/.local/bin/simple-chat", "letter", "registry"])
            out = io.StringIO()
            code = CL.registry_main(words[3:], env=self.test.remote_env(), stdout=out)
            return SimpleNamespace(stdout=out.getvalue(), returncode=code)

        def ops(self) -> list[str]:
            return [shlex.split(c[8])[3] for c in self.calls]

    def setUp(self) -> None:
        super().setUp()
        self.remote_state = os.path.join(self.tmp.name, "remote-state")
        self.remote_db = os.path.join(self.remote_state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.remote_db).close_db()  # the canonical registry, on the registry host
        os.remove(self.db)  # once the registry lives elsewhere this host keeps no copy at all

    def remote_env(self, **extra: str) -> dict[str, str]:
        return self.env(SIMPLE_TOOLS_STATE_DIR=self.remote_state, **extra)

    def mac_hook(self, payload: dict, runner, **extra: str):
        out = io.StringIO()
        env = self.env(**extra)
        code = CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=runner, platform="darwin")
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def start(self, sid: str, runner, **extra: str):
        return self.mac_hook(
            {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"}, runner, **extra
        )

    def remote_lookup(self, sid: str, location: str = "mac") -> dict | None:
        reg = OR.SessionRegistry(self.remote_db)
        try:
            return reg.lookup(CL.PLATFORM, location, CL.SCOPE, sid)
        finally:
            reg.close_db()

    def test_a_mac_session_registers_over_the_hop_and_keeps_no_local_file(self) -> None:
        ssh = self.RemoteSsh(self)
        out = self.start("a-0001", ssh, LETTER_REGISTRY_REMOTE="registry-host")
        self.assertEqual(ssh.ops(), ["register"])
        rec = self.remote_lookup("a-0001")
        self.assertIsNotNone(rec)
        self.assertIsNone(self.remote_lookup("a-0001", location="server"),
                          "the location stays where the session runs")
        self.assertFalse(os.path.exists(self.db), "no local registry file is created")

    def test_a_mac_session_without_the_setting_registers_locally_as_before(self) -> None:
        OR.SessionRegistry(self.db).close_db()  # the Mac's registry exists, as it does today
        ssh = self.RemoteSsh(self, fail=AssertionError("the hop must not be tried"))
        out = self.start("a-0002", ssh)
        self.assertEqual(ssh.calls, [])
        self.assertTrue(os.path.exists(self.db), "the Mac-local registry is used")
        reg = OR.SessionRegistry(self.db)
        try:
            self.assertIsNotNone(reg.lookup(CL.PLATFORM, "mac", CL.SCOPE, "a-0002"))
        finally:
            reg.close_db()

    def test_a_failed_hop_is_an_error_answer_and_never_a_local_fallback(self) -> None:
        ssh = self.RemoteSsh(self, fail=OSError("no route"))
        out = self.start("a-0003", ssh, LETTER_REGISTRY_REMOTE="registry-host")
        self.assertEqual(ssh.ops(), ["register"])
        self.assertIn("No Operator letter yet", (out or {}).get("additionalContext", ""))
        self.assertFalse(os.path.exists(self.db), "a failed hop never becomes a local write")
        self.assertIsNone(self.remote_lookup("a-0003"))

    def test_a_destination_that_is_not_plain_is_refused_by_its_own_name(self) -> None:
        ssh = self.RemoteSsh(self, fail=AssertionError("a refused destination is never run"))
        out = self.start("a-0004", ssh, LETTER_REGISTRY_REMOTE="-oProxyCommand=touch /tmp/x")
        self.assertEqual(ssh.calls, [])
        self.assertIn("LETTER_REGISTRY_REMOTE does not name an ssh destination",
                      (out or {}).get("additionalContext", ""))
        self.assertFalse(os.path.exists(self.db))

    def test_the_parent_is_resolved_where_the_app_record_is(self) -> None:
        # Chat A is started on this Mac;  the desktop app then starts chat B from A.  The registry
        # host holds no copy of the app's records, so the hook names the parent it resolved here.
        from simple_tools.tests.test_claude_app_lineage import write as write_app_record

        write_app_record(self.home, "local_11111111-1111-4111-8111-111111111111",
                         {"cliSessionId": "33333333-3333-4333-8333-333333333333"})
        write_app_record(self.home, "local_22222222-2222-4222-8222-222222222222",
                         {"cliSessionId": "44444444-4444-4444-8444-444444444444",
                          "spawnedFrom": {"sessionId": "local_11111111-1111-4111-8111-111111111111",
                                          "title": "t", "handOff": True}})
        ssh = self.RemoteSsh(self)
        self.start("33333333-3333-4333-8333-333333333333", ssh,
                   LETTER_REGISTRY_REMOTE="registry-host",
                   CLAUDE_CODE_HOST_SESSION_ID="local_11111111-1111-4111-8111-111111111111")
        self.start("44444444-4444-4444-8444-444444444444", ssh,
                   LETTER_REGISTRY_REMOTE="registry-host",
                   CLAUDE_CODE_HOST_SESSION_ID="local_22222222-2222-4222-8222-222222222222")
        self.assertEqual(ssh.ops(), ["register", "register"])
        argv = shlex.split(ssh.calls[1][8])
        self.assertIn("--parent-session-id=33333333-3333-4333-8333-333333333333", argv)
        child = self.remote_lookup("44444444-4444-4444-8444-444444444444")
        self.assertIsNotNone(child)
        parent = self.remote_lookup("33333333-3333-4333-8333-333333333333")
        self.assertEqual(child["label"][0], parent["label"][0], "the child takes the parent's pair")
