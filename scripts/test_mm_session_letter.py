"""Tests for mm_session_letter helpers.  Run:  python3 scripts/test_mm_session_letter.py"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mm_session_letter as M

SESSION_ID = "mvs_df2c110b38c7469cb1566174b03447e8"


class _StdinBytes:
    """A stand-in for sys.stdin whose .buffer is a real byte stream.

    Python 3.15 makes ``sys.stdin.buffer`` a readonly attribute, so the old
    ``mock.patch("sys.stdin.buffer")`` cannot replace it.  Replacing
    sys.stdin itself works on every version.
    """

    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


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
            with self.assertRaises(ValueError):
                M.letter_cache_dir()

    def test_require_registry_rejects_an_empty_label(self):
        with self.assertRaises(M.RegistryError):
            M._require_registry({"label": ""}, needs_label=True)

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
        self.assertEqual(code, 0)
        self.assertIn("topic_secret_detected", out)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)

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

    def test_any_other_registry_error_fails_closed_with_2(self):
        code, out, err = self._run_sweep("", {"error": "denied", "code": "denied"})
        self.assertEqual(code, 2)
        self.assertIn("operator registry unavailable", err)
        self.assertNotIn("denied", err.replace('"code": "denied"', ""))


class RegistryTransportTest(unittest.TestCase):
    """The registry transport is the deployment's, not the code's."""

    def setUp(self):
        self.saved = {
            key: os.environ.get(key)
            for key in ("MM_OPERATOR_REGISTRY_COMMAND", "AGENT_SYNC_OPERATOR_REGISTRY")
        }
        for key in self.saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_no_command_means_the_local_cli(self):
        self.assertIsNone(M.registry_command())

    def test_the_host_names_its_own_command(self):
        os.environ["MM_OPERATOR_REGISTRY_COMMAND"] = "/usr/local/bin/mm-registry --remote"
        self.assertEqual(M.registry_command(), ["/usr/local/bin/mm-registry", "--remote"])

    def test_a_blank_command_is_no_command(self):
        os.environ["MM_OPERATOR_REGISTRY_COMMAND"] = "   "
        self.assertIsNone(M.registry_command())

    def test_the_command_answers_without_a_local_registry(self):
        # A host with no copy of the registry file still reaches it, because the
        # command it names owns the transport and the database path.
        with tempfile.TemporaryDirectory() as tmp:
            runner = os.path.join(tmp, "fake-registry")
            with open(runner, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\necho \'{"ok": true}\'\n')
            os.chmod(runner, 0o700)
            os.environ["MM_OPERATOR_REGISTRY_COMMAND"] = runner
            answer = M.registry_call(["history", "--platform", "minimax"])
        self.assertNotEqual(answer.get("code"), "no_registry")
        self.assertNotIn("error", answer)
        self.assertTrue(answer.get("ok"))

    def test_a_missing_command_reports_the_fault(self):
        os.environ["MM_OPERATOR_REGISTRY_COMMAND"] = "/nonexistent/mm-registry"
        answer = M.registry_call(["history", "--platform", "minimax"])
        self.assertEqual(answer.get("code"), "missing_tool")

    def test_no_registry_and_no_command_is_a_fault(self):
        answer = M.registry_call(["history", "--platform", "minimax"])
        if M.registry_db() and M.repo_scripts_dir():
            self.skipTest("this host keeps a local registry copy")
        self.assertEqual(answer.get("code"), "no_registry")


if __name__ == "__main__":
    unittest.main()
