"""Exact default-branch paths for wf_file_put and wf_file_delete (AFC-633)."""

import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from git_relay.github_client import RelayGitHubError
from git_relay.policy import PolicyError, SeatPolicy
from git_relay.workflow_verbs import (
    _DEFAULT_BRANCH_PREFIX,
    DEFAULT_BRANCH_SHA_MISMATCH,
    Refused,
    WorkflowVerbs,
)

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy

PATH = "fleet-facts.json"
OTHER = "b" * 40


class Boom(Client):
    def _call(
        self,
        method,
        path,
        payload=None,
        bearer=None,
        expected_status=None,
        allow_empty_object=False,
    ):
        value = super()._call(method, path, payload, bearer, expected_status, allow_empty_object)
        if isinstance(value, BaseException):
            raise value
        return value


class GrantPolicy(Policy):
    def app_identity(self, seat):
        return seat, dict(self.ceiling)


def policy():
    return GrantPolicy(paths={"swu-wise": {REPO: (PATH, "mac-facts.json")}})


def handle(client, action, seat="swu-wise"):
    return WorkflowVerbs(client, policy(), seat, "opaque", seat, action, REPO)


def put_args(**kw):
    body = {
        "repo": REPO,
        "path": PATH,
        "branch": "main",
        "message": "publish facts",
        "content_b64": base64.b64encode(b"hello").decode(),
        "prior_sha": SHA,
    }
    body.update(kw)
    return body


class DefaultBranchPaths(unittest.TestCase):
    def test_allowed_path_writes_with_prefix_and_cas(self):
        client = Boom(
            {"default_branch": "main"},
            {"type": "file", "path": PATH, "sha": SHA},
            {"commit": {"sha": "c" * 40}, "content": {"path": PATH}},
        )
        out = handle(client, "wf_file_put").run("wf_file_put", put_args())
        self.assertEqual(out["commit"], "c" * 40)
        method, path, payload, _bearer = client.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertTrue(path.endswith("/contents/" + PATH))
        self.assertEqual(payload["branch"], "main")
        self.assertEqual(payload["sha"], SHA)
        self.assertTrue(payload["message"].startswith(_DEFAULT_BRANCH_PREFIX))
        self.assertEqual(len(client.calls), 3)

    def test_disallowed_path_keeps_the_existing_refusal(self):
        client = Boom({"default_branch": "main"})
        with self.assertRaisesRegex(Refused, "default branch is not writable"):
            handle(client, "wf_file_put").run("wf_file_put", put_args(path="docs/a.md"))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], "GET")

    def test_other_seat_is_refused_on_the_same_path(self):
        client = Boom({"default_branch": "main"})
        with self.assertRaisesRegex(Refused, "default branch is not writable"):
            handle(client, "wf_file_put", seat="swu-echo").run("wf_file_put", put_args())
        self.assertEqual(len(client.calls), 1)

    def test_cas_mismatch_does_not_write(self):
        client = Boom(
            {"default_branch": "main"},
            {"type": "file", "path": PATH, "sha": OTHER},
        )
        with self.assertRaisesRegex(Refused, DEFAULT_BRANCH_SHA_MISMATCH):
            handle(client, "wf_file_put").run("wf_file_put", put_args())
        self.assertEqual([call[0] for call in client.calls], ["GET", "GET"])

    def test_create_requires_the_file_to_be_absent(self):
        client = Boom(
            {"default_branch": "main"},
            RelayGitHubError(404, "missing"),
            {"commit": {"sha": "c" * 40}, "content": {"path": PATH}},
        )
        args = put_args()
        del args["prior_sha"]
        handle(client, "wf_file_put").run("wf_file_put", args)
        self.assertNotIn("sha", client.calls[-1][2])

    def test_delete_allowed_path_matches_the_blob_and_prefixes_the_message(self):
        client = Boom(
            {"default_branch": "main"},
            {"name": "main", "commit": {"sha": OTHER}},
            {"type": "file", "path": "mac-facts.json", "sha": SHA},
            {"content": None, "commit": {"sha": "c" * 40}},
        )
        out = handle(client, "wf_file_delete").run(
            "wf_file_delete",
            {
                "repo": REPO,
                "path": "mac-facts.json",
                "branch": "main",
                "message": "remove the full copy",
                "prior_sha": SHA,
            },
        )
        self.assertEqual(out["deleted"], True)
        self.assertEqual(client.calls[-1][0], "DELETE")
        self.assertEqual(client.calls[-1][2]["sha"], SHA)
        self.assertTrue(client.calls[-1][2]["message"].startswith(_DEFAULT_BRANCH_PREFIX))

    def test_delete_cas_mismatch_does_not_delete(self):
        client = Boom(
            {"default_branch": "main"},
            {"name": "main", "commit": {"sha": OTHER}},
            {"type": "file", "path": "mac-facts.json", "sha": OTHER},
        )
        with self.assertRaisesRegex(Refused, DEFAULT_BRANCH_SHA_MISMATCH):
            handle(client, "wf_file_delete").run(
                "wf_file_delete",
                {
                    "repo": REPO,
                    "path": "mac-facts.json",
                    "branch": "main",
                    "message": "remove the full copy",
                    "prior_sha": SHA,
                },
            )
        self.assertNotIn("DELETE", [call[0] for call in client.calls])

    def test_multi_file_put_stays_refused_on_the_default_branch(self):
        client = Boom({"default_branch": "main"})
        with self.assertRaisesRegex(Refused, "default branch is not writable"):
            handle(client, "wf_files_put").run(
                "wf_files_put",
                {
                    "repo": REPO,
                    "branch": "main",
                    "message": "publish facts",
                    "expected_head_sha": SHA,
                    "files": [{"path": PATH, "content_b64": base64.b64encode(b"hello").decode()}],
                },
            )
        self.assertEqual([call[0] for call in client.calls], ["GET"])


def _seat(grants=None, repos=None, slug="swu-wise"):
    cfg = {
        "token": "t" * 16,
        "actions": ["wf_file_put", "wf_file_delete"],
        "repos": repos or ["Simple-With-Us/*"],
        "app_slug": slug,
        "mint_permissions": {"contents": "write", "metadata": "read"},
    }
    if grants is not None:
        cfg["default_branch_paths"] = grants
    return cfg


class PolicyGrants(unittest.TestCase):
    def test_exact_grant_is_per_seat_and_per_repo(self):
        raw = {
            "swu-wise": _seat({REPO: [PATH]}),
            "swu-echo": _seat(slug="swu-echo"),
        }
        policy = SeatPolicy(json.dumps(raw))
        self.assertTrue(policy.default_branch_path_allowed("swu-wise", REPO, PATH))
        self.assertFalse(policy.default_branch_path_allowed("swu-echo", REPO, PATH))
        self.assertFalse(policy.default_branch_path_allowed("swu-wise", REPO, "docs/a.md"))
        self.assertNotIn("default_branch_paths", policy._seats["swu-echo"])

    def test_globs_dot_segments_and_github_paths_are_refused_at_load(self):
        for paths in (
            ["gb/*.json"],
            ["../fleet-facts.json"],
            [".github/workflows/ci.yml"],
            ["gb/../fleet-facts.json"],
            [PATH, PATH],
        ):
            raw = {"swu-wise": _seat({REPO: paths})}
            with self.assertRaises(PolicyError):
                SeatPolicy(json.dumps(raw))

    def test_a_repo_outside_the_seat_list_is_refused(self):
        raw = {"swu-wise": _seat({"other/repo": [PATH]}, repos=[REPO])}
        with self.assertRaises(PolicyError):
            SeatPolicy(json.dumps(raw))


def _seat_policy():
    path = Path(__file__).resolve().parents[2] / "deploy" / "seat_policy.py"
    spec = importlib.util.spec_from_file_location("seat_policy_default_branch", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RenderGrant(unittest.TestCase):
    def test_a_seat_row_renders_only_its_own_exact_paths(self):
        seat_policy = _seat_policy()
        doc = {
            "profiles": {
                "p": {
                    "actions": ["wf_file_put"],
                    "mint_permissions": {"metadata": "read", "contents": "write"},
                    "repos": ["Simple-With-Us/*"],
                }
            },
            "seats": [
                {
                    "identity": "WISE",
                    "app_slug": "swu-wise",
                    "profile": "p",
                    "default_branch_paths": {REPO: [PATH]},
                },
                {"identity": "ECHO", "app_slug": "swu-echo", "profile": "p"},
            ],
        }
        seats = seat_policy.render(doc)
        self.assertEqual(seats["swu-wise"]["default_branch_paths"], {REPO: [PATH]})
        self.assertNotIn("default_branch_paths", seats["swu-echo"])
        doc["seats"][0]["default_branch_paths"] = {REPO: ["gb/*.json"]}
        with self.assertRaises(seat_policy.PolicyFileError):
            seat_policy.render(doc)


if __name__ == "__main__":
    unittest.main()
