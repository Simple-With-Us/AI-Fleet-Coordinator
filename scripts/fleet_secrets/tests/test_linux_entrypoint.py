"""Bootstrap file security checks; no real credentials or network."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "linux_entrypoint", Path(__file__).parents[1] / "linux-entrypoint.py"
)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class BootstrapTests(unittest.TestCase):
    def test_only_owned_private_regular_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bootstrap.json"
            path.write_text(
                json.dumps(
                    {
                        "INFISICAL_AUTOMATION_CLIENT_ID": "fake-id",
                        "INFISICAL_AUTOMATION_CLIENT_SECRET": "fake-secret",
                    }
                )
            )
            path.chmod(0o600)
            self.assertEqual(entry.load_bootstrap(path), ["fake-id", "fake-secret"])
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                entry.load_bootstrap(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                entry.load_bootstrap(link)

    def test_missing_pair_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bootstrap.json"
            path.write_text("{}")
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                entry.load_bootstrap(path)

    def test_failed_login_never_prints_child_output(self):
        result = mock.Mock(
            returncode=1, stdout="FAKE_SECRET_FROM_CHILD", stderr="FAKE_SECRET_FROM_CHILD"
        )
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(entry, "load_bootstrap", return_value=["fake-id", "fake-secret"]),
            mock.patch.object(entry.subprocess, "run", return_value=result) as run,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(entry.main(), 4)
        self.assertNotIn("FAKE_SECRET_FROM_CHILD", stderr.getvalue())
        self.assertNotIn("fake-secret", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
