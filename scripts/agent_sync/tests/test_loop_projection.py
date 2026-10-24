import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location("loop_projection", Path(__file__).resolve().parents[1] / "server/project_loop_prevention.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

class LoopProjectionTests(unittest.TestCase):
    def test_single_setting_and_idempotent(self):
        old = '[daemon]\nowner_user_id = 12\n[seat.CLAUDE]\nwake = "http"\n'
        new = P.project(old, False)
        self.assertIn('wake_loop_prevention = false', new)
        self.assertEqual(P.project(new, False), new)
        self.assertIn('owner_user_id = 12', new)
        self.assertIn('wake = "http"', new)
        self.assertIn('wake_loop_prevention = true', P.project(new, True))
    def test_only_current_matching_source(self):
        row = {"secret": {"secretKey": P.KEY, "secretValue": "false", "workspace": "p", "environment": "prod", "secretPath": "/zulip"}}
        self.assertFalse(P.verified_value(row, "p", "prod", "/zulip"))
        for field, value in (("workspace", "other"), ("environment", "dev"), ("secretPath", "/"), ("secretValue", "no")):
            copy = {"secret": {**row["secret"], field: value}}
            with self.assertRaises(ValueError): P.verified_value(copy, "p", "prod", "/zulip")
    def test_invalid_config_is_not_replaced(self):
        with self.assertRaises(ValueError): P.project('[seat.X]\nwake="http"\n', False)

    def test_malformed_source_is_rejected_without_values(self):
        for row in (None, [], "payload", {"secret": None}, {"secret": []}, {"secret": {"secretValue": False}}):
            with self.subTest(row_type=type(row).__name__):
                with self.assertRaises(ValueError):
                    P.verified_value(row, "p", "prod", "/zulip")

    def test_apply_syncs_file_and_directory(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest import mock
        row = {"secret": {"secretKey": P.KEY, "secretValue": "false", "workspace": "p", "environment": "prod", "secretPath": "/zulip"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listener.toml"
            path.write_text("[daemon]\nowner_user_id = 12\n")
            with mock.patch.object(P.sys, "argv", ["projection", "--config", str(path), "--project-id", "p", "--apply"]), mock.patch.object(P.sys, "stdin", io.StringIO(json.dumps(row))), mock.patch.object(P.os, "fsync", wraps=P.os.fsync) as sync, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(P.main(), 0)
            self.assertEqual(sync.call_count, 2)
            self.assertIn("wake_loop_prevention = false", path.read_text())
