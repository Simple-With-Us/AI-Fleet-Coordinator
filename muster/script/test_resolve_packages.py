import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "resolve_packages", Path(__file__).with_name("resolve_packages.py")
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ResolutionTests(unittest.TestCase):
    def run_fake(self, body, **options):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.py"
            path.write_text(body)
            return m.resolve([sys.executable, str(path), tmp], delay=0, **options)

    def test_504_recovers_on_second_attempt(self):
        self.assertEqual(
            self.run_fake(
                'import pathlib,sys\np=pathlib.Path(sys.argv[1])/"count"\nn=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\nprint("badResponseStatusCode(504)" if n==0 else "resolved")\nsys.exit(74 if n==0 else 0)'
            ),
            0,
        )

    def test_persistent_504_is_bounded(self):
        self.assertEqual(
            self.run_fake(
                'import sys;print("badResponseStatusCode(504)");sys.exit(74)', attempts=2
            ),
            74,
        )

    def test_unknown_failure_is_not_retried(self):
        self.assertEqual(self.run_fake('import sys;print("package missing");sys.exit(65)'), 65)

    def test_checksum_error_stops_even_with_transient_text(self):
        self.assertEqual(
            self.run_fake(
                'import sys;print("checksum mismatch badResponseStatusCode(504)");sys.exit(1)'
            ),
            1,
        )

    def test_timeout_stops_not_retried(self):
        self.assertEqual(self.run_fake("import time;time.sleep(5)", timeout=0.05), 124)

    def test_classifier_is_narrow(self):
        for status in [429, 500, 502, 503, 504]:
            self.assertTrue(m.TRANSIENT.search("badResponseStatusCode(%s)" % status))
        for text in [
            "badResponseStatusCode(404)",
            "badResponseStatusCode(401)",
            "Swift error: expected 504",
            "badResponseStatusCode(5040)",
        ]:
            self.assertIsNone(m.TRANSIENT.search(text))

    def test_resolution_does_not_run_build_or_signing(self):
        helper = Path(__file__).with_name("resolve_packages.py").read_text()
        self.assertIn('"-resolvePackageDependencies"', helper)
        self.assertNotIn('"build"', helper)
        self.assertNotIn('"codesign"', helper)

    def test_both_build_routes_resolve_before_compiling(self):
        root = Path(__file__).resolve().parents[2]
        ci = (root / ".github/workflows/ci.yml").read_text()
        debug = ci[ci.index("- name: Generate and compile unsigned scaffold") :]
        self.assertLess(debug.index("resolve_packages.py"), debug.index("xcodebuild -project"))
        release = (root / "muster/script/release_build.sh").read_text()
        build = release[release.index("build_and_stage() {") :]
        self.assertLess(
            build.index("resolve_packages.py"), build.index("/usr/bin/xcodebuild -project")
        )
        self.assertIn('--derived-data "$DERIVED"', build)
        self.assertIn("--derived-data .build/CI", debug)

    def test_unknown_and_integrity_failures_do_not_sleep_or_retry(self):
        for text in [
            "package missing",
            "checksum mismatch badResponseStatusCode(504)",
            "authentication badResponseStatusCode(503)",
        ]:
            sleeps = []
            result = self.run_fake(
                "import sys;print(" + repr(text) + ");sys.exit(65)", sleep=sleeps.append
            )
            self.assertEqual(result, 65)
            self.assertEqual(sleeps, [])

    def test_transient_retry_budget_is_exact(self):
        sleeps = []
        self.assertEqual(
            self.run_fake(
                'import sys;print("badResponseStatusCode(504)");sys.exit(74)',
                attempts=3,
                sleep=sleeps.append,
            ),
            74,
        )
        self.assertEqual(sleeps, [0, 0])
