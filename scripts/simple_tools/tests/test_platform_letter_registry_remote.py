from __future__ import annotations
import ast
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools.tests.test_platform_letter import Base


class RegistryRemoteTests(Base):
    """LETTER_REGISTRY_REMOTE:  the registry host named outright, decoupled from where the chat runs.

    Same contract as the Claude letter's:  the destination wins over the Mac-local default, a
    failure of it is an error answer and never a quiet local write, and this host keeps no
    registry file of its own."""

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
            self.test.assertEqual(words[:2], ["$HOME/.local/bin/platform-letter", "registry"])
            out = io.StringIO()
            code = PL.registry_main(words[2:], env=self.test.remote_env(), stdout=out)
            return SimpleNamespace(stdout=out.getvalue(), returncode=code)

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
        code = PL.hook(self.spec, self.env(**extra), io.StringIO(json.dumps(payload)), out,
                       runner=runner, platform="darwin")
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def remote_lookup(self, sid: str, location: str = "mac") -> dict | None:
        reg = OR.SessionRegistry(self.remote_db)
        try:
            return reg.lookup("codex", location, "default", sid)
        finally:
            reg.close_db()

    def test_a_mac_session_registers_over_the_hop_and_keeps_no_local_file(self) -> None:
        ssh = self.RemoteSsh(self)
        self.mac_hook({"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "startup"},
                      ssh, LETTER_REGISTRY_REMOTE="registry-host")
        self.assertEqual(len(ssh.calls), 1)
        self.assertIsNotNone(self.remote_lookup("c-0001"))
        self.assertIsNone(self.remote_lookup("c-0001", location="server"),
                          "the location stays where the session runs")
        self.assertFalse(os.path.exists(self.db), "no local registry file is created")

    def test_a_mac_session_without_the_setting_registers_locally_as_before(self) -> None:
        OR.SessionRegistry(self.db).close_db()  # the Mac's registry exists, as it does today
        ssh = self.RemoteSsh(self, fail=AssertionError("the hop must not be tried"))
        self.mac_hook({"hook_event_name": "SessionStart", "session_id": "c-0002", "source": "startup"}, ssh)
        self.assertEqual(ssh.calls, [])
        reg = OR.SessionRegistry(self.db)
        try:
            self.assertIsNotNone(reg.lookup("codex", "mac", "default", "c-0002"))
        finally:
            reg.close_db()

    def test_a_failed_hop_is_an_error_answer_and_never_a_local_fallback(self) -> None:
        ssh = self.RemoteSsh(self, fail=OSError("no route"))
        out = self.mac_hook({"hook_event_name": "SessionStart", "session_id": "c-0003", "source": "startup"},
                            ssh, LETTER_REGISTRY_REMOTE="registry-host")
        self.assertEqual(len(ssh.calls), 1)
        self.assertFalse(os.path.exists(self.db), "a failed hop never becomes a local write")
        self.assertIsNone(self.remote_lookup("c-0003"))

    def test_a_destination_that_is_not_plain_is_refused_by_its_own_name(self) -> None:
        ssh = self.RemoteSsh(self, fail=AssertionError("a refused destination is never run"))
        out = self.mac_hook({"hook_event_name": "SessionStart", "session_id": "c-0004", "source": "startup"},
                            ssh, LETTER_REGISTRY_REMOTE="-oProxyCommand=touch /tmp/x")
        self.assertEqual(ssh.calls, [])
        self.assertIn("LETTER_REGISTRY_REMOTE does not name an ssh destination",
                      (out or {}).get("additionalContext", ""))
        self.assertFalse(os.path.exists(self.db))
