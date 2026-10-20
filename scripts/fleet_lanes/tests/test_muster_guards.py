"""AFC-387 tests: the Vibe, OpenCode and fx guards, and the --format vibe deny shape.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_muster_guards -v

Everything runs against a fake home inside a TemporaryDirectory; the real home is never read or
written, and no test passes a real path to apply or verify.  The OpenCode config fixture copies the
SHAPE of the real file (an mcp block and a plugin array, plus a dummy credential-shaped value that
must never be printed or copied) with dummy values only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
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
        ):
            with self.subTest(text=text):
                self.assertIsNone(T._toml_problem(text))

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

    def test_only_command_shaped_values_are_probed(self) -> None:
        # A permission pattern is a glob that names WHAT may be called,  and a title is
        # prose.  Neither is a shell command,  so neither is sent to the guard:  that
        # would cost one child process per string and could deny a call that is not a
        # fleet clone at all.
        text = self.plugin_text()
        self.assertIn("isCommandShaped", text)
        self.assertIn("COMMAND_SHAPE", text)
        # The filter must be applied to every source of candidates.
        self.assertGreaterEqual(text.count("isCommandShaped("), 3)

    def test_the_permission_input_is_validated_at_runtime(self) -> None:
        # A TypeScript annotation is not a runtime check,  so a malformed hookInput
        # would be read as if it were well formed.
        text = self.plugin_text()
        self.assertIn("permissionInput", text)
        self.assertIn("must be an object", text)
        self.assertIn("must be a string or an array of strings", text)
        self.assertIn("must not be empty", text)

    def test_the_plugin_source_is_valid_typescript(self) -> None:
        # The plugin is TypeScript that OpenCode loads,  so it has to parse.  A syntax
        # error in it would break every OpenCode session,  not just the guard.
        text = self.plugin_text()
        self.assertNotIn("\\s", text.split("const COMMAND_SHAPE")[1].split("\n")[0])


class OpenCodeTests(World):
    def setUp(self) -> None:
        super().setUp()
        (self.home / ".config" / "opencode").mkdir(parents=True)
        self.config = self.home / ".config" / "opencode" / "opencode.json"
        doc = {
            "mcp": {
                "verde": {
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
        (self.home / ".vibe").mkdir(parents=True)
        (self.home / ".vibe" / "config.toml").write_text('active_model = "x"\n', encoding="utf-8")
        (self.home / ".config" / "opencode").mkdir(parents=True)
        (self.home / ".config" / "opencode" / "opencode.json").write_text(
            json.dumps({"plugin": ["opencode-mobile@latest"]}, indent=2) + "\n", encoding="utf-8"
        )
        self.install_tools()

    def test_apply_then_verify_through_the_cli_for_each_new_platform(self) -> None:
        for key in ("vibe", "opencode"):
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
        self.assertTrue((self.home / ".vibe" / "hooks.toml").is_file())
        self.assertTrue((self.home / ".config" / "opencode" / "opencode.json").is_file())
        self.assertFalse(fx_settings.exists())

    def test_verify_all_reports_fx_red_and_the_others_green(self) -> None:
        (self.home / ".fx").mkdir(parents=True)
        (self.home / ".fx" / "settings.json").write_text(
            json.dumps({"provider": "gateway", "permission_mode": "yolo"}) + "\n", encoding="utf-8"
        )
        run_main("apply", *self.base(), "--no-verify", "vibe", "opencode")
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
        for key in ("vibe", "opencode"):
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


if __name__ == "__main__":
    unittest.main()
