"""Install only synthetic temporary code; never read the real root bootstrap."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]


class InstallerTests(unittest.TestCase):
    def _run_installer(self, env, extra_args=(), uname="Linux", uid="0"):
        wrapper = Path(env["HOME"]) / "run-install.sh"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"_fleet_secrets_uname_s() {{ echo {uname}; }}\n"
            f"_fleet_secrets_uid() {{ echo {uid}; }}\n"
            f'source "{SCRIPTS / "install-fleet-secrets.sh"}"\n'
            'install_fleet_secrets_main "$@"\n'
        )
        wrapper.chmod(0o755)
        args = [str(wrapper), *extra_args]
        return subprocess.run(args, env=env, capture_output=True, check=False)

    def test_installed_launchers_delegate_without_recursion_and_help_needs_no_auth(self):
        for opt_in in (False, True):
            with self.subTest(opt_in=opt_in), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = dict(os.environ, HOME=tmp)
                for key in list(env):
                    if key.startswith(("INFISICAL_", "FLEET_SECRETS_")):
                        env.pop(key)
                extra = ("--linux-private-bootstrap",) if opt_in else ()
                result = self._run_installer(env, extra)
                self.assertEqual(result.returncode, 0, result.stderr)
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
                revert = self._run_installer(env)
                self.assertEqual(revert.returncode, 0, revert.stderr)
                self.assertEqual(command.resolve().name, "fleet-secrets")
                self.assertFalse((home / ".local/share/fleet-secrets/linux-entrypoint.py").exists())
                self.assertFalse((home / ".local/share/fleet-secrets/fleet-secrets-cli").exists())

    def test_the_bootstrap_gate_refuses_a_non_linux_non_root_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp)
            for key in list(env):
                if key.startswith(("INFISICAL_", "FLEET_SECRETS_")):
                    env.pop(key)
            result = self._run_installer(
                env, ("--linux-private-bootstrap",), uname="Darwin", uid="1000"
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn(b"requires Linux root", result.stderr)
            self.assertFalse((Path(tmp) / ".local/bin/fleet-secrets").exists())
            self.assertFalse(
                (Path(tmp) / ".local/share/fleet-secrets/linux-entrypoint.py").exists()
            )

    def test_rejects_unknown_flags_before_installing(self):
        result = subprocess.run(
            ["bash", str(SCRIPTS / "install-fleet-secrets.sh"), "--unknown"],
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
