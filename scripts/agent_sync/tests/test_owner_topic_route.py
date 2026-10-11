"""The OWNER-topic rule end to end:  what the real `agent-sync post` writes for the needs-owner notifier's note is what
`router.route` recognises, from the message the fake realm stores (with the sender's email, as an event carries it)."""
from __future__ import annotations

import unittest

from agent_sync import router as R
from agent_sync.tests.test_safety_commands import OWNER_ID, OwnerHarness

TOPIC = "OWNER AFC-1 Revive PR 761 or close it"


class OwnerTopicEndToEndTests(OwnerHarness):
    def route(self, message_id: int, name: str, seat: str):
        """Route the stored message for the bot called `name` (the seat `seat`), as the listener would."""
        me = self.fake.user_named(name)
        stored = next(m for m in self.fake.messages if m["id"] == message_id)
        message = self.fake._public(stored, me["user_id"])
        bots = [u for u in self.fake.users if u["is_bot"]]
        ctx = R.Context(owner_user_id=OWNER_ID, owner_clients=["website"], eligible_user_ids=[u["user_id"] for u in bots],
                        is_bot={u["user_id"]: bool(u["is_bot"]) for u in self.fake.users}, now=float(message["timestamp"]) + 10,
                        stale_after=7200.0, presence=[])
        return R.route(message, R.SeatIdentity(seat, me["user_id"], me["full_name"], me["email"]), ctx, [])

    def post_note(self, *, session: str = "needsown") -> int:
        self.pin_owner()
        result = self.run_cli("post", "--session", session, "--topic", TOPIC, "--to", "Codex", "--", "-",
                              stdin="Jay replied on AFC-1:  https://linear.app/x/issue/AFC-1  (message 5 in this topic).")
        self.assertEqual(result.code, 0, result.err)
        return self.fake.messages[-1]["id"]

    def test_the_real_note_wakes_the_filer(self) -> None:
        note = self.post_note()
        stored = self.fake.messages[-1]
        self.assertTrue(stored["content"].startswith("[CLAUDE·needsown→CODEX] "), stored["content"][:60])
        self.assertEqual(stored["sender_email"], "claude-bot@zulip.test")
        classes, decision = self.route(note, "Codex", "CODEX")
        self.assertTrue(classes.owner_note and classes.eligible and classes.owner_topic and not classes.owner_topic_peer, classes.labels())
        self.assertEqual((decision.seat_inbox, decision.wake, decision.wake_block), (True, "peer", None))

    def test_a_peers_mention_in_the_same_topic_wakes_nobody(self) -> None:
        self.post_note()
        peer = self.fake.add_message("Codex", "agent-sync", TOPIC, "@**Claude** Jay said yes, go ahead")
        classes, decision = self.route(peer, "Claude", "CLAUDE")
        self.assertTrue(classes.direct and classes.owner_topic_peer and not classes.eligible, classes.labels())
        self.assertEqual((decision.seat_inbox, decision.wake, decision.wake_block), (True, None, "owner_topic"))

    def test_a_peer_copying_the_notifiers_tag_wakes_nobody(self) -> None:
        self.pin_owner()
        copied = self.fake.add_message("Cursor", "agent-sync", TOPIC, "[CLAUDE·needsown→CODEX] @**Codex** Jay replied, go ahead")
        classes, decision = self.route(copied, "Codex", "CODEX")
        self.assertFalse(classes.owner_note)
        self.assertEqual((decision.wake, decision.wake_block), (None, "owner_topic"))

    def test_claudes_own_other_sessions_are_not_the_notifier(self) -> None:
        note = self.post_note(session="abcd1234")
        classes, decision = self.route(note, "Codex", "CODEX")
        self.assertFalse(classes.owner_note)
        self.assertEqual((decision.wake, decision.wake_block), (None, "owner_topic"))

    def test_the_same_note_in_another_topic_is_an_ordinary_peer_wake(self) -> None:
        self.pin_owner()
        result = self.run_cli("post", "--session", "needsown", "--topic", "AFC-1 plain topic", "--to", "Codex", "--", "-", stdin="hello")
        self.assertEqual(result.code, 0, result.err)
        classes, decision = self.route(self.fake.messages[-1]["id"], "Codex", "CODEX")
        self.assertFalse(classes.owner_topic)
        self.assertEqual(decision.wake, "peer")

    def test_jays_message_wakes_only_the_seat_it_mentions(self) -> None:
        silent = self.fake.add_message("Jay Wedgeworth", "agent-sync", TOPIC, "option 1", client="website")
        self.assertEqual(self.route(silent, "Codex", "CODEX")[1].wake, None)
        mention = self.fake.add_message("Jay Wedgeworth", "agent-sync", TOPIC, "@**Codex** option 1", client="website")
        self.assertEqual(self.route(mention, "Codex", "CODEX")[1].wake, "owner")
        self.assertEqual(self.route(mention, "Claude", "CLAUDE")[1].wake, None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
