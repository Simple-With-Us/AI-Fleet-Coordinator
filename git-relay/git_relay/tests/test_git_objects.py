import base64
import hashlib
import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Objects(unittest.TestCase):
    def handle(self, c, a):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)

    def test_blob_text_hash_and_scan(self):
        raw = b"hi"
        sha = hashlib.sha1(b"blob 2\0hi").hexdigest()
        c = Client({"sha": sha})
        a = "wf_git_blob_create"
        out = self.handle(c, a).run(a, {"repo": REPO, "text": "hi"})
        self.assertEqual(out["blob"]["sha"], sha)
        self.assertEqual(
            c.calls[0],
            (
                "POST",
                "/repos/" + REPO + "/git/blobs",
                {"content": "hi", "encoding": "utf-8"},
                "own",
            ),
        )
        c = Client(
            {"sha": sha, "encoding": "base64", "size": 2, "content": base64.b64encode(raw).decode()}
        )
        a = "wf_git_blob_get"
        self.assertEqual(self.handle(c, a).run(a, {"repo": REPO, "sha": sha})["blob"]["text"], "hi")
        for text in ["é" * 20001, "ghp_" + "a" * 30]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_git_blob_create", {"repo": REPO, "text": text}, REPO)

    def test_wrong_blob_receipt_is_unknown(self):
        c = Client({"sha": SHA})
        a = "wf_git_blob_create"
        with self.assertRaises(RelayGitHubError):
            self.handle(c, a).run(a, {"repo": REPO, "text": "hi"})

    def test_reads_closed_projection_and_routes(self):
        for a, reply in [
            (
                "wf_git_commit_get",
                {
                    "sha": SHA,
                    "tree": {"sha": SHA},
                    "parents": [{"sha": SHA}],
                    "message": "hi",
                    "private": "omit",
                },
            ),
            (
                "wf_git_tree_get",
                {
                    "sha": SHA,
                    "truncated": False,
                    "tree": [{"sha": SHA, "type": "blob", "mode": "100644", "path": "x"}],
                    "private": "omit",
                },
            ),
        ]:
            c = Client(reply)
            out = self.handle(c, a).run(a, {"repo": REPO, "sha": SHA})
            self.assertNotIn("private", str(out))
            self.assertEqual(c.calls[0][3], "own")
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(a, {"repo": REPO, "sha": SHA, "recursive": True}, REPO)

    def test_bounded_reads_refuse_malformed(self):
        for a, reply in [
            ("wf_git_tree_get", {"sha": SHA, "truncated": False, "tree": [{}] * 101}),
            ("wf_git_commit_get", {"sha": SHA, "tree": [], "parents": [], "message": "hi"}),
            ("wf_git_blob_get", {"sha": SHA, "encoding": "base64", "size": 40001, "content": ""}),
        ]:
            c = Client(reply)
            with self.assertRaises((RelayGitHubError, Refused)):
                self.handle(c, a).run(a, {"repo": REPO, "sha": SHA})

    def test_deterministic_read_limits_are_terminal_refusals(self):
        for action, reply in [
            ("wf_git_tree_get", {"tree": [{}] * 101}),
            ("wf_git_blob_get", {"size": 40001}),
        ]:
            with self.assertRaises(Refused):
                self.handle(Client(reply), action).run(action, {"repo": REPO, "sha": SHA})
        raw = b"\xff"
        sha = hashlib.sha1(b"blob 1\0" + raw).hexdigest()
        with self.assertRaises(Refused):
            self.handle(
                Client({"sha": sha, "encoding": "base64", "size": 1, "content": "/w=="}),
                "wf_git_blob_get",
            ).run("wf_git_blob_get", {"repo": REPO, "sha": sha})

    def test_decoded_credentials_and_commit_read_limits_refused(self):
        raw = ("ghp_" + "a" * 30).encode()
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        c = Client(
            {
                "sha": sha,
                "encoding": "base64",
                "size": len(raw),
                "content": base64.b64encode(raw).decode(),
            }
        )
        with self.assertRaises(Refused):
            self.handle(c, "wf_git_blob_get").run("wf_git_blob_get", {"repo": REPO, "sha": sha})
        for value in [{"message": "x" * 32769}, {"parents": [{}] * 21}]:
            with self.assertRaises(Refused):
                self.handle(Client(value), "wf_git_commit_get").run(
                    "wf_git_commit_get", {"repo": REPO, "sha": SHA}
                )
