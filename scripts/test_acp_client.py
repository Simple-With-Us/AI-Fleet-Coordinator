#!/usr/bin/env python3
"""Unit tests for grok-acp permission auto-approve and ACP terminals.  No live serve."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts.test_session_disk import RuntimeInfisicalFixture

ROOT = Path(__file__).resolve().parents[1]
ACP = ROOT / "scripts" / "grok-acp-runtime"
sys.path.insert(0, str(ACP))


def _load():
    import importlib.util

    path = ACP / "acp-client.py"
    spec = importlib.util.spec_from_file_location("acp_client_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["acp_client_under_test"] = mod
    if importlib.util.find_spec("websockets") is None:
        with mock.patch.dict(sys.modules, {"websockets": types.SimpleNamespace()}):
            spec.loader.exec_module(mod)
    else:
        spec.loader.exec_module(mod)
    return mod


MOD = _load()


class PermissionTests(unittest.TestCase):
    def test_secret_from_environment_skips_child_reader(self):
        with (
            mock.patch.dict("os.environ", {"GROK_AGENT_SECRET": "env-secret"}),
            mock.patch.object(MOD.subprocess, "run") as run,
        ):
            self.assertEqual(MOD.load_secret(), "env-secret")
        run.assert_not_called()

    def test_secret_uses_absolute_isolated_shared_cli_against_loopback_provider(self):
        with RuntimeInfisicalFixture() as provider, tempfile.TemporaryDirectory() as home:
            env = {
                "HOME": home,
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": "/hostile",
                "FLEET_SECRETS_API": provider.url,
                "INFISICAL_TOKEN": "st.test-only-grok-key",
                "INFISICAL_FLEET_OPS_PROJECT_ID": provider.project,
                "INFISICAL_ENVIRONMENT": "prod",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                self.assertEqual(MOD.load_secret(), "fixture-grok-secret")
        self.assertTrue(any("GROK_AGENT_SECRET" in call for call in provider.calls))
        self.assertTrue(any("%2Fgrok-acp" in call for call in provider.calls))

    def test_prefers_allow_always(self):
        params = {
            "options": [
                {"optionId": "allow-once", "kind": "allow_once"},
                {"optionId": "allow-always", "kind": "allow_always"},
                {"optionId": "reject", "kind": "reject_once"},
            ]
        }
        out = MOD.pick_permission_result(params)
        self.assertEqual(out["outcome"]["outcome"], "selected")
        self.assertEqual(out["outcome"]["optionId"], "allow-always")

    def test_falls_back_to_allow_once(self):
        params = {
            "options": [
                {"optionId": "allow-once", "kind": "allow_once"},
                {"optionId": "reject-once", "kind": "reject_once"},
            ]
        }
        out = MOD.pick_permission_result(params)
        self.assertEqual(out["outcome"]["optionId"], "allow-once")

    def test_no_allow_cancels(self):
        params = {"options": [{"optionId": "reject", "kind": "reject_once"}]}
        out = MOD.pick_permission_result(params)
        self.assertEqual(out["outcome"]["outcome"], "cancelled")

    def test_empty_options_cancels(self):
        out = MOD.pick_permission_result({"options": []})
        self.assertEqual(out["outcome"]["outcome"], "cancelled")


class RpcShapeTests(unittest.TestCase):
    def test_response_not_request(self):
        self.assertTrue(MOD.is_rpc_response({"jsonrpc": "2.0", "id": 3, "result": {}}))
        self.assertTrue(MOD.is_rpc_response({"id": 3, "error": {"code": 1, "message": "x"}}))
        self.assertFalse(
            MOD.is_rpc_response(
                {"jsonrpc": "2.0", "id": 3, "method": "session/request_permission", "params": {}}
            )
        )
        self.assertFalse(MOD.is_rpc_response({"method": "session/update", "params": {}}))


class TerminalTests(unittest.TestCase):
    def test_echo_roundtrip(self):
        hub = MOD.TerminalHub()
        created = hub.create(
            {"command": "printf", "args": ["perm-ok\n"], "cwd": tempfile.gettempdir()}
        )
        tid = created["terminalId"]
        self.assertTrue(tid)
        waited = hub.wait_sync({"terminalId": tid})
        self.assertEqual(waited["exitCode"], 0)
        out = hub.output({"terminalId": tid})
        self.assertIn("perm-ok", out["output"])
        self.assertFalse(out["truncated"])
        hub.release({"terminalId": tid})

    def test_shell_string_without_args(self):
        hub = MOD.TerminalHub()
        created = hub.create({"command": "echo shell-ok", "cwd": tempfile.gettempdir()})
        hub.wait_sync({"terminalId": created["terminalId"]})
        out = hub.output({"terminalId": created["terminalId"]})
        self.assertIn("shell-ok", out["output"])
        hub.close_all()

    def test_unknown_terminal_errors(self):
        hub = MOD.TerminalHub()
        with self.assertRaises(KeyError):
            hub.output({"terminalId": "nope"})


if __name__ == "__main__":
    unittest.main()
