import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / "deploy" / "preservation-pairs-2426"
REPO = Path(__file__).resolve().parents[3]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("preservation_pairs_builder", "build.py")
proof = load("preservation_pairs_proof", "runtime_proof.py")

CONFIG = "c" * 64
POLICY = "b" * 64
CANDIDATE = b"fixture candidate module\n"
# A stand-in engine closure. The builder's own logic is under test, never a
# live engine module, so an engine change cannot make this test stale.
ENGINE = {
    "__init__.py": b"",
    "deploy_runner.py": b'UNIT = "git-relay-patch-1429.service"\n',
    "deploy_transaction.py": b"# fixture engine module\n",
}
UNIT_BOUND = [
    "manifest.json",
    "runner-package/git_relay/deploy_runner.py",
    "runner.py",
]


def fixture_repo(root):
    """A stand-in checkout: (root, installed image, output path, engine pins)."""
    package = root / "git-relay" / "git_relay"
    package.mkdir(parents=True)
    for name, raw in ENGINE.items():
        (package / name).write_bytes(raw)
    (package / "reviewed_preservation.py").write_bytes(CANDIDATE)
    before = root / "before"
    before.write_bytes(b"fixture installed image")
    pins = {name: hashlib.sha256(raw).hexdigest() for name, raw in ENGINE.items()}
    return root, before, root / "package", pins


def build_at(test, attempt):
    """Build one attempt package over the stand-in engine; ({path: bytes}, result)."""
    with tempfile.TemporaryDirectory() as tmp:
        root, before, output, pins = fixture_repo(Path(tmp))
        with (
            patch.object(builder, "MODULES", pins),
            patch.object(builder, "image", return_value=CANDIDATE),
        ):
            result = builder.build(root, before, output, CONFIG, POLICY, attempt)
        files = {
            path.relative_to(output).as_posix(): path.read_bytes()
            for path in sorted(output.rglob("*"))
            if path.is_file()
        }
        return files, result


class TransactionNameTests(unittest.TestCase):
    def test_original_root_and_numbered_attempt_roots(self):
        for value, name in (
            (None, "preservation-pairs-2426"),
            ("preservation-pairs-2426", "preservation-pairs-2426"),
            ("preservation-pairs-2426-a01", "preservation-pairs-2426-a01"),
            ("preservation-pairs-2426-a123", "preservation-pairs-2426-a123"),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    builder.transaction_name(value),
                    (
                        name,
                        "/opt/git-relay/transactions/" + name,
                        "git-relay-" + name + ".service",
                    ),
                )

    def test_anything_else_refuses_before_a_path_or_unit_is_built(self):
        for value in (
            "",
            "preservation-pairs-2426-",
            "preservation-pairs-2426-a",
            "preservation-pairs-2426-a1",
            "preservation-pairs-2426-a1x",
            "preservation-pairs-2426-a01-a02",
            "preservation-pairs-2426-A01",
            "preservation-pairs-2426-a01.service",
            "preservation-pairs-2426-a01/..",
            "../preservation-pairs-2426-a01",
            "preservation-pairs-9999",
            "workflow-1614",
            2426,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                builder.transaction_name(value)


class BuildTests(unittest.TestCase):
    def test_each_attempt_binds_its_own_root_and_unit(self):
        for attempt, name in (
            (None, "preservation-pairs-2426"),
            ("preservation-pairs-2426-a01", "preservation-pairs-2426-a01"),
            ("preservation-pairs-2426-a99", "preservation-pairs-2426-a99"),
        ):
            with self.subTest(attempt=attempt):
                root = "/opt/git-relay/transactions/" + name
                unit = "git-relay-" + name + ".service"
                files, result = build_at(self, attempt)
                self.assertEqual((result["root"], result["unit"]), (root, unit))
                self.assertEqual(
                    json.loads(files["manifest.json"]),
                    {
                        "config": CONFIG,
                        "files": [
                            {
                                "root": "code",
                                "name": "reviewed_preservation.py",
                                "before": builder.BEFORE,
                                "after": builder.AFTER,
                                "meta": {"uid": 0, "gid": 0, "mode": 420},
                            }
                        ],
                        "runner": root + "/runner.py",
                        "unit": unit,
                    },
                )
                self.assertEqual(result["manifest"], builder.digest(files["manifest.json"]))
                self.assertEqual(result["runner"], builder.digest(files["runner.py"]))
                runner = files["runner.py"]
                self.assertIn((f'DIRECTORY = Path("{root}")').encode(), runner)
                self.assertIn(b'MANIFEST_DIGEST = "%s"' % result["manifest"].encode(), runner)
                self.assertNotIn(b"PLACEHOLDER", runner)
                engine_runner = files["runner-package/git_relay/deploy_runner.py"]
                self.assertEqual(engine_runner.count(unit.encode()), 1)
                self.assertNotIn(b"git-relay-patch-1429.service", engine_runner)
                self.assertEqual(files["0.candidate"], CANDIDATE)
                self.assertEqual(
                    sorted(files),
                    sorted(
                        ["0.candidate", "manifest.json", "runner.py"]
                        + ["runner-package/git_relay/" + name for name in ENGINE]
                        + ["runner-package/git_relay/deploy_proof.py"]
                    ),
                )

    def test_attempt_roots_differ_only_in_the_unit_bound_files(self):
        first, _ = build_at(self, "preservation-pairs-2426-a01")
        second, _ = build_at(self, "preservation-pairs-2426-a02")
        self.assertEqual(sorted(first), sorted(second))
        self.assertEqual(sorted(name for name in first if first[name] != second[name]), UNIT_BOUND)
        # The image, the file row and the engine closure are the same bytes.
        for name in sorted(set(first) - set(UNIT_BOUND)):
            self.assertEqual(first[name], second[name], name)
        first_manifest, second_manifest = (
            json.loads(first["manifest.json"]),
            json.loads(second["manifest.json"]),
        )
        self.assertEqual(first_manifest["files"], second_manifest["files"])
        self.assertEqual(first_manifest["config"], second_manifest["config"])

    def test_engine_drift_refuses_before_output_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, before, output, pins = fixture_repo(Path(tmp))
            (root / "git-relay" / "git_relay" / "deploy_transaction.py").write_bytes(b"drifted\n")
            with (
                patch.object(builder, "MODULES", pins),
                patch.object(builder, "image", return_value=CANDIDATE),
                self.assertRaisesRegex(
                    ValueError, "finite recovery module drift: deploy_transaction.py"
                ),
            ):
                builder.build(root, before, output, CONFIG, POLICY, "preservation-pairs-2426-a01")
            self.assertFalse(output.exists())

    def test_output_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, before, output, pins = fixture_repo(Path(tmp))
            output.mkdir()
            with (
                patch.object(builder, "MODULES", pins),
                patch.object(builder, "image", return_value=CANDIDATE),
                self.assertRaises(FileExistsError),
            ):
                builder.build(root, before, output, CONFIG, POLICY)
            self.assertEqual(list(output.iterdir()), [])


class PinTests(unittest.TestCase):
    def test_proof_template_and_image_pins(self):
        template = (HERE / "runtime_proof.py").read_bytes()
        self.assertEqual(builder.digest(template), builder.PROOF_TEMPLATE)
        self.assertEqual(proof.RuntimeProof.BASELINE, builder.BEFORE)
        self.assertEqual(proof.RuntimeProof.CANDIDATE, builder.AFTER)

    def test_runner_template_carries_each_placeholder_once(self):
        template = (HERE / "runner.template.py").read_text()
        for marker in (
            "MANIFEST_DIGEST_PLACEHOLDER",
            "MODULE_HASHES_PLACEHOLDER",
            "ROOT_PATH_PLACEHOLDER",
        ):
            self.assertEqual(template.count(marker), 1, marker)

    def test_preparation_tracks_the_module_on_main(self):
        """The preparation installs main's module over the installed image. A record
        added, renewed or retired on main without refreshing AFTER leaves this package
        unable to build (it happened with #2513)."""
        module = REPO / "git-relay" / "git_relay" / "reviewed_preservation.py"
        self.assertEqual(
            builder.digest(module.read_bytes()),
            builder.AFTER,
            "reviewed_preservation.py changed on main: refresh AFTER, the proof's "
            "CANDIDATE and PROOF_TEMPLATE in this preparation (see its README)",
        )

    def test_directory_binding_drift_refuses(self):
        template = (HERE / "runner.template.py").read_text()
        with self.assertRaises(ValueError):
            builder.render_entry(
                template.replace("ROOT_PATH_PLACEHOLDER", "/wrong"),
                b"manifest",
                {"x": b"x"},
                "/opt/git-relay/transactions/preservation-pairs-2426",
            )


if __name__ == "__main__":
    unittest.main()
