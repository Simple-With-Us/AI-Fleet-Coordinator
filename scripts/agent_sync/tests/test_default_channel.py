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
import re
import unittest

from agent_sync import live as L
from agent_sync import router as R
from agent_sync import zulip as Z
from agent_sync.mcp import tools as T
from agent_sync.mcp.tools import with_defaults
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


def _without_comments(text: str, suffix: str) -> str:
    """The file's text with its comments taken out.

    A channel is written `#name`, and a comment marker is `#` followed by whitespace in Python
    and shell and `//` in JavaScript, so the two never look the same.  A `#!/usr/bin/env bash`
    line and a `#` inside a shell string are comments and go too.
    """
    if suffix in (".js", ".mjs"):
        return re.sub(r"//.*", "", text)
    if suffix in (".py", ".sh"):
        return re.sub(r"#(?![A-Za-z]).*", "", text)
    return text

MCP_DIR = pathlib.Path(__file__).resolve().parents[1] / "mcp"
AGENT_SYNC_DIR = pathlib.Path(__file__).resolve().parents[1]


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

    def test_the_presence_topics_follow_the_default_channel(self) -> None:
        # live.py sits on the hook fast path and may not import agent_sync.zulip, so it holds the
        # coordination channel as a literal.  This test is the drift guard:  when the realm renames
        # the channel again, that literal has to move with it, or the listener stops recognising
        # presence messages in the channel it actually lives in.
        presence_channels = {c for c, _ in L.DEFAULT_PRESENCE}
        self.assertIn(Z.DEFAULT_CHANNEL, presence_channels)
        # The realm kept the old name as a "DO NOT USE" tombstone stream, so a regression to it
        # would post into a stream nobody reads and fail silently rather than loudly.
        self.assertNotIn("agent-sync", presence_channels)

    def test_the_shipped_listener_configs_carry_the_same_presence_topics(self) -> None:
        # The server config and its test fixture are what the listener actually reads, so a
        # literal that only the in-code default moved on leaves the deployed listener deaf.
        shipped = (
            AGENT_SYNC_DIR / "server" / "listener.toml",
            AGENT_SYNC_DIR / "tests" / "fixtures" / "server-listener-live-oct9.toml",
        )
        for path in shipped:
            self.assertTrue(path.exists(), "%s is missing" % path)
            text = path.read_text()
            self.assertIn('"roll call"', text, "%s lost the roll call topic" % path)
            self.assertIn(
                Z.DEFAULT_CHANNEL,
                text,
                "%s still names the old channel for presence" % path,
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

    def test_the_cli_fleet_help_names_the_live_channel(self) -> None:
        # `--fleet` only works in the fleet coordination channel, and the help text is where a
        # seat learns that.  A help string still naming the old channel sends it to the tombstone.
        # The channel's display name is title-cased and its slug is not, so the check folds case.
        help_text = (AGENT_SYNC_DIR / "cli.py").read_text()
        self.assertIn("#%s" % Z.DEFAULT_CHANNEL, help_text.casefold())
        self.assertNotIn("#agent-sync", help_text.casefold())

    def test_no_channel_in_the_skills_pack_names_the_old_channel(self) -> None:
        # The pack is what every seat reads, and it is rendered from the canonical tree, so a
        # rename that moves the code and not the pack leaves every seat pointing at a stream
        # nobody reads.  A channel is always written in its hashtag form, so that form is the
        # signal;  the runtime's own source may still mention the old name in a comment that
        # explains the rename, and the pack may not.
        # There is no exemption for a retired seat:  their packs are rendered from the canonical
        # too, so a retired seat's pack may not name the old channel either.  A blanket skip
        # here would hide any stale consumer that landed under one of those folders.
        repo_root = AGENT_SYNC_DIR.parents[1]
        pack = repo_root / "docs" / "fleet-skills"
        offenders = []
        for path in sorted(pack.rglob("*.md")):
            if "#agent-sync" in path.read_text():
                offenders.append(str(path.relative_to(repo_root)))
        for path in sorted((repo_root / ".claude" / "skills").rglob("SKILL.md")):
            if "#agent-sync" in path.read_text():
                offenders.append(str(path.relative_to(repo_root)))
        self.assertEqual(
            offenders,
            [],
            "the skills pack still names the old channel:  %s" % ", ".join(offenders),
        )

    def test_every_generated_pack_matches_the_channel_of_its_canonical(self) -> None:
        # Rejecting the old channel is not the same as holding the new one:  a generator that
        # drops the live label, or a generated copy that has drifted from its source, passes
        # the check above and leaves every seat without a channel to post in.  So the generated
        # copy has to name exactly the channels its canonical source names, and the live label
        # has to be in the canonical and in the rendered tree.
        repo_root = AGENT_SYNC_DIR.parents[1]
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
        repo_root = AGENT_SYNC_DIR.parents[1]
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

    def test_no_onboarding_prompt_names_the_old_channel(self) -> None:
        # An onboarding prompt is the first thing a seat reads, so a prompt that names the old
        # channel sends that seat to the tombstone before it has read anything else.  These are
        # the live prompts;  the fleet guide's changelog rows are history and are not checked
        # here.
        repo_root = AGENT_SYNC_DIR.parents[1]
        prompts = (
            "docs/FX-ONBOARDING-PROMPT.md",
            "docs/INSTINCT-ONBOARDING-PROMPT.md",
            "docs/MUSE-ONBOARDING.md",
            "docs/ONBOARDING-NEW-AGENT.md",
            "docs/ONBOARDING-NEW-APP.md",
            "docs/OPENCODE-ONBOARDING-PROMPT.md",
        )
        for name in prompts:
            path = repo_root / name
            self.assertTrue(path.exists(), "%s is missing" % name)
            self.assertNotIn(
                "#agent-sync",
                path.read_text(),
                "%s still names the old channel" % name,
            )

    def test_no_live_source_names_the_old_channel(self) -> None:
        # The last three renames each left a live straggler:  a CLI help string, a tool error
        # message and a shell script's echo.  A channel reference is written `#name`, and a
        # comment marker is `#` followed by whitespace in Python and shell and `//` in
        # JavaScript, so the comments can go and the references can be read.  A comment that
        # explains the rename is allowed to name the old channel;  a string that tells a seat
        # where to post is not.
        repo_root = AGENT_SYNC_DIR.parents[1]
        offenders = []
        for base in ("scripts", "workers"):
            for path in sorted((repo_root / base).rglob("*")):
                if path.suffix not in (".py", ".js", ".mjs", ".sh"):
                    continue
                parts = path.relative_to(repo_root).parts
                if "node_modules" in parts or "dist" in parts or "tests" in parts:
                    continue
                if path.name.startswith("test_") or ".test." in path.name:
                    continue
                text = _without_comments(path.read_text(), path.suffix)
                if "#agent-sync" in text:
                    offenders.append(str(path.relative_to(repo_root)))
        self.assertEqual(
            offenders,
            [],
            "a live source still names the old channel:  %s" % ", ".join(offenders),
        )

    def test_the_bot_provisioning_script_subscribes_to_the_live_channel(self) -> None:
        # A newly provisioned bot that is subscribed to the old channel never sees fleet
        # coordination traffic, and the seat that waits for it waits forever.  The script takes
        # the channel from the runtime that owns it, so the check is that the runtime's own
        # default is in the list and that the old name is not.
        # The environment has to go in:  `default_channel()` without it never reads
        # AGENT_SYNC_CHANNEL, so a deployment that names its own channel would still provision
        # bots into the realm's default.
        repo_root = AGENT_SYNC_DIR.parents[1]
        text = (repo_root / "scripts" / "zulip_provision_bots.py").read_text()
        self.assertIn("default_channel(os.environ)", text)
        self.assertNotIn('"agent-sync"', text)


if __name__ == "__main__":
    unittest.main()
