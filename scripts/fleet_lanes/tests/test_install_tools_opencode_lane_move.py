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


def render_slice(start_marker: str, end_marker: str) -> str:
    """The rendered text between two markers, so an assertion reads the function it names."""
    start = TEXT.index(start_marker)
    end = TEXT.index(end_marker, start)
    return TEXT[start:end]


class LaneMoveRemoteParsing(unittest.TestCase):
    """Cluster 1:  the origin URL parser must not fold an owner, a port or a
    credential into the repo name, and must refuse names that cannot be a lane."""

    def test_the_parser_cuts_the_scheme_and_the_scp_colon_not_the_last_colon(self):
        body = render_slice("function repoFromRemoteUrl", "function runBounded")
        self.assertNotIn('lastIndexOf(":")', body)
        self.assertIn("body.slice(scheme + 3)", body)
        self.assertIn("scpColon", body)
        self.assertIn("segments[segments.length - 1]", body)
        self.assertIn("segments.length < 2", body)

    def test_a_repo_name_that_cannot_be_a_lane_directory_is_refused(self):
        body = render_slice("function repoFromRemoteUrl", "function runBounded")
        self.assertIn('name === "." || name === ".."', body)
        self.assertIn('name.startsWith("-")', body)

    def test_a_lane_path_is_accepted_only_inside_a_vouched_lanes_root(self):
        body = render_slice("function lanePathAccepted", "function ensureLane")
        self.assertIn("expectedTail", body)
        self.assertIn("lanesRoots()", body)
        self.assertIn("realOf(", body)
        self.assertIn('startsWith(under + sep)', body)


class LaneMoveHandles(unittest.TestCase):
    """Cluster 2:  a copy that misses, errors or hangs hands the work to the
    next copy instead of ending the fallback."""

    def test_a_get_miss_throws_instead_of_returning_null(self):
        self.assertIn("is not known to this copy", TEXT)
        self.assertNotIn("catch {\n          return null;", TEXT)

    def test_any_handle_treats_a_miss_and_a_hang_as_reasons_to_move_on(self):
        body = render_slice("const LANE_MOVE_TIMED_OUT", "// Create the lane when it is not there yet")
        self.assertIn("lane-move handle had no answer", body)
        self.assertIn("lane-move handle timed out", body)
        self.assertIn("LANE_MOVE_HANDLE_TIMEOUT_MS", body)
        self.assertIn("continue;", body)


class LaneMoveSchedule(unittest.TestCase):
    """Cluster 3:  events are never dropped behind a slower timer, one pass runs
    per session at a time, and a deferral spends no attempt."""

    def test_a_higher_priority_nudge_supersedes_a_pending_slower_timer(self):
        body = render_slice("nudge(sid: string, delayMs: number, priority: number)", "private schedule(")
        self.assertIn("(state.priority ?? LANE_MOVE_PRIORITY_FALLBACK) >= priority) return", body)
        self.assertIn("clearTimeout(state.timer)", body)

    def test_one_pass_per_session_with_a_single_rerun(self):
        body = render_slice("private async pass(", "async consider(")
        self.assertIn("state.inFlight = true", body)
        self.assertIn("state.inFlight = false", body)
        self.assertIn("state.rerun = true", body)

    def test_a_deferral_spends_no_attempt(self):
        body = render_slice("async consider(", "// Join the one engine")
        self.assertLess(body.index('return "deferred"'), body.index("state.attempts += 1"))
        self.assertIn("gave-up", body)


class LaneMoveArgumentSafety(unittest.TestCase):
    """Cluster 4:  nothing session-controlled reaches a child in a position where
    it reads as a flag."""

    def test_a_dash_leading_directory_never_reaches_git_c(self):
        body = render_slice("async function repoOfWorktree", "// Create the lane when it is not there yet")
        self.assertIn('directory.startsWith("-")', body)

    def test_lane_arguments_are_validated_before_the_child(self):
        body = render_slice("function laneShapeOk", "function lanePathAccepted")
        self.assertIn("/^[A-Za-z0-9][A-Za-z0-9._-]*$/", body)
        self.assertIn("/^[a-z0-9][a-z0-9-]*$/", body)
        self.assertIn("laneShapeOk(repo, slug)", TEXT)


class LaneCreationBound(unittest.TestCase):
    """Cluster 5:  lane children are bounded and deduplicated per repo/slug."""

    def test_lane_children_are_bounded_and_share_one_creation_per_key(self):
        self.assertIn("LANE_CREATE_CONCURRENT = 2", TEXT)
        self.assertIn("laneGate.running < LANE_CREATE_CONCURRENT", TEXT)
        self.assertIn("laneGate.inflight.get(key)", TEXT)


if __name__ == "__main__":
    unittest.main()

