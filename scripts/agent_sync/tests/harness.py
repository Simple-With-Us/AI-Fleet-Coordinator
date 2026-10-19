"""Shared test harness: a fake Zulip server, a private secrets and state dir, and `run()`.

Every environment is an explicit dict handed to cli.main(env=...), so the real shell's
CLAUDE_CODE_SESSION_ID, AGENT_SEAT and HOME never leak in, and no test can touch
~/.secrets, ~/.agent-sync, the real fleet-secrets or the live realm.  The fake API key is generated per test run, so
the source holds no key-looking literal (the gitleaks job runs on this repo).  Every byte any
command prints is collected, and tearDown fails the test if the key or its base64 form appears.
"""

from __future__ import annotations

import base64
import configparser
import io
import os
import secrets
import shutil
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import cli
from agent_sync import notes as NOTES
from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import BOT_EMAIL, FakeZulip

SESSION = "11112222-3333-4444-5555-666677778888"
TAG = "11112222"


@dataclass
class Result:
    code: int
    out: str
    err: str


def write_rc(path: Path, *, email: str, key: str, site: str, mode: int = 0o600) -> Path:
    parser = configparser.ConfigParser(interpolation=None)
    parser["api"] = {"email": email, "key": key, "site": site}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        parser.write(fh)
    os.chmod(path, mode)
    return path


class Harness(unittest.TestCase):
    """Base class.  `self.fake` is the server, `self.run_cli(...)` runs one command in-process."""

    heartbeat = 0.3

    def setUp(self) -> None:
        self.key = secrets.token_hex(16)
        # The Notes relay consumer comes from the NOTES_RELAY_CONSUMER fleet knob (Infisical):  never reached from a test.
        # A test sets self.notes_knob to change what the knob answers.
        self.notes_knob = "CLAUDE"
        resolver = mock.patch.object(
            NOTES,
            "KNOB_RESOLVER",
            lambda local, refresh: (
                self.notes_knob,
                "test knob",
                local is not None and local != self.notes_knob,
            ),
        )
        resolver.start()
        self.addCleanup(resolver.stop)
        # The Infisical credential fallback (OPS-818) is faked:  a test fills self.infisical_values (name -> value) or sets
        # self.infisical_error, and reads self.infisical_calls (the names each call asked for).  Never the real CLI.
        self.infisical_values: dict[str, str] = {}
        self.infisical_error: Exception | None = None
        self.infisical_calls: list[tuple[str, ...]] = []

        def fake_infisical(names: tuple[str, ...], *_args: Any) -> dict[str, str]:
            self.infisical_calls.append(tuple(names))
            if self.infisical_error is not None:
                raise self.infisical_error
            return {
                name: self.infisical_values[name] for name in names if name in self.infisical_values
            }

        infisical = mock.patch.object(Z, "_default_infisical_fetch", fake_infisical)
        infisical.start()
        self.addCleanup(infisical.stop)
        # The /zulip key NAMES (identities, listener init) come from the same dict:  never a value.
        names = mock.patch.object(
            Z, "_default_infisical_names", lambda *_a: frozenset(self.infisical_values)
        )
        names.start()
        self.addCleanup(names.stop)
        self.fake = FakeZulip(self.key, heartbeat=self.heartbeat)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-sync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.secrets_dir = self.tmp / "secrets"
        self.state_dir = self.tmp / "state"
        self.rc_path = write_rc(
            self.secrets_dir / "Claude-zuliprc", email=BOT_EMAIL, key=self.key, site=self.fake.url
        )
        self.sleeps: list[float] = []
        self.transcript: list[str] = []
        self._transcript_lock = threading.Lock()

    def tearDown(self) -> None:
        token = base64.b64encode(("%s:%s" % (BOT_EMAIL, self.key)).encode()).decode()
        everything = "\n".join(self.transcript)
        self.assertNotIn(self.key, everything, "the API key reached the output")
        self.assertNotIn(token, everything, "the base64 auth token reached the output")

    def add_infisical_bot(
        self, seat: str, *, email: str, key: str, site: str | None = None
    ) -> None:
        """Put a bot's three /zulip names (<BOT>_ZULIP_EMAIL, _KEY and _SITE) in the fake Infisical."""
        email_name, key_name, site_name = Z.infisical_key_names(seat)
        self.infisical_values.update(
            {email_name: email, key_name: key, site_name: site or self.fake.url}
        )

    def drop_infisical_bot(self, seat: str) -> None:
        for name in Z.infisical_key_names(seat):
            self.infisical_values.pop(name, None)

    # ---- environment ----------------------------------------------------------------------
    def env(self, **overrides: str | None) -> dict[str, str]:
        base = {
            "HOME": str(self.home),
            "AGENT_SYNC_REALM": self.fake.url,
            "AGENT_SYNC_SECRETS_DIR": str(self.secrets_dir),
            "AGENT_SYNC_STATE_DIR": str(self.state_dir),
            "AGENT_SEAT": "CLAUDE",
            "CLAUDE_CODE_SESSION_ID": SESSION,
        }
        base.update({k: v for k, v in overrides.items() if v is not None})
        for name, value in overrides.items():
            if value is None:
                base.pop(name, None)
        return base

    def state_path(self, tag: str | None = TAG, seat: str = "CLAUDE") -> Path:
        return self.state_dir / seat / (tag or "nosession") / "state.json"

    # ---- running ----------------------------------------------------------------------------
    def run_cli(
        self,
        *argv: str,
        env: dict[str, str] | None = None,
        stdin: str = "",
        sleep: Callable[[float], None] | None = None,
        timeout: float = 5.0,
        events_timeout: float = 3.0,
        stop: threading.Event | None = None,
    ) -> Result:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            env=self.env() if env is None else env,
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
            home=self.home,
            sleep=sleep or self.sleeps.append,
            timeout=timeout,
            events_timeout=events_timeout,
            stop=stop,
        )
        with self._transcript_lock:
            self.transcript.extend([out.getvalue(), err.getvalue()])
        return Result(code, out.getvalue(), err.getvalue())

    def run_in_thread(self, *argv: str, **kw: Any) -> ThreadedRun:
        return ThreadedRun(self, argv, kw)


class ThreadedRun:
    """Run a command on a worker thread so the test thread can drive the fake server."""

    def __init__(self, harness: Harness, argv: tuple[str, ...], kw: dict[str, Any]) -> None:
        self.result: Result | None = None
        self.thread = threading.Thread(target=self._go, args=(harness, argv, kw), daemon=True)
        self.thread.start()

    def _go(self, harness: Harness, argv: tuple[str, ...], kw: dict[str, Any]) -> None:
        self.result = harness.run_cli(*argv, **kw)

    def join(self, timeout: float = 15.0) -> Result:
        self.thread.join(timeout)
        if self.thread.is_alive() or self.result is None:
            raise AssertionError("the command did not finish within %s seconds" % timeout)
        return self.result
