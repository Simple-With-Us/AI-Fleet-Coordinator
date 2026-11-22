"""Fixed systemd credential boundary, no live credential reads."""

import io
import os
import stat
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from git_relay import app_tokens as A

D = "/run/credentials/git-relay.service"


def acl(permission, uid=None):
    uid = os.geteuid() if uid is None else uid
    none = 0xFFFFFFFF
    entries = [
        (1, permission, none),
        (2, permission, uid),
        (4, 0, none),
        (16, permission, none),
        (32, 0, none),
    ]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)


def info(mode, **over):
    fields = dict(
        st_mode=mode,
        st_uid=0,
        st_gid=0,
        st_size=32,
        st_nlink=1,
        st_dev=1,
        st_ino=2,
        st_mtime_ns=3,
        st_ctime_ns=4,
    )
    return SimpleNamespace(**dict(fields, **over))


class SystemdSourceTests(unittest.TestCase):
    def run_open(
        self,
        *,
        scope="/tools",
        path=None,
        env=None,
        changes=None,
        bad_acl=None,
        open_error=None,
        consumer=False,
    ):
        path = path or D + "/git-relay-tools-token"
        env = {"CREDENTIALS_DIRECTORY": D} if env is None else env
        changes = changes or {}
        opened = []
        modes = {
            10: info(stat.S_IFDIR | 0o755),
            11: info(stat.S_IFDIR | 0o755),
            12: info(stat.S_IFDIR | 0o755),
            13: info(stat.S_IFDIR | 0o550),
            14: info(stat.S_IFREG | 0o440),
        }
        modes.update(changes)

        def opening(name, flags, **kw):
            if open_error and name == open_error:
                raise OSError("synthetic no-follow refusal")
            fd = 10 + len(opened)
            opened.append((fd, name, flags, kw))
            return fd

        def getacl(fd, name):
            self.assertEqual(name, "system.posix_acl_access")
            return bad_acl if bad_acl is not None else acl(5 if fd == 13 else 4)

        class Stream(io.BytesIO):
            def fileno(self):
                return 14

        client = SimpleNamespace(
            infisical_project_id=lambda env, root: env["INFISICAL_FLEET_OPS_PROJECT_ID"],
            SecretsError=RuntimeError,
        )
        reader = mock.Mock(return_value={"FIXTURE": "synthetic-result"})
        with (
            mock.patch.object(A.os, "open", side_effect=opening),
            mock.patch.object(A.os, "fstat", side_effect=lambda fd: modes[fd]),
            mock.patch.object(A.os, "getxattr", side_effect=getacl),
            mock.patch.object(A.os, "close") as close,
            mock.patch.object(A.os, "stat", return_value=modes[14]),
            mock.patch.object(
                A.os, "fdopen", side_effect=lambda fd, mode: Stream(b"synthetic-systemd-fixture")
            ),
            mock.patch.object(A, "_shared_client", return_value=client),
        ):
            if consumer:
                source = A.FleetSecretsSource(
                    path=scope,
                    credential_file=path,
                    environ=dict(
                        env, INFISICAL_FLEET_OPS_PROJECT_ID="11111111-1111-1111-1111-111111111111"
                    ),
                    reader=reader,
                )
                self.assertEqual(source._token(), "synthetic-systemd-fixture")
                opened.clear()
                self.assertEqual(source._read(["FIXTURE"]), {"FIXTURE": "synthetic-result"})
                self.assertEqual(reader.call_count, 1)
                self.assertEqual(reader.call_args.args[1], scope)
                return
            result = A._open_systemd_source(path, scope, env)
        self.assertEqual(result, 14)
        self.assertEqual([c.args[0] for c in close.call_args_list], [13, 12, 11, 10])
        self.assertEqual(
            [row[1] for row in opened],
            ["/", "run", "credentials", "git-relay.service", path.rsplit("/", 1)[1]],
        )
        for _, _, flags, _ in opened[1:]:
            self.assertTrue(flags & os.O_NOFOLLOW)
        return result

    def test_exact_systemd_acl_boundary_for_each_declared_source(self):
        for scope, name in A._SYSTEMD_SOURCE_NAMES.items():
            with self.subTest(scope=scope):
                self.run_open(scope=scope, path=D + "/" + name)

    def test_full_token_and_read_accept_exact_boundary_for_every_scope(self):
        for scope, name in A._SYSTEMD_SOURCE_NAMES.items():
            with self.subTest(scope=scope):
                self.run_open(scope=scope, path=D + "/" + name, consumer=True)

    def test_full_consumer_refuses_bad_scope_environment_path_and_acl(self):
        for over in (
            {"scope": "/github-apps"},
            {"env": {}},
            {"path": D + "/git-relay-apps-token"},
            {"bad_acl": acl(7)},
            {"bad_acl": acl(4, uid=os.geteuid() + 1)},
            {"changes": {14: info(stat.S_IFREG | 0o444)}},
        ):
            with self.subTest(over=over), self.assertRaises(A.AppTokenError):
                self.run_open(consumer=True, **over)

    def test_unbound_other_scope_or_noncanonical_path_refused(self):
        for over in (
            {"env": {}},
            {"env": {"CREDENTIALS_DIRECTORY": "/tmp/credentials"}},
            {"path": D + "/git-relay-apps-token"},
            {"scope": "/other"},
            {"path": D + "/../git-relay.service/git-relay-tools-token"},
        ):
            with self.subTest(over=over), self.assertRaises(ValueError):
                self.run_open(**over)

    def test_bad_parent_file_and_acl_refused(self):
        for fd, value in (
            (11, info(stat.S_IFDIR | 0o777)),
            (12, info(stat.S_IFDIR | 0o755, st_uid=10)),
            (13, info(stat.S_IFDIR | 0o755)),
            (14, info(stat.S_IFREG | 0o444)),
            (14, info(stat.S_IFREG | 0o440, st_gid=5)),
            (14, info(stat.S_IFREG | 0o440, st_uid=9)),
            (14, info(stat.S_IFREG | 0o440, st_nlink=2)),
            (14, info(stat.S_IFREG | 0o440, st_size=9000)),
        ):
            with self.subTest(fd=fd, value=value), self.assertRaises(ValueError):
                self.run_open(changes={fd: value})
        for raw in (b"", acl(7), acl(4, uid=os.geteuid() + 1)):
            with self.subTest(raw_length=len(raw)), self.assertRaises(ValueError):
                self.run_open(bad_acl=raw)
        with self.assertRaises(ValueError):
            self.run_open(open_error="credentials")

    def test_arbitrary_group_readable_file_stays_refused_private_file_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "credential")
            with open(path, "w") as f:
                f.write("synthetic-local-fixture")
            source = A.FleetSecretsSource(path="/tools", credential_file=path, environ={})
            os.chmod(path, 0o640)
            with self.assertRaises(A.AppTokenError):
                source._token()
            os.chmod(path, 0o600)
            self.assertEqual(source._token(), "synthetic-local-fixture")


if __name__ == "__main__":
    unittest.main()
