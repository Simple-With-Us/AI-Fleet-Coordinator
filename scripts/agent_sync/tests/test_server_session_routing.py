"""Server session routing acceptance with fake Zulip and real listener inbox files."""
from __future__ import annotations

import io

from agent_sync import attach
from agent_sync.tests.test_daemon import DaemonHarness, MENTION


class ServerSessionRoutingTests(DaemonHarness):
    def drain(self, lease_id):
        out, err = io.StringIO(), io.StringIO()
        code = attach.main(
            ["--drain"], env=self.env(AGENT_LEASE=lease_id),
            stdin=io.StringIO(), stdout=out, stderr=err,
            now=self.clock.time, sleep=self.clock.sleep,
        )
        self.assertEqual(code, 0, err.getvalue())
        self.transcript.extend([out.getvalue(), err.getvalue()])
        return out.getvalue()

    def exercise(self, read_before_route):
        daemon = self.started()
        self.write_lease("session-A", topics=[("agent-sync", "owned")],
                         session_id="aaaa1111", posted=[400], last_prompt=1)
        self.write_lease("session-B", session_id="bbbb2222", last_prompt=100)
        mid = self.fake.add_message("Codex", "agent-sync", "owned", MENTION)
        sibling = self.env(CLAUDE_CODE_SESSION_ID="bbbb2222", AGENT_LEASE="session-B")
        if read_before_route:
            self.assertEqual(self.run_cli("inbox", env=sibling).code, 0)
        self.pump_until(daemon, lambda: len(self.live_items("session-A")) == 1)
        if not read_before_route:
            self.assertEqual(self.run_cli("inbox", env=sibling).code, 0)
        self.assertEqual(self.live_items("session-B"), [])
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.ledger(), [], "no headless fallback for an owned topic")
        self.assertEqual(self.live_items("session-A")[0]["id"], mid)
        self.assertEqual(self.live_items("session-A")[0]["class"], "interrupt")
        self.assertIn("nothing pending", self.drain("session-B"))
        self.assertIn(str(mid), self.drain("session-A"))
        self.assertIn("nothing pending", self.drain("session-A"), "at-most-once claim")
        reply = self.fake.add_message("Codex", "agent-sync", "owned", "re=400 reply to A")
        self.pump_until(daemon, lambda: len(self.live_items("session-A")) == 2)
        self.assertEqual(self.live_items("session-A")[-1]["id"], reply)
        self.assertEqual(self.live_items("session-A")[-1]["class"], "interrupt")
        self.assertEqual(self.live_items("session-B"), [])

    def test_sibling_inbox_read_before_routing_cannot_steal_owned_message(self):
        self.exercise(True)

    def test_sibling_inbox_read_after_routing_cannot_ack_live_delivery(self):
        self.exercise(False)
