"""How a channel name given to the CLI is read.

A leading `#` and surrounding spaces are dropped, no name means the effective default
channel, and `--fleet` is allowed in exactly one channel.
"""

from __future__ import annotations

import unittest

from simple_tools import zulip as Z
from simple_tools.tests.harness import Harness


class ResolveChannelTests(unittest.TestCase):
    def test_a_channel_is_left_alone(self) -> None:
        for name in ("side-chats", "alerts", "builds"):
            self.assertEqual(Z.resolve_channel(name), name)

    def test_a_hash_and_spaces_are_dropped(self) -> None:
        for name in ("#side-chats", " side-chats ", " # side-chats"):
            self.assertEqual(Z.resolve_channel(name), "side-chats", name)

    def test_no_name_is_the_default_channel(self) -> None:
        self.assertEqual(Z.resolve_channel(""), Z.DEFAULT_CHANNEL)
        self.assertEqual(Z.resolve_channel(None), Z.DEFAULT_CHANNEL)
        self.assertEqual(
            Z.resolve_channel("  ", {"SIMPLE_TOOLS_CHANNEL": "side-chats"}), "side-chats"
        )


class FleetWakeChannelTests(Harness):
    def last_post(self):
        return self.fake.requests_to("POST", "messages")[-1]

    def test_the_fleet_wake_still_refuses_another_channel(self) -> None:
        result = self.run_cli(
            "post",
            "--channel",
            "other",
            "--topic",
            "fleet",
            "--fleet",
            "wake up",
            env=self.env(SIMPLE_TOOLS_CHANNEL=None),
        )
        self.assertEqual(result.code, 2)
        self.assertIn("only works in", result.err)


if __name__ == "__main__":
    unittest.main()
