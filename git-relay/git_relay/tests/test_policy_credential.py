import os
import stat
import struct
import unittest
from typing import ClassVar
from unittest.mock import patch

from git_relay.policy import PolicyError
from git_relay.policy_input import MAX_POLICY_BYTES, _systemd_acl, seats_json_from_env


class CredentialInput(unittest.TestCase):
    env: ClassVar[dict] = {
        "GIT_RELAY_SEATS_CREDENTIAL": "seat-policy",
        "CREDENTIALS_DIRECTORY": "/run/credentials/git-relay.service",
    }

    def info(self, mode, uid=0, gid=0, size=2, nlink=1):
        return os.stat_result((mode, 1, 1, nlink, uid, gid, size, 0, 0, 0))

    def acl(self, permission):
        return struct.pack("<I", 2) + b"".join(
            struct.pack("<HHI", *row)
            for row in [
                (1, permission, 0xFFFFFFFF),
                (2, permission, os.geteuid()),
                (4, 0, 0xFFFFFFFF),
                (16, permission, 0xFFFFFFFF),
                (32, 0, 0xFFFFFFFF),
            ]
        )

    def test_selector_binding_ambiguity_before_open(self):
        for extra in [
            {"GIT_RELAY_SEATS_FILE": "/private"},
            {"GIT_RELAY_SEATS_JSON": ""},
            {"GIT_RELAY_SEATS_CREDENTIAL": "../apps-token"},
            {"CREDENTIALS_DIRECTORY": "/tmp/credentials"},
        ]:
            with patch("git_relay.policy_input.os.open") as opened, self.assertRaises(PolicyError):
                seats_json_from_env({**self.env, **extra})
            opened.assert_not_called()

    def test_acl_only_root_and_exact_dynamic_uid(self):
        for perm in [4, 5]:
            with patch("git_relay.policy_input.os.getxattr", return_value=self.acl(perm)):
                _systemd_acl(8, perm)
            for raw in [
                b"",
                self.acl(perm) + struct.pack("<HHI", 2, perm, os.geteuid() + 1),
                self.acl(perm).replace(
                    struct.pack("<HHI", 4, 0, 0xFFFFFFFF), struct.pack("<HHI", 4, perm, 0xFFFFFFFF)
                ),
            ]:
                with (
                    patch("git_relay.policy_input.os.getxattr", return_value=raw),
                    self.assertRaises(PolicyError),
                ):
                    _systemd_acl(8, perm)

    def test_fixed_relative_open_and_fd_cleanup_success(self):
        infos = [
            self.info(stat.S_IFDIR | 0o755),
            self.info(stat.S_IFDIR | 0o755),
            self.info(stat.S_IFDIR | 0o550),
            self.info(stat.S_IFDIR | 0o550),
            self.info(stat.S_IFREG | 0o440),
        ]
        with (
            patch("git_relay.policy_input.os.open", side_effect=[10, 11, 12, 13, 14]) as opened,
            patch("git_relay.policy_input.os.fstat", side_effect=infos),
            patch("git_relay.policy_input.os.getxattr", side_effect=[self.acl(5), self.acl(4)]),
            patch("git_relay.policy_input._read_policy_fd", return_value="{}") as read,
            patch("git_relay.policy_input.os.close") as close,
        ):
            self.assertEqual(seats_json_from_env(self.env), "{}")
            self.assertEqual(opened.call_args_list[-1].args[0], "seat-policy")
            self.assertEqual(opened.call_args_list[-1].kwargs, {"dir_fd": 13})
            self.assertTrue(opened.call_args_list[-1].args[1] & os.O_NOFOLLOW)
            read.assert_called_once_with(14)
            self.assertEqual([x.args[0] for x in close.call_args_list], [14, 13, 12, 11, 10])

    def test_bad_mode_owner_group_link_size_and_permission_failure_private(self):
        for info in [
            self.info(stat.S_IFREG | 0o444),
            self.info(stat.S_IFREG | 0o440, uid=123),
            self.info(stat.S_IFREG | 0o440, gid=123),
            self.info(stat.S_IFREG | 0o440, nlink=2),
            self.info(stat.S_IFREG | 0o440, size=MAX_POLICY_BYTES + 1),
            self.info(stat.S_IFIFO | 0o440),
        ]:
            dirs = [
                self.info(stat.S_IFDIR | 0o755),
                self.info(stat.S_IFDIR | 0o755),
                self.info(stat.S_IFDIR | 0o550),
                self.info(stat.S_IFDIR | 0o550),
            ]
            with (
                patch("git_relay.policy_input.os.open", side_effect=[10, 11, 12, 13, 14]),
                patch("git_relay.policy_input.os.fstat", side_effect=dirs + [info]),
                patch("git_relay.policy_input.os.getxattr", return_value=self.acl(5)),
                patch("git_relay.policy_input.os.close"),
                self.assertRaises(PolicyError),
            ):
                seats_json_from_env(self.env)
        with (
            patch(
                "git_relay.policy_input.os.open",
                side_effect=PermissionError("private-policy-contents"),
            ),
            self.assertRaises(PolicyError) as error,
        ):
            seats_json_from_env(self.env)
        self.assertNotIn("private-policy", str(error.exception))

    def test_production_credential_errors_redacted(self):
        from git_relay import server

        with (
            patch.dict(os.environ, self.env, clear=True),
            patch("git_relay.policy_input.os.open", side_effect=OSError("private-path")),
            self.assertRaises(SystemExit) as error,
        ):
            server.from_env()
        self.assertEqual(
            str(error.exception), "git-relay config: file seat policy invalid or unreadable"
        )


class RealAclFixture(unittest.TestCase):
    def test_real_named_uid_acl_bytes_and_bounded_read(self):
        import tempfile
        from pathlib import Path

        from git_relay.policy_input import _read_policy_fd

        # A local synthetic file only. No service, credentials or policy touched.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic-policy"
            path.write_text('{"synthetic":{}}')
            path.chmod(0o400)
            none = 0xFFFFFFFF
            uid = os.geteuid() + 12345
            acl = struct.pack("<I", 2) + b"".join(
                struct.pack("<HHI", *row)
                for row in [(1, 4, none), (2, 4, uid), (4, 0, none), (16, 4, none), (32, 0, none)]
            )
            try:
                os.setxattr(path, "system.posix_acl_access", acl)
            except OSError as error:
                self.skipTest(
                    "local filesystem does not support synthetic ACL fixture: " + str(error.errno)
                )
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with patch("git_relay.policy_input.os.geteuid", return_value=uid):
                    _systemd_acl(fd, 4)
                self.assertEqual(_read_policy_fd(fd), '{"synthetic":{}}')
            finally:
                os.close(fd)
