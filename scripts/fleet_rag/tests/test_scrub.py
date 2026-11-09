"""scrub tests: redaction patterns, the private gitleaks report, and the fail-closed gate."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from fleet_rag import scrub


class RedactionTests(unittest.TestCase):
    def _kinds(self, text: str) -> tuple[str, list[str]]:
        return scrub.scrub(text)

    def test_shapes_are_redacted(self) -> None:
        cases = {
            "aws-access-key": "AKIAABCDEFGHIJKLMNOP",
            "github-token": "ghp_" + "a" * 36,
            "github-pat": "github_pat_" + "A" * 70,
            "slack-token": "xoxb-123456789012-abcdefghijkl",
            "slack-webhook": "https://hooks.slack.com/services/T000/B000/XXXXXXXX",
            "anthropic-key": "sk-ant-" + "x" * 30,
            "openai-key": "sk-proj-" + "y" * 30,
            "google-api-key": "AIza" + "0" * 35,
            "tailscale-key": "tskey-auth-" + "k" * 20,
            "infisical-token": "st.aaaaaaaaaa.bbbbbbbbbb.cccccccccc",
            "jwt": "eyJ" + "a" * 20 + ".eyJ" + "b" * 20 + "." + "c" * 20,
            "sentry-dsn": "https://0123456789abcdef0123@o1.ingest.sentry.io/12345",
            "bearer": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "private-key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
        }
        for kind, sample in cases.items():
            out, kinds = self._kinds(f"before {sample} after")
            self.assertIn(kind, kinds, kind)
            self.assertIn("[REDACTED:", out, kind)
            secret_core = sample.split()[-1] if kind != "private-key" else "MIIE"
            self.assertNotIn(secret_core, out, kind)
            self.assertTrue(out.startswith("before ") and out.endswith(" after"), kind)

    def test_assignment_and_basic_auth(self) -> None:
        out, kinds = self._kinds("QDRANT_API_KEY=abcdefghijklmnop1234 and password: 'hunter2hunter2hunter2'")
        self.assertIn("assignment", kinds)
        self.assertEqual(out.count("[REDACTED:secret]"), 2)
        self.assertTrue(out.startswith("QDRANT_API_KEY="))
        out, kinds = self._kinds("postgres://user:s3cretpw@db.internal:5432/x")
        self.assertIn("url-basic-auth", kinds)
        self.assertEqual(out, "postgres://user:[REDACTED:password]@db.internal:5432/x")

    def test_placeholders_and_hashes_are_kept(self) -> None:
        sha = "a" * 64
        for text in ("API_KEY=${API_KEY}", "TOKEN=<TOKEN>", "secret=[REDACTED:secret]", "PASSWORD=***",
                     f"content_hash {sha}", f"sha256:{sha}", "version 1.2.3", "id 0123456789abcdef"):
            out, kinds = self._kinds(text)
            self.assertEqual(out, text, text)
            self.assertEqual(kinds, [], text)
        # 64-hex next to a key word IS redacted (bare form; `token: <hex>` is caught by the assignment rule)
        out, kinds = self._kinds(f"api key {sha}")
        self.assertIn("hex-64", kinds)
        self.assertNotIn(sha, out)

    def test_idempotent_and_scrub_rows(self) -> None:
        once, kinds = scrub.scrub("SLACK_TOKEN=xoxb-123456789012-abcdefghijkl")
        twice, kinds2 = scrub.scrub(once)
        self.assertEqual(once, twice)
        self.assertEqual(kinds2, [])
        rows, touched = scrub.scrub_rows([{"text": "clean"}, {"text": "ghp_" + "b" * 36}, {}])
        self.assertEqual(touched, 1)
        self.assertEqual(rows[0], {"text": "clean"})
        self.assertIn("github-token", rows[1]["scrubbed"])
        self.assertNotIn("ghp_", rows[1]["text"])
        self.assertEqual(rows[2], {})


class _FakeRun:
    """Stand-in for subprocess.run that mimics `gitleaks version` and a scan invocation."""

    def __init__(self, findings=None, returncode=0, stderr=b"", stdout=b"", write_report=True,
                 version=b"8.30.1\n", raise_timeout=False):  # noqa: ANN001
        self.findings, self.returncode, self.stderr, self.stdout = findings or [], returncode, stderr, stdout
        self.write_report, self.version, self.raise_timeout = write_report, version, raise_timeout
        self.calls: list[list[str]] = []
        self.report_paths: list[str] = []

    def __call__(self, cmd, capture_output=False, timeout=None, check=False, **kw):  # noqa: ANN001
        self.calls.append(list(cmd))
        if cmd[1] == "version":
            return subprocess.CompletedProcess(cmd, 0, self.version, b"")
        if cmd[1] == "dir" and cmd[2] == "--help":
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd, timeout or 0)
        report = cmd[cmd.index("--report-path") + 1]
        self.report_paths.append(report)
        if self.write_report:
            pathlib.Path(report).write_text(json.dumps(self.findings), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


class GitleaksGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = pathlib.Path(self.tmp.name)
        self.staged = self.tmpdir / "staged.jsonl"
        self.staged.write_text('{"text":"a"}\n{"text":"b"}\n{"text":"c"}\n', encoding="utf-8")
        # Every temp file the gate creates lands in our private directory so leftovers are visible.
        self._old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.tmpdir)
        scrub._mode_cache.clear()
        self.which = mock.patch.object(scrub.shutil, "which", lambda name: "/fake/bin/gitleaks")
        self.which.start()

    def tearDown(self) -> None:
        self.which.stop()
        tempfile.tempdir = self._old_tempdir
        scrub._mode_cache.clear()
        self.tmp.cleanup()

    def _leftovers(self) -> list[str]:
        return sorted(p.name for p in self.tmpdir.iterdir() if p.name != "staged.jsonl")

    def test_absent_gitleaks_returns_empty(self) -> None:
        with mock.patch.object(scrub.shutil, "which", lambda name: None):
            self.assertEqual(scrub.gitleaks_flagged(str(self.staged)), set())
        self.assertEqual(self._leftovers(), [])

    def test_flagged_lines_and_private_report_cleanup(self) -> None:
        fake = _FakeRun(findings=[{"RuleID": "slack", "StartLine": 2}, {"RuleID": "gh", "StartLine": 3},
                                  {"RuleID": "x", "StartLine": 0}])
        with mock.patch.object(scrub.subprocess, "run", fake):
            flagged = scrub.gitleaks_flagged(str(self.staged))
        self.assertEqual(flagged, {2, 3})
        # dir subcommand on an 8.30 build, --exit-code 0, json report
        scan = [c for c in fake.calls if c[1] == "dir"][0]
        self.assertEqual(scan[2], str(self.staged))
        self.assertIn("--exit-code", scan)
        self.assertEqual(scan[scan.index("--exit-code") + 1], "0")
        self.assertEqual(scan[scan.index("--report-format") + 1], "json")
        # report lived inside a private temp dir and is gone afterwards, dir included
        rep = pathlib.Path(fake.report_paths[0])
        self.assertEqual(rep.parent.parent, self.tmpdir)
        self.assertFalse(rep.exists())
        self.assertFalse(rep.parent.exists())
        self.assertEqual(self._leftovers(), [])

    def test_report_dir_is_private(self) -> None:
        seen: dict[str, int] = {}

        class Peek(_FakeRun):
            def __call__(self, cmd, **kw):  # noqa: ANN001
                if cmd[1] not in ("version",):
                    d = pathlib.Path(cmd[cmd.index("--report-path") + 1]).parent
                    seen["mode"] = d.stat().st_mode & 0o777
                return super().__call__(cmd, **kw)

        with mock.patch.object(scrub.subprocess, "run", Peek()):
            scrub.gitleaks_flagged(str(self.staged))
        self.assertEqual(seen["mode"], 0o700)

    def test_falls_back_to_detect_on_old_versions(self) -> None:
        fake = _FakeRun(version=b"v8.18.4\n")
        with mock.patch.object(scrub.subprocess, "run", fake):
            self.assertEqual(scrub.gitleaks_flagged(str(self.staged)), set())
        scan = fake.calls[-1]
        self.assertEqual(scan[1:4], ["detect", "--no-git", "--source"])
        self.assertEqual(scan[4], str(self.staged))
        self.assertEqual(self._leftovers(), [])

    def test_fail_closed_paths(self) -> None:
        cases = {
            "non-zero exit": _FakeRun(returncode=1, stderr=b"7:15PM FTL stat x: no such file\n", write_report=False),
            "missing report": _FakeRun(write_report=False),
            "FTL on stderr": _FakeRun(stderr=b"\x1b[90m7:15PM\x1b[0m \x1b[31mFTL\x1b[0m boom\n"),
            "ERR on stdout": _FakeRun(stdout=b"7:15PM ERR could not read config\n"),
            "timeout": _FakeRun(raise_timeout=True),
        }
        for label, fake in cases.items():
            scrub._mode_cache.clear()
            with mock.patch.object(scrub.subprocess, "run", fake):
                with self.assertRaises(scrub.GitleaksError, msg=label):
                    scrub.gitleaks_flagged(str(self.staged), timeout=5)
            self.assertEqual(self._leftovers(), [], label)
        # unparsable report
        scrub._mode_cache.clear()

        class Garbage(_FakeRun):
            def __call__(self, cmd, **kw):  # noqa: ANN001
                r = super().__call__(cmd, **kw)
                if cmd[1] == "dir" and cmd[2] != "--help":
                    pathlib.Path(cmd[cmd.index("--report-path") + 1]).write_text("{not json", encoding="utf-8")
                return r

        with mock.patch.object(scrub.subprocess, "run", Garbage()):
            with self.assertRaises(scrub.GitleaksError):
                scrub.gitleaks_flagged(str(self.staged))
        self.assertEqual(self._leftovers(), [])
        # WRN ("leaks found: 2") and INF lines are not failures
        scrub._mode_cache.clear()
        ok = _FakeRun(findings=[{"StartLine": 1}], stderr=b"7:15PM INF scanned\n7:15PM WRN leaks found: 1\n")
        with mock.patch.object(scrub.subprocess, "run", ok):
            self.assertEqual(scrub.gitleaks_flagged(str(self.staged)), {1})

    def test_missing_staged_file_fails_closed(self) -> None:
        with mock.patch.object(scrub.subprocess, "run", _FakeRun()):
            with self.assertRaises(scrub.GitleaksError):
                scrub.gitleaks_flagged(str(self.tmpdir / "nope.jsonl"))
        self.assertEqual(self._leftovers(), [])

    @unittest.skipUnless(scrub.shutil.which("gitleaks"), "gitleaks not installed")
    def test_real_binary_smoke(self) -> None:
        self.which.stop()
        scrub._mode_cache.clear()
        try:
            leaky = self.tmpdir / "leaky.jsonl"
            # Assemble at runtime so git history is not a contiguous Slack bot token
            # (GitHub push protection).  gitleaks still has to flag the written value.
            fake = "xoxb-" + "123456789012" + "-" + "123456789012" + "-" + "AbCdEfGhIjKlMnOpQrStUvWx"
            leaky.write_text('{"text":"clean"}\n{"text":"' + fake + '"}\n', encoding="utf-8")
            self.assertEqual(scrub.gitleaks_flagged(str(leaky)), {2})
            self.assertEqual(scrub.gitleaks_flagged(str(self.staged)), set())
            with self.assertRaises(scrub.GitleaksError):
                scrub.gitleaks_flagged(str(self.tmpdir / "missing.jsonl"))
            self.assertEqual(sorted(self._leftovers()), ["leaky.jsonl"])
        finally:
            self.which.start()


if __name__ == "__main__":
    unittest.main()
