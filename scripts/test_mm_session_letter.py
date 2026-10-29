"""Tests for mm_session_letter helpers.  Run:  python3 scripts/test_mm_session_letter.py"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mm_session_letter as M

# Synthetic:  the accepted session-ID shape, never a real native session.
SESSION_ID = "mvs_test0000000000000000000000000001"


class _StdinBytes:
    """A stand-in for sys.stdin whose .buffer is a real byte stream.

    Python 3.15 makes ``sys.stdin.buffer`` a readonly attribute, so the old
    ``mock.patch("sys.stdin.buffer")`` cannot replace it.  Replacing
    sys.stdin itself works on every version.
    """

    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


def _kill_recorder(seen: list):
    """A stand-in for os.killpg that records what it was asked to signal."""

    def record(pid: int, sig: int) -> None:
        seen.append((pid, sig))

    return record


class _FakeProc:
    """A child process that is interrupted while the parent waits for it."""

    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.waited = False
        self.killed = False

    def communicate(self, timeout=None):
        raise KeyboardInterrupt("interrupted")

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        self.killed = True


class MmSessionLetterTest(unittest.TestCase):
    def test_first_line_states_letter(self):
        self.assertTrue(M.first_line_states_letter("B: hello\nmore", "B"))
        self.assertFalse(M.first_line_states_letter("hello\nB: late", "B"))

    def test_registry_topic_from_display(self):
        self.assertEqual(M.registry_topic_from_display("B", "B: Topic"), "Topic")

    def test_wanted_title_english_fallback(self):
        self.assertEqual(M.wanted_title("B", "你好"), "B: Session B")

    def test_cached_letter_rejects_bad_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MINIMAX_HOOKS_DATA"] = tmp
            path = os.path.join(M.letter_cache_dir(), "sid.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"letter": "not-a-letter"}')
            self.assertEqual(M.cached_letter("sid"), "")

    def test_a_title_with_a_credential_is_refused_with_a_stable_code(self):
        secret = "ghp_" + "a1B2c3D4" * 5
        with self.assertRaises(ValueError) as caught:
            M._scan_title("Deploy with " + secret)
        self.assertEqual(str(caught.exception), "secret_detected")
        self.assertNotIn(secret, str(caught.exception))

    def test_the_letter_cache_is_written_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MINIMAX_HOOKS_DATA"] = tmp
            M.cache_letter("sid", "D", "Wake verification", "single")
            path = os.path.join(M.letter_cache_dir(), "sid.json")
            self.assertEqual(M.cached_letter("sid"), "D")
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            leftovers = [n for n in os.listdir(M.letter_cache_dir()) if n.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_a_symlinked_letter_cache_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "hook-state")
            os.symlink("/tmp", link)
            os.environ["MINIMAX_HOOKS_DATA"] = link
            with self.assertRaises(M.RegistryError):
                M.letter_cache_dir()

    def test_require_registry_rejects_an_empty_label(self):
        with self.assertRaises(M.RegistryError):
            M._require_registry({"label": ""}, needs_label=True)

    def test_a_registry_failure_carries_its_code_and_its_outcome(self):
        with self.assertRaises(M.RegistryError) as caught:
            M._require_registry(
                {"error": "no answer", "code": "timeout", "outcome": "outcome_unknown"}
            )
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(caught.exception.outcome, "outcome_unknown")
        self.assertEqual(M._registry_exit_code(caught.exception), 124)

    def test_register_fails_closed_on_a_registry_error(self):
        # A registry that is out of reach is a failure, not a session with no letter.
        with (
            mock.patch.object(
                M,
                "registry_call",
                return_value={"error": "the registry is out of reach", "code": "no_registry"},
            ),
            self.assertRaises(M.RegistryError),
        ):
            M.register("sid")

    def test_touch_fails_closed_on_a_registry_error(self):
        with (
            mock.patch.object(
                M, "registry_call", return_value={"error": "the registry is out of reach"}
            ),
            self.assertRaises(M.RegistryError),
        ):
            M.touch_session("sid")

    def test_an_interrupted_registry_call_stops_the_child(self):
        # The child sits in its own process group, so an interrupted runner has to
        # stop it or it outlives this process.
        proc = _FakeProc()
        killed = []
        with (
            mock.patch.object(M, "registry_db", return_value="/tmp/registry.sqlite3"),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(M.subprocess, "Popen", return_value=proc),
            mock.patch.object(M.os, "killpg", side_effect=_kill_recorder(killed)),
        ):
            with self.assertRaises(KeyboardInterrupt):
                M.registry_call(["register"])
        self.assertTrue(killed, "the child process group was not stopped")
        self.assertEqual(killed[0][1], signal.SIGTERM)
        self.assertTrue(proc.waited)

    # --- the command line answers with the status the failure deserves ------

    def test_the_letter_command_exits_124_on_a_registry_timeout(self):
        out = io.StringIO()
        with (
            mock.patch.object(
                M, "registry_call", return_value={"error": "no answer", "code": "timeout"}
            ),
            contextlib.redirect_stdout(out),
        ):
            code = M.main(["letter", "--session-id", "sid"])
        self.assertEqual(code, 124)

    def test_the_letter_command_exits_127_on_a_missing_registry(self):
        out = io.StringIO()
        with (
            mock.patch.object(
                M, "registry_call", return_value={"error": "gone", "code": "missing_tool"}
            ),
            contextlib.redirect_stdout(out),
        ):
            code = M.main(["letter", "--session-id", "sid"])
        self.assertEqual(code, 127)

    def test_the_letter_command_exits_2_on_any_other_registry_error(self):
        out = io.StringIO()
        with (
            mock.patch.object(
                M, "registry_call", return_value={"error": "denied", "code": "denied"}
            ),
            contextlib.redirect_stdout(out),
        ):
            code = M.main(["letter", "--session-id", "sid"])
        self.assertEqual(code, 2)

    def test_the_rename_command_refuses_a_credential_title_without_a_traceback(self):
        secret = "ghp_" + "a1B2c3D4" * 5
        out = io.StringIO()
        with (
            mock.patch.object(M, "registry_call", return_value={"label": "B"}),
            mock.patch.object(M, "set_title", side_effect=ValueError("secret_detected")),
            contextlib.redirect_stdout(out),
        ):
            code = M.main(["rename", "--session-id", "sid", "--title", secret])
        self.assertEqual(code, 2)
        self.assertIn("secret_detected", out.getvalue())
        self.assertNotIn(secret, out.getvalue())

    def test_read_hook_input_rejects_oversized_bytes(self):
        payload = b"x" * (M.HOOK_INPUT_MAX + 2)
        with mock.patch.object(sys, "stdin", _StdinBytes(payload)):
            self.assertEqual(M.read_hook_input(), {})

    def test_read_hook_input_rejects_deeply_nested_json(self):
        deep = b'{"a":' + b'["x",' * 5000 + b'"y"' + b"]" * 5000 + b"}"
        with mock.patch.object(sys, "stdin", _StdinBytes(deep)):
            self.assertEqual(M.read_hook_input(), {})

    # --- the sweep refuses a topic file whose shape is wrong -----------------

    def _topic_file(self, tmp: str, payload: object) -> str:
        path = os.path.join(tmp, "topics.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    def test_the_sweep_refuses_a_topic_file_with_a_non_string_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._topic_file(tmp, {SESSION_ID: 123})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = M.sweep(False, topics_file=path)
            self.assertEqual(code, 2)
            self.assertIn("cannot read the topic file", err.getvalue())

    def test_the_sweep_refuses_a_topic_file_with_a_bad_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._topic_file(tmp, {"bad key!": "A title"})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = M.sweep(False, topics_file=path)
            self.assertEqual(code, 2)
            self.assertIn("cannot read the topic file", err.getvalue())

    def test_the_sweep_refuses_an_overlong_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._topic_file(tmp, {SESSION_ID: "x" * (M.TITLE_MAX + 1)})
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = M.sweep(False, topics_file=path)
            self.assertEqual(code, 2)
            self.assertIn("cannot read the topic file", err.getvalue())

    # --- a secret in a topic answers with a code, never with the value ------

    def _run_sweep(
        self, topic_file: str, registry_answer: dict, title: str = "Wake platform verification"
    ) -> tuple:
        rows = [
            {
                "session_id": SESSION_ID,
                "title": title,
                "session_kind": "conversation",
                "created_at_ms": 0,
            }
        ]
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(M, "sessions", return_value=rows),
            mock.patch.object(M, "letter_for", return_value="M"),
            mock.patch.object(M, "set_title", return_value=True),
            mock.patch.object(M, "update_title"),
            mock.patch.object(M, "registry_call", return_value=registry_answer),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = M.sweep(True, topics_file=topic_file)
        return code, out.getvalue(), err.getvalue()

    def test_a_secret_shaped_topic_answers_with_a_stable_code(self):
        secret = "ghp_" + "a1B2c3D4" * 5
        with tempfile.TemporaryDirectory() as tmp:
            path = self._topic_file(tmp, {SESSION_ID: secret})
            code, out, err = self._run_sweep(path, {"count": 0})
        self.assertEqual(code, 2)
        self.assertIn("topic_secret_detected", out)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)

    def test_a_credential_title_is_refused_by_the_sweep_without_a_traceback(self):
        # The rename in the sweep scans the title it is about to write, and a
        # refusal has to be an error entry, not a traceback that loses the sweep.
        rows = [
            {
                "session_id": SESSION_ID,
                "title": "Wake platform verification",
                "session_kind": "conversation",
                "created_at_ms": 0,
            }
        ]
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(M, "sessions", return_value=rows),
            mock.patch.object(M, "letter_for", return_value="M"),
            mock.patch.object(M, "set_title", side_effect=ValueError("secret_detected")),
            mock.patch.object(M, "update_title"),
            mock.patch.object(M, "registry_call", return_value={"count": 0}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = M.sweep(True)
        self.assertEqual(code, 2)
        self.assertIn("secret_detected", out.getvalue())

    def test_a_prompt_shaped_title_is_skipped_without_a_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._topic_file(tmp, {"other-session": "A title"})
            code, out, err = self._run_sweep(path, {"count": 0}, title="Reply to Claude in Zulip")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "[]")

    def test_the_topic_file_rescues_a_prompt_shaped_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._topic_file(tmp, {SESSION_ID: "Wake platform verification"})
            code, out, err = self._run_sweep(path, {"count": 0}, title="Reply to Claude in Zulip")
        self.assertEqual(code, 0)
        self.assertIn("M: Wake platform verification", out)

    # --- a registry that is unavailable fails closed, with a distinct status --

    def test_a_registry_sweep_timeout_fails_closed_with_124(self):
        code, out, err = self._run_sweep("", {"error": "no answer", "code": "timeout"})
        self.assertEqual(code, 124)
        self.assertIn("operator registry unavailable", err)
        self.assertIn("outcome_unknown", err)
        self.assertIn("timeout", err)
        self.assertNotIn("no answer", err)
        self.assertIn("Wake platform verification", out)

    def test_a_missing_registry_tool_fails_closed_with_127(self):
        code, out, err = self._run_sweep("", {"error": "gone", "code": "missing_tool"})
        self.assertEqual(code, 127)
        self.assertIn("operator registry unavailable", err)
        self.assertIn("failed", err)

    def test_a_registry_sweep_uses_the_outcome_the_registry_reported(self):
        code, out, err = self._run_sweep(
            "", {"error": "no answer", "code": "denied", "outcome": "outcome_unknown"}
        )
        self.assertEqual(code, 2)
        self.assertIn("outcome_unknown", err)

    def test_any_other_registry_error_fails_closed_with_2(self):
        code, out, err = self._run_sweep("", {"error": "denied", "code": "denied"})
        self.assertEqual(code, 2)
        self.assertIn("operator registry unavailable", err)
        self.assertNotIn("denied", err.replace('"code": "denied"', ""))

    # --- round 5:  partial failures, recursion, cache path, one table read ---

    def test_a_partial_registry_failure_fails_the_sweep(self):
        rows = [
            {
                "session_id": SESSION_ID,
                "title": "M: Wake platform verification",
                "session_kind": "conversation",
                "created_at_ms": 0,
            },
        ]
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(M, "sessions", return_value=rows),
            mock.patch.object(M, "letter_for", return_value="M"),
            mock.patch.object(
                M,
                "update_title",
                side_effect=M.RegistryError("lost", "timeout", "outcome_unknown"),
            ),
            mock.patch.object(M, "registry_call", return_value={"count": 0}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = M.sweep(True)
        self.assertEqual(code, 2)
        entry = json.loads(out.getvalue())[0]
        self.assertEqual(entry["outcome"], "outcome_unknown")
        self.assertEqual(entry["code"], "timeout")
        self.assertIn('"sessions_failed": 1', err.getvalue())
        self.assertNotIn("live", entry)

    def test_a_clean_sweep_still_exits_zero(self):
        code, _out, err = self._run_sweep("", {"count": 0}, title="M: Wake platform verification")
        self.assertEqual(code, 0)
        self.assertNotIn("sessions_failed", err)

    def test_read_hook_input_survives_parser_recursion(self):
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(b'{"a": 1}')),
            mock.patch.object(M.json, "loads", side_effect=RecursionError("deep")),
        ):
            self.assertEqual(M.read_hook_input(), {})

    def test_a_symlinked_letter_cache_is_a_registry_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.mkdir(real)
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            with mock.patch.dict(os.environ, {"MINIMAX_HOOKS_DATA": link}):
                with self.assertRaises(M.RegistryError) as ctx:
                    M.letter_cache_dir()
                self.assertEqual(ctx.exception.code, "unsafe_cache")
                payload = json.dumps(
                    {
                        "session_id": SESSION_ID,
                        "tool_name": "zulip_post",
                        "tool_input": {"body": "hi"},
                    }
                ).encode()
                row = {"session_id": SESSION_ID, "session_kind": "conversation"}
                with (
                    mock.patch.object(sys, "stdin", _StdinBytes(payload)),
                    mock.patch.object(M, "session_row", return_value=row),
                ):
                    self.assertEqual(M.hook_pre_tool_use(), 0)

    def test_hooks_read_the_session_table_once(self):
        payload = json.dumps(
            {"session_id": SESSION_ID, "tool_name": "zulip_post", "tool_input": {"body": "M: hi"}}
        ).encode()
        row = {"session_id": SESSION_ID, "session_kind": "conversation"}
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(M, "session_row", return_value=row) as read,
            mock.patch.object(M, "cached_letter", return_value=""),
            mock.patch.object(M, "letter_for", return_value="M"),
        ):
            self.assertEqual(M.hook_pre_tool_use(), 0)
        self.assertEqual(read.call_count, 1)

    def test_space_for_row_reads_the_given_row(self):
        self.assertEqual(M.space_for_row({"session_kind": "task"}), "double")
        self.assertEqual(M.space_for_row({"parent_session_id": "p"}), "double")
        self.assertEqual(M.space_for_row({"session_kind": "conversation"}), "single")


if __name__ == "__main__":
    unittest.main()
