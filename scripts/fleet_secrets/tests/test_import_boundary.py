"""The compatibility import must resolve its packaged sibling without reading any credentials."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]


class AliasImportTests(unittest.TestCase):
    def run_probe(self, code, *, scripts=SCRIPTS):
        with tempfile.TemporaryDirectory() as temporary:
            hostile = Path(temporary)
            (hostile / "fleet_infisical.py").write_text(
                "raise AssertionError('foreign path executed')\n"
            )
            boot = (
                "import sys\nfrom pathlib import Path\n"
                "sys.path.insert(0,sys.argv[1])\nsys.path.insert(0,sys.argv[2])\n" + code
            )
            return subprocess.run(
                [sys.executable, "-I", "-B", "-c", boot, str(scripts), str(hostile)],
                cwd=hostile,
                env={"HOME": temporary, "PATH": os.defpath, "PYTHONPATH": str(hostile)},
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_absent_client_loads_only_pinned_sibling_despite_hostile_paths(self):
        result = self.run_probe("""
from fleet_secrets import core
import fleet_infisical
assert core is fleet_infisical
assert Path(core.__file__).resolve() == Path(sys.argv[1]) / 'fleet_infisical.py'
print('pinned-client')
""")
        self.assertEqual(
            (result.returncode, result.stdout.strip()), (0, "pinned-client"), result.stderr
        )

    def test_legitimate_preload_preserves_identity_and_intentional_monkeypatch(self):
        result = self.run_probe("""
import importlib
from unittest.mock import Mock
sys.path.remove(sys.argv[2])
import fleet_infisical
sys.path.insert(0,sys.argv[2])
marker = Mock()
fleet_infisical._api_json = marker
from fleet_secrets import core
assert core is fleet_infisical and core._api_json is marker
del sys.modules['fleet_secrets.core']
assert importlib.import_module('fleet_secrets.core') is fleet_infisical
assert fleet_infisical._api_json is marker
print('identity-preserved')
""")
        self.assertEqual(
            (result.returncode, result.stdout.strip()), (0, "identity-preserved"), result.stderr
        )

    def test_preloaded_foreign_missing_or_forged_file_only_origin_fails_fixed(self):
        result = self.run_probe("""
import importlib, importlib.util, types
for kind in ('foreign','missing','file-only','foreign-spec','lazy-marker','blocked'):
    fake = types.ModuleType('fleet_infisical')
    if kind == 'foreign':
        fake.__file__ = '/synthetic-foreign/fleet_infisical.py'
    if kind in ('file-only','foreign-spec'):
        fake.__file__ = str(Path(sys.argv[1]) / 'fleet_infisical.py')
    if kind == 'foreign-spec':
        fake.__spec__ = importlib.util.spec_from_file_location('fleet_infisical','/synthetic-foreign/fleet_infisical.py')
    if kind == 'lazy-marker':
        def forbidden(name):
            raise AssertionError('foreign metadata hook executed')
        fake.__getattr__ = forbidden
    sys.modules['fleet_infisical'] = None if kind == 'blocked' else fake
    try:
        importlib.import_module('fleet_secrets.core')
    except ImportError as exc:
        assert str(exc) == 'shared Infisical client origin invalid'
    else:
        raise AssertionError('foreign module accepted')
    assert 'fleet_secrets.core' not in sys.modules
print('foreign-refused')
""")
        self.assertEqual(
            (result.returncode, result.stdout.strip()), (0, "foreign-refused"), result.stderr
        )

    def test_missing_pinned_source_never_uses_another_path_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary)
            package = payload / "fleet_secrets"
            package.mkdir()
            for name in ("__init__.py", "core.py"):
                shutil.copyfile(SCRIPTS / "fleet_secrets" / name, package / name)
            result = self.run_probe(
                """
import importlib
try:
    importlib.import_module('fleet_secrets.core')
except ImportError as exc:
    assert str(exc) == 'shared Infisical client unavailable'
else:
    raise AssertionError('missing source accepted')
assert 'fleet_infisical' not in sys.modules
print('missing-refused')
""",
                scripts=payload,
            )
        self.assertEqual(
            (result.returncode, result.stdout.strip()), (0, "missing-refused"), result.stderr
        )

    def test_concurrent_direct_import_waits_for_alias_client_initialization(self):
        self.concurrent_imports(alias_first=True)

    def test_concurrent_alias_import_waits_for_canonical_client_initialization(self):
        self.concurrent_imports(alias_first=False)

    def concurrent_imports(self, *, alias_first):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary)
            package = payload / "fleet_secrets"
            package.mkdir()
            for name in ("__init__.py", "core.py"):
                shutil.copyfile(SCRIPTS / "fleet_secrets" / name, package / name)
            (payload / "fleet_infisical.py").write_text(
                "import builtins\n"
                "builtins.fixture_started.set()\n"
                "assert builtins.fixture_release.wait(5)\n"
                "READY = True\n"
            )
            result = self.run_probe(
                "FIRST_ALIAS = "
                + repr(alias_first)
                + "\n"
                + """
import builtins, importlib, threading
from concurrent.futures import ThreadPoolExecutor
builtins.fixture_started = threading.Event()
builtins.fixture_release = threading.Event()
entered, returned = threading.Event(), threading.Event()
if not FIRST_ALIAS:
    sys.path.remove(sys.argv[2])
first_name, second_name = ('fleet_secrets.core','fleet_infisical') if FIRST_ALIAS else ('fleet_infisical','fleet_secrets.core')
def second_import():
    entered.set()
    module = importlib.import_module(second_name)
    ready = getattr(module,'READY',False)
    returned.set()
    return module, ready
with ThreadPoolExecutor(max_workers=2) as pool:
    first = pool.submit(importlib.import_module,first_name)
    assert builtins.fixture_started.wait(2)
    second = pool.submit(second_import)
    assert entered.wait(2)
    try:
        returned.wait(0.25)
    finally:
        builtins.fixture_release.set()
    module, ready = second.result(timeout=2)
    assert ready and module is first.result(timeout=2)
print('initialization-serialized')
""",
                scripts=payload,
            )
        self.assertEqual(
            (result.returncode, result.stdout.strip()),
            (0, "initialization-serialized"),
            result.stderr,
        )

    def test_failed_or_interrupted_pinned_initialization_cleans_registration_and_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary)
            package = payload / "fleet_secrets"
            package.mkdir()
            for name in ("__init__.py", "core.py"):
                shutil.copyfile(SCRIPTS / "fleet_secrets" / name, package / name)
            result = self.run_probe(
                """
import builtins, importlib
path = Path(sys.argv[1]) / 'fleet_infisical.py'
for error, expected in (('RuntimeError','ImportError'),('KeyboardInterrupt','KeyboardInterrupt')):
    sys.modules.pop('fleet_infisical',None)
    sys.modules.pop('fleet_secrets.core',None)
    path.write_text("import builtins,sys\\nbuiltins.failed_client=sys.modules[__name__]\\nraise " + error + "('synthetic failure marker')\\n")
    try:
        importlib.import_module('fleet_secrets.core')
    except BaseException as exc:
        assert type(exc).__name__ == expected
        if expected == 'ImportError':
            assert str(exc) == 'shared Infisical client unavailable'
    else:
        raise AssertionError('source failure accepted')
    assert 'fleet_infisical' not in sys.modules and 'fleet_secrets.core' not in sys.modules
    assert builtins.failed_client.__spec__._initializing is False
    path.write_text('READY = True\\n')
    client = importlib.import_module('fleet_secrets.core')
    assert client.READY and client.__spec__._initializing is False
print('failure-cleanup-verified')
""",
                scripts=payload,
            )
        self.assertEqual(
            (result.returncode, result.stdout.strip()),
            (0, "failure-cleanup-verified"),
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
