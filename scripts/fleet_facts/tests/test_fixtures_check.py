"""The CI fixture check:  it passes the real tests folder and fails each kind of planted bad input."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fleet_facts import check_fixtures as C

PACKAGE = Path(C.__file__).resolve().parent
KEYS = C.real_team_keys()


def planted(*parts: str) -> str:
    return "".join(parts)


class RealTree(unittest.TestCase):
    def test_the_real_tests_folder_is_clean(self):
        self.assertEqual(C.check_tree(PACKAGE / "tests"), [])

    def test_the_command_exits_zero_on_it(self):
        proc = subprocess.run([sys.executable, str(PACKAGE / "check_fixtures.py")], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_team_keys_match_the_board_script(self):
        text = (PACKAGE.parent / "linear-board" / "board").read_text(encoding="utf-8")
        self.assertIn("TEAM_KEYS", text)
        self.assertEqual(set(KEYS), set(C.FALLBACK_TEAM_KEYS), "update FALLBACK_TEAM_KEYS in check_fixtures.py")


class PlantedBad(unittest.TestCase):
    def rules(self, text: str) -> list[str]:
        return [rule for _, rule in C.check_text(text, KEYS, C._scanner())]

    def test_each_bad_input_is_caught(self):
        cases = {
            "real path": planted("path = '", "/Us", "ers/someone/apps'"),
            "real path ": planted("disk = '", "/Vol", "umes/External/x'"),
            "real Linear identifier": planted("ticket ", KEYS[0], "-", "123", " is open"),
            "non-synthetic UUID": planted("id = '", "5f1c2d3e-4a5b-4c6d-8e7f-", "0123456789ab", "'"),
            "real identity": planted("owner ", "jay", "wedge", "worth"),
            "secret-shaped text": planted("token ", "ghp_", "a1B2c3D4" * 5),
        }
        for rule, text in cases.items():
            self.assertIn(rule.strip(), self.rules(text), rule)

    def test_every_real_team_key_is_caught(self):
        for key in KEYS:
            self.assertIn("real Linear identifier", self.rules(key + "-7"), key)

    def test_synthetic_data_passes(self):
        good = ["FFX-1 and FFX-22 are fake", "id = '00000000-0000-4000-8000-0000000000ab'", "path = /home/example/apps/lanes/x",
                "BF-DIRECTOR is a seat name, not an identifier", "Simple-With-Us/example-repo", "sha " + "a" * 40]
        for line in good:
            self.assertEqual(self.rules(line), [], line)

    def test_a_uuid_that_is_only_almost_synthetic_still_fails(self):
        for almost in (planted("00000000-0000-4000-9000-", "000000000001"), planted("00000001-0000-4000-8000-", "000000000001")):
            self.assertIn("non-synthetic UUID", self.rules("id " + almost), almost)

    def test_the_command_fails_and_never_prints_the_matched_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "fixtures"
            bad.mkdir()
            secret = planted("ghp_", "z9Y8x7W6" * 5)
            (bad / "a.json").write_text('{"x": "%s", "p": "%s"}\n' % (secret, planted("/Us", "ers/x")), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(PACKAGE / "check_fixtures.py"), "--root", tmp], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("fixtures/a.json:1:real path", proc.stdout)
            self.assertIn("secret-shaped text", proc.stdout)
            self.assertNotIn(secret, proc.stdout + proc.stderr)

    def test_usage_error(self):
        proc = subprocess.run([sys.executable, str(PACKAGE / "check_fixtures.py"), "--root", "/no/such/folder"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
