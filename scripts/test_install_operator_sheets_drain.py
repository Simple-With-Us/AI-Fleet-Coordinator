"""Offline checks for scripts/simple_tools/server/install-operator-sheets-drain.sh (OPS-1037).

No network, no Infisical, no real docker and no systemd change.  The shell script runs three
ways:  in --dry-run or up to a refusal against this checkout;  in its test mode
(OPERATOR_SHEETS_INSTALL_TEST_ROOT) against a throwaway git checkout, with every destination
under a temporary root and docker and systemctl as recording stubs, which drives steps 1, 4 and
5 for real;  and its Python block (steps 2 and 3) in-process, with root ownership and the
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
SCRIPT = HERE / "simple_tools" / "server" / "install-operator-sheets-drain.sh"
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
MODULES = ("operator_sheets_export.mjs", "operator_sheets_google.mjs", "operator_sheets_drain.mjs")
REVIEWED_DRAIN = "export function pacedAdapter() {}\nexport const RUN_DEADLINE_MS = 1;\n"
REVIEWED_TIMER = "[Timer]\nOnUnitInactiveSec=5min\n"

DOCKER_STUB = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  *pending-outbox*) printf '%s\\n' "${STUB_PENDING_OUT-[]}"; exit "${STUB_PENDING_EXIT:-0}" ;;
  *export-attempts*) printf '%s\\n' "${STUB_ATTEMPTS_OUT-[]}"; exit "${STUB_ATTEMPTS_EXIT:-0}" ;;
esac
exit 0
"""

SYSTEMCTL_STUB = """#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_LOG"
case "$1" in
  is-active) [[ "${STUB_TIMER_ACTIVE:-0}" == 1 ]] && exit 0; exit 3 ;;
  is-enabled) echo disabled; exit 1 ;;
esac
exit 0
"""


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


def git(repo, *args):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


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
        for name in MODULES:
            self.assertRegex(
                out, r"(would install|unchanged) /usr/local/lib/operator-sheets-drain/" + name
            )
        self.assertIn("Fleet-OPS prod /operator-thread-registry", out)
        self.assertIn("the token is not read in a dry run", out)
        self.assertIn("OPERATOR_SHEETS_WORKBOOK_ID=workbook_FIXTURE-1", out)
        self.assertIn("OPERATOR_SHEETS_SESSIONS_SHEET_ID=20964676", out)
        self.assertIn("OPERATOR_SHEETS_EVENTS_SHEET_ID=1449318075", out)
        self.assertIn("pending-outbox", out)
        self.assertIn("export-attempts", out)
        self.assertRegex(
            out, r"(would install|unchanged) /etc/systemd/system/operator-sheets-drain.timer"
        )
        self.assertIn("the timer was not enabled by this script", out)
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
            ({"OPERATOR_SHEETS_PAGE_LIMIT": "9000"}, "1 to 5000"),
            ({"OPERATOR_SHEETS_LOCK": "relative.lock"}, "OPERATOR_SHEETS_LOCK"),
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

    @unittest.skipIf(os.geteuid() == 0, "the token here is root-owned when the test runs as root")
    def test_a_token_file_root_does_not_own_is_refused_before_step_1(self):
        # Kody, PR 1458:  a mode-valid file with the wrong owner used to pass the shell checks and
        # fail only in step 3, after the timer was stopped and the modules installed.
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"a real run would refuse: OPERATOR_SHEETS_TOKEN_FILE must be owned by uid 0 (it is uid {os.geteuid()})",
            result.stdout,
        )

    def test_unknown_argument(self):
        result = self.run_script("--once")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    @unittest.skipIf(os.geteuid() == 0, "a root run would install for real")
    def test_a_real_run_from_this_checkout_changes_nothing_before_refusing(self):
        # This branch is not a clean origin/main checkout (and, before the reviewed drain merges,
        # its modules predate it), or this is not root:  either way it stops before step 1.
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_test_mode_requires_stub_docker_and_systemctl(self):
        root = self.tmp / "root"
        (root / "bin").mkdir(parents=True)
        result = self.run_script(OPERATOR_SHEETS_INSTALL_TEST_ROOT=str(root))
        self.assertEqual(result.returncode, 2)
        self.assertIn("needs a stub", result.stderr)


class StubbedInstallTests(unittest.TestCase):
    """Steps 1, 4 and 5 for real, in test mode, from a throwaway checkout at origin/main."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.token = self.tmp / "token"
        self.token.write_text(FAKE_TOKEN + "\n")
        self.token.chmod(0o600)
        self.root = self.tmp / "root"
        (self.root / "bin").mkdir(parents=True)
        for name, text in (("docker", DOCKER_STUB), ("systemctl", SYSTEMCTL_STUB)):
            stub = self.root / "bin" / name
            stub.write_text(text)
            stub.chmod(0o755)
        self.log = self.tmp / "stub.log"
        self.repo = self.make_repo()
        self.lib = self.root / "usr/local/lib/operator-sheets-drain"
        self.units = self.root / "etc/systemd/system"

    def make_repo(self, drain=REVIEWED_DRAIN, timer=REVIEWED_TIMER):
        repo = self.tmp / "repo"
        agent = repo / "scripts" / "simple_tools"
        (agent / "server").mkdir(parents=True)
        (repo / "scripts" / "fleet_secrets").mkdir()
        (repo / "scripts" / "fleet_secrets" / "core.py").write_text("")
        shutil.copy(SCRIPT, agent / "server" / SCRIPT.name)
        for name in MODULES:
            (agent / name).write_text(drain if name.endswith("drain.mjs") else f"// {name}\n")
        (agent / "server" / "operator-sheets-drain.service").write_text("[Service]\nType=oneshot\n")
        (agent / "server" / "operator-sheets-drain.timer").write_text(timer)
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "fixture")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        return repo

    def run_install(self, **env):
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp),
            "OPERATOR_SHEETS_WORKBOOK_ID": "workbook_FIXTURE-1",
            "OPERATOR_SHEETS_TOKEN_FILE": str(self.token),
            "OPERATOR_SHEETS_INSTALL_TEST_ROOT": str(self.root),
            "STUB_LOG": str(self.log),
        }
        base.update(env)
        script = self.repo / "scripts" / "simple_tools" / "server" / SCRIPT.name
        return subprocess.run(
            ["bash", str(script)], env=base, capture_output=True, text=True, timeout=60
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_install_then_rerun_changes_nothing(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in MODULES:
            path = self.lib / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        for name in ("operator-sheets-drain.service", "operator-sheets-drain.timer"):
            self.assertTrue((self.units / name).is_file())
        calls = self.calls()
        self.assertTrue(any("pending-outbox" in c and "--limit 1" in c for c in calls))
        self.assertTrue(any("export-attempts" in c and "--limit 1" in c for c in calls))
        self.assertIn("systemctl daemon-reload", calls)
        self.assertFalse(any(c.startswith(("systemctl enable", "systemctl start")) for c in calls))
        self.assertIn("test mode: skipped the Infisical read", result.stdout)
        self.assertNotIn("fixture-not-a-real-token", result.stdout + result.stderr)
        self.log.unlink()
        again = self.run_install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertNotIn("installed /", again.stdout)
        self.assertNotIn("systemctl daemon-reload", self.calls())

    def test_a_drifted_mode_is_repaired(self):
        self.assertEqual(self.run_install().returncode, 0)
        drifted = self.lib / "operator_sheets_drain.mjs"
        drifted.chmod(0o600)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"installed {drifted}", result.stdout)
        self.assertIn(f"unchanged {self.lib / 'operator_sheets_export.mjs'}", result.stdout)
        self.assertEqual(stat.S_IMODE(drifted.stat().st_mode), 0o644)

    def test_a_running_timer_is_stopped_for_the_install_and_started_again(self):
        result = self.run_install(STUB_TIMER_ACTIVE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        stop = calls.index("systemctl stop operator-sheets-drain.timer")
        start = calls.index("systemctl start operator-sheets-drain.timer")
        preflight = next(i for i, c in enumerate(calls) if "pending-outbox" in c)
        self.assertLess(stop, preflight)
        self.assertLess(calls.index("systemctl daemon-reload"), start)
        out = result.stdout
        self.assertLess(out.index("stopped operator-sheets-drain.timer"), out.index("installed "))
        self.assertNotIn("systemctl enable", "\n".join(calls))

    def test_preflight_without_the_export_fence_stops_in_one_line(self):
        result = self.run_install(
            STUB_TIMER_ACTIVE="1",
            STUB_ATTEMPTS_OUT="usage: operator_registry: invalid choice",
            STUB_ATTEMPTS_EXIT="2",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("export-attempts", result.stderr)
        self.assertIn("schema v2", result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertFalse((self.units / "operator-sheets-drain.timer").exists())
        # The timer it stopped stays stopped, and the exit says so.
        self.assertNotIn("systemctl start operator-sheets-drain.timer", self.calls())
        self.assertIn("is still stopped", result.stderr)

    def test_preflight_with_bad_json_stops_in_one_line(self):
        result = self.run_install(STUB_PENDING_OUT="not json at all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pending-outbox", result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertFalse((self.units / "operator-sheets-drain.service").exists())

    def test_a_dirty_checkout_is_refused_before_any_change(self):
        (self.repo / "scripts" / "simple_tools" / "operator_sheets_google.mjs").write_text("edit\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertFalse(self.lib.exists())

    def test_a_checkout_not_at_origin_main_is_refused(self):
        (self.repo / "README").write_text("ahead\n")
        git(self.repo, "add", "README")
        git(self.repo, "commit", "-q", "-m", "ahead of origin/main")
        result = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("not origin/main", result.stderr)
        self.assertFalse(self.lib.exists())

    def test_a_checkout_with_the_old_drain_is_refused(self):
        shutil.rmtree(self.repo)
        self.repo = self.make_repo(drain="export async function drainOnce() {}\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no pacedAdapter", result.stderr)
        self.assertFalse(self.lib.exists())
        shutil.rmtree(self.repo)
        self.repo = self.make_repo(timer="[Timer]\nOnUnitActiveSec=5min\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("OnUnitInactiveSec", result.stderr)

    def test_a_loose_config_directory_is_refused_before_step_1(self):
        etc = self.root / "etc" / "operator-sheets"
        etc.mkdir(parents=True)
        etc.chmod(0o755)
        result = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no group or other access", result.stderr)
        self.assertFalse(self.lib.exists())


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
            {"OPERATOR_SHEETS_PAGE_LIMIT": "200"},
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
                "OPERATOR_SHEETS_PAGE_LIMIT=200",
            ],
        )

    def test_knobs_come_from_the_caller_or_are_kept_from_the_existing_file(self):
        existing = {"OPERATOR_SHEETS_PAGE_LIMIT": "300", "OPERATOR_SHEETS_LOCK": "/run/drain.lock"}
        knobs, notes = self.block.resolve_knobs({"OPERATOR_SHEETS_PAGE_LIMIT": "50"}, existing)
        self.assertEqual(
            knobs, {"OPERATOR_SHEETS_PAGE_LIMIT": "50", "OPERATOR_SHEETS_LOCK": "/run/drain.lock"}
        )
        self.assertEqual(notes, ["OPERATOR_SHEETS_LOCK kept from the existing drain.env"])
        self.assertEqual(self.block.resolve_knobs({}, {}), ({}, []))
        with self.assertRaises(self.block.InstallError) as caught:
            self.block.resolve_knobs({}, {"OPERATOR_SHEETS_PAGE_LIMIT": "999999"})
        self.assertIn(
            "OPERATOR_SHEETS_PAGE_LIMIT (kept from the existing drain.env)", str(caught.exception)
        )

    def test_env_changes_name_settings_only(self):
        old = b"OPERATOR_SHEETS_WORKBOOK_ID=a\nOPERATOR_SHEETS_LOCK=/x\nHAND_SET=1\n"
        new = b"# comment\nOPERATOR_SHEETS_WORKBOOK_ID=b\nOPERATOR_SHEETS_LOCK=/x\n"
        self.assertEqual(
            self.block.env_changes(old, new), (["OPERATOR_SHEETS_WORKBOOK_ID"], ["HAND_SET"])
        )

    def test_private_directory_and_files_are_0700_and_0600_and_idempotent(self):
        etc = self.tmp / "etc-operator-sheets"
        fd = self.block.open_private_directory(str(etc))
        try:
            self.assertEqual(stat.S_IMODE(etc.stat().st_mode), 0o700)
            self.assertTrue(self.block.write_private(fd, "sa.json", b"one"))
            self.assertEqual(stat.S_IMODE((etc / "sa.json").stat().st_mode), 0o600)
            # Unchanged:  untouched.
            self.assertFalse(self.block.write_private(fd, "sa.json", b"one"))
            self.assertTrue(self.block.write_private(fd, "sa.json", b"two"))
            self.assertEqual((etc / "sa.json").read_bytes(), b"two")
            (etc / "sa.json").chmod(0o644)
            # Mode drift is repaired.
            self.assertTrue(self.block.write_private(fd, "sa.json", b"two"))
            self.assertEqual(stat.S_IMODE((etc / "sa.json").stat().st_mode), 0o600)
            outside = self.tmp / "outside"
            outside.write_text("untouched")
            (etc / "drain.env").symlink_to(outside)
            with self.assertRaises(self.block.InstallError):
                self.block.write_private(fd, "drain.env", b"x")
            with self.assertRaises(self.block.InstallError):
                self.block.read_private(fd, "drain.env")
            self.assertEqual(outside.read_text(), "untouched")
            self.assertEqual([p.name for p in etc.iterdir() if p.name.startswith(".install-")], [])
        finally:
            os.close(fd)
        loose = self.tmp / "loose"
        loose.mkdir(mode=0o755)
        loose.chmod(0o755)
        with self.assertRaises(self.block.InstallError):
            self.block.open_private_directory(str(loose))

    def run_main(self, etc, page_limit="", lock_path=""):
        core, calls = fake_core()
        package = types.ModuleType("fleet_secrets")
        package.core = core
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
                        str(self.token_file()),
                        "wb_1",
                        "20964676",
                        "1449318075",
                        "eavesdropper",
                        "/data/operator/registry.sqlite3",
                        page_limit,
                        lock_path,
                    ]
                )
        finally:
            sys.path[:] = path_before
        return out.getvalue(), calls

    def test_main_writes_both_files_and_prints_neither_secret(self):
        etc = self.tmp / "etc-operator-sheets"
        printed, calls = self.run_main(etc)
        self.assertEqual((etc / "sa.json").read_text(), FAKE_SA)
        self.assertIn("OPERATOR_SHEETS_WORKBOOK_ID=wb_1\n", (etc / "drain.env").read_text())
        for name in ("sa.json", "drain.env"):
            self.assertEqual(stat.S_IMODE((etc / name).stat().st_mode), 0o600)
        self.assertIn("installed " + str(etc) + "/sa.json", printed)
        self.assertNotIn("fixture", printed)
        self.assertEqual(len(calls), 1)

    def test_main_keeps_hand_set_knobs_and_names_what_changed(self):
        etc = self.tmp / "etc-operator-sheets"
        self.run_main(etc)
        env_file = etc / "drain.env"
        env_file.write_text(
            env_file.read_text()
            + "OPERATOR_SHEETS_PAGE_LIMIT=250\nOPERATOR_SHEETS_LOCK=/run/drain.lock\n"
        )
        printed, _ = self.run_main(etc, page_limit="100")
        text = env_file.read_text()
        self.assertIn("OPERATOR_SHEETS_PAGE_LIMIT=100\n", text)  # the caller's value wins
        self.assertIn("OPERATOR_SHEETS_LOCK=/run/drain.lock\n", text)  # a hand-set one is kept
        self.assertIn("OPERATOR_SHEETS_LOCK kept from the existing drain.env", printed)
        self.assertIn("drain.env settings changed: OPERATOR_SHEETS_PAGE_LIMIT", printed)
        self.assertNotIn("=250", printed)  # names only, never values


if __name__ == "__main__":
    unittest.main()
