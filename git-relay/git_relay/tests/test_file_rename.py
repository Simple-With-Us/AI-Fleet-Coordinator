import tempfile
import unittest
from unittest.mock import patch

from git_relay.github_client import RelayGitHubError
from git_relay.outcomes import OutcomeStore, key_for
from git_relay.server import _REAPPLY_STATE_ACTIONS, PreReadUnavailable
from git_relay.workflow_verbs import SPECS, Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy
from .test_workflow_wiring import TOKEN, relay

TREE = "b" * 40
NEW_TREE = "c" * 40
NEW = "d" * 40
BLOB = "e" * 40


class Rename(unittest.TestCase):
    def args(self, **kwargs):
        return {
            "repo": REPO,
            "source_path": "old.txt",
            "destination_path": "new.txt",
            "branch": "feature/x",
            "message": "Rename file",
            "expected_head_sha": SHA,
            "prior_sha": BLOB,
            **kwargs,
        }

    def replies(self, mode="100755"):
        return [
            {"default_branch": "main"},
            {"ref": "refs/heads/feature/x", "object": {"type": "commit", "sha": SHA}},
            {"sha": SHA, "tree": {"sha": TREE}},
            {
                "sha": TREE,
                "truncated": False,
                "tree": [{"path": "old.txt", "type": "blob", "mode": mode, "sha": BLOB}],
            },
            {"sha": NEW_TREE},
            {"sha": NEW, "tree": {"sha": NEW_TREE}, "parents": [{"sha": SHA}]},
            {"ref": "refs/heads/feature/x", "object": {"type": "commit", "sha": NEW}},
        ]

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_file_rename", REPO)

    def test_exact_blob_mode_and_no_content_read(self):
        for mode in ["100644", "100755"]:
            c = Client(*self.replies(mode))
            out = self.handle(c).run("wf_file_rename", self.args())
            self.assertEqual(
                out,
                {
                    "source_path": "old.txt",
                    "destination_path": "new.txt",
                    "branch": "feature/x",
                    "commit": NEW,
                    "blob_sha": BLOB,
                },
            )
            self.assertEqual(
                c.calls[4],
                (
                    "POST",
                    f"/repos/{REPO}/git/trees",
                    {
                        "base_tree": TREE,
                        "tree": [
                            {"path": "old.txt", "mode": mode, "type": "blob", "sha": None},
                            {"path": "new.txt", "mode": mode, "type": "blob", "sha": BLOB},
                        ],
                    },
                    "own",
                ),
            )
            self.assertEqual(c.calls[-1][2], {"sha": NEW, "force": False})
            self.assertTrue(all(x[3] == "own" for x in c.calls))
            self.assertFalse(any("/contents/" in x[1] or "/git/blobs/" in x[1] for x in c.calls))
            self.assertEqual(SPECS["wf_file_rename"], ({"contents": "write"}, True))
            self.assertNotIn("wf_file_rename", _REAPPLY_STATE_ACTIONS)
        minter = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), minter, "swu-wise", "wf_file_rename", REPO)
        self.assertEqual(minter.calls[0][2], {"contents": "write", "metadata": "read"})

    def test_closed_preflight_and_workflow_paths(self):
        # Synthetic assembled fixtures, never credentials.
        for bad in [
            {"source_path": ".github/workflows/x.yml"},
            {"destination_path": ".github"},
            {"source_path": "../x"},
            {"destination_path": "old.txt/a"},
            {"destination_path": "old.txt"},
            {"destination_path": "a/" * 9 + "x"},
            {"branch": "refs/heads/main"},
            {"message": " "},
            {"message": "é" * 251},
            {"message": "\ud800"},
            {"message": "sk-proj-" + "x" * 24},
            {"force": False},
            {"prior_sha": False},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_file_rename", self.args(**bad), REPO)

    def test_no_write_for_source_destination_or_head_mismatch(self):
        for index, value in [
            (0, {"default_branch": "feature/x"}),
            (1, {"ref": "refs/heads/feature/x", "object": {"type": "commit", "sha": "f" * 40}}),
            (3, {"sha": TREE, "truncated": False, "tree": []}),
            (
                3,
                {
                    "sha": TREE,
                    "truncated": False,
                    "tree": [{"path": "old.txt", "type": "blob", "mode": "120000", "sha": BLOB}],
                },
            ),
            (
                3,
                {
                    "sha": TREE,
                    "truncated": False,
                    "tree": [
                        {"path": "old.txt", "type": "blob", "mode": "100644", "sha": BLOB},
                        {"path": "new.txt", "type": "blob", "mode": "100644", "sha": BLOB},
                    ],
                },
            ),
        ]:
            replies = self.replies()
            replies[index] = value
            c = Client(*replies)
            with self.assertRaises(Refused):
                self.handle(c).run("wf_file_rename", self.args())
            self.assertTrue(all(x[0] == "GET" for x in c.calls))

    def test_incomplete_trees_definitely_unsent(self):
        for value in [
            {"sha": TREE, "truncated": True, "tree": []},
            {"sha": TREE, "tree": []},
            {"sha": TREE, "truncated": False, "tree": [{}]},
        ]:
            replies = self.replies()
            replies[3] = value
            c = Client(*replies)
            with self.assertRaises(PreReadUnavailable):
                self.handle(c).run("wf_file_rename", self.args())
            self.assertTrue(all(x[0] == "GET" for x in c.calls))

    def test_nested_paths_bounded_and_mode_preserved(self):
        replies = self.replies()
        replies[3] = {
            "sha": TREE,
            "truncated": False,
            "tree": [{"path": "src", "type": "tree", "mode": "040000", "sha": "f" * 40}],
        }
        replies.insert(
            4,
            {
                "sha": "f" * 40,
                "truncated": False,
                "tree": [{"path": "old.txt", "type": "blob", "mode": "100755", "sha": BLOB}],
            },
        )
        c = Client(*replies)
        out = self.handle(c).run(
            "wf_file_rename",
            self.args(source_path="src/old.txt", destination_path="new/deeper/file.txt"),
        )
        self.assertEqual(out["commit"], NEW)
        self.assertEqual(c.calls[5][2]["tree"][1]["mode"], "100755")

    def test_malformed_write_receipts_unknown(self):
        for index in [4, 5, 6]:
            replies = self.replies()
            replies[index] = {}
            c = Client(*replies)
            with self.assertRaises(RelayGitHubError) as err:
                self.handle(c).run("wf_file_rename", self.args())
            self.assertEqual(err.exception.status, 502)

    def test_real_relay_unknown_restart_reworded_no_second_dispatch(self):
        for fail_at in ["POST", "PATCH"]:

            class Lost(Client):
                def _call(inner, method, path, payload=None, bearer=None):
                    if method == fail_at:
                        inner.calls.append((method, path, payload, bearer))
                        raise RelayGitHubError(422, "post-side-effect refusal")
                    return super()._call(method, path, payload, bearer)

            client = Lost(*self.replies())
            a = self.args(repo="example/x")
            reword = {**a, "message": "Different wording"}
            self.assertEqual(
                key_for("swu-wise", "wf_file_rename", a),
                key_for("swu-wise", "wf_file_rename", reword),
            )
            with tempfile.TemporaryDirectory() as tmp:
                r = relay(tmp, client=client)
                store = tmp + "/outcomes.json"
                r.outcomes = OutcomeStore(store)
                with patch.object(r.policy, "authorize", return_value=None):
                    status, body = r.handle(TOKEN, "wf_file_rename", a)
                    self.assertEqual(status, 502)
                    self.assertEqual(body["outcome"], "unknown")
                    self.assertFalse(body["retryable"])
                    count = len(client.calls)
                    r.outcomes = OutcomeStore(store)
                    status, body = r.handle(TOKEN, "wf_file_rename", reword)
                    self.assertEqual(status, 409)
                    self.assertEqual(len(client.calls), count)
