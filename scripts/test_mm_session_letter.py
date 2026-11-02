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

# Synthetic and provider-neutral:  an assigned credential the scanner refuses, with no
# real provider prefix, so it never looks like a leaked key to a repository scanner.
FIXTURE_SECRET = "FIXTURE_TOKEN=" + "NotARealValue" * 2


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
        secret = FIXTURE_SECRET
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

    # --- the letter cache follows the runtime's scopes ---

    def _write_cache(self, directory, session_id, letter):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, session_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"letter": letter, "host": "mac", "space": "double"}, fh)

    def _shell_env(self, tmp):
        """A shell that is not a hook:  the runtime names no scope, and MINIMAX_DATA_DIR
        points at a temporary data directory, so the real one is never touched."""
        return mock.patch.dict(os.environ, {"MINIMAX_HOOKS_DATA": "", "MINIMAX_DATA_DIR": tmp})

    def test_the_letter_cache_reads_the_runtime_user_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope = os.path.join(tmp, "v2", "hook-data", "user", "session-letters")
            self._write_cache(scope, "sid", "BB")
            with self._shell_env(tmp):
                self.assertEqual(M.cached_letter("sid"), "BB")

    def test_the_letter_cache_prefers_the_live_scope_over_the_pre_v2_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = os.path.join(tmp, "v2", "hook-data", "user", "session-letters")
            legacy = os.path.join(tmp, "hook-state", "session-letters")
            self._write_cache(live, "sid", "BB")
            self._write_cache(legacy, "sid", "A")
            with self._shell_env(tmp):
                # The runtime writes the live scope, so the live letter wins over the
                # stale one the pre-v2 layout still holds.
                self.assertEqual(M.cached_letter("sid"), "BB")

    def test_a_project_scope_letter_is_found_from_a_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "v2", "hook-data", "projects", "abc123", "session-letters")
            self._write_cache(project, "sid", "CD")
            with self._shell_env(tmp):
                self.assertEqual(M.cached_letter("sid"), "CD")

    def test_a_letter_written_from_a_shell_lands_in_the_runtime_user_scope(self):
        with tempfile.TemporaryDirectory() as tmp, self._shell_env(tmp):
            M.cache_letter("sid", "BB", "Topic", "double")
            written = os.path.join(
                tmp, "v2", "hook-data", "user", "session-letters", "sid.json"
            )
            self.assertTrue(os.path.isfile(written))
            self.assertEqual(M.cached_letter("sid"), "BB")

    def test_a_letter_stays_in_the_scope_that_already_holds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "v2", "hook-data", "projects", "abc123", "session-letters")
            self._write_cache(project, "sid", "CC")
            with self._shell_env(tmp):
                M.cache_letter("sid", "DD", "Topic", "double")
                # The scope that holds the letter keeps it, so the letter does not split
                # into two copies that drift apart.
                self.assertEqual(M.cached_letter("sid"), "DD")
                user_scope = os.path.join(tmp, "v2", "hook-data", "user", "session-letters")
                self.assertFalse(os.path.isfile(os.path.join(user_scope, "sid.json")))

    def test_a_symlinked_scope_is_refused_rather_than_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            self._write_cache(real, "sid", "BB")
            projects = os.path.join(tmp, "v2", "hook-data", "projects")
            os.makedirs(projects)
            os.symlink(real, os.path.join(projects, "abc123"))
            with self._shell_env(tmp):
                # The same rule the primary cache follows:  a symlinked cache path is
                # refused with its own code, never followed.
                with self.assertRaises(M.RegistryError) as caught:
                    M.cached_letter("sid")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")

    def test_a_symlinked_user_scope_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            self._write_cache(real, "sid", "BB")
            hook_data = os.path.join(tmp, "v2", "hook-data")
            os.makedirs(hook_data)
            os.symlink(real, os.path.join(hook_data, "user"))
            with self._shell_env(tmp):
                with self.assertRaises(M.RegistryError) as caught:
                    M.cached_letter("sid")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")

    def test_a_symlinked_letter_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope = os.path.join(tmp, "v2", "hook-data", "user", "session-letters")
            os.makedirs(scope)
            target = os.path.join(tmp, "target.json")
            with open(target, "w", encoding="utf-8") as fh:
                json.dump({"letter": "BB"}, fh)
            os.symlink(target, os.path.join(scope, "sid.json"))
            with self._shell_env(tmp):
                with self.assertRaises(M.RegistryError) as caught:
                    M.cached_letter("sid")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")

    def test_a_project_scope_makes_this_a_v2_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "hook-state")
            os.makedirs(legacy)
            os.makedirs(os.path.join(tmp, "v2", "hook-data", "projects", "abc123"))
            with self._shell_env(tmp):
                # A host with a project scope is a v2 host even when the user scope
                # is not there yet, so a new letter does not go to the old layout,
                # where no hook ever reads it.
                self.assertEqual(
                    M.letter_cache_dir(),
                    os.path.join(tmp, "v2", "hook-data", "user", "session-letters"),
                )

    def test_a_scope_that_appears_later_is_still_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "v2", "hook-data", "projects")
            os.makedirs(projects)
            scope = os.path.join(projects, "abc123", "session-letters")
            with self._shell_env(tmp):
                self.assertEqual(M.cached_letter("sid"), "")
                self._write_cache(scope, "sid", "CD")
                # The scope list is remembered, but a scope that appears after the
                # listing is still picked up.
                self.assertEqual(M.cached_letter("sid"), "CD")

    def test_the_pre_v2_layout_is_used_when_the_runtime_has_no_v2_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "hook-state")
            os.makedirs(legacy)
            with self._shell_env(tmp):
                self.assertEqual(M.letter_cache_dir(), os.path.join(legacy, "session-letters"))

    def test_the_migration_finds_the_live_letters_from_a_plain_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope = os.path.join(tmp, "v2", "hook-data", "user", "session-letters")
            self._write_cache(scope, "mvs_main", "A")
            self._write_cache(scope, "mvs_child", "A")
            calls = []

            def fake_relabel(session_id, parent="", force=False):
                calls.append((session_id, parent, force))
                return {
                    "label": "BB",
                    "previous": "A",
                    "outcome": "relabelled",
                    "placed_after": "",
                }

            with (
                mock.patch.object(M, "sessions", return_value=self._migration_rows()),
                mock.patch.object(M, "relabel", side_effect=fake_relabel),
                mock.patch.object(M, "cache_letter"),
                mock.patch.object(M, "set_title", return_value=True),
                mock.patch.object(M, "update_title"),
                self._shell_env(tmp),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = M.migrate_pairs()
            self.assertEqual(code, 0)
            # The letters are found in the runtime's scope without a scope in the
            # environment, so the migration moves the sessions instead of skipping them.
            self.assertEqual([call[0] for call in calls], ["mvs_main", "mvs_child"])

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
        secret = FIXTURE_SECRET
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

    # --- the pair migration ---

    def _migration_rows(self):
        return [
            {
                "session_id": "mvs_main",
                "title": "A: Fix the sweep",
                "session_kind": "conversation",
                "created_at_ms": 1,
            },
            {
                "session_id": "mvs_child",
                "title": "explore: look",
                "session_kind": "task",
                "parent_session_id": "mvs_main",
                "created_at_ms": 2,
            },
        ]

    def _run_migration(self, letters, calls):
        def fake_relabel(session_id, parent="", force=False):
            calls.append((session_id, parent, force))
            return {
                "label": "BB",
                "previous": "A",
                "outcome": "relabelled",
                "placed_after": "",
            }

        with (
            mock.patch.object(M, "sessions", return_value=self._migration_rows()),
            mock.patch.object(M, "cached_letter", side_effect=lambda sid: letters[sid]),
            mock.patch.object(M, "relabel", side_effect=fake_relabel),
            mock.patch.object(M, "cache_letter"),
            mock.patch.object(M, "set_title", return_value=True),
            mock.patch.object(M, "update_title"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return M.migrate_pairs()

    def test_the_migration_moves_a_session_that_holds_a_single_letter(self):
        calls = []
        code = self._run_migration({"mvs_main": "A", "mvs_child": "A"}, calls)
        self.assertEqual(code, 0)
        # The main chat goes first, and the child names it as its parent.
        self.assertEqual([call[0] for call in calls], ["mvs_main", "mvs_child"])
        self.assertEqual(calls[1][1], "mvs_main")

    def test_the_migration_leaves_a_session_that_already_holds_a_pair(self):
        calls = []
        code = self._run_migration({"mvs_main": "AA", "mvs_child": "AB"}, calls)
        self.assertEqual(code, 0)
        # The main chat holds a pair and the child holds a pair in its parent's row:  a
        # second run of the migration finds nothing to move.
        self.assertEqual(calls, [])

    def test_the_migration_moves_a_child_that_is_not_in_its_parents_row(self):
        calls = []
        code = self._run_migration({"mvs_main": "AA", "mvs_child": "BB"}, calls)
        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in calls], ["mvs_child"])
        # It already holds a pair, so it has to be placed again rather than moved space.
        self.assertTrue(calls[0][2])

    def test_the_migration_registers_a_session_the_registry_has_never_seen(self):
        calls = []
        registered = []

        def fake_relabel(session_id, parent="", force=False):
            calls.append((session_id, parent, force))
            raise M.RegistryError(
                "session mvs_main is not registered on minimax at mac/default",
                "not_registered",
                "failed",
            )

        def fake_register(session_id, title="", parent_session_id=""):
            registered.append((session_id, title, parent_session_id))
            return {"label": "AA"}

        out = io.StringIO()
        with (
            mock.patch.object(M, "sessions", return_value=self._migration_rows()),
            mock.patch.object(M, "cached_letter", side_effect=lambda sid: "A"),
            mock.patch.object(M, "relabel", side_effect=fake_relabel),
            mock.patch.object(M, "register", side_effect=fake_register),
            mock.patch.object(M, "cache_letter"),
            mock.patch.object(M, "set_title", return_value=True),
            mock.patch.object(M, "update_title"),
            contextlib.redirect_stdout(out),
        ):
            code = M.migrate_pairs()
        self.assertEqual(code, 0)
        # The session the registry never saw is registered, so it gets a pair from the start.
        # The cache is not asked:  it still holds the old single letter.
        self.assertIn('"outcome": "registered"', out.getvalue())
        self.assertIn('"letter": "AA"', out.getvalue())
        self.assertTrue(registered)

    def test_the_letter_command_names_the_parent(self):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = list(args)
            return {"label": "CD"}

        row = {"session_id": "mvs_child", "session_kind": "task", "parent_session_id": "mvs_main"}
        with (
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "registry_call", side_effect=fake),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            M.main(["letter", "--session-id", "mvs_child"])
        self.assertIn("--parent-session-id=mvs_main", seen["args"])

    def test_a_secret_shaped_topic_answers_with_a_stable_code(self):
        secret = FIXTURE_SECRET
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
        # A registry that hung is 124, the status a shell uses for a timeout.
        self.assertEqual(code, 124)
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

    def test_the_session_start_hook_titles_the_thread_with_its_letter(self):
        payload = json.dumps({"session_id": SESSION_ID}).encode()
        row = {"session_id": SESSION_ID, "title": "Fix The Build", "session_kind": "conversation"}
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value=""),
            mock.patch.object(M, "letter_for", return_value="B"),
            mock.patch.object(M, "set_title", return_value=True) as set_title,
            mock.patch.object(M, "update_title", return_value={}) as update_title,
        ):
            self.assertEqual(M.hook_session_start(), 0)
        set_title.assert_called_once_with(SESSION_ID, "B: Fix The Build")
        update_title.assert_called_once_with(SESSION_ID, "B: Fix The Build")

    def test_a_thread_that_already_carries_its_letter_is_not_rewritten(self):
        payload = json.dumps({"session_id": SESSION_ID}).encode()
        row = {
            "session_id": SESSION_ID,
            "title": "B: Fix The Build",
            "session_kind": "conversation",
        }
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value=""),
            mock.patch.object(M, "letter_for", return_value="B"),
            mock.patch.object(M, "set_title", return_value=True) as set_title,
            mock.patch.object(M, "update_title", return_value={}),
        ):
            self.assertEqual(M.hook_session_start(), 0)
        set_title.assert_not_called()

    def test_a_thread_without_a_title_is_left_for_the_stop_path(self):
        payload = json.dumps({"session_id": SESSION_ID}).encode()
        row = {"session_id": SESSION_ID, "title": "", "session_kind": "conversation"}
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value=""),
            mock.patch.object(M, "letter_for", return_value="B"),
            mock.patch.object(M, "set_title", return_value=True) as set_title,
            mock.patch.object(M, "update_title", return_value={}),
        ):
            self.assertEqual(M.hook_session_start(), 0)
        set_title.assert_not_called()

    def test_a_title_with_a_credential_is_never_written_or_printed(self):
        payload = json.dumps({"session_id": SESSION_ID}).encode()
        row = {
            "session_id": SESSION_ID,
            "title": "Deploy with " + FIXTURE_SECRET,
            "session_kind": "conversation",
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value=""),
            mock.patch.object(M, "letter_for", return_value="B"),
            mock.patch.object(M, "set_title", side_effect=ValueError("secret_detected")),
            mock.patch.object(M, "update_title", return_value={}) as update_title,
        ):
            self.assertEqual(M.hook_session_start(), 0)
        update_title.assert_not_called()
        self.assertNotIn(FIXTURE_SECRET, stdout.getvalue())

    def test_the_stop_hook_asks_for_a_real_topic_when_the_title_is_the_placeholder(self):
        payload = json.dumps({"session_id": SESSION_ID}).encode()
        row = {
            "session_id": SESSION_ID,
            "title": M.placeholder_title("B"),
            "session_kind": "conversation",
        }
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value="B"),
        ):
            self.assertEqual(M.hook_stop(), 2)
        self.assertIn("placeholder topic", stderr.getvalue())

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

    def test_parent_for_row_names_the_parent(self):
        self.assertEqual(M.parent_for_row({"parent_session_id": "mvs_parent"}), "mvs_parent")
        self.assertEqual(M.parent_for_row({"session_kind": "task"}), "")
        self.assertEqual(M.parent_for_row({"parent_session_id": "not a session id"}), "")

    # ---- a MiniMax thread is a pair, like every other platform (AFC-586) ----

    def test_a_pair_letter_is_a_valid_prefix(self):
        self.assertTrue(M.first_line_states_letter("AA: hello\nmore", "AA"))
        self.assertTrue(M.title_ok("AA: Topic", "AA")[0])
        self.assertFalse(M.title_ok("A: Topic", "AA")[0])

    def test_wanted_title_drops_the_letter_the_title_already_carries(self):
        # A thread that is re-lettered keeps its topic:  the old prefix goes, the new one comes.
        self.assertEqual(M.wanted_title("AA", "A: Fix the sweep"), "AA: Fix the sweep")
        self.assertEqual(M.wanted_title("AA", "AA: Fix the sweep"), "AA: Fix the sweep")
        self.assertEqual(M.wanted_title("AA", "Fix the sweep"), "AA: Fix the sweep")
        # A letter the thread does not hold is dropped when the thread says which one it was.
        self.assertEqual(M.wanted_title("AA", "A: Fix the sweep", ("A",)), "AA: Fix the sweep")

    def test_a_topic_that_starts_with_an_acronym_keeps_it(self):
        # Two capitals that are not a pair are an acronym, not a letter.
        self.assertEqual(M.wanted_title("AA", "HTTP: the protocol"), "AA: HTTP: the protocol")
        self.assertEqual(M.wanted_title("AA", "AI: the model"), "AA: AI: the model")

    def test_the_registration_asks_for_a_pair_and_names_the_parent(self):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = list(args)
            return {"label": "CD"}

        with mock.patch.object(M, "registry_call", side_effect=fake):
            M.register("sid", "Topic", "mvs_parent")
        self.assertIn("--letter-space=double", seen["args"])
        self.assertIn("--letter-pick=primary", seen["args"])
        self.assertIn("--parent-session-id=mvs_parent", seen["args"])

    def test_a_main_chat_registration_names_no_parent(self):
        seen = {}

        def fake(args, **kwargs):
            seen["args"] = list(args)
            return {"label": "AA"}

        with mock.patch.object(M, "registry_call", side_effect=fake):
            M.register("sid")
        self.assertIn("--letter-space=double", seen["args"])
        self.assertNotIn("--parent-session-id=mvs_parent", seen["args"])
        self.assertFalse([a for a in seen["args"] if a.startswith("--parent-session-id=")])

    def test_a_child_states_its_own_letter(self):
        # A child has a letter of its own, one row after its parent's, so it states that one.
        payload = json.dumps(
            {
                "session_id": SESSION_ID,
                "tool_name": "zulip_post",
                "tool_input": {"body": "AA: hi"},
            }
        ).encode()
        row = {
            "session_id": SESSION_ID,
            "session_kind": "task",
            "parent_session_id": "mvs_parent",
        }
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value="CD"),
        ):
            self.assertEqual(M.hook_pre_tool_use(), 2)

    def test_a_child_states_its_own_letter_and_passes_when_it_does(self):
        payload = json.dumps(
            {
                "session_id": SESSION_ID,
                "tool_name": "zulip_post",
                "tool_input": {"body": "CD: hi"},
            }
        ).encode()
        row = {
            "session_id": SESSION_ID,
            "session_kind": "task",
            "parent_session_id": "mvs_parent",
        }
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value="CD"),
        ):
            self.assertEqual(M.hook_pre_tool_use(), 0)

    def test_a_cron_session_has_no_thread_and_is_not_blocked(self):
        payload = json.dumps(
            {"session_id": SESSION_ID, "tool_name": "zulip_post", "tool_input": {"body": "hi"}}
        ).encode()
        row = {"session_id": SESSION_ID, "session_kind": "cron"}
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(M, "session_row", return_value=row),
        ):
            self.assertEqual(M.hook_pre_tool_use(), 0)

    # ---- the Kody pass of Oct 29 ----

    def test_an_error_field_that_is_empty_is_still_a_failure(self):
        with self.assertRaises(M.RegistryError):
            M._require_registry({"error": "", "label": "B"})

    def test_a_malformed_error_field_is_a_bad_shape_not_a_coerced_message(self):
        for answer in ({"error": 7}, {"error": "x", "code": ["timeout"]}, {"label": 3}):
            with self.assertRaises(M.RegistryError) as caught:
                M._require_registry(answer)
            self.assertEqual(caught.exception.code, "bad_shape")
            self.assertEqual(caught.exception.outcome, "outcome_unknown")

    def test_a_topic_starting_with_a_dash_is_passed_as_one_title_argument(self):
        seen = []
        with (
            mock.patch.object(M, "cached_letter", return_value="B"),
            mock.patch.object(M, "registry_call", side_effect=lambda a: seen.append(a) or {}),
        ):
            M.update_title(SESSION_ID, "B: --seat=XX")
        self.assertIn("--title=--seat=XX", seen[0])
        self.assertNotIn("--title", [a for a in seen[0] if a == "--title"])
        self.assertIn("--session-id=" + SESSION_ID, seen[0])

    def test_an_io_error_after_the_child_started_has_an_unknown_outcome(self):
        proc = _FakeProc()
        proc.communicate = mock.Mock(side_effect=OSError("broken pipe"))
        with (
            mock.patch.object(M, "registry_db", return_value="/tmp/registry.sqlite3"),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(M.subprocess, "Popen", return_value=proc),
            mock.patch.object(M.os, "killpg"),
        ):
            answer = M.registry_call(["touch"])
        self.assertEqual(answer["code"], "io_error")
        self.assertEqual(answer["outcome"], "outcome_unknown")

    def test_a_session_waiting_for_an_english_topic_does_not_fail_the_sweep(self):
        code, out, err = self._run_sweep("", {"count": 0}, title="\u8bb0\u5fc6\u540c\u6b65")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["skipped"], "needs an English topic")
        self.assertIn('"sessions_skipped": 1', err)
        self.assertIn('"sessions_failed": 0', err)

    def test_a_missing_registry_tool_fails_the_sweep_with_127(self):
        self.assertEqual(M._sweep_exit_code([{"code": "timeout"}, {"code": "missing_tool"}]), 127)
        self.assertEqual(M._sweep_exit_code([{"code": "denied"}]), 2)
        self.assertEqual(M._sweep_exit_code([]), 0)

    def test_the_sweep_reports_a_touch_that_did_not_land(self):
        rows = [
            {
                "session_id": SESSION_ID,
                "title": "M: Wake platform verification",
                "session_kind": "conversation",
                "created_at_ms": 0,
            }
        ]
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(M, "sessions", return_value=rows),
            mock.patch.object(M, "cached_letter", return_value="M"),
            mock.patch.object(
                M,
                "touch_session",
                side_effect=M.RegistryError("lost", "timeout", "outcome_unknown"),
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = M.sweep(False)
        self.assertEqual(code, 124)
        self.assertEqual(json.loads(out.getvalue())[0]["outcome"], "outcome_unknown")

    def test_a_hook_keeps_the_cached_letter_when_the_touch_did_not_land(self):
        with (
            mock.patch.object(M, "cached_letter", return_value="M"),
            mock.patch.object(M, "touch_session", side_effect=M.RegistryError("lost", "timeout")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(M.letter_for(SESSION_ID), "M")

    # ---- the PR 1088 handoff fixes the first merge left out ----

    def test_the_raw_text_is_refused_on_bracket_depth_before_it_is_parsed(self):
        levels = M.HOOK_JSON_MAX_DEPTH + 5
        deep = b'{"a":' + b"[" * levels + b"]" * levels + b"}"
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(deep)),
            mock.patch.object(M.json, "loads", side_effect=AssertionError("parsed")) as parse,
        ):
            self.assertEqual(M.read_hook_input(), {})
        parse.assert_not_called()

    def test_brackets_inside_a_string_do_not_count_toward_depth(self):
        quoted = json.dumps({"session_id": SESSION_ID, "note": "[" * 500 + '\\"{{' + "]" * 500})
        self.assertFalse(M._raw_json_too_deep(quoted))
        with mock.patch.object(sys, "stdin", _StdinBytes(quoted.encode())):
            self.assertEqual(M.read_hook_input()["session_id"], SESSION_ID)

    def test_an_unbalanced_close_does_not_hide_a_deep_payload(self):
        self.assertTrue(M._raw_json_too_deep("]]]]" + "[" * (M.HOOK_JSON_MAX_DEPTH + 1)))
        self.assertFalse(M._raw_json_too_deep("[" * M.HOOK_JSON_MAX_DEPTH))

    def _hook_fault(self, hook, payload: dict, error: M.RegistryError):
        err = io.StringIO()
        row = {"session_id": SESSION_ID, "session_kind": "conversation", "title": "M: T"}
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(json.dumps(payload).encode())),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value=""),
            mock.patch.object(M, "letter_for", side_effect=error),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            code = hook()
        return code, err.getvalue()

    def test_every_hook_exits_zero_and_names_the_code_of_a_missing_registry(self):
        payload = {
            "session_id": SESSION_ID,
            "tool_name": "zulip_post",
            "tool_input": {"body": "hi"},
        }
        for hook in (M.hook_session_start, M.hook_pre_tool_use, M.hook_stop):
            code, err = self._hook_fault(hook, payload, M.RegistryError("gone", "missing_tool"))
            self.assertEqual(code, 0, hook.__name__)
            self.assertIn("missing_tool", err, hook.__name__)
            self.assertIn("status 127", err, hook.__name__)

    def test_a_hung_registry_reads_differently_from_a_missing_one(self):
        payload = {"session_id": SESSION_ID}
        code, err = self._hook_fault(
            M.hook_stop, payload, M.RegistryError("slow", "timeout", "outcome_unknown")
        )
        self.assertEqual(code, 0)
        self.assertIn("timeout", err)
        self.assertIn("status 124", err)
        self.assertNotIn("missing_tool", err)

    def test_a_registry_that_is_not_on_this_host_stays_quiet(self):
        payload = {"session_id": SESSION_ID}
        code, err = self._hook_fault(
            M.hook_stop, payload, M.RegistryError("not here", "no_registry")
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_the_sweep_folds_the_registry_ended_rows_into_the_entries(self):
        ended = [
            {
                "record_id": "r1",
                "label": "A",
                "native_session_id": SESSION_ID,
                "reason": "stale and replaced by a newer session",
                "title": "Old",
            },
            {
                "record_id": "r2",
                "label": "B",
                "native_session_id": "mvs_gone",
                "reason": "stale and replaced by a newer session",
            },
        ]
        code, out, err = self._run_sweep("", {"count": 2, "ended": ended}, title="M: Wake check")
        self.assertEqual(code, 0)
        entries = json.loads(out)
        self.assertEqual(entries[0]["session_id"], SESSION_ID)
        self.assertEqual(entries[0]["registry_ended"]["label"], "A")
        self.assertEqual(
            entries[0]["registry_ended"]["reason"], "stale and replaced by a newer session"
        )
        self.assertEqual(entries[1]["session_id"], "mvs_gone")
        self.assertEqual(entries[1]["registry_ended"]["label"], "B")
        self.assertNotIn("title", entries[1]["registry_ended"])
        self.assertIn('"registry_sweep": 2', err)

    def test_a_registry_sweep_without_an_ended_list_still_succeeds(self):
        code, out, _err = self._run_sweep("", {"count": 0}, title="M: Wake check")
        self.assertEqual(code, 0)
        self.assertNotIn("registry_ended", out)

    def test_a_malformed_ended_field_is_a_bad_shape(self):
        for bad in ("A", {"label": "A"}, ["A"], [{"label": 7}], [{"reason": ["x"]}]):
            code, out, err = self._run_sweep("", {"count": 1, "ended": bad})
            self.assertEqual(code, 2, bad)
            self.assertIn("bad_shape", err, bad)
            self.assertIn("outcome_unknown", err, bad)

    def test_a_registry_row_with_no_session_id_makes_no_entry(self):
        ended = [
            {"label": "A", "reason": "r"},
            {"native_session_id": None, "label": "B", "reason": "r"},
            {"native_session_id": "", "label": "C", "reason": "r"},
            {"native_session_id": "mvs_gone", "label": "D", "reason": "r"},
        ]
        code, out, _err = self._run_sweep("", {"count": 4, "ended": ended}, title="M: Wake check")
        self.assertEqual(code, 0)
        self.assertEqual([e["session_id"] for e in json.loads(out)], [SESSION_ID, "mvs_gone"])

    def test_few_brackets_skip_the_scan_and_deep_ones_still_refuse(self):
        plain = '{"a": "' + "x" * 100_000 + '"}'
        self.assertFalse(M._raw_json_too_deep(plain))
        levels = M.HOOK_JSON_MAX_DEPTH + 1
        self.assertTrue(M._raw_json_too_deep("[" * levels + "]" * levels))
        # More brackets than the limit, none of them nested:  the scan runs and finds the depth is fine.
        self.assertFalse(M._raw_json_too_deep("[1]" * (M.HOOK_JSON_MAX_DEPTH * 3)))


if __name__ == "__main__":
    unittest.main()
