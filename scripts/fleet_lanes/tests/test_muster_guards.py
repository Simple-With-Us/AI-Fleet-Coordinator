"""AFC-387 tests: the Copilot, Vibe, OpenCode and fx guards, and the --format vibe deny shape.

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

# The names of the permission prompts the harness sends.  The harness and this list have to
# agree,  so the test compares them.
MALFORMED_PROMPTS = (
    "the session id is not a string",
    "the call id is not a string",
    "the session id is empty",
    "the call id is empty",
    "the pattern is not a string",
    "the pattern holds a number",
    "the title is not a string",
    "the title is empty",
    "the prompt is not an object",
    "the prompt is null",
)
UNBOUNDED_PROMPTS = (
    "the prompt is oversized",
    "the prompt is cyclic",
    "the prompt is too deep",
    "the prompt is too wide",
)
WELL_FORMED_PROMPT = "the prompt is well formed"

# The harness runs the plugin the way OpenCode does:  it imports the generated file and calls
# the permission hook with each prompt in turn.  The answer is read back as JSON,  so a deny
# and a throw are told apart:  a throw is the bug these tests exist for.
PERMISSION_HARNESS = '''import { server } from "PLUGIN_URL";

const plugin = await server({ directory: process.cwd() });
const ask = plugin["permission.ask"] as unknown as (
  input: unknown,
  output: { status: string },
) => Promise<void>;

const cyclic: Record<string, unknown> = { sessionID: "session-1", callID: "call-1" };
cyclic.metadata = cyclic;

const deep: Record<string, unknown> = { sessionID: "session-1", callID: "call-1" };
let cursor = deep;
for (let i = 0; i < 40; i += 1) {
  const next: Record<string, unknown> = {};
  cursor.metadata = next;
  cursor = next;
}

const cases: Array<[string, unknown]> = [
  ["the session id is not a string", { sessionID: 5, callID: "call-1" }],
  ["the call id is not a string", { sessionID: "session-1", callID: 7 }],
  ["the session id is empty", { sessionID: "", callID: "call-1" }],
  ["the call id is empty", { sessionID: "session-1", callID: "" }],
  ["the pattern is not a string", { sessionID: "session-1", pattern: 5 }],
  ["the pattern holds a number", { sessionID: "session-1", pattern: ["git clone", 5] }],
  ["the title is not a string", { sessionID: "session-1", title: 5 }],
  ["the title is empty", { sessionID: "session-1", title: "" }],
  ["the prompt is not an object", "not an object"],
  ["the prompt is null", null],
  [
    "the prompt is oversized",
    { sessionID: "session-1", callID: "call-1", metadata: { blob: "x".repeat(200000) } },
  ],
  ["the prompt is cyclic", cyclic],
  ["the prompt is too deep", deep],
  [
    "the prompt is too wide",
    { sessionID: "session-1", callID: "call-1", metadata: { command: new Array(2000).fill("x") } },
  ],
  ["the prompt is well formed", { sessionID: "session-1", callID: "call-1" }],
];

const results: unknown[] = [];
for (const [name, input] of cases) {
  const output = { status: "ask" };
  try {
    await ask(input, output);
    results.push({ name, threw: false, status: output.status });
  } catch (error) {
    results.push({ name, threw: true, status: output.status, message: String(error) });
  }
}
console.log(JSON.stringify(results));
'''

EXECUTE_HARNESS = '''import { server } from "PLUGIN_URL";

const plugin = await server({ directory: process.cwd() });
const before = plugin["tool.execute.before"] as unknown as (
  input: unknown,
  output: { args: unknown },
) => Promise<void>;

const cases: Array<[string, unknown, unknown]> = [
  // sentry #757:  a non-shell payload over the command ceiling must not be denied.
  [
    "a non-shell tool over the byte ceiling is not guarded",
    { tool: "write", sessionID: "session-1", callID: "call-1" },
    { content: "x".repeat(200000) },
  ],
  [
    "a non-shell tool with a command field is not guarded",
    { tool: "write", sessionID: "session-1", callID: "call-1" },
    { command: "git clone https://example.test/repo" },
  ],
  [
    "an oversized shell payload still fails closed",
    { tool: "bash", sessionID: "session-1", callID: "call-1" },
    { command: "x".repeat(200000) },
  ],
  [
    "a missing tool name falls through and fails closed",
    { sessionID: "session-1", callID: "call-1" },
    { command: "x".repeat(200000) },
  ],
];

const results: unknown[] = [];
for (const [name, input, args] of cases) {
  try {
    await before(input, { args });
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


class VibeFormatTests(unittest.TestCase):
    def test_vibe_is_a_format_and_renders_the_vibe_deny_shape(self) -> None:
        self.assertIn("vibe", H.FORMATS)
        fmt, exit2 = H._parse_args(["--format", "vibe"])
        self.assertEqual((fmt, exit2), ("vibe", False))
        body = json.loads(H.render("no clone at /tmp/x please", "vibe", "/tmp/x"))
        self.assertEqual(body, {"decision": "deny", "reason": "no clone at /tmp/x please"})

    def test_vibe_deny_output_is_checked_as_decision_and_reason(self) -> None:
        good = json.dumps({"decision": "deny", "reason": "no /tmp/hh-verify"}).encode()
        self.assertIsNone(T.check_deny_output("vibe", good))
        claude = json.dumps(
            {"hookSpecificOutput": {"permissionDecisionReason": "x /tmp/hh-verify"}}
        ).encode()
        self.assertIsNotNone(T.check_deny_output("vibe", claude))


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


class VibeTomlSafetyTests(World):
    """The Vibe gate is written safely, and the file stays loadable on every Python."""

    def test_the_gate_replaces_the_whole_value_when_it_spans_lines(self) -> None:
        # A value may span lines,  and replacing only its first line leaves the rest of
        # it behind as orphan text,  which is not valid TOML:  Vibe's config would stop
        # loading.  The whole value goes.
        source = 'enable_experimental_hooks = [\n  "x",\n]\nother = 1\n'
        out = T.vibe_set_gate(source)
        self.assertIn("enable_experimental_hooks = true", out)
        self.assertNotIn('"x"', out)
        self.assertNotIn("]", out)
        self.assertIn("other = 1", out)
        self.assertTrue(T.vibe_gate(out))
        self.assertIsNone(T._toml_problem(out))

    def test_a_single_line_gate_is_replaced_in_place(self) -> None:
        # The ordinary case must not change:  the key is replaced and nothing else moves.
        source = "enable_experimental_hooks = false\nx = 1\n"
        out = T.vibe_set_gate(source)
        self.assertEqual(out, "enable_experimental_hooks = true\nx = 1\n")

    def test_the_toml_check_catches_a_value_that_was_replaced_in_the_middle(self) -> None:
        # The corruption a replace leaves behind,  which is what the check has to see.
        broken = 'enable_experimental_hooks = true\n  "x",\n]\nother = 1\n'
        self.assertIsNotNone(T._toml_problem(broken))

    def test_the_structural_check_catches_what_tomllib_catches(self) -> None:
        # On 3.11+ tomllib catches the corruption,  but the installer runs on the
        # minimum Python,  where there is no tomllib and the single regex it used to
        # fall back to found nothing.  The structural check is what stands in for
        # tomllib there,  so it has to catch the same corruption.
        broken = 'enable_experimental_hooks = true\n  "x",\n]\nother = 1\n'
        self.assertIsNotNone(T._toml_structure_problem(broken))
        self.assertIsNotNone(T._toml_structure_problem('models = [\n  "a",\n'))
        self.assertIsNotNone(T._toml_structure_problem('s = """\nhello\n'))
        self.assertIsNone(T._toml_structure_problem('models = [\n  "a",\n]\nx = 1\n'))
        self.assertIsNone(T._toml_structure_problem("[section]\nhooks = 1\n"))

    def test_the_toml_check_accepts_a_valid_file(self) -> None:
        for text in (
            'models = [\n  "a",\n]\nother = 1\n',
            "[section]\nhooks = 1\n",
            '[tools.bash]\nname = "x"\n',
            "# a comment\nkey = 1\n",
            # A dotted key is valid TOML,  and Vibe's own config uses them,  so a
            # scanner that rejects one would refuse a good file and block the install.
            "models.openai = 1\n",
            "a.b.c = 2\n",
            'quoted."with.dots" = 1\n',
        ):
            with self.subTest(text=text):
                self.assertIsNone(T._toml_problem(text))

    def test_the_toml_check_still_catches_a_corrupt_line(self) -> None:
        # The dotted-key allowance must not weaken the check:  a line that is not a
        # key at all is still reported.
        self.assertIsNotNone(T._toml_problem('enable_experimental_hooks = true\n  "x",\n]\nother = 1\n'))

    def test_the_toml_check_reports_an_unclosed_value(self) -> None:
        self.assertIsNotNone(T._toml_problem('models = [\n  "a",\n'))
        self.assertIsNotNone(T._toml_problem('s = """\nhello\n'))

    def test_a_top_level_hooks_key_is_reported_but_a_table_scoped_one_is_not(self) -> None:
        # A top-level `hooks` key would be redefined by the [[hooks]] table this tool
        # writes,  and that is a problem.  A key under a table belongs to that table,
        # and refusing it would refuse a good file.
        self.assertIsNotNone(T._toml_problem("hooks = 1\nother = 2\n"))
        self.assertIsNone(T._toml_problem("[section]\nhooks = 1\n"))


class VibeTests(World):
    CONFIG = (
        'active_model = "mistral-medium-3.5"\nvim_keybindings = false\n\n[experiment_overrides]\n'
    )

    def setUp(self) -> None:
        super().setUp()
        (self.home / ".vibe").mkdir(parents=True)
        (self.home / ".vibe" / "config.toml").write_text(self.CONFIG, encoding="utf-8")
        self.install_tools()
        self.hooks = self.home / ".vibe" / "hooks.toml"
        self.config = self.home / ".vibe" / "config.toml"

    def test_the_gate_is_read_and_set_at_the_top_level_only(self) -> None:
        self.assertIsNone(T.vibe_gate(self.CONFIG))
        self.assertIs(
            T.vibe_gate("[experiment_overrides]\nenable_experimental_hooks = true\n"), None
        )
        new = T.vibe_set_gate(self.CONFIG)
        self.assertIs(T.vibe_gate(new), True)
        self.assertIn("[experiment_overrides]", new)
        self.assertLess(new.index("enable_experimental_hooks"), new.index("[experiment_overrides]"))
        off = T.vibe_set_gate("enable_experimental_hooks = false\nx = 1\n")
        self.assertTrue(off.startswith("enable_experimental_hooks = true\nx = 1\n"))

    def test_the_gate_is_not_inserted_inside_a_value_that_spans_lines(self) -> None:
        # The gate goes after the last top-level key, and a value that spans lines ends where it ends: inserting
        # into the middle of one writes a config.toml that will not parse (and nothing catches it without tomllib).
        for src in (
            'mcp_servers = [\n  "alpha",\n  "beta",\n]\n',
            'system_prompt = """one\ntwo\nthree"""\n',
            'mcp_servers = [\n  "alpha",\n]\n\n[experiment_overrides]\nx = 1\n',
        ):
            with self.subTest(src=src):
                new = T.vibe_set_gate(src)
                self.assertIs(T.vibe_gate(new), True)
                try:
                    import tomllib

                    tomllib.loads(new)
                except ImportError:
                    self.skipTest("no tomllib to validate with")
                except tomllib.TOMLDecodeError as exc:
                    self.fail(f"the new config.toml would not parse ({exc}):\n{new}")

    def test_the_match_is_vibes_shell_tool_name(self) -> None:
        # Vibe matches the hook against the tool name it sends, and a wrong exact name silently never fires, so the
        # name is pinned to the one the installed package and the live config agree on.
        self.assertEqual(T.VIBE_SHELL_TOOL, "bash")
        self.assertEqual(T.vibe_fields(T.PLATFORMS["vibe"], self.paths)["match"], "bash")
        self.assertIn('match = "bash"', T.block_body(T.PLATFORMS["vibe"], self.paths))

    def test_apply_writes_the_block_and_the_gate_and_is_idempotent(self) -> None:
        r = self.apply("vibe")
        self.assertEqual(r.status, "added", r)
        text = self.hooks.read_text(encoding="utf-8")
        for needle in (
            "[[hooks]]",
            'name = "fleet-lane-guard"',
            'type = "before_tool"',
            'match = "bash"',
            "--format vibe",
            "strict = true",
        ):
            self.assertIn(needle, text)
        self.assertIs(T.vibe_gate(self.config.read_text(encoding="utf-8")), True)
        self.assertIn(
            'active_model = "mistral-medium-3.5"', self.config.read_text(encoding="utf-8")
        )
        fields = T.vibe_fields(T.PLATFORMS["vibe"], self.paths)
        self.assertEqual(fields["match"], "bash")
        after = snapshot(self.home)
        self.assertEqual(self.apply("vibe").status, "unchanged")
        self.assertEqual(snapshot(self.home), after)

    def test_plan_is_read_only_and_names_the_match_choice(self) -> None:
        before = snapshot(self.tmp)
        rc, out, err = run_main("plan", *self.base(), "vibe")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn('match = "bash"', out)
        self.assertIn("enable_experimental_hooks = true", out)
        self.assertIn("UNVERIFIED", out)
        self.assertNotIn("mistral-medium-3.5", out)

    def test_verify_proves_the_deny_and_the_gate(self) -> None:
        self.apply("vibe")
        checks = T.verify_block(
            T.PLATFORMS["vibe"], self.paths, timeout=TIMEOUT, minimal_path=False
        )
        self.assertPassOrSkip(checks)
        by = {c.name: c for c in checks}
        for name in (
            "block",
            "hooks gate",
            "deny/login-PATH",
            "allow/login-PATH",
            "secret/login-PATH",
        ):
            self.assertEqual(by[name].status, "PASS", by[name].line())
        self.assertEqual(by["fires"].status, "SKIP")

    def test_verify_fails_when_the_gate_is_off(self) -> None:
        self.apply("vibe")
        self.config.write_text(self.CONFIG, encoding="utf-8")
        checks = T.verify_block(
            T.PLATFORMS["vibe"], self.paths, timeout=TIMEOUT, minimal_path=False
        )
        gate = [c for c in checks if c.name == "hooks gate"]
        self.assertEqual(gate[0].status, "FAIL", gate[0].line())
        self.assertIn("loads no hooks", gate[0].detail)

    def test_a_missing_config_is_refused_not_created(self) -> None:
        self.config.unlink()
        r = self.apply("vibe")
        self.assertEqual(r.status, "refused", r)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.hooks.exists())


class OpenCodePluginShapeTests(World):
    """The plugin probes only command-shaped values, and validates the permission input."""

    def setUp(self) -> None:
        super().setUp()
        (self.home / ".config" / "opencode").mkdir(parents=True)

    def plugin_text(self) -> str:
        """The plugin source the installer writes, as text.

        ``build_files`` returns the bytes it would write,  so they are written into the
        fake stable directory here and the plugin is read back from it,  which is what
        the plugin that ships actually contains.
        """
        files = T.build_files(self.source, self.paths)
        self.assertIn(T.OPENCODE_PLUGIN_FILE, files)
        stable = Path(self.paths.stable)
        for relative, blob in files.items():
            target = stable / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        return Path(self.paths.opencode_plugin).read_text(encoding="utf-8")

    def node(self) -> str:
        """The node binary,  or a skip:  the plugin is TypeScript,  so node parses it."""
        found = shutil.which("node")
        if found is None:
            self.skipTest("no node on PATH to parse the plugin with")
        return found

    def run_node(self, args: list, cwd: str) -> subprocess.CompletedProcess:
        """node,  with a skip when this node is too old for the type stripping."""
        run = subprocess.run(
            [self.node(), *args],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
        )
        if run.returncode != 0 and "bad option" in run.stderr:
            self.skipTest("this node has no --experimental-strip-types")
        return run

    def permission_results(self) -> dict:
        """The plugin's answer to each prompt the harness sends,  read back from node."""
        with tempfile.TemporaryDirectory(prefix="muster-plugin-") as tmp:
            plugin = Path(tmp) / "fleet-lane-guard.ts"
            plugin.write_text(self.plugin_text(), encoding="utf-8")
            harness = Path(tmp) / "harness.ts"
            harness.write_text(
                PERMISSION_HARNESS.replace("PLUGIN_URL", plugin.as_uri()), encoding="utf-8"
            )
            run = self.run_node(["--experimental-strip-types", str(harness)], cwd=tmp)
            self.assertEqual(run.returncode, 0, run.stderr)
            lines = [line for line in run.stdout.splitlines() if line.strip()]
            self.assertTrue(lines, run.stdout + run.stderr)
            results = json.loads(lines[-1])
        by_name = {r["name"]: r for r in results}
        self.assertEqual(
            sorted(by_name),
            sorted(MALFORMED_PROMPTS + UNBOUNDED_PROMPTS + (WELL_FORMED_PROMPT,)),
        )
        return by_name

    def test_only_command_shaped_values_are_probed(self) -> None:
        # A permission pattern is a glob that names WHAT may be called,  and a title is
        # prose.  Neither is a shell command,  so neither is sent to the guard:  that
        # would cost one child process per string and could deny a call that is not a
        # fleet clone at all.
        text = self.plugin_text()
        self.assertIn("isCommandShaped", text)
        self.assertIn("COMMAND_SHAPE", text)
        # The filter must be applied to every source of candidates,  each one on its own:
        # a count of the call sites would still pass with one of them left unfiltered.
        body = text.split("function guardCandidates(", 1)[1].split("\n}", 1)[0]
        for needle in (
            "isCommandShaped(pattern)",  # the pattern string
            "isCommandShaped(item)",  # the entries of a pattern array
            "isCommandShaped(value)",  # the command keys in the metadata
        ):
            self.assertIn(needle, body)
        # The definition is not a call site,  so the calls are counted on their own.
        calls = text.count("isCommandShaped(") - text.count("function isCommandShaped(")
        self.assertEqual(calls, 3)

    def test_the_permission_input_is_validated_at_runtime(self) -> None:
        # A TypeScript annotation is not a runtime check,  so a malformed hookInput
        # would be read as if it were well formed.
        text = self.plugin_text()
        self.assertIn("permissionInput", text)
        self.assertIn("must be an object", text)
        self.assertIn("must be a string or an array of strings", text)
        self.assertIn("must not be empty", text)
        # The identifiers are the correlation key,  so they are checked as well:  a null
        # key would let the call through.
        self.assertIn("a permission sessionID must be a non-empty string", text)
        self.assertIn("a permission callID must be a non-empty string", text)

    def test_the_command_shape_class_needs_no_backslash_escape(self) -> None:
        # The COMMAND_SHAPE class is spelled with plain characters,  because the plugin
        # lives in a Python string,  where a backslash is an escape and would arrive as
        # the character it names.
        text = self.plugin_text()
        self.assertNotIn("\\s", text.split("const COMMAND_SHAPE")[1].split("\n")[0])

    def test_the_plugin_source_parses_as_typescript(self) -> None:
        # The plugin is TypeScript that OpenCode loads,  so it has to parse.  A syntax
        # error in it would break every OpenCode session,  not just the guard.
        # node --check does not strip the types,  so node's own TypeScript loader parses
        # the file here.
        with tempfile.TemporaryDirectory(prefix="muster-plugin-parse-") as tmp:
            plugin = Path(tmp) / "fleet-lane-guard.ts"
            plugin.write_text(self.plugin_text(), encoding="utf-8")
            run = self.run_node(["--experimental-strip-types", str(plugin)], cwd=tmp)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_the_javascript_the_plugin_becomes_passes_node_check(self) -> None:
        # The JavaScript the plugin becomes is what OpenCode runs,  so node --check
        # parses that too.
        with tempfile.TemporaryDirectory(prefix="muster-plugin-check-") as tmp:
            plugin = Path(tmp) / "fleet-lane-guard.ts"
            plugin.write_text(self.plugin_text(), encoding="utf-8")
            stripped = Path(tmp) / "fleet-lane-guard.mjs"
            probe = self.run_node(
                ["--eval", "console.log(typeof require('node:module').stripTypeScriptTypes)"],
                cwd=tmp,
            )
            if probe.returncode != 0 or "function" not in probe.stdout:
                self.skipTest("this node cannot strip the types itself")
            strip = self.run_node(
                [
                    "--eval",
                    STRIP_SCRIPT,
                    str(plugin),
                    str(stripped),
                ],
                cwd=tmp,
            )
            self.assertEqual(strip.returncode, 0, strip.stderr)
            check = self.run_node(["--check", str(stripped)], cwd=tmp)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_a_malformed_permission_prompt_is_denied_and_never_thrown(self) -> None:
        # A prompt the plugin cannot rule on is a denial,  not an error the caller sees:
        # a throw would leave the answer to OpenCode's default handling,  which is to
        # allow.
        results = self.permission_results()
        for name in MALFORMED_PROMPTS:
            self.assertFalse(results[name]["threw"], results[name])
            self.assertEqual(results[name]["status"], "deny", results[name])
        # A prompt the plugin can rule on is left alone:  the deny is specific.
        self.assertFalse(results[WELL_FORMED_PROMPT]["threw"], results[WELL_FORMED_PROMPT])
        self.assertEqual(results[WELL_FORMED_PROMPT]["status"], "ask")

    def test_only_the_shell_tool_reaches_the_command_guard(self) -> None:
        # sentry #757:  the bounds check ran for every tool,  so a file write over 64 KiB was denied.
        with tempfile.TemporaryDirectory(prefix="muster-plugin-") as tmp:
            plugin = Path(tmp) / "fleet-lane-guard.ts"
            plugin.write_text(self.plugin_text(), encoding="utf-8")
            harness = Path(tmp) / "harness.ts"
            harness.write_text(
                EXECUTE_HARNESS.replace("PLUGIN_URL", plugin.as_uri()), encoding="utf-8"
            )
            run = self.run_node(["--experimental-strip-types", str(harness)], cwd=tmp)
            self.assertEqual(run.returncode, 0, run.stderr)
            results = {r["name"]: r for r in json.loads(run.stdout.splitlines()[-1])}
        for name in (
            "a non-shell tool over the byte ceiling is not guarded",
            "a non-shell tool with a command field is not guarded",
        ):
            self.assertFalse(results[name]["threw"], results[name])
        for name in (
            "an oversized shell payload still fails closed",
            "a missing tool name falls through and fails closed",
        ):
            self.assertTrue(results[name]["threw"], results[name])

    def test_an_oversized_or_cyclic_permission_prompt_is_denied(self) -> None:
        # The bounds the command boundary already carries apply to the permission
        # prompt too,  and they are checked before any field is read:  a payload that is
        # too large,  too deep,  too wide or cyclic is refused,  not walked.
        results = self.permission_results()
        for name in UNBOUNDED_PROMPTS:
            self.assertFalse(results[name]["threw"], results[name])
            self.assertEqual(results[name]["status"], "deny", results[name])


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
            "plugin": ["opencode-mobile@latest"],
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
            '"tool.execute.before"',
            '"permission.ask"',
            'output.status = "deny"',
            "fleet-guard-hook",
            "args.command",
        ):
            if needle == "args.command":
                continue  # keyed on args, not on a tool name; see the next assertion
            self.assertIn(needle, text)
        self.assertIn("commandFrom(output.args)", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Bearer", text)
        self.assertNotIn(DUMMY_TOKEN, text)

    def test_apply_lists_the_plugin_by_absolute_path_and_preserves_the_file(self) -> None:
        before = json.loads(self.config.read_text(encoding="utf-8"))
        r = self.apply("opencode")
        self.assertEqual(r.status, "updated", r)
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(doc["mcp"], before["mcp"])
        self.assertEqual(doc["plugin"], ["opencode-mobile@latest", self.paths.opencode_plugin])
        self.assertTrue(os.path.isabs(doc["plugin"][-1]))
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
        self.assertIn(self.paths.opencode_plugin, out)
        self.assertIn("UNVERIFIED", out)
        self.assertIn("permission.ask", out)
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
        doc["plugin"] = ["opencode-mobile@latest"]
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
        doc["plugin"] = [
            "/old/stable/opencode-plugin/fleet-lane-guard.ts",
            "opencode-mobile@latest",
        ]
        self.config.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        new, action = T.opencode_merge(doc, self.paths)
        self.assertEqual(action, "updated")
        self.assertEqual(new["plugin"], [self.paths.opencode_plugin, "opencode-mobile@latest"])
        again, action = T.opencode_merge(new, self.paths)
        self.assertEqual((action, again), ("unchanged", new))

    def test_a_missing_config_file_is_created_with_the_plugin_and_an_owner_only_mode(self) -> None:
        self.config.unlink()
        r = self.apply("opencode")
        self.assertEqual(r.status, "added", r)
        doc = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(doc["plugin"], [self.paths.opencode_plugin])
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)

    def test_a_plugin_key_that_is_not_a_list_is_refused_and_left_alone(self) -> None:
        self.config.write_text(
            json.dumps({"plugin": "not-a-list"}, indent=2) + "\n", encoding="utf-8"
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
        (self.home / ".vibe").mkdir(parents=True)
        (self.home / ".vibe" / "config.toml").write_text('active_model = "x"\n', encoding="utf-8")
        (self.home / ".config" / "opencode").mkdir(parents=True)
        (self.home / ".config" / "opencode" / "opencode.json").write_text(
            json.dumps({"plugin": ["opencode-mobile@latest"]}, indent=2) + "\n", encoding="utf-8"
        )
        self.install_tools()

    def test_apply_then_verify_through_the_cli_for_each_new_platform(self) -> None:
        for key in ("copilot", "vibe", "opencode"):
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
        self.assertTrue((self.home / ".vibe" / "hooks.toml").is_file())
        self.assertTrue((self.home / ".config" / "opencode" / "opencode.json").is_file())
        self.assertFalse(fx_settings.exists())

    def test_verify_all_reports_fx_red_and_the_others_green(self) -> None:
        (self.home / ".fx").mkdir(parents=True)
        (self.home / ".fx" / "settings.json").write_text(
            json.dumps({"provider": "gateway", "permission_mode": "yolo"}) + "\n", encoding="utf-8"
        )
        run_main("apply", *self.base(), "--no-verify", "copilot", "vibe", "opencode")
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
        for key in ("copilot", "vibe", "opencode"):
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
        target = self.home / ".vibe" / "hooks.toml"
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
        vibe = self.home / ".vibe"
        vibe.mkdir()
        with T._target_lock(str(self.home), str(vibe / "hooks.toml")):
            pass
        self.assertEqual(sorted(p.name for p in vibe.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
