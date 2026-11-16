"""Exact public-integrity relay gates; synthetic values and fake provider only."""

import base64
import json
from hashlib import sha256
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from git_relay import patch_secretscan
from git_relay.reviewed_fixtures import _REVIEWED_FIXTURES, unreviewed_file_secret
from git_relay.tests import test_workflow_verbs as support
from git_relay.tests.test_workflow_verbs import REPO, SHA, Multi
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class ReviewedPublicIntegrityTests(TestCase):
    path = "app/package-lock.json"

    def setUp(self):
        encoded = "Ab1" * 10 + "C2" + "/" + "A" * 53 + "=="
        self.raw = (json.dumps({"integrity": "sha512-" + encoded}) + "\n").encode()

    def approve(self, raw):
        return patch.object(
            patch_secretscan,
            "_REVIEWED_PUBLIC_INTEGRITIES",
            frozenset({(REPO, self.path, sha256(raw).hexdigest())}),
        )

    def args(self, raw=None):
        return {
            "repo": REPO,
            "branch": "feat/x",
            "message": "test",
            "expected_head_sha": SHA,
            "files": [
                {
                    "path": self.path,
                    "content_b64": base64.b64encode(self.raw if raw is None else raw).decode(),
                }
            ],
        }

    def test_vendor_is_byte_identical_and_legacy_registry_is_not_extended(self):
        root = Path(__file__).resolve().parents[3]
        self.assertEqual(
            (root / "scripts/simple_tools/secretscan.py").read_bytes(),
            (root / "git-relay/git_relay/patch_secretscan.py").read_bytes(),
        )
        self.assertEqual(len(_REVIEWED_FIXTURES), 13)
        self.assertTrue(all(repo == REPO for repo, _path, _digest in _REVIEWED_FIXTURES))
        self.assertEqual(len(patch_secretscan._REVIEWED_PUBLIC_INTEGRITIES), 2)

    def test_relay_full_file_gate_keeps_loaded_and_own_provider_checks(self):
        self.assertEqual(
            unreviewed_file_secret(REPO, self.path, self.raw), "a Zulip-shaped API key"
        )
        with self.approve(self.raw):
            self.assertIsNone(unreviewed_file_secret(REPO, self.path, self.raw))
            self.assertEqual(
                unreviewed_file_secret(REPO, self.path, self.raw, known=(v for v in ("", "Ab1"))),
                "a loaded credential",
            )
            for repo, path, raw in [
                (REPO + "2", self.path, self.raw),
                (REPO, self.path + ".copy", self.raw),
                (REPO, self.path, self.raw + b" "),
            ]:
                self.assertEqual(unreviewed_file_secret(repo, path, raw), "a Zulip-shaped API key")
        # These are relay-specific shapes, including a shorter GitHub shape
        # than the shared scanner detects; even exact public review cannot waive them.
        for value, category in [
            ("Bearer " + "a" * 24, "bearer_credential"),
            ("gh" + "p_" + "a" * 20, "github_token"),
        ]:
            raw = self.raw + value.encode()
            with self.approve(raw):
                self.assertEqual(unreviewed_file_secret(REPO, self.path, raw), category)

    def test_both_normal_file_actions_preflight_full_bytes(self):
        multi = self.args()
        single = {
            key: value for key, value in multi.items() if key not in {"files", "expected_head_sha"}
        }
        single.update(multi["files"][0])
        for action, args in [("wf_file_put", single), ("wf_files_put", multi)]:
            with self.subTest(action=action):
                with self.assertRaises(Refused):
                    WorkflowVerbs.check_shape(action, args, REPO)
                with self.approve(self.raw):
                    WorkflowVerbs.check_shape(action, args, REPO)
                    with self.assertRaises(Refused):
                        WorkflowVerbs.check_shape(
                            action, dict(args, message="gh" + "p_" + "a" * 24), REPO
                        )

    def test_real_files_put_dispatch_admits_only_exact_review_before_provider_calls(self):
        client = support.Tests()._git_data_client()
        with self.assertRaises(Refused):
            Multi(client).run("wf_files_put", self.args())
        self.assertEqual(client.calls, [])
        with self.approve(self.raw):
            out = Multi(client).run("wf_files_put", self.args())
            self.assertEqual(out["commit"], "1" * 40)
            blob = next(
                payload
                for method, path, payload, _bearer in client.calls
                if method == "POST" and path.endswith("/git/blobs")
            )
            self.assertEqual(base64.b64decode(blob["content"]), self.raw)
            for args, known in [
                (self.args(self.raw + b" "), ()),
                (self.args(), ("Ab1",)),
                (
                    dict(
                        self.args(),
                        files=self.args()["files"]
                        + [
                            {
                                "path": "other.json",
                                "content_b64": self.args()["files"][0]["content_b64"],
                            }
                        ],
                    ),
                    (),
                ),
            ]:
                refused_client = support.Tests()._git_data_client()
                handle = WorkflowVerbs(
                    refused_client,
                    support.Policy(),
                    "swu-wise",
                    "install-token-value",
                    "swu-wise",
                    "wf_files_put",
                    REPO,
                )
                handle.loaded_known = known
                with self.assertRaises(Refused):
                    handle.run("wf_files_put", args)
                self.assertEqual(refused_client.calls, [])
