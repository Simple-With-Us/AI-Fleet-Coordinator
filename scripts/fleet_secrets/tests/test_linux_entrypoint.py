"""Scoped Linux authentication; only synthetic credentials and no network."""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "linux_entrypoint", Path(__file__).parents[1] / "linux-entrypoint.py"
)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

PAIR = {
    "INFISICAL_AUTOMATION_CLIENT_ID": "synthetic-id",
    "INFISICAL_AUTOMATION_CLIENT_SECRET": "synthetic-secret",
}


class BootstrapTests(unittest.TestCase):
    def bootstrap_context(self, payload=None, *, directory=None, file=None):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(entry.core, "is_linux", return_value=True))
        stack.enter_context(mock.patch.object(entry.os, "geteuid", return_value=0))
        opened = stack.enter_context(
            mock.patch.object(entry.core.os, "open", side_effect=[10, 11, 12, 13, 14])
        )
        closed = stack.enter_context(mock.patch.object(entry.core.os, "close"))
        directory = directory or SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o700)
        file = file or SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1)
        stack.enter_context(
            mock.patch.object(
                entry.core.os, "fstat", side_effect=[directory] * 3 + [file]
            )
        )
        stream = io.StringIO(json.dumps(PAIR) if payload is None else payload)
        stack.enter_context(mock.patch.object(entry.core.os, "fdopen", return_value=stream))
        return opened, closed

    def test_private_bootstrap_is_descriptor_scoped_and_validated(self):
        opened, closed = self.bootstrap_context()
        self.assertEqual(entry.private_bootstrap(), PAIR)
        self.assertEqual(opened.call_args.args[0], entry.core.LINUX_ROOT_PRIVATE_BOOTSTRAP_BASENAME)
        self.assertEqual(opened.call_args.kwargs["dir_fd"], 13)
        self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)
        self.assertEqual(
            closed.call_args_list, [mock.call(10), mock.call(11), mock.call(12), mock.call(13)]
        )

    def test_private_bootstrap_rejects_schema_and_duplicate_keys_without_values(self):
        cases = [
            "[]",
            "null",
            "{}",
            json.dumps(dict(PAIR, extra="private")),
            json.dumps({key: 1 for key in PAIR}),
            '{"INFISICAL_AUTOMATION_CLIENT_ID":"synthetic-secret",'
            + '"INFISICAL_AUTOMATION_CLIENT_ID":"duplicate"}',
            "x" * 16385,
        ]
        for payload in cases:
            with self.subTest(payload=payload[:20]):
                self.bootstrap_context(payload)
                with self.assertRaises(entry.EntryError) as caught:
                    entry.private_bootstrap()
                self.assertEqual(
                    str(caught.exception), "root-private bootstrap is invalid or unsafe"
                )

    def test_private_bootstrap_rejects_permissions_owner_type_and_links(self):
        for changes in (
            {"st_uid": 1},
            {"st_gid": 1},
            {"st_mode": stat.S_IFREG | 0o644},
            {"st_mode": stat.S_IFIFO | 0o600},
            {"st_nlink": 2},
        ):
            values = {"st_uid": 0, "st_gid": 0, "st_mode": stat.S_IFREG | 0o600, "st_nlink": 1}
            values.update(changes)
            self.bootstrap_context(file=SimpleNamespace(**values))
            with self.assertRaises(entry.EntryError):
                entry.private_bootstrap()
        self.bootstrap_context(
            directory=SimpleNamespace(st_uid=0, st_gid=0, st_mode=stat.S_IFDIR | 0o755)
        )
        with self.assertRaises(entry.EntryError):
            entry.private_bootstrap()

    def test_private_bootstrap_never_reads_on_other_platform_or_nonroot(self):
        for linux, uid in ((False, 0), (True, 1000)):
            with (
                mock.patch.object(entry.core, "is_linux", return_value=linux),
                mock.patch.object(entry.os, "geteuid", return_value=uid),
                mock.patch.object(entry.core.os, "open") as opened,
            ):
                self.assertEqual(entry.private_bootstrap(), {})
            opened.assert_not_called()

    def test_bootstrap_fallback_does_not_mix_partial_environment_pairs(self):
        with mock.patch.object(entry, "private_bootstrap", return_value=PAIR) as read:
            self.assertEqual(entry.load_machine_credentials({}), list(PAIR.values()))
            read.assert_called_once()
        with mock.patch.object(entry, "private_bootstrap") as read:
            with self.assertRaises(entry.EntryError):
                entry.load_machine_credentials({entry.core.ENV_AUTOMATION_ID: "inherited"})
            read.assert_not_called()

    def test_unsafe_open_is_sanitized_and_descriptor_closed(self):
        with (
            mock.patch.object(entry.core, "is_linux", return_value=True),
            mock.patch.object(entry.os, "geteuid", return_value=0),
            mock.patch.object(entry.core.os, "open", side_effect=[10, OSError("synthetic-secret")]),
            mock.patch.object(entry.core.os, "close") as closed,
        ):
            with self.assertRaises(entry.EntryError) as caught:
                entry.private_bootstrap()
            self.assertNotIn("synthetic-secret", str(caught.exception))
        closed.assert_called_once_with(10)

    def process(self, stdout="synthetic-token\n", returncode=0):
        proc = mock.Mock(returncode=returncode)
        proc.communicate.return_value = (stdout, "synthetic-private-output")
        proc.poll.return_value = returncode
        return proc

    def delegate(self, tmpdir="delegate"):
        """A real, executable stand-in for the plain CLI, so the delegate check passes and the
        test reaches the login it is actually about."""
        path = Path(tempfile.mkdtemp()) / tmpdir / "fleet-secrets"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        self.addCleanup(shutil.rmtree, path.parent, True)
        return path

    def test_environment_login_uses_resolved_cli_and_confines_credentials(self):
        proc = self.process()
        with (
            mock.patch.dict(os.environ, PAIR, clear=True),
            mock.patch.object(entry.core, "find_cli", return_value="/synthetic/infisical"),
            mock.patch.object(entry.subprocess, "Popen", return_value=proc) as run,
            mock.patch("builtins.open", side_effect=AssertionError("credential files forbidden")),
        ):
            env = entry.authenticated_env()
        self.assertEqual(env, {"INFISICAL_TOKEN": "synthetic-token"})
        argv = " ".join(run.call_args.args[0])
        self.assertNotIn("synthetic-secret", argv)
        self.assertNotIn("synthetic-id", argv)
        child = run.call_args.kwargs["env"]
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_SECRET", child)
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_ID", child)
        self.assertEqual(child["INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"], "synthetic-secret")
        self.assertEqual(child["INFISICAL_DISABLE_UPDATE_CHECK"], "true")
        self.assertEqual(run.call_args.kwargs["cwd"], "/")

    def test_inherited_token_wins_and_strips_all_machine_credentials(self):
        with (
            mock.patch.dict(os.environ, dict(PAIR, INFISICAL_TOKEN="synthetic-token"), clear=True),
            mock.patch.object(entry, "load_machine_credentials") as load,
        ):
            self.assertEqual(entry.authenticated_env(), {"INFISICAL_TOKEN": "synthetic-token"})
        load.assert_not_called()

    def test_missing_environment_credentials_are_named_without_values(self):
        for missing in PAIR:
            env = {key: value for key, value in PAIR.items() if key != missing}
            with self.assertRaises(entry.EntryError) as caught:
                entry.load_machine_credentials(env)
            self.assertIn(missing, str(caught.exception))
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_failed_login_suppresses_private_child_output(self):
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                dict(PAIR, FLEET_SECRETS_CLI=str(self.delegate())),
                clear=True,
            ),
            mock.patch.object(entry.core, "find_cli", return_value="/synthetic/infisical"),
            mock.patch.object(
                entry.subprocess, "Popen", return_value=self.process("PRIVATE_CHILD", 1)
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(entry.main(), 4)
        self.assertNotIn("PRIVATE_CHILD", stderr.getvalue())
        self.assertNotIn("synthetic-secret", stderr.getvalue())

    def test_timeout_reaps_the_child_and_restores_the_signal_handler(self):
        proc = self.process()
        proc.poll.return_value = None
        proc.communicate.side_effect = [subprocess.TimeoutExpired("login", 25), ("", "")]
        original = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(entry.subprocess, "Popen", return_value=proc):
            with self.assertRaises(subprocess.TimeoutExpired):
                entry._login("/synthetic/infisical", {})
        proc.terminate.assert_called_once()
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_interruption_reaps_the_child(self):
        proc = self.process()
        proc.poll.return_value = None
        proc.communicate.side_effect = [entry.Interrupted(signal.SIGTERM), ("", "")]
        with mock.patch.object(entry.subprocess, "Popen", return_value=proc):
            with self.assertRaises(entry.Interrupted):
                entry._login("/synthetic/infisical", {})
        proc.terminate.assert_called_once()

    def test_delegating_to_the_entry_point_itself_is_refused(self):
        # FLEET_SECRETS_BIN names the fleet-secrets command, which on the server is this
        # script:  exec'ing it would re-authenticate and exec again, forever.
        self_file = Path(entry.__file__)
        with tempfile.TemporaryDirectory() as home:
            install = Path(home) / "share"
            install.mkdir()
            (install / "fleet-secrets").symlink_to(self_file)
            for env in (
                {"FLEET_SECRETS_CLI": str(self_file), "HOME": "/nonexistent"},
                {"FLEET_SECRETS_HOME": str(install), "HOME": "/nonexistent"},
            ):
                with self.subTest(env=sorted(env)):
                    with (
                        mock.patch.dict(os.environ, dict(PAIR, **env), clear=True),
                        mock.patch.object(entry, "authenticated_env") as auth,
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(entry.main(), 127)
                    auth.assert_not_called()

    def test_a_missing_cli_is_reported_before_the_login(self):
        # The delegate is checked first, so a mis-pathed CLI costs no login and is named.
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, dict(PAIR, FLEET_SECRETS_CLI="/nonexistent/fleet-secrets"), clear=True
            ),
            mock.patch.object(entry, "authenticated_env") as auth,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(entry.main(), 127)
        auth.assert_not_called()
        self.assertIn("FLEET_SECRETS_CLI", stderr.getvalue())

    def test_usage_requests_skip_the_login_and_carry_no_credential(self):
        # `--help` reads no secret:  no network login, and the delegate gets no machine credential.
        for argv in ([], ["--help"], ["-h"], ["help"]):
            with (
                self.subTest(argv=argv),
                mock.patch.dict(
                    os.environ,
                    dict(
                        PAIR,
                        INFISICAL_TOKEN="synthetic-token",
                        FLEET_SECRETS_CLI=str(self.delegate()),
                    ),
                    clear=True,
                ),
                mock.patch.object(entry.sys, "argv", ["fleet-secrets", *argv]),
                mock.patch.object(entry, "authenticated_env") as auth,
                mock.patch.object(entry.os, "execve") as execve,
            ):
                entry.main()
            auth.assert_not_called()
            sent = execve.call_args.args[2]
            self.assertFalse(set(sent) & set(entry.core.AUTH_ENV_VARS))

    def test_a_command_that_reads_secrets_still_logs_in(self):
        with (
            mock.patch.dict(
                os.environ, dict(PAIR, FLEET_SECRETS_CLI=str(self.delegate())), clear=True
            ),
            mock.patch.object(entry.sys, "argv", ["fleet-secrets", "names"]),
            mock.patch.object(
                entry, "authenticated_env", return_value={"INFISICAL_TOKEN": "t"}
            ) as auth,
            mock.patch.object(entry.os, "execve") as execve,
        ):
            entry.main()
        auth.assert_called_once()
        self.assertEqual(execve.call_args.args[2], {"INFISICAL_TOKEN": "t"})

    def test_missing_cli_has_a_distinct_exit_status(self):
        # The delegate is present, so this is the infisical CLI that is missing.
        with (
            mock.patch.dict(
                os.environ,
                dict(PAIR, FLEET_SECRETS_CLI=str(self.delegate())),
                clear=True,
            ),
            mock.patch.object(entry.core, "find_cli", return_value=None),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(entry.main(), 127)

    def test_inherited_token_is_validated_without_falling_back(self):
        with mock.patch.dict(os.environ, dict(PAIR, INFISICAL_TOKEN="invalid token"), clear=True):
            with self.assertRaises(entry.EntryError):
                entry.authenticated_env()


if __name__ == "__main__":
    unittest.main()
