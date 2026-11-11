import unittest

from git_relay.deploy_proof import RuntimeProof


class ProofTests(unittest.TestCase):
    def test_actual_read_identity_and_negative_only(self):
        p = RuntimeProof("synthetic")
        calls = []

        def call(action, args):
            calls.append((action, args))
            if action == "wf_repo_get":
                return 200, {"repository": {"full_name": p.REPO, "default_branch": "main"}}
            return 400, {"error": "default branch is not writable"}

        p.call = call
        self.assertTrue(p({}, {"code/provenance_patch.py": "new"}))
        self.assertEqual(calls[1][1]["branch"], "main")
        self.assertEqual(calls[1][1]["expected_head_sha"], "0" * 40)
        self.assertEqual(calls[1][1]["base_blob_sha"], "0" * 40)
        p.call = lambda *_: (403, {"error": "action not allowed"})
        self.assertFalse(p({}, {"code/provenance_patch.py": "new"}))

    def test_merged_default_branch_probe_has_zero_provider_mutations(self):
        from git_relay.workflow_verbs import Refused, WorkflowVerbs

        class Client:
            def __init__(self):
                self.calls = []

            def _call(self, method, path, body, **kw):
                self.calls.append((method, path))
                if method != "GET":
                    raise AssertionError("provider mutation")
                return {"default_branch": "main"}

        client = Client()
        handle = WorkflowVerbs.__new__(WorkflowVerbs)
        handle.client, handle.bearer = client, "synthetic-proof"
        handle.workflow_write_allowed = False
        args = {
            "repo": RuntimeProof.REPO,
            "branch": "main",
            "path": "README.md",
            "message": "Bounded runtime negative probe: default branch must refuse",
            "expected_head_sha": "0" * 40,
            "base_blob_sha": "0" * 40,
            "edits": [{"start": 0, "end": 0, "text": ""}],
        }
        with self.assertRaisesRegex(Refused, "default branch is not writable"):
            handle.wf_file_patch(RuntimeProof.REPO, args)
        self.assertEqual(client.calls, [("GET", "/repos/" + RuntimeProof.REPO)])
