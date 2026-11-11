"""The wake router's settings:  Infisical through fleet_knobs, held to the service contract (OPS-1048).
Infisical is a fake at the scoped reader's boundary, as in the fleet_knobs tests, so nothing here
reaches a real store, and no router is started."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import time
from unittest import mock

from fleet_knobs import source
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
        self.assertIn("stale service-token-required", self.stderr.getvalue())

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

    def test_the_mac_reads_its_limits_through_the_knobs(self) -> None:
        self.fake.set(secrets={"WAKE_ROUTER_RESUME_TIMEOUT_SECONDS": "120"})
        self.local(confirm_seconds=5, delivery_enabled=True)
        with mock.patch.object(CW, "state_dir", return_value=self.state):
            config = CW._remote_config({})
        self.assertEqual(config["resume_timeout_seconds"], 120)
        self.assertEqual(config["confirm_seconds"], 5)
        self.assertIs(config["delivery_enabled"], False)
