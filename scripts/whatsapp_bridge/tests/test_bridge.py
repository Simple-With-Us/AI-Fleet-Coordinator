import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from whatsapp_bridge.bridge import (
    Bridge,
    BridgeError,
    State,
    StopBridge,
    bounded_tree,
    digest,
    normalize,
)


def payload(text="hello", offset=9223372036854775807):
    return {
        "object": "whatsapp_agent_platform",
        "entry": [
            {
                "id": "123456",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "id": "wamid.1234",
                                    "from": "user:owner",
                                    "timestamp": "1792700000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                            "statuses": [],
                        },
                    }
                ],
            }
        ],
        "next_offset": offset,
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(self.directory.name)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_persist_only_exact_ack_and_sign_raw_body(self):
        calls = []

        def request(url, headers, body=None):
            calls.append((url, headers, body))
            if body is None:
                return payload("👀 silent")
            decoded = json.loads(body)
            self.assertEqual(
                headers["X-WA-Bridge-Signature"],
                hmac.new(b"k" * 64, body, hashlib.sha256).hexdigest(),
            )
            self.assertEqual(decoded["messages"][0]["text"], "👀 silent")
            self.assertEqual(decoded["messages"][0]["sender_hash"], digest("user:owner"))
            self.assertNotIn("user:owner", body.decode())
            return {"ok": True, "processed": True, "next_offset": decoded["next_offset"]}

        Bridge(self.state, "synthetic", "k" * 64, "https://worker.example", request).step()
        self.assertEqual(self.state.offset(), 9223372036854775807)
        self.assertEqual(len(calls), 2)
        self.assertIn("timeout=25", calls[0][0])
        with open(os.path.join(self.directory.name, "state.sqlite3"), "rb") as f:
            data = f.read()
        self.assertNotIn("👀 silent".encode(), data)
        self.assertNotIn(b"synthetic", data)

    def test_bad_ack_never_advances(self):
        for answer in [
            None,
            {},
            {"ok": True, "next_offset": "9223372036854775807"},
            {"ok": True, "processed": True, "next_offset": 9223372036854775807},
        ]:
            bridge = Bridge(
                self.state,
                "synthetic",
                "k" * 64,
                "https://worker.example",
                lambda url, headers, body=None: payload() if body is None else answer,
            )
            with self.assertRaises(BridgeError):
                bridge.step()
            self.assertEqual(self.state.offset(), 0)

    def test_restart_and_single_local_poller(self):
        self.state.save(123)
        with self.assertRaisesRegex(StopBridge, "poller_already_running"):
            State(self.directory.name)
        self.state.close()
        self.state = State(self.directory.name)
        self.assertEqual(self.state.offset(), 123)

    def test_strict_batch_validation_and_unknown_type(self):
        for change in [
            {"next_offset": 1.0},
            {"next_offset": -1},
            {"next_offset": 2**63},
            {"object": "wrong"},
            {"entry": []},
        ]:
            with self.assertRaises(BridgeError):
                normalize({**payload(), **change}, 0)
        p = payload()
        p["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "new_type"
        self.assertIsNone(normalize(p, 0)["messages"][0]["text"])
        with self.assertRaises(BridgeError):
            normalize(payload(offset=5), 6)
        with self.assertRaises(BridgeError):
            normalize(payload("x" * 4097), 0)

    def test_idle_poll_heartbeats(self):
        self.state.save(99)

        def request(url, headers, body=None):
            if body is None:
                return None
            decoded = json.loads(body)
            self.assertEqual(decoded["messages"], [])
            self.assertEqual(decoded["agent_hash"], "a" * 64)
            return {"ok": True, "processed": True, "next_offset": "99"}

        with patch.dict(os.environ, {"WA_AGENT_HASH_JET": "a" * 64}):
            Bridge(self.state, "synthetic", "k" * 64, "https://worker.example", request).step()
        self.assertEqual(self.state.offset(), 99)

    def test_limits_and_urls(self):
        nested = {}
        child = nested
        for _ in range(18):
            child["x"] = {}
            child = child["x"]
        with self.assertRaises(BridgeError):
            bounded_tree(nested)
        for url in [
            "http://worker.example",
            "https://user:pass@worker.example",
            "https://worker.example/path",
            "https://worker.example?x=1",
        ]:
            with self.assertRaises(StopBridge):
                Bridge(self.state, "synthetic", "k" * 64, url)


if __name__ == "__main__":
    unittest.main()


class BindingTests(unittest.TestCase):
    def test_code_and_creator_check_both_required(self):
        from whatsapp_bridge.binding import verify_candidate

        code = "JET-0123456789ABCDEF"
        p = payload(code)
        calls = []

        def request(url, headers, body):
            calls.append(json.loads(body))
            return {"success": True}

        pin = verify_candidate(
            p, code, "synthetic", 1792699999, 1792700100, request, now=lambda: 1792700001
        )
        self.assertEqual(pin["verified_via"], "code+creator_check")
        self.assertEqual(pin["sender_hash"], digest("user:owner"))
        self.assertEqual(calls[0]["status"], "read")
        with self.assertRaises(StopBridge):
            verify_candidate(
                p,
                "JET-FFFFFFFFFFFFFFFF",
                "synthetic",
                1792699999,
                1792700100,
                request,
                now=lambda: 1792700001,
            )
        with self.assertRaises(StopBridge):
            verify_candidate(
                p,
                code,
                "synthetic",
                1792699999,
                1792700100,
                lambda *a: {"success": False},
                now=lambda: 1792700001,
            )
