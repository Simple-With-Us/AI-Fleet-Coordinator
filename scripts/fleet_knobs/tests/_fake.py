"""A fake ``infisical`` executable and the hermetic environment the knob tests run in.

The fake is a real script on PATH, so the code under test runs its normal subprocess path.
It records every argv, so a test can prove how many fetches ran and that ``--env=prod`` was
the only environment ever asked for.  Nothing here touches the network, the real CLI, the
real HOME or ~/.secrets.
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
        exe = self.bin_dir / "infisical"
        exe.write_text(SCRIPT.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        self.set()

    def set(self, mode: str = "ok", secrets=None, shape: str = "map", **extra) -> None:
        cfg = {"mode": mode, "secrets": secrets or {}, "shape": shape,
               "calls": str(self.calls_file)}
        cfg.update(extra)
        self.config.write_text(json.dumps(cfg))

    def calls(self) -> list:
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines() if line]


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
        env = {"HOME": str(self.home), "PATH": str(self.fake.bin_dir),
               "FAKE_INFISICAL_CONFIG": str(self.fake.config)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in source.ENV_NAMES:
            os.environ.pop(var, None)
        fallback = mock.patch.object(source, "FALLBACK_DIRS", ())
        fallback.start()
        self.addCleanup(fallback.stop)
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

    def new_process(self) -> None:
        """Forget the in-process fetch, as a new job process would start."""
        knobs.reset_for_tests()
