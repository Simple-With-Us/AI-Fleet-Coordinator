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
