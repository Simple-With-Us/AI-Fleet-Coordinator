"""Local HTTP envelope tests only; no external service or credentials."""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from git_relay.secret_scan import scan_args
from git_relay.server import MAX_FILE_REQUEST_BYTES, MAX_REQUEST_BYTES, make_handler
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Stub:
    def __init__(self):
        self.calls = []

    def handle(self, token, action, args):
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
            b"a" * 60000 + b"\n-----BEGIN PRIVATE KEY-----"
        ).decode()
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_file_put", args, args["repo"])


if __name__ == "__main__":
    unittest.main()


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
                {"text": "a" * 300000 + "-----BEGIN PRIVATE KEY-----"},
                _budget=[2000, MAX_FILE_REQUEST_BYTES],
            )
