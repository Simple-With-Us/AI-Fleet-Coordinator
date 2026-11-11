"""Acceptance must fail closed on receipts without actual target-chat input."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from simple_tools import wake_acceptance as WA

SID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.args = dict(
            wake_id="w-123", message_id=12345, session_id=SID, generation=3,
            seat="CODEX", mention="Codex",
            source={"id": 12345, "content": "@**Codex** BB please read the topic"},
            listener=[{"wake_id": "w-123", "seat": "CODEX", "trigger_ids": [12345], "state": "done"}],
            receipt={"wake_id": "w-123", "seat": "CODEX", "test": False,
                     "session_id": SID, "generation": 3,
                     "status": "delivered", "delivered": None},
            transcript=[{"type": "session_meta", "payload": {"id": SID}},
                        {"type": "event_msg", "payload": {"type": "user_message",
                         "message": "Wake w-123; source message 12345"}}],
        )

    def check(self, status):
        result = WA.assess(**self.args)
        self.assertEqual(result["status"], status, result)
        return result

    def test_complete_correlated_chain_proves_user_input_arrival(self):
        self.check("proven")

    def test_queue_acceptance_and_confirmed_receipt_without_user_input_are_pending(self):
        self.args["transcript"] = self.args["transcript"][:1]
        self.args["receipt"].update(delivered=True, ack="confirmed")
        self.check("pending")

    def test_assistant_quoting_wake_is_not_arrival(self):
        self.args["transcript"][1] = {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "input_text", "text": "w-123 12345"}]}}
        self.check("pending")

    def test_user_response_item_is_arrival(self):
        self.args["transcript"][1] = {"type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "w-123 12345"}]}}
        self.check("proven")

    def test_other_chat_cannot_satisfy_the_target(self):
        self.args["transcript"][0]["payload"]["id"] = OTHER
        self.assertIn("transcript-session-not-proven", self.check("rejected")["errors"])

    def test_tail_without_authoritative_header_is_rejected(self):
        self.args["transcript"] = self.args["transcript"][1:]
        self.check("rejected")

    def test_silent_mention_does_not_prove_source_capture(self):
        self.args["source"]["content"] = "@_**Codex** BB please read"
        self.assertIn("source-has-no-loud-mention", self.check("rejected")["errors"])

    def test_wake_prefix_and_other_message_do_not_match(self):
        self.args["transcript"][1]["payload"]["message"] = "w-1234 123456"
        self.check("pending")

    def test_no_single_input_with_both_ids_does_not_match(self):
        self.args["transcript"][1]["payload"]["message"] = "w-123"
        self.args["transcript"].append({"type": "event_msg", "payload": {
            "type": "user_message", "message": "source 12345"}})
        self.check("pending")

    def test_wrong_source_wake_seat_or_target_fail(self):
        for where, key, value in (("source", "id", 99), ("receipt", "wake_id", "other"),
                                  ("receipt", "seat", "ZC"), ("receipt", "session_id", OTHER)):
            with self.subTest(where=where, key=key):
                args = copy.deepcopy(self.args)
                args[where][key] = value
                self.assertEqual(WA.assess(**args)["status"], "rejected")

    def test_listener_correlation_and_final_state_are_required(self):
        self.args["listener"][0]["state"] = "accepted"
        self.check("pending")
        self.args["listener"][0]["state"] = "failed"
        self.check("rejected")
        self.args["listener"][0].update(state="done", trigger_ids=[88])
        self.check("rejected")

    def test_dry_run_or_failed_delivery_is_not_real_acceptance(self):
        self.args["receipt"]["test"] = True
        self.check("rejected")
        self.args["receipt"].update(test=False, status="dead", delivered=False)
        self.check("rejected")

    def test_pending_router_cannot_be_overruled_by_transcript(self):
        self.args["receipt"]["status"] = "accepted"
        self.check("pending")

    def test_generation_reuse_or_missing_generation_cannot_pass(self):
        for value in (None, 0, 2, True, "3"):
            with self.subTest(value=value):
                self.args["receipt"]["generation"] = value
                result = self.check("rejected")
                self.assertIn("router-generation-mismatch", result["errors"])

    def test_expected_generation_must_be_a_positive_integer(self):
        for value in (0, -1, True, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WA.assess(**{**self.args, "generation": value})

    def test_boolean_source_id_cannot_equal_integer_message_id(self):
        self.args["message_id"] = 1
        self.args["source"]["id"] = True
        self.args["listener"][0]["trigger_ids"] = [1]
        self.args["transcript"][1]["payload"]["message"] = "w-123; source 1"
        self.assertIn("source-message-mismatch", self.check("rejected")["errors"])

    def test_malformed_delivered_value_cannot_pass(self):
        for value in (0, 1, "unknown", [], {}):
            with self.subTest(value=value):
                self.args["receipt"]["delivered"] = value
                self.check("pending")
        self.args["receipt"].pop("delivered")
        self.check("pending")

    def test_jsonl_tail_keeps_original_session_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rollout.jsonl"
            header = self.args["transcript"][0]
            path.write_text(json.dumps(header) + "\n" + json.dumps({"filler": "x" * 500}) + "\n"
                            + json.dumps(self.args["transcript"][1]) + "\n")
            original = WA.MAX_BYTES
            try:
                WA.MAX_BYTES = 300
                rows = WA.json_lines(str(path))
            finally:
                WA.MAX_BYTES = original
            self.assertEqual(rows[0], header)
            self.assertEqual(rows[-1], self.args["transcript"][1])


if __name__ == "__main__":
    unittest.main()
