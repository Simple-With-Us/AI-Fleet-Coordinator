import importlib.util
import unittest
from unittest.mock import patch

s = importlib.util.spec_from_file_location("deploy", "scripts/deploy-mapped-worker.py")
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


class Checks(unittest.TestCase):
    def test_partial_rollout_is_not_exact_receipt(self):
        with patch.object(
            m,
            "api",
            return_value={"deployments": [{"versions": [{"version_id": "a", "percentage": 50}]}]},
        ):
            self.assertIsNone(m.deployed_tag("x"))

    def test_exact_tag(self):
        with patch.object(
            m,
            "api",
            side_effect=[
                {"deployments": [{"versions": [{"version_id": "a", "percentage": 100}]}]},
                {"annotations": {"workers/tag": "sha"}},
            ],
        ):
            self.assertEqual(m.deployed_tag("x"), "sha")

    def test_unconfigured_probe_blocks(self):
        with self.assertRaises(RuntimeError):
            m.probe({"name": "x"})

    def test_probe_sends_explicit_user_agent(self):
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return b'{"ok": true}'

        def fake(req, timeout):
            seen.append(req)
            return Resp()

        with patch.object(m.urllib.request, "urlopen", side_effect=fake):
            m.probe({"name": "x", "health": "https://example.invalid/health", "ok": True})
        self.assertEqual(seen[0].get_header("User-agent"), m.USER_AGENT)
        self.assertFalse(seen[0].get_header("User-agent").startswith("Python-urllib"))

    def test_targets_are_explicit(self):
        self.assertNotIn("start-jays-services", m.TARGETS)
        self.assertNotIn("fleet-share-api", m.TARGETS)


if __name__ == "__main__":
    unittest.main()
