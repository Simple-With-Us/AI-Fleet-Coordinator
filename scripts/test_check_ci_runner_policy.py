#!/usr/bin/env python3
"""Tests for scripts/check-ci-runner-policy.py policy wording and inventory scan."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-ci-runner-policy.py"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_ci_runner_policy", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


lint = _load()


class PolicyBanWordingTests(unittest.TestCase):
    def test_current_agent_sync_wording(self) -> None:
        text = (
            "- **Local Mac self-hosted runners are permanently banned.**  "
            "Never start, spawn, re-enable or configure `trading-live-mac-ci`."
        )
        self.assertTrue(lint.policy_states_ban(text))

    def test_legacy_heading_wording(self) -> None:
        self.assertTrue(
            lint.policy_states_ban("Local Mac Runner PERMANENTLY BANNED from CI.")
        )

    def test_singular_runner(self) -> None:
        self.assertTrue(
            lint.policy_states_ban("Local Mac self-hosted runner is permanently banned.")
        )

    def test_unrelated_text_is_not_a_ban(self) -> None:
        self.assertFalse(lint.policy_states_ban("Use ubuntu-latest in GitHub Actions."))


class ScanInventoryTests(unittest.TestCase):
    def test_live_runner_row_is_an_offender(self) -> None:
        text = (
            "| actions.runner.Simple-With-Us.trading-live-mac | Always-on | ~/actions-runner |\n"
            "| restic backup | Running | nightly |\n"
        )
        offenders = lint.scan_inventory(text)
        self.assertEqual(len(offenders), 1)
        self.assertIn("actions.runner", offenders[0][1])

    def test_retired_runner_row_is_ignored(self) -> None:
        text = "| actions.runner.old | Retired | was ~/actions-runner |\n"
        self.assertEqual(lint.scan_inventory(text), [])


if __name__ == "__main__":
    unittest.main()
