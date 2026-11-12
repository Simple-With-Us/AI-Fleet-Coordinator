"""Default-branch refusal cannot be walked around with a dot segment, a case change or a qualified name (AFC-775).

GitHub drops "." segments from a URL path, so "./main" reaches the branch "main".  The ref
validator refuses those spellings, and every verb that refuses the default branch decides with
one shared helper and, where the verb already reads the ref, from the ref the provider returned.
"""

import unittest

try:
    from . import test_file_patch, test_file_rename, test_workflow_verbs
    from .test_workflow_verbs import REPO, SHA, Client, Multi, Policy
except ImportError:
    import test_file_patch
    import test_file_rename
    import test_workflow_verbs
    from test_workflow_verbs import REPO, SHA, Client, Multi, Policy

from git_relay.github_client import RelayGitHubError
from git_relay.server import PreReadUnavailable
from git_relay.workflow_verbs import (
    Refused,
    WorkflowVerbs,
    _is_default_branch,
    _ref,
)

DOT_SPELLINGS = ["./main", "main/.", ".//main", "main/./", "./", ".", "..", "a/../main", ".hidden"]
DEFAULT_SPELLINGS = [
    "refs/heads/main",
    "heads/main",
    "main.",
    "main.lock",
    "MAIN",
    "Main",
    "refs/heads/Main",
]
BAD_NAMES = DOT_SPELLINGS + DEFAULT_SPELLINGS + ["feature/x.", "feature/.x", "x//y", "feature/y.lock/z.lock"]
GOOD_NAMES = ["main", "claude/fix-thing", "release/v1.2.3", "feature/a.b", "a-b_c", "x/y/z"]


class Raising(Client):
    """Client whose queued exceptions are raised, as the real client raises them."""

    def _call(self, *args, **kwargs):
        value = super()._call(*args, **kwargs)
        if isinstance(value, BaseException):
            raise value
        return value


def handle(action, *replies, policy=None):
    c = Raising(*replies)
    return WorkflowVerbs(c, policy or Policy(), "swu-wise", "own", "swu-wise", action, REPO), c


def only_reads(test, client):
    test.assertTrue(all(call[0] == "GET" for call in client.calls), client.calls)


class RefValidator(unittest.TestCase):
    def test_dot_empty_and_lock_segments_refused(self):
        for name in BAD_NAMES:
            if name in ("MAIN", "Main", "heads/main", "refs/heads/main", "refs/heads/Main"):
                continue
            with self.subTest(name=name), self.assertRaises(Refused):
                _ref({"branch": name}, "branch")

    def test_legitimate_names_stay_accepted(self):
        for name in GOOD_NAMES + ["MAIN", "refs/heads/main", "v1.2.3", "HEAD"]:
            with self.subTest(name=name):
                self.assertEqual(_ref({"branch": name}, "branch"), name)

    def test_default_branch_helper(self):
        for name in ["main", "MAIN", "Main", "refs/heads/main", "heads/main", "./main", "main/."]:
            with self.subTest(name=name):
                self.assertTrue(_is_default_branch(name, "main"))
        self.assertTrue(_is_default_branch("refs/heads/Main", "main"))
        self.assertTrue(_is_default_branch("feature/x", ""))
        self.assertTrue(_is_default_branch("feature/x", None))
        self.assertTrue(_is_default_branch(None, "main"))
        self.assertTrue(_is_default_branch("", "main"))
        for name in ["feature/main", "main/x", "maint", "claude/fix-thing", "release/v1.2.3"]:
            with self.subTest(name=name):
                self.assertFalse(_is_default_branch(name, "main"))


class BranchUpdateDelete(unittest.TestCase):
    def args(self, action, branch):
        args = {"repo": REPO, "branch": branch, "expected_sha": SHA}
        if action == "wf_branch_update":
            args["sha"] = "b" * 40
        return args

    def test_spellings_of_the_default_branch_refused_before_any_write(self):
        for action in ("wf_branch_update", "wf_branch_delete"):
            for name in BAD_NAMES:
                h, c = handle(action, {"default_branch": "main"}, {"ref": "refs/heads/main"})
                with self.subTest(action=action, name=name), self.assertRaises(Refused):
                    h.run(action, self.args(action, name))
                only_reads(self, c)

    def test_provider_ref_that_is_the_default_branch_refused(self):
        for action in ("wf_branch_update", "wf_branch_delete"):
            for ref in ("refs/heads/main", "refs/heads/Main", "refs/heads/./main"):
                h, c = handle(
                    action,
                    {"default_branch": "main"},
                    {"ref": ref, "object": {"sha": SHA}},
                )
                with self.subTest(action=action, ref=ref), self.assertRaisesRegex(
                    Refused, "default branch mutation refused"
                ):
                    h.run(action, self.args(action, "feature/x"))
                only_reads(self, c)

    def test_provider_answer_without_a_ref_refused(self):
        for action in ("wf_branch_update", "wf_branch_delete"):
            for answer in (
                {"object": {"sha": SHA}},
                {"ref": None, "object": {"sha": SHA}},
                {"ref": 7, "object": {"sha": SHA}},
                {"ref": "refs/tags/feature/x", "object": {"sha": SHA}},
                {"ref": "refs/heads/", "object": {"sha": SHA}},
                {"ref": "refs/heads/other", "object": {"sha": SHA}},
            ):
                h, c = handle(action, {"default_branch": "main"}, answer)
                with self.subTest(action=action, answer=answer), self.assertRaises(Refused):
                    h.run(action, self.args(action, "feature/x"))
                only_reads(self, c)

    def test_unknown_default_branch_refused(self):
        for action in ("wf_branch_update", "wf_branch_delete"):
            for repository in ({}, {"default_branch": ""}, {"default_branch": 3}):
                h, c = handle(action, repository)
                with self.subTest(action=action, repository=repository), self.assertRaises(Refused):
                    h.run(action, self.args(action, "feature/x"))
                only_reads(self, c)

    def test_legitimate_branches_still_allowed(self):
        for action, method in (("wf_branch_update", "PATCH"), ("wf_branch_delete", "DELETE")):
            for name in GOOD_NAMES[1:]:
                receipt = {"ref": "refs/heads/" + name, "object": {"sha": "b" * 40}}
                h, c = handle(
                    action,
                    {"default_branch": "main"},
                    {"ref": "refs/heads/" + name, "object": {"sha": SHA}},
                    receipt,
                )
                with self.subTest(action=action, name=name):
                    h.run(action, self.args(action, name))
                    self.assertEqual(c.calls[-1][0], method)


class FileVerbs(unittest.TestCase):
    def test_file_put_refuses_every_spelling_with_no_write(self):
        for name in BAD_NAMES:
            h, c = handle("wf_file_put", {"default_branch": "main"})
            args = {
                "repo": REPO,
                "path": "docs/a.txt",
                "branch": name,
                "message": "Add a file",
                "content_b64": "aGk=",
            }
            with self.subTest(name=name), self.assertRaises(Refused):
                h.run("wf_file_put", args)
            only_reads(self, c)

    def test_file_delete_refuses_every_spelling_with_no_write(self):
        for name in BAD_NAMES:
            h, c = handle(
                "wf_file_delete",
                {"default_branch": "main"},
                {"name": "main", "commit": {"sha": SHA}},
            )
            args = {
                "repo": REPO,
                "path": "docs/a.txt",
                "branch": name,
                "message": "Remove a file",
                "prior_sha": SHA,
            }
            with self.subTest(name=name), self.assertRaises((Refused, RelayGitHubError)):
                h.run("wf_file_delete", args)
            only_reads(self, c)

    def test_file_put_provider_name_decides_before_any_write(self):
        args = {
            "repo": REPO,
            "path": "docs/a.txt",
            "branch": "feature/x",
            "message": "Add a file",
            "content_b64": "aGk=",
        }
        # The provider calls the branch the default branch: refused, with the reason.
        for provider in ("main", "MAIN", "refs/heads/main", "./main"):
            h, c = handle("wf_file_put", {"default_branch": "main"}, {"name": provider})
            with self.subTest(provider=provider), self.assertRaisesRegex(
                Refused, "default branch is not writable"
            ):
                h.run("wf_file_put", args)
            only_reads(self, c)
        # The provider's answer names nothing, or another branch: fail closed.
        for answer in ({}, {"name": ""}, {"name": 7}, {"name": "other"}, ["feature/x"]):
            h, c = handle("wf_file_put", {"default_branch": "main"}, answer)
            with self.subTest(answer=answer), self.assertRaises(RelayGitHubError):
                h.run("wf_file_put", args)
            only_reads(self, c)
        # A name the provider does not know (HEAD, a tag) is refused before any write.
        h, c = handle("wf_file_put", {"default_branch": "main"}, RelayGitHubError(404, "nf"))
        with self.assertRaises(RelayGitHubError):
            h.run("wf_file_put", dict(args, branch="HEAD"))
        only_reads(self, c)

    def test_file_put_on_a_legitimate_branch_still_allowed(self):
        h, c = handle(
            "wf_file_put",
            {"default_branch": "main"},
            {"name": "feature/x"},
            {"commit": {"sha": "b" * 40}, "content": {"path": "docs/a.txt"}},
        )
        out = h.run(
            "wf_file_put",
            {
                "repo": REPO,
                "path": "docs/a.txt",
                "branch": "feature/x",
                "message": "Add a file",
                "content_b64": "aGk=",
            },
        )
        self.assertEqual(out["branch"], "feature/x")
        self.assertEqual(c.calls[-1][0], "PUT")

    def test_file_delete_provider_name_mismatch_refused_with_no_write(self):
        args = {
            "repo": REPO,
            "path": "docs/a.txt",
            "branch": "feature/x",
            "message": "Remove a file",
            "prior_sha": SHA,
        }
        for provider in ("main", "MAIN", "other"):
            h, c = handle(
                "wf_file_delete",
                {"default_branch": "main"},
                {"name": provider, "commit": {"sha": SHA}},
            )
            with self.subTest(provider=provider), self.assertRaisesRegex(
                PreReadUnavailable, "no write attempted"
            ):
                h.run("wf_file_delete", args)
            only_reads(self, c)

    def test_file_delete_on_a_legitimate_branch_still_allowed(self):
        h, c = handle(
            "wf_file_delete",
            {"default_branch": "main"},
            {"name": "feature/x", "commit": {"sha": SHA}},
            {"content": None, "commit": {"sha": "b" * 40}},
        )
        h.run(
            "wf_file_delete",
            {
                "repo": REPO,
                "path": "docs/a.txt",
                "branch": "feature/x",
                "message": "Remove a file",
                "prior_sha": SHA,
            },
        )
        self.assertEqual(c.calls[-1][0], "DELETE")

    def test_file_rename_refuses_every_spelling_with_no_write(self):
        for name in BAD_NAMES:
            h, c = handle(
                "wf_file_rename",
                {"default_branch": "main"},
                {"ref": "refs/heads/main", "object": {"type": "commit", "sha": SHA}},
            )
            args = {
                "repo": REPO,
                "source_path": "old.txt",
                "destination_path": "new.txt",
                "branch": name,
                "message": "Rename file",
                "expected_head_sha": SHA,
                "prior_sha": "e" * 40,
            }
            with self.subTest(name=name), self.assertRaises(Refused):
                h.run("wf_file_rename", args)
            only_reads(self, c)

    def test_file_rename_provider_ref_that_is_the_default_branch_refused(self):
        h, c = handle(
            "wf_file_rename",
            {"default_branch": "main"},
            {"ref": "refs/heads/main", "object": {"type": "commit", "sha": SHA}},
        )
        args = {
            "repo": REPO,
            "source_path": "old.txt",
            "destination_path": "new.txt",
            "branch": "feature/x",
            "message": "Rename file",
            "expected_head_sha": SHA,
            "prior_sha": "e" * 40,
        }
        with self.assertRaisesRegex(Refused, "default branch is not writable"):
            h.run("wf_file_rename", args)
        only_reads(self, c)

    def test_file_rename_on_a_legitimate_branch_still_allowed(self):
        rename = test_file_rename.Rename()
        c = Client(*rename.replies())
        out = rename.handle(c).run("wf_file_rename", rename.args())
        self.assertEqual(out["branch"], "feature/x")
        self.assertEqual(c.calls[-1][0], "PATCH")

    def test_file_patch_legitimate_branch_still_allowed(self):
        args, replies = test_file_patch.PatchTests().setup_case()
        c = Client(*replies)
        out = Multi(c).run("wf_file_patch", args)
        self.assertEqual(out["branch"], "feature/x")
        self.assertEqual([m for m, *_ in c.calls].count("POST"), 1)

    def test_files_put_legitimate_branch_still_allowed(self):
        c = test_workflow_verbs.Tests()._git_data_client()
        out = Multi(c).run(
            "wf_files_put",
            {
                "repo": REPO,
                "branch": "feat/x",
                "message": "m",
                "expected_head_sha": SHA,
                "files": [{"path": "docs/new.md", "content_b64": "eA=="}],
            },
        )
        self.assertEqual(out["branch"], "feat/x")

    def test_file_patch_refuses_every_spelling_and_provider_mismatch(self):
        def args(branch):
            return {
                "repo": REPO,
                "branch": branch,
                "path": "docs/a.txt",
                "message": "Patch",
                "expected_head_sha": SHA,
                "base_blob_sha": "e" * 40,
                "edits": [{"start": 0, "end": 1, "text": "x"}],
            }

        for name in BAD_NAMES:
            h, c = handle("wf_file_patch", {"default_branch": "main"}, {"ref": "refs/heads/main"})
            with self.subTest(name=name), self.assertRaises(Refused):
                h.run("wf_file_patch", args(name))
            only_reads(self, c)
        for answer in (
            {"ref": "refs/heads/main", "object": {"sha": SHA}},
            {"ref": "refs/heads/MAIN", "object": {"sha": SHA}},
            {"object": {"sha": SHA}},
        ):
            h, c = handle("wf_file_patch", {"default_branch": "main"}, answer)
            with self.subTest(answer=answer), self.assertRaises(Refused):
                h.run("wf_file_patch", args("feature/x"))
            only_reads(self, c)

    def test_files_put_refuses_every_spelling_and_provider_mismatch(self):
        def args(branch):
            return {
                "repo": REPO,
                "branch": branch,
                "message": "Add files",
                "expected_head_sha": SHA,
                "files": [{"path": "docs/a.txt", "content_b64": "aGk="}],
            }

        for name in BAD_NAMES:
            h, c = handle("wf_files_put", {"default_branch": "main"}, {"ref": "refs/heads/main"})
            with self.subTest(name=name), self.assertRaises(Refused):
                h.run("wf_files_put", args(name))
            only_reads(self, c)
        for answer in (
            {"ref": "refs/heads/main", "object": {"sha": SHA}},
            {"ref": "refs/heads/Main", "object": {"sha": SHA}},
            {"object": {"sha": SHA}},
        ):
            h, c = handle("wf_files_put", {"default_branch": "main"}, answer)
            with self.subTest(answer=answer), self.assertRaises(Refused):
                h.run("wf_files_put", args("feature/x"))
            only_reads(self, c)


class OtherVerbs(unittest.TestCase):
    def test_branch_create_and_tags_reject_dot_segment_names(self):
        for action, key in (("wf_branch_create", "branch"), ("wf_tag_create", "tag")):
            for name in DOT_SPELLINGS + ["main.", "main.lock"]:
                h, c = handle(action)
                with self.subTest(action=action, name=name), self.assertRaises(Refused):
                    h.run(action, {"repo": REPO, key: name, "sha": SHA})
                self.assertEqual(c.calls, [])

    def test_reads_that_take_a_ref_reject_dot_segment_names(self):
        for action, key in (("wf_branch_get", "branch"), ("wf_tag_get", "tag")):
            for name in DOT_SPELLINGS:
                h, c = handle(action)
                with self.subTest(action=action, name=name), self.assertRaises(Refused):
                    h.run(action, {"repo": REPO, key: name})
                self.assertEqual(c.calls, [])


if __name__ == "__main__":
    unittest.main()
