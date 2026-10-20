"""A fake ``infisical`` executable and the hermetic environment the knob tests run in.

The fake is a real script on PATH, so the code under test runs its normal subprocess path.
It records every argv, so a test can prove how many fetches ran and that ``--env=prod`` was
the only environment ever asked for.  Nothing here touches the network, the real CLI, the
real HOME or any credential folder.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

from fleet_knobs import knobs, source

SCRIPT = """#!{python}
import json, os, sys, time
cfg = json.load(open(os.environ["FAKE_INFISICAL_CONFIG"]))
with open(cfg["calls"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
if cfg.get("tokens"):
    with open(cfg["tokens"], "a") as fh:
        fh.write(json.dumps(os.environ.get("INFISICAL_TOKEN")) + "\\n")
if cfg.get("automation"):
    with open(cfg["automation"], "a") as fh:
        fh.write(json.dumps({{
            "id": os.environ.get("INFISICAL_AUTOMATION_CLIENT_ID"),
            "secret": os.environ.get("INFISICAL_AUTOMATION_CLIENT_SECRET"),
        }}) + "\\n")
mode = cfg["mode"]
if mode == "fail":
    sys.stderr.write("fake infisical: boom " + cfg.get("leak", "") + "\\n")
    sys.exit(cfg.get("code", 1))
if mode == "hang":
    time.sleep(60)
if mode == "garbage":
    sys.stdout.write("this is not json\\n")
    sys.exit(0)
secrets = cfg["secrets"]
if cfg.get("shape") == "list":
    print(json.dumps([{{"key": k, "value": v}} for k, v in secrets.items()]))
elif cfg.get("shape") == "api":
    print(json.dumps({{"secrets": [{{"secretKey": k, "secretValue": v}} for k, v in secrets.items()]}}))
else:
    print(json.dumps(secrets))
"""

SIGNING = "sentinelSIGNINGkey0123456789abcdefghijklmnop"
UNRELATED = "unrelatedSecretSentinelValue9876543210"


class FakeInfisical:
    def __init__(self, root: pathlib.Path) -> None:
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.config = root / "fake-config.json"
        self.calls_file = root / "calls.jsonl"
        self.tokens_file = root / "tokens.jsonl"
        self.automation_file = root / "automation.jsonl"
        exe = self.bin_dir / "infisical"
        exe.write_text(SCRIPT.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.set()

    def set(self, mode: str = "ok", secrets=None, shape: str = "map", **extra) -> None:
        cfg = {
            "mode": mode,
            "secrets": secrets or {},
            "shape": shape,
            "calls": str(self.calls_file),
            "tokens": str(self.tokens_file),
            "automation": str(self.automation_file),
        }
        cfg.update(extra)
        self.config.write_text(json.dumps(cfg))

    def tokens(self) -> list:
        """The INFISICAL_TOKEN each call saw (None when unset).  Fake tokens only."""
        if not self.tokens_file.exists():
            return []
        return [json.loads(line) for line in self.tokens_file.read_text().splitlines() if line]

    def calls(self) -> list:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines() if line]

    def automation_seen(self) -> list:
        """The automation pair each CLI call actually received (None when absent)."""
        if not self.automation_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.automation_file.read_text().splitlines()
            if line
        ]


class KnobTestCase(unittest.TestCase):
    """Fresh HOME, fake CLI first and only on PATH, no INFISICAL_ENV*, clean module state."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake = FakeInfisical(self.root)
        env = {
            "HOME": str(self.home),
            "PATH": str(self.fake.bin_dir),
            "FAKE_INFISICAL_CONFIG": str(self.fake.config),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in source.ENV_NAMES:
            os.environ.pop(var, None)
        # The automation pair too:  these tests set whichever half they mean to set, and an
        # ambient pair on the host (the Hetzner server exports it) would make a half-set case
        # look complete and flip the reason token.
        for var in (source.ENV_AUTOMATION_ID, source.ENV_AUTOMATION_SECRET):
            os.environ.pop(var, None)
        fallback = mock.patch.object(source, "FALLBACK_DIRS", ())
        fallback.start()
        self.addCleanup(fallback.stop)
        # No fleet-secrets helper:  the real one would read this Mac's keychains.  A test that
        # covers the machine identity patches source.HELPER_LOADER with a fake helper itself.
        helper = mock.patch.object(source, "HELPER_LOADER", lambda: None)
        helper.start()
        self.addCleanup(helper.stop)
        knobs.reset_for_tests()
        self.addCleanup(knobs.reset_for_tests)
        # Keep the library's warnings out of the test output (assertLogs still sees them).
        quiet = logging.NullHandler()
        logging.getLogger("fleet_knobs").addHandler(quiet)
        self.addCleanup(logging.getLogger("fleet_knobs").removeHandler, quiet)

    @property
    def state_dir(self) -> pathlib.Path:
        return self.home / ".local" / "state" / "fleet-knobs"

    @property
    def lkg_file(self) -> pathlib.Path:
        return self.state_dir / "lkg.json"

    def new_process(self, *, keep_failure_marker: bool = False) -> None:
        """Forget the in-process fetch, as a new job process would start.

        The negative-cache marker is cleared too unless asked to keep it, as if the 60 second
        window had passed, so a test of a later fetch is not skipped by an earlier failure."""
        knobs.reset_for_tests()
        if not keep_failure_marker:
            (self.state_dir / "fetch-failed.json").unlink(missing_ok=True)
