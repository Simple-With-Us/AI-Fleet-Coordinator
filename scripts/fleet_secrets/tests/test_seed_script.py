"""seed-system-keychain.sh against a fake `security`:  no real keychain is touched.

The fake stores each item as a file named for its account and records every argv, so the tests can
check the flags (-U, -A, service, keychain path) and that no value reaches stdout or stderr."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "seed-system-keychain.sh"
ID_VAL = "SENTINEL_CLIENT_ID_c0de"
SECRET_VAL = "SENTINEL_CLIENT_SECRET_5ec7"

FAKE_SECURITY = r"""#!/bin/bash
store="$FAKE_STORE"
echo "$*" >> "$store/argv.log"
case "$1" in
  add-generic-password)
    [ -e "$store/fail" ] && exit 1
    acct=""; val=""
    while [ $# -gt 0 ]; do
      case "$1" in -a) acct="$2"; shift;; -w) val="$2"; shift;; esac
      shift
    done
    printf '%s' "$val" > "$store/$acct"
    ;;
  find-generic-password)
    acct=""
    while [ $# -gt 0 ]; do case "$1" in -a) acct="$2"; shift;; esac; shift; done
    [ -f "$store/$acct" ] || exit 44
    [ -e "$store/corrupt" ] && { echo wrong; exit 0; }
    cat "$store/$acct"; echo
    ;;
  *) exit 2;;
esac
"""


class SeedScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        fake = self.store / "security"
        fake.write_text(FAKE_SECURITY)
        fake.chmod(0o755)
        self.fake = fake

    def run_script(self, stdin: str):
        env = {
            "PATH": "/usr/bin:/bin",
            "SECURITY_BIN": str(self.fake),
            "FAKE_STORE": str(self.store),
            "FLEET_SECRETS_KEYCHAIN": "/fake/System.keychain",
        }
        return subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            input=stdin.encode(),
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )

    def argv_log(self):
        return (self.store / "argv.log").read_text().splitlines()

    def test_writes_both_items_with_the_right_flags_and_reports_booleans_only(self):
        p = self.run_script(ID_VAL + "\n" + SECRET_VAL + "\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout.decode()
        self.assertIn("infisical-client-id match: true", out)
        self.assertIn("infisical-client-secret match: true", out)
        for v in (ID_VAL, SECRET_VAL):
            self.assertNotIn(v, out)
            self.assertNotIn(v, p.stderr.decode())
        adds = [line for line in self.argv_log() if line.startswith("add-generic-password")]
        self.assertEqual(len(adds), 2)
        for line in adds:
            self.assertIn("-U", line.split())
            self.assertIn("-A", line.split())
            self.assertIn("-s fleet-secrets", line)
            self.assertTrue(line.endswith("/fake/System.keychain"))
        self.assertEqual((self.store / "infisical-client-id").read_text(), ID_VAL)
        self.assertEqual((self.store / "infisical-client-secret").read_text(), SECRET_VAL)

    def test_a_missing_second_line_writes_nothing(self):
        p = self.run_script(ID_VAL + "\n")
        self.assertEqual(p.returncode, 3)
        self.assertFalse((self.store / "argv.log").exists())
        self.assertNotIn(ID_VAL, p.stdout.decode() + p.stderr.decode())

    def test_empty_lines_write_nothing(self):
        p = self.run_script("\n\n")
        self.assertEqual(p.returncode, 3)
        self.assertFalse((self.store / "argv.log").exists())

    def test_crlf_input_is_trimmed(self):
        p = self.run_script(ID_VAL + "\r\n" + SECRET_VAL + "\r\n")
        self.assertEqual(p.returncode, 0)
        self.assertEqual((self.store / "infisical-client-id").read_text(), ID_VAL)

    def test_a_failed_add_exits_nonzero_without_leaking(self):
        (self.store / "fail").write_text("")
        p = self.run_script(ID_VAL + "\n" + SECRET_VAL + "\n")
        self.assertEqual(p.returncode, 4)
        self.assertNotIn(SECRET_VAL, p.stdout.decode() + p.stderr.decode())

    def test_a_mismatched_readback_reports_false_and_fails(self):
        (self.store / "corrupt").write_text("")
        p = self.run_script(ID_VAL + "\n" + SECRET_VAL + "\n")
        self.assertEqual(p.returncode, 5)
        out = p.stdout.decode()
        self.assertIn("infisical-client-id match: false", out)
        self.assertIn("infisical-client-secret match: false", out)
        self.assertNotIn("wrong", out)

    def test_refuses_a_terminal_and_a_non_root_run_on_the_real_keychain(self):
        env = {"PATH": "/usr/bin:/bin", "SECURITY_BIN": str(self.fake), "FAKE_STORE": str(self.store)}
        p = subprocess.run(
            ["/bin/bash", str(SCRIPT)], input=b"a\nb\n", capture_output=True, env=env, timeout=30
        )
        if os.getuid() != 0:
            self.assertEqual(p.returncode, 2)
            self.assertFalse((self.store / "argv.log").exists())


if __name__ == "__main__":
    unittest.main()
