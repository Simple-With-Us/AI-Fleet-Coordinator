import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.server import _REAPPLY_STATE_ACTIONS
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Writes(unittest.TestCase):
    def handle(self, c, a):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)

    def test_tree_content_only_fixed_payload(self):
        a = "wf_git_tree_create"
        args = {
            "repo": REPO,
            "base_tree": SHA,
            "files": [{"path": "x.txt", "text": "hi", "mode": "100755"}],
        }
        c = Client({"sha": "b" * 40, "truncated": False, "tree": []})
        self.assertEqual(self.handle(c, a).run(a, args)["tree"]["files"], 1)
        self.assertEqual(
            c.calls[0][2],
            {
                "base_tree": SHA,
                "tree": [{"path": "x.txt", "content": "hi", "mode": "100755", "type": "blob"}],
            },
        )
        self.assertEqual(c.calls[0][3], "own")
        for files in [
            [{"path": ".github/workflows/x", "text": "hi", "mode": "100644"}],
            [{"path": "a", "text": "hi", "mode": "120000"}],
            [{"path": "a", "text": "é" * 20001, "mode": "100644"}],
            args["files"] * 2,
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(a, {**args, "files": files}, REPO)

    def test_tree_sha_entries_reference_verified_existing_blobs(self):
        import base64 as b64m
        import hashlib as hl

        a = "wf_git_tree_create"

        def blob_of(raw):
            sha = hl.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False
            ).hexdigest()
            return sha, {
                "sha": sha,
                "encoding": "base64",
                "size": len(raw),
                "content": b64m.b64encode(raw).decode(),
            }

        # A 61 KB blob, over the inline text cap, is referenceable once verified.
        blob_sha, blob = blob_of(b"x = 1\n" * 8000)
        tree_reply = {"sha": "b" * 40, "truncated": False, "tree": []}
        args = {
            "repo": REPO,
            "base_tree": SHA,
            "files": [
                {"path": "big.py", "sha": blob_sha, "mode": "100644"},
                {"path": "small.txt", "text": "hi", "mode": "100644"},
            ],
        }
        c = Client(blob, tree_reply)
        out = self.handle(c, a).run(a, args)["tree"]
        self.assertEqual(out["files"], 2)
        self.assertEqual(c.calls[0][0], "GET")
        self.assertIn("/git/blobs/" + blob_sha, c.calls[0][1])
        self.assertEqual(
            c.calls[1][2]["tree"][0],
            {"path": "big.py", "mode": "100644", "type": "blob", "sha": blob_sha},
        )

        # Content the relay scanner reads as credential-shaped is refused at the
        # destination path even though the blob already exists upstream.
        secret_sha, secret_blob = blob_of(b"Authorization: Bearer " + b"a1" * 12)
        with self.assertRaises(Refused):
            self.handle(Client(secret_blob), a).run(
                a, {"repo": REPO, "base_tree": SHA,
                    "files": [{"path": "s.py", "sha": secret_sha, "mode": "100644"}]}
            )
        # An unverifiable blob identity is a 502 unknown-outcome, never a write.
        bad = dict(blob)
        bad["content"] = b64m.b64encode(b"different bytes").decode()
        with self.assertRaises(RelayGitHubError):
            self.handle(Client(bad), a).run(
                a, {"repo": REPO, "base_tree": SHA,
                    "files": [{"path": "big.py", "sha": blob_sha, "mode": "100644"}]}
            )
        # Shape refusals before any upstream call:  mixed keys, workflow path, bad mode.
        for files in [
            [{"path": "x", "sha": blob_sha, "text": "hi", "mode": "100644"}],
            [{"path": ".github/workflows/x", "sha": blob_sha, "mode": "100644"}],
            [{"path": "x", "sha": blob_sha, "mode": "120000"}],
            [{"path": "x", "sha": "z" * 40, "mode": "100644"}],
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    a, {"repo": REPO, "base_tree": SHA, "files": files}, REPO
                )

    def test_commit_exact_tree_parents_and_no_identity_selector(self):
        a = "wf_git_commit_create"
        args = {"repo": REPO, "tree_sha": SHA, "parents": ["b" * 40], "message": "edit"}
        c = Client(
            {
                "sha": "c" * 40,
                "tree": {"sha": SHA},
                "parents": [{"sha": "b" * 40}],
                "message": "edit",
            }
        )
        self.assertEqual(self.handle(c, a).run(a, args)["commit"]["sha"], "c" * 40)
        for extra in [{"author": {}}, {"parents": []}, {"parents": [SHA, SHA]}, {"message": " "}]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(a, {**args, **extra}, REPO)

    def test_postwrite_malformed_stays_unknown_strict_idempotency(self):
        for a, args in [
            (
                "wf_git_tree_create",
                {"base_tree": SHA, "files": [{"path": "x", "text": "hi", "mode": "100644"}]},
            ),
            ("wf_git_commit_create", {"tree_sha": SHA, "parents": [SHA], "message": "edit"}),
        ]:
            c = Client({})
            with self.assertRaises(RelayGitHubError) as err:
                self.handle(c, a).run(a, {"repo": REPO, **args})
            self.assertEqual(err.exception.status, 502)
            self.assertEqual(len(c.calls), 1)
            self.assertNotIn(a, _REAPPLY_STATE_ACTIONS)

    def test_audit_identifiers_only_and_path_scanned(self):
        from git_relay.server import _workflow_audit_detail

        result = {
            "commit": {"sha": SHA, "message": "omit"},
            "tree": {"sha": SHA},
            "blob": {"sha": SHA, "text": "omit"},
        }
        detail = _workflow_audit_detail({}, result)
        self.assertEqual(
            detail, {"result_commit_sha": SHA, "result_tree_sha": SHA, "result_blob_sha": SHA}
        )
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape(
                "wf_git_tree_create",
                {
                    "repo": REPO,
                    "base_tree": SHA,
                    "files": [{"path": "ghp_" + "a" * 30, "text": "hi", "mode": "100644"}],
                },
                REPO,
            )

    def test_lone_surrogates_are_terminal_refusals_before_upstream(self):
        for text in ("\ud800", "\udfff", "ok\ud800end"):
            for action, args in (
                (
                    "wf_git_tree_create",
                    {
                        "base_tree": SHA,
                        "files": [{"path": "x.txt", "text": text, "mode": "100644"}],
                    },
                ),
                ("wf_git_commit_create", {"tree_sha": SHA, "parents": [SHA], "message": text}),
                ("wf_git_blob_create", {"text": text}),
            ):
                with self.subTest(action=action, text=repr(text)):
                    args = {"repo": REPO, **args}
                    with self.assertRaises(Refused):
                        WorkflowVerbs.check_shape(action, args, REPO)
                    client = Client({})
                    with self.assertRaises(Refused):
                        self.handle(client, action).run(action, args)
                    self.assertEqual(client.calls, [])
