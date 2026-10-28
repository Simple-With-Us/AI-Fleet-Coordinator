import unittest
from .test_workflow_verbs import REPO, svc
from git_relay.workflow_verbs import Refused

class PrResponseScan(unittest.TestCase):
    def test_plain_and_null_text_remains_available(self):
        for body in (None, "plain text"):
            s, c = svc({"title": "plain title", "body": body})
            self.assertEqual(s.run("wf_pr_get", {"repo": REPO, "number": 1})["pull"]["body"], body)
    def test_each_credential_shaped_field_is_refused_without_leak(self):
        for key in ("title", "body"):
            for token in ("ghp_" + "x" * 30, "github_pat_" + "x" * 30):
                with self.subTest(key=key):
                    s, c = svc({"title": "plain", "body": None, key: token})
                    with self.assertRaises(Refused) as caught:
                        s.run("wf_pr_get", {"repo": REPO, "number": 1})
                    self.assertNotIn(token, str(caught.exception))
