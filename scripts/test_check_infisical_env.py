#!/usr/bin/env python3
"""Tests for scripts/check-infisical-env.py (prod is the only Infisical environment).

    python3 scripts/test_check_infisical_env.py

Fixtures copy the shapes found in the fleet repos on 2026-10-10 (Clutch's literal
export, HogHunter's ${..:-dev}, DealDex's ${..:=dev}, CodeCaps's two fallbacks).  Nothing
here reads a real secret or the network.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-infisical-env.py"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_infisical_env", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod       # @dataclass looks its module up here
    loader.exec_module(mod)
    return mod


lint = _load()


def messages(path: str, text: str) -> list[tuple[int, str]]:
    return [(f.line, f.message) for f in lint.scan_text(path, text)]


class EnvFileTests(unittest.TestCase):
    def test_cursor_env_dev_and_staging_are_flagged(self):
        for slug in ("dev", "staging"):
            found = messages(".cursor/infisical.env",
                             f"INFISICAL_DOMAIN=https://app.infisical.com\nINFISICAL_ENV={slug}\n")
            self.assertEqual([line for line, _ in found], [2], slug)
            self.assertIn(repr(slug), found[0][1])

    def test_prod_empty_and_comments_pass(self):
        text = ("INFISICAL_ENV=prod\n# INFISICAL_ENV=dev\nINFISICAL_ENVIRONMENT=\n"
                "INFISICAL_ENV=\"prod\"  # the only environment\n  PROD_INFISICAL_ENV=PROD\n")
        self.assertEqual(messages(".cursor/infisical.env", text), [])

    def test_prefixed_name_and_export_and_quotes(self):
        found = messages("dir/.env.example", 'export CLUTCH_INFISICAL_ENV="dev"\n')
        self.assertEqual([line for line, _ in found], [1])
        self.assertEqual(len(messages("a/b.env", "INFISICAL_ENVIRONMENT='staging'\n")), 1)

    def test_reference_value_is_left_to_the_shell_rule(self):
        self.assertEqual(messages(".env", "INFISICAL_ENV=${OTHER_ENV}\n"), [])
        self.assertEqual(len(messages(".env", "INFISICAL_ENV=${OTHER_ENV:-dev}\n")), 1)

    def test_allow_marker_skips_a_line(self):
        text = "INFISICAL_ENV=dev  # infisical-env: allow (migration fixture)\n"
        self.assertEqual(messages(".env", text), [])

    def test_unrelated_files_and_keys_are_ignored(self):
        self.assertEqual(messages("README.md", "INFISICAL_ENV=dev\n"), [])
        self.assertEqual(messages("config.py", 'INFISICAL_ENV = "dev"\n'), [])
        self.assertEqual(messages(".env", "NODE_ENV=dev\nAPP_ENV=staging\n"), [])

    def test_output_never_echoes_other_values(self):
        canary = "s3cr3t-canary-value-0123456789"
        found = messages(".cursor/infisical.env",
                         f"INFISICAL_CLIENT_SECRET={canary}\nINFISICAL_ENV={canary}\n")
        self.assertEqual(len(found), 1)
        self.assertNotIn(canary, found[0][1])
        self.assertIn("non-slug", found[0][1])


class StartScriptTests(unittest.TestCase):
    PATH = "scripts/cursor-cloud-start.sh"

    def test_shapes_seen_in_the_fleet(self):
        cases = {
            'export INFISICAL_ENV="dev"': 1,                         # Clutch
            'export CLUTCH_INFISICAL_ENV="${INFISICAL_ENV:-dev}"': 1,  # Clutch
            'INFISICAL_ENV_VALUE="${INFISICAL_ENV:-dev}"': 1,        # HogHunter
            ': "${INFISICAL_ENV:=dev}"': 1,                          # DealDex, Fleet-OPS
            'INFISICAL_ENVIRONMENT="${INFISICAL_ENV:-dev}" \\': 1,   # CodeCaps
            'echo "(env=${INFISICAL_ENV:-staging})"': 1,
            'infisical run --env=dev -- ./go': 1,
            'infisical export --env staging': 1,
        }
        for line, expected in cases.items():
            self.assertEqual(len(messages(self.PATH, line + "\n")), expected, line)

    def test_prod_forms_pass(self):
        text = ('#!/usr/bin/env bash\n: "${INFISICAL_ENV:=prod}"\n'
                'if [ "${INFISICAL_ENV}" != "prod" ]; then exit 1; fi\n'
                'infisical run --env=prod -- ./go\ninfisical run --env="$INFISICAL_ENV" -- ./go\n'
                '# old: ${INFISICAL_ENV:-dev}\n')
        self.assertEqual(messages(self.PATH, text), [])

    def test_allow_marker(self):
        line = 'x="${INFISICAL_ENV:-dev}"  # infisical-env: allow (explained here)\n'
        self.assertEqual(messages(self.PATH, line), [])

    def test_cli_rule_is_for_scripts_only(self):
        self.assertEqual(messages(".env", "FLAGS=--env=dev\n"), [])


class InfisicalJsonTests(unittest.TestCase):
    def test_prod_missing_and_non_prod(self):
        self.assertEqual(messages(".infisical.json", '{"workspaceId": "w", "defaultEnvironment": "prod"}'), [])
        self.assertEqual(messages(".infisical.json", '{"workspaceId": "w"}'), [])
        found = messages(".infisical.json",
                         '{\n  "workspaceId": "w",\n  "defaultEnvironment": "dev"\n}\n')
        self.assertEqual([line for line, _ in found], [3])
        self.assertEqual(len(messages(".infisical.json", '{"defaultEnvironment": ""}')), 1)

    def test_unparseable_is_reported(self):
        self.assertEqual(len(messages(".infisical.json", "{nope")), 1)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                    *args], check=True, capture_output=True)


class ScanTests(unittest.TestCase):
    def test_plain_folder_walk_skips_vendored_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".cursor").mkdir()
            (root / ".cursor" / "infisical.env").write_text("INFISICAL_ENV=dev\n")
            (root / "node_modules" / "x").mkdir(parents=True)
            (root / "node_modules" / "x" / "cursor-cloud-start.sh").write_text(': "${INFISICAL_ENV:=dev}"\n')
            found = lint.scan_source(lint.WorkingTree(root))
            self.assertEqual([f.path for f in found], [".cursor/infisical.env"])

    def test_git_ref_reads_the_commit_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q", "-b", "main")
            (repo / "scripts").mkdir()
            script = repo / "scripts" / "cursor-cloud-start.sh"
            script.write_text(': "${INFISICAL_ENV:=dev}"\n')
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "dev default")
            script.write_text(': "${INFISICAL_ENV:=prod}"\n')          # uncommitted fix
            self.assertEqual(len(lint.scan_source(lint.GitRef(repo, "main"))), 1)
            self.assertEqual(lint.scan_source(lint.WorkingTree(repo)), [])

    def test_main_exit_codes_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "svc.env").write_text("INFISICAL_CLIENT_SECRET=hunter2hunter2\nINFISICAL_ENV=staging\n")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(lint.main([str(root)]), 1)
            self.assertIn("svc.env:2:", out.getvalue())
            self.assertNotIn("hunter2", out.getvalue() + err.getvalue())
            (root / "svc.env").write_text("INFISICAL_ENV=prod\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(lint.main([str(root)]), 0)
            self.assertIn("ok", out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(lint.main([str(root / "missing")]), 2)

    def test_this_repo_is_clean(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lint.main([]), 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
