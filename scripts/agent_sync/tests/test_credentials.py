"""Credential order, file-mode and realm checks, seat resolution, and key secrecy."""
from __future__ import annotations

import json
import os
import secrets
import unittest

from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.harness import TAG, Harness, write_rc


class CredentialOrderTests(Harness):
    def credential_used(self, *argv: str, env: dict[str, str] | None = None) -> str:
        result = self.run_cli("whoami", "--json", *argv, env=env)
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)["credential"]

    def test_rc_flag_beats_everything(self) -> None:
        flag_rc = write_rc(self.tmp / "flag-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        env_rc = write_rc(self.tmp / "env-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        env = self.env(ZULIP_RC=str(env_rc), ZULIP_EMAIL=BOT_EMAIL, ZULIP_API_KEY=self.key, ZULIP_SITE=self.fake.url)
        self.assertEqual(self.credential_used("--rc", str(flag_rc), env=env), str(flag_rc))

    def test_zulip_rc_env_beats_secrets_dir_and_triple(self) -> None:
        env_rc = write_rc(self.tmp / "env-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        env = self.env(ZULIP_RC=str(env_rc), ZULIP_EMAIL=BOT_EMAIL, ZULIP_API_KEY=self.key, ZULIP_SITE=self.fake.url)
        self.assertEqual(self.credential_used(env=env), str(env_rc))

    def test_secrets_dir_file_beats_env_triple(self) -> None:
        env = self.env(ZULIP_EMAIL=BOT_EMAIL, ZULIP_API_KEY=self.key, ZULIP_SITE=self.fake.url)
        self.assertEqual(self.credential_used(env=env), str(self.secrets_dir / "Claude-zuliprc"))

    def test_env_triple_is_the_last_resort(self) -> None:
        self.rc_path.unlink()
        env = self.env(ZULIP_EMAIL=BOT_EMAIL, ZULIP_API_KEY=self.key, ZULIP_SITE=self.fake.url)
        self.assertEqual(self.credential_used(env=env), "env")

    def test_partial_triple_is_not_merged_with_anything(self) -> None:
        self.rc_path.unlink()
        env = self.env(ZULIP_EMAIL=BOT_EMAIL, ZULIP_SITE=self.fake.url)
        result = self.run_cli("whoami", env=env)
        self.assertEqual(result.code, 3)
        self.assertIn("ZULIP_API_KEY", result.err)

    def test_no_credentials_anywhere(self) -> None:
        self.rc_path.unlink()
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("Claude-zuliprc", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_explicit_rc_that_is_missing_does_not_fall_through(self) -> None:
        result = self.run_cli("whoami", "--rc", str(self.tmp / "nope-rc"))
        self.assertEqual(result.code, 3)
        self.assertIn("nope-rc", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_dotfiles_in_cwd_and_home_are_never_read(self) -> None:
        self.rc_path.unlink()
        write_rc(self.home / ".zuliprc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        previous = os.getcwd()
        os.chdir(self.tmp)
        try:
            write_rc(self.tmp / "zuliprc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
            (self.tmp / ".env").write_text("ZULIP_API_KEY=ignored\n")
            result = self.run_cli("whoami")
        finally:
            os.chdir(previous)
        self.assertEqual(result.code, 3)
        self.assertEqual(self.fake.requests, [])

    def test_site_with_trailing_slash_and_rc_in_home_style_path(self) -> None:
        rc = write_rc(self.tmp / "slash-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url + "/")
        self.assertEqual(self.credential_used("--rc", str(rc)), str(rc))


class SeatTests(Harness):
    def test_no_seat_and_no_rc_exits_3_with_the_instruction(self) -> None:
        result = self.run_cli("whoami", env=self.env(AGENT_SEAT=None))
        self.assertEqual(result.code, 3)
        self.assertIn("set AGENT_SEAT (e.g. CLAUDE)", result.err)
        self.assertIn("or pass --rc", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_agent_tag_is_the_fallback_and_is_upper_cased(self) -> None:
        result = self.run_cli("whoami", "--json", env=self.env(AGENT_SEAT=None, AGENT_TAG="claude"))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out)["seat"], "CLAUDE")

    def other_bot(self, email: str, name: str, file_name: str) -> None:
        """That bot's own key and rc file (a seat's key must be its own bot's:  identity.check_bot)."""
        key = secrets.token_hex(16)
        self.other_keys.append(key)
        self.fake.add_bot(email, name, key)
        write_rc(self.secrets_dir / file_name, email=email, key=key, site=self.fake.url)

    def setUp(self) -> None:
        super().setUp()
        self.other_keys: list[str] = []

    def tearDown(self) -> None:
        super().tearDown()
        for key in self.other_keys:
            self.assertNotIn(key, "\n".join(self.transcript))

    def test_as_flag_beats_env(self) -> None:
        self.other_bot("codex-bot@zulip.test", "Codex", "Codex-zuliprc")
        result = self.run_cli("whoami", "--json", "--as", "codex")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual(info["seat"], "CODEX")
        self.assertTrue(info["credential"].endswith("Codex-zuliprc"))

    def test_flags_work_before_the_command_too(self) -> None:
        result = self.run_cli("--json", "--as", "claude", "whoami")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out)["seat"], "CLAUDE")

    def test_seat_with_a_path_in_it_is_refused(self) -> None:
        result = self.run_cli("whoami", "--as", "../x")
        self.assertEqual(result.code, 2)

    def test_seat_is_derived_from_the_rc_file_name_when_none_is_set(self) -> None:
        result = self.run_cli("whoami", "--json", "--rc", str(self.rc_path), env=self.env(AGENT_SEAT=None))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out)["seat"], "CLAUDE")

    def test_credential_file_names(self) -> None:
        for seat, expected in (("CLAUDE", "Claude-zuliprc"), ("claude", "Claude-zuliprc"),
                               ("GB-COMPILER", "GB-Compiler-zuliprc"), ("CODEX-2", "Codex-2-zuliprc"),
                               ("OPENCODE", "OpenCode-zuliprc"), ("opencode", "OpenCode-zuliprc")):
            with self.subTest(seat=seat):
                self.assertEqual(Z.credential_file_name(seat), expected)

    def test_credential_file_names_match_what_the_provisioning_script_writes(self) -> None:
        # The real credential file names on the owner's Mac, 2026-10-07 (names only).  The rule must produce every one.
        written = {
            "CLAUDE": "Claude", "CODEX": "Codex", "AG": "AG", "CURSOR": "Cursor", "GROK": "Grok-Build",
            "GROK-WEB": "Grok-Web", "CLUTCH": "Clutch", "FX": "FX", "MM": "MM", "MC": "MC", "MA": "MA",
            "OPENCODE": "OpenCode", "ECHO": "Echo", "INSTINCT": "Instinct", "GB-COMPILER": "GB-Compiler",
            "BF-BUILDER": "BF-Builder", "BF-DEPLOYER": "BF-Deployer", "BF-DESIGNER": "BF-Designer",
            "BF-FIXER": "BF-Fixer", "BF-HOUSEKEEPER": "BF-Housekeeper",
            "BF-MONITOR": "BF-Monitor", "BF-ORACLE": "BF-Oracle", "BF-PLUMBER": "BF-Plumber",
            "BF-PUBLISHER": "BF-Publisher", "BF-COMPILER": "BF-Compiler",
        }
        for seat, stem in written.items():
            with self.subTest(seat=seat):
                self.assertEqual(Z.credential_file_name(seat), stem + "-zuliprc")
                self.assertEqual(Z.seat_from_rc_path("/x/" + stem + "-zuliprc"), seat)  # and back again

    def test_a_seat_with_an_override_finds_its_file_in_the_secrets_dir(self) -> None:
        self.other_bot("mm-bot@zulip.test", "MiniMax", "MM-zuliprc")
        result = self.run_cli("whoami", "--json", "--as", "mm")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual(info["seat"], "MM")
        self.assertTrue(info["credential"].endswith("MM-zuliprc"))

    def test_session_tag(self) -> None:
        self.assertEqual(Z.session_tag("ABCDEF12-3456-7890-abcd-ef1234567890"), "abcdef12")
        self.assertEqual(Z.session_tag("ab-cd"), "abcd")
        self.assertEqual(Z.session_tag("../../etc"), "etc")
        self.assertIsNone(Z.session_tag(""))
        self.assertIsNone(Z.session_tag(None))
        self.assertIsNone(Z.session_tag("///"))

    def test_session_precedence_flag_then_claude_then_agent_session(self) -> None:
        def tag(*argv: str, **env: str | None) -> str | None:
            result = self.run_cli("whoami", "--json", *argv, env=self.env(**env))
            self.assertEqual(result.code, 0, result.err)
            return json.loads(result.out)["session"]

        self.assertEqual(tag("--session", "aaaaaaaa-1"), "aaaaaaaa")
        self.assertEqual(tag(), TAG)
        self.assertEqual(tag(CLAUDE_CODE_SESSION_ID=None, AGENT_SESSION="bbbbbbbb-2"), "bbbbbbbb")
        self.assertIsNone(tag(CLAUDE_CODE_SESSION_ID=None))


class RefusalTests(Harness):
    def test_group_or_world_accessible_rc_is_refused(self) -> None:
        for mode in (0o644, 0o640, 0o604, 0o660):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.rc_path, mode)
                result = self.run_cli("whoami")
                self.assertEqual(result.code, 3)
                self.assertIn("chmod 600", result.err)
                self.assertIn(str(self.rc_path), result.err)
                self.assertEqual(self.fake.requests, [])

    def test_foreign_site_host_is_refused_before_any_request(self) -> None:
        write_rc(self.rc_path, email=BOT_EMAIL, key=self.key, site="https://evil.example.test")
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("evil.example.test", result.err)
        self.assertIn("does not match", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_same_host_other_port_is_refused(self) -> None:
        host = self.fake.url.rsplit(":", 1)[0]
        write_rc(self.rc_path, email=BOT_EMAIL, key=self.key, site=host + ":1")
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertEqual(self.fake.requests, [])

    def test_plain_http_to_a_real_host_is_refused(self) -> None:
        write_rc(self.rc_path, email=BOT_EMAIL, key=self.key, site="http://zulip.example.test")
        result = self.run_cli("whoami", env=self.env(AGENT_SYNC_REALM="http://zulip.example.test"))
        self.assertEqual(result.code, 3)
        self.assertIn("https", result.err)

    def test_malformed_rc_never_echoes_its_content(self) -> None:
        self.rc_path.write_text("this line is not ini and holds %s\n" % self.key)
        os.chmod(self.rc_path, 0o600)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn(str(self.rc_path), result.err)
        self.assertNotIn(self.key, result.out + result.err)

    def test_rc_with_a_percent_in_the_key_parses(self) -> None:
        write_rc(self.rc_path, email=BOT_EMAIL, key=self.key + "%s", site=self.fake.url)
        result = self.run_cli("whoami")
        # the key is wrong for the fake server, so this is a 401, not a parse error
        self.assertEqual(result.code, 5)

    def test_rc_missing_fields_names_them(self) -> None:
        self.rc_path.write_text("[api]\nemail = %s\n" % BOT_EMAIL)
        os.chmod(self.rc_path, 0o600)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("key", result.err)
        self.assertIn("site", result.err)

    def test_rc_without_api_section(self) -> None:
        self.rc_path.write_text("[other]\nx = 1\n")
        os.chmod(self.rc_path, 0o600)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("[api]", result.err)


class SecrecyTests(Harness):
    def test_key_is_absent_everywhere_across_commands_and_error_paths(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "topic a", "hello @**Claude**")
        self.fake.echo_auth = True  # the server echoes the Authorization header into its errors
        commands = [
            ("whoami",), ("whoami", "--json"), ("channels",), ("topics",), ("read", "--topic", "topic a"),
            ("read", "--topic", "topic a", "--json"), ("inbox", "--peek"), ("post", "--topic", "topic a", "hi"),
            ("react", "--id", "999999", "eyes"), ("react", "--id", "1", "eyes"),  # server errors that echo auth
            ("resolve", "--topic", "no such topic"), ("topics", "--channel", "nonexistent"),
            ("post", "--topic", "x" * 61, "hi"), ("post", "hi"), ("whoami", "--rc", str(self.tmp / "missing")),
            ("wait", "--topic", "topic a", "--timeout", "0.3"),
        ]
        for argv in commands:
            self.run_cli(*argv)
        bad = write_rc(self.tmp / "bad-rc", email=BOT_EMAIL, key=self.key + "x", site=self.fake.url)
        self.run_cli("whoami", "--rc", str(bad))
        result = self.run_cli("react", "--id", "999999", "eyes")
        self.assertEqual(result.code, 5)
        self.assertIn("[redacted]", result.err)
        # tearDown asserts that neither the key nor its base64 form is anywhere in the transcript

    def test_credentials_repr_hides_the_key(self) -> None:
        creds = Z.Credentials(email="a@b.test", key=self.key, site="https://x.test", source="env")
        self.assertNotIn(self.key, repr(creds))
        self.assertNotIn(self.key, str(creds))

    def test_unexpected_exception_is_reported_without_a_traceback_or_key(self) -> None:
        from unittest import mock
        from agent_sync import cli

        with mock.patch.object(cli, "cmd_whoami", side_effect=RuntimeError("boom " + self.key)):
            with mock.patch.dict(cli.COMMANDS, {"whoami": cli.cmd_whoami}):
                result = self.run_cli("whoami")
        self.assertEqual(result.code, 1)
        self.assertIn("internal error", result.err)
        self.assertNotIn("Traceback", result.err)


if __name__ == "__main__":
    unittest.main()
