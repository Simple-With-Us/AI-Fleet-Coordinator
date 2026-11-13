import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_relay.policy import PolicyError, SeatPolicy
from git_relay.policy_input import MAX_POLICY_BYTES, seats_json_from_env


class FilePolicyInput(unittest.TestCase):
    def private(self, folder, raw):
        path = Path(folder) / "seats.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        return path

    def test_inline_is_unchanged_including_absent_and_empty(self):
        for value in ("", "not json", '{"seat":{}}'):
            self.assertEqual(seats_json_from_env({"GIT_RELAY_SEATS_JSON": value}), value)
        self.assertEqual(seats_json_from_env({}), "")

    def test_exact_bytes_and_policy_semantics_for_40_large_seats(self):
        raw = {
            f"swu-seat-{n}": {
                "token": "synthetic-" + str(n) + "x" * 32,
                "actions": ["open_pr", "wf_pr_list_page"],
                "repos": ["o/r"],
                "app_slug": f"swu-seat-{n}",
                "mint_permissions": {"metadata": "read", "pull_requests": "read"},
                "unused": "synthetic padding " * 300,
            }
            for n in range(40)
        }
        text = json.dumps(raw, ensure_ascii=False, indent=2)
        self.assertGreater(len(text.encode()), 131072)
        with tempfile.TemporaryDirectory() as tmp:
            path = self.private(tmp, text.encode())
            loaded = seats_json_from_env({"GIT_RELAY_SEATS_FILE": str(path)})
            self.assertEqual(loaded, text)
            inline, file = SeatPolicy(text), SeatPolicy(loaded)
            self.assertEqual(inline._seats, file._seats)
            self.assertEqual(len(file._seats), 40)

    def test_ambiguity_empty_relative_missing_and_safe_errors(self):
        for env in (
            {"GIT_RELAY_SEATS_FILE": "/missing", "GIT_RELAY_SEATS_JSON": ""},
            {"GIT_RELAY_SEATS_FILE": ""},
            {"GIT_RELAY_SEATS_FILE": "relative"},
            {"GIT_RELAY_SEATS_FILE": "/synthetic-private-path-missing"},
        ):
            with self.assertRaises(PolicyError) as error:
                seats_json_from_env(env)
            self.assertNotIn("synthetic-private-path", str(error.exception))

    def test_private_regular_file_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.private(tmp, b"{}")
            env = {"GIT_RELAY_SEATS_FILE": str(path)}
            for mode in (0o644, 0o640, 0o610, 0o700, 0o4600):
                path.chmod(mode)
                with self.assertRaises(PolicyError):
                    seats_json_from_env(env)
            path.chmod(0o400)
            self.assertEqual(seats_json_from_env(env), "{}")
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(PolicyError):
                seats_json_from_env({"GIT_RELAY_SEATS_FILE": str(link)})
            with self.assertRaises(PolicyError):
                seats_json_from_env({"GIT_RELAY_SEATS_FILE": tmp})
            fifo = Path(tmp) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(PolicyError):
                seats_json_from_env({"GIT_RELAY_SEATS_FILE": str(fifo)})

    def test_empty_oversize_invalid_utf8_refuse_without_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            for raw in (b"", b"x" * (MAX_POLICY_BYTES + 1), b"\xffsynthetic-private-data"):
                path = self.private(tmp, raw)
                with self.assertRaises(PolicyError) as error:
                    seats_json_from_env({"GIT_RELAY_SEATS_FILE": str(path)})
                self.assertNotIn("synthetic-private-data", str(error.exception))

    def test_validation_diagnostics_remain_specific_without_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.private(tmp, b"{}")
            path.chmod(0o644)
            with self.assertRaisesRegex(PolicyError, "must be private, owned and bounded") as error:
                seats_json_from_env({"GIT_RELAY_SEATS_FILE": str(path)})
            self.assertNotIn(tmp, str(error.exception))
            path.chmod(0o600)
            with patch("git_relay.policy_input.os.open", side_effect=ValueError("private-path")):
                with self.assertRaisesRegex(PolicyError, "could not be safely read") as error:
                    seats_json_from_env({"GIT_RELAY_SEATS_FILE": str(path)})
                self.assertNotIn("private-path", str(error.exception))

    def test_foreign_owner_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.private(tmp, b"{}")
            actual = os.stat(path)
            foreign = os.stat_result(
                (
                    actual.st_mode,
                    actual.st_ino,
                    actual.st_dev,
                    actual.st_nlink,
                    os.geteuid() + 12345,
                    actual.st_gid,
                    actual.st_size,
                    actual.st_atime,
                    actual.st_mtime,
                    actual.st_ctime,
                )
            )
            with (
                patch("git_relay.policy_input.os.fstat", return_value=foreign),
                self.assertRaises(PolicyError),
            ):
                seats_json_from_env({"GIT_RELAY_SEATS_FILE": str(path)})

    def test_production_from_env_file_policy_and_safe_failures(self):
        from git_relay import server

        policy = json.dumps(
            {"synthetic": {"token": "x" * 32, "actions": ["open_pr"], "repos": ["o/r"]}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.private(tmp, policy.encode())
            env = {
                "GIT_RELAY_SEATS_FILE": str(path),
                "GIT_RELAY_APP_MINT": "0",
                "GIT_RELAY_AUDIT_PATH": tmp + "/audit.jsonl",
                "GIT_RELAY_OUTCOMES_PATH": tmp + "/outcomes.json",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(server, "_owner_token_from_cli", return_value="x" * 40),
            ):
                relay = server.from_env()
                self.assertEqual(relay.policy._seats, SeatPolicy(policy)._seats)
            for raw in (b"{synthetic-private-data", b'{"synthetic-private-seat":{}}'):
                path.write_bytes(raw)
                with (
                    patch.dict(os.environ, env, clear=True),
                    self.assertRaises(SystemExit) as error,
                ):
                    server.from_env()
                self.assertNotIn("synthetic-private", str(error.exception))
                self.assertEqual(
                    str(error.exception), "git-relay config: file seat policy invalid or unreadable"
                )
            with (
                patch.dict(os.environ, {**env, "GIT_RELAY_SEATS_JSON": policy}, clear=True),
                self.assertRaises(SystemExit),
            ):
                server.from_env()
