import pathlib
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
INSTALLER = HERE / "install-infisical-scoped.sh"
CLIENT = HERE / "fleet_infisical.py"


class DestinationIsAPathTests(unittest.TestCase):
    """The destination is a path,  never an option.

    `install` reads a leading "-" as a flag,  so a destination that begins with one
    must land as a directory name and never reach the child process as a flag.
    """

    def test_a_destination_that_begins_with_a_dash_is_installed_as_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["bash", str(INSTALLER), "-weird"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = pathlib.Path(tmp) / "-weird"
            self.assertTrue((installed / "fleet_infisical.py").is_file())
            self.assertTrue((installed / "infisical-scoped.py").is_file())
            self.assertEqual(
                (installed / "fleet_infisical.py").read_bytes(), CLIENT.read_bytes()
            )

    def test_a_destination_that_begins_with_a_dash_is_not_an_option(self):
        # The child must never see the destination as a flag:  the install lands under
        # the current directory,  and the child prints no option complaint.
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                ["bash", str(INSTALLER), "--help"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("illegal option", result.stderr)
            self.assertNotIn("usage: install", result.stderr)
            self.assertTrue((pathlib.Path(tmp) / "--help" / "fleet_infisical.py").is_file())


if __name__ == "__main__":
    unittest.main()
