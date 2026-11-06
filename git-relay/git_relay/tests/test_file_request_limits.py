"""Local HTTP envelope tests only; no external service or credentials."""

import base64
import hashlib
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from git_relay.secret_scan import scan_args
from git_relay.server import (
    MAX_FILE_REQUEST_BYTES,
    MAX_LARGE_TEXT_REQUEST_BYTES,
    MAX_REQUEST_BYTES,
    make_handler,
)
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Stub:
    def __init__(self):
        self.calls = []

    def handle(self, token, action, args, proof=None):
        self.calls.append((token, action, args))
        return 403, {"error": "stub refuses authorization"}


class FileEnvelopeTests(unittest.TestCase):
    def test_exact_http_limits_and_non_file_default(self):
        stub = Stub()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(stub))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for action, cap in [
                ("wf_file_put", MAX_FILE_REQUEST_BYTES),
                ("wf_files_put", MAX_FILE_REQUEST_BYTES),
                ("wf_git_blob_create", MAX_LARGE_TEXT_REQUEST_BYTES),
                ("wf_git_tree_create", MAX_LARGE_TEXT_REQUEST_BYTES),
                ("wf_runs", MAX_REQUEST_BYTES),
            ]:
                for delta in (0, 1):
                    raw = json.dumps({"x": ""}).encode()
                    raw = json.dumps({"x": "a" * (cap - len(raw) + delta)}).encode()
                    self.assertEqual(len(raw), cap + delta)
                    before = len(stub.calls)
                    import http.client

                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                    conn.putrequest("POST", f"/v1/{action}")
                    conn.putheader("Content-Length", str(len(raw)))
                    conn.endheaders()
                    if delta == 0:
                        conn.send(raw)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 403 if delta == 0 else 400)
                    response.read()
                    conn.close()
                    self.assertEqual(len(stub.calls) - before, 0 if delta else 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_large_file_shapes_and_secret_refusal(self):
        import base64

        for size in (45001, 143259, 184320, 2 * 1024 * 1024):
            args = {
                "repo": "example/repo",
                "branch": "review",
                "path": "source.txt",
                "message": "update",
                "content_b64": base64.b64encode(b"a" * size).decode(),
            }
            scan_args(args, _budget=[2000, MAX_FILE_REQUEST_BYTES])
            WorkflowVerbs.check_shape("wf_file_put", args, args["repo"])
        args["content_b64"] = base64.b64encode(
            b"a" * 60000 + b"\n-----BEGIN " + b"PRIVATE KEY-----"
        ).decode()
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_file_put", args, args["repo"])


class DecodedFileBudgetTests(unittest.TestCase):
    def test_decoded_single_and_aggregate_boundaries(self):
        import base64

        from git_relay.workflow_verbs import MAX_FILE_RAW

        def enc(n):
            return base64.b64encode(b"a" * n).decode()

        single = {
            "repo": "example/repo",
            "branch": "review",
            "path": "a",
            "message": "update",
            "content_b64": enc(MAX_FILE_RAW),
        }
        WorkflowVerbs.check_shape("wf_file_put", single, single["repo"])
        single["content_b64"] = enc(MAX_FILE_RAW + 1)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_file_put", single, single["repo"])
        multi = {
            "repo": "example/repo",
            "branch": "review",
            "message": "update",
            "expected_head_sha": "a" * 40,
            "files": [
                {"path": "a", "content_b64": enc(MAX_FILE_RAW)},
                {"path": "b", "content_b64": enc(MAX_FILE_RAW)},
            ],
        }
        WorkflowVerbs.check_shape("wf_files_put", multi, multi["repo"])
        multi["files"].append({"path": "c", "content_b64": enc(1)})
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_files_put", multi, multi["repo"])
        multi["files"] = [{"path": "a", "content_b64": enc(MAX_FILE_RAW + 1)}]
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_files_put", multi, multi["repo"])

    def test_complete_scan_late_secret_and_unrelated_budget(self):
        from git_relay.secret_scan import InputTooComplexError, SecretInputError

        with self.assertRaises(InputTooComplexError):
            scan_args({"text": "a" * 300000})
        with self.assertRaises(SecretInputError):
            scan_args(
                {"text": "a" * 300000 + "-----BEGIN " + "PRIVATE KEY-----"},
                _budget=[2000, MAX_FILE_REQUEST_BYTES],
            )


class GitObjectsClient:
    """Upstream double: computes the real blob receipt shape, records every call."""

    def __init__(self, blob_raw=b""):
        self.calls = []
        self.blob_raw = blob_raw

    @staticmethod
    def blob_sha(raw):
        return hashlib.sha1(
            b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False
        ).hexdigest()

    def _call(self, method, path, payload=None, bearer=None):
        self.calls.append((method, path))
        if method == "POST" and path.endswith("/git/blobs"):
            return {"sha": self.blob_sha(payload["content"].encode())}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "b" * 40, "truncated": False, "tree": []}
        if method == "GET" and "/git/blobs/" in path:
            return {
                "sha": path.rsplit("/", 1)[1],
                "encoding": "base64",
                "size": len(self.blob_raw),
                "content": base64.b64encode(self.blob_raw).decode(),
            }
        raise AssertionError(path)


class LargeTextEndToEndTests(unittest.TestCase):
    """Real stack E2E: HTTP envelope -> secret scan -> shape -> policy -> verb.

    Drives the actual ThreadingHTTPServer + Relay + WorkflowVerbs path with a
    fake upstream; no external service or credentials.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay, make_handler

        config = json.dumps(
            {
                "swu-wise": {
                    "token": "s" * 32,
                    "app_slug": "swu-wise",
                    "mint_permissions": {"contents": "write", "metadata": "read"},
                    "actions": ["wf_git_blob_create", "wf_git_tree_create", "wf_git_blob_get"],
                    "repos": ["example/repo"],
                }
            }
        )

        class Minter:
            def token_for(self, *args):
                return "synthetic-own-token", 0

        self.client = GitObjectsClient()
        self._tmp = tempfile.TemporaryDirectory()
        relay = Relay(
            SeatPolicy(config),
            self.client,
            AuditLog(str(Path(self._tmp.name) / "audit.jsonl")),
            app_minter=Minter(),
            workflow_verbs_enabled=True,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(relay))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def post(self, action, args):
        import http.client

        raw = json.dumps(args).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(
            "POST",
            f"/v1/{action}",
            body=raw,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + "s" * 32},
        )
        response = conn.getresponse()
        status, body = response.status, json.loads(response.read())
        conn.close()
        return status, body

    def test_explicit_false_accepted_as_base_cap_end_to_end(self):
        status, body = self.post(
            "wf_git_blob_create", {"repo": "example/repo", "text": "x", "allow_large_text": False}
        )
        self.assertEqual(
            (status, body), (200, {"blob": {"sha": GitObjectsClient.blob_sha(b"x"), "size": 1}})
        )
        status, body = self.post(
            "wf_git_tree_create",
            {
                "repo": "example/repo",
                "base_tree": "a" * 40,
                "files": [{"path": "a", "text": "x", "mode": "100644"}],
                "allow_large_text": False,
            },
        )
        self.assertEqual(status, 200)
        raw = b"x"
        self.client.blob_raw = raw
        status, body = self.post(
            "wf_git_blob_get",
            {
                "repo": "example/repo",
                "sha": GitObjectsClient.blob_sha(raw),
                "allow_large_text": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["blob"]["text"], "x")

    def test_explicit_false_holds_base_cap_end_to_end(self):
        before = len(self.client.calls)
        status, _ = self.post(
            "wf_git_blob_create",
            {"repo": "example/repo", "text": "y" * 50000, "allow_large_text": False},
        )
        self.assertEqual(status, 400)
        self.assertEqual(len(self.client.calls), before)

    def test_opt_in_large_text_end_to_end(self):
        status, body = self.post(
            "wf_git_blob_create",
            {"repo": "example/repo", "text": "y" * 83854, "allow_large_text": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["blob"]["size"], 83854)

    def test_full_body_scan_catches_late_secret_past_old_envelope_cap(self):
        # 83854 bytes exceeds the retired 64KiB envelope cap: before the
        # large-text class this body was rejected unread; the scan budget now
        # covers the full body and must catch a secret at its very end.
        before = len(self.client.calls)
        text = "a" * 83800 + "\n-----BEGIN " + "PRIVATE KEY-----"
        status, _ = self.post(
            "wf_git_blob_create", {"repo": "example/repo", "text": text, "allow_large_text": True}
        )
        self.assertEqual(status, 400)
        self.assertEqual(len(self.client.calls), before)


if __name__ == "__main__":
    unittest.main()
