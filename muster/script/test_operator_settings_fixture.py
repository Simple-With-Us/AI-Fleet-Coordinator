"""The native wire fixture must name the actual helper, transaction and schema bytes."""

import importlib.util
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
    def test_all_cases_are_actual_offline_helper_receipts(self):
        generator_path = pathlib.Path(__file__).with_name("make_operator_settings_fixture.py")
        spec = importlib.util.spec_from_file_location("operator_fixture_generator", generator_path)
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        actual = generator.generate()
        artifact = json.loads(FIXTURE.read_text())
        self.assertEqual(len(actual["cases"]), 15)
        self.assertEqual(generator.semantic(actual), generator.semantic(artifact))

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
        self.assertEqual(
            artifact["schema_sha256"], hashlib.sha256((ROOT / SCHEMA).read_bytes()).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()
