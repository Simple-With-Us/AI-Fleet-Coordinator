"""Cursor on `platform-letter`:  a local chat takes a letter as `additional_context`, a Cloud agent is left to
`cursor_cloud_letters`, a chat's end closes its letter, and the installer writes Cursor's own hooks.json shape next to
the lane guard.  The registry side runs in process against a temporary registry, on the Mac's rules."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import platform_letter_cursor as PC
from simple_tools.tests.registry_fake import FakeRegistry

CID = "7f3a1c52-0b8e-4d7a-9a51-2c4e6f0b9d11"

GUARD_HOOKS = {
    "version": 1,
    "hooks": {"beforeShellExecution": [{"command": "/x/lane-guard-hook --format cursor", "timeout": 5, "failClosed": False}]},
}


def payload(event: str, cid: str = CID, **extra) -> dict:
    base = {"conversation_id": cid, "hook_event_name": event, "model": "gpt-5", "workspace_roots": ["/x/lanes/r"]}
    if event == "sessionStart":
        base.update(is_background_agent=False, composer_mode="agent")
    if event == "beforeSubmitPrompt":
        base.update(generation_id="g-1", prompt="fix the failing login test")
    if event == "sessionEnd":
        base.update(reason="user_close")
    base.update(extra)
    return base


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.home)
        # The one assigner:  the registry API, served in process over a temporary store.
        self.assigner = FakeRegistry(os.path.join(self.tmp.name, "assigner"))
        if getattr(self, "FAKE", True):
            self.assigner.install()
            self.addCleanup(self.assigner.uninstall)
        self.db = self.assigner.db
        self.calls: list[list[str]] = []

    def env(self) -> dict[str, str]:
        return {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.state, **(self.assigner.env() if getattr(self, "FAKE", True) else {}), "SESSION_LETTER_LOCATION": "mac"}

    def transport(self, argv: list[str]) -> dict:
        self.calls.append(list(argv))
        out = io.StringIO()
        PL.registry_main(list(argv), env=self.env(), stdout=out)
        return json.loads(out.getvalue())

    def hook(self, data: dict) -> dict:
        out = io.StringIO()
        self.assertEqual(PC.hook_main(self.env(), io.StringIO(json.dumps(data)), out, transport=self.transport), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)  # Cursor reads one object from every hook
        return json.loads(lines[0])

    def lookup(self) -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("cursor", "mac", PL.SCOPE, CID)
        finally:
            reg.close_db()


class HookTests(Base):
    def test_a_local_chat_is_told_its_letter(self) -> None:
        answer = self.hook(payload("sessionStart"))
        label = self.lookup()["label"]
        self.assertRegex(label, r"^[A-Z]{2}$")
        self.assertEqual(list(answer), ["additional_context"])
        self.assertIn('"@**Cursor** %s"' % label, answer["additional_context"])

    def test_a_prompt_keeps_the_letter_and_never_blocks(self) -> None:
        self.hook(payload("sessionStart"))
        label = self.lookup()["label"]
        answer = self.hook(payload("beforeSubmitPrompt"))
        self.assertNotIn("continue", answer)
        self.assertEqual(self.lookup()["label"], label)

    def test_a_cloud_agent_is_left_to_the_cloud_sweep(self) -> None:
        self.assertEqual(self.hook(payload("sessionStart", is_background_agent=True)), {})
        # beforeSubmitPrompt carries no is_background_agent:  the mark from sessionStart keeps it the sweep's
        self.assertEqual(self.hook(payload("beforeSubmitPrompt")), {})
        self.assertIsNone(self.lookup())
        self.assertEqual(self.calls, [])

    def test_an_end_closes_nothing_so_a_resumed_chat_keeps_its_letter(self) -> None:
        self.hook(payload("sessionStart"))
        label = self.lookup()["label"]
        self.assertEqual(self.hook(payload("sessionEnd")), {})
        self.assertNotIn("close", [c[0] for c in self.calls])
        self.hook(payload("beforeSubmitPrompt"))  # a CLI `--resume` sends no sessionStart
        self.assertEqual(self.lookup()["label"], label)

    def test_a_chat_first_seen_at_a_prompt_takes_a_letter(self) -> None:
        answer = self.hook(payload("beforeSubmitPrompt"))
        label = self.lookup()["label"]
        self.assertIn('"@**Cursor** %s"' % label, answer["additional_context"])

    def test_a_broken_payload_answers_an_empty_object(self) -> None:
        out = io.StringIO()
        PC.hook_main(self.env(), io.StringIO("[1, 2"), out, transport=self.transport)
        self.assertEqual(json.loads(out.getvalue()), {})


class TitleTests(Base):
    """Cursor keeps a local chat's title in `~/.cursor/acp-sessions/<id>/meta.json`; the adapter reads it and
    puts the letter on it as soon as the registry answers, so the chat is not left titled without its pair."""

    def store(self, cid: str = CID) -> str:
        return os.path.join(self.home, ".cursor", "acp-sessions", cid, "meta.json")

    def write_store(self, title: str, cid: str = CID, cwd: str = "/x/lanes/r") -> None:
        path = self.store(cid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"title": title, "cwd": cwd}, fh)

    def read_store(self, cid: str = CID) -> dict:
        with open(self.store(cid)) as fh:
            return json.load(fh)

    def test_a_start_puts_its_letter_on_the_local_store_title(self) -> None:
        self.write_store("Fix the failing login test")
        self.hook(payload("sessionStart"))
        label = self.lookup()["label"]
        doc = self.read_store()
        self.assertEqual(doc["title"], "%s: Fix the failing login test" % label)
        self.assertEqual(doc["cwd"], "/x/lanes/r")  # every other field is kept

    def test_a_chat_whose_store_appears_after_start_is_titled_at_its_prompt(self) -> None:
        self.hook(payload("sessionStart"))  # no record yet:  the write is pending
        label = self.lookup()["label"]
        self.assertFalse(os.path.exists(self.store()))
        self.write_store("New work")
        self.hook(payload("beforeSubmitPrompt"))
        self.assertEqual(self.read_store()["title"], "%s: New work" % label)

    def test_a_title_the_app_replaced_gets_its_letter_back(self) -> None:
        self.write_store("First words")
        self.hook(payload("sessionStart"))
        label = self.lookup()["label"]
        self.write_store("App generated title")
        self.hook(payload("beforeSubmitPrompt"))
        self.assertEqual(self.read_store()["title"], "%s: App generated title" % label)

    def test_a_store_title_that_already_carries_the_letter_is_kept(self) -> None:
        self.write_store("Placeholder")
        self.hook(payload("sessionStart"))
        label = self.lookup()["label"]
        self.write_store("%s: Already set" % label)
        self.hook(payload("beforeSubmitPrompt"))
        self.assertEqual(self.read_store()["title"], "%s: Already set" % label)

    def test_a_failed_write_leaves_no_staging_file(self) -> None:
        self.write_store("First words")
        before = set(os.listdir(os.path.dirname(self.store())))
        request = PL.TitleRequest(
            platform="cursor", session_id=CID, label="AB", title="AB: Words", words="Words",
            current="First words", reason="start", data={}, env=self.env(),
            record_thread_link=lambda link: {}, shown="", held=[],
        )
        with mock.patch.object(PC.os, "replace", side_effect=OSError("no replace")):
            self.assertFalse(PC.cursor_rename(request))
        self.assertEqual(set(os.listdir(os.path.dirname(self.store()))), before)


class InstallTests(Base):
    def test_it_installs_next_to_the_lane_guard_and_removes_cleanly(self) -> None:
        path = os.path.join(self.home, PC.HOOKS_FILE)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            json.dump(GUARD_HOOKS, fh)
        opts = PL.InstallOptions("cursor", "mac")
        self.assertEqual(PL.install_json_hooks(PC.INSTALL, opts, self.env(), io.StringIO()), 0)
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["hooks"]["beforeShellExecution"], GUARD_HOOKS["hooks"]["beforeShellExecution"])
        for event in PC.HOOK_TIMEOUTS:
            (only,) = doc["hooks"][event]
            self.assertIn("--platform cursor", only["command"])
            self.assertIs(only["failClosed"], False)
        remove = PL.InstallOptions("cursor", "mac", remove=True)
        PL.install_json_hooks(PC.INSTALL, remove, self.env(), io.StringIO())
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["hooks"]["beforeShellExecution"], GUARD_HOOKS["hooks"]["beforeShellExecution"])
        self.assertFalse(any(doc["hooks"].get(e) for e in PC.HOOK_TIMEOUTS))


if __name__ == "__main__":
    unittest.main()
