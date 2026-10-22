"""AFC-387 tests: the Copilot, OpenCode and fx guards.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_muster_guards -v

Everything runs against a fake home inside a TemporaryDirectory; the real home is never read or
written, and no test passes a real path to apply or verify.  The OpenCode config fixture copies the
SHAPE of the real file (an mcp block and a plugin array, plus a dummy credential-shaped value that
must never be printed or copied) with dummy values only.

The generated plugin is TypeScript that OpenCode loads, so the tests that need it to behave run it
under node:  the plugin is imported and its permission hook is called, and the answer is read back.
Those tests skip cleanly when node is not on PATH;  every other test runs everywhere.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from fleet_lanes import install_tools as T
from fleet_lanes import lane_guard_hook as H

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
REAL_PKG = SCRIPTS_DIR / "fleet_lanes"
REGISTRY_FILE = SCRIPTS_DIR.parent / "fleet-apps.json"
SHA = "0123456789abcdef0123456789abcdef01234567"
TIMEOUT = 90.0
STAMP = 1_790_000_000.0
DUMMY_TOKEN = "dummy-bearer-value-0123456789"

LANE_STUB = '''"""Stub lane module for the installer tests."""
import sys


def main(argv=None):
    print("lane-stub-ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# The harness runs the plugin the way OpenCode V2 does:  it imports the default definition,
# calls setup with a context that records hooks, then fires execute.before.  A throw is a deny.
EXECUTE_HARNESS = '''import definition from "PLUGIN_URL";

const registrations: Array<[string, (event: unknown) => Promise<void> | void]> = [];
const ctx = {
  location: { directory: process.cwd() },
  tool: {
    hook: async (name: string, callback: (event: unknown) => Promise<void> | void) => {
      registrations.push([name, callback]);
      return {};
    },
  },
};

await definition.setup(ctx);
const entry = registrations.find(([name]) => name === "execute.before");
if (!entry) {
  console.log(JSON.stringify([{ name: "no execute.before hook registered", threw: true }]));
  process.exit(0);
}
const before = entry[1];

const cyclic: Record<string, unknown> = {};
cyclic.self = cyclic;
let deep: unknown = "x";
for (let i = 0; i < 40; i += 1) deep = { next: deep };

const cases: Array<[string, unknown]> = [
  // a file write can carry 200 KB of content:  it has no command,  so the guard never sees it.
  [
    "a non-command payload over the byte ceiling is not guarded",
    { tool: "write", input: { filePath: "/repo/src/a.ts", content: "x".repeat(200000) } },
  ],
  ["an allowed command is not denied", { tool: "bash", input: { command: "echo hello" } }],
  ["a denied command throws with the guard reason", { tool: "bash", input: { command: "DENYTARGET" } }],
  ["an oversized command fails closed", { tool: "bash", input: { command: "x".repeat(200000) } }],
  ["a cyclic payload fails closed", { tool: "edit", input: cyclic }],
  ["a too-deep payload fails closed", { tool: "edit", input: deep }],
];

const results: unknown[] = [];
for (const [name, event] of cases) {
  try {
    await before(event);
    results.push({ name, threw: false });
  } catch (error) {
    results.push({ name, threw: true, message: String(error) });
  }
}
console.log(JSON.stringify(results));
'''

# node's own type stripper,  used to turn the plugin into the JavaScript node --check parses.
STRIP_SCRIPT = """const fs = require("node:fs");
const { stripTypeScriptTypes } = require("node:module");
const [source, target] = process.argv.slice(1);
fs.writeFileSync(target, stripTypeScriptTypes(fs.readFileSync(source, "utf8"), { mode: "strip" }));
"""


def snapshot(root: Path) -> dict:
    out: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            p = Path(dirpath) / d
            out[str(p.relative_to(root))] = ("dir", None, stat.S_IMODE(p.lstat().st_mode))
        for f in filenames:
            p = Path(dirpath) / f
            st = p.lstat()
            out[str(p.relative_to(root))] = (
                "file",
                hashlib.sha256(p.read_bytes()).hexdigest() if stat.S_ISREG(st.st_mode) else "link",
                stat.S_IMODE(st.st_mode),
            )
    return out


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = T.main(list(argv), stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


class World(unittest.TestCase):
    """A fake home and a small source tree.  Nothing here touches the real home."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="muster-guards-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.paths = T.make_paths(str(self.home))
        self.source = self.make_source()

    def make_source(self) -> T.Source:
        pkg = self.tmp / "src" / "scripts" / "fleet_lanes"
        pkg.mkdir(parents=True)
        for m in T.REQUIRED_MODULES:
            shutil.copy(REAL_PKG / m, pkg / m)
        (pkg / "lane.py").write_text(LANE_STUB, encoding="utf-8")
        shutil.copy(REGISTRY_FILE, self.tmp / "src" / "fleet-apps.json")
        hooks = self.tmp / "src" / "scripts" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy(
            SCRIPTS_DIR / "hooks" / "secret-guard-pretooluse.py",
            hooks / "secret-guard-pretooluse.py",
        )
        return T.Source(
            str(self.tmp / "src" / "scripts"), str(self.tmp / "src" / "fleet-apps.json"), SHA
        )

    def install_tools(self) -> None:
        r = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False)
        self.assertTrue(r.ok, r)

    def apply(self, key: str, **kw) -> T.Result:
        return T.apply_platform(
            T.PLATFORMS[key],
            self.paths,
            now=lambda: STAMP,
            timeout=TIMEOUT,
            minimal_path=False,
            **kw,
        )

    def base(self) -> list:
        return [
            "--home",
            str(self.home),
            "--source",
            self.source.scripts_dir,
            "--registry",
            self.source.registry,
            "--sha",
            SHA,
            "--timeout",
            str(TIMEOUT),
            "--no-minimal-path",
        ]

    def assertPassOrSkip(self, checks: list) -> None:
        bad = [c.line() for c in checks if c.status == "FAIL"]
        self.assertEqual(bad, [], "\n".join(bad))


class CopilotTests(World):
    def setUp(self) -> None:
        super().setUp()
        (self.home / ".copilot" / "hooks").mkdir(parents=True)
        (self.home / ".copilot" / "settings.json").write_text(
            json.dumps({"env": {"EXAMPLE": DUMMY_TOKEN}}) + "\n", encoding="utf-8"
        )
        (self.home / ".copilot" / "hooks" / "agent-hook-otlp.json").write_text(
            json.dumps({"version": 1, "hooks": {"postToolUse": []}}) + "\n", encoding="utf-8"
        )
        self.install_tools()
        self.file = self.home / ".copilot" / "hooks" / "fleet-lane-guard.json"

    def test_plan_is_read_only_and_shows_the_file(self) -> None:
        before = snapshot(self.tmp)
        rc, out, err = run_main("plan", *self.base(), "copilot")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        for needle in (
            "[new]",
            '"version": 1',
            '"matcher": "Bash"',
            "timeoutSec",
            "UNVERIFIED",
            "OWNER-GATED",
        ):
            self.assertIn(needle, out)
        self.assertNotIn(DUMMY_TOKEN, out)

    def test_apply_writes_the_fleet_owned_file_and_is_idempotent(self) -> None:
        settings_before = (self.home / ".copilot" / "settings.json").read_bytes()
        otlp_before = (self.home / ".copilot" / "hooks" / "agent-hook-otlp.json").read_bytes()
        r = self.apply("copilot")
        self.assertEqual(r.status, "added", r)
        doc = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(doc["version"], 1)
        (group,) = doc["hooks"]["PreToolUse"]
        self.assertEqual(group["matcher"], "Bash")
        (hook,) = group["hooks"]
        self.assertEqual(hook["type"], "command")
        self.assertEqual(hook["command"], T.fleet_hook_command(self.paths, "claude"))
        self.assertEqual(hook["timeoutSec"], 5)
        self.assertEqual((self.home / ".copilot" / "settings.json").read_bytes(), settings_before)
        self.assertEqual(
            (self.home / ".copilot" / "hooks" / "agent-hook-otlp.json").read_bytes(), otlp_before
        )
        after = snapshot(self.home)
        self.assertEqual(self.apply("copilot").status, "unchanged")
        self.assertEqual(snapshot(self.home), after)

    def test_apply_makes_an_owner_only_backup_of_a_stale_file_of_ours(self) -> None:
        self.assertEqual(self.apply("copilot").status, "added")
        doc = json.loads(self.file.read_text(encoding="utf-8"))
        doc["hooks"]["PreToolUse"][0]["hooks"][0]["timeoutSec"] = 3
        self.file.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        r = self.apply("copilot")
        self.assertEqual(r.status, "updated", r)
        self.assertTrue(r.backup and os.path.exists(r.backup), r)
        self.assertEqual(stat.S_IMODE(os.stat(r.backup).st_mode), 0o600)
        self.assertEqual(
            json.loads(self.file.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0][
                "timeoutSec"
            ],
            5,
        )

    def test_a_foreign_file_at_our_path_is_refused_and_left_alone(self) -> None:
        self.file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [{"type": "command", "command": "/bin/true"}],
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        before = snapshot(self.home)
        r = self.apply("copilot")
        self.assertEqual(r.status, "refused", r)
        self.assertEqual(snapshot(self.home), before)

    def test_verify_proves_the_deny_and_keeps_the_live_proof_a_skip(self) -> None:
        self.apply("copilot")
        checks = T.verify_copilot(self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertPassOrSkip(checks)
        by = {c.name: c for c in checks}
        for name in ("hooks entry", "deny/login-PATH", "allow/login-PATH", "secret/login-PATH"):
            self.assertEqual(by[name].status, "PASS", by[name].line())
        self.assertEqual(by["fires"].status, "SKIP")
        self.assertIn("UNVERIFIED", by["fires"].detail)

    def test_verify_fails_when_the_fleet_hook_allows_everything(self) -> None:
        self.apply("copilot")
        shim = Path(self.paths.fleet_shim)
        shim.write_text("#!/bin/sh\nexit 0\n")
        os.chmod(shim, 0o755)
        checks = T.verify_copilot(self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertTrue(any(c.name == "deny/login-PATH" and c.status == "FAIL" for c in checks))

    def test_an_unwired_copilot_is_a_skip_unless_named(self) -> None:
        checks = T.verify_copilot(self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual([(c.name, c.status) for c in checks], [("hooks file", "SKIP")])
        rc, out, _ = run_main(
            "verify",
            "--home",
            str(self.home),
            "--timeout",
            str(TIMEOUT),
            "--no-minimal-path",
            "copilot",
        )
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL\s+copilot\s+hooks file.*named explicitly")


class OpenCodePluginShapeTests(World):
    """The generated plugin is a V2 module:  it registers execute.before,  throws to deny,  and
    reads the guard's empty allow reply as an allow."""

    def setUp(self) -> None:
        super().setUp()
        (self.home / ".config" / "opencode").mkdir(parents=True)

    def plugin_text(self) -> str:
        """The plugin source the installer writes, as text."""
        files = T.build_files(self.source, self.paths)
        self.assertIn(T.OPENCODE_PLUGIN_FILE, files)
        stable = Path(self.paths.stable)
        for relative, blob in files.items():
            target = stable / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        return Path(self.paths.opencode_plugin).read_text(encoding="utf-8")

    def node(self) -> str:
        found = shutil.which("node")
        if found is None:
            self.skipTest("no node on PATH to parse the plugin with")
        return found

    def run_node(self, args: list, cwd: str) -> subprocess.CompletedProcess:
        run = subprocess.run(
            [self.node(), *args], capture_output=True, text=True, timeout=120, cwd=cwd
        )
        if run.returncode != 0 and "bad option" in run.stderr:
            self.skipTest("this node has no --experimental-strip-types")
        return run

    def harness(self, source: str) -> list:
        """Run a V2 harness against the generated plugin and return its result rows.

        The plugin imports `@opencode/plugin` and spawns `../fleet-guard-hook`; a stub SDK
        and a fake guard are written beside it, so the harness exercises the same entry
        OpenCode loads without needing OpenCode itself.
        """
        self.plugin_text()
        stable = Path(self.paths.stable)
        sdk = stable / "node_modules" / "@opencode" / "plugin"
        sdk.mkdir(parents=True, exist_ok=True)
        (sdk / "package.json").write_text(
            json.dumps({"name": "@opencode/plugin", "type": "module", "main": "index.js"}),
            encoding="utf-8",
        )
        (sdk / "index.js").write_text(
            "export const Plugin = { define: (definition) => definition };\n", encoding="utf-8"
        )
        guard = stable / "fleet-guard-hook"
        guard.write_text(
            "#!/bin/sh\n"
            "input=$(cat)\n"
            'case "$input" in\n'
            "  *DENYTARGET*) printf '%s\\n' '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\","
            "\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"blocked by the test guard\"}}' ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(guard, 0o755)
        harness = stable / "harness.ts"
        harness.write_text(
            source.replace("PLUGIN_URL", Path(self.paths.opencode_plugin).as_uri()),
            encoding="utf-8",
        )
        run = self.run_node(["--experimental-strip-types", str(harness)], cwd=str(stable))
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = [line for line in run.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, run.stdout + run.stderr)
        return json.loads(lines[-1])

    def test_the_plugin_registers_execute_before_and_has_no_permission_channel(self) -> None:
        text = self.plugin_text()
        for needle in (
            "Plugin.define",
            'ctx.tool.hook("execute.before"',
            "throw new Error(reason)",
            "fleet-guard-hook",
            'out.trim() === ""',
        ):
            self.assertIn(needle, text)
        # V2 has no permission.ask:  keeping it would be dead code pretending to guard.
        self.assertNotIn('"permission.ask"', text)
        self.assertNotIn('"tool.execute.before"', text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Bearer", text)
        self.assertNotIn(DUMMY_TOKEN, text)

    def test_an_allowed_command_is_not_denied_and_a_deny_throws_the_reason(self) -> None:
        results = {r["name"]: r for r in self.harness(EXECUTE_HARNESS)}
        self.assertFalse(results["an allowed command is not denied"]["threw"], results)
        denied = results["a denied command throws with the guard reason"]
        self.assertTrue(denied["threw"], results)
        self.assertIn("blocked by the test guard", denied["message"])

    def test_non_command_payloads_are_not_sent_to_the_guard(self) -> None:
        results = {r["name"]: r for r in self.harness(EXECUTE_HARNESS)}
        self.assertFalse(
            results["a non-command payload over the byte ceiling is not guarded"]["threw"], results
        )

    def test_malformed_or_oversized_input_fails_closed(self) -> None:
        results = {r["name"]: r for r in self.harness(EXECUTE_HARNESS)}
        for name in (
            "an oversized command fails closed",
            "a cyclic payload fails closed",
            "a too-deep payload fails closed",
        ):
            self.assertTrue(results[name]["threw"], results[name])

    def test_the_plugin_source_parses_as_typescript(self) -> None:
        with tempfile.TemporaryDirectory(prefix="muster-plugin-parse-") as tmp:
            plugin = Path(tmp) / "index.ts"
            plugin.write_text(self.plugin_text(), encoding="utf-8")
            sdk = Path(tmp) / "node_modules" / "@opencode" / "plugin"
            sdk.mkdir(parents=True)
            (sdk / "package.json").write_text(
                json.dumps({"name": "@opencode/plugin", "type": "module", "main": "index.js"}),
                encoding="utf-8",
            )
            (sdk / "index.js").write_text(
                "export const Plugin = { define: (definition) => definition };\n", encoding="utf-8"
            )
            run = self.run_node(["--experimental-strip-types", str(plugin)], cwd=tmp)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_the_javascript_the_plugin_becomes_passes_node_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="muster-plugin-check-") as tmp:
            plugin = Path(tmp) / "index.ts"
            plugin.write_text(self.plugin_text(), encoding="utf-8")
            stripped = Path(tmp) / "index.mjs"
            probe = self.run_node(
                ["--eval", "console.log(typeof require('node:module').stripTypeScriptTypes)"], cwd=tmp
            )
            if probe.returncode != 0 or "function" not in probe.stdout:
                self.skipTest("this node cannot strip the types itself")
            strip = self.run_node(["--eval", STRIP_SCRIPT, str(plugin), str(stripped)], cwd=tmp)
            self.assertEqual(strip.returncode, 0, strip.stderr)
            check = self.run_node(["--check", str(stripped)], cwd=tmp)
            self.assertEqual(check.returncode, 0, check.stderr)


class OpenCodeTests(World):
    def setUp(self) -> None:
        super().setUp()
        (self.home / ".config" / "opencode").mkdir(parents=True)
        self.config = self.home / ".config" / "opencode" / "opencode.json"
        doc = {
            "mcp": {
                "fixture-mcp": {
                    "type": "remote",
                    "headers": {"Authorization": "Bearer " + DUMMY_TOKEN},
                }
            },
            "plugins": ["opencode-mobile@latest"],
        }
        self.config.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.config, 0o600)
        self.install_tools()

    def test_the_plugin_lives_in_the_stable_dir_and_holds_no_credential(self) -> None:
        files = T.build_files(self.source, self.paths)
        self.assertIn(T.OPENCODE_PLUGIN_FILE, files)
        plugin = Path(self.paths.opencode_plugin)
        self.assertTrue(plugin.is_file())
        text = plugin.read_text(encoding="utf-8")
        for needle in (
            "Plugin.define",
            'ctx.tool.hook("execute.before"',
            "throw new Error(reason)",
            "fleet-guard-hook",
        ):
            self.assertIn(needle, text)
        self.assertIn("commandFrom(event.input)", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Bearer", text)
        self.assertNotIn(DUMMY_TOKEN, text)

    def test_apply_lists_the_plugin_by_absolute_path_and_preserves_the_file(self) -> None:
        before = json.loads(self.config.read_text(encoding="utf-8"))
        r = self.apply("opencode")
        self.assertEqual(r.status, "updated", r)
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(doc["mcp"], before["mcp"])
        self.assertEqual(doc["plugins"], ["opencode-mobile@latest", self.paths.opencode_plugin_dir])
        self.assertTrue(os.path.isabs(doc["plugins"][-1]))
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)
        self.assertTrue(r.backup and os.path.exists(r.backup), r)
        self.assertEqual(stat.S_IMODE(os.stat(r.backup).st_mode), 0o600)
        after = snapshot(self.home)
        self.assertEqual(self.apply("opencode").status, "unchanged")
        self.assertEqual(snapshot(self.home), after)

    def test_plan_is_read_only_and_never_prints_the_other_keys(self) -> None:
        before = snapshot(self.tmp)
        rc, out, err = run_main("plan", *self.base(), "opencode")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn(self.paths.opencode_plugin_dir, out)
        self.assertIn("opencode-plugin/index.ts", out)
        self.assertIn("UNVERIFIED", out)
        self.assertNotIn(DUMMY_TOKEN, out)
        self.assertNotIn("Bearer", out)

    def test_verify_reads_the_entry_back_and_probes_the_guard(self) -> None:
        self.apply("opencode")
        checks = T.verify_opencode(self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertPassOrSkip(checks)
        by = {c.name: c for c in checks}
        for name in (
            "plugin file",
            "plugin entry",
            "no credential copy",
            "deny/login-PATH",
            "allow/login-PATH",
            "secret/login-PATH",
        ):
            self.assertEqual(by[name].status, "PASS", by[name].line())
        self.assertEqual(by["fires"].status, "SKIP")
        self.assertIn("UNVERIFIED", by["fires"].detail)

    def test_verify_fails_when_the_entry_is_gone(self) -> None:
        self.apply("opencode")
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        doc["plugins"] = ["opencode-mobile@latest"]
        self.config.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        checks = T.verify_opencode(self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertTrue(any(c.name == "plugin entry" and c.status == "FAIL" for c in checks))

    def test_an_unwired_opencode_is_one_skip_not_a_page_of_fails(self) -> None:
        os.unlink(Path(self.paths.opencode_plugin))
        checks = T.verify_opencode(self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual([(c.name, c.status) for c in checks], [("plugin file", "SKIP")])
        rc, out, _ = run_main(
            "verify",
            "--home",
            str(self.home),
            "--timeout",
            str(TIMEOUT),
            "--no-minimal-path",
            "opencode",
        )
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL\s+opencode\s+plugin file.*named explicitly")

    def test_a_stale_plugin_path_is_updated_in_place(self) -> None:
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        doc["plugins"] = [
            "/old/stable/opencode-plugin/fleet-lane-guard.ts",
            "opencode-mobile@latest",
        ]
        self.config.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        new, action = T.opencode_merge(doc, self.paths)
        self.assertEqual(action, "updated")
        self.assertEqual(new["plugins"], [self.paths.opencode_plugin_dir, "opencode-mobile@latest"])
        again, action = T.opencode_merge(new, self.paths)
        self.assertEqual((action, again), ("unchanged", new))

    def test_a_missing_config_file_is_created_with_the_plugin_and_an_owner_only_mode(self) -> None:
        self.config.unlink()
        r = self.apply("opencode")
        self.assertEqual(r.status, "added", r)
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(doc["plugins"], [self.paths.opencode_plugin_dir])
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)

    def test_a_plugin_key_that_is_not_a_list_is_refused_and_left_alone(self) -> None:
        self.config.write_text(
            json.dumps({"plugins": "not-a-list"}, indent=2) + "\n", encoding="utf-8"
        )
        before = snapshot(self.home)
        r = self.apply("opencode")
        self.assertEqual(r.status, "refused", r)
        self.assertIn("not a list", r.detail)
        self.assertEqual(snapshot(self.home), before)


class CliRoundTripTests(World):
    """The path Jay actually runs: `install_tools apply <target>` and `install_tools verify <target>`, against a
    fake home, with the CLI's own argument handling."""

    def setUp(self) -> None:
        super().setUp()
        (self.home / ".copilot" / "hooks").mkdir(parents=True)
        (self.home / ".config" / "opencode").mkdir(parents=True)
        (self.home / ".config" / "opencode" / "opencode.json").write_text(
            json.dumps({"plugins": ["opencode-mobile@latest"]}, indent=2) + "\n", encoding="utf-8"
        )
        self.install_tools()

    def test_apply_then_verify_through_the_cli_for_each_new_platform(self) -> None:
        for key in ("copilot", "opencode"):
            rc, out, err = run_main("apply", *self.base(), "--no-verify", key)
            self.assertEqual((rc, err), (0, ""), out)
            self.assertIn(key, out)
            rc, out, err = run_main(
                "verify",
                "--home",
                str(self.home),
                "--timeout",
                str(TIMEOUT),
                "--no-minimal-path",
                key,
            )
            self.assertEqual((rc, err), (0, ""), out)
            self.assertRegex(out, r"verify: \d+ PASS, 0 FAIL, 0 WARN")
            self.assertIn("fires", out)

    def test_apply_all_writes_the_new_platforms_and_never_touches_fx(self) -> None:
        fx_settings = self.home / ".fx"
        rc, out, err = run_main("apply", *self.base(), "--no-verify", "all")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertTrue((self.home / ".copilot" / "hooks" / "fleet-lane-guard.json").is_file())
        self.assertTrue((self.home / ".config" / "opencode" / "opencode.json").is_file())
        self.assertFalse(fx_settings.exists())

    def test_verify_all_reports_fx_red_and_the_others_green(self) -> None:
        (self.home / ".fx").mkdir(parents=True)
        (self.home / ".fx" / "settings.json").write_text(
            json.dumps({"provider": "gateway", "permission_mode": "yolo"}) + "\n", encoding="utf-8"
        )
        run_main("apply", *self.base(), "--no-verify", "copilot", "opencode")
        rc, out, _ = run_main(
            "verify",
            "--home",
            str(self.home),
            "--timeout",
            str(TIMEOUT),
            "--no-minimal-path",
            "all",
        )
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL\s+fx\s+platform")
        for key in ("copilot", "opencode"):
            self.assertNotRegex(out, r"FAIL\s+%s\s" % key, out)


class FxTests(World):
    def setUp(self) -> None:
        super().setUp()
        (self.home / ".fx").mkdir(parents=True)
        self.settings = self.home / ".fx" / "settings.json"
        self.settings.write_text(
            json.dumps(
                {"provider": "gateway", "permission_mode": "yolo", "yolo_acknowledged": True}
            )
            + "\n",
            encoding="utf-8",
        )

    def test_the_compensating_control_is_prepared_and_static(self) -> None:
        doc = json.loads(T.render_fx_compensating_control())
        self.assertEqual(doc["permission_mode"], "ask")
        self.assertEqual(doc["permission"]["*"], "ask")
        bash = doc["permission"]["bash"]
        self.assertTrue(bash)
        self.assertTrue(all(v == "deny" for v in bash.values()))
        for root in ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp"):
            self.assertTrue(any(root in k for k in bash), root)

    def test_plan_prints_the_control_and_writes_nothing(self) -> None:
        before = snapshot(self.tmp)
        rc, out, err = run_main("plan", *self.base(), "fx")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn("UNSUPPORTED", out)
        self.assertIn('"permission_mode": "ask"', out)
        self.assertIn("cannot resolve", out)
        self.assertIn("never writes", out)

    def test_apply_writes_nothing_and_verify_stays_red(self) -> None:
        before = snapshot(self.tmp)
        r = self.apply("fx")
        self.assertEqual(r.status, "skipped", r)
        self.assertIn("UNSUPPORTED", r.detail)
        self.assertEqual(snapshot(self.tmp), before)
        checks = T.verify_platform(
            T.PLATFORMS["fx"], self.paths, timeout=TIMEOUT, minimal_path=False
        )
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, "FAIL")
        self.assertIn("UNSUPPORTED", checks[0].detail)
        rc, out, _ = run_main(
            "verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "fx"
        )
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"FAIL\s+fx\s+platform")

    def test_fx_without_a_home_is_a_skip(self) -> None:
        shutil.rmtree(self.home / ".fx")
        checks = T.verify_platform(
            T.PLATFORMS["fx"], self.paths, timeout=TIMEOUT, minimal_path=False
        )
        self.assertEqual(checks[0].status, "SKIP")


class InstallLockAndPrivateDirTests(World):
    """The install writes into a private directory, and the check and the write are one unit.

    The re-read that guards against a concurrent edit used to run outside any lock,  so two
    installers could both pass the check and the second write erased the first.  An
    exclusive per-target lock now covers the check,  the write,  the validation and the
    rollback,  and the lock files live in the fleet's own state directory,  never beside
    the file they guard.
    """

    def test_a_private_directory_is_created_owner_only(self) -> None:
        target = self.home / "private" / "nested"
        T._makedirs_private(str(target))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)

    def test_an_existing_directory_keeps_its_own_permissions(self) -> None:
        target = self.home / "already"
        target.mkdir(mode=0o750)
        T._makedirs_private(str(target))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)

    def test_a_symlinked_directory_is_refused_and_nothing_is_written(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.home / "link").symlink_to(elsewhere)
        with self.assertRaises(OSError):
            T._makedirs_private(str(self.home / "link"))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_the_lock_is_exclusive_and_lives_outside_the_platform_home(self) -> None:
        target = self.home / ".claude" / "hooks.toml"
        lock_dir = self.home / ".local" / "state" / "fleet-lanes" / "locks"
        with T._target_lock(str(self.home), str(target)):
            self.assertTrue(lock_dir.is_dir())
            locks = list(lock_dir.iterdir())
            self.assertEqual(len(locks), 1)
            # The lock is held:  a second open file description cannot take it.
            other = os.open(str(locks[0]), os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(other)
        # Released:  the same lock is free again.
        other = os.open(str(locks[0]), os.O_RDWR)
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(other)

    def test_the_lock_file_is_not_left_in_the_platform_directory(self) -> None:
        platform = self.home / ".claude"
        platform.mkdir()
        with T._target_lock(str(self.home), str(platform / "hooks.toml")):
            pass
        self.assertEqual(sorted(p.name for p in platform.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
