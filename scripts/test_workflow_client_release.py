import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "builder", Path(__file__).with_name("build-workflow-client-release.py")
)
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


class Builder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "scripts/git-tools/git"
        self.file.parent.mkdir(parents=True)
        self.fixture = B.BEFORE + "    batches = _publish_batches(files)\n"
        self.file.write_text(self.fixture)
        self.destination = self.root / "out"
        self.stack = mock.patch.dict(
            B.FILES, {"scripts/git-tools/git": B.digest(self.fixture.encode())}, clear=True
        )
        self.stack.start()
        self.addCleanup(self.stack.stop)
        atomic = '    batches = _publish_batches(files)\n    # A workflow permission refusal must reject the whole operation, not arrive\n    # after earlier ordinary-file batches committed. Keep workflow publishes atomic.\n    if len(batches) > 1 and any(path.startswith(".github/workflows/") for path, _ in files):\n        die("workflow publish must fit one atomic batch; no write was sent", EXIT_USAGE)\n'
        self.expected = B.AFTER + atomic
        self.result = mock.patch.object(B, "RESULT_SHA", B.digest(self.expected.encode()))
        self.result.start()
        self.addCleanup(self.result.stop)

    def test_build_is_finite_no_install_and_input_unchanged(self):
        result = B.build(self.source, self.destination)
        self.assertFalse(result["activation_performed"])
        self.assertEqual(self.file.read_text(), self.fixture)
        self.assertEqual((self.destination / "scripts/git-tools/git").read_text(), self.expected)
        self.assertEqual(set(result["files"]), {"scripts/git-tools/git"})

    def test_bad_baseline_never_creates_output(self):
        self.file.write_text(B.BEFORE + "changed")
        with self.assertRaises(ValueError):
            B.build(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_source_symlink_never_creates_output(self):
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            B.build(alias, self.destination)
        self.assertFalse(self.destination.exists())

    def test_member_symlink_never_creates_output(self):
        external = self.root / "external"
        external.write_text(B.BEFORE)
        self.file.unlink()
        self.file.symlink_to(external)
        with self.assertRaises(ValueError):
            B.build(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_existing_output_not_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / "retained"
        marker.write_text("safe")
        with self.assertRaises(ValueError):
            B.build(self.source, self.destination)
        self.assertEqual(marker.read_text(), "safe")

    def test_wrong_result_pin_never_creates_output(self):
        with mock.patch.object(B, "RESULT_SHA", "0" * 64), self.assertRaises(ValueError):
            B.build(self.source, self.destination)
        self.assertFalse(self.destination.exists())
