"""The lane mover's behavioral dry run:  the rendered plugin is driven under Node with a fake
OpenCode context and a recording fake lane command, and an internal-worktree fixture whose name
has spaces and mixed case must map to the repo's own lane slug.  Everything is hermetic:  the
fixture worktree is a throwaway git repo under the test's temp tree, the plugin's HOME points at
that tree so its worktree-root check sees the fixture, the lane command is a fake that records
its argv and seat, and no volume or home path is written in this file.  Requires `node` (>= 22)
and the `amaro` type stripper resolvable through NODE_PATH;  without them the test skips,
because a host without the runtime cannot run the artifact and the invariant tests pin the text.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from fleet_lanes.install_tools import render_opencode_plugin

DRIVER = r"""
import { chmodSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";

const [tsPath, work] = [process.argv[2], process.argv[3]];
const require = createRequire(tsPath);
let strip;
try {
  strip = require("amaro").transformSync;
} catch {
  console.log("SKIP no amaro on NODE_PATH");
  process.exit(0);
}
const pluginDir = work + "/plugin";
mkdirSync(pluginDir + "/node_modules/@opencode/plugin", { recursive: true });
writeFileSync(pluginDir + "/node_modules/@opencode/plugin/package.json",
  JSON.stringify({ name: "@opencode/plugin", version: "0.0.0", type: "module", main: "index.js" }));
writeFileSync(pluginDir + "/node_modules/@opencode/plugin/index.js",
  "export const Plugin = { define(definition) { return definition; } };\n");
writeFileSync(pluginDir + "/index.js", strip(readFileSync(tsPath, "utf8"), {}).code);

const fakeLane = work + "/fake-lane";
writeFileSync(fakeLane, [
  "#!/bin/bash",
  "{",
  "  printf 'ARGV:'; printf ' %s' \"$@\"; printf '\\n'",
  "  printf 'AGENT_SEAT=%s\\n' \"${AGENT_SEAT-<unset>}\"",
  "} >> " + JSON.stringify(work + "/lane-calls.log"),
  'verb="$1"; repo="$2"; slug="$3"',
  'if [ "$verb" != "new" ]; then printf "unexpected verb: %s\\n" "$verb" >&2; exit 64; fi',
  'path="' + work + '/fake-lanes/$repo/opencode-$slug"',
  'printf "lane: created %s (branch opencode/%s)\\n%s\\n" "$path" "$slug" "$path"',
].join("\n"));
chmodSync(fakeLane, 0o755);
process.env.OPENCODE_LANE_CMD = fakeLane;
process.env.OPENCODE_LANE_MOVE_FALLBACK_MS = "1500";
// The containment check only accepts a lane path under a lanes root;  the fake
// tool answers under this one, so the env names it for the plugin.
process.env.FLEET_LANES_ROOT = work + "/fake-lanes";

// The fixture directory the python side prepared:  a real git repo under the worktree root of
// the temp HOME, named with spaces and mixed case the way the runtime names its own worktrees.
const WORKTREE_DIR = process.env.HOME + "/.local/share/opencode/worktree/dryrun-fixture/UI Improvement B";
const LANE_DIR = work + "/already-lane/Clutch/opencode-existing";
const moved = [];
const gets = [];
const sessions = {
  ses_dryTitleFixed001: { directory: WORKTREE_DIR, title: "CC: Mac iOS apps UI UX improvement", slug: "cosmic-cactus" },
  ses_dryUntitled00002: { directory: WORKTREE_DIR, title: "", slug: "cosmic-cactus" },
  ses_dryInLane0000003: { directory: LANE_DIR, title: "AA: Mac and iOS App UI/UX Improvements", slug: "hidden-nebula" },
  ses_dryLateTitle0004: { directory: WORKTREE_DIR, title: "CC: Mac iOS apps UI UX improvement", slug: "mighty-squid" },
  ses_dryQuiet0000005: { directory: WORKTREE_DIR, title: "", slug: "quiet-falcon" },
};
const ctx = {
  session: {
    get: async ({ sessionID }) => {
      gets.push(sessionID);
      const s = sessions[sessionID];
      if (!s) return null;
      if (sessionID === "ses_dryLateTitle0004" && gets.filter((g) => g === sessionID).length < 3) {
        return { ...s, title: "" };
      }
      return { ...s };
    },
    move: async ({ sessionID, directory }) => {
      moved.push({ sessionID, directory });
      return { sessionID, directory };
    },
  },
  tool: { hook: async () => {} },
  event: {
    subscribe: async function* () {
      yield { type: "session.created", data: { sessionID: "ses_dryTitleFixed001", parentID: null } };
      yield { type: "session.created", data: { sessionID: "ses_dryUntitled00002" } };
      yield { type: "session.created", data: { sessionID: "ses_dryInLane0000003" } };
      yield { type: "session.created", data: { sessionID: "ses_dryLateTitle0004" } };
      yield { type: "session.created", data: { sessionID: "ses_dryQuiet0000005" } };
      yield { type: "session.created", data: { sessionID: "ses_dryTitleFixed001" } };
      yield { type: "session.created", data: { sessionID: "ses_dryQuiet0000005" } };
      yield { type: "session.inbox.enqueued", data: { sessionID: "ses_dryUntitled00002", item: { type: "user" } } };
      yield { type: "session.renamed", data: { sessionID: "ses_dryInLane0000003", title: "AA: Mac and iOS App UI/UX Improvements" } };
    },
  },
};

const checks = [];
const fail = (name, detail) => checks.push("FAIL " + name + (detail ? ": " + detail : ""));
const ok = (name) => checks.push("ok " + name);

const mod = await import("file://" + pluginDir + "/index.js");
await mod.default.setup(ctx);
await new Promise((r) => setTimeout(r, 11000));

const calls = readFileSync(work + "/lane-calls.log", "utf8").trim().split("\n");
const movesFor = (sid) => moved.filter((m) => m.sessionID === sid);
const LANE = (slug) => work + "/fake-lanes/Clutch/opencode-" + slug;

const m1 = movesFor("ses_dryTitleFixed001");
if (m1.length === 1 && m1[0].directory === LANE("mac-ios-apps-ui-ux-improvement")) {
  ok("titled session moved once to the repo lane for its title slug");
} else {
  fail("titled move", JSON.stringify(m1));
}
if (calls[0] === "ARGV: new Clutch mac-ios-apps-ui-ux-improvement" && calls[1] === "AGENT_SEAT=OPENCODE") {
  ok("lane command new Clutch <slug> ran with AGENT_SEAT=OPENCODE");
} else {
  fail("lane command", JSON.stringify(calls.slice(0, 6)));
}
const m2 = movesFor("ses_dryUntitled00002");
if (m2.length === 1 && m2[0].directory === LANE("cosmic-cactus")) {
  ok("untitled session fell back to the session slug");
} else {
  fail("untitled fallback", JSON.stringify(m2));
}
const m4 = movesFor("ses_dryLateTitle0004");
if (m4.length === 1 && m4[0].directory === LANE("mac-ios-apps-ui-ux-improvement")) {
  ok("late-titled session took the title slug once the title arrived");
} else {
  fail("late title", JSON.stringify(m4));
}
const m5 = movesFor("ses_dryQuiet0000005");
if (m5.length === 1 && m5[0].directory === LANE("quiet-falcon")) {
  ok("promptless session moved by the fallback pass");
} else {
  fail("promptless", JSON.stringify(m5));
}
if (movesFor("ses_dryInLane0000003").length === 0) {
  ok("in-lane session untouched");
} else {
  fail("in-lane moved", JSON.stringify(movesFor("ses_dryInLane0000003")));
}
if (moved.length === 4) {
  ok("exactly four moves (duplicates dropped)");
} else {
  fail("total moves", JSON.stringify(moved));
}
const logText = readFileSync(process.env.HOME + "/.local/state/fleet-lanes/lane-move.log", "utf8");
if (logText.includes('"moved"') && !logText.includes("example.invalid") && !logText.includes("git@")) {
  ok("move log written without remote URLs");
} else {
  fail("move log", logText.slice(0, 300));
}

const failures = checks.filter((line) => line.startsWith("FAIL"));
if (failures.length) {
  console.log("FAIL " + failures.length + " of " + checks.length);
  for (const line of failures) console.log(line);
  process.exit(1);
}
console.log("PASS " + checks.length + " checks");
"""

FIXTURE_ORIGIN = "https://lanes.example.invalid/Simple-With-Us/Clutch.git"


def node_at_least_22() -> bool:
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    parts = out.lstrip("v").split(".")
    return len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) >= 22


def fixture_worktree(work: str) -> str:
    """A throwaway git repo under the temp HOME's worktree root, named like the runtime names
    its own worktrees, carrying an origin remote so the mover can read a repo name off it."""
    fixture = os.path.join(
        work, "home", ".local", "share", "opencode", "worktree", "dryrun-fixture", "UI Improvement B"
    )
    os.makedirs(fixture)
    for args in (["init", "-q"], ["remote", "add", "origin", FIXTURE_ORIGIN]):
        subprocess.run(["git", "-C", fixture, *args], capture_output=True, text=True, timeout=30, check=True)
    return fixture


class LaneMoveDryRun(unittest.TestCase):
    def test_the_rendered_plugin_moves_an_internal_worktree_session_into_its_repo_lane(self):
        if shutil.which("node") is None or not node_at_least_22():
            self.skipTest("node >= 22 is not on PATH")
        if shutil.which("git") is None:
            self.skipTest("git is not on PATH")
        with tempfile.TemporaryDirectory(prefix="lane-move-dryrun-") as work:
            home = os.path.join(work, "home")
            fixture_worktree(work)
            ts_path = os.path.join(work, "index.ts")
            with open(ts_path, "w", encoding="utf-8") as fh:
                fh.write(render_opencode_plugin())
            driver_path = os.path.join(work, "driver.mjs")
            with open(driver_path, "w", encoding="utf-8") as fh:
                fh.write(DRIVER)
            proc = subprocess.run(
                ["node", driver_path, ts_path, work],
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "HOME": home, "NODE_PATH": os.environ.get("OPENCODE_DRYRUN_NODE_MODULES", "")},
            )
            out = (proc.stdout or "").strip().splitlines()
            self.assertTrue(out, "the dry run printed nothing: " + proc.stderr[-1500:])
            if out[0].startswith("SKIP"):
                self.skipTest(out[0])
            self.assertEqual(out[0], "PASS 8 checks", proc.stdout[-2000:] + proc.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
