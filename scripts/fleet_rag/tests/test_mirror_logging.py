"""Logging and summary lines for the Mac fleet-rag mirror (AFC-741)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_rag import mirror, mirror_install

REPO = Path(__file__).resolve().parents[3]


class MirrorLogHelpersTests(unittest.TestCase):
    def test_format_push_summary_lists_files_and_outcome(self) -> None:
        line = mirror.format_push_summary(
            outcome="promoted",
            duration_s=1.25,
            folder_counts={"skills/claude": 3, "skills/cursor": 1},
        )
        self.assertIn("outcome=promoted", line)
        self.assertIn("duration_s=1.250", line)
        self.assertIn("files=skills/claude:3,skills/cursor:1", line)

    def test_rotate_moves_oversize_file_to_dot_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fleet-rag-mirror.log"
            path.write_bytes(b"x" * 100)
            mirror.maybe_rotate_log(path, max_bytes=50)
            self.assertFalse(path.is_file())
            rotated = path.with_name(path.name + ".1")
            self.assertTrue(rotated.is_file())
            self.assertEqual(rotated.stat().st_size, 100)

    def test_append_mirror_log_creates_parent_and_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Logs" / "fleet-rag-mirror.log"
            mirror.append_mirror_log(path, "line one")
            mirror.append_mirror_log(path, "line two")
            text = path.read_text(encoding="utf-8")
        self.assertEqual(text.splitlines(), ["line one", "line two"])


class MirrorPushLoggingTests(unittest.TestCase):
    def test_disabled_push_writes_a_summary_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            log_path = home / "Library" / "Logs" / "fleet-rag-mirror.log"
            cfg = home / "mirror.json"
            cfg.write_text(
                json.dumps(
                    {
                        "enabled": False,
                        "home": str(home),
                        "log": str(log_path),
                        "ssh_target": "server",
                        "remote_root": "/srv/mirror",
                        "remote_script": "/srv/repo/scripts/fleet_rag/mirror.py",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(cfg, 0o600)
            with mock.patch.object(mirror.subprocess, "run", side_effect=AssertionError("ran")):
                self.assertEqual(mirror._cmd_push(cfg), 0)
            line = log_path.read_text(encoding="utf-8").strip()
        self.assertIn("outcome=disabled", line)
        self.assertIn("duration_s=", line)

    def test_successful_push_summary_includes_per_tree_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude" / "skills").mkdir(parents=True)
            (home / ".claude" / "skills" / "SKILL.md").write_text("x\n", encoding="utf-8")
            log_path = home / "log.txt"
            cfg = home / "mirror.json"
            cfg.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "home": str(home),
                        "log": str(log_path),
                        "ssh_target": "server",
                        "remote_root": "/srv/mirror",
                        "remote_script": "/srv/repo/scripts/fleet_rag/mirror.py",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(cfg, 0o600)

            def fake_push(**kwargs):  # noqa: ANN003
                return mirror.PushResult(True, "promoted", {"skills/claude": 1, "memory/codex": 0})

            with mock.patch.object(mirror, "push", side_effect=fake_push):
                self.assertEqual(mirror._cmd_push(cfg), 0)
            line = log_path.read_text(encoding="utf-8").strip()
        self.assertIn("outcome=promoted", line)
        self.assertIn("skills/claude:1", line)
        self.assertIn("memory/codex:0", line)


class InstallEnvExampleTests(unittest.TestCase):
    def test_env_example_has_server_script_path_and_install_writes_log(self) -> None:
        example = (REPO / "scripts/launchd/fleet-rag-mirror.env.example").read_text(encoding="utf-8")
        needle = "/mnt/" + "fleet-extra"
        self.assertIn(needle, example)
        self.assertIn("FLEET_RAG_MIRROR_REMOTE_SCRIPT=", example)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            result = mirror_install.apply(
                home=home,
                ssh_target="server",
                remote_root="/srv/mirror",
                remote_script=f"{needle}/Code/AI-Fleet-Coordinator/scripts/fleet_rag/mirror.py",
                enable=False,
                config_path=root / "mirror.json",
                plist_path=root / "agent.plist",
            )
            payload = json.loads((root / "mirror.json").read_text(encoding="utf-8"))
            plist = (root / "agent.plist").read_text(encoding="utf-8")
        self.assertEqual(payload["log"], str(home / "Library" / "Logs" / "fleet-rag-mirror.log"))
        self.assertIn("StandardOutPath", plist)
        self.assertIn("StandardErrorPath", plist)
        self.assertIn("fleet-rag-mirror.log", plist)
        self.assertFalse(result["bootstrapped"])


class PlistTemplateTests(unittest.TestCase):
    def test_committed_plist_template_routes_stdout_and_stderr_to_log_placeholder(self) -> None:
        plist = (REPO / "scripts/launchd/com.jay.fleet-rag-mirror.plist").read_text(encoding="utf-8")
        self.assertIn("<key>StandardOutPath</key>", plist)
        self.assertIn("<key>StandardErrorPath</key>", plist)
        self.assertIn("@LOG@", plist)


if __name__ == "__main__":
    unittest.main()
