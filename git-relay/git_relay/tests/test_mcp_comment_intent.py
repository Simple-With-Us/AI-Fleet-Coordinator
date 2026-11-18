"""comment_pr advertises and forwards the optional intent_id dedupe override
(sentry thread 4535033892):  the MCP catalog used to reject it as an
unexpected argument even though the HTTP relay accepts it (server.py spec)."""

import http.server
import json
import threading
import unittest

from git_relay.mcp_server import RelayMcp, _validate_args

ARGS = {"repo": "o/r", "number": 1, "body": "x", "intent_id": "pr-1780-kody-refresh"}


class CommentIntentTests(unittest.TestCase):
    def test_validate_accepts_optional_intent_id(self):
        self.assertIsNone(_validate_args("comment_pr", ARGS))
        self.assertIsNone(_validate_args("comment_pr", {"repo": "o/r", "number": 1, "body": "x"}))

    def test_validate_still_rejects_unknown_fields(self):
        self.assertEqual(
            _validate_args("comment_pr", dict(ARGS, bogus="y")),
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
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "comment_pr", "arguments": ARGS},
            })
            self.assertNotIn("error", reply)
            self.assertEqual(bodies, [dict(ARGS)])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
