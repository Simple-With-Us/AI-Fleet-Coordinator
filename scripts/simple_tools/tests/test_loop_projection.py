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
            before = path.stat()
            with mock.patch.object(P.sys, "argv", ["projection", "--config", str(path), "--project-id", "p", "--apply"]), mock.patch.object(P.sys, "stdin", io.StringIO(json.dumps(row))), mock.patch.object(P.os, "fsync", wraps=P.os.fsync) as sync, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(P.main(), 0)
            self.assertEqual(sync.call_count, 2)
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (before.st_uid, before.st_gid))
            self.assertIn("wake_loop_prevention = false", path.read_text())

    def test_directory_sync_failure_reports_applied_not_refused(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest import mock
        row = {"secret": {"secretKey": P.KEY, "secretValue": "false", "workspace": "p", "environment": "prod", "secretPath": "/zulip"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listener.toml"
            path.write_text("[daemon]\nowner_user_id = 12\n")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(P.sys, "argv", ["projection", "--config", str(path), "--project-id", "p", "--apply"]), mock.patch.object(P.sys, "stdin", io.StringIO(json.dumps(row))), mock.patch.object(P.os, "fsync", side_effect=[None, OSError("test")]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(P.main(), 0)
            report = json.loads(out.getvalue())
            self.assertTrue(report["applied"])
            self.assertFalse(report["directory_synced"])
            self.assertIn("durability not confirmed", err.getvalue())
            self.assertNotIn("refused", err.getvalue())
            self.assertIn("wake_loop_prevention = false", path.read_text())

    def test_converged_apply_retries_directory_sync(self):
        import contextlib
        import io
        import json
        import tempfile
        from unittest import mock
        row = {"secret": {"secretKey": P.KEY, "secretValue": "false", "workspace": "p", "environment": "prod", "secretPath": "/zulip"}}
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "listener.toml"
                old = "[daemon]\nwake_loop_prevention = false\nowner_user_id = 12\n"
                path.write_text(old)
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(P.sys, "argv", ["projection", "--config", str(path), "--project-id", "p", "--apply"]), mock.patch.object(P.sys, "stdin", io.StringIO(json.dumps(row))), mock.patch.object(P.os, "fsync", side_effect=OSError("test") if failure else None) as sync, mock.patch.object(P.os, "replace") as replace, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(P.main(), 0)
                report = json.loads(out.getvalue())
                self.assertFalse(report["changed"])
                self.assertTrue(report["applied"])
                self.assertEqual(report["directory_synced"], not failure)
                self.assertEqual(sync.call_count, 1)
                replace.assert_not_called()
                self.assertEqual(path.read_text(), old)
                self.assertEqual("durability not confirmed" in err.getvalue(), failure)
