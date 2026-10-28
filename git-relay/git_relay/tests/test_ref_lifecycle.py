import unittest

from test_workflow_verbs import REPO, SHA, Client, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Refs(unittest.TestCase):
    def handle(self, action, replies):
        c = Client(*replies)
        return WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO), c

    def test_branch_updates_no_force_and_deletes_have_expected_preread(self):
        for action in ["wf_branch_update", "wf_branch_delete"]:
            args = {"repo": REPO, "branch": "feature/x", "expected_sha": SHA}
            if action.endswith("update"):
                args["sha"] = "b" * 40
            response = {"ref": "refs/heads/feature/x", "object": {"sha": "b" * 40}}
            h, c = self.handle(
                action, [{"default_branch": "main"}, {"object": {"sha": SHA}}, response]
            )
            h.run(action, args)
            self.assertEqual(c.calls[0][0], "GET")
            self.assertEqual(c.calls[1][0], "GET")
            self.assertIn("/git/ref/heads/feature%2Fx", c.calls[1][1])
            self.assertEqual(c.calls[2][0], "PATCH" if action.endswith("update") else "DELETE")
            if action.endswith("update"):
                self.assertEqual(c.calls[2][2], {"sha": "b" * 40, "force": False})

    def test_default_branch_and_changed_refs_refuse_before_write(self):
        for replies in [
            [{"default_branch": "main"}],
            [{"default_branch": "other"}, {"object": {"sha": "b" * 40}}],
        ]:
            h, c = self.handle("wf_branch_delete", replies)
            with self.assertRaises(Refused):
                h.run("wf_branch_delete", {"repo": REPO, "branch": "main", "expected_sha": SHA})
            self.assertTrue(all(x[0] == "GET" for x in c.calls))

    def test_tag_routes_and_bound_receipts(self):
        for action, args, replies, method in [
            (
                "wf_tag_create",
                {"tag": "v1", "sha": SHA},
                [{"ref": "refs/tags/v1", "object": {"sha": SHA}}],
                "POST",
            ),
            ("wf_tag_get", {"tag": "v1"}, [{"ref": "refs/tags/v1", "object": {"sha": SHA}}], "GET"),
            (
                "wf_tag_delete",
                {"tag": "v1", "expected_sha": SHA},
                [{"object": {"sha": SHA}}, {}],
                "DELETE",
            ),
        ]:
            h, c = self.handle(action, replies)
            h.run(action, {"repo": REPO, **args})
            self.assertEqual(c.calls[-1][0], method)
