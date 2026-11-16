#!/usr/bin/env python3
"""Static assertions on .github/workflows/hold-guard.yml: the privileged
(pull_request_target) workflow must never execute code from a PR-controlled
ref, must use a SHA-pinned checkout, and must keep minimal permissions.

Rationale: a repo writer can create an arbitrary branch and target a PR at
it; pull_request_target grants the GITHUB_TOKEN write scopes, so any code
taken from pull_request.base.ref or head refs would be writer-controlled.
"""

import os
import re
import unittest

import yaml

WF = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "..",
    ".github",
    "workflows",
    "hold-guard.yml",
)


class WorkflowStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = open(WF).read()
        cls.doc = yaml.safe_load(cls.raw)

    def test_checkout_pinned_to_full_sha(self):
        steps = self.doc["jobs"]["guard"]["steps"]
        co = next(
            s for s in steps if "uses" in s and str(s["uses"]).startswith("actions/checkout@")
        )
        ref = str(co["uses"]).split("@", 1)[1].split()[0]
        self.assertRegex(ref, r"^[0-9a-f]{40}$", "checkout must be SHA-pinned")

    def test_scripts_always_from_default_branch(self):
        steps = self.doc["jobs"]["guard"]["steps"]
        co = next(
            s for s in steps if "uses" in s and str(s["uses"]).startswith("actions/checkout@")
        )
        self.assertEqual(co["with"]["ref"], "${{ github.event.repository.default_branch }}")

    def test_no_pr_controlled_refs_anywhere(self):
        # base.ref and head.ref are writer-controllable for same-repo PRs
        for banned in [
            "pull_request.base.ref",
            "pull_request.base.sha",
            "pull_request.head.ref",
            "github.head_ref",
            "github.base_ref",
        ]:
            self.assertNotIn(banned, self.raw, f"{banned} must not be used")
        # head.sha appears only as HEAD_SHA env (check-target data, never executed)
        for m in re.finditer(r"pull_request\.head\.sha", self.raw):
            line = self.raw[: m.start()].count("\n")
            self.assertIn("HEAD_SHA", self.raw.splitlines()[line])

    def test_permissions_include_disarm_repository_write(self):
        perms = self.doc["permissions"]
        self.assertEqual(
            perms,
            {
                "contents": "write",
                "pull-requests": "write",
                "issues": "write",
                "checks": "write",
            },
        )

    def test_concurrency_serialized_no_cancel(self):
        conc = self.doc["concurrency"]
        self.assertIn("group", conc)
        self.assertEqual(conc["cancel-in-progress"], False)
        # default queue mode is single-pending: a newer queued run replaces
        # an older PENDING one, so the last-completing run reflects the
        # newest state (docs: control-workflow-concurrency)
        self.assertNotIn("queue", conc)

    def test_run_step_executes_only_checked_out_script(self):
        steps = self.doc["jobs"]["guard"]["steps"]
        runs = [s["run"] for s in steps if "run" in s]
        self.assertEqual(runs, ["bash scripts/hold-guard/guard_run.sh"])

    def test_auto_merge_enabled_trigger_present(self):
        # Kody r4373585289: without this type an auto-merge armed after the
        # guard's latest run is never disarmed until an unrelated event
        on = self.doc.get("on", self.doc.get(True))
        types = on["pull_request_target"]["types"]
        self.assertIn("auto_merge_enabled", types)

    def test_ci_runs_hold_guard_suites(self):
        # Kody r4373587808: these suites must be wired into ci.yml or they
        # are decoration - regressions would ship under a green CI
        ci = os.path.join(os.path.dirname(WF), "ci.yml")
        doc = yaml.safe_load(open(ci))
        runs = [
            step["run"]
            for job in doc["jobs"].values()
            for step in job.get("steps", [])
            if "run" in step
        ]
        self.assertTrue(
            any("hold-guard" in r and "unittest" in r for r in runs),
            "ci.yml must run the scripts/hold-guard suites",
        )

    def test_no_merge_group_trigger(self):
        # removed in slice 1: queue refs carry PR-head content, so the
        # workflow file there is not default-branch-only trusted code
        on = self.doc.get("on", self.doc.get(True))  # YAML 1.1 parses on: as True
        if isinstance(on, dict):
            self.assertNotIn("merge_group", on)
        else:
            self.fail(f"unexpected on: structure {on!r}")


if __name__ == "__main__":
    unittest.main()
