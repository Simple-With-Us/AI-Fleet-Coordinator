"""Tests for the L0 check modules of ``fleet_config``.

Every test builds its fixtures in a temporary home directory.  The real home is never
read or written:  a check that escaped its context would show up as a MISSING result
against a fixture that is right there in the temporary tree.
"""

from __future__ import annotations

import builtins
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from typing import Any

from fleet_config.checks import CheckContext, default_registry, sha256_text
from fleet_config.checks_hooks import (
    check_hook_registered,
    hook_entries,
    parse_toml,
    read_config_document,
)
from fleet_config.checks_instructions import (
    block_body_hash,
    check_file_hash,
    check_marked_block_hash,
    marked_block_body,
)
from fleet_config.checks_mcp import check_mcp_servers_registered
from fleet_config.checks_permissions import check_credential_stat, check_permissions_match
from fleet_config.checks_skills import check_skills_installed
from fleet_config.checks_subagents import check_subagents_installed
from fleet_config.contract import is_failing, result_to_dict
from fleet_config.types import (
    CheckSpec,
    CheckState,
    CheckType,
    ConfigKind,
    ExpectedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

MARKED_BLOCK = "# fleet:begin fleet-lane-map\nthe lane map rule\n# fleet:end fleet-lane-map\n"
MARKED_BODY = "the lane map rule\n"


def make_rule(
    check_type: CheckType,
    target: str,
    detail: str = "",
    expected: ExpectedState | None = None,
    kind: ConfigKind = ConfigKind.INSTRUCTIONS,
    seat: str = "CLAUDE",
    ttl_seconds: int = 900,
) -> Rule:
    """One rule for a test, with the fields a check reads."""
    return Rule(
        id="rule-test",
        kind=kind,
        platforms=("claude",),
        check=CheckSpec(type=check_type, target=target, detail=detail),
        expected=expected if expected is not None else ExpectedState(description="a test rule"),
        proof_level=ProofLevel.L0,
        owner_seat=seat,
        autofix_allowed=False,
        seat=seat,
        ttl_seconds=ttl_seconds,
    )


def make_context(home: str) -> CheckContext:
    """A context pinned to one home and one clock."""
    return CheckContext(
        home=home,
        now=datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC),
    )


class TempHome(unittest.TestCase):
    """A test case with a temporary home directory and a context pinned to it."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.home = self._temp.name
        self.ctx = make_context(self.home)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def write(self, relative: str, text: str) -> str:
        """Write a fixture file under the temporary home and return its path."""
        path = os.path.join(self.home, relative)
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_json(self, relative: str, data: Any) -> str:
        """Write a fixture JSON file under the temporary home."""
        return self.write(relative, json.dumps(data, indent=2))


class InstructionsTests(TempHome):
    """The marked block hash and the whole file hash."""

    def test_marked_block_hash_passes_on_a_matching_block(self):
        self.write(".claude/CLAUDE.md", MARKED_BLOCK)
        rule = make_rule(
            CheckType.MARKED_BLOCK_HASH,
            "~/.claude/CLAUDE.md",
            detail="fleet-lane-map",
            expected=ExpectedState(description="d", sha256=sha256_text(MARKED_BODY)),
        )
        result = check_marked_block_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.reason_code, ReasonCode.OK)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertEqual(result.observed.sha256, sha256_text(MARKED_BODY))

    def test_marked_block_hash_drifts_on_a_different_body(self):
        self.write(
            ".claude/CLAUDE.md",
            "# fleet:begin fleet-lane-map\nsomething else\n# fleet:end fleet-lane-map\n",
        )
        rule = make_rule(
            CheckType.MARKED_BLOCK_HASH,
            "~/.claude/CLAUDE.md",
            detail="fleet-lane-map",
            expected=ExpectedState(description="d", sha256=sha256_text(MARKED_BODY)),
        )
        result = check_marked_block_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.DRIFT)
        self.assertTrue(is_failing(result))
        self.assertIsNone(result.proof_level_achieved)

    def test_marked_block_hash_is_missing_without_the_block(self):
        self.write(".claude/CLAUDE.md", "no markers here\n")
        rule = make_rule(
            CheckType.MARKED_BLOCK_HASH,
            "~/.claude/CLAUDE.md",
            detail="fleet-lane-map",
            expected=ExpectedState(description="d", sha256=sha256_text(MARKED_BODY)),
        )
        result = check_marked_block_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)
        self.assertIs(result.reason_code, ReasonCode.MISSING)

    def test_marked_block_hash_is_missing_without_the_file(self):
        rule = make_rule(
            CheckType.MARKED_BLOCK_HASH,
            "~/.claude/CLAUDE.md",
            detail="fleet-lane-map",
            expected=ExpectedState(description="d", sha256=sha256_text(MARKED_BODY)),
        )
        result = check_marked_block_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)

    def test_marked_block_hash_is_unparseable_when_the_markers_are_broken(self):
        self.write(".claude/CLAUDE.md", "# fleet:begin fleet-lane-map\nbody without an end\n")
        rule = make_rule(
            CheckType.MARKED_BLOCK_HASH,
            "~/.claude/CLAUDE.md",
            detail="fleet-lane-map",
            expected=ExpectedState(description="d", sha256=sha256_text(MARKED_BODY)),
        )
        result = check_marked_block_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.UNPARSEABLE)
        self.assertTrue(is_failing(result))

    def test_marked_block_hash_without_an_expected_hash_is_a_presence_check(self):
        self.write(".claude/CLAUDE.md", MARKED_BLOCK)
        rule = make_rule(
            CheckType.MARKED_BLOCK_HASH,
            "~/.claude/CLAUDE.md",
            detail="fleet-lane-map",
            expected=ExpectedState(description="d"),
        )
        result = check_marked_block_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertEqual(result.observed.sha256, sha256_text(MARKED_BODY))

    def test_marked_block_hash_reads_the_html_marker_family(self):
        self.write(
            ".cursor/rules/fleet-lane-map.mdc",
            "<!-- fleet-lane-map:begin v2 -->\nthe lane map rule\n<!-- fleet-lane-map:end -->\n",
        )
        rule = make_rule(
            CheckType.MARKED_BLOCK_HASH,
            "~/.cursor/rules/fleet-lane-map.mdc",
            detail="fleet-lane-map",
            expected=ExpectedState(description="d", sha256=sha256_text(MARKED_BODY)),
        )
        result = check_marked_block_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertEqual(result.observed.sha256, sha256_text(MARKED_BODY))

    def test_file_hash_passes_drifts_and_reports_a_missing_file(self):
        path = self.write(".claude/CLAUDE.md", "the whole file\n")
        digest = sha256_text("the whole file\n")
        expected = ExpectedState(description="d", sha256=digest)
        rule = make_rule(CheckType.FILE_HASH, "~/.claude/CLAUDE.md", expected=expected)
        result = check_file_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertEqual(result.observed.sha256, digest)

        self.write(".claude/CLAUDE.md", "a different file\n")
        result = check_file_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.DRIFT)

        os.remove(path)
        result = check_file_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)

    def test_file_hash_without_an_expected_hash_is_a_presence_check(self):
        self.write(".claude/CLAUDE.md", "the whole file\n")
        rule = make_rule(CheckType.FILE_HASH, "~/.claude/CLAUDE.md")
        result = check_file_hash(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertEqual(result.observed.sha256, sha256_text("the whole file\n"))

    def test_the_target_is_expanded_against_the_context_home(self):
        self.write(".codex/AGENTS.md", "body\n")
        rule = make_rule(
            CheckType.FILE_HASH,
            "$HOME/.codex/AGENTS.md",
            expected=ExpectedState(description="d", sha256=sha256_text("body\n")),
        )
        result = check_file_hash(rule, "codex", self.ctx)
        self.assertIs(result.state, CheckState.PASS)

        rule = make_rule(
            CheckType.FILE_HASH,
            "~/.codex/AGENTS.md",
            expected=ExpectedState(description="d", sha256=sha256_text("body\n")),
        )
        result = check_file_hash(rule, "codex", self.ctx)
        self.assertIs(result.state, CheckState.PASS)

    def test_the_marked_block_extraction_matches_fleet_lanes(self):
        """The parity the design doc pins:  this package and fleet_lanes agree."""
        from fleet_lanes import marked_block as mb

        text = "before\n# fleet:begin fleet-lane-map\nline one\nline two\n# fleet:end fleet-lane-map\nafter\n"
        mine = marked_block_body(text, "fleet-lane-map")
        theirs = mb.block_body(text, "fleet-lane-map")
        self.assertEqual(mine, theirs)
        self.assertEqual(block_body_hash(mine), sha256_text("line one\nline two\n"))


class HooksTests(TempHome):
    """The hook registrations of a harness settings file."""

    def claude_settings(self) -> dict[str, Any]:
        """A Claude shaped settings file with the fleet hook."""
        return {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/Users/jay/apps/lane-tools/lane-guard-hook --format claude",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }

    def test_hook_registered_passes_on_the_claude_shape(self):
        self.write_json(".claude/settings.json", self.claude_settings())
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.claude/settings.json",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("lane-guard",), timeout_seconds=5),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertEqual(result.observed.names, ("lane-guard",))
        self.assertEqual(result.observed.timeout_seconds, 5)

    def test_hook_registered_is_missing_without_the_file(self):
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.claude/settings.json",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("lane-guard",)),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)

    def test_hook_registered_is_missing_without_an_entry_for_the_event(self):
        self.write_json(".claude/settings.json", {"env": {"HOME": "/tmp"}})
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.claude/settings.json",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("lane-guard",)),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)

    def test_hook_registered_drifts_when_the_named_hook_is_not_there(self):
        self.write_json(".claude/settings.json", self.claude_settings())
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.claude/settings.json",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("fleet-guard",)),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.DRIFT)

    def test_hook_registered_drifts_on_a_timeout_mismatch(self):
        self.write_json(".claude/settings.json", self.claude_settings())
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.claude/settings.json",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("lane-guard",), timeout_seconds=10),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)

    def test_hook_registered_is_unparseable_on_malformed_json(self):
        self.write(".claude/settings.json", "{ this is not json ")
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.claude/settings.json",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("lane-guard",)),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.UNPARSEABLE)
        self.assertTrue(is_failing(result))

    def test_hook_registered_reads_the_cursor_shape(self):
        self.write_json(
            ".cursor/hooks.json",
            {
                "version": 1,
                "hooks": {
                    "beforeShellExecution": [
                        {"command": "/Users/jay/apps/lane-tools/lane-guard-hook", "timeout": 5}
                    ]
                },
            },
        )
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.cursor/hooks.json",
            detail="beforeShellExecution",
            expected=ExpectedState(description="d", names=("lane-guard",)),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "cursor", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertEqual(result.observed.names, ("lane-guard",))

    def test_hook_registered_reads_the_antigravity_named_group(self):
        self.write_json(
            ".gemini/config/hooks.json",
            {
                "lane-guard": {
                    "PreToolUse": [
                        {"matcher": "run_command", "command": "/x/lane-guard-hook", "timeout": 5}
                    ],
                    "enabled": True,
                }
            },
        )
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.gemini/config/hooks.json",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("lane-guard",)),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "antigravity", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertEqual(result.observed.names, ("lane-guard",))

    def test_hook_registered_reads_the_kimi_toml_shape(self):
        self.write(
            ".kimi-code/config.toml",
            '[[hooks]]\nevent = "PreToolUse"\nmatcher = "Bash"\n'
            'command = "/x/lane-guard-hook --format kimi"\ntimeout = 5\n',
        )
        rule = make_rule(
            CheckType.HOOK_REGISTERED,
            "~/.kimi-code/config.toml",
            detail="PreToolUse",
            expected=ExpectedState(description="d", names=("lane-guard",), timeout_seconds=5),
            kind=ConfigKind.HOOKS,
        )
        result = check_hook_registered(rule, "kimi", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertEqual(result.observed.names, ("lane-guard",))
        self.assertEqual(result.observed.timeout_seconds, 5)

    def test_the_toml_subset_reader_reads_tables_and_arrays(self):
        text = (
            'title = "a"\n'
            "\n"
            "[mcp_servers.fleet-recall]\n"
            'command = "python3"\n'
            "args = [\n"
            '  "/Users/jay/apps/fleet-rag/fleet-recall-mcp.py",\n'
            "]\n"
            "enabled = true\n"
            "\n"
            "[[hooks]]\n"
            'event = "PreToolUse"\n'
            "timeout = 5\n"
        )
        document = parse_toml(text)
        self.assertEqual(document["title"], "a")
        self.assertEqual(document["mcp_servers"]["fleet-recall"]["command"], "python3")
        self.assertEqual(
            document["mcp_servers"]["fleet-recall"]["args"],
            ["/Users/jay/apps/fleet-rag/fleet-recall-mcp.py"],
        )
        self.assertIs(document["mcp_servers"]["fleet-recall"]["enabled"], True)
        self.assertEqual(document["hooks"][0]["event"], "PreToolUse")
        self.assertEqual(document["hooks"][0]["timeout"], 5)

    def test_the_event_name_does_not_depend_on_case_or_underscores(self):
        entries = hook_entries(
            {"hooks": {"pre_tool_use": [{"command": "/x/lane-guard-hook"}]}}, "PreToolUse"
        )
        self.assertEqual([name for name, _timeout in entries], ["lane-guard-hook"])


class McpTests(TempHome):
    """The MCP server names of a platform config file."""

    def test_mcp_passes_on_the_flat_json_shape(self):
        self.write_json(".claude.json", {"mcpServers": {"fleet-recall": {"command": "python3"}}})
        rule = make_rule(
            CheckType.MCP_SERVERS_REGISTERED,
            "~/.claude.json",
            expected=ExpectedState(description="d", names=("fleet-recall",)),
            kind=ConfigKind.MCP,
        )
        result = check_mcp_servers_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertEqual(result.observed.names, ("fleet-recall",))

    def test_mcp_passes_on_the_opencode_shape(self):
        self.write_json(
            ".config/opencode/opencode.json",
            {"mcp": {"servers": {"fleet-recall": {"type": "local"}}}},
        )
        rule = make_rule(
            CheckType.MCP_SERVERS_REGISTERED,
            "~/.config/opencode/opencode.json",
            expected=ExpectedState(description="d", names=("fleet-recall",)),
            kind=ConfigKind.MCP,
        )
        result = check_mcp_servers_registered(rule, "opencode", self.ctx)
        self.assertIs(result.state, CheckState.PASS)

    def test_mcp_passes_on_the_toml_shape(self):
        self.write(
            ".codex/config.toml",
            '[mcp_servers.fleet-recall]\ncommand = "python3"\n',
        )
        rule = make_rule(
            CheckType.MCP_SERVERS_REGISTERED,
            "~/.codex/config.toml",
            expected=ExpectedState(description="d", names=("fleet-recall",)),
            kind=ConfigKind.MCP,
        )
        result = check_mcp_servers_registered(rule, "codex", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertEqual(result.observed.names, ("fleet-recall",))

    def test_mcp_is_missing_without_the_file(self):
        rule = make_rule(
            CheckType.MCP_SERVERS_REGISTERED,
            "~/.claude.json",
            expected=ExpectedState(description="d", names=("fleet-recall",)),
            kind=ConfigKind.MCP,
        )
        result = check_mcp_servers_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)

    def test_mcp_drifts_when_an_expected_server_is_not_registered(self):
        self.write_json(".claude.json", {"mcpServers": {"fleet-recall": {"command": "python3"}}})
        rule = make_rule(
            CheckType.MCP_SERVERS_REGISTERED,
            "~/.claude.json",
            expected=ExpectedState(description="d", names=("fleet-recall", "agent-sync")),
            kind=ConfigKind.MCP,
        )
        result = check_mcp_servers_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertEqual(result.observed.names, ("fleet-recall",))

    def test_mcp_is_unparseable_on_malformed_json(self):
        self.write(".claude.json", "{ nope ")
        rule = make_rule(
            CheckType.MCP_SERVERS_REGISTERED,
            "~/.claude.json",
            expected=ExpectedState(description="d", names=("fleet-recall",)),
            kind=ConfigKind.MCP,
        )
        result = check_mcp_servers_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.UNPARSEABLE)

    def test_mcp_is_unparseable_when_the_file_holds_no_mcp_section(self):
        self.write_json(".claude.json", {"projects": {}})
        rule = make_rule(
            CheckType.MCP_SERVERS_REGISTERED,
            "~/.claude.json",
            expected=ExpectedState(description="d", names=("fleet-recall",)),
            kind=ConfigKind.MCP,
        )
        result = check_mcp_servers_registered(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.UNPARSEABLE)


class SkillsTests(TempHome):
    """The skills installed in a platform's skills directory."""

    def test_skills_pass_missing_and_drift(self):
        os.makedirs(os.path.join(self.home, ".claude", "skills", "fleet-mode"))
        rule = make_rule(
            CheckType.SKILLS_INSTALLED,
            "~/.claude/skills",
            expected=ExpectedState(description="d", names=("fleet-mode",)),
            kind=ConfigKind.SKILLS,
        )
        result = check_skills_installed(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertEqual(result.observed.names, ("fleet-mode",))

        result = check_skills_installed(
            make_rule(
                CheckType.SKILLS_INSTALLED,
                "~/.claude/skills",
                expected=ExpectedState(description="d", names=("sentence-gap",)),
                kind=ConfigKind.SKILLS,
            ),
            "claude",
            self.ctx,
        )
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.DRIFT)

        result = check_skills_installed(
            make_rule(
                CheckType.SKILLS_INSTALLED,
                "~/.codex/skills",
                expected=ExpectedState(description="d", names=("fleet-mode",)),
                kind=ConfigKind.SKILLS,
            ),
            "codex",
            self.ctx,
        )
        self.assertIs(result.state, CheckState.MISSING)

    def test_a_skill_may_be_a_file(self):
        self.write(".claude/skills/fleet-mode.md", "a skill")
        rule = make_rule(
            CheckType.SKILLS_INSTALLED,
            "~/.claude/skills",
            expected=ExpectedState(description="d", names=("fleet-mode",)),
            kind=ConfigKind.SKILLS,
        )
        result = check_skills_installed(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)


class SubagentsTests(TempHome):
    """The sub-agent definitions installed for a platform."""

    def test_subagents_pass_missing_and_drift(self):
        self.write(".claude/agents/code-scout.md", "an agent")
        rule = make_rule(
            CheckType.SUBAGENTS_INSTALLED,
            "~/.claude/agents",
            expected=ExpectedState(description="d", names=("code-scout",)),
            kind=ConfigKind.SUBAGENTS,
        )
        result = check_subagents_installed(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)

        result = check_subagents_installed(
            make_rule(
                CheckType.SUBAGENTS_INSTALLED,
                "~/.claude/agents",
                expected=ExpectedState(description="d", names=("docs-scribe",)),
                kind=ConfigKind.SUBAGENTS,
            ),
            "claude",
            self.ctx,
        )
        self.assertIs(result.state, CheckState.DRIFT)

        result = check_subagents_installed(
            make_rule(
                CheckType.SUBAGENTS_INSTALLED,
                "~/.codex/agents",
                expected=ExpectedState(description="d", names=("code-scout",)),
                kind=ConfigKind.SUBAGENTS,
            ),
            "codex",
            self.ctx,
        )
        self.assertIs(result.state, CheckState.MISSING)


class PermissionsTests(TempHome):
    """The permission identifiers of a settings file."""

    def settings(self) -> dict[str, Any]:
        """A settings file with the fleet's permissions."""
        return {
            "permissions": {
                "allow": ["Bash(git:*)", "Read(//Users/jay/apps/**)"],
                "deny": ["Bash(rm:*)"],
                "ask": [],
            }
        }

    def test_permissions_pass_on_an_exact_match(self):
        self.write_json(".claude/settings.json", self.settings())
        rule = make_rule(
            CheckType.PERMISSIONS_MATCH,
            "~/.claude/settings.json",
            expected=ExpectedState(
                description="d",
                names=("Bash(git:*)", "Bash(rm:*)", "Read(//Users/jay/apps/**)"),
                mode="0600",
            ),
            kind=ConfigKind.PERMISSIONS,
        )
        os.chmod(os.path.join(self.home, ".claude", "settings.json"), 0o600)
        result = check_permissions_match(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertEqual(result.observed.mode, "0600")

    def test_permissions_drift_on_an_extra_permission(self):
        self.write_json(".claude/settings.json", self.settings())
        rule = make_rule(
            CheckType.PERMISSIONS_MATCH,
            "~/.claude/settings.json",
            expected=ExpectedState(description="d", names=("Bash(git:*)",)),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_permissions_match(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.DRIFT)

    def test_permissions_drift_on_a_missing_permission(self):
        self.write_json(".claude/settings.json", self.settings())
        rule = make_rule(
            CheckType.PERMISSIONS_MATCH,
            "~/.claude/settings.json",
            expected=ExpectedState(description="d", names=("Bash(git:*)", "Bash(rm:*)", "Write")),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_permissions_match(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)

    def test_permissions_drift_on_a_mode_mismatch(self):
        self.write_json(".claude/settings.json", self.settings())
        os.chmod(os.path.join(self.home, ".claude", "settings.json"), 0o644)
        rule = make_rule(
            CheckType.PERMISSIONS_MATCH,
            "~/.claude/settings.json",
            expected=ExpectedState(
                description="d",
                names=("Bash(git:*)", "Bash(rm:*)", "Read(//Users/jay/apps/**)"),
                mode="0600",
            ),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_permissions_match(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIn("mode", result.reason)

    def test_a_rule_that_names_no_permission_checks_the_mode_alone(self):
        self.write_json(".claude/settings.json", self.settings())
        os.chmod(os.path.join(self.home, ".claude", "settings.json"), 0o600)
        rule = make_rule(
            CheckType.PERMISSIONS_MATCH,
            "~/.claude/settings.json",
            expected=ExpectedState(description="d", mode="0600"),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_permissions_match(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)

    def test_permissions_are_missing_without_the_file(self):
        rule = make_rule(
            CheckType.PERMISSIONS_MATCH,
            "~/.claude/settings.json",
            expected=ExpectedState(description="d", names=("Bash(git:*)",)),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_permissions_match(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)

    def test_permissions_are_unparseable_on_malformed_json(self):
        self.write(".claude/settings.json", "{ nope ")
        rule = make_rule(
            CheckType.PERMISSIONS_MATCH,
            "~/.claude/settings.json",
            expected=ExpectedState(description="d", names=("Bash(git:*)",)),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_permissions_match(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.UNPARSEABLE)


class CredentialTests(TempHome):
    """The credential check, which is stat only."""

    SENTINEL = "sentinel-secret-value-do-not-leak"

    def test_the_credential_is_checked_by_stat_alone(self):
        path = self.write(".secrets/Zulip/Claude-zuliprc", self.SENTINEL)
        os.chmod(path, 0o600)
        rule = make_rule(
            CheckType.CREDENTIAL_STAT,
            "~/.secrets/Zulip/Claude-zuliprc",
            detail="ZULIP_CLAUDE",
            expected=ExpectedState(description="d", present=True, mode="0600"),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_credential_stat(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L0)
        self.assertIs(result.observed.present, True)
        self.assertEqual(result.observed.mode, "0600")
        self.assertEqual(result.observed.size_bytes, len(self.SENTINEL))
        self.assertTrue(result.observed.mtime.endswith("Z"))
        self.assertNotIn(self.SENTINEL, result.reason)
        self.assertNotIn(self.SENTINEL, json.dumps(result_to_dict(result)))

    def test_the_credential_file_is_never_opened(self):
        path = self.write(".secrets/Zulip/Claude-zuliprc", self.SENTINEL)
        os.chmod(path, 0o600)
        rule = make_rule(
            CheckType.CREDENTIAL_STAT,
            "~/.secrets/Zulip/Claude-zuliprc",
            detail="ZULIP_CLAUDE",
            expected=ExpectedState(description="d", present=True, mode="0600"),
            kind=ConfigKind.PERMISSIONS,
        )
        opened: list = []
        real_open = builtins.open

        def refuse(*args: Any, **kwargs: Any) -> Any:
            opened.append(args)
            raise AssertionError("the credential check opened a file")

        builtins.open = refuse  # type: ignore[assignment]
        try:
            result = check_credential_stat(rule, "claude", self.ctx)
        finally:
            builtins.open = real_open  # type: ignore[assignment]
        self.assertEqual(opened, [])
        self.assertIs(result.state, CheckState.PASS)

    def test_the_credential_is_missing_without_the_file(self):
        rule = make_rule(
            CheckType.CREDENTIAL_STAT,
            "~/.secrets/Zulip/Claude-zuliprc",
            detail="ZULIP_CLAUDE",
            expected=ExpectedState(description="d", present=True, mode="0600"),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_credential_stat(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.MISSING)
        self.assertIs(result.reason_code, ReasonCode.MISSING)

    def test_the_credential_drifts_on_a_wrong_mode(self):
        path = self.write(".secrets/Zulip/Claude-zuliprc", self.SENTINEL)
        os.chmod(path, 0o644)
        rule = make_rule(
            CheckType.CREDENTIAL_STAT,
            "~/.secrets/Zulip/Claude-zuliprc",
            detail="ZULIP_CLAUDE",
            expected=ExpectedState(description="d", present=True, mode="0600"),
            kind=ConfigKind.PERMISSIONS,
        )
        result = check_credential_stat(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertEqual(result.observed.mode, "0644")
        self.assertNotIn(self.SENTINEL, result.reason)


class RegistryTests(unittest.TestCase):
    """The dispatch table holds every check type."""

    def test_every_check_type_is_registered(self):
        implemented = default_registry().implemented()
        expected = tuple(sorted(check.value for check in CheckType))
        self.assertEqual(implemented, expected)
        self.assertEqual(len(implemented), 8)


class DocumentReaderTests(TempHome):
    """The shared JSON and TOML reader."""

    def test_an_unknown_extension_falls_back_to_json_then_toml(self):
        path = self.write("config.json", '{"a": 1}')
        self.assertEqual(read_config_document(path), {"a": 1})
        path = self.write("config.conf", "a = 1\n")
        self.assertEqual(read_config_document(path), {"a": 1})

    def test_a_file_that_is_neither_json_nor_toml_raises(self):
        path = self.write("config.conf", "- insert:\n  - package: nope\n")
        with self.assertRaises(ValueError):
            read_config_document(path)


if __name__ == "__main__":
    unittest.main()
