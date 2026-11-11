#!/usr/bin/env python3
"""Tests for scripts/check-ci-runner-policy.py.

    python3 scripts/test_check_ci_runner_policy.py

Pins policy_states_ban() against AGENTS.md's CI runners ban line and scan_inventory().

CI: .github/workflows/ci.yml runs this file in the step named
``CI runner policy lint tracks the AGENTS.md ban text``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "check-ci-runner-policy.py"


def _load():
    sys.path.insert(0, str(SCRIPTS))
    loader = importlib.machinery.SourceFileLoader("check_ci_runner_policy", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


mod = _load()
# The ban lives in AGENTS.md;  the FLEET-PROTOCOL.md mirror is a tools manual now.
AGENTS = (SCRIPTS.parent / "AGENTS.md").read_text()


class PolicyBan(unittest.TestCase):
    def test_live_agents_md_states_ban(self) -> None:
        self.assertTrue(mod.policy_states_ban(AGENTS))

    def test_current_wording_without_markdown(self) -> None:
        self.assertTrue(
            mod.policy_states_ban(
                "- Local Mac self-hosted runners are permanently banned.  Never start."
            )
        )

    def test_legacy_permanently_banned_heading_still_matches(self) -> None:
        self.assertTrue(mod.policy_states_ban("Local Mac Runner PERMANENTLY BANNED for all repos."))

    def test_missing_ban_is_false(self) -> None:
        self.assertFalse(mod.policy_states_ban("# CI\n- Use coolify runners only.\n"))


class ScanInventory(unittest.TestCase):
    def test_live_runner_row_is_offender(self) -> None:
        row = "| actions.runner.foo | mac-xcode26 | Always-on |"
        hits = mod.scan_inventory(row)
        self.assertEqual(len(hits), 1)

    def test_retired_runner_row_is_ignored(self) -> None:
        row = "| actions.runner.foo | mac-xcode26 | Retired |"
        self.assertEqual(mod.scan_inventory(row), [])


class DefaultPolicy(unittest.TestCase):
    def test_default_policy_resolves_to_agents_md(self) -> None:
        self.assertEqual(mod.DEFAULT_POLICY.name, "AGENTS.md")
        self.assertTrue(mod.DEFAULT_POLICY.is_file())


if __name__ == "__main__":
    unittest.main()
