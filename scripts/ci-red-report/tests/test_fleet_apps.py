"""fleet-apps.json loader tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleet_apps


class FleetAppsTests(unittest.TestCase):
    def test_load_repo_names_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fleet-apps.json"
            path.write_text(
                json.dumps(
                    {
                        "apps": [
                            {"repo": "A"},
                            {"repo": "B"},
                            {"repo": "A"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            names = fleet_apps.load_fleet_repo_names(path=path)
            self.assertEqual(names, ["A", "B"])

    def test_missing_fleet_token_note_names_secret(self) -> None:
        note = fleet_apps.missing_fleet_token_visibility_note(scanning_repo="AI-Fleet-Coordinator")
        self.assertIn("FLEET_READ_TOKEN", note)
        self.assertIn("AI-Fleet-Coordinator", note)
        self.assertIn("fleet-apps.json", note)


if __name__ == "__main__":
    unittest.main()
