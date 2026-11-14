import importlib.machinery
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parent
loader = importlib.machinery.SourceFileLoader("owner_pr_cli", str(ROOT / "git"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cli
loader.exec_module(cli)


class Tests(unittest.TestCase):
    def test_legacy_call_refuses_before_credential_discovery(self):
        with patch.object(cli, "remote_bearer", side_effect=AssertionError("credential read")):
            with self.assertRaises(SystemExit):
                cli.main(["call", "gr_wf_pr_create", "--args-json", "{}"])

    def test_stale_catalog_capture_cannot_restore_retired_create(self):
        tools = [
            {"name": name, "inputSchema": {}}
            for name in ("gr_wf_pr_create", "gr_open_pr", "gr_wf_pr_edit")
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
            json.dump({"tools": tools}, f)
            f.flush()
            run = subprocess.run(
                [sys.executable, str(ROOT / "export_catalog.py"), f.name],
                capture_output=True,
                text=True,
                check=True,
            )
        names = [t["name"] for t in json.loads(run.stdout)["tools"]]
        self.assertEqual(names, ["gr_open_pr", "gr_wf_pr_edit"])
