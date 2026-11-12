"""Synthetic transaction tests; no keychain, application or process is touched."""

import contextlib
import errno
import io
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


FINGERPRINT_A = "1A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D"
FINGERPRINT_B = "9F8E7D6C5B4A39281706F5E4D3C2B1A099887766"
LABEL_A = "Developer ID Application: Alpha Company (ABCDE12345)"
LABEL_B = "Developer ID Application: Beta Company (FGHIJ67890)"

# The message the owner reads, kept exactly as the script has always printed it.
EXPECTED_SEARCH_LIST_MESSAGE = (
    "The login keychain is not on the keychain search list,  so the private key that "
    "signs this app cannot be reached.  The certificate is there and the identity is "
    "correct;  the key is locked away.  The owner restores it with:\n"
    '  security list-keychains -s "$HOME/Library/Keychains/login.keychain-db" '
    "/Library/Keychains/System.keychain\n"
    "and by unlocking the login keychain,  which needs the owner's password."
)


def security_output(*identities):
    """The public metadata that security prints for each identity."""
    lines = [
        '  %d) %s "%s"' % (index, fingerprint, label)
        for index, (fingerprint, label) in enumerate(identities, start=1)
    ]
    lines.append("     %d valid identities found" % len(identities))
    return "\n".join(lines) + "\n"


def colon_form(fingerprint):
    return ":".join(fingerprint[index : index + 2] for index in range(0, len(fingerprint), 2))


class SecurityFailureTests(unittest.TestCase):
    """A crashed security command is a failed command, named and not a traceback."""

    def call_main(self, command, environment=None):
        stderr = io.StringIO()
        previous = dict(os.environ)
        try:
            os.environ.pop("MUSTER_SIGNING_IDENTITY", None)
            if environment:
                os.environ.update(environment)
            with contextlib.redirect_stderr(stderr):
                status = support.main(["build_support", command])
        finally:
            os.environ.clear()
            os.environ.update(previous)
        return status, stderr.getvalue()

    def test_timeout_fails_closed_with_a_named_diagnostic(self):
        with (
            mock.patch.object(
                support.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("security", 30),
            ),
            self.assertRaises(support.BuildFailure) as caught,
        ):
            support.run_security(["list-keychains"])
        self.assertEqual(caught.exception.status, 124)
        self.assertIn("list-keychains", str(caught.exception))

        with mock.patch.object(
            support.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("security", 30),
        ):
            status, stderr = self.call_main("keychain-preflight")
        self.assertEqual(status, 124)
        self.assertIn("did not answer", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_missing_security_binary_fails_closed_with_a_named_diagnostic(self):
        with (
            mock.patch.object(support.subprocess, "run", side_effect=FileNotFoundError("security")),
            self.assertRaises(support.BuildFailure) as caught,
        ):
            support.run_security(["list-keychains"])
        self.assertEqual(caught.exception.status, 127)
        self.assertIn(support.SECURITY, str(caught.exception))

        with mock.patch.object(
            support.subprocess, "run", side_effect=FileNotFoundError("security")
        ):
            status, stderr = self.call_main("keychain-preflight")
        self.assertEqual(status, 127)
        self.assertIn("is missing", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_failed_security_command_names_the_cause_without_a_traceback(self):
        failure = subprocess.CalledProcessError(
            1, ["security", "find-identity"], output="", stderr="security: keychain refused\n"
        )
        with mock.patch.object(support.subprocess, "run", side_effect=failure):
            with self.assertRaises(support.BuildFailure) as caught:
                support.run_security(["find-identity", "-v"])
        self.assertEqual(caught.exception.status, 1)
        self.assertIn("find-identity", str(caught.exception))
        self.assertIn("keychain refused", str(caught.exception))

        with mock.patch.object(support.subprocess, "run", side_effect=failure):
            status, stderr = self.call_main("signing-identity")
        self.assertNotEqual(status, 0)
        self.assertIn("find-identity", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_unknown_command_is_a_usage_error(self):
        status, stderr = self.call_main("nonsense")
        self.assertEqual(status, 2)
        self.assertIn("Usage", stderr)


class IdentitySelectionTests(unittest.TestCase):
    """Both keychain lookups are searched, and one identity is chosen."""

    def fake_run(self, search, by_path):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            output = search if len(command) == 5 else by_path
            return subprocess.CompletedProcess(command, 0, output, "")

        return run, calls

    def test_both_lookups_are_searched_and_the_login_keychain_is_named_by_path(self):
        run, calls = self.fake_run(
            security_output((FINGERPRINT_A, LABEL_A)),
            security_output((FINGERPRINT_B, LABEL_B)),
        )
        with mock.patch.object(support.subprocess, "run", side_effect=run):
            identities = support.find_coding_identities()

        self.assertEqual([item[0] for item in identities], [FINGERPRINT_A, FINGERPRINT_B])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], [support.SECURITY, "find-identity", "-v", "-p", "codesigning"])
        self.assertEqual(
            calls[1],
            [
                support.SECURITY,
                "find-identity",
                "-v",
                "-p",
                "codesigning",
                os.path.expanduser(support.LOGIN_KEYCHAIN),
            ],
        )

    def test_the_same_identity_in_both_keychains_is_counted_once(self):
        both = security_output((FINGERPRINT_A, LABEL_A), (FINGERPRINT_B, LABEL_B))
        run, _ = self.fake_run(both, both)
        with mock.patch.object(support.subprocess, "run", side_effect=run):
            identities = support.find_coding_identities()
        self.assertEqual(len(identities), 2)
        self.assertEqual([item[0] for item in identities], [FINGERPRINT_A, FINGERPRINT_B])

    def test_identity_in_another_keychain_is_found_and_chosen(self):
        run, _ = self.fake_run(
            security_output((FINGERPRINT_B, LABEL_B)),
            security_output((FINGERPRINT_A, LABEL_A)),
        )
        with mock.patch.object(support.subprocess, "run", side_effect=run):
            fingerprint, team = support.select_signing_identity(FINGERPRINT_B)
        self.assertEqual(fingerprint, FINGERPRINT_B)
        self.assertEqual(team, "FGHIJ67890")

    def test_requested_identity_matches_the_fingerprint_with_or_without_colons(self):
        run, _ = self.fake_run(
            security_output((FINGERPRINT_A, LABEL_A)),
            security_output((FINGERPRINT_B, LABEL_B)),
        )
        for requested in (FINGERPRINT_A, FINGERPRINT_A.lower(), colon_form(FINGERPRINT_A)):
            with mock.patch.object(support.subprocess, "run", side_effect=run):
                fingerprint, team = support.select_signing_identity(requested)
            self.assertEqual(fingerprint, FINGERPRINT_A)
            self.assertEqual(team, "ABCDE12345")

    def test_requested_identity_matches_the_exact_label_only(self):
        run, _ = self.fake_run(
            security_output((FINGERPRINT_A, LABEL_A)),
            security_output((FINGERPRINT_B, LABEL_B)),
        )
        with mock.patch.object(support.subprocess, "run", side_effect=run):
            fingerprint, _ = support.select_signing_identity(LABEL_A)
        self.assertEqual(fingerprint, FINGERPRINT_A)

        with mock.patch.object(support.subprocess, "run", side_effect=run):
            with self.assertRaises(support.BuildFailure) as caught:
                support.select_signing_identity("Alpha Company")
        self.assertIn("Expected exactly one matching valid", str(caught.exception))

    def test_ambiguous_and_missing_identities_are_refused_with_the_named_message(self):
        both = security_output((FINGERPRINT_A, LABEL_A), (FINGERPRINT_B, LABEL_B))
        run, _ = self.fake_run(both, both)
        with mock.patch.object(support.subprocess, "run", side_effect=run):
            with self.assertRaises(support.BuildFailure) as caught:
                support.select_signing_identity()
        self.assertEqual(caught.exception.status, 1)
        self.assertEqual(
            str(caught.exception),
            "Expected exactly one matching valid Developer ID Application identity; "
            "set MUSTER_SIGNING_IDENTITY to the certificate SHA-1 (colons optional) "
            "or the full 'Developer ID Application: <name> (<TEAM>)' label.",
        )

        empty, _ = self.fake_run("", "")
        with mock.patch.object(support.subprocess, "run", side_effect=empty):
            with self.assertRaises(support.BuildFailure) as caught:
                support.select_signing_identity()
        self.assertIn("Expected exactly one matching valid", str(caught.exception))

    def test_the_selected_identity_carries_no_key_material(self):
        run, _ = self.fake_run("", security_output((FINGERPRINT_A, LABEL_A)))
        with mock.patch.object(support.subprocess, "run", side_effect=run):
            fingerprint, team = support.select_signing_identity()
        self.assertEqual(fingerprint, FINGERPRINT_A)
        self.assertEqual(team, "ABCDE12345")
        self.assertNotIn("PRIVATE KEY", fingerprint + team)


class KeychainPreflightTests(unittest.TestCase):
    """The preflight names the search-list problem and fails closed."""

    def listing(self, *paths):
        return "".join('    "%s"\n' % path for path in paths)

    def test_login_keychain_on_the_search_list_passes(self):
        listing = self.listing(
            "/Library/Keychains/System.keychain",
            os.path.expanduser(support.LOGIN_KEYCHAIN),
        )
        with mock.patch.object(
            support.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, listing, "")
        ):
            self.assertIsNone(support.check_keychain_search_list())

    def test_login_keychain_off_the_search_list_is_refused_with_the_owners_message(self):
        listing = self.listing("/Library/Keychains/System.keychain")
        with (
            mock.patch.object(
                support.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, listing, ""),
            ),
            self.assertRaises(support.BuildFailure) as caught,
        ):
            support.check_keychain_search_list()
        self.assertEqual(caught.exception.status, 3)
        self.assertEqual(str(caught.exception), EXPECTED_SEARCH_LIST_MESSAGE)
        self.assertEqual(support.KEYCHAIN_SEARCH_LIST_MESSAGE, EXPECTED_SEARCH_LIST_MESSAGE)


if __name__ == "__main__":
    unittest.main()
