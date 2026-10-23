"""A retired channel name has to land in the channel the fleet reads.

The realm renamed the fleet's coordination channel to Work-In-Progress on Oct 22, 2026 and
left the old name behind as a tombstone stream marked DO NOT USE.  The protocol says the
CLI and the hosted tools default to the new name and still accept the old one as an alias
for it, so a caller that still names the old channel has to land in the live one.

These tests hold the alias to the channel the fleet uses now, and they hold the case
where a realm still calls that channel agent-sync:  there the name stays, because the
effective default is the same name and nothing changes.
"""

from __future__ import annotations

import unittest

from agent_sync import zulip as Z
from agent_sync.tests.harness import Harness


class ResolveChannelTests(unittest.TestCase):
    def test_a_retired_name_resolves_to_the_live_channel(self) -> None:
        self.assertEqual(Z.resolve_channel("agent-sync"), Z.DEFAULT_CHANNEL)

    def test_the_retired_name_is_matched_without_case_or_hash(self) -> None:
        for name in ("agent-sync", "Agent-Sync", "AGENT-SYNC", " agent-sync ", "#agent-sync"):
            self.assertEqual(Z.resolve_channel(name), Z.DEFAULT_CHANNEL, name)

    def test_another_channel_is_left_alone(self) -> None:
        for name in ("side-chats", "alerts", "builds"):
            self.assertEqual(Z.resolve_channel(name), name)

    def test_no_name_is_the_default_channel(self) -> None:
        self.assertEqual(Z.resolve_channel(""), Z.DEFAULT_CHANNEL)
        self.assertEqual(Z.resolve_channel(None), Z.DEFAULT_CHANNEL)
        self.assertEqual(
            Z.resolve_channel("  ", {"AGENT_SYNC_CHANNEL": "side-chats"}), "side-chats"
        )

    def test_the_retired_name_follows_the_channel_the_fleet_uses_now(self) -> None:
        # A deployment that moved the fleet's channel:  the old name means where the
        # coordination channel is now, not a literal that names the tombstone stream.
        self.assertEqual(
            Z.resolve_channel("agent-sync", {"AGENT_SYNC_CHANNEL": "side-chats"}),
            "side-chats",
        )

    def test_a_realm_that_still_calls_it_agent_sync_keeps_the_name(self) -> None:
        # The fake realm, and any deployment whose realm still has that name, is
        # unaffected:  its effective default is the same name.
        env = {"AGENT_SYNC_CHANNEL": "agent-sync"}
        self.assertEqual(Z.resolve_channel("agent-sync", env), "agent-sync")


class ChannelAliasCommandTests(Harness):
    def last_post(self):
        return self.fake.requests_to("POST", "messages")[-1]

    def test_a_post_to_the_retired_name_lands_in_the_live_channel(self) -> None:
        # The harness pins the default to the fake realm's own channel, so the alias
        # resolves to it and the post lands there.
        result = self.run_cli("post", "--channel", "agent-sync", "--topic", "t", "hello")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["to"], "agent-sync")

    def test_the_retired_name_lands_in_the_channel_the_runtime_uses(self) -> None:
        # With no override the runtime's channel is the realm's live one, so the post
        # carries that name and not the retired one.
        result = self.run_cli(
            "post",
            "--channel",
            "agent-sync",
            "--topic",
            "t",
            "hello",
            env=self.env(AGENT_SYNC_CHANNEL=None),
        )
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["to"], Z.DEFAULT_CHANNEL)

    def test_the_retired_name_follows_the_channel_the_fleet_uses_now(self) -> None:
        result = self.run_cli(
            "post",
            "--channel",
            "agent-sync",
            "--topic",
            "t",
            "hello",
            env=self.env(AGENT_SYNC_CHANNEL="other"),
        )
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["to"], "other")

    def test_a_read_of_the_retired_name_finds_what_was_posted_to_the_live_channel(self) -> None:
        # The narrow names the channel too, so the alias has to be resolved before it
        # becomes an operand:  otherwise the read looks in the tombstone stream.
        env = self.env(AGENT_SYNC_CHANNEL=None)
        posted = self.run_cli(
            "post", "--channel", "agent-sync", "--topic", "t", "hello", env=env
        )
        self.assertEqual(posted.code, 0, posted.err)
        read = self.run_cli(
            "read", "--channel", "agent-sync", "--topic", "t", "--include-self", env=env
        )
        self.assertEqual(read.code, 0, read.err)
        self.assertIn("hello", read.out)

    def test_the_fleet_wake_accepts_the_retired_name(self) -> None:
        # --fleet is allowed in exactly one channel, and the retired name is that
        # channel, so the guard lets it through and the group mention goes with it.
        result = self.run_cli(
            "post",
            "--channel",
            "agent-sync",
            "--topic",
            "fleet",
            "--fleet",
            "wake up",
            env=self.env(AGENT_SYNC_CHANNEL=None),
        )
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["to"], Z.DEFAULT_CHANNEL)
        self.assertIn("@**all**", self.last_post().form["content"])

    def test_the_fleet_wake_still_refuses_another_channel(self) -> None:
        result = self.run_cli(
            "post",
            "--channel",
            "other",
            "--topic",
            "fleet",
            "--fleet",
            "wake up",
            env=self.env(AGENT_SYNC_CHANNEL=None),
        )
        self.assertEqual(result.code, 2)
        self.assertIn("only works in", result.err)


if __name__ == "__main__":
    unittest.main()
