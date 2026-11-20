#!/usr/bin/env python3
"""Offline execution and source contracts. No live GitHub calls or credentials."""

import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows/auto-merge-reusable.yml"


def yaml_data(path):
    # PyYAML==6.0.3 is installed by the existing AFC CI hold-guard step.
    import yaml

    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


D = yaml_data(WF)
GUARD = "Enforce protected merge execution provenance before any checkout or mint"


def step(job, name):
    return next(s for s in D["jobs"][job]["steps"] if s.get("name") == name)


def run(script, env):
    with tempfile.TemporaryDirectory() as td:
        e = dict(os.environ, **env, GITHUB_OUTPUT=str(pathlib.Path(td) / "out"))
        return subprocess.run(["bash", "-eu", "-c", script], env=e, text=True, capture_output=True)


class Provenance(unittest.TestCase):
    def env(self):
        return dict(
            USE_WISE="false",
            EXPECTED_ACTOR="swu-merge-automation[bot]",
            EVENT_NAME="pull_request_target",
            EXECUTION_REF="refs/heads/main",
            DEFAULT_BRANCH="main",
            CALLER_WORKFLOW_REF="Simple-With-Us/Fleet-OPS/.github/workflows/auto-merge-prs.yml@refs/heads/main",
            REUSABLE_REPOSITORY="Simple-With-Us/AI-Fleet-Coordinator",
            REPO="Simple-With-Us/Fleet-OPS",
            OWNER="Simple-With-Us",
            PR_HEAD_REPO="Simple-With-Us/Fleet-OPS",
            PR_BASE_REF="main",
            REARM_PR_NUMBER="",
            SELF_GUARD=".github/workflows/auto-merge-prs.yml",
        )

    def assert_guard(self, changes, okay):
        for j in ["classify", "arm"]:
            env = self.env()
            env.update(changes)
            p = run(step(j, GUARD)["run"], env)
            self.assertEqual(p.returncode == 0, okay, (j, changes, p.stdout, p.stderr))

    def test_valid_target(self):
        self.assert_guard({}, True)

    def test_valid_schedule(self):
        self.assert_guard(
            dict(
                EVENT_NAME="schedule",
                PR_HEAD_REPO="",
                PR_BASE_REF="",
                REARM_PR_NUMBER="7",
            ),
            True,
        )

    def test_valid_dispatch(self):
        self.assert_guard(
            dict(
                EVENT_NAME="workflow_dispatch",
                PR_HEAD_REPO="",
                PR_BASE_REF="",
                REARM_PR_NUMBER="19",
            ),
            True,
        )

    def test_rejections(self):
        for change in [
            dict(USE_WISE="true"),
            dict(EXPECTED_ACTOR="jaywedgeworth22"),
            dict(EXPECTED_ACTOR=""),
            dict(OWNER="elsewhere"),
            dict(REUSABLE_REPOSITORY="attacker/fork"),
            dict(EXECUTION_REF="refs/pull/7/merge"),
            dict(EXECUTION_REF="refs/heads/other"),
            dict(
                CALLER_WORKFLOW_REF="Simple-With-Us/Fleet-OPS/.github/workflows/evil.yml@refs/heads/main"
            ),
            dict(PR_HEAD_REPO="fork/Fleet-OPS"),
            dict(PR_BASE_REF="branch-owned-by-author"),
            dict(EVENT_NAME="pull_request"),
            dict(EVENT_NAME="pull_request_review"),
            dict(EVENT_NAME="push"),
            dict(EVENT_NAME="workflow_dispatch", REARM_PR_NUMBER=""),
            dict(EVENT_NAME="schedule", REARM_PR_NUMBER="1; echo bad"),
            dict(EVENT_NAME="schedule", REARM_PR_NUMBER="0"),
            dict(EVENT_NAME="schedule", REARM_PR_NUMBER="01"),
            dict(SELF_GUARD=""),
            dict(SELF_GUARD="not-the-workflow"),
        ]:
            with self.subTest(change=change):
                self.assert_guard(change, False)


class Credentials(unittest.TestCase):
    def test_protected_missing_fails_no_fallback(self):
        for job in ["classify", "arm"]:
            s = next(x for x in D["jobs"][job]["steps"] if x.get("id") == "merge_app_creds")
            for app, key in [("", ""), ("id", ""), ("", "marker")]:
                p = run(
                    s["run"],
                    dict(PROTECTED_MODE="true", MERGE_APP_ID=app, MERGE_APP_KEY=key),
                )
                self.assertNotEqual(p.returncode, 0, p.stdout)

    def test_legacy_presence_semantics_preserved(self):
        for job in ["classify", "arm"]:
            s = next(x for x in D["jobs"][job]["steps"] if x.get("id") == "merge_app_creds")
            for app, key in [("", ""), ("id", ""), ("", "marker"), ("id", "marker")]:
                p = run(
                    s["run"],
                    dict(PROTECTED_MODE="false", MERGE_APP_ID=app, MERGE_APP_KEY=key),
                )
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_protected_present(self):
        for j in ["classify", "arm"]:
            s = next(x for x in D["jobs"][j]["steps"] if x.get("id") == "merge_app_creds")
            p = run(
                s["run"],
                dict(
                    PROTECTED_MODE="true",
                    MERGE_APP_ID="id",
                    MERGE_APP_KEY="harmless-marker",
                ),
            )
            self.assertEqual(p.returncode, 0, p.stderr)


class Structure(unittest.TestCase):
    def test_guard_before_any_checkout_or_mint(self):
        for j in ["classify", "arm"]:
            steps = D["jobs"][j]["steps"]
            self.assertEqual(steps[0]["name"], GUARD)
            for i, s in enumerate(steps):
                if "uses" in s:
                    self.assertGreater(i, 0)

    def test_environment_fixed_on_consumers(self):
        self.assertEqual(
            D["jobs"]["classify"]["environment"],
            "${{ inputs.use_protected_merge_app && 'fleet-merge-automation' || '' }}",
        )
        self.assertEqual(
            D["jobs"]["arm"]["environment"],
            "${{ inputs.use_protected_merge_app && 'fleet-merge-automation' || (inputs.use_wise_relay && 'merge-shepherd' || '') }}",
        )
        self.assertEqual(
            D["on"]["workflow_call"]["inputs"]["use_protected_merge_app"]["default"],
            "false",
        )

    def test_no_legacy_fallback_protected(self):
        consumers = []
        for j in ["classify", "arm"]:
            for s in D["jobs"][j]["steps"]:
                for k, v in s.get("env", {}).items():
                    if "secrets.merge_token" in v:
                        consumers.append(v)
                        self.assertIn(
                            "(inputs.use_protected_merge_app == false && secrets.merge_token)",
                            v,
                        )
        self.assertEqual(len(consumers), 6)

    def test_no_pr_expression_in_program_text(self):
        for j in D["jobs"].values():
            for s in j.get("steps", []):
                self.assertNotIn("${{", s.get("run", ""))

    def test_only_pinned_or_default_source_checkout(self):
        for j in D["jobs"].values():
            for s in j.get("steps", []):
                if "checkout@" in s.get("uses", ""):
                    self.assertNotIn("pull_request.head", s["with"]["ref"])
                    self.assertNotIn("pull_request_target.head", str(s["with"]))

    def test_disarm_never_uses_service_credential(self):
        s = str(D["jobs"]["disarm"])
        self.assertNotIn("merge_app", s)
        self.assertNotIn("secrets.merge_token", s)


class DraftDisposition(unittest.TestCase):
    def test_draft_classify_outputs_skip(self):
        p = ROOT / "scripts/test_auto_merge_reusable.py"
        spec = importlib.util.spec_from_file_location("existing", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        code, out, reason, calls = m.run_classify_full(
            "clear", "", extra_env={"EVENT_DRAFT": "true"}
        )
        self.assertEqual(code, 0, out)
        self.assertIn("PR is a draft", reason)
        self.assertEqual(calls, [])


class DefaultBaseFence(unittest.TestCase):
    def load(self):
        p = ROOT / "scripts/test_auto_merge_reusable.py"
        spec = importlib.util.spec_from_file_location("existingbase", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_rearm_nondefault_base_rejected(self):
        m = self.load()
        code, out, reason, calls = m.run_classify_full(
            "rearm_open",
            "",
            rearm_pr_number="7",
            extra_env={"PROTECTED_MODE": "true", "DEFAULT_BRANCH": "main"},
        )
        self.assertEqual(code, 0, out)
        self.assertIn("base is not default branch", reason)

    def test_arm_nondefault_base_rejected_and_disarmed(self):
        m = self.load()
        code, out, calls = m.run_arm_guard(
            "clean", extra_env={"PROTECTED_MODE": "true", "DEFAULT_BRANCH": "main"}
        )
        self.assertNotEqual(code, 0, out)
        self.assertIn("base is not default branch", out)
        self.assertTrue(any("--disable-auto" in c for c in calls), calls)


class FailurePathDisarm(unittest.TestCase):
    def test_early_failure_without_classify_url_disarms_exact_context_pr(self):
        script = D["jobs"]["disarm"]["steps"][0]["run"]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            gh = root / "gh"
            gh.write_text(
                '#!/bin/bash\nif [[ "$*" == *graphql* ]]; then echo "github-actions[bot]"; elif [[ "$*" == *--disable-auto* ]]; then touch "$MARKER"; printf "%s" "$*" > "$CALLS"; elif [ -f "$MARKER" ]; then echo off; else echo armed; fi\n'
            )
            gh.chmod(0o755)
            env = dict(
                os.environ,
                PATH=td + ":" + os.environ["PATH"],
                MARKER=str(root / "off"),
                CALLS=str(root / "calls"),
                PR_URL="",
                REPO="Simple-With-Us/Example",
                PR_NUMBER="7",
                REASON="classify failed",
                GH_TOKEN="fake",
            )
            p = subprocess.run(
                ["bash", "-eu", "-c", script], env=env, text=True, capture_output=True
            )
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue((root / "off").exists())
            self.assertIn(
                "https://github.com/Simple-With-Us/Example/pull/7",
                (root / "calls").read_text(),
            )

    def test_invalid_coordinates_reject_before_gh(self):
        script = D["jobs"]["disarm"]["steps"][0]["run"]
        for n in ["", "x", "7;echo bad"]:
            p = run(
                script,
                dict(
                    PR_URL="",
                    REPO="Simple-With-Us/Example",
                    PR_NUMBER=n,
                    REASON="earlyfailure",
                    GH_TOKEN="fake",
                ),
            )
            self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
