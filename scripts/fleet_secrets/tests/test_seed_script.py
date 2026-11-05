"""The retired Keychain machine-identity seeder is inert and never reads stdin."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "seed-system-keychain.sh"


class SeedScriptTests(unittest.TestCase):
    def test_refuses_without_consuming_input_or_invoking_security(self):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            input=b"synthetic-client-id\nsynthetic-client-secret\n",
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "SECURITY_BIN": "/nonexistent/security"},
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn(b"seed-system-keychain: retired", result.stderr)
        self.assertIn(b"no keychain items changed", result.stderr)
        self.assertNotIn(b"synthetic-client", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
