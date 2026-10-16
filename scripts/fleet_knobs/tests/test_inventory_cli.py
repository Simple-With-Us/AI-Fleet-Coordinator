"""INFISICAL.md stays in step with the registry;  the status CLI shows no values;  packaging."""
from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fleet_knobs import __main__ as cli
from fleet_knobs import inventory, knobs
from fleet_knobs.registry import BY_NAME, REGISTRY

from ._fake import SIGNING, UNRELATED, KnobTestCase

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
REPO = SCRIPTS.parent

# The inventory the open-loops plan (K1) names, key by key, plus FLEET_LOOPS_DATA_ALLOWED_APPS:  the one decision the
# F7 publisher's repository gate leaves to the owner (which installed apps may read fleet-loops-data;  empty by default), and
# FLEET_FACTS_PUBLISH_FULL_COPY (the path-bearing mac-facts.json is published only when the owner turns it on) and the publisher's
# time cap and load limit (FLEET_FACTS_PUBLISH_CAP_MINUTES, FLEET_FACTS_PUBLISH_MAX_LOAD1).
PLAN_KEYS = {
    "FLEET_AUTOPUSH_MODE", "FLEET_AUTOPUSH_REPOS", "FLEET_AUTOPUSH_IDLE_COMMIT",
    "FLEET_AUTOPUSH_IDLE_HOURS", "FLEET_AUTOPUSH_PUBLIC_DRAFTS", "FLEET_FACTS_SIGNING_KEY",
    "NEEDS_OWNER_NOTIFY_PAUSED", "NEEDS_OWNER_CAPS", "OWNER_LINEAR_USER_ID",
    "CLAIM_EXPIRY_MODE", "CLAIM_EXPIRY_CAPS", "CLAIM_EXPIRY_LAUNCH_AT",
    "ASK_CAPTURE_ENABLED", "ASK_CAPTURE_SHADOW", "ASK_CAPTURE_CAPS",
    "LANE_SWEEP_MODE", "LANE_SWEEP_CAPS", "LANE_SWEEP_OPTIN_REPOS", "CONSULT_ENABLED",
    "FLEET_LOOPS_DATA_ALLOWED_APPS", "FLEET_FACTS_PUBLISH_FULL_COPY", "FLEET_FACTS_PUBLISH_CAP_MINUTES", "FLEET_FACTS_PUBLISH_MAX_LOAD1",
    "NOTES_RELAY_CONSUMER",  # AFC-331:  the Apple Notes relay consumer's seat
}


class InventoryTests(unittest.TestCase):
    def test_the_registry_holds_every_planned_knob_and_only_one_secret(self):
        self.assertEqual({s.name for s in REGISTRY}, PLAN_KEYS)
        self.assertEqual([s.name for s in REGISTRY if s.secret], ["FLEET_FACTS_SIGNING_KEY"])

    def test_infisical_md_carries_the_generated_block(self):
        text = (REPO / "INFISICAL.md").read_text(encoding="utf-8")
        self.assertTrue(
            inventory.check(text),
            "INFISICAL.md is out of step with scripts/fleet_knobs/registry.py;  from scripts/ run "
            "`python3 -m fleet_knobs inventory --write ../INFISICAL.md` and commit the result")
        for spec in REGISTRY:
            self.assertEqual(text.count(f"| `{spec.name}` |"), 1, spec.name)

    def test_drift_is_detected_and_repaired(self):
        text = (REPO / "INFISICAL.md").read_text(encoding="utf-8")
        drifted = text.replace("| `CONSULT_ENABLED` | C7 |", "| `CONSULT_ENABLED` | C9 |")
        self.assertNotEqual(drifted, text)
        self.assertFalse(inventory.check(drifted))
        self.assertEqual(inventory.splice(drifted), text)
        with self.assertRaises(ValueError):
            inventory.splice("no markers here")

    def test_each_row_names_owner_default_and_secret_flag(self):
        for row in inventory.rows():
            cells = [c.strip() for c in row.strip("|").split("|")]
            self.assertEqual(len(cells), 7, row)
        secret = [r for r in inventory.rows() if "FLEET_FACTS_SIGNING_KEY" in r][0]
        self.assertIn("| secret |", secret)
        self.assertIn("fails closed", secret)


class CliTests(KnobTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            code = cli.main(list(argv), out)
        return code, out.getvalue(), err.getvalue()

    def test_status_shows_sources_and_never_a_value(self):
        self.fake.set(secrets={"FLEET_AUTOPUSH_MODE": "snapshot", "FLEET_FACTS_SIGNING_KEY": SIGNING,
                               "QDRANT_API_KEY": UNRELATED})
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        rows = {r["name"]: r for r in doc["knobs"]}
        self.assertEqual(set(rows), {s.name for s in REGISTRY})
        self.assertEqual(rows["FLEET_AUTOPUSH_MODE"]["source"], "infisical")
        self.assertTrue(rows["FLEET_FACTS_SIGNING_KEY"]["set"])
        self.assertEqual(rows["CONSULT_ENABLED"]["source"], "default")
        for text in (out, err):
            for value in (SIGNING, UNRELATED, "snapshot"):
                self.assertNotIn(value, text)

    def test_status_exits_3_when_infisical_is_unreachable(self):
        self.fake.set(mode="fail")
        code, out, _err = self.run_cli("status")
        self.assertEqual(code, 3)
        self.assertIn("fetch: failed (exit-1)", out)
        self.assertIn("stale", out)

    def test_get_prints_a_non_secret_and_refuses_a_secret(self):
        self.fake.set(secrets={"FLEET_AUTOPUSH_REPOS": "a/b,c/d", "FLEET_FACTS_SIGNING_KEY": SIGNING})
        self.assertEqual(self.run_cli("get", "FLEET_AUTOPUSH_REPOS")[:2], (0, "a/b,c/d\n"))
        code, out, err = self.run_cli("get", "FLEET_FACTS_SIGNING_KEY")
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn(SIGNING, err)
        self.assertEqual(self.run_cli("get", "NOT_A_KNOB")[0], 2)

    def test_get_warns_on_stderr_when_stale(self):
        self.fake.set(mode="fail")
        code, out, err = self.run_cli("get", "CLAIM_EXPIRY_MODE")
        self.assertEqual((code, out), (0, "dry-run\n"))
        self.assertIn("stale", err)

    def test_inventory_check_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "INFISICAL.md"
            target.write_text("# x\n\n<!-- fleet-knobs:begin -->\nold\n<!-- fleet-knobs:end -->\n\ntail\n")
            self.assertEqual(self.run_cli("inventory", "--check", str(target))[0], 1)
            self.assertEqual(self.run_cli("inventory", "--write", str(target))[0], 0)
            self.assertEqual(self.run_cli("inventory", "--check", str(target))[0], 0)
            self.assertTrue(target.read_text().endswith("\n\ntail\n"))
        code, out, _ = self.run_cli("inventory")
        self.assertEqual((code, out), (0, inventory.block() + "\n"))


class PackagingTests(KnobTestCase):
    def test_a_copy_of_just_this_package_runs_under_isolated_python(self):
        """The packaging rule:  a stable copy run with `python3 -I` carries every package it
        imports.  Copy only fleet_knobs (no tests, no fleet_rag) and run it."""
        dest = self.root / "stable"
        shutil.copytree(SCRIPTS / "fleet_knobs", dest / "fleet_knobs",
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
        self.fake.set(secrets={"CLAIM_EXPIRY_MODE": "ping-only", "FLEET_FACTS_SIGNING_KEY": SIGNING})
        code = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "import fleet_knobs\n"
            "from fleet_knobs import knobs\n"
            "assert fleet_knobs.__file__.startswith(sys.argv[1]), fleet_knobs.__file__\n"
            "print(knobs.get('CLAIM_EXPIRY_MODE'), knobs.report()['fetch'])\n"
            "sys.exit(__import__('fleet_knobs.__main__', fromlist=['main']).main(['get', 'FLEET_AUTOPUSH_MODE']))\n")
        env = dict(os.environ)
        for var in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(var, None)
        proc = subprocess.run([sys.executable, "-I", "-c", code, str(dest)], env=env,
                              capture_output=True, text=True, timeout=60, cwd=str(self.root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split("\n")[:2], ["ping-only ok", "off"])
        self.assertNotIn(SIGNING, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
