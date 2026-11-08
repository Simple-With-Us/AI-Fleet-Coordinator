"""The default channel has to follow the realm, and be settable when the realm moves it.

A default that names a channel the realm does not have makes every seat's post without
``--channel`` fail with STREAM_DOES_NOT_EXIST.  These tests hold the default to the realm's
live name and hold the override in place, so a channel rename is a configuration change and
not a fleet-wide outage.
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

from simple_tools import live as L
from simple_tools import router as R
from simple_tools import zulip as Z
from simple_tools.mcp import tools as T
from simple_tools.mcp.tools import with_defaults
from fleet_skill_identity import catalog_seats

#: A channel is written in its hashtag form.  The pattern is deliberately loose:  it catches
#: every hashtag a file holds, and the checks compare one set with another rather than asking
#: whether a name is a real channel.
CHANNEL_TAG = re.compile(r"#[A-Za-z][A-Za-z0-9_-]*")


def _channels(text: str) -> set:
    """The channel-shaped names the text holds."""
    return set(CHANNEL_TAG.findall(text))


#: Every tree this repository renders a skill copy into.  The platform trees hold the skill
#: under a ``skills`` directory, and the repo root tree holds it directly, so the root tree is
#: not built the platform way:  that path resolves to nothing and the tree is then skipped
#: without saying so, which is the failure this list exists to catch.
RENDERED_TREES = (
    ".claude",
    ".codex",
    ".config/muse",
    ".cursor",
    ".fx",
    ".gemini",
    ".grok",
    ".clutch/dsh",
    ".minimax",
    "skills",
)


def _rendered_copy(repo_root: pathlib.Path, tree: str, name: str) -> pathlib.Path:
    """The rendered copy of a skill in one tree, or a path that does not exist."""
    if tree == "skills":
        return repo_root / "skills" / name / "SKILL.md"
    return repo_root / tree / "skills" / name / "SKILL.md"


MCP_DIR = pathlib.Path(__file__).resolve().parents[1] / "mcp"
SIMPLE_TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1]


class DefaultChannelTests(unittest.TestCase):
    def test_the_default_channel_is_the_realm_s_live_coordination_channel(self) -> None:
        self.assertEqual(Z.DEFAULT_CHANNEL, "work-in-progress")

    def test_the_fleet_wake_lives_where_the_default_channel_points(self) -> None:
        # `--fleet` without `--channel` posts to the default channel, and the listener
        # classifies the wake by channel:  the two names must agree or the default-path
        # wake is refused and live wakes never classify.
        self.assertEqual(R.FLEET_WAKE_CHANNEL, Z.DEFAULT_CHANNEL)

    def test_the_channel_name_is_settable(self) -> None:
        self.assertEqual(Z.ENV_CHANNEL, "SIMPLE_TOOLS_CHANNEL")

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

    def test_the_presence_topics_follow_the_default_channel(self) -> None:
        # live.py sits on the hook fast path and may not import simple_tools.zulip, so it holds the
        # coordination channel as a literal.  This test is the drift guard:  when the realm renames
        # the channel again, that literal has to move with it, or the listener stops recognising
        # presence messages in the channel it actually lives in.
        presence_channels = {c for c, _ in L.DEFAULT_PRESENCE}
        self.assertIn(Z.DEFAULT_CHANNEL, presence_channels)

    def test_the_shipped_listener_configs_carry_the_same_presence_topics(self) -> None:
        # The server config and its test fixture are what the listener actually reads, so a
        # literal that only the in-code default moved on leaves the deployed listener deaf.
        shipped = (
            SIMPLE_TOOLS_DIR / "server" / "listener.toml",
            SIMPLE_TOOLS_DIR / "tests" / "fixtures" / "server-listener-live-oct9.toml",
        )
        for path in shipped:
            self.assertTrue(path.exists(), "%s is missing" % path)
            text = path.read_text()
            self.assertIn('"roll call"', text, "%s lost the roll call topic" % path)
            self.assertIn(
                Z.DEFAULT_CHANNEL,
                text,
                "%s does not name the default channel for presence" % path,
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
        # silently override SIMPLE_TOOLS_CHANNEL.  mcp/tools.py _with_channel replaces the schema
        # default with the runtime's own answer, keeping one source of truth across MCP and CLI.
        schema = {"properties": {"channel": {"type": "string", "default": Z.DEFAULT_CHANNEL}}}
        with_default = with_defaults(schema, {})
        self.assertEqual(with_default["channel"], Z.DEFAULT_CHANNEL)
        # the same call under an override must not be served the schema literal
        self.assertEqual(
            T.default_channel_for({"SIMPLE_TOOLS_CHANNEL": "side-chats"}, schema),
            "side-chats",
        )

    def test_the_cli_fleet_help_names_the_live_channel(self) -> None:
        # `--fleet` only works in the fleet coordination channel, and the help text is where a
        # seat learns that.
        # The channel's display name is title-cased and its slug is not, so the check folds case.
        help_text = (SIMPLE_TOOLS_DIR / "cli.py").read_text()
        self.assertIn("#%s" % Z.DEFAULT_CHANNEL, help_text.casefold())

    def test_every_generated_pack_matches_the_channel_of_its_canonical(self) -> None:
        # A generator that drops the live label, or a generated copy that has drifted from its
        # source, leaves every seat without a channel to post in.  So the generated
        # copy has to name exactly the channels its canonical source names, and the live label
        # has to be in the canonical and in the rendered tree.
        repo_root = SIMPLE_TOOLS_DIR.parents[1]
        pack = repo_root / "docs" / "fleet-skills"
        live = "#%s" % Z.DEFAULT_CHANNEL.title()
        drifted = []
        for canonical in sorted(pack.glob("*/SKILL.md")):
            name = canonical.parent.name
            source_channels = _channels(canonical.read_text())
            for seat_dir in sorted((pack / "by-seat").iterdir()):
                generated = seat_dir / name / "SKILL.md"
                if not generated.exists():
                    continue
                # Both directions:  a generated pack that names a channel its canonical does not
                # is stale, and one that omits a channel its canonical names has lost it.  The
                # extra-only check let the second through, so a renamed canonical channel could
                # vanish from every seat's copy while the test stayed green (Kody review on
                # AFC-473).
                generated_channels = _channels(generated.read_text())
                extra = generated_channels - source_channels
                missing = source_channels - generated_channels
                if extra or missing:
                    drifted.append(
                        "%s/%s does not match its canonical:  extra %s, missing %s"
                        % (
                            seat_dir.name,
                            name,
                            ", ".join(sorted(extra)) or "none",
                            ", ".join(sorted(missing)) or "none",
                        )
                    )
        self.assertEqual(drifted, [], ";  ".join(drifted))
        # The live channel has to be in the canonical, or nothing renders it.
        self.assertIn(
            live,
            (pack / "session-start" / "SKILL.md").read_text(),
            "the canonical session-start pack does not name the live channel",
        )
        # Every tree a seat loads, not just the ones the generator happens to write first:  the
        # muse, the dsh and the plain skills trees are loaded by seats too, so a rename that
        # missed one of them would leave that seat without a channel.
        for tree in RENDERED_TREES:
            path = _rendered_copy(repo_root, tree, "session-start")
            if path.exists():
                self.assertIn(live, path.read_text(), "%s does not name the live channel" % tree)

    def test_every_rendered_tree_matches_the_channel_of_its_canonical(self) -> None:
        # The by-seat copies are checked against their canonical above.  The platform trees and
        # the repo root tree carry the same content, so the same equality has to hold for them:
        # a rendered copy that lost a channel its canonical names leaves the seat that reads it
        # without a channel to post in, and the suite stayed green while it happened.
        repo_root = SIMPLE_TOOLS_DIR.parents[1]
        pack = repo_root / "docs" / "fleet-skills"
        drifted = []
        compared = {tree: 0 for tree in RENDERED_TREES}
        for canonical in sorted(pack.glob("*/SKILL.md")):
            name = canonical.parent.name
            source_channels = _channels(canonical.read_text())
            for tree in RENDERED_TREES:
                rendered = _rendered_copy(repo_root, tree, name)
                if not rendered.exists():
                    continue
                compared[tree] += 1
                rendered_channels = _channels(rendered.read_text())
                extra = rendered_channels - source_channels
                missing = source_channels - rendered_channels
                if extra or missing:
                    drifted.append(
                        "%s/%s does not match its canonical:  extra %s, missing %s"
                        % (
                            tree,
                            name,
                            ", ".join(sorted(extra)) or "none",
                            ", ".join(sorted(missing)) or "none",
                        )
                    )
        # A tree whose path resolves to nothing would compare zero copies and still pass, so
        # every tree has to contribute:  that is the check that catches a tree the path shape
        # silently skipped.
        skipped = sorted(tree for tree, count in compared.items() if not count)
        self.assertEqual(skipped, [], "no rendered copy was compared for:  %s" % ", ".join(skipped))
        self.assertEqual(drifted, [], ";  ".join(drifted))

    def test_the_bot_provisioning_script_subscribes_to_the_live_channel(self) -> None:
        # A newly provisioned bot that is not subscribed to the default channel never sees
        # fleet coordination traffic, and the seat that waits for it waits forever.  The script
        # takes the channel from the runtime that owns it, so the check is that the runtime's
        # own default is in the list.
        # The environment has to go in:  `default_channel()` without it never reads
        # SIMPLE_TOOLS_CHANNEL, so a deployment that names its own channel would still provision
        # bots into the realm's default.
        repo_root = SIMPLE_TOOLS_DIR.parents[1]
        text = (repo_root / "scripts" / "zulip_provision_bots.py").read_text()
        self.assertIn("default_channel(os.environ)", text)


if __name__ == "__main__":
    unittest.main()
