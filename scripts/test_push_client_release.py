import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("push_builder", pathlib.Path(__file__).with_name("build-push-client-release.py"))
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)


class Safety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name).resolve()
        self.base = self.root / "base"
        self.inputs = self.root / "inputs"
        self.base.mkdir()
        self.inputs.mkdir()
        self.dest = self.root / "output"

    def test_invalid_baseline_refuses_before_output(self):
        with self.assertRaises(FileNotFoundError):
            B.build(self.base, self.inputs, self.dest)
        self.assertFalse(self.dest.exists())

    def test_wrong_digest_refuses_before_output(self):
        p = self.base / "scripts/simple-git"
        p.parent.mkdir()
        p.write_text("drift")
        with self.assertRaisesRegex(ValueError, "digest"):
            B.build(self.base, self.inputs, self.dest)
        self.assertFalse(self.dest.exists())

    def test_symlink_root_refuses(self):
        link = self.root / "linked"
        link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            B.checked(link, "x", "unused")

    def test_symlink_file_refuses(self):
        f = self.inputs / "file"
        f.write_text("data")
        (self.base / "link").symlink_to(f)
        with self.assertRaisesRegex(ValueError, "symlink"):
            B.checked(self.base, "link", B.digest(b"data"))

    def test_source_anchor_requires_exact_one(self):
        for value in ("", "twice twice"):
            with self.assertRaisesRegex(ValueError, "anchor"):
                B.once(value, "twice", "new")

    def test_checked_reads_exact_bytes(self):
        (self.base / "x").write_bytes(b"data\n")
        self.assertEqual(B.checked(self.base, "x", B.digest(b"data\n")), b"data\n")

    def test_late_result_mismatch_writes_nothing(self):
        with mock.patch.object(B, "checked", return_value=b"{}"), mock.patch.object(B, "transform", return_value="pass\n"):
            with self.assertRaises((ValueError, KeyError)):
                B.build(self.base, self.inputs, self.dest)
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
