"""Offline installer checks; root ownership and the root filesystem are simulated."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "root"
        self.root.mkdir()
        self.module = types.ModuleType("installer_test")
        text = (HERE / "install-resource-control.sh").read_text()
        body = text.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
        exec(compile(body, "install-resource-control.sh", "exec"), self.module.__dict__)
        self.module.ROOT = str(self.root)
        self.module.ROOT_UID = os.getuid()
        self.destination = self.root / "usr/local/lib/fleet-resource-control"

    def install(self, source=SCRIPTS):
        with (
            mock.patch.object(self.module.os, "geteuid", return_value=0),
            mock.patch.object(self.module.os, "fchown"),
            mock.patch.object(self.module.os, "fsync"),
            mock.patch.object(self.module.sys, "platform", "linux"),
        ):
            self.module.install(source)

    def test_fixed_payload_modes_isolated_imports_and_preserved_state(self):
        credential = self.root / "home/owner/.config/fleet-tools/resource-policy-token"
        credential.parent.mkdir(parents=True)
        credential.write_text("untouched-test-credential")
        credential.chmod(0o600)
        state = self.root / "run/fleet-coding-resources/status.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"untouched":true}')
        self.install()
        expected = {
            "fleet-ops-control.py",
            "fleet-ops-control.json",
            "fleet_resource_control.py",
            "hetzner/__init__.py",
            "hetzner/fleet_coding_memory.py",
            "hetzner/coding_resources.py",
        }
        expected.update(
            "fleet_secrets/" + path.name for path in (SCRIPTS / "fleet_secrets").glob("*.py")
        )
        actual = {
            str(path.relative_to(self.destination))
            for path in self.destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, expected)
        for path in (
            self.destination,
            self.destination / "fleet_secrets",
            self.destination / "hetzner",
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
        for relative in expected:
            path = self.destination / relative
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(credential.read_text(), "untouched-test-credential")
        self.assertEqual(state.read_text(), '{"untouched":true}')
        trap = self.root / "untrusted-cwd"
        trap.mkdir()
        (trap / "fleet_resource_control.py").write_text("raise RuntimeError('untrusted import')")
        code = (
            "import sys,runpy;sys.path.insert(0,sys.argv[1]);"
            "import fleet_resource_control,fleet_secrets.core,hetzner.fleet_coding_memory;"
            "runpy.run_path(sys.argv[1]+'/fleet-ops-control.py');print('imports-ok')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", code, str(self.destination)],
            cwd=trap,
            env={"PYTHONPATH": str(trap)},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "imports-ok")
        self.install()  # Existing protected installation can be refreshed without state changes.
        self.assertEqual(credential.read_text(), "untouched-test-credential")

    def test_symlink_destination_directory_refused(self):
        lib = self.root / "usr/local/lib"
        lib.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install()
        self.assertFalse(list(outside.iterdir()))

    def test_writable_destination_directory_refused(self):
        self.destination.mkdir(parents=True)
        self.destination.chmod(0o777)
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse(list(self.destination.iterdir()))

    def test_symlink_installed_file_refused_without_touching_target(self):
        self.destination.mkdir(parents=True)
        outside = self.root / "outside"
        outside.write_text("untouched")
        (self.destination / "fleet-ops-control.py").symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(outside.read_text(), "untouched")

    def test_symlink_source_package_refused_before_destination_created(self):
        source = self.root / "source"
        source.mkdir()
        for name in ("fleet-ops-control.py", "fleet_resource_control.py"):
            (source / name).write_text("pass\n")
        (source / "fleet-ops-control.json").write_text(json.dumps({"schemaVersion": 1}))
        (source / "fleet_secrets").symlink_to(SCRIPTS / "fleet_secrets", target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())

    def test_nonroot_or_nonlinux_refused_before_any_destination_write(self):
        for uid, platform in ((1, "linux"), (0, "darwin")):
            with (
                self.subTest(uid=uid, platform=platform),
                mock.patch.object(self.module.os, "geteuid", return_value=uid),
                mock.patch.object(self.module.sys, "platform", platform),
            ):
                with self.assertRaises(RuntimeError):
                    self.module.install(SCRIPTS)
                self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
