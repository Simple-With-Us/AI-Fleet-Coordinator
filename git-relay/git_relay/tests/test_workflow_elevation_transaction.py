import ast
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / "deploy" / "workflow-elevation-1549"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("workflow1549_builder", "build.py")
proof = load("workflow1549_proof", "runtime_proof.py")


class BuilderTests(unittest.TestCase):
    def test_baseline_drift_refuses(self):
        with self.assertRaisesRegex(ValueError, "baseline drift"):
            builder.image(b"changed", b"candidate")

    def test_candidate_drift_refuses(self):
        with patch.object(builder, "digest", side_effect=[builder.BEFORE, "wrong"]):
            with self.assertRaisesRegex(ValueError, "candidate drift"):
                builder.image(b"before", b"candidate")

    def test_image_parses_syntax(self):
        with patch.object(builder, "digest", side_effect=[builder.BEFORE, builder.AFTER]):
            with self.assertRaises(SyntaxError):
                builder.image(b"before", b"def broken(")

    def test_render_binds_exact_manifest_and_modules(self):
        raw = b"manifest"
        sources = {"git_relay/a.py": b"content"}
        entry = builder.render_entry((HERE / "runner.template.py").read_text(), raw, sources)
        self.assertNotIn("PLACEHOLDER", entry)
        self.assertIn(builder.digest(raw), entry)
        self.assertIn(builder.digest(b"content"), entry)
        self.assertIn(builder.ROOT, entry)

    def test_render_refuses_missing_or_repeated_placeholders(self):
        template = (HERE / "runner.template.py").read_text()
        for changed in [
            template.replace("MANIFEST_DIGEST_PLACEHOLDER", "missing"),
            template + "\n# MANIFEST_DIGEST_PLACEHOLDER",
        ]:
            with self.assertRaises(ValueError):
                builder.render_entry(changed, b"manifest", {"a": b"content"})

    def test_build_has_only_one_exact_file_and_fixed_bindings(self):
        root = Path(__file__).resolve().parents[3]
        self.assertEqual(
            builder.digest((root / "git-relay/git_relay/workflow_verbs.py").read_bytes()),
            builder.AFTER,
        )
        with tempfile.TemporaryDirectory() as directory:
            before = Path(directory) / "before"
            before.write_bytes(b"before")
            output = Path(directory) / "new"
            with patch.object(builder, "image", return_value=b"candidate"):
                receipt = builder.build(root, before, output)
            manifest = json.loads((output / "manifest.json").read_bytes())
            self.assertEqual(
                manifest["files"],
                [
                    {
                        "root": "code",
                        "name": "workflow_verbs.py",
                        "before": builder.BEFORE,
                        "after": builder.AFTER,
                        "meta": {"uid": 0, "gid": 0, "mode": 420},
                    }
                ],
            )
            self.assertEqual(manifest["runner"], builder.ROOT + "/runner.py")
            self.assertEqual(manifest["unit"], builder.UNIT)
            self.assertEqual(manifest["config"], builder.CONFIG)
            self.assertEqual(
                receipt["manifest"], builder.digest((output / "manifest.json").read_bytes())
            )
            self.assertFalse((output / "1.candidate").exists())
            with self.assertRaises(FileExistsError):
                with patch.object(builder, "image", return_value=b"candidate"):
                    builder.build(root, before, output)

    def test_wrong_directory_binding_refuses(self):
        template = (HERE / "runner.template.py").read_text()
        for changed in [
            template.replace(builder.ROOT, "/wrong"),
            template.replace("DIRECTORY = Path(", "DIRECTORY = str("),
        ]:
            with self.assertRaises(ValueError):
                builder.render_entry(changed, b"manifest", {"a": b"content"})

    def test_files_explicit_mode_under_permissive_or_strict_umask(self):
        for mask in (0, 0o777):
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "artifact"
                old = os.umask(mask)
                try:
                    builder.write_file(target, b"source")
                finally:
                    os.umask(old)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
                self.assertEqual(target.read_bytes(), b"source")
                with self.assertRaises(FileExistsError):
                    builder.write_file(target, b"overwrite")

    def test_module_open_is_nonblocking_and_no_follow(self):
        tree = ast.parse((HERE / "runner.template.py").read_text())
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "open"
        ]
        self.assertEqual(len(calls), 1)
        flags = ast.unparse(calls[0].args[1])
        self.assertIn("O_NONBLOCK", flags)
        self.assertIn("O_NOFOLLOW", flags)


class ProofTests(unittest.TestCase):
    def run_proof(self, replies):
        instance = proof.RuntimeProof("test-only")
        calls = []

        def call(action, args):
            calls.append((action, args))
            return replies[len(calls) - 1]

        instance.call = call
        result = instance({}, {"code/workflow_verbs.py": builder.AFTER})
        return result, calls

    def repository(self, default="main"):
        return (
            200,
            {"repository": {"full_name": proof.RuntimeProof.REPO, "default_branch": default}},
        )

    def test_unknown_hash_fails_before_network(self):
        instance = proof.RuntimeProof("test-only")
        instance.call = lambda *args: self.fail("unqualified image reached network")
        for hashes in (
            {},
            {"code/workflow_verbs.py": "wrong"},
            {"code/workflow_verbs.py": builder.AFTER, "extra": "wrong"},
        ):
            self.assertFalse(instance({}, hashes))

    def test_both_pinned_images_use_same_dispatch_component(self):
        for digest in (proof.RuntimeProof.BASELINE, proof.RuntimeProof.CANDIDATE):
            instance = proof.RuntimeProof("test-only")
            replies = iter([self.repository(), (400, {"error": "default branch is not writable"})])
            instance.call = lambda *args: next(replies)
            self.assertTrue(instance({}, {"code/workflow_verbs.py": digest}))

    def test_exact_no_write_probes(self):
        result, calls = self.run_proof(
            [self.repository(), (400, {"error": "default branch is not writable"})]
        )
        self.assertTrue(result)
        self.assertEqual([x[0] for x in calls], ["wf_repo_get", "wf_file_patch"])
        args = calls[1][1]
        self.assertEqual(args["branch"], "main")
        self.assertEqual(args["expected_head_sha"], "0" * 40)
        self.assertEqual(args["base_blob_sha"], "0" * 40)
        self.assertEqual(args["edits"], [{"start": 0, "end": 0, "text": ""}])

    def test_wrong_repository_or_default_stops_before_patch(self):
        for reply in [
            (401, {}),
            self.repository("other"),
            (200, {"repository": {"full_name": "wrong", "default_branch": "main"}}),
        ]:
            result, calls = self.run_proof([reply])
            self.assertFalse(result)
            self.assertEqual(len(calls), 1)

    def test_unexpected_patch_response_fails_without_retry(self):
        for response in [(200, {}), (400, {"error": "unknown action"}), (500, None)]:
            result, calls = self.run_proof([self.repository(), response])
            self.assertFalse(result)
            self.assertEqual(len(calls), 2)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(
            proof.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://other")
        )


if __name__ == "__main__":
    unittest.main()
