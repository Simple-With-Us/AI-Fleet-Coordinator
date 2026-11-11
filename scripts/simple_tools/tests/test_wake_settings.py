"""The wake router's settings:  Infisical through fleet_knobs, held to the service contract (OPS-1048).
Infisical is a fake at the scoped reader's boundary, as in the fleet_knobs tests, so nothing here
reaches a real store, and no router is started."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import threading
import time
from unittest import mock

from fleet_knobs import knobs, source
from fleet_knobs.registry import BY_NAME
from fleet_knobs.tests._fake import KnobTestCase
from simple_tools import claude_wake as CW
from simple_tools import wake_router as WR
from simple_tools import wake_settings as WS

ARMED = {WS.DELIVERY_ENABLED: "true", WS.DELIVERY_SEATS: "CLAUDE"}


class Settings(KnobTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.local_path = self.state / "config.json"
        stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr = stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

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
        self.assertTrue(self.lkg_file.exists())

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
        self.lkg_file.unlink()
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

        def serve(router, bind, port):
            seen.update(config=router.config, hup=signal.getsignal(signal.SIGHUP))
            seen.update(thread=router.config._thread, attempts=router.config["max_attempts"])
            return 0

        with (
            mock.patch.dict(os.environ, {"CLAUDE_WAKE_STATE": str(self.root)}),
            mock.patch.object(CW, "state_dir", return_value=self.state),
            mock.patch.object(WR, "serve", side_effect=serve),
        ):
            self.assertEqual(CW.main(["serve"]), 0)
        self.assertIsInstance(seen["config"], WS.WakeSettings)
        self.assertIs(seen["config"]["delivery_enabled"], False)
        self.assertEqual(seen["attempts"], 9)  # loaded before the router serves
        self.assertIsNot(seen["hup"], before)
        self.assertFalse(seen["thread"].is_alive())
        self.assertEqual(signal.getsignal(signal.SIGHUP), before)

    def test_sighup_refreshes(self) -> None:
        self.fake.set(secrets={})
        seen = {}

        def serve(router, bind, port):
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
            CW.main(["serve"])
        self.assertIs(seen["armed"], True)

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
        router = WR.Router(platform, {}, settings, self.state, self.state / "key")
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
