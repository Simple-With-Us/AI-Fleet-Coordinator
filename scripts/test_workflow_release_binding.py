#!/usr/bin/env python3
"""Static release-source binding assertions (AFC-653).

Two bindings keep a release provable through a merge storm:

1. .github/workflows/ci.yml must never cancel an in-progress run on main.
   A main CI run that has started always finishes, so a merge storm ends
   with a completed CI run on the newest push (the single pending slot
   still moves to the newest push; an older queued run may be replaced,
   and completion itself is never guaranteed - only freedom from
   concurrency cancellation).
2. .github/workflows/alert-workers-deploy.yml must check out the exact
   head the triggering CI run proved.  scripts/deploy-alert-workers.py
   releases the checkout's HEAD and requires it to equal current main
   with a completed successful CI run for that exact sha;  binding the
   checkout to the triggering run's head_sha keeps the tested tree, the
   proven sha and the released source one value, and turns the
   moved-on-main case into the script's clean "Superseded" skip.

Text assertions, not YAML parsing:  the binding must stay literal and
greppable, and this suite needs no third-party packages.
"""

import os
import re
import unittest

WF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows")


class CiMainConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = open(os.path.join(WF, "ci.yml")).read()

    def _concurrency_block(self):
        m = re.search(r"^concurrency:\n(?:  .*\n)+", self.raw, re.MULTILINE)
        self.assertIsNotNone(m, "no top-level concurrency block")
        return m.group(0)

    def test_main_runs_never_cancelled_in_progress(self):
        self.assertIn(
            "cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}",
            self._concurrency_block(),
        )

    def test_concurrency_group_stays_per_ref(self):
        self.assertIn(
            "group: ci-${{ github.repository }}-${{ github.ref }}",
            self._concurrency_block(),
        )

    def test_permissions_unchanged(self):
        self.assertIn("permissions:\n  contents: read", self.raw)


class DeployCheckoutBinding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = open(os.path.join(WF, "alert-workers-deploy.yml")).read()

    def test_checkout_selects_triggering_head(self):
        self.assertIn(
            "ref: ${{ github.event_name == 'workflow_run' && github.event.workflow_run.head_sha || 'main' }}",
            self.raw,
        )

    def test_gate_still_requires_triggering_ci_success(self):
        self.assertIn(
            "github.event.workflow_run.event == 'push' && github.event.workflow_run.conclusion == 'success'",
            self.raw,
        )

    def test_deploy_concurrency_stays_serial_without_cancel(self):
        m = re.search(r"^concurrency:\n(?:  .*\n)+", self.raw, re.MULTILINE)
        self.assertIsNotNone(m, "no top-level concurrency block")
        block = m.group(0)
        self.assertIn("group: alert-workers-production", block)
        self.assertIn("cancel-in-progress: false", block)


if __name__ == "__main__":
    unittest.main()
