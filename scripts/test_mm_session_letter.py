"""Tests for mm_session_letter helpers.  Run:  python3 scripts/test_mm_session_letter.py"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import pathlib
import signal
import sys
import tempfile
import unittest
from pathlib import Path
import urllib.error
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
            written = os.path.join(tmp, "v2", "hook-data", "user", "session-letters", "sid.json")
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
                # The scopes are not remembered:  a scope that appears after the
                # first listing is picked up straight away, with no tick to wait for.
                self.assertEqual(M.cached_letter("sid"), "CD")

    def test_a_write_never_lands_in_the_pre_v2_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = os.path.join(tmp, "hook-state", "session-letters")
            self._write_cache(legacy, "sid", "A")
            # The host has a v2 scope, so the pre-v2 directory is a read fallback
            # and never the place a letter is written.
            os.makedirs(os.path.join(tmp, "v2", "hook-data", "user"))
            with self._shell_env(tmp):
                directories = M.letter_cache_dirs()
                self.assertIn(legacy, directories)
                M.cache_letter("sid", "BB", "Topic", "double", directories)
                # The pre-v2 directory is a read fallback and never a write target, so
                # a letter does not stay in a place no hook reads.
                with open(os.path.join(legacy, "sid.json"), encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh)["letter"], "A")
                written = os.path.join(
                    tmp, "v2", "hook-data", "user", "session-letters", "sid.json"
                )
                self.assertTrue(os.path.isfile(written))

    def test_the_migration_reports_a_refused_scope_instead_of_aborting(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "v2", "hook-data", "projects")
            os.makedirs(projects)
            real = os.path.join(tmp, "real")
            os.makedirs(real)
            os.symlink(real, os.path.join(projects, "abc123"))
            out = io.StringIO()
            with (
                mock.patch.object(M, "sessions", return_value=self._migration_rows()),
                mock.patch.object(
                    M, "relabel", side_effect=AssertionError("no session may be moved")
                ),
                mock.patch.object(M, "set_title", return_value=True),
                mock.patch.object(M, "update_title"),
                self._shell_env(tmp),
                contextlib.redirect_stdout(out),
            ):
                code = M.migrate_pairs()
            # The refusal is reported per session, and the run does not abort with a
            # traceback and no entries.
            self.assertNotEqual(code, 0)
            self.assertIn("symlink", out.getvalue())
            self.assertIn("mvs_main", out.getvalue())

    def test_the_sweep_passes_the_enumerated_scopes_to_every_title_write(self):
        seen = []

        def fake_update_title(session_id, title, directories=None):
            seen.append((session_id, directories))
            return {}

        row = {
            "session_id": "mvs_sweep",
            "title": "BB: Topic",
            "session_kind": "conversation",
        }
        with (
            mock.patch.object(M, "sessions", return_value=[row]),
            mock.patch.object(M, "letter_for", return_value="BB"),
            mock.patch.object(M, "update_title", side_effect=fake_update_title),
            self._shell_env(tempfile.gettempdir()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            M.sweep(True)
        # The already_ok branch writes the title too, and it passes the scopes on, so
        # a sweep does not re-list them for every session.
        self.assertTrue(seen)
        for session_id, directories in seen:
            self.assertEqual(session_id, "mvs_sweep")
            self.assertIsNotNone(directories)

    def test_a_scope_created_after_the_snapshot_is_still_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = os.path.join(tmp, "v2", "hook-data", "projects")
            os.makedirs(projects)
            scope = os.path.join(projects, "abc123", "session-letters")
            with self._shell_env(tmp):
                snapshot = M.letter_cache_dirs()
                self.assertEqual(M.cached_letter("sid", snapshot), "")
                self._write_cache(scope, "sid", "CD")
                # A miss re-enumerates once, so a scope created while the run was
                # going is found.
                self.assertEqual(M.cached_letter("sid", snapshot), "CD")

    def test_a_hook_lookup_does_not_list_the_scopes_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            scope = os.path.join(tmp, "v2", "hook-data", "user", "session-letters")
            self._write_cache(scope, "sid", "BB")
            with self._shell_env(tmp):
                calls = []
                real = os.listdir

                def counting_listdir(path):
                    calls.append(str(path))
                    return real(path)

                with mock.patch.object(os, "listdir", side_effect=counting_listdir):
                    self.assertEqual(M.cached_letter("sid"), "BB")
                    self.assertEqual(M.cached_letter("sid"), "BB")
                # The scopes are listed once and the listing is remembered, so a hook
                # that finds its letter pays a stat and not a listing.
                listings = [c for c in calls if c.endswith("projects")]
                self.assertLessEqual(len(listings), 1)

    def test_a_caller_can_pass_the_scopes_it_enumerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = os.path.join(tmp, "elsewhere")
            self._write_cache(other, "sid", "CD")
            with self._shell_env(tmp):
                # The sweep and the migration enumerate the scopes once and pass the
                # list on, so the listing is not repeated for every session.
                self.assertEqual(M.cached_letter("sid", [other]), "CD")
                self.assertEqual(M.cached_letter("sid"), "")

    def test_a_symlinked_legacy_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            self._write_cache(real, "sid", "BB")
            os.symlink(real, os.path.join(tmp, "hook-state"))
            with self._shell_env(tmp):
                # The pre-v2 directory is the last place a letter is read from, and a
                # link one level up is refused there too.
                with self.assertRaises(M.RegistryError) as caught:
                    M.cached_letter("sid")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")

    def test_a_symlinked_ancestor_of_the_scope_root_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            self._write_cache(os.path.join(real, "user", "session-letters"), "sid", "BB")
            os.makedirs(os.path.join(tmp, "v2"))
            os.symlink(real, os.path.join(tmp, "v2", "hook-data"))
            with self._shell_env(tmp):
                with self.assertRaises(M.RegistryError) as caught:
                    M.cached_letter("sid")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")

    def test_a_symlinked_override_root_is_refused_on_the_read_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            self._write_cache(os.path.join(real, "session-letters"), "sid", "BB")
            scope = os.path.join(tmp, "scope")
            os.symlink(real, scope)
            with mock.patch.dict(
                os.environ, {"MINIMAX_HOOKS_DATA": scope, "MINIMAX_DATA_DIR": tmp}
            ):
                # The scope the runtime names is refused on the read path too:  the
                # write path already refused that base.
                with self.assertRaises(M.RegistryError) as caught:
                    M.cached_letter("sid")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")

    def test_a_symlinked_user_scope_leaf_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            self._write_cache(real, "sid", "BB")
            user = os.path.join(tmp, "v2", "hook-data", "user")
            os.makedirs(user)
            os.symlink(real, os.path.join(user, "session-letters"))
            with self._shell_env(tmp):
                with self.assertRaises(M.RegistryError) as caught:
                    M.cached_letter("sid")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")

    def test_a_write_into_a_symlinked_scope_leaf_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(real)
            user = os.path.join(tmp, "v2", "hook-data", "user")
            os.makedirs(user)
            os.symlink(real, os.path.join(user, "session-letters"))
            with self._shell_env(tmp):
                # The letter must not land in the link's target.
                with self.assertRaises(M.RegistryError) as caught:
                    M.cache_letter("sid", "BB", "Topic", "double")
                self.assertEqual(getattr(caught.exception, "code", ""), "unsafe_cache")
                self.assertEqual(os.listdir(real), [])

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

    def test_a_remote_registry_call_forwards_the_caller_timeout_capped(self):
        # Both remote branches forward the caller's budget, capped at REMOTE_TIMEOUT:
        # a custom deadline is never silently dropped back to the default.
        for transport_env, local_db in (("registry-hop", "/tmp/registry.sqlite3"), ("", "")):
            for caller_timeout, expected in ((3, 3), (25, M.REMOTE_TIMEOUT)):
                seen = {}

                def fake_remote(args, transport="", timeout=M.REMOTE_TIMEOUT):
                    seen["transport"] = transport
                    seen["timeout"] = timeout
                    return {"ok": True}

                env = {M.ENV_TRANSPORT: transport_env} if transport_env else {}
                with (
                    mock.patch.dict(M.os.environ, env, clear=False),
                    mock.patch.object(M, "registry_db", return_value=local_db),
                    mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
                    mock.patch.object(M, "remote_registry_call", side_effect=fake_remote),
                ):
                    if not transport_env:
                        M.os.environ.pop(M.ENV_TRANSPORT, None)
                    self.assertEqual(
                        M.registry_call(["pending"], timeout=caller_timeout), {"ok": True}
                    )
                self.assertEqual(seen["timeout"], expected, (transport_env, caller_timeout))
        self.assertEqual(seen["transport"], "")

    def test_an_explicit_transport_wins_and_carries_its_name(self):
        seen = {}

        def fake_remote(args, transport="", timeout=M.REMOTE_TIMEOUT):
            seen["transport"] = transport
            return {"ok": True}

        with (
            mock.patch.dict(M.os.environ, {M.ENV_TRANSPORT: "registry-hop"}, clear=False),
            mock.patch.object(M, "registry_db", return_value="/tmp/registry.sqlite3"),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(M, "remote_registry_call", side_effect=fake_remote),
        ):
            M.registry_call(["pending"])
        self.assertEqual(seen["transport"], "registry-hop")

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
            mock.patch.object(
                M, "cached_letter", side_effect=lambda sid, directories=None: letters[sid]
            ),
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
            mock.patch.object(M, "cached_letter", side_effect=lambda sid, directories=None: "A"),
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

    def test_a_registry_fault_on_the_title_update_does_not_block_the_stop(self):
        # A registry that cannot be reached is not the agent's fault, and a Stop hook
        # that exits 2 keeps the agent in a loop while the far host is asleep.
        payload = json.dumps({"session_id": SESSION_ID}).encode()
        row = {"session_id": SESSION_ID, "title": "B: A real topic", "session_kind": "conversation"}
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value="B"),
            mock.patch.object(
                M,
                "update_title",
                side_effect=M.RegistryError("the registry host did not answer", "timeout"),
            ),
        ):
            self.assertEqual(M.hook_stop(), 0)
        self.assertIn("timeout", stderr.getvalue())

    def test_a_registry_fault_after_the_rename_does_not_block_the_stop(self):
        # The thread carries the title in the store;  the registry copy is a second
        # copy, and the sweep carries it later.
        payload = json.dumps({"session_id": SESSION_ID, "stop_hook_active": True}).encode()
        row = {
            "session_id": SESSION_ID,
            "title": "no letter prefix",
            "session_kind": "conversation",
        }
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value="B"),
            mock.patch.object(M, "held_letters", return_value=()),
            mock.patch.object(M, "set_title", return_value=True),
            mock.patch.object(
                M,
                "update_title",
                side_effect=M.RegistryError("the registry host did not answer", "timeout"),
            ),
        ):
            self.assertEqual(M.hook_stop(), 0)
        self.assertIn("renamed", stderr.getvalue())

    def test_a_title_that_carries_a_credential_still_blocks_the_stop(self):
        # A bad title is the agent's to fix, so that one still says so.
        payload = json.dumps({"session_id": SESSION_ID}).encode()
        row = {"session_id": SESSION_ID, "title": "B: A real topic", "session_kind": "conversation"}
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(payload)),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(M, "session_row", return_value=row),
            mock.patch.object(M, "cached_letter", return_value="B"),
            mock.patch.object(M, "update_title", side_effect=ValueError("secret_detected")),
        ):
            self.assertEqual(M.hook_stop(), 2)
        self.assertIn("secret_detected", stderr.getvalue())

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


class ScriptsDirTest(unittest.TestCase):
    """The helper and the CLI it calls have to be the same version."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = os.path.dirname(os.path.realpath(M.__file__))

    def _checkout(self, root):
        # A competing checkout that LOOKS like the fleet scripts:  the search accepts
        # it, so only the order keeps it from winning.
        scripts = os.path.join(root, "AI-Fleet-Coordinator", "scripts")
        os.makedirs(os.path.join(scripts, "simple_tools"))
        open(os.path.join(scripts, "simple_tools", "operator_registry.py"), "w").close()
        return scripts

    def test_the_checkout_the_helper_runs_in_comes_first(self):
        # A verb the helper sends has to be a verb the CLI knows, so the CLI comes
        # from the checkout the helper itself is in, never from a stale checkout the
        # environment happens to name.
        stale = self._checkout(self.tmp.name)
        with mock.patch.dict(os.environ, {"FLEET_CODE_ROOT": self.tmp.name, "FLEET_APPS_ROOT": ""}):
            self.assertEqual(M.repo_scripts_dir(), self.here)
        # The competing root is a real candidate:  without the running checkout in
        # front of it, it is what the search returns.
        self.assertNotEqual(stale, self.here)

    def test_a_stale_checkout_in_the_home_layout_loses_too(self):
        root = os.path.join(self.tmp.name, "home")
        stale = self._checkout(os.path.join(root, "Code"))
        with mock.patch.dict(
            os.environ,
            {"FLEET_CODE_ROOT": "", "FLEET_APPS_ROOT": "", "HOME": root},
        ):
            self.assertEqual(M.repo_scripts_dir(), self.here)
        self.assertNotEqual(stale, self.here)

    def test_a_host_that_names_no_checkout_still_finds_one(self):
        with mock.patch.dict(
            os.environ,
            {"FLEET_CODE_ROOT": "", "FLEET_APPS_ROOT": "", "HOME": self.tmp.name},
        ):
            self.assertEqual(M.repo_scripts_dir(), self.here)


class MmLetterReopenTest(unittest.TestCase):
    """A session the registry swept while its chat was live gets its letter back.

    The sweep ends a session's letter when the session has not been seen for the stale
    window and another session for the same seat has taken over since.  The MiniMax
    path has to reopen the session when the chat comes back to life:  without it the
    registry keeps no letter for a live chat, so a reply addressed to this seat by its
    pair has nothing to resolve to, and the next chat can take the same pair a second
    time.

    These tests use the real registry and the real registry CLI against a temporary
    registry file, so the whole path is exercised rather than a stand-in for it.
    """

    # Synthetic:  the accepted session-ID shape, never a real native session.
    OTHER_SESSION_ID = "mvs_test0000000000000000000000000002"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        self.scripts = os.path.dirname(os.path.abspath(M.__file__))
        self.scope = os.path.join(self.tmp.name, "v2", "hook-data", "user", "session-letters")
        # The script never brings a registry into being, so the file is created here.
        from simple_tools import operator_registry as R

        R.SessionRegistry(self.db).close_db()

    def _env(self):
        return mock.patch.dict(
            os.environ,
            {
                "SIMPLE_TOOLS_OPERATOR_REGISTRY": self.db,
                "MINIMAX_HOOKS_DATA": "",
                "MINIMAX_DATA_DIR": self.tmp.name,
            },
        )

    def _registry(self):
        from simple_tools import operator_registry as R

        reg = R.SessionRegistry(self.db)
        self.addCleanup(reg.close_db)
        return reg

    def _write_cache(self, session_id, letter, topic=""):
        os.makedirs(self.scope, exist_ok=True)
        with open(os.path.join(self.scope, session_id + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"letter": letter, "topic": topic, "host": "mac", "space": "double"}, fh)

    def _register(self, session_id):
        with self._env(), mock.patch.object(M, "repo_scripts_dir", return_value=self.scripts):
            return M.register(session_id, "AA: Swept thread", "")

    def _letter_for(self, session_id):
        with self._env(), mock.patch.object(M, "repo_scripts_dir", return_value=self.scripts):
            return M.letter_for(session_id, "AA: Swept thread", "")

    def test_a_swept_session_that_comes_back_to_life_gets_its_letter_back(self):
        first = self._register(SESSION_ID)
        self.assertEqual(first["label"], "AA")
        reg = self._registry()
        reg.close(first["record_id"], actor="MM", source="the test", reason="stale")
        self.assertIsNotNone(reg._get(first["record_id"])["closed_at"])
        # The cache still holds the letter the live chat had.
        self._write_cache(SESSION_ID, "AA")
        self.assertEqual(self._letter_for(SESSION_ID), "AA")
        # The registry holds it again, so a reply addressed to the pair resolves.
        self.assertIsNone(reg._get(first["record_id"])["closed_at"])

    def test_a_reopened_session_holds_a_pair_again_when_its_letter_went_to_another_chat(self):
        first = self._register(SESSION_ID)
        reg = self._registry()
        reg.close(first["record_id"], actor="MM", source="the test", reason="stale")
        # Another chat takes AA while the first one is away.
        second = self._register(self.OTHER_SESSION_ID)
        self.assertEqual(second["label"], "AA")
        self._write_cache(SESSION_ID, "AA")
        letter = self._letter_for(SESSION_ID)
        # The chat comes back with a pair, never with a single letter.
        self.assertEqual(len(letter), 2)
        self.assertNotEqual(letter, "AA")
        # The cache is this seat's own copy of the letter, so it follows what the
        # registry now says.
        with self._env():
            self.assertEqual(M.cached_letter(SESSION_ID), letter)

    def test_the_reopened_letter_keeps_the_topic_the_cache_held(self):
        first = self._register(SESSION_ID)
        reg = self._registry()
        reg.close(first["record_id"], actor="MM", source="the test", reason="stale")
        # Another chat takes AA while the first one is away.
        second = self._register(self.OTHER_SESSION_ID)
        self.assertEqual(second["label"], "AA")
        self._write_cache(SESSION_ID, "AA", "The topic this thread holds")
        letter = self._letter_for(SESSION_ID)
        self.assertNotEqual(letter, "AA")
        with self._env():
            self.assertEqual(M.cached_topic(SESSION_ID), "The topic this thread holds")
            self.assertEqual(M.cached_letter(SESSION_ID), letter)

    def test_a_cache_write_that_fails_with_an_oserror_still_returns_the_letter(self):
        err = io.StringIO()
        with (
            mock.patch.object(
                M, "touch_session", return_value={"record_id": "r1", "outcome": "closed"}
            ),
            mock.patch.object(M, "reopen_closed", return_value={"label": "BB"}),
            mock.patch.object(M, "cached_letter", return_value="AA"),
            mock.patch.object(M, "cached_topic", return_value="A topic"),
            mock.patch.object(M, "cache_letter", side_effect=OSError("the disk is full")),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(M.letter_for(SESSION_ID, "AA: Swept thread", ""), "BB")
        self.assertIn("Could not cache the reopened letter", err.getvalue())

    def test_a_reopen_that_cannot_be_done_keeps_the_cached_letter(self):
        err = io.StringIO()
        with (
            mock.patch.object(
                M, "touch_session", return_value={"record_id": "r1", "outcome": "closed"}
            ),
            mock.patch.object(
                M,
                "reopen_closed",
                side_effect=M.RegistryError("no letter", "space_lost", "reopen_refused"),
            ),
            mock.patch.object(M, "cached_letter", return_value="AA"),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(M.letter_for(SESSION_ID, "AA: Swept thread", ""), "AA")
        self.assertIn("Could not reopen", err.getvalue())

    def test_the_letter_command_reopens_a_closed_session(self):
        first = self._register(SESSION_ID)
        reg = self._registry()
        reg.close(first["record_id"], actor="MM", source="the test", reason="stale")
        out = io.StringIO()
        with (
            self._env(),
            mock.patch.object(M, "repo_scripts_dir", return_value=self.scripts),
            mock.patch.object(M, "sessions", return_value=[]),
            contextlib.redirect_stdout(out),
        ):
            code = M.main(["letter", "--session-id", SESSION_ID])
        self.assertEqual(code, 0)
        answer = json.loads(out.getvalue())
        self.assertEqual(answer["label"], "AA")
        self.assertIsNone(reg._get(first["record_id"])["closed_at"])


class MmReLetterTitleTest(unittest.TestCase):
    """A thread that is re-lettered keeps one prefix, and a bare topic keeps its acronym.

    A letter on this platform is a pair that need not be doubled:  a child of CC holds
    CD.  A title starts with the letter the thread holds, so the first two capitals of a
    title are that letter.  A bare topic is not a title, so an acronym at the start of one
    is left alone.
    """

    def test_a_title_keeps_one_prefix_when_the_letter_changes(self):
        # The session held CD and now holds CE, so the prefix of its title is its old
        # letter and it goes before the new one.
        held = ("CE", "CD")
        self.assertEqual(M.title_topic("CD: Topic", "CE", held), "Topic")
        self.assertEqual(M.wanted_title("CE", M.title_topic("CD: Topic", "CE", held)), "CE: Topic")

    def test_a_doubled_pair_title_keeps_one_prefix_too(self):
        held = ("CC", "AA")
        self.assertEqual(M.title_topic("AA: Topic", "CC", held), "Topic")
        self.assertEqual(M.wanted_title("CC", M.title_topic("AA: Topic", "CC", held)), "CC: Topic")

    def test_a_title_whose_prefix_this_session_never_held_keeps_its_acronym(self):
        # The session held only AA, so "AI:" is a topic that begins with an acronym and
        # not a letter of this thread.
        self.assertEqual(M.title_topic("AI: the model", "AA", ("AA",)), "AI: the model")
        self.assertEqual(
            M.wanted_title("AA", M.title_topic("AI: the model", "AA", ("AA",))),
            "AA: AI: the model",
        )

    def test_a_title_with_no_prefix_keeps_its_topic(self):
        self.assertEqual(M.title_topic("Topic", "CC"), "Topic")

    def test_a_session_that_held_nothing_still_keeps_an_acronym(self):
        # With no history the shape rules apply, and an acronym is not a letter shape
        # this platform issues for a thread that never held one.
        self.assertEqual(M.title_topic("AI: the model", "CC"), "AI: the model")

    def test_a_bare_topic_that_starts_with_an_acronym_keeps_it(self):
        # The topic is not a title, so the acronym is not a letter and stays.
        self.assertEqual(M.wanted_title("AA", "AI: the model"), "AA: AI: the model")


class MmReopenStrictTouchTest(unittest.TestCase):
    """The sweep sees a reopen that could not be done, so it keeps the session for a retry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        # The script never brings a registry into being, so the file is created here.  A
        # test that reads the registry of the host it runs on passes for the wrong
        # reason on the seat's own Mac and fails on a runner that keeps none.
        from simple_tools import operator_registry as R

        R.SessionRegistry(self.db).close_db()

    def _env(self):
        return mock.patch.dict(
            os.environ,
            {
                "SIMPLE_TOOLS_OPERATOR_REGISTRY": self.db,
                "MINIMAX_HOOKS_DATA": "",
                "MINIMAX_DATA_DIR": self.tmp.name,
            },
        )

    def test_a_reopen_that_cannot_be_done_is_raised_when_strict_touch_is_set(self):
        with (
            mock.patch.object(
                M, "touch_session", return_value={"record_id": "r1", "outcome": "closed"}
            ),
            mock.patch.object(
                M,
                "reopen_closed",
                side_effect=M.RegistryError("no letter", "space_lost", "reopen_refused"),
            ),
            mock.patch.object(M, "cached_letter", return_value="AA"),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(M.RegistryError),
        ):
            M.letter_for(SESSION_ID, "AA: Swept thread", "", strict_touch=True)

    def test_a_reopen_that_cannot_be_done_is_quiet_without_strict_touch(self):
        err = io.StringIO()
        with (
            mock.patch.object(
                M, "touch_session", return_value={"record_id": "r1", "outcome": "closed"}
            ),
            mock.patch.object(
                M,
                "reopen_closed",
                side_effect=M.RegistryError("no letter", "space_lost", "reopen_refused"),
            ),
            mock.patch.object(M, "cached_letter", return_value="AA"),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(M.letter_for(SESSION_ID, "AA: Swept thread", ""), "AA")
        self.assertIn("Could not reopen", err.getvalue())

    def test_a_faulting_registry_file_is_a_registry_error_not_a_traceback(self):
        # A corrupt registry file faults when it is opened or read:  that is a
        # refusal the caller can handle, never an uncaught sqlite error through the
        # hook.
        broken = os.path.join(self.tmp.name, "broken.sqlite3")
        with open(broken, "wb") as handle:
            handle.write(b"this is not a database")
        with (
            mock.patch.dict(
                os.environ,
                {
                    "SIMPLE_TOOLS_OPERATOR_REGISTRY": broken,
                    "MINIMAX_HOOKS_DATA": "",
                    "MINIMAX_DATA_DIR": self.tmp.name,
                },
            ),
            self.assertRaises(M.RegistryError) as caught,
        ):
            M.reopen_closed("r1")
        self.assertEqual(caught.exception.code, "reopen_failed")
        self.assertEqual(caught.exception.outcome, "reopen_refused")

    def test_a_reopen_that_fails_on_the_registry_is_a_registry_error(self):
        # A raw storage failure out of the reopen travels as a RegistryError, so every
        # caller handles it the same way.
        from simple_tools import operator_registry as OR

        with (
            self._env(),
            mock.patch.object(OR.SessionRegistry, "reopen", side_effect=OR.UnknownRecord("r1")),
        ):
            with self.assertRaises(M.RegistryError) as caught:
                M.reopen_closed("r1")
        self.assertEqual(caught.exception.code, "reopen_failed")

    def test_a_reopen_without_a_registry_is_a_registry_error_too(self):
        # A host that keeps no registry file routes the reopen to the transport;  a
        # host whose PATH also holds no transport surfaces the transport fault, and
        # that travels as the same one error type:  the registry is out of reach.
        with (
            mock.patch.object(M, "registry_db", return_value=""),
            mock.patch.dict(
                os.environ,
                {
                    "MM_OPERATOR_REGISTRY_CMD": "definitely-missing-registry-test",
                    "PATH": os.pathsep.join(
                        p
                        for p in os.environ.get("PATH", "").split(os.pathsep)
                        if "mm-operator-registry" not in p
                    )
                },
            ),
            self.assertRaises(M.RegistryError) as caught,
        ):
            M.reopen_closed("r1")
        self.assertIn(caught.exception.code, ("missing_tool", "remote_unreachable", "no_registry"))


class TransportTests(unittest.TestCase):
    """The registry transport:  explicit destination wins, no local second allocator."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "state"
        (self.state / "operator").mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "transport.log"
        self.registry = self.state / "operator" / "registry.sqlite3"
        self._env = {
            "SIMPLE_TOOLS_STATE_DIR": str(self.state),
            "HOME": str(root),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
        }

    def _transport(self, answer=None):
        if answer is None:
            answer = '{"letter": "AA", "record_id": 1}'
        cmd = self.bin / "mm-operator-registry"
        script = (
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> '" + str(self.log) + "'\n"
            "printf '%s' '" + answer + "'\n"
        )
        cmd.write_text(script)
        cmd.chmod(0o755)

    def _call(self, args, **env):
        merged = dict(self._env)
        merged.update(env)
        with mock.patch.dict(os.environ, merged, clear=False):
            return M.registry_call(args)

    def test_explicit_transport_wins_over_a_local_file(self):
        self.registry.write_bytes(b"not empty")  # a retired local store must not be used
        self._transport()
        out = self._call(
            ["register", "--platform=minimax"], MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"
        )
        self.assertEqual(out.get("letter"), "AA")
        self.assertIn("register --platform=minimax", self.log.read_text())
        self.assertEqual(self.registry.read_bytes(), b"not empty")  # untouched

    def test_all_mm_verbs_reach_the_transport(self):
        self._transport()
        for verb in (
            ["register", "--platform=minimax", "--session-id=s1"],
            ["touch", "--platform=minimax", "--scope=default", "--session-id=s1"],
            ["update", "--platform=minimax", "--scope=default", "--session-id=s1", "--title=AA: t"],
            [
                "relabel",
                "--platform=minimax",
                "--scope=default",
                "--session-id=s1",
                "--letter=AB",
                "--force",
            ],
            ["sweep", "--platform=minimax", "--stale-after-minutes=120"],
        ):
            self._call(verb, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry")
        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 5)
        self.assertIn("relabel", calls[3])
        self.assertIn("--force", calls[3])  # the double-apply risk verb passes through verbatim

    def test_no_local_store_uses_the_default_transport(self):
        self._transport()
        out = self._call(["history", "--platform=minimax"])
        self.assertEqual(out.get("letter"), "AA")
        self.assertIn("history", self.log.read_text())

    def test_a_missing_transport_is_missing_tool_not_no_registry(self):
        out = self._call(["history", "--platform=minimax"], MM_OPERATOR_REGISTRY_CMD="definitely-missing-registry-test")
        # Both are non-success transport faults;  which one fires depends on how the
        # host's PATH resolves the name (absent vs. unusable), not on the registry.
        self.assertIn(out.get("code"), ("missing_tool", "remote_unreachable"))

    def test_a_bad_transport_name_is_refused_never_run(self):
        out = self._call(["history"], MM_OPERATOR_REGISTRY_CMD="bad; rm -rf /")
        self.assertEqual(out.get("code"), "remote_refused")

    def test_a_success_shaped_answer_with_a_nonzero_exit_is_not_a_success(self):
        # The transport answered like a success but exited 1:  never treat it as one.
        cmd = self.bin / "mm-operator-registry"
        cmd.write_text("#!/bin/sh\nprintf '%s' '" + '{"letter": "AA"}' + "'\nexit 1\n")
        cmd.chmod(0o755)
        out = self._call(
            ["relabel", "--platform=minimax", "--force"],
            MM_OPERATOR_REGISTRY_CMD="mm-operator-registry",
        )
        self.assertNotEqual(out.get("letter"), "AA")
        self.assertEqual(out.get("outcome"), "outcome_unknown")
        self.assertIn("error", out)

    def test_reopen_closed_goes_to_the_transport_not_a_stale_local_store(self):
        self.registry.write_bytes(b"stale")  # a retired local store must not be written
        self._transport(
            answer='{"record_id": "r1", "label": "AB", "generation": 3, "previous": "AA", "outcome": "reopened"}'
        )
        with mock.patch.dict(
            os.environ,
            dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
            clear=False,
        ):
            answer = M.reopen_closed("r1", "parent-session-1")
        self.assertEqual(answer["label"], "AB")
        self.assertEqual(answer["generation"], 3)
        logged = self.log.read_text()
        self.assertIn("reopen --record-id=r1", logged)
        self.assertIn("--parent-session-id=parent-session-1", logged)
        self.assertEqual(self.registry.read_bytes(), b"stale")  # untouched

    def test_reopen_closed_space_lost_from_the_transport_keeps_its_code(self):
        self._transport(answer='{"error": "the pair went to another chat", "code": "space_lost"}')
        with (
            mock.patch.dict(
                os.environ,
                dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
                clear=False,
            ),
            self.assertRaises(M.RegistryError) as ctx,
        ):
            M.reopen_closed("r1")
        self.assertEqual(ctx.exception.code, "space_lost")

    def test_held_letters_come_from_the_transport_not_a_stale_local_store(self):
        # A retired local store that still answers must not win over the transport.
        self.registry.write_bytes(b"stale")
        self._transport(answer='{"letters": ["AB", "AA"]}')
        with mock.patch.dict(
            os.environ,
            dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
            clear=False,
        ):
            held = M.held_letters("session-0001")
        self.assertEqual(held, ("AB", "AA"))
        self.assertIn("held --platform=minimax", self.log.read_text())
        self.assertIn("--session-id=session-0001", self.log.read_text())

    def test_held_letters_rejects_a_string_answer_not_its_characters(self):
        self._transport(answer='{"letters": "AB"}')
        with mock.patch.dict(
            os.environ,
            dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
            clear=False,
        ):
            self.assertEqual(M.held_letters("session-0001"), ())

    def test_held_letters_rejects_a_non_list_answer_without_crashing(self):
        self._transport(answer='{"letters": 5}')
        with mock.patch.dict(
            os.environ,
            dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
            clear=False,
        ):
            self.assertEqual(M.held_letters("session-0001"), ())

    def test_held_letters_rejects_a_list_with_a_malformed_entry(self):
        for bad in (
            '{"letters": ["AA", 7]}',
            '{"letters": ["AA", "abc"]}',
            '{"letters": ["AA", "TOOLONG"]}',
        ):
            self._transport(answer=bad)
            with mock.patch.dict(
                os.environ,
                dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
                clear=False,
            ):
                self.assertEqual(M.held_letters("session-0001"), (), bad)
            self.log.unlink()

    def test_held_letters_rejects_an_error_answer_even_with_valid_letters(self):
        self._transport(
            answer='{"error": "registry exploded", "code": "held_failed", "letters": ["AB", "AA"]}'
        )
        with mock.patch.dict(
            os.environ,
            dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
            clear=False,
        ):
            self.assertEqual(M.held_letters("session-0001"), ())

    def test_held_letters_accepts_single_letters_and_pairs(self):
        self._transport(answer='{"letters": ["AB", "B"]}')
        with mock.patch.dict(
            os.environ,
            dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
            clear=False,
        ):
            self.assertEqual(M.held_letters("session-0001"), ("AB", "B"))

    def test_held_letters_on_a_transport_fault_is_empty_never_the_local_store(self):
        # The stale local store holds letters for this session;  a transport fault must
        # still return the empty history rather than the local answer.
        self.registry.write_bytes(b"stale")
        cmd = self.bin / "mm-operator-registry"
        cmd.write_text("#!/bin/sh\nexit 1\n")
        cmd.chmod(0o755)
        with mock.patch.dict(
            os.environ,
            dict(self._env, MM_OPERATOR_REGISTRY_CMD="mm-operator-registry"),
            clear=False,
        ):
            held = M.held_letters("session-0001")
        self.assertEqual(held, ())

    def test_the_far_side_no_registry_guard_maps_to_no_registry(self):
        self._transport(answer='{"error": "no Operator registry on this host"}')
        out = self._call(["register", "--platform=minimax"])
        self.assertEqual(out.get("code"), "no_registry")


class RegistryCliNeverCreatesTests(unittest.TestCase):
    """The registry CLI refuses a missing store instead of creating a second one."""

    def test_a_missing_db_is_refused_for_a_write_and_a_read(self):
        from simple_tools import operator_registry

        missing = os.path.join(tempfile.mkdtemp(), "operator", "registry.sqlite3")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            argv = [
                "register",
                "--db",
                missing,
                "--platform=minimax",
                "--location=server",
                "--scope=default",
                "--session-id=s1",
                "--seat=MM",
                "--source=test",
            ]
            code = operator_registry.main(argv)
        self.assertEqual(code, 3)
        self.assertIn("no Operator registry on this host", out.getvalue())
        self.assertFalse(os.path.exists(missing))  # never brought into being


class RegistryHttpsCallTest(unittest.TestCase):
    """_registry_https_call:  the registry API's client discipline (Path B)."""

    def _call(self, args, *, error=None, raw=b'{"label": "AA"}'):
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return raw

        class _Opener:
            def open(self, request, timeout=None):
                captured["request"] = request
                captured["timeout"] = timeout
                if error is not None:
                    raise error
                return _Response()

        env = {"SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "k" * 40}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(M, "_REGISTRY_OPENER", _Opener()),
        ):
            answer = M._registry_https_call(args, "https://operator.example.test", timeout=5)
        return answer, captured["request"]

    def _http_error(self, code, body):
        return urllib.error.HTTPError(
            "https://operator.example.test", code, "error", None, io.BytesIO(body)
        )

    def test_the_request_carries_an_explicit_user_agent(self):
        # The API's host is Cloudflare-fronted, and Cloudflare answers 1010 to
        # Python's default urllib agent:  the call names itself instead.
        _answer, request = self._call(["resolve", "--platform=minimax", "--letter=AA"])
        self.assertEqual(request.get_header("User-agent"), "mm-session-letter/1.0")

    def test_a_post_5xx_with_a_json_body_is_outcome_unknown(self):
        answer, _request = self._call(
            ["touch", "--platform=minimax"],
            error=self._http_error(500, b'{"error": "boom"}'),
        )
        self.assertEqual(answer["code"], "http_500")
        self.assertEqual(answer["outcome"], "outcome_unknown")

    def test_a_post_5xx_without_a_json_body_is_outcome_unknown(self):
        answer, _request = self._call(
            ["touch", "--platform=minimax"],
            error=self._http_error(502, b"bad gateway"),
        )
        self.assertEqual(answer["code"], "http_error")
        self.assertEqual(answer["outcome"], "outcome_unknown")

    def test_a_post_4xx_answer_is_a_definitive_refusal(self):
        # A 4xx is the API speaking for itself:  the write was not applied, so
        # the outcome is known and no idempotency re-check is owed.
        answer, _request = self._call(
            ["register", "--platform=minimax"],
            error=self._http_error(403, b'{"error": "refused", "code": "issuer_refused"}'),
        )
        self.assertEqual(answer["code"], "issuer_refused")
        self.assertNotIn("outcome", answer)

    def test_a_get_5xx_is_not_a_write_outcome(self):
        answer, _request = self._call(
            ["resolve", "--platform=minimax", "--letter=AA"],
            error=self._http_error(500, b'{"error": "boom"}'),
        )
        self.assertEqual(answer["code"], "http_500")
        self.assertNotIn("outcome", answer)


class RemoteRegistryHopTest(unittest.TestCase):
    """The hop to the host that holds the Operator's registry (AFC-412, server seat).

    This host keeps no registry of its own, so it asks the host that holds one
    through the transport command.  The transport is host configuration:  the helper
    names the command and the command carries the host, so the helper names no host,
    no user, no alias, no ssh flag and no remote path anywhere.
    """

    def _hop(self, **env: str):
        """Run the hop with the environment given, and hand back the answer."""

        def fake_run(cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout=self.stdout, stderr="")

        self.stdout = '{"label": "AA"}\n'
        self.cmd = []
        self.kwargs = {}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(M.subprocess, "run", side_effect=fake_run),
        ):
            answer = M.remote_registry_call(["history", "--platform=minimax"])
        return answer

    def test_the_transport_is_a_command_and_the_helper_names_nothing_else(self):
        answer = self._hop(**{M.ENV_TRANSPORT: "mm-operator-registry"})
        self.assertEqual(answer, {"label": "AA"})
        # The whole command:  the transport and the registry CLI's own arguments.
        # There is no host, no user, no alias, no ssh flag, no remote path and no
        # remote interpreter in it.
        self.assertEqual(self.cmd, ["mm-operator-registry", "history", "--platform=minimax"])

    def test_the_default_transport_is_the_one_the_house_keeps(self):
        # The transport defaults to the wrapper the house already installs, so a host
        # that names no transport of its own still has one.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(M.ENV_TRANSPORT, None)
            with mock.patch.object(M.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess(
                    [], 0, stdout='{"label": "AA"}', stderr=""
                )
                M.remote_registry_call(["history"])
        self.assertEqual(M.DEFAULT_TRANSPORT, "mm-operator-registry")
        self.assertEqual(run.call_args.args[0][0], M.DEFAULT_TRANSPORT)

    def test_the_helper_names_no_host_and_no_ssh_in_its_source(self):
        # The rule is about the source, not only about the command that comes out of
        # it:  a host, a user, an alias or an ssh flag in this file is the thing the
        # transport command exists to keep out.
        source = pathlib.Path(M.__file__).read_text(encoding="utf-8")
        for forbidden in ("BatchMode", "ConnectTimeout", "ProxyCommand"):
            self.assertNotIn(forbidden, source)

    def test_a_transport_that_is_not_a_plain_command_name_is_refused(self):
        # The shell reads an option before a command, so a transport name that is not
        # a plain command name is refused rather than run.
        for name in ("-oProxyCommand=touch /tmp/x", "mac host", "mac;id", "a/b", "-mac"):
            with self.subTest(name=name):
                with mock.patch.dict(os.environ, {M.ENV_TRANSPORT: name}):
                    answer = M.remote_registry_call(["history"])
                self.assertIsNotNone(answer)
                self.assertEqual(answer["code"], "remote_refused")

    def test_the_transport_runs_the_registry_cli_s_own_arguments(self):
        self._hop(**{M.ENV_TRANSPORT: "mm-operator-registry"})
        self.assertEqual(self.cmd[1:], ["history", "--platform=minimax"])
        # The hop has to finish inside the hook's own timeout.
        self.assertEqual(self.kwargs["timeout"], M.REMOTE_TIMEOUT)
        self.assertLess(M.REMOTE_TIMEOUT, 8)

    def test_an_argument_that_holds_a_space_is_one_argument(self):
        # The title is one argument on the far side, never two words the far shell
        # would read as two arguments.
        with (
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
            mock.patch.object(
                M.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout='{"label": "AA"}', stderr=""
                ),
            ) as run,
        ):
            M.remote_registry_call(["update", "--title=two words"])
        cmd = run.call_args.args[0]
        self.assertIn("--title=two words", cmd)
        self.assertEqual(len([a for a in cmd if a.startswith("--title")]), 1)

    def test_a_transport_that_cannot_start_is_unreachable(self):
        with (
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
            mock.patch.object(M.subprocess, "run", side_effect=OSError("no transport")),
        ):
            answer = M.remote_registry_call(["history"])
        self.assertEqual(answer["code"], "remote_unreachable")

    def test_a_missing_transport_is_its_own_failure(self):
        # A host with no transport command at all is a different fault from one whose
        # transport cannot reach the registry host, and the operator has to be able to
        # tell them apart.
        with (
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
            mock.patch.object(
                M.subprocess, "run", side_effect=FileNotFoundError(2, "No such file")
            ),
        ):
            answer = M.remote_registry_call(["history"])
        self.assertEqual(answer["code"], "missing_tool")
        self.assertEqual(M._registry_exit_code(M.RegistryError("x", "missing_tool")), 127)

    def test_a_hop_that_times_out_has_an_unknown_outcome(self):
        # The write may have landed before the answer was lost, so this is an
        # unknown outcome and never a call that never happened.
        with (
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
            mock.patch.object(
                M.subprocess, "run", side_effect=subprocess.TimeoutExpired("transport", 6)
            ),
        ):
            answer = M.remote_registry_call(["register"])
        self.assertEqual(answer["code"], "timeout")
        self.assertEqual(answer["outcome"], "outcome_unknown")
        self.assertEqual(M._registry_exit_code(M.RegistryError("x", "timeout")), 124)

    def test_a_hop_that_answers_with_nothing_has_an_unknown_outcome(self):
        self.stdout = "   \n"
        with (
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
            mock.patch.object(
                M.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=self.stdout, stderr=""),
            ),
        ):
            answer = M.remote_registry_call(["register"])
        self.assertEqual(answer["code"], "remote_empty")
        self.assertEqual(answer["outcome"], "outcome_unknown")

    def test_a_hop_that_answers_with_something_else_is_refused(self):
        for payload in ("not json at all", "[1, 2, 3]", '"a string"'):
            with self.subTest(payload=payload):
                self.stdout = payload
                with (
                    mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
                    mock.patch.object(
                        M.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, stdout=payload, stderr=""),
                    ),
                ):
                    answer = M.remote_registry_call(["register"])
                self.assertEqual(answer["code"], "remote_unparseable")
                self.assertEqual(answer["outcome"], "outcome_unknown")

    def test_a_far_host_that_exits_non_zero_with_a_success_answer_is_a_failure(self):
        # The far side's exit code counts:  a call that failed with a non-zero exit is
        # not a success, whatever its answer looks like.
        with (
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
            mock.patch.object(
                M.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 2, stdout='{"label": "AA"}', stderr=""
                ),
            ),
        ):
            answer = M.remote_registry_call(["register"])
        self.assertIn("error", answer)
        self.assertNotIn("label", answer)

    def test_a_far_host_that_keeps_no_registry_is_a_configuration_state(self):
        # The registry's own guard answers that the host that holds it keeps no
        # registry:  that is a configuration state, not a fault of this call.
        with (
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "mm-operator-registry"}),
            mock.patch.object(
                M.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 3, stdout='{"error": "no Operator registry on this host"}', stderr=""
                ),
            ),
        ):
            answer = M.remote_registry_call(["history"])
        self.assertEqual(answer["code"], "no_registry")

    def test_the_local_registry_is_used_when_this_host_has_one(self):
        # A host that keeps its own registry never goes over the wire.
        with (
            mock.patch.object(M, "registry_db", return_value="/tmp/registry.sqlite3"),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(M.subprocess, "Popen") as popen,
            mock.patch.object(M.subprocess, "run") as run,
        ):
            popen.return_value.communicate.return_value = ('{"label": "AA"}', "")
            popen.return_value.returncode = 0
            answer = M.registry_call(["history"])
        self.assertEqual(answer, {"label": "AA"})
        run.assert_not_called()

    def test_the_far_host_is_asked_when_this_host_has_no_registry(self):
        with (
            mock.patch.object(M, "registry_db", return_value=""),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(M, "remote_registry_call", return_value={"label": "AA"}) as hop,
        ):
            answer = M.registry_call(["history", "--platform=minimax"])
        self.assertEqual(answer, {"label": "AA"})
        hop.assert_called_once()
        self.assertEqual(hop.call_args.args[0], ["history", "--platform=minimax"])

    def test_a_refused_transport_is_a_fault_and_not_a_missing_registry(self):
        # A host that names a transport the shell would read as an option has a
        # misconfiguration, and that is not the same as having no registry at all.
        with (
            mock.patch.object(M, "registry_db", return_value=""),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.dict(os.environ, {M.ENV_TRANSPORT: "-oProxyCommand=touch /tmp/x"}),
        ):
            answer = M.registry_call(["history"])
        self.assertEqual(answer["code"], "remote_refused")

    def test_a_letter_is_issued_over_the_hop(self):
        # The whole path a MiniMax chat walks when it has no local registry.
        with (
            mock.patch.object(M, "registry_db", return_value=""),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(
                M, "remote_registry_call", return_value={"label": "AA", "created": True}
            ) as hop,
        ):
            answer = M.register(SESSION_ID)
        self.assertEqual(answer["label"], "AA")
        args = hop.call_args.args[0]
        self.assertEqual(args[0], "register")
        self.assertIn("--platform=minimax", args)
        self.assertIn("--session-id=" + SESSION_ID, args)



class ReopenOverTheHopTest(unittest.TestCase):
    """A chat that comes back to life gets its letter back, wherever the registry is.

    The sweep ends a session's letter while its chat is live.  When the chat returns,
    the reopen has to reach the registry:  on a host that keeps no copy, that is the
    host that holds one.
    """

    def test_the_reopen_goes_to_the_far_host_when_this_host_has_no_registry(self):
        with (
            mock.patch.object(M, "registry_db", return_value=""),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(
                M,
                "remote_registry_call",
                return_value={"record_id": "r1", "label": "AA", "outcome": "reopened"},
            ) as hop,
        ):
            answer = M.reopen_closed("r1")
        self.assertEqual(answer["label"], "AA")
        args = hop.call_args.args[0]
        self.assertEqual(args[0], "reopen")
        self.assertIn("--record-id=r1", args)
        self.assertIn("--letter-pick=" + M.LETTER_PICK, args)

    def test_the_reopen_asks_for_the_parents_pair_when_there_is_one(self):
        parent = "mvs_parent000000000000000000000000000001"
        with (
            mock.patch.object(M, "registry_db", return_value=""),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(
                M, "remote_registry_call", return_value={"record_id": "r1", "label": "BB"}
            ) as hop,
        ):
            M.reopen_closed("r1", parent)
        self.assertIn("--parent-session-id=" + parent, hop.call_args.args[0])

    def test_a_refused_reopen_keeps_its_own_code_and_outcome(self):
        # The far host answers a refusal with the code and the outcome, and the
        # caller reads them the same way it reads a local refusal.
        with (
            mock.patch.object(M, "registry_db", return_value=""),
            mock.patch.object(M, "repo_scripts_dir", return_value="/tmp"),
            mock.patch.object(
                M,
                "remote_registry_call",
                return_value={
                    "error": "the pair AA went to another chat",
                    "code": "space_lost",
                    "outcome": "reopen_refused",
                },
            ),
            self.assertRaises(M.RegistryError) as caught,
        ):
            M.reopen_closed("r1")
        self.assertEqual(caught.exception.code, "space_lost")
        self.assertEqual(caught.exception.outcome, "reopen_refused")



class StopHookTest(unittest.TestCase):
    """The Stop hook never blocks the agent over a registry that cannot be reached."""

    def _payload(self):
        return json.dumps({"session_id": SESSION_ID}).encode()

    def _row(self):
        return {"session_id": SESSION_ID, "title": "AA: A topic", "session_kind": "conversation"}

    def test_a_registry_that_cannot_be_reached_does_not_block_the_stop(self):
        # A far host that is asleep is not the agent's fault, and a Stop hook that
        # exits 2 keeps the agent in a loop.
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(self._payload())),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(M, "session_row", return_value=self._row()),
            mock.patch.object(M, "cached_letter", return_value="AA"),
            mock.patch.object(
                M,
                "update_title",
                side_effect=M.RegistryError("the registry host did not answer", "timeout"),
            ),
        ):
            self.assertEqual(M.hook_stop(), 0)
        self.assertIn("registry host did not answer", stderr.getvalue())

    def test_a_title_that_carries_a_credential_still_blocks_the_stop(self):
        # A bad title is the agent's to fix, so that one still says so.
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", _StdinBytes(self._payload())),
            mock.patch.object(sys, "stderr", stderr),
            mock.patch.object(M, "session_row", return_value=self._row()),
            mock.patch.object(M, "cached_letter", return_value="AA"),
            mock.patch.object(M, "update_title", side_effect=ValueError("secret_detected")),
        ):
            self.assertEqual(M.hook_stop(), 2)
        self.assertIn("secret_detected", stderr.getvalue())
