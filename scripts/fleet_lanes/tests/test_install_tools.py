"""install_tools tests: merge shapes, safe writes, plan purity, and the proof that verify catches a broken install.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_install_tools -v

Everything runs against a fake home inside a TemporaryDirectory; the real home is never read or
written, and no test passes a real path to apply or verify.  Config fixtures copy the SHAPES of the
real hooks files (key names and nesting) with dummy commands.  The verify tests run real
subprocesses through the installed shim, so they prove the same path a platform uses; they use a
generous timeout because the machine running them may be loaded.
"""
from __future__ import annotations

import ast
import concurrent.futures
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import guard as G
from fleet_lanes import install_tools as T
from fleet_lanes import lane_guard_hook as H
from fleet_lanes.tests import fixtures_guard as F

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
REAL_PKG = SCRIPTS_DIR / "fleet_lanes"
REGISTRY_FILE = SCRIPTS_DIR.parent / "fleet-apps.json"
SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"
TIMEOUT = 90.0
STAMP = 1_790_000_000.0          # a fixed clock for backup names
# The minimal-PATH probe needs /usr/bin/python3, which a bare container image may lack.  There the
# product's FAIL is correct; the tests that are not about that fall back to one probe run.
MINIMAL = os.path.exists("/usr/bin/python3")
MINIMAL_ARGS: list = [] if MINIMAL else ["--no-minimal-path"]

LANE_STUB = '''"""Stub lane module for the installer tests."""
import sys


def main(argv=None):
    print("lane-stub-ok " + " ".join(sys.argv[1:] if argv is None else argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

BROKEN_GUARD = '''"""A guard that imports fine and then fails on every call (the hook fails open on it)."""
ALLOW = "allow"
DENY = "deny"


def evaluate(payload, ctx=None):
    raise RuntimeError("broken on purpose")
'''


# --------------------------------------------------------------------------- shapes of the real files

def claude_settings() -> dict:
    """The shape of ~/.claude/settings.json: env block, permissions, and several PreToolUse groups."""
    return {
        "env": {"EXAMPLE_API_TOKEN": "dummy-secret-value-0123456789", "OTHER": "1"},
        "permissions": {"allow": ["Bash(git status)"], "deny": []},
        "hooks": {
            "Notification": [{"matcher": "*", "hooks": [{"type": "command", "command": "/x/notify.sh"}]}],
            "PreToolUse": [
                {"matcher": "Agent|Task|Workflow", "hooks": [
                    {"type": "command", "command": "python3 ~/.claude/hooks/economy.py", "timeout": 5,
                     "statusMessage": "Checking subagent model tier"}]},
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "[ -f scripts/board-check.sh ] && bash scripts/board-check.sh || true",
                     "timeout": 10, "statusMessage": "Checking effort-log protocol"},
                    {"type": "command", "command": "python3 ~/.claude/hooks/command-class.py", "timeout": 3}]},
                {"matcher": "*", "hooks": [{"type": "command", "command": "node /x/remote-pre.js"}]},
                {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "python3 /x/block-xcode.py"}]},
                {"hooks": [{"type": "command", "command": "/x/cc-status"},
                           {"type": "command", "command": "curl -H 'Author" "ization: Bea" "rer Zq9fakeSecret" "TokenValue77' http://127.0.0.1:1/x"}]},
            ],
            "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "node /x/post.js"}]}],
        },
        "theme": "dark",
    }


def codex_hooks() -> dict:
    return {"hooks": {"PreToolUse": [
        {"matcher": "Agent|Task|Workflow", "hooks": [{"type": "command", "command": "python3 '/x/economy.py'", "timeout": 5}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 '/x/secret-guard.py'", "timeout": 5}]},
        {"matcher": "*", "hooks": [{"type": "command", "command": "node '/x/remote.js'"}]},
    ], "Stop": [{"hooks": [{"type": "command", "command": "/x/cc-status"}]}]}}


def cursor_hooks() -> dict:
    return {"version": 1, "hooks": {
        "sessionStart": [{"command": "./hooks/check.sh", "timeout": 8, "failClosed": False}],
        "afterFileEdit": [{"command": "'/x/node' '/x/otlp.mjs' cursor", "timeout": 3}],
    }}


def gemini_hooks() -> dict:
    return {"agent-hook-otlp": {
        "PostInvocation": [{"command": "'/x/node' '/x/otlp.mjs' antigravity", "timeout": 3, "type": "command"}],
        "Stop": [{"command": "'/x/node' '/x/otlp.mjs' antigravity", "timeout": 3, "type": "command"}],
        "enabled": True}}


def grok_imported() -> dict:
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 ~/.claude/hooks/secret-guard.py", "timeout": 5}]}]}}


MUSE_SETTINGS = {"schema_version": 1, "provider": "x", "model": "y", "tui": {}, "permissions": {}}

SHAPES = {
    ".claude/settings.json": claude_settings, ".codex/hooks.json": codex_hooks,
    ".cursor/hooks.json": cursor_hooks, ".gemini/config/hooks.json": gemini_hooks,
    ".grok/hooks/imported-from-claude.json": grok_imported,
    ".config/muse/settings.json": lambda: copy.deepcopy(MUSE_SETTINGS),
}


def write_json(path: Path, doc: object, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)


def snapshot(root: Path) -> dict:
    """Every entry under root: kind, sha256 of file bytes, permission bits.  Used to prove 'no write'."""
    out: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            p = Path(dirpath) / d
            out[str(p.relative_to(root))] = ("dir", None, stat.S_IMODE(p.lstat().st_mode))
        for f in filenames:
            p = Path(dirpath) / f
            st = p.lstat()
            out[str(p.relative_to(root))] = ("file", hashlib.sha256(p.read_bytes()).hexdigest() if stat.S_ISREG(st.st_mode) else "link",
                                             stat.S_IMODE(st.st_mode))
    return out


def run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = T.main(list(argv), stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


REAL_MUSE = shutil.which("muse")          # looked up before any test changes PATH; only one opt-in test may run it
REAL_MUSE_OPT_IN = "FLEET_LANES_TEST_REAL_MUSE"


def path_without_muse() -> str:
    """PATH with every directory that holds a `muse` removed, so no test but the one dedicated to it reaches the
    real Muse Code, and a machine with muse and one without give the same results."""
    keep = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not os.path.exists(os.path.join(d, "muse"))]
    return os.pathsep.join(keep)


class World(unittest.TestCase):
    """A fake home, a small source tree, and helpers.  Nothing here touches the real home."""

    def setUp(self) -> None:
        path_patch = mock.patch.dict(os.environ, {"PATH": path_without_muse()})
        path_patch.start()
        self.addCleanup(path_patch.stop)
        tmp = tempfile.TemporaryDirectory(prefix="lane-tools-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.paths = T.make_paths(str(self.home))
        self.source = self.make_source("src")

    # -- builders

    def make_source(self, name: str, with_lane: bool = True, guard: str | None = None, sha: str = SHA) -> T.Source:
        pkg = self.tmp / name / "scripts" / "fleet_lanes"
        pkg.mkdir(parents=True)
        for m in T.REQUIRED_MODULES:
            shutil.copy(REAL_PKG / m, pkg / m)
        if guard is not None:
            (pkg / "guard.py").write_text(guard, encoding="utf-8")
        if with_lane:
            (pkg / "lane.py").write_text(LANE_STUB, encoding="utf-8")
        shutil.copy(REGISTRY_FILE, self.tmp / name / "fleet-apps.json")
        hooks = self.tmp / name / "scripts" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy(SCRIPTS_DIR / "hooks" / "secret-guard-pretooluse.py", hooks / "secret-guard-pretooluse.py")
        return T.Source(str(self.tmp / name / "scripts"), str(self.tmp / name / "fleet-apps.json"), sha)

    def seed(self, *rels: str) -> None:
        for rel in rels or tuple(SHAPES):
            write_json(self.home / rel, SHAPES[rel](), 0o600 if rel == ".claude/settings.json" else None)

    def seed_platforms(self) -> None:
        self.seed(".claude/settings.json", ".codex/hooks.json", ".cursor/hooks.json",
                  ".gemini/config/hooks.json", ".grok/hooks/imported-from-claude.json")

    def install(self, platforms: tuple = T.SUPPORTED_KEYS, source: T.Source | None = None) -> None:
        """apply tools and the platforms without probing (verify does the proving in the tests)."""
        r = T.apply_tools(source or self.source, self.paths, timeout=TIMEOUT, probes=False)
        self.assertTrue(r.ok, r)
        for key in platforms:
            r = T.apply_platform(T.PLATFORMS[key], self.paths, now=lambda: STAMP, shim_proven=True)
            self.assertTrue(r.ok, r)

    def config(self, key: str) -> Path:
        return Path(T.config_path(self.paths, T.PLATFORMS[key]))

    def load(self, key: str) -> dict:
        return json.loads(self.config(key).read_text(encoding="utf-8"))

    def verify(self, key: str, minimal_path: bool = False, timeout: float = TIMEOUT) -> list:
        return T.verify_platform(T.PLATFORMS[key], self.paths, timeout=timeout, minimal_path=minimal_path)

    def statuses(self, checks: list) -> set:
        return {c.status for c in checks}

    def assertAllPass(self, checks: list) -> None:
        bad = [c.line() for c in checks if c.status not in ("PASS", "WARN")]
        self.assertEqual(bad, [], "\n".join(bad))

    def assertFails(self, checks: list, needle: str = "") -> None:
        fails = [c for c in checks if c.status == "FAIL"]
        self.assertTrue(fails, "expected a FAIL, got:\n" + "\n".join(c.line() for c in checks))
        if needle:
            self.assertTrue(any(needle in c.line() for c in fails), f"no FAIL mentions {needle!r}:\n" + "\n".join(c.line() for c in fails))

    def backups(self, directory: Path) -> list[Path]:
        return sorted(p for p in directory.iterdir() if ".bak-lane-guard-" in p.name)


# --------------------------------------------------------------------------- the probes themselves

class ProbeTests(unittest.TestCase):
    """The probes are copies of fixture rows; the guard must really decide them."""

    @staticmethod
    def fixture(fid: str) -> F.GuardFixture:
        return next(f for f in F.FIXTURES if f.id == fid)

    def test_probe_commands_are_fixture_rows(self) -> None:
        deny, allow = self.fixture("spec-gh-hoghunter"), self.fixture("spec-third-party")
        self.assertEqual((T.DENY_COMMAND, deny.expect), (deny.command, "deny"))
        self.assertEqual((T.ALLOW_COMMAND, allow.expect), (allow.command, "allow"))
        self.assertIn(T.DENY_MARKER, deny.command)

    def test_allow_probe_reaches_the_evaluator(self) -> None:
        self.assertIsNotNone(G._TRIGGER.search(T.ALLOW_COMMAND))
        self.assertIsNotNone(G._TRIGGER.search(T.DENY_COMMAND))

    def test_every_payload_shape_is_decided_by_the_real_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            ctx = G.make_context({"HOME": home, "FLEET_APPS_JSON": str(REGISTRY_FILE)}, home=home)   # no AGENT_SEAT
            for shape in ("claude", "antigravity", "cursor"):
                with self.subTest(shape):
                    d = G.evaluate(T.probe_payload(shape, T.DENY_COMMAND, home), ctx)
                    self.assertEqual(d.action, G.DENY, d)
                    a = G.evaluate(T.probe_payload(shape, T.ALLOW_COMMAND, home), ctx)
                    self.assertEqual(a.action, G.ALLOW, a)

    def test_check_deny_output_per_format(self) -> None:
        reason = "no clone at /tmp/hh-verify please"
        good = {
            "claude": {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}},
            "grok": {"decision": "deny", "reason": reason},
            "cursor": {"permission": "deny", "user_message": "blocked", "agent_message": reason},
        }
        good["codex"] = good["muse"] = good["claude"]
        good["antigravity"] = good["grok"]
        for fmt, body in good.items():
            with self.subTest(fmt):
                self.assertIsNone(T.check_deny_output(fmt, json.dumps(body).encode()))
        # wrong shape for the format, not a deny, missing marker, empty, not JSON
        self.assertIsNotNone(T.check_deny_output("cursor", json.dumps(good["claude"]).encode()))
        self.assertIsNotNone(T.check_deny_output("claude", json.dumps(good["grok"]).encode()))
        self.assertIsNotNone(T.check_deny_output("grok", json.dumps(good["cursor"]).encode()))
        self.assertIsNotNone(T.check_deny_output("grok", json.dumps({"decision": "allow", "reason": reason}).encode()))
        self.assertIsNotNone(T.check_deny_output("grok", json.dumps({"decision": "deny", "reason": "other"}).encode()))
        self.assertIn("no output", T.check_deny_output("claude", b"") or "")
        self.assertIsNotNone(T.check_deny_output("claude", b"not json"))
        self.assertIsNotNone(T.check_deny_output("claude", b"[1]"))

    def test_probe_env_scrubs_and_pins(self) -> None:
        base = {"PATH": "/a:/b", "HOME": "/real", "PYTHONPATH": "/x", "PYTHONHOME": "/y", "FLEET_APPS_JSON": "/z",
                "FLEET_LANE_GUARD": "off", "AGENT_SEAT": "CLAUDE", "KEEP": "1"}
        env = T.probe_env("/fake", None, base)
        self.assertEqual(env, {"PATH": "/a:/b", "HOME": "/fake", "KEEP": "1"})
        self.assertEqual(T.probe_env("/fake", T.MINIMAL_PATH, base)["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")

    def test_run_shell_times_out_and_kills_the_group(self) -> None:
        t0 = time.time()
        res = T.run_shell("sleep 30 & sleep 30", b"", dict(os.environ), 0.5)
        self.assertTrue(res.timed_out)
        self.assertLess(time.time() - t0, 10)

    def test_module_is_python39_syntax(self) -> None:
        src = Path(T.__file__).read_text(encoding="utf-8")
        ast.parse(src, feature_version=(3, 9))

    def test_imports_under_system_python(self) -> None:
        py = "/usr/bin/python3"
        if not os.path.exists(py):
            self.skipTest("no /usr/bin/python3")
        p = subprocess.run([py, "-c", "import fleet_lanes.install_tools as m; print(','.join(sorted(m.PLATFORMS)))"],
                           cwd=str(SCRIPTS_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"claude", p.stdout)


# --------------------------------------------------------------------------- merge (pure)

class MergeTests(unittest.TestCase):
    P = T.make_paths("/h")

    def cmd(self, fmt: str) -> str:
        return T.hook_command(self.P, fmt)

    def merge(self, key: str, doc: dict) -> tuple[dict, str, list]:
        return T.merge_document(doc, T.PLATFORMS[key], self.cmd(T.PLATFORMS[key].fmt))

    def test_is_ours(self) -> None:
        self.assertTrue(T.is_ours("/h/apps/lane-tools/lane-guard-hook --format claude"))
        self.assertTrue(T.is_ours("'/h x/lane-guard-hook' --format cursor"))
        self.assertFalse(T.is_ours("python3 /x/other-hook.py"))
        self.assertFalse(T.is_ours("lane-guard-hooks-extra"))
        self.assertFalse(T.is_ours(None))

    def test_claude_style_into_empty(self) -> None:
        for key in ("claude", "codex", "grok"):
            with self.subTest(key):
                new, action, _ = self.merge(key, {})
                self.assertEqual(action, "added")
                found = T.locate(new, T.PLATFORMS[key])
                self.assertEqual([e.command for e in found], [self.cmd(key)])
                self.assertEqual(new["hooks"]["PreToolUse"][0]["matcher"], "Bash")
                self.assertEqual(new, T.new_document(T.PLATFORMS[key], self.cmd(key)))

    def test_claude_into_populated_appends_a_last_group_and_keeps_everything(self) -> None:
        orig = claude_settings()
        new, action, _ = self.merge("claude", copy.deepcopy(orig))
        self.assertEqual(action, "added")
        groups = new["hooks"]["PreToolUse"]
        self.assertEqual(len(groups), len(orig["hooks"]["PreToolUse"]) + 1)
        self.assertEqual(groups[:-1], orig["hooks"]["PreToolUse"])            # positions unchanged (Codex trust is positional)
        self.assertEqual(groups[-1]["matcher"], "Bash")
        self.assertEqual(groups[-1]["hooks"][0]["command"], self.cmd("claude"))
        trimmed = copy.deepcopy(new)
        trimmed["hooks"]["PreToolUse"].pop()
        self.assertEqual(trimmed, orig)
        self.assertEqual(list(new), list(orig))                                # key order kept
        self.assertTrue(T._is_extension(orig, new))

    def test_claude_already_installed_is_unchanged(self) -> None:
        once, _, _ = self.merge("claude", claude_settings())
        twice, action, notes = self.merge("claude", once)
        self.assertEqual((action, notes), ("unchanged", []))
        self.assertEqual(twice, once)

    def test_claude_stale_command_is_updated_and_custom_fields_survive(self) -> None:
        doc = claude_settings()
        doc["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/old/place/lane-guard-hook --format claude", "timeout": 9}]})
        new, action, notes = self.merge("claude", doc)
        self.assertEqual(action, "updated")
        hook = new["hooks"]["PreToolUse"][-1]["hooks"][0]
        self.assertEqual((hook["command"], hook["timeout"]), (self.cmd("claude"), 9))
        self.assertEqual(len(new["hooks"]["PreToolUse"]), len(doc["hooks"]["PreToolUse"]))
        self.assertTrue(notes)

    def test_claude_wrong_matcher_is_corrected(self) -> None:
        doc = {"hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": self.cmd("claude")}]}]}}
        new, action, _ = self.merge("claude", doc)
        self.assertEqual(action, "updated")
        self.assertEqual(new["hooks"]["PreToolUse"][0]["matcher"], "Bash")

    def test_an_entry_under_another_event_does_not_count(self) -> None:
        doc = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": self.cmd("claude")}]}]}}
        plat = T.PLATFORMS["claude"]
        self.assertEqual(T.locate(doc, plat), [])
        self.assertEqual(T.count_ours(doc), 1)
        new, action, _ = self.merge("claude", doc)
        self.assertEqual(action, "added")
        self.assertEqual(T.count_ours(new), 2)

    def test_duplicates_are_left_alone_with_a_note(self) -> None:
        doc = {"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": self.cmd("claude")}]},
            {"matcher": "Bash", "hooks": [{"type": "command", "command": self.cmd("claude")}]}]}}
        new, action, notes = self.merge("claude", doc)
        self.assertEqual((action, new), ("unchanged", doc))
        self.assertTrue(any("duplicate" in n for n in notes))

    def test_shape_errors_refuse(self) -> None:
        for doc in ({"hooks": []}, {"hooks": "x"}, {"hooks": {"PreToolUse": "x"}}, {"hooks": {"PreToolUse": {}}}):
            with self.subTest(doc), self.assertRaises(T.ShapeError):
                self.merge("claude", copy.deepcopy(doc))
        with self.assertRaises(T.ShapeError):
            self.merge("cursor", {"hooks": {"beforeShellExecution": "x"}})
        with self.assertRaises(T.ShapeError):
            self.merge("antigravity", {"lane-guard": "x"})
        with self.assertRaises(T.ShapeError):
            self.merge("antigravity", {"lane-guard": {"PreToolUse": 1}})

    def test_cursor_merge(self) -> None:
        orig = cursor_hooks()
        new, action, _ = self.merge("cursor", copy.deepcopy(orig))
        self.assertEqual(action, "added")
        entry = new["hooks"]["beforeShellExecution"][0]
        self.assertEqual(entry, {"command": self.cmd("cursor"), "timeout": 5, "failClosed": False})
        self.assertEqual(new["version"], 1)
        self.assertTrue(T._is_extension(orig, new))
        again, action, _ = self.merge("cursor", new)
        self.assertEqual((action, again), ("unchanged", new))
        empty, action, _ = self.merge("cursor", {})
        self.assertEqual((action, empty["version"]), ("added", 1))
        stale = copy.deepcopy(new)
        stale["hooks"]["beforeShellExecution"][0]["command"] = "/old/lane-guard-hook --format claude"
        fixed, action, _ = self.merge("cursor", stale)
        self.assertEqual((action, fixed["hooks"]["beforeShellExecution"][0]["command"]), ("updated", self.cmd("cursor")))

    def test_antigravity_merge(self) -> None:
        orig = gemini_hooks()
        new, action, _ = self.merge("antigravity", copy.deepcopy(orig))
        self.assertEqual(action, "added")
        self.assertEqual(new["agent-hook-otlp"], orig["agent-hook-otlp"])
        grp = new["lane-guard"]
        self.assertEqual(grp["enabled"], True)
        self.assertEqual(grp["PreToolUse"][0]["matcher"], "run_command")
        self.assertEqual(grp["PreToolUse"][0]["command"], self.cmd("antigravity"))
        again, action, _ = self.merge("antigravity", new)
        self.assertEqual((action, again), ("unchanged", new))
        # an existing lane-guard group without our entry gets it appended
        partial = {"lane-guard": {"PreToolUse": [{"command": "/x/other", "type": "command"}], "enabled": False}}
        got, action, _ = self.merge("antigravity", partial)
        self.assertEqual(action, "added")
        self.assertEqual(len(got["lane-guard"]["PreToolUse"]), 2)
        self.assertTrue(got["lane-guard"]["enabled"])
        # a disabled group that holds our entry is re-enabled
        off = copy.deepcopy(new)
        off["lane-guard"]["enabled"] = False
        self.assertTrue(T.locate(off, T.PLATFORMS["antigravity"])[0].problems)
        fixed, action, _ = self.merge("antigravity", off)
        self.assertEqual((action, fixed["lane-guard"]["enabled"]), ("updated", True))

    def test_subtree_and_redact(self) -> None:
        doc = claude_settings()
        label, sub = T.subtree(doc, T.PLATFORMS["claude"])
        self.assertEqual(label, "hooks.PreToolUse")
        self.assertEqual(sub, doc["hooks"]["PreToolUse"])
        text = "\n".join(T._json_lines(sub))
        self.assertNotIn("dummy-secret-value", text)                       # the env block is not in the subtree
        red = T.redact(text)
        self.assertNotIn("Zq9fakeSecretTokenValue77", red)
        self.assertIn("<redacted>", red)
        self.assertEqual(T.redact('  "EXAMPLE_API_TOKEN": "hunter2hunter2"'), '  "EXAMPLE_API_TOKEN": "<redacted>"')
        self.assertEqual(T.redact("git status"), "git status")


# --------------------------------------------------------------------------- reading and writing configs

class LoadConfigTests(World):
    def write(self, text: str, name: str = "c.json") -> str:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_round_trip_formats(self) -> None:
        doc = {"a": [1, {"b": "é"}], "c": None}
        for label, text in (("indent2", json.dumps(doc, indent=2) + "\n"),
                            ("indent4-no-newline", json.dumps(doc, indent=4)),
                            ("tab", json.dumps(doc, indent="\t") + "\n"),
                            ("utf8-literal", json.dumps(doc, indent=2, ensure_ascii=False) + "\n")):
            with self.subTest(label):
                cfg = T.load_config(self.write(text))
                self.assertTrue(cfg.exact, cfg.note)
                self.assertEqual(T.dump_config(cfg.doc, cfg), text)

    def test_compact_json_is_flagged_not_refused(self) -> None:
        cfg = T.load_config(self.write('{"a":1}'))
        self.assertFalse(cfg.exact)
        self.assertIn("round-trip", cfg.note)

    def test_empty_and_missing(self) -> None:
        cfg = T.load_config(self.write(" \n"))
        self.assertEqual((cfg.doc, cfg.exists), ({}, True))
        gone = T.load_config(str(self.tmp / "nope.json"))
        self.assertEqual((gone.doc, gone.exists, gone.mode), ({}, False, None))

    def test_refusals(self) -> None:
        for label, text in (("garbage", "{not json"), ("list", "[1, 2]"), ("scalar", "3"), ("trailing", '{"a":1} x'),
                            ("bom", "\ufeff{}")):
            with self.subTest(label), self.assertRaises(T.Refused):
                T.load_config(self.write(text))
        with self.assertRaises(T.Refused):
            T.load_config(str(self.tmp))


class ApplyPlatformTests(World):
    def apply(self, key: str, **kw) -> T.Result:
        return T.apply_platform(T.PLATFORMS[key], self.paths, now=lambda: STAMP, shim_proven=True, **kw)

    def test_merge_into_populated_backs_up_and_keeps_mode(self) -> None:
        self.seed_platforms()
        cfg = self.config("claude")
        original = cfg.read_bytes()
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
        res = self.apply("claude")
        self.assertEqual(res.status, "added", res)
        baks = self.backups(cfg.parent)
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_bytes(), original)
        self.assertEqual(stat.S_IMODE(baks[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
        self.assertEqual(res.backup, str(baks[0]))
        doc = self.load("claude")
        self.assertEqual(len(T.locate(doc, T.PLATFORMS["claude"])), 1)
        self.assertEqual(doc["env"], claude_settings()["env"])
        self.assertTrue(cfg.read_text().endswith("\n"))
        self.assertEqual(sorted(p.name for p in cfg.parent.iterdir() if p.name.startswith(".")), [])   # no temp files

    def test_second_apply_changes_nothing_and_makes_no_backup(self) -> None:
        self.seed_platforms()
        self.apply("codex")
        after_first = snapshot(self.home)
        res = self.apply("codex")
        self.assertEqual(res.status, "unchanged", res)
        self.assertEqual(snapshot(self.home), after_first)

    def test_merge_into_empty_file_and_empty_object(self) -> None:
        for content in ("", "{}\n"):
            with self.subTest(repr(content)):
                p = self.home / ".codex" / "hooks.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
                res = self.apply("codex")
                self.assertEqual(res.status, "added", res)
                self.assertEqual(len(T.locate(self.load("codex"), T.PLATFORMS["codex"])), 1)
                p.unlink()
                for b in self.backups(p.parent):
                    b.unlink()

    def test_backup_names_never_collide(self) -> None:
        self.seed_platforms()
        self.apply("cursor")
        doc = self.load("cursor")
        doc["extra"] = 1
        write_json(self.config("cursor"), doc)
        doc["hooks"]["beforeShellExecution"][0]["command"] = "/old/lane-guard-hook --format cursor"
        write_json(self.config("cursor"), doc)
        res = self.apply("cursor")                      # same fixed clock as the first apply
        self.assertEqual(res.status, "updated", res)
        names = [p.name for p in self.backups(self.config("cursor").parent)]
        self.assertEqual(len(names), 2, names)
        self.assertEqual(len(set(names)), 2)

    def test_invalid_json_is_refused_and_untouched(self) -> None:
        self.seed_platforms()
        for label, text in (("garbage", "{oops"), ("list", "[]"), ("hooks-wrong-type", '{"hooks": []}')):
            with self.subTest(label):
                p = self.config("claude")
                p.write_text(text, encoding="utf-8")
                before = snapshot(self.home)
                res = self.apply("claude")
                self.assertEqual(res.status, "refused", res)
                self.assertEqual(snapshot(self.home), before)
                self.assertEqual(self.backups(p.parent), [])

    def test_platform_not_installed_is_skipped_without_creating_dirs(self) -> None:
        before = snapshot(self.tmp)
        res = self.apply("cursor")
        self.assertEqual(res.status, "skipped", res)
        self.assertEqual(snapshot(self.tmp), before)

    def test_muse_apply_without_the_tools_is_refused_and_writes_nothing(self) -> None:
        self.seed(".config/muse/settings.json")
        before = snapshot(self.home)
        res = self.apply("muse")
        self.assertEqual(res.status, "refused", res)
        self.assertIn("apply tools", res.detail)
        self.assertEqual(snapshot(self.home), before)

    def test_grok_gets_its_own_file_and_leaves_the_imported_one(self) -> None:
        self.seed(".grok/hooks/imported-from-claude.json")
        imported = (self.home / ".grok/hooks/imported-from-claude.json").read_bytes()
        res = self.apply("grok")
        self.assertEqual(res.status, "added", res)
        self.assertEqual(self.config("grok").name, "lane-guard.json")
        self.assertEqual((self.home / ".grok/hooks/imported-from-claude.json").read_bytes(), imported)
        self.assertIsNone(res.backup)
        self.assertEqual(self.load("grok")["hooks"]["PreToolUse"][0]["hooks"][0]["command"], T.hook_command(self.paths, "grok"))

    def test_grok_creates_the_hooks_dir_when_grok_exists(self) -> None:
        (self.home / ".grok").mkdir()
        self.assertEqual(self.apply("grok").status, "added")
        self.assertTrue(self.config("grok").is_file())

    def test_symlinked_config_is_written_through(self) -> None:
        self.seed_platforms()
        real = self.tmp / "dotfiles" / "settings.json"
        real.parent.mkdir()
        shutil.move(str(self.config("claude")), str(real))
        os.symlink(real, self.config("claude"))
        self.assertEqual(self.apply("claude").status, "refused")                # outside --home: needs --follow-symlinks
        res = self.apply("claude", follow_symlinks=True)
        self.assertEqual(res.status, "added", res)
        self.assertTrue(os.path.islink(self.config("claude")))
        self.assertEqual(len(T.locate(json.loads(real.read_text()), T.PLATFORMS["claude"])), 1)
        self.assertEqual(len(self.backups(real.parent)), 1)
        self.assertEqual(self.backups(self.config("claude").parent), [])

    def test_atomic_write_failure_leaves_the_original_intact(self) -> None:
        self.seed_platforms()
        cfg = self.config("claude")
        original = cfg.read_bytes()
        names_before = {p.name for p in cfg.parent.iterdir()}
        with mock.patch.object(T.os, "replace", side_effect=OSError("disk full")):
            res = self.apply("claude")
        self.assertEqual(res.status, "failed", res)
        self.assertIn("disk full", res.detail)
        self.assertEqual(cfg.read_bytes(), original)
        new_names = {p.name for p in cfg.parent.iterdir()} - names_before
        self.assertEqual(len(new_names), 1, new_names)                  # only the backup, no temp file
        self.assertIn(".bak-lane-guard-", next(iter(new_names)))

    def test_a_concurrent_change_aborts_the_write(self) -> None:
        self.seed_platforms()
        cfg = self.config("claude")
        original = cfg.read_bytes()
        with mock.patch.object(T, "_reread_matches", return_value=False):
            res = self.apply("claude")
        self.assertEqual(res.status, "failed", res)
        self.assertIn("changed while", res.detail)
        self.assertEqual(cfg.read_bytes(), original)
        self.assertEqual(self.backups(cfg.parent), [])

    def test_a_bad_write_is_detected_and_the_original_restored(self) -> None:
        self.seed_platforms()
        cfg = self.config("claude")
        original = cfg.read_bytes()
        real_write = T.atomic_write
        calls = []

        def corrupting(path: str, data: bytes, mode: int) -> None:
            calls.append(path)
            real_write(path, b"{broken" if len(calls) == 1 else data, mode)

        with mock.patch.object(T, "atomic_write", side_effect=corrupting):
            res = self.apply("claude")
        self.assertEqual(res.status, "failed", res)
        self.assertIn("restored", res.detail)
        self.assertEqual(cfg.read_bytes(), original)

    def test_refuses_to_wire_in_a_shim_that_fails_its_probes(self) -> None:
        self.seed_platforms()
        before = snapshot(self.home)
        res = T.apply_platform(T.PLATFORMS["claude"], self.paths, now=lambda: STAMP, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(res.status, "refused", res)                       # no shim installed at all
        self.assertIn("apply tools", res.detail)
        self.assertEqual(snapshot(self.home), before)


# --------------------------------------------------------------------------- tools

class ToolsTests(World):
    def stable(self) -> Path:
        return Path(self.paths.stable)

    def test_layout_version_and_shims(self) -> None:
        res = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(res.status, "installed", res)
        s = self.stable()
        names = sorted(p.name for p in (s / "fleet_lanes").iterdir())
        self.assertEqual(names, sorted(list(T.REQUIRED_MODULES) + ["lane.py", T.SECRET_GUARD_MODULE]))        # no tests, no __pycache__
        self.assertFalse((s / "fleet_lanes" / "tests").exists())
        self.assertTrue((s / "fleet-apps.json").is_file())
        ver = T.parse_version((s / "VERSION").read_text())
        self.assertEqual((ver["sha"], ver["package"]), (SHA, "0.1.0"))
        files = T.read_tree(str(s))
        files.pop("VERSION")
        self.assertEqual(ver["tree"], T.tree_digest(files))
        self.assertEqual(int(ver["files"]), len(files))
        hook = s / "lane-guard-hook"
        self.assertTrue(os.access(hook, os.X_OK))
        text = hook.read_text()
        self.assertEqual(text, T.render_hook_shim())
        self.assertNotIn(str(s), text)                                                # relocatable: derives its own dir
        self.assertIn("python3 -I", text)
        lane = Path(self.paths.lane_shim)
        self.assertTrue(os.access(lane, os.X_OK))
        self.assertEqual(lane.read_text(), T.render_lane_shim(str(s)))
        self.assertIn("fleet_lanes.lane", lane.read_text())
        self.assertIn("FLEET_APPS_JSON", text)
        self.assertEqual(sorted(p.name for p in s.parent.iterdir()), ["lane", "lane-tools"])   # no stage dir left over

    def test_stable_copy_contains_no_pycache_even_after_the_probes_ran(self) -> None:
        T.apply_tools(self.source, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertFalse(any("__pycache__" in str(p) for p in self.stable().rglob("*")))

    def test_second_apply_is_unchanged(self) -> None:
        self.install(())
        marker = self.stable() / "VERSION"
        mtime = marker.stat().st_mtime_ns
        res = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False)
        self.assertEqual(res.status, "unchanged", res)
        self.assertEqual(marker.stat().st_mtime_ns, mtime)

    def test_new_source_refreshes_and_removes_stale_files(self) -> None:
        self.install(())
        extra = Path(self.source.scripts_dir) / "fleet_lanes" / "extra.py"
        extra.write_text("X = 1\n")
        T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False)
        self.assertTrue((self.stable() / "fleet_lanes" / "extra.py").exists())
        extra.unlink()
        newer = T.Source(self.source.scripts_dir, self.source.registry, SHA2)
        res = T.apply_tools(newer, self.paths, timeout=TIMEOUT, probes=False)
        self.assertEqual(res.status, "installed", res)
        self.assertFalse((self.stable() / "fleet_lanes" / "extra.py").exists())
        self.assertEqual(T.parse_version((self.stable() / "VERSION").read_text())["sha"], SHA2)

    def test_no_lane_module_means_no_lane_shim(self) -> None:
        src = self.make_source("nolane", with_lane=False)
        res = T.apply_tools(src, self.paths, timeout=TIMEOUT, probes=False)
        self.assertEqual(res.status, "installed", res)
        self.assertFalse(os.path.lexists(self.paths.lane_shim))
        checks = T.verify_tools(self.paths, timeout=TIMEOUT)
        self.assertEqual({c.name: c.status for c in checks}["lane"], "WARN")
        self.assertNotIn("FAIL", self.statuses(checks))
        # a later install that has lane.py adds the shim; one that loses it removes the stale shim again
        T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False)
        self.assertTrue(os.path.isfile(self.paths.lane_shim))
        res = T.apply_tools(src, self.paths, timeout=TIMEOUT, probes=False)
        self.assertEqual(res.status, "installed", res)
        self.assertFalse(os.path.lexists(self.paths.lane_shim))

    def test_refuses_a_foreign_stable_dir(self) -> None:
        stable = self.stable()
        stable.mkdir(parents=True)
        (stable / "mine.txt").write_text("not yours")
        res = T.apply_tools(self.source, self.paths, probes=False)
        self.assertEqual(res.status, "refused", res)
        self.assertEqual([p.name for p in stable.iterdir()], ["mine.txt"])

    def test_a_foreign_lane_shim_is_left_alone_and_does_not_block_the_guard(self) -> None:
        lane = Path(self.paths.lane_shim)
        for label, make in (("file", lambda: lane.write_text("#!/bin/sh\necho mine\n")),
                            ("symlink to a checkout shim", lambda: os.symlink(self.tmp / "checkout" / "bin" / "lane", lane)),
                            ("directory", lambda: lane.mkdir())):
            with self.subTest(label):
                if lane.is_symlink() or lane.is_file():
                    lane.unlink()
                elif lane.is_dir():
                    lane.rmdir()
                shutil.rmtree(self.stable(), ignore_errors=True)
                lane.parent.mkdir(parents=True, exist_ok=True)
                make()
                mine = os.readlink(lane) if lane.is_symlink() else (lane.read_text() if lane.is_file() else None)
                res = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False)
                self.assertEqual(res.status, "installed", res)
                self.assertIn("lane shim NOT written", res.detail)
                self.assertTrue(os.access(self.paths.hook_shim, os.X_OK))
                self.assertEqual(os.readlink(lane) if lane.is_symlink() else (lane.read_text() if lane.is_file() else None), mine)
                checks = T.verify_tools(self.paths, timeout=TIMEOUT)
                self.assertEqual({c.name: c.status for c in checks}["lane"], "WARN")
                self.assertNotIn("FAIL", self.statuses(checks))
                self.assertIn("NOT the stable copy", next(c for c in checks if c.name == "lane").detail)
                self.assertEqual(T.apply_tools(self.source, self.paths, probes=False).status, "unchanged")
                tp = T.plan_tools(self.source, self.paths)
                self.assertEqual((tp.lane_shim_state, tp.problems), ("foreign", []))
        rc, out, _ = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, "tools")
        self.assertEqual(rc, 0, out)
        self.assertIn("LEFT ALONE", out)

    def test_refuses_a_symlinked_stable_dir_and_a_source_that_is_the_stable_dir(self) -> None:
        target = self.tmp / "elsewhere"
        target.mkdir()
        (self.home / "apps").mkdir()
        os.symlink(target, self.stable())
        self.assertEqual(T.apply_tools(self.source, self.paths, probes=False).status, "refused")
        os.unlink(self.stable())
        self.install(())
        inside = T.Source(self.paths.stable, self.paths.registry, SHA)
        res = T.apply_tools(inside, self.paths, probes=False)
        self.assertEqual(res.status, "refused", res)
        self.assertIn("stable dir itself", res.detail)

    def test_refuses_an_incomplete_source(self) -> None:
        src = self.make_source("bad")
        (Path(src.scripts_dir) / "fleet_lanes" / "guard.py").unlink()
        res = T.apply_tools(src, self.paths, probes=False)
        self.assertEqual(res.status, "refused", res)
        self.assertIn("guard.py", res.detail)
        for name, text in (("bad2", "{not json"), ("bad3", '{"apps": []}'), ("bad4", "[1]")):
            with self.subTest(name):
                src = self.make_source(name)
                Path(src.registry).write_text(text)
                self.assertEqual(T.apply_tools(src, self.paths, probes=False).status, "refused")
        self.assertFalse(os.path.exists(self.paths.apps_dir))                         # refused before anything was created

    def test_a_staged_copy_that_fails_its_probes_is_never_swapped_in(self) -> None:
        self.install(())
        before = snapshot(self.tmp)
        bad = self.make_source("badguard", guard=BROKEN_GUARD, sha=SHA2)
        res = T.apply_tools(bad, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(res.status, "refused", res)
        self.assertIn("staged copy failed", res.detail)
        after = snapshot(self.tmp)
        self.assertEqual({k: v for k, v in after.items() if k.startswith("home")}, {k: v for k, v in before.items() if k.startswith("home")})
        self.assertEqual(T.parse_version((self.stable() / "VERSION").read_text())["sha"], SHA)

    def test_swap_failure_restores_the_installed_copy(self) -> None:
        self.install(())
        before = snapshot(self.home)
        newer = T.Source(self.source.scripts_dir, self.source.registry, SHA2)
        real_rename, calls = os.rename, []

        def flaky(a: str, b: str) -> None:
            calls.append((a, b))
            if len(calls) == 2:                       # the stage-to-final rename
                raise OSError("rename failed")
            real_rename(a, b)

        with mock.patch.object(T.os, "rename", side_effect=flaky):
            res = T.apply_tools(newer, self.paths, probes=False)
        self.assertEqual(res.status, "failed", res)
        self.assertEqual(snapshot(self.home), before)                                  # also proves no stage or .old dir remains

    def test_plan_tools_reports_state(self) -> None:
        plan = T.plan_tools(self.source, self.paths)
        self.assertEqual(plan.state, "missing")
        self.assertTrue(all(f.status == "new" for f in plan.files))
        self.install(())
        plan = T.plan_tools(self.source, self.paths)
        self.assertEqual(plan.state, "current")
        (self.stable() / "fleet_lanes" / "stray.py").write_text("x")
        plan = T.plan_tools(self.source, self.paths)
        self.assertEqual((plan.state, [f.rel for f in plan.files if f.status == "stale"]), ("stale", ["fleet_lanes/stray.py"]))


# --------------------------------------------------------------------------- plan is read-only

class PlanTests(World):
    def plan_args(self, *extra: str) -> list[str]:
        return ["plan", "--home", str(self.home), "--source", self.source.scripts_dir,
                "--registry", self.source.registry, "--sha", SHA, *extra]

    def test_plan_never_writes_anything(self) -> None:
        self.seed()
        before = snapshot(self.tmp)
        rc, out, err = run_main(*self.plan_args())
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertFalse((self.home / "apps").exists())
        # and again with the tools installed and a platform already wired
        self.install(("claude",))
        before = snapshot(self.tmp)
        rc, out, _ = run_main(*self.plan_args())
        self.assertEqual(rc, 0, out)
        self.assertEqual(snapshot(self.tmp), before)

    def test_plan_output_shows_commands_diffs_shims_and_unverified(self) -> None:
        self.seed()
        rc, out, _ = run_main(*self.plan_args())
        self.assertEqual(rc, 0)
        for needle in ("== tools", "VERSION would read", f"sha={SHA}", "python3 -I -c", "+++ ", "--- ", "@@ ",
                       f"{self.paths.stable}/lane-guard-hook --format claude", "--format codex", "--format grok",
                       "--format antigravity", "--format cursor", "== muse", "UNVERIFIED", "failClosed",
                       "[plugin bundle new]", "OWNER ACTIONS", "lane-guard.json", "/hooks in Codex"):
            self.assertIn(needle, out)
        self.assertIn("shim " + self.paths.lane_shim + " [new]", out)

    def test_plan_never_prints_the_env_block_or_secret_values(self) -> None:
        self.seed()
        rc, out, _ = run_main(*self.plan_args("claude"))
        self.assertEqual(rc, 0)
        self.assertNotIn("dummy-secret-value", out)
        self.assertNotIn("EXAMPLE_API_TOKEN", out)
        self.assertNotIn("Zq9fakeSecretTokenValue77", out)
        self.assertNotIn("Authorization", out)                                    # other hooks are counted, not echoed
        self.assertIn("other hooks under hooks.PreToolUse are left untouched and not shown", out)

    def test_plan_exit_codes(self) -> None:
        self.seed()
        self.assertEqual(run_main(*self.plan_args())[0], 0)                       # muse is listed, not failed
        self.assertEqual(run_main(*self.plan_args("muse"))[0], 0)                 # informational: it writes nothing itself
        self.config("claude").write_text("{nope")
        rc, out, _ = run_main(*self.plan_args("claude"))
        self.assertEqual(rc, 1)
        self.assertIn("not valid JSON", out)
        self.assertEqual(run_main(*self.plan_args("cursor"))[0], 0)
        shutil.rmtree(self.home / ".cursor")
        self.assertEqual(run_main(*self.plan_args("cursor"))[0], 1)               # named but not installed
        self.assertEqual(run_main(*self.plan_args("all"))[0], 1)                  # claude is still invalid JSON

    def test_plan_of_an_installed_platform_says_unchanged(self) -> None:
        self.seed()
        self.install(("claude",))
        rc, out, _ = run_main(*self.plan_args("claude"))
        self.assertEqual(rc, 0)
        self.assertIn("[unchanged]", out)
        self.assertNotIn("@@ ", out)


# --------------------------------------------------------------------------- verify: the proof

class VerifyTests(World):
    def setUp(self) -> None:
        super().setUp()
        self.seed_platforms()
        self.install()

    def test_everything_passes_after_apply_including_the_minimal_path(self) -> None:
        for key in T.SUPPORTED_KEYS:
            with self.subTest(key):
                checks = self.verify(key, minimal_path=MINIMAL)
                self.assertAllPass(checks)
                names = [c.name for c in checks]
                for n in ("deny/login-PATH", "allow/login-PATH") + (("deny/minimal-PATH", "allow/minimal-PATH") if MINIMAL else ()):
                    self.assertIn(n, names)
                deny = next(c for c in checks if c.name == "deny/login-PATH")
                self.assertIn("permission" if key == "cursor" else ("decision" if key in ("grok", "antigravity") else "hookSpecificOutput"),
                              deny.detail)                                            # the observed bytes are printed
                self.assertIn("exit=0", deny.detail)
        self.assertAllPass(T.verify_tools(self.paths, timeout=TIMEOUT))

    def test_cli_verify_prints_pass_lines_and_exits_zero(self) -> None:
        rc, out, err = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("PASS  claude", out)
        self.assertNotIn("FAIL", out.replace("0 FAIL", ""))
        self.assertIn("-> healthy", out)
        self.assertIn("(see WARN)", out)                                              # Codex trust is pending in a fake home

    def test_fails_when_the_stable_dir_is_missing(self) -> None:
        os.rename(self.paths.stable, self.paths.stable + ".gone")
        checks = self.verify("claude")
        self.assertFails(checks, "exit 127")
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "does not exist")
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "claude", "tools")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT HEALTHY", out)

    def test_fails_when_the_guard_module_is_gone_or_broken(self) -> None:
        guard = Path(self.paths.package_dir) / "guard.py"
        off = guard.with_suffix(".off")
        original = guard.read_bytes()

        def restore() -> None:
            if off.exists():
                off.unlink()
            guard.write_bytes(original)

        for label, action in (("renamed", lambda: guard.rename(off)),
                              ("syntax error", lambda: guard.write_text("def evaluate(:\n")),
                              ("raises", lambda: guard.write_text(BROKEN_GUARD))):
            with self.subTest(label):
                restore()
                action()
                checks = self.verify("claude")
                self.assertFails(checks, "no output")
                deny = next(c for c in checks if c.name == "deny/login-PATH")
                self.assertIn("exit=0", deny.detail)                                  # fails open: exit 0, nothing printed
                self.assertEqual(self.statuses([c for c in checks if c.name.startswith("allow")]), {"PASS"})
        restore()
        self.assertAllPass(self.verify("claude"))

    def test_the_repo_copy_in_the_working_directory_cannot_mask_a_broken_install(self) -> None:
        guard = Path(self.paths.package_dir) / "guard.py"
        guard.rename(guard.with_suffix(".off"))
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(SCRIPTS_DIR)                                                         # holds the real, working fleet_lanes
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(SCRIPTS_DIR)}):
            self.assertFails(self.verify("claude"), "no output")
            # and the shim itself, run by hand from there, does not reach the repo copy either
            payload = json.dumps(T.probe_payload("claude", T.DENY_COMMAND, str(self.home))).encode()
            p = subprocess.run([self.paths.hook_shim, "--format", "claude"], input=payload, cwd=str(SCRIPTS_DIR),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
        self.assertEqual((p.returncode, p.stdout), (0, b""), p.stderr)

    def test_fails_when_the_shim_is_not_executable(self) -> None:
        os.chmod(self.paths.hook_shim, 0o644)
        checks = self.verify("cursor")
        self.assertFails(checks, "exit 126")
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "not executable")

    def test_fails_when_the_config_command_was_edited(self) -> None:
        # a path that does not exist
        doc = self.load("claude")
        doc["hooks"]["PreToolUse"][-1]["hooks"][0]["command"] = "/nonexistent/dir/lane-guard-hook --format claude"
        write_json(self.config("claude"), doc)
        self.assertFails(self.verify("claude"), "exit 127")
        # the wrong format for the platform: Cursor handed the Claude shape
        doc = self.load("cursor")
        doc["hooks"]["beforeShellExecution"][0]["command"] = T.hook_command(self.paths, "claude")
        write_json(self.config("cursor"), doc)
        self.assertFails(self.verify("cursor"), "expected")
        # the hook name edited away
        doc = self.load("codex")
        doc["hooks"]["PreToolUse"][-1]["hooks"][0]["command"] = "/bin/true"
        write_json(self.config("codex"), doc)
        self.assertFails(self.verify("codex"), "no lane-guard-hook hook")

    def test_fails_when_the_entry_is_not_where_the_platform_gates_on_it(self) -> None:
        doc = self.load("claude")
        group = doc["hooks"]["PreToolUse"].pop()
        doc["hooks"]["PostToolUse"].append(group)                                     # runs, prints a deny, protects nothing
        write_json(self.config("claude"), doc)
        checks = self.verify("claude")
        self.assertEqual(len(checks), 1)
        self.assertFails(checks, "elsewhere")
        doc = self.load("codex")
        doc["hooks"]["PreToolUse"][-1]["matcher"] = "Edit"
        write_json(self.config("codex"), doc)
        self.assertFails(self.verify("codex"), "never matches Bash")
        doc = self.load("antigravity")
        doc["lane-guard"]["enabled"] = False
        write_json(self.config("antigravity"), doc)
        self.assertFails(self.verify("antigravity"), "disabled")
        doc = self.load("cursor")
        doc["hooks"]["afterFileEdit"] = doc["hooks"].pop("beforeShellExecution")
        write_json(self.config("cursor"), doc)
        self.assertFails(self.verify("cursor"), "elsewhere")

    def test_fails_on_missing_config_unparsable_config_and_unsupported_platform(self) -> None:
        self.config("grok").unlink()
        self.assertFails(self.verify("grok"), "no hook installed")
        self.config("codex").write_text("{broken")
        self.assertFails(self.verify("codex"), "not valid JSON")
        self.assertAllPass([c for c in self.verify("muse") if c.status != "SKIP"])      # muse is a plugin now, see the Muse tests

    def test_fails_on_a_hook_that_hangs_exits_nonzero_or_prints_nothing(self) -> None:
        shim = Path(self.paths.hook_shim)
        for label, body, needle in (("hang", "#!/bin/sh\nexec sleep 30\n", "timed out"),
                                    ("nonzero", "#!/bin/sh\ncat >/dev/null\nexit 3\n", "exit 3"),
                                    ("silent allow", "#!/bin/sh\ncat >/dev/null\nexit 0\n", "no output"),
                                    ("noise on allow", '#!/bin/sh\ncat >/dev/null\necho \'{"decision": "deny"}\'\n', "allow probe")):
            with self.subTest(label):
                shim.write_text(body)
                os.chmod(shim, 0o755)
                self.assertFails(self.verify("grok", timeout=2.0), needle)

    def test_a_platform_that_is_not_installed_is_a_skip_unless_named(self) -> None:
        shutil.rmtree(self.home / ".cursor")
        self.assertEqual({c.status for c in self.verify("cursor")}, {"SKIP"})
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path")
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP", out)
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "cursor")
        self.assertEqual(rc, 1, out)

    def test_codex_trust_record(self) -> None:
        def trust():
            return next(c for c in self.verify("codex") if c.name == "trust")

        self.assertEqual(trust().status, "WARN")                                      # no config.toml at all
        entry = T.locate(self.load("codex"), T.PLATFORMS["codex"])[0]
        key = f"{self.config('codex')}:pre_tool_use:{entry.group_index}:{entry.hook_index}"
        toml = self.home / ".codex" / "config.toml"
        toml.write_text('[features]\nhooks = true\n')
        self.assertEqual(trust().status, "WARN")
        self.assertIn("NOT TRUSTED YET", trust().detail)
        toml.write_text('[features]\nhooks = true\n\n[hooks.state."%s"]\ntrusted_hash = "sha256:%s"\n' % (key, "ab" * 32))
        self.assertEqual(trust().status, "WARN")                                      # a record is found, but its hash is not recomputed
        self.assertIn("UNCHECKED", trust().detail)
        self.assertNotIn("NOT TRUSTED YET", trust().detail)
        toml.write_text('[hooks.state."%s"]\nenabled = true\n\n[other]\ntrusted_hash = "sha256:%s"\n' % (key, "ab" * 32))
        self.assertIn("NOT TRUSTED YET", trust().detail)                              # the hash belongs to another table
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "codex", "--strict")
        self.assertEqual(rc, 1, out)
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "codex")
        self.assertEqual(rc, 0, out)

    def test_tools_verify_catches_tampering(self) -> None:
        stable = Path(self.paths.stable)
        self.assertAllPass(T.verify_tools(self.paths, timeout=TIMEOUT))
        with open(stable / "fleet_lanes" / "layout.py", "a") as fh:
            fh.write("\n# edited\n")
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "VERSION's tree digest")
        self.install()                                                                # re-apply heals it
        self.assertAllPass(T.verify_tools(self.paths, timeout=TIMEOUT))
        (stable / "fleet_lanes" / "added.py").write_text("x = 1\n")
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "digest")
        (stable / "fleet_lanes" / "added.py").unlink()
        (stable / "fleet-apps.json").write_text("{nope")
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "fleet-apps.json")
        self.install()
        Path(self.paths.lane_shim).unlink()
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "shim lane")
        self.install()
        os.chmod(self.paths.lane_shim, 0o644)
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "not executable")
        self.install()
        (stable / "VERSION").unlink()
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "VERSION")

    def test_lane_shim_runs_the_installed_module(self) -> None:
        p = subprocess.run([self.paths.lane_shim, "ls", "--x"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           cwd=str(SCRIPTS_DIR), timeout=TIMEOUT)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, b"lane-stub-ok ls --x"), p.stderr)


# --------------------------------------------------------------------------- command line

class CliTests(World):
    def test_usage_errors_exit_64(self) -> None:
        h = ["--home", str(self.home)]
        for argv in ([], ["apply"] + h, ["apply", "nonsense"] + h, ["plan", "--bogus"], ["verify", "everything"] + h,
                     ["nonsense"]):
            with self.subTest(argv):
                rc, out, err = run_main(*argv)
                self.assertEqual(rc, T.EXIT_USAGE, (out, err))
        with contextlib.redirect_stdout(io.StringIO()):
            rc, _out, _err = run_main("--help")
        self.assertEqual(rc, 0)

    def test_verify_muse_before_the_tools_exits_1_and_writes_nothing(self) -> None:
        self.seed()
        before = snapshot(self.tmp)
        rc, out, _ = run_main("verify", "--home", str(self.home), "muse")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT HEALTHY", out)
        self.assertEqual(snapshot(self.tmp), before)

    def test_apply_all_then_verify_then_again(self) -> None:
        self.seed_platforms()
        base = ["--home", str(self.home), "--source", self.source.scripts_dir, "--registry", self.source.registry,
                "--sha", SHA, "--timeout", str(TIMEOUT), *MINIMAL_ARGS]
        rc, out, err = run_main("apply", *base, "all")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("INSTALLED  tools", out)
        for key in T.SUPPORTED_KEYS:
            self.assertIn(f"ADDED      {key}", out)
        self.assertIn("NEXT       codex", out)
        self.assertIn("NEXT       claude", out)
        self.assertIn("verifying what was just applied", out)
        if MINIMAL:
            self.assertIn("minimal-PATH", out)
        self.assertIn("-> healthy", out)
        self.assertEqual(len(self.backups(self.home / ".claude")), 1)
        after = snapshot(self.home)
        rc, out, _ = run_main("apply", *base, "--no-minimal-path", "--no-verify", "all")
        self.assertEqual(rc, 0, out)
        self.assertIn("UNCHANGED  tools", out)
        for key in T.SUPPORTED_KEYS:
            self.assertIn(f"UNCHANGED  {key}", out)
        self.assertEqual(snapshot(self.home), after)

    def test_apply_a_skipped_platform_is_a_failure_only_when_named(self) -> None:
        self.seed(".claude/settings.json")
        base = ["--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, "--timeout", str(TIMEOUT),
                "--no-minimal-path", "--no-verify"]
        rc, out, _ = run_main("apply", *base, "all")
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIPPED    cursor", out)
        rc, out, _ = run_main("apply", *base, "cursor")
        self.assertEqual(rc, 1, out)

    def test_apply_refuses_a_broken_shim_for_a_platform(self) -> None:
        self.seed_platforms()
        self.install(())
        Path(self.paths.hook_shim).write_text("#!/bin/sh\nexit 0\n")
        rc, out, _ = run_main("apply", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "claude")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.backups(self.config("claude").parent), [])
        self.assertEqual(T.locate(self.load("claude"), T.PLATFORMS["claude"]), [])

    def test_self_test_passes_and_proves_a_renamed_guard_fails(self) -> None:
        lines: list[str] = []
        rc = T.self_test(lines.append, timeout=TIMEOUT, minimal_path=False, source=self.source)
        text = "\n".join(lines)
        self.assertEqual(rc, 0, text)
        self.assertIn("self-test PASS", text)
        self.assertIn("with guard.py renamed", text)
        self.assertIn("FAIL  claude", text)                                            # the failures it provoked are printed
        self.assertNotIn("self-test FAIL", text)

    def test_self_test_reports_a_verify_that_cannot_fail(self) -> None:
        # if verify never failed, the self-test must say so instead of passing
        lines: list[str] = []

        def always_pass(plat, paths, **kw):
            return [T.Check(plat.key, "deny/login-PATH", "PASS", "fake")]

        with mock.patch.object(T, "verify_platform", side_effect=always_pass):
            rc = T.self_test(lines.append, timeout=TIMEOUT, minimal_path=False, source=self.source)
        self.assertEqual(rc, 1, "\n".join(lines))
        self.assertIn("did NOT fail", "\n".join(lines))


# --------------------------------------------------------------------------- review round 2: confirmed findings
#
# Each class below reproduces one confirmed review finding.  The values that look like credentials are
# fakes assembled from pieces, so no literal in this file looks like a live key.

class StableDirOwnershipTests(World):
    """A --stable-dir that this tool did not create is never emptied, whatever file sits at its root."""

    def project(self, name: str = "Proj") -> Path:
        proj = self.home / "Code" / name
        (proj / "src").mkdir(parents=True)
        (proj / "VERSION").write_text("1.2.3\n")
        (proj / "src" / "precious.txt").write_text("data\n")
        return proj

    def paths_for(self, d: Path) -> T.Paths:
        return T.make_paths(str(self.home), str(d))

    def stable(self) -> Path:
        return Path(self.paths.stable)

    def test_a_project_with_its_own_version_file_is_foreign_and_untouched(self) -> None:
        proj = self.project()
        before = snapshot(proj)
        paths = self.paths_for(proj)
        plan = T.plan_tools(self.source, paths)
        self.assertEqual(plan.state, "foreign")
        self.assertTrue(plan.problems)
        res = T.apply_tools(self.source, paths, probes=False)
        self.assertEqual(res.status, "refused", res)
        self.assertEqual(snapshot(proj), before)
        rc, out, _ = run_main("apply", "--home", str(self.home), "--stable-dir", str(proj), "--source", self.source.scripts_dir,
                              "--sha", SHA, "--no-minimal-path", "--no-verify", "tools")
        self.assertEqual(rc, 1, out)
        self.assertEqual(snapshot(proj), before)
        self.assertTrue((proj / "src" / "precious.txt").exists())
        # plan says so too, and lists nothing as a deletion
        rc, out, _ = run_main("plan", "--home", str(self.home), "--stable-dir", str(proj), "--source", self.source.scripts_dir,
                              "--sha", SHA, "tools")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSE", out)
        self.assertNotIn("WILL DELETE", out)

    def test_a_version_file_must_look_like_ours_to_count(self) -> None:
        for label, text in (("sha only", "sha=abc1234\n"), ("tree is not a digest", "sha=abc1234\ntree=nope\n"),
                            ("tree only", "tree=" + "a" * 64 + "\n"), ("empty sha", "sha=\ntree=" + "a" * 64 + "\n")):
            with self.subTest(label):
                proj = self.tmp / ("p-" + label.replace(" ", "-"))
                proj.mkdir()
                (proj / "VERSION").write_text(text)
                (proj / "keep.txt").write_text("x")
                before = snapshot(proj)
                paths = self.paths_for(proj)
                self.assertEqual(T.plan_tools(self.source, paths).state, "foreign")
                self.assertEqual(T.apply_tools(self.source, paths, probes=False, replace_extra=True).status, "refused")
                self.assertEqual(snapshot(proj), before)

    def test_a_forged_version_without_our_hook_shim_is_foreign_even_with_replace_extra(self) -> None:
        version = "sha=abc1234\npackage=0.1.0\nfiles=1\ntree=" + "0" * 64 + "\n"
        for label, shim in (("no shim", None), ("someone else's shim", "#!/bin/sh\necho mine\n")):
            with self.subTest(label):
                proj = self.tmp / ("forged-" + label.replace(" ", "-").replace("'", ""))
                proj.mkdir()
                (proj / "VERSION").write_text(version)
                (proj / "keep.txt").write_text("x")
                if shim is not None:
                    (proj / "lane-guard-hook").write_text(shim)
                before = snapshot(proj)
                paths = self.paths_for(proj)
                self.assertEqual(T.plan_tools(self.source, paths, replace_extra=True).state, "foreign")
                self.assertEqual(T.apply_tools(self.source, paths, probes=False, replace_extra=True).status, "refused")
                self.assertEqual(snapshot(proj), before)

    def test_unexpected_files_in_an_installed_dir_need_replace_extra(self) -> None:
        self.install(())
        stable = self.stable()
        (stable / "notes.txt").write_text("mine")
        (stable / "sub").mkdir()
        (stable / "sub" / "deep.txt").write_text("mine")
        os.symlink("/nonexistent/target", stable / "link")
        before = snapshot(self.home)
        plan = T.plan_tools(self.source, self.paths)
        self.assertEqual(plan.state, "foreign")
        self.assertTrue(any("notes.txt" in p for p in plan.problems), plan.problems)
        self.assertEqual(T.apply_tools(self.source, self.paths, probes=False).status, "refused")
        self.assertEqual(snapshot(self.home), before)
        plan = T.plan_tools(self.source, self.paths, replace_extra=True)
        self.assertEqual(plan.state, "stale")
        self.assertEqual(sorted(f.rel for f in plan.files if f.status == "stale"), ["link", "notes.txt", "sub/deep.txt"])
        res = T.apply_tools(self.source, self.paths, probes=False, replace_extra=True)
        self.assertEqual(res.status, "installed", res)
        self.assertEqual(sorted(p.name for p in stable.iterdir()),
                         ["VERSION", "clutch", "fleet-apps.json", "fleet-guard-hook", "fleet_lanes", "lane-guard-hook", "muse-plugin", "muse-seat"])

    def test_leftovers_this_tool_makes_itself_are_not_extras(self) -> None:
        self.install(())
        stable = self.stable()
        (stable / "fleet_lanes" / "stray.py").write_text("x = 1\n")
        (stable / ".DS_Store").write_bytes(b"\0")
        (stable / "fleet_lanes" / "__pycache__").mkdir()
        (stable / "fleet_lanes" / "__pycache__" / "stray.cpython-39.pyc").write_bytes(b"\0")
        plan = T.plan_tools(self.source, self.paths)
        self.assertEqual((plan.state, plan.problems), ("stale", []))
        self.assertEqual(T.apply_tools(self.source, self.paths, probes=False).status, "installed")
        self.assertFalse((stable / "fleet_lanes" / "stray.py").exists())

    def test_a_git_dir_or_gitfile_is_never_replaced(self) -> None:
        for kind in ("dir", "file"):
            with self.subTest(kind):
                shutil.rmtree(self.paths.stable, ignore_errors=True)
                self.install(())
                git = self.stable() / ".git"
                if kind == "dir":
                    git.mkdir()
                    (git / "HEAD").write_text("ref: refs/heads/main\n")
                else:
                    git.write_text("gitdir: /elsewhere/.git/worktrees/x\n")
                before = snapshot(self.home)
                plan = T.plan_tools(self.source, self.paths, replace_extra=True)
                self.assertEqual(plan.state, "foreign")
                self.assertTrue(any(".git" in p for p in plan.problems), plan.problems)
                self.assertEqual(T.apply_tools(self.source, self.paths, probes=False, replace_extra=True).status, "refused")
                self.assertEqual(snapshot(self.home), before)

    def test_a_stable_dir_that_holds_the_source_is_refused_even_with_replace_extra(self) -> None:
        self.install(())
        stable = self.stable()
        shutil.copytree(self.tmp / "src", stable / "checkout")
        inner = T.Source(str(stable / "checkout" / "scripts"), str(stable / "checkout" / "fleet-apps.json"), SHA)
        before = snapshot(self.home)
        plan = T.plan_tools(inner, self.paths, replace_extra=True)
        self.assertEqual(plan.state, "foreign")
        self.assertTrue(any("source" in p for p in plan.problems), plan.problems)
        self.assertEqual(T.apply_tools(inner, self.paths, probes=False, replace_extra=True).status, "refused")
        self.assertEqual(snapshot(self.home), before)

    def test_a_stable_dir_that_holds_the_home_is_refused_even_with_replace_extra(self) -> None:
        box = self.tmp / "box"
        (box / "h").mkdir(parents=True)
        T._write_tree(str(box), T.build_files(self.source, self.paths))      # an otherwise valid install, around the home
        (box / "Documents").mkdir()
        (box / "Documents" / "thesis.txt").write_text("mine")
        for home in (box / "h", box):
            with self.subTest(str(home)):
                paths = T.Paths(home=str(home), stable=str(box))
                before = snapshot(box)
                self.assertEqual(T.plan_tools(self.source, paths, replace_extra=True).state, "foreign")
                self.assertEqual(T.apply_tools(self.source, paths, probes=False, replace_extra=True).status, "refused")
                self.assertEqual(snapshot(box), before)
        self.assertTrue((box / "Documents" / "thesis.txt").exists())

    def test_plan_prints_how_many_files_it_will_delete(self) -> None:
        self.install(())
        (self.stable() / "fleet_lanes" / "old1.py").write_text("x = 1\n")
        (self.stable() / "fleet_lanes" / "old2.py").write_text("x = 2\n")
        args = ["--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA]
        rc, out, _ = run_main("plan", *args, "tools")
        self.assertEqual(rc, 0, out)
        self.assertIn("WILL DELETE 2 files", out)
        self.assertIn("fleet_lanes/old1.py", out)
        (self.stable() / "fleet_lanes" / "old2.py").unlink()
        rc, out, _ = run_main("plan", *args, "tools")
        self.assertIn("WILL DELETE 1 file", out)
        self.assertNotIn("WILL DELETE 1 files", out)
        (self.stable() / "fleet_lanes" / "old1.py").unlink()
        rc, out, _ = run_main("plan", *args, "tools")
        self.assertNotIn("WILL DELETE", out)

    def test_replace_extra_is_a_flag_of_plan_and_apply(self) -> None:
        self.install(())
        (self.stable() / "notes.txt").write_text("mine")
        args = ["--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, "--timeout", str(TIMEOUT)]
        rc, out, _ = run_main("plan", *args, "tools")
        self.assertEqual(rc, 1, out)
        rc, out, _ = run_main("plan", *args, "--replace-extra", "tools")
        self.assertEqual(rc, 0, out)
        self.assertIn("WILL DELETE 1 file", out)
        self.assertIn("notes.txt", out)
        rc, out, _ = run_main("apply", *args, "--no-minimal-path", "--no-verify", "tools")
        self.assertEqual(rc, 1, out)
        self.assertTrue((self.stable() / "notes.txt").exists())
        rc, out, _ = run_main("apply", *args, "--no-minimal-path", "--no-verify", "--replace-extra", "tools")
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.stable() / "notes.txt").exists())


class InertHookVerifyTests(World):
    """verify must not say PASS while the platform will not run the hook."""

    def setUp(self) -> None:
        super().setUp()
        self.seed_platforms()
        self.install()

    def edit(self, key: str, **updates) -> None:
        doc = self.load(key)
        doc.update(updates)
        write_json(self.config(key), doc)

    def probes(self, checks: list) -> set:
        return {c.status for c in checks if c.name.startswith(("deny", "allow"))}

    def test_disable_all_hooks_fails_although_the_probes_pass(self) -> None:
        self.edit("claude", disableAllHooks=True)
        checks = self.verify("claude")
        self.assertFails(checks, "disableAllHooks")
        self.assertEqual(self.probes(checks), {"PASS"})                  # the trap: the command itself works
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "claude")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT HEALTHY", out)
        self.edit("claude", disableAllHooks=False)
        self.assertNotIn("FAIL", self.statuses(self.verify("claude")))

    def test_disable_all_hooks_fails_for_every_claude_style_platform(self) -> None:
        for key in ("codex", "grok"):
            with self.subTest(key):
                self.edit(key, disableAllHooks=True)
                self.assertFails(self.verify(key), "disableAllHooks")

    def test_the_guards_kill_switch_in_the_env_block_fails(self) -> None:
        for value in ("off", "OFF", " 0 ", "false", "no", "disabled", False, 0):
            with self.subTest(repr(value)):
                doc = self.load("claude")
                doc["env"] = dict(doc.get("env", {}), FLEET_LANE_GUARD=value)
                write_json(self.config("claude"), doc)
                checks = self.verify("claude")
                self.assertFails(checks, "FLEET_LANE_GUARD")
                self.assertEqual(self.probes(checks), {"PASS"})
        for value in ("on", "1", "", "true", None):
            with self.subTest(repr(value)):
                doc = self.load("claude")
                doc["env"] = dict(doc.get("env", {}), FLEET_LANE_GUARD=value)
                write_json(self.config("claude"), doc)
                self.assertNotIn("FAIL", self.statuses(self.verify("claude")))

    def test_the_off_values_are_the_guards_own(self) -> None:
        name, off = T.guard_switch()
        self.assertEqual((name, off), (G.ENV_GUARD, G._OFF_VALUES))
        self.assertEqual(T._FALLBACK_OFF_VALUES, G._OFF_VALUES)          # the copy used when guard.py cannot be imported

    def test_a_kill_switch_in_the_callers_environment_is_a_warning_not_silence(self) -> None:
        with mock.patch.dict(os.environ, {"FLEET_LANE_GUARD": "off"}):
            checks = self.verify("claude")
        warn = [c for c in checks if c.status == "WARN" and "FLEET_LANE_GUARD" in c.detail]
        self.assertTrue(warn, "\n".join(c.line() for c in checks))
        self.assertNotIn("FAIL", self.statuses(checks))                    # the probes scrub it, so they still pass
        with mock.patch.dict(os.environ, {"FLEET_LANE_GUARD": "on"}):
            self.assertFalse([c for c in self.verify("claude") if "FLEET_LANE_GUARD" in c.detail])
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "claude", "--strict")
        self.assertEqual(rc, 0, out)
        with mock.patch.dict(os.environ, {"FLEET_LANE_GUARD": "off"}):
            rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "claude", "--strict")
        self.assertEqual(rc, 1, out)

    def test_apply_notes_a_switch_that_would_make_the_new_hook_inert(self) -> None:
        self.seed(".claude/settings.json")
        self.edit("claude", disableAllHooks=True)
        res = T.apply_platform(T.PLATFORMS["claude"], self.paths, now=lambda: STAMP, shim_proven=True)
        self.assertEqual(res.status, "added", res)
        self.assertIn("disableAllHooks", res.detail)


class CodexChecksTests(World):
    """Codex PASS means Codex will run the hook: the feature flag is on, and the trust hash is not assumed."""

    def setUp(self) -> None:
        super().setUp()
        self.seed_platforms()
        self.install()
        entry = T.locate(self.load("codex"), T.PLATFORMS["codex"])[0]
        self.key = f"{self.config('codex')}:pre_tool_use:{entry.group_index}:{entry.hook_index}"
        self.toml_path = self.home / ".codex" / "config.toml"

    def record(self, key: str | None = None) -> str:
        return '[hooks.state."%s"]\ntrusted_hash = "sha256:%s"\n' % (key or self.key, "00" * 32)

    def named(self, name: str) -> list:
        return [c for c in self.verify("codex") if c.name == name]

    def test_features_hooks_false_fails_even_with_a_trust_record(self) -> None:
        self.toml_path.write_text("[features]\nhooks = false\n\n" + self.record())
        checks = self.verify("codex")
        self.assertFails(checks, "features")
        self.assertEqual({c.status for c in checks if c.name.startswith(("deny", "allow"))}, {"PASS"})

    def test_features_hooks_spellings(self) -> None:
        cases = (("[features]\nhooks = false\n", True), ("features.hooks = false\n", True),
                 ('[features]\nhooks = "true"\n', True), ("[ features ]\nhooks=false\n", True),
                 ("features = { hooks = false }\n", True), ('["features"]\nhooks = false\n', True),
                 ("[features]\nhooks = true\n", False), ("[features]\nhooks = true # on\n", False),
                 ("features.hooks = true\n", False), ("[features]\nother = 1\n", False),
                 ("[other]\nhooks = false\n", False), ("[features]\nx = 1\n[other]\nhooks = false\n", False),
                 ("# [features]\n# hooks = false\n", False), ("", False))
        for text, fails in cases:
            with self.subTest(text):
                self.toml_path.write_text(text + "\n" + self.record())
                bad = [c for c in self.verify("codex") if c.status == "FAIL"]
                self.assertEqual(bool(bad), fails, [c.line() for c in bad])
                if fails:
                    self.assertTrue(any("features" in c.line() for c in bad))

    def test_a_trust_record_is_never_a_pass(self) -> None:
        self.toml_path.write_text("[features]\nhooks = true\n\n" + self.record())
        (trust,) = self.named("trust")
        self.assertEqual(trust.status, "WARN")
        self.assertIn("UNCHECKED", trust.detail)
        base = ["verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "codex"]
        self.assertEqual(run_main(*base)[0], 0)
        rc, out, _ = run_main(*base, "--strict")
        self.assertEqual(rc, 1, out)                                       # --strict can never pass for codex

    def test_a_missing_record_still_says_not_trusted(self) -> None:
        self.toml_path.write_text("[features]\nhooks = true\n")
        (trust,) = self.named("trust")
        self.assertEqual(trust.status, "WARN")
        self.assertIn("NOT TRUSTED YET", trust.detail)

    def test_an_updated_entry_says_the_old_trust_record_is_stale(self) -> None:
        doc = self.load("codex")
        doc["hooks"]["PreToolUse"][-1]["hooks"][0]["command"] = "/old/place/lane-guard-hook --format codex"
        write_json(self.config("codex"), doc)
        res = T.apply_platform(T.PLATFORMS["codex"], self.paths, now=lambda: STAMP, shim_proven=True)
        self.assertEqual(res.status, "updated", res)
        self.assertIn("trust record", res.detail)
        self.assertIn("stale", res.detail)
        self.assertIn("/hooks", res.detail)
        plan = T.plan_platform(T.PLATFORMS["codex"], self.paths)
        self.assertEqual(plan.action, "unchanged")


class HomeArgumentTests(World):
    """An empty --home is a usage error, never the real home."""

    def setUp(self) -> None:
        super().setUp()
        self.real = self.tmp / "realhome"                       # stands in for the real home: HOME points here
        (self.real / ".claude").mkdir(parents=True)
        (self.real / ".claude" / "settings.json").write_text('{\n  "theme": "dark"\n}\n')

    def test_an_empty_home_is_a_usage_error_and_writes_nothing(self) -> None:
        before = snapshot(self.real)
        src = ["--source", self.source.scripts_dir, "--sha", SHA]
        with mock.patch.dict(os.environ, {"HOME": str(self.real)}):
            for argv in (["plan"], ["verify"], ["apply", "--no-verify", "tools", "claude"], ["apply", "tools"],
                         ["apply", "--no-verify", "claude"], ["plan", "claude"]):
                with self.subTest(argv):
                    rc, out, err = run_main(*argv, *src, "--home", "") if argv[0] != "verify" else run_main(*argv, "--home", "")
                    self.assertEqual(rc, T.EXIT_USAGE, (out, err))
                    self.assertIn("--home", err)
        self.assertEqual(snapshot(self.real), before)
        self.assertFalse((self.real / "apps").exists())

    def test_an_empty_stable_dir_is_a_usage_error_too(self) -> None:
        before = snapshot(self.tmp)
        for argv in (["plan"], ["verify"], ["apply", "--no-verify", "tools"]):
            with self.subTest(argv):
                rc, out, err = run_main(*argv, "--home", str(self.home), "--stable-dir", "", "--source", self.source.scripts_dir
                                        ) if argv[0] != "verify" else run_main(*argv, "--home", str(self.home), "--stable-dir", "")
                self.assertEqual(rc, T.EXIT_USAGE, (out, err))
        self.assertEqual(snapshot(self.tmp), before)

    def test_a_home_that_is_not_a_directory_is_a_usage_error_and_is_not_created(self) -> None:
        for argv in (["plan"], ["verify"], ["apply", "--no-verify", "tools"]):
            with self.subTest(argv):
                ghost = self.tmp / "typo" / "home"
                rc, out, err = run_main(*argv, "--home", str(ghost), "--source", self.source.scripts_dir
                                        ) if argv[0] != "verify" else run_main(*argv, "--home", str(ghost))
                self.assertEqual(rc, T.EXIT_USAGE, (out, err))
                self.assertFalse(ghost.exists())
                self.assertFalse(ghost.parent.exists())

    def test_an_empty_home_variable_is_not_the_filesystem_root(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": ""}):
            for argv in (["plan"], ["verify"]):
                with self.subTest(argv):
                    rc, out, err = run_main(*argv)
                    self.assertEqual(rc, T.EXIT_USAGE, (out, err))

    def test_the_default_still_comes_from_the_home_variable(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.real)}):
            rc, out, err = run_main("plan", "claude", "--source", self.source.scripts_dir, "--sha", SHA)
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn(str(self.real), out)


class ConfigFidelityTests(World):
    """Re-serialising a config must not change what it says."""

    def setUp(self) -> None:
        super().setUp()
        r = T.apply_tools(self.source, self.paths, probes=False)
        self.assertTrue(r.ok, r)
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude" / "settings.json"

    def put(self, text: str) -> None:
        self.settings.write_text(text, encoding="utf-8")
        os.chmod(self.settings, 0o600)

    def apply(self, **kw) -> T.Result:
        return T.apply_platform(T.PLATFORMS["claude"], self.paths, now=lambda: STAMP, shim_proven=True, **kw)

    def assertRefusedUntouched(self, needle: str) -> None:
        original = self.settings.read_bytes()
        before = snapshot(self.home)
        plan = T.plan_platform(T.PLATFORMS["claude"], self.paths)
        self.assertEqual(plan.action, "refuse", plan)
        self.assertIn(needle, plan.reason)
        res = self.apply()
        self.assertEqual(res.status, "refused", res)
        self.assertIn(needle, res.detail)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(self.backups(self.settings.parent), [])

    def test_duplicate_keys_are_refused_not_silently_dropped(self) -> None:
        self.put('{\n  "env": {"A": "1"},\n  "model": "x",\n  "env": {"B": "2"}\n}\n')
        self.assertRefusedUntouched("duplicate")
        self.put('{\n  "model": "x",\n  "nested": {"k": 1, "k": 2}\n}\n')
        self.assertRefusedUntouched("duplicate")

    def test_numbers_json_cannot_hold_are_refused(self) -> None:
        for label, text in (("overflow", '{\n  "n": 1e999,\n  "hooks": {}\n}\n'),
                            ("negative overflow", '{\n  "n": -1e999\n}\n'),
                            ("NaN", '{\n  "n": NaN\n}\n'), ("Infinity", '{\n  "n": Infinity\n}\n'),
                            ("-Infinity", '{\n  "n": -Infinity\n}\n'), ("nested", '{\n  "a": [{"b": 1e999}]\n}\n')):
            with self.subTest(label):
                self.put(text)
                original = self.settings.read_bytes()
                plan = T.plan_platform(T.PLATFORMS["claude"], self.paths)
                self.assertEqual(plan.action, "refuse", plan)
                self.assertEqual(self.apply().status, "refused")
                self.assertEqual(self.settings.read_bytes(), original)
                self.assertEqual(self.backups(self.settings.parent), [])

    def test_numbers_that_would_change_value_are_refused(self) -> None:
        for label, text in (("underflow", '{\n  "n": 1e-400\n}\n'),
                            ("too many digits", '{\n  "n": 3.141592653589793238462643383279\n}\n')):
            with self.subTest(label):
                self.put(text)
                self.assertRefusedUntouched("number")

    def test_ordinary_files_still_merge(self) -> None:
        for label, text in (("canonical", '{\n  "n": 1.5,\n  "big": 12345678901234567890,\n  "e": "\u00e9"\n}\n'),
                            ("compact", '{"n":1.5,"e":"\u00e9"}'), ("exponent", '{\n  "n": 1E5,\n  "m": 2.50\n}\n')):
            with self.subTest(label):
                self.put(text)
                before = json.loads(text)
                res = self.apply()
                self.assertEqual(res.status, "added", res)
                after = self.load("claude")
                after.pop("hooks")
                self.assertEqual(after, before)
                self.settings.unlink()
                for b in self.backups(self.settings.parent):
                    b.unlink()

    def test_text_that_cannot_be_encoded_is_not_a_traceback_and_keeps_its_content(self) -> None:
        text = '{\n  "a": "\u00e9",\n  "b": "\\ud800"\n}\n'                 # a raw e-acute and an escaped lone surrogate
        self.put(text)
        before = json.loads(text)
        plan = T.plan_platform(T.PLATFORMS["claude"], self.paths)
        self.assertEqual(plan.action, "add", plan)
        plan.new_text.encode("utf-8")                                          # encodable
        res = self.apply()
        self.assertEqual(res.status, "added", res)
        after = self.load("claude")
        after.pop("hooks")
        self.assertEqual(after, before)

    def test_verify_reads_duplicates_the_way_the_platform_does_but_fails_on_numbers_it_rejects(self) -> None:
        self.seed(".claude/settings.json")
        r = self.apply()
        self.assertEqual(r.status, "added", r)
        text = self.settings.read_text()
        dup = text.replace('"theme": "dark"', '"theme": "dark",\n  "theme": "light"')
        self.assertNotEqual(dup, text)
        self.settings.write_text(dup)
        self.assertAllPass(self.verify("claude"))                                         # last one wins, as in JSON.parse
        bad = text.replace('"theme": "dark"', '"theme": 1e999')
        self.settings.write_text(bad)
        self.assertFails(self.verify("claude"), "1e999")


class ReadOnlyConfigTests(World):
    def setUp(self) -> None:
        super().setUp()
        self.seed_platforms()
        r = T.apply_tools(self.source, self.paths, probes=False)
        self.assertTrue(r.ok, r)

    def apply(self, key: str = "claude") -> T.Result:
        return T.apply_platform(T.PLATFORMS[key], self.paths, now=lambda: STAMP, shim_proven=True)

    def test_a_read_only_config_is_refused_and_untouched(self) -> None:
        cfg = self.config("claude")
        os.chmod(cfg, 0o444)
        before = snapshot(self.home)
        plan = T.plan_platform(T.PLATFORMS["claude"], self.paths)
        self.assertEqual(plan.action, "refuse", plan)
        self.assertIn("read-only", plan.reason)
        self.assertIn("0444", plan.reason)
        res = self.apply()
        self.assertEqual(res.status, "refused", res)
        self.assertIn("not overriding", res.detail)
        self.assertEqual(snapshot(self.home), before)
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o444)
        rc, out, _ = run_main("apply", "--home", str(self.home), "--no-minimal-path", "--no-verify", "claude")
        self.assertEqual(rc, 1, out)
        self.assertIn("read-only", out)
        rc, out, _ = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, "claude")
        self.assertEqual(rc, 1, out)
        self.assertIn("read-only", out)

    def test_a_config_locked_after_the_install_is_unchanged_not_refused(self) -> None:
        self.assertEqual(self.apply().status, "added")
        os.chmod(self.config("claude"), 0o444)
        self.assertEqual(self.apply().status, "unchanged")
        self.assertEqual(T.plan_platform(T.PLATFORMS["claude"], self.paths).action, "unchanged")

    def test_an_unwritable_directory_is_refused(self) -> None:
        d = self.config("claude").parent
        os.chmod(d, 0o555)
        self.addCleanup(os.chmod, d, 0o755)
        if os.access(d, os.W_OK):
            self.skipTest("running with privileges that ignore directory permissions")
        before = snapshot(self.home)
        self.assertEqual(T.plan_platform(T.PLATFORMS["claude"], self.paths).action, "refuse")
        self.assertEqual(self.apply().status, "refused")
        self.assertEqual(snapshot(self.home), before)


class SymlinkedConfigTests(World):
    def setUp(self) -> None:
        super().setUp()
        r = T.apply_tools(self.source, self.paths, probes=False)
        self.assertTrue(r.ok, r)
        (self.home / ".claude").mkdir()
        self.outside = self.tmp / "dotfiles"
        self.outside.mkdir()

    def apply(self, **kw) -> T.Result:
        return T.apply_platform(T.PLATFORMS["claude"], self.paths, now=lambda: STAMP, shim_proven=True, **kw)

    def link(self) -> Path:
        return self.home / ".claude" / "settings.json"

    def test_a_dangling_link_is_refused_and_creates_nothing(self) -> None:
        target = self.tmp / "gone" / "deeper" / "settings.json"
        os.symlink(target, self.link())
        before = snapshot(self.tmp)
        plan = T.plan_platform(T.PLATFORMS["claude"], self.paths)
        self.assertEqual(plan.action, "refuse", plan)
        self.assertIn("dangling", plan.reason)
        for kw in ({}, {"follow_symlinks": True}):
            res = self.apply(**kw)
            self.assertEqual(res.status, "refused", res)
            self.assertIn("dangling", res.detail)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertFalse((self.tmp / "gone").exists())

    def test_a_dangling_directory_link_is_refused_even_with_follow_symlinks(self) -> None:
        (self.home / ".grok").mkdir()
        os.symlink(self.tmp / "nowhere" / "hooks", self.home / ".grok" / "hooks")
        before = snapshot(self.tmp)
        for kw in ({}, {"follow_symlinks": True}):
            res = T.apply_platform(T.PLATFORMS["grok"], self.paths, now=lambda: STAMP, shim_proven=True, **kw)
            self.assertEqual(res.status, "refused", res)
            self.assertIn("dangling", res.detail)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertFalse((self.tmp / "nowhere").exists())

    def test_a_link_to_a_file_outside_home_needs_follow_symlinks(self) -> None:
        real = self.outside / "settings.json"
        write_json(real, claude_settings(), 0o600)
        os.symlink(real, self.link())
        before = snapshot(self.tmp)
        plan = T.plan_platform(T.PLATFORMS["claude"], self.paths)
        self.assertEqual(plan.action, "refuse", plan)
        self.assertIn(str(real), plan.reason)
        self.assertIn("--follow-symlinks", plan.reason)
        res = self.apply()
        self.assertEqual(res.status, "refused", res)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.backups(self.outside), [])
        rc, out, _ = run_main("apply", "--home", str(self.home), "--no-minimal-path", "--no-verify", "claude")
        self.assertEqual(rc, 1, out)
        self.assertEqual(snapshot(self.tmp), before)

    def test_follow_symlinks_writes_through_and_plan_shows_the_real_path(self) -> None:
        real = self.outside / "settings.json"
        write_json(real, claude_settings(), 0o600)
        os.symlink(real, self.link())
        plan = T.plan_platform(T.PLATFORMS["claude"], self.paths, follow_symlinks=True)
        self.assertEqual(plan.action, "add", plan)
        rc, out, _ = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA,
                              "--follow-symlinks", "claude")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"symlink -> {real}", out)
        res = self.apply(follow_symlinks=True)
        self.assertEqual(res.status, "added", res)
        self.assertTrue(os.path.islink(self.link()))
        self.assertEqual(len(T.locate(json.loads(real.read_text()), T.PLATFORMS["claude"])), 1)
        self.assertEqual(len(self.backups(self.outside)), 1)

    def test_a_link_that_stays_inside_home_is_written_through_by_default(self) -> None:
        real = self.home / "dotfiles" / "settings.json"
        write_json(real, claude_settings(), 0o600)
        os.symlink(real, self.link())
        rc, out, _ = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, "claude")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"symlink -> {real}", out)
        res = self.apply()
        self.assertEqual(res.status, "added", res)
        self.assertTrue(os.path.islink(self.link()))
        self.assertEqual(len(T.locate(json.loads(real.read_text()), T.PLATFORMS["claude"])), 1)

    def test_a_symlinked_platform_directory_outside_home_is_refused(self) -> None:
        shutil.rmtree(self.home / ".claude")
        write_json(self.outside / "claudedir" / "settings.json", claude_settings(), 0o600)
        os.symlink(self.outside / "claudedir", self.home / ".claude")
        before = snapshot(self.tmp)
        res = self.apply()
        self.assertEqual(res.status, "refused", res)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.apply(follow_symlinks=True).status, "added")


class ApplyAllProbeTests(World):
    """`apply all` proves each platform's own command, and never treats an unchanged install as proof."""

    def broken_grok_source(self, name: str = "bgrok", sha: str = SHA) -> T.Source:
        src = self.make_source(name, sha=sha)
        hook = Path(src.scripts_dir) / "fleet_lanes" / "lane_guard_hook.py"
        text = hook.read_text(encoding="utf-8")
        broken = text.replace('if fmt in ("grok", "antigravity"):', 'if fmt in ("antigravity",):')
        self.assertNotEqual(broken, text, "the grok output branch moved; update this test")
        hook.write_text(broken, encoding="utf-8")
        return src

    def base(self, src: T.Source) -> list:
        return ["--home", str(self.home), "--source", src.scripts_dir, "--registry", src.registry, "--sha", src.sha,
                "--timeout", str(TIMEOUT), "--no-minimal-path"]

    def test_a_broken_grok_output_is_not_wired_in(self) -> None:
        self.seed_platforms()
        src = self.broken_grok_source()
        for extra in ([], ["--no-verify"]):
            with self.subTest(extra):
                shutil.rmtree(self.paths.stable, ignore_errors=True)
                rc, out, _ = run_main("apply", *self.base(src), *extra, "all")
                self.assertEqual(rc, 1, out)
                self.assertFalse(self.config("grok").exists(), out)
                self.assertNotIn("ADDED      grok", out)

    def test_the_staging_probes_cover_every_supported_format(self) -> None:
        self.install(())
        good = T.parse_version((Path(self.paths.stable) / "VERSION").read_text())["sha"]
        res = T.apply_tools(self.broken_grok_source("bgrok2", SHA2), self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(res.status, "refused", res)
        self.assertIn("staged copy failed", res.detail)
        self.assertEqual(T.parse_version((Path(self.paths.stable) / "VERSION").read_text())["sha"], good)

    def test_the_cli_never_passes_a_platform_off_as_already_proven(self) -> None:
        self.seed_platforms()
        calls = []
        real = T.apply_platform

        def spy(*a, **kw):
            calls.append(kw.get("shim_proven"))
            return real(*a, **kw)

        with mock.patch.object(T, "apply_platform", side_effect=spy):
            rc, out, _ = run_main("apply", *self.base(self.source), "--no-verify", "all")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(calls), len(T.SUPPORTED_KEYS))
        self.assertFalse(any(calls), calls)
        with mock.patch.object(T, "apply_platform", side_effect=spy):
            calls.clear()
            rc, out, _ = run_main("apply", *self.base(self.source), "--no-verify", "all")      # tools are unchanged now
        self.assertIn("UNCHANGED  tools", out)
        self.assertFalse(any(calls), calls)

    def test_apply_platform_refuses_when_only_its_own_format_is_broken(self) -> None:
        self.seed_platforms()
        self.install(())
        hook = Path(self.paths.package_dir) / "lane_guard_hook.py"
        text = hook.read_text(encoding="utf-8")
        hook.write_text(text.replace('if fmt in ("grok", "antigravity"):', 'if fmt in ("antigravity",):'), encoding="utf-8")
        res = T.apply_platform(T.PLATFORMS["grok"], self.paths, now=lambda: STAMP, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(res.status, "refused", res)
        self.assertFalse(self.config("grok").exists())
        ok = T.apply_platform(T.PLATFORMS["claude"], self.paths, now=lambda: STAMP, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(ok.status, "added", ok)


class PlanEchoTests(World):
    """plan never prints a line of the owner's config that is not our own entry."""

    def secrets(self) -> dict:
        return {
            "aws": "AWS_SECRET_ACCESS_KEY=" + "wJalrXUtn" + "FEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY",
            "pw": "DB_PASSWORD=" + "hunt3r2",
            "curl": "curl -u " + "alice:" + "s3cr3t" + " https://example.invalid/x",
            "flag": "tool --password " + "pa55wd " + "--go",
            "stripe": "STRIPE=" + "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
            "gitlab": "GL=" + "glpat-" + "abcdefghij0123456789",
        }

    def seeded(self) -> dict:
        s = self.secrets()
        doc = claude_settings()
        doc["hooks"]["PreToolUse"].append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{s['aws']} {s['pw']} run", "timeout": 3},
            {"type": "command", "command": f"{s['curl']} && {s['flag']}", "timeout": 3},
            {"type": "command", "command": f"env {s['stripe']} {s['gitlab']} true"}]})
        write_json(self.home / ".claude" / "settings.json", doc, 0o600)
        return s

    def plan(self, *extra: str) -> str:
        rc, out, err = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, *extra)
        self.assertEqual((rc, err), (0, ""), out)
        return out

    def test_other_hooks_are_counted_not_echoed(self) -> None:
        s = self.seeded()
        out = self.plan("claude")
        for name, secret in s.items():
            self.assertNotIn(secret, out, name)
        for fragment in ("hunt3r2", "s3cr3t", "pa55wd", "wJalrXUtn", "4eC39HqLyjWDarjtT1zdp7dc", "abcdefghij0123456789",
                         "economy.py", "board-check", "command-class", "remote-pre", "Zq9fakeSecretTokenValue77", "notify.sh"):
            self.assertNotIn(fragment, out)
        self.assertIn("other hook", out)
        self.assertIn("not shown", out)
        # the diff still shows what will be added, in diff form
        for needle in ("+++ ", "--- ", "@@ ", "+", f"{self.paths.stable}/lane-guard-hook --format claude"):
            self.assertIn(needle, out)

    def test_an_updated_entry_shows_only_our_old_and_new_command(self) -> None:
        self.seeded()
        self.install(("claude",))
        doc = self.load("claude")
        ours = T.locate(doc, T.PLATFORMS["claude"])[0]
        ours.holder["command"] = "/old/place/lane-guard-hook --format claude"
        write_json(self.config("claude"), doc, 0o600)
        out = self.plan("claude")
        self.assertTrue(any(ln.strip().startswith('-') and "/old/place/lane-guard-hook" in ln for ln in out.splitlines()), out)
        self.assertTrue(any(ln.strip().startswith('+') and f"{self.paths.stable}/lane-guard-hook --format claude" in ln
                            for ln in out.splitlines()), out)
        self.assertIn("[update]", out)
        for secret in self.secrets().values():
            self.assertNotIn(secret, out)

    def test_every_platform_style_elides_other_hooks(self) -> None:
        self.seed()
        out = self.plan()
        for needle in ("/x/otlp.mjs", "/x/node", "./hooks/check.sh", "python3 '/x/economy.py'", "secret-guard"):
            self.assertNotIn(needle, out)
        self.assertIn("lane-guard", out)

    def test_redact_covers_the_shapes_that_used_to_leak(self) -> None:
        for name, secret, leak in (
                ("aws secret", "AWS_SECRET_ACCESS_KEY=" + "wJalrXUtn" + "FEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY", "wJalrXUtn"),
                ("short password", "DB_PASSWORD=" + "hunt3r2", "hunt3r2"),
                ("curl -u", "curl -u " + "alice:" + "s3cr3t" + " https://example.invalid", "s3cr3t"),
                ("--password", "tool --password " + "pa55wd --go", "pa55wd"),
                ("--password=", "tool --password=" + "pa55wd --go", "pa55wd"),
                ("stripe", "x sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc y", "4eC39HqLyjWDarjtT1zdp7dc"),
                ("stripe restricted", "x rk_live_" + "4eC39HqLyjWDarjtT1zdp7dc y", "4eC39HqLyjWDarjtT1zdp7dc"),
                ("gitlab", "x glpat-" + "abcdefghij0123456789 y", "abcdefghij0123456789"),
                ("twilio sid", "x AC" + "0123456789abcdef" + "0123456789abcdef y", "0123456789abcdef0123456789abcdef"),
                ("json api_key", '"api_key": "' + 'abc123' + '"', "abc123"),
                ("json secret", '  "client_secret": "' + 'xyz' + '"', "xyz"),
                ("bare 40 char secret", "key " + "wJalrXUtn" + "FEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY" + " end", "K7MDENG")):
            with self.subTest(name):
                out = T.redact(secret)
                self.assertNotIn(leak, out)
                self.assertIn("<redacted>", out)

    def test_redact_leaves_ordinary_commands_and_our_own_alone(self) -> None:
        for text in ("python3 ~/.claude/hooks/economy.py", "/usr/bin/env python3 -I -c 'import sys'",
                     "'/private/var/folders/zz/abcdefghijklmn/T/lane-tools-test-x/home/apps/lane-tools/lane-guard-hook' --format claude",
                     '"statusMessage": "Checking checkout location"', "git status", "ls -la /usr/local/share/man/man1/something"):
            with self.subTest(text):
                self.assertEqual(T.redact(text), text)


# --------------------------------------------------------------------------- the shell prefilter in the hook shim
#
# The hook shim reads the payload with the shell and exits 0 without starting Python when the payload cannot
# trigger the guard.  The classes below prove that is a superset of the guard's own early exit and that it never
# changes an answer.  A fake python3 first on PATH records every call, so "Python did not start" is observable.

SHELLS = [("sh", "/bin/sh")] + [(n, p) for n in ("dash", "bash", "ksh") for p in [shutil.which(n)] if p]


class Recorder:
    """A directory with a fake python3 that logs each call and keeps a copy of its stdin."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "fakebin"
        self.bin.mkdir()
        self.log = root / "python-calls.log"
        self.stdin_copy = root / "python-stdin.bin"
        script = ("#!/bin/sh\n"
                  f"echo called >> {shlex.quote(str(self.log))}\n"
                  f"cat > {shlex.quote(str(self.stdin_copy))}\n"
                  "exit 0\n")
        fake = self.bin / "python3"
        fake.write_text(script, encoding="utf-8")
        os.chmod(fake, 0o755)

    def env(self, **extra: str) -> dict:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.bin.parent)}
        env.update(extra)
        return env

    def calls(self) -> int:
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def reset(self) -> None:
        for p in (self.log, self.stdin_copy):
            if p.exists():
                p.unlink()


def run_bytes(argv: list, payload: bytes, env: dict, timeout: float = TIMEOUT, cwd: str | None = None) -> tuple:
    p = subprocess.run(argv, input=payload, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def bash_payload(command: str, tool: str = "Bash", ensure_ascii: bool = True, **extra: object) -> bytes:
    doc = {"session_id": "t", "hook_event_name": "PreToolUse", "tool_name": tool,
           "tool_input": {"command": command, "description": "test"}, "cwd": "/Users/jay"}
    doc.update(extra)
    return json.dumps(doc, ensure_ascii=ensure_ascii).encode("utf-8")


class GuardFactsTests(World):
    """What the shell scripts copy from guard.py, and what they do when it cannot be copied."""

    GUARD_TEXT = (REAL_PKG / "guard.py").read_text(encoding="utf-8")

    def guard_with(self, old: str, new: str) -> str:
        self.assertIn(old, self.GUARD_TEXT, "guard.py moved this line; update the test")
        return self.GUARD_TEXT.replace(old, new, 1)

    def test_facts_come_from_the_guard(self) -> None:
        f = T.read_guard_facts()
        self.assertEqual(f.words, tuple(G._TRIGGER.pattern.split("|")))
        self.assertEqual(f.max_chars, G.MAX_COMMAND_CHARS)
        self.assertEqual(f.shell_hints, tuple(dict.fromkeys(("bash",) + tuple(G._SHELL_TOOL_HINTS))))
        self.assertEqual(f.problems, ())
        self.assertEqual(set(f.lookalikes), {"\u0130", "\u0131", "\u212a"})        # dotted I, dotless i, Kelvin sign
        self.assertEqual(T.read_guard_facts(str(SCRIPTS_DIR)), f)

    def test_the_text_reader_agrees_with_the_import(self) -> None:
        # a source tree elsewhere is read as text, never run
        self.assertEqual(T.read_guard_facts(self.source.scripts_dir), T.read_guard_facts())

    def test_nothing_above_the_basic_plane_folds_onto_a_trigger_letter(self) -> None:
        letters = "".join(sorted({c for w in G._TRIGGER.pattern.split("|") for c in w if c.isalpha()}))
        cls = re.compile("[" + letters + "]", re.IGNORECASE)
        hits = [hex(cp) for cp in range(0x80, 0x110000) if not 0xD800 <= cp < 0xE000 and cls.fullmatch(chr(cp))]
        self.assertEqual(hits, [hex(ord(c)) for c in T.read_guard_facts().lookalikes])

    def test_the_letters_lower_can_make_from_non_ascii_are_all_known(self) -> None:
        # guard._extract lower()s the tool name; a character outside ASCII whose lower() holds a letter of a shell
        # word could complete that word without its ASCII spelling, so the Muse wrapper passes such a character on.
        letters = "".join(sorted({c for h in T.read_guard_facts().shell_hints for c in h if c.isalpha()}))
        hits = [hex(cp) for cp in range(0x80, 0x110000)
                if not 0xD800 <= cp < 0xE000 and set(letters).intersection(chr(cp).lower())]
        self.assertEqual(hits, [hex(ord(c)) for c in T._lower_lookalikes(letters)])
        self.assertEqual(T._lower_lookalikes(letters), ("İ",))                  # dotted capital I lowers to i
        self.assertIn("*'\"tool_name\"'*İ*) return 1 ;;", T.render_muse_wrapper("/s"))

    def test_a_trigger_that_is_not_plain_words_gives_no_prefilter_and_a_warning(self) -> None:
        cases = {
            "a regex": re.compile(r"clon.|worktree|archive", re.IGNORECASE),
            "case sensitive": re.compile(r"clone|worktree|archive"),
            "upper case words": re.compile(r"Clone|Worktree", re.IGNORECASE),
            "a group": re.compile(r"(?:clone)|pull", re.IGNORECASE),
            "not even a pattern": None,
        }
        for label, rx in cases.items():
            with self.subTest(label), mock.patch.object(G, "_TRIGGER", rx):
                f = T.read_guard_facts()
                self.assertEqual(f.words, ())
                self.assertTrue(f.problems and "no prefilter" in f.problems[0].lower(), f.problems)
                shim = T.render_hook_shim(f)
                self.assertNotIn("case $_lg_in", shim)
                self.assertIn('_lg_py "$@"', shim)

    def test_a_source_tree_with_an_odd_trigger_is_read_the_same_way(self) -> None:
        odd = {
            "regex": self.guard_with('r"clone|worktree', 'r"clon.|worktree'),
            "re.I alias": self.guard_with("re.IGNORECASE)\n\n_UNK", "re.I)\n\n_UNK"),
            "no trigger at all": BROKEN_GUARD,
            "syntax error": "def evaluate(:\n",
        }
        for label, text in odd.items():
            with self.subTest(label):
                src = self.make_source("odd-" + label.replace(" ", "-"), guard=text)
                f = T.read_guard_facts(src.scripts_dir)
                self.assertEqual(f.words, (), f)
                self.assertTrue(f.problems, f)
        ok = self.make_source("fewer", guard=self.guard_with("r\"clone|worktree|archive|codeload|tarball|zipball|fetch|pull|remote|checkout-index\"",
                                                             "r\"clone|pull\""))
        self.assertEqual(T.read_guard_facts(ok.scripts_dir).words, ("clone", "pull"))
        gone = Path(ok.scripts_dir) / "fleet_lanes" / "guard.py"
        gone.unlink()
        self.assertEqual(T.read_guard_facts(ok.scripts_dir).words, ())

    def test_plan_and_apply_say_when_there_is_no_prefilter(self) -> None:
        src = self.make_source("noprefilter", guard=self.guard_with('r"clone|worktree', 'r"clon.|worktree'))
        tp = T.plan_tools(src, self.paths)
        self.assertTrue(tp.warnings and "NO prefilter" in tp.warnings[0])
        self.assertNotIn("case $_lg_in", tp.hook_shim_text)
        rc, out, _ = run_main("plan", "--home", str(self.home), "--source", src.scripts_dir, "--registry", src.registry,
                              "--sha", SHA, "tools")
        self.assertEqual(rc, 0, out)
        self.assertIn("WARNING: ", out)
        res = T.apply_tools(src, self.paths, timeout=TIMEOUT, probes=False)
        self.assertEqual(res.status, "installed", res)
        self.assertIn("WARNING", res.detail)
        # the install is still healthy: every call reaches the guard, which is correct and only slower
        checks = T.verify_tools(self.paths, timeout=TIMEOUT)
        self.assertNotIn("FAIL", self.statuses(checks), "\n".join(c.line() for c in checks))
        self.assertEqual({c.name: c.status for c in checks}["prefilter"], "WARN")

    def test_a_normal_install_reports_a_prefilter(self) -> None:
        self.install(())
        checks = {c.name: c for c in T.verify_tools(self.paths, timeout=TIMEOUT)}
        self.assertEqual(checks["prefilter"].status, "PASS", checks["prefilter"].line())
        self.assertIn("10 trigger words", checks["prefilter"].detail)

    def test_the_generated_patterns_are_exactly_the_guards_words(self) -> None:
        text = T.render_hook_shim()
        clauses = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("*") and ln.strip().endswith(") ;;")]
        words = list(G._TRIGGER.pattern.split("|"))
        self.assertEqual(len(clauses), len(words) + 1 + 3, clauses)                # words, the \u escape, 3 lookalikes
        for word, clause in zip(words, clauses):
            self.assertEqual(clause, T._ci_glob(word) + ") ;;")
            self.assertNotIn(word, clause)                                         # spelled with bracket pairs, not literally
        self.assertIn("*'\\u'*) ;;", clauses)
        self.assertEqual(T.empty_prefilter(text).count(") ;;"), 0)
        self.assertIn("*) exit 0 ;;", T.empty_prefilter(text))
        self.assertEqual(T.empty_prefilter(T.render_hook_shim(T.GuardFacts())), T.render_hook_shim(T.GuardFacts()))
        self.assertIn(f'-le {G.MAX_COMMAND_CHARS} ]', text)

    def test_the_shim_text_is_ascii_safe_to_print(self) -> None:
        text = T.render_hook_shim()
        self.assertTrue(any(ord(c) > 127 for c in text))                           # the lookalike letters are raw bytes in the file
        printed = "\n".join(T._printable(ln) for ln in text.splitlines())
        self.assertTrue(all(ord(c) < 128 for c in printed))
        self.assertIn("<U+212A>", printed)


class PrefilterShimTests(World):
    """The installed hook shim, run for real, against the guard it fronts."""

    FORMATS = ("claude", "codex", "grok", "antigravity", "cursor", "muse")
    SHAPE = {"claude": "claude", "codex": "claude", "grok": "claude", "antigravity": "antigravity",
             "cursor": "cursor", "muse": "muse"}

    def setUp(self) -> None:
        super().setUp()
        # the shim pins FLEET_APPS_JSON to its own copy of the registry, so the frozen fixture registry goes in the copy
        Path(self.source.registry).write_text(json.dumps(F.REGISTRY_DATA), encoding="utf-8")
        self.install(())
        self.shim = self.paths.hook_shim
        self.rec = Recorder(self.tmp)

    # -- helpers

    def shim_env(self) -> dict:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": F.HOME}

    def run_shim(self, payload: bytes, *args: str, env: dict | None = None, shell: str | None = None) -> tuple:
        argv = ([shell] if shell else []) + [self.shim, *(args or ("--format", "claude"))]
        return run_bytes(argv, payload, env if env is not None else self.shim_env())

    def py_calls(self, payload: bytes, *, shell: str | None = None, env: dict | None = None) -> int:
        """How many times the fake python3 started for this payload (0 means the shell decided alone)."""
        self.rec.reset()
        rc, out, err = self.run_shim(payload, env=env if env is not None else self.rec.env(), shell=shell)
        self.assertEqual((rc, out), (0, b""), err)
        return self.rec.calls()

    def row_env(self, fx: F.GuardFixture) -> dict:
        env = dict(fx.env) if fx.env is not None else dict(F.BASE_ENV)
        env["FLEET_APPS_JSON"] = self.source.registry
        return env

    def payload_for(self, fx: F.GuardFixture, fmt: str) -> bytes:
        doc = T.probe_payload(self.SHAPE[fmt], fx.command, fx.cwd)
        if fx.scratchpad_dir:
            doc["scratchpad_dir"] = fx.scratchpad_dir
        return json.dumps(doc).encode("utf-8")

    def direct(self, payload: bytes, fmt: str, env: dict) -> tuple:
        """What `python3 -m fleet_lanes.lane_guard_hook --format FMT` prints: the module's own main(), in process."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            rc = H.main(["--format", fmt], io.StringIO(payload.decode("utf-8")), out, err)
        return rc, out.getvalue().encode("utf-8")

    # -- (a) the property: the shim never changes an answer

    def test_every_fixture_row_gives_the_same_bytes_through_the_shim_and_directly(self) -> None:
        jobs = []                        # (label, payload, fmt, env, expected)
        for fx in F.FIXTURES:
            env = self.row_env(fx)
            for fmt in self.FORMATS:
                payload = self.payload_for(fx, fmt)
                jobs.append((f"{fx.id}/{fmt}", payload, fmt, env, self.direct(payload, fmt, env)))
        self.assertEqual(len(jobs), len(F.FIXTURES) * len(self.FORMATS))

        def run(job: tuple) -> tuple:
            label, payload, fmt, env, _ = job
            full_env = dict(env, PATH=os.environ.get("PATH", "/usr/bin:/bin"))
            rc, out, err = run_bytes([self.shim, "--format", fmt], payload, full_env)
            return label, rc, out, err

        workers = max(2, min(8, (os.cpu_count() or 2) * 2))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            got = list(pool.map(run, jobs))
        bad = []
        for job, (label, rc, out, err) in zip(jobs, got):
            if (rc, out) != job[4]:
                bad.append(f"{label}: shim {(rc, out[:160])} != direct {(job[4][0], job[4][1][:160])} stderr={err[:200]!r}")
        self.assertEqual(bad[:5], [], f"{len(bad)} of {len(jobs)} differ")
        # the table really exercised both outcomes
        outcomes = {(j[4][0], bool(j[4][1])) for j in jobs}
        self.assertEqual(outcomes, {(0, True), (0, False)})

    def test_a_sample_of_rows_matches_the_real_module_run_as_a_process(self) -> None:
        rows = F.FIXTURES[::12]
        self.assertGreater(len(rows), 10)
        for fx in rows:
            env = self.row_env(fx)
            for fmt in ("claude", "cursor", "muse"):
                with self.subTest(f"{fx.id}/{fmt}"):
                    payload = self.payload_for(fx, fmt)
                    full_env = dict(env, PATH=os.environ.get("PATH", "/usr/bin:/bin"))
                    proc = run_bytes([sys.executable, "-m", "fleet_lanes.lane_guard_hook", "--format", fmt], payload,
                                     full_env, cwd=str(SCRIPTS_DIR))
                    via_shim = run_bytes([self.shim, "--format", fmt], payload, full_env)
                    self.assertEqual((via_shim[0], via_shim[1]), (proc[0], proc[1]), via_shim[2])

    def test_a_json_escaped_trigger_word_is_still_denied(self) -> None:
        # "clone" decodes to "clone"; a prefilter that only looked at the raw text would let it through
        command = "gh repo cl\\u006fne Simple-With-Us/HogHunter /tmp/hh-verify"
        raw = ('{"tool_name":"Bash","tool_input":{"command":"' + command + '"},"cwd":"/Users/jay"}').encode("ascii")
        self.assertNotIn(b"clone", raw)
        self.assertEqual(json.loads(raw)["tool_input"]["command"], "gh repo clone Simple-With-Us/HogHunter /tmp/hh-verify")
        rc, out, err = self.run_shim(raw, env=dict(self.shim_env(), AGENT_SEAT="CLAUDE"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny", out)

    def test_the_shim_runs_under_every_shell_that_is_installed(self) -> None:
        self.assertIn("sh", dict(SHELLS))
        for name, shell in SHELLS:
            with self.subTest(name):
                self.assertEqual(self.py_calls(bash_payload("ls -la"), shell=shell), 0)
                self.assertEqual(self.py_calls(bash_payload("git clone x y"), shell=shell), 1)
                self.assertEqual(self.py_calls(bash_payload("git CLONE x y"), shell=shell), 1)
                self.assertEqual(self.py_calls(bash_payload("echo \u212a", ensure_ascii=False), shell=shell), 1)
                # and the real guard behind it still denies
                rc, out, err = self.run_shim(bash_payload(T.DENY_COMMAND), shell=shell)
                self.assertEqual(rc, 0, err)
                self.assertIn(b"permissionDecision", out)

    # -- the locale: a multibyte locale must not change what the shell sees

    def shells_with_zsh_as_sh(self) -> list:
        """SHELLS plus zsh started under the name sh (its sh emulation), when zsh is installed."""
        out = list(SHELLS)
        zsh = shutil.which("zsh")
        if zsh:
            link_dir = self.tmp / "zsh-as-sh"
            link_dir.mkdir(exist_ok=True)
            link = link_dir / "sh"
            if not link.exists():
                os.symlink(zsh, link)
            out.append(("zsh-as-sh", str(link)))
        return out

    SJIS_REPRO = ('{"tool_name":"Bash","tool_input":{"command":"c=あclone; gh repo ${c#あ} '
                  'Simple-With-Us/HogHunter /tmp/hh-verify"},"cwd":"/Users/jay"}').encode("utf-8")

    def test_a_multibyte_locale_cannot_hide_a_trigger_word(self) -> None:
        # In ja_JP.SJIS and zh_CN.GB18030 an ASCII byte can be the second byte of a character.  A shell that honours
        # the locale (bash and zsh do; dash does not) then reads the last byte of a UTF-8 character and the next
        # letter as one character, so a trigger word glued to a non-ASCII character would never match.  Where those
        # locales are not installed (a bare Linux image) the shell falls back to C and this passes trivially.
        raw = self.SJIS_REPRO
        direct = self.direct(raw, "claude", dict(F.BASE_ENV, FLEET_APPS_JSON=self.source.registry))
        self.assertIn(b"permissionDecision", direct[1])                            # the guard denies it
        for name, shell in self.shells_with_zsh_as_sh():
            for var in ("LC_ALL", "LC_CTYPE", "LANG"):
                for locale in ("ja_JP.SJIS", "zh_CN.GB18030"):
                    with self.subTest(shell=name, var=var, locale=locale):
                        self.assertEqual(self.py_calls(raw, shell=shell, env=self.rec.env(**{var: locale})), 1)
        for var in ("LC_ALL", "LC_CTYPE", "LANG"):
            with self.subTest(real_guard=var):
                rc, out, err = self.run_shim(raw, env=dict(self.shim_env(), **{var: "ja_JP.SJIS"}))
                self.assertEqual(rc, 0, err)
                self.assertIn(b"permissionDecision", out)

    def test_python_gets_the_callers_locale_unchanged(self) -> None:
        env_log = self.tmp / "env.log"
        fake = self.rec.bin / "python3"
        fake.write_text("#!/bin/sh\nenv > %s\ncat >/dev/null\n" % shlex.quote(str(env_log)), encoding="utf-8")
        os.chmod(fake, 0o755)
        for name, shell in self.shells_with_zsh_as_sh():
            for given, line in (({"LC_ALL": "ja_JP.SJIS"}, "LC_ALL=ja_JP.SJIS"), ({"LC_ALL": ""}, "LC_ALL="),
                                ({}, None)):
                with self.subTest(shell=name, given=given):
                    if env_log.exists():
                        env_log.unlink()
                    self.run_shim(bash_payload("git clone x y"), env=self.rec.env(LANG="ja_JP.SJIS", **given), shell=shell)
                    text = env_log.read_text()
                    self.assertEqual([ln for ln in text.splitlines() if ln.startswith("LC_ALL=")],
                                     [line] if line is not None else [])
                    self.assertIn("LANG=ja_JP.SJIS", text.splitlines())
                    self.assertNotIn("_lg_lc", text)

    # -- (b) a benign payload never starts Python

    def test_a_benign_payload_exits_without_starting_python(self) -> None:
        for command in ("ls -la", "git status", "npm test", "echo hello && pwd", "cat README.md",
                        "git commit -m 'fix the thing' && git push origin HEAD", "python3 -m unittest discover",
                        "rm -rf node_modules .next", "caf\u00e9 \u2014 na\u00efve \u65e5\u672c\u8a9e"):
            with self.subTest(command):
                self.assertEqual(self.py_calls(bash_payload(command, ensure_ascii=False)), 0)
                for locale in ("C", "en_US.UTF-8", "ja_JP.SJIS", "zh_CN.GB18030"):
                    self.assertEqual(self.py_calls(bash_payload(command, ensure_ascii=False),
                                                   env=self.rec.env(LC_ALL=locale)), 0)
        for raw in (b"", b"   ", b"\n", b"not json", b"{", b"null", b"[]", b'{"tool_name":"Bash"}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.py_calls(raw), 0)

    def test_a_json_escaped_non_ascii_letter_costs_a_python_start(self) -> None:
        # a \\u escape can spell any letter, so the shell cannot rule it out; only encoders that escape non-ASCII pay
        escaped = bash_payload("caf\u00e9")
        self.assertIn(b"\\u00e9", escaped)
        self.assertEqual(self.py_calls(escaped), 1)

    def test_a_benign_escape_is_not_a_reason_to_start_python(self) -> None:
        # \n, \t and \" are JSON escapes that cannot spell a letter
        doc = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo \"a\"\n\tls\\"}})
        self.assertIn("\\n", doc)
        self.assertNotIn("\\u", doc)
        self.assertEqual(self.py_calls(doc.encode()), 0)

    # -- (c) every trigger word starts it, in any case

    def test_each_trigger_word_starts_python_in_any_letter_case(self) -> None:
        for word in G._TRIGGER.pattern.split("|"):
            for variant in (word, word.upper(), word.capitalize(), "".join(c.upper() if i % 2 else c for i, c in enumerate(word))):
                with self.subTest(variant):
                    self.assertEqual(self.py_calls(bash_payload(f"echo {variant}")), 1)
                    # also when the word is only in another field of the payload (a superset is fine)
                    self.assertEqual(self.py_calls(bash_payload("ls", cwd=f"/x/{variant}")), 1)

    def test_a_json_u_escape_starts_python(self) -> None:
        for raw in (b'{"tool_input":{"command":"gh repo cl\\u006fne a b"}}', b'{"a":"\\u0063lone"}', b'\\u'):
            with self.subTest(raw=raw):
                self.assertEqual(self.py_calls(raw), 1)

    def test_the_non_ascii_letters_that_fold_onto_a_trigger_letter_start_python(self) -> None:
        for ch in T.read_guard_facts().lookalikes:
            word = "wor" + ch + "tree" if ch == "\u212a" else "arch" + ch + "ve"
            self.assertIsNotNone(G._TRIGGER.search(word), "the guard's own trigger matches this spelling")
            payload = bash_payload(f"git {word} add x", ensure_ascii=False)
            self.assertNotIn(b"\\u", payload)
            for locale in ("C", "en_US.UTF-8", "tr_TR.UTF-8"):
                with self.subTest(hex(ord(ch)) + " " + locale):
                    self.assertEqual(self.py_calls(payload, env=self.rec.env(LC_ALL=locale)), 1)

    def test_a_payload_over_the_guards_size_cap_goes_to_python(self) -> None:
        head = b'{"tool_name":"Bash","tool_input":{"command":"ls","description":"'
        tail = b'"}}'
        cap = G.MAX_COMMAND_CHARS
        for size, expected in ((cap - 1, 0), (cap, 0), (cap + 1, 1), (cap * 2, 1)):
            with self.subTest(size):
                pad = b"a" * (size - len(head) - len(tail))
                payload = head + pad + tail
                self.assertEqual(len(payload), size)
                self.assertEqual(self.py_calls(payload), expected)

    def test_a_big_payload_with_a_trigger_word_reaches_python_whole(self) -> None:
        big = bash_payload("echo clone", description="x" * 600_000)
        self.rec.reset()
        rc, out, _ = self.run_shim(big, env=self.rec.env())
        self.assertEqual((rc, out), (0, b""))
        self.assertEqual(self.rec.stdin_copy.read_bytes(), big)

    # -- python receives the original bytes

    def test_python_gets_the_original_bytes_unchanged(self) -> None:
        payloads = [
            bash_payload("git clone a b"),
            bash_payload("git clone a b") + b"\n",
            bash_payload("git clone a b") + b"\n\n\n",
            b"\n\n" + bash_payload("git clone a b"),
            b'{"command":"echo clone \\\\ \\" \\n \\t"}',
            b'{"a":\n"clone",\n  "b": 1}\n',
            b"clone\r\nclone\r\n",
            b"-n clone",
            b"clone %s %d \\ $HOME `date` $(id) *",
            "clone \u00e9\u2014".encode("utf-8"),
            b"clone \xff\xfe invalid utf-8",
            b"clone " + b"x" * 100_000,
        ]
        for payload in payloads:
            with self.subTest(payload=payload[:40]):
                self.rec.reset()
                rc, out, err = self.run_shim(payload, "--format", "muse", env=self.rec.env())
                self.assertEqual((rc, out), (0, b""), err)
                self.assertEqual(self.rec.calls(), 1)
                self.assertEqual(self.rec.stdin_copy.read_bytes(), payload)

    def test_the_arguments_reach_python_too(self) -> None:
        argv_log = self.tmp / "argv.log"
        fake = self.rec.bin / "python3"
        fake.write_text("#!/bin/sh\nfor a in \"$@\"; do echo \"$a\"; done > %s\ncat >/dev/null\n" % shlex.quote(str(argv_log)),
                        encoding="utf-8")
        os.chmod(fake, 0o755)
        self.run_shim(bash_payload("git clone a b"), "--format", "cursor", "--exit2", env=self.rec.env())
        self.assertEqual(argv_log.read_text().splitlines()[-3:], ["--format", "cursor", "--exit2"])

    def test_the_arguments_are_not_needed_to_decide(self) -> None:
        # an unknown or missing flag never changes the early exit
        self.assertEqual(self.py_calls(bash_payload("ls")), 0)
        rc, out, _ = self.run_shim(bash_payload("ls"), "--bogus", "--format", "nope", env=self.rec.env())
        self.assertEqual((rc, out), (0, b""))

    def test_the_shell_variable_does_not_leak_into_pythons_environment(self) -> None:
        env_log = self.tmp / "env.log"
        fake = self.rec.bin / "python3"
        fake.write_text("#!/bin/sh\nenv > %s\ncat >/dev/null\n" % shlex.quote(str(env_log)), encoding="utf-8")
        os.chmod(fake, 0o755)
        self.run_shim(bash_payload("git clone " + "a" * 5000), env=self.rec.env())
        self.assertNotIn("_lg_in", env_log.read_text())
        # even if the caller exported a variable of that name, the payload is not handed to Python in its environment
        self.run_shim(bash_payload("git clone " + "a" * 5000), env=self.rec.env(_lg_in="preset"))
        self.assertNotIn("_lg_in", env_log.read_text())

    # -- (d) malformed input still ends in exit 0 with no output

    def test_malformed_input_is_an_allow_through_the_real_guard(self) -> None:
        cases = [b"", b"not json clone", b"{clone", b'{"tool_name": "Bash", "tool_input": {"command": "clone', b"\xff\xfe\x00clone",
                 b"null", b"[]", b'"clone"', b"12 clone", b'{"tool_input": "clone"}', b'{"tool_name": 5, "command": "git clone x"}',
                 b'{"tool_name":"Bash","tool_input":{"command":["git","clone"]}}', b"clone\x00 \x00",
                 json.dumps({"tool_name": "Bash", "tool_input": {"command": "x" * 400_000 + " clone"}}).encode()]
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                rc, out, err = self.run_shim(raw)
                self.assertEqual((rc, out), (0, b""), err)

    def test_a_broken_python_behind_the_shim_is_still_an_allow(self) -> None:
        guard = Path(self.paths.package_dir) / "guard.py"
        guard.write_text("def evaluate(:\n", encoding="utf-8")
        for raw in (bash_payload(T.DENY_COMMAND), bash_payload("ls")):
            rc, out, _ = self.run_shim(raw)
            self.assertEqual((rc, out), (0, b""))

    # -- (e) latency, recorded and never asserted

    def test_latency_smoke(self) -> None:
        def timed(payload: bytes, n: int) -> float:
            t0 = time.perf_counter()
            for _ in range(n):
                self.run_shim(payload)
            return (time.perf_counter() - t0) / n

        benign = timed(bash_payload("ls -la"), 5)
        python = timed(bash_payload("git clone https://example.com/x.git y"), 3)
        sys.stderr.write(f"\nlatency (recorded, not asserted): benign Bash call {benign * 1000:.0f} ms through the shell "
                         f"prefilter, a call that reaches Python {python * 1000:.0f} ms\n")
        self.assertGreater(benign, 0)

    # -- verify stays honest

    def test_verify_fails_when_the_prefilter_patterns_are_emptied(self) -> None:
        self.seed_platforms()
        self.install()
        self.assertAllPass(self.verify("claude"))
        shim = Path(self.shim)
        original = shim.read_bytes()
        shim.write_bytes(T.empty_prefilter(original.decode("utf-8")).encode("utf-8"))
        os.chmod(shim, 0o755)
        # a shim that exits 0 for everything gives the deny probe no output
        for key in T.SUPPORTED_KEYS:
            with self.subTest(key):
                checks = self.verify(key)
                self.assertFails(checks, "no output")
                self.assertTrue(any(c.name == "deny/login-PATH" and c.status == "FAIL" for c in checks))
        # Muse Code's wrapper runs the fleet hook now, not this shim, so this edit leaves it passing (its own tests
        # break the fleet shim instead: MuseFleetGuardTests)
        self.assertAllPass([c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL) if c.status != "SKIP"])
        # and verify tools sees the edit twice: the digest and the shim text
        tools = T.verify_tools(self.paths, timeout=TIMEOUT)
        self.assertFails(tools, "differs")
        self.assertFails(tools, "tree digest")
        shim.write_bytes(original)
        os.chmod(shim, 0o755)
        self.assertAllPass(self.verify("claude"))
        self.assertAllPass(T.verify_tools(self.paths, timeout=TIMEOUT))

    def test_verify_fails_when_one_trigger_word_is_missing_from_the_patterns(self) -> None:
        shim = Path(self.shim)
        text = shim.read_text(encoding="utf-8")
        cut = "\n".join(ln for ln in text.splitlines() if ln.strip() != T._ci_glob("worktree") + ") ;;") + "\n"
        self.assertNotEqual(cut, text)
        shim.write_text(cut, encoding="utf-8")
        os.chmod(shim, 0o755)
        # the probes use "clone", so only the exact-text check can see this one
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "differs")

    def test_the_self_test_includes_the_emptied_prefilter_phase(self) -> None:
        lines: list = []
        rc = T.self_test(lines.append, timeout=TIMEOUT, minimal_path=False, source=self.source)
        text = "\n".join(lines)
        self.assertEqual(rc, 0, text)
        self.assertIn("with the prefilter patterns emptied", text)
        self.assertIn("FAIL  muse", text)


# --------------------------------------------------------------------------- Muse Code: the plugin bundle

class MuseBundleTests(World):
    def test_apply_tools_writes_the_bundle_and_the_seat_shim(self) -> None:
        res = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(res.status, "installed", res)
        s = Path(self.paths.stable)
        manifest = s / "muse-plugin" / "fleet-lane-guard" / ".muse-plugin" / "plugin.json"
        wrapper = s / "muse-plugin" / "fleet-lane-guard" / "hooks" / "lane-guard.sh"
        seat = s / "muse-seat"
        for p in (manifest, wrapper, seat):
            self.assertTrue(p.is_file(), p)
        self.assertEqual(manifest.read_text(), T.render_muse_manifest(str(s)))
        self.assertEqual(wrapper.read_text(), T.render_muse_wrapper(str(s)))
        self.assertEqual(seat.read_text(), T.render_muse_seat())
        self.assertTrue(os.access(wrapper, os.X_OK) and os.access(seat, os.X_OK))
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o644)
        self.assertEqual((self.paths.muse_bundle, self.paths.muse_manifest, self.paths.muse_wrapper, self.paths.muse_seat),
                         (str(s / "muse-plugin" / "fleet-lane-guard"), str(manifest), str(wrapper), str(seat)))
        # they are part of the digest, like every other file in the stable dir
        ver = T.parse_version((s / "VERSION").read_text())
        files = T.read_tree(str(s))
        files.pop("VERSION")
        self.assertEqual(ver["tree"], T.tree_digest(files))
        self.assertIn("muse-seat", files)
        self.assertIn("muse-plugin/fleet-lane-guard/hooks/lane-guard.sh", files)
        self.assertEqual(sorted(p.name for p in s.parent.iterdir()), ["lane", "lane-tools"])    # no stage dir left over

    def test_a_second_apply_is_unchanged_and_a_changed_stable_path_is_not_a_foreign_dir(self) -> None:
        self.install(())
        res = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False)
        self.assertEqual(res.status, "unchanged", res)
        plan = T.plan_tools(self.source, self.paths)
        self.assertEqual(plan.problems, [])
        self.assertEqual({fs.status for fs in plan.files}, {"same"})

    def test_apply_tools_heals_a_muse_file_that_lost_its_executable_bit(self) -> None:
        self.install(())
        for path in (self.paths.muse_seat, self.paths.muse_wrapper):
            with self.subTest(path):
                os.chmod(path, 0o644)
                self.assertFails([c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)], "not executable")
                res = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False)
                self.assertEqual(res.status, "installed", res)
                self.assertTrue(os.access(path, os.X_OK))
                self.assertEqual(T.apply_tools(self.source, self.paths, timeout=TIMEOUT, probes=False).status, "unchanged")

    def test_the_manifest_has_the_shape_muse_validated(self) -> None:
        doc = json.loads(T.render_muse_manifest("/stable dir/lane-tools"))
        self.assertEqual((doc["schemaVersion"], doc["name"]), (1, "fleet-lane-guard"))
        self.assertEqual(doc["compat"], {"source": "native", "manifestDir": ".muse-plugin"})
        hooks = doc["capabilities"]["hooks"]
        self.assertEqual(len(hooks), 1)
        hook = hooks[0]
        self.assertEqual((hook["id"], hook["event"], hook["timeoutMs"]), ("lane-guard", "PreToolUse", 5000))
        self.assertNotIn("matcher", hook)                                          # Muse rejects it
        self.assertEqual(hook["command"], ["/bin/sh", "/stable dir/lane-tools/muse-plugin/fleet-lane-guard/hooks/lane-guard.sh"])
        self.assertTrue(all(os.path.isabs(a) for a in hook["command"]))
        self.assertEqual(T.muse_hook_argv("/stable dir/lane-tools"), hook["command"])
        for key in ("skills", "commands", "mcpServers", "reminders"):
            self.assertEqual(doc["capabilities"][key], [])

    def test_the_wrapper_has_absolute_paths_only(self) -> None:
        text = T.render_muse_wrapper("/stable dir/it's")
        self.assertIn("_lg_guard='/stable dir/it'\"'\"'s/fleet-guard-hook'", text)
        self.assertNotIn("$HOME", text)
        self.assertNotIn("$PATH", text)

    def test_muse_validate_accepts_the_bundle_when_muse_is_installed(self) -> None:
        # Opt-in: this runs the REAL muse launcher and binary, which live outside the test's temp dir.  It goes
        # through verify_muse, so it gets exactly the confined environment the product gives it (throwaway HOME and
        # XDG dirs, auto-update off); MuseValidateEnvTests proves that environment with a fake launcher.
        if os.environ.get(REAL_MUSE_OPT_IN) != "1":
            self.skipTest(f"runs the real muse; set {REAL_MUSE_OPT_IN}=1 to run it")
        if not REAL_MUSE:
            self.skipTest("muse is not on PATH")
        self.install(())
        with mock.patch.dict(os.environ, {"PATH": os.path.dirname(REAL_MUSE) + os.pathsep + os.environ.get("PATH", "")}):
            checks = {c.name: c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)}
        self.assertEqual(checks["muse validate"].status, "PASS", checks["muse validate"].line())


def muse_shapes() -> tuple:
    """Raw hook payloads (key order, spacing and duplicate keys exactly as written) for the Muse wrapper's tool filter.

    SHELL: guard._extract reads each one as a shell call (a missing or null top-level tool key, or a shell word in
    the value it reads), so the wrapper must hand every one to the guard.  FAST: plain non-shell tool calls in the
    spellings encoders produce, which must exit before the guard.  EITHER: not shell calls, in shapes the wrapper
    cannot prove from the raw text (a shell word anywhere after the key counts), so it may pass them on (a process,
    never a wrong answer)."""
    cmd = json.dumps(T.DENY_COMMAND)
    inp = '"tool_input": {"command": %s}' % cmd
    shell = {
        "no-tool-key": '{%s, "cwd": "/"}' % inp,
        "tool_name-null": '{"tool_name": null, %s, "cwd": "/"}' % inp,
        "tool_name-null-compact": '{"tool_name":null,"tool_input":{"command":%s},"cwd":"/"}' % cmd,
        "tool_name-null-two-spaces": '{"tool_name":  null, %s, "cwd": "/"}' % inp,
        "tool_name-null-tab": '{"tool_name":\tnull, %s, "cwd": "/"}' % inp,
        "tool_name-null-newline": '{\n  "tool_name":\n    null,\n  %s,\n  "cwd": "/"\n}' % inp,
        "toolName-null": '{"toolName": null, %s, "cwd": "/"}' % inp,
        "tool_name-null+toolName-read": '{"tool_name": null, "toolName": "read", %s, "cwd": "/"}' % inp,
        "toolName-read+tool_name-null": '{"toolName": "read", "tool_name": null, %s, "cwd": "/"}' % inp,
        "nested-tool_name-no-top-key": '{"tool_input": {"command": %s, "tool_name": "x"}, "cwd": "/"}' % cmd,
        "nested-tool_name-first": '{"meta": {"tool_name": "read"}, %s, "cwd": "/"}' % inp,
        "tool_name-as-a-value": '{%s, "cwd": "/", "source": "tool_name"}' % inp,
        "cursor-shape+toolName-value": ('{"hook_event_name": "beforeShellExecution", "command": %s, "cwd": "/", '
                                        '"workspace_roots": ["/"], "meta": {"label": "toolName"}}' % cmd),
        "bash-before-key-only": '{"cwd": "/bash", "tool_name": null, %s}' % inp,
        "toolName-Bash+nested-tool_name-read": '{"meta": {"tool_name": "read"}, "toolName": "Bash", %s, "cwd": "/"}' % inp,
        "duplicate-tool_name-last-Bash": '{"tool_name": "read", %s, "cwd": "/", "tool_name": "Bash"}' % inp,
        "duplicate-tool_name-last-null": '{"tool_name": "read", %s, "cwd": "/", "tool_name": null}' % inp,
        "escaped-quote-in-a-key": '{"x\\"tool_name": "read", %s, "cwd": "/"}' % inp,
        "space-before-colon-Bash": '{"tool_name" : "Bash", %s, "cwd": "/"}' % inp,
        "escaped-quote-then-bash": '{"tool_name": "x\\"bash", %s, "cwd": "/"}' % inp,
        "shell-word-glued-to-non-ascii": '{"tool_name": "あbash", %s, "cwd": "/"}' % inp,
        "json-escaped-shell-word": '{"tool_name": "\\u0062ash", %s, "cwd": "/"}' % inp,
    }
    fast = {
        "read-compact": '{"hook_event_name":"PreToolUse","tool_name":"read","tool_input":{"command":%s},"cwd":"/"}' % cmd,
        "read-default-separators": json.dumps({"hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/",
                                               "permission_mode": "default", "tool_name": "read",
                                               "tool_input": {"command": T.DENY_COMMAND}}),
        "read-indented": json.dumps({"hook_event_name": "PreToolUse", "tool_name": "read",
                                     "tool_input": {"command": T.DENY_COMMAND}}, indent=2),
        "toolName-read": '{"toolName":"read","toolInput":{"command":%s},"cwd":"/"}' % cmd,
    }
    either = {
        "write-whose-content-says-bash": json.dumps({"tool_name": "write", "tool_input": {
            "file_path": "/x", "content": "run it in bash: " + T.DENY_COMMAND}, "cwd": "/"}),
        "tool_name-number": '{"tool_name": 5, %s, "cwd": "/"}' % inp,
        "space-before-colon-read": '{"tool_name" : "read", %s, "cwd": "/"}' % inp,
        "tool_name-after-a-nested-object": '{%s, "tool_name": "read", "cwd": "/"}' % inp,
        "dotted-capital-I": '{"tool_name": "termİnal", %s, "cwd": "/"}' % inp,
        "top-level-array": '[{"tool_name": "Bash", %s, "cwd": "/"}]' % inp,
    }
    enc = lambda d: {k: v.encode("utf-8") for k, v in d.items()}                    # noqa: E731
    return enc(shell), enc(fast), enc(either)


class MuseWrapperTests(World):
    """hooks/lane-guard.sh with a fake guard shim behind it: which calls reach the guard, and with what bytes."""

    def setUp(self) -> None:
        super().setUp()
        self.stable = self.tmp / "stable dir"
        self.stable.mkdir()
        self.calls = self.tmp / "guard-calls.log"
        self.stdin_copy = self.tmp / "guard-stdin.bin"
        fake = self.stable / "fleet-guard-hook"
        fake.write_text("#!/bin/sh\n"
                        f"echo \"$@\" >> {shlex.quote(str(self.calls))}\n"
                        f"cat > {shlex.quote(str(self.stdin_copy))}\n"
                        "echo '{\"fake\": \"deny\"}'\n"
                        "exit 3\n", encoding="utf-8")
        os.chmod(fake, 0o755)
        self.wrapper = self.tmp / "lane-guard.sh"
        self.wrapper.write_text(T.render_muse_wrapper(str(self.stable)), encoding="utf-8")
        os.chmod(self.wrapper, 0o755)

    def run_wrapper(self, payload: bytes, env: dict | None = None) -> tuple:
        res = T.run_argv(["/bin/sh", str(self.wrapper)], payload, {} if env is None else env, TIMEOUT, cwd="/")
        return res.returncode, res.stdout

    def reached(self, payload: bytes) -> bool:
        for p in (self.calls, self.stdin_copy):
            if p.exists():
                p.unlink()
        rc, out = self.run_wrapper(payload)
        self.assertEqual(rc, 0)                                                  # always: a hook failure is never a block
        reached = self.calls.exists()
        if reached:
            self.assertEqual(self.calls.read_text().strip(), "--format muse")
            self.assertEqual(self.stdin_copy.read_bytes(), payload)
            self.assertEqual(out, b'{"fake": "deny"}\n')                         # its stdout, passed through
        else:
            self.assertEqual(out, b"")
        return reached

    def payload(self, tool: str, command: str = "ls") -> bytes:
        return json.dumps({"hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/", "permission_mode": "default",
                           "tool_name": tool, "tool_input": {"command": command, "description": "d"}}).encode()

    def test_shell_tool_calls_reach_the_guard_in_any_letter_case(self) -> None:
        for tool in ("bash", "Bash", "BASH", "bAsH"):
            with self.subTest(tool):
                self.assertTrue(self.reached(self.payload(tool)))

    def test_the_other_shell_tool_words_the_guard_knows_reach_it_too(self) -> None:
        for tool in G._SHELL_TOOL_HINTS:
            with self.subTest(tool):
                self.assertTrue(self.reached(self.payload(tool)))

    def test_tools_that_are_not_shell_tools_exit_before_the_guard(self) -> None:
        for tool in ("read", "write", "edit", "grep", "glob", "web_fetch", "task", "mcp__thing__lookup"):
            with self.subTest(tool):
                self.assertFalse(self.reached(self.payload(tool, command="git clone https://example.com/a b")))

    def test_the_tool_filter_is_a_superset_not_a_parse(self) -> None:
        # the word is looked for after the tool_name key anywhere in the payload, so a non-shell tool whose input
        # mentions a shell reaches the guard, which allows it.  That costs a process, never a wrong answer.
        doc = {"tool_name": "write", "tool_input": {"file_path": "/x", "content": "run it in bash"}}
        self.assertTrue(self.reached(json.dumps(doc).encode()))
        # a word that comes BEFORE the key does not count
        before = '{"cwd": "/bash", "tool_name": "read", "tool_input": {"file_path": "/x"}}'
        self.assertFalse(self.reached(before.encode()))

    def test_a_payload_that_names_no_tool_is_passed_on(self) -> None:
        self.assertTrue(self.reached(b'{"command":"git clone a b","cwd":"/"}'))
        self.assertTrue(self.reached(b'{"toolName":"Bash","toolInput":{"command":"ls"}}'))
        self.assertFalse(self.reached(b'{"toolName":"read","toolInput":{"file_path":"/x"}}'))

    def test_a_json_escaped_tool_name_is_passed_on(self) -> None:
        self.assertTrue(self.reached(b'{"tool_name":"\\u0062ash","tool_input":{"command":"ls"}}'))

    def test_every_shape_the_guard_reads_as_a_shell_call_reaches_it(self) -> None:
        # null and missing tool keys, keys that only occur nested or as a value, odd spacing, duplicate keys
        shell, fast, _ = muse_shapes()
        for label, raw in shell.items():
            with self.subTest(label):
                self.assertTrue(self.reached(raw))
        for label, raw in fast.items():
            with self.subTest(label):
                self.assertFalse(self.reached(raw))

    def test_the_shapes_hold_under_every_installed_shell(self) -> None:
        shell, fast, _ = muse_shapes()
        for name, sh in SHELLS:
            for label, raw in list(shell.items()) + list(fast.items()):
                with self.subTest(shell=name, shape=label):
                    for p in (self.calls, self.stdin_copy):
                        if p.exists():
                            p.unlink()
                    res = T.run_argv([sh, str(self.wrapper)], raw, {}, TIMEOUT, cwd="/")
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertEqual(self.calls.exists(), label in shell, res.stdout)

    def test_a_lookalike_letter_in_the_tool_name_reaches_the_guard(self) -> None:
        self.assertTrue(self.reached('{"tool_name":"termİnal","tool_input":{"command":"ls"}}'.encode("utf-8")))

    def test_a_big_payload_never_makes_the_wrapper_slow(self) -> None:
        # Only case globs scan the whole payload; they stay linear in every shell.  `${x#*key}` and `${x%%key*}` do
        # not (dash took 4 s on a 20 KB payload, bash 3.2 about 2 s on 256 KB), which is why the wrapper never runs
        # them on a key that is not in its first MUSE_KEY_WINDOW bytes.  A hook that times out is never a deny.
        content = ('a\\n{"b": [1]}\\\\ bash ' * 14000)[:256 * 1024]
        shapes = {
            "key-first": (json.dumps({"tool_name": "read", "tool_input": {"content": content}}), True),
            "key-after-the-content": (json.dumps({"tool_input": {"content": content}, "tool_name": "read"}), True),
            "key-after-a-long-flat-value": (json.dumps({"cwd": "x" * (256 * 1024), "tool_name": "read"}), True),
            "no-key": (json.dumps({"tool_input": {"content": content}}), True),
            "key-first-no-shell-word": (json.dumps({"tool_name": "read", "tool_input": {
                "content": content.replace("bash", "bush")}}), False),
        }
        for name, sh in SHELLS:
            for label, (raw, reaches) in shapes.items():
                with self.subTest(shell=name, shape=label):
                    for p in (self.calls, self.stdin_copy):
                        if p.exists():
                            p.unlink()
                    t0 = time.perf_counter()
                    res = T.run_argv([sh, str(self.wrapper)], raw.encode(), {}, TIMEOUT, cwd="/")
                    took = time.perf_counter() - t0
                    self.assertFalse(res.timed_out)
                    self.assertEqual((res.returncode, self.calls.exists()), (0, reaches), res.stderr)
                    self.assertLess(took, 20.0, f"{took:.1f}s")                    # the quadratic forms take minutes
                    sys.stderr.write(f"\n{name} {label}: {took * 1000:.0f} ms (recorded)")

    def test_a_multibyte_locale_cannot_hide_a_shell_word(self) -> None:
        # In ja_JP.SJIS and zh_CN.GB18030 an ASCII byte can be the second byte of a character, so a shell that
        # honours the locale would read the last byte of a UTF-8 character and the next letter as one character.
        raw = '{"tool_name":"あbash","tool_input":{"command":"ls"}}'.encode("utf-8")
        for var in ("LC_ALL", "LC_CTYPE", "LANG"):
            for locale in ("ja_JP.SJIS", "zh_CN.GB18030"):
                with self.subTest(var=var, locale=locale):
                    for p in (self.calls, self.stdin_copy):
                        if p.exists():
                            p.unlink()
                    rc, _ = self.run_wrapper(raw, env={var: locale})
                    self.assertEqual(rc, 0)
                    self.assertTrue(self.calls.exists())

    def test_the_guard_gets_the_callers_locale_unchanged(self) -> None:
        env_log = self.tmp / "guard-env.log"
        (self.stable / "fleet-guard-hook").write_text(f"#!/bin/sh\nenv > {shlex.quote(str(env_log))}\ncat >/dev/null\n",
                                                     encoding="utf-8")
        for given, line in (({"LC_ALL": "ja_JP.SJIS"}, "LC_ALL=ja_JP.SJIS"), ({"LC_ALL": ""}, "LC_ALL="), ({}, None)):
            with self.subTest(given=given):
                if env_log.exists():
                    env_log.unlink()
                self.run_wrapper(self.payload("bash"), env=given)
                lines = [ln for ln in env_log.read_text().splitlines() if ln.startswith("LC_ALL=")]
                self.assertEqual(lines, [line] if line is not None else [])

    def test_input_that_names_no_tool_is_passed_on_and_the_guard_decides(self) -> None:
        for raw in (b"", b"\xff\xfe", b"garbage"):
            self.assertTrue(self.reached(raw))                                     # exit 0 either way (asserted inside)

    def test_a_missing_guard_is_an_allow(self) -> None:
        (self.stable / "fleet-guard-hook").unlink()
        rc, out = self.run_wrapper(self.payload("bash"))
        self.assertEqual((rc, out), (0, b""))
        rc, out = self.run_wrapper(b"")
        self.assertEqual((rc, out), (0, b""))

    def test_a_guard_that_fails_cannot_become_a_block(self) -> None:
        # the fake exits 3; the wrapper still exits 0
        self.assertTrue(self.reached(self.payload("bash")))

    def test_no_environment_is_needed(self) -> None:
        rc, out = self.run_wrapper(self.payload("bash"), env={})
        self.assertEqual((rc, out), (0, b'{"fake": "deny"}\n'))

    def test_without_hint_facts_every_call_reaches_the_guard(self) -> None:
        self.wrapper.write_text(T.render_muse_wrapper(str(self.stable), T.GuardFacts()), encoding="utf-8")
        self.assertTrue(self.reached(self.payload("read")))
        self.assertNotIn("case $_lg_in", T.render_muse_wrapper(str(self.stable), T.GuardFacts()))


class MuseWrapperRealGuardTests(World):
    """The installed wrapper with the REAL hook shim and guard behind it, run the way Muse runs it (no shell, an empty
    environment, from /), against the guard's own main() in process.  The wrapper may only ever add work, never
    change an answer: every shape must give the same bytes both ways."""

    def setUp(self) -> None:
        super().setUp()
        Path(self.source.registry).write_text(json.dumps(F.REGISTRY_DATA), encoding="utf-8")
        self.install(())
        self.registry = os.path.join(self.paths.stable, T.REGISTRY_NAME)
        self.hook_env: dict = {} if MINIMAL else {"PATH": os.environ.get("PATH", "")}

    def direct(self, raw: bytes) -> bytes:
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FLEET_APPS_JSON": self.registry}, clear=True):
            H.main(["--format", "muse"], io.StringIO(raw.decode("utf-8")), out, io.StringIO())
        return out.getvalue().encode("utf-8")

    def via_wrapper(self, raw: bytes, env: dict | None = None) -> bytes:
        res = T.run_argv(T.muse_hook_argv(self.paths.stable), raw, self.hook_env if env is None else env, TIMEOUT, cwd="/")
        self.assertEqual((res.returncode, res.timed_out, res.error or ""), (0, False, ""), res.stderr)
        return res.stdout

    def test_the_wrapper_never_changes_the_guards_answer(self) -> None:
        shell, fast, either = muse_shapes()
        denied = 0
        for group in (shell, fast, either):
            for label, raw in group.items():
                with self.subTest(label):
                    want = self.direct(raw)
                    self.assertEqual(self.via_wrapper(raw), want)
                    denied += bool(want)
        # the table really holds denials: every shell shape is one, the rest are not
        self.assertEqual(denied, len(shell))
        for label, raw in shell.items():
            self.assertIn(b'"permissionDecision": "deny"', self.direct(raw), label)

    def test_a_multibyte_locale_changes_nothing(self) -> None:
        raw = '{"tool_name":"あbash","tool_input":{"command":%s},"cwd":"/"}' % json.dumps(T.DENY_COMMAND)
        want = self.direct(raw.encode("utf-8"))
        self.assertTrue(want)
        for locale in ("ja_JP.SJIS", "zh_CN.GB18030"):
            with self.subTest(locale):
                self.assertEqual(self.via_wrapper(raw.encode("utf-8"), dict(self.hook_env, LC_ALL=locale)), want)


class MuseSeatTests(World):
    def setUp(self) -> None:
        super().setUp()
        self.install(())
        self.seat = self.paths.muse_seat
        self.fakebin = self.tmp / "muse-bin"
        self.fakebin.mkdir()
        fake = self.fakebin / "muse"
        fake.write_text("#!/bin/sh\n"
                        "echo \"seat=$AGENT_SEAT tag=$AGENT_TAG argc=$#\"\n"
                        "for a in \"$@\"; do echo \"arg=[$a]\"; done\n", encoding="utf-8")
        os.chmod(fake, 0o755)
        self.emptybin = self.tmp / "empty-bin"
        self.emptybin.mkdir()

    def run_seat(self, *args: str, path: str | None = None, extra: dict | None = None, via: str | None = None) -> tuple:
        env = {"PATH": path if path is not None else f"{self.fakebin}:/usr/bin:/bin", "HOME": str(self.home)}
        env.update(extra or {})
        p = subprocess.run([via or self.seat, *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=TIMEOUT)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def test_it_sets_the_seat_and_passes_every_argument_through(self) -> None:
        rc, out, err = self.run_seat("--model", "x y", "", "-p", "it's \"quoted\"")
        self.assertEqual((rc, err), (0, ""), out)
        lines = out.splitlines()
        self.assertEqual(lines[0], "seat=MC tag=MC argc=5")
        self.assertEqual(lines[1:], ["arg=[--model]", "arg=[x y]", "arg=[]", "arg=[-p]", "arg=[it's \"quoted\"]"])

    def test_it_replaces_a_seat_that_was_already_set(self) -> None:
        rc, out, _ = self.run_seat(extra={"AGENT_SEAT": "CLAUDE", "AGENT_TAG": "CLAUDE"})
        self.assertEqual((rc, out.splitlines()[0]), (0, "seat=MC tag=MC argc=0"))

    def test_the_seat_is_the_registry_tag_of_muse_code(self) -> None:
        reg = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        tags = {s["tag"]: s for s in reg["seats"]}
        self.assertEqual(T.MUSE_SEAT_TAG, "MC")
        self.assertEqual(tags["MC"]["notesName"], "Muse Code")

    def test_it_refuses_when_muse_is_not_found(self) -> None:
        rc, out, err = self.run_seat("a", path=f"{self.emptybin}:/usr/bin:/bin")
        self.assertEqual((rc, out), (127, ""))
        self.assertIn("muse-seat: cannot find the real muse", err)

    def test_it_refuses_when_the_muse_it_finds_is_itself(self) -> None:
        (self.fakebin / "muse").unlink()
        os.symlink(self.seat, self.fakebin / "muse")
        for via in (self.seat, str(self.fakebin / "muse")):
            with self.subTest(via):
                rc, out, err = self.run_seat("a", via=via)
                self.assertEqual((rc, out), (127, ""))
                self.assertIn("is this wrapper itself", err)

    def wrapper_named_muse(self, use_exec: bool) -> Path:
        """A script named muse, first on PATH, that starts muse-seat again (the obvious way to give scripts the seat
        too).  A hop counter stops it after 5 hops, so a broken muse-seat fails the test instead of hanging it."""
        hops = self.tmp / "hops"
        if hops.exists():
            hops.unlink()
        (self.fakebin / "muse").write_text(
            "#!/bin/sh\n"
            f"echo hop >> {shlex.quote(str(hops))}\n"
            f"[ $(wc -l < {shlex.quote(str(hops))}) -gt 5 ] && exit 99\n"
            + ("exec " if use_exec else "") + f"{shlex.quote(self.seat)} \"$@\"\n", encoding="utf-8")
        os.chmod(self.fakebin / "muse", 0o755)
        return hops

    def test_it_refuses_a_muse_script_that_starts_it_again(self) -> None:
        # -ef cannot see this: the script is a different file.  Without the guard it execs itself forever.
        for name, shell in SHELLS:
            for use_exec in (True, False):
                with self.subTest(shell=name, exec=use_exec):
                    hops = self.wrapper_named_muse(use_exec)
                    p = subprocess.run([shell, self.seat, "a"], env={"PATH": f"{self.fakebin}:/usr/bin:/bin"},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
                    rc, out, err = p.returncode, p.stdout.decode(), p.stderr.decode()
                    self.assertEqual((rc, out), (127, ""), err)
                    self.assertEqual(hops.read_text().splitlines(), ["hop"])          # one hop, then the refusal
                    self.assertIn("muse-seat: muse-seat is already running", err)

    def test_a_session_it_started_cannot_start_it_again(self) -> None:
        # The guard is an exported variable, so it is also set inside the Muse session muse-seat started.  There
        # AGENT_SEAT is already MC and plain `muse` is the way to start another session; muse-seat says so.
        rc, out, _ = self.run_seat("x")
        self.assertEqual(rc, 0)
        rc, out, err = self.run_seat("x", extra={"_MUSE_SEAT_ACTIVE": "1"})
        self.assertEqual((rc, out), (127, ""))
        self.assertIn("run muse itself", err)
        fake = self.fakebin / "muse"
        fake.write_text("#!/bin/sh\necho \"active=${_MUSE_SEAT_ACTIVE-unset} seat=$AGENT_SEAT\"\n", encoding="utf-8")
        rc, out, _ = self.run_seat()
        self.assertEqual((rc, out.strip()), (0, "active=1 seat=MC"))

    def test_it_refuses_a_muse_that_is_not_executable(self) -> None:
        os.chmod(self.fakebin / "muse", 0o644)
        rc, _out, err = self.run_seat("a", path=f"{self.fakebin}:{self.emptybin}")
        self.assertEqual(rc, 127)
        self.assertIn("cannot find the real muse", err)

    def test_it_works_when_started_by_name_from_path(self) -> None:
        env = {"PATH": f"{self.paths.stable}:{self.fakebin}:/usr/bin:/bin", "HOME": str(self.home)}
        p = subprocess.run(["/bin/sh", "-c", "muse-seat one two"], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=TIMEOUT)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"seat=MC tag=MC argc=2", p.stdout)
        # and a symlink named muse that comes first is caught by name too
        os.symlink(self.seat, self.emptybin / "muse")
        env["PATH"] = f"{self.emptybin}:{self.paths.stable}:{self.fakebin}:/usr/bin:/bin"
        p = subprocess.run(["/bin/sh", "-c", "muse"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
        self.assertEqual(p.returncode, 127)
        self.assertIn(b"is this wrapper itself", p.stderr)

    def test_the_seat_shim_runs_under_every_installed_shell(self) -> None:
        for name, shell in SHELLS:
            with self.subTest(name):
                p = subprocess.run([shell, self.seat, "x"], env={"PATH": f"{self.fakebin}:/usr/bin:/bin"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(b"seat=MC tag=MC argc=1", p.stdout)


class MusePlanApplyVerifyTests(World):
    """plan, apply and verify for the muse target.  The installer never runs muse for plan or apply, never edits
    Muse's config, and says what the owner has to do."""

    def setUp(self) -> None:
        super().setUp()
        self.muse_log = self.tmp / "muse-calls.log"
        self.fakebin = self.tmp / "fake-muse-bin"
        self.fakebin.mkdir()
        self.set_fake_muse('{"valid": true}', 0)
        self.path_env = mock.patch.dict(os.environ, {"PATH": f"{self.fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}"})
        self.path_env.start()
        self.addCleanup(self.path_env.stop)

    def set_fake_muse(self, body: str, rc: int) -> None:
        fake = self.fakebin / "muse"
        fake.write_text("#!/bin/sh\n"
                        f"echo \"$@\" >> {shlex.quote(str(self.muse_log))}\n"
                        f"echo \"$HOME\" >> {shlex.quote(str(self.muse_log) + '.home')}\n"
                        f"echo {shlex.quote(body)}\n"
                        f"exit {rc}\n", encoding="utf-8")
        os.chmod(fake, 0o755)

    def muse_calls(self) -> list:
        return self.muse_log.read_text().splitlines() if self.muse_log.exists() else []

    def base(self) -> list:
        return ["--home", str(self.home), "--source", self.source.scripts_dir, "--registry", self.source.registry,
                "--sha", SHA, "--timeout", str(TIMEOUT), "--no-minimal-path"]

    # -- plan

    def test_plan_muse_prints_the_owner_actions_exactly_and_never_runs_muse(self) -> None:
        self.seed(".config/muse/settings.json")
        before = snapshot(self.tmp)
        rc, out, err = run_main("plan", *self.base(), "muse")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.muse_calls(), [])
        bundle = shlex.quote(self.paths.muse_bundle)
        self.assertIn(f"     1. muse plugins install {bundle} --scope user\n", out)
        self.assertIn("     2. muse plugins approve fleet-lane-guard\n", out)
        self.assertIn("     3. start a new Muse Code session\n", out)
        self.assertIn("OWNER ACTIONS (yours to run; this tool never edits Muse's config, and the only muse it runs "
                      "is `muse plugins validate` in verify, confined to a throwaway HOME)", out)
        self.assertIn("MUSE_EXPERIMENTAL_PLUGINS=1 may have to be set", out)
        self.assertIn("UNVERIFIED", out)
        self.assertIn(f"run {self.paths.muse_seat} instead of `muse`", out)
        self.assertIn("AGENT_SEAT=MC and AGENT_TAG=MC", out)
        self.assertIn("[plugin bundle new]", out)
        self.assertIn("/bin/sh", out)                                              # the manifest is shown
        self.assertNotIn('"matcher"', out)                                          # the manifest shown has no matcher key

    def test_plan_muse_follows_the_installed_state(self) -> None:
        self.install(())
        rc, out, _ = run_main("plan", *self.base(), "muse")
        self.assertEqual(rc, 0, out)
        self.assertIn("[plugin bundle current]", out)
        Path(self.paths.muse_wrapper).write_text("#!/bin/sh\nexit 0\n")
        rc, out, _ = run_main("plan", *self.base(), "muse")
        self.assertIn("[plugin bundle stale]", out)
        self.assertIn("changed", out)

    def test_plan_tools_lists_the_muse_files_and_the_seat_shim(self) -> None:
        rc, out, _ = run_main("plan", *self.base(), "tools")
        self.assertEqual(rc, 0, out)
        self.assertIn("new      muse-seat", out)
        self.assertIn("muse-plugin/fleet-lane-guard/hooks/lane-guard.sh", out)
        self.assertIn("muse-plugin/fleet-lane-guard/.muse-plugin/plugin.json", out)
        self.assertIn("prefilter: derived from guard.py _TRIGGER (10 words: clone, worktree", out)
        self.assertTrue(all(ord(c) < 128 for c in out), "plan output must print on any terminal")

    def test_plan_all_default_still_lists_muse_and_exits_zero(self) -> None:
        self.seed()
        rc, out, _ = run_main("plan", *self.base())
        self.assertEqual(rc, 0, out)
        self.assertIn("== muse", out)

    # -- apply

    def test_apply_muse_on_a_fresh_home_installs_the_stable_copy_with_the_bundle(self) -> None:
        self.seed(".config/muse/settings.json")
        cfg = self.home / ".config" / "muse" / "settings.json"
        cfg_before = (cfg.read_bytes(), cfg.stat().st_mtime_ns)
        rc, out, err = run_main("apply", *self.base(), "--no-verify", "muse")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertIn("INSTALLED  tools", out)                                      # the bundle is written with the stable copy
        self.assertIn("UNCHANGED  muse", out)
        self.assertTrue(os.access(self.paths.muse_wrapper, os.X_OK))
        self.assertEqual(Path(self.paths.muse_manifest).read_text(), T.render_muse_manifest(self.paths.stable))
        self.assertIn(f"OWNER ACTION 1 muse plugins install {shlex.quote(self.paths.muse_bundle)} --scope user\n", out)
        self.assertEqual(self.muse_calls(), [])
        self.assertEqual((cfg.read_bytes(), cfg.stat().st_mtime_ns), cfg_before)
        self.assertFalse((self.home / ".config" / "muse" / "plugins").exists())

    def test_apply_muse_refreshes_a_stale_bundle_and_leaves_a_current_one_alone(self) -> None:
        self.install(())
        Path(self.paths.muse_wrapper).write_text("#!/bin/sh\nexit 0\n")
        rc, out, _ = run_main("apply", *self.base(), "--no-verify", "muse")
        self.assertEqual(rc, 0, out)
        self.assertIn("INSTALLED  tools", out)
        self.assertEqual(Path(self.paths.muse_wrapper).read_text(), T.render_muse_wrapper(self.paths.stable))
        before = snapshot(self.tmp)
        rc, out, _ = run_main("apply", *self.base(), "--no-verify", "muse")
        self.assertEqual(rc, 0, out)
        self.assertIn("UNCHANGED  tools", out)
        self.assertEqual(snapshot(self.tmp), before)

    def test_apply_muse_refuses_when_the_stable_copy_cannot_be_installed(self) -> None:
        stable = Path(self.paths.stable)
        stable.mkdir(parents=True)
        (stable / "mine.txt").write_text("not yours")                               # a foreign stable dir
        rc, out, _ = run_main("apply", *self.base(), "--no-verify", "muse")
        self.assertEqual(rc, 1, out)
        self.assertIn("REFUSED", out)
        self.assertEqual([p.name for p in stable.iterdir()], ["mine.txt"])

    def test_apply_tools_then_muse_prints_the_owner_actions_and_never_runs_muse_or_edits_its_config(self) -> None:
        self.seed(".config/muse/settings.json")
        cfg = self.home / ".config" / "muse" / "settings.json"
        cfg_before = (cfg.read_bytes(), cfg.stat().st_mtime_ns)
        rc, out, err = run_main("apply", *self.base(), "--no-verify", "tools")
        self.assertEqual((rc, err), (0, ""), out)
        before = snapshot(self.tmp)
        rc, out, err = run_main("apply", *self.base(), "--no-verify", "muse")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)                               # a current stable copy: nothing is written
        self.assertEqual(self.muse_calls(), [])                                    # and muse is not run
        self.assertIn("UNCHANGED  tools", out)
        self.assertIn("UNCHANGED  muse", out)
        bundle = shlex.quote(self.paths.muse_bundle)
        self.assertIn(f"OWNER ACTION 1 muse plugins install {bundle} --scope user\n", out)
        self.assertIn("OWNER ACTION 2 muse plugins approve fleet-lane-guard\n", out)
        self.assertIn("OWNER ACTION 3 start a new Muse Code session\n", out)
        self.assertIn("MUSE_EXPERIMENTAL_PLUGINS=1 may be required", out)
        self.assertEqual((cfg.read_bytes(), cfg.stat().st_mtime_ns), cfg_before)
        # with the verify that normally follows, the only thing ever run is the confined validate
        rc, out, _ = run_main("apply", *self.base(), "muse")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.muse_calls(), [f"plugins validate {self.paths.muse_bundle} --json"])
        self.assertEqual((cfg.read_bytes(), cfg.stat().st_mtime_ns), cfg_before)

    def test_apply_tools_alone_never_calls_muse(self) -> None:
        rc, out, _ = run_main("apply", *self.base(), "--no-verify", "tools")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.muse_calls(), [])
        self.assertFalse((self.home / ".config").exists())                          # nothing was created for Muse

    def test_apply_muse_refuses_a_stale_bundle(self) -> None:
        self.install(())
        Path(self.paths.muse_manifest).write_text("{}\n")
        res = T.apply_muse(self.paths)
        self.assertEqual(res.status, "refused", res)
        self.assertIn("not what this install_tools writes", res.detail)

    def test_the_platform_functions_route_muse_to_the_plugin_code(self) -> None:
        plat = T.PLATFORMS["muse"]
        self.assertEqual(plat.kind, "plugin")
        self.assertNotIn("muse", T.SUPPORTED_KEYS)
        self.assertEqual(T.plan_platform(plat, self.paths).action, "unsupported")
        self.assertEqual(T.apply_platform(plat, self.paths).status, "refused")        # no bundle yet
        self.install(())
        self.assertEqual(T.apply_platform(plat, self.paths).status, "unchanged")
        self.assertIn(("muse", "muse"), T.probe_formats())

    # -- verify

    def test_verify_muse_runs_the_hook_the_way_muse_does(self) -> None:
        self.install(())
        checks = T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)
        self.assertAllPass([c for c in checks if c.status != "SKIP"])
        names = {c.name: c for c in checks}
        for n in ("plugin.json", "hooks/lane-guard.sh", "shim muse-seat", "hook command", "deny/hook-env", "deny-Bash/hook-env",
                  "allow/hook-env", "other-tool/hook-env", "muse validate", "owner actions"):
            self.assertIn(n, names)
        self.assertIn("hookSpecificOutput", names["deny/hook-env"].detail)
        self.assertEqual(names["allow/hook-env"].status, "PASS")
        self.assertEqual(names["muse validate"].status, "PASS")
        self.assertEqual(names["owner actions"].status, "SKIP")
        self.assertEqual(self.muse_calls(), [f"plugins validate {self.paths.muse_bundle} --json"])

    def test_validate_runs_with_a_throwaway_home_so_muses_own_directory_is_never_touched(self) -> None:
        self.install(())
        self.seed(".config/muse/settings.json")
        # The installed hook runs with an empty environment, so on Linux (/usr/bin/python3 has no
        # pycache prefix) Python writes bytecode beside the installed module.  The product allows
        # that on purpose (the tree digest and the staleness scan ignore __pycache__); only the rest
        # of the home must stay untouched.
        def without_bytecode(snap: dict) -> dict:
            return {k: v for k, v in snap.items() if "__pycache__" not in k.split(os.sep)}
        before = without_bytecode(snapshot(self.home))
        T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)
        self.assertEqual(without_bytecode(snapshot(self.home)), before)
        homes = Path(str(self.muse_log) + ".home").read_text().splitlines()
        self.assertEqual(len(homes), 1)
        self.assertNotIn(homes[0], (str(self.home), os.path.expanduser("~")))
        self.assertTrue(os.path.basename(homes[0]).startswith("lane-muse-validate-"))
        self.assertFalse(os.path.exists(homes[0]), "the throwaway home is removed afterwards")

    def test_validate_is_confined_and_cannot_update_muse(self) -> None:
        # The real muse is a launcher that finds its install dir from its own path, not from HOME.  Unless
        # MUSE_NO_AUTO_UPDATE=1, it stamps that dir and starts a background self-update; MUSE_LAUNCHER_INSTALL=1
        # installs before anything else; XDG_CONFIG_HOME and the MUSE_ variables would point it at the real config.
        # This fake behaves like that, so a leak shows up as a file beside it.
        self.install(())
        env_log = self.tmp / "validate-env.log"
        fake = self.fakebin / "muse"
        fake.write_text("#!/bin/sh\n"
                        f"env > {shlex.quote(str(env_log))}\n"
                        'd=$(dirname "$0")\n'
                        '[ "${MUSE_LAUNCHER_INSTALL-}" = 1 ] && : > "$d/installed-by-validate"\n'
                        '[ "${MUSE_NO_AUTO_UPDATE-}" = 1 ] || : > "$d/.muse-update-checked-at"\n'
                        "echo '{\"valid\": true}'\n", encoding="utf-8")
        os.chmod(fake, 0o755)
        real_xdg = self.tmp / "real-xdg"
        leaked = {"XDG_CONFIG_HOME": str(real_xdg / "config"), "XDG_DATA_HOME": str(real_xdg / "data"),
                  "XDG_CACHE_HOME": str(real_xdg / "cache"), "XDG_STATE_HOME": str(real_xdg / "state"),
                  "MUSE_LAUNCHER_INSTALL": "1", "MUSE_SYNC_UPDATE": "1", "MUSE_CHANNEL": "muse-canary",
                  "MUSE_LOGIN": "1", "MUSE_EXPERIMENTAL_PLUGINS": "1"}
        with mock.patch.dict(os.environ, leaked):
            checks = {c.name: c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)}
        self.assertEqual(checks["muse validate"].status, "PASS", checks["muse validate"].line())
        self.assertEqual(sorted(p.name for p in self.fakebin.iterdir()), ["muse"])      # no stamp, no install
        self.assertFalse(real_xdg.exists())
        seen = dict(ln.split("=", 1) for ln in env_log.read_text().splitlines() if "=" in ln)
        scratch = seen["HOME"]
        self.assertTrue(os.path.basename(scratch).startswith("lane-muse-validate-"), scratch)
        self.assertFalse(os.path.exists(scratch), "the throwaway home is removed afterwards")
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
                     "TMPDIR", "MUSE_AUTH_PATH"):
            with self.subTest(name):
                self.assertTrue(seen.get(name, "").startswith(scratch + os.sep), (name, seen.get(name)))
        self.assertEqual((seen.get("MUSE_NO_AUTO_UPDATE"), seen.get("MUSE_LOGIN")), ("1", "0"))
        for name in ("MUSE_LAUNCHER_INSTALL", "MUSE_SYNC_UPDATE", "MUSE_CHANNEL"):
            self.assertNotIn(name, seen)
        self.assertEqual(seen.get("MUSE_EXPERIMENTAL_PLUGINS"), "1")                  # a feature switch, not a path
        self.assertEqual(seen.get("PATH"), os.environ.get("PATH"))

    def test_the_probes_run_with_an_empty_environment(self) -> None:
        self.install(())
        seen: list = []
        real = T.run_argv

        def spy(argv, stdin, env, timeout, cwd=None):
            seen.append((list(argv), dict(env), cwd))
            return real(argv, stdin, env, timeout, cwd)

        with mock.patch.object(T, "run_argv", side_effect=spy):
            T.verify_muse(self.paths, timeout=TIMEOUT)                                 # the default: an empty environment
        hook_runs = [s for s in seen if s[0][:1] == ["/bin/sh"]]
        self.assertEqual(len(hook_runs), len(T._muse_cases()))                          # six: two deny, a ps, an allow, two other tools
        self.assertEqual(len(hook_runs), 6)
        for argv, env, cwd in hook_runs:
            self.assertEqual((argv, env, cwd), (T.muse_hook_argv(self.paths.stable), {}, "/"))

    def test_verify_muse_without_muse_installed_is_a_skip_not_a_failure(self) -> None:
        self.install(())
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.assertIsNone(shutil.which("muse"))
            checks = T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)
        by = {c.name: c for c in checks}
        self.assertEqual(by["muse validate"].status, "SKIP")
        self.assertNotIn("FAIL", self.statuses(checks), "\n".join(c.line() for c in checks))
        self.assertEqual(self.muse_calls(), [])

    def test_verify_muse_reports_a_bundle_that_muse_rejects(self) -> None:
        self.install(())
        self.set_fake_muse('{"error": {"code": "unsupported-field"}}', 1)
        checks = {c.name: c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)}
        self.assertEqual(checks["muse validate"].status, "FAIL")
        self.set_fake_muse('{"valid": false}', 0)
        checks = {c.name: c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)}
        self.assertEqual(checks["muse validate"].status, "FAIL")
        self.set_fake_muse("not json at all", 0)
        checks = {c.name: c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)}
        self.assertEqual(checks["muse validate"].status, "WARN")                    # no verdict is not a failure

    def test_verify_muse_fails_when_the_wrapper_cannot_deny(self) -> None:
        self.install(())
        wrapper = Path(self.paths.muse_wrapper)
        original = wrapper.read_bytes()
        for label, text in (("allows everything", "#!/bin/sh\nexit 0\n"),
                            ("prints junk", "#!/bin/sh\necho junk\n"),
                            ("exits non-zero", "#!/bin/sh\nexit 3\n"),
                            ("hangs", "#!/bin/sh\nsleep 30\n")):
            with self.subTest(label):
                wrapper.write_text(text)
                os.chmod(wrapper, 0o755)
                checks = T.verify_muse(self.paths, timeout=2.0 if label == "hangs" else TIMEOUT, minimal_path=MINIMAL)
                self.assertFails(checks)
                self.assertFails(checks, "differs")
                self.assertTrue(any(c.name.startswith("deny") and c.status == "FAIL" for c in checks), label)
        wrapper.write_bytes(original)
        os.chmod(wrapper, 0o755)
        self.assertAllPass([c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL) if c.status != "SKIP"])

    def test_verify_muse_fails_when_the_manifest_is_wrong(self) -> None:
        self.install(())
        manifest = Path(self.paths.muse_manifest)
        original = manifest.read_text()
        doc = json.loads(original)
        doc["capabilities"]["hooks"][0]["matcher"] = "bash"
        manifest.write_text(json.dumps(doc))
        self.assertFails(T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL), "matcher")
        doc = json.loads(original)
        doc["capabilities"]["hooks"][0]["command"] = ["sh", "hooks/lane-guard.sh"]           # relative: not found with no cwd
        manifest.write_text(json.dumps(doc))
        checks = T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)
        self.assertFails(checks, "hook command")
        self.assertFails(checks, "not run")
        manifest.write_text("{nope")
        self.assertFails(T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL), "cannot read the hook command")
        manifest.write_text(original)
        self.assertAllPass([c for c in T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL) if c.status != "SKIP"])

    def test_verify_muse_fails_when_the_guard_behind_it_is_broken_or_missing(self) -> None:
        self.install(())
        guard = Path(self.paths.package_dir) / "guard.py"
        guard.write_text(BROKEN_GUARD)
        self.assertFails(T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL), "no output")
        guard.unlink()
        os.rename(self.paths.stable, self.paths.stable + ".gone")
        checks = T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=MINIMAL)
        self.assertFails(checks, "missing")
        self.assertNotIn("PASS", {c.status for c in checks if c.name.startswith("deny")})

    def test_the_cli_verify_default_covers_muse_and_a_named_muse_works(self) -> None:
        self.seed_platforms()
        self.install()
        rc, out, err = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertRegex(out, r"PASS\s+muse\s+deny/hook-env")
        self.assertRegex(out, r"SKIP\s+muse\s+owner actions")
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), *MINIMAL_ARGS, "muse")
        self.assertEqual(rc, 0, out)
        self.assertIn("-> healthy", out)
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), *MINIMAL_ARGS, "--strict", "muse")
        self.assertEqual(rc, 0, out)                                               # nothing the owner has to clear shows as a WARN

    def test_the_cli_verify_of_muse_fails_without_the_bundle(self) -> None:
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "muse")
        self.assertEqual(rc, 1, out)
        self.assertIn("NOT HEALTHY", out)

    def test_the_self_test_covers_muse(self) -> None:
        lines: list = []
        rc = T.self_test(lines.append, timeout=TIMEOUT, minimal_path=False, source=self.source)
        text = "\n".join(lines)
        self.assertEqual(rc, 0, text)
        self.assertIn("FAIL  muse", text)


# --------------------------------------------------------------------------- the fleet hook: lane guard plus secret guard

class FleetHookStableCopyTests(World):
    """fleet-guard-hook, the secret guard copy and Clutch's hooks.json travel in the stable dir with everything else."""

    def test_build_files_carries_the_fleet_hook_the_secret_guard_and_the_clutch_hooks_file(self) -> None:
        files = T.build_files(self.source, self.paths)
        self.assertIn("fleet-guard-hook", files)
        self.assertEqual(files["fleet_lanes/secret_guard.py"],
                         (Path(self.source.scripts_dir) / "hooks" / "secret-guard-pretooluse.py").read_bytes())
        self.assertEqual(files["clutch/hooks.json"].decode(), T.render_clutch_hooks(self.paths.stable))
        self.assertEqual(files["fleet-guard-hook"].decode(), T.render_fleet_hook_shim())
        for rel in ("fleet-guard-hook", "lane-guard-hook"):
            self.assertEqual(T.file_mode(rel), 0o755)
        self.assertEqual(T.file_mode("clutch/hooks.json"), 0o644)

    def test_a_source_without_the_secret_guard_is_refused(self) -> None:
        (Path(self.source.scripts_dir) / "hooks" / "secret-guard-pretooluse.py").unlink()
        with self.assertRaises(T.Refused) as cm:
            T.build_files(self.source, self.paths)
        self.assertIn("secret guard", str(cm.exception))

    def test_the_fleet_shim_has_no_prefilter_and_runs_the_fleet_module(self) -> None:
        text = T.render_fleet_hook_shim()
        self.assertNotIn("case $_lg_in", text)
        self.assertIn("fleet_lanes.fleet_guard_hook", text)
        self.assertNotIn("fleet_lanes.lane_guard_hook", text)
        self.assertIn(T.GENERATED_MARK, text)
        self.assertEqual(T.render_hook_shim(), T.render_hook_shim(T.read_guard_facts()))      # the lane shim is unchanged

    def test_both_names_are_ours_and_lookalikes_are_not(self) -> None:
        for cmd in ("/h/apps/lane-tools/fleet-guard-hook --format kimi", "'/h x/fleet-guard-hook' --format claude"):
            self.assertTrue(T.is_ours(cmd), cmd)
        for cmd in ("fleet-guard-hooks-extra", "my-fleet-guard-hook-2", "ps aux"):
            self.assertFalse(T.is_ours(cmd), cmd)

    def test_the_installed_tree_has_the_new_files_and_stays_owned(self) -> None:
        self.install(())
        for rel in ("fleet-guard-hook", "clutch/hooks.json", "fleet_lanes/secret_guard.py", "fleet_lanes/fleet_guard_hook.py"):
            self.assertTrue((Path(self.paths.stable) / rel).is_file(), rel)
        self.assertTrue(os.access(self.paths.fleet_shim, os.X_OK))
        plan = T.plan_tools(self.source, self.paths)
        self.assertEqual(plan.problems, [])
        self.assertEqual(plan.state, "current")
        self.assertAllPass(T.verify_tools(self.paths, timeout=TIMEOUT))

    def test_verify_tools_fails_when_the_fleet_shim_or_the_secret_guard_is_gone(self) -> None:
        self.install(())
        Path(self.paths.fleet_shim).write_text("#!/bin/sh\nexit 0\n")
        self.assertFails(T.verify_tools(self.paths, timeout=TIMEOUT), "fleet-guard-hook")
        self.install(())
        os.unlink(os.path.join(self.paths.package_dir, T.SECRET_GUARD_MODULE))
        checks = T.verify_tools(self.paths, timeout=TIMEOUT)
        self.assertTrue(any(c.name == "secret guard" and c.status == "FAIL" for c in checks), [c.line() for c in checks])

    def test_the_staged_copy_is_probed_with_the_secret_case(self) -> None:
        calls: list = []
        real = T.probe_command

        def spy(command, **kw):
            calls.append((command, kw.get("secret", False), kw["fmt"]))
            return real(command, **kw)
        with mock.patch.object(T, "probe_command", side_effect=spy):
            r = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertTrue(r.ok, r)
        fleet = [c for c in calls if "fleet-guard-hook" in c[0]]
        self.assertEqual({c[2] for c in fleet}, {"muse", "claude", "kimi"})
        self.assertTrue(all(c[1] for c in fleet))

    def test_a_staged_fleet_shim_that_cannot_deny_a_ps_is_never_swapped_in(self) -> None:
        guard_hook = Path(self.source.scripts_dir) / "fleet_lanes" / "fleet_guard_hook.py"
        guard_hook.write_text("import sys\nsys.exit(0)\n")
        r = T.apply_tools(self.source, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual(r.status, "refused", r)
        self.assertFalse(os.path.exists(self.paths.stable))

    def test_probe_command_secret_case_fails_a_lane_only_command(self) -> None:
        self.install(())
        lane_only = T.hook_command(self.paths, "claude")
        checks = T.probe_command(lane_only, target="t", fmt="claude", shape="claude", home=str(self.home),
                                 timeout=TIMEOUT, minimal_path=False, secret=True)
        self.assertEqual([c.name for c in checks if c.status == "FAIL"], ["secret/login-PATH"])
        both = T.probe_command(T.fleet_hook_command(self.paths, "claude"), target="t", fmt="claude", shape="claude",
                               home=str(self.home), timeout=TIMEOUT, minimal_path=False, secret=True)
        self.assertEqual({c.status for c in both}, {"PASS"})

    def test_the_exit2_contract_is_checked_as_exit_two_with_stderr(self) -> None:
        ok = T.RunResult(2, b"", b"blocked `ps` here")
        self.assertIsNone(T.check_exit2_output(ok, "`ps`"))
        self.assertIn("expected 2", T.check_exit2_output(T.RunResult(0, b"", b""), "`ps`"))
        self.assertIn("stdout is not empty", T.check_exit2_output(T.RunResult(2, b"{}", b"`ps`"), "`ps`"))
        self.assertIn("stderr does not name", T.check_exit2_output(T.RunResult(2, b"", b"nope"), "`ps`"))


class MuseFleetGuardTests(World):
    """Muse Code's plugin runs the fleet hook, so it denies a ps as well as a temp checkout."""

    def setUp(self) -> None:
        super().setUp()
        self.install(())

    def test_the_wrapper_hands_shell_calls_to_the_fleet_hook(self) -> None:
        text = Path(self.paths.muse_wrapper).read_text()
        self.assertIn(f"_lg_guard={shlex.quote(self.paths.fleet_shim)}", text)
        self.assertNotIn("lane-guard-hook", text)

    def test_the_manifest_names_both_guards(self) -> None:
        doc = json.loads(Path(self.paths.muse_manifest).read_text())
        self.assertEqual(doc["displayName"], "Fleet Guards")
        self.assertIn("secret guard", doc["description"])
        self.assertEqual(doc["name"], T.MUSE_PLUGIN_ID, "the plugin id is unchanged, so an approval is not lost")

    def test_verify_muse_runs_the_secret_and_the_other_tool_probes(self) -> None:
        checks = T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertAllPass([c for c in checks if c.status != "SKIP"])
        names = {c.name for c in checks}
        self.assertTrue({"secret/hook-env", "other-tool-ps/hook-env", "shim fleet-guard-hook"} <= names, names)

    def test_a_muse_install_whose_fleet_hook_allows_everything_fails_verify(self) -> None:
        Path(self.paths.fleet_shim).write_text("#!/bin/sh\nexit 0\n")
        checks = T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=False)
        failed = {c.name for c in checks if c.status == "FAIL"}
        self.assertTrue({"deny/hook-env", "secret/hook-env", "shim fleet-guard-hook"} <= failed, failed)

    def test_a_missing_secret_guard_fails_only_the_secret_probe(self) -> None:
        os.unlink(os.path.join(self.paths.package_dir, T.SECRET_GUARD_MODULE))
        checks = T.verify_muse(self.paths, timeout=TIMEOUT, minimal_path=False)
        failed = {c.name for c in checks if c.status == "FAIL"}
        self.assertEqual(failed, {"secret/hook-env"})


# --------------------------------------------------------------------------- clutch and kimi: a marked block

FAKE_DSH = '''#!{python}
import os, sys
home = os.environ["DSH_HOME"]
path = os.path.join(home, "cordis.patch.yml")
patch = open(path).read() if os.path.exists(path) else ""
if os.path.exists({flag!r}):
    sys.stderr.write("boom: broken on purpose\\n")
    sys.exit(1)
if not any(line.startswith("- ") for line in patch.splitlines()):
    sys.exit(1)                      # the real engine exits on an empty or comment-only patch
if not os.path.isdir(os.path.join(home, "profiles")):
    sys.exit(2)
print("@deepseek-ai/dsh-base")
if "dsh-hooks-claude-code" in patch:
    print("@deepseek-ai/dsh-hooks-claude-code")
if "profile" in sys.argv:
    pass
'''


class ClutchBlockTests(World):
    def setUp(self) -> None:
        super().setUp()
        self.flag = self.tmp / "dsh-fail-flag"
        self.dsh_dir = self.home / "apps" / "clutch-runtime" / "node_modules" / ".bin"
        self.dsh_dir.mkdir(parents=True)
        dsh = self.dsh_dir / "dsh"
        dsh.write_text(FAKE_DSH.format(python=sys.executable, flag=str(self.flag)))
        os.chmod(dsh, 0o755)
        web = self.home / ".clutch" / "dsh" / "profiles" / "web"
        web.mkdir(parents=True)
        (web / "cordis.yml").write_text("plugins: []\n")
        (self.home / ".clutch" / "dsh" / "profiles" / "node_modules").mkdir()
        self.plat = T.PLATFORMS["clutch"]
        self.install(())

    @property
    def patch(self) -> Path:
        return Path(self.paths.clutch_patch)

    def apply(self, **kw):
        return T.apply_platform(self.plat, self.paths, now=lambda: STAMP, timeout=TIMEOUT, minimal_path=False, **kw)

    def test_the_platform_is_not_part_of_all_or_the_config_platforms(self) -> None:
        self.assertNotIn("clutch", T.SUPPORTED_KEYS)
        self.assertEqual(self.plat.kind, "block")
        self.assertIn("clutch", T.PLATFORM_ORDER)

    def test_plan_is_read_only_and_shows_the_block(self) -> None:
        before = snapshot(self.tmp)
        rc, out, err = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, "clutch")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn("# fleet:begin hooks-fleet-guards (managed by fleet_lanes.install_tools)", out)
        self.assertIn("name: '@deepseek-ai/dsh-hooks-claude-code'", out)
        self.assertIn(f"configPath: {json.dumps(self.paths.clutch_hooks)}", out)
        self.assertIn("matcher", out)                                       # the hooks.json is shown too
        self.assertIn("UNVERIFIED", out)
        self.assertTrue(all(ord(c) < 128 for c in out))

    def test_apply_creates_the_patch_with_a_header_and_one_block(self) -> None:
        r = self.apply()
        self.assertEqual(r.status, "added", r)
        text = self.patch.read_text()
        self.assertTrue(text.startswith("# Home-level cordis patch"))
        self.assertEqual(text.count("# fleet:begin hooks-fleet-guards"), 1)
        self.assertIn("- insert:\n    - id: fleet-hooks-guards\n", text)
        self.assertIn("mount", r.detail)
        hooks = json.loads(Path(self.paths.clutch_hooks).read_text())
        entry = hooks["hooks"]["PreToolUse"][0]
        self.assertEqual(entry["matcher"], "bash")
        self.assertEqual(entry["hooks"][0]["command"], T.fleet_hook_command(self.paths, "claude"))
        self.assertEqual(stat.S_IMODE(self.patch.stat().st_mode), 0o600)

    def test_a_second_apply_changes_nothing(self) -> None:
        self.apply()
        before = snapshot(self.tmp)
        r = self.apply()
        self.assertEqual(r.status, "unchanged", r)
        self.assertEqual(snapshot(self.tmp), before)

    def test_verify_passes_and_names_the_engine_proof(self) -> None:
        self.apply()
        checks = T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertAllPass([c for c in checks if c.status != "SKIP"])
        names = {c.name for c in checks}
        self.assertTrue({"block", "hooks.json", "hook command", "dsh --dump-config", "secret/login-PATH"} <= names, names)
        self.assertEqual([c.status for c in checks if c.name == "engine restart"], ["SKIP"])

    def test_the_owners_patch_entries_survive_and_a_backup_is_made(self) -> None:
        mine = "# my layer\n- id: typert-gateway\n  config:\n    websocketHeartbeatIntervalMs: 15000\n"
        self.patch.write_text(mine)
        os.chmod(self.patch, 0o640)
        r = self.apply()
        self.assertEqual(r.status, "added", r)
        self.assertTrue(self.patch.read_text().startswith(mine + "\n# fleet:begin"))
        self.assertEqual(stat.S_IMODE(self.patch.stat().st_mode), 0o640)
        backups = self.backups(self.patch.parent)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), mine)

    def test_the_engine_rejecting_the_patch_refuses_the_write(self) -> None:
        mine = "- id: a\n"
        self.patch.write_text(mine)
        self.flag.write_text("x")
        r = self.apply()
        self.assertEqual(r.status, "refused", r)
        self.assertIn("pinned engine does not accept", r.detail)
        self.assertEqual(self.patch.read_text(), mine)
        self.assertEqual(self.backups(self.patch.parent), [])

    def test_without_the_engine_the_write_is_refused_unless_the_owner_says_so(self) -> None:
        os.unlink(self.dsh_dir / "dsh")
        r = self.apply()
        self.assertEqual(r.status, "refused", r)
        self.assertIn("--no-dsh-check", r.detail)
        self.assertFalse(self.patch.exists())
        r = self.apply(check_dsh=False)
        self.assertEqual(r.status, "added", r)
        self.assertIn("NOT checked with the engine", r.detail)

    def test_an_explicit_engine_path_is_used(self) -> None:
        other = self.tmp / "other-dsh"
        other.write_text(FAKE_DSH.format(python=sys.executable, flag=str(self.flag)))
        os.chmod(other, 0o755)
        os.unlink(self.dsh_dir / "dsh")
        self.assertEqual(self.apply(dsh_bin=str(other)).status, "added")

    def test_a_clutch_that_is_not_installed_is_skipped(self) -> None:
        shutil.rmtree(self.home / ".clutch")
        r = self.apply()
        self.assertEqual(r.status, "skipped", r)
        self.assertFalse((self.home / ".clutch").exists())

    def test_a_missing_hooks_file_or_shim_means_run_apply_tools_first(self) -> None:
        Path(self.paths.clutch_hooks).write_text("{}\n")
        self.assertIn("apply tools", self.apply().detail)
        self.install(())
        os.chmod(self.paths.fleet_shim, 0o644)
        r = self.apply()
        self.assertEqual(r.status, "refused")
        self.assertIn("apply tools", r.detail)

    def test_a_patch_that_is_not_a_list_or_has_broken_markers_is_refused(self) -> None:
        self.patch.write_text("plugins:\n  - a\n")
        self.assertIn("not a top-level YAML list", self.apply().detail)
        self.patch.write_text("- id: a\n# fleet:begin hooks-fleet-guards (managed by x)\n- insert: []\n")
        self.assertEqual(self.apply().status, "refused")

    def test_a_lane_only_shim_is_refused_because_it_cannot_deny_a_ps(self) -> None:
        shutil.copy(self.paths.hook_shim, self.paths.fleet_shim)
        r = self.apply()
        self.assertEqual(r.status, "refused", r)
        self.assertIn("fails its probes", r.detail)

    def test_an_installed_but_unwired_clutch_is_a_skip_unless_named(self) -> None:
        checks = T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual([c.status for c in checks], ["SKIP"])
        self.assertIn("not wired", checks[0].detail)
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path")
        self.assertRegex(out, r"SKIP\s+clutch\s+block\s+not wired")
        self.assertRegex(out, r"SKIP\s+kimi\s+platform")
        self.assertNotIn("NOT HEALTHY", out, "an opt-in platform nobody wired must not make a bare verify fail")
        self.assertEqual(rc, 0, out)
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "clutch")
        self.assertEqual(rc, 1, out)
        self.assertIn("(named explicitly)", out)
        self.patch.write_text("# no block here\n- id: a\n")
        self.assertEqual([c.status for c in T.verify_block(self.plat, self.paths)], ["SKIP"])

    def test_verify_fails_with_an_edited_hooks_file(self) -> None:
        self.apply()
        Path(self.paths.clutch_hooks).write_text('{"hooks": {}}\n')
        checks = T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertTrue(any(c.name == "hooks.json" and c.status == "FAIL" for c in checks))
        self.assertTrue(any(c.name == "hook command" and c.status == "FAIL" for c in checks))

    def test_verify_fails_when_the_shim_allows_everything(self) -> None:
        self.apply()
        Path(self.paths.fleet_shim).write_text("#!/bin/sh\nexit 0\n")
        checks = T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False)
        failed = {c.name for c in checks if c.status == "FAIL"}
        self.assertTrue({"deny/login-PATH", "secret/login-PATH"} <= failed, failed)

    def test_verify_fails_when_the_engine_stops_accepting_the_file(self) -> None:
        self.apply()
        self.flag.write_text("x")
        checks = T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertTrue(any(c.name == "dsh --dump-config" and c.status == "FAIL" for c in checks))

    def test_verify_skips_a_tool_that_is_not_installed(self) -> None:
        shutil.rmtree(self.home / ".clutch")
        self.assertEqual([c.status for c in T.verify_block(self.plat, self.paths)], ["SKIP"])

    def test_the_command_line_applies_tools_first_and_wires_clutch(self) -> None:
        shutil.rmtree(self.paths.stable)
        base = ["--home", str(self.home), "--source", self.source.scripts_dir, "--registry", self.source.registry,
                "--sha", SHA, "--timeout", str(TIMEOUT), "--no-minimal-path"]
        rc, out, err = run_main("apply", *base, "clutch")
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"INSTALLED\s+tools")
        self.assertRegex(out, r"ADDED\s+clutch")
        self.assertIn("NEXT       clutch", out)
        self.assertIn("pm2 restart clutch-web", out)
        self.assertIn("verify: ", out)
        self.assertTrue(self.patch.is_file())

    def test_the_command_line_honours_no_dsh_check(self) -> None:
        os.unlink(self.dsh_dir / "dsh")
        base = ["--home", str(self.home), "--source", self.source.scripts_dir, "--registry", self.source.registry,
                "--sha", SHA, "--timeout", str(TIMEOUT), "--no-minimal-path", "--no-verify"]
        rc, out, _ = run_main("apply", *base, "clutch")
        self.assertEqual(rc, 1, out)
        rc, out, _ = run_main("apply", *base, "--no-dsh-check", "clutch")
        self.assertEqual(rc, 0, out)


KIMI_CONFIG = '''default_model = "k2"

[providers.kimi]
type = "kimi"
base_url = "https://api.example.invalid/v1"

[[hooks]]
event = "Notification"
command = "terminal-notifier -message done"
'''


class KimiBlockTests(World):
    def setUp(self) -> None:
        super().setUp()
        keep = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not os.path.exists(os.path.join(d, "kimi"))]
        scrub = mock.patch.dict(os.environ, {"PATH": os.pathsep.join(keep)})      # never reach the real kimi
        scrub.start()
        self.addCleanup(scrub.stop)
        (self.home / ".kimi-code").mkdir()
        self.plat = T.PLATFORMS["kimi"]
        self.install(())

    @property
    def cfg(self) -> Path:
        return Path(self.paths.kimi_config)

    def apply(self):
        return T.apply_platform(self.plat, self.paths, now=lambda: STAMP, timeout=TIMEOUT, minimal_path=False)

    def fake_kimi(self, body: str, rc: int = 0, log: Path | None = None) -> Path:
        d = self.tmp / "fake-kimi-bin"
        d.mkdir(exist_ok=True)
        k = d / "kimi"
        record = f"echo \"$@\" >> {shlex.quote(str(log))}; env | sort >> {shlex.quote(str(log) + '.env')}\n" if log else ""
        k.write_text("#!/bin/sh\n" + record + f"echo {shlex.quote(body)}\nexit {rc}\n")
        os.chmod(k, 0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{d}:{os.environ.get('PATH', '/usr/bin:/bin')}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return k

    def test_apply_appends_one_hooks_block_that_parses(self) -> None:
        self.cfg.write_text(KIMI_CONFIG)
        r = self.apply()
        self.assertEqual(r.status, "added", r)
        text = self.cfg.read_text()
        self.assertTrue(text.startswith(KIMI_CONFIG + "\n# fleet:begin hooks-fleet-guards"))
        import tomllib
        hooks = tomllib.loads(text)["hooks"]
        self.assertEqual(len(hooks), 2, "the owner's [[hooks]] entry is still there")
        mine = hooks[1]
        self.assertEqual(sorted(mine), ["command", "event", "matcher", "timeout"], "Kimi allows exactly these four fields")
        self.assertEqual((mine["event"], mine["matcher"], mine["timeout"]), ("PreToolUse", "Bash", 5))
        self.assertEqual(mine["command"], T.fleet_hook_command(self.paths, "kimi"))
        self.assertTrue(mine["command"].endswith("--format kimi"))
        backups = self.backups(self.cfg.parent)
        self.assertEqual([b.read_text() for b in backups], [KIMI_CONFIG])

    def test_a_missing_config_is_created(self) -> None:
        r = self.apply()
        self.assertEqual(r.status, "added", r)
        self.assertIn("(new file)", r.detail)
        self.assertEqual(self.cfg.read_text().count("[[hooks]]"), 1)
        self.assertEqual(self.backups(self.cfg.parent), [])

    def test_a_second_apply_changes_nothing(self) -> None:
        self.cfg.write_text(KIMI_CONFIG)
        self.apply()
        before = snapshot(self.tmp)
        self.assertEqual(self.apply().status, "unchanged")
        self.assertEqual(snapshot(self.tmp), before)

    def test_a_changed_command_is_replaced_in_place(self) -> None:
        self.cfg.write_text(KIMI_CONFIG)
        self.apply()
        self.cfg.write_text(self.cfg.read_text().replace(self.paths.fleet_shim, "/old/fleet-guard-hook") + "tail = 1\n")
        r = self.apply()
        self.assertEqual(r.status, "updated", r)
        text = self.cfg.read_text()
        self.assertNotIn("/old/fleet-guard-hook", text)
        self.assertTrue(text.endswith("tail = 1\n"))

    def test_a_top_level_hooks_key_is_refused_because_the_file_would_not_load(self) -> None:
        text = 'hooks = []\n'
        self.cfg.write_text(text)
        r = self.apply()
        self.assertEqual(r.status, "refused", r)
        self.assertEqual(self.cfg.read_text(), text)

    def test_read_only_symlinked_and_non_text_configs_are_refused(self) -> None:
        self.cfg.write_text(KIMI_CONFIG)
        os.chmod(self.cfg, 0o444)
        self.assertIn("read-only", self.apply().detail)
        os.chmod(self.cfg, 0o644)
        self.cfg.unlink()
        target = self.tmp / "elsewhere.toml"
        target.write_text("x = 1\n")
        os.symlink(target, self.cfg)
        self.assertIn("symlink", self.apply().detail)
        self.cfg.unlink()
        self.cfg.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(self.apply().status, "refused")

    def test_kimi_doctor_runs_on_a_scratch_copy_with_auto_install_off(self) -> None:
        log = self.tmp / "kimi-calls.log"
        self.fake_kimi("All checked config files are valid.", 0, log)
        self.cfg.write_text(KIMI_CONFIG)
        r = self.apply()
        self.assertEqual(r.status, "added", r)
        self.assertIn("doctor accepts", r.detail)
        self.assertEqual(log.read_text().strip(), "doctor")
        env = (Path(str(log) + ".env")).read_text()
        kh = [ln for ln in env.splitlines() if ln.startswith("KIMI_CODE_HOME=")][0].split("=", 1)[1]
        self.assertNotEqual(os.path.realpath(kh), os.path.realpath(self.home / ".kimi-code"))
        self.assertNotIn(f"HOME={self.home}\n", env + "\n")

    def test_kimi_doctor_rejecting_the_new_file_refuses_the_write(self) -> None:
        self.fake_kimi("config.toml: unknown field", 1)
        self.cfg.write_text(KIMI_CONFIG)
        r = self.apply()
        self.assertEqual(r.status, "refused", r)
        self.assertIn("doctor rejects", r.detail)
        self.assertEqual(self.cfg.read_text(), KIMI_CONFIG)

    def test_kimi_not_installed_is_skipped(self) -> None:
        shutil.rmtree(self.home / ".kimi-code")
        self.assertEqual(self.apply().status, "skipped")
        self.assertFalse((self.home / ".kimi-code").exists())

    def test_verify_proves_the_exit2_contract_and_the_secret_case(self) -> None:
        self.apply()
        checks = T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertAllPass([c for c in checks if c.status != "SKIP"])
        names = {c.name for c in checks}
        self.assertTrue({"block", "hook command", "deny/login-PATH", "secret/login-PATH", "allow/login-PATH", "config.toml"} <= names, names)
        self.assertEqual([c.status for c in checks if c.name == "fires"], ["SKIP"])

    def test_verify_fails_when_the_hook_stops_blocking(self) -> None:
        self.apply()
        Path(self.paths.fleet_shim).write_text("#!/bin/sh\nexit 0\n")
        failed = {c.name for c in T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False) if c.status == "FAIL"}
        self.assertTrue({"deny/login-PATH", "secret/login-PATH"} <= failed, failed)

    def test_an_installed_but_unwired_kimi_is_a_skip_unless_named(self) -> None:
        self.cfg.write_text(KIMI_CONFIG)
        checks = T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False)
        self.assertEqual([c.status for c in checks], ["SKIP"])
        self.assertIn("not wired", checks[0].detail)
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path", "kimi")
        self.assertEqual(rc, 1, out)
        self.assertIn("(named explicitly)", out)

    def test_verify_fails_with_an_edited_block(self) -> None:
        self.cfg.write_text(KIMI_CONFIG)
        self.apply()
        self.cfg.write_text(self.cfg.read_text().replace('matcher = "Bash"', 'matcher = "Nothing"'))
        self.assertFails(T.verify_block(self.plat, self.paths, timeout=TIMEOUT, minimal_path=False), "not what this install_tools writes")

    def test_the_command_line_wires_kimi_after_apply_tools(self) -> None:
        shutil.rmtree(self.paths.stable)
        base = ["--home", str(self.home), "--source", self.source.scripts_dir, "--registry", self.source.registry,
                "--sha", SHA, "--timeout", str(TIMEOUT), "--no-minimal-path"]
        rc, out, err = run_main("apply", *base, "kimi")
        self.assertEqual(rc, 0, out + err)
        self.assertRegex(out, r"ADDED\s+kimi")
        self.assertIn("NEXT       kimi", out)

    def test_plan_shows_the_block_and_writes_nothing(self) -> None:
        before = snapshot(self.tmp)
        rc, out, err = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA, "kimi")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertIn("[[hooks]]", out)
        self.assertIn("--format kimi", out)
        self.assertIn("UNVERIFIED", out)

    def test_default_plan_and_verify_list_both_block_platforms_without_failing_on_absent_tools(self) -> None:
        shutil.rmtree(self.home / ".kimi-code")
        rc, out, _ = run_main("plan", "--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA)
        self.assertEqual(rc, 0, out)
        self.assertIn("== clutch", out)
        self.assertIn("== kimi", out)
        rc, out, _ = run_main("verify", "--home", str(self.home), "--timeout", str(TIMEOUT), "--no-minimal-path")
        self.assertRegex(out, r"SKIP\s+clutch\s+platform")
        self.assertRegex(out, r"SKIP\s+kimi\s+platform")


if __name__ == "__main__":
    unittest.main()
