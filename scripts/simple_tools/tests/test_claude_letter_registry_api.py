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
from unittest import mock

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
        code, titled = self.registry("title", "--location", "mac", "--session-id", SID, "--title=New words")
        self.assertEqual(code, 0, titled)
        self.assertEqual((titled["label"], titled["record_id"]), ("QX", made["record_id"]))

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

    def test_the_issues_op_reaches_the_server_registry_through_its_update(self) -> None:
        self.registry("register", "--location", "mac", "--session-id", SID)
        issues = [{"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"}]
        code, answer = self.registry(
            "issues", "--location", "mac", "--session-id", SID, "--issues=" + json.dumps(issues)
        )
        self.assertEqual(code, 0, answer)
        self.assertEqual((answer["ok"], answer["issues"]), (True, issues))
        code, touched = self.registry("touch", "--location", "mac", "--session-id", SID)
        self.assertEqual(code, 0, touched)
        found = OR.resolve_label(self.db, "claude", self.record()["label"])
        self.assertEqual(found["issues"], issues)
        # A closed chat's list is not changed:  the answer is the retired one the hook already knows.
        self.registry("close", "--location", "mac", "--session-id", SID)
        code, closed = self.registry("issues", "--location", "mac", "--session-id", SID, "--issues=[]")
        self.assertEqual((code, closed.get("retired")), (0, True))

    # ---- AFC-772:  a Claude chat that arrives under another platform's seat ----

    def test_another_platforms_seat_is_refused_with_the_registry_code_and_nothing_is_stored(self) -> None:
        code, answer = self.registry("register", "--location", "mac", "--session-id", SID, "--seat", "ZC")
        self.assertEqual(code, 5, answer)  # an answer that carries an error, like every other refusal
        self.assertEqual(answer["code"], "seat_platform_mismatch")
        self.assertEqual(answer["error"], "platform claude registers under seat CLAUDE, not ZC")
        self.assertEqual((answer["platform"], answer["seat"], answer["expected"]), ("claude", "ZC", ["CLAUDE"]))
        self.assertIsNone(self.record())
        code, answer = self.registry("register", "--location", "mac", "--session-id", SID)
        self.assertEqual((code, answer["outcome"]), (0, "registered"))  # the right seat is not held up

    def test_a_chat_whose_host_exports_another_seat_carries_on_with_no_letter(self) -> None:
        # What happened on the Mac:  AGENT_SEAT named ZCode's seat, and the Claude hook takes its seat from it.
        env = dict(self.api_env, CLAUDE_CODE_ENTRYPOINT="cli", AGENT_SEAT="ZC")
        start = time.time()

        def hook(payload: dict, now: float) -> dict | None:
            out = io.StringIO()
            code = CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=no_runner, now=now, platform="darwin")
            self.assertEqual(code, 0)  # the hook never blocks a chat
            text = out.getvalue().strip()
            return json.loads(text)["hookSpecificOutput"] if text else None

        out = hook({"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}, start)
        self.assertIn(
            "No Operator letter yet (platform claude registers under seat CLAUDE, not ZC)", out["additionalContext"]
        )
        self.assertIn("tries again", out["additionalContext"])
        self.assertNotIn("sessionTitle", out)
        self.assertIsNone(self.record())
        with open(os.path.join(self.hook_state, "ZC", "letters", SID + ".json"), encoding="utf-8") as fh:
            note = json.load(fh)
        self.assertEqual((note["state"], note.get("label", "")), ("failed", ""))
        # A prompt inside the retry window asks the registry nothing and says nothing;  a later one asks again and
        # gets the same answer, still without stopping the chat.
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "fix the build"}
        self.assertIsNone(hook(prompt, start + 5))
        hook(prompt, start + CL.RETRY_SECONDS + 5)
        self.assertIsNone(self.record())

    def test_a_chat_filed_under_another_seat_earlier_still_resumes_and_ends_its_own_row(self) -> None:
        # The Mac chats of AFC-772 already held a letter under ZC when the registry began to refuse that pair.  A
        # refused resume left the note "failed", SessionEnd closes only an open note, and the stale sweep waits for a
        # newer chat of the same seat, which can no longer register:  the letter stayed live with nothing to end it.
        # So a live row is still resumed under the seat it is stored under, and the chat ends it itself.
        env = dict(self.api_env, CLAUDE_CODE_ENTRYPOINT="cli", AGENT_SEAT="ZC")
        start = time.time()

        def hook(payload: dict, now: float) -> dict | None:
            out = io.StringIO()
            code = CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=no_runner, now=now, platform="darwin")
            self.assertEqual(code, 0)
            text = out.getvalue().strip()
            return json.loads(text)["hookSpecificOutput"] if text else None

        def note() -> dict:
            with open(os.path.join(self.hook_state, "ZC", "letters", SID + ".json"), encoding="utf-8") as fh:
                return json.load(fh)

        with mock.patch.object(OR, "check_seat_platform", lambda *_: None):  # filed before the check existed
            first = hook({"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}, start)
        label = first["sessionTitle"].split(":")[0]
        self.assertEqual((self.record()["seat"], self.record()["state"]), ("ZC", "active"))
        resume = {"hook_event_name": "SessionStart", "session_id": SID, "source": "resume",
                  "session_title": "%s: Fix The Build" % label}
        out = hook(resume, start + 30)
        self.assertNotIn("No Operator letter yet", json.dumps(out))
        self.assertEqual((note()["state"], note()["label"]), ("open", label))
        hook({"hook_event_name": "SessionEnd", "session_id": SID, "reason": "logout"}, start + 60)
        self.assertEqual(self.record()["state"], "closed")
        self.assertEqual(note()["state"], "closed")
        # Ended, the chat is not given a letter under that seat again:  it reads why and carries on.
        out = hook(resume, start + 90)
        self.assertIn(
            "No Operator letter yet (platform claude registers under seat CLAUDE, not ZC)", out["additionalContext"]
        )
        self.assertEqual(self.record()["state"], "closed")

    def test_a_chat_filed_under_another_seat_earlier_gets_its_letter_back_once_its_seat_is_right(self) -> None:
        # The heal (AFC-772):  the host's seat is fixed, the hook registers as CLAUDE, and the registry
        # re-seats the row the chat already held and answers its letter, so the chat keeps AA.
        start = time.time()

        def hook(env: dict, payload: dict, now: float) -> dict | None:
            out = io.StringIO()
            code = CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=no_runner, now=now, platform="darwin")
            self.assertEqual(code, 0)
            text = out.getvalue().strip()
            return json.loads(text)["hookSpecificOutput"] if text else None

        wrong = dict(self.api_env, CLAUDE_CODE_ENTRYPOINT="cli", AGENT_SEAT="ZC")
        with mock.patch.object(OR, "check_seat_platform", lambda *_: None):  # filed before the check existed
            first = hook(wrong, {"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}, start)
        label = first["sessionTitle"].split(":")[0]
        self.assertEqual(self.record()["seat"], "ZC")
        right = dict(self.api_env, CLAUDE_CODE_ENTRYPOINT="cli", AGENT_SEAT="CLAUDE")
        resume = {
            "hook_event_name": "SessionStart",
            "session_id": SID,
            "source": "resume",
            "session_title": "%s: Fix The Build" % label,
        }
        out = hook(right, resume, start + 30)
        self.assertNotIn("No Operator letter yet", json.dumps(out))
        self.assertEqual((self.record()["seat"], self.record()["state"]), ("CLAUDE", "active"))
        self.assertEqual(self.record()["label"], label)

    def test_a_closed_row_filed_under_another_seat_is_reseated_then_the_chat_reopens_it(self) -> None:
        # The registry re-seats a closed row and leaves it closed;  the chat's own reopen (which a seat may
        # ask only for its own row) then brings it back, so the heal needs no hand work.
        start = time.time()

        def hook(env: dict, payload: dict, now: float) -> dict | None:
            out = io.StringIO()
            code = CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=no_runner, now=now, platform="darwin")
            self.assertEqual(code, 0)
            text = out.getvalue().strip()
            return json.loads(text)["hookSpecificOutput"] if text else None

        wrong = dict(self.api_env, CLAUDE_CODE_ENTRYPOINT="cli", AGENT_SEAT="ZC")
        with mock.patch.object(OR, "check_seat_platform", lambda *_: None):
            hook(wrong, {"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}, start)
        registry = OR.SessionRegistry(self.db)
        try:
            self.assertTrue(registry.close(self.record()["record_id"]))
        finally:
            registry.close_db()
        right = dict(self.api_env, CLAUDE_CODE_ENTRYPOINT="cli", AGENT_SEAT="CLAUDE")
        out = hook(right, {"hook_event_name": "SessionStart", "session_id": SID, "source": "resume"}, start + 30)
        self.assertNotIn("No Operator letter yet", json.dumps(out))
        self.assertEqual((self.record()["seat"], self.record()["state"]), ("CLAUDE", "active"))

    # ---- AFC-702:  the end-of-turn report on the API transport ----

    def stop_hook(self, env: dict, transcript: str, now: float) -> str:
        out = io.StringIO()
        payload = {"hook_event_name": "Stop", "session_id": SID, "transcript_path": transcript}
        CL.hook(env, io.StringIO(json.dumps(payload)), out, runner=no_runner, now=now, platform="linux")
        return out.getvalue()

    def server_chat(self) -> tuple[dict, str]:
        env = dict(self.api_env, CLAUDE_LETTER_LOCATION="server", CLAUDE_CODE_ENTRYPOINT="cli")
        out = io.StringIO()
        start = {"hook_event_name": "SessionStart", "session_id": SID, "source": "startup"}
        CL.hook(env, io.StringIO(json.dumps(start)), out, runner=no_runner, now=time.time(), platform="linux")
        folder = os.path.join(self.home, ".claude", "projects", "p")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "t.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "please look at AFC-1"}}) + "\n")
        return env, path

    def test_a_stop_on_the_api_transport_reaches_the_server_registry(self) -> None:
        env, path = self.server_chat()
        self.assertEqual(self.stop_hook(env, path, time.time()), "")
        label = OR.SessionRegistry(self.db).lookup("claude", "server", "default", SID)["label"]
        found = OR.resolve_label(self.db, "claude", label)
        self.assertEqual([i["id"] for i in found.get("issues", [])], ["AFC-1"])

    def test_a_stop_with_the_api_unreachable_is_silent_and_inside_its_budget(self) -> None:
        env, path = self.server_chat()
        self.server.close()  # the registry API is gone:  the one call fails
        began = time.monotonic()
        self.assertEqual(self.stop_hook(env, path, time.time()), "")
        self.assertLess(time.monotonic() - began, CL.HOOK_TIMEOUTS["Stop"])
        note = CL.Chat(env, "CLAUDE", "server", SID, runner=no_runner, now=time.time()).read()
        self.assertIn("issues_failed_at", note)
        self.assertNotIn("issues_sent", note)

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

    # ---- the archive sweep over the API (AFC-694) ----

    def app_record(self, cli: str, archived: bool) -> None:
        from simple_tools import claude_app_lineage as AL

        folder = os.path.join(AL.store_root(self.home), "acct", "org")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "local_%s.json" % cli[:8]), "w", encoding="utf-8") as fh:
            json.dump({"cliSessionId": cli, "isArchived": archived, "title": "Private"}, fh)

    def sweep(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = CL.main(["sweep-archived", *argv], env=self.api_env, stdout=out)
        return code, out.getvalue()

    def test_the_archive_sweep_closes_through_the_api(self) -> None:
        kept = "d-0002-0000-0000-0000-000000000000"
        _, made = self.registry("register", "--location", "mac", "--session-id", SID)
        self.registry("register", "--location", "mac", "--session-id", kept)
        self.app_record(SID, archived=True)
        self.app_record(kept, archived=False)
        code, out = self.sweep("--apply")
        self.assertEqual(code, 0, out)
        self.assertIn("closed %s (%s, mac)" % (made["label"], SID[:8]), out)
        self.assertNotIn("Private", out)
        self.assertEqual(self.record()["state"], "closed")
        self.assertEqual(self.record(kept)["state"], "active")
        self.assertFalse(os.path.exists(os.path.join(self.hook_state, "operator", "registry.sqlite3")))
        code, again = self.sweep("--apply")  # idempotent:  the closed chat is no longer open
        self.assertEqual(code, 0, again)
        self.assertIn("0 archived", again)

    def test_a_dry_run_sends_only_the_history_read(self) -> None:
        from unittest import mock

        from simple_tools import registry_api

        self.registry("register", "--location", "mac", "--session-id", SID)
        self.app_record(SID, archived=True)
        real = registry_api.call
        ops: list = []

        def call(env, op, params=None, **kwargs):
            ops.append(op)
            return real(env, op, params, **kwargs)

        with mock.patch.object(registry_api, "call", side_effect=call):
            code, out = self.sweep()
        self.assertEqual(code, 0, out)
        self.assertIn("would close", out)
        # The API drops other seats' rows after its window is cut, so a short answer is read once more with a
        # wider window to be sure the history ended;  every call is still a read.
        self.assertEqual(set(ops), {"history"})
        self.assertLessEqual(len(ops), 2)
        self.assertEqual(self.record()["state"], "active")


if __name__ == "__main__":
    unittest.main()
