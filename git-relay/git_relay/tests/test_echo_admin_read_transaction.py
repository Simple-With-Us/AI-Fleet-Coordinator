import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / "deploy" / "echo-admin-read"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("echo_admin_builder", "build.py")
proof = load("echo_admin_proof", "runtime_proof.py")


class Tests(unittest.TestCase):
    def test_live_byte_drift_refuses(self):
        with self.assertRaisesRegex(ValueError, "baseline drift"):
            builder.image(b"changed")

    def test_only_one_exact_state_file_and_private_candidate(self):
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / "before", Path(tmp) / "package"
            source.write_bytes(b"fixture")
            with patch.object(builder, "image", return_value=b"fixture candidate"):
                builder.build(root, source, out)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(
                manifest["files"],
                [
                    {
                        "root": "state",
                        "name": "seats.json",
                        "before": builder.BEFORE,
                        "after": builder.AFTER,
                        "meta": {"uid": 65534, "gid": 65534, "mode": 384},
                    }
                ],
            )
            self.assertEqual(manifest["runner"], builder.ROOT + "/runner.py")
            self.assertEqual(manifest["unit"], builder.UNIT)
            self.assertEqual(stat.S_IMODE((out / "0.candidate").stat().st_mode), 0o600)
            self.assertFalse((out / "1.candidate").exists())
            self.assertNotIn(b"PLACEHOLDER", (out / "runner.py").read_bytes())
            with (
                self.assertRaises(FileExistsError),
                patch.object(builder, "image", return_value=b"fixture candidate"),
            ):
                builder.build(root, source, out)

    def test_runtime_proof_pin_and_tamper_refusal(self):
        self.assertEqual(builder.digest((HERE / "runtime_proof.py").read_bytes()), builder.PROOF)
        root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            before = Path(tmp) / "before"
            before.write_bytes(b"fixture")
            with (
                patch.object(builder, "image", return_value=b"candidate"),
                patch.object(builder, "PROOF", "wrong"),
                self.assertRaisesRegex(ValueError, "runtime proof module drift"),
            ):
                builder.build(root, before, Path(tmp) / "out")

    def test_directory_binding_drift_refuses(self):
        template = (HERE / "runner.template.py").read_text()
        with self.assertRaises(ValueError):
            builder.render_entry(template.replace(builder.ROOT, "/wrong"), b"manifest", {"x": b"x"})

    def test_candidate_mode_independent_of_umask(self):
        for mask in (0, 0o777):
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "candidate"
                old = os.umask(mask)
                try:
                    builder.write_file(p, b"private fixture", private=True)
                finally:
                    os.umask(old)
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_proof_inventory_selector_and_ownseat_read(self):
        p = proof.RuntimeProof("test-only")
        calls = []
        p.selector = lambda identity: True
        p.call = lambda action, args, calls=calls, p=p: (
            calls.append((action, args))
            or (
                200,
                {"repository": {"full_name": p.REPO, "default_branch": "main"}},
            )
        )
        self.assertTrue(p({"pid": 1}, {"state/seats.json": builder.BEFORE}))
        self.assertEqual(calls, [("wf_repo_get", {"repo": p.REPO})])
        p.selector = lambda identity: False
        self.assertFalse(p({"pid": 1}, {"state/seats.json": builder.BEFORE}))
        self.assertEqual(len(calls), 1)
        self.assertFalse(p({"pid": 1}, {"state/seats.json": "wrong"}))
        self.assertEqual(len(calls), 1)

    def test_unpinned_candidate_placeholder_refused(self):
        p = proof.RuntimeProof("test-only")
        p.selector = lambda identity: True
        p.call = lambda action, args: (
            200,
            {"repository": {"full_name": p.REPO, "default_branch": "main"}},
        )
        self.assertFalse(p({"pid": 1}, {"state/seats.json": builder.AFTER}))
        pinned = "a" * 64
        with patch.object(proof.RuntimeProof, "CANDIDATE", pinned):
            self.assertTrue(p({"pid": 1}, {"state/seats.json": pinned}))

    def test_selector_pid_shape_validated(self):
        p = proof.RuntimeProof("test-only")
        for bad in (-1, 0, True, "1/2", "../1", "abc", "", None, 1.5):
            self.assertFalse(p.selector({"pid": bad}))

    def test_selector_exact_startup_file_and_same_physical_file(self):
        p = proof.RuntimeProof("test-only")
        for env, allowed in [
            (b"GIT_RELAY_SEATS_FILE=/var/lib/git-relay/seats.json\0OTHER=hidden", True),
            (b"GIT_RELAY_SEATS_JSON={}\0", False),
            (b"GIT_RELAY_SEATS_FILE=/wrong\0", False),
            (
                b"GIT_RELAY_SEATS_FILE=/var/lib/git-relay/seats.json\0GIT_RELAY_SEATS_JSON={}\0",
                False,
            ),
        ]:
            with (
                patch.object(proof.Path, "read_bytes", return_value=env),
                patch.object(proof.os.path, "samefile", return_value=True),
            ):
                self.assertEqual(p.selector({"pid": 1}), allowed)
        with (
            patch.object(
                proof.Path,
                "read_bytes",
                return_value=b"GIT_RELAY_SEATS_FILE=/var/lib/git-relay/seats.json",
            ),
            patch.object(proof.os.path, "samefile", return_value=False),
        ):
            self.assertFalse(p.selector({"pid": 1}))

    def test_proof_bad_repo_and_provider_failure_refuse(self):
        for reply in [
            (401, {}),
            (200, {"repository": {"full_name": "wrong", "default_branch": "main"}}),
            (200, {}),
        ]:
            p = proof.RuntimeProof("test-only")
            p.selector = lambda identity: True
            p.call = lambda *args, reply=reply: reply
            self.assertFalse(p({"pid": 1}, {"state/seats.json": builder.AFTER}))


if __name__ == "__main__":
    unittest.main()
