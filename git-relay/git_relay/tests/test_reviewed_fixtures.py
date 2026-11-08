"""Exact-content attestation boundary tests. No network or live credentials."""

import base64
from hashlib import sha256
from unittest import TestCase
from unittest.mock import patch

from git_relay.reviewed_fixtures import _REVIEWED_FIXTURES, unreviewed_file_secret
from git_relay.secret_scan import find_secret
from git_relay.tests import test_workflow_verbs as support
from git_relay.workflow_verbs import Refused, WorkflowVerbs

Multi, REPO, SHA = support.Multi, support.REPO, support.SHA


class ReviewedFixturesTests(TestCase):
    def setUp(self):
        self.path = "tests/synthetic.py"
        self.raw = ("fixture = 'gh" + "p_" + "A" * 30 + "'\n").encode()
        self.entry = (REPO, self.path, sha256(self.raw).hexdigest())

    def test_registry_is_bounded_exact_and_cannot_be_supplied_by_caller(self):
        self.assertIsInstance(_REVIEWED_FIXTURES, frozenset)
        self.assertEqual(
            len(_REVIEWED_FIXTURES), 13
        )  # pin moves only with a reviewed attestation (2026-11-05, PR #1399)
        for repo, path, digest in _REVIEWED_FIXTURES:
            self.assertEqual(repo, REPO)
            self.assertTrue(path.startswith("scripts/"))
            self.assertEqual(len(digest), 64)
            self.assertEqual(digest, digest.lower())
        args = self.args()
        args["reviewed_fixture"] = True
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_files_put", args, REPO)

    def test_full_scan_and_exact_binding(self):
        self.assertEqual(find_secret(self.raw.decode()), "github_token")
        self.assertEqual(unreviewed_file_secret(REPO, self.path, self.raw), "github_token")
        with patch("git_relay.reviewed_fixtures._REVIEWED_FIXTURES", frozenset({self.entry})):
            self.assertIsNone(unreviewed_file_secret(REPO, self.path, self.raw))
            for repo, path, raw in [
                ("attacker/repo", self.path, self.raw),
                (REPO.lower(), self.path, self.raw),
                (REPO, self.path + ".copy", self.raw),
                (REPO, self.path, self.raw + b"# harmless edit\n"),
                (REPO, self.path, self.raw.replace(b"A", b"B", 1)),
                (REPO, self.path, self.raw + ("gh" + "p_" + "B" * 30).encode()),
            ]:
                self.assertEqual(unreviewed_file_secret(repo, path, raw), "github_token")
        self.assertIsNone(unreviewed_file_secret(REPO, self.path, b"ordinary source"))

    def args(self):
        return {
            "repo": REPO,
            "branch": "feat/x",
            "message": "test",
            "expected_head_sha": SHA,
            "files": [{"path": self.path, "content_b64": base64.b64encode(self.raw).decode()}],
        }

    def test_both_file_actions_preflight_and_every_byte_still_checked(self):
        multi = self.args()
        single = {
            "repo": REPO,
            "branch": "feat/x",
            "message": "test",
            "path": self.path,
            "content_b64": multi["files"][0]["content_b64"],
        }
        for action, args in [("wf_file_put", single), ("wf_files_put", multi)]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, args, REPO)
            with patch("git_relay.reviewed_fixtures._REVIEWED_FIXTURES", frozenset({self.entry})):
                WorkflowVerbs.check_shape(action, args, REPO)
                bad = dict(args, message=self.raw.decode())
                with self.assertRaises(Refused):
                    WorkflowVerbs.check_shape(action, bad, REPO)
        with patch("git_relay.reviewed_fixtures._REVIEWED_FIXTURES", frozenset({self.entry})):
            multi["files"].append({"path": "tests/other.py", "content_b64": single["content_b64"]})
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_files_put", multi, REPO)

    def test_preflight_refusal_has_no_upstream_write(self):
        c = support.Tests()._git_data_client()
        with self.assertRaises(Refused):
            Multi(c).run("wf_files_put", self.args())
        self.assertEqual(c.calls, [])
        with patch("git_relay.reviewed_fixtures._REVIEWED_FIXTURES", frozenset({self.entry})):
            out = Multi(c).run("wf_files_put", self.args())
        self.assertEqual(out["commit"], "1" * 40)


class RepoTreeConsistencyTests(TestCase):
    """Registry vs the checked-out tree: future targets stay attested, drift gates."""

    repo_root = __import__("pathlib").Path(__file__).resolve().parents[3]

    # Files whose bytes legitimately differ from the attested ones: the rename
    # that landed spells their credential-shaped test text in pieces, so the
    # files no longer contain anything the scanner refuses and need no
    # attestation.  Every other existing path must digest-match.
    KNOWN_PRE_RENAME_DRIFT = frozenset(
        {
            "scripts/fleet_lanes/tests/test_install_mcp.py",
            "scripts/simple-tools-mcp/DEPLOY-PHASE0.md",
            "scripts/simple-tools-mcp/test/gr-workflow-v1.test.mjs",
            "scripts/simple-tools-mcp/test/integration.workerd.mjs",
            "scripts/simple_tools/mcp/fixtures.jsonl",
            "scripts/simple_tools/tests/test_infisical_api_route.py",
            "scripts/simple_tools/tests/test_mcp_extras.py",
            "scripts/simple_tools/tests/test_operator_send.py",
        }
    )

    def test_existing_attested_paths_carry_exactly_the_attested_bytes(self):
        for _repo, path, digest in sorted(_REVIEWED_FIXTURES):
            target = self.repo_root / path
            if not target.is_file():
                continue  # future rename target: absent on main is expected
            actual = sha256(target.read_bytes()).hexdigest()
            if actual == digest:
                self.assertIsNone(
                    unreviewed_file_secret(REPO, path, target.read_bytes())
                )  # the real registry accepts the live bytes
                continue
            self.assertIn(
                path,
                self.KNOWN_PRE_RENAME_DRIFT,
                f"{path} exists with bytes matching neither the attested "
                "digest nor a declared pre-rename drift",
            )

    def test_absent_future_targets_still_refuse_changed_bytes(self):
        for _repo, path, _digest in sorted(_REVIEWED_FIXTURES):
            if (self.repo_root / path).is_file():
                continue  # live-path acceptance is covered above
            self.assertIsNotNone(
                unreviewed_file_secret(
                    REPO, path, ("fixture = 'gh" + "p_" + "A" * 30 + "'\n").encode()
                )
            )  # no registry entry may excuse bytes other than the exact attested ones
