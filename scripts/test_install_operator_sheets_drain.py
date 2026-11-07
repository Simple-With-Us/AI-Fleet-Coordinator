"""Offline checks for scripts/agent_sync/server/install-operator-sheets-drain.sh (OPS-1037).

No network, no Infisical, no docker and no systemd change:  the shell script runs only in
--dry-run or up to a refusal, and its Python block runs in-process with root ownership and the
fleet_secrets helper faked.  Fixture tokens and keys are built in parts at run time, so no
credential-shaped literal sits in this file.

Run:  python3 scripts/test_install_operator_sheets_drain.py
"""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "agent_sync" / "server" / "install-operator-sheets-drain.sh"
FAKE_TOKEN = "st" + "." + "fixture" + "-not-a-real-token-" + "0123456789"
PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"
FAKE_SA = json.dumps(
    {
        "type": "service_account",
        "client_email": "drain@example.invalid",
        "private_key": PEM_BEGIN + "\nfixture-key-body\n" + PEM_END + "\n",
    }
)


def load_python_block():
    text = SCRIPT.read_text(encoding="utf-8")
    body = text.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    module = types.ModuleType("install_operator_sheets_drain_block")
    exec(compile(body, str(SCRIPT), "exec"), module.__dict__)
    module.ROOT_UID = os.getuid()
    return module


def fake_core(found=None, error=None):
    calls = []

    class SecretsError(Exception):
        def __init__(self, message):
            super().__init__(message)
            self.message = message

    def fetch_project(project, path, env, *, token=None, names=None):
        calls.append({"project": project, "path": path, "env": env, "token": token, "names": names})
        if error:
            raise SecretsError(error)
        return {"OPERATOR_GOOGLE_SHEETS_SA": FAKE_SA} if found is None else found

    core = types.SimpleNamespace(
        SecretsError=SecretsError,
        ENV_PROJECT="INFISICAL_FLEET_OPS_PROJECT_ID",
        infisical_project_id=lambda env, home: "00000000-0000-4000-8000-000000000000",
        home_dir=lambda env: Path(env.get("HOME", "/nonexistent")),
        fetch_project=fetch_project,
    )
    return core, calls


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.token = self.tmp / "token"
        self.token.write_text(FAKE_TOKEN + "\n")
        self.token.chmod(0o600)

    def run_script(self, *args, **env):
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp),
            "OPERATOR_SHEETS_WORKBOOK_ID": "workbook_FIXTURE-1",
            "OPERATOR_SHEETS_TOKEN_FILE": str(self.token),
        }
        base.update({k: v for k, v in env.items() if v is not None})
        for key in [k for k, v in env.items() if v is None]:
            base.pop(key, None)
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=base, capture_output=True, text=True, timeout=60
        )

    def test_bash_syntax(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self):
        result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dry_run_prints_the_plan_and_never_the_token(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("dry run: nothing will be changed", out)
        for name in (
            "operator_sheets_export.mjs",
            "operator_sheets_google.mjs",
            "operator_sheets_drain.mjs",
        ):
            self.assertRegex(
                out, r"(would install|unchanged) /usr/local/lib/operator-sheets-drain/" + name
            )
        self.assertIn("Fleet-OPS prod /operator-thread-registry", out)
        self.assertIn("the token is not read in a dry run", out)
        self.assertIn("OPERATOR_SHEETS_WORKBOOK_ID=workbook_FIXTURE-1", out)
        self.assertIn("OPERATOR_SHEETS_SESSIONS_SHEET_ID=20964676", out)
        self.assertIn("OPERATOR_SHEETS_EVENTS_SHEET_ID=1449318075", out)
        self.assertIn("pending-outbox", out)
        self.assertRegex(
            out, r"(would install|unchanged) /etc/systemd/system/operator-sheets-drain.timer"
        )
        self.assertIn("not enabled or started by this script", out)
        self.assertNotIn("installed /", out.replace("would install", ""))
        self.assertNotIn("fixture-not-a-real-token", out + result.stderr)

    def test_inputs_are_refused_before_anything_runs(self):
        cases = [
            ({"OPERATOR_SHEETS_WORKBOOK_ID": None}, "OPERATOR_SHEETS_WORKBOOK_ID"),
            ({"OPERATOR_SHEETS_WORKBOOK_ID": "bad id!"}, "OPERATOR_SHEETS_WORKBOOK_ID"),
            (
                {"OPERATOR_SHEETS_SESSIONS_SHEET_ID": "5", "OPERATOR_SHEETS_EVENTS_SHEET_ID": "5"},
                "must differ",
            ),
            ({"OPERATOR_SHEETS_EVENTS_SHEET_ID": "12x"}, "whole numbers"),
            ({"OPERATOR_REGISTRY_DB": "relative/path"}, "absolute path"),
            ({"OPERATOR_SHEETS_TOKEN_FILE": None}, "OPERATOR_SHEETS_TOKEN_FILE"),
            ({"OPERATOR_SHEETS_TOKEN_FILE": "/nonexistent/token"}, "not a regular file"),
        ]
        for env, message in cases:
            with self.subTest(env=env):
                result = self.run_script("--dry-run", **env)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(message, result.stderr)
                self.assertNotIn("would install", result.stdout)

    def test_token_file_must_be_private_and_not_a_symlink(self):
        self.token.chmod(0o644)
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mode 0600 or 0400", result.stderr)
        self.token.chmod(0o600)
        link = self.tmp / "token-link"
        link.symlink_to(self.token)
        result = self.run_script("--dry-run", OPERATOR_SHEETS_TOKEN_FILE=str(link))
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink is refused", result.stderr)

    def test_unknown_argument(self):
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    @unittest.skipIf(os.geteuid() == 0, "a root run would install for real")
    def test_a_real_run_needs_root_and_changes_nothing_first(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("run as root", result.stderr)
        self.assertEqual(result.stdout, "")


class PythonBlockTests(unittest.TestCase):
    def setUp(self):
        self.block = load_python_block()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        patcher = mock.patch.object(self.block.os, "fchown")
        patcher.start()
        self.addCleanup(patcher.stop)

    def token_file(self, text=FAKE_TOKEN, mode=0o600):
        path = self.tmp / "token"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_read_token_accepts_a_private_service_token_only(self):
        self.assertEqual(self.block.read_token(str(self.token_file(FAKE_TOKEN + "\n"))), FAKE_TOKEN)
        for text, mode, message in [
            (FAKE_TOKEN, 0o640, "no group or other access"),
            ("not-a-service-token", 0o600, "does not hold an Infisical service token"),
            (FAKE_TOKEN + " extra", 0o600, "does not hold an Infisical service token"),
        ]:
            with self.subTest(message=message, mode=oct(mode)):
                with self.assertRaises(self.block.InstallError) as caught:
                    self.block.read_token(str(self.token_file(text, mode)))
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("fixture", str(caught.exception))
        link = self.tmp / "link"
        link.symlink_to(self.token_file())
        with self.assertRaises(self.block.InstallError):
            self.block.read_token(str(link))

    def test_fetch_reads_one_named_secret_from_the_scoped_folder_with_a_pinned_env(self):
        core, calls = fake_core()
        value = self.block.fetch_service_account(core, FAKE_TOKEN, {"HOME": str(self.tmp)})
        self.assertEqual(value, FAKE_SA)
        self.assertEqual(
            calls,
            [
                {
                    "project": "00000000-0000-4000-8000-000000000000",
                    "path": "/operator-thread-registry",
                    "env": {},  # no caller variable can redirect the API or widen the folders
                    "token": FAKE_TOKEN,
                    "names": ["OPERATOR_GOOGLE_SHEETS_SA"],
                }
            ],
        )

    def test_fetch_failures_name_keys_never_values(self):
        core, _ = fake_core(error="token-scope-mismatch")
        with self.assertRaises(self.block.InstallError) as caught:
            self.block.fetch_service_account(core, FAKE_TOKEN, {})
        self.assertIn("token-scope-mismatch", str(caught.exception))
        self.assertNotIn("fixture", str(caught.exception))
        core, _ = fake_core(found={})
        with self.assertRaises(self.block.InstallError) as caught:
            self.block.fetch_service_account(core, FAKE_TOKEN, {})
        self.assertIn("OPERATOR_GOOGLE_SHEETS_SA is not in Fleet-OPS prod", str(caught.exception))

    def test_service_account_shape_is_checked_without_echoing_it(self):
        self.assertEqual(self.block.check_service_account(FAKE_SA), FAKE_SA.encode())
        bad = [
            ("not json fixture-key-body", "is not JSON"),
            (json.dumps({"type": "user", "private_key": "fixture-key-body"}), "(type)"),
            (
                json.dumps({"type": "service_account", "private_key": PEM_BEGIN}),
                "missing client_email",
            ),
            (
                json.dumps(
                    {
                        "type": "service_account",
                        "client_email": "a@b",
                        "private_key": "fixture-key-body",
                    }
                ),
                "not a PEM key",
            ),
        ]
        for text, message in bad:
            with self.subTest(message=message):
                with self.assertRaises(self.block.InstallError) as caught:
                    self.block.check_service_account(text)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("fixture-key-body", str(caught.exception))

    def test_render_env(self):
        text = self.block.render_env(
            "/etc/operator-sheets",
            "wb_1",
            "20964676",
            "1449318075",
            "eavesdropper",
            "/data/operator/registry.sqlite3",
        ).decode()
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        self.assertEqual(
            lines,
            [
                "OPERATOR_SHEETS_SA_FILE=/etc/operator-sheets/sa.json",
                "OPERATOR_SHEETS_WORKBOOK_ID=wb_1",
                "OPERATOR_SHEETS_SESSIONS_SHEET_ID=20964676",
                "OPERATOR_SHEETS_EVENTS_SHEET_ID=1449318075",
                "OPERATOR_REGISTRY_CONTAINER=eavesdropper",
                "OPERATOR_REGISTRY_DB=/data/operator/registry.sqlite3",
            ],
        )

    def test_private_directory_and_files_are_0700_and_0600_and_idempotent(self):
        etc = self.tmp / "etc-operator-sheets"
        fd = self.block.open_private_directory(str(etc))
        try:
            self.assertEqual(stat.S_IMODE(etc.stat().st_mode), 0o700)
            self.assertTrue(self.block.write_private(fd, "sa.json", b"one"))
            self.assertEqual(stat.S_IMODE((etc / "sa.json").stat().st_mode), 0o600)
            self.assertFalse(
                self.block.write_private(fd, "sa.json", b"one")
            )  # unchanged: untouched
            self.assertTrue(self.block.write_private(fd, "sa.json", b"two"))
            self.assertEqual((etc / "sa.json").read_bytes(), b"two")
            (etc / "sa.json").chmod(0o644)
            self.assertTrue(
                self.block.write_private(fd, "sa.json", b"two")
            )  # mode drift is repaired
            self.assertEqual(stat.S_IMODE((etc / "sa.json").stat().st_mode), 0o600)
            outside = self.tmp / "outside"
            outside.write_text("untouched")
            (etc / "drain.env").symlink_to(outside)
            with self.assertRaises(self.block.InstallError):
                self.block.write_private(fd, "drain.env", b"x")
            self.assertEqual(outside.read_text(), "untouched")
            self.assertEqual([p.name for p in etc.iterdir() if p.name.startswith(".install-")], [])
        finally:
            os.close(fd)
        loose = self.tmp / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        with self.assertRaises(self.block.InstallError):
            self.block.open_private_directory(str(loose))

    def test_main_writes_both_files_and_prints_neither_secret(self):
        core, calls = fake_core()
        package = types.ModuleType("fleet_secrets")
        package.core = core
        etc = self.tmp / "etc-operator-sheets"
        token = self.token_file()
        out = io.StringIO()
        path_before = list(sys.path)
        try:
            with (
                mock.patch.dict(
                    sys.modules, {"fleet_secrets": package, "fleet_secrets.core": core}
                ),
                redirect_stdout(out),
            ):
                self.block.main(
                    [
                        str(self.tmp),
                        str(etc),
                        str(token),
                        "wb_1",
                        "20964676",
                        "1449318075",
                        "eavesdropper",
                        "/data/operator/registry.sqlite3",
                    ]
                )
        finally:
            sys.path[:] = path_before
        self.assertEqual((etc / "sa.json").read_text(), FAKE_SA)
        self.assertIn("OPERATOR_SHEETS_WORKBOOK_ID=wb_1\n", (etc / "drain.env").read_text())
        for name in ("sa.json", "drain.env"):
            self.assertEqual(stat.S_IMODE((etc / name).stat().st_mode), 0o600)
        printed = out.getvalue()
        self.assertIn("installed " + str(etc) + "/sa.json", printed)
        self.assertNotIn("fixture", printed)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
