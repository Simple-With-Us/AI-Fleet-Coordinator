import unittest
from unittest.mock import patch

from git_relay import same_path_reuse
from git_relay.github_client import RelayGitHubError
from git_relay.same_path_reuse import verify_same_path
from git_relay.server import PreReadUnavailable
from git_relay.tests.test_workflow_verbs import REPO, SHA, Client, Policy
from git_relay.workflow_verbs import Refused, WorkflowVerbs

COMMIT, ROOT, DIR, BLOB = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def receipts(size=12):
    return [
        {"sha": COMMIT, "tree": {"sha": ROOT}},
        {
            "sha": ROOT,
            "truncated": False,
            "tree": [{"path": "dir", "type": "tree", "mode": "040000", "sha": DIR}],
        },
        {
            "sha": DIR,
            "truncated": False,
            "tree": [
                {"path": "file.txt", "type": "blob", "mode": "100644", "sha": BLOB, "size": size}
            ],
        },
    ]


class ReuseTests(unittest.TestCase):
    def setUp(self):
        records = {(REPO, COMMIT, "dir/file.txt", BLOB, "100644")}
        records.update((REPO, COMMIT, "file-%s.txt" % n, BLOB, "100644") for n in range(20))
        self.records_patch = patch.object(
            same_path_reuse, "REVIEWED_SAME_PATH_SOURCES", frozenset(records)
        )
        self.records_patch.start()
        self.addCleanup(self.records_patch.stop)

    def run_proof(self, rows=None, **kwargs):
        c = Client(*(rows if rows is not None else receipts()))
        result = verify_same_path(
            lambda path: c._call("GET", path),
            REPO,
            kwargs.get("commit", COMMIT),
            kwargs.get("path", "dir/file.txt"),
            kwargs.get("blob", BLOB),
            kwargs.get("mode", "100644"),
            kwargs.get("max_bytes", 20),
            {},
        )
        return result, c

    def test_metadata_only_same_repository_same_path(self):
        size, c = self.run_proof()
        self.assertEqual(size, 12)
        self.assertEqual(
            [x[1] for x in c.calls],
            [
                f"/repos/{REPO}/git/commits/{COMMIT}",
                f"/repos/{REPO}/git/trees/{ROOT}",
                f"/repos/{REPO}/git/trees/{DIR}",
            ],
        )
        self.assertTrue(all("/blobs/" not in x[1] and "/contents/" not in x[1] for x in c.calls))

    def test_every_blob_identity_mode_type_size_and_ancestor_is_bound(self):
        for change in [
            {"sha": "e" * 40},
            {"type": "commit"},
            {"mode": "120000"},
            {"mode": "100755"},
            {"size": True},
            {"size": -1},
            {"size": 21},
            {"size": None},
        ]:
            rows = receipts()
            rows[2]["tree"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.run_proof(rows)
        rows = receipts()
        rows[1]["tree"][0]["type"] = "blob"
        with self.assertRaises(ValueError):
            self.run_proof(rows)

    def test_commit_tree_and_path_cannot_be_substituted(self):
        for index, key, value in [
            (0, "sha", "e" * 40),
            (1, "sha", "e" * 40),
            (2, "sha", "e" * 40),
            (1, "truncated", True),
            (2, "truncated", None),
        ]:
            rows = receipts()
            rows[index][key] = value
            with self.subTest(index=index, key=key), self.assertRaises(ValueError):
                self.run_proof(rows)
        for kwargs in [
            {"path": "dir/else.txt"},
            {"path": "different/file.txt"},
            {"commit": "main"},
            {"blob": "z" * 40},
            {"path": "../file.txt"},
            {"path": "a/" * 9 + "file"},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_proof(**kwargs)

    def test_ambiguous_duplicate_and_unbounded_tree_refused(self):
        for rows in [receipts(), receipts(), receipts()]:
            if len(rows[1]["tree"]) == 1:
                rows[1]["tree"] *= 2
            with self.assertRaises(ValueError):
                self.run_proof(rows)
        rows = receipts()
        rows[1]["tree"] = [{"path": str(n)} for n in range(101)]
        with self.assertRaises(ValueError):
            self.run_proof(rows)

    def test_metadata_cached_without_caching_destination_approval(self):
        c = Client(*receipts())
        cache = {}
        get = lambda path: c._call("GET", path)
        for _ in range(2):
            self.assertEqual(
                verify_same_path(get, REPO, COMMIT, "dir/file.txt", BLOB, "100644", 20, cache), 12
            )
        self.assertEqual(len(c.calls), 3)
        with self.assertRaises(ValueError):
            verify_same_path(get, REPO, COMMIT, "dir/file.txt", "e" * 40, "100644", 20, cache)


class ReuseWorkflowTests(unittest.TestCase):
    def setUp(self):
        records = {(REPO, COMMIT, "dir/file.txt", BLOB, "100644")}
        records.update((REPO, COMMIT, "file-%s.txt" % n, BLOB, "100644") for n in range(20))
        self.records_patch = patch.object(
            same_path_reuse, "REVIEWED_SAME_PATH_SOURCES", frozenset(records)
        )
        self.records_patch.start()
        self.addCleanup(self.records_patch.stop)

    def args(self):
        return {
            "repo": REPO,
            "base_tree": SHA,
            "files": [
                {"path": "dir/file.txt", "sha": BLOB, "source_commit": COMMIT, "mode": "100644"}
            ],
        }

    def handle(self, client, args):
        return WorkflowVerbs(
            client, Policy(), "swu-wise", "own", "swu-wise", "wf_git_tree_create", REPO
        ).run("wf_git_tree_create", args)

    def test_fixed_tree_payload_and_no_ref_or_blob_operation(self):
        client = Client(*receipts(), {"sha": "f" * 40, "truncated": False, "tree": []})
        self.assertEqual(self.handle(client, self.args())["tree"]["files"], 1)
        self.assertEqual(
            client.calls[-1][:3],
            (
                "POST",
                f"/repos/{REPO}/git/trees",
                {
                    "base_tree": SHA,
                    "tree": [
                        {"path": "dir/file.txt", "mode": "100644", "type": "blob", "sha": BLOB}
                    ],
                },
            ),
        )

    def test_no_source_override_renaming_or_inline_ambiguity(self):
        for update in [
            {"text": "safe"},
            {"sha": None},
            {"source_path": "other"},
            {"source_commit": None},
            {"source_commit": "main"},
            {"path": ".github/workflows/file.yml"},
        ]:
            args = self.args()
            args["files"][0].update(update)
            client = Client(*receipts())
            with self.subTest(update=update), self.assertRaises(Refused):
                self.handle(client, args)
            self.assertTrue(all(x[0] == "GET" for x in client.calls))

    def test_new_different_sha_still_uses_ordinary_scan(self):
        import base64

        args = self.args()
        del args["files"][0]["source_commit"]
        raw = ("gh" + "p_" + "a" * 40).encode()
        client = Client(
            {
                "sha": BLOB,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            }
        )
        with self.assertRaises(Refused):
            self.handle(client, args)
        self.assertEqual(len(client.calls), 1)
        self.assertIn("/blobs/", client.calls[0][1])

    def test_transient_pre_read_failure_is_never_unknown_write(self):
        class Boom(Client):
            def _call(self, *args, **kwargs):
                raise RelayGitHubError(503, "unavailable")

        with self.assertRaises(PreReadUnavailable):
            self.handle(Boom(), self.args())

    def test_raw_budget_is_charged_for_every_reference(self):
        from git_relay.workflow_verbs import MAX_FILE_RAW, MAX_FILES_RAW

        size = MAX_FILE_RAW
        args = self.args()
        count = MAX_FILES_RAW // size + 1
        self.assertLessEqual(count, 20)
        # The same source path cannot be renamed; repeated path entries are refused.
        # Use distinct original paths, each proved by its own exact source entry.
        args["files"] = [
            dict(path="file-%s.txt" % n, sha=BLOB, source_commit=COMMIT, mode="100644")
            for n in range(count)
        ]
        tree = {
            "sha": ROOT,
            "truncated": False,
            "tree": [
                dict(path=f["path"], type="blob", mode="100644", sha=BLOB, size=size)
                for f in args["files"]
            ],
        }
        client = Client({"sha": COMMIT, "tree": {"sha": ROOT}}, tree)
        with self.assertRaises(Refused):
            self.handle(client, args)
        self.assertTrue(all(x[0] == "GET" for x in client.calls))
        self.assertEqual(len(client.calls), 2)
