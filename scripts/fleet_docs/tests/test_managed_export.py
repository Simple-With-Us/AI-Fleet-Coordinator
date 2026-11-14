"""The exporter accepts only the existing managed route and matching assigned actor."""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

source = Path(__file__).resolve().parents[2] / "export-repo-git-source.py"
spec = importlib.util.spec_from_file_location("managed_export_fixture", source)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ManagedExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.wrapper = self.home / ".local/bin/simple-git"
        self.wrapper.parent.mkdir(parents=True)
        self.wrapper.write_text("#!/usr/bin/python3\n# fleet-cli managed wrapper v1\n")
        self.wrapper.chmod(0o700)
        exporter._MANAGED_ROUTE_VERIFIED = False

    def invoke(self, actor="CODEX", seat="CODEX", identity=None, raw_json=None):
        def run(argv, **kwargs):
            value = (
                identity
                if identity is not None
                else {
                    "authenticated_seat": actor,
                    "default_identity": actor,
                    "identities": [{"identity": actor}],
                }
            )
            kwargs["stdout"].write(
                (raw_json if raw_json is not None else json.dumps({"structured": value})).encode()
            )
            return SimpleNamespace(returncode=0)

        # The fixture's system temp ancestor is world-writable; production still checks every parent.
        original = Path.lstat

        def metadata(path):
            value = original(path)
            if path in self.home.parents:
                return SimpleNamespace(st_mode=0o40755, st_uid=0)
            return value

        with (
            patch.dict(os.environ, {"AGENT_SEAT": seat} if seat else {}, clear=True),
            patch.object(Path, "home", return_value=self.home),
            patch.object(exporter.shutil, "which", return_value=str(self.wrapper)),
            patch.object(Path, "lstat", autospec=True, side_effect=metadata),
            patch.object(exporter, "run_child", side_effect=run) as process,
        ):
            exporter.verify_managed_route()
            self.assertTrue(process.call_args.kwargs["start_new_session"])
            self.assertEqual(process.call_args.args[0], [str(self.wrapper), "whoami", "--json"])

    def test_matching_assigned_actor_uses_no_credential_file_or_environment_copy(self):
        self.invoke()
        self.assertTrue(exporter._MANAGED_ROUTE_VERIFIED)

    def test_malformed_external_json_layers_are_refused_as_source_errors(self):
        for value in (None, [], "text", {"structured": None}, {"structured": []}, {}):
            with self.subTest(value=value), self.assertRaises(exporter.remote.SourceError):
                self.invoke(raw_json=json.dumps(value))

    def test_missing_or_mismatched_actor_is_refused(self):
        for actor, seat in (("CODEX", ""), ("OTHER", "CODEX")):
            with (
                self.subTest(actor=actor, seat=seat),
                self.assertRaises(exporter.remote.SourceError),
            ):
                self.invoke(actor, seat)

    def test_group_writable_or_unmanaged_entrypoint_is_refused(self):
        self.wrapper.chmod(0o720)
        with self.assertRaises(exporter.remote.SourceError):
            self.invoke()
        self.wrapper.chmod(0o700)
        self.wrapper.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaises(exporter.remote.SourceError):
            self.invoke()

    def test_both_authenticated_and_default_identity_are_required_and_match(self):
        complete = {
            "authenticated_seat": "CODEX",
            "default_identity": "CODEX",
            "identities": [{"identity": "CODEX"}],
        }
        for field in ("authenticated_seat", "default_identity"):
            for invalid in (None, "OTHER", 1):
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaises(exporter.remote.SourceError),
                ):
                    self.invoke(identity=dict(complete, **{field: invalid}))
            missing = dict(complete)
            missing.pop(field)
            with self.subTest(missing=field), self.assertRaises(exporter.remote.SourceError):
                self.invoke(identity=missing)

    def test_explicit_process_bearer_keeps_existing_transport_without_fallback(self):
        def run(argv, **kwargs):
            kwargs["stdout"].write(b'{"structured":{"expected":"fixture"}}')
            return SimpleNamespace(returncode=0)

        with (
            patch.dict(os.environ, {"SIMPLE_TOOLS_BEARER": "synthetic-fixture"}, clear=True),
            patch.object(exporter, "verify_managed_route") as fallback,
            patch.object(exporter, "run_child", side_effect=run),
        ):
            self.assertEqual(
                exporter.relay_read("gr_wf_repo_get", {"repo": "fixture/repo"}),
                {"expected": "fixture"},
            )
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
