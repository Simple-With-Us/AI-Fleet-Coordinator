"""Exercise the actual loader against real source replacement and stale timestamp bytecode."""

import importlib.util
import os
import py_compile
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from fleet_knobs import source

SCRIPTS = Path(__file__).resolve().parents[2]


class HelperLoadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        package = self.root / "fleet_knobs"
        package.mkdir()
        target = package / "source.py"
        shutil.copyfile(SCRIPTS / "fleet_knobs/source.py", target)
        self.client = self.root / "fleet_infisical.py"
        self.bytes = (SCRIPTS / "fleet_infisical.py").read_bytes() + b"\n_SOURCE_TEST_MARKER = 'one'\n"
        self.client.write_bytes(self.bytes)
        name = "fixture_knob_source_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(name, target)
        self.loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.loader)
        original = sys.modules.get(source._HELPER_NAME)
        self.addCleanup(self.restore_private_module, original)

    def restore_private_module(self, original):
        if original is None:
            sys.modules.pop(source._HELPER_NAME, None)
        else:
            sys.modules[source._HELPER_NAME] = original

    def test_twenty_real_canonical_loads_reuse_one_private_module(self):
        with mock.patch.object(source, "_HELPER_CACHE", None):
            modules = [source._load_helper() for _ in range(20)]
        self.assertIsNotNone(modules[0])
        self.assertTrue(all(module is modules[0] for module in modules))
        self.assertEqual(Path(modules[0].__file__).resolve(), SCRIPTS / "fleet_infisical.py")

    def test_private_cache_ignores_poisoned_public_and_private_module_entries(self):
        first = self.loader._load_helper()
        poison = types.ModuleType("fleet_infisical")
        poison.fetch_project = lambda *args, **kwargs: self.fail("foreign helper executed")
        with mock.patch.dict(
            sys.modules,
            {"fleet_infisical": poison, source._HELPER_NAME: poison},
        ):
            self.assertIs(self.loader._load_helper(), first)
            self.assertIs(sys.modules[source._HELPER_NAME], first)

    def test_same_size_same_second_source_change_ignores_timestamp_valid_stale_pyc(self):
        first = self.loader._load_helper()
        py_compile.compile(str(self.client), doraise=True)
        original = self.client.stat()
        changed = self.bytes.replace(b"_SOURCE_TEST_MARKER = 'one'", b"_SOURCE_TEST_MARKER = 'two'")
        self.assertEqual(len(changed), len(self.bytes))
        self.client.write_bytes(changed)
        os.utime(self.client, ns=(original.st_atime_ns, original.st_mtime_ns))
        # The usual timestamp loader really does accept the stale on-disk bytecode.
        spec = importlib.util.spec_from_file_location("stale_control", self.client)
        stale = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stale)
        self.assertEqual(stale._SOURCE_TEST_MARKER, "one")
        current = self.loader._load_helper()
        self.assertIsNot(current, first)
        self.assertEqual(current._SOURCE_TEST_MARKER, "two")
        self.assertIs(self.loader._load_helper(), current)

    def test_identical_content_file_replacement_invalidates_identity(self):
        first = self.loader._load_helper()
        replacement = self.client.with_suffix(".new")
        replacement.write_bytes(self.bytes)
        replacement.replace(self.client)
        current = self.loader._load_helper()
        self.assertIsNot(current, first)
        self.assertEqual(current._SOURCE_TEST_MARKER, "one")

    def test_changed_broken_or_missing_source_never_returns_stale_code(self):
        first = self.loader._load_helper()
        self.client.write_text("invalid = (\n")
        self.assertIsNone(self.loader._load_helper())
        self.assertIsNone(self.loader._HELPER_CACHE)
        self.assertIsNot(sys.modules.get(source._HELPER_NAME), first)
        self.client.unlink()
        self.assertIsNone(self.loader._load_helper())
        self.client.write_bytes(self.bytes)
        self.assertIsNot(self.loader._load_helper(), first)

    def test_concurrent_calls_compile_one_coherent_module(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            modules = list(pool.map(lambda _: self.loader._load_helper(), range(24)))
        self.assertIsNotNone(modules[0])
        self.assertTrue(all(module is modules[0] for module in modules))
        self.assertEqual(modules[0]._SOURCE_TEST_MARKER, "one")


if __name__ == "__main__":
    unittest.main()
