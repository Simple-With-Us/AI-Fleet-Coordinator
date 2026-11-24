import stat
import unittest
from types import SimpleNamespace as S

from native_path_custody import (
    local_nss_complete,
    status_holds_foreign_group,
    validate_paths,
)


class PathCustody(unittest.TestCase):
    def setUp(self):
        self.mode = stat.S_IFDIR | 0o775
        self.g = S(gr_mem=[])
        self.users = [
            S(pw_name="jay", pw_uid=1001, pw_gid=1001),
            S(pw_name="other", pw_uid=1002, pw_gid=1002),
        ]
        self.acl = "user::rwx\ngroup::rwx\nother::r-x\n"
        self.h = []

    def runcheck(self):
        return validate_paths(
            ["/fixture"],
            1001,
            lstat=lambda _: S(st_mode=self.mode, st_uid=1001, st_gid=1001),
            accounts=lambda: self.users,
            group=lambda _: self.g,
            groups=lambda n, g: [1001] if n == "jay" else [1002],
            acl=lambda _: self.acl,
            holders=lambda _: self.h,
        )

    def test_owner_private_group_allowed(self):
        self.assertTrue(self.runcheck()["ok"])

    def test_world_write_refused(self):
        self.mode |= 2
        self.assertRaises(ValueError, self.runcheck)

    def test_symlink_refused(self):
        self.mode = stat.S_IFLNK | 0o775
        self.assertRaises(ValueError, self.runcheck)

    def test_foreign_supplementary_refused(self):
        self.g.gr_mem = ["other"]
        self.assertRaises(ValueError, self.runcheck)

    def test_unknown_supplementary_refused(self):
        self.g.gr_mem = ["unknown"]
        self.assertRaises(ValueError, self.runcheck)

    def test_foreign_primary_refused(self):
        self.users[1].pw_gid = 1001
        self.assertRaises(
            ValueError,
            lambda: validate_paths(
                ["/fixture"],
                1001,
                lstat=lambda _: S(st_mode=self.mode, st_uid=1001, st_gid=1001),
                accounts=lambda: self.users,
                group=lambda _: self.g,
                groups=lambda n, g: [g],
                acl=lambda _: self.acl,
                holders=lambda _: [],
            ),
        )

    def test_extended_acl_refused(self):
        self.acl += "user:other:rwx\n"
        self.assertRaises(ValueError, self.runcheck)

    def test_default_acl_refused(self):
        self.acl += "default:user::rwx\n"
        self.assertRaises(ValueError, self.runcheck)

    def test_acl_error_refused(self):
        def bad(_):
            raise OSError("acl-unavailable")

        self.assertRaises(
            OSError,
            lambda: validate_paths(
                ["/fixture"],
                1001,
                lstat=lambda _: S(st_mode=self.mode, st_uid=1001, st_gid=1001),
                acl=bad,
            ),
        )

    def test_foreign_process_refused(self):
        self.h = ["123"]
        self.assertRaises(ValueError, self.runcheck)


if __name__ == "__main__":
    unittest.main()


class ProcessGroups(unittest.TestCase):
    def status(self, gids, groups="1002", uids="1002 1002 1002 1002"):
        return "Uid:\t" + uids + "\nGid:\t" + gids + "\nGroups:\t" + groups + "\n"

    def test_all_primary_gid_fields(self):
        for i in range(4):
            gids = ["1002"] * 4
            gids[i] = "1001"
            self.assertTrue(
                status_holds_foreign_group(self.status(" ".join(gids)), 1001, 1001)
            )

    def test_supplementary_only(self):
        self.assertTrue(
            status_holds_foreign_group(self.status("1002 " * 4, "1001"), 1001, 1001)
        )

    def test_owner_allowed(self):
        self.assertFalse(
            status_holds_foreign_group(
                self.status("1001 " * 4, "", "1001 " * 4), 1001, 1001
            )
        )

    def test_malformed_missing_duplicate(self):
        for data in [
            "Uid: 1002",
            "Uid: 1002 1002 1002 1002\nGid: x x x x\nGroups: 1001",
            self.status("1001 " * 4) + "Gid: 1001 1001 1001 1001",
        ]:
            self.assertRaises(ValueError, status_holds_foreign_group, data, 1001, 1001)

    def test_qualified_nss(self):
        self.assertTrue(
            local_nss_complete("passwd: files systemd\ngroup: files systemd")
        )

    def test_unqualified_nss(self):
        for text in [
            "passwd: sss\ngroup: files",
            "passwd: files",
            "passwd: files ldap\ngroup: files",
            "passwd: files\ngroup: files [SUCCESS=return] systemd",
        ]:
            self.assertRaises(ValueError, local_nss_complete, text)
