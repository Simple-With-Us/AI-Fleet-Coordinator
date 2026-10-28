"""Install only synthetic temporary code; never read the real root bootstrap."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]


class InstallerTests(unittest.TestCase):
    def test_installed_launchers_delegate_without_recursion_and_help_needs_no_auth(self):
        for opt_in in (False, True):
            with self.subTest(opt_in=opt_in), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                fake = home / "fake-bin"
                fake.mkdir()
                for name, output in (("id", "0"), ("uname", "Linux")):
                    executable = fake / name
                    executable.write_text("#!/bin/sh\nprintf '%s\\n' " + output + "\n")
                    executable.chmod(0o755)
                env = dict(os.environ, HOME=tmp, PATH=str(fake) + os.pathsep + os.environ["PATH"])
                for key in list(env):
                    if key.startswith(("INFISICAL_", "FLEET_SECRETS_")):
                        env.pop(key)
                args = ["bash", str(SCRIPTS / "install-fleet-secrets.sh")]
                if opt_in:
                    args.append("--linux-private-bootstrap")
                subprocess.run(args, env=env, capture_output=True, check=True)
                command = home / ".local/bin/fleet-secrets"
                self.assertEqual(
                    command.resolve().name, "linux-entrypoint.py" if opt_in else "fleet-secrets"
                )
                completed = subprocess.run(
                    [str(command), "--help"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("fleet-secrets", completed.stdout)
                # A subsequent default install restores the default plain launcher.
                subprocess.run(args[:2], env=env, capture_output=True, check=True)
                self.assertEqual(command.resolve().name, "fleet-secrets")

    def test_rejects_unknown_flags_before_installing(self):
        result = subprocess.run(
            ["bash", str(SCRIPTS / "install-fleet-secrets.sh"), "--unknown"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
