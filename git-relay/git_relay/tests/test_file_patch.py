import base64
import hashlib
import unittest

from git_relay.tests.test_workflow_verbs import REPO, SHA, Client, Multi
from git_relay.workflow_verbs import Refused, WriteOutcomeUnknown


def blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


class PatchTests(unittest.TestCase):
    def setup_case(self):
        raw = b"const apiKey = " + b"stsProviderCredential(provider);\n" + b"other = old;\n"
        result = raw.replace(b"other = old", b"other = new")
        args = dict(
            repo=REPO,
            branch="feature/x",
            path="server/index.ts",
            message="fix",
            expected_head_sha=SHA,
            base_blob_sha=blob(raw),
            edits=[dict(start=raw.index(b"old"), end=raw.index(b"old") + 3, text="new")],
        )
        replies = [
            dict(default_branch="main"),
            dict(ref="refs/heads/feature/x", object=dict(sha=SHA)),
            dict(sha=SHA, tree=dict(sha="b" * 40)),
            dict(
                type="file",
                path=args["path"],
                sha=blob(raw),
                encoding="base64",
                content=base64.b64encode(raw).decode(),
            ),
            dict(tree=[dict(path="server", type="tree", sha="c" * 40)]),
            dict(tree=[dict(path="index.ts", type="blob", mode="100644", sha=blob(raw))]),
            dict(
                data=dict(
                    createCommitOnBranch=dict(
                        commit=dict(oid="e" * 40, parents=dict(nodes=[dict(oid=SHA)])),
                        ref=dict(name="feature/x", target=dict(oid="e" * 40)),
                    )
                )
            ),
            dict(object=dict(sha="e" * 40)),
            dict(type="file", path=args["path"], sha=blob(result)),
        ]
        return args, replies

    def test_happy_and_nonforce_parent_pin(self):
        args, replies = self.setup_case()
        c = Client(*replies)
        out = Multi(c).run("wf_file_patch", args)
        self.assertEqual(out, dict(branch="feature/x", path="server/index.ts", commit="e" * 40))
        writes = [(m, p, b) for m, p, b, _ in c.calls if m != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1], "/graphql")
        payload = writes[0][2]
        self.assertEqual(payload["variables"]["input"]["expectedHeadOid"], SHA)
        self.assertEqual(payload["variables"]["input"]["branch"]["repositoryNameWithOwner"], REPO)
        self.assertEqual(
            payload["variables"]["input"]["fileChanges"]["additions"][0]["path"],
            args["path"],
        )

    def test_large_base_falls_back_to_git_blob(self):
        args, replies = self.setup_case()
        raw = b"const apiKey = " + b"stsProviderCredential(provider);\n" + b"other = old;\n"
        replies[3] = dict(
            type="file", path=args["path"], sha=blob(raw), encoding="none", content=None
        )
        replies.insert(
            4,
            dict(sha=blob(raw), encoding="base64", content=base64.b64encode(raw).decode()),
        )
        c = Client(*replies)
        out = Multi(c).run("wf_file_patch", args)
        self.assertEqual(out["commit"], "e" * 40)
        blob_gets = [p for m, p, *_ in c.calls if m == "GET" and "/git/blobs/" in p]
        self.assertEqual(blob_gets, [f"/repos/{REPO}/git/blobs/{blob(raw)}"])

    def test_transient_pre_read_is_retryable_unavailable(self):
        from git_relay.github_client import RelayGitHubError
        from git_relay.server import PreReadUnavailable

        class RaisingClient(Client):
            def _call(self, *a, **kw):
                reply = super()._call(*a, **kw)
                if isinstance(reply, Exception):
                    raise reply
                return reply

        args, replies = self.setup_case()
        raw = b"const apiKey = " + b"stsProviderCredential(provider);\n" + b"other = old;\n"
        replies[3] = dict(
            type="file", path=args["path"], sha=blob(raw), encoding="none", content=None
        )
        replies.insert(
            4,
            dict(sha=blob(raw), encoding="base64", content=base64.b64encode(raw).decode()),
        )
        # 0 repo, 1 ref, 2 commit, 3 contents, 4 blob fallback, 5/6 entry-mode trees:
        # a transient failure on any pre-read is a retryable PreReadUnavailable and
        # no write is attempted.
        for at in range(7):
            for status in (0, 408, 500):
                test_replies = list(replies)
                test_replies[at] = RelayGitHubError(status, "transient")
                c = RaisingClient(*test_replies)
                with self.subTest(at=at, status=status), self.assertRaises(PreReadUnavailable):
                    Multi(c).run("wf_file_patch", args)
                self.assertTrue(all(m == "GET" for m, *_ in c.calls))

        # A terminal refusal stays a plain RelayGitHubError, never an availability signal.
        term_replies = list(replies)
        term_replies[4] = RelayGitHubError(403, "refused")
        c = RaisingClient(*term_replies)
        with self.assertRaises(RelayGitHubError):
            Multi(c).run("wf_file_patch", args)
        self.assertTrue(all(m == "GET" for m, *_ in c.calls))

    def test_blob_fallback_mismatch_refuses_before_write(self):
        args, replies = self.setup_case()
        raw = b"const apiKey = " + b"stsProviderCredential(provider);\n" + b"other = old;\n"
        replies[3] = dict(
            type="file", path=args["path"], sha=blob(raw), encoding="none", content=None
        )
        replies.insert(4, dict(sha="f" * 40, encoding="base64", content="eA=="))
        c = Client(*replies)
        with self.assertRaises(Refused):
            Multi(c).run("wf_file_patch", args)
        self.assertTrue(all(m == "GET" for m, *_ in c.calls))

    def test_pins_fail_before_write(self):
        for at, change in [
            (0, dict(default_branch="feature/x")),
            (1, dict(object=dict(sha="f" * 40))),
            (2, dict(sha="f" * 40, tree=dict(sha="b" * 40))),
            (
                3,
                dict(
                    type="file",
                    path="other",
                    sha="f" * 40,
                    encoding="base64",
                    content="eA==",
                ),
            ),
            (
                3,
                dict(
                    type="file",
                    path="server/index.ts",
                    sha="f" * 40,
                    encoding="base64",
                    content="eA==",
                ),
            ),
        ]:
            args, replies = self.setup_case()
            replies[at] = change
            c = Client(*replies)
            with self.assertRaises(Refused):
                Multi(c).run("wf_file_patch", args)
            self.assertTrue(all(m == "GET" for m, *_ in c.calls))

    def test_new_literal_refused_before_write(self):
        args, replies = self.setup_case()
        args["edits"][0]["text"] = "token = " + "a" * 24
        c = Client(*replies)
        with self.assertRaises(Refused):
            Multi(c).run("wf_file_patch", args)
        self.assertTrue(all(m == "GET" for m, *_ in c.calls))

    def test_symlink_and_workflow_refused(self):
        args, replies = self.setup_case()
        replies[5]["tree"][0]["mode"] = "120000"
        c = Client(*replies)
        with self.assertRaises(Refused):
            Multi(c).run("wf_file_patch", args)
        self.assertTrue(all(m == "GET" for m, *_ in c.calls))
        args, replies = self.setup_case()
        args["path"] = ".github/workflows/test.yml"
        c = Client(*replies)
        with self.assertRaises(Refused):
            Multi(c).run("wf_file_patch", args)
        self.assertEqual(c.calls, [])

    def test_partial_graphql_and_bad_receipts_are_unknown(self):
        from git_relay.github_client import RelayGitHubError

        for index in (6, 7, 8):
            args, replies = self.setup_case()
            replies[index] = {}
            c = Client(*replies)
            with self.assertRaises((WriteOutcomeUnknown, RelayGitHubError)):
                Multi(c).run("wf_file_patch", args)
        args, replies = self.setup_case()
        replies[6] = {"data": {}, "errors": [{"type": "UNPROCESSABLE"}]}
        c = Client(*replies)
        with self.assertRaises(RelayGitHubError):
            Multi(c).run("wf_file_patch", args)
        self.assertEqual(len([1 for m, *_ in c.calls if m != "GET"]), 1)

    def test_executable_missing_node_id_refuses_before_write(self):
        args, replies = self.setup_case()
        replies[5]["tree"][0]["mode"] = "100755"
        c = Client(*replies)
        with self.assertRaises(Refused):
            Multi(c).run("wf_file_patch", args)
        self.assertTrue(all(m == "GET" for m, *_ in c.calls))


class ExecutablePatchTests(PatchTests):
    def executable_case(self):
        args, old = self.setup_case()
        old[0]["node_id"] = "R_repository"
        old[5]["tree"][0]["mode"] = "100755"
        raw = base64.b64decode(old[3]["content"]).replace(b"other = old", b"other = new")
        tree_sha, new = "d" * 40, "e" * 40
        tail = [
            {"sha": blob(raw)},
            {"sha": tree_sha},
            {"tree": [{"path": "server", "type": "tree", "sha": "c" * 40}]},
            {
                "tree": [
                    {
                        "path": "index.ts",
                        "type": "blob",
                        "mode": "100755",
                        "sha": blob(raw),
                    }
                ]
            },
            {"sha": new, "tree": {"sha": tree_sha}, "parents": [{"sha": SHA}]},
            {
                "type": "file",
                "path": args["path"],
                "sha": blob(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            },
            {"data": {"updateRefs": {"clientMutationId": new}}},
            {"object": {"sha": new}},
        ]
        return args, old[:6] + tail

    def test_executable_mode_and_atomic_before_oid(self):
        args, replies = self.executable_case()
        c = Client(*replies)
        out = Multi(c).run("wf_file_patch", args)
        self.assertEqual(out["commit"], "e" * 40)
        trees = [
            body
            for method, path, body, _ in c.calls
            if method == "POST" and path.endswith("/git/trees")
        ]
        self.assertEqual(trees[0]["tree"][0]["mode"], "100755")
        graph = [body for method, path, body, _ in c.calls if path == "/graphql"]
        self.assertEqual(len(graph), 1)
        update = graph[0]["variables"]["input"]["refUpdates"]
        self.assertEqual(
            update,
            [
                {
                    "name": "refs/heads/feature/x",
                    "beforeOid": SHA,
                    "afterOid": "e" * 40,
                    "force": False,
                }
            ],
        )
        self.assertFalse(any(method == "PATCH" for method, *_ in c.calls))

    def test_bad_immutable_receipts_never_publish_a_ref(self):
        for at in [6, 7, 9, 10, 11]:
            args, replies = self.executable_case()
            replies[at] = {}
            c = Client(*replies)
            with self.subTest(at=at), self.assertRaises(WriteOutcomeUnknown):
                Multi(c).run("wf_file_patch", args)
            self.assertFalse(any(path == "/graphql" for _, path, *_ in c.calls))

    def test_bad_final_ref_or_mutation_receipt_is_unknown(self):
        for at in [12, 13]:
            args, replies = self.executable_case()
            replies[at] = {"data": {"updateRefs": {}}} if at == 12 else {}
            c = Client(*replies)
            with self.subTest(at=at), self.assertRaises(WriteOutcomeUnknown):
                Multi(c).run("wf_file_patch", args)

    def test_non_string_and_invalid_published_content_is_unknown(self):
        for value in [None, [], {}, 12, "%%%"]:
            args, replies = self.executable_case()
            replies[11]["content"] = value
            c = Client(*replies)
            with self.subTest(value=value), self.assertRaises(WriteOutcomeUnknown):
                Multi(c).run("wf_file_patch", args)
            self.assertFalse(any(path == "/graphql" for _, path, *_ in c.calls))

    def test_post_write_upstream_rejections_are_unknown(self):
        from git_relay.github_client import RelayGitHubError

        for at in [6, 7, 8, 9, 10, 11, 12, 13]:
            args, replies = self.executable_case()
            replies[at] = RelayGitHubError(403, "refused")

            class RaisingClient(Client):
                def _call(self, *args, **kwargs):
                    reply = super()._call(*args, **kwargs)
                    if isinstance(reply, Exception):
                        raise reply
                    return reply

            c = RaisingClient(*replies)
            with self.subTest(at=at), self.assertRaises(WriteOutcomeUnknown):
                Multi(c).run("wf_file_patch", args)
