"""The registry checker, extended with the desired-state manifest check.

The checker is loaded by path, because its file name holds a hyphen.  Every test
writes its files in a temporary directory, and no test writes to the real home or
to a real repository.

    cd scripts && python3 -m unittest discover -s fleet_config/tests -t .
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO / "scripts" / "check-fleet-registry.py"
SEED_FILE = REPO / "fleet-seats.json"
REGISTRY_FILE = REPO / "fleet-apps.json"

# The seats the fixture registry holds live.
FIXTURE_SEATS = ("CLAUDE", "CODEX")


def _load_checker() -> ModuleType:
    """The checker module, loaded from its path."""
    spec = importlib.util.spec_from_file_location("check_fleet_registry", CHECKER_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - a broken checkout
        raise RuntimeError("cannot load %s" % CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rule(rule_id: str, kind: str, platforms: list, **overrides) -> dict:
    """One rule in the manifest's JSON shape."""
    rule = {
        "id": rule_id,
        "kind": kind,
        "platforms": list(platforms),
        "check": {
            "type": "marked_block_hash",
            "target": "~/.claude/CLAUDE.md",
            "detail": "fleet-lane-map",
        },
        "expected": {"description": "a rule", "present": True, "sha256": "0" * 64},
        "proofLevel": "L0",
        "ownerSeat": "CLAUDE",
        "autofixAllowed": False,
        "collector": "mac",
        "ttlSeconds": 900,
    }
    rule.update(overrides)
    return rule


class RegistryCheckerManifestTests(unittest.TestCase):
    """The checker rejects a bad manifest and passes a good one."""

    def setUp(self) -> None:
        self.checker = _load_checker()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry_path = self.root / "fleet-apps.json"
        self.registry_path.write_text(
            json.dumps({"seats": [{"tag": "CLAUDE"}, {"tag": "CODEX"}]}, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write(self, document: dict) -> Path:
        path = self.root / "fleet-seats.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _good_document(self) -> dict:
        covers = [
            {"kind": kind, "reason": "this harness does not have that kind", "platforms": []}
            for kind in ("instructions", "hooks", "mcp", "skills", "subagents", "permissions")
        ]
        return {
            "schemaVersion": 1,
            "seats": {
                "CLAUDE": {
                    "platforms": ["claude"],
                    "rules": [_rule("claude-instructions", "instructions", ["claude"])],
                    "notApplicable": [cover for cover in covers if cover["kind"] != "instructions"],
                },
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [_rule("codex-instructions", "instructions", ["codex"])],
                    "notApplicable": [cover for cover in covers if cover["kind"] != "instructions"],
                },
            },
        }

    def test_the_seeded_manifest_passes_the_checker(self) -> None:
        errors = self.checker.manifest_errors(manifest_path=SEED_FILE, registry_path=REGISTRY_FILE)
        self.assertEqual(errors, [])

    def test_an_absent_manifest_is_a_skip_and_not_a_failure(self) -> None:
        errors = self.checker.manifest_errors(
            manifest_path=self.root / "absent.json", registry_path=self.registry_path
        )
        self.assertEqual(errors, [])

    def test_a_good_manifest_passes(self) -> None:
        path = self._write(self._good_document())
        errors = self.checker.manifest_errors(manifest_path=path, registry_path=self.registry_path)
        self.assertEqual(errors, [])

    def test_a_manifest_with_an_unknown_seat_is_rejected(self) -> None:
        document = self._good_document()
        document["seats"]["GHOST"] = document["seats"]["CLAUDE"]
        path = self._write(document)
        errors = self.checker.manifest_errors(manifest_path=path, registry_path=self.registry_path)
        self.assertTrue(errors)
        self.assertIn("GHOST", errors[0])

    def test_a_manifest_with_an_uncovered_triple_is_rejected(self) -> None:
        document = self._good_document()
        document["seats"]["CLAUDE"]["notApplicable"] = [
            cover
            for cover in document["seats"]["CLAUDE"]["notApplicable"]
            if cover["kind"] != "hooks"
        ]
        path = self._write(document)
        errors = self.checker.manifest_errors(manifest_path=path, registry_path=self.registry_path)
        self.assertTrue(errors)
        self.assertIn("hooks", errors[0])

    def test_a_manifest_with_a_bad_proof_level_is_rejected(self) -> None:
        document = self._good_document()
        document["seats"]["CLAUDE"]["rules"][0]["proofLevel"] = "L1"
        path = self._write(document)
        errors = self.checker.manifest_errors(manifest_path=path, registry_path=self.registry_path)
        self.assertTrue(errors)
        self.assertIn("L1", errors[0])

    def test_a_manifest_with_an_unknown_platform_is_rejected(self) -> None:
        document = self._good_document()
        document["seats"]["CLAUDE"]["platforms"] = ["not-a-platform"]
        path = self._write(document)
        errors = self.checker.manifest_errors(manifest_path=path, registry_path=self.registry_path)
        self.assertTrue(errors)
        self.assertIn("not-a-platform", errors[0])

    def test_a_manifest_that_is_not_json_is_rejected(self) -> None:
        path = self.root / "fleet-seats.json"
        path.write_text("{not json", encoding="utf-8")
        errors = self.checker.manifest_errors(manifest_path=path, registry_path=self.registry_path)
        self.assertTrue(errors)

    def test_an_added_app_row_does_not_break_the_manifest_check(self) -> None:
        """A new app in the registry (Muster) leaves the seats, and the manifest, alone."""
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["apps"] = [
            {
                "repo": "Simple-With-Us/Muster",
                "acronym": "MUS",
                "digestColor": "123456",
                "badgeClass": "badge-mus",
                "iconFile": "agent-logos/muster.png",
                "hasAppIcon": False,
            }
        ]
        self.registry_path.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
        path = self._write(self._good_document())
        errors = self.checker.manifest_errors(manifest_path=path, registry_path=self.registry_path)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
