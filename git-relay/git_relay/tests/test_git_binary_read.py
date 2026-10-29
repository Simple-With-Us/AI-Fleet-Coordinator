import base64
import hashlib
import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Minter, Policy


class BinaryRead(unittest.TestCase):
    def runblob(self, raw, **extra):
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        c = Client(
            {
                "sha": sha,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
                **extra,
            }
        )
        a = "wf_git_blob_binary_get"
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
            a, {"repo": REPO, "sha": sha}
        ), c

    def test_binary_roundtrip_and_fixed_own_bearer(self):
        for raw in [b"", b"\xff\x00\x80", bytes(range(256)) * 125]:
            out, c = self.runblob(raw)
            self.assertEqual(base64.b64decode(out["blob"]["content_b64"]), raw)
            self.assertEqual(c.calls[0][0], "GET")
            self.assertEqual(c.calls[0][3], "own")

    def test_hash_scan_size_and_encoding_fail_closed(self):
        for raw in [b"x" * 32001, b"\xffghp_" + b"x" * 36]:
            with self.assertRaises(Refused):
                self.runblob(raw)
        for extra in [{"size": True}, {"sha": "a" * 40}, {"content": "%%%"}, {"encoding": "utf-8"}]:
            with self.assertRaises(RelayGitHubError):
                self.runblob(b"hi", **extra)

    def test_shape_and_least_permission(self):
        a = "wf_git_blob_binary_get"
        args = {"repo": REPO, "sha": "a" * 40}
        self.assertIsNone(WorkflowVerbs.check_shape(a, args, REPO))
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape(a, {**args, "offset": 1}, REPO)
        m = Minter()
        WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", a, REPO)
        self.assertEqual(m.calls[0][2], {"contents": "read", "metadata": "read"})

    def test_definite_response_size_vs_real_transport_failure(self):
        class BigClient(Client):
            def _call(self, *args, **kw):
                raise RelayGitHubError(0, "size", code="response_too_large")

        a = "wf_git_blob_binary_get"
        args = {"repo": REPO, "sha": "a" * 40}
        with self.assertRaises(Refused):
            WorkflowVerbs(BigClient(), Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
                a, args
            )

        class NetworkClient(Client):
            def _call(self, *args, **kw):
                raise RelayGitHubError(0, "network")

        with self.assertRaises(RelayGitHubError):
            WorkflowVerbs(NetworkClient(), Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
                a, args
            )

    def test_binary_crlf_wrapping_and_malformed_base64(self):
        out, _ = self.runblob(b"hello", content="aGVs\r\nbG8=\r\n")
        self.assertEqual(out["blob"]["content_b64"], "aGVsbG8=")
        with self.assertRaises(RelayGitHubError):
            self.runblob(b"hello", content="aGVs% bG8=")
