#!/usr/bin/env python3
"""Integration tests: execute the SHIPPED shell wrappers (guard_run.sh,
armer_hold_check.sh) against a fake gh serving canned API responses.

Covers what the canned-state evaluator tests cannot: call ordering
(evaluate before conditional disarm before repair before check), jq-built request bodies, label names
with spaces (no word-splitting), merge_group PR mapping, multi-page event
pagination, error fail-closed behavior, and the check-run payload shape.
"""

import errno
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

DIR = os.path.dirname(os.path.abspath(__file__))
_seq = [0]


def ev(kind, label, actor_id, login, ts):
    _seq[0] += 1
    return {
        "id": _seq[0],
        "event": kind,
        "label": {"name": label},
        "actor": {"id": actor_id, "login": login},
        "created_at": ts,
    }


ECHO = (41898282, "swu-echo[bot]")
WISE = (41900001, "swu-wise[bot]")


class GuardIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "gh.log")
        open(self.log, "w").close()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        shim = os.path.join(bindir, "gh")
        with open(shim, "w") as fh:
            fh.write(
                '#!/usr/bin/env bash\nexec python3 "%s" "$@"\n' % os.path.join(DIR, "fake_gh.py")
            )
        os.chmod(shim, os.stat(shim).st_mode | stat.S_IEXEC)
        self.env = dict(os.environ)
        self.env["PATH"] = bindir + os.pathsep + self.env["PATH"]
        self.env["FAKE_GH_LOG"] = self.log
        self.env.update(REPO="Fixture-Org/Fixture-Repo", HEAD_SHA="deadbeef" * 5)
        self.scenario_path = os.path.join(self.tmp, "scenario.json")

    def scenario(self, data):
        json.dump(data, open(self.scenario_path, "w"))
        self.env["FAKE_GH_SCENARIO"] = self.scenario_path

    def calls(self):
        return [json.loads(l) for l in open(self.log)]

    def run_guard(self, **env):
        self.env.update(env)
        return subprocess.run(
            ["bash", os.path.join(DIR, "guard_run.sh")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def run_armer(self, **env):
        self.env.update(env)
        return subprocess.run(
            ["bash", os.path.join(DIR, "armer_hold_check.sh")],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_held_repairs_comments_and_publishes_failure_in_order(self):
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *WISE,
                                "2026-10-25T02:00:00Z",
                            ),
                        ]
                    ]
                },
            }
        )
        p = self.run_guard(
            PR="3",
            PR_URL="https://github.com/x/pull/3",
            EVENT_NAME="pull_request_target",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        kinds = [(" ".join(c["argv"][:3])) for c in self.calls()]
        # order: evaluate -> disarm -> label re-apply -> comment -> check
        self.assertEqual(kinds[0], "api repos/Fixture-Org/Fixture-Repo/issues/3/labels")
        label_posts = [
            c
            for c in self.calls()
            if c["argv"][0] == "api"
            and any(a.endswith("/labels") for a in c["argv"])
            and "-X" in c["argv"]
        ]
        self.assertEqual(len(label_posts), 1)
        self.assertEqual(json.loads(label_posts[0]["stdin"]), {"labels": ["do-not-automerge"]})
        comments = [c for c in self.calls() if c["argv"][:2] == ["pr", "comment"]]
        self.assertEqual(len(comments), 1)
        body = comments[0]["argv"][comments[0]["argv"].index("--body") + 1]
        self.assertIn("swu-wise[bot]", body)
        self.assertIn("swu-echo[bot]", body)
        checks = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        self.assertEqual(len(checks), 1)
        payload = json.loads(checks[0]["stdin"])
        self.assertEqual(payload["conclusion"], "failure")
        self.assertEqual(payload["head_sha"], "deadbeef" * 5)
        self.assertIn("swu-echo[bot]", payload["output"]["summary"])
        # disarm strictly before repair and check
        self.assertLess(
            kinds.index("pr merge --disable-auto"),
            max(
                i
                for i, c in enumerate(self.calls())
                if any(a.endswith("/check-runs") for a in c["argv"])
            ),
        )

    def test_clear_publishes_success_no_repairs(self):
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T02:00:00Z",
                            ),
                        ]
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr)
        checks = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "success")
        self.assertFalse([c for c in self.calls() if c["argv"][:2] == ["pr", "comment"]])

    def test_clear_armed_pr_is_never_disarmed(self):
        self.scenario(
            {
                "labels": {"3": []},
                "events": {"3": [[]]},
                "armed": True,
                "fail": {"pr_merge": {"rc": 1}, "pr_view": {"rc": 1}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse([c for c in self.calls() if c["argv"][:1] == ["pr"]])
        posts = [
            c
            for c in self.calls()
            if "POST" in c["argv"] and any(a.endswith("/check-runs") for a in c["argv"])
        ]
        self.assertEqual(json.loads(posts[0]["stdin"])["conclusion"], "success")

    def test_visible_hold_disarms_before_paginated_timeline(self):
        self.scenario({"labels": {"3": [{"name": "do-not-merge"}]}, "events": {"3": [[]]}})
        p = self.run_guard(PR="3", PR_URL="u")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        calls = self.calls()
        disarm = next(i for i, c in enumerate(calls) if c["argv"][:2] == ["pr", "merge"])
        events = next(
            i for i, c in enumerate(calls) if any(a.endswith("/events") for a in c["argv"])
        )
        self.assertLess(disarm, events)

    def test_clear_publish_failure_does_not_disarm(self):
        self.scenario(
            {
                "labels": {"3": []},
                "events": {"3": [[]]},
                "armed": True,
                "fail": {"check-runs": {"rc": 1}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertFalse([c for c in self.calls() if c["argv"][:2] == ["pr", "merge"]])

    def test_current_label_signal_error_disarms_and_fails(self):
        self.scenario({"fail": {"labels/3": {"rc": 1}}})
        p = self.run_guard(PR="3", PR_URL="u")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(len([c for c in self.calls() if c["argv"][:2] == ["pr", "merge"]]), 1)

    def test_hold_arriving_at_fence_disarms_before_red_publish(self):
        hold = ev("labeled", "do-not-merge", *ECHO, "2026-10-25T01:00:00Z")
        self.scenario({"labels": {"3": []}, "events_seq": {"3": [[[]], [[hold]]]}, "armed": False})
        p = self.run_guard(PR="3", PR_URL="u")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        calls = self.calls()
        disarm = next(i for i, c in enumerate(calls) if c["argv"][:2] == ["pr", "merge"])
        publish = next(
            i
            for i, c in enumerate(calls)
            if "POST" in c["argv"] and any(a.endswith("/check-runs") for a in c["argv"])
        )
        self.assertLess(disarm, publish)
        self.assertEqual(json.loads(calls[publish]["stdin"])["conclusion"], "failure")

    def test_events_api_error_fails_closed_and_loud(self):
        self.scenario({"labels": {"3": []}, "fail": {"events/3": {"rc": 1, "stderr": "boom 500"}}})
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(len([c for c in self.calls() if c["argv"][:2] == ["pr", "merge"]]), 1)
        checks = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        payload = json.loads(checks[0]["stdin"])
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("evaluation error", payload["output"]["title"])

    def test_missing_pr_fails_closed(self):
        self.scenario({"labels": {}, "events": {}})
        p = self.run_guard(PR="", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)

    def test_disarm_uses_real_gh_contract(self):
        self._held_scenario()
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr)
        disarm = next(c for c in self.calls() if c["argv"][:2] == ["pr", "merge"])
        self.assertEqual(disarm["argv"], ["pr", "merge", "--disable-auto", "u"])

    def test_do_not_merge_family_enforced(self):
        self.scenario(
            {
                "labels": {"3": [{"name": "do-not-merge"}]},
                "events": {"3": [[ev("labeled", "do-not-merge", *ECHO, "2026-10-25T01:00:00Z")]]},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "failure")

    def test_fresh_fence_reevaluates_before_publish(self):
        # first evaluation: held; owner releases mid-run; the conclusion
        # published must come from the SECOND (write-time) evaluation
        sha = "deadbeef" * 5
        self.scenario(
            {
                "labels": {"3": []},
                "events_seq": {
                    "3": [
                        [
                            [
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T01:00:00Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *WISE,
                                    "2026-10-25T02:00:00Z",
                                ),
                            ]
                        ],
                        [
                            [
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T01:00:00Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *WISE,
                                    "2026-10-25T02:00:00Z",
                                ),
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T02:00:05Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T03:00:00Z",
                                ),
                            ]
                        ],
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr)
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "success")

    def test_foreign_run_never_probed_or_patched(self):
        sha = "deadbeef" * 5
        self._clear_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "check_runs": {sha: [{"id": 777, "app": {"slug": "swu-wise"}}]},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr)
        patches = [c for c in self.calls() if "PATCH" in c["argv"]]
        self.assertEqual(len(patches), 0)  # zero mutation attempts on foreign runs
        posts = [
            c
            for c in self.calls()
            if "POST" in c["argv"] and any(a.endswith("/check-runs") for a in c["argv"])
        ]
        self.assertEqual(len(posts), 1)  # our identity posts its own current run

    def test_label_with_spaces_reapplied_intact(self):
        spaced = "do-not-automerge:hold please"
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev("labeled", spaced, *ECHO, "2026-10-25T01:00:00Z"),
                            ev("unlabeled", spaced, *WISE, "2026-10-25T02:00:00Z"),
                        ]
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr)
        posts = [
            c
            for c in self.calls()
            if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
        ]
        self.assertEqual(json.loads(posts[0]["stdin"]), {"labels": [spaced]})

    def test_pagination_across_pages_unsorted(self):
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T02:00:00Z",
                            )
                        ],
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            )
                        ],
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr)
        checks = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "success")

    def test_armer_wrapper_held_blocks_with_reason(self):
        self.scenario(
            {
                "labels": {"3": [{"name": "do-not-automerge"}]},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            )
                        ]
                    ]
                },
            }
        )
        p = self.run_armer(PR_NUMBER="3")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("do-not-automerge", p.stdout)

    def test_armer_wrapper_clear_passes(self):
        self.scenario({"labels": {"3": []}, "events": {"3": [[]]}})
        p = self.run_armer(PR_NUMBER="3")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_armer_wrapper_error_fails_closed(self):
        self.scenario({"labels": {"3": []}, "fail": {"events/3": {"rc": 1, "stderr": "boom"}}})
        p = self.run_armer(PR_NUMBER="3")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("error", p.stdout.lower())

    def _clear_scenario(self):
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T02:00:00Z",
                            ),
                        ]
                    ]
                },
            }
        )

    def test_existing_runs_patched_no_duplicate_no_stale(self):
        # two of OUR prior merge-gate runs on the SHA: both must be PATCHed
        # to the current conclusion; no new POST -> no same-name ambiguity
        sha = "deadbeef" * 5
        self._clear_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "check_runs": {
                    sha: [
                        {"id": 555, "app": {"slug": "github-actions"}},
                        {"id": 556, "app": {"slug": "github-actions"}},
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr)
        patches = [c for c in self.calls() if "PATCH" in c["argv"]]
        posts = [
            c
            for c in self.calls()
            if "POST" in c["argv"] and any(a.endswith("/check-runs") for a in c["argv"])
        ]
        self.assertEqual(len(patches), 2)
        self.assertEqual(len(posts), 0)
        for c in patches:
            self.assertEqual(json.loads(c["stdin"])["conclusion"], "success")

    def _held_scenario(self):
        # active hold needing repair (illegitimate removal by WISE)
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *WISE,
                                "2026-10-25T02:00:00Z",
                            ),
                        ]
                    ]
                },
            }
        )

    def test_disarm_unconfirmed_state_red_job(self):
        # armed and the disable mutation is refused; state read still says
        # armed -> critical failure (fleet contract: fail closed on state)
        self._held_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "armed": True,
                "fail": {
                    "pr_merge": {
                        "rc": 1,
                        "stderr": "Resource not accessible by integration",
                    }
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("auto-merge state after disarm", p.stdout)

    def test_disarm_unconfirmed_publishes_a_red_gate_not_a_green_one(self):
        # armed, the disable mutation is refused, and the state read still says
        # armed: the hold state may be clean, but the guard does not know that
        # auto-merge is off, so it must not publish a green gate over a pull
        # request that can still merge itself (AFC-553).
        self._held_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "armed": True,
                "fail": {
                    "pr_merge": {
                        "rc": 1,
                        "stderr": "Resource not accessible by integration",
                    }
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        published = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        self.assertEqual(len(published), 1, published)
        body = json.loads(published[0]["stdin"])
        self.assertEqual(body["name"], "merge-gate")
        self.assertEqual(body["conclusion"], "failure")
        self.assertIn("disarm", body["output"]["title"])
        self.assertIn("disarm", body["output"]["summary"])

    def test_disarm_state_unreadable_red_job(self):
        self._held_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "fail": {"pr_view": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("unreadable", p.stdout)

    def test_disarm_not_armed_is_benign(self):
        # disable attempt errors with "not enabled" but the state read says
        # off -> success, no red job (the fleet's own already-off case)
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)), "armed": False})
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def test_disarm_unconfirmed_publishes_failure_not_green(self):
        # AFC-553: the disarm mutation is refused for the workflow's own
        # token, so auto-merge may still be armed.  The guard cannot vouch
        # that a merge will not run past the hold check, so the merge-gate
        # conclusion must be failure and must say so - a degraded guard
        # never publishes green.
        self._held_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "armed": True,
                "fail": {
                    "pr_merge": {
                        "rc": 1,
                        "stderr": "Resource not accessible by integration",
                    }
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        checks = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        self.assertEqual(len(checks), 1)
        payload = json.loads(checks[0]["stdin"])
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("disarm", payload["output"]["title"])
        self.assertIn("armed", payload["output"]["summary"])

    def test_disarm_not_armed_control_publishes_success(self):
        # Control for the test above: with the SAME scenario and auto-merge
        # off, the guard publishes green.  Without this the failure test
        # could pass for the wrong reason (a broken fixture rather than the
        # disarm).
        self._clear_scenario()
        self.scenario({**json.load(open(self.scenario_path)), "armed": False})
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        checks = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        self.assertEqual(len(checks), 1)
        payload = json.loads(checks[0]["stdin"])
        self.assertEqual(payload["conclusion"], "success")
        self.assertIn("no active merge holds", payload["output"]["title"])

    def test_disarm_unconfirmed_keeps_the_hold_list_in_the_summary(self):
        # An active hold AND an unconfirmed disarm: the conclusion is
        # failure, the title names the disarm, and the summary still names
        # the hold, so neither reason is lost.
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *WISE,
                                "2026-10-25T02:00:00Z",
                            ),
                        ]
                    ]
                },
                "armed": True,
                "fail": {
                    "pr_merge": {
                        "rc": 1,
                        "stderr": "Resource not accessible by integration",
                    }
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        checks = [c for c in self.calls() if any(a.endswith("/check-runs") for a in c["argv"])]
        self.assertEqual(len(checks), 1)
        payload = json.loads(checks[0]["stdin"])
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("disarm", payload["output"]["title"])
        self.assertIn("do-not-automerge", payload["output"]["summary"])

    def test_owned_patch_failure_posts_fresh_and_red_job(self):
        sha = "deadbeef" * 5
        self._clear_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "check_runs": {sha: [{"id": 555, "app": {"slug": "github-actions"}}]},
                "fail": {"patch/555": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        posts = [
            c
            for c in self.calls()
            if "POST" in c["argv"] and any(a.endswith("/check-runs") for a in c["argv"])
        ]
        self.assertEqual(len(posts), 1)  # fresh owned run carries the state
        self.assertEqual(json.loads(posts[0]["stdin"])["conclusion"], "success")

    def test_check_post_failure_red_job(self):
        self._clear_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "fail": {"check-runs": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("failed to create merge-gate check run", p.stdout)

    def test_checkruns_get_failure_still_posts_and_red_job(self):
        sha = "deadbeef" * 5
        self._clear_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "fail": {f"sha-checks/{sha}": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("could not list merge-gate runs", p.stdout)
        posts = [
            c
            for c in self.calls()
            if "POST" in c["argv"] and any(a.endswith("/check-runs") for a in c["argv"])
        ]
        self.assertEqual(len(posts), 1)  # best-effort conclusion still published
        payload = json.loads(posts[0]["stdin"])
        # clear initial state, but the critical read error must recompute
        # the payload: a degraded guard never publishes green
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("check-run listing error", payload["output"]["title"])

    def test_repair_label_post_failure_red_job(self):
        self._held_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "fail": {"labels-post/3": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("failed to re-apply label", p.stdout)

    def test_comment_failure_red_job(self):
        self._held_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "fail": {"comment/3": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("failed to comment", p.stdout)

    def test_clear_initial_then_fence_error_red_and_failure_payload(self):
        # first evaluation CLEAR, fresh-state fence read fails: job red AND
        # the published check is a failure payload, never green
        self.scenario(
            {
                "labels": {"3": []},
                "events_seq": {
                    "3": [
                        [
                            [
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T01:00:00Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T02:00:00Z",
                                ),
                            ]
                        ],
                        {"__fail__": {"rc": 1, "stderr": "boom 500"}},
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertEqual(len([c for c in self.calls() if c["argv"][:2] == ["pr", "merge"]]), 1)
        posts = [
            c
            for c in self.calls()
            if "POST" in c["argv"] and any(a.endswith("/check-runs") for a in c["argv"])
        ]
        self.assertEqual(len(posts), 1)
        payload = json.loads(posts[0]["stdin"])
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("evaluation error", payload["output"]["title"])

    def test_repaired_hold_comments_once_not_every_run(self):
        # Sentry finding 4373451447 on #964: after the guard re-applies a
        # hold, the illegitimate removal stays in timeline history; the
        # comment step must not refire while no new repair happens.
        # Repaired state: label present, history = set, illegit removal,
        # re-applied -> no label POST, no comment, check still failure.
        GUARD = (41999999, "github-actions[bot]")
        self.scenario(
            {
                "labels": {"3": [{"name": "do-not-automerge"}]},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *WISE,
                                "2026-10-25T02:00:00Z",
                            ),
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *GUARD,
                                "2026-10-25T02:01:00Z",
                            ),
                        ]
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertFalse(
            [
                c
                for c in self.calls()
                if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
            ]
        )
        self.assertFalse([c for c in self.calls() if c["argv"][:2] == ["pr", "comment"]])
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "failure")

    def test_comment_fires_on_the_repair_run(self):
        # the SAME illegitimate history, label still absent: the repair run
        # re-applies AND comments exactly once, after the successful POST
        self._held_scenario()
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(len([c for c in self.calls() if c["argv"][:2] == ["pr", "comment"]]), 1)
        label_post = next(
            i
            for i, c in enumerate(self.calls())
            if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
        )
        comment = next(i for i, c in enumerate(self.calls()) if c["argv"][:2] == ["pr", "comment"])
        self.assertLess(label_post, comment)  # narrates the actual repair

    def test_no_comment_when_repair_post_fails(self):
        # failed repair: red job, NO comment - never claim "the hold is
        # back on" when it is not
        self._held_scenario()
        self.scenario(
            {
                **json.load(open(self.scenario_path)),
                "fail": {"labels-post/3": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("failed to re-apply label", p.stdout)
        self.assertFalse([c for c in self.calls() if c["argv"][:2] == ["pr", "comment"]])
        # the published check still says the hold is active (fail closed)
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "failure")

    def test_stale_repair_skipped_when_recheck_shows_release(self):
        # Kody r4373591313: candidate list is from the first evaluation; a
        # legitimate release landing before the repair POST must NOT be
        # resurrected as a guard-owned hold.  Sequence: eval1 = illegitimate
        # removal (repair candidate); re-check = owner re-applied then
        # released (clear); fence = still clear.
        self.scenario(
            {
                "labels": {"3": []},
                "events_seq": {
                    "3": [
                        [
                            [
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T01:00:00Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *WISE,
                                    "2026-10-25T02:00:00Z",
                                ),
                            ]
                        ],
                        [
                            [
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T01:00:00Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *WISE,
                                    "2026-10-25T02:00:00Z",
                                ),
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T02:30:00Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T03:00:00Z",
                                ),
                            ]
                        ],
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("skipping stale repair", p.stdout)
        self.assertFalse(
            [
                c
                for c in self.calls()
                if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
            ]
        )
        self.assertFalse([c for c in self.calls() if c["argv"][:2] == ["pr", "comment"]])
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "success")

    def test_repair_recheck_error_blocks_repair_and_red_job(self):
        # the pre-repair re-check is a critical read: its failure skips the
        # repair (fail closed) and turns the job red
        self.scenario(
            {
                "labels": {"3": []},
                "events_seq": {
                    "3": [
                        [
                            [
                                ev(
                                    "labeled",
                                    "do-not-automerge",
                                    *ECHO,
                                    "2026-10-25T01:00:00Z",
                                ),
                                ev(
                                    "unlabeled",
                                    "do-not-automerge",
                                    *WISE,
                                    "2026-10-25T02:00:00Z",
                                ),
                            ]
                        ],
                        {"__fail__": {"rc": 1, "stderr": "boom 500"}},
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)
        self.assertIn("pre-repair re-check failed", p.stdout)
        self.assertFalse(
            [
                c
                for c in self.calls()
                if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
            ]
        )

    def test_unknown_owner_hold_not_rearmed_but_gate_red(self):
        # Kody r4373587152: an unknown-owner hold must NOT be re-applied
        # (re-applying makes it releaseable by nobody); it escalates through
        # the red gate summary instead
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *WISE,
                                "2026-10-25T02:00:00Z",
                            )
                        ]
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertFalse(
            [
                c
                for c in self.calls()
                if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
            ]
        )
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        payload = json.loads(checks[0]["stdin"])
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("owner unknown, needs escalation", payload["output"]["summary"])

    def test_null_remover_login_coalesced_in_comment(self):
        # Kody r4373591641: a timeline event with no actor login must not
        # crash the jq join into an empty comment
        no_login = {
            "id": 900,
            "event": "unlabeled",
            "label": {"name": "do-not-automerge"},
            "actor": {"id": 424242},
            "created_at": "2026-10-25T02:00:00Z",
        }
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            no_login,
                        ]
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        comments = [c for c in self.calls() if c["argv"][:2] == ["pr", "comment"]]
        self.assertEqual(len(comments), 1)
        body = comments[0]["argv"][comments[0]["argv"].index("--body") + 1]
        self.assertIn("unknown", body)

    def test_checkrun_listing_uses_pagination(self):
        # Kody r4373586178/r4373591470: the owned-run listing must page
        self._clear_scenario()
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        gets = [
            c
            for c in self.calls()
            if c["argv"][0] == "api" and any("check-runs?check_name=" in a for a in c["argv"])
        ]
        self.assertEqual(len(gets), 1)
        self.assertIn("--paginate", gets[0]["argv"])
        self.assertIn("--slurp", gets[0]["argv"])

    def test_fake_gh_refuses_secret_shaped_invocation(self):
        # Kody r4373585476: the fake itself refuses to persist token-shaped
        # argv, exiting 2 with only the field name
        self.scenario({"labels": {}, "events": {}})
        shim = os.path.join(os.path.dirname(self.log), "bin", "gh")
        p = subprocess.run(
            [shim, "api", "repos/x", "-H", "Authorization: token ghp_" + "a" * 20],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(p.returncode, 2)
        self.assertIn("code=secret field=argv", p.stderr)
        self.assertNotIn("ghp_", p.stderr)
        self.assertEqual(open(self.log).read(), "")

    def test_fake_gh_rejects_unknown_scenario_section(self):
        # Kody r4373585668: malformed scenario -> precise failure, and the
        # guard fails closed on the resulting evaluation error
        self.scenario({"labels": {"3": []}, "events": {"3": [[]]}, "bogus": 1})
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 3)

    def test_repair_recheck_parse_failure_red_job(self):
        # Kody r4373834619: a jq failure reading the re-check result must
        # record a repair error and turn the job red - never fall into the
        # stale-skip branch and silently leave the hold unrepaired
        self._held_scenario()
        jq_shim = os.path.join(self.tmp, "bin", "jq")
        real_jq = subprocess.run(
            ["which", "jq"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        with open(jq_shim, "w") as fh:
            fh.write(
                "#!/usr/bin/env bash\n"
                'for a in "$@"; do\n'
                '  if [ "$a" = "l" ] && [ "${FAKE_JQ_FAIL_REPAIR_CHECK:-}" = "1" ]; then\n'
                '    echo "jq: simulated re-check parse failure" >&2; exit 2\n'
                "  fi\n"
                "done\n"
                'exec "%s" "$@"\n' % real_jq
            )
        os.chmod(jq_shim, os.stat(jq_shim).st_mode | stat.S_IEXEC)
        p = self.run_guard(
            PR="3", PR_URL="u", EVENT_NAME="pull_request_target", FAKE_JQ_FAIL_REPAIR_CHECK="1"
        )
        self.assertEqual(p.returncode, 3)
        self.assertIn("pre-repair re-check parse failed", p.stdout)
        self.assertFalse(
            [
                c
                for c in self.calls()
                if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
            ]
        )
        # and the published check still says the hold is active (fail closed)
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "failure")

    def test_repair_fence_evaluates_once_per_pr(self):
        # Kody r4373834824: the stale-repair fence re-evaluates each PR once,
        # not once per hold - two needing-repair holds on one PR cost one
        # re-check evaluation, and both repairs still land
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev("labeled", "do-not-automerge", *ECHO, "2026-10-25T01:00:00Z"),
                            ev("unlabeled", "do-not-automerge", *WISE, "2026-10-25T02:00:00Z"),
                            ev("labeled", "do-not-merge", *ECHO, "2026-10-25T01:10:00Z"),
                            ev("unlabeled", "do-not-merge", *WISE, "2026-10-25T02:10:00Z"),
                        ]
                    ]
                },
            }
        )
        p = self.run_guard(PR="3", PR_URL="u", EVENT_NAME="pull_request_target")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        events_gets = [
            c
            for c in self.calls()
            if c["argv"][0] == "api" and any(a.endswith("/events") for a in c["argv"])
        ]
        # exactly 3 evaluations total: step-2 eval, ONE hoisted repair
        # re-check, step-4 fresh-state fence
        self.assertEqual(len(events_gets), 3)
        label_posts = [
            c
            for c in self.calls()
            if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
        ]
        self.assertEqual(len(label_posts), 2)
        repaired = sorted(json.loads(c["stdin"])["labels"][0] for c in label_posts)
        self.assertEqual(repaired, ["do-not-automerge", "do-not-merge"])

    def test_repair_recheck_cache_write_failure_red_job(self):
        # Kody r4374043272: a failed cache write must not leave the PR
        # marked fresh-ok for a stale file to be trusted - error + red job
        self._held_scenario()
        # a directory at the cache path inside a caller-provided scratch
        # dir makes the write fail (r4374207226 moved scratch per-run)
        scratch = tempfile.mkdtemp(prefix="hold-guard-test-")
        os.makedirs(os.path.join(scratch, "fresh-repair-3.json"))
        try:
            p = self.run_guard(
                PR="3",
                PR_URL="u",
                EVENT_NAME="pull_request_target",
                HOLD_GUARD_SCRATCH=scratch,
            )
        finally:
            shutil.rmtree(scratch)
        self.assertEqual(p.returncode, 3)
        self.assertIn("pre-repair re-check cache write failed", p.stdout)
        self.assertFalse(
            [
                c
                for c in self.calls()
                if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
            ]
        )
        checks = [
            c
            for c in self.calls()
            if any(a.endswith("/check-runs") for a in c["argv"]) and "POST" in c["argv"]
        ]
        self.assertEqual(json.loads(checks[0]["stdin"])["conclusion"], "failure")

    def test_concurrent_run_snapshot_survives_this_run(self):
        # Kody r4374207226: a concurrent guard run on the same reused
        # runner must not lose its snapshot to this run's startup cleanup.
        # Sentinels at the OLD shared paths simulate that concurrent run's
        # in-use files; this run must neither delete nor trust them.
        # Assertion boundary is test-owned state only (Kody r4374404625 -
        # exact globs over shared /tmp are flaky and pollute): planted
        # sentinel survival + the contents of a caller-provided scratch
        # dir prove confinement; no global glob asserts.
        self._held_scenario()
        # Sentinels match the OLD glob pattern (a reintroduced startup glob
        # would delete them) but carry this process's pid, so parallel test
        # processes never share a path: no collision, no overwrite of
        # foreign files, by construction (Kody r4374404625).  O_EXCL makes
        # any residual collision a loud failure instead of a clobber.
        pid = os.getpid()
        sentinels = [
            f"/tmp/hold-guard-fresh-repair-{pid}-3.json",
            f"/tmp/hold-guard-fresh-repair-{pid}-99.json",
        ]
        for path in sentinels:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write("concurrent-run-data")
        scratch = tempfile.mkdtemp(prefix="hold-guard-test-")
        try:
            p = self.run_guard(
                PR="3",
                PR_URL="u",
                EVENT_NAME="pull_request_target",
                HOLD_GUARD_SCRATCH=scratch,
            )
            for path in sentinels:
                with open(path) as f:
                    self.assertEqual(f.read(), "concurrent-run-data", path)
            # the run confined ALL of its scratch to its own dir: markers,
            # per-PR evaluation dump, repair cache, combined fence
            for name in (
                "errors",
                "patch-fail",
                "repaired",
                "hold-guard-3.json",
                "fresh-repair-3.json",
                "combined.json",
            ):
                self.assertTrue(
                    os.path.isfile(os.path.join(scratch, name)),
                    name + " missing from run scratch",
                )
        finally:
            for path in sentinels:
                os.unlink(path)
            shutil.rmtree(scratch)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        # the repair still happened (run trusted only its own snapshot)
        label_posts = [
            c
            for c in self.calls()
            if any(a.endswith("/labels") for a in c["argv"]) and "-X" in c["argv"]
        ]
        self.assertEqual(len(label_posts), 1)
        self.assertEqual(json.loads(label_posts[0]["stdin"])["labels"], ["do-not-automerge"])

    def test_marker_append_failure_after_init_red_job(self):
        # Root-cause follow-up to Kody r4374404410: init-time validation
        # cannot catch a scratch that breaks AFTER init (disk full).  The
        # errors marker is a symlink to /dev/full: the init truncation
        # succeeds, the post-init append fails with ENOSPC.  The guard must
        # still exit red - signalled without writing to the broken marker.
        self.scenario(
            {
                "labels": {"3": []},
                "events": {
                    "3": [
                        [
                            ev(
                                "labeled",
                                "do-not-automerge",
                                *ECHO,
                                "2026-10-25T01:00:00Z",
                            ),
                            ev(
                                "unlabeled",
                                "do-not-automerge",
                                *WISE,
                                "2026-10-25T02:00:00Z",
                            ),
                        ]
                    ]
                },
                "fail": {"labels-post/3": {"rc": 1, "stderr": "boom 500"}},
            }
        )
        # Platform guard (Kody r4374666905): /dev/full is Linux-only.  Where
        # it is absent the dangling symlink could make the guard's init
        # truncate create a host file or fail the run for the wrong reason,
        # invalidating the assertion - so probe the exact ENOSPC semantics
        # the test needs and skip loudly elsewhere.  (A read-only file is
        # NOT a substitute: chmod 444 fails the init truncation, exercising
        # the init check, not the post-init append path this test targets.)
        # The probe itself must never create, truncate or write to a host
        # file: stat first and require an existing character device, then
        # open O_WRONLY WITHOUT O_CREAT/O_TRUNC and expect ENOSPC on write.
        try:
            if not stat.S_ISCHR(os.stat("/dev/full").st_mode):
                self.skipTest("/dev/full is not a character device here")
            fd = os.open("/dev/full", os.O_WRONLY)
            try:
                os.write(fd, b"x")
            finally:
                os.close(fd)
            self.skipTest("/dev/full lacks ENOSPC semantics on this platform")
        except FileNotFoundError:
            self.skipTest("/dev/full not available on this platform")
        except OSError as e:
            if e.errno != errno.ENOSPC:
                self.skipTest("/dev/full not usable on this platform")
        scratch = tempfile.mkdtemp(prefix="hold-guard-test-")
        os.symlink("/dev/full", os.path.join(scratch, "errors"))
        try:
            p = self.run_guard(
                PR="3",
                PR_URL="u",
                EVENT_NAME="pull_request_target",
                HOLD_GUARD_SCRATCH=scratch,
            )
        finally:
            shutil.rmtree(scratch)
        # init truncation on /dev/full succeeded, so the run proceeded; the
        # failed repair POST forced an errors append, which failed - and
        # the job is red anyway, with the failure narrated on stdout
        self.assertEqual(p.returncode, 3, p.stderr + p.stdout)
        self.assertIn("failed to re-apply label", p.stdout)
        self.assertIn("repair loop or its error accounting failed", p.stdout)

    def test_scratch_override_invalid_red_job(self):
        # Kody r4374404410: an unusable HOLD_GUARD_SCRATCH must fail closed
        # (red), never silently swallow error markers and exit 0.
        # nonexistent path
        p = self.run_guard(
            PR="3",
            PR_URL="u",
            EVENT_NAME="pull_request_target",
            HOLD_GUARD_SCRATCH="/tmp/hold-guard-test-does-not-exist",
        )
        self.assertEqual(p.returncode, 3, p.stderr + p.stdout)
        self.assertIn("not a writable directory", p.stdout)
        # a regular file is not a directory
        fd, file_path = tempfile.mkstemp(prefix="hold-guard-test-")
        os.close(fd)
        try:
            p = self.run_guard(
                PR="3",
                PR_URL="u",
                EVENT_NAME="pull_request_target",
                HOLD_GUARD_SCRATCH=file_path,
            )
            self.assertEqual(p.returncode, 3, p.stderr + p.stdout)
            self.assertIn("not a writable directory", p.stdout)
        finally:
            os.unlink(file_path)
        # an unwritable directory
        scratch = tempfile.mkdtemp(prefix="hold-guard-test-")
        os.chmod(scratch, 0o555)
        try:
            p = self.run_guard(
                PR="3",
                PR_URL="u",
                EVENT_NAME="pull_request_target",
                HOLD_GUARD_SCRATCH=scratch,
            )
            self.assertEqual(p.returncode, 3, p.stderr + p.stdout)
            self.assertIn("not a writable directory", p.stdout)
        finally:
            os.chmod(scratch, 0o755)
            os.rmdir(scratch)


if __name__ == "__main__":
    unittest.main()
