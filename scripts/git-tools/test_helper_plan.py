#!/usr/bin/env python3
"""Tests for the seat-helper integration of simple-git.

These run the REAL host helper (seat-remote-defaults.py from the grounding capture, referenced by the
shipped integrations/seat-remote-defaults.py.patch) with the patch's COMMANDS entry applied in memory, so
the simple-git routing is tested against the code that actually runs on the hosts - not a stub.  The seat's
bearer file read is faked (the pinned owner and its directory exist only on the hosts).

The capture ships at testdata/seat-remote-defaults.capture.py (nonsecret host configuration); GIT_CLI_TEST_HELPER_SRC overrides it.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

CANDIDATES = [
    Path(os.environ.get("GIT_CLI_TEST_HELPER_SRC", "")),
    Path(__file__).resolve().parent / "testdata" / "seat-remote-defaults.capture.py",
    Path(__file__).resolve().parents[3] / "ground" / "seat-remote-defaults.live.py",
]
LIVE_HELPER = next((p for p in CANDIDATES if str(p) and p.is_file()), None)

PATCH_OLD = '    "recall": {"mode": "SIMPLE_RECALL_REMOTE", "native": None},\n'
PATCH_NEW = (
    PATCH_OLD
    + '    "simple-git": {"mode": "SIMPLE_GIT_REMOTE", "native": "SIMPLE_GIT_NATIVE_ECHO"},\n'
)

OWN = "a" * 40
OTHER = "b" * 40


@unittest.skipUnless(LIVE_HELPER is not None, "live helper capture not available")
class TestHelperPlan(unittest.TestCase):
    def setUp(self):
        source = LIVE_HELPER.read_text()
        assert PATCH_OLD in source, "the capture no longer matches the patch anchor"
        patched = source.replace(PATCH_OLD, PATCH_NEW, 1)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        tmp.write(patched)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        loader = importlib.machinery.SourceFileLoader("helper_patched", tmp.name)
        spec = importlib.util.spec_from_loader("helper_patched", loader)
        self.helper = importlib.util.module_from_spec(spec)
        loader.exec_module(self.helper)
        self.reads = []
        def fake_read_bearer(seat):
            self.reads.append(seat)
            return OWN
        self.helper._read_bearer = fake_read_bearer

    def plan(self, argv, env):
        return self.helper.plan("simple-git", argv, env)

    def test_remote_plan_loads_the_declared_seats_own_bearer(self):
        changes = self.plan(["list"], {"AGENT_SEAT": "CODEX"})
        self.assertEqual(changes, {"SIMPLE_TOOLS_BEARER": OWN, "SIMPLE_GIT_REMOTE": "1"})
        self.assertEqual(self.reads, ["CODEX"])  # the helper lowercases inside

    def test_native_shortcut_reads_nothing(self):
        changes = self.plan(["call", "gr_wf_repo_get"], {"AGENT_SEAT": "ECHO", "SIMPLE_GIT_NATIVE_ECHO": "1"})
        self.assertEqual(changes, {})
        self.assertEqual(self.reads, [])

    def test_inherited_other_seat_bearer_rejected(self):
        with self.assertRaises(self.helper.FleetCliStop):
            self.plan(["list"], {"AGENT_SEAT": "CODEX", "SIMPLE_TOOLS_BEARER": OTHER})

    def test_inherited_matching_bearer_accepted(self):
        changes = self.plan(["list"], {"AGENT_SEAT": "CODEX", "SIMPLE_TOOLS_BEARER": OWN})
        self.assertEqual(changes["SIMPLE_TOOLS_BEARER"], OWN)

    def test_help_stays_out_and_reads_nothing(self):
        for argv in (["--help"], ["list", "--help"], ["--version"]):
            self.assertEqual(self.plan(argv, {"AGENT_SEAT": "CODEX"}), {})
        self.assertEqual(self.reads, [])

    def test_undeclared_process_gets_no_bearer(self):
        self.assertEqual(self.plan(["list"], {}), {})
        self.assertEqual(self.reads, [])

    def test_conflicting_declarations_refused(self):
        with self.assertRaises(self.helper.FleetCliStop):
            self.plan(["list"], {"AGENT_SEAT": "CODEX", "AGENT_TAG": "GROK"})

    def test_mode_explicitly_off_stays_out(self):
        changes = self.plan(["list"], {"AGENT_SEAT": "CODEX", "SIMPLE_GIT_REMOTE": "0"})
        self.assertEqual(changes, {})
        self.assertEqual(self.reads, [])

    def test_unsupported_command_refused(self):
        with self.assertRaises(self.helper.FleetCliStop):
            self.helper.plan("not-a-command", [], {})


if __name__ == "__main__":
    unittest.main()
