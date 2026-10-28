"""Synthetic only: no native command, secret, installed path or service touched."""

import importlib.util
import io
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "custody", Path(__file__).with_name("linux-custody.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
SYNTHETIC = b"sbt_echo_" + b"A" * 43


class CustodyTests(unittest.TestCase):
    def test_ingress_only_native_stdin_and_exclusive_encrypted_output(self):
        parent = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        out = io.BytesIO()
        with (
            mock.patch.object(mod.os, "geteuid", return_value=0),
            mock.patch.object(mod, "check_code"),
            mock.patch.object(mod.os, "fstat", return_value=parent),
            mock.patch.object(mod.os, "stat", side_effect=FileNotFoundError),
            mock.patch.object(mod.os, "close"),
            mock.patch.object(Path, "exists", return_value=False),
            mock.patch.object(Path, "is_symlink", return_value=False),
            mock.patch.object(
                mod.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout=b"ciphertext"),
            ) as run,
            mock.patch.object(mod.os, "open", return_value=5) as opened,
            mock.patch.object(mod.os, "fdopen") as fdopen,
        ):
            fdopen.return_value.__enter__.return_value = out
            self.assertEqual(mod.ingress(io.BytesIO(SYNTHETIC)), 0)
        self.assertEqual(run.call_args.kwargs["input"], SYNTHETIC)
        self.assertNotIn(SYNTHETIC.decode(), " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/systemd-creds")
        self.assertEqual(out.getvalue(), b"ciphertext")
        self.assertTrue(opened.call_args.args[1] & mod.os.O_EXCL)
        self.assertEqual(opened.call_args.args[0], mod.ENCRYPTED.name)
        self.assertEqual(opened.call_args.kwargs["dir_fd"], 5)

    def test_wrong_seat_or_existing_entry_never_encrypts(self):
        parent = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        for existing, token in ((True, SYNTHETIC), (False, b"sbt_wise_" + b"A" * 43)):
            with (
                mock.patch.object(mod.os, "geteuid", return_value=0),
                mock.patch.object(mod, "check_code"),
                mock.patch.object(mod.os, "fstat", return_value=parent),
                mock.patch.object(
                    mod.os, "stat", side_effect=None if existing else FileNotFoundError
                ),
                mock.patch.object(mod.os, "open", return_value=5),
                mock.patch.object(mod.os, "close"),
                mock.patch.object(Path, "exists", return_value=existing),
                mock.patch.object(Path, "is_symlink", return_value=False),
                mock.patch.object(mod.subprocess, "run") as run,
            ):
                with self.assertRaises(ValueError):
                    mod.ingress(io.BytesIO(token))
                run.assert_not_called()

    def test_ingress_checks_code_and_rejects_oversized_before_encryption(self):
        parent = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        with (
            mock.patch.object(mod, "check_code") as checked,
            mock.patch.object(mod.os, "geteuid", return_value=0),
            mock.patch.object(mod.os, "open", return_value=5),
            mock.patch.object(mod.os, "close"),
            mock.patch.object(mod.os, "fstat", return_value=parent),
            mock.patch.object(mod.os, "stat", side_effect=FileNotFoundError),
            mock.patch.object(mod.subprocess, "run") as run,
        ):
            with self.assertRaises(ValueError):
                mod.ingress(io.BytesIO(SYNTHETIC + b" " * 300))
            checked.assert_called_once()
            run.assert_not_called()

    def test_launcher_fixed_service_hardening_no_caller_selector_or_environment(self):
        with (
            mock.patch.object(mod, "check_code"),
            mock.patch.object(mod.os, "geteuid", return_value=0),
            mock.patch.object(mod.os, "execve") as execute,
        ):
            mod.launch()
        executable, argv, env = execute.call_args.args
        self.assertEqual(executable, "/usr/bin/systemd-run")
        self.assertIn("--unit=assigned-echo-mcp", argv)
        self.assertIn("--property=DynamicUser=yes", argv)
        self.assertIn(
            "--property=LoadCredentialEncrypted=assigned-echo-mcp:" + str(mod.ENCRYPTED), argv
        )
        self.assertEqual(env, {"PATH": "/usr/bin:/bin"})
        self.assertNotIn(SYNTHETIC.decode(), " ".join(argv))

    def test_child_only_credential_file_and_memory_environment(self):
        source = io.BytesIO(SYNTHETIC)
        with (
            mock.patch.object(mod, "check_code"),
            mock.patch.object(mod.os, "geteuid", return_value=123),
            mock.patch.object(mod.os, "open", return_value=5) as opened,
            mock.patch.object(mod.os, "fdopen") as fdopen,
            mock.patch.object(
                mod.os,
                "fstat",
                return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o400, st_uid=123),
            ),
            mock.patch.object(mod.os, "execve") as execute,
        ):
            fdopen.return_value.__enter__.return_value = source
            source.fileno = lambda: 5
            mod.child()
        self.assertEqual(opened.call_args.args[0], mod.CREDENTIAL)
        exe, argv, env = execute.call_args.args
        self.assertEqual(exe, "/usr/bin/python3")
        self.assertEqual(argv[-1], str(mod.CODE / "assigned-hosted-stdio.py"))
        self.assertEqual(env, {"PATH": "/usr/bin:/bin", "ASSIGNED_MCP_BEARER": SYNTHETIC.decode()})

    def test_no_root_or_seat_flags_are_refused_without_effect(self):
        with (
            mock.patch.object(mod.os, "geteuid", return_value=1000),
            mock.patch.object(mod, "check_code"),
            mock.patch.object(mod.os, "execve") as execute,
        ):
            with self.assertRaises(ValueError):
                mod.launch()
            execute.assert_not_called()
        with mock.patch.object(mod.sys, "stderr", io.StringIO()):
            self.assertEqual(mod.main(["WISE"]), 3)


if __name__ == "__main__":
    unittest.main()
