"""The default channel has to follow the realm, and be settable when the realm moves it.

The realm renamed the fleet's coordination channel, and the runtime's default was a
hardcoded name that no longer existed, so every seat's post without ``--channel`` failed
with STREAM_DOES_NOT_EXIST.  These tests hold the default to the realm's live name and
hold the override in place, so the next rename is a configuration change and not a
fleet-wide outage.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from agent_sync import router as R
from agent_sync import zulip as Z
from agent_sync.mcp import tools as T
from agent_sync.mcp.tools import with_defaults

MCP_DIR = pathlib.Path(__file__).resolve().parents[1] / "mcp"


class DefaultChannelTests(unittest.TestCase):
    def test_the_default_channel_is_the_realm_s_live_coordination_channel(self) -> None:
        self.assertEqual(Z.DEFAULT_CHANNEL, "work-in-progress")

    def test_the_fleet_wake_lives_where_the_default_channel_points(self) -> None:
        # `--fleet` without `--channel` posts to the default channel, and the listener
        # classifies the wake by channel:  the two names must agree or the default-path
        # wake is refused and live wakes never classify.
        self.assertEqual(R.FLEET_WAKE_CHANNEL, Z.DEFAULT_CHANNEL)

    def test_the_channel_name_is_settable(self) -> None:
        self.assertEqual(Z.ENV_CHANNEL, "AGENT_SYNC_CHANNEL")

    def test_the_override_wins_and_the_default_is_the_fallback(self) -> None:
        self.assertEqual(
            Z.default_channel({Z.ENV_CHANNEL: "side-chats"}), "side-chats"
        )
        self.assertEqual(
            Z.default_channel({Z.ENV_CHANNEL: "  side-chats  "}), "side-chats"
        )
        # A blank override is no override.
        self.assertEqual(Z.default_channel({Z.ENV_CHANNEL: "   "}), Z.DEFAULT_CHANNEL)
        self.assertEqual(Z.default_channel({}), Z.DEFAULT_CHANNEL)
        self.assertEqual(Z.default_channel(None), Z.DEFAULT_CHANNEL)

    def test_the_override_is_read_from_the_environment_not_the_process(self) -> None:
        # The runtime hands the environment around as a mapping, so a caller that passes
        # one gets the channel it asked for without the process environment mattering.
        self.assertEqual(
            Z.default_channel({Z.ENV_CHANNEL: "other"}), "other"
        )

    def test_every_mcp_tool_that_defaults_a_channel_defaults_it_to_the_same_name(self) -> None:
        # `with_defaults()` fills a schema default into every MCP call that omits `channel`, so a
        # stale default in the shipped tool contract targets a stream name that no longer exists —
        # the outage this default was changed to end, still live on the MCP path.
        spec = json.loads((MCP_DIR / "tools.json").read_text())
        seen = 0
        for tool in spec["tools"]:
            channel = (tool.get("inputSchema") or {}).get("properties", {}).get("channel")
            if not isinstance(channel, dict) or "default" not in channel:
                continue
            seen += 1
            self.assertEqual(
                channel["default"],
                Z.DEFAULT_CHANNEL,
                "%s still defaults channel to %r" % (tool.get("name"), channel["default"]),
            )
        self.assertGreater(seen, 0, "no MCP channel default was checked")

    def test_the_mcp_layer_resolves_that_default_from_the_runtime_not_the_schema(self) -> None:
        # A schema default is applied before any handler runs, so a literal in tools.json would also
        # silently override AGENT_SYNC_CHANNEL.  mcp/tools.py _with_channel replaces the schema
        # default with the runtime's own answer, keeping one source of truth across MCP and CLI.
        schema = {"properties": {"channel": {"type": "string", "default": Z.DEFAULT_CHANNEL}}}
        with_default = with_defaults(schema, {})
        self.assertEqual(with_default["channel"], Z.DEFAULT_CHANNEL)
        # the same call under an override must not be served the schema literal
        self.assertEqual(
            T.default_channel_for({"AGENT_SYNC_CHANNEL": "side-chats"}, schema),
            "side-chats",
        )


if __name__ == "__main__":
    unittest.main()
