"""The native wire fixture must name the actual helper, transaction and schema bytes."""

import hashlib
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "muster/MusterTests/Fixtures/operator-settings-wire.json"
PRODUCERS = {
    "scripts/fleet-ops-control.py",
    "scripts/simple_tools/server/eavesdropper_settings_edit.py",
    "scripts/simple_tools/server/operator_settings.py",
    "scripts/simple_tools/operator_github_state.py",
}
SCHEMA = "scripts/fleet-ops-eavesdropper-settings.json"


class OperatorSettingsFixtureTests(unittest.TestCase):
    def test_generated_fixture_matches_the_actual_producers_and_schema(self):
        artifact = json.loads(FIXTURE.read_text())
        self.assertIs(artifact["synthetic"], True)
        self.assertEqual(set(artifact["producer_sha256"]), PRODUCERS)
        for relative in PRODUCERS:
            self.assertEqual(
                artifact["producer_sha256"][relative],
                hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(),
                relative,
            )
        self.assertEqual(artifact["schema_path"], SCHEMA)
        self.assertEqual(artifact["schema_sha256"], hashlib.sha256((ROOT / SCHEMA).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
