"""The fleet-wide compliance family:  the loader,  the validators,  the checks and the
two rules that run on this Mac.

Every test that writes a file writes it in a temporary directory.  The one test that
reads this Mac's real state is skipped unless ``FLEET_CONFIG_LIVE_TESTS`` is set,  and it
never prints a secret value:  the checks only ever count names and shapes.

    cd scripts && python3 -m unittest discover -s fleet_config/tests -t .
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fleet_config import contract
from fleet_config import manifest as manifest_module
from fleet_config.certs import ExpiryUnknown, not_after_from_pem, profile_expiry
from fleet_config.checks import CheckContext
from fleet_config.checks_compliance import (
    KnobAnswer,
    check_certificate_expiry,
    check_command,
    check_secrets_hygiene,
    read_knob,
)
from fleet_config.manifest import (
    ManifestError,
    load_manifest,
    validate_compliance_rules,
    validate_coverage,
    validate_platforms,
    validate_rules,
)
from fleet_config.types import (
    CheckSpec,
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    ProofLevel,
    ReasonCode,
    Rule,
    Threshold,
)

# The repository root, two levels above this file.
REPO = Path(__file__).resolve().parents[3]
SEED_FILE = REPO / "fleet-seats.json"
REGISTRY_FILE = REPO / "fleet-apps.json"

# The knob names the seeded compliance rules read.
CERT_KNOB = "COMPLIANCE_CERT_EXPIRY_DAYS"
SECRETS_KNOB = "COMPLIANCE_SECRETS_FINDINGS_MAX"

# A sentinel that must never reach a result,  a reason or the envelope.
SENTINEL = "sentinel-credential-value-must-never-be-printed"


def _compliance_rule(rule_id: str = "compliance-example", **overrides: Any) -> dict[str, Any]:
    """One compliance rule in the manifest's JSON shape."""
    rule: dict[str, Any] = {
        "id": rule_id,
        "kind": "compliance",
        "scope": ["mac"],
        "check": {"type": "secrets_hygiene", "target": "~/.secrets", "detail": ""},
        "expected": {"description": "a rule", "names": ["published:.github/workflows"]},
        "proofLevel": "L0",
        "ownerSeat": "CLAUDE",
        "autofixAllowed": False,
        "collector": "mac",
        "ttlSeconds": 3600,
        "implemented": True,
    }
    rule.update(overrides)
    return rule


def _manifest_dict(seats: dict[str, Any], compliance: list[Any] | None = None) -> dict[str, Any]:
    """A whole manifest document in its JSON shape."""
    document: dict[str, Any] = {"schemaVersion": 2, "seats": seats}
    if compliance is not None:
        document["complianceRules"] = compliance
    return document


def _seat_entry(platform: str = "claude") -> dict[str, Any]:
    """A seat entry with one rule for the one kind a fixture needs."""
    return {
        "platforms": [platform],
        "rules": [
            {
                "id": "%s-instructions" % platform,
                "kind": "instructions",
                "platforms": [platform],
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
        ],
        "notApplicable": [],
    }


class _Rule:
    """A compliance rule as the engine holds it,  built from a plain dict."""

    @staticmethod
    def build(**overrides: Any) -> Rule:
        """One rule with the fields a test needs,  and the rest at their defaults."""
        fields: dict[str, Any] = {
            "id": "compliance-example",
            "kind": ConfigKind.COMPLIANCE,
            "platforms": (),
            "check": CheckSpec(CheckType.SECRETS_HYGIENE, "~/.secrets", ""),
            "expected": ExpectedState(
                description="a rule", names=("published:.github/workflows",)
            ),
            "proof_level": ProofLevel.L0,
            "owner_seat": "CLAUDE",
            "autofix_allowed": False,
            "seat": "",
            "collector": Collector.MAC,
            "ttl_seconds": 3600,
            "scope": ("mac",),
            "threshold": Threshold(knob=SECRETS_KNOB, minimum=0, maximum=10, unit="findings"),
        }
        fields.update(overrides)
        return Rule(**fields)


class SeededComplianceTests(unittest.TestCase):
    """The seeded ``fleet-seats.json`` holds the compliance family,  and it is valid."""

    def setUp(self) -> None:
        self.manifest = load_manifest(SEED_FILE, registry_path=REGISTRY_FILE)

    def test_the_seeded_manifest_passes_every_validator(self) -> None:
        validate_coverage(self.manifest)
        validate_rules(self.manifest)
        validate_platforms(self.manifest, manifest_module.known_platforms())
        validate_compliance_rules(self.manifest)

    def test_the_seeded_compliance_family_is_present(self) -> None:
        self.assertTrue(self.manifest.compliance_rules)
        for rule in self.manifest.compliance_rules:
            self.assertIs(rule.kind, ConfigKind.COMPLIANCE)
            self.assertTrue(rule.owner_seat.strip())
            self.assertTrue(rule.scope)
            self.assertFalse(rule.platforms)
            self.assertEqual(rule.seat, "")

    def test_every_threshold_names_a_knob_the_registry_holds(self) -> None:
        from fleet_knobs.registry import BY_NAME

        for rule in self.manifest.compliance_rules:
            if rule.threshold is None:
                continue
            self.assertIn(
                rule.threshold.knob,
                BY_NAME,
                "the rule %s names a knob the fleet registry does not hold" % rule.id,
            )

    def test_every_rule_the_engine_can_run_names_a_check_it_registers(self) -> None:
        from fleet_config.checks import default_registry

        registry = default_registry()
        for rule in self.manifest.compliance_rules:
            if not rule.implemented:
                self.assertTrue(rule.implementation_reason.strip())
                continue
            self.assertIsNotNone(
                registry.get(rule.check.type),
                "the rule %s names a check the engine does not register" % rule.id,
            )

    def test_no_compliance_rule_allows_an_automatic_fix(self) -> None:
        for rule in self.manifest.compliance_rules:
            self.assertFalse(rule.autofix_allowed)

    def test_every_owner_seat_is_a_live_registry_seat(self) -> None:
        live = set(manifest_module.registry_seat_tags(REGISTRY_FILE))
        for rule in self.manifest.compliance_rules:
            self.assertIn(rule.owner_seat, live)

    def test_the_seed_generator_still_reproduces_the_file_byte_for_byte(self) -> None:
        from fleet_config import seed_manifest

        text = seed_manifest.render_manifest(seed_manifest.build_manifest())
        self.assertEqual(text, SEED_FILE.read_text(encoding="utf-8"))


class ComplianceValidationTests(unittest.TestCase):
    """The loader and the validator refuse an inconsistent fleet-wide rule."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry_path = self.root / "fleet-apps.json"
        self.registry_path.write_text(
            json.dumps({"seats": [{"tag": "CLAUDE"}, {"tag": "CODEX"}]}, indent=2) + "\n",
            encoding="utf-8",
        )

    def load(self, compliance: list[Any]):
        """Load a manifest document with the given compliance family."""
        document = _manifest_dict(
            {"CLAUDE": _seat_entry(), "CODEX": _seat_entry("codex")}, compliance
        )
        path = self.root / "fleet-seats.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return load_manifest(path, registry_path=self.registry_path)

    def test_a_valid_family_loads(self) -> None:
        manifest = self.load([_compliance_rule()])
        validate_compliance_rules(manifest)
        self.assertEqual(len(manifest.compliance_rules), 1)

    def test_a_rule_without_an_owner_is_refused(self) -> None:
        manifest = self.load([_compliance_rule(ownerSeat="")])
        with self.assertRaises(ManifestError) as caught:
            validate_compliance_rules(manifest)
        self.assertIn("owner seat", str(caught.exception))

    def test_a_rule_without_a_check_is_refused(self) -> None:
        document = _manifest_dict(
            {"CLAUDE": _seat_entry(), "CODEX": _seat_entry("codex")},
            [dict(_compliance_rule(), check=None)],
        )
        path = self.root / "fleet-seats.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(ManifestError):
            load_manifest(path, registry_path=self.registry_path)

    def test_an_unknown_collector_is_refused(self) -> None:
        document = _manifest_dict(
            {"CLAUDE": _seat_entry(), "CODEX": _seat_entry("codex")},
            [dict(_compliance_rule(), collector="satellite")],
        )
        path = self.root / "fleet-seats.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(ManifestError) as caught:
            load_manifest(path, registry_path=self.registry_path)
        self.assertIn("collector", str(caught.exception))

    def test_an_unknown_scope_is_refused(self) -> None:
        manifest = self.load([_compliance_rule(scope=["mars"])])
        with self.assertRaises(ManifestError) as caught:
            validate_compliance_rules(manifest)
        self.assertIn("scope", str(caught.exception))

    def test_a_duplicate_id_across_the_families_is_refused_by_the_loader(self) -> None:
        document = _manifest_dict(
            {"CLAUDE": _seat_entry(), "CODEX": _seat_entry("codex")},
            [_compliance_rule(id="claude-instructions")],
        )
        path = self.root / "fleet-seats.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(ManifestError) as caught:
            load_manifest(path, registry_path=self.registry_path)
        self.assertIn("duplicate rule id", str(caught.exception))

    def test_a_rule_that_is_not_implemented_must_say_why(self) -> None:
        manifest = self.load([_compliance_rule(implemented=False)])
        with self.assertRaises(ManifestError) as caught:
            validate_compliance_rules(manifest)
        self.assertIn("not implemented", str(caught.exception))

    def test_a_rule_that_allows_an_automatic_fix_is_refused(self) -> None:
        manifest = self.load([_compliance_rule(autofixAllowed=True)])
        with self.assertRaises(ManifestError) as caught:
            validate_compliance_rules(manifest)
        self.assertIn("automatic fix", str(caught.exception))

    def test_a_rule_whose_owner_is_not_a_live_seat_is_refused(self) -> None:
        with self.assertRaises(ManifestError) as caught:
            self.load([_compliance_rule(ownerSeat="GHOST")])
        self.assertIn("GHOST", str(caught.exception))

    def test_a_seat_rule_may_not_carry_a_fleet_threshold(self) -> None:
        document = _manifest_dict({"CLAUDE": _seat_entry(), "CODEX": _seat_entry("codex")})
        document["seats"]["CLAUDE"]["rules"][0]["threshold"] = {
            "knob": "COMPLIANCE_CERT_EXPIRY_DAYS",
            "minimum": 1,
            "maximum": 365,
        }
        path = self.root / "fleet-seats.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest = load_manifest(path, registry_path=self.registry_path)
        with self.assertRaises(ManifestError) as caught:
            validate_rules(manifest)
        self.assertIn("threshold", str(caught.exception))


class ThresholdTests(unittest.TestCase):
    """A threshold comes from a knob,  and a knob that cannot answer leaves the rule
    undecided."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ctx = CheckContext(
            home=self.home,
            now=datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc),
            timeout=5,
        )

    def _with_knob(self, value: int | None) -> None:
        self.ctx.cache["knob:" + SECRETS_KNOB] = KnobAnswer(
            SECRETS_KNOB, value, "infisical", False, "" if value is not None else "unset"
        )

    def test_an_unset_knob_leaves_the_rule_undecided(self) -> None:
        self._with_knob(None)
        result = check_secrets_hygiene(_Rule.build(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.ERROR)
        self.assertIn("has no value", result.reason)

    def test_a_knob_that_only_holds_its_default_leaves_the_rule_undecided(self) -> None:
        self.ctx.cache["knob:" + SECRETS_KNOB] = KnobAnswer(
            SECRETS_KNOB, 0, "default", False, "unset"
        )
        result = check_secrets_hygiene(_Rule.build(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn("is not set", result.reason)

    def test_a_knob_outside_the_range_leaves_the_rule_undecided(self) -> None:
        self._with_knob(99)
        result = check_secrets_hygiene(_Rule.build(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn("outside", result.reason)

    def test_a_knob_inside_the_range_lets_the_rule_decide(self) -> None:
        self._with_knob(0)
        result = check_secrets_hygiene(_Rule.build(), "mac", self.ctx)
        self.assertIn(result.state, (CheckState.PASS, CheckState.DRIFT))

    def test_the_knob_read_is_cached_on_the_run_context(self) -> None:
        answer = KnobAnswer(SECRETS_KNOB, 3, "infisical", False, "")
        self.ctx.cache["knob:" + SECRETS_KNOB] = answer
        self.assertIs(read_knob(SECRETS_KNOB, self.ctx), answer)


class SecretsHygieneTests(unittest.TestCase):
    """The secrets hygiene rule,  on a fixture home and a fixture checkout."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.home.mkdir(parents=True)
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        self.ctx = CheckContext(
            home=self.home,
            now=datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc),
            timeout=5,
        )
        self.ctx.cache["repoRoot"] = str(self.repo)
        self.ctx.cache["knob:" + SECRETS_KNOB] = KnobAnswer(
            SECRETS_KNOB, 0, "infisical", False, ""
        )

    def _rule(self, **overrides: Any) -> Rule:
        return _Rule.build(**overrides)

    def test_a_clean_machine_passes(self) -> None:
        result = check_secrets_hygiene(self._rule(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIn("no secrets-hygiene finding", result.reason)

    def test_the_retired_flat_key_directory_is_a_finding(self) -> None:
        (self.home / ".secrets").mkdir()
        (self.home / ".secrets" / "global-api-keys").write_text(SENTINEL, encoding="utf-8")
        result = check_secrets_hygiene(self._rule(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIn("retired secrets directory", result.reason)

    def test_a_published_file_that_names_an_internal_asset_is_a_finding(self) -> None:
        (self.repo / ".github" / "workflows" / "deploy.yml").write_text(
            "env:\n  INFISICAL_PROJECT_ID: 8dae6d03-8f12-48b2-adf3-e5c1b881c8c5\n",
            encoding="utf-8",
        )
        result = check_secrets_hygiene(self._rule(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIn("internal project or container id", result.reason)

    def test_a_published_file_with_an_internal_address_is_a_finding(self) -> None:
        (self.repo / ".github" / "workflows" / "deploy.yml").write_text(
            "run: ssh 100.101.102.103 true\n", encoding="utf-8"
        )
        result = check_secrets_hygiene(self._rule(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIn("internal host address", result.reason)

    def test_a_loopback_address_is_not_a_finding(self) -> None:
        (self.repo / ".github" / "workflows" / "deploy.yml").write_text(
            "run: curl 127.0.0.1:8799/health\n", encoding="utf-8"
        )
        result = check_secrets_hygiene(self._rule(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.PASS)

    def test_a_missing_published_surface_leaves_the_rule_undecided(self) -> None:
        rule = self._rule(expected=ExpectedState(description="a rule", names=("published:.github/nope",)))
        result = check_secrets_hygiene(rule, "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn("not there", result.reason)

    def test_no_secret_value_reaches_the_result_or_the_envelope(self) -> None:
        (self.home / ".secrets").mkdir()
        (self.home / ".secrets" / "global-api-keys").write_text(SENTINEL, encoding="utf-8")
        (self.repo / ".github" / "workflows" / "deploy.yml").write_text(
            "env:\n  INFISICAL_PROJECT_ID: 8dae6d03-8f12-48b2-adf3-e5c1b881c8c5\n",
            encoding="utf-8",
        )
        result = check_secrets_hygiene(self._rule(), "mac", self.ctx)
        self.assertNotIn(SENTINEL, result.reason)
        self.assertNotIn(SENTINEL, json.dumps(contract.observed_to_dict(result.observed)))
        self.assertNotIn("8dae6d03", result.reason)


class CommandCheckTests(unittest.TestCase):
    """The generic command runner:  a pass,  a drift,  and a refusal."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = CheckContext(
            home=Path(self.tmp.name),
            now=datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc),
            timeout=10,
        )

    def _rule(self, target: str, detail: str = "") -> Rule:
        return _Rule.build(
            id="compliance-command",
            check=CheckSpec(CheckType.COMMAND, target, detail),
            expected=ExpectedState(description="a command"),
        )

    def test_a_command_that_is_not_allowed_is_unknown_and_failing(self) -> None:
        result = check_command(self._rule("/bin/echo hello"), "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertEqual(result.reason_code, ReasonCode.ERROR)
        self.assertTrue(contract.is_failing(result))

    def test_a_command_that_is_never_named_is_unknown(self) -> None:
        result = check_command(self._rule(""), "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn("no command", result.reason)


class CertificateTests(unittest.TestCase):
    """Reading a certificate's expiry,  and the rule that uses it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = CheckContext(
            home=self.root,
            now=datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc),
            timeout=5,
        )
        self.ctx.cache["knob:" + CERT_KNOB] = KnobAnswer(
            CERT_KNOB, 30, "infisical", False, ""
        )

    def _rule(self, names: tuple[str, ...] = ()) -> Rule:
        return _Rule.build(
            id="compliance-cert",
            check=CheckSpec(CheckType.CERTIFICATE_EXPIRY, "", ""),
            expected=ExpectedState(description="a rule", names=names),
            threshold=Threshold(knob=CERT_KNOB, minimum=1, maximum=365, unit="days"),
        )

    def test_a_rule_with_no_inventory_leaves_the_rule_undecided(self) -> None:
        result = check_certificate_expiry(self._rule(), "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn("no certificate inventory", result.reason)

    def test_an_unknown_source_spelling_leaves_the_rule_undecided(self) -> None:
        result = check_certificate_expiry(self._rule(("vault:whatever",)), "mac", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIn("not one this check knows", result.reason)

    def test_a_profile_with_no_expiry_is_unknown(self) -> None:
        with self.assertRaises(ExpiryUnknown):
            profile_expiry(b"not a plist")

    @unittest.skipUnless(os.path.exists("/usr/bin/openssl"), "openssl is a macOS tool")
    def test_the_expiry_of_a_real_certificate_is_read(self) -> None:
        cert = self.root / "cert.pem"
        key = self.root / "key.pem"
        subprocess.run(
            [
                "/usr/bin/openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "30",
                "-nodes",
                "-subj",
                "/CN=fleet-config test",
            ],
            check=True,
            capture_output=True,
        )
        expires_at = not_after_from_pem(cert.read_text(encoding="utf-8"))
        self.assertGreater(expires_at, datetime.now(timezone.utc) + timedelta(days=20))


class LiveMachineTests(unittest.TestCase):
    """The two rules that run on this Mac,  against this Mac's real state.

    These tests read the real machine,  so they are skipped unless
    ``FLEET_CONFIG_LIVE_TESTS`` is set.  They never print a secret value:  the checks
    count names and shapes,  and the assertions read the state and the reason code.
    """

    @unittest.skipUnless(
        os.environ.get("FLEET_CONFIG_LIVE_TESTS"),
        "set FLEET_CONFIG_LIVE_TESTS=1 to run the checks against this Mac",
    )
    def test_the_two_real_rules_run_and_produce_a_verdict(self) -> None:
        from fleet_config.aggregator import run_status

        manifest = load_manifest(SEED_FILE, registry_path=REGISTRY_FILE)
        ctx = CheckContext(
            home=Path(os.environ.get("HOME", "/")).expanduser(),
            now=datetime.now(timezone.utc),
            timeout=60,
        )
        report = run_status(manifest, kind=ConfigKind.COMPLIANCE, ctx=ctx)
        states = {result.rule_id: result for result in report.results}
        for rule_id in ("compliance-secrets-hygiene", "compliance-apple-cert-expiry"):
            self.assertIn(rule_id, states)
            result = states[rule_id]
            self.assertIn(
                result.state,
                (CheckState.PASS, CheckState.DRIFT, CheckState.UNKNOWN),
            )
            self.assertNotIn(SENTINEL, result.reason)


if __name__ == "__main__":  # pragma: no cover - the runner is the discover command
    unittest.main()
