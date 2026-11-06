import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.server import _REAPPLY_STATE_ACTIONS, PreReadUnavailable
from git_relay.workflow_verbs import MAX_FILE_RAW, Refused, WorkflowVerbs


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

    def test_tree_existing_blob_reference(self):
        import base64

        a = "wf_git_tree_create"
        blob_sha = "d" * 40
        blob = {
            "encoding": "base64",
            "size": 2,
            "content": base64.b64encode(b"hi").decode(),
        }
        tree = {"sha": "e" * 40, "truncated": False, "tree": []}
        args = {
            "repo": REPO,
            "base_tree": SHA,
            "files": [{"path": "big.txt", "sha": blob_sha, "mode": "100644"}],
        }
        c = Client(blob, tree)
        out = self.handle(c, a).run(a, args)
        self.assertEqual(out["tree"]["sha"], "e" * 40)
        self.assertEqual(c.calls[0][0], "GET")
        self.assertIn(f"/git/blobs/{blob_sha}", c.calls[0][1])
        self.assertEqual(
            c.calls[1][2],
            {
                "base_tree": SHA,
                "tree": [{"path": "big.txt", "mode": "100644", "type": "blob", "sha": blob_sha}],
            },
        )
        # exactly one of text/sha, well-formed sha
        for bad_files in [
            [{"path": "x", "mode": "100644"}],
            [{"path": "x", "text": "hi", "sha": blob_sha, "mode": "100644"}],
            [{"path": "x", "sha": "zz", "mode": "100644"}],
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    a, {"repo": REPO, "base_tree": SHA, "files": bad_files}, REPO
                )

    def test_tree_blob_reference_never_bypasses_scan(self):
        import base64

        a = "wf_git_tree_create"
        blob_sha = "d" * 40

        def blob(payload):
            return {
                "encoding": "base64",
                "size": len(payload),
                "content": base64.b64encode(payload).decode(),
            }

        base = {"repo": REPO, "base_tree": SHA}
        files = [{"path": "x.txt", "sha": blob_sha, "mode": "100644"}]
        # secret-shaped referenced content is refused before any tree write
        c = Client(blob(b"ghp_" + b"a" * 30))
        with self.assertRaises(Refused):
            self.handle(c, a).run(a, {**base, "files": files})
        self.assertEqual(len(c.calls), 1)
        # non-utf-8 referenced content is refused (text trees only)
        c = Client(blob(b"\xff\xfe\x00"))
        with self.assertRaises(Refused):
            self.handle(c, a).run(a, {**base, "files": files})
        # GitHub wraps blob base64 at 60 columns: wrapped content still decodes
        wrapped = blob(b"hi")
        wrapped["content"] = wrapped["content"][:2] + "\n" + wrapped["content"][2:]
        tree = {"sha": "e" * 40, "truncated": False, "tree": []}
        c = Client(wrapped, tree)
        self.assertEqual(self.handle(c, a).run(a, {**base, "files": files})["tree"]["sha"], "e" * 40)
        # a receipt that overstates the size but decodes small is accepted: budgets
        # measure the decoded bytes, never the receipt's declared size
        tree = {"sha": "e" * 40, "truncated": False, "tree": []}
        c = Client({"encoding": "base64", "size": 3 * 1024 * 1024, "content": "aGk="}, tree)
        out = self.handle(c, a).run(a, {**base, "files": files})
        self.assertEqual(out["tree"]["sha"], "e" * 40)
        c = Client({"encoding": "utf-8", "size": 2, "content": "hi"})
        with self.assertRaises(Refused):
            self.handle(c, a).run(a, {**base, "files": files})

    def test_tree_blob_reference_budget_measures_decoded_bytes(self):
        import base64

        a = "wf_git_tree_create"
        blob_sha = "d" * 40
        base = {"repo": REPO, "base_tree": SHA}
        files = [{"path": "x.txt", "sha": blob_sha, "mode": "100644"}]
        # adversarial: the declared size lies low while the decoded bytes exceed the
        # per-file raw budget - refused on the measured length, never the receipt
        big = b"a" * (MAX_FILE_RAW + 1)
        c = Client({"encoding": "base64", "size": 2, "content": base64.b64encode(big).decode()})
        with self.assertRaises(Refused):
            self.handle(c, a).run(a, {**base, "files": files})
        self.assertEqual(len(c.calls), 1, "refused before any tree write")

    def test_tree_blob_pre_read_failures_release_retryable(self):
        a = "wf_git_tree_create"
        blob_sha = "d" * 40
        base = {"repo": REPO, "base_tree": SHA}
        files = [{"path": "x.txt", "sha": blob_sha, "mode": "100644"}]

        class Boom(Client):
            def __init__(self, exc):
                super().__init__()
                self.exc = exc

            def _call(self, *args, **kw):
                raise self.exc

        # transient upstream failures during the pre-read release as retryable
        # (PreReadUnavailable): the read ran before any dispatch, so no write was
        # attempted and nothing is unknown
        for status in (0, 408, 500, 503):
            with self.assertRaises(PreReadUnavailable):
                self.handle(Boom(RelayGitHubError(status, "boom")), a).run(
                    a, {**base, "files": files}
                )
        # an oversized response is a definite refusal, never a retryable unknown
        with self.assertRaises(Refused):
            self.handle(
                Boom(
                    RelayGitHubError(
                        0, "github response exceeded 1 MiB bound", code="response_too_large"
                    )
                ),
                a,
            ).run(a, {**base, "files": files})
        # a definite 404 stays a hard upstream error, not a retry
        with self.assertRaises(RelayGitHubError):
            self.handle(Boom(RelayGitHubError(404, "not found")), a).run(
                a, {**base, "files": files}
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
