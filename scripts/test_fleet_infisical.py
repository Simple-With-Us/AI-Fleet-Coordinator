"""The shared client works without the legacy application and preserves the verified scope boundary."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fleet_infisical
from fleet_knobs.tests._fake import ScopedReadAPI


class SharedClientTests(unittest.TestCase):
    def test_legacy_import_is_an_alias_to_one_client(self):
        from fleet_secrets import core

        self.assertIs(core, fleet_infisical)
        self.assertIs(core.SecretsError, fleet_infisical.SecretsError)

    def test_real_scoped_read_runs_without_legacy_application_or_executable(self):
        scripts = str(Path(__file__).resolve().parent)
        boot = """
import json,sys
sys.path.insert(0, sys.argv[1])
sys.modules['fleet_secrets'] = None
sys.modules['fleet_secrets.core'] = None
from fleet_knobs import source
import fleet_infisical
assert source._load_helper() is fleet_infisical
print(json.dumps(source.fetch({'OPERATOR_API_URL'})))
"""
        with (
            tempfile.TemporaryDirectory() as home,
            ScopedReadAPI({"OPERATOR_API_URL": "https://operator.invalid"}) as api,
        ):
            env = {"HOME": home, "PATH": "/unavailable", **api.env}
            for wrong_scope in (False, True):
                with self.subTest(wrong_scope=wrong_scope):
                    api.requests.clear()
                    if wrong_scope:
                        api.metadata_override["permissions"] = ["read", "write"]
                    proc = subprocess.run(
                        [sys.executable, "-I", "-B", "-c", boot, scripts],
                        env=env,
                        cwd=home,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if wrong_scope:
                        self.assertNotEqual(proc.returncode, 0)
                        self.assertEqual(api.requests, ["/api/v2/service-token"])
                    else:
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(
                            json.loads(proc.stdout),
                            {"OPERATOR_API_URL": "https://operator.invalid"},
                        )
                        self.assertEqual(len(api.requests), 2)
                    self.assertNotIn(api.token, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
