import unittest
from agent_sync import listener_cli as CLI

class LoopStatusTests(unittest.TestCase):
    def test_guard_state_is_explicit(self):
        self.assertIn("OFF", CLI._wake_loop_status_line({"wake_loop_prevention": False, "loop_guard": []}))
        self.assertEqual(CLI._wake_loop_status_line({"wake_loop_prevention": True, "loop_guard": []}), "wake loop prevention: ON")

    def test_older_status_does_not_claim_a_guard_state(self):
        self.assertIsNone(CLI._wake_loop_status_line({"loop_guard": []}))
        self.assertIsNone(CLI._wake_loop_status_line({"wake_loop_prevention": "false"}))
