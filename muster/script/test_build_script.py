"""The build script is valid shell, and it fails closed on the keychain.

The keychain commands are run through a stand-in binary, so no real keychain
is read, no identity is chosen from the owner's certificates, and no unlock
prompt can reach the owner's screen.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent
BUILD_AND_RUN = SCRIPT / "build_and_run.sh"

import build_support as support


class BuildScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_build_script_is_valid_shell(self):
        result = subprocess.run(["bash", "-n", str(BUILD_AND_RUN)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_script_delegates_the_keychain_work(self):
        text = BUILD_AND_RUN.read_text()
        self.assertNotIn("find-identity", text)
        self.assertNotIn("list-keychains", text)
        self.assertIn('build_support.py" signing-identity', text)
        self.assertIn('build_support.py" keychain-preflight', text)

    def test_the_owners_search_list_message_lives_with_the_logic(self):
        text = BUILD_AND_RUN.read_text()
        self.assertNotIn("not on the keychain search list", text)
        self.assertIn("not on the keychain search list", support.KEYCHAIN_SEARCH_LIST_MESSAGE)
        self.assertIn("security list-keychains -s", support.KEYCHAIN_SEARCH_LIST_MESSAGE)

    def stand_in_security(self, body):
        path = pathlib.Path(self.tmp.name) / "security"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return str(path)

    def run_cli(self, command, security, timeout=None):
        """Run the entry point in a child process with a stand-in security."""
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(SCRIPT)!r})\n"
            "import build_support as support\n"
            f"support.SECURITY = {security!r}\n"
        )
        # Only a timeout that was asked for replaces the default.  Setting it to
        # None would take the deadline away,  and the fail-closed behaviour the
        # tests are here for needs the deadline.
        if timeout is not None:
            code += f"support.SECURITY_TIMEOUT = {timeout!r}\n"
        code += f"sys.exit(support.main(['build_support', {command!r}]))\n"
        return subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )

    def test_a_timeout_fails_the_build_with_a_diagnostic_and_no_traceback(self):
        security = self.stand_in_security("exec sleep 30\n")
        result = self.run_cli("keychain-preflight", security, timeout=0.5)
        self.assertEqual(result.returncode, 124)
        self.assertIn("did not answer", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_missing_security_binary_fails_the_build_with_a_diagnostic(self):
        missing = str(pathlib.Path(self.tmp.name) / "not-security")
        result = self.run_cli("keychain-preflight", missing)
        self.assertEqual(result.returncode, 127)
        self.assertIn("is missing", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_failing_security_command_names_the_cause_without_a_traceback(self):
        security = self.stand_in_security(
            "echo 'security: SecKeychainSearchCopyNext: refused' >&2\nexit 1\n"
        )
        result = self.run_cli("signing-identity", security)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("find-identity", result.stderr)
        self.assertIn("refused", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_search_list_problem_still_names_the_fix(self):
        security = self.stand_in_security("echo '    \"/Library/Keychains/System.keychain\"'\n")
        result = self.run_cli("keychain-preflight", security)
        self.assertEqual(result.returncode, 3)
        self.assertIn("not on the keychain search list", result.stderr)
        self.assertIn("security list-keychains -s", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_identity_is_chosen_from_both_keychains_and_printed_as_public_metadata(self):
        identity = '  1) 1A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D "Developer ID Application: Alpha Company (ABCDE12345)"'
        security = self.stand_in_security(
            "if [ -z \"$5\" ]; then\n  echo '%s'\nelse\n  echo '%s'\nfi\n" % (identity, identity)
        )
        result = self.run_cli("signing-identity", security)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "1A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D ABCDE12345",
        )

    def test_a_failing_path_less_search_still_lets_the_by_path_lookup_sign(self):
        """An unreadable search list must not stop the by-path lookup."""
        identity = '  1) 1A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D "Developer ID Application: Alpha Company (ABCDE12345)"'
        security = self.stand_in_security(
            'if [ -z "$5" ]; then\n'
            "  echo 'security: SecKeychainSearchCopyNext: refused' >&2\n"
            "  exit 1\n"
            "else\n"
            "  echo '%s'\n"
            "fi\n" % identity
        )
        result = self.run_cli("signing-identity", security)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1A2B3C4D5E6F708192A3B4C5D6E7F8091A2B3C4D", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_the_script_entry_point_reports_a_usage_error(self):
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT / "build_support.py")],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            self.fail("the usage check hung instead of reporting a usage error")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
