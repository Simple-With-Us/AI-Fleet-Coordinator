"""Synthetic transaction tests; no keychain, application or process is touched."""

import errno
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_support as support


class BuildSupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.staged, self.installed = root / "staged.app", root / "installed.app"
        for path, text in ((self.staged, "new"), (self.installed, "old")):
            path.mkdir()
            (path / "version").write_text(text)

    def test_existing_lock_is_distinct_from_other_mkdir_errors(self):
        with self.assertRaises(RuntimeError):
            support.acquire_lock(self.installed)
        with mock.patch.object(Path, "mkdir", side_effect=PermissionError(errno.EACCES, "refused")):
            with self.assertRaises(PermissionError):
                support.acquire_lock(self.installed.parent / "lock")

    def test_success_prepares_on_target_filesystem_and_verifies_before_replacing(self):
        observed = []

        def verify(path):
            observed.append((path, (self.installed / "version").read_text()))

        support.install_bundle(self.staged, self.installed, verify)
        self.assertEqual([version for _, version in observed], ["old", "new"])
        self.assertEqual((self.installed / "version").read_text(), "new")
        self.assertEqual(list(self.installed.parent.glob(".muster-install-*")), [])

    def test_copied_bytes_and_rename_directories_are_synced(self):
        events = []
        replace = support.os.replace

        def rename(source, destination):
            events.append(("rename", Path(source).name))
            replace(source, destination)

        with (
            mock.patch.object(
                support, "fsync_tree", side_effect=lambda path: events.append(("tree", path.name))
            ),
            mock.patch.object(
                support, "fsync_path", side_effect=lambda path: events.append(("directory", path))
            ),
            mock.patch.object(support.os, "replace", side_effect=rename),
        ):
            support.install_bundle(self.staged, self.installed, lambda _: None)
        tree = events.index(("tree", "new.app"))
        first_rename = next(i for i, event in enumerate(events) if event[0] == "rename")
        self.assertLess(tree, first_rename)
        renames = [i for i, event in enumerate(events) if event[0] == "rename"]
        for position in renames:
            self.assertEqual(events[position + 1][0], "directory")
            self.assertEqual(events[position + 2], ("directory", self.installed.parent))

    def test_install_flushes_the_copied_bundle_and_the_install_directory(self):
        flushed = []
        with mock.patch.object(support, "fsync_path", side_effect=flushed.append):
            support.install_bundle(self.staged, self.installed, lambda _: None)
        self.assertIn(self.installed.parent, flushed)
        self.assertTrue(any(path.name == "version" for path in flushed))

    def test_copy_failure_keeps_original(self):
        with mock.patch.object(support.shutil, "copytree", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                support.install_bundle(self.staged, self.installed, lambda _: None)
        self.assertEqual((self.installed / "version").read_text(), "old")

    def test_failed_replacement_restores_original(self):
        replace = support.os.replace

        def fail_new(source, destination):
            if Path(source).name == "new.app":
                raise OSError("rename failed")
            return replace(source, destination)

        with mock.patch.object(support.os, "replace", side_effect=fail_new):
            with self.assertRaises(OSError):
                support.install_bundle(self.staged, self.installed, lambda _: None)
        self.assertEqual((self.installed / "version").read_text(), "old")

    def test_verification_failure_and_interrupt_restore_original(self):
        for error in (ValueError("bad signature"), KeyboardInterrupt()):

            def verify(path):
                if path == self.installed:
                    raise error

            with self.assertRaises(type(error)):
                support.install_bundle(self.staged, self.installed, verify)
            self.assertEqual((self.installed / "version").read_text(), "old")

    def test_failed_first_install_leaves_no_bundle(self):
        shutil.rmtree(self.installed)

        def verify(path):
            if path == self.installed:
                raise ValueError("bad signature")

        with self.assertRaises(ValueError):
            support.install_bundle(self.staged, self.installed, verify)
        self.assertFalse(os.path.lexists(self.installed))
        self.assertEqual(list(self.installed.parent.glob(".muster-install-*")), [])

    def test_failed_rollback_preserves_recovery_copy(self):
        replace = support.os.replace

        def fail_restore(source, destination):
            if Path(source).name in ("new.app", "previous.app"):
                raise OSError("rename failed")
            return replace(source, destination)

        with mock.patch.object(support.os, "replace", side_effect=fail_restore):
            with self.assertRaises(OSError):
                support.install_bundle(self.staged, self.installed, lambda _: None)
        backups = list(self.installed.parent.glob(".muster-install-*/previous.app/version"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "old")

    def test_interrupt_after_old_rename_restores_original(self):
        replace = support.os.replace

        def interrupt_after_old(source, destination):
            replace(source, destination)
            if Path(destination).name == "previous.app":
                raise KeyboardInterrupt()

        with mock.patch.object(support.os, "replace", side_effect=interrupt_after_old):
            with self.assertRaises(KeyboardInterrupt):
                support.install_bundle(self.staged, self.installed, lambda _: None)
        self.assertEqual((self.installed / "version").read_text(), "old")


class ProcessInspectionTests(unittest.TestCase):
    binary = Path("/synthetic/Applications/Muster.app/Contents/MacOS/Muster")

    def result(self, output="", status=0):
        return subprocess.CompletedProcess([], status, output, "")

    def test_matches_exact_executable_and_ignores_other_copy(self):
        results = [
            self.result("12\n13\n"),
            self.result("p12\nn" + str(self.binary) + "\n"),
            self.result("p13\nn/other/Muster.app/Contents/MacOS/Muster\n"),
        ]
        with mock.patch.object(support.subprocess, "run", side_effect=results) as run:
            self.assertEqual(support.installed_pids(self.binary), ["12"])
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/pgrep", "-x", "Muster"])
        for call in run.call_args_list[1:]:
            self.assertIn("-b", call.args[0])
            self.assertIn("-nP", call.args[0])
            self.assertEqual(call.args[0][-3:], ["-d", "txt", "-Fn"])
            self.assertEqual(call.kwargs["timeout"], 5)

    def test_no_match_is_empty_and_discovery_error_refuses(self):
        for status in (1, 2):
            with mock.patch.object(
                support.subprocess, "run", return_value=self.result(status=status)
            ):
                if status == 1:
                    self.assertEqual(support.installed_pids(self.binary), [])
                else:
                    with self.assertRaises(RuntimeError):
                        support.installed_pids(self.binary)

    def test_uninspectable_active_process_refuses(self):
        for inspected in (
            self.result(status=1),
            self.result("p12\n"),
            subprocess.TimeoutExpired("synthetic", 5),
        ):
            with (
                mock.patch.object(
                    support.subprocess, "run", side_effect=[self.result("12\n"), inspected]
                ),
                mock.patch.object(support.os, "kill", return_value=None),
            ):
                with self.assertRaises(RuntimeError):
                    support.installed_pids(self.binary)

    def test_nonblocking_inspection_failure_never_trusts_a_partial_path(self):
        with (
            mock.patch.object(
                support.subprocess,
                "run",
                side_effect=[
                    self.result("12\n"),
                    self.result("p12\nn" + str(self.binary) + "\n", status=1),
                ],
            ),
            mock.patch.object(support.os, "kill", return_value=None) as probe,
        ):
            with self.assertRaises(RuntimeError):
                support.installed_pids(self.binary)
        probe.assert_called_once_with(12, 0)

    def test_process_exit_during_inspection_is_harmless(self):
        with (
            mock.patch.object(
                support.subprocess, "run", side_effect=[self.result("12\n"), self.result(status=1)]
            ),
            mock.patch.object(support.os, "kill", side_effect=ProcessLookupError),
        ):
            self.assertEqual(support.installed_pids(self.binary), [])

    def test_malformed_pid_refuses_before_inspection(self):
        with mock.patch.object(
            support.subprocess, "run", return_value=self.result("not-a-pid\n")
        ) as run:
            with self.assertRaises(RuntimeError):
                support.installed_pids(self.binary)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
