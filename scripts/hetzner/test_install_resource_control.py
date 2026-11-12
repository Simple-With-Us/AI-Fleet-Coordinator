"""Offline installer checks; root ownership and the root filesystem are simulated."""

import importlib.util
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
            "fleet_seat_inventory.py",
            "muster_approval.py",
            "muster_trust_files.py",
            "reduced_status.py",
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
        # The `--from-muster` modules are installed byte for byte, so that the shared /etc/muster
        # reader is the same file wherever it is installed.
        self.assertEqual(
            (self.destination / "muster_approval.py").read_bytes(),
            (SCRIPTS / "muster_approval.py").read_bytes(),
        )
        # The reader has one source file, in muster_agent/, and is installed flat beside the helper.
        self.assertEqual(
            (self.destination / "muster_trust_files.py").read_bytes(),
            (SCRIPTS / "muster_agent" / "muster_trust_files.py").read_bytes(),
        )
        trap = self.root / "untrusted-cwd"
        trap.mkdir()
        (trap / "fleet_resource_control.py").write_text("raise RuntimeError('untrusted import')")
        # The two `--from-muster` modules import `cryptography` (the system package on the server);
        # a machine without it still checks every other import, then reports the test skipped.
        has_cryptography = importlib.util.find_spec("cryptography") is not None
        muster_imports = ",muster_approval,muster_trust_files" if has_cryptography else ""
        code = (
            "import sys,runpy;sys.path.insert(0,sys.argv[1]);"
            "import fleet_resource_control,fleet_seat_inventory,reduced_status,fleet_secrets.core,hetzner.fleet_coding_memory"
            + muster_imports
            + ";runpy.run_path(sys.argv[1]+'/fleet-ops-control.py');print('imports-ok')"
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
        if not has_cryptography:
            self.skipTest(
                "cryptography is not installed: muster_approval and muster_trust_files were not "
                "imported (everything else in this test ran)"
            )

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
        for name in (
            "fleet-ops-control.py",
            "fleet_resource_control.py",
            "fleet_seat_inventory.py",
            "reduced_status.py",
            "muster_approval.py",
        ):
            (source / name).write_text("pass\n")
        (source / "muster_agent").mkdir()
        (source / "muster_agent" / "muster_trust_files.py").write_text("pass\n")
        (source / "fleet-ops-control.json").write_text(json.dumps({"schemaVersion": 1}))
        (source / "fleet_secrets").symlink_to(SCRIPTS / "fleet_secrets", target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())

    def test_symlinked_reducer_source_refused_before_destination_created(self):
        source = self.root / "source"
        source.mkdir()
        for name in (
            "fleet-ops-control.py",
            "fleet_resource_control.py",
            "fleet_seat_inventory.py",
            "muster_approval.py",
        ):
            (source / name).write_text("pass\n")
        (source / "muster_agent").mkdir()
        (source / "muster_agent" / "muster_trust_files.py").write_text("pass\n")
        (source / "fleet-ops-control.json").write_text(json.dumps({"schemaVersion": 1}))
        for package, names in (
            ("fleet_secrets", ("__init__.py", "core.py")),
            ("hetzner", ("fleet_coding_memory.py", "coding_resources.py")),
        ):
            (source / package).mkdir()
            for name in names:
                (source / package / name).write_text("pass\n")
        real = self.root / "real-reduced-status.py"
        real.write_text("pass\n")
        (source / "reduced_status.py").symlink_to(real)
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())
        # The same source with a plain file installs, so the refusal was the symlink.
        (source / "reduced_status.py").unlink()
        (source / "reduced_status.py").write_text("pass\n")
        self.install(source)
        self.assertEqual((self.destination / "reduced_status.py").read_text(), "pass\n")

    def test_the_reducer_sits_beside_the_helper_in_the_scripts_folder(self):
        self.assertTrue((SCRIPTS / "reduced_status.py").is_file())
        self.assertTrue((SCRIPTS / "fleet-ops-control.py").is_file())
        self.assertFalse((SCRIPTS / "simple_tools" / "reduced_status.py").exists())

    def minimal_source(self, trust_text="pass\n"):
        """A small valid source tree.  `trust_text` None leaves out the `muster_agent` folder."""
        source = self.root / "source"
        source.mkdir()
        for name in (
            "fleet-ops-control.py",
            "fleet_resource_control.py",
            "fleet_seat_inventory.py",
            "reduced_status.py",
            "muster_approval.py",
        ):
            (source / name).write_text("pass\n")
        (source / "fleet-ops-control.json").write_text(json.dumps({"schemaVersion": 1}))
        if trust_text is not None:
            (source / "muster_agent").mkdir()
            (source / "muster_agent" / "muster_trust_files.py").write_text(trust_text)
        for package, names in (
            ("fleet_secrets", ("__init__.py", "core.py")),
            ("hetzner", ("fleet_coding_memory.py", "coding_resources.py")),
        ):
            (source / package).mkdir()
            for name in names:
                (source / package / name).write_text("pass\n")
        return source

    def test_the_trust_files_reader_comes_from_muster_agent_only_and_is_installed_flat(self):
        source = self.minimal_source("REAL = 1\n")
        # A flat file of the same name in the source folder is never the one that is installed.
        (source / "muster_trust_files.py").write_text("DECOY = 1\n")
        self.install(source)
        self.assertEqual((self.destination / "muster_trust_files.py").read_text(), "REAL = 1\n")
        self.assertFalse((self.destination / "muster_agent").exists())

    def test_a_source_without_muster_agent_or_with_a_linked_one_is_refused(self):
        source = self.minimal_source(None)
        (source / "muster_trust_files.py").write_text("DECOY = 1\n")
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())
        real = self.root / "elsewhere"
        real.mkdir()
        (real / "muster_trust_files.py").write_text("pass\n")
        (source / "muster_agent").symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source)
        self.assertFalse(self.destination.exists())

    def test_the_other_installed_copy_must_be_the_same_bytes_when_it_is_present(self):
        source = self.minimal_source("SAME = 1\n")
        self.install(source)  # no muster-agent copy exists: nothing to compare
        other = self.root / "usr/local/lib/muster-agent"
        other.mkdir()
        (other / "muster_trust_files.py").write_text("SAME = 1\n")
        self.install(source)  # the same bytes: fine
        (other / "muster_trust_files.py").write_text("OTHER = 1\n")
        with self.assertRaises(RuntimeError):
            self.install(source)
        (other / "muster_trust_files.py").unlink()
        (other / "muster_trust_files.py").symlink_to(self.root / "elsewhere")
        with self.assertRaises(OSError):
            self.install(source)

    def test_a_linked_muster_agent_installation_folder_is_never_followed(self):
        # The link leads to a folder with the very same bytes, so that a link that is followed
        # would pass the comparison: only the refusal to follow it stops the install.
        source = self.minimal_source("SAME = 1\n")
        real = self.root / "elsewhere-agent"
        real.mkdir()
        (real / "muster_trust_files.py").write_text("SAME = 1\n")
        lib = self.root / "usr/local/lib"
        lib.mkdir(parents=True)
        for folder in (self.root / "usr", self.root / "usr/local", lib):
            folder.chmod(0o755)  # whatever the umask:  the installer refuses a group-writable one
        (lib / "muster-agent").symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(source)
        # The same folder as a real one, with the same bytes, is compared and passes.
        (lib / "muster-agent").unlink()
        (lib / "muster-agent").mkdir()
        (lib / "muster-agent" / "muster_trust_files.py").write_text("SAME = 1\n")
        self.install(source)

    def test_the_repository_has_exactly_one_source_of_the_trust_files_reader(self):
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=SCRIPTS.parent,
            capture_output=True,
            check=False,
        )
        if listed.returncode != 0:
            self.skipTest("not a git checkout")
        names = [
            path.decode()
            for path in listed.stdout.split(b"\0")
            if path.endswith(b"muster_trust_files.py")
        ]
        self.assertEqual(names, ["scripts/muster_agent/muster_trust_files.py"])
        text = (HERE / "install-resource-control.sh").read_text()
        # The installer names that one source path, and no other place the file could come from.
        self.assertIn("os.open('muster_agent', DIRECTORY_FLAGS, dir_fd=source)", text)
        self.assertIn("read_source(agent, 'muster_trust_files.py')", text)
        self.assertEqual(text.count("read_source(source, 'muster_trust_files.py')"), 0)

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
