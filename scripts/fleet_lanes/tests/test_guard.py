"""guard tests: the fixture table, deny reasons, payload shapes, fail-open, speed, purity, hook CLI.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_guard -v

Every context is built from a literal home (/Users/jay), an explicit env dict and the frozen
registry in fixtures_guard, with case-insensitive comparison passed explicitly, so neither the
real home nor the process environment leaks in.  No test touches the network, and the only files
written are a registry copy for the hook CLI tests, inside a TemporaryDirectory.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import guard as G
from fleet_lanes import lane_guard_hook as H
from fleet_lanes import layout as L
from fleet_lanes.tests import fixtures_guard as F

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
HOOK_PATH = SCRIPTS_DIR / "fleet_lanes" / "lane_guard_hook.py"
REGISTRY = L.parse_registry(F.REGISTRY_DATA)


def _ctx(env: dict | None = None, **extra: str) -> G.GuardContext:
    e = dict(F.BASE_ENV if env is None else env)
    e.update(extra)
    return G.make_context(e, home=F.HOME, registry=REGISTRY, case_insensitive=True)


def _payload(command, cwd: str | None = F.HOME, scratchpad_dir: str | None = None, tool: str = "Bash") -> dict:
    p: dict = {"session_id": "test", "hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": {"command": command, "description": "test"}}
    if cwd is not None:
        p["cwd"] = cwd
    if scratchpad_dir:
        p["scratchpad_dir"] = scratchpad_dir
    return p


def _run(fx: F.GuardFixture) -> G.Decision:
    ctx = _ctx(dict(fx.env) if fx.env is not None else None)
    return G.evaluate(_payload(fx.command, fx.cwd, fx.scratchpad_dir), ctx)


def _fixture(fid: str) -> F.GuardFixture:
    return next(f for f in F.FIXTURES if f.id == fid)


class FixtureTableTests(unittest.TestCase):
    def test_table_shape(self) -> None:
        self.assertGreaterEqual(len(F.FIXTURES), 60)
        self.assertGreaterEqual(len(F.DENY_FIXTURES), 30)
        self.assertGreaterEqual(len(F.ALLOW_FIXTURES), 30)
        ids = [f.id for f in F.FIXTURES]
        self.assertEqual(len(ids), len(set(ids)), "fixture ids must be unique")
        for fx in F.FIXTURES:
            self.assertTrue(fx.source, f"{fx.id} needs a source")
            self.assertIn(fx.expect, (G.ALLOW, G.DENY))
            if fx.expect == G.DENY:
                self.assertIn(fx.rule, G.DENY_RULES, fx.id)

    def test_rule_constants_match(self) -> None:
        self.assertEqual(
            (F.CLONE, F.GH_CLONE, F.WT_ADD, F.WT_MOVE, F.ARCHIVE, F.TARBALL, F.INIT_FETCH, F.EXPORT),
            (G.RULE_CLONE, G.RULE_GH_CLONE, G.RULE_WORKTREE_ADD, G.RULE_WORKTREE_MOVE, G.RULE_ARCHIVE,
             G.RULE_TARBALL, G.RULE_INIT_FETCH, G.RULE_EXPORT))

    def test_every_fixture(self) -> None:
        for fx in F.FIXTURES:
            with self.subTest(fx.id):
                d = _run(fx)
                self.assertEqual(d.action, fx.expect, f"{fx.id}: {d}")
                if fx.expect == G.DENY:
                    self.assertEqual(d.rule_id, fx.rule)
                    if fx.dest is not None:
                        self.assertEqual(d.destination, fx.dest)
                else:
                    self.assertEqual(d.reason, "")

    def test_deny_fixtures_pass_the_prefilter(self) -> None:
        # The keyword prefilter must never be what hides a deny.
        for fx in F.DENY_FIXTURES:
            with self.subTest(fx.id):
                self.assertIsNotNone(G._TRIGGER.search(fx.command))

    def test_allow_fixtures_hold_without_the_prefilter(self) -> None:
        # Allows must come from the parser, not from the keyword shortcut.
        with mock.patch.object(G, "_TRIGGER", re.compile("")):
            for fx in F.ALLOW_FIXTURES:
                with self.subTest(fx.id):
                    self.assertEqual(_run(fx).action, G.ALLOW)

    def test_scratchpad_needs_the_payload_dir_or_claude_uid(self) -> None:
        fx = _fixture("spec-scratch-clone")
        cmd = fx.command.replace("/private/tmp/claude-501", "/private/tmp/not-claude")
        self.assertEqual(G.evaluate(_payload(cmd), _ctx()).action, G.DENY)
        self.assertEqual(G.evaluate(_payload(cmd, scratchpad_dir=cmd.split()[-1].rsplit("/", 1)[0]),
                                    _ctx()).action, G.ALLOW)


class ReasonTests(unittest.TestCase):
    def _deny(self, fid: str = "spec-mm-dealdex-cd-clone", **env: str) -> G.Decision:
        fx = _fixture(fid)
        base = dict(F.BASE_ENV)
        for k, v in env.items():
            if v is None:
                base.pop(k, None)
            else:
                base[k] = v
        d = G.evaluate(_payload(fx.command, fx.cwd), _ctx(base))
        self.assertEqual(d.action, G.DENY)
        return d

    def test_every_deny_reason_is_one_actionable_paragraph(self) -> None:
        for fx in F.DENY_FIXTURES:
            with self.subTest(fx.id):
                r = _run(fx).reason
                self.assertNotIn("\n", r)
                self.assertIn("not allowed in temp directories", r)
                self.assertIn(_run(fx).destination, r)
                # `lane new` is the way for an app with an integration tree; the others keep the path.
                self.assertTrue("/lane new " in r or "lanes root" in r, r)
                self.assertIn("~/apps/lanes/", r)
                self.assertRegex(r, r"detached checkout at ~/apps/lanes/[A-Za-z0-9._<>-]+/review-pr-<n>")
                self.assertNotIn("_review", r)
                self.assertNotIn("review root", r)
                self.assertIn("main integration tree", r)
                self.assertIn("for the human", r)
                self.assertNotIn(G.ENV_GUARD, r)
                self.assertNotRegex(r.lower(), r"bypass|override|disable")
                self.assertIn(".  ", r)
                self.assertIsNone(re.search(r"[.!?] [A-Z]", r), "two spaces between sentences")
                self.assertNotIn(G._UNK, r)

    def test_lane_new_is_the_primary_instruction_then_the_concrete_lane(self) -> None:
        r = self._deny().reason
        self.assertIn("Run `~/apps/lane new DealDex work` instead; it creates branch claude/work off origin/main "
                      "and the lane at ~/apps/lanes/DealDex/claude-work.", r)
        self.assertIn("For a read-only PR check, run `~/apps/lane new DealDex --review --pr <n>` instead; it makes "
                      "a detached checkout at ~/apps/lanes/DealDex/review-pr-<n>.", r)
        self.assertIn("integration tree ~/Code/DealDex", r)
        self.assertIn("a clone of Simple-With-Us/DealDex at /tmp/dealdex-work", r)
        self.assertLess(r.index("`~/apps/lane new DealDex work`"), r.index("~/apps/lanes/DealDex/claude-work"))
        self.assertNotIn("git -C", r, "the raw git command is not offered next to lane new")
        self.assertEqual(len(re.findall(r"\.  ", r)), 3, "four sentences, two spaces between them")
        self.assertNotIn("AGENT_SEAT", r, "no seat hint once the seat is usable")

    def test_seat_placeholder_when_unset(self) -> None:
        r = self._deny(AGENT_SEAT=None).reason
        self.assertIn("~/apps/lanes/DealDex/<seat>-work", r)
        self.assertIn("branch <seat>/work", r)
        self.assertIn("Set AGENT_SEAT to your seat tag, then run `~/apps/lane new DealDex work` instead;", r)

    def test_a_seat_that_lane_new_would_refuse_gets_the_same_hint_as_an_unset_seat(self) -> None:
        # DSH and KIMI are retired in the fixture registry; antigravity is a whole name, not a tag.
        # GROK-BOT shares CURSOR's folder name and branch prefix, so the CLI refuses it too.
        for value in ("NOPE", "antigravity", "DSH", "KIMI", "  ", "GROK-BOT"):
            with self.subTest(value=value):
                r = self._deny(AGENT_SEAT=value).reason
                self.assertIn("Set AGENT_SEAT to your seat tag, then run `~/apps/lane new DealDex work` instead;", r)
                self.assertIn("branch <seat>/work", r)
                self.assertIn("~/apps/lanes/DealDex/<seat>-work", r)
                self.assertNotRegex(r, r"nope|deepseek|kimi|cursor")

    def test_a_target_name_that_would_read_as_another_seat_falls_back_to_work(self) -> None:
        # GROK plus slug build would be folder grok-build, which reads as GROK-BUILD's lane, and
        # grok-bot reads as the cursor seat; `lane new` refuses both, so the guard must not offer them.
        for target in ("/tmp/build", "/tmp/build-cache", "/tmp/bot", "/tmp/bot-x"):
            with self.subTest(target=target):
                d = G.evaluate(_payload(f"git clone {F._DD} {target}"), _ctx(AGENT_SEAT="GROK"))
                self.assertEqual(d.action, G.DENY)
                self.assertIn("Run `~/apps/lane new DealDex work` instead; it creates branch grok/work off "
                              "origin/main and the lane at ~/apps/lanes/DealDex/grok-work.", d.reason)
                self.assertNotRegex(d.reason, r"grok-build|grok-bot")
        d = G.evaluate(_payload(f"git clone {F._DD} /tmp/build"), _ctx(AGENT_SEAT="CLAUDE"))
        self.assertIn("`~/apps/lane new DealDex build`", d.reason, "a slug that reads back is kept")

    def test_seat_tags_are_normalized(self) -> None:
        r = self._deny(AGENT_SEAT="AG").reason
        self.assertIn("~/apps/lanes/DealDex/antigravity-work", r)
        self.assertIn("branch ag/work", r)
        self.assertNotIn("AGENT_SEAT", r)
        r = self._deny(AGENT_SEAT="MM").reason
        self.assertIn("~/apps/lanes/DealDex/minimax-work", r)
        self.assertIn("branch minimax/work", r)
        r = self._deny(AGENT_SEAT="mm").reason
        self.assertIn("branch minimax/work", r, "the CLI accepts a tag in any case")

    def test_flat_layout(self) -> None:
        r = self._deny(FLEET_LAYOUT="flat").reason
        self.assertIn("at ~/apps/dealdex-claude-work", r)

    def test_custom_lanes_root(self) -> None:
        r = self._deny(FLEET_LANES_ROOT="~/work/lanes").reason
        self.assertIn("~/work/lanes/DealDex/claude-work", r)
        self.assertIn("a detached checkout at ~/work/lanes/DealDex/review-pr-<n>.", r)

    def test_slug_from_destination(self) -> None:
        cases = {
            "spec-grok-botfleet-wt": "~/apps/lanes/BotFleet/claude-fix-thing",
            "spec-um-1595-private-tmp": "~/apps/lanes/Usage-Monitor/claude-1595-wt",
            "spec-gh-hoghunter": "~/apps/lanes/HogHunter/claude-verify",
            "spec-mktemp-clutch": "~/apps/lanes/Clutch/claude-repo",
            "spec-tmpdir-codecaps": "~/apps/lanes/CodeCaps/claude-work",
            "spec-mm-afc-baseline-cwd": "~/apps/lanes/AI-Fleet-Coordinator/claude-baseline",
            "sweep-bf-st4013-workspace-clone": "~/apps/lanes/Socratic-Trade/claude-st4013",
            "var-mktemp-inline": "~/apps/lanes/ContactLogo/claude-work",     # cl is the app's acronym
            "spec-mktemp-template-botfleet": "~/apps/lanes/BotFleet/claude-work",  # so is bf
        }
        for fid, lane in cases.items():
            with self.subTest(fid):
                self.assertIn(lane, _run(_fixture(fid)).reason)

    def test_app_without_integration_tree(self) -> None:
        d = G.evaluate(_payload("gh repo clone Simple-With-Us/Kodus-Config /tmp/kc"), _ctx())
        self.assertEqual(d.rule_id, G.RULE_GH_CLONE)
        self.assertIn("integration tree ~/Code/<App>", d.reason)
        self.assertIn("gh repo clone Simple-With-Us/Kodus-Config ~/apps/lanes/Kodus-Config/claude-kc", d.reason)
        self.assertNotIn("lane new", d.reason, "lane new refuses an app with no integration tree")
        self.assertIn("lanes root", d.reason)

    def test_unregistered_app_keeps_the_lane_path_and_the_manual_command(self) -> None:
        # The fixture registry has no CodeCaps row (the real one does now), so the CLI would refuse it here.
        r = _run(_fixture("spec-tmpdir-codecaps")).reason
        self.assertNotIn("lane new", r)
        self.assertIn("at ~/apps/lanes/CodeCaps/claude-work", r)
        self.assertIn("git -C ~/Code/CodeCaps worktree add -b claude/work ~/apps/lanes/CodeCaps/claude-work origin/main", r)

    def test_a_folder_sharing_seat_gets_a_placeholder_in_the_manual_command_too(self) -> None:
        # GROK-BOT's folder name and branch prefix are CURSOR's, so the raw git command must not
        # hand it cursor-work on cursor/work either.
        r = self._deny("spec-tmpdir-codecaps", AGENT_SEAT="GROK-BOT").reason
        self.assertIn("~/apps/lanes/CodeCaps/<seat>-work", r)
        self.assertIn("worktree add -b <seat>/work", r)
        self.assertNotIn("cursor", r)

    def test_unregistered_repo_of_a_fleet_owner(self) -> None:
        d = G.evaluate(_payload("git clone https://github.com/Simple-With-Us/NewThing.git /tmp/nt"), _ctx())
        self.assertEqual(d.action, G.DENY)
        self.assertIn("~/apps/lanes/NewThing/claude-nt", d.reason, "the repo name as written, not a lowercase prefix")
        self.assertIn("~/apps/lanes/NewThing/review-pr-<n>", d.reason)
        self.assertNotIn("lane new", d.reason, "lane new does not know a repo that is not in the registry")

    def test_unknown_app_uses_placeholders(self) -> None:
        r = _run(_fixture("var-for-loop-unknown-name")).reason
        self.assertIn("~/apps/lanes/<Repo>/claude-check", r)
        self.assertIn("review-pr-<n>", r)
        self.assertNotIn("<prefix>", r)
        self.assertIn("Simple-With-Us/*", r)
        self.assertNotIn("lane new", r)

    def test_repo_folders_with_capitals_and_dots_are_named_in_full(self) -> None:
        # Layout v2 spells the folder as the human tree is spelled.  The old lowercase-kebab gate on the
        # folder would have turned Congress.Trade and AI-Fleet-Coordinator into a `<prefix>` placeholder.
        cases = {
            "https://github.com/Simple-With-Us/Congress.Trade.git": "Congress.Trade",
            "https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git": "AI-Fleet-Coordinator",
            "https://github.com/Simple-With-Us/congress-trading-shared.git": "congress-trading-shared",
            "https://github.com/Simple-With-Us/Socratic-Trade.git": "Socratic-Trade",
        }
        for url, folder in cases.items():
            with self.subTest(folder):
                d = G.evaluate(_payload(f"git clone {url} /tmp/work"), _ctx())
                self.assertEqual(d.action, G.DENY)
                self.assertIn(f"the lane at ~/apps/lanes/{folder}/claude-work.", d.reason)
                self.assertIn(f"a detached checkout at ~/apps/lanes/{folder}/review-pr-<n>.", d.reason)
                self.assertNotIn("<Repo>", d.reason)
                self.assertNotIn("<prefix>", d.reason)

    def test_repo_folder_placeholder_is_repo_not_prefix(self) -> None:
        r = _run(_fixture("var-for-loop-unknown-name")).reason
        self.assertIn("lanes/<Repo>/", r)
        self.assertNotIn("<prefix>", r)

    def test_a_lane_name_in_an_old_prefix_folder_still_reads_back(self) -> None:
        # `lane new` stays idempotent on a lane that has not migrated yet, and the guard still accepts it
        reg = REGISTRY
        for prefix in ("trading", "fleet", "congress", "botfleet", "dealdex"):
            self.assertTrue(G.lane_name_reads_back(prefix, "claude", "x", reg), prefix)
        self.assertFalse(G.lane_name_reads_back("nonesuch", "claude", "x", reg))
        self.assertFalse(G.lane_name_reads_back("Socratic-Trade", "claude", "x", reg), "the argument is a prefix")
        self.assertTrue(G.lane_name_reads_back("trading", "claude", "x", reg, flat=True))
        self.assertFalse(G.lane_name_reads_back("trading", "grok", "build", reg), "grok-build is another seat's")

    def test_credentials_never_reach_the_decision(self) -> None:
        d = _run(_fixture("var-credential-url"))
        for text in (d.reason, d.source, d.destination):
            self.assertNotIn("hunter2", text)
            self.assertNotIn("agentuser", text)
        self.assertEqual(d.source, "Simple-With-Us/DealDex")

    def test_redact_url(self) -> None:
        self.assertEqual(G.redact_url("https://user:tok@github.com/o/r.git"), "https://REDACTED@github.com/o/r.git")
        self.assertEqual(G.redact_url("clone https://tok@github.com/o/r x"), "clone https://REDACTED@github.com/o/r x")
        self.assertEqual(G.redact_url("ssh://git@github.com/o/r.git"), "ssh://git@github.com/o/r.git")
        self.assertEqual(G.redact_url("ssh://me:pw@host/r"), "ssh://REDACTED@host/r")
        self.assertEqual(G.redact_url("git@github.com:o/r.git"), "git@github.com:o/r.git")


class LaneNewAgreementTests(unittest.TestCase):
    """Whatever `lane new` command a deny reason names, the CLI accepts it with the same seat and
    prints the same lane path; and when the reason shows a placeholder, the CLI refuses that seat
    and the slug works for every seat it accepts.  The CLI runs against a throwaway home."""

    TARGETS = ("/tmp/build", "/tmp/build-cache", "/tmp/bot", "/tmp/bot-x", "/tmp/dealdex-work", "/tmp/code-x",
               "/tmp/assist-me", "/tmp/fix-login", "/tmp/muse-x", "/tmp/grok-build-x", "/tmp/claude-x",
               "/tmp/tmp", "/tmp/DealDex")

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="guard-lane-agree-")
        cls.home = os.path.realpath(cls._tmp.name)
        cls.registry_path = os.path.join(cls.home, "fleet-apps.json")
        with open(cls.registry_path, "w", encoding="utf-8") as fh:
            json.dump(F.REGISTRY_DATA, fh)
        cls.cache: dict = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _cli(self, tag: str, slug: str, flat: bool) -> tuple:
        from fleet_lanes import lane as K
        key = (tag, slug, flat)
        if key not in self.cache:
            env = {"HOME": self.home, "AGENT_SEAT": tag, "FLEET_APPS_JSON": self.registry_path,
                   "PATH": os.environ.get("PATH", "")}
            if flat:
                env["FLEET_LAYOUT"] = "flat"
            out, err = io.StringIO(), io.StringIO()
            rc = K.main(["path", "DealDex", slug], env=env, stdout=out, stderr=err)
            path = out.getvalue().strip()
            shown = "~/" + os.path.relpath(path, self.home) if path else ""
            self.cache[key] = (rc, shown, err.getvalue())
        return self.cache[key]

    def test_every_seat_and_target_in_both_layouts(self) -> None:
        from fleet_lanes import lane as K
        tags = [s.name for s in REGISTRY.seats] + ["NOPE"]
        accepted = [t for t in tags if self._cli(t, "work", False)[0] == 0]
        self.assertIn("GROK", accepted)
        self.assertNotIn("GROK-BOT", accepted)
        pattern = re.compile(r"[Rr]un `~/apps/lane new DealDex (\S+)` instead; it creates branch (\S+) off origin/main "
                             r"and the lane at (\S+)\.  ")
        for flat in (False, True):
            for tag in tags:
                for target in self.TARGETS:
                    with self.subTest(flat=flat, tag=tag, target=target):
                        ctx = _ctx(AGENT_SEAT=tag, **({"FLEET_LAYOUT": "flat"} if flat else {}))
                        d = G.evaluate(_payload(f"git clone {F._DD} {target}"), ctx)
                        self.assertEqual(d.action, G.DENY)
                        m = pattern.search(d.reason)
                        self.assertIsNotNone(m, d.reason)
                        slug, branch, lane = m.groups()
                        if tag in accepted:
                            self.assertNotIn("Set AGENT_SEAT", d.reason)
                            rc, shown, err = self._cli(tag, slug, flat)
                            self.assertEqual(rc, 0, err)
                            self.assertEqual(shown, lane)
                            self.assertEqual(branch, K.branch_for(REGISTRY.seat_by_name(tag), slug))
                        else:
                            self.assertIn("Set AGENT_SEAT", d.reason)
                            self.assertEqual(self._cli(tag, slug, flat)[0], 64)
                            for other in accepted:
                                self.assertEqual(self._cli(other, slug, flat)[0], 0, f"{other} with slug {slug}")


class PayloadShapeTests(unittest.TestCase):
    CMD = "git worktree add /tmp/bf-x origin/main"

    def test_cursor_before_shell_execution(self) -> None:
        p = {"hook_event_name": "beforeShellExecution", "command": self.CMD, "cwd": "",
             "workspace_roots": ["/Users/jay/Code/BotFleet"]}
        self.assertEqual(G.evaluate(p, _ctx()).rule_id, G.RULE_WORKTREE_ADD)
        p["cwd"] = "/Users/jay/Code/BotFleet"
        p["workspace_roots"] = []
        self.assertEqual(G.evaluate(p, _ctx()).action, G.DENY)

    def test_antigravity_run_command(self) -> None:
        p = {"tool_name": "run_command", "tool_input": {"CommandLine": self.CMD, "Cwd": "/Users/jay/Code/BotFleet"}}
        self.assertEqual(G.evaluate(p, _ctx()).action, G.DENY)

    def test_codex_argv_command(self) -> None:
        p = {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", self.CMD],
                                                   "workdir": "/Users/jay/Code/BotFleet"}}
        self.assertEqual(G.evaluate(p, _ctx()).action, G.DENY)
        p["tool_input"]["command"] = ["git", "clone", "https://github.com/Simple-With-Us/DealDex.git", "/tmp/dd x"]
        d = G.evaluate(p, _ctx())
        self.assertEqual((d.action, d.destination), (G.DENY, "/tmp/dd x"))

    def test_per_call_directory_beats_session_cwd(self) -> None:
        p = {"tool_name": "shell", "cwd": F.HOME,
             "tool_input": {"command": self.CMD, "workdir": "/Users/jay/Code/BotFleet"}}
        self.assertEqual(G.evaluate(p, _ctx()).action, G.DENY)
        p = {"tool_name": "run_command", "cwd": "/Users/jay/apps/lanes/DealDex/claude-x",
             "tool_input": {"CommandLine": "git clone https://github.com/Simple-With-Us/DealDex.git", "Cwd": "/tmp/x"}}
        d = G.evaluate(p, _ctx())
        self.assertEqual((d.action, d.destination), (G.DENY, "/tmp/x/DealDex"))

    def test_non_shell_tools_are_ignored(self) -> None:
        for tool in ("Write", "Edit", "Read", "WebFetch", "Task"):
            with self.subTest(tool):
                p = _payload("git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd", tool=tool)
                self.assertEqual(G.evaluate(p, _ctx()), G.Decision(G.ALLOW, "", "not-applicable"))

    def test_missing_cwd(self) -> None:
        # A relative destination is unknowable without a cwd; an absolute temp one is still denied.
        self.assertEqual(G.evaluate(_payload("git clone https://github.com/Simple-With-Us/DealDex.git dd", cwd=None),
                                    _ctx()).action, G.ALLOW)
        self.assertEqual(G.evaluate(_payload("gh repo clone Simple-With-Us/DealDex /tmp/dd", cwd=None),
                                    _ctx()).action, G.DENY)
        self.assertEqual(G.evaluate(_payload(self.CMD, cwd="relative/dir"), _ctx()).action, G.ALLOW)


class FailOpenTests(unittest.TestCase):
    def test_garbage_payloads(self) -> None:
        cmd = "git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd"
        garbage = [None, [], "text", 42, 3.5, b"bytes", {}, {"tool_name": "Bash"},
                   {"tool_name": "Bash", "tool_input": None}, {"tool_name": "Bash", "tool_input": "x"},
                   {"tool_name": "Bash", "tool_input": {"command": 123}},
                   {"tool_name": "Bash", "tool_input": {"command": None}},
                   {"tool_name": "Bash", "tool_input": {"command": ""}},
                   {"tool_name": "Bash", "tool_input": {"command": [1, 2]}},
                   {"tool_name": 7, "tool_input": {"command": cmd}},
                   {"tool_name": None, "tool_input": {"command": "git clone"}, "cwd": 5},
                   {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": ["/tmp"]},
                   {"command": {"nested": cmd}}]
        for p in garbage:
            with self.subTest(repr(p)[:60]):
                d = G.evaluate(p, _ctx())
                self.assertIsInstance(d, G.Decision)
                if not (isinstance(p, dict) and p.get("cwd") == ["/tmp"]):
                    self.assertEqual(d.action, G.ALLOW)

    def test_malformed_shell_never_raises(self) -> None:
        nasty = [
            "git clone 'https://github.com/Simple-With-Us/DealDex.git /tmp/dd",
            'git clone "https://github.com/Simple-With-Us/DealDex.git /tmp/dd',
            "git clone $(https://github.com/Simple-With-Us/DealDex.git /tmp/dd",
            "git clone ${TMPDIR /tmp/dd",
            "git clone `mktemp -d",
            "cat <<EOF\ngit clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd",
            "git worktree add )))) ((((",
            "git clone \\",
            "git clone \x00\x01\x02 /tmp/x",
            "git clone " + "$(" * 2000 + "x" + ")" * 2000,
            "git clone " + '"' * 999,
            "git -C",
            "git worktree add",
            "tar -xf",
            "curl -o",
            ";;;;&&&&||||",
        ]
        for cmd in nasty:
            with self.subTest(cmd[:40]):
                d = G.evaluate(_payload(cmd), _ctx())
                self.assertIn(d.action, (G.ALLOW, G.DENY))

    def test_internal_error_allows(self) -> None:
        with mock.patch.object(G._Evaluator, "run", side_effect=RuntimeError("boom")):
            d = G.evaluate(_payload(_fixture("spec-gh-hoghunter").command), _ctx())
        self.assertEqual((d.action, d.rule_id), (G.ALLOW, "fail-open"))

    def test_one_bad_segment_does_not_stop_the_rest(self) -> None:
        with mock.patch.object(G._Evaluator, "_tar", side_effect=RuntimeError("boom")):
            d = G.evaluate(_payload("tar -xf a.tgz -C /tmp/a; gh repo clone Simple-With-Us/HogHunter /tmp/hh"), _ctx())
        self.assertEqual(d.rule_id, G.RULE_GH_CLONE)

    def test_too_large_allows(self) -> None:
        cmd = "gh repo clone Simple-With-Us/HogHunter /tmp/hh;" + " " * G.MAX_COMMAND_CHARS
        self.assertEqual(G.evaluate(_payload(cmd), _ctx()).rule_id, "too-large")

    def test_disable_switch(self) -> None:
        cmd = _fixture("spec-gh-hoghunter").command
        for value in ("off", "OFF", "0", "false", "disabled"):
            with self.subTest(value):
                d = G.evaluate(_payload(cmd), _ctx(**{G.ENV_GUARD: value}))
                self.assertEqual((d.action, d.rule_id), (G.ALLOW, "disabled"))
        self.assertEqual(G.evaluate(_payload(cmd), _ctx(**{G.ENV_GUARD: "on"})).action, G.DENY)

    def test_unreadable_registry_falls_back(self) -> None:
        env = dict(F.BASE_ENV, FLEET_APPS_JSON="/nonexistent/fleet-apps.json")
        ctx = G.make_context(env, home=F.HOME, case_insensitive=True)
        self.assertEqual(ctx.registry.source, "fallback")
        # Owner and path rules still work without the registry.
        self.assertEqual(G.evaluate(_payload("gh repo clone Simple-With-Us/HogHunter /tmp/hh"), ctx).action, G.DENY)
        self.assertEqual(G.evaluate(_payload("git -C ~/Code/BotFleet worktree add /tmp/bf"), ctx).action, G.DENY)

    def test_default_context_from_the_environment(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in (G.ENV_GUARD, "FLEET_APPS_JSON")}
        with mock.patch.dict(os.environ, env, clear=True):
            d = G.evaluate(_payload("gh repo clone Simple-With-Us/HogHunter /tmp/hh-verify"))
        self.assertEqual(d.rule_id, G.RULE_GH_CLONE)


class PurityTests(unittest.TestCase):
    def test_evaluate_touches_no_filesystem_and_runs_nothing(self) -> None:
        ctxs = {}
        for fx in F.FIXTURES:
            key = tuple(sorted((fx.env or F.BASE_ENV).items()))
            if key not in ctxs:
                ctxs[key] = _ctx(dict(fx.env) if fx.env is not None else None)
        calls: list[str] = []

        def record(name):
            def fn(*a, **k):
                calls.append(name)
                raise AssertionError(f"{name} called")
            return fn

        patches = [mock.patch("os.path.realpath", record("realpath")),
                   mock.patch("os.path.exists", record("exists")),
                   mock.patch("os.path.lexists", record("lexists")),
                   mock.patch("os.path.isdir", record("isdir")),
                   mock.patch("os.stat", record("stat")),
                   mock.patch("os.lstat", record("lstat")),
                   mock.patch("os.listdir", record("listdir")),
                   mock.patch("os.scandir", record("scandir")),
                   mock.patch("builtins.open", record("open")),
                   mock.patch("subprocess.Popen", record("Popen"))]
        results = []
        for p in patches:
            p.start()
        try:
            for fx in F.FIXTURES:
                ctx = ctxs[tuple(sorted((fx.env or F.BASE_ENV).items()))]
                results.append((fx, G.evaluate(_payload(fx.command, fx.cwd, fx.scratchpad_dir), ctx)))
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(calls, [])
        for fx, d in results:
            self.assertEqual(d.action, fx.expect, fx.id)


def _big_command(target: int = 20_000, deny_at_end: bool = True) -> str:
    """A long, parse-heavy command: every segment carries a trigger word, so the keyword shortcut
    cannot skip the parse."""
    parts = [
        "cd /tmp/work-{i} && git -C /tmp/dealdex-work-{i} status --short",
        "git clone --depth 1 https://github.com/someone/else-{i}.git /tmp/else-{i}",
        'd=$(mktemp -d) && git init "$d" && git -C "$d" commit --allow-empty -m "wip {i}"',
        "git -C ~/Code/BotFleet worktree add -b claude/x{i} ~/apps/lanes/BotFleet/claude-x{i} origin/main",
        "(cd ~/apps/lanes/DealDex/claude-{i} && git fetch origin && git pull --rebase) || true",
        "curl -sL https://example.com/pkg-{i}.tgz | tar -xz -C /tmp/pkg-{i}",
        "git worktree remove --force /tmp/um-wt-{i}; rm -rf /tmp/bf{i} /tmp/bf{i}.tgz",
        "echo 'git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd-{i}' >> /tmp/notes-{i}.txt",
        "cat > ~/apps/lanes/AI-Fleet-Coordinator/claude-x/n{i}.md <<'EOF'\ngit clone https://github.com/Simple-With-Us/X.git /tmp/x{i}\nit's fine\nEOF",
        'git -C "$HOME/Code/Usage-Monitor" archive HEAD | tar -t | grep -c "worktree{i}"',
    ]
    out: list[str] = []
    size = 0
    i = 0
    while size < target:
        seg = parts[i % len(parts)].format(i=i)
        out.append(seg)
        size += len(seg) + 1
        i += 1
    if deny_at_end:
        out.append("gh repo clone Simple-With-Us/HogHunter /tmp/hh-verify")
    return "\n".join(out)


class PerfTests(unittest.TestCase):
    LIMIT_S = 0.050

    def _best_of(self, cmd: str, n: int = 3) -> tuple[float, G.Decision]:
        ctx = _ctx()
        best = float("inf")
        d = None
        for _ in range(n):
            t0 = time.perf_counter()
            d = G.evaluate(_payload(cmd), ctx)
            best = min(best, time.perf_counter() - t0)
        return best, d

    def test_20kb_command_under_50ms(self) -> None:
        cmd = _big_command()
        self.assertGreaterEqual(len(cmd), 20_000)
        took, d = self._best_of(cmd)
        self.assertEqual(d.rule_id, G.RULE_GH_CLONE, "the deny at the very end proves the whole command was parsed")
        self.assertLess(took, self.LIMIT_S, f"{took * 1000:.1f} ms")

    def test_20kb_allowed_command_under_50ms(self) -> None:
        cmd = _big_command(deny_at_end=False)
        took, d = self._best_of(cmd)
        self.assertEqual((d.action, d.rule_id), (G.ALLOW, "no-match"))
        self.assertLess(took, self.LIMIT_S, f"{took * 1000:.1f} ms")


class LexerTests(unittest.TestCase):
    def _words(self, s: str) -> list:
        ev = G._Evaluator(_ctx(), None)
        st = G._State(F.HOME)
        out = []
        for tok in G._lex(s):
            if tok[0] == "w":
                out.append(ev._expand(tok[1], st))
            elif tok[0] == "op":
                out.append(("op", tok[1]))
            else:
                out.append(("r", tok[1], tok[3], tok[4]))
        return out

    def test_quotes_and_escapes(self) -> None:
        self.assertEqual(self._words("""a 'b c' "d $HOME" e\\ f '$HOME'"""),
                         ["a", "b c", "d /Users/jay", "e f", "$HOME"])

    def test_redirections_are_not_words(self) -> None:
        w = self._words("git clone x y > /tmp/log 2>&1")
        self.assertEqual(w[:4], ["git", "clone", "x", "y"])
        self.assertEqual([t[1] for t in w[4:]], [">", ">&"])
        self.assertEqual(w[5][3], "2")

    def test_heredoc_body_is_captured_not_lexed(self) -> None:
        toks = G._lex("cat <<'EOF' > f\ngit clone x /tmp/y\nEOF\necho done")
        bodies = [t[3] for t in toks if t[0] == "r" and t[1] == "<<"]
        self.assertEqual(bodies, ["git clone x /tmp/y"])
        words = [w for w in self._words("cat <<'EOF' > f\ngit clone x /tmp/y\nEOF\necho done") if isinstance(w, str)]
        self.assertEqual(words, ["cat", "echo", "done"])      # f is a redirect target, the body is not lexed

    def test_scan_balanced_skips_heredoc_prose(self) -> None:
        s = "$(cat <<'EOF'\nit's (1) don't\nEOF\n) tail"
        inner, end = G._scan_balanced(s, 2)
        self.assertEqual(s[end:], " tail")
        self.assertIn("don't", inner)

    def test_mktemp_forms(self) -> None:
        ev = G._Evaluator(_ctx(), None)
        st = G._State("/Users/jay/apps/x")
        self.assertEqual(ev._mktemp(["-d"], st), F.TMPDIR_N + "/tmp.XXXXXXXXXX")
        self.assertEqual(ev._mktemp(["-d", "-t", "pre"], st), F.TMPDIR_N + "/pre")
        self.assertEqual(ev._mktemp(["-d", "/tmp/gt.XXXX"], st), "/tmp/gt.XXXX")
        self.assertEqual(ev._mktemp(["-d", "rel.XXXX"], st), "/Users/jay/apps/x/rel.XXXX")
        self.assertEqual(ev._mktemp(["-p", "/var/tmp", "-d"], st), "/var/tmp/tmp.XXXXXXXXXX")
        self.assertEqual(ev._mktemp(["--tmpdir=/opt/t", "x.XXXX"], st), "/opt/t/x.XXXX")

    def test_where(self) -> None:
        ev = G._Evaluator(_ctx(), F.SCRATCH)
        self.assertEqual(ev._where("/tmp/x"), "temp")
        self.assertEqual(ev._where("/private/var/folders/aa/bb/T/x"), "temp")
        self.assertEqual(ev._where(F.TMPDIR_N + "/x"), "temp")
        self.assertEqual(ev._where("/tmp/gatetest-" + G._UNK), "temp")
        self.assertEqual(ev._where(G._UNK + "/repo"), "unknown")
        self.assertEqual(ev._where("/Users/jay/apps/" + G._UNK), "unknown")
        self.assertEqual(ev._where("/Users/jay/apps/lanes/x"), "other")
        self.assertEqual(ev._where(F.SCRATCH + "/dd"), "scratch")
        self.assertEqual(ev._where(F.SCRATCH.replace("/private/tmp/", "/tmp/") + "/dd"), "scratch")
        self.assertEqual(ev._where("/var/folders/aa/bb/T/claude-501/s/x"), "scratch")
        self.assertEqual(ev._where("/tmp/claude-x/s"), "temp")


class ReviewFindingTests(unittest.TestCase):
    """Edges behind the 2026-10-07 review findings that the fixture table shows only by example."""

    def test_implicit_destination_keeps_a_known_base(self) -> None:
        self.assertEqual(G._child("/tmp", G._UNK), "/tmp/" + G._UNK)
        self.assertEqual(G._child("/tmp/x", "DealDex"), "/tmp/x/DealDex")
        self.assertEqual(G._child(G._UNK, "DealDex"), G._UNK)
        self.assertEqual(G._child("/tmp", ""), G._UNK)
        # _join itself must stay strict: a leading unknown may be absolute (unknown-dest-var).
        self.assertEqual(G._join("/tmp", G._UNK), G._UNK)

    def test_whole_tree_pathspecs(self) -> None:
        base = "/Users/jay/Code/BotFleet/scripts"
        for spec in ("", ".", "./", "..", "../", "./*", "*", "*.ts", "**/x", "s*/x", "../*", ":/", ":(top)",
                     ":(glob)src", "/", "/Users/jay/Code/BotFleet", "/Users/jay/Code/BotFleet/scripts/"):
            with self.subTest(spec=spec):
                self.assertTrue(G._whole_tree_pathspec(spec, base))
        for spec in ("package.json", "src", "src/*", "src/**/x", "./x.mjs", "../package.json",
                     "/Users/jay/Code/BotFleet/scripts/x.mjs", G._UNK, "src/" + G._UNK):
            with self.subTest(spec=spec):
                self.assertFalse(G._whole_tree_pathspec(spec, base))
        # An unknown base: only the forms that are whole-tree from any directory count.
        for spec, whole in ((".", True), ("..", True), ("../x", True), ("*", True), ("src", False),
                            ("/abs/x", False)):
            with self.subTest(spec=spec, base="unknown"):
                self.assertEqual(G._whole_tree_pathspec(spec, G._UNK), whole)

    def test_exclude_pathspecs(self) -> None:
        for spec in (":!docs", ":^docs", ":/!docs", ":(exclude)docs", ":(top,exclude)docs"):
            with self.subTest(spec=spec):
                self.assertTrue(G._exclude_pathspec(spec))
        for spec in (":/docs", ":(top)docs", "docs", "!docs"):
            with self.subTest(spec=spec):
                self.assertFalse(G._exclude_pathspec(spec))

    def test_archive_excludes_beside_a_named_path(self) -> None:
        # Excludes narrow a named directory; on their own they mean everything else.
        allow = "git -C ~/Code/BotFleet archive HEAD -- scripts ':!scripts/test' | tar -x -C /tmp/bf-s"
        self.assertEqual(G.evaluate(_payload(allow), _ctx()).action, G.ALLOW)
        deny = "git -C ~/Code/BotFleet archive HEAD -- ':!docs' ':^README.md' | tar -x -C /tmp/bf-s"
        self.assertEqual(G.evaluate(_payload(deny), _ctx()).rule_id, G.RULE_ARCHIVE)

    def test_checkout_index_without_paths(self) -> None:
        # No paths and no -a writes nothing, but the rule stays on the safe side of it.
        d = G.evaluate(_payload("git -C ~/Code/DealDex checkout-index -f --prefix=/tmp/dd-none/"), _ctx())
        self.assertEqual((d.rule_id, d.destination), (G.RULE_EXPORT, "/tmp/dd-none"))


class HookCliTests(unittest.TestCase):
    DENY_CMD = "gh repo clone Simple-With-Us/HogHunter /tmp/hh-verify"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reg = Path(tmp.name) / "fleet-apps.json"
        reg.write_text(json.dumps(F.REGISTRY_DATA), encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in (G.ENV_GUARD, "FLEET_LAYOUT", "FLEET_LANES_ROOT", "AGENT_SEAT")}
        self.env.update({"HOME": F.HOME, "TMPDIR": F.TMPDIR, "AGENT_SEAT": "CLAUDE", "FLEET_APPS_JSON": str(reg)})
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv: list[str], stdin: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        rc = H.main(argv, io.StringIO(stdin), out, err)
        return rc, out.getvalue(), err.getvalue()

    def _deny_payload(self) -> str:
        return json.dumps(_payload(self.DENY_CMD))

    def test_claude_format(self) -> None:
        for argv in ([], ["--format", "claude"], ["--format=codex"], ["--format", "muse"], ["--format", "nonsense"]):
            with self.subTest(argv):
                rc, out, err = self._main(argv, self._deny_payload())
                self.assertEqual((rc, err), (0, ""))
                self.assertTrue(out.endswith("\n") and out.count("\n") == 1)
                body = json.loads(out)
                self.assertEqual(set(body), {"hookSpecificOutput"})
                hso = body["hookSpecificOutput"]
                self.assertEqual(hso["hookEventName"], "PreToolUse")
                self.assertEqual(hso["permissionDecision"], "deny")
                self.assertIn("/tmp/hh-verify", hso["permissionDecisionReason"])

    def test_grok_and_antigravity_format(self) -> None:
        for fmt in ("grok", "antigravity"):
            with self.subTest(fmt):
                rc, out, _ = self._main(["--format", fmt], self._deny_payload())
                body = json.loads(out)
                self.assertEqual(rc, 0)
                self.assertEqual(set(body), {"decision", "reason"})
                self.assertEqual(body["decision"], "deny")

    def test_cursor_format(self) -> None:
        payload = json.dumps({"hook_event_name": "beforeShellExecution", "command": self.DENY_CMD,
                              "cwd": F.HOME, "workspace_roots": [F.HOME]})
        rc, out, _ = self._main(["--format", "cursor"], payload)
        body = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertEqual(set(body), {"permission", "user_message", "agent_message"})
        self.assertEqual(body["permission"], "deny")
        self.assertEqual(body["user_message"], "Blocked a fleet-repo checkout in a temp directory: /tmp/hh-verify.")
        self.assertIn("`~/apps/lane new HogHunter verify`", body["agent_message"])

    def test_exit2_contract(self) -> None:
        rc, out, err = self._main(["--exit2"], self._deny_payload())
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("/tmp/hh-verify", err)

    def test_allow_prints_nothing(self) -> None:
        for argv in ([], ["--exit2"], ["--format", "cursor"]):
            with self.subTest(argv):
                self.assertEqual(self._main(argv, json.dumps(_payload("git status"))), (0, "", ""))

    def test_garbage_stdin_prints_nothing(self) -> None:
        for raw in ("", "not json", "[1,2", "null", "42", '{"tool_input": 5}'):
            with self.subTest(raw):
                self.assertEqual(self._main(["--exit2"], raw), (0, "", ""))

    def test_render_exact_bytes(self) -> None:
        self.assertEqual(H.render("R.  S.", "grok"), '{"decision": "deny", "reason": "R.  S."}')
        self.assertEqual(H.render("R.", "claude"),
                         '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
                         '"permissionDecisionReason": "R."}}')
        self.assertEqual(H.render("R.", "cursor", "/tmp/x"),
                         '{"permission": "deny", "user_message": "Blocked a fleet-repo checkout in a temp '
                         'directory: /tmp/x.", "agent_message": "R."}')

    def test_subprocess_by_path_and_module(self) -> None:
        deny = self._deny_payload().encode()
        allow = json.dumps(_payload("git status")).encode()
        for argv, cwd in (([sys.executable, str(HOOK_PATH), "--format", "grok"], None),
                          ([sys.executable, "-m", "fleet_lanes.lane_guard_hook", "--format", "grok"], str(SCRIPTS_DIR))):
            with self.subTest(argv[1]):
                p = subprocess.run(argv, input=deny, capture_output=True, cwd=cwd, env=self.env, timeout=30)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout)["decision"], "deny")
                p = subprocess.run(argv, input=allow, capture_output=True, cwd=cwd, env=self.env, timeout=30)
                self.assertEqual((p.returncode, p.stdout), (0, b""))


if __name__ == "__main__":
    unittest.main()
