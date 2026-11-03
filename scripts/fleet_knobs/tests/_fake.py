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
import secrets
import stat
import sys
import tempfile
import time
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

def fixture_service_token() -> str:
    return "st." + secrets.token_urlsafe(32)


SIGNING = secrets.token_urlsafe(32)
UNRELATED = secrets.token_urlsafe(24)


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
        return [json.loads(line) for line in self.automation_file.read_text().splitlines() if line]

    def fetch_project(self, project_id, path, env, *, names=None, timeout=None):
        """Fake the shared REST helper at its API boundary without invoking a CLI or network."""
        token = env.get("INFISICAL_AI_FLEET_COORDINATOR_TOKEN")
        with self.calls_file.open("a") as fh:
            fh.write(json.dumps({"project": project_id, "path": path, "names": sorted(names) if names is not None else None}) + "\n")
        with self.tokens_file.open("a") as fh:
            fh.write(json.dumps(token) + "\n")
        with self.automation_file.open("a") as fh:
            fh.write(json.dumps({"id": env.get("INFISICAL_AUTOMATION_CLIENT_ID"), "secret": env.get("INFISICAL_AUTOMATION_CLIENT_SECRET")}) + "\n")
        cfg = json.loads(self.config.read_text())
        if cfg["mode"] == "hang":
            raise TimeoutError("fake bounded helper timeout")
        if cfg["mode"] == "fail":
            raise RuntimeError("fake provider failure " + cfg.get("leak", ""))
        if cfg["mode"] == "garbage":
            return object()
        secrets = cfg["secrets"]
        if cfg.get("shape") == "list":
            return [{"key": k, "value": v} for k, v in secrets.items()]
        if cfg.get("shape") == "api":
            return {"secrets": [{"secretKey": k, "secretValue": v} for k, v in secrets.items()]}
        return secrets


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
            source.ENV_KNOBS_TOKEN: fixture_service_token(),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in source.ENV_NAMES:
            os.environ.pop(var, None)
        # Keep old machine-auth variables out of test setup so each case's explicit scoped token
        # determines whether an automated fetch can run.
        for var in (
            "INFISICAL_AUTOMATION_CLIENT_ID",
            "INFISICAL_AUTOMATION_CLIENT_SECRET",
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
        ):
            os.environ.pop(var, None)
        fallback = mock.patch.object(source, "FALLBACK_DIRS", ())
        fallback.start()
        self.addCleanup(fallback.stop)
        helper = mock.patch.object(source, "HELPER_LOADER", return_value=self.fake)
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
