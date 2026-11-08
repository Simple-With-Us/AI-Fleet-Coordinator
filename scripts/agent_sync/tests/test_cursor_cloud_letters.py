"""The Cursor Cloud letter sweep:  every open Cloud agent takes a letter at location `cloud`, a chat titled for a GB bot
sends that bot one DM per letter with the title to rename it to, and nothing is sent for a chat that already carries
its letter, a title that names no GB bot, or a run that only looks.  The registry runs in process against a temporary
registry;  the Cursor API and `simple-chat dm` are replaced."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest

from agent_sync import cursor_cloud_letters as CCL
from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL

A1 = "bc-11111111-aaaa-4bbb-8ccc-000000000001"
A2 = "bc-22222222-aaaa-4bbb-8ccc-000000000002"


def agent(sid: str, title: str) -> dict:
    return {"session_id": sid, "title": title, "url": "https://cursor.com/agents?id=" + sid, "platform": "cursor-cloud"}


class Fake:
    def __init__(self, code: int = 0, out: str = "") -> None:
        self.calls: list[list[str]] = []
        self.code, self.out = code, out

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.code, self.out, "")


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()
        self.env = {"HOME": self.tmp.name, "AGENT_SYNC_STATE_DIR": self.state, "AGENT_SEAT": "CLAUDE"}
        self.agents = [agent(A1, "GB-Director: tidy the board"), agent(A2, "fix the login page")]
        self.dm = Fake()

    def transport(self, argv: list[str]) -> dict:
        out = io.StringIO()
        PL.registry_main(list(argv), env=self.env, stdout=out)
        return json.loads(out.getvalue())

    def run_sweep(self, **kw) -> dict:
        return CCL.sweep(self.env, discover=lambda: list(self.agents), runner=self.dm, transport=self.transport, **kw)

    def label(self, sid: str) -> str:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("cursor", "cloud", PL.SCOPE, sid)["label"]
        finally:
            reg.close_db()

    def test_every_open_agent_takes_a_letter_and_the_named_bot_is_told_once(self) -> None:
        out = self.run_sweep()
        self.assertEqual(out["agents"], 2)
        l1, l2 = self.label(A1), self.label(A2)
        self.assertRegex(l1, r"^[A-Z]{2}$")
        self.assertNotEqual(l1, l2)
        (call,) = self.dm.calls
        self.assertEqual(call[:4], ["simple-chat", "dm", "--to", "GB-Director"])
        self.assertIn('"%s: GB-Director: tidy the board"' % l1, call[-1])
        self.assertIn('"@**Cursor** %s"' % l1, call[-1])
        self.run_sweep()  # the same letter again:  no second DM
        self.assertEqual(len(self.dm.calls), 1)
        self.assertEqual(self.label(A1), l1)

    def test_a_chat_that_carries_its_letter_sends_nothing(self) -> None:
        self.run_sweep()
        l1 = self.label(A1)
        self.dm.calls.clear()
        self.agents[0] = agent(A1, "%s: GB-Director: tidy the board" % l1)
        CCL.write_state(self.env, {})  # even with nothing remembered
        self.run_sweep()
        self.assertEqual(self.dm.calls, [])
        self.assertEqual(self.label(A1), l1)

    def test_a_dry_run_registers_and_sends_nothing(self) -> None:
        out = self.run_sweep(dry_run=True)
        self.assertEqual([r.get("bot") for r in out["rows"]], ["GB-Director", None])
        self.assertEqual(self.dm.calls, [])
        reg = OR.SessionRegistry(self.db)
        try:
            self.assertIsNone(reg.lookup("cursor", "cloud", PL.SCOPE, A1))
        finally:
            reg.close_db()

    def test_a_dm_that_could_not_start_is_tried_again(self) -> None:
        def broken(argv, **kw):
            raise FileNotFoundError("simple-chat")

        self.dm = broken  # type: ignore[assignment]
        out = self.run_sweep()
        self.assertEqual(out["rows"][0]["dm"], "not-started")
        self.dm = Fake()
        self.run_sweep()
        self.assertEqual(len(self.dm.calls), 1)

    def test_a_refused_name_is_not_asked_again(self) -> None:
        self.dm = Fake(code=2, out="refused: unknown name GB-Director")
        self.assertEqual(self.run_sweep()["rows"][0]["dm"], "refused")
        self.run_sweep()
        self.assertEqual(len(self.dm.calls), 1)

    def test_a_gone_agent_is_forgotten(self) -> None:
        self.run_sweep()
        self.agents = [self.agents[1]]
        self.run_sweep()
        self.assertNotIn(A1, CCL.read_state(self.env))


class TitleTests(unittest.TestCase):
    def test_bot_of(self) -> None:
        self.assertEqual(CCL.bot_of("GB-Director: tidy the board"), ("GB-Director", "tidy the board"))
        self.assertEqual(CCL.bot_of("AB: GB-Fixer: hotfix"), ("GB-Fixer", "hotfix"))
        self.assertEqual(CCL.bot_of("BF-Plumber: tunnels"), ("", ""))
        self.assertEqual(CCL.bot_of("GB-Director"), ("", ""))
        self.assertEqual(CCL.bot_of("fix GB-Director: x"), ("", ""))


if __name__ == "__main__":
    unittest.main()
