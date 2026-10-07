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
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import guard as G
from fleet_lanes import install_tools as T
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


class World(unittest.TestCase):
    """A fake home, a small source tree, and helpers.  Nothing here touches the real home."""

    def setUp(self) -> None:
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

    def test_muse_is_never_written(self) -> None:
        self.seed(".config/muse/settings.json")
        before = snapshot(self.home)
        res = self.apply("muse")
        self.assertEqual(res.status, "unsupported", res)
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
        self.assertEqual(names, sorted(list(T.REQUIRED_MODULES) + ["lane.py"]))        # no tests, no __pycache__
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
                       "--format antigravity", "--format cursor", "== muse", "UNVERIFIED", "failClosed", "[unsupported]",
                       "lane-guard.json", "/hooks in Codex"):
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
        self.assertEqual(run_main(*self.plan_args("muse"))[0], 1)                 # named explicitly
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
        self.assertFails(self.verify("muse"), "UNSUPPORTED")

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

    def test_apply_muse_exits_1_and_writes_nothing(self) -> None:
        self.seed()
        before = snapshot(self.tmp)
        rc, out, _ = run_main("apply", "--home", str(self.home), "--source", self.source.scripts_dir, "muse")
        self.assertEqual(rc, 1, out)
        self.assertIn("UNSUPPORTED", out)
        self.assertEqual(snapshot(self.tmp), before)
        rc, out, _ = run_main("verify", "--home", str(self.home), "muse")
        self.assertEqual(rc, 1, out)

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
        self.assertEqual(sorted(p.name for p in stable.iterdir()), ["VERSION", "fleet-apps.json", "fleet_lanes", "lane-guard-hook"])

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
        args = ["--home", str(self.home), "--source", self.source.scripts_dir, "--sha", SHA]
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


if __name__ == "__main__":
    unittest.main()
