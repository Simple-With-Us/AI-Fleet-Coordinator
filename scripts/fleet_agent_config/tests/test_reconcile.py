"""Tests for the agent-config reconciler.

Run from scripts/:

    python3 -m unittest fleet_agent_config.tests.test_reconcile -v

Every home is a temporary directory.  Nothing here passes --apply to a real host.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from fleet_agent_config.reconcile import (
    Finding,
    UsageError,
    _default_hooks,
    _default_recall,
    _env_for,
    reconcile,
)
from fleet_agent_config.__main__ import main

_SCRIPTS = Path(__file__).resolve().parents[2]


def _load_doctor():
    spec = importlib.util.spec_from_file_location("doctor_for_agent_config", _SCRIPTS / "fleet-tools-doctor.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load fleet-tools-doctor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


class TempHome(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def status(self, result, surface: str, name: str) -> str:
        matches = [item for item in result.findings if item.surface == surface and item.name == name]
        self.assertEqual(len(matches), 1, (surface, name, result.findings))
        return matches[0].status


class CheckOnlyTests(TempHome):
    def test_empty_home_is_clean_and_writes_nothing(self) -> None:
        before = _files(self.home)
        result = reconcile(str(self.home))
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.mode, "check")
        self.assertEqual(result.applied, [])
        self.assertEqual(_files(self.home), before)
        self.assertTrue(any(item.status == "skip" for item in result.findings))
        self.assertNotIn("drift", {item.status for item in result.findings})

    def test_check_does_not_call_an_installer(self) -> None:
        (self.home / ".fx").mkdir()
        with mock.patch("fleet_agent_config.reconcile.subprocess.run") as run:
            with mock.patch("fleet_agent_config.reconcile.rules.main") as rules_main:
                result = reconcile(str(self.home))
        run.assert_not_called()
        rules_main.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(self.status(result, "skills", "fx"), "drift")
        self.assertEqual(_files(self.home), [])

    def test_owner_files_stay_report_only_under_apply(self) -> None:
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("owner text\n")
        agents = self.home / "AGENTS.md"
        agents.write_text("owner text\n")
        called: list[str] = []

        def rules_runner(home: str, platform: str, env: dict) -> int:
            called.append(platform)
            return 0

        before = {path.name: path.read_bytes() for path in (claude / "CLAUDE.md", agents)}
        result = reconcile(
            str(self.home),
            apply=True,
            runners={"rules": rules_runner, "skills": lambda *_: 0, "hooks": lambda *_: 0, "mcp": lambda *_: 0, "recall": lambda *_: 0},
        )
        self.assertEqual(self.status(result, "rules", "claude"), "report-only")
        self.assertEqual(self.status(result, "rules", "home-agents"), "report-only")
        self.assertNotIn("claude", called)
        self.assertNotIn("home-agents", called)
        self.assertEqual((claude / "CLAUDE.md").read_bytes(), before["CLAUDE.md"])
        self.assertEqual(agents.read_bytes(), before["AGENTS.md"])
        self.assertTrue(all(not item.startswith("rules:claude") and not item.startswith("rules:home-agents") for item in result.applied))

    def test_malformed_markers_are_refused_and_not_applied(self) -> None:
        codex = self.home / ".codex"
        codex.mkdir()
        target = codex / "AGENTS.md"
        target.write_text("<!-- fleet-lane-map:begin v2 -->\nstill open\n")
        called: list[str] = []

        def rules_runner(home: str, platform: str, env: dict) -> int:
            called.append(platform)
            return 0

        result = reconcile(
            str(self.home),
            apply=True,
            runners={
                "rules": rules_runner,
                "skills": lambda *_: 0,
                "hooks": lambda *_: 0,
                "mcp": lambda *_: 0,
                "recall": lambda *_: 0,
            },
        )
        self.assertEqual(self.status(result, "rules", "codex"), "refused")
        self.assertNotIn("codex", called)
        self.assertEqual(target.read_text(encoding="utf-8"), "<!-- fleet-lane-map:begin v2 -->\nstill open\n")
        self.assertFalse(result.ok)

    def test_json_envelope(self) -> None:
        result = reconcile(str(self.home))
        payload = result.as_dict()
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["mode"], "check")
        self.assertEqual(payload["ok"], True)
        self.assertIsInstance(payload["findings"], list)
        self.assertEqual(
            set(payload["findings"][0]),
            {"surface", "name", "status", "detail"},
        )

    def test_usage_rejects_an_unusable_home(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            self.assertEqual(main(["reconcile", "--home", ""]), 64)
            self.assertEqual(main(["reconcile", "--home", "/"]), 64)
            self.assertEqual(main(["reconcile", "--home", str(self.home / "missing")]), 64)
        with self.assertRaises(UsageError):
            reconcile("  ")

    def test_default_command_is_check(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["--home", str(self.home), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["mode"], "check")
        self.assertTrue(payload["ok"])


class ApplyContractTests(TempHome):
    def test_cursor_hooks_drift_asks_for_apply_without_verify(self) -> None:
        (self.home / ".cursor").mkdir()
        seen: list[list[str]] = []

        def hooks_runner(home: str, platform: str, env: dict) -> int:
            seen.append(["apply", platform, "--home", home, "--no-verify"])
            return 0

        result = reconcile(
            str(self.home),
            apply=True,
            runners={
                "hooks": hooks_runner,
                "skills": lambda *_: 0,
                "rules": lambda *_: 0,
                "mcp": lambda *_: 0,
                "recall": lambda *_: 0,
            },
        )
        self.assertIn(["apply", "cursor", "--home", str(self.home), "--no-verify"], seen)
        self.assertIn("hooks:cursor", result.applied)

    def test_default_hooks_runner_passes_no_verify(self) -> None:
        with mock.patch("fleet_agent_config.reconcile.tools.main", return_value=0) as install:
            code = _default_hooks(str(self.home), "cursor", {"HOME": str(self.home)})
        self.assertEqual(code, 0)
        argv = install.call_args.args[0]
        self.assertEqual(argv, ["apply", "cursor", "--home", str(self.home), "--no-verify"])

    def test_recall_drift_records_the_hooks_installer(self) -> None:
        claude = self.home / ".claude"
        claude.mkdir()
        with mock.patch("fleet_agent_config.reconcile.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            code = _default_recall(str(self.home), {"HOME": str(self.home), "PATH": "/usr/bin"})
        self.assertEqual(code, 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "bash")
        self.assertTrue(argv[1].endswith("install-fleet-rag.sh"))
        self.assertEqual(argv[2:], ["--hooks"])
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], str(self.home))

    def test_recall_hooks_drift_uses_the_injected_runner_once(self) -> None:
        (self.home / ".claude").mkdir()
        calls: list[str] = []

        def recall(home: str, env: dict) -> int:
            calls.append(env["HOME"])
            return 0

        result = reconcile(
            str(self.home),
            apply=True,
            runners={
                "recall": recall,
                "skills": lambda *_: 0,
                "rules": lambda *_: 0,
                "hooks": lambda *_: 0,
                "mcp": lambda *_: 0,
            },
        )
        self.assertEqual(calls, [str(self.home)])
        self.assertIn("recall", result.applied)
        self.assertEqual(self.status(result, "recall-hooks", "claude"), "drift")

    def test_invalid_settings_with_current_hook_files_is_not_rewritten(self) -> None:
        claude = self.home / ".claude"
        hooks = claude / "hooks"
        hooks.mkdir(parents=True)
        for name in (
            "fleet-recall-session-start.sh",
            "fleet-recall-stop.py",
            "fleet-recall-prompt-gate.py",
        ):
            source = (_SCRIPTS / "hooks" / name).read_bytes()
            (hooks / name).write_bytes(source)
        settings = claude / "settings.json"
        settings.write_text("{", encoding="utf-8")
        calls: list[str] = []

        def recall(home: str, env: dict) -> int:
            calls.append("recall")
            return 0

        result = reconcile(
            str(self.home),
            apply=True,
            runners={
                "recall": recall,
                "skills": lambda *_: 0,
                "rules": lambda *_: 0,
                "hooks": lambda *_: 0,
                "mcp": lambda *_: 0,
            },
        )
        self.assertEqual(self.status(result, "recall-hooks", "claude"), "refused")
        self.assertEqual(calls, [])
        self.assertEqual(settings.read_text(encoding="utf-8"), "{")

    def test_a_foreign_fleet_recall_entry_is_report_only(self) -> None:
        cursor = self.home / ".cursor"
        cursor.mkdir()
        mcp_file = cursor / "mcp.json"
        mcp_file.write_text(
            json.dumps({"mcpServers": {"fleet-recall": {"command": "other", "args": ["x"]}}}),
            encoding="utf-8",
        )
        calls: list[str] = []

        def recall(home: str, env: dict) -> int:
            calls.append("recall")
            return 0

        result = reconcile(
            str(self.home),
            apply=True,
            runners={"recall": recall, "skills": lambda *_: 0, "rules": lambda *_: 0, "hooks": lambda *_: 0, "mcp": lambda *_: 0},
        )
        self.assertEqual(self.status(result, "recall-mcp", ".cursor/mcp.json"), "report-only")
        self.assertEqual(calls, [])
        self.assertIn("other", mcp_file.read_text(encoding="utf-8"))

    def test_scoped_env_drops_roots_outside_a_stand_in_home(self) -> None:
        env = _env_for(
            str(self.home),
            {
                "HOME": "/tmp/somewhere-else",
                "PATH": "/usr/bin",
                "FLEET_APPS_ROOT": "/opt/fleet-apps",
                "FLEET_CODE_ROOT": "/opt/code",
                "FLEET_SERVER_VOLUME": "/srv/volume",
            },
        )
        self.assertEqual(env["HOME"], str(self.home))
        self.assertEqual(env["PATH"], "/usr/bin")
        for key in ("FLEET_APPS_ROOT", "FLEET_CODE_ROOT", "FLEET_SERVER_VOLUME"):
            self.assertNotIn(key, env)

    def test_scoped_env_keeps_roots_when_checking_the_process_home(self) -> None:
        real = os.path.realpath(os.path.expanduser("~"))
        env = _env_for(real, {"HOME": real, "FLEET_APPS_ROOT": "/opt/fleet-apps", "PATH": "/usr/bin"})
        self.assertEqual(env["FLEET_APPS_ROOT"], "/opt/fleet-apps")


class FxApplyTests(TempHome):
    def test_fx_apply_repairs_managed_drift_and_leaves_the_checkout(self) -> None:
        (self.home / ".fx").mkdir()
        repo = _SCRIPTS.parent
        watched = [
            repo / "skills" / "session-start" / "SKILL.md",
            repo / "docs" / "fleet-skills" / "session-start" / "SKILL.md",
        ]
        before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in watched]
        env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin"),
            "FLEET_APPS_ROOT": "/opt/fleet-apps",
            "FLEET_CODE_ROOT": "/opt/code",
            "FLEET_SERVER_VOLUME": "/srv/volume",
        }
        result = reconcile(str(self.home), apply=True, env=env)
        self.assertIn("skills", result.applied)
        self.assertIn("rules:fx", result.applied)
        self.assertIn("mcp:fx", result.applied)
        self.assertNotIn("hooks:fx", result.applied)
        self.assertEqual(self.status(result, "hooks", "fx"), "report-only")
        self.assertNotIn("drift", {item.status for item in result.findings if item.surface != "hooks"})
        self.assertTrue(result.ok, result.summary() + " " + json.dumps(result.as_dict()))
        for path, (mtime, body) in zip(watched, before):
            self.assertEqual(path.read_bytes(), body, path)
            self.assertEqual(path.stat().st_mtime_ns, mtime, path)
        self.assertFalse((Path("/opt/fleet-apps")).exists())


class DoctorRowTests(TempHome):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doctor = _load_doctor()

    def test_empty_home_is_current(self) -> None:
        report = self.doctor.Report()
        self.doctor.check_agent_config(report, self.home)
        row = report.row("agent-config")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "OK")
        self.assertEqual(row["state"], "current")
        self.assertEqual(row["category"], "agent-config")
        self.assertEqual(_files(self.home), [])

    def test_drift_is_stale_and_nothing_is_written(self) -> None:
        (self.home / ".fx").mkdir()
        report = self.doctor.Report()
        self.doctor.check_agent_config(report, self.home)
        row = report.row("agent-config")
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["state"], "stale")
        self.assertTrue(row["detail"].startswith("drift:"))
        self.assertEqual(_files(self.home), [])

    def test_a_broken_check_warns(self) -> None:
        report = self.doctor.Report()
        self.doctor.check_agent_config(report, self.home / "missing")
        row = report.row("agent-config")
        self.assertEqual(row["status"], "WARN")
        self.assertEqual(row["state"], "unknown")
        self.assertIn("UsageError", row["detail"])


class FindingShapeTests(unittest.TestCase):
    def test_detail_stays_short(self) -> None:
        item = Finding("rules", "fx", "drift", "x")
        self.assertEqual(item.as_dict()["status"], "drift")
