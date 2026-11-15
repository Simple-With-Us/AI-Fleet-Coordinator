"""Read-only collection keeps existing host trust and strips source redirection."""

import os
import unittest
from unittest.mock import patch

from fleet_docs import snapshot


class CollectorEnvironmentTests(unittest.TestCase):
    def test_host_configuration_is_preserved_without_source_or_injected_overrides(self):
        trusted = {
            "HOME": "/fixture/home",
            "PATH": "/fixture/bin",
            "GIT_CONFIG_GLOBAL": "/fixture/gitconfig",
            "GIT_CONFIG_SYSTEM": "/fixture/system-gitconfig",
            "XDG_CONFIG_HOME": "/fixture/xdg",
        }
        overrides = {
            name: "synthetic"
            for name in (
                "GIT_DIR",
                "GIT_WORK_TREE",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR",
                "GIT_NAMESPACE",
                "GIT_CONFIG_COUNT",
                "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0",
                "GIT_CONFIG_PARAMETERS",
            )
        }
        with patch.dict(os.environ, {**trusted, **overrides}, clear=True):
            actual = snapshot.collector()._git_read_environment()
        self.assertTrue(all(actual.get(key) == value for key, value in trusted.items()))
        self.assertTrue(all(key not in actual for key in overrides))
        self.assertNotIn("GIT_CONFIG_NOSYSTEM", actual)
        self.assertEqual(actual["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(actual["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(actual["GIT_TERMINAL_PROMPT"], "0")

    def test_repo_info_ignores_global_instead_of_rewrites(self):
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "Fixture-Repo"
            repo.mkdir()
            for args in (
                ("init", "-q", "-b", "main"),
                ("remote", "add", "origin", "https://github.com/Simple-With-Us/BotFleet.git"),
                (
                    "-c",
                    "user.name=fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "fixture",
                ),
            ):
                subprocess.check_call(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)
            head = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            subprocess.check_call(
                ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", head],
                stderr=subprocess.DEVNULL,
            )
            gitconfig = root / "gitconfig"
            gitconfig.write_text('[url "https://redirected.example/other/"]\n\tinsteadOf = https://github.com/\n')
            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(gitconfig)}):
                info = snapshot.collector()._repo_info(repo)
            self.assertIsNotNone(info)
            self.assertEqual((info.owner, info.name), ("Simple-With-Us", "BotFleet"))
