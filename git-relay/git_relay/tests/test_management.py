import unittest

from test_workflow_verbs import REPO, Client, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Management(unittest.TestCase):
    def test_valid_shapes_and_fixed_paths(self):
        cases = [
            (
                "wf_labels_set",
                {"number": 3, "labels": ["bug"]},
                "PUT",
                "issues/3/labels",
                [],
                {"labels": ["bug"]},
            ),
            (
                "wf_reviewers_request",
                {"number": 3, "reviewers": ["swu-wise"]},
                "POST",
                "pulls/3/requested_reviewers",
                {"requested_reviewers": []},
                {"reviewers": ["swu-wise"]},
            ),
            (
                "wf_reviewers_remove",
                {"number": 3, "reviewers": ["swu-wise"]},
                "DELETE",
                "pulls/3/requested_reviewers",
                {"requested_reviewers": []},
                {"reviewers": ["swu-wise"]},
            ),
            (
                "wf_release_create",
                {
                    "tag": "v1",
                    "name": "Release",
                    "body": "Notes",
                    "draft": True,
                    "prerelease": False,
                },
                "POST",
                "releases",
                {"id": 1},
                {
                    "tag_name": "v1",
                    "name": "Release",
                    "body": "Notes",
                    "draft": True,
                    "prerelease": False,
                },
            ),
            (
                "wf_release_update",
                {
                    "release_id": 2,
                    "name": "Release",
                    "body": "Notes",
                    "draft": False,
                    "prerelease": True,
                },
                "PATCH",
                "releases/2",
                {"id": 2},
                {"name": "Release", "body": "Notes", "draft": False, "prerelease": True},
            ),
        ]
        for action, args, method, path, reply, payload in cases:
            client = Client(reply)
            h = WorkflowVerbs(client, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            h.run(action, {"repo": REPO, **args})
            self.assertEqual(
                client.calls, [(method, "/repos/" + REPO + "/" + path, payload, "private")]
            )
            client = Client()
            h = WorkflowVerbs(client, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            with self.assertRaises(Refused):
                h.run(action, {"repo": REPO, **args, "url": "https://evil.invalid"})
            self.assertFalse(client.calls)

    def test_malformed_inputs_refuse_before_network(self):
        for args in [
            {"number": 1, "labels": []},
            {"number": 1, "labels": ["\nattack"]},
            {"number": True, "labels": ["bug"]},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_labels_set", {"repo": REPO, **args}, REPO)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape(
                "wf_release_create",
                {
                    "repo": REPO,
                    "tag": "v1",
                    "name": "n",
                    "body": "b",
                    "draft": "yes",
                    "prerelease": False,
                },
                REPO,
            )
