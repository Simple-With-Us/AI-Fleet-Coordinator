"""The shared cycle retains Git authorization and keeps Docs failures independent."""

import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fleet_docs import pipeline, snapshot


class PipelineTests(unittest.TestCase):
    def test_configuration_failure_has_specific_receipt_without_failing_git(self):
        with tempfile.TemporaryDirectory() as tmp, patch("fleet_lanes.code_main_sync.run_sweep_command", return_value=0):
            def unavailable():
                raise ValueError("private-provider-sentinel")
            self.assertEqual(pipeline.run(["/fixture/bootstrap"], read=unavailable, state=Path(tmp)), 0)
            receipt = json.loads((Path(tmp) / "receipt.json").read_text())
            self.assertEqual(receipt["setting"], "DOCS_SYNC_CONFIGURATION")
            self.assertEqual(receipt["docs_exit"], 1)
            self.assertNotIn("private-provider-sentinel", json.dumps(receipt))

    def test_state_symlink_and_symlinked_parent_are_refused_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            (root / "link").symlink_to(target, target_is_directory=True)
            for state in (root / "link", root / "link/nested"):
                with self.subTest(state=state), self.assertRaises((ValueError, OSError)):
                    pipeline.record(state, {"state": "disabled"})
            self.assertEqual(list(target.iterdir()), [])

    def test_public_repositories_share_one_collection_batch(self):
        c = snapshot.collector()
        public = [c.RepoInfo(Path(name), "Simple-With-Us", name, "origin/main", "0" * 40)
                  for name in ("ExampleOne", "ExampleTwo")]
        private = c.RepoInfo(Path("Fleet-OPS"), "Simple-With-Us", "Fleet-OPS", "origin/main", "0" * 40)
        with patch.object(c, "discover_repositories", return_value=(public + [private], [])):
            batches = pipeline.collection_batches(Path("."), {"private_docs": ["Simple-With-Us/Fleet-OPS"]}, [])
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0][1][0], public)
        self.assertEqual(batches[1][0]["private_docs_repo"], "Simple-With-Us/Fleet-OPS")

    def test_private_stored_owner_alias_preserves_the_existing_opt_in(self):
        c = snapshot.collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Fleet-OPS/manifest.json"
            path.parent.mkdir()
            value = {"schema_version": 2, "generated_by": "collect-repo-documents.py", "repo": "Fleet-OPS",
                     "source_ref": "origin/main", "source_commit": "0" * 40,
                     "owner_repo": "jaywedgeworth22/Fleet-OPS", "private_docs_opt_in": "jaywedgeworth22/Fleet-OPS", "files": {}}
            path.write_text(json.dumps(value))
            repo = c.RepoInfo(Path("."), "Simple-With-Us", "Fleet-OPS", "origin/main", "0" * 40)
            pipeline.validate_existing_profile(root, {"private_docs": ["Simple-With-Us/Fleet-OPS"]}, [({}, ([repo], []))])
            with self.assertRaises(pipeline.ConfigurationRefused) as refused:
                pipeline.validate_existing_profile(root, {"private_docs": ["Simple-With-Us/Fleet-OPS"]}, [])
            self.assertEqual(refused.exception.source, "jaywedgeworth22/Fleet-OPS")
            self.assertTrue(path.exists())

    def test_offline_peer_retries_next_cycle_without_changing_git_result(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("fleet_lanes.code_main_sync.run_sweep_command", return_value=0) as git,
        ):
            calls = []

            def update(*args):
                calls.append(args)
                if len(calls) == 1:
                    raise snapshot.Refused("docs-peer-unavailable")
                return {"state": "synced", "sha256": "0" * 64}

            error = io.StringIO()
            with redirect_stderr(error):
                for _ in range(2):
                    self.assertEqual(
                        pipeline.run(
                            ["/authorized/bootstrap"],
                            read=lambda: {"peer": "mac"},
                            update=update,
                            state=Path(tmp),
                            code_root=Path(tmp),
                        ),
                        0,
                    )
            self.assertEqual(git.call_count, 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                json.loads((Path(tmp) / "receipt.json").read_text())["state"], "synced"
            )
            git.assert_called_with(["/authorized/bootstrap"])

    def test_git_failure_skips_docs_and_keeps_original_result(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("fleet_lanes.code_main_sync.run_sweep_command", return_value=7),
            patch.object(pipeline, "settings") as read,
            patch.object(pipeline, "refresh") as update,
        ):
            self.assertEqual(
                pipeline.run(["/authorized/bootstrap"], read=read, update=update, state=Path(tmp)),
                7,
            )
        read.assert_not_called()
        update.assert_not_called()

    def test_receipt_io_failure_does_not_turn_git_success_into_failure(self):
        with (
            patch("fleet_lanes.code_main_sync.run_sweep_command", return_value=0),
            patch.object(pipeline, "record", side_effect=PermissionError),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(pipeline.run(["/authorized/bootstrap"], read=lambda: None), 0)

    def test_configuration_rejects_peer_options_and_unapproved_private_scope(self):
        good = {
            "peer": "mac",
            "actor": "CODEX",
            "private_docs": ["Simple-With-Us/Fleet-OPS"],
            "remote_sources": [],
        }
        self.assertEqual(pipeline.parse_configuration(json.dumps(good)), good)
        for bad in (
            {**good, "peer": "-oProxyCommand=bad"},
            {**good, "peer": "mac;bad"},
            {**good, "private_docs": ["foreign/private"]},
        ):
            with self.assertRaises(snapshot.Refused):
                pipeline.parse_configuration(json.dumps(bad))

    def test_lifetime_pipeline_lock_excludes_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp, pipeline.lifetime_lock(Path(tmp)):
            with self.assertRaises(snapshot.Refused):
                with pipeline.lifetime_lock(Path(tmp)):
                    self.fail("duplicate acquired lock")

    def test_unchanged_hash_does_not_transfer_archive(self):
        desired = {"sha256": "0" * 64, "files": {"README.md": {}}, "bytes": 10}
        with patch.object(pipeline.subprocess, "Popen") as process:
            result = pipeline.replicate(
                None,
                desired,
                {"peer": "mac"},
                status=lambda _: {"state": "ready", "sha256": "0" * 64},
                runner=process,
            )
        self.assertEqual(result["state"], "current")
        process.assert_not_called()

    def test_existing_profile_covers_every_collection_and_cannot_add_private_opt_in(self):
        c = snapshot.collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Existing/manifest.json"
            path.parent.mkdir()
            manifest = {
                "schema_version": 2,
                "generated_by": "collect-repo-documents.py",
                "repo": "Existing",
                "source_ref": "origin/main",
                "source_commit": "0" * 40,
                "owner_repo": "jaywedgeworth22/Existing",
                "files": {},
            }
            path.write_text(json.dumps(manifest))
            configuration = {"private_docs": [], "remote_sources": []}
            repo = c.RepoInfo(Path("."), "Simple-With-Us", "Existing", "origin/main", "0" * 40)
            batches = [({}, ([repo], []))]
            # The existing owner-transfer alias is canonicalized without changing provenance.
            pipeline.validate_existing_profile(root, configuration, batches)
            with self.assertRaises(snapshot.Refused):
                pipeline.validate_existing_profile(root, configuration, [])
            with self.assertRaises(snapshot.Refused):
                pipeline.validate_existing_profile(
                    root, {"private_docs": ["Simple-With-Us/Fleet-OPS"]}, batches
                )

    def test_only_trusted_false_is_disabled(self):
        from fleet_knobs import knobs
        from fleet_lanes import code_mirror_credentials

        for source in ("infisical", "lkg", "default"):
            with (
                patch.object(
                    code_mirror_credentials, "settings_read_binding", return_value=nullcontext()
                ),
                patch.object(knobs, "refresh"),
                patch.object(
                    knobs, "resolve", return_value=SimpleNamespace(value=False, source=source)
                ),
            ):
                if source == "default":
                    with self.assertRaises(snapshot.Refused):
                        pipeline.settings()
                else:
                    self.assertIsNone(pipeline.settings())

    def test_actor_binding_is_explicit_child_only_and_refuses_conflicts(self):
        original = {"HOME": "/fixture/home"}
        child = pipeline.child_relay_environment({"actor": "CODEX"}, original)
        self.assertEqual(original, {"HOME": "/fixture/home"})
        self.assertEqual(child["AGENT_SEAT"], "CODEX")
        declared = {"AGENT_SEAT": "CODEX"}
        self.assertEqual(pipeline.child_relay_environment({"actor": "CODEX"}, declared), declared)
        for inherited in (
            {"AGENT_SEAT": "OTHER"},
            {"AGENT_TAG": "OTHER"},
            {"AGENT_LAUNCHER": "fixture"},
            {"AGENT_LAUNCH_SEAT": "CODEX"},
        ):
            with self.assertRaises(snapshot.Refused):
                pipeline.child_relay_environment({"actor": "CODEX"}, inherited)


if __name__ == "__main__":
    unittest.main()
