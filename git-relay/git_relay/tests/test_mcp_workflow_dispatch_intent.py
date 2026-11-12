"""workflow_dispatch advertises and forwards optional intent_id (AFC-727).

intent_id alone must pass MCP validation and reach the relay body; only
expected_head_sha without intent_id is refused."""

import http.server
import json
import threading
import unittest

from git_relay.mcp_server import RelayMcp, _validate_args

ARGS = {
    "repo": "o/r",
    "workflow": "ci.yml",
    "ref": "main",
    "intent_id": "qualification-one",
}


class WorkflowDispatchIntentTests(unittest.TestCase):
    def test_validate_accepts_optional_intent_id(self):
        self.assertIsNone(_validate_args("workflow_dispatch", ARGS))
        self.assertIsNone(
            _validate_args("workflow_dispatch", {"repo": "o/r", "workflow": "ci.yml", "ref": "main"})
        )

    def test_validate_refuses_expected_head_sha_without_intent(self):
        self.assertEqual(
            _validate_args(
                "workflow_dispatch",
                {"repo": "o/r", "workflow": "ci.yml", "ref": "main", "expected_head_sha": "a" * 40},
            ),
            "expected_head_sha requires intent_id",
        )

    def test_validate_still_rejects_unknown_fields(self):
        self.assertEqual(
            _validate_args("workflow_dispatch", dict(ARGS, bogus="y")),
            "unexpected argument \x27bogus\x27",
        )

    def test_intent_id_reaches_the_relay_request_body(self):
        bodies = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = b"{\"ok\": true}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            reply = RelayMcp(f"http://127.0.0.1:{server.server_port}", "seat-token-value").handle({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "workflow_dispatch", "arguments": ARGS},
            })
            self.assertNotIn("error", reply)
            self.assertEqual(bodies, [dict(ARGS)])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
