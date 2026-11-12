"""Synthetic data and fake programs for the open_loops tests.  Nothing here is real:  repositories are
`example-*`, Linear identifiers use the team key FFX (not a real team), UUIDs are 00000000-0000-4000-8000-<12 hex>,
and every program is a shim or a scripted runner.  `check_fixtures.py --root scripts/open_loops/tests` (a CI
step) fails the build if a real path, identifier, UUID or secret-shaped string shows up in this folder."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fleet_facts.runner import Result
from fleet_facts.tests.builders import (board_row, board_show, comment, doctor_checkout, doctor_report, gql_node, gql_page,
                                        read_log, write_scenario)

SCRIPTS = Path(__file__).resolve().parents[2]
ENTRY = SCRIPTS / "open-loops"
FF_BIN = SCRIPTS / "fleet_facts" / "tests" / "fixtures" / "bin"
OL_BIN = Path(__file__).resolve().parent / "fixtures" / "bin"
NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)       # Sun, Oct 11, 7:00am Central (daylight time)
FAKE_KEY = "fake-key"

__all__ = ["SCRIPTS", "ENTRY", "NOW", "FAKE_KEY", "World", "Sandbox", "iso", "ago", "install_shim", "read_log",
           "board_row", "board_show", "comment", "doctor_checkout", "doctor_report", "gql_node", "gql_page", "write_scenario"]


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(hours: float, now: datetime = NOW) -> str:
    from datetime import timedelta
    return iso(now - timedelta(hours=hours))


def install_shim(directory: Path, name: str, source: str, *, where: Path = OL_BIN) -> Path:
    """Copy a fixture program into `directory` as `name`, with this Python as its interpreter."""
    body = (Path(where) / source).read_text(encoding="utf-8").replace("#!/usr/bin/env python3", "#!" + sys.executable, 1)
    target = Path(directory) / name
    target.write_text(body, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


class World:
    """A scripted outside world for in-process tests of `build_facts`:  every program the gatherers start is
    answered from these attributes by `runner()`, and every call is recorded in `calls`."""

    def __init__(self):
        self.pages = {"": gql_page([], None, 0)}
        self.in_progress: list = []
        self.needs_owner: list = []
        self.show: dict = {}
        self.show_fail: set = set()
        self.lane_report = None
        self.docs: dict = {}          # program name -> JSON document
        self.simple_chat: dict = {"topics": [], "read": {}, "read_rc": {}}
        self.github_fail = False
        self.verify_rc = 0
        self.calls: list = []

    def handle(self, argv: list[str]) -> Result:
        name = os.path.basename(argv[0])
        if name == "gh":
            if self.github_fail:
                return Result(1, "", "gh: HTTP 500")
            cursor = ""
            for i, a in enumerate(argv):
                if a.startswith("endCursor="):
                    cursor = a.split("=", 1)[1]
            return Result(0, json.dumps(self.pages[cursor]), "")
        if name == "simple-board":
            if argv[1] == "list":
                key = argv[argv.index("--status") + 1] if "--status" in argv else argv[argv.index("--label") + 1]
                return Result(0, json.dumps({"in_progress": self.in_progress, "needs-owner": self.needs_owner}[key]), "")
            if argv[1] == "show":
                if argv[2] in self.show_fail:
                    return Result(5, "", "simple-board: unreachable")
                return Result(0, json.dumps(self.show[argv[2]]), "")
        if name == "lane":
            return Result(0, json.dumps(self.lane_report), "") if self.lane_report is not None else Result(1, "", "lane failed")
        if name == "simple-chat":
            if argv[1] == "asks":
                return Result(2, "", "invalid choice: 'asks'")
            if argv[1] == "topics":
                return Result(0, json.dumps(self.simple_chat["topics"]), "")
            if argv[1] == "read":
                topic = argv[argv.index("--topic") + 1]
                if self.simple_chat["read_rc"].get(topic, 0):
                    return Result(self.simple_chat["read_rc"][topic], "", "read failed")
                return Result(0, "".join(json.dumps(m) + "\n" for m in self.simple_chat["read"].get(topic, [])), "")
        if name == "fleet-facts" and argv[1] == "verify":
            return Result(self.verify_rc, "", "")
        if name in self.docs:
            return Result(0, json.dumps(self.docs[name]), "")
        return Result(None, reason="not found")

    def runner(self):
        def run(argv, *, timeout, env=None, cwd=None):
            self.calls.append({"argv": list(argv), "env": dict(env or {})})
            return self.handle(list(argv))
        return run

    def programs(self, directory: Path, names) -> None:
        """Dummy executables so the gatherers' `which` finds them (the scripted runner never runs them)."""
        for name in names:
            path = Path(directory) / name
            path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            path.chmod(0o755)


class Sandbox(unittest.TestCase):
    """A temp HOME and a temp bin folder first on PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        for d in (self.home, self.bin, self.state):
            d.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % self.bin}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # `which` also looks in ~/.local/bin, /opt/homebrew/bin and /usr/local/bin;  a program installed on the
        # machine running the tests must not be found when a test says it is absent.
        extra = mock.patch("fleet_facts.runner.extra_dirs", return_value=[str(self.home / ".local" / "bin")])
        extra.start()
        self.addCleanup(extra.stop)
        os.environ.pop("FLEET_FACTS_BOARD", None)
        os.environ.pop(FAKE_KEY, None)
        self.log = self.root / "calls.log"
        self.scenario: dict = {}

    def write(self, name: str, text: str, mode: int = 0o600) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def calls(self) -> list:
        return read_log(self.log)

    def env(self, **extra) -> dict:
        env = {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % self.bin, "LANG": "C",
               "OL_SCENARIO": str(self.root / "scenario.json"), "OL_LOG": str(self.log), "OL_SCRIPTS": str(SCRIPTS),
               "FF_SCENARIO": str(self.root / "scenario.json"), "FF_LOG": str(self.log), "FF_STATE": str(self.state)}
        env.update(extra)
        return env

    def run_cli(self, *args, entry=ENTRY, env=None, **extra):
        import subprocess
        write_scenario(self.root / "scenario.json", self.scenario)
        return subprocess.run([sys.executable, "-I", str(entry), *args], capture_output=True, text=True,
                              env=env if env is not None else self.env(**extra), timeout=120)
