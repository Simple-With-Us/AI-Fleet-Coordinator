"""A scanner refusal names the matched category and the field, never content.

The verb-level file scan computes the category (reviewed_fixtures returns it)
and used to discard it, raising a bare "credential-shaped content refused":
the caller could not tell an AWS-shaped fixture from a real private key, and
the publication froze undiagnosed.  These tests pin the new shape - the same
one the pre-dispatch scan (SecretInputError) already used - and prove the
matched fixture text appears nowhere in the refusal.
"""

import base64
import hashlib
import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Multi
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Multi
from git_relay.secret_scan import category_id, scan_refusal
from git_relay.workflow_verbs import Refused

# Obviously synthetic fixtures, one per scanner category exercised below.
AWS_FIXTURE = "AKIAIOSFODNN7EXAMPLE"  # AWS's published example access key id
GITHUB_FIXTURE = "ghp_" + "A" * 36
BEARER_FIXTURE = "Bearer " + "synthetic" * 3
JWT_FIXTURE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepart"
SLACK_FIXTURE = "xoxb-1234567890ab"
ASSIGNED_FIXTURE = "api_key = 'syntheticvalue123'"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class CategoryId(unittest.TestCase):
    def test_pattern_names_are_already_identifiers(self):
        for name in ("github_token", "aws_access_key_id", "jwt", "bearer_credential"):
            self.assertEqual(category_id(name), name)

    def test_patch_scanner_labels_map_to_identifiers(self):
        self.assertEqual(category_id("AWS access key"), "aws_access_key_id")
        self.assertEqual(category_id("assigned secret"), "assigned_secret")
        self.assertEqual(category_id("access token (JWT)"), "jwt")
        self.assertEqual(category_id("a loaded credential"), "loaded_credential")
        self.assertEqual(category_id("a Zulip-shaped API key"), "zulip_api_key")

    def test_unmapped_categories_collapse_to_the_generic_id(self):
        self.assertEqual(category_id("some future scanner prose"), "credential")
        self.assertEqual(category_id(None), "credential")

    def test_scan_refusal_shape(self):
        self.assertEqual(
            scan_refusal("args.files[2].content_b64", "AWS access key"),
            "args.files[2].content_b64 looks like it contains a credential "
            "(aws_access_key_id); refusing to relay it",
        )


class FilesPutCategory(unittest.TestCase):
    def run_put(self, files, message="m"):
        c = Client()
        with self.assertRaises(Refused) as caught:
            Multi(c).run(
                "wf_files_put",
                {
                    "repo": REPO,
                    "branch": "feat/x",
                    "message": message,
                    "expected_head_sha": SHA,
                    "files": files,
                },
            )
        # The scan fires in the plan loop, before any upstream call.
        self.assertEqual(c.calls, [])
        return str(caught.exception)

    def test_third_file_names_its_index_and_category(self):
        message = self.run_put(
            [
                {"path": "a.txt", "content_b64": b64(b"clean")},
                {"path": "b.txt", "content_b64": b64(b"also clean")},
                {"path": "c.txt", "content_b64": b64(("key " + AWS_FIXTURE).encode())},
            ]
        )
        self.assertEqual(
            message,
            "args.files[2].content_b64 looks like it contains a credential "
            "(aws_access_key_id); refusing to relay it",
        )
        self.assertNotIn(AWS_FIXTURE, message)

    def test_patch_scanner_label_is_normalized(self):
        message = self.run_put(
            [{"path": "settings.py", "content_b64": b64(ASSIGNED_FIXTURE.encode())}]
        )
        self.assertEqual(
            message,
            "args.files[0].content_b64 looks like it contains a credential "
            "(assigned_secret); refusing to relay it",
        )
        self.assertNotIn("syntheticvalue123", message)

    def test_message_category(self):
        message = self.run_put(
            [{"path": "a.txt", "content_b64": b64(b"clean")}],
            message="see " + BEARER_FIXTURE,
        )
        self.assertEqual(
            message,
            "args.message looks like it contains a credential "
            "(bearer_credential); refusing to relay it",
        )
        self.assertNotIn("synthetic", message)


class FilePutCategory(unittest.TestCase):
    def run_put(self, content: bytes, message="m"):
        c = Client()
        with self.assertRaises(Refused) as caught:
            Multi(c).run(
                "wf_file_put",
                {
                    "repo": REPO,
                    "path": "docs/a.md",
                    "branch": "wise/x",
                    "message": message,
                    "content_b64": b64(content),
                },
            )
        self.assertEqual(c.calls, [])
        return str(caught.exception)

    def test_content_category(self):
        message = self.run_put(("token " + GITHUB_FIXTURE).encode())
        self.assertEqual(
            message,
            "args.content_b64 looks like it contains a credential "
            "(github_token); refusing to relay it",
        )
        self.assertNotIn(GITHUB_FIXTURE, message)

    def test_message_category(self):
        message = self.run_put(b"clean", message="auth " + JWT_FIXTURE)
        self.assertEqual(
            message,
            "args.message looks like it contains a credential (jwt); "
            "refusing to relay it",
        )
        self.assertNotIn(JWT_FIXTURE, message)


class TreeCreateCategory(unittest.TestCase):
    def test_text_entry_category(self):
        c = Client()
        with self.assertRaises(Refused) as caught:
            Multi(c).run(
                "wf_git_tree_create",
                {
                    "repo": REPO,
                    "base_tree": SHA,
                    "files": [
                        {"path": "a.txt", "text": "clean", "mode": "100644"},
                        {"path": "b.txt", "text": "tok " + SLACK_FIXTURE, "mode": "100644"},
                    ],
                },
            )
        message = str(caught.exception)
        self.assertEqual(
            message,
            "args.files[1].text looks like it contains a credential "
            "(slack_token); refusing to relay it",
        )
        self.assertNotIn(SLACK_FIXTURE, message)


class FilePatchCategory(unittest.TestCase):
    def test_base_finding_names_path_and_category(self):
        raw = ("key = '" + GITHUB_FIXTURE + "'\n").encode()
        blob_sha = hashlib.sha1(
            b"blob " + str(len(raw)).encode() + b"\0" + raw
        ).hexdigest()
        args = {
            "repo": REPO,
            "branch": "feature/x",
            "path": "server/index.ts",
            "message": "fix",
            "expected_head_sha": SHA,
            "base_blob_sha": blob_sha,
            "edits": [dict(start=0, end=3, text="key")],
        }
        replies = [
            dict(default_branch="main"),
            dict(ref="refs/heads/feature/x", object=dict(sha=SHA)),
            dict(sha=SHA, tree=dict(sha="b" * 40)),
            dict(
                type="file",
                path=args["path"],
                sha=blob_sha,
                encoding="base64",
                content=b64(raw),
            ),
        ]
        c = Client(*replies)
        with self.assertRaises(Refused) as caught:
            Multi(c).run("wf_file_patch", args)
        message = str(caught.exception)
        self.assertEqual(
            message,
            "server/index.ts looks like it contains a credential "
            "(github_token); refusing to relay it",
        )
        self.assertNotIn(GITHUB_FIXTURE, message)
        self.assertTrue(all(m == "GET" for m, *_ in c.calls))


if __name__ == "__main__":
    unittest.main()
