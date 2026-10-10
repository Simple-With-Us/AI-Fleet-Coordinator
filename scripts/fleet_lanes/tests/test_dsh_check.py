"""dsh_check tests: the --dump-config proof for a Clutch cordis patch, against a stub engine.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_dsh_check -v

The stub records what it was given (the scratch DSH_HOME's entries and the environment's names), so the tests can
prove the real DSH_HOME was never read except for its profile folders, and that no inherited variable reaches the
engine.  No real dsh runs here; the real engine was run by hand against a scratch DSH_HOME on 2026-10-10.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import cordis_patch as CP
from fleet_lanes import dsh_check as DC

STUB = '''#!{python}
import json, os, sys
log = {log!r}
home = os.environ["DSH_HOME"]
with open(log, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "entries": sorted(os.listdir(home)),
                          "env": sorted(os.environ), "profiles": sorted(os.listdir(os.path.join(home, "profiles")))}}) + "\\n")
patch = os.path.join(home, "cordis.patch.yml")
text = open(patch).read() if os.path.exists(patch) else ""
if {mode!r} == "slow":
    import time
    time.sleep(30)
if {mode!r} == "crash" or not any(line.startswith("- ") for line in text.splitlines()):
    sys.stderr.write("TypeError: patch is not an array\\nNode.js v24\\n")
    sys.exit(1)
print("@deepseek-ai/dsh-base")
if "dsh-hooks-claude-code" in text:
    print("@deepseek-ai/dsh-hooks-claude-code")
'''

PATCH = "- insert:\n    - id: fleet-hooks-guards\n      name: '@deepseek-ai/dsh-hooks-claude-code'\n      config:\n        configPath: \"/x/hooks.json\"\n"
PLUGIN = "@deepseek-ai/dsh-hooks-claude-code"


class World(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.log = self.tmp / "calls.jsonl"
        self.profiles = self.tmp / "dsh" / "profiles"
        for name in ("web", "headless"):
            (self.profiles / name).mkdir(parents=True)
            (self.profiles / name / "cordis.yml").write_text("plugins: []\n")
        (self.profiles / "node_modules").mkdir()
        (self.profiles / "notes").mkdir()                                  # no cordis.yml: not a profile
        # things a real DSH_HOME holds that must never be copied
        (self.tmp / "dsh" / ".credentials.yaml").write_text("secret: sentinel-credential\n")
        (self.tmp / "dsh" / "settings.yaml").write_text("key: sentinel-setting\n")

    def stub(self, mode: str = "ok") -> str:
        p = self.tmp / "dsh-stub"
        p.write_text(STUB.format(python=sys.executable, log=str(self.log), mode=mode))
        os.chmod(p, 0o755)
        return str(p)

    def calls(self) -> list:
        return [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.exists() else []


class FindTests(World):
    def test_find_dsh_needs_an_executable_file(self) -> None:
        home = self.tmp / "h"
        binary = home / "apps" / "clutch-runtime" / "node_modules" / ".bin" / "dsh"
        self.assertIsNone(DC.find_dsh(str(home)))
        binary.parent.mkdir(parents=True)
        binary.write_text("x")
        self.assertIsNone(DC.find_dsh(str(home)))
        os.chmod(binary, 0o755)
        self.assertEqual(DC.find_dsh(str(home)), str(binary))

    def test_profile_names_are_folders_with_a_cordis_yml(self) -> None:
        self.assertEqual(DC.profile_names(str(self.profiles)), ["headless", "web"])
        self.assertEqual(DC.profile_names(str(self.tmp / "missing")), [])


def link_plugin(profiles: Path, package: str, target: Path | None = None) -> Path:
    """<profiles>/node_modules/<package> as a link to a real folder (or to `target`, which may not exist)."""
    link = profiles / "node_modules" / Path(*package.split("/"))
    link.parent.mkdir(parents=True, exist_ok=True)
    real = target or (profiles.parent / "pkgs" / package.replace("/", "__"))
    if target is None:
        real.mkdir(parents=True, exist_ok=True)
    os.symlink(real, link)
    return link


class ResolveAndReloadTests(World):
    def set_reload(self, name: str, value) -> None:
        pkg = {"name": "dsh-profile-" + name, "dsh": {"profile": {"bundles": []}}}
        if value is not None:
            pkg["dsh"]["profile"]["patchReload"] = value
        (self.profiles / name / "package.json").write_text(json.dumps(pkg))

    def test_a_plugin_must_resolve_under_the_real_profiles_node_modules(self) -> None:
        self.assertEqual(len(DC.unresolved(str(self.profiles), [PLUGIN])), 1)
        self.assertIn("not linked under", DC.unresolved(str(self.profiles), [PLUGIN])[0])
        link_plugin(self.profiles, PLUGIN)
        self.assertEqual(DC.unresolved(str(self.profiles), [PLUGIN]), [])

    def test_a_link_to_a_path_that_is_gone_is_named_as_such(self) -> None:
        link_plugin(self.profiles, PLUGIN, self.tmp / "gone" / "pnpm" / "dsh-hooks-claude-code")
        (line,) = DC.unresolved(str(self.profiles), [PLUGIN])
        self.assertIn("no longer exists", line)
        self.assertIn(str(self.tmp / "gone"), line)

    def test_every_expected_plugin_is_checked(self) -> None:
        link_plugin(self.profiles, PLUGIN)
        other = "@deepseek-ai/dsh-mcp-client"
        self.assertEqual(len(DC.unresolved(str(self.profiles), [PLUGIN, other])), 1)

    def test_patch_reload_is_read_from_each_profile(self) -> None:
        self.set_reload("web", "live")
        self.set_reload("headless", "startup")
        self.assertEqual(DC.patch_reload_modes(str(self.profiles)), {"headless": "startup", "web": "live"})
        self.set_reload("web", None)
        (self.profiles / "headless" / "package.json").write_text("{not json")
        self.assertEqual(DC.patch_reload_modes(str(self.profiles)), {"headless": "unknown", "web": "live (default)"})
        self.set_reload("web", "sometimes")
        self.assertEqual(DC.patch_reload_modes(str(self.profiles))["web"], "unknown")

    def test_the_note_says_which_engine_gets_the_write_at_once(self) -> None:
        self.set_reload("web", "live")
        self.set_reload("headless", "startup")
        note = DC.reload_note(str(self.profiles))
        self.assertIn("headless startup, web live", note)
        self.assertIn("reaches the running web engine at once", note)
        self.assertIn("headless read it at their next start", note)
        self.assertNotIn("read once", note)
        self.assertIn("unknown", DC.reload_note(str(self.tmp / "none")))

    def test_check_patch_refuses_an_unresolved_plugin_without_starting_the_engine(self) -> None:
        link_plugin(self.profiles, PLUGIN, self.tmp / "gone")
        ok, detail = DC.check_patch(self.stub(), PATCH, str(self.profiles), [PLUGIN], timeout=60)
        self.assertFalse(ok)
        self.assertIn("no longer exists", detail)
        self.assertIn("Restart clutch-web first", detail)
        self.assertEqual(self.calls(), [], "the engine was not run")

    def test_check_patch_passes_when_the_plugin_resolves_and_reports_when_the_patch_is_read(self) -> None:
        link_plugin(self.profiles, PLUGIN)
        self.set_reload("web", "live")
        self.set_reload("headless", "startup")
        ok, detail = DC.check_patch(self.stub(), PATCH, str(self.profiles), [PLUGIN], timeout=60)
        self.assertTrue(ok, detail)
        self.assertIn("merge-and-parse", detail)
        self.assertIn("patchReload: headless startup, web live", detail)

    def test_check_patch_still_fails_a_patch_the_engine_rejects(self) -> None:
        link_plugin(self.profiles, PLUGIN)
        ok, detail = DC.check_patch(self.stub(), "# only a comment\n", str(self.profiles), [PLUGIN], timeout=60)
        self.assertFalse(ok)
        self.assertIn("exited 1", detail)


class DumpConfigTests(World):
    def test_every_profile_must_mount_the_plugin(self) -> None:
        ok, detail = DC.dump_config(self.stub(), PATCH, str(self.profiles), [PLUGIN], timeout=60)
        self.assertTrue(ok, detail)
        self.assertEqual(detail, f"headless, web: the patch merges and the dump names {PLUGIN} "
                                 "(a merge-and-parse check, not a plugin load)")
        self.assertEqual([c["argv"] for c in self.calls()],
                         [["--profile", "headless", "--dump-config"], ["--profile", "web", "--dump-config"]])

    def test_a_patch_without_the_plugin_fails(self) -> None:
        ok, detail = DC.dump_config(self.stub(), "- id: other\n", str(self.profiles), [PLUGIN], timeout=60)
        self.assertFalse(ok)
        self.assertIn("does not name", detail)

    def test_an_engine_that_exits_nonzero_fails_with_its_last_stderr_line(self) -> None:
        ok, detail = DC.dump_config(self.stub("crash"), PATCH, str(self.profiles), [PLUGIN], timeout=60)
        self.assertFalse(ok)
        self.assertIn("exited 1", detail)
        self.assertIn("Node.js v24", detail)

    def test_an_empty_or_comment_only_patch_fails_like_the_real_engine(self) -> None:
        for text in ("", "# only a comment\n"):
            with self.subTest(text=text):
                ok, detail = DC.dump_config(self.stub(), text, str(self.profiles), [PLUGIN], timeout=60)
                self.assertFalse(ok)
        self.assertTrue(DC.dump_config(self.stub(), "[]\n- id: a\n", str(self.profiles), ["@deepseek-ai/dsh-base"], timeout=60)[0])

    def test_a_slow_engine_times_out(self) -> None:
        ok, detail = DC.dump_config(self.stub("slow"), PATCH, str(self.profiles), [PLUGIN], timeout=1)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)

    def test_no_profiles_and_a_missing_binary_are_failures_not_crashes(self) -> None:
        ok, detail = DC.dump_config(self.stub(), PATCH, str(self.tmp / "none"), [PLUGIN])
        self.assertFalse(ok)
        self.assertIn("no profile", detail)
        ok, detail = DC.dump_config(str(self.tmp / "no-such-dsh"), PATCH, str(self.profiles), [PLUGIN])
        self.assertFalse(ok)
        self.assertIn("cannot run", detail)

    def test_only_the_profiles_are_copied_and_the_environment_is_bare(self) -> None:
        with mock.patch.dict(os.environ, {"CLUTCH_API_TOKEN_TEST": "sentinel-env", "DSH_HOME": "/real/dsh"}):
            ok, _ = DC.dump_config(self.stub(), PATCH, str(self.profiles), [PLUGIN], timeout=60)
        self.assertTrue(ok)
        first = self.calls()[0]
        self.assertEqual(first["entries"], ["cordis.patch.yml", "profiles"], "no credentials, no settings")
        self.assertEqual(first["profiles"], ["headless", "node_modules", "notes", "web"], "the profiles folder is copied whole")
        for name in ("CLUTCH_API_TOKEN_TEST",):
            self.assertNotIn(name, first["env"])
        self.assertTrue({"DSH_HOME", "HOME", "PATH"} <= set(first["env"]))

    def test_the_scratch_copy_is_removed_and_the_real_tree_is_untouched(self) -> None:
        before = sorted(str(p) for p in (self.tmp / "dsh").rglob("*"))
        scratch_before = set(os.listdir(tempfile.gettempdir()))
        DC.dump_config(self.stub(), PATCH, str(self.profiles), [PLUGIN], timeout=60)
        self.assertEqual(sorted(str(p) for p in (self.tmp / "dsh").rglob("*")), before)
        leftover = [n for n in set(os.listdir(tempfile.gettempdir())) - scratch_before if n.startswith("fleet-dsh-check-")]
        self.assertEqual(leftover, [])

    def test_the_output_is_never_returned(self) -> None:
        ok, detail = DC.dump_config(self.stub(), PATCH, str(self.profiles), [PLUGIN], timeout=60)
        self.assertNotIn("@deepseek-ai/dsh-base", detail.replace(PLUGIN, ""))


class CordisPatchTests(unittest.TestCase):
    def test_has_entries_and_list_shape(self) -> None:
        self.assertFalse(CP.has_entries(None))
        self.assertFalse(CP.has_entries("# only comments\n"))
        self.assertTrue(CP.has_entries("# c\n- id: a\n"))
        self.assertTrue(CP.is_list_patch("# c\n- id: a\n  config: {}\n"))
        self.assertFalse(CP.is_list_patch("plugins:\n  - a\n"))
        self.assertTrue(CP.is_list_patch(""))

    def test_a_new_patch_starts_with_the_header_and_holds_the_block(self) -> None:
        text, action = CP.upsert(None, "x", "tool", ["- id: a"])
        self.assertEqual(action, "create")
        self.assertTrue(text.startswith(CP.HEADER))
        self.assertTrue(CP.has_entries(text))

    def test_two_blocks_live_side_by_side_and_leave_in_either_order(self) -> None:
        t, _ = CP.upsert(None, "a", "tool", ["- id: a"])
        t, action = CP.upsert(t, "b", "tool", ["- id: b"])
        self.assertEqual(action, "append")
        t, action = CP.remove(t, "a")
        self.assertEqual(action, "remove")
        self.assertIn("- id: b", t)
        self.assertNotIn("- id: a", t)

    def test_removing_the_last_block_deletes_the_file_text(self) -> None:
        t, _ = CP.upsert(None, "a", "tool", ["- id: a"])
        self.assertEqual(CP.remove(t, "a"), (None, "remove"))
        self.assertEqual(CP.remove(t, "zzz"), (t, "none"))
        self.assertEqual(CP.remove(None, "a"), (None, "none"))


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
