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
            try:
                run = subprocess.run(
                    [sys.executable, str(ROOT / "export_catalog.py"), f.name],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                self.fail("export_catalog.py exceeded its 30-second test budget")

        names = [t["name"] for t in json.loads(run.stdout)["tools"]]
        self.assertEqual(names, ["gr_open_pr", "gr_wf_pr_edit"])

    def test_committed_catalog_does_not_advertise_retired_creation(self):
        catalog = json.loads((ROOT / "catalog.json").read_text())
        self.assertNotIn("gr_wf_pr_create", [tool["name"] for tool in catalog["tools"]])

    def test_committed_catalog_tools_match_export_and_ignore_stale_create(self):
        catalog = json.loads((ROOT / "catalog.json").read_text())
        capture = {"tools": [{"name": tool["name"], "description": tool["description"],
            "title": tool.get("title", ""), "inputSchema": tool["schema"]}
            for tool in catalog["tools"]]}
        capture["tools"].append({"name": "gr_wf_pr_create", "inputSchema": {}})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as f:
            json.dump(capture, f)
            f.flush()
            try:
                run = subprocess.run([sys.executable, str(ROOT / "export_catalog.py"), f.name],
                    capture_output=True, text=True, check=True, timeout=30)
            except subprocess.TimeoutExpired:
                self.fail("catalog parity export exceeded its 30-second test budget")
        # Provenance is historical capture metadata, not derived from tool contracts.
        self.assertEqual(json.loads(run.stdout)["tools"], catalog["tools"])
