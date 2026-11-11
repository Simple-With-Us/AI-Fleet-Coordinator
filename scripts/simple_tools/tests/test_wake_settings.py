"""The wake router's settings:  Infisical through fleet_knobs, held to the service contract (OPS-1048).
Infisical is a fake at the scoped reader's boundary, as in the fleet_knobs tests, so nothing here
reaches a real store, and no router is started."""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import signal
import threading
import time
from types import SimpleNamespace
from unittest import mock

from fleet_knobs import knobs, source
from fleet_knobs import values as knob_values
from fleet_knobs.registry import BY_NAME
from fleet_knobs.tests._fake import KnobTestCase
from simple_tools import claude_wake as CW
from simple_tools import registry_api
from simple_tools import wake_router as WR
from simple_tools import wake_service as SVC
from simple_tools import wake_settings as WS

ARMED = {WS.DELIVERY_ENABLED: "true", WS.DELIVERY_SEATS: "CLAUDE"}
KEY_KNOB = "WAKE_ROUTER_KEY"
KEY = "k3y-" + "0123456789abcdef" * 2  # synthetic, long enough
REGISTRY_URL = "https://registry.example.test"
REGISTRY_KEY = "reg-" + "fedcba9876543210" * 2  # synthetic
# The registry's address and key, as Infisical would hold them.
REGISTRY = {WS.REGISTRY_URL: REGISTRY_URL, WS.REGISTRY_KEY: REGISTRY_KEY}


class Settings(KnobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.local_path = self.state / "config.json"
        stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr = stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    @property
    def router_copy(self):
        """The last-known-good copy of the router's folder (the root's is `lkg_file`)."""
        return self.state_dir / "lkg-wake-router.json"

    def local(self, **values) -> None:
        self.local_path.write_text(json.dumps(values), encoding="utf-8")

    def loaded(self, **secrets) -> WS.WakeSettings:
        self.fake.set(secrets=secrets)
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        settings.load()
        return settings


class Registered(Settings):
    def test_every_setting_is_a_registered_knob(self) -> None:
        names = {*WS.INT_KNOBS.values(), WS.DELIVERY_ENABLED, WS.DELIVERY_SEATS}
        names |= {WS.RETRY_BACKOFF, WS.MAC_SSH, WS.REFRESH_SECONDS}
        self.assertLessEqual(names, set(BY_NAME))

    def test_the_registry_defaults_are_the_routers_defaults(self) -> None:
        for name, knob in WS.INT_KNOBS.items():
            with self.subTest(name=name):
                self.assertEqual(BY_NAME[knob].default, CW.DEFAULTS[name])
        self.assertIs(BY_NAME[WS.DELIVERY_ENABLED].default, WR.ROUTER_DEFAULTS["delivery_enabled"])
        backoff = [int(part) for part in BY_NAME[WS.RETRY_BACKOFF].default.split(",")]
        self.assertEqual(backoff, WR.ROUTER_DEFAULTS["retry_backoff"])
        self.assertEqual(BY_NAME[WS.MAC_SSH].default, CW.DEFAULTS["mac_ssh"])

    def test_every_tunable_is_a_knob_or_a_named_host_fact(self) -> None:
        handled = {*WS.INT_KNOBS, *WS.HOST_FACTS, "delivery_enabled", "retry_backoff", "mac_ssh"}
        self.assertEqual(set(CW.DEFAULTS), handled)

    def test_every_router_knob_and_only_those_live_in_its_own_folder(self) -> None:
        names = {*WS.INT_KNOBS.values(), WS.DELIVERY_ENABLED, WS.DELIVERY_SEATS}
        names |= {WS.RETRY_BACKOFF, WS.MAC_SSH, WS.REFRESH_SECONDS}
        names |= {WS.KEY_KNOB, WS.REGISTRY_URL, WS.REGISTRY_KEY}
        self.assertEqual(WS.FOLDER, "/wake-router")
        in_folder = {name for name, spec in BY_NAME.items() if spec.folder == WS.FOLDER}
        self.assertEqual(in_folder, names, "the router's token must reach every knob it reads")

    def test_the_key_is_a_secret_knob_with_the_old_minimum(self) -> None:
        spec = BY_NAME[KEY_KNOB]
        self.assertTrue(spec.secret)
        self.assertEqual(WS.KEY_KNOB, KEY_KNOB)
        self.assertEqual(WR.KEY_MIN_LENGTH, 16)
        self.assertIsNone(knob_values.lenient(spec, "x" * 15)[1])
        self.assertEqual(knob_values.lenient(spec, "x" * 16), (True, "x" * 16))

    def test_one_bearer_and_no_seat_has_one_of_its_own(self) -> None:
        self.assertFalse(hasattr(WS, "KEY_KNOBS"))
        per_seat = [name for name in BY_NAME if re.fullmatch(r"WAKE_ROUTER_[A-Z]+_KEY", name)]
        self.assertEqual(sorted(per_seat), ["WAKE_ROUTER_REGISTRY_KEY"], "only the registry's")
        for name in ("WAKE_ROUTER_CLAUDE_KEY", "WAKE_ROUTER_CODEX_KEY", "WAKE_ROUTER_MC_KEY"):
            self.assertNotIn(name, BY_NAME)

    def test_the_registry_knobs(self) -> None:
        url, key = BY_NAME[WS.REGISTRY_URL], BY_NAME[WS.REGISTRY_KEY]
        self.assertFalse(url.secret)
        self.assertTrue(key.secret)
        self.assertEqual((url.folder, key.folder), (WS.FOLDER, WS.FOLDER))
        self.assertEqual(url.local, "deny", "a local file never points the router elsewhere")
        self.assertEqual(url.default, "", "unset by default:  not configured")
        for good in ("https://registry.example.test", "http://127.0.0.1:8788"):
            self.assertEqual(knob_values.lenient(url, good), (True, good), good)
        for bad in ("https://x.test/registry/v1/", "ftp://x.test", "https://x.test?a=b"):
            self.assertIsNone(knob_values.lenient(url, bad)[1], bad)


class Folder(Settings):
    def test_the_router_fetches_its_folder_and_nothing_else(self) -> None:
        settings = self.loaded(**ARMED)
        self.assertTrue(settings.confirmed)
        self.assertEqual({call["path"] for call in self.fake.calls()}, {"/wake-router"})
        asked = set(self.fake.calls()[0]["names"])
        self.assertIn(KEY_KNOB, asked)
        self.assertFalse(any(not name.startswith("WAKE_ROUTER_") for name in asked))

    def test_root_knobs_cannot_arm_or_confirm_the_router(self) -> None:
        # Infisical holds the switch only at the root:  the router does not read it there.
        self.fake.set(secrets={}, folders={"/": ARMED, "/wake-router": {}})
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        settings.load()
        self.assertTrue(settings.confirmed)
        self.assertIs(settings["delivery_enabled"], False)

    def test_a_failed_root_fetch_does_not_unconfirm_the_router(self) -> None:
        self.fake.set(secrets=ARMED, folder_modes={"/": "fail"})
        self.assertEqual(knobs.get("LANE_SWEEP_MODE"), "report")  # a root read, and it fails
        self.assertEqual(knobs.report()["fetch"], "failed")
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        settings.load()
        self.assertTrue(settings.confirmed)
        self.assertIs(settings["delivery_enabled"], True)

    def test_a_failed_router_fetch_is_unconfirmed_whatever_the_root_did(self) -> None:
        self.fake.set(secrets=ARMED, folder_modes={"/wake-router": "fail"})
        self.assertEqual(knobs.resolve("LANE_SWEEP_MODE").source, "default")
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        settings.load()
        self.assertFalse(settings.confirmed)
        self.assertIn("unconfirmed scoped-read-failed", self.stderr.getvalue())


class Key(Settings):
    def test_the_key_comes_from_the_secret_knob(self) -> None:
        settings = self.loaded(**{KEY_KNOB: KEY})
        self.assertEqual(settings.routine_key(), (KEY, ""))

    def test_no_key_until_it_is_read(self) -> None:
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        self.assertEqual(settings.routine_key(), ("", "not-read"))

    def test_an_unset_short_or_unreadable_key_is_no_key(self) -> None:
        cases = (
            ("unset", {"mode": "ok", "secrets": {}}),
            ("malformed", {"mode": "ok", "secrets": {KEY_KNOB: "x" * 15}}),
            ("malformed", {"mode": "ok", "secrets": {KEY_KNOB: "has a space in it ok"}}),
            ("unreachable:scoped-read-failed", {"mode": "fail"}),
        )
        for reason, cfg in cases:
            with self.subTest(reason=reason, cfg=cfg):
                self.new_process()
                self.fake.set(**cfg)
                settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
                settings.load()
                self.assertEqual(settings.routine_key(), ("", reason))
                self.assertIn("key-unavailable %s" % reason, self.stderr.getvalue())

    def test_the_key_is_never_written_or_logged(self) -> None:
        settings = self.loaded(**ARMED, **{KEY_KNOB: KEY})
        settings.refresh()
        self.assertTrue((self.state_dir / "lkg-wake-router.json").exists(), "a copy was written")
        blob = self.stderr.getvalue()
        for top in (self.home, self.state):  # the fake's own config sits beside them, not in them
            for directory, _dirs, files in os.walk(top):
                for name in files:
                    with open(os.path.join(directory, name), "rb") as fh:
                        blob += fh.read().decode("utf-8", "replace")
        self.assertNotIn(KEY, blob)
        self.assertNotIn(KEY_KNOB, blob.replace("key-unavailable", ""))
        self.assertNotIn(KEY, json.dumps(knobs.report()))

    # Only a refresh whose fetch worked may change the key (AGENTS.md:  a background refresh keeps
    # last-known-good on failure).  An Infisical outage must not turn every seat away.

    def test_a_good_key_survives_a_failed_refresh(self) -> None:
        settings = self.loaded(**ARMED, **{KEY_KNOB: KEY})
        self.fake.set(mode="fail")
        self.assertFalse(settings.refresh())
        self.assertEqual(settings.routine_key(), (KEY, ""))
        self.assertIs(settings["delivery_enabled"], True, "the other settings are still held")
        self.assertIn("key-kept refresh-failed seat=CLAUDE key=held", self.stderr.getvalue())
        self.fake.set(secrets={**ARMED, KEY_KNOB: KEY})
        self.assertTrue(settings.refresh())
        self.assertEqual(settings.routine_key(), (KEY, ""))

    def test_a_good_key_survives_a_refresh_that_raises(self) -> None:
        settings = self.loaded(**{KEY_KNOB: KEY})
        with mock.patch.object(WS, "from_knobs", side_effect=RuntimeError("boom")):
            self.assertFalse(settings.refresh())
        self.assertEqual(settings.routine_key(), (KEY, ""))
        with mock.patch.object(knobs, "refresh", side_effect=RuntimeError("boom")):
            self.assertFalse(settings.refresh())
        self.assertEqual(settings.routine_key(), (KEY, ""))
        self.assertIn("key-kept refresh-error seat=CLAUDE key=held", self.stderr.getvalue())

    def test_a_good_fetch_without_the_knob_drops_the_key(self) -> None:
        settings = self.loaded(**{KEY_KNOB: KEY})
        self.fake.set(secrets={})
        self.assertTrue(settings.refresh())
        self.assertEqual(settings.routine_key(), ("", "unset"))

    def test_a_good_fetch_with_a_new_value_takes_it(self) -> None:
        settings = self.loaded(**{KEY_KNOB: KEY})
        rotated = KEY[::-1]
        self.fake.set(secrets={KEY_KNOB: rotated})
        self.assertTrue(settings.refresh())
        self.assertEqual(settings.routine_key(), (rotated, ""))

    def test_a_failed_refresh_does_not_bring_back_a_revoked_key(self) -> None:
        settings = self.loaded(**{KEY_KNOB: KEY})
        self.fake.set(secrets={})
        self.assertTrue(settings.refresh())  # revoked
        self.fake.set(mode="fail")
        self.assertFalse(settings.refresh())
        self.assertEqual(settings.routine_key(), ("", "unset"))
        self.assertIn("key-kept refresh-failed seat=CLAUDE key=none", self.stderr.getvalue())

    def test_the_mac_reader_never_asks_for_the_key(self) -> None:
        self.fake.set(secrets={KEY_KNOB: KEY})
        with mock.patch.object(WS, "read_key", side_effect=AssertionError("read_key")):
            WS.read_once(CW.SEAT, CW.DEFAULTS, self.local_path)
        self.assertNotIn(KEY_KNOB, knobs.report()["knobs"])

    def test_every_seat_reads_the_one_key(self) -> None:
        self.fake.set(secrets={KEY_KNOB: KEY})
        for seat in ("CLAUDE", "ZC", "GROK"):
            settings = WS.WakeSettings(seat, WR.ROUTER_DEFAULTS, self.local_path)
            settings.load()
            self.assertEqual(settings.routine_key(), (KEY, ""), seat)


class Reading(Settings):
    def test_nothing_set_means_the_defaults_with_delivery_off(self) -> None:
        settings = self.loaded()
        self.assertEqual(dict(settings), CW.DEFAULTS)
        self.assertIs(settings["delivery_enabled"], False)
        self.assertTrue(settings.confirmed)

    def test_delivery_needs_the_switch_and_the_seat(self) -> None:
        cases = [
            ({WS.DELIVERY_ENABLED: "true"}, False),
            ({WS.DELIVERY_SEATS: "CLAUDE"}, False),
            ({WS.DELIVERY_ENABLED: "true", WS.DELIVERY_SEATS: "CODEX"}, False),
            ({WS.DELIVERY_ENABLED: "false", WS.DELIVERY_SEATS: "CLAUDE"}, False),
            (ARMED, True),
            ({WS.DELIVERY_ENABLED: "true", WS.DELIVERY_SEATS: "CODEX,CLAUDE"}, True),
        ]
        for secrets, want in cases:
            with self.subTest(secrets=secrets):
                self.new_process()
                self.assertIs(self.loaded(**secrets)["delivery_enabled"], want)

    def test_the_knobs_are_the_settings(self) -> None:
        settings = self.loaded(
            WAKE_ROUTER_WORKERS="8",
            WAKE_ROUTER_MAX_ATTEMPTS="9",
            WAKE_ROUTER_RETRY_BACKOFF="1,2,3",
            WAKE_ROUTER_RESUME_TIMEOUT_SECONDS="120",
            WAKE_ROUTER_MAC_SSH="mac-host",
        )
        self.assertEqual(settings["workers"], 8)
        self.assertEqual(settings["max_attempts"], 9)
        self.assertEqual(settings["retry_backoff"], [1, 2, 3])
        self.assertEqual(settings["resume_timeout_seconds"], 120)
        self.assertEqual(settings["mac_ssh"], "mac-host")

    def test_a_value_that_fails_validation_reads_the_default(self) -> None:
        settings = self.loaded(
            WAKE_ROUTER_WORKERS="0",
            WAKE_ROUTER_RETRY_BACKOFF="soon",
            WAKE_ROUTER_MAC_SSH="-oProxyCommand=x",
        )
        self.assertEqual(settings["workers"], CW.DEFAULTS["workers"])
        self.assertEqual(settings["retry_backoff"], CW.DEFAULTS["retry_backoff"])
        self.assertEqual(settings["mac_ssh"], "")

    def test_a_router_with_fewer_settings_gets_only_those(self) -> None:
        self.fake.set(secrets=ARMED)
        settings = WS.WakeSettings("CLAUDE", WR.ROUTER_DEFAULTS, self.local_path)
        settings.load()
        self.assertEqual(set(settings), set(WR.ROUTER_DEFAULTS))

    def test_an_environment_variable_cannot_arm_delivery(self) -> None:
        # Each variable alone, with Infisical holding the other half, so the other half cannot be
        # what keeps delivery off.
        for variable, secrets in (
            (WS.DELIVERY_ENABLED, {WS.DELIVERY_SEATS: "CLAUDE"}),
            (WS.DELIVERY_SEATS, {WS.DELIVERY_ENABLED: "true"}),
        ):
            with self.subTest(variable=variable):
                self.new_process()
                value = "CLAUDE" if variable == WS.DELIVERY_SEATS else "true"
                with mock.patch.dict(os.environ, {variable: value}):
                    self.assertIs(self.loaded(**secrets)["delivery_enabled"], False)

    def test_no_reader_token_runs_with_delivery_off(self) -> None:
        del os.environ[source.ENV_KNOBS_TOKEN]
        settings = self.loaded(**ARMED)
        self.assertIs(settings["delivery_enabled"], False)
        self.assertFalse(settings.confirmed)
        self.assertIn("unconfirmed service-token-required", self.stderr.getvalue())

    def armed_copy_on_disk(self) -> None:
        """A good fetch with delivery on, which leaves it in the last-known-good copy."""
        self.assertIs(self.loaded(**ARMED)["delivery_enabled"], True)
        self.assertTrue(self.router_copy.exists())
        self.assertFalse(self.lkg_file.exists(), "the router never fetches or copies the root")

    def assert_not_armed_by_the_copy(self, settings: WS.WakeSettings) -> None:
        self.assertIs(settings["delivery_enabled"], False)
        self.assertFalse(settings.confirmed)

    def starting_on_the_copy(self) -> WS.WakeSettings:
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        settings.load()
        return settings

    def test_an_old_copy_cannot_arm_delivery_during_an_outage(self) -> None:
        self.armed_copy_on_disk()
        self.new_process()
        self.fake.set(mode="fail")
        self.assert_not_armed_by_the_copy(self.starting_on_the_copy())

    def test_an_old_copy_cannot_arm_delivery_with_no_token(self) -> None:
        self.armed_copy_on_disk()
        self.new_process()
        del os.environ[source.ENV_KNOBS_TOKEN]
        self.assert_not_armed_by_the_copy(self.starting_on_the_copy())

    def test_an_old_copy_cannot_arm_delivery_in_the_failed_fetch_cooldown(self) -> None:
        self.armed_copy_on_disk()
        self.new_process()
        self.fake.set(mode="fail")
        self.starting_on_the_copy()  # records the failure
        self.new_process(keep_failure_marker=True)
        self.fake.set(secrets=ARMED)  # it would answer, but the cooldown skips the fetch
        settings = self.starting_on_the_copy()
        self.assertIn("recent-", knobs.report()["fetch_reason"])
        self.assert_not_armed_by_the_copy(settings)

    def test_an_old_copy_cannot_arm_delivery_on_the_mac_reader(self) -> None:
        self.armed_copy_on_disk()
        self.new_process()
        self.fake.set(mode="fail")
        config = WS.read_once(CW.SEAT, CW.DEFAULTS, self.local_path)
        self.assertIs(config["delivery_enabled"], False)

    def test_other_settings_may_still_come_from_the_copy(self) -> None:
        self.loaded(WAKE_ROUTER_MAX_ATTEMPTS="9")
        self.new_process()
        self.fake.set(mode="fail")
        self.assertEqual(self.starting_on_the_copy()["max_attempts"], 9)

    def test_a_non_prod_environment_stops_the_router(self) -> None:
        os.environ[source.ENV_NAMES[0]] = "dev"
        self.addCleanup(os.environ.pop, source.ENV_NAMES[0], None)
        with self.assertRaises(SystemExit):
            self.loaded()


class LocalFile(Settings):
    def test_a_local_file_cannot_arm_delivery(self) -> None:
        self.local(delivery_enabled=True)
        self.assertIs(self.loaded()["delivery_enabled"], False)
        self.assertIn("local-ignored %s" % WS.DELIVERY_ENABLED, self.stderr.getvalue())

    def test_a_local_file_can_turn_delivery_off(self) -> None:
        self.local(delivery_enabled=False)
        self.assertIs(self.loaded(**ARMED)["delivery_enabled"], False)

    def test_a_local_file_can_lower_a_cap_and_not_raise_it(self) -> None:
        self.local(workers=2)
        self.assertEqual(self.loaded(WAKE_ROUTER_WORKERS="8")["workers"], 2)
        self.new_process()
        self.local(workers=16)
        self.assertEqual(self.loaded(WAKE_ROUTER_WORKERS="8")["workers"], 8)

    def test_a_wait_narrows_upward(self) -> None:
        self.local(busy_retry_seconds=60)
        self.assertEqual(self.loaded()["busy_retry_seconds"], 60)
        self.new_process()
        self.local(busy_retry_seconds=5)
        self.assertEqual(self.loaded()["busy_retry_seconds"], 30)

    def test_a_local_file_cannot_set_the_route_or_the_backoff(self) -> None:
        self.local(mac_ssh="some-host", retry_backoff=[1, 1, 1], shutdown_wait_seconds=5)
        settings = self.loaded()
        self.assertEqual(settings["mac_ssh"], "")
        self.assertEqual(settings["retry_backoff"], CW.DEFAULTS["retry_backoff"])
        self.assertEqual(settings["shutdown_wait_seconds"], CW.DEFAULTS["shutdown_wait_seconds"])
        for knob in (WS.MAC_SSH, WS.RETRY_BACKOFF):
            self.assertIn("local-ignored %s" % knob, self.stderr.getvalue())

    def test_a_setting_the_file_does_not_name_is_not_narrowed_by_its_default(self) -> None:
        self.local(max_attempts=2)
        self.assertEqual(self.loaded(WAKE_ROUTER_WORKERS="8")["workers"], 8)

    def test_host_facts_come_from_the_file(self) -> None:
        self.local(local_host="mac", claude_bin="/opt/claude")
        settings = self.loaded()
        self.assertEqual((settings["local_host"], settings["claude_bin"]), ("mac", "/opt/claude"))

    def test_a_bad_file_stops_the_router(self) -> None:
        for text in ("{not json", '{"nonsense": 1}', '{"workers": "many"}'):
            with self.subTest(text):
                self.local_path.write_text(text, encoding="utf-8")
                with self.assertRaises(SystemExit):
                    self.loaded()

    def test_a_named_file_that_is_missing_stops_the_router(self) -> None:
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path, required=True)
        with self.assertRaises(SystemExit):
            settings.load()


class Service(Settings):
    def test_a_read_touches_memory_only(self) -> None:
        settings = self.loaded(**ARMED)
        fetches = len(self.fake.calls())
        for _ in range(50):
            settings["delivery_enabled"]
            settings.get("workers")
        self.assertEqual(len(self.fake.calls()), fetches)

    def test_a_refresh_picks_up_a_change_in_either_direction(self) -> None:
        settings = self.loaded()
        self.assertIs(settings["delivery_enabled"], False)
        self.fake.set(secrets=ARMED)
        self.assertTrue(settings.refresh())
        self.assertIs(settings["delivery_enabled"], True)
        self.fake.set(secrets={WS.DELIVERY_ENABLED: "false", WS.DELIVERY_SEATS: "CLAUDE"})
        self.assertTrue(settings.refresh())
        self.assertIs(settings["delivery_enabled"], False)

    def test_a_failed_refresh_keeps_what_was_held_with_no_copy_on_disk(self) -> None:
        settings = self.loaded(
            WAKE_ROUTER_WORKERS="8", WAKE_ROUTER_SETTINGS_REFRESH_SECONDS="60", **ARMED
        )
        self.router_copy.unlink()
        self.fake.set(mode="fail")
        self.assertFalse(settings.refresh())
        self.assertIs(settings["delivery_enabled"], True)
        self.assertEqual(settings["workers"], 8)
        self.assertEqual(settings.interval, 60.0)
        self.assertIn("refresh-kept-last-good", self.stderr.getvalue())

    def test_the_local_brake_works_during_an_outage(self) -> None:
        settings = self.loaded(**ARMED)
        self.fake.set(mode="fail")
        self.local(delivery_enabled=False)
        self.assertFalse(settings.refresh())
        self.assertIs(settings["delivery_enabled"], False)

    def test_taking_the_brake_off_during_an_outage_restores_what_was_held(self) -> None:
        self.local(delivery_enabled=False)
        settings = self.loaded(**ARMED)
        self.assertIs(settings["delivery_enabled"], False)
        self.fake.set(mode="fail")
        self.local()
        self.assertFalse(settings.refresh())
        self.assertIs(settings["delivery_enabled"], True)

    def test_a_local_cap_applies_during_an_outage(self) -> None:
        settings = self.loaded(WAKE_ROUTER_MAX_DELIVERIES="8")
        self.fake.set(mode="fail")
        self.local(max_deliveries=2)
        settings.refresh()
        self.assertEqual(settings["max_deliveries"], 2)
        self.local(max_deliveries=64)  # would widen what is held
        settings.refresh()
        self.assertEqual(settings["max_deliveries"], 8)

    def test_the_brake_is_on_before_the_fetch_finishes(self) -> None:
        settings = self.loaded(**ARMED)
        self.local(delivery_enabled=False)
        seen = {}

        def during_the_fetch():
            seen["delivery"] = settings["delivery_enabled"]

        with mock.patch.object(knobs, "refresh", side_effect=during_the_fetch):
            settings.refresh()
        self.assertIs(seen["delivery"], False)

    def test_the_first_good_fetch_confirms_and_can_arm(self) -> None:
        self.fake.set(mode="fail")
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        settings.load()
        self.assertFalse(settings.confirmed)
        self.assertFalse(settings.refresh())
        self.assertFalse(settings.confirmed)
        self.fake.set(secrets=ARMED)
        self.assertTrue(settings.refresh())
        self.assertTrue(settings.confirmed)
        self.assertIs(settings["delivery_enabled"], True)

    def test_a_failed_refresh_never_takes_back_a_confirmation(self) -> None:
        settings = self.loaded(**ARMED)
        self.fake.set(mode="fail")
        settings.refresh()
        self.assertTrue(settings.confirmed)

    def test_a_refresh_after_a_failure_applies_again(self) -> None:
        settings = self.loaded(**ARMED)
        self.fake.set(mode="fail")
        settings.refresh()
        self.fake.set(secrets={WS.DELIVERY_ENABLED: "false"})
        self.assertTrue(settings.refresh())
        self.assertIs(settings["delivery_enabled"], False)

    def test_a_refresh_with_a_broken_local_file_keeps_what_was_held(self) -> None:
        settings = self.loaded(**ARMED)
        self.local_path.write_text("{not json", encoding="utf-8")
        self.assertFalse(settings.refresh())
        self.assertIs(settings["delivery_enabled"], True)

    def test_a_refresh_reads_the_local_file_again(self) -> None:
        settings = self.loaded(**ARMED)
        self.local(delivery_enabled=False)
        self.assertTrue(settings.refresh())
        self.assertIs(settings["delivery_enabled"], False)

    def test_the_interval_is_a_knob(self) -> None:
        settings = self.loaded(WAKE_ROUTER_SETTINGS_REFRESH_SECONDS="60")
        self.assertEqual(settings.interval, 60.0)
        self.fake.set(secrets={"WAKE_ROUTER_SETTINGS_REFRESH_SECONDS": "90"})
        self.assertTrue(settings.refresh())
        self.assertEqual(settings.interval, 90.0)

    def wait_for(self, check) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.01)
        return False

    def test_a_kick_refreshes_in_the_background_and_stop_ends_the_thread(self) -> None:
        settings = self.loaded()
        settings.start_refresh()
        thread = settings._thread
        self.addCleanup(settings.stop_refresh)
        self.fake.set(secrets=ARMED)
        settings.kick()
        self.assertTrue(self.wait_for(lambda: settings["delivery_enabled"]))
        settings.stop_refresh()
        self.assertFalse(thread.is_alive())

    def test_a_kick_takes_no_lock(self) -> None:
        settings = self.loaded()
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        settings._kick_write = write_end
        # A signal handler may interrupt the very thread holding one of these.
        with (
            mock.patch.object(threading.Event, "set", side_effect=AssertionError("Event.set")),
            mock.patch.object(threading.Condition, "notify_all", side_effect=AssertionError("notify")),
            mock.patch.object(WS, "_log", side_effect=AssertionError("write to stderr")),
            mock.patch.object(settings, "refresh", side_effect=AssertionError("refresh in handler")),
        ):
            settings.kick()
        self.assertEqual(os.read(read_end, 1), b"\0")

    def test_a_kick_with_a_full_pipe_is_harmless(self) -> None:
        settings = self.loaded()
        settings._kick_write = 99
        with mock.patch.object(WS.os, "write", side_effect=BlockingIOError):
            settings.kick()
        settings._kick_write = None

    def test_a_kick_before_the_thread_starts_is_harmless(self) -> None:
        self.loaded().kick()

    def test_stop_closes_the_pipe(self) -> None:
        settings = self.loaded()
        settings.start_refresh()
        read_end, write_end = settings._read_end, settings._kick_write
        settings.stop_refresh()
        self.assertIsNone(settings._kick_write)
        for descriptor in (read_end, write_end):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_the_refresh_thread_runs_on_its_interval(self) -> None:
        settings = self.loaded()
        settings.interval = 0.01
        settings.start_refresh()
        self.addCleanup(settings.stop_refresh)
        self.fake.set(secrets={**ARMED, "WAKE_ROUTER_SETTINGS_REFRESH_SECONDS": "30"})
        self.assertTrue(self.wait_for(lambda: settings["delivery_enabled"]))


class Wiring(Settings):
    def test_the_router_runs_on_the_knobs_and_not_the_file(self) -> None:
        self.local(delivery_enabled=True)
        self.fake.set(
            secrets={
                WS.DELIVERY_ENABLED: "false",
                WS.DELIVERY_SEATS: "CLAUDE",
                "WAKE_ROUTER_MAX_ATTEMPTS": "9",
            }
        )
        seen = {}
        before = signal.getsignal(signal.SIGHUP)

        def serve(seats, bind, port):
            router = seats.routers[CW.SEAT]
            seen.update(config=router.config, hup=signal.getsignal(signal.SIGHUP))
            seen.update(threads=refresh_threads(), attempts=router.config["max_attempts"])
            seen.update(address=(bind, port))
            return 0

        with (
            mock.patch.dict(os.environ, {"CLAUDE_WAKE_STATE": str(self.root)}),
            mock.patch.object(CW, "state_dir", return_value=self.state),
            mock.patch.object(WR, "serve", side_effect=serve),
        ):
            self.assertEqual(SVC.main(["serve"]), 0)
        self.assertIsInstance(seen["config"], WS.WakeSettings)
        self.assertIs(seen["config"]["delivery_enabled"], False)
        self.assertEqual(seen["attempts"], 9)  # loaded before the router serves
        self.assertEqual(seen["address"], ("127.0.0.1", 8794))
        self.assertIsNot(seen["hup"], before)
        self.assertEqual(len(seen["threads"]), 1, "one refresh thread for every seat")
        self.assertFalse(seen["threads"][0].is_alive())
        self.assertEqual(signal.getsignal(signal.SIGHUP), before)

    def test_the_router_checks_the_bearer_from_the_knob_and_reads_no_file(self) -> None:
        planted = "f1le-" + "fedcba9876543210" * 2
        (self.state / "routine-key").write_text(planted, encoding="utf-8")
        os.chmod(self.state / "routine-key", 0o600)
        self.fake.set(secrets={**ARMED, KEY_KNOB: KEY})
        seen = {}

        def serve(seats, bind, port):
            router = seats.routers[CW.SEAT]
            seen["key"] = router.routine_key()
            self.fake.set(mode="fail")
            router.config.refresh()  # an outage on a later refresh keeps the key held
            seen["after"] = router.routine_key()
            return 0

        with (
            mock.patch.dict(os.environ, {"CLAUDE_WAKE_KEY_FILE": str(self.state / "routine-key")}),
            mock.patch.object(CW, "state_dir", return_value=self.state),
            mock.patch.object(WR, "serve", side_effect=serve),
        ):
            self.assertEqual(SVC.main(["serve"]), 0)
        self.assertEqual(seen["key"], (KEY, ""))
        self.assertEqual(seen["after"], (KEY, ""))

    def test_sighup_refreshes(self) -> None:
        self.fake.set(secrets={})
        seen = {}

        def serve(seats, bind, port):
            router = seats.routers[CW.SEAT]
            self.fake.set(secrets=ARMED)
            os.kill(os.getpid(), signal.SIGHUP)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not router.config["delivery_enabled"]:
                time.sleep(0.01)
            seen["armed"] = router.config["delivery_enabled"]
            return 0

        with (
            mock.patch.object(CW, "state_dir", return_value=self.state),
            mock.patch.object(WR, "serve", side_effect=serve),
        ):
            SVC.main(["serve"])
        self.assertIs(seen["armed"], True)

    def test_every_registered_seat_is_served_whatever_the_delivery_seats_say(self) -> None:
        self.fake.set(secrets={WS.DELIVERY_ENABLED: "true", WS.DELIVERY_SEATS: "ZC"})
        seen = {}

        def serve(seats, bind, port):
            seen.update(names=seats.names(), served=sorted(seats.routers))
            seen["delivery"] = seats.routers[CW.SEAT].config["delivery_enabled"]
            return 0

        with (
            mock.patch.object(CW, "state_dir", return_value=self.state),
            mock.patch.object(WR, "serve", side_effect=serve),
        ):
            SVC.main(["serve"])
        # Every registered seat is either served or named down with a reason.
        self.assertEqual(seen["names"], sorted(SVC.BUILDERS))
        self.assertIn(CW.SEAT, seen["served"])
        self.assertIs(seen["delivery"], False, "served, and held until the knob names it")

    def test_seats_whose_modules_are_not_installed_do_not_fail_health(self) -> None:
        """Codex, Muse Code and ZCode are registered before their modules land on main:  each
        shows as not-installed, the overall health stays 200 and CLAUDE is served."""
        self.fake.set(secrets={**ARMED, KEY_KNOB: KEY, **REGISTRY})
        pending = {SVC.BUILDERS[seat].module for seat in ("CODEX", "MC", "ZC")}
        real = importlib.import_module

        def importer(name):
            if name in pending:
                raise ModuleNotFoundError("No module named %r" % name, name=name)
            return real(name)

        with mock.patch.object(CW, "state_dir", return_value=self.state):
            served = SVC.build({"HOME": str(self.home)}, importer=importer)
        self.assertEqual(served.seats.not_installed, {"CODEX", "MC", "ZC"})
        self.assertEqual((served.seats.down, sorted(served.seats.routers)), ({}, [CW.SEAT]))
        status, health = served.seats.health()
        self.assertEqual(status, 200)
        self.assertEqual(health["seats"]["MC"], {"healthy": False, "reason": "not-installed"})
        self.assertEqual(health["seats"][CW.SEAT], {"healthy": True, "delivery": True})
        self.assertEqual(served.seats.seat_health("MC"), (503, {
            "seat": "MC", "healthy": False, "reason": "not-installed"}))

    def test_muse_code_present_but_broken_disables_only_mc_and_fails_health(self) -> None:
        self.fake.set(secrets={**ARMED, KEY_KNOB: KEY, **REGISTRY})
        real = importlib.import_module
        muse_module = SVC.BUILDERS["MC"].module
        pending = {SVC.BUILDERS[seat].module for seat in ("CODEX", "ZC")}

        def failing(env, settings):
            raise RuntimeError("no muse workspace")

        def missing_dependency():
            raise ModuleNotFoundError("No module named 'muse_sdk'", name="muse_sdk")

        cases = {
            "no-builder": SimpleNamespace(SEAT="MC"),
            "builder-error-RuntimeError": SimpleNamespace(SEAT="MC", router_platform=failing),
            "import-error-ModuleNotFoundError": missing_dependency,
        }
        for reason, muse in cases.items():
            with self.subTest(reason=reason):

                def importer(name, muse=muse):
                    if name in pending:
                        raise ModuleNotFoundError(name, name=name)
                    if name == muse_module:
                        return muse() if callable(muse) else muse
                    return real(name)

                with mock.patch.object(CW, "state_dir", return_value=self.state):
                    served = SVC.build({"HOME": str(self.home)}, importer=importer)
                self.assertEqual(served.seats.down, {"MC": reason})
                self.assertEqual(sorted(served.seats.routers), [CW.SEAT])
                status, health = served.seats.health()
                self.assertEqual(status, 503, "a module that is there and broken is a fault")
                self.assertEqual(
                    health["seats"]["MC"],
                    {"healthy": False, "reason": "seat-not-served:" + reason},
                )
                self.assertEqual(health["seats"][CW.SEAT], {"healthy": True, "delivery": True})
                claude = served.seats.routers[CW.SEAT]
                wake = WR.Wake(wake_id="w-%s" % reason, seat=CW.SEAT)
                self.assertEqual(claude.accept(wake)[0], 202)
                self.assertEqual([m.seat for m in served.settings.members], [CW.SEAT])

    def test_a_non_prod_environment_stops_the_service(self) -> None:
        os.environ[source.ENV_NAMES[0]] = "dev"
        self.addCleanup(os.environ.pop, source.ENV_NAMES[0], None)
        with (
            mock.patch.object(WR, "serve", side_effect=AssertionError("served")),
            self.assertRaises(SystemExit),
        ):
            SVC.main(["serve"])


class RouterOnSettings(Settings):
    def test_a_router_on_unconfirmed_settings_keeps_its_wakes(self) -> None:
        del os.environ[source.ENV_KNOBS_TOKEN]
        settings = WS.WakeSettings(CW.SEAT, CW.DEFAULTS, self.local_path)
        settings.load()
        platform = WR.Platform(
            seat=CW.SEAT,
            platform=CW.PLATFORM,
            mention=CW.MENTION,
            target_re=CW.SESSION_RE,
            deliver=mock.Mock(),
        )
        router = WR.Router(platform, {}, settings, self.state, settings.routine_key)
        router.accept(WR.Wake(wake_id="w-1", seat=CW.SEAT))
        self.assertTrue(router.run_once())
        self.assertTrue((self.state / "pending" / "w-1.json").exists())
        self.assertFalse((self.state / "done" / "w-1.json").exists())
        platform.deliver.assert_not_called()

    def test_the_mac_reads_its_limits_through_the_knobs(self) -> None:
        self.fake.set(secrets={"WAKE_ROUTER_RESUME_TIMEOUT_SECONDS": "120"})
        self.local(confirm_seconds=5, delivery_enabled=True)
        with mock.patch.object(CW, "state_dir", return_value=self.state):
            config = CW._remote_config({})
        self.assertEqual(config["resume_timeout_seconds"], 120)
        self.assertEqual(config["confirm_seconds"], 5)
        self.assertIs(config["delivery_enabled"], False)


INHERITED = {
    registry_api.URL_ENV: "https://inherited.example.test",
    registry_api.KEY_ENV: "inherited-" + "0" * 20,
    registry_api.KEY_REF_ENV: "OTHER_KEY",
}


class Registry(Settings):
    """The registry's address and key come from /wake-router knobs, held like the routine key, and
    nothing inherited is ever a fallback."""

    def test_the_router_env_carries_the_registry_from_the_knobs(self) -> None:
        settings = self.loaded(**REGISTRY)
        env = settings.registry_env({"HOME": str(self.home), **INHERITED, "OTHER_KEY": "x"})
        self.assertEqual(env[registry_api.URL_ENV], REGISTRY_URL)
        self.assertEqual(env[registry_api.KEY_ENV], REGISTRY_KEY)
        self.assertNotIn(registry_api.KEY_REF_ENV, env)
        self.assertEqual(env["HOME"], str(self.home))

    def test_without_it_every_call_is_not_configured_and_nothing_is_inherited(self) -> None:
        for secrets in ({}, {WS.REGISTRY_URL: REGISTRY_URL}, {WS.REGISTRY_KEY: REGISTRY_KEY}):
            with self.subTest(secrets=secrets):
                self.new_process()
                settings = self.loaded(**secrets)
                env = settings.registry_env({"HOME": str(self.home), **INHERITED})
                self.assertEqual(env.get(registry_api.KEY_ENV, ""), secrets.get(WS.REGISTRY_KEY, ""))
                self.assertEqual(registry_api.base_url(env), secrets.get(WS.REGISTRY_URL, ""))
                answer = registry_api.call(env, "history", {"limit": 1})
                self.assertEqual(answer["code"], "not_configured")
                self.assertIn("registry-not-configured", self.stderr.getvalue())

    def test_a_refresh_brings_it_and_an_outage_keeps_it(self) -> None:
        settings = self.loaded()
        env = settings.registry_env({})
        self.assertEqual(registry_api.base_url(env), "")
        self.fake.set(secrets=REGISTRY)
        self.assertTrue(settings.refresh())
        self.assertEqual(registry_api.base_url(env), REGISTRY_URL, "the env reads it live")
        self.fake.set(mode="fail")
        self.assertFalse(settings.refresh())
        self.assertEqual(settings.registry(), (REGISTRY_URL, REGISTRY_KEY))

    def test_a_good_fetch_without_it_drops_it(self) -> None:
        settings = self.loaded(**REGISTRY)
        self.fake.set(secrets={WS.REGISTRY_URL: REGISTRY_URL})
        self.assertTrue(settings.refresh())
        self.assertEqual(settings.registry(), (REGISTRY_URL, ""))

    def test_the_mac_reads_it_once_from_the_knobs(self) -> None:
        self.fake.set(secrets=REGISTRY)
        env = WS.registry_env_once({"HOME": str(self.home), **INHERITED})
        self.assertEqual(
            (env[registry_api.URL_ENV], env[registry_api.KEY_ENV]), (REGISTRY_URL, REGISTRY_KEY)
        )
        # In an outage the address may come from the last-known-good copy, but the key is a
        # secret and fails closed, and nothing inherited stands in for it:  not configured.
        self.new_process()
        self.fake.set(mode="fail")
        env = WS.registry_env_once({"HOME": str(self.home), **INHERITED})
        self.assertNotIn(registry_api.KEY_ENV, env)
        self.assertNotEqual(env.get(registry_api.URL_ENV), INHERITED[registry_api.URL_ENV])
        self.assertEqual(registry_api.call(env, "history", {})["code"], "not_configured")

    def test_the_registry_key_is_never_logged(self) -> None:
        settings = self.loaded(**REGISTRY)
        self.fake.set(mode="fail")
        settings.refresh()
        self.assertNotIn(REGISTRY_KEY, self.stderr.getvalue())


def refresh_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "wake-settings-refresh"]


FAKE_SESS = r"sess_[a-z0-9]{4,40}"


def fake_module(
    seat: str, state, *, build=None, defaults=None, platform=None, session=FAKE_SESS
) -> SimpleNamespace:
    """A platform module as the service imports it:  its SEAT, its builder and its state folder."""

    def make_platform(env, settings):
        return WR.Platform(
            seat=seat,
            platform=platform or seat.lower(),
            mention=seat.title(),
            target_re=re.compile(session),
            deliver=mock.Mock(return_value=WR.Delivery("completed")),
        )

    module = SimpleNamespace(SEAT=seat, make_platform=build or make_platform)
    module.state_dir = lambda env: state / seat.lower()
    if defaults is not None:
        module.DEFAULTS = defaults
    return module


def fake_builder(seat: str) -> SVC.Builder:
    return SVC.Builder(seat.lower(), "fake.%s" % seat.lower(), "make_platform", session_re=FAKE_SESS)


class OneService(Settings):
    """The one service:  every registered seat built on its own, refreshed together."""

    def build(self, modules: dict, builders: dict | None = None) -> SVC.Served:
        def importer(name: str):
            if name not in modules:
                raise ModuleNotFoundError("No module named %r" % name, name=name)
            found = modules[name]
            if isinstance(found, Exception):
                raise found
            return found

        table = builders or {seat: fake_builder(seat) for seat in modules_seats(modules)}
        return SVC.build({"HOME": str(self.home)}, table, importer=importer)

    def two_seats(self, **build) -> SVC.Served:
        return self.build(
            {
                "fake.claude": fake_module("CLAUDE", self.state),
                "fake.zc": fake_module("ZC", self.state, **build),
            }
        )

    @property
    def shared_file(self):
        return self.home / ".local" / "state" / "wake-router" / "config.json"

    def test_two_seats_have_their_own_folders_and_settings_and_one_key(self) -> None:
        self.fake.set(secrets={**ARMED, KEY_KNOB: KEY, **REGISTRY})
        served = self.two_seats()
        claude, zc = served.seats.routers["CLAUDE"], served.seats.routers["ZC"]
        self.assertEqual(served.seats.down, {})
        self.assertEqual((claude.state, zc.state), (self.state / "claude", self.state / "zc"))
        self.assertEqual(claude.routine_key(), (KEY, ""))
        self.assertEqual(zc.routine_key(), (KEY, ""), "every seat answers to the one key")
        self.assertIsNot(claude.config, zc.config)
        self.assertIs(claude.config["delivery_enabled"], True)
        self.assertIs(zc.config["delivery_enabled"], False, "ZC is not in the delivery seats")
        self.assertEqual([m.seat for m in served.settings.members], ["CLAUDE", "ZC"])
        self.assertEqual(served.seats.health()[0], 200)
        for router in (claude, zc):
            self.assertEqual(registry_api.base_url(router.env), REGISTRY_URL)

    def test_one_refresh_is_one_fetch_for_every_seat(self) -> None:
        self.fake.set(secrets={KEY_KNOB: KEY})
        served = self.two_seats()
        before = len(self.fake.calls())
        self.fake.set(secrets={WS.DELIVERY_ENABLED: "true", WS.DELIVERY_SEATS: "CLAUDE,ZC"})
        self.assertTrue(served.settings.refresh())
        self.assertEqual(len(self.fake.calls()), before + 1)
        for router in served.seats.routers.values():
            self.assertIs(router.config["delivery_enabled"], True)
            self.assertEqual(router.routine_key()[1], "unset")

    def test_an_outage_keeps_the_key_for_every_seat(self) -> None:
        self.fake.set(secrets={KEY_KNOB: KEY, **REGISTRY})
        served = self.two_seats()
        self.fake.set(mode="fail")
        self.assertFalse(served.settings.refresh())
        for router in served.seats.routers.values():
            self.assertEqual(router.routine_key(), (KEY, ""))
        self.assertEqual(served.seats.health()[0], 200, "no seat is turned away by the outage")

    def test_without_the_key_every_seat_refuses_and_health_says_why(self) -> None:
        self.fake.set(secrets=REGISTRY)
        served = self.two_seats()
        status, health = served.seats.health()
        self.assertEqual(status, 503)
        for seat in ("CLAUDE", "ZC"):
            self.assertEqual(health["seats"][seat]["reason"], "routine-key-unavailable:unset")

    def test_a_failing_builder_downs_only_its_seat(self) -> None:
        self.fake.set(secrets={KEY_KNOB: KEY})

        def broken(env, settings):
            raise RuntimeError("no workspace")

        served = self.two_seats(build=broken)
        self.assertEqual(sorted(served.seats.routers), ["CLAUDE"])
        self.assertEqual(served.seats.down, {"ZC": "builder-error-RuntimeError"})
        self.assertEqual([m.seat for m in served.settings.members], ["CLAUDE"])

    def test_each_way_a_seat_cannot_be_built_is_named(self) -> None:
        self.fake.set(secrets={})
        cases = {
            "import-error-ValueError": ValueError("bad module"),
            "import-error-ModuleNotFoundError": ModuleNotFoundError("dep", name="some_dependency"),
            "seat-mismatch": fake_module("CODEX", self.state),
            "no-builder": SimpleNamespace(SEAT="ZC", state_dir=lambda env: self.state),
            "bad-defaults": fake_module("ZC", self.state, defaults={"workers": 1}),
            "builder-bad-platform": fake_module("ZC", self.state, build=lambda env, s: object()),
            "platform-name-mismatch": fake_module("ZC", self.state, platform="zedcode"),
            "session-pattern-mismatch": fake_module("ZC", self.state, session=r"sess_.+"),
        }
        bad_never_ran = fake_module("ZC", self.state)
        bad_never_ran.NEVER_RAN = {"Not A Code"}
        cases["bad-never-ran"] = bad_never_ran
        for reason, module in cases.items():
            with self.subTest(reason=reason):
                served = self.build(
                    {"fake.claude": fake_module("CLAUDE", self.state), "fake.zc": module},
                    {"CLAUDE": fake_builder("CLAUDE"), "ZC": fake_builder("ZC")},
                )
                self.assertEqual(served.seats.down, {"ZC": reason})
                self.assertEqual(served.seats.not_installed, set())
                self.assertEqual(sorted(served.seats.routers), ["CLAUDE"])

    def test_a_module_not_there_yet_is_not_installed_and_not_a_fault(self) -> None:
        self.fake.set(secrets={KEY_KNOB: KEY, **REGISTRY})
        served = self.build(
            {"fake.claude": fake_module("CLAUDE", self.state)},
            {"CLAUDE": fake_builder("CLAUDE"), "ZC": fake_builder("ZC")},
        )
        self.assertEqual((served.seats.not_installed, served.seats.down), ({"ZC"}, {}))
        self.assertEqual(served.seats.health()[0], 200)
        self.assertIn("seat ZC not served (not-installed)", self.stderr.getvalue())

    def test_a_modules_own_never_ran_codes_reach_its_router(self) -> None:
        self.fake.set(secrets={})
        zc = fake_module("ZC", self.state)
        zc.NEVER_RAN = frozenset({"workspace-mismatch", "subagent-child"})
        served = self.build({"fake.claude": fake_module("CLAUDE", self.state), "fake.zc": zc})
        self.assertLessEqual(zc.NEVER_RAN, served.seats.routers["ZC"].never_ran)
        self.assertFalse(zc.NEVER_RAN & served.seats.routers["CLAUDE"].never_ran)
        self.assertFalse(zc.NEVER_RAN & WR.NEVER_RAN, "the pinned set does not grow")

    def test_the_seat_folder_defaults_under_the_routers_own(self) -> None:
        module = fake_module("ZC", self.state)
        del module.state_dir
        self.fake.set(secrets={})
        served = self.build({"fake.zc": module}, {"ZC": fake_builder("ZC")})
        expected = self.home / ".local" / "state" / "wake-router" / "zc"
        self.assertEqual(served.seats.routers["ZC"].state, expected)

    def test_every_seat_reads_the_one_local_file(self) -> None:
        self.fake.set(secrets={})
        served = self.two_seats()
        for settings in served.settings.members:
            self.assertEqual(settings.local_path, self.shared_file)

    def local_file(self, **values) -> None:
        self.shared_file.parent.mkdir(parents=True, exist_ok=True)
        self.shared_file.write_text(json.dumps(values), encoding="utf-8")

    def test_the_one_file_narrows_every_seat(self) -> None:
        self.fake.set(secrets={**ARMED, WS.DELIVERY_SEATS: "CLAUDE,ZC"})
        self.local_file(max_deliveries=2)
        served = self.two_seats()
        for router in served.seats.routers.values():
            self.assertEqual(router.config["max_deliveries"], 2)
            self.assertIs(router.config["delivery_enabled"], True)

    def test_delivery_seats_in_the_file_can_only_remove_seats(self) -> None:
        self.fake.set(secrets={**ARMED, WS.DELIVERY_SEATS: "CLAUDE"})
        self.local_file(delivery_seats=["ZC"])
        served = self.two_seats()
        self.assertIs(served.seats.routers["CLAUDE"].config["delivery_enabled"], False, "removed")
        self.assertIs(served.seats.routers["ZC"].config["delivery_enabled"], False, "never added")
        self.new_process()
        self.fake.set(secrets={**ARMED, WS.DELIVERY_SEATS: "CLAUDE,ZC"})
        self.local_file(delivery_seats=["ZC", "GROK"])
        served = self.two_seats()
        self.assertIs(served.seats.routers["CLAUDE"].config["delivery_enabled"], False)
        self.assertIs(served.seats.routers["ZC"].config["delivery_enabled"], True)

    def test_a_key_one_seat_has_does_not_refuse_the_file_for_another(self) -> None:
        self.fake.set(secrets={})
        self.local_file(claude_bin="/opt/claude/bin/claude")
        served = self.build(
            {
                "fake.claude": fake_module("CLAUDE", self.state, defaults=CW.DEFAULTS),
                "fake.zc": fake_module("ZC", self.state),
            }
        )
        self.assertEqual(served.seats.down, {})
        self.assertEqual(served.seats.routers["CLAUDE"].config["claude_bin"], "/opt/claude/bin/claude")
        self.assertNotIn("claude_bin", served.seats.routers["ZC"].config)

    def test_a_malformed_file_is_refused_as_before(self) -> None:
        self.fake.set(secrets={})
        for text in ("{not json", '{"no_such_setting": 1}', '{"delivery_seats": "CLAUDE"}'):
            with self.subTest(text=text):
                self.shared_file.parent.mkdir(parents=True, exist_ok=True)
                self.shared_file.write_text(text, encoding="utf-8")
                served = self.two_seats()
                self.assertEqual(served.seats.down, {"CLAUDE": "settings-refused", "ZC": "settings-refused"})

    def test_a_file_broken_later_holds_what_every_seat_had(self) -> None:
        self.fake.set(secrets={**ARMED, WS.DELIVERY_SEATS: "CLAUDE,ZC"})
        served = self.two_seats()
        self.local_file()
        self.shared_file.write_text("{not json", encoding="utf-8")
        self.assertFalse(served.settings.refresh())
        for router in served.seats.routers.values():
            self.assertIs(router.config["delivery_enabled"], True)

    def test_the_table(self) -> None:
        uuid = SVC.UUID_SESSION
        self.assertEqual(
            {seat: (b.platform, b.module, b.function, b.handler, b.session_re) for seat, b in SVC.BUILDERS.items()},
            {
                "CLAUDE": ("claude", "simple_tools.claude_wake", "make_platform", "_deliver_claude", uuid),
                "CODEX": ("codex", "simple_tools.codex_wake", "router_platform", "deliver_request", uuid),
                "MC": ("muse", "simple_tools.muse_wake", "router_platform", "deliver_request", uuid),
                "ZC": ("zcode", "simple_tools.zcode_wake", "router_platform", "deliver_request",
                       r"^sess_[A-Za-z0-9._-]{1,123}$"),
            },
        )

    def test_claudes_entry_matches_its_module(self) -> None:
        entry = SVC.BUILDERS[CW.SEAT]
        self.assertEqual(entry.platform, CW.PLATFORM)
        self.assertEqual(entry.session_re, CW.SESSION_RE.pattern)
        self.assertTrue(callable(getattr(CW, entry.function)))
        self.assertTrue(callable(getattr(CW, entry.handler)))


def modules_seats(modules: dict) -> list[str]:
    return [name.split(".", 1)[1].upper() for name in modules]
