"""The default channel has to follow the realm, and be settable when the realm moves it.

The realm renamed the fleet's coordination channel, and the runtime's default was a
hardcoded name that no longer existed, so every seat's post without ``--channel`` failed
with STREAM_DOES_NOT_EXIST.  These tests hold the default to the realm's live name and
hold the override in place, so the next rename is a configuration change and not a
fleet-wide outage.
"""

from __future__ import annotations

import unittest

from agent_sync import zulip as Z


class DefaultChannelTests(unittest.TestCase):
    def test_the_default_channel_is_the_realm_s_live_coordination_channel(self) -> None:
        self.assertEqual(Z.DEFAULT_CHANNEL, "work-in-progress")

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


if __name__ == "__main__":
    unittest.main()
