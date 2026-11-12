"""Exact-content attestation boundary tests. No network or live credentials."""

import base64
import unittest
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

    def test_loaded_credentials_refused_before_attestation(self):
        loaded = "loaded-value-" + "x" * 20
        tainted = self.raw + ("token = '" + loaded + "'" + chr(10)).encode()
        entry = (REPO, self.path, sha256(tainted).hexdigest())
        with patch("git_relay.reviewed_fixtures._REVIEWED_FIXTURES", frozenset({entry})):
            self.assertEqual(
                unreviewed_file_secret(REPO, self.path, tainted, known=[loaded]),
                "a loaded credential",
            )
            self.assertIsNone(unreviewed_file_secret(REPO, self.path, tainted, known=[]))
        self.assertEqual(
            unreviewed_file_secret(
                REPO, self.path, b"ordinary source " + loaded.encode(), known=[loaded]
            ),
            "a loaded credential",
        )
        # Every nonempty loaded value refuses, even if short.
        self.assertEqual(
            unreviewed_file_secret(REPO, self.path, b"ordinary source tok", known=["tok"]),
            "a loaded credential",
        )
        self.assertIsNone(
            unreviewed_file_secret(REPO, self.path, b"ordinary source", known=["tok", ""])
        )

    def _handle(self, client, action, known):
        handle = WorkflowVerbs(
            client, support.Policy(), "swu-wise", "install-token-value", "swu-wise", action, REPO
        )
        handle.loaded_known = tuple(known)
        return handle

    def test_caller_and_installation_credentials_refused_on_file_publication(self):
        # for_action threads caller then installation token; each refuses on
        # both file actions before any mutation, and clean content passes.
        caller = "caller-token-value-0001"
        install = "install-token-value-0002"
        for token in (caller, install):
            content_b64 = base64.b64encode(("x = " + repr(token) + chr(10)).encode()).decode()
            single = {
                "repo": REPO,
                "branch": "feat/x",
                "message": "test",
                "path": self.path,
                "content_b64": content_b64,
            }
            multi = dict(self.args(), files=[{"path": self.path, "content_b64": content_b64}])
            for action, args in [("wf_file_put", single), ("wf_files_put", multi)]:
                c = support.Tests()._git_data_client()
                with self.assertRaises(Refused):
                    self._handle(c, action, (caller, install)).run(action, args)
                self.assertEqual(c.calls, [])
        clean = base64.b64encode(b"x = 1\n").decode()
        multi = dict(self.args(), files=[{"path": self.path, "content_b64": clean}])
        c = support.Tests()._git_data_client()
        out = self._handle(c, "wf_files_put", (caller, install)).run("wf_files_put", multi)
        self.assertEqual(out["commit"], "1" * 40)

    def test_syntax_probe_carries_no_credential(self):
        # The word probe appears in ordinary source; the check_shape probe
        # carries no real credential, so this must not refuse.
        probe_content = base64.b64encode(b"probe probe probe\n").decode()
        single = {
            "repo": REPO,
            "branch": "feat/x",
            "message": "test",
            "path": self.path,
            "content_b64": probe_content,
        }
        multi = dict(self.args(), files=[{"path": self.path, "content_b64": probe_content}])
        WorkflowVerbs.check_shape("wf_file_put", single, REPO)
        WorkflowVerbs.check_shape("wf_files_put", multi, REPO)

    def test_generic_registry_admission_reaches_publication_scan(self):
        raw = b'CLIENT_SECRET = "mock-client-secret-value"\n'
        entry = (REPO, self.path, sha256(raw).hexdigest())
        generic = ("assigned credential", "assigned secret")
        self.assertIn(unreviewed_file_secret(REPO, self.path, raw), generic)
        with patch("git_relay.patch_secretscan._REVIEWED_GENERIC_FIXTURES", frozenset({entry})):
            self.assertIsNone(unreviewed_file_secret(REPO, self.path, raw))
        # A byte of drift never waives.
        self.assertIn(unreviewed_file_secret(REPO, self.path, raw + b"# drift\n"), generic)

    def test_zulip_shape_refused_on_publication_scan(self):
        token = "Ab1" * 10 + "C2"  # exactly 32 chars, mixed case and digits
        raw = (token + chr(10)).encode()
        self.assertEqual(unreviewed_file_secret(REPO, self.path, raw), "a Zulip-shaped API key")

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


class CurrentManifestScopeTests(unittest.TestCase):
    def test_single_refresh_is_bound_to_manifest_and_existing_destination(self):
        import json
        from pathlib import Path

        from git_relay.reviewed_fixtures import _REVIEWED_FIXTURES

        root = Path(__file__).resolve().parents[3]
        doc = json.loads((root / "docs/security/preservation-pending-manifest.json").read_text())
        self.assertFalse(doc["active"])
        rows = doc["single_exact_attestation_refresh"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["path"], "scripts/simple_tools/tests/test_infisical_api_route.py")
        self.assertEqual(len(row["edits"]), 5)
        self.assertIn((row["repo"], row["path"], row["result_sha256"]), _REVIEWED_FIXTURES)
        self.assertEqual(len(_REVIEWED_FIXTURES), 13)


class GrWorkflowV1AttestationRowTests(TestCase):
    """The 2026-11-10 refresh binds exactly the PR #1707 bytes; superseded bytes fail closed."""

    PATH = "scripts/simple-tools-mcp/test/gr-workflow-v1.test.mjs"
    DIGEST = "8a0cf816252c39f32202429861483d0d128d4a30a7ff92e6cbbbad988ee7dad2"

    def test_row_is_exact_and_unique(self):
        rows = [r for r in _REVIEWED_FIXTURES if r[1] == self.PATH]
        self.assertEqual(rows, [(support.REPO, self.PATH, self.DIGEST)])

    def test_bearer_fixture_bytes_pass_only_with_the_exact_attestation(self):
        raw = ("h = 'Bear" + "er caller-own-" + "bearer'\n").encode()
        self.assertEqual(find_secret(raw.decode()), "bearer_credential")
        self.assertEqual(unreviewed_file_secret(support.REPO, self.PATH, raw), "bearer_credential")
        entry = (support.REPO, self.PATH, sha256(raw).hexdigest())
        with patch("git_relay.reviewed_fixtures._REVIEWED_FIXTURES", frozenset({entry})):
            self.assertIsNone(unreviewed_file_secret(support.REPO, self.PATH, raw))
