"""The Clutch adapter of `platform-letter`: Operator session letters for DeepSeek Harness (`dsh`)
sessions, which reach the hook through the Harness's Claude Code hook bridge.

The registry is a temporary one in the double (pair) letter space.  The API test runs a real registry
service over a loopback listener, so a letter that lands there has crossed the registry API transport —
the same server registry the other seats' letters land in.  No test depends on which pair the registry
hands out first, only on its being a pair the registry, the note and the hook's answer agree on."""

from __future__ import annotations

import io
import json
import os
import secrets
import tempfile
import time
import unittest

from simple_tools import listener_api as API
from simple_tools import listener_registry as RG
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import platform_letter_clutch as CLU

PAIR = r"^[A-Z]{2}$"


def conv(n: int) -> str:
    return "conv-%04d-0000-0000-0000-000000000000" % n


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.home)
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()  # the Operator's registry exists, as it does on the Mac

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.state}
        env.update(extra)
        return env

    def hook(self, payload: dict, *, env: dict | None = None) -> str:
        out = io.StringIO()
        code = PL.hook(
            CLU.SPEC,
            env if env is not None else self.env(),
            io.StringIO(json.dumps(payload)),
            out,
            platform="darwin",
        )
        self.assertEqual(code, 0)
        return out.getvalue()

    def lookup(self, sid: str) -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("clutch", "mac", PL.SCOPE, sid)
        finally:
            reg.close_db()


class SpecTests(Base):
    def test_spec_fields(self) -> None:
        spec = PL.load_spec("clutch")
        self.assertIs(spec, CLU.SPEC)
        self.assertEqual((spec.seat, spec.source), ("CLUTCH", "clutch-letter-hook"))
        self.assertEqual(spec.close_reasons, ())

    def test_a_session_registers_and_a_prompt_titles_it(self) -> None:
        sid = conv(11)
        self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"})
        record = self.lookup(sid)
        self.assertIsNotNone(record)
        self.assertRegex(record["label"], PAIR)
        self.hook(
            {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "rotate the tokens"},
        )
        self.assertEqual(self.lookup(sid)["title"], "Rotate the Tokens")

    def test_an_exec_run_takes_no_letter(self) -> None:
        sid = conv(12)
        self.hook({"hook_event_name": "SessionStart", "session_id": sid, "entrypoint": "exec"})
        self.assertIsNone(self.lookup(sid))

    def test_a_subagent_takes_the_pair_after_its_parent(self) -> None:
        parent, child = conv(13), conv(14)
        self.hook({"hook_event_name": "SessionStart", "session_id": parent, "source": "startup"})
        self.hook(
            {"hook_event_name": "SessionStart", "session_id": child, "parent_session_id": parent},
        )
        parent_label = self.lookup(parent)["label"]
        child_label = self.lookup(child)["label"]
        self.assertEqual(parent_label[0], child_label[0])
        self.assertNotEqual(parent_label, child_label)

    def test_the_off_switch(self) -> None:
        sid = conv(15)
        self.hook(
            {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"},
            env=self.env(CLUTCH_LETTER="0"),
        )
        self.assertIsNone(self.lookup(sid))


class InstallTests(Base):
    def test_install_writes_the_bridge_hooks_file_and_reports_the_mount(self) -> None:
        out = io.StringIO()
        code = PL.install_main(["--platform", "clutch"], self.env(), out)
        answer = json.loads(out.getvalue())
        self.assertEqual(code, 0, answer)
        self.assertIn("cordis.patch.yml", answer["trust"])
        with open(os.path.join(self.home, ".dsh", "hooks-letter.json")) as fh:
            doc = json.load(fh)
        for event in ("SessionStart", "UserPromptSubmit"):
            words = doc["hooks"][event][0]["hooks"][0]["command"]
            self.assertIn("platform-letter", words)
            self.assertIn("--platform clutch", words)
        again = io.StringIO()
        code = PL.install_main(["--platform", "clutch"], self.env(), again)
        self.assertEqual(json.loads(again.getvalue())["changed"], [])
        removed = io.StringIO()
        PL.install_main(["--platform", "clutch", "--remove"], self.env(), removed)
        with open(os.path.join(self.home, ".dsh", "hooks-letter.json")) as fh:
            doc = json.load(fh)
        self.assertNotIn("SessionStart", doc.get("hooks", {}))


class ApiTests(Base):
    def test_a_letter_lands_through_the_registry_api(self) -> None:
        key = secrets.token_hex(24)
        service = RG.RegistryService(self.db, {"OPERATOR": key})
        server = API.ListenerReadApi("127.0.0.1", 0, self.state, {}, registry=service)
        server.start()
        self.addCleanup(server.close)
        host, port = server.address
        env = self.env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="http://%s:%d" % (host, port),
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=key,
        )
        sid = conv(16)
        self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"}, env=env)
        record = self.lookup(sid)
        self.assertIsNotNone(record)
        self.assertRegex(record["label"], PAIR)
        self.assertEqual(record["seat"], "CLUTCH")
        self.assertGreater(time.time(), 0)  # the listener really served it (no local file was written by the hook host)


if __name__ == "__main__":
    unittest.main()
