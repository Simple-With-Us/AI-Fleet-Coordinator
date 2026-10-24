"""Credential order, file-mode and realm checks, seat resolution, and key secrecy."""

from __future__ import annotations

import json
import os
import secrets
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.harness import TAG, Harness, write_rc

FAKE_FLEET_SECRETS = Path(__file__).resolve().parent / "fixtures" / "fake-fleet-secrets"
FAKE_PROJECT_ID = "11111111-2222-3333-4444-555555555555"
OTHER_FAKE_PROJECT_ID = "66666666-7777-8888-9999-000000000000"


class CredentialOrderTests(Harness):
    def credential_used(self, *argv: str, env: dict[str, str] | None = None) -> str:
        result = self.run_cli("whoami", "--json", *argv, env=env)
        self.assertEqual(result.code, 0, result.err)
        return json.loads(result.out)["credential"]

    def test_rc_flag_beats_everything(self) -> None:
        flag_rc = write_rc(self.tmp / "flag-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        env_rc = write_rc(self.tmp / "env-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        env = self.env(
            ZULIP_RC=str(env_rc),
            ZULIP_EMAIL=BOT_EMAIL,
            ZULIP_API_KEY=self.key,
            ZULIP_SITE=self.fake.url,
        )
        self.assertEqual(self.credential_used("--rc", str(flag_rc), env=env), str(flag_rc))

    def test_zulip_rc_env_beats_secrets_dir_and_triple(self) -> None:
        env_rc = write_rc(self.tmp / "env-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url)
        env = self.env(
            ZULIP_RC=str(env_rc),
            ZULIP_EMAIL=BOT_EMAIL,
            ZULIP_API_KEY=self.key,
            ZULIP_SITE=self.fake.url,
        )
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
        rc = write_rc(
            self.tmp / "slash-rc", email=BOT_EMAIL, key=self.key, site=self.fake.url + "/"
        )
        self.assertEqual(self.credential_used("--rc", str(rc)), str(rc))


class InfisicalFallbackTests(Harness):
    """OPS-818:  with no zuliprc and no env triple, the seat's own three Fleet-OPS /zulip keys are the last source."""

    def seed(self, prefix: str = "CLAUDE") -> None:
        self.infisical_values = {
            "%s_ZULIP_EMAIL" % prefix: BOT_EMAIL,
            "%s_ZULIP_KEY" % prefix: self.key,
            "%s_ZULIP_SITE" % prefix: self.fake.url,
            # Another seat's keys in the same folder:  never asked for, never used.
            "CODEX_ZULIP_KEY": "other-seat-key-should-never-be-requested",
        }

    def test_file_present_wins_and_infisical_is_not_asked(self) -> None:
        self.seed()
        result = self.run_cli("whoami", "--json")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out)["credential"], str(self.rc_path))
        self.assertEqual(self.infisical_calls, [])

    def test_file_absent_uses_infisical_for_this_seat_only(self) -> None:
        self.rc_path.unlink()
        self.seed()
        result = self.run_cli("whoami", "--json")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual(info["seat"], "CLAUDE")
        self.assertEqual(info["credential"], "infisical Fleet-OPS /zulip CLAUDE_ZULIP_KEY")
        self.assertEqual(
            self.infisical_calls, [("CLAUDE_ZULIP_EMAIL", "CLAUDE_ZULIP_KEY", "CLAUDE_ZULIP_SITE")]
        )
        self.assertNotIn(self.key, result.out + result.err)

    def test_env_triple_beats_infisical(self) -> None:
        self.rc_path.unlink()
        self.seed()
        env = self.env(ZULIP_EMAIL=BOT_EMAIL, ZULIP_API_KEY=self.key, ZULIP_SITE=self.fake.url)
        result = self.run_cli("whoami", "--json", env=env)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out)["credential"], "env")
        self.assertEqual(self.infisical_calls, [])

    def test_explicit_rc_that_is_missing_never_reaches_infisical(self) -> None:
        self.seed()
        result = self.run_cli("whoami", "--rc", str(self.tmp / "nope-rc"))
        self.assertEqual(result.code, 3)
        self.assertEqual(self.infisical_calls, [])

    def test_opt_out_knob(self) -> None:
        self.rc_path.unlink()
        self.seed()
        result = self.run_cli("whoami", env=self.env(AGENT_SYNC_INFISICAL="0"))
        self.assertEqual(result.code, 3)
        self.assertIn("Claude-zuliprc", result.err)
        self.assertEqual(self.infisical_calls, [])

    def test_infisical_failure_fails_closed_naming_folder_and_keys_never_a_value(self) -> None:
        self.rc_path.unlink()
        self.seed()
        self.infisical_error = Z.CredentialError("infisical exit-1")
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        for expected in (
            "Fleet-OPS",
            "/zulip",
            "CLAUDE_ZULIP_EMAIL",
            "CLAUDE_ZULIP_KEY",
            "CLAUDE_ZULIP_SITE",
            "Claude-zuliprc",
            "infisical exit-1",
        ):
            self.assertIn(expected, result.err)
        self.assertNotIn(self.key, result.err)
        self.assertNotIn("other-seat-key", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_unexpected_fetch_exception_is_reduced_to_its_type(self) -> None:
        self.rc_path.unlink()
        self.infisical_error = RuntimeError("boom " + self.key)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("RuntimeError", result.err)
        self.assertNotIn(self.key, result.err)

    def test_a_missing_key_is_named_and_values_are_not(self) -> None:
        self.rc_path.unlink()
        self.seed()
        del self.infisical_values["CLAUDE_ZULIP_SITE"]
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("CLAUDE_ZULIP_SITE", result.err)
        self.assertNotIn(self.key, result.err)

    def test_the_cli_not_found_placeholder_is_a_missing_key(self) -> None:
        # The real fetch drops "*not found*" (fleet_knobs.source); a fetch that lets it through is still refused here.
        self.rc_path.unlink()
        self.seed()
        self.infisical_values["CLAUDE_ZULIP_SITE"] = "*not found*"
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("not set: CLAUDE_ZULIP_SITE", result.err)

    def test_a_key_with_a_control_character_is_refused_without_echoing_it(self) -> None:
        self.rc_path.unlink()
        self.seed()
        self.infisical_values["CLAUDE_ZULIP_KEY"] = self.key + "\nextra"
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("Infisical Fleet-OPS /zulip (prod) keys CLAUDE_ZULIP_EMAIL", result.err)
        self.assertIn("CLAUDE_ZULIP_KEY holds a control character", result.err)
        self.assertNotIn(self.key, result.err)

    def test_the_bot_check_still_applies_to_infisical_credentials(self) -> None:
        self.rc_path.unlink()
        self.seed()
        self.infisical_values["CLAUDE_ZULIP_EMAIL"] = "someone-else-bot@zulip.test"
        result = self.run_cli("whoami")
        self.assertNotEqual(
            result.code, 0
        )  # the server rejects the pair, or the seat/bot check does
        self.assertNotIn(self.key, result.out + result.err)

    def test_project_id_comes_from_env_then_the_home_file_and_fails_closed(self) -> None:
        fake_id = "11111111-2222-3333-4444-555555555555"
        other_id = "66666666-7777-8888-9999-000000000000"
        with self.assertRaises(Z.CredentialError) as ctx:
            Z.infisical_project_id({}, self.home)
        self.assertIn("INFISICAL_FLEET_OPS_PROJECT_ID", str(ctx.exception))
        self.assertIn("fleet-ops-project-id", str(ctx.exception))
        (self.home / ".agent-sync").mkdir()
        (self.home / ".agent-sync" / "fleet-ops-project-id").write_text(other_id + "\n")
        self.assertEqual(Z.infisical_project_id({}, self.home), other_id)
        self.assertEqual(
            Z.infisical_project_id({"INFISICAL_FLEET_OPS_PROJECT_ID": fake_id}, self.home), fake_id
        )
        with self.assertRaises(Z.CredentialError) as ctx:
            Z.infisical_project_id(
                {"INFISICAL_FLEET_OPS_PROJECT_ID": "not-a-uuid-secret-looking"}, self.home
            )
        self.assertNotIn("secret-looking", str(ctx.exception))

    def fake_fleet_secrets(self, folder: dict, *, binary: Path | None = None) -> dict:
        """An env whose FLEET_SECRETS_BIN is the fixture fake, serving `folder` as /zulip."""
        data = self.tmp / "fake-fleet-secrets.json"
        data.write_text(json.dumps({"/zulip": folder}), encoding="utf-8")
        for cache in (Z._value_cache, Z._names_cache, Z._failures):
            cache.clear()
            self.addCleanup(cache.clear)
        return {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            # the real binary resolves its project from the env or the home file and fails closed
            # without one;  the cache keys the fetches per project, so every env here names one.
            Z.ENV_INFISICAL_PROJECT: FAKE_PROJECT_ID,
            "FLEET_SECRETS_BIN": str(binary or FAKE_FLEET_SECRETS),
            "FAKE_FLEET_SECRETS_FILE": str(data),
        }

    def test_default_fetch_asks_fleet_secrets_for_only_these_names_in_one_call(self) -> None:
        env = self.fake_fleet_secrets({"A": "1", "B": "2", "C": "3", "OTHER": "never"})
        self.assertEqual(
            Z._fetch_from_fleet_secrets(("A", "B", "C"), env, self.home),
            {"A": "1", "B": "2", "C": "3"},
        )

    def test_default_fetch_is_cached_per_process_and_keeps_the_last_good_values(self) -> None:
        env = self.fake_fleet_secrets({"A": "1", "B": "2"})
        self.assertEqual(
            Z._fetch_from_fleet_secrets(("A", "B"), env, self.home), {"A": "1", "B": "2"}
        )
        Path(env["FAKE_FLEET_SECRETS_FILE"]).write_text(
            "{}", encoding="utf-8"
        )  # Infisical now fails
        self.assertEqual(
            Z._fetch_from_fleet_secrets(("A", "B"), env, self.home), {"A": "1", "B": "2"}
        )
        with mock.patch.object(
            Z, "INFISICAL_CACHE_TTL", 0.0
        ):  # stale:  refetch fails, the held values win
            self.assertEqual(
                Z._fetch_from_fleet_secrets(("A", "B"), env, self.home), {"A": "1", "B": "2"}
            )
        with self.assertRaises(
            Z.CredentialError
        ) as ctx:  # never held:  fails closed with a reason token
            Z._fetch_from_fleet_secrets(("C",), env, self.home)
        self.assertIn("key-absent", str(ctx.exception))

    def test_default_fetch_failure_names_a_reason_never_output(self) -> None:
        env = self.fake_fleet_secrets({})
        env["FLEET_SECRETS_BIN"] = str(self.tmp / "no-such-fleet-secrets")
        with self.assertRaises(Z.CredentialError) as ctx:
            Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("cannot-start", str(ctx.exception))

    def test_names_listing_never_offers_the_owner_the_realm_admin_or_an_integration_bot(
        self,
    ) -> None:
        folder = {}
        for prefix in (
            "CLAUDE",
            "BF_PLUMBER",
            "GROK_BUILD",
            "JAY",
            "REALM_ADMIN",
            "SENTRY",
            "LINEAR",
            "HALF",
        ):
            for part in ("EMAIL", "KEY", "SITE"):
                if not (prefix == "HALF" and part == "SITE"):
                    folder["%s_ZULIP_%s" % (prefix, part)] = "v"
        env = self.fake_fleet_secrets(folder)
        with mock.patch.object(Z, "_default_infisical_names", Z._list_from_fleet_secrets):
            self.assertEqual(
                sorted(Z.infisical_seats(env, self.home)), ["BF-PLUMBER", "CLAUDE", "GROK"]
            )
            self.assertFalse(Z.infisical_has("JAY", env, self.home))

    def test_an_infisical_outage_costs_one_names_fetch_for_every_identity_candidate(self) -> None:
        from agent_sync import identities as X

        log = self.tmp / "calls.log"
        wrapper = self.tmp / "counting-fleet-secrets"
        wrapper.write_text(
            '#!/bin/sh\necho "$1" >> "%s"\nexit 4\n' % log
        )  # every call fails like an outage
        wrapper.chmod(0o755)
        env = self.fake_fleet_secrets({}, binary=wrapper)
        with mock.patch.object(Z, "_default_infisical_names", Z._list_from_fleet_secrets):
            found = X.allowed_identities(env, self.home)
            self.assertEqual(found, {})
            X.allowed_identities(env, self.home)  # again, inside the backoff:  still no new fetch
        self.assertEqual(log.read_text().split(), ["names"])
        with self.assertRaises(Z.CredentialError) as ctx:
            Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("fetch-or-auth-failure", str(ctx.exception))
        with self.assertRaises(
            Z.CredentialError
        ) as ctx:  # the same values inside the backoff:  no new call
            Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("not retried", str(ctx.exception))
        self.assertEqual(log.read_text().split(), ["names", "run"])

    def test_an_outage_on_the_prefetch_fails_every_later_seat_at_once(self) -> None:
        log = self.tmp / "calls.log"
        wrapper = self.tmp / "hanging-fleet-secrets"
        wrapper.write_text(
            '#!/bin/sh\necho "$1" >> "%s"\nexit 4\n' % log
        )  # an auth or fetch failure
        wrapper.chmod(0o755)
        env = self.fake_fleet_secrets({}, binary=wrapper)
        with mock.patch.object(Z, "_default_infisical_fetch", Z._fetch_from_fleet_secrets):
            Z.prefetch_infisical(
                ("CLAUDE", "CODEX", "MM"), env, self.home
            )  # the daemon's one call fails
            for seat in ("CLAUDE", "CODEX", "MM"):
                with self.assertRaises(Z.CredentialError) as ctx:
                    Z.infisical_credentials(seat, env=env, home=self.home)
                self.assertIn("not retried", str(ctx.exception))
        self.assertEqual(log.read_text().split(), ["run"])  # one call in all, not one per seat

    def test_a_missing_key_does_not_block_other_seats(self) -> None:
        env = self.fake_fleet_secrets(
            {
                "CLAUDE_ZULIP_EMAIL": BOT_EMAIL,
                "CLAUDE_ZULIP_KEY": self.key,
                "CLAUDE_ZULIP_SITE": self.fake.url,
            }
        )
        with self.assertRaises(Z.CredentialError):
            Z._fetch_from_fleet_secrets(Z.infisical_key_names("CODEX"), env, self.home)
        got = Z._fetch_from_fleet_secrets(Z.infisical_key_names("CLAUDE"), env, self.home)
        self.assertEqual(sorted(got), sorted(Z.infisical_key_names("CLAUDE")))

    def test_one_binary_keeps_two_projects_apart(self) -> None:
        # One `fleet-secrets` serves both the AFC /zulip the Notes service reads and the Fleet-OPS
        # /zulip the seats read:  values, names and outages are keyed by project, never by binary.
        env = self.fake_fleet_secrets({"SHARED": "from-fleet-ops", "ONLY_FLEET_OPS": "x"})
        other = dict(env)
        other[Z.ENV_INFISICAL_PROJECT] = OTHER_FAKE_PROJECT_ID
        other_data = self.tmp / "other-project.json"
        other_data.write_text(json.dumps({"/zulip": {"SHARED": "from-afc", "ONLY_AFC": "y"}}))
        other["FAKE_FLEET_SECRETS_FILE"] = str(other_data)
        self.assertEqual(Z._fetch_from_fleet_secrets(("SHARED",), env, self.home), {"SHARED": "from-fleet-ops"})
        self.assertEqual(Z._fetch_from_fleet_secrets(("SHARED",), other, self.home), {"SHARED": "from-afc"})
        self.assertEqual(sorted(Z._list_from_fleet_secrets(env, self.home)), ["ONLY_FLEET_OPS", "SHARED"])
        self.assertEqual(sorted(Z._list_from_fleet_secrets(other, self.home)), ["ONLY_AFC", "SHARED"])
        Path(env["FAKE_FLEET_SECRETS_FILE"]).write_text("{", encoding="utf-8")  # that store is down
        with mock.patch.object(Z, "INFISICAL_CACHE_TTL", 0.0):
            with self.assertRaises(Z.CredentialError) as ctx:  # never held:  it raises
                Z._fetch_from_fleet_secrets(("NEVER_HELD",), env, self.home)
            self.assertIn("fetch-or-auth-failure", str(ctx.exception))
            with self.assertRaises(Z.CredentialError) as ctx:
                Z._fetch_from_fleet_secrets(("NEVER_HELD",), env, self.home)
            self.assertIn("not retried", str(ctx.exception))
            # the healthy project's fetch is not refused by the other store's outage
            self.assertEqual(Z._fetch_from_fleet_secrets(("SHARED",), other, self.home), {"SHARED": "from-afc"})

    def test_an_unresolvable_project_fails_closed_and_is_never_cached(self) -> None:
        env = self.fake_fleet_secrets({"A": "1"})
        del env[Z.ENV_INFISICAL_PROJECT]  # and no ~/.agent-sync/fleet-ops-project-id
        for call in (
            lambda: Z._fetch_from_fleet_secrets(("A",), env, self.home),
            lambda: Z._list_from_fleet_secrets(env, self.home),
        ):
            with self.assertRaises(Z.CredentialError) as ctx:
                call()
            self.assertIn(Z.ENV_INFISICAL_PROJECT, str(ctx.exception))
        self.assertEqual(Z._value_cache, {})
        self.assertEqual(Z._names_cache, {})

    def test_the_old_default_file_under_home_is_never_read(self) -> None:
        # AFC-359:  ~/.secrets/Zulip is retired.  A file there, with no override and nothing in Infisical, is ignored.
        write_rc(
            self.home / ".secrets" / "Zulip" / "Claude-zuliprc",
            email=BOT_EMAIL,
            key=self.key,
            site=self.fake.url,
        )
        self.assertIsNone(Z.default_rc_path("CLAUDE", {"HOME": str(self.home)}, self.home))
        result = self.run_cli("whoami", env=self.env(AGENT_SYNC_SECRETS_DIR=None))
        self.assertEqual(result.code, 3, result.out)
        self.assertNotIn(".secrets", result.err)
        self.assertIn("CLAUDE_ZULIP_KEY", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_key_names_follow_the_credential_file_stem(self) -> None:
        for seat, prefix in (
            ("CLAUDE", "CLAUDE"),
            ("claude", "CLAUDE"),
            ("GROK", "GROK_BUILD"),
            ("GROK-BUILD", "GROK_BUILD"),
            ("GROK-WEB", "GROK_WEB"),
            ("BF-BUILDER", "BF_BUILDER"),
            ("GB-COMPILER", "GB_COMPILER"),
            ("MM", "MM"),
            ("AG", "AG"),
            ("OPENCODE", "OPENCODE"),
        ):
            with self.subTest(seat=seat):
                self.assertEqual(
                    Z.infisical_key_names(Z.normalise_seat(seat)),
                    tuple("%s_ZULIP_%s" % (prefix, part) for part in ("EMAIL", "KEY", "SITE")),
                )

    def test_infisical_credentials_hold_no_value_in_repr_or_source(self) -> None:
        self.seed()
        creds = Z.infisical_credentials("CLAUDE", fetch=lambda names: self.infisical_values)
        self.assertNotIn(self.key, repr(creds))
        self.assertNotIn(self.key, creds.source)


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
        result = self.run_cli(
            "whoami", "--json", "--rc", str(self.rc_path), env=self.env(AGENT_SEAT=None)
        )
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out)["seat"], "CLAUDE")

    def test_credential_file_names(self) -> None:
        for seat, expected in (
            ("CLAUDE", "Claude-zuliprc"),
            ("claude", "Claude-zuliprc"),
            ("GB-COMPILER", "GB-Compiler-zuliprc"),
            ("CODEX-2", "Codex-2-zuliprc"),
            ("OPENCODE", "OpenCode-zuliprc"),
            ("opencode", "OpenCode-zuliprc"),
        ):
            with self.subTest(seat=seat):
                self.assertEqual(Z.credential_file_name(seat), expected)

    def test_credential_file_names_match_what_the_provisioning_script_writes(self) -> None:
        # The real credential file names on the owner's Mac, 2026-10-07 (names only).  The rule must produce every one.
        written = {
            "CLAUDE": "Claude",
            "CODEX": "Codex",
            "AG": "AG",
            "CURSOR": "Cursor",
            "GROK": "Grok-Build",
            "GROK-WEB": "Grok-Web",
            "CLUTCH": "Clutch",
            "FX": "FX",
            "MM": "MM",
            "MC": "MC",
            "MA": "MA",
            "OPENCODE": "OpenCode",
            "ECHO": "Echo",
            "WISE": "Wise",
            "GB-COMPILER": "GB-Compiler",
            "BF-BUILDER": "BF-Builder",
            "BF-DEPLOYER": "BF-Deployer",
            "BF-DESIGNER": "BF-Designer",
            "BF-FIXER": "BF-Fixer",
            "BF-HOUSEKEEPER": "BF-Housekeeper",
            "BF-MONITOR": "BF-Monitor",
            "BF-ORACLE": "BF-Oracle",
            "BF-PLUMBER": "BF-Plumber",
            "BF-PUBLISHER": "BF-Publisher",
            "BF-COMPILER": "BF-Compiler",
        }
        for seat, stem in written.items():
            with self.subTest(seat=seat):
                self.assertEqual(Z.credential_file_name(seat), stem + "-zuliprc")
                self.assertEqual(
                    Z.seat_from_rc_path("/x/" + stem + "-zuliprc"), seat
                )  # and back again

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
            ("whoami",),
            ("whoami", "--json"),
            ("channels",),
            ("topics",),
            ("read", "--topic", "topic a"),
            ("read", "--topic", "topic a", "--json"),
            ("inbox", "--peek"),
            ("post", "--topic", "topic a", "hi"),
            ("react", "--id", "999999", "eyes"),
            ("react", "--id", "1", "eyes"),  # server errors that echo auth
            ("resolve", "--topic", "no such topic"),
            ("topics", "--channel", "nonexistent"),
            ("post", "--topic", "x" * 61, "hi"),
            ("post", "hi"),
            ("whoami", "--rc", str(self.tmp / "missing")),
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


class OperatorIdentityTests(unittest.TestCase):
    """The Operator's Zulip user id is private inventory, so it is read from the environment
    and never committed.  A refused key is named by a masked preview, never in full."""

    def test_the_operator_user_id_is_read_from_the_environment(self) -> None:
        self.assertEqual(Z.operator_user_id({"OPERATOR_ZULIP_USER_ID": " 4242 "}), 4242)

    def test_an_unset_operator_user_id_is_none_so_the_caller_fails_closed(self) -> None:
        self.assertIsNone(Z.operator_user_id({}))
        self.assertIsNone(Z.operator_user_id({"OPERATOR_ZULIP_USER_ID": "   "}))
        self.assertIsNone(Z.operator_user_id({"OPERATOR_ZULIP_USER_ID": "not-a-number"}))

    def test_no_production_user_id_is_committed(self) -> None:
        # The constant is gone on purpose:  a pinned id is private inventory in source.
        self.assertFalse(hasattr(Z, "OPERATOR_USER_ID"))

    def test_a_masked_preview_names_the_key_without_writing_it_out(self) -> None:
        key = "abcdefgh" + "x" * 20 + "wxyz"
        preview = Z.masked_credential(key)
        self.assertEqual(preview, "abcdefgh\u2026wxyz")
        self.assertNotIn(key, preview)

    def test_a_short_or_missing_key_still_gets_a_preview(self) -> None:
        self.assertEqual(Z.masked_credential(""), "missing")
        self.assertEqual(Z.masked_credential("short"), "s\u2026")

    def test_a_short_credential_exposes_one_character_only(self) -> None:
        #  Thirteen characters:  the old threshold showed twelve of them, which is nearly the
        #  whole secret, in an error that can reach a log.
        key = "abcdefghijklm"
        preview = Z.masked_credential(key)
        self.assertEqual(preview, "a\u2026")
        self.assertNotIn(key[1:], preview)
        #  The threshold itself:  under 24 characters one leading character, at or over it the
        #  first eight and the last four.
        self.assertEqual(Z.masked_credential("x" * 23), "x\u2026")
        self.assertEqual(Z.masked_credential("y" * 24), "yyyyyyyy\u2026yyyy")


if __name__ == "__main__":
    unittest.main()
