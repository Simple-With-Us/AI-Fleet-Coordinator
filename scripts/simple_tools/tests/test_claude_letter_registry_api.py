"""The Claude letter registry side's registry-API transport: with the registry API URL configured,
`letter registry` answers from the server registry over the API and never opens a local registry
file, and a chat off the Mac reaches it through its own in-process registry side instead of the SSH
hop.  The API side is a real registry service over a loopback listener backed by its own store; the
hook side's state folder deliberately holds no registry file at all, so every answer below could
only have come through the API — the local path's answer for such a host is the "no Operator
registry on this host" refusal, which the no-URL control test asserts."""

from __future__ import annotations

import io
import json
import os
import secrets
import sqlite3
import subprocess
import tempfile
import time
import unittest

from simple_tools import claude_letter as CL
from simple_tools import listener_api as API
from simple_tools import listener_registry as RG
from simple_tools import operator_registry as OR

PAIR = r"^[A-Z]{2}$"
SID = "c-0001-0000-0000-0000-000000000000"


def no_runner(*args, **kwargs):  # a chat that must never shell out (no SSH hop on the API path)
    raise AssertionError("the registry API path must not run a subprocess: %r" % (args,))


class ApiSide(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.hook_state = os.path.join(self.tmp.name, "hook-state")  # no registry file here, ever
        self.service_state = os.path.join(self.tmp.name, "service-state")
        os.makedirs(self.home)
        self.db = os.path.join(self.service_state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()  # the server registry's own store
        self.operator_key = secrets.token_hex(24)
        self.service = RG.RegistryService(self.db, {"OPERATOR": self.operator_key})
        self.server = API.ListenerReadApi("127.0.0.1", 0, self.service_state, {}, registry=self.service)
        self.server.start()
        self.addCleanup(self.server.close)
        host, port = self.server.address
        self.api_env = {
            "HOME": self.home,
            "SIMPLE_TOOLS_STATE_DIR": self.hook_state,
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL": "http://%s:%d" % (host, port),
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL": "OPERATOR",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": self.operator_key,
        }

    def registry(self, *argv: str, env: dict | None = None) -> tuple[int, dict]:
        out = io.StringIO()
        code = CL.registry_main(list(argv), env=env if env is not None else self.api_env, stdout=out)
        return code, json.loads(out.getvalue())

    def record(self, sid: str = SID) -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup("claude", "mac", "default", sid)
        finally:
            reg.close_db()

    def test_register_lands_in_the_server_registry_and_no_local_file_appears(self) -> None:
        code, answer = self.registry("register", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["outcome"], "registered")
        self.assertRegex(answer["label"], PAIR)
        record = self.record()
        self.assertIsNotNone(record)
        self.assertEqual(record["label"], answer["label"])
        self.assertEqual(record["seat"], "CLAUDE")
        # The hook host still holds no registry file: the API was the store, not a local fallback.
        self.assertFalse(os.path.exists(os.path.join(self.hook_state, "operator", "registry.sqlite3")))

    def test_a_second_register_resumes_the_same_letter(self) -> None:
        _, first = self.registry("register", "--location", "mac", "--session-id", SID)
        code, second = self.registry("register", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, second)
        self.assertEqual(second["outcome"], "resumed")
        self.assertEqual(second["label"], first["label"])

    def test_title_touch_and_close_fold_like_the_local_path(self) -> None:
        self.registry("register", "--location", "mac", "--session-id", SID)
        code, titled = self.registry(
            "title", "--location", "mac", "--session-id", SID, "--title=Registry API Work"
        )
        self.assertEqual(code, 0, titled)
        self.assertTrue(titled["ok"])
        self.assertEqual(self.record()["title"], "Registry API Work")
        code, touched = self.registry("touch", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, touched)
        self.assertTrue(touched["ok"])
        code, closed = self.registry("close", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, closed)
        self.assertTrue(closed["closed"])
        self.assertEqual(self.record()["state"], "closed")
        code, again = self.registry("close", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, again)
        self.assertTrue(again["retired"])  # a closed chat's second close is the local path's retired answer

    def test_a_touch_names_the_letter_the_server_holds_now(self) -> None:
        _, made = self.registry("register", "--location", "mac", "--session-id", SID)
        code, touched = self.registry("touch", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, touched)
        self.assertEqual((touched["label"], touched["generation"]), (made["label"], made["generation"]))
        # The registry moves the letter in place (a relabel, or a restore):  the next touch names the new one.
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE alias_assignments SET letter = 'QX' WHERE retired_at IS NULL")
        conn.close()
        code, moved = self.registry("touch", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, moved)
        self.assertEqual(moved["label"], "QX")

    def test_a_server_chat_retitles_when_the_server_moved_its_letter(self) -> None:
        env = dict(self.api_env, CLAUDE_LETTER_LOCATION="server", CLAUDE_CODE_ENTRYPOINT="cli")
        start = time.time()

        def hook(payload: dict, now: float) -> dict | None:
            out = io.StringIO()
            CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=no_runner, now=now, platform="linux")
            text = out.getvalue().strip()
            return json.loads(text)["hookSpecificOutput"] if text else None

        first = hook({"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}, start)
        old = first["sessionTitle"].split(":")[0]
        hook({"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "fix the build",
              "session_title": first["sessionTitle"]}, start + 1)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE alias_assignments SET letter = 'QX' WHERE retired_at IS NULL")
        conn.close()
        out = hook({"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "more",
                    "session_title": "%s: Fix the Build" % old}, start + CL.TOUCH_SECONDS + 5)
        self.assertEqual(out["sessionTitle"], "QX: Fix the Build")
        self.assertIn("letter changed from %s to QX" % old, out["additionalContext"])

    def test_a_chat_that_comes_back_after_close_reopens(self) -> None:
        _, first = self.registry("register", "--location", "mac", "--session-id", SID)
        self.registry("close", "--location", "mac", "--session-id", SID)
        code, back = self.registry("register", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, back)
        self.assertEqual(back["outcome"], "reopened")
        self.assertRegex(back["label"], PAIR)
        self.assertEqual(self.record()["state"], "active")

    def test_an_unregistered_chat_is_the_no_registration_error(self) -> None:
        code, answer = self.registry("touch", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 5)
        self.assertIn("error", answer)

    def test_without_the_url_the_same_host_still_refuses(self) -> None:
        env = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.hook_state}
        code, answer = self.registry("register", "--location", "mac", "--session-id", SID, env=env)
        self.assertEqual(code, 3)
        self.assertEqual(answer["error"], "no Operator registry on this host")
        self.assertIsNone(self.record())  # nothing was written anywhere

    def test_a_server_chat_calls_the_api_in_process_without_ssh(self) -> None:
        chat = CL.Chat(self.api_env, "CLAUDE", "server", SID, runner=no_runner, now=time.time())
        answer = chat.registry("register")
        self.assertNotIn("error", answer)
        self.assertRegex(answer["label"], PAIR)
        record = OR.SessionRegistry(self.db).lookup("claude", "server", "default", SID)
        self.assertIsNotNone(record)
        self.assertEqual(record["label"], answer["label"])

    def test_a_server_register_with_the_mac_remote_hops_for_the_parent_lookup(self) -> None:
        calls: list = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='{"label": "AB", "outcome": "registered"}\n')

        env = dict(self.api_env, CLAUDE_LETTER_REMOTE="mac-host")
        chat = CL.Chat(env, "CLAUDE", "server", SID, runner=runner, now=time.time())
        answer = chat.registry("register")
        self.assertEqual(answer.get("label"), "AB")
        self.assertEqual(len(calls), 1)
        self.assertIn("mac-host", calls[0])
        # Every other op needs no lineage, so it calls the API in process.
        in_process = CL.Chat(env, "CLAUDE", "server", SID, runner=no_runner, now=time.time())
        self.assertEqual(in_process.registry("touch").get("error"), "this chat has no registration")


if __name__ == "__main__":
    unittest.main()
