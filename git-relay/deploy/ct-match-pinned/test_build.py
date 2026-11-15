import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ct_builder", HERE / "build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuilderTests(unittest.TestCase):
    def prepare(self, root):
        repo = root / "repo"
        package = repo / "git-relay/git_relay"
        package.mkdir(parents=True)
        sources = {
            "__init__.py": b"",
            "deploy_runner.py": b'UNIT = "git-relay-patch-1429.service"\n',
        }
        for name, raw in sources.items():
            (package / name).write_bytes(raw)
        before = root / "before"
        before.write_bytes(b"baseline")
        candidate = b"candidate"
        (package / "patch_secretscan.py").write_bytes(candidate)
        mirror = repo / "scripts/simple_tools/secretscan.py"
        mirror.parent.mkdir(parents=True)
        mirror.write_bytes(candidate)
        return repo, before, sources

    def test_pinned_one_file_and_exact_unit_transform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, before, sources = self.prepare(root)
            pins = {n: builder.digest(raw) for n, raw in sources.items()}
            with (
                patch.object(builder, "BEFORE", builder.digest(b"baseline")),
                patch.object(builder, "AFTER", builder.digest(b"candidate")),
                patch.object(builder, "MODULES", pins),
            ):
                output = root / "out"
                result = builder.build(repo, before, output)
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(len(manifest["files"]), 1)
                self.assertEqual(manifest["files"][0]["name"], "patch_secretscan.py")
                self.assertEqual(manifest["unit"], builder.UNIT)
                self.assertEqual(manifest["runner"], builder.ROOT + "/runner.py")
                self.assertEqual(
                    result["manifest"], builder.digest((output / "manifest.json").read_bytes())
                )
                runner = (output / "runner.py").read_text()
                self.assertNotIn("PLACEHOLDER", runner)
                self.assertNotIn("PROOF_BEARER", runner)
                self.assertIn("wise_runtime_proof", runner)
                transformed = (output / "runner-package/git_relay/deploy_runner.py").read_bytes()
                self.assertEqual(
                    transformed,
                    sources["deploy_runner.py"].replace(
                        b"git-relay-patch-1429.service", builder.UNIT.encode()
                    ),
                )
                self.assertEqual(before.read_bytes(), b"baseline")
                with self.assertRaises(FileExistsError):
                    builder.build(repo, before, output)

    def test_stale_inputs_and_mirror_refuse_before_output(self):
        for drift in ("before", "candidate", "mirror", "module"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                repo, before, sources = self.prepare(root)
                pins = {n: builder.digest(raw) for n, raw in sources.items()}
                targets = {
                    "before": before,
                    "candidate": repo / "git-relay/git_relay/patch_secretscan.py",
                    "mirror": repo / "scripts/simple_tools/secretscan.py",
                    "module": repo / "git-relay/git_relay/__init__.py",
                }
                targets[drift].write_bytes(b"drift")
                with (
                    patch.object(builder, "BEFORE", builder.digest(b"baseline")),
                    patch.object(builder, "AFTER", builder.digest(b"candidate")),
                    patch.object(builder, "MODULES", pins),
                ):
                    output = root / "out"
                    with self.assertRaises(ValueError):
                        builder.build(repo, before, output)
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
