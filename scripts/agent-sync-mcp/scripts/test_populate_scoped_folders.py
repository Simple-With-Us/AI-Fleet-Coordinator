#!/usr/bin/env python3
"""Mocked-API contract tests for populate-scoped-folders.py.

No network: urllib.request.urlopen is stubbed with a fake Infisical.  Values are
sentinel strings ("VALUE-<name>"); every test asserts they never appear in any
printed output (names-only reporting, redacted errors)."""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location(
    "psf", str(pathlib.Path(__file__).with_name("populate-scoped-folders.py"))
)
psf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(psf)

DEST_PATHS = {(psf.ENV, p) for _, _, _, p, _ in psf.MAPPING}


class FakeInfisical:
    """Routes urlopen calls. values[(ws, path, name)] -> value; missing = 404."""

    def __init__(self, scopes=None, values=None, fail=None):
        self.scopes = (
            scopes
            if scopes is not None
            else [{"environment": psf.ENV, "secretPath": p} for _, p in sorted(DEST_PATHS)]
        )
        self.values = values or {}
        self.fail = fail or {}  # ("read"|"upsert", name) -> HTTP code to raise
        self.upserts = []

    def _err(self, url, code, body="server message"):
        raise urllib.error.HTTPError(
            url, code, "err", {}, io.BytesIO(json.dumps({"message": body}).encode())
        )

    def open(self, request, timeout=35):
        url, method = request.full_url, request.get_method()
        if url.endswith("/api/v2/service-token"):
            return io.BytesIO(json.dumps({"id": "st1", "scopes": self.scopes}).encode())
        if url.endswith("/auth/universal-auth/login"):
            return io.BytesIO(json.dumps({"accessToken": "automation"}).encode())
        if "/api/v3/secrets/raw/" in url:
            name = urllib.parse.unquote(url.split("/api/v3/secrets/raw/")[1].split("?")[0])
            if method == "GET":
                if ("read", name) in self.fail:
                    self._err(url, self.fail[("read", name)], "leak VALUE-x")
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
                key = (q["workspaceId"], q["secretPath"], name)
                if key not in self.values:
                    self._err(url, 404)
                return io.BytesIO(
                    json.dumps({"secret": {"secretValue": self.values[key]}}).encode()
                )
            if method == "POST":
                if ("upsert", name) in self.fail:
                    self._err(url, self.fail[("upsert", name)], "leak VALUE-x")
                body = json.loads(request.data.decode())
                self.values[(body["workspaceId"], body["secretPath"], name)] = body["secretValue"]
                self.upserts.append((body["secretPath"], name))
                return io.BytesIO(b"{}")
        raise AssertionError("unexpected call: " + method + " " + url)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeInfisical()
        self._orig = urllib.request.urlopen
        urllib.request.urlopen = self.fake.open
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        (d / "username").write_text("u")
        (d / "password").write_text("p")
        self._env = dict(os.environ)
        os.environ["INFISICAL_AUTOMATION_DIR"] = self.tmp.name
        os.environ["READ_TOOLS_SERVICE_TOKEN"] = "read-tools-st"

    def tearDown(self):
        urllib.request.urlopen = self._orig
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def run_main(self, *argv):
        out = io.StringIO()
        old = sys.argv
        sys.argv = ["populate-scoped-folders.py", *argv]
        code = None
        with contextlib.redirect_stdout(out):
            try:
                psf.main()
            except SystemExit as e:
                code = e.code
        sys.argv = old
        return code, out.getvalue()


class TestMapping(Base):
    def test_integrity(self):
        self.assertEqual(len(psf.MAPPING), 72)
        self.assertEqual(len({(d, n) for _, _, _, d, n in psf.MAPPING}), 72)
        self.assertEqual(len(DEST_PATHS), 5)  # /wake /fleetlink /zulip /linear /tools


class TestGate(Base):
    def test_full_coverage_passes(self):
        self.assertTrue(psf.check_read_tools_scope())

    def test_missing_scope_aborts(self):
        self.fake.scopes = [s for s in self.fake.scopes if s["secretPath"] != "/tools"]
        self.assertFalse(psf.check_read_tools_scope())

    def test_endpoint_404_aborts(self):
        def boom(request, timeout=35):
            if request.full_url.endswith("/api/v2/service-token"):
                raise urllib.error.HTTPError(request.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
            return self.fake.open(request, timeout)

        urllib.request.urlopen = boom
        self.assertFalse(psf.check_read_tools_scope())

    def test_unknown_schema_aborts(self):
        self.fake.scopes = {"prod": ["/wake"]}  # not the expected list of dicts
        self.assertFalse(psf.check_read_tools_scope())

    def test_no_token_aborts(self):
        del os.environ["READ_TOOLS_SERVICE_TOKEN"]
        self.assertFalse(psf.check_read_tools_scope())


class TestDryRun(Base):
    def test_dry_run_writes_nothing(self):
        code, out = self.run_main()
        self.assertIsNone(code)
        self.assertEqual(self.fake.upserts, [])
        self.assertIn("DRY RUN", out)


def seeded(fake):
    """Source values present; destinations absent."""
    for sws, spath, sname, dpath, dname in psf.MAPPING:
        fake.values[(sws, spath or "/", sname)] = "VALUE-" + sname


class TestApply(Base):
    def test_happy_path_72_copies_names_only(self):
        seeded(self.fake)
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(len(self.fake.upserts), 72)
        for sws, spath, sname, dpath, dname in psf.MAPPING:
            self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-" + sname)
        self.assertNotIn("VALUE-", out)

    def test_idempotent_rerun_skips_equal(self):
        seeded(self.fake)
        self.run_main("--apply")
        self.fake.upserts.clear()
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(self.fake.upserts, [])
        self.assertIn("already in sync", out)

    def test_collision_stops_no_overwrite(self):
        seeded(self.fake)
        sws, spath, sname, dpath, dname = psf.MAPPING[10]
        self.fake.values[(psf.FLEETOPS, dpath, dname)] = "VALUE-different-existing"
        code, out = self.run_main("--apply")
        self.assertEqual(code, 2)
        self.assertNotIn((dpath, dname), self.fake.upserts)
        self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-different-existing")
        self.assertIn("COLLISION", out)
        self.assertNotIn("VALUE-", out)

    def test_permission_error_visible_stop_redacted(self):
        seeded(self.fake)
        victim = psf.MAPPING[5][2]
        self.fake.fail[("read", victim)] = 403
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403", out)
        self.assertIn(victim, out)  # the NAME is fine
        self.assertNotIn("VALUE-", out)  # values and server body never printed

    def test_partial_write_stop_then_rerun_completes(self):
        seeded(self.fake)
        victim = psf.MAPPING[40][2]
        self.fake.fail[("upsert", victim)] = 500
        code, out = self.run_main("--apply")
        self.assertEqual(code, 1)
        self.assertIn("STOP at 41/72", out)
        first = list(self.fake.upserts)
        self.assertEqual(len(first), 40)
        del self.fake.fail[("upsert", victim)]
        code, out = self.run_main("--apply")
        self.assertIsNone(code)
        self.assertEqual(len(self.fake.upserts) - len(first), 32)  # only the remainder re-copied
        for sws, spath, sname, dpath, dname in psf.MAPPING:
            self.assertEqual(self.fake.values[(psf.FLEETOPS, dpath, dname)], "VALUE-" + sname)


if __name__ == "__main__":
    unittest.main(verbosity=2)
