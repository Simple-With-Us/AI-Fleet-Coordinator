"""The manifest:  the loader, the three validators and the seeded ``fleet-seats.json``.

Every test that writes a file writes it in a temporary directory, and no test reads
the real home or writes to a real repository.  The tests that read the seeded
``fleet-seats.json`` and the installer tables read them, and write nothing.

    cd scripts && python3 -m unittest discover -s fleet_config/tests -t .
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fleet_config import manifest as manifest_module
from fleet_config.manifest import (
    ManifestError,
    load_manifest,
    registry_seat_tags,
    validate_coverage,
    validate_platforms,
    validate_rules,
)

# The repository root, two levels above this file.
REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
SEED_FILE = REPO / "fleet-seats.json"
REGISTRY_FILE = REPO / "fleet-apps.json"

# The two seats the fixture registry holds, so a test can break one thing at a time.
FIXTURE_SEATS = ("CLAUDE", "CODEX")


def _rule(rule_id: str, kind: str, platforms: list[str], **overrides: Any) -> dict[str, Any]:
    """One rule in the manifest's JSON shape, with the fields a test needs."""
    rule: dict[str, Any] = {
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


def _manifest_dict(seats: dict[str, Any]) -> dict[str, Any]:
    """A whole manifest document in its JSON shape."""
    return {"schemaVersion": 1, "seats": seats}


class ManifestFixture(unittest.TestCase):
    """A base class with a temporary directory and a two-seat fixture registry."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry_path = self.root / "fleet-apps.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "seats": [
                        {"tag": "CLAUDE"},
                        {"tag": "CODEX"},
                        {"tag": "MONET", "retired": True},
                        {"tag": "GROK-BUILD", "retired": True, "aliasOf": "GROK"},
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def write_manifest(self, document: dict[str, Any]) -> Path:
        """Write a manifest document into the temporary directory and return its path."""
        path = self.root / "fleet-seats.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def load(self, document: dict[str, Any]):
        """Load a manifest document from the temporary directory."""
        return load_manifest(self.write_manifest(document), registry_path=self.registry_path)


class RosterTests(ManifestFixture):
    """The live roster comes from the registry, never from the manifest."""

    def test_registry_roster_skips_retired_and_alias_rows(self) -> None:
        tags = registry_seat_tags(self.registry_path)
        self.assertEqual(tags, FIXTURE_SEATS)

    def test_loader_refuses_an_unknown_seat_key(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": [], "rules": [], "notApplicable": []},
                "CODEX": {"platforms": [], "rules": [], "notApplicable": []},
                "GHOST": {"platforms": [], "rules": [], "notApplicable": []},
            }
        )
        with self.assertRaises(ManifestError) as caught:
            self.load(document)
        self.assertIn("GHOST", str(caught.exception))

    def test_loader_refuses_a_retired_seat_key(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": [], "rules": [], "notApplicable": []},
                "CODEX": {"platforms": [], "rules": [], "notApplicable": []},
                "MONET": {"platforms": [], "rules": [], "notApplicable": []},
            }
        )
        with self.assertRaises(ManifestError) as caught:
            self.load(document)
        self.assertIn("MONET", str(caught.exception))

    def test_loader_refuses_a_live_seat_with_no_entry(self) -> None:
        document = _manifest_dict({"CLAUDE": {"platforms": [], "rules": [], "notApplicable": []}})
        with self.assertRaises(ManifestError) as caught:
            self.load(document)
        self.assertIn("CODEX", str(caught.exception))

    def test_loader_refuses_a_duplicate_rule_id(self) -> None:
        rule = _rule("dup-rule", "instructions", ["claude"])
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": ["claude"], "rules": [rule], "notApplicable": []},
                "CODEX": {
                    "platforms": ["claude"],
                    "rules": [dict(rule, platforms=["claude"])],
                    "notApplicable": [],
                },
            }
        )
        with self.assertRaises(ManifestError) as caught:
            self.load(document)
        self.assertIn("dup-rule", str(caught.exception))

    def test_loader_refuses_a_missing_file(self) -> None:
        with self.assertRaises(ManifestError):
            load_manifest(self.root / "absent.json", registry_path=self.registry_path)


class CoverageTests(ManifestFixture):
    """Every (seat, platform, kind) triple needs a rule or an explicit cover."""

    def test_an_uncovered_triple_is_rejected_and_named(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {
                    "platforms": ["claude"],
                    "rules": [_rule("claude-instructions", "instructions", ["claude"])],
                    "notApplicable": [],
                },
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [_rule("codex-instructions", "instructions", ["codex"])],
                    "notApplicable": [],
                },
            }
        )
        loaded = self.load(document)
        with self.assertRaises(ManifestError) as caught:
            validate_coverage(loaded)
        message = str(caught.exception)
        self.assertIn("CLAUDE", message)
        self.assertIn("claude", message)
        self.assertIn("hooks", message)

    def test_a_not_applicable_entry_covers_every_platform_of_the_seat(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {
                    "platforms": ["claude"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "not this harness", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "not this harness", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
            }
        )
        validate_coverage(self.load(document))

    def test_a_seat_with_no_platform_needs_one_cover_per_kind(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {
                    "platforms": [],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "hosted seat", "platforms": []}
                        for kind in ("instructions", "hooks", "mcp")
                    ],
                },
                "CODEX": {
                    "platforms": [],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "hosted seat", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
            }
        )
        with self.assertRaises(ManifestError) as caught:
            validate_coverage(self.load(document))
        message = str(caught.exception)
        self.assertIn("CLAUDE", message)
        self.assertIn("skills", message)

    def test_a_cover_without_a_reason_is_rejected(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {
                    "platforms": ["claude"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "hosted seat", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
            }
        )
        with self.assertRaises(ManifestError) as caught:
            validate_coverage(self.load(document))
        self.assertIn("no reason", str(caught.exception))


class PlatformTests(ManifestFixture):
    """A platform id the installer tables do not define is rejected."""

    def test_an_unknown_platform_is_rejected_and_named(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {
                    "platforms": ["not-a-platform"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "hosted seat", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "hosted seat", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
            }
        )
        loaded = self.load(document)
        with self.assertRaises(ManifestError) as caught:
            validate_platforms(loaded, ("claude", "codex"))
        self.assertIn("not-a-platform", str(caught.exception))

    def test_a_known_platform_passes(self) -> None:
        document = _manifest_dict(
            {
                "CLAUDE": {
                    "platforms": ["claude"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "hosted seat", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [],
                    "notApplicable": [
                        {"kind": kind, "reason": "hosted seat", "platforms": []}
                        for kind in (
                            "instructions",
                            "hooks",
                            "mcp",
                            "skills",
                            "subagents",
                            "permissions",
                        )
                    ],
                },
            }
        )
        validate_platforms(self.load(document), ("claude", "codex"))

    def test_the_known_set_covers_every_platform_the_seed_names(self) -> None:
        known = set(manifest_module.known_platforms())
        document = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        named = set()
        for entry in document["seats"].values():
            named.update(entry.get("platforms") or [])
            for rule in entry.get("rules") or []:
                named.update(rule.get("platforms") or [])
            for cover in entry.get("notApplicable") or []:
                named.update(cover.get("platforms") or [])
        self.assertTrue(named)
        self.assertEqual(sorted(named - known), [])


class RuleTests(ManifestFixture):
    """A rule must be internally consistent."""

    def test_a_proof_level_above_l0_outside_hooks_is_rejected(self) -> None:
        rule = _rule("claude-instructions", "instructions", ["claude"], proofLevel="L1")
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": ["claude"], "rules": [rule], "notApplicable": []},
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [_rule("codex-instructions", "instructions", ["codex"])],
                    "notApplicable": [],
                },
            }
        )
        loaded = self.load(document)
        with self.assertRaises(ManifestError) as caught:
            validate_rules(loaded)
        self.assertIn("L1", str(caught.exception))

    def test_a_check_type_the_kind_does_not_support_is_rejected(self) -> None:
        rule = _rule(
            "claude-skills",
            "skills",
            ["claude"],
            check={
                "type": "hook_registered",
                "target": "~/.claude/settings.json",
                "detail": "PreToolUse",
            },
        )
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": ["claude"], "rules": [rule], "notApplicable": []},
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [_rule("codex-instructions", "instructions", ["codex"])],
                    "notApplicable": [],
                },
            }
        )
        with self.assertRaises(ManifestError) as caught:
            validate_rules(self.load(document))
        self.assertIn("skills", str(caught.exception))

    def test_a_rule_with_no_platform_is_rejected(self) -> None:
        rule = _rule("claude-instructions", "instructions", [])
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": ["claude"], "rules": [rule], "notApplicable": []},
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [_rule("codex-instructions", "instructions", ["codex"])],
                    "notApplicable": [],
                },
            }
        )
        with self.assertRaises(ManifestError) as caught:
            validate_rules(self.load(document))
        self.assertIn("no platform", str(caught.exception))

    def test_a_rule_for_a_platform_the_seat_does_not_list_is_rejected(self) -> None:
        rule = _rule("claude-instructions", "instructions", ["codex"])
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": ["claude"], "rules": [rule], "notApplicable": []},
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [_rule("codex-instructions", "instructions", ["codex"])],
                    "notApplicable": [],
                },
            }
        )
        with self.assertRaises(ManifestError) as caught:
            validate_rules(self.load(document))
        self.assertIn("does not list", str(caught.exception))

    def test_a_rule_with_no_owner_seat_is_rejected(self) -> None:
        rule = _rule("claude-instructions", "instructions", ["claude"], ownerSeat="")
        document = _manifest_dict(
            {
                "CLAUDE": {"platforms": ["claude"], "rules": [rule], "notApplicable": []},
                "CODEX": {
                    "platforms": ["codex"],
                    "rules": [_rule("codex-instructions", "instructions", ["codex"])],
                    "notApplicable": [],
                },
            }
        )
        with self.assertRaises(ManifestError) as caught:
            validate_rules(self.load(document))
        self.assertIn("owner seat", str(caught.exception))


class SeededManifestTests(unittest.TestCase):
    """The seeded ``fleet-seats.json`` is the desired state, and it is current."""

    def setUp(self) -> None:
        self.document = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        self.manifest = load_manifest(SEED_FILE, registry_path=REGISTRY_FILE)

    def test_the_seeded_manifest_passes_every_validator(self) -> None:
        validate_coverage(self.manifest)
        validate_rules(self.manifest)
        validate_platforms(self.manifest, manifest_module.known_platforms())

    def test_the_seeded_manifest_covers_every_live_seat(self) -> None:
        live = registry_seat_tags(REGISTRY_FILE)
        seeded = sorted(seat.seat for seat in self.manifest.seats)
        self.assertEqual(seeded, sorted(live))

    def test_the_seed_generator_reproduces_the_file_byte_for_byte(self) -> None:
        from fleet_config import seed_manifest

        text = seed_manifest.render_manifest(seed_manifest.build_manifest())
        self.assertEqual(text, SEED_FILE.read_text(encoding="utf-8"))

    def test_the_seed_generator_is_deterministic(self) -> None:
        from fleet_config import seed_manifest

        first = seed_manifest.render_manifest(seed_manifest.build_manifest())
        second = seed_manifest.render_manifest(seed_manifest.build_manifest())
        self.assertEqual(first, second)

    def test_every_instruction_hash_comes_from_the_installer_source(self) -> None:
        """The expected hash is the block body the installer writes, not the installed file."""
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from fleet_lanes import install_rules

        checked = 0
        for seat in self.manifest.seats:
            for rule in seat.rules:
                if rule.kind.value != "instructions":
                    continue
                platform = rule.platforms[0]
                row = install_rules.PLATFORM_BY_NAME[platform]
                self.assertIsNotNone(row.rel_path, "%s has no rules file" % platform)
                self.assertEqual(rule.check.target, "~/" + str(row.rel_path))
                self.assertEqual(rule.check.detail, "fleet-lane-map")
                lines = install_rules.block_lines(str(row.variant))
                digest = hashlib.sha256(("\n".join(lines[1:-1]) + "\n").encode("utf-8")).hexdigest()
                self.assertEqual(rule.expected.sha256, digest, "rule %s" % rule.id)
                checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_every_rule_id_is_unique_and_well_formed(self) -> None:
        seen = set()
        for seat in self.manifest.seats:
            for rule in seat.rules:
                self.assertNotIn(rule.id, seen)
                seen.add(rule.id)
                self.assertRegex(rule.id, r"^[a-z0-9-]+$")

    def test_every_not_applicable_entry_carries_a_reason(self) -> None:
        for seat in self.manifest.seats:
            for cover in seat.not_applicable:
                self.assertTrue(cover.reason.strip(), "%s: %s" % (seat.seat, cover.kind.value))

    def test_no_rule_holds_a_secret_value(self) -> None:
        """A credential rule carries a mode and a presence flag, never a value."""
        for seat in self.manifest.seats:
            for rule in seat.rules:
                if rule.check.type.value != "credential_stat":
                    continue
                self.assertIn(rule.expected.mode, (None, "0600"))
                self.assertIsNone(rule.expected.sha256)
                self.assertEqual(rule.expected.names, ())


if __name__ == "__main__":
    unittest.main()
