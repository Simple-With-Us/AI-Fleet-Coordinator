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
from types import SimpleNamespace
from typing import Any

from agent_sync import live as L
from agent_sync import zulip as Z
from agent_sync import zulip_ops as OPS
from agent_sync.tests.harness import Harness
from agent_sync.tests.mcp_harness import McpHarness
from agent_sync.tests.test_attach import LEASE, AttachHarness
from agent_sync.tests.test_daemon import DaemonHarness


class ResolveChannelTests(unittest.TestCase):
    def test_a_retired_name_resolves_to_the_live_channel(self) -> None:
        self.assertEqual(Z.resolve_channel("agent-sync"), Z.DEFAULT_CHANNEL)

    def test_the_retired_name_is_matched_without_case_or_hash(self) -> None:
        for name in ("agent-sync", "Agent-Sync", "FLEET-PROTOCOL", " agent-sync ", "#agent-sync"):
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

    def test_a_move_with_an_anchor_accepts_the_resolved_channel(self) -> None:
        # The anchor guard and the lookup it falls back to have to name the same
        # channel:  the retired name resolves once and both halves use the result.
        env = self.env(AGENT_SYNC_CHANNEL="other")
        message = self.fake.add_message("Codex", "other", "old name", "a")
        result = self.run_cli(
            "move",
            "--channel",
            "agent-sync",
            "--topic",
            "old name",
            "--id",
            str(message),
            "--new-topic",
            "new name",
            env=env,
        )
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.fake._find(message)["subject"], "new name")


class OpsAgent:
    """The slice of the agent the ops layer touches, so the layer can be tested on its own.

    The CLI and the hosted tools resolve a channel name before they call this layer, so a
    test that goes through them cannot tell whether the layer resolves for itself.  This
    stub answers the two calls a move makes:  the anchor it guards with, and the patch it
    sends.
    """

    def __init__(self, env: dict[str, str], message: dict[str, Any]) -> None:
        self.rt = SimpleNamespace(env=env)
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self._message = message
        self.client = SimpleNamespace(get=self._get, patch=self._patch)

    def _get(self, path: str, params: Any = None) -> dict[str, Any]:
        return {"message": self._message}

    def _patch(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.patches.append((path, params))
        return {"id": self._message.get("id") or 0}


class ChannelAliasOpsTests(unittest.TestCase):
    """The ops layer resolves a channel name for itself, not only because its callers do."""

    def test_a_move_named_with_the_retired_channel_finds_its_anchor(self) -> None:
        # The anchor lives in the live channel.  The guard that checks the anchor against
        # the requested channel has to name the same channel the lookup does, or it
        # refuses a move the caller asked for.
        agent = OpsAgent(
            {"AGENT_SYNC_CHANNEL": "other"},
            {"id": 7, "type": "stream", "display_recipient": "other", "subject": "old name"},
        )
        result = OPS.move_topic(
            agent, "agent-sync", "old name", new_topic="new name", message_id=7
        )
        self.assertEqual(result["id"], 7)
        self.assertEqual([path for path, _ in agent.patches], ["messages/7"])
        self.assertEqual(agent.patches[0][1]["topic"], "new name")

    def test_a_move_in_a_realm_that_still_calls_it_agent_sync_keeps_the_name(self) -> None:
        # The effective default is the same name there, so nothing changes.
        agent = OpsAgent(
            {"AGENT_SYNC_CHANNEL": "agent-sync"},
            {"id": 8, "type": "stream", "display_recipient": "agent-sync", "subject": "old name"},
        )
        result = OPS.move_topic(
            agent, "agent-sync", "old name", new_topic="new name", message_id=8
        )
        self.assertEqual(result["id"], 8)


class ChannelAliasAttachTests(AttachHarness):
    """The lease stores the channel the fleet reads, not the name the caller typed."""

    def test_attach_stores_the_resolved_channel(self) -> None:
        self.hook("session-start", {"source": "startup"})
        code, out, err = self.attach_run("--channel", "agent-sync", "--topic", "AFC work")
        self.assertEqual(code, 0, err)
        topics = L.load_lease(self.seat_paths(), LEASE)["topics"]
        self.assertEqual(
            topics,
            [{"channel": Z.DEFAULT_CHANNEL, "topic": "AFC work", "expires": None}],
        )

    def test_detach_removes_the_resolved_channel(self) -> None:
        self.hook("session-start", {"source": "startup"})
        self.attach_run("--channel", "agent-sync", "--topic", "AFC work")
        code, out, err = self.attach_run(
            "--channel", "agent-sync", "--topic", "AFC work", command="detach"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(L.load_lease(self.seat_paths(), LEASE)["topics"], [])


class ChannelAliasLeaseTests(DaemonHarness):
    """A lease written before the rename still watches the live channel."""

    def test_a_lease_that_names_the_retired_channel_watches_the_live_one(self) -> None:
        daemon = self.started(env=self.env(AGENT_SYNC_CHANNEL=None))
        self.write_lease("stale", topics=[("agent-sync", "fleet")])
        views = {v.lease_id: v.topics for v in daemon.lease_views(daemon.seats["CLAUDE"])}
        self.assertEqual(views["stale"], {L.topic_key(Z.DEFAULT_CHANNEL, "fleet")})
        self.assertNotIn(L.topic_key("agent-sync", "fleet"), views["stale"])

    def test_a_lease_in_a_realm_that_still_calls_it_agent_sync_keeps_watching_it(self) -> None:
        daemon = self.started()
        self.write_lease("stale", topics=[("agent-sync", "fleet")])
        views = {v.lease_id: v.topics for v in daemon.lease_views(daemon.seats["CLAUDE"])}
        self.assertEqual(views["stale"], {L.topic_key("agent-sync", "fleet")})


class ChannelAliasMcpTests(McpHarness):
    """The hosted tools resolve the alias too."""

    def test_a_tool_call_that_names_the_retired_channel_lands_in_the_live_one(self) -> None:
        session = self.session(self.mcp_env(AGENT_SYNC_CHANNEL="other"))
        self.assert_structured(
            "zulip_post",
            session.call("zulip_post", {"channel": "agent-sync", "topic": "t", "text": "hello"}),
        )
        posted = self.fake.requests_to("POST", "messages")[-1]
        self.assertEqual(posted.form["to"], "other")
        self.assertNotEqual(posted.form["to"], "agent-sync")


if __name__ == "__main__":
    unittest.main()
