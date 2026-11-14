"""The Docs coordinator uses the real signed Git descriptor and selected-blob verifier."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import remote_git_source
from test_remote_git_source import ProviderFixture

from fleet_docs import pipeline, snapshot


class RemotePipelineTests(unittest.TestCase):
    def setUp(self):
        self.provider = ProviderFixture(private=True)
        self.provider.owner_repo = "Simple-With-Us/Kodus-Config"
        self.configuration = {
            "peer": "mac",
            "actor": "CODEX",
            "private_docs": [self.provider.owner_repo],
            "remote_sources": [
                {
                    "owner_repo": self.provider.owner_repo,
                    "repository_id": 123,
                    "visibility": "private",
                }
            ],
        }
        self.exporter = SimpleNamespace(
            verify_managed_route=lambda environ=None: "CODEX",
            relay_read=lambda tool, args, **kwargs: self.provider.call(tool, args),
            remote=remote_git_source,
        )

    def test_adapter_exports_and_imports_proven_source_without_excluded_content(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(pipeline, "load_exporter", return_value=self.exporter),
        ):
            descriptors = pipeline.prepare_remote_sources(self.configuration, Path(tmp))
            self.assertEqual(len(descriptors), 1)
            descriptor, identity = descriptors[0]
            loaded = remote_git_source.load_descriptor(descriptor)
            self.assertEqual(loaded.identity["source_commit"], self.provider.commit)
            self.assertEqual(loaded.identity["repository_id"], 123)
            snapshot.collector().discover_repositories(
                None, git_source_descriptors=[descriptor], private_docs_repo=identity
            )
            proof = json.loads((Path(tmp) / "relay-proof.json").read_text())[0]
            self.assertEqual(proof["actor"], "CODEX")
            self.assertEqual(proof["source_commit"], self.provider.commit)
            self.assertEqual(len(proof["descriptor_sha256"]), 64)
        paths = {args["path"] for tool, args in self.provider.calls if tool == "gr_wf_file_get"}
        self.assertNotIn(".env", paths)
        self.assertNotIn("src/app.py", paths)

    def test_wrong_repository_identity_stops_before_branch_or_blob_reads(self):
        self.configuration["remote_sources"][0]["repository_id"] = 999
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(pipeline, "load_exporter", return_value=self.exporter),
        ):
            with self.assertRaises(snapshot.Refused):
                pipeline.prepare_remote_sources(self.configuration, Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual([tool for tool, _ in self.provider.calls], ["gr_wf_repo_get"])


if __name__ == "__main__":
    unittest.main()
