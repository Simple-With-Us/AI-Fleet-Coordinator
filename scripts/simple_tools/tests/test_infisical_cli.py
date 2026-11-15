"""The shared command must not import a hostile current-directory reader."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class IsolatedCommandTests(unittest.TestCase):
    def test_selected_binding_without_its_packaged_helper_fails_closed(self):
        scripts = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            for name in ("fleet_infisical_cli.py", "fleet_infisical.py"):
                shutil.copyfile(scripts / name, release / name)
            hostile = root / "hostile"
            hostile.mkdir()
            (hostile / "fleet_infisical_bootstrap.py").write_text(
                "raise AssertionError('foreign bootstrap executed')\n"
            )
            result = subprocess.run(
                [sys.executable, "-I", str(release / "fleet_infisical_cli.py"), "names",
                 "--consumer-binding", str(root / "missing.json")],
                cwd=hostile,
                env={"HOME": directory, "PATH": os.defpath, "PYTHONPATH": str(hostile)},
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 4)
            self.assertEqual(result.stdout, "")
            self.assertIn("consumer-binding-helper-unavailable", result.stderr)
            self.assertNotIn("foreign bootstrap", result.stderr)
            self.assertNotIn(str(root), result.stderr)

    def test_coding_memory_installer_payload_can_run_its_delegate_in_isolation(self):
        scripts = Path(__file__).resolve().parents[2]
        installer = (scripts / "hetzner/install-coding-memory.sh").read_text()
        start = 'install -m 0700 "$scripts/fleet-secrets"'
        payload = start + installer.split(start, 1)[1].split('python3 - "$config/environment"', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "coding-memory"
            settings = destination / "settings"
            (settings / "fleet_secrets").mkdir(parents=True)
            hostile = root / "hostile"
            hostile.mkdir()
            (hostile / "fleet_infisical_cli.py").write_text("raise AssertionError('foreign command')\n")
            installed = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -eu; scripts=$1; dest=$2\n' + payload,
                    "payload",
                    str(scripts),
                    str(destination),
                ],
                env={"PATH": os.defpath},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            for name in ("fleet_infisical.py", "fleet_infisical_cli.py"):
                self.assertEqual((settings / name).read_bytes(), (scripts / name).read_bytes())
            result = subprocess.run(
                [sys.executable, "-I", str(settings / "linux-entrypoint.py"), "--help"],
                cwd=hostile,
                env={
                    "HOME": directory,
                    "PATH": os.defpath,
                    "PYTHONPATH": str(hostile),
                    "FLEET_SECRETS_CLI": str(settings / "fleet-secrets"),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("names", result.stdout)
            self.assertNotIn("foreign command", result.stdout + result.stderr)

    def test_bridge_release_uses_shared_command_without_compatibility_package(self):
        scripts = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "release/lib"
            library.mkdir(parents=True)
            launcher = root / "release/scripts/whatsapp_bridge/launch.py"
            launcher.parent.mkdir(parents=True)
            shutil.copyfile(scripts / "whatsapp_bridge/launch.py", launcher)
            for name in ("fleet_infisical.py", "fleet_infisical_cli.py"):
                shutil.copyfile(scripts / name, library / name)
            hostile = root / "hostile"
            hostile.mkdir()
            (hostile / "fleet_infisical_cli.py").write_text("raise AssertionError('foreign command')\n")
            code = (
                "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
                "import fleet_infisical_cli;fleet_infisical_cli.cmd_run=lambda *args:0;"
                "entry=runpy.run_path(sys.argv[2])['main'];"
                "entry.__globals__['load_service_token']=lambda *args:'synthetic';"
                "entry.__globals__['prepare']=lambda *args:{};"
                "assert entry(['bind'],{'CREDENTIALS_DIRECTORY':'unused'})==0;print('bridge-import-ok')"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-c", code, str(library), str(launcher)],
                cwd=hostile,
                env={"HOME": directory, "PATH": os.defpath, "PYTHONPATH": str(hostile)},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertFalse((library / "fleet_secrets").exists())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "bridge-import-ok")

    def test_absolute_isolated_entry_ignores_hostile_cwd_and_pythonpath(self):
        entry = Path(__file__).resolve().parents[2] / "fleet_infisical_cli.py"
        with tempfile.TemporaryDirectory() as directory:
            hostile = Path(directory)
            for name in ("fleet_infisical_cli.py", "fleet_infisical.py"):
                (hostile / name).write_text("raise AssertionError('foreign reader executed')\n")
            result = subprocess.run(
                [sys.executable, "-I", str(entry), "--help"],
                cwd=hostile,
                env={
                    "HOME": directory,
                    "PATH": os.defpath,
                    "PYTHONPATH": directory,
                    "INFISICAL_TOKEN": "synthetic-fixture",
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("names", result.stdout)
        self.assertNotIn("foreign reader", result.stdout + result.stderr)
        self.assertNotIn("synthetic-fixture", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
