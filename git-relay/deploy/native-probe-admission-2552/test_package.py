"""Isolated builder and runtime-proof regressions. No network, store or service access."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load("scanner2552_build", HERE / "build.py")
P = load("scanner2552_proof", HERE / "runtime_proof.py")


class PackageTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(B.transaction_name(None)[0], "native-probe-admission-2552")
        self.assertEqual(
            B.transaction_name("native-probe-admission-2552-a01")[0],
            "native-probe-admission-2552-a01",
        )
        for name in ["../escape", "preservation-pairs-2426", "native-probe-admission-2552-a1"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                B.transaction_name(name)

    def test_digest_refusal(self):
        for raw in ["", "A" * 64, "g" * 64]:
            with self.assertRaises(ValueError):
                B.observed_digest(raw, "input")

    def test_image_drift(self):
        with self.assertRaises(ValueError):
            B.image(b"wrong baseline", b"wrong candidate")

    def test_render_proof_exact(self):
        raw = (HERE / "runtime_proof.py").read_bytes()
        rendered = B.render_proof(raw, "a" * 64)
        self.assertNotIn(b"PLACEHOLDER", rendered)
        self.assertIn(B.BEFORE.encode(), rendered)
        self.assertIn(B.AFTER.encode(), rendered)
        with self.assertRaises(ValueError):
            B.render_proof(raw + b"\n", "a" * 64)

    def test_private_symlink_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "link"
            p.symlink_to(d)
            with self.assertRaises(ValueError):
                B.private_dir(p)

    def test_calls_only_read_verbs(self):
        for image in [P.RuntimeProof.BASELINE, P.RuntimeProof.CANDIDATE]:
            proof = P.RuntimeProof("synthetic-test-input")
            calls = []

            def call(action, args):
                calls.append((action, args))
                if action == "wf_repo_get":
                    return 200, {"repository": {"full_name": proof.REPO, "default_branch": "main"}}
                return 200, {"issues": [], "complete": False}

            with (
                mock.patch.object(proof, "selector", return_value=True),
                mock.patch.object(proof, "call", side_effect=call),
            ):
                self.assertTrue(proof({}, {"code/patch_secretscan.py": image}))
            self.assertEqual([x[0] for x in calls], ["wf_repo_get", "wf_issue_list"])
            self.assertNotIn("labels", calls[1][1])

    def test_wrong_image_or_selector_refused(self):
        proof = P.RuntimeProof("synthetic-test-input")
        with mock.patch.object(proof, "call") as call:
            self.assertFalse(proof({}, {"code/patch_secretscan.py": "0" * 64}))
            with mock.patch.object(proof, "selector", return_value=False):
                self.assertFalse(proof({}, {"code/patch_secretscan.py": proof.CANDIDATE}))
            call.assert_not_called()

    def test_wrong_repository_or_unavailable_refused(self):
        proof = P.RuntimeProof("synthetic-test-input")
        for result in [
            (None, None),
            (200, {"repository": {"full_name": "wrong", "default_branch": "main"}}),
        ]:
            with (
                mock.patch.object(proof, "selector", return_value=True),
                mock.patch.object(proof, "call", return_value=result),
            ):
                self.assertFalse(proof({}, {"code/patch_secretscan.py": proof.CANDIDATE}))


if __name__ == "__main__":
    unittest.main()
