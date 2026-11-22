import json
import tempfile
import unittest
from pathlib import Path

from review_alerts.config import configured


class Config(unittest.TestCase):
    def test_missing_is_inert_and_env_can_disable(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"SIMPLE_TOOLS_STATE_DIR": d}
            self.assertEqual(configured(env), env)
            p = Path(d) / "review-alerts/config.json"
            p.parent.mkdir()
            p.write_text(json.dumps({"RA_ENABLED": "true", "RA_WAKE_KEY_FILE": "/protected/path"}))
            p.chmod(0o600)
            self.assertEqual(configured(env)["RA_ENABLED"], "true")
            self.assertEqual(configured(dict(env, RA_ENABLED="false"))["RA_ENABLED"], "false")

    def test_secret_values_unknown_duplicate_world_readable_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "review-alerts/config.json"
            p.parent.mkdir()
            env = {"SIMPLE_TOOLS_STATE_DIR": d}
            for body in (
                '{"RA_WAKE_KEY":"not-allowed"}',
                '{"RA_ENABLED":"true","RA_ENABLED":"false"}',
                '{"RA_ENABLED":true}',
            ):
                p.write_text(body)
                p.chmod(0o600)
                with self.assertRaises(Exception):
                    configured(env)
            p.write_text("{}")
            p.chmod(0o644)
            with self.assertRaises(Exception):
                configured(env)
            p.unlink()
            p.symlink_to(p.parent / "other")
            (p.parent / "other").write_text("{}")
            with self.assertRaises(Exception):
                configured(env)
