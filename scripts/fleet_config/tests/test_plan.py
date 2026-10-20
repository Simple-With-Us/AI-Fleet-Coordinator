"""The plan:  what an apply would change,  which installer would do it,  and nothing else.

The plan is read-only,  so the strongest thing a test can say about it is that it changed
nothing:  the tree is snapshotted by mtime and by hash before and after,  and the two
snapshots must be identical.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fleet_config import plan as plan_module
from fleet_config.checks import build_result
from fleet_config.types import (
    CheckState,
    CheckType,
    ConfigKind,
    ObservedState,
    ReasonCode,
)

from ._fake import FleetTestCase, make_manifest, make_rule, snapshot_tree

MOMENT = datetime(2026, 10, 19, 14, 30, 0, tzinfo=timezone.utc)


def a_result(rule, state=CheckState.DRIFT, reason="it differs"):
    """A result for a rule,  built without a check module."""
    from fleet_config.checks import CheckContext

    ctx = CheckContext(home="/tmp/fleet-config-home", now=MOMENT)
    code = {
        CheckState.DRIFT: ReasonCode.DRIFT,
        CheckState.MISSING: ReasonCode.MISSING,
        CheckState.PASS: ReasonCode.OK,
        CheckState.UNKNOWN: ReasonCode.ERROR,
    }[state]
    return build_result(
        rule,
        rule.platforms[0],
        state,
        reason,
        code,
        ObservedState(),
        ctx,
    )


class PlanTests(FleetTestCase):
    def test_a_drifting_rule_produces_one_entry_that_names_everything(self):
        rule = make_rule(
            "mm-hooks-lane-guard",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
        )
        manifest = make_manifest([rule])
        built = plan_module.build_plan(
            manifest, [a_result(rule)], seat="MM", kind=ConfigKind.HOOKS, now=MOMENT
        )
        self.assertEqual(len(built.entries), 1)
        entry = built.entries[0]
        self.assertEqual(entry.rule_id, "mm-hooks-lane-guard")
        self.assertEqual(entry.seat, "MM")
        self.assertEqual(entry.platform, "minimax")
        self.assertEqual(entry.kind, ConfigKind.HOOKS)
        self.assertEqual(entry.state, CheckState.DRIFT)
        self.assertIn("does not match", entry.change)
        self.assertIsNotNone(entry.installer)
        self.assertIn("fleet_lanes.install_tools", entry.command())
        self.assertIn(".minimax/plugins/fleet-guard/hooks/hooks.json", entry.files)

    def test_a_passing_rule_produces_no_entry(self):
        rule = make_rule("mm-instructions", ConfigKind.INSTRUCTIONS)
        manifest = make_manifest([rule])
        built = plan_module.build_plan(manifest, [a_result(rule, CheckState.PASS)], now=MOMENT)
        self.assertEqual(built.entries, ())
        self.assertIn("nothing to change", built.render())

    def test_a_rule_that_may_not_be_fixed_is_shown_but_not_auto_fixable(self):
        rule = make_rule(
            "mm-instructions",
            ConfigKind.INSTRUCTIONS,
            check_type=CheckType.MARKED_BLOCK_HASH,
            target="~/.minimax/memory/user.md",
            autofix=False,
        )
        manifest = make_manifest([rule])
        built = plan_module.build_plan(manifest, [a_result(rule, CheckState.DRIFT)], now=MOMENT)
        entry = built.entries[0]
        self.assertFalse(entry.auto_fixable)
        self.assertFalse(entry.autofix_allowed)
        self.assertIn("does not allow an automatic fix", entry.blocked_reason)
        self.assertEqual(built.autofixable(), ())
        self.assertEqual(len(built.blocked()), 1)

    def test_the_skills_installer_is_never_named_because_it_always_writes(self):
        rule = make_rule(
            "mm-skills-fleet",
            ConfigKind.SKILLS,
            check_type=CheckType.SKILLS_INSTALLED,
            target="~/.minimax/skills",
        )
        manifest = make_manifest([rule])
        built = plan_module.build_plan(manifest, [a_result(rule, CheckState.MISSING)], now=MOMENT)
        entry = built.entries[0]
        self.assertIsNone(entry.installer)
        self.assertFalse(entry.auto_fixable)
        self.assertIn("install-fleet-skills.py", entry.blocked_reason)
        # It is never offered as a command:  the plan names no way to run it.
        self.assertNotIn("install-fleet-skills.py", entry.command())
        self.assertEqual([item for item in built.entries if item.installer], [])

    def test_an_instructions_rule_that_is_not_the_marked_block_has_no_installer(self):
        rule = make_rule(
            "mm-instructions-file",
            ConfigKind.INSTRUCTIONS,
            check_type=CheckType.FILE_HASH,
        )
        manifest = make_manifest([rule])
        built = plan_module.build_plan(manifest, [a_result(rule, CheckState.DRIFT)], now=MOMENT)
        self.assertIsNone(built.entries[0].installer)
        self.assertFalse(built.entries[0].auto_fixable)

    def test_a_persistence_class_entry_says_it_needs_a_touch_id(self):
        rule = make_rule(
            "mm-hooks-lane-guard",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
        )
        manifest = make_manifest([rule])
        built = plan_module.build_plan(manifest, [a_result(rule, CheckState.DRIFT)], now=MOMENT)
        self.assertTrue(built.entries[0].persistence_class)
        self.assertIn("Touch ID", built.render())

    def test_the_plan_writes_nothing(self):
        tree = self.root / "home"
        target = tree / ".minimax" / "memory" / "user.md"
        target.parent.mkdir(parents=True)
        target.write_text("the original file\n", encoding="utf-8")
        rule = make_rule(
            "mm-instructions",
            ConfigKind.INSTRUCTIONS,
            check_type=CheckType.MARKED_BLOCK_HASH,
        )
        manifest = make_manifest([rule])
        before = snapshot_tree(tree)
        built = plan_module.build_plan(manifest, [a_result(rule)], now=MOMENT)
        built.render()
        after = snapshot_tree(tree)
        self.assertEqual(before, after)

    def test_the_plan_filters_by_seat_kind_and_platform(self):
        rules = [
            make_rule("mm-a", ConfigKind.INSTRUCTIONS),
            make_rule("mm-b", ConfigKind.SKILLS, check_type=CheckType.SKILLS_INSTALLED),
            make_rule("mm-c", ConfigKind.MCP, check_type=CheckType.MCP_SERVERS_REGISTERED, platform="codex"),
        ]
        manifest = make_manifest(rules)
        results = [a_result(rule) for rule in rules]
        by_kind = plan_module.build_plan(manifest, results, kind=ConfigKind.SKILLS, now=MOMENT)
        self.assertEqual([entry.rule_id for entry in by_kind.entries], ["mm-b"])
        by_platform = plan_module.build_plan(manifest, results, platform="codex", now=MOMENT)
        self.assertEqual([entry.rule_id for entry in by_platform.entries], ["mm-c"])

    def test_an_unknown_state_names_no_change(self):
        rule = make_rule("mm-a", ConfigKind.INSTRUCTIONS)
        manifest = make_manifest([rule])
        built = plan_module.build_plan(
            manifest, [a_result(rule, CheckState.UNKNOWN)], now=MOMENT
        )
        entry = built.entries[0]
        self.assertFalse(entry.auto_fixable)
        self.assertIn("no change is identified", entry.change)

    def test_the_plan_serializes_to_plain_data(self):
        rule = make_rule(
            "mm-hooks",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
        )
        manifest = make_manifest([rule])
        built = plan_module.build_plan(manifest, [a_result(rule)], now=MOMENT)
        data = built.to_dict()
        self.assertEqual(data["seat"], None)
        self.assertEqual(len(data["entries"]), 1)
        self.assertEqual(data["entries"][0]["ruleId"], "mm-hooks")
        self.assertTrue(data["entries"][0]["persistenceClass"])


class InstallerMirrorTests(unittest.TestCase):
    """The mirrored installer tables must agree with the engines they mirror.

    The package itself never imports ``fleet_lanes`` at runtime;  a test may,  and this one
    does,  so a change to an installer table cannot drift away from the plan silently.
    """

    def test_the_rules_files_match_the_installer(self):
        from fleet_lanes import install_rules

        for platform in plan_module.RULES_FILES:
            self.assertIn(platform, [row.name for row in install_rules.PLATFORMS])
            row = [r for r in install_rules.PLATFORMS if r.name == platform][0]
            self.assertEqual(plan_module.RULES_FILES[platform], row.rel_path, platform)

    def test_the_hooks_files_match_the_installer(self):
        from fleet_lanes import install_tools

        for platform in plan_module.TOOLS_FILES:
            self.assertIn(platform, install_tools.PLATFORMS)
            self.assertEqual(
                plan_module.TOOLS_FILES[platform],
                install_tools.PLATFORMS[platform].config_rel,
                platform,
            )

    def test_the_mcp_targets_and_files_match_the_installer(self):
        from fleet_lanes import install_mcp

        self.assertTrue(set(plan_module.MCP_TARGETS.values()).issubset(set(install_mcp.TARGET_KEYS)))
        for target, path in plan_module.MCP_FILES.items():
            if target == install_mcp.CLUTCH_KEY:
                self.assertEqual(path, ".clutch/dsh/cordis.patch.yml")
                continue
            row = [t for t in install_mcp.JSON_TARGETS if t.key == target][0]
            self.assertEqual(path, row.rel_path, target)

    def test_the_owner_file_flag_matches_the_installer(self):
        from fleet_lanes import install_rules

        owners = {row.name for row in install_rules.PLATFORMS if row.owner_file}
        self.assertEqual(set(plan_module.RULES_NEEDS_OWN_FLAG), owners - {"home-agents"})


if __name__ == "__main__":
    unittest.main()
