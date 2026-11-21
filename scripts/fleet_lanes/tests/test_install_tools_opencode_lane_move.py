"""The OpenCode lane mover half of the rendered plugin:  only a session that starts in the
runtime's own worktree root is moved, the lane is created through the seat's lane tool with
AGENT_SEAT=OPENCODE, the session is moved with the session.move call, and nothing about the
mover can stop the guard from registering.  The plugin text is generated, so these tests pin
the invariants on the rendered text;  the behavioral dry run lives beside the lane tool."""

import re
import unittest

from fleet_lanes.install_tools import render_opencode_plugin

TEXT = render_opencode_plugin()


class LaneMoveInvariants(unittest.TestCase):
    def test_the_worktree_root_is_built_from_the_home_not_baked_in(self):
        # The same text is right on every fleet host, so no absolute home path may appear in it.
        self.assertIn('resolve(homedir(), ".local", "share", "opencode", "worktree")', TEXT)
        self.assertNotRegex(TEXT, r"[/']home/jay")
        self.assertNotIn("/Users/", TEXT)

    def test_only_internal_worktree_sessions_are_moved(self):
        # The gate reads the directory before anything else about a session acts.
        gate = TEXT.index("async consider(sid")
        body = TEXT[gate:]
        self.assertRegex(body, r"inInternalWorktree\(directory\)")
        self.assertLess(body.index("inInternalWorktree(directory)"), body.index("repoOfWorktree(directory)"))

    def test_the_lane_child_runs_as_the_opencode_seat(self):
        self.assertIn('AGENT_SEAT: "OPENCODE"', TEXT)
        self.assertIn('["new", repo, slug]', TEXT)

    def test_no_spawn_uses_a_shell(self):
        # A shell string would turn a worktree name with spaces into a command injection surface.
        for match in re.finditer(r"spawn\((.*?)\)", TEXT, re.S):
            self.assertNotIn("shell", match.group(1))

    def test_the_slug_is_kebab_and_capped_at_the_lane_tool_limit(self):
        self.assertIn('slice(0, 40)', TEXT)
        self.assertIn("/^[A-Za-z]{2}: /.exec(base)", TEXT)

    def test_the_move_is_the_session_move_call(self):
        self.assertIn("ctx.session.move({ sessionID: sid, directory })", TEXT)

    def test_a_build_without_the_session_api_is_reported_and_skipped(self):
        self.assertIn("session.get/session.move", TEXT)
        self.assertRegex(TEXT, r'typeof ctx\.session\?\.get !== "function"')

    def test_the_engine_is_shared_across_plugin_copies(self):
        self.assertIn('Symbol.for("fleet-lane-guard.lane-move")', TEXT)
        self.assertIn("LANE_MOVE_ATTEMPTS = 3", TEXT)

    def test_a_failure_is_logged_never_thrown_at_a_chat(self):
        # Every laneMoveLog call site is inside a catch or before a plain return;  the setup
        # wrapper swallows whatever startLaneMove raises.
        self.assertRegex(TEXT, r"catch \{\n    // a log that cannot be written is not worth a chat")

    def test_the_guard_still_registers_when_the_mover_cannot_start(self):
        # The mover is fired and forgotten before the guard hook is awaited.
        mover_at = TEXT.index("void startLaneMove(context);")
        hook_at = TEXT.index('await ctx.tool.hook("execute.before"')
        self.assertLess(mover_at, hook_at)


class LaneMoveUnchangedGuard(unittest.TestCase):
    def test_the_guard_half_is_untouched(self):
        # The mover was added beside the guard, not instead of it.
        for marker in (
            "const guardCommand = ",
            "runGuard(command, cwdFrom(payload, fallbackCwd))",
            "A guard that cannot answer is a denial, never an allow.",
        ):
            self.assertIn(marker, TEXT)


if __name__ == "__main__":
    unittest.main()
