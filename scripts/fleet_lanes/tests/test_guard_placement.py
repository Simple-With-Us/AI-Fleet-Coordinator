"""The widened guard rules, behind the mode file, against every probe the AFC-618 audit found allowed.

The table is the audit's probe list (Mac and server), plus the destinations each class must leave alone.  Each row runs
in detect, where the hook must still allow and name the class, and in block with only that class listed, so turning one
rule class on cannot turn on another.
"""
from __future__ import annotations

import unittest

from fleet_lanes import guard as G
from fleet_lanes import layout as L
from fleet_lanes import placement as P
from fleet_lanes.tests import fixtures_guard as F

HOME = "/Users/fixture-home"
LANES = f"{HOME}/apps/lanes"
FLEET = "https://github.com/Simple-With-Us/BotFleet.git"
THIRD = "https://github.com/someone/else.git"


def ctx(mode: str = "detect", *, env: dict | None = None, rows: tuple = (), host: str = "mac") -> G.GuardContext:
    full = {"HOME": HOME, "TMPDIR": "/tmp", "AGENT_SEAT": "CLAUDE", P.ENV_MODE_FILE: "/nonexistent", **(env or {})}
    made = G.make_context(full, home=HOME, registry=L.parse_registry(F.REGISTRY_DATA), case_insensitive=True)
    made.mode = P.parse_mode(mode)
    made.exceptions = rows
    made.host = host
    return made


def fallback_ctx(mode: str = "detect", *, env: dict | None = None, rows: tuple = (), host: str = "mac") -> G.GuardContext:
    """A context whose registry could not be read, so the guard holds the extras alone (AFC-811).

    The registry file is named and missing, so the fallback is the one the guard really takes when
    a host cannot read it, not a registry row built here to look like one."""
    full = {
        "HOME": HOME,
        "TMPDIR": "/tmp",
        "AGENT_SEAT": "CLAUDE",
        P.ENV_MODE_FILE: "/nonexistent",
        L.ENV_APPS_JSON: "/nonexistent/fleet-apps.json",
        **(env or {}),
    }
    made = G.make_context(full, home=HOME, case_insensitive=True)
    made.mode = P.parse_mode(mode)
    made.exceptions = rows
    made.host = host
    return made


def empty_registry_ctx(mode: str = "detect", *, env: dict | None = None, host: str = "mac") -> G.GuardContext:
    """A context whose registry was read and holds no owner apps of its own (AFC-811).

    The file is a real, readable fleet-apps.json with no apps in it, so the registry answers no
    membership question either.  The guard must not read that as a clearance."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = f"{tmp}/fleet-apps.json"
        with open(target, "w", encoding="utf-8") as fh:
            fh.write('{"owner": "Simple-With-Us", "apps": []}')
        full = {
            "HOME": HOME,
            "TMPDIR": "/tmp",
            "AGENT_SEAT": "CLAUDE",
            P.ENV_MODE_FILE: "/nonexistent",
            L.ENV_APPS_JSON: target,
            **(env or {}),
        }
        made = G.make_context(full, home=HOME, case_insensitive=True)
    made.mode = P.parse_mode(mode)
    made.host = host
    return made


def payload(command: str, cwd: str = f"{HOME}/Code/BotFleet", tool: str = "Bash") -> dict:
    return {"tool_name": tool, "tool_input": {"command": command}, "cwd": cwd}


# (the audit's probe, the rule class it must land in)
CLASSED = (
    (f"git clone {FLEET} ~/tmp-x", P.CLASS_HOME),
    (f"git clone {FLEET} {HOME}/fleetlink-qual/one", P.CLASS_HOME),
    (f"git clone {FLEET} ~/apps/botfleet-x", P.CLASS_APPS),
    (f"git clone {FLEET} ~/apps/scratch/afc-merge", P.CLASS_APPS),
    (f"git clone {FLEET} ~/apps/lanes/BotFleet/x", P.CLASS_CLONE_IN_LANES),
    ("git worktree add ~/apps/botfleet-fixer-1", P.CLASS_APPS),
    ("git worktree add ~/scratch-tree", P.CLASS_HOME),
    ("git worktree add /var/lib/nowhere", P.CLASS_ELSEWHERE),
    ("gh repo clone Simple-With-Us/BotFleet ~/tmp-z", P.CLASS_HOME),
    (f"git clone {FLEET} /dev/shm/x", P.CLASS_ELSEWHERE),
    (f"git clone --bare {FLEET} ~/apps/bare.git", P.CLASS_APPS),
)

# Allowed in every mode.
FINE = (
    "git worktree add ~/apps/lanes/BotFleet/claude-x",
    f"git clone {FLEET} ~/apps/lanes/_managed/fleet/runtime",
    f"git clone {FLEET} ~/apps/lanes/_codex/slug/BotFleet",
    f"git clone {THIRD} ~/tmp-x",
    f"git clone {THIRD} /dev/shm/x",
    "git worktree add ~/.cursor/worktrees/x",
    "git worktree add ~/.codex/worktrees/x",
    "git status",
    "git worktree list",
)

# The text parser cannot see through these.  They stay allowed here, and test_git_shim proves the shim sees the ones that
# reach git through PATH.  This row keeps the documented limit honest: if the parser learns one, say so in the docs.
INVISIBLE = (
    "python3 -c 'import subprocess; subprocess.run([\"git\", \"clone\", \"url\"])'",
    "sh ./do-clone.sh",
)


class Detect(unittest.TestCase):
    def test_each_probe_is_allowed_and_named(self) -> None:
        c = ctx("detect")
        for command, cls in CLASSED:
            with self.subTest(command=command):
                d = G.evaluate(payload(command), c)
                self.assertEqual(d.action, G.ALLOW)
                self.assertEqual(d.detected, cls)
                self.assertTrue(d.detected_detail["dest"])

    def test_fine_commands_name_nothing(self) -> None:
        for mode in ("detect", "block"):
            c = ctx(mode)
            for command in FINE:
                with self.subTest(command=command, mode=mode):
                    d = G.evaluate(payload(command), c)
                    self.assertEqual((d.action, d.detected), (G.ALLOW, ""))

    def test_the_documented_limit(self) -> None:
        c = ctx("block")
        for command in INVISIBLE:
            self.assertEqual(G.evaluate(payload(command), c).action, G.ALLOW)


class Block(unittest.TestCase):
    def test_block_denies_each_class_with_a_lane_new_path(self) -> None:
        for command, cls in CLASSED:
            with self.subTest(command=command):
                d = G.evaluate(payload(command), ctx(f"block {cls}"))
                self.assertEqual(d.action, G.DENY)
                self.assertEqual(d.rule_id, G.PLACEMENT_RULES[cls])
                self.assertIn("lane", d.reason)

    def test_one_class_at_a_time(self) -> None:
        for command, cls in CLASSED:
            other = next(c for c in (P.CLASS_HOME, P.CLASS_APPS) if c != cls)
            with self.subTest(command=command):
                d = G.evaluate(payload(command), ctx(f"block {other}"))
                self.assertEqual((d.action, d.detected), (G.ALLOW, cls))

    def test_off_records_nothing(self) -> None:
        d = G.evaluate(payload(f"git clone {FLEET} ~/tmp-x"), ctx("off"))
        self.assertEqual((d.action, d.detected), (G.ALLOW, ""))

    def test_the_kill_switch_allows_a_block(self) -> None:
        c = ctx("block", env={"FLEET_LANE_GUARD": "off"})
        self.assertEqual(G.evaluate(payload(f"git clone {FLEET} ~/tmp-x"), c).action, G.ALLOW)


class ExistingRulesStand(unittest.TestCase):
    """No live behaviour change on merge: temp and ~/Code deny whatever the mode file says."""

    def test_temp_and_code_still_deny_in_off_mode(self) -> None:
        c = ctx("off")
        for command in (f"git clone {FLEET} /tmp/x", "git worktree add /tmp/y", f"git clone {FLEET} ~/Code/Other"):
            with self.subTest(command=command):
                self.assertEqual(G.evaluate(payload(command), c).action, G.DENY)


class Compound(unittest.TestCase):
    def test_a_later_destination_in_a_blocked_class_denies(self) -> None:
        command = f"git clone {FLEET} ~/apps/first && git clone {FLEET} ~/second"
        d = G.evaluate(payload(command), ctx("block home"))
        self.assertEqual((d.action, d.rule_id), (G.DENY, G.RULE_PLACEMENT_HOME))

    def test_an_isolation_request_still_has_its_command_judged(self) -> None:
        body = {"tool_name": "Bash", "cwd": f"{LANES}/BotFleet/claude-x",
                "tool_input": {"command": f"git clone {FLEET} ~/tmp-x", "isolation": "worktree"}}
        self.assertEqual(G.evaluate(body, ctx("block home\nisolation block")).action, G.DENY)


class Advice(unittest.TestCase):
    def test_the_deny_never_suggests_what_the_mode_blocks(self) -> None:
        command = "gh repo clone Simple-With-Us/not-registered ~/tmp-z"
        self.assertIn("gh repo clone", G.evaluate(payload(command), ctx("block home")).reason)
        self.assertNotIn("gh repo clone", G.evaluate(payload(command), ctx("block home,clone-in-lanes")).reason)


class Exceptions(unittest.TestCase):
    ROW = {
        "id": "svc", "pattern": "{apps}/botfleet-server", "class": "service", "creator": "b", "reason": "r",
        "decided_by": "CLAUDE", "decided_on": "2026-11-04", "expires": "2099-01-01", "hosts": ["mac"],
    }

    def test_an_in_date_row_excuses_a_destination(self) -> None:
        rows = P.parse_rows({"exceptions": [self.ROW]})
        d = G.evaluate(payload(f"git clone {FLEET} ~/apps/botfleet-server"), ctx("block", rows=rows))
        self.assertEqual((d.action, d.detected), (G.ALLOW, ""))

    def test_an_expired_row_does_not(self) -> None:
        rows = P.parse_rows({"exceptions": [{**self.ROW, "decided_on": "2020-01-01", "expires": "2020-02-01"}]})
        self.assertEqual(G.evaluate(payload(f"git clone {FLEET} ~/apps/botfleet-server"), ctx("block", rows=rows)).action, G.DENY)

    def test_another_hosts_row_does_not(self) -> None:
        rows = P.parse_rows({"exceptions": [{**self.ROW, "hosts": ["server"]}]})
        self.assertEqual(G.evaluate(payload(f"git clone {FLEET} ~/apps/botfleet-server"), ctx("block", rows=rows)).action, G.DENY)

    def test_the_shipped_rows_cover_the_tools_that_clone_fleet_sources(self) -> None:
        rows = P.load_exceptions()
        c = ctx("block", rows=rows)
        self.assertEqual(G.evaluate(payload(f"git clone {FLEET} ~/.botfleet/workspaces/abc/BotFleet"), c).action, G.ALLOW)
        cwd = f"{HOME}/Code/BotFleet"
        derived = f"{HOME}/Library/Developer/Xcode/DerivedData/A/SourcePackages"
        for argv in (["clone", FLEET, f"{HOME}/.claude/plugins/marketplaces/x"],
                     ["clone", FLEET, f"{HOME}/.codex/.tmp/marketplaces/.staging/y"],
                     ["clone", "--shared", "--no-checkout", f"{derived}/repositories/x", f"{derived}/checkouts/x"],
                     ["clone", "--mirror", FLEET, f"{HOME}/Library/Caches/org.swift.swiftpm/repositories/z"]):
            with self.subTest(argv=argv):
                verdict = G.evaluate_git_argv(argv, cwd, c, origin_of=lambda _path: FLEET)
                self.assertEqual(verdict[0], G.CLASS_EXCUSED)


class Spellings(unittest.TestCase):
    """A host whose apps or lanes folder is a symlink, and a lanes folder whose disk is gone."""

    def test_a_typed_lanes_path_on_a_relocated_root(self) -> None:
        env = {L.ENV_LANES_ROOT: "/data/vol/apps/lanes", L.ENV_APPS_ROOT: "/data/vol/apps", L.ENV_CODE_ROOT: "/data/vol/Code"}
        c = ctx("block", env=env)
        self.assertEqual(G.evaluate(payload("git worktree add ~/apps/lanes/BotFleet/x", cwd="/data/vol/Code/BotFleet"), c).action, G.ALLOW)
        d = G.evaluate(payload(f"git clone {FLEET} ~/apps/x", cwd="/data/vol/Code/BotFleet"), c)
        self.assertEqual((d.action, d.rule_id), (G.DENY, G.RULE_PLACEMENT_APPS))

    def test_real_root_spelling_is_the_lanes_root_too(self) -> None:
        env = {L.ENV_LANES_ROOT: "/data/vol/apps/lanes", L.ENV_APPS_ROOT: "/data/vol/apps", L.ENV_CODE_ROOT: "/data/vol/Code"}
        c = ctx("block", env=env)
        self.assertEqual(G.evaluate(payload("git worktree add /data/vol/apps/lanes/BotFleet/x", cwd="/data/vol/Code/BotFleet"), c).action, G.ALLOW)


class Isolation(unittest.TestCase):
    CODE = f"{HOME}/Code/BotFleet"
    LANE = f"{LANES}/BotFleet/claude-x"

    def spawn(self, cwd: str, tool: str = "Agent", **tool_input) -> dict:
        return {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}

    def test_off_by_default(self) -> None:
        self.assertEqual(ctx("").mode.isolation, "off")
        d = G.evaluate(self.spawn(self.CODE, isolation="worktree"), ctx(""))
        self.assertEqual((d.action, d.detected), (G.ALLOW, ""))

    def test_detect_names_it_and_allows(self) -> None:
        d = G.evaluate(self.spawn(self.CODE, isolation="worktree"), ctx("isolation detect"))
        self.assertEqual((d.action, d.detected), (G.ALLOW, "isolation"))

    def test_block_denies_from_the_integration_tree_with_the_lane_path(self) -> None:
        c = ctx("isolation block")
        for payload_ in (self.spawn(self.CODE, isolation="worktree"), self.spawn(self.CODE, "Task", isolation="worktree"),
                         self.spawn(self.CODE, "EnterWorktree"), self.spawn(self.CODE, "spawn_agent", worktree_isolation=True),
                         self.spawn(f"{self.CODE}/sub/dir", isolation="worktree")):
            with self.subTest(payload=payload_):
                d = G.evaluate(payload_, c)
                self.assertEqual((d.action, d.rule_id), (G.DENY, G.RULE_ISOLATION))
                self.assertIn(f"{HOME}/apps/lane new BotFleet", d.reason)

    def test_a_lane_is_where_isolation_belongs(self) -> None:
        c = ctx("isolation block")
        self.assertEqual(G.evaluate(self.spawn(self.LANE, isolation="worktree"), c).action, G.ALLOW)
        self.assertEqual(G.evaluate(self.spawn(self.LANE, "EnterWorktree"), c).action, G.ALLOW)

    def test_an_unreadable_registry_fails_the_membership_question_closed(self) -> None:
        # The registry says which repositories are fleet repositories.  When it cannot be read the
        # guard holds the extras alone, so it cannot say that a repo is nobody's:  the worktree is
        # refused until the registry names it (AFC-811).
        c = fallback_ctx("isolation block")
        self.assertEqual(c.registry.source, "fallback")
        self.assertTrue(c.registry_fallback)
        d = G.evaluate(self.spawn(self.CODE, isolation="worktree"), c)
        self.assertEqual((d.action, d.rule_id), (G.DENY, G.RULE_ISOLATION))
        self.assertIn("FLEET_APPS_JSON", d.reason)

    def test_detect_mode_only_names_the_fallback_instead_of_blocking_it(self) -> None:
        # Detect mode reports, block mode refuses.  A host whose registry cannot be read is no
        # different from any other host in detect:  the worktree is named, not refused.
        c = fallback_ctx("isolation detect")
        self.assertTrue(c.registry_fallback)
        d = G.evaluate(self.spawn(self.CODE, isolation="worktree"), c)
        self.assertEqual((d.action, d.detected), (G.ALLOW, "isolation"))
        self.assertNotEqual(d.rule_id, G.RULE_ISOLATION)

    def test_a_registry_with_no_owner_apps_fails_the_membership_question_closed(self) -> None:
        # A registry that was read and holds nothing but the extras answers no membership
        # question, so the answer is unknown, and an unknown answer is not a clearance.
        c = empty_registry_ctx("isolation block")
        self.assertNotEqual(c.registry.source, "fallback")
        self.assertTrue(c.registry_fallback)
        d = G.evaluate(self.spawn(self.CODE, isolation="worktree"), c)
        self.assertEqual((d.action, d.rule_id), (G.DENY, G.RULE_ISOLATION))

    def test_the_refusal_names_the_registry_file(self) -> None:
        # The refusal has to tell the agent what to read, and the file is fleet-apps.json.
        for c in (fallback_ctx("isolation block"), empty_registry_ctx("isolation block")):
            with self.subTest(registry=c.registry.source):
                d = G.evaluate(self.spawn(self.CODE, isolation="worktree"), c)
                self.assertIn("fleet-apps.json", d.reason)
                self.assertIn("FLEET_APPS_JSON", d.reason)

    def test_a_readable_registry_still_clears_a_repo_it_does_not_hold(self) -> None:
        # The refusal is narrow:  a registry that was read answers the question, and a repo it does
        # not hold is still someone's own.
        c = ctx("isolation block")
        d = G.evaluate(self.spawn(f"{HOME}/Code/SomeOtherRepo", isolation="worktree"), c)
        self.assertEqual(d.action, G.ALLOW)

    def test_the_fallback_still_answers_what_the_owner_set_says(self) -> None:
        # Only the membership question fails closed.  The owner set is built in, so a fleet owner's
        # clone into temp is still denied on a host whose registry cannot be read.
        c = fallback_ctx("block")
        self.assertEqual(
            G.evaluate(payload("gh repo clone Simple-With-Us/HogHunter /tmp/hh"), c).action,
            G.DENY,
        )
        self.assertEqual(
            G.evaluate(payload("git -C ~/Code/BotFleet worktree add /tmp/bf"), c).action,
            G.DENY,
        )

    def test_a_plain_spawn_is_no_request(self) -> None:
        c = ctx("isolation block")
        self.assertEqual(G.evaluate(self.spawn(self.CODE, prompt="hello"), c).action, G.ALLOW)
        self.assertEqual(G.evaluate(self.spawn(self.CODE, "spawn_agent", worktree_isolation="off"), c).action, G.ALLOW)

    def test_a_block_on_isolation_leaves_placement_in_detect(self) -> None:
        d = G.evaluate(payload(f"git clone {FLEET} ~/tmp-x"), ctx("isolation block"))
        self.assertEqual((d.action, d.detected), (G.ALLOW, P.CLASS_HOME))


class OtherShellTools(unittest.TestCase):
    """The matcher widening reaches Desktop Commander and the terminal tools, which name their command the same way."""

    def test_they_are_judged_like_bash(self) -> None:
        c = ctx("block")
        command = f"git clone {FLEET} ~/tmp-x"
        for tool in ("mcp__plugin_desktop-commander_desktop-commander__start_process", "mcp__terminal__run_in_terminal"):
            with self.subTest(tool=tool):
                self.assertEqual(G.evaluate(payload(command, tool=tool), c).action, G.DENY)


class ArgvEntry(unittest.TestCase):
    def test_classes_for_the_shim(self) -> None:
        c = ctx("detect")
        cwd = f"{HOME}/Code/BotFleet"
        for args, cls in ((["clone", FLEET, "/tmp/x"], P.CLASS_TEMP), (["clone", FLEET, f"{HOME}/Code/Other"], P.CLASS_CODE),
                          (["clone", FLEET, f"{HOME}/x"], P.CLASS_HOME), (["worktree", "add", f"{HOME}/apps/y"], P.CLASS_APPS),
                          (["-C", cwd, "worktree", "add", "/tmp/y"], P.CLASS_TEMP)):
            with self.subTest(args=args):
                got = G.evaluate_git_argv(args, cwd, c)
                self.assertEqual(got[0], cls)
                self.assertEqual(got[1].action, G.DENY)

    def test_fine_is_none(self) -> None:
        c = ctx("detect")
        self.assertIsNone(G.evaluate_git_argv(["clone", THIRD, "/tmp/x"], "/tmp", c))
        self.assertIsNone(G.evaluate_git_argv(["worktree", "add", f"{LANES}/BotFleet/a"], f"{HOME}/Code/BotFleet", c))
        self.assertIsNone(G.evaluate_git_argv(["status"], f"{HOME}/Code/BotFleet", c))


if __name__ == "__main__":
    unittest.main()
