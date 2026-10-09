"""layout tests: registry, seat aliases, roots, location classes, lane and branch naming.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_layout -v

Every test builds its roots from a throwaway home and an explicit env dict, so neither the real
home nor the process environment leaks in.  Case sensitivity is passed explicitly, never read from
the test filesystem.  The throwaway home itself sits inside a forbidden temp directory (macOS
/private/var/folders/*/*/T, Linux /tmp), which is exactly the situation the forbidden-tmp skip
rule exists for, so the sanctioned-path tests double as a regression test for it.
"""
from __future__ import annotations

import dataclasses
import enum
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fleet_lanes import layout as L
from fleet_lanes.layout import (
    BranchVerdict, LayoutError, LocationClass as LC, NameVerdict as NV,
)

# A frozen copy of the fleet-apps.json shape, so naming tests do not move when the real registry
# gains a row.  The real file is parsed separately in RealRegistryTests.
_APPS = [
    ("Socratic-Trade", "ST", "trading", "Socratic-Trade"),
    ("Congress.Trade", "CT", "congress", "Congress.Trade"),
    ("Usage-Monitor", "UM", "usage", "Usage-Monitor"),
    ("congress-trading-shared", "CTS", "cts", "congress-trading-shared"),
    ("DealDex", "DD", "dealdex", "DealDex"),
    ("AI-Fleet-Coordinator", "AFC", "fleet", "AI-Fleet-Coordinator"),
    ("Personal-Site", "PS", "personal", "Personal-Site"),
    ("Autorotate", "AR", "autorotate", "Autorotate"),
    ("ContactLogo", "CL", "contactlogo", "ContactLogo"),
    ("BotFleet", "BF", "botfleet", "BotFleet"),
    ("HogHunter", "HH", "hoghunter", "HogHunter"),
    ("fleet-ops", "OPS", "fleet-ops", "fleet-ops"),
    ("Clutch", "CK", "clutch", "Clutch"),
]
_SEATS = [
    ("CLAUDE", "claude", ["claude/", "agent/claude"], False),
    ("MONET", "monet", ["monet/"], False),
    ("CODEX", "codex", ["codex/"], False),
    ("AG", "antigravity", ["ag/", "agent/antigravity"], False),
    ("CURSOR", "cursor", ["cursor/"], False),
    ("GROK", "grok", ["grok/"], False),
    ("GROK-BUILD", "grok-build", ["grok-build/"], False),
    ("DSH", "deepseek", ["deepseek/"], True),
    ("HARNESS", "harness", ["harness/"], False),
    ("GROK-BOT", "cursor", ["cursor/"], False),
    ("RENOIR", "renoir", ["renoir/"], False),
    ("FX", "fx", ["fx/"], False),
    ("KIMI", "kimi", ["kimi/"], True),
    ("MM", "minimax", ["minimax/"], False),
    ("MA", "muse-assist", ["muse-assist/", "muse/"], False),
    ("MC", "muse-code", ["muse-code/"], False),
]
REGISTRY_DATA = {
    "owner": "Simple-With-Us",
    "codeRoot": "/Users/jay/Code",
    "appsRoot": "/Users/jay/apps",
    "apps": [{"repo": r, "acronym": a, "worktreePrefix": p, "codeDir": c, "displayName": r, "kind": "product"}
             for r, a, p, c in _APPS],
    "seats": [{"tag": t, "worktreeSuffix": s, "branchPrefixes": b, **({"retired": True} if ret else {})}
              for t, s, b, ret in _SEATS],
}


def _registry() -> L.Registry:
    return L.parse_registry(REGISTRY_DATA)


class HomeCase(unittest.TestCase):
    """A throwaway home with helpers to build checkouts and roots."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(os.path.realpath(tmp.name))
        self.reg = _registry()

    def roots(self, env: dict[str, str] | None = None, *, ci: bool = False) -> L.Roots:
        return L.make_roots(self.home, env or {}, registry=self.reg, case_insensitive=ci)

    def mkdir(self, *rel: str) -> pathlib.Path:
        p = self.home.joinpath(*rel)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def checkout(self, *rel: str, git_file: bool = False) -> pathlib.Path:
        """A directory with `.git`: a directory for a clone, a file for a linked worktree."""
        p = self.mkdir(*rel)
        if git_file:
            (p / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
        else:
            (p / ".git").mkdir()
        return p


# --------------------------------------------------------------------------- registry

class RealRegistryTests(unittest.TestCase):
    def test_real_file_parses_with_a_prefix_on_every_app(self) -> None:
        reg = L.load_registry(env={})
        self.assertGreaterEqual(len(reg.registered_apps), 13)
        for app in reg.apps:
            self.assertTrue(app.prefix, app.name)
            self.assertTrue(app.name, app.prefix)
        for app in reg.registered_apps:
            self.assertTrue(app.integration_dir_name, app.name)
            self.assertTrue(app.owner_repo.startswith(reg.owner + "/"), app.owner_repo)
            self.assertTrue(app.registered)
        prefixes = [a.prefix.lower() for a in reg.apps]
        self.assertEqual(len(prefixes), len(set(prefixes)), "prefixes must be unique")
        for seat in reg.seats:
            self.assertTrue(seat.suffix, seat.name)
            self.assertEqual(seat.suffix, seat.suffix.lower())
        self.assertGreaterEqual(len(reg.seats), 16)

    def test_real_file_matches_known_rows(self) -> None:
        reg = L.load_registry(env={})
        self.assertEqual(reg.app_by_prefix("trading").name, "Socratic-Trade")
        self.assertEqual(reg.app_by_prefix("fleet-ops").integration_dir_name, "fleet-ops")
        self.assertEqual(reg.seat_by_name("AG").suffix, "antigravity")
        self.assertEqual(reg.seat_by_suffix("cursor").name, "CURSOR")
        # every seat's primary branch prefix round-trips through branch_name
        for seat in reg.seats:
            if seat.primary_branch_prefix():
                self.assertEqual(L.branch_name(seat.suffix, "x", reg).split("/")[0],
                                 reg.seat_by_suffix(seat.suffix).primary_branch_prefix())

    def test_default_path_is_repo_root_and_env_overrides(self) -> None:
        path = L.default_registry_path({})
        self.assertEqual(path.name, "fleet-apps.json")
        self.assertTrue(path.is_file(), path)
        self.assertEqual(L.default_registry_path({"FLEET_APPS_JSON": "/x/y.json"}), pathlib.Path("/x/y.json"))

    def test_extras_are_appended_and_unregistered(self) -> None:
        reg = L.load_registry(env={})
        extras = {a.name: a for a in reg.apps if not a.registered}
        for name in ("FleetLink", "Simple-With-Us", "Kodus-Config", "upptime-status",
                     "mmx-acp", "homebrew-tap"):
            self.assertIn(name, extras)

    def test_codecaps_has_a_registry_row_so_it_is_not_an_extra(self) -> None:
        reg = L.load_registry(env={})
        caps = reg.app_by_prefix("codecaps")
        self.assertEqual((caps.name, caps.acronym, caps.integration_dir_name), ("CodeCaps", "CC", "CodeCaps"))
        self.assertTrue(caps.registered)
        self.assertEqual([a.name for a in reg.apps if a.name.lower() == "codecaps"], ["CodeCaps"])


class RegistryParsingTests(HomeCase):
    def test_load_from_path_and_env_override(self) -> None:
        path = self.home / "apps.json"
        path.write_text(json.dumps({"apps": [{"repo": "Zed", "worktreePrefix": "zd"}]}))
        by_arg = L.load_registry(path, env={})
        by_env = L.load_registry(env={"FLEET_APPS_JSON": str(path)})
        self.assertEqual(by_arg.app_by_prefix("zd").name, "Zed")
        self.assertEqual(by_env.app_by_prefix("zd").name, "Zed")
        self.assertEqual(by_arg.source, str(path))

    def test_bad_files_raise(self) -> None:
        with self.assertRaises(OSError):
            L.load_registry(self.home / "missing.json")
        bad = self.home / "bad.json"
        bad.write_text("[1, 2]")
        with self.assertRaises(ValueError):
            L.load_registry(bad)
        bad.write_text("{not json")
        with self.assertRaises(ValueError):
            L.load_registry(bad)

    def test_missing_and_extra_fields_are_tolerated(self) -> None:
        reg = L.parse_registry({
            "unknownTopLevel": 1,
            "apps": [{"repo": "Foo-Bar"}, {"nope": 1}, "text", None,
                     {"repo": "Baz", "worktreePrefix": "bz", "codeDir": "BazDir", "surprise": [1]}],
            "seats": [{"tag": "ZED"}, {}, 7, {"tag": "YU", "worktreeSuffix": "Yuu", "branchPrefixes": "bad"}],
        }, include_extras=False)
        foo, baz = reg.apps
        self.assertEqual((foo.prefix, foo.integration_dir_name, foo.owner_repo), ("foo-bar", "Foo-Bar", "Simple-With-Us/Foo-Bar"))
        self.assertEqual((baz.prefix, baz.integration_dir_name), ("bz", "BazDir"))
        zed, yu = reg.seats
        self.assertEqual((zed.name, zed.suffix, zed.branch_prefixes, zed.retired), ("ZED", "zed", (), False))
        self.assertEqual((yu.suffix, yu.branch_prefixes), ("yuu", ()))

    def test_empty_registry_is_extras_only(self) -> None:
        self.assertEqual(len(L.parse_registry({}).apps), len(L.EXTRA_APPS))
        self.assertEqual(L.parse_registry({}, include_extras=False).apps, ())

    def test_extra_is_skipped_when_the_registry_gains_the_row(self) -> None:
        reg = L.parse_registry({"apps": [{"repo": "codecaps", "worktreePrefix": "cc"}]})
        caps = [a for a in reg.apps if a.name.lower() == "codecaps"]
        self.assertEqual(len(caps), 1)
        self.assertTrue(caps[0].registered)
        self.assertEqual(caps[0].prefix, "cc")

    def test_extra_app_details(self) -> None:
        reg = self.reg
        caps = reg.app_by_prefix("codecaps")
        self.assertFalse(caps.registered)
        self.assertEqual(caps.owner_repo, "Simple-With-Us/codecaps")
        self.assertEqual(caps.integration_dir_name, "CodeCaps")
        self.assertEqual(reg.app_by_prefix("mmx-acp").owner_repo, "")
        self.assertEqual(reg.app_by_prefix("upptime-status").integration_dir_name, "")
        self.assertEqual(len(reg.registered_apps), len(_APPS))
        self.assertEqual(reg.app_by_prefix("simple-with-us").prefix_aliases(), ("simplewithus", "swu"))

    def test_lookups_ignore_case_and_dot_versus_dash(self) -> None:
        self.assertEqual(self.reg.app_by_name("Socratic.Trade").prefix, "trading")
        self.assertEqual(self.reg.app_by_name("Fleet-OPS").prefix, "fleet-ops")
        self.assertEqual(self.reg.app_by_name("congress-trade").prefix, "congress")
        self.assertIsNone(self.reg.app_by_name("nonesuch"))
        self.assertEqual(self.reg.app_by_prefix(" Trading ").name, "Socratic-Trade")

    def test_many_to_one_suffix_is_deterministic(self) -> None:
        self.assertEqual([s.name for s in self.reg.seats_by_suffix("cursor")], ["CURSOR", "GROK-BOT"])
        self.assertEqual(self.reg.seat_by_suffix("cursor").name, "CURSOR")
        self.assertEqual(self.reg.seat_by_suffix("Cursor").name, "CURSOR")
        reordered = L.parse_registry({"seats": [
            {"tag": "OLD", "worktreeSuffix": "shared", "retired": True},
            {"tag": "LIVE2", "worktreeSuffix": "shared"},
            {"tag": "LIVE1", "worktreeSuffix": "shared"},
        ]})
        self.assertEqual(reordered.seat_by_suffix("shared").name, "LIVE2")
        only_retired = L.parse_registry({"seats": [{"tag": "A", "worktreeSuffix": "x", "retired": True},
                                                   {"tag": "B", "worktreeSuffix": "x", "retired": True}]})
        self.assertEqual(only_retired.seat_by_suffix("x").name, "A")
        self.assertIsNone(self.reg.seat_by_suffix("nonesuch"))

    def test_primary_branch_prefix(self) -> None:
        reg = self.reg
        self.assertEqual(reg.seat_by_name("AG").primary_branch_prefix(), "ag")
        self.assertEqual(reg.seat_by_name("MA").primary_branch_prefix(), "muse-assist")
        self.assertEqual(reg.seat_by_name("CLAUDE").primary_branch_prefix(), "claude")
        self.assertEqual(L.Seat("X", "x", ("agent/x",)).primary_branch_prefix(), "")

    def test_prefix_aliases(self) -> None:
        aliases = {a.prefix: a.prefix_aliases() for a in self.reg.apps}
        self.assertIn("afc", aliases["fleet"])
        self.assertIn("ai-fleet-coordinator", aliases["fleet"])
        self.assertIn("usage-monitor", aliases["usage"])
        self.assertIn("congress.trade", aliases["congress"])
        self.assertIn("bf", aliases["botfleet"])
        self.assertNotIn("fleet-ops", aliases["fleet-ops"], "the prefix itself is not an alias")
        for prefix, found in aliases.items():
            self.assertNotIn(prefix, found)
            self.assertEqual(list(found), sorted(found, key=lambda s: (-len(s), s)))


# --------------------------------------------------------------------------- seats

class SeatTests(unittest.TestCase):
    def test_normalize_known_short_forms(self) -> None:
        cases = {"ag": "antigravity", "AG": "antigravity", " mm ": "minimax", "ma": "muse-assist",
                 "muse": "muse-assist", "mc": "muse-code", "dsh": "deepseek", "gb": "cursor",
                 "grok-bot": "cursor", "Grok-Build": "grok-build", "claude": "claude",
                 "antigravity": "antigravity", "minimax": "minimax", "zzz": "zzz"}
        for given, want in cases.items():
            self.assertEqual(L.normalize_seat(given), want, given)

    def test_alias_only(self) -> None:
        for alias in ("ag", "mm", "ma", "muse", "mc", "dsh", "gb", "grok-bot", "AG"):
            self.assertTrue(L.is_alias_only(alias), alias)
        for canonical in ("antigravity", "minimax", "claude", "grok-build", "muse-code", "zzz", ""):
            self.assertFalse(L.is_alias_only(canonical), canonical)

    def test_alias_table_points_only_at_canonical_seats(self) -> None:
        for alias, target in L.SEAT_ALIASES.items():
            self.assertIn(target, L.CANONICAL_SEATS, alias)
            self.assertNotIn(alias, L.CANONICAL_SEATS)

    def test_registry_adds_tags_and_branch_prefixes(self) -> None:
        reg = L.parse_registry({"seats": [
            {"tag": "ZED", "worktreeSuffix": "zedlong", "branchPrefixes": ["zd/", "agent/zed"]},
            {"tag": "PLAIN", "worktreeSuffix": "plain", "branchPrefixes": ["plain/"]},
        ]})
        self.assertEqual(L.normalize_seat("zed", reg), "zedlong")
        self.assertEqual(L.normalize_seat("zd", reg), "zedlong")
        self.assertEqual(L.normalize_seat("zedlong", reg), "zedlong")
        self.assertTrue(L.is_alias_only("zd", reg))
        self.assertFalse(L.is_alias_only("zedlong", reg))
        self.assertFalse(L.is_alias_only("plain", reg))
        self.assertNotIn("agent", L.seat_alias_map(reg), "legacy prefixes must not become aliases")
        # without the registry the new seat is unknown and comes back unchanged
        self.assertEqual(L.normalize_seat("zd"), "zd")
        self.assertFalse(L.is_known_seat("zd"))
        self.assertTrue(L.is_known_seat("zd", reg))

    def test_registry_derived_aliases_match_the_hand_table(self) -> None:
        reg = _registry()
        derived = L.seat_alias_map(reg)
        for alias, target in L.SEAT_ALIASES.items():
            self.assertEqual(derived[alias], target, alias)
        self.assertEqual(derived["ag"], "antigravity")
        self.assertEqual(derived["mm"], "minimax")
        self.assertEqual(L.normalize_seat("dsh", reg), "deepseek")


# --------------------------------------------------------------------------- roots

class RootsTests(HomeCase):
    def test_defaults(self) -> None:
        r = self.roots()
        self.assertEqual(r.home, self.home)
        self.assertEqual(r.code_root, self.home / "Code")
        self.assertEqual(r.apps_root, self.home / "apps")
        self.assertEqual(r.lanes_root, self.home / "apps" / "lanes")
        self.assertEqual(r.managed_root, self.home / "apps" / "lanes" / "_managed")
        self.assertEqual(r.review_root, self.home / "apps" / "lanes" / "_review")
        self.assertEqual(r.layout_mode, "nested")
        self.assertFalse(r.case_insensitive)
        self.assertEqual(r.warnings, ())
        self.assertIn("Socratic-Trade", r.integration_names)
        self.assertIn("fleet-ops", r.integration_names)
        self.assertNotIn("", r.integration_names)
        self.assertNotIn("upptime-status", r.integration_names, "apps with no ~/Code tree add no name")

    def test_home_is_resolved_through_symlinks(self) -> None:
        link = self.home / "home-link"
        real = self.mkdir("real-home")
        link.symlink_to(real)
        r = L.make_roots(link, {}, registry=self.reg, case_insensitive=False)
        self.assertEqual(r.home, real)
        self.checkout("real-home", "Code", "BotFleet")
        self.assertEqual(L.classify_location(link / "Code" / "BotFleet", r), LC.INTEGRATION_TREE)

    def test_lanes_root_override(self) -> None:
        r = self.roots({"FLEET_LANES_ROOT": str(self.home / "elsewhere" / "lanes")})
        self.assertEqual(r.lanes_root, self.home / "elsewhere" / "lanes")
        self.assertEqual(r.review_root, self.home / "elsewhere" / "lanes" / "_review")
        self.assertEqual(self.roots({"FLEET_LANES_ROOT": "~/apps/l2"}).lanes_root, self.home / "apps" / "l2")
        self.assertEqual(self.roots({"FLEET_LANES_ROOT": "rel/lanes"}).lanes_root, self.home / "rel" / "lanes")
        self.assertEqual(self.roots({"FLEET_LANES_ROOT": "  "}).lanes_root, self.home / "apps" / "lanes")

    def test_layout_mode_from_env(self) -> None:
        self.assertEqual(self.roots({"FLEET_LAYOUT": "flat"}).layout_mode, "flat")
        self.assertEqual(self.roots({"FLEET_LAYOUT": " FLAT "}).layout_mode, "flat")
        self.assertEqual(self.roots({"FLEET_LAYOUT": "nested"}).layout_mode, "nested")
        self.assertEqual(self.roots({"FLEET_LAYOUT": ""}).warnings, ())

    def test_bad_layout_falls_back_and_never_disables_tmp_detection(self) -> None:
        r = self.roots({"FLEET_LAYOUT": "sideways"})
        self.assertEqual(r.layout_mode, "nested")
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("sideways", r.warnings[0])
        self.assertTrue(r.tmp_roots and r.tmp_globs)
        far = L.make_roots(pathlib.Path("/nonexistent-fleet-home"), {"FLEET_LAYOUT": "sideways"}, registry=self.reg)
        self.assertEqual(L.classify_location("/private/tmp/x/repo", far), LC.FORBIDDEN_TMP)

    def test_tmp_roots_cover_both_spellings_and_resolved_forms(self) -> None:
        tmp_roots, globs = L.make_tmp_roots({})
        strings = {str(p) for p in tmp_roots}
        for want in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", os.path.realpath("/tmp"),
                     os.path.realpath("/var/tmp")):
            self.assertIn(want, strings)
        self.assertEqual(len(strings), len(tmp_roots), "no duplicates")
        self.assertIn("/private/var/folders/*/*/T", globs)
        self.assertIn("/var/folders/*/*/T", globs)

    def test_tmpdir_from_env_is_added_in_both_forms(self) -> None:
        real = self.mkdir("work-tmp")
        link = self.home / "tmp-link"
        link.symlink_to(real)
        r = self.roots({"TMPDIR": str(link) + "/"})
        self.assertIn(link, r.tmp_roots)
        self.assertIn(real, r.tmp_roots)
        self.assertEqual(self.roots({"TMPDIR": "relative/tmp"}).tmp_roots, self.roots({}).tmp_roots)
        self.assertEqual(self.roots({"TMPDIR": ""}).tmp_roots, self.roots({}).tmp_roots)
        self.assertNotIn(pathlib.Path("/"), self.roots({"TMPDIR": "/"}).tmp_roots)

    def test_tmpdir_tilde_expands_against_the_given_home(self) -> None:
        tmp_roots, _ = L.make_tmp_roots({"TMPDIR": "~/mytmp"}, self.home)
        self.assertIn(self.home / "mytmp", tmp_roots)

    def test_harness_locations(self) -> None:
        r = self.roots()
        by_name = {h.name: h for h in r.harness_locations}
        for name in ("claude-repo", "muse-repo", "codex", "cursor", "grok", "antigravity", "antigravity-ag",
                     "botfleet", "codecaps-pages"):
            self.assertTrue(by_name[name].sanctioned, name)
        for name in ("antigravity-scratch", "botfleet-workspaces", "documents"):
            self.assertFalse(by_name[name].sanctioned, name)
        self.assertTrue(by_name["codex"].glob_or_prefix.endswith("/.codex/worktrees"))
        self.assertIn("/Code/*/.claude/worktrees", by_name["claude-repo"].glob_or_prefix)
        self.assertIn("/Code/*/.muse/worktrees", by_name["muse-repo"].glob_or_prefix)

    def test_glob_characters_in_the_home_path_are_escaped(self) -> None:
        odd = self.mkdir("we[ird]*home")
        r = L.make_roots(odd, {}, registry=self.reg, case_insensitive=False)
        (odd / ".codex" / "worktrees" / "x").mkdir(parents=True)
        self.assertEqual(L.classify_location(odd / ".codex" / "worktrees" / "x", r), LC.MANAGED)
        self.assertEqual(L.classify_location(odd / "Code" / "x" / ".claude" / "worktrees" / "y", r), LC.MANAGED)
        # a path the unescaped glob would have matched is not a managed location (it may be a
        # forbidden temp sibling of the temp-dir test home, which is also correct)
        self.assertNotEqual(L.classify_location(self.home / "weixhome" / ".codex" / "worktrees" / "x", r), LC.MANAGED)

    def test_process_environment_is_read_when_no_env_is_given(self) -> None:
        reg_file = self.home / "apps.json"
        reg_file.write_text(json.dumps({"apps": [{"repo": "Zed", "worktreePrefix": "zd"}]}))
        work = self.mkdir("env-tmp")
        patched = {"FLEET_LAYOUT": "flat", "FLEET_APPS_JSON": str(reg_file), "TMPDIR": str(work),
                   "FLEET_LANES_ROOT": str(self.home / "my-lanes")}
        with mock.patch.dict(os.environ, patched):
            reg = L.load_registry()
            r = L.make_roots(self.home, case_insensitive=False)
            self.assertEqual(reg.app_by_prefix("zd").name, "Zed")
            self.assertEqual(r.layout_mode, "flat")
            self.assertEqual(r.lanes_root, self.home / "my-lanes")
            self.assertIn(work, r.tmp_roots)
            self.assertIn("Zed", r.integration_names)
            self.assertEqual(L.default_registry_path(), reg_file)
            self.assertEqual(L.classify_location(work / "clone", r), LC.FORBIDDEN_TMP)
            self.assertEqual(L.classify_location(self.mkdir("apps", "zd-claude-x"), r), LC.LANE_FLAT)
        with mock.patch.dict(os.environ, {"FLEET_LAYOUT": "", "FLEET_APPS_JSON": ""}):
            self.assertEqual(L.make_roots(self.home, registry=self.reg).layout_mode, "nested")
            self.assertEqual(L.default_registry_path().name, "fleet-apps.json")

    def test_guard_roots_read_no_file_and_cannot_fail(self) -> None:
        env = {"FLEET_APPS_JSON": str(self.home / "missing.json"), "FLEET_LAYOUT": "sideways"}
        with self.assertRaises(OSError):
            L.make_roots(self.home, env)
        guard = L.make_guard_roots(self.home, env)
        self.assertEqual(guard.layout_mode, "nested")
        self.assertEqual(guard.integration_names, ())
        far = L.make_guard_roots(pathlib.Path("/nonexistent-fleet-home"), env)
        self.assertTrue(L.is_forbidden_tmp("/tmp/anything/repo", far))
        self.assertTrue(L.is_forbidden_tmp("/private/var/folders/zz/yy/T/repo", far))
        self.assertFalse(L.is_forbidden_tmp("/nonexistent-fleet-home/apps/lanes/trading/claude-x", far))

    def test_default_registry_is_the_real_one(self) -> None:
        r = L.make_roots(self.home, {}, case_insensitive=False)
        self.assertIn("Socratic-Trade", r.integration_names)
        self.assertIn("fleet-ops", r.integration_names)

    def test_case_insensitive_default_follows_the_platform(self) -> None:
        import sys
        r = L.make_roots(self.home, {}, registry=self.reg)
        self.assertEqual(r.case_insensitive, sys.platform == "darwin")


# --------------------------------------------------------------------------- classification

class ClassifyTests(HomeCase):
    def test_integration_trees_including_extras(self) -> None:
        r = self.roots()
        for name in ("BotFleet", "Socratic-Trade", "Congress.Trade", "fleet-ops", "CodeCaps", "FleetLink",
                     "Simple-With-Us", "homebrew-tap"):
            tree = self.checkout("Code", name)
            self.assertEqual(L.classify_location(tree, r), LC.INTEGRATION_TREE, name)
        self.assertEqual(L.classify_location(self.home / "Code" / "BotFleet" / "src" / "x.ts", r), LC.INTEGRATION_TREE)

    def test_apps_without_a_code_tree_are_not_integration_trees(self) -> None:
        r = self.roots()
        for name in ("Kodus-Config", "upptime-status", "mmx-acp"):
            self.assertEqual(L.classify_location(self.checkout("Code", name), r), LC.FORBIDDEN_CODE_TOPLEVEL, name)

    def test_claude_repo_worktrees_are_managed_not_integration(self) -> None:
        r = self.roots()
        self.checkout("Code", "BotFleet")
        wt = self.checkout("Code", "BotFleet", ".claude", "worktrees", "active-engines-e380b8", git_file=True)
        self.assertEqual(L.classify_location(wt, r), LC.MANAGED)
        self.assertEqual(L.classify_location(wt.parent, r), LC.MANAGED)
        self.assertEqual(L.classify_location(wt / "src" / "a.ts", r), LC.MANAGED)
        self.assertEqual(L.classify_location(self.home / "Code" / "BotFleet" / ".claude", r), LC.INTEGRATION_TREE)

    def test_muse_repo_worktrees_are_managed_not_integration(self) -> None:
        # `muse -w` makes worktrees at <repo>/.muse/worktrees inside the integration tree, and the
        # root cannot be moved, so the place is sanctioned and tracked like the Claude one.
        r = self.roots()
        self.checkout("Code", "BotFleet")
        wt = self.checkout("Code", "BotFleet", ".muse", "worktrees", "fix-login", git_file=True)
        self.assertEqual(L.classify_location(wt, r), LC.MANAGED)
        self.assertEqual(L.classify_location(wt.parent, r), LC.MANAGED)
        self.assertEqual(L.classify_location(wt / "src" / "a.ts", r), LC.MANAGED)
        self.assertEqual(L.classify_location(self.home / "Code" / "BotFleet" / ".muse", r), LC.INTEGRATION_TREE)
        # only the registered worktrees folder is sanctioned: a sibling folder or another dot
        # folder under the integration tree is not a harness location
        other = self.checkout("Code", "BotFleet", ".muse", "scratch", "x", git_file=True)
        self.assertEqual(L.classify_location(other, r), LC.INTEGRATION_TREE)

    def test_forbidden_code_toplevel_needs_a_checkout(self) -> None:
        r = self.roots()
        clone = self.checkout("Code", "HogHunter-ag-am-icon")
        linked = self.checkout("Code", "Usage-Monitor-worktree", git_file=True)
        data = self.mkdir("Code", "Bots")
        for forbidden in (clone, linked, clone / "src"):
            self.assertEqual(L.classify_location(forbidden, r), LC.FORBIDDEN_CODE_TOPLEVEL, forbidden)
        self.assertEqual(L.classify_location(data, r), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.mkdir("Code"), r), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(data, r, is_checkout=True), LC.FORBIDDEN_CODE_TOPLEVEL)
        self.assertEqual(L.classify_location(clone, r, is_checkout=False), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.home / "Code" / "not-created-yet", r), LC.UNSANCTIONED)

    def test_socratic_symlink_dedupes_to_one_key(self) -> None:
        r = self.roots()
        real = self.checkout("Code", "Socratic-Trade")
        link = self.home / "Code" / "Socratic.Trade"
        link.symlink_to(real)
        self.assertEqual(L.classify_location(link, r), LC.INTEGRATION_TREE)
        self.assertEqual(L.classify_location(real, r), LC.INTEGRATION_TREE)
        self.assertEqual(L.real_key(link, r), L.real_key(real, r))
        other = self.checkout("Code", "BotFleet")
        self.assertNotEqual(L.real_key(other, r), L.real_key(real, r))
        self.assertEqual(L.dedupe_paths([link, real, other, str(link)], r), [link, other])
        self.assertEqual(L.dedupe_paths([], r), [])

    def test_socratic_dot_spelling_without_a_symlink_still_counts(self) -> None:
        r = self.roots()
        self.assertEqual(L.classify_location(self.checkout("Code", "Socratic.Trade"), r), LC.INTEGRATION_TREE)

    def test_nested_lanes(self) -> None:
        r = self.roots()
        lane = self.checkout("apps", "lanes", "trading", "claude-fix-x", git_file=True)
        self.assertEqual(L.classify_location(lane, r), LC.LANE_NESTED)
        self.assertEqual(L.classify_location(lane / "src" / "a.py", r), LC.LANE_NESTED)
        self.assertEqual(L.lane_root(lane / "src" / "a.py", r), lane)
        self.assertEqual(L.lane_root(lane, r), lane)
        # containers and reserved names are not lanes
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "trading"), r), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes"), r), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "_archive", "x"), r), LC.UNSANCTIONED)
        self.assertIsNone(L.lane_root(self.mkdir("apps", "lanes", "trading"), r))

    def test_review_and_managed_under_lanes(self) -> None:
        r = self.roots()
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "_review", "pr-12"), r), LC.REVIEW)
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "_review"), r), LC.REVIEW)
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "_managed", "codex", "x"), r), LC.MANAGED)
        self.assertIsNone(L.lane_root(self.mkdir("apps", "lanes", "_review", "pr-12"), r))

    def test_flat_lane_is_legacy_in_nested_mode_and_plain_in_flat_mode(self) -> None:
        lane = self.checkout("apps", "trading-claude-fix-x", git_file=True)
        nested = self.roots({"FLEET_LAYOUT": "nested"})
        flat = self.roots({"FLEET_LAYOUT": "flat"})
        self.assertEqual(L.classify_location(lane, nested), LC.LANE_FLAT_LEGACY)
        self.assertEqual(L.classify_location(lane / "deep" / "x", nested), LC.LANE_FLAT_LEGACY)
        self.assertEqual(L.classify_location(lane, flat), LC.LANE_FLAT)
        self.assertEqual(L.lane_root(lane / "deep", flat), lane)
        for roots in (nested, flat):
            self.assertEqual(L.classify_location(self.mkdir("apps", "_pr-lanes"), roots), LC.UNSANCTIONED)
            self.assertEqual(L.classify_location(self.mkdir("apps", ".hidden-clone"), roots), LC.UNSANCTIONED)
            self.assertEqual(L.classify_location(self.mkdir("apps"), roots), LC.UNSANCTIONED)

    def test_flat_mode_does_not_recognise_the_nested_lane_form(self) -> None:
        flat = self.roots({"FLEET_LAYOUT": "flat"})
        lane = self.mkdir("apps", "lanes", "trading", "claude-x")
        self.assertEqual(L.classify_location(lane, flat), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "_review", "p"), flat), LC.REVIEW)
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "_managed", "c"), flat), LC.MANAGED)

    def test_harness_managed_locations(self) -> None:
        r = self.roots()
        for rel in ((".codex", "worktrees", "codecaps-board", "CodeCaps"), (".cursor", "worktrees", "x"),
                    (".grok", "worktrees", "repo", "worktree-1"), (".gemini", "antigravity", "worktrees", "p"),
                    (".ag", "worktrees", "x"), (".botfleet", "worktrees", "x"), ("codecaps-pages",)):
            self.assertEqual(L.classify_location(self.mkdir(*rel), r), LC.MANAGED, rel)

    def test_unsanctioned_places_are_reported_not_forbidden(self) -> None:
        r = self.roots()
        for rel in ((".gemini", "antigravity", "scratch", "OpenMausBot"),
                    (".botfleet", "workspaces", "e8da75c0", "BotFleet"), ("Documents", "Website"),
                    ("Desktop", "botfleet-site"), ("agy-acp",), ("anythingmcp",)):
            self.assertEqual(L.classify_location(self.mkdir(*rel), r), LC.UNSANCTIONED, rel)
        self.assertEqual(L.classify_location("/Volumes/external/repo", r), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.home, r), LC.UNSANCTIONED)

    def test_tilde_expands_against_the_given_home(self) -> None:
        r = self.roots()
        self.mkdir("apps", "lanes", "trading", "claude-x")
        self.assertEqual(L.classify_location("~/apps/lanes/trading/claude-x", r), LC.LANE_NESTED)
        self.assertEqual(L.classify_location("~", r), LC.UNSANCTIONED)

    def test_tmp_through_a_symlink_is_forbidden(self) -> None:
        fake_tmp = self.mkdir("scratch-tmp")
        target = self.mkdir("scratch-tmp", "repo")
        r = dataclasses.replace(self.roots(), tmp_roots=(pathlib.Path(os.path.realpath(fake_tmp)),), tmp_globs=())
        self.assertEqual(L.classify_location(target, r), LC.FORBIDDEN_TMP)
        # a symlink that lives in a sanctioned place but points into tmp
        sneaky = self.home / "apps" / "trading-claude-link"
        self.mkdir("apps")
        sneaky.symlink_to(target)
        self.assertEqual(L.classify_location(sneaky, r), LC.FORBIDDEN_TMP)
        self.assertTrue(L.is_forbidden_tmp(sneaky, r))
        # a symlink that stands in for the tmp dir itself, the way /tmp stands in for /private/tmp
        alias = self.home / "tmp-alias"
        alias.symlink_to(fake_tmp)
        self.assertEqual(L.classify_location(alias / "new-clone" / "repo", r), LC.FORBIDDEN_TMP)
        self.assertEqual(L.resolve_path(alias / "x", r), str(self.home / "scratch-tmp" / "x"))
        self.assertFalse(L.is_forbidden_tmp(self.home / "apps" / "lanes" / "trading" / "claude-x", r))

    def test_tmpdir_env_directory_is_forbidden(self) -> None:
        work = self.mkdir("elsewhere", "build-tmp")
        r = self.roots({"TMPDIR": str(work)})
        self.assertEqual(L.classify_location(work / "checkout", r), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location(self.mkdir("elsewhere", "other"), r), LC.UNSANCTIONED)

    def test_tmp_wins_over_a_sanctioned_prefix(self) -> None:
        under_apps = self.mkdir("apps", "scratch")
        r = self.roots({"TMPDIR": str(under_apps)})
        self.assertEqual(L.classify_location(under_apps / "repo", r), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location(self.mkdir("apps", "trading-claude"), r), LC.LANE_FLAT_LEGACY)

    def test_system_tmp_spellings(self) -> None:
        far = L.make_roots(pathlib.Path("/nonexistent-fleet-home"), {}, registry=self.reg, case_insensitive=False)
        for path in ("/tmp/foo/repo", "/private/tmp/foo/repo", "/var/tmp/x", "/private/var/tmp/x",
                     "/private/var/folders/zz/yy/T/repo", "/var/folders/zz/yy/T/repo",
                     "/private/var/folders/zz/yy/T/a/b/c"):
            self.assertEqual(L.classify_location(path, far), LC.FORBIDDEN_TMP, path)
            self.assertTrue(L.is_forbidden_tmp(path, far), path)
        for path in ("/private/var/folders/zz/yy/C/cache", "/private/var/folders/zz/yy", "/private/tmpfoo/x",
                     "/usr/local/x"):
            self.assertNotEqual(L.classify_location(path, far), LC.FORBIDDEN_TMP, path)

    def test_claude_scratchpad_is_not_special_cased_here(self) -> None:
        far = L.make_roots(pathlib.Path("/nonexistent-fleet-home"), {}, registry=self.reg, case_insensitive=False)
        pad = "/private/tmp/claude-501/-Users-jay-Code-X/abc/scratchpad"
        self.assertEqual(L.classify_location(pad, far), LC.FORBIDDEN_TMP)

    def test_env_tmpdir_on_a_far_home(self) -> None:
        far = L.make_roots(pathlib.Path("/nonexistent-fleet-home"), {"TMPDIR": "/Volumes/work/tmpdir"},
                           registry=self.reg, case_insensitive=False)
        self.assertEqual(L.classify_location("/Volumes/work/tmpdir/clone", far), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location("/Volumes/work/other", far), LC.UNSANCTIONED)

    def test_home_inside_a_temp_dir_suppresses_only_that_concrete_dir(self) -> None:
        # a macOS test home: /private/var/folders/aa/bb/T/<random>
        home = pathlib.Path("/private/var/folders/aa/bb/T/fakehome")
        r = L.make_roots(home, {}, registry=self.reg, case_insensitive=False)
        self.assertEqual(r.home, home)
        self.assertEqual(L.classify_location(home / "apps" / "lanes" / "trading" / "claude-x", r), LC.LANE_NESTED)
        self.assertEqual(L.classify_location(home / "Code" / "BotFleet", r), LC.INTEGRATION_TREE)
        self.assertEqual(L.classify_location(home / ".codex" / "worktrees" / "x", r), LC.MANAGED)
        # a different user temp dir is still forbidden, so the glob was not dropped as a whole
        self.assertEqual(L.classify_location("/private/var/folders/zz/yy/T/repo", r), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location("/var/folders/zz/yy/T/repo", r), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location("/private/tmp/repo", r), LC.FORBIDDEN_TMP)
        # only the home's own subtree is exempt: a sibling of the home inside the same temp dir is
        # still forbidden, so a temp-dir test home cannot switch that whole temp root off
        self.assertEqual(L.classify_location("/private/var/folders/aa/bb/T/other", r), LC.FORBIDDEN_TMP)

    def test_linux_style_home_directly_under_tmp_does_not_switch_tmp_off(self) -> None:
        # CI shape: tempfile makes the home /tmp/tmpAbC123 and the deny payloads name /tmp/<repo>
        home = pathlib.Path("/tmp/tmpAbC123")
        r = L.make_roots(home, {}, registry=self.reg, case_insensitive=False)
        self.assertEqual(L.classify_location(home / "apps" / "lanes" / "trading" / "claude-x", r), LC.LANE_NESTED)
        self.assertEqual(L.classify_location(home / "Code" / "BotFleet", r), LC.INTEGRATION_TREE)
        self.assertEqual(L.classify_location("/tmp/dealdex-work", r), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location("/tmp/tmpOther/apps/lanes/trading/claude-x", r), LC.FORBIDDEN_TMP)
        self.assertTrue(L.is_forbidden_tmp("/tmp/dealdex-work", r))
        self.assertFalse(L.is_forbidden_tmp(home / "apps", r))

    def test_home_inside_system_tmp_keeps_the_other_roots(self) -> None:
        home = pathlib.Path("/private/tmp/fleet-fake-home")
        r = L.make_roots(home, {}, registry=self.reg, case_insensitive=False)
        self.assertEqual(L.classify_location(home / "apps" / "trading-claude", r), LC.LANE_FLAT_LEGACY)
        self.assertEqual(L.classify_location("/var/tmp/x", r), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location("/private/var/folders/zz/yy/T/x", r), LC.FORBIDDEN_TMP)

    def test_the_real_throwaway_home_classifies_sanctioned_paths(self) -> None:
        # self.home was made by tempfile, so on macOS it sits in /private/var/folders/*/*/T
        r = self.roots()
        self.assertEqual(L.classify_location(self.mkdir("apps", "lanes", "trading", "claude-x"), r), LC.LANE_NESTED)
        self.assertFalse(L.is_forbidden_tmp(self.home / "apps", r))

    def test_case_insensitive_matching(self) -> None:
        ci = self.roots(ci=True)
        cs = self.roots(ci=False)
        self.mkdir("apps", "lanes", "trading", "claude-x")
        shouty = self.home / "APPS" / "Lanes" / "Trading" / "claude-x"
        self.assertEqual(L.classify_location(shouty, ci), LC.LANE_NESTED)
        self.assertEqual(L.classify_location(self.home / "APPS" / "trading-claude", ci), LC.LANE_FLAT_LEGACY)
        self.assertEqual(L.classify_location(self.home / "CODE" / "BOTFLEET", ci), LC.INTEGRATION_TREE)
        self.assertEqual(L.classify_location(self.home / ".CODEX" / "Worktrees" / "x", ci), LC.MANAGED)
        self.assertEqual(L.classify_location(self.home / "apps" / "lanes" / "_REVIEW" / "p", ci), LC.REVIEW)
        # the same spellings are strangers on a case-sensitive volume
        self.assertEqual(L.classify_location(shouty, cs), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.home / "CODE" / "BOTFLEET", cs), LC.UNSANCTIONED)
        self.assertEqual(L.classify_location(self.home / ".CODEX" / "Worktrees" / "x", cs), LC.UNSANCTIONED)

    def test_case_insensitive_integration_name_and_tmp(self) -> None:
        self.checkout("Code", "Fleet-OPS")      # disk says Fleet-OPS, fleet-apps.json says fleet-ops
        ci = self.roots(ci=True)
        cs = self.roots(ci=False)
        self.assertEqual(L.classify_location(self.home / "Code" / "Fleet-OPS", ci), LC.INTEGRATION_TREE)
        self.assertEqual(L.classify_location(self.home / "Code" / "Fleet-OPS", cs), LC.FORBIDDEN_CODE_TOPLEVEL)
        self.assertEqual(L.classify_location(self.mkdir("Code", "fleet-ops"), cs), LC.INTEGRATION_TREE)
        fake_tmp = self.mkdir("scratch-tmp")
        roots = dataclasses.replace(ci, tmp_roots=(pathlib.Path(os.path.realpath(fake_tmp)),), tmp_globs=())
        self.assertEqual(L.classify_location(self.home / "SCRATCH-TMP" / "repo", roots), LC.FORBIDDEN_TMP)
        far = L.make_roots(pathlib.Path("/nonexistent-fleet-home"), {}, registry=self.reg, case_insensitive=True)
        self.assertEqual(L.classify_location("/PRIVATE/TMP/x", far), LC.FORBIDDEN_TMP)
        self.assertEqual(L.classify_location("/private/var/FOLDERS/zz/yy/t/x", far), LC.FORBIDDEN_TMP)

    def test_every_location_class_is_reachable(self) -> None:
        r = self.roots()
        fake_tmp = self.mkdir("scratch-tmp")
        r_tmp = dataclasses.replace(r, tmp_roots=(pathlib.Path(os.path.realpath(fake_tmp)),), tmp_globs=())
        seen = {
            L.classify_location(self.checkout("Code", "BotFleet"), r),
            L.classify_location(self.mkdir("apps", "lanes", "trading", "claude-x"), r),
            L.classify_location(self.mkdir("apps", "trading-claude"), r),
            L.classify_location(self.mkdir("apps", "trading-claude"), self.roots({"FLEET_LAYOUT": "flat"})),
            L.classify_location(self.mkdir("apps", "lanes", "_review", "p"), r),
            L.classify_location(self.mkdir(".codex", "worktrees", "x"), r),
            L.classify_location(fake_tmp / "repo", r_tmp),
            L.classify_location(self.checkout("Code", "stray"), r),
            L.classify_location(self.mkdir("Documents", "x"), r),
        }
        self.assertEqual(seen, set(LC))

    def test_location_class_compares_and_prints_as_its_name(self) -> None:
        self.assertEqual(LC.FORBIDDEN_TMP, "FORBIDDEN_TMP")
        self.assertEqual(f"{LC.LANE_NESTED}", "LANE_NESTED")
        self.assertEqual(len(set(LC)), 9)


# --------------------------------------------------------------------------- naming

class SlugAndNameBuilderTests(HomeCase):
    def test_validate_slug_accepts(self) -> None:
        for slug in ("a", "x1", "fix-x-1", "21", "a" * 40, "ios-fix-2026"):
            self.assertEqual(L.validate_slug(slug), slug)
            self.assertTrue(L.is_valid_slug(slug))

    def test_validate_slug_rejects(self) -> None:
        for slug in ("", "a" * 41, "-a", "a-", "-", "A", "Fix", "a_b", "a--b", "a b", "a/b", "a.b", "ä"):
            with self.assertRaises(LayoutError, msg=repr(slug)):
                L.validate_slug(slug)
            self.assertFalse(L.is_valid_slug(slug))
        for bad in (None, 5, b"x"):
            with self.assertRaises(LayoutError):
                L.validate_slug(bad)  # type: ignore[arg-type]
            self.assertFalse(L.is_valid_slug(bad))
        self.assertTrue(issubclass(LayoutError, ValueError))

    def test_lane_dir_name(self) -> None:
        self.assertEqual(L.lane_dir_name("trading", "claude", "fix-x"), "trading-claude-fix-x")
        self.assertEqual(L.lane_dir_name("fleet-ops", "grok-build"), "fleet-ops-grok-build")
        self.assertEqual(L.lane_dir_name("trading", "ag", "x"), "trading-ag-x", "the seat is used as given")
        for args in (("Trading", "claude", "x"), ("trading", "Claude", "x"), ("trading", "claude", "X"),
                     ("", "claude", "x"), ("trading", "", "x"), ("trading", "claude", "")):
            with self.assertRaises(LayoutError, msg=args):
                L.lane_dir_name(*args)

    def test_nested_dir_name(self) -> None:
        self.assertEqual(L.nested_dir_name("claude", "fix-x"), "claude-fix-x")
        self.assertEqual(L.nested_dir_name("claude"), "claude")
        with self.assertRaises(LayoutError):
            L.nested_dir_name("claude", "Bad")

    def test_branch_name(self) -> None:
        self.assertEqual(L.branch_name("claude", "fix-x"), "claude/fix-x")
        self.assertEqual(L.branch_name("Claude", "fix-x"), "claude/fix-x")
        self.assertEqual(L.branch_name("ag", "x"), "ag/x", "without a registry the seat is used as given")
        self.assertEqual(L.branch_name("antigravity", "x", self.reg), "ag/x")
        self.assertEqual(L.branch_name("ag", "x", self.reg), "ag/x")
        self.assertEqual(L.branch_name("mm", "x", self.reg), "minimax/x")
        self.assertEqual(L.branch_name("gb", "x", self.reg), "cursor/x")
        self.assertEqual(L.branch_name("muse", "x", self.reg), "muse-assist/x")
        self.assertEqual(L.branch_name("GROK-BUILD", "x", self.reg), "grok-build/x")
        self.assertEqual(L.branch_name("zzz", "x", self.reg), "zzz/x")
        with self.assertRaises(LayoutError):
            L.branch_name("claude", "Bad Slug")
        # every branch name the builder makes is a conforming branch
        for seat in self.reg.seats:
            self.assertEqual(L.check_branch_name(L.branch_name(seat.suffix, "x", self.reg), self.reg),
                             BranchVerdict.CONFORMING, seat.name)

    def test_expected_lane_path_nested_and_flat(self) -> None:
        nested = self.roots()
        flat = self.roots({"FLEET_LAYOUT": "flat"})
        st = self.reg.app_by_prefix("trading")
        self.assertEqual(L.expected_lane_path(st, "claude", "fix-x", nested),
                         self.home / "apps" / "lanes" / "trading" / "claude-fix-x")
        self.assertEqual(L.expected_lane_path(st, "claude", "fix-x", flat),
                         self.home / "apps" / "trading-claude-fix-x")
        self.assertEqual(L.expected_lane_path("trading", "claude", None, nested),
                         self.home / "apps" / "lanes" / "trading" / "claude")
        self.assertEqual(L.expected_lane_path("trading", "claude", None, flat), self.home / "apps" / "trading-claude")
        self.assertEqual(L.expected_lane_path("Trading", "ag", "x", nested, self.reg),
                         self.home / "apps" / "lanes" / "trading" / "antigravity-x")
        self.assertEqual(L.expected_lane_path(st, "mm", "x", flat),
                         self.home / "apps" / "trading-minimax-x")
        for bad in (("trading", "claude", "Bad"), ("Bad Prefix", "claude", "x"), ("trading", "bad seat", "x")):
            with self.assertRaises(LayoutError, msg=bad):
                L.expected_lane_path(*bad, nested)

    def test_expected_paths_classify_and_check_as_conforming(self) -> None:
        for mode in ("nested", "flat"):
            r = self.roots({"FLEET_LAYOUT": mode})
            for app in self.reg.apps:
                for seat in ("claude", "mm", "grok-build"):
                    path = L.expected_lane_path(app, seat, "some-slug", r, self.reg)
                    want_class = LC.LANE_NESTED if mode == "nested" else LC.LANE_FLAT
                    self.assertEqual(L.classify_location(path, r), want_class, (mode, path))
                    self.assertEqual(L.check_lane_name(path, self.reg, r), NV.CONFORMING, (mode, path))


class LaneNameTests(HomeCase):
    """Real directory names from the 2026-10-07 sweep, judged by the name alone."""

    FLAT = {
        # CONFORMING: exact prefix, canonical seat
        "fleet-ops-minimax": NV.CONFORMING,
        "trading-minimax": NV.CONFORMING,
        "botfleet-claude-eng-classifier": NV.CONFORMING,
        "botfleet-grok-build": NV.CONFORMING,
        "botfleet-grok-speech": NV.CONFORMING,
        "botfleet-codex-workspace-binding": NV.CONFORMING,
        "usage-codex-codecaps-provenance": NV.CONFORMING,
        "hoghunter-grok-build-bonjour": NV.CONFORMING,
        "contactlogo-minimax": NV.CONFORMING,
        "clutch-claude-ios": NV.CONFORMING,
        "botfleet-harness-x": NV.CONFORMING,
        "trading-claude": NV.CONFORMING,
        "simple-with-us-codex-web-assets": NV.CONFORMING,
        "codecaps-grok-build": NV.CONFORMING,
        "fleetlink-minimax": NV.CONFORMING,
        # ALIAS-ONLY: exact prefix, seat is a short form
        "fleet-ops-mm-domains": NV.ALIAS_ONLY,
        "fleet-ops-ag": NV.ALIAS_ONLY,
        "botfleet-ag-orb": NV.ALIAS_ONLY,
        "botfleet-ag": NV.ALIAS_ONLY,
        "botfleet-mm-mcode": NV.ALIAS_ONLY,
        "hoghunter-gb-altool-fix": NV.ALIAS_ONLY,
        "codecaps-mm-accent": NV.ALIAS_ONLY,
        # NAME-DRIFT: lane-shaped but wrong
        "BotFleet-minimax": NV.NAME_DRIFT,
        "Clutch-mm-rename": NV.NAME_DRIFT,
        "Simple-With-Us-mm-rename": NV.NAME_DRIFT,
        "Congress.Trade-mm-rename": NV.NAME_DRIFT,
        "afc-minimax-cmm": NV.NAME_DRIFT,
        "afc-main-clean": NV.NAME_DRIFT,
        "ai-fleet-coordinator-ag": NV.NAME_DRIFT,
        "usage-monitor-ag-pin": NV.NAME_DRIFT,
        "congress-trade-minimax": NV.NAME_DRIFT,
        "socratic-trade-minimax": NV.NAME_DRIFT,
        "st-mm-landing": NV.NAME_DRIFT,
        "simplewithus-mm-web": NV.NAME_DRIFT,
        "bf-claude-prfix": NV.NAME_DRIFT,
        "harness-minimax-neutral-ui": NV.NAME_DRIFT,
        "claude-st-rotfix": NV.NAME_DRIFT,
        "fleet-tools-mm": NV.NAME_DRIFT,
        "botfleet-kody-734": NV.NAME_DRIFT,
        "botfleet-fixer-879": NV.NAME_DRIFT,
        "botfleet-bf-compiler": NV.NAME_DRIFT,
        "hoghunter-bf-compiler": NV.NAME_DRIFT,
        "codecaps-fixer-175": NV.NAME_DRIFT,
        "botfleet-mm-av-concepts 2": NV.NAME_DRIFT,
        "botfleet--claude": NV.NAME_DRIFT,
        "botfleet-MM-x": NV.NAME_DRIFT,
        # NON-LANE: nothing says it is a lane
        "botfleet-server": NV.NON_LANE,
        "botfleet-package-mac": NV.NON_LANE,
        "clutch-runtime": NV.NON_LANE,
        "hoghunter-cleantier": NV.NON_LANE,
        "fleet-rag": NV.NON_LANE,
        "research": NV.NON_LANE,
        "send-sms": NV.NON_LANE,
        "congress-trading-shared": NV.NON_LANE,
        "HogHunter": NV.NON_LANE,
        "upptime-status": NV.NON_LANE,
        "mmx-acp": NV.NON_LANE,
    }

    def test_flat_fixtures(self) -> None:
        for mode in ("nested", "flat"):
            r = self.roots({"FLEET_LAYOUT": mode})
            for name, want in self.FLAT.items():
                got = L.check_lane_name(self.home / "apps" / name, self.reg, r)
                self.assertEqual(got, want, f"{mode}: {name}: {got}")

    def test_verdicts_are_plain_strings(self) -> None:
        r = self.roots()
        got = L.check_lane_name(self.home / "apps" / "botfleet-ag-orb", self.reg, r)
        self.assertEqual(got, "ALIAS-ONLY")
        self.assertEqual(f"{got}", "ALIAS-ONLY")
        self.assertEqual(json.dumps({"v": got}), '{"v": "ALIAS-ONLY"}')
        self.assertEqual({v.value for v in NV}, {"CONFORMING", "ALIAS-ONLY", "NAME-DRIFT", "NON-LANE"})

    def test_longest_prefix_wins(self) -> None:
        r = self.roots()
        res = L.explain_lane_name(self.home / "apps" / "fleet-ops-minimax", self.reg, r)
        self.assertEqual(res.app.prefix, "fleet-ops")
        self.assertEqual((res.seat, res.slug), ("minimax", None))
        res = L.explain_lane_name(self.home / "apps" / "fleet-tools-mm", self.reg, r)
        self.assertEqual(res.app.prefix, "fleet")
        res = L.explain_lane_name(self.home / "apps" / "usage-monitor-ag-pin", self.reg, r)
        self.assertEqual(res.app.prefix, "usage", "alias usage-monitor beats prefix usage, same app")
        self.assertIn("alias-prefix:usage-monitor", res.reasons)

    def test_seat_and_slug_are_split_correctly(self) -> None:
        r = self.roots()
        build = L.explain_lane_name(self.home / "apps" / "botfleet-grok-build", self.reg, r)
        speech = L.explain_lane_name(self.home / "apps" / "botfleet-grok-speech", self.reg, r)
        self.assertEqual((build.seat, build.seat_token, build.slug), ("grok-build", "grok-build", None))
        self.assertEqual((speech.seat, speech.seat_token, speech.slug), ("grok", "grok", "speech"))
        both = L.explain_lane_name(self.home / "apps" / "hoghunter-grok-build-bonjour", self.reg, r)
        self.assertEqual((both.seat, both.slug), ("grok-build", "bonjour"))
        alias = L.explain_lane_name(self.home / "apps" / "botfleet-mm-lint-batch-1", self.reg, r)
        self.assertEqual((alias.seat, alias.seat_token, alias.slug), ("minimax", "mm", "lint-batch-1"))
        gb = L.explain_lane_name(self.home / "apps" / "hoghunter-gb-altool-fix", self.reg, r)
        self.assertEqual((gb.seat, gb.seat_token), ("cursor", "gb"))
        self.assertEqual(build.layout, "flat")
        self.assertEqual(build.location, LC.LANE_FLAT_LEGACY)

    def test_unregistered_apps_are_flagged_on_the_result(self) -> None:
        r = self.roots()
        res = L.explain_lane_name(self.home / "apps" / "codecaps-mm-accent", self.reg, r)
        self.assertEqual(res.verdict, NV.ALIAS_ONLY)
        self.assertFalse(res.app.registered)
        self.assertEqual(res.app.name, "CodeCaps")
        res = L.explain_lane_name(self.home / "apps" / "botfleet-ag-orb", self.reg, r)
        self.assertTrue(res.app.registered)

    def test_role_is_not_a_seat(self) -> None:
        r = self.roots()
        res = L.explain_lane_name(self.home / "apps" / "botfleet-kody-734", self.reg, r)
        self.assertEqual(res.verdict, NV.NAME_DRIFT)
        self.assertEqual(res.seat, "BF-KODY")
        self.assertIn("bot-role-not-seat:kody", res.reasons)
        res = L.explain_lane_name(self.home / "apps" / "botfleet-bf-compiler", self.reg, r)
        self.assertEqual(res.seat, "BF-COMPILER")

    def test_ambiguous_names_land_where_intended(self) -> None:
        r = self.roots()
        # `harness` is both a seat suffix and an alias of the Clutch app: the app reading wins at token 0
        res = L.explain_lane_name(self.home / "apps" / "harness-minimax-neutral-ui", self.reg, r)
        self.assertEqual((res.verdict, res.app.name, res.seat), (NV.NAME_DRIFT, "Clutch", "minimax"))
        # `bf` is both the BotFleet acronym and a role token
        res = L.explain_lane_name(self.home / "apps" / "bf-claude-prfix", self.reg, r)
        self.assertEqual((res.verdict, res.app.name, res.seat), (NV.NAME_DRIFT, "BotFleet", "claude"))
        # a bare repo name is a clone, not a lane, even when the repo name is an alias
        res = L.explain_lane_name(self.home / "apps" / "congress-trading-shared", self.reg, r)
        self.assertEqual((res.verdict, res.app.name), (NV.NON_LANE, "congress-trading-shared"))
        # the harness seat is still a seat after an exact prefix
        res = L.explain_lane_name(self.home / "apps" / "botfleet-harness-x", self.reg, r)
        self.assertEqual((res.verdict, res.seat), (NV.CONFORMING, "harness"))

    def test_slug_over_the_builder_limit_is_reported_but_not_a_drift(self) -> None:
        r = self.roots()
        name = "botfleet-claude-" + "a" * 50
        res = L.explain_lane_name(self.home / "apps" / name, self.reg, r)
        self.assertEqual(res.verdict, NV.CONFORMING)
        self.assertIn("slug-over-40", res.reasons)

    def test_case_and_kebab_reasons(self) -> None:
        r = self.roots()
        res = L.explain_lane_name(self.home / "apps" / "BotFleet-minimax", self.reg, r)
        self.assertIn("case", res.reasons)
        self.assertIn("prefix-case", res.reasons)
        res = L.explain_lane_name(self.home / "apps" / "botfleet-mm-av-concepts 2", self.reg, r)
        self.assertIn("not-kebab", res.reasons)

    def test_nested_fixtures(self) -> None:
        r = self.roots()
        lanes = self.home / "apps" / "lanes"
        cases = {
            ("trading", "claude-fix-x"): NV.CONFORMING,
            ("trading", "claude"): NV.CONFORMING,
            ("trading", "grok-build-x"): NV.CONFORMING,
            ("fleet-ops", "minimax-x"): NV.CONFORMING,
            ("codecaps", "claude-x"): NV.CONFORMING,
            ("trading", "ag-x"): NV.ALIAS_ONLY,
            ("trading", "mm"): NV.ALIAS_ONLY,
            ("codecaps", "mm-accent"): NV.ALIAS_ONLY,
            ("Trading", "claude-x"): NV.NAME_DRIFT,
            ("st", "claude-x"): NV.NAME_DRIFT,
            ("socratic-trade", "claude-x"): NV.NAME_DRIFT,
            ("nonesuch", "claude-x"): NV.NAME_DRIFT,
            ("trading", "trading-claude-x"): NV.NAME_DRIFT,
            ("trading", "kody-12"): NV.NAME_DRIFT,
            ("trading", "Claude-x"): NV.NAME_DRIFT,
            ("trading", "notes"): NV.NON_LANE,
            ("nonesuch", "notes"): NV.NON_LANE,
        }
        for (prefix, name), want in cases.items():
            got = L.check_lane_name(lanes / prefix / name, self.reg, r)
            self.assertEqual(got, want, f"{prefix}/{name}: {got}")

    def test_nested_result_details(self) -> None:
        r = self.roots()
        path = self.home / "apps" / "lanes" / "codecaps" / "mm-accent"
        res = L.explain_lane_name(path / "sub" / "dir", self.reg, r)
        self.assertEqual((res.verdict, res.layout, res.location), (NV.ALIAS_ONLY, "nested", LC.LANE_NESTED))
        self.assertEqual((res.seat, res.seat_token, res.slug), ("minimax", "mm", "accent"))
        self.assertFalse(res.app.registered)
        res = L.explain_lane_name(self.home / "apps" / "lanes" / "st" / "claude-x", self.reg, r)
        self.assertIn("prefix-dir-alias:st", res.reasons)
        res = L.explain_lane_name(self.home / "apps" / "lanes" / "Trading" / "claude-x", self.reg, r)
        self.assertIn("prefix-dir-case:Trading", res.reasons)
        res = L.explain_lane_name(self.home / "apps" / "lanes" / "nonesuch" / "claude-x", self.reg, r)
        self.assertIn("unknown-prefix-dir:nonesuch", res.reasons)
        self.assertIsNone(res.app)

    def test_paths_outside_a_lane_location_are_non_lane(self) -> None:
        r = self.roots()
        cases = {
            self.home / "Code" / "BotFleet": LC.INTEGRATION_TREE,
            self.home / "Code" / "BotFleet" / ".claude" / "worktrees" / "x": LC.MANAGED,
            self.home / "apps" / "lanes" / "_review" / "botfleet-claude-x": LC.REVIEW,
            self.home / ".codex" / "worktrees" / "x" / "BotFleet": LC.MANAGED,
            self.home / "Documents" / "trading-claude-x": LC.UNSANCTIONED,
            self.home / "apps" / "_pr-lanes": LC.UNSANCTIONED,
            self.home / "apps": LC.UNSANCTIONED,
        }
        for path, cls in cases.items():
            res = L.explain_lane_name(path, self.reg, r)
            self.assertEqual(res.verdict, NV.NON_LANE, path)
            self.assertEqual(res.location, cls, path)
            self.assertEqual(res.reasons, (f"location:{cls}",), path)
        fake_tmp = self.mkdir("scratch-tmp")
        r_tmp = dataclasses.replace(r, tmp_roots=(pathlib.Path(os.path.realpath(fake_tmp)),), tmp_globs=())
        res = L.explain_lane_name(fake_tmp / "trading-claude-x", self.reg, r_tmp)
        self.assertEqual((res.verdict, res.location), (NV.NON_LANE, LC.FORBIDDEN_TMP))

    def test_flat_mode_does_not_judge_the_nested_form(self) -> None:
        flat = self.roots({"FLEET_LAYOUT": "flat"})
        res = L.explain_lane_name(self.home / "apps" / "lanes" / "trading" / "claude-x", self.reg, flat)
        self.assertEqual((res.verdict, res.location), (NV.NON_LANE, LC.UNSANCTIONED))

    def test_a_lane_inside_a_lane_is_judged_by_the_lane_directory(self) -> None:
        r = self.roots()
        res = L.explain_lane_name(self.home / "apps" / "claude-st-rotfix" / "repo", self.reg, r)
        self.assertEqual(res.verdict, NV.NAME_DRIFT)
        res = L.explain_lane_name(self.home / "apps" / "botfleet-claude-x" / "repo", self.reg, r)
        self.assertEqual(res.verdict, NV.CONFORMING)
        self.assertEqual(L.lane_root(self.home / "apps" / "botfleet-claude-x" / "repo", r),
                         self.home / "apps" / "botfleet-claude-x")


class BranchNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = _registry()

    def check(self, branch: str | None) -> BranchVerdict:
        return L.check_branch_name(branch, self.reg)

    def test_fixtures(self) -> None:
        cases = {
            "claude/worktree-branch-folder-structure-f5651a": BranchVerdict.CONFORMING,
            "ag/cua-and-settings-audit": BranchVerdict.CONFORMING,
            "minimax/ios-ship-cold-cache-baseline": BranchVerdict.CONFORMING,
            "grok-build/explicit-room-members": BranchVerdict.CONFORMING,
            "grok/speech-markup": BranchVerdict.CONFORMING,
            "muse/x": BranchVerdict.CONFORMING,
            "muse-assist/x": BranchVerdict.CONFORMING,
            "cursor/x": BranchVerdict.CONFORMING,
            "refs/heads/claude/x": BranchVerdict.CONFORMING,
            "antigravity/ios-remove-hardcoded-prices": BranchVerdict.LEGACY_BRANCH,
            "mm/slack-poller-fixes": BranchVerdict.LEGACY_BRANCH,
            "gb/fix-altool-auth-staging": BranchVerdict.LEGACY_BRANCH,
            "Muse-Assist/muse-no-report-fix": BranchVerdict.LEGACY_BRANCH,
            "Claude/x": BranchVerdict.LEGACY_BRANCH,
            "agent/claude": BranchVerdict.LEGACY_BRANCH,
            "agent/claude-fix": BranchVerdict.LEGACY_BRANCH,
            "agent/antigravity": BranchVerdict.LEGACY_BRANCH,
            "agent/anything-at-all": BranchVerdict.LEGACY_BRANCH,
            "compiler/asc-seq-surface-stderr": BranchVerdict.NON_CONFORMING,
            "kody/734": BranchVerdict.NON_CONFORMING,
            "bf-compiler/win-lsof": BranchVerdict.NON_CONFORMING,
            "fix/plist-comment": BranchVerdict.NON_CONFORMING,
            "bfc/verify-merge-75": BranchVerdict.NON_CONFORMING,
            "claude/": BranchVerdict.NON_CONFORMING,
            "claude": BranchVerdict.NON_CONFORMING,
            "tmp": BranchVerdict.NON_CONFORMING,
            "main": BranchVerdict.DEFAULT_BRANCH,
            "master": BranchVerdict.DEFAULT_BRANCH,
            None: BranchVerdict.DETACHED,
            "": BranchVerdict.DETACHED,
            "  ": BranchVerdict.DETACHED,
            "HEAD": BranchVerdict.DETACHED,
        }
        for branch, want in cases.items():
            self.assertEqual(self.check(branch), want, repr(branch))

    def test_spec_strings(self) -> None:
        self.assertEqual(self.check("ag/x"), "CONFORMING")
        self.assertEqual(self.check("agent/antigravity"), "LEGACY-BRANCH")
        self.assertEqual(self.check("fix/x"), "NON-CONFORMING")
        self.assertEqual(f"{self.check('mm/x')}", "LEGACY-BRANCH")
        self.assertEqual({v.value for v in BranchVerdict},
                         {"CONFORMING", "LEGACY-BRANCH", "NON-CONFORMING", "DEFAULT-BRANCH", "DETACHED"})

    def test_seat_from_branch(self) -> None:
        cases = {
            "ag/x": "antigravity", "agent/antigravity": "antigravity", "antigravity/x": "antigravity",
            "mm/x": "minimax", "minimax/x": "minimax", "agent/claude-fix": "claude", "claude/x": "claude",
            "cursor/x": "cursor", "gb/x": "cursor", "muse/x": "muse-assist", "Muse-Assist/x": "muse-assist",
            "grok-build/x": "grok-build", "refs/heads/codex/x": "codex",
            "kody/734": None, "fix/x": None, "main": None, "claude/": None, "agent/zzz": None, None: None,
        }
        for branch, want in cases.items():
            self.assertEqual(L.seat_from_branch(branch, self.reg), want, repr(branch))

    def test_upgrade_with_branch(self) -> None:
        up = lambda v, b: L.upgrade_with_branch(v, b, self.reg)  # noqa: E731
        self.assertEqual(up(NV.NON_LANE, "grok/cpu-mach-timebase"), NV.NAME_DRIFT)
        self.assertEqual(up(NV.NON_LANE, "bf-compiler/cleantier"), NV.NON_LANE)
        self.assertEqual(up(NV.NON_LANE, "main"), NV.NON_LANE)
        self.assertEqual(up(NV.NON_LANE, None), NV.NON_LANE)
        for verdict in (NV.CONFORMING, NV.ALIAS_ONLY, NV.NAME_DRIFT):
            self.assertEqual(up(verdict, "grok/x"), verdict)


# --------------------------------------------------------------------------- old interpreters

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2]
MAC_SYSTEM_PYTHON = "/usr/bin/python3"  # 3.9 on macOS: what a hook sees under a minimal PATH


class StrEnumFallbackTests(unittest.TestCase):
    """layout.py must import on Python 3.9.  The temp-dir guard hook imports it and treats every
    exception as an allow, so an ImportError here silently turns the guard off."""

    def load_without_strenum(self):
        """layout.py executed again as if `enum.StrEnum` (new in 3.11) did not exist."""
        name = "fleet_lanes_layout_without_strenum"
        spec = importlib.util.spec_from_file_location(name, L.__file__)
        module = importlib.util.module_from_spec(spec)
        saved = getattr(enum, "StrEnum", None)
        if saved is not None:
            del enum.StrEnum
        sys.modules[name] = module  # dataclasses looks its own module up while the class body runs
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
            if saved is not None:
                enum.StrEnum = saved
        return module

    def test_the_module_imports_when_enum_has_no_strenum(self) -> None:
        mod = self.load_without_strenum()
        self.assertTrue(issubclass(mod.LocationClass, str))
        self.assertEqual(mod.LocationClass.FORBIDDEN_TMP, "FORBIDDEN_TMP")
        self.assertEqual(mod.LocationClass("UNSANCTIONED"), mod.LocationClass.UNSANCTIONED)

    def test_the_fallback_prints_and_serializes_like_strenum(self) -> None:
        mod = self.load_without_strenum()
        verdict = mod.NameVerdict.ALIAS_ONLY
        self.assertEqual(str(verdict), "ALIAS-ONLY")
        self.assertEqual(f"{verdict}", "ALIAS-ONLY")
        self.assertEqual("%s" % verdict, "ALIAS-ONLY")
        self.assertEqual(json.dumps({"v": verdict}), '{"v": "ALIAS-ONLY"}')
        self.assertIn(verdict, ("ALIAS-ONLY",))
        self.assertEqual({v.value for v in mod.BranchVerdict},
                         {"CONFORMING", "LEGACY-BRANCH", "NON-CONFORMING", "DEFAULT-BRANCH", "DETACHED"})

    @unittest.skipUnless(sys.platform == "darwin" and os.path.exists(MAC_SYSTEM_PYTHON),
                         "needs the macOS system python")
    def test_the_system_python_can_import_layout(self) -> None:
        code = ("import fleet_lanes.layout as L; "
                "print(L.LocationClass.FORBIDDEN_TMP, L.NameVerdict.NAME_DRIFT)")
        res = subprocess.run([MAC_SYSTEM_PYTHON, "-B", "-c", code], cwd=SCRIPTS_DIR, capture_output=True,
                             text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "FORBIDDEN_TMP NAME-DRIFT")


if __name__ == "__main__":
    unittest.main()
