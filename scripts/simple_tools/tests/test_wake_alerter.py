"""Tests for the Mac wake alerter (hosted-listener thin client)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import fleet_infisical as FI

from simple_tools import wake_alerter as WA

KEY = "r" * 40
SEAT = "ECHO"
BASE = "http://listener.example.ts.net:8788"


def payload(items):
    return {"contract": "simple-tools-read/1", "seat": SEAT, "record": "wakes",
            "view": {"seat": SEAT, "total": len(items), "by_state": {}, "items": items}}


class Resp:
    def __init__(self, data):
        self._raw = json.dumps(data).encode()

    def read(self, _n):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AlerterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = os.path.join(self.temp.name, "state.json")
        self.ran = []

    def poll(self, items=None, opener=None):
        if opener is None:
            def opener(req, timeout=0):
                return Resp(payload(items or []))
        return WA.poll_once(BASE, SEAT, KEY, self.state, runner=self.ran.append, opener=opener)

    def test_new_wake_banners_once(self):
        items = [{"wake_id": "w1", "state": "queued", "channel": "work-in-progress", "topic": "fleet"}]
        self.assertEqual(self.poll(items), 1)
        self.assertEqual(len(self.ran), 1)
        argv = self.ran[0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertIn("wake in work-in-progress / fleet", argv)
        self.assertIn("Operator wake (ECHO)", argv)
        # second poll with the same wake: no repeat banner
        self.assertEqual(self.poll(items), 0)
        self.assertEqual(len(self.ran), 1)

    def test_dm_without_channel_banners_dm(self):
        self.assertEqual(self.poll([{"wake_id": "w2", "state": "queued", "dm": True}]), 1)
        self.assertIn("wake in DM", self.ran[0])

    def test_seen_state_survives_restart(self):
        self.assertEqual(self.poll([{"wake_id": "w3", "state": "queued"}]), 1)
        self.ran.clear()
        # a fresh "process" (new runner list, same state file) does not repeat
        self.assertEqual(self.poll([{"wake_id": "w3", "state": "queued"}]), 0)
        self.assertEqual(self.ran, [])

    def test_server_down_banners_nothing_and_keeps_state(self):
        def boom(req, timeout=0):
            raise urllib.error.URLError("refused")

        self.assertEqual(self.poll([{"wake_id": "w4", "state": "queued"}]), 1)
        self.assertEqual(self.poll(opener=boom), 0)
        self.assertEqual(self.poll([{"wake_id": "w4", "state": "queued"}]), 0)

    def test_malformed_items_are_skipped(self):
        items = [None, "x", {"state": "queued"}, {"wake_id": 7}, {"wake_id": "w5", "state": "queued"}]
        self.assertEqual(self.poll(items), 1)
        self.assertEqual(len(self.ran), 1)

    def test_request_is_signed_for_the_exact_query(self):
        seen_req = []

        def opener(req, timeout=0):
            seen_req.append(req)
            return Resp(payload([]))

        self.poll(opener=opener)
        req = seen_req[0]
        path = "/listener/v1/ECHO/wakes"
        stamp = req.headers["X-simple-tools-timestamp"]
        from simple_tools.listener_api import sign

        self.assertEqual(req.headers["X-simple-tools-signature"], sign(KEY, path, WA.QUERY, stamp))
        self.assertTrue(req.full_url.endswith(path + "?" + WA.QUERY))


class HookTests(unittest.TestCase):
    """Hook mode: one configured command per new wake (headless seats)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = os.path.join(self.temp.name, "state.json")
        self.calls = []

    def poll(self, items, rc=0, hook="/opt/fleet/muse-wake.sh {seat} {wake_id} {channel} {topic}"):
        def runner(argv, env=None, timeout=0):
            self.calls.append((argv, dict(env or {})))
            return rc

        def opener(req, timeout=0):
            return Resp(payload(items))

        return WA.poll_once(BASE, SEAT, KEY, self.state, runner=runner, opener=opener, hook=hook)

    def test_hook_runs_once_per_new_wake_with_metadata_only(self):
        items = [{"wake_id": "w9", "state": "queued", "channel": "work-in-progress", "topic": "fleet"}]
        self.assertEqual(self.poll(items), 1)
        argv, env = self.calls[0]
        self.assertEqual(argv, ["/opt/fleet/muse-wake.sh", "ECHO", "w9", "work-in-progress", "fleet"])
        self.assertEqual(env["OPERATOR_WAKE_ID"], "w9")
        self.assertEqual(env["OPERATOR_WAKE_CHANNEL"], "work-in-progress")
        self.assertNotIn("body", " ".join(argv).lower())
        self.assertEqual(self.poll(items), 0)

    def test_failed_hook_retries_next_poll(self):
        items = [{"wake_id": "w10", "state": "queued", "channel": "ops"}]
        self.assertEqual(self.poll(items, rc=1), 0)
        self.assertEqual(self.poll(items, rc=1), 0)
        self.assertEqual(self.poll(items, rc=0), 1)
        self.assertEqual(self.poll(items, rc=0), 0)
        self.assertEqual(len(self.calls), 3)

    def test_values_with_spaces_stay_one_argument(self):
        items = [{"wake_id": "w11", "state": "queued", "channel": "fleet ops", "topic": "a b"}]
        self.assertEqual(self.poll(items), 1)
        argv, _ = self.calls[0]
        self.assertEqual(argv[3:], ["fleet ops", "a b"])

    def test_missing_fields_become_empty_strings(self):
        items = [{"wake_id": "w12", "state": "queued"}]
        self.assertEqual(self.poll(items), 1)
        argv, env = self.calls[0]
        self.assertEqual(argv[3:], ["", ""])
        self.assertEqual(env["OPERATOR_WAKE_CHANNEL"], "")


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = os.path.join(self.temp.name, "state.json")
        self.calls = []

    def poll(self, items, hook="", rc=0, body=None, opener=None):
        def runner(argv, env=None, timeout=0):
            self.calls.append((argv, dict(env or {})))
            return rc

        if opener is None:
            def opener(req, timeout=0):
                return Resp(body if body is not None else payload(items))
        return WA.poll_once(BASE, SEAT, KEY, self.state, runner=runner, opener=opener, hook=hook)

    def test_only_wakes_that_ran_or_will_run_alert(self):
        items = [{"wake_id": "a", "state": "held_back"}, {"wake_id": "b", "state": "dropped"},
                 {"wake_id": "c", "state": "queued"}, {"wake_id": "d"}]
        self.assertEqual(self.poll(items), 1)

    def test_duplicate_ids_in_one_response_alert_once(self):
        items = [{"wake_id": "a", "state": "queued"}, {"wake_id": "a", "state": "started"}]
        self.assertEqual(self.poll(items), 1)

    def test_a_response_for_another_seat_or_contract_is_ignored(self):
        bad = payload([{"wake_id": "a", "state": "queued"}])
        bad["seat"] = "OTHER"
        self.assertEqual(self.poll([], body=bad), 0)
        bad = payload([{"wake_id": "a", "state": "queued"}])
        bad["contract"] = "x/1"
        self.assertEqual(self.poll([], body=bad), 0)

    def test_failed_banner_is_not_marked_seen(self):
        items = [{"wake_id": "a", "state": "queued"}]
        self.assertEqual(self.poll(items, rc=1), 0)
        self.assertEqual(self.poll(items, rc=0), 1)

    def test_braces_and_quotes_in_values_stay_in_one_argument(self):
        items = [{"wake_id": "a", "state": "queued", "channel": "c", "topic": "x {y} \"q\" ${Z}"}]
        self.assertEqual(self.poll(items, hook="/h {seat} {topic}"), 1)
        self.assertEqual(self.calls[0][0], ["/h", "ECHO", 'x {y} "q" ${Z}'])

    def test_a_broken_hook_template_does_not_raise(self):
        items = [{"wake_id": "a", "state": "queued"}]
        self.assertEqual(self.poll(items, hook="/h 'unterminated {seat}"), 0)
        self.assertEqual(self.calls, [])

    def test_non_string_metadata_is_coerced_and_the_routine_key_is_withheld(self):
        os.environ["ECHO_ROUTINE_KEY"] = KEY
        self.addCleanup(os.environ.pop, "ECHO_ROUTINE_KEY", None)
        items = [{"wake_id": "a", "state": "queued", "channel": 5, "topic": None}]
        self.assertEqual(self.poll(items, hook="/h {seat}"), 1)
        env = self.calls[0][1]
        self.assertEqual(env["OPERATOR_WAKE_CHANNEL"], "")
        self.assertNotIn("ECHO_ROUTINE_KEY", env)

    def test_hook_timeout_setting_falls_back_to_the_default(self):
        for raw in ("abc", "0", "-5", ""):
            self.assertEqual(WA._hook_timeout(raw), 600)
        self.assertEqual(WA._hook_timeout("30"), 30)

    def test_a_bare_state_filename_still_saves(self):
        cwd = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, cwd)
        WA._save_seen("state.json", ["a"])
        self.assertEqual(WA._load_seen("state.json"), ["a"])

    def test_credential_shaped_topics_are_redacted_in_the_banner(self):
        text = WA._banner_text({"channel": "c", "topic": "key ghp_" + "a" * 30})
        self.assertNotIn("ghp_", text)

    def test_http_errors_are_logged_not_raised(self):
        def opener(req, timeout=0):
            raise urllib.error.HTTPError("u", 401, "no", {}, None)

        self.assertEqual(self.poll([], opener=opener), 0)


class KeyLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"HOME": self.temp.name, FI.ENV_PROJECT: "11111111-2222-3333-4444-555555555555",
                    FI.ENV_TOKEN: "st." + "synthetic-" * 4}
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_key_comes_from_the_named_direct_read_without_a_subprocess(self):
        os.environ["OPERATOR_SECRET_PATH"] = "/zulip"
        os.environ[FI.ENV_READ_PATHS] = "/zulip,/unrelated"
        with mock.patch.object(FI, "fetch_project", return_value={"ECHO_ROUTINE_KEY": "the-key-0123"}) as fetch:
            self.assertEqual(WA._fleet_secrets_get("ECHO_ROUTINE_KEY", run=mock.Mock(side_effect=AssertionError("no subprocess"))), "the-key-0123")
        self.assertEqual(fetch.call_args.args[:2], (self.env[FI.ENV_PROJECT], "/zulip"))
        self.assertEqual(fetch.call_args.args[2][FI.ENV_READ_PATHS], "/zulip")
        self.assertEqual(fetch.call_args.kwargs["names"], ("ECHO_ROUTINE_KEY",))
        self.assertEqual(fetch.call_args.kwargs["timeout"], WA.SECRET_TIMEOUT_SECONDS)

    def test_a_failing_or_missing_lookup_is_empty(self):
        with mock.patch.object(FI, "fetch_project", return_value={}):
            self.assertEqual(WA._fleet_secrets_get("N"), "")
        with mock.patch.object(FI, "fetch_project", side_effect=OSError("untrusted failure")):
            self.assertEqual(WA._fleet_secrets_get("N"), "")

    def test_failures_keep_their_reason_and_never_the_value(self):
        for value, exc, want in (
            ("secret-value-xyz", FI.SecretsError("untrusted failure"), "unavailable"),
            ("", None, "empty"),
            ("has space in it", None, "does not look like"),
            ("short", None, "does not look like"),
            ("x", TimeoutError("untrusted failure"), "timed out"),
            ("x", OSError("untrusted failure"), "unavailable"),
        ):
            with mock.patch.object(FI, "fetch_project", return_value={"ECHO_ROUTINE_KEY": value}, side_effect=exc):
                key, why = WA._read_key("ECHO_ROUTINE_KEY")
            self.assertEqual(key, "")
            self.assertIn(want, why)
            self.assertNotIn("secret-value-xyz", why)
            self.assertNotIn("untrusted failure", why)

    def test_a_missing_bootstrap_never_reaches_infisical(self):
        os.environ.pop(FI.ENV_TOKEN)
        with mock.patch.object(FI, "fetch_project") as fetch:
            self.assertEqual(WA._read_key("ECHO_ROUTINE_KEY")[0], "")
            fetch.assert_not_called()

    def test_project_selection_uses_only_that_projects_own_binding(self):
        os.environ.update({"OPERATOR_SECRET_PROJECT": "ai-fleet-coordinator",
                           "OPERATOR_SECRET_PATH": "/zulip",
                           "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID": "66666666-7777-8888-9999-000000000000"})
        with mock.patch.object(FI, "fetch_project") as fetch:
            self.assertEqual(WA._read_key("ECHO_ROUTINE_KEY")[0], "")
            fetch.assert_not_called()
        os.environ[FI.ENV_AI_FLEET_COORDINATOR_TOKEN] = "st." + "afc-synthetic-" * 3
        with mock.patch.object(FI, "fetch_project", return_value={"ECHO_ROUTINE_KEY": KEY}) as fetch:
            self.assertEqual(WA._read_key("ECHO_ROUTINE_KEY")[0], KEY)
        self.assertEqual(fetch.call_args.args[0], os.environ["INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID"])
        self.assertEqual(fetch.call_args.kwargs["token"], os.environ[FI.ENV_AI_FLEET_COORDINATOR_TOKEN])

    def test_hook_children_receive_no_infisical_bootstrap_binding(self):
        os.environ.update({"INFISICAL_AI_FLEET_COORDINATOR_TOKEN": "private-binding",
                           "FLEET_KNOBS_INFISICAL_TOKEN": "private-binding",
                           "ECHO_ROUTINE_KEY": "private-key"})
        child = WA._hook_env({"wake_id": "w1"})
        for name in (FI.ENV_TOKEN, "INFISICAL_AI_FLEET_COORDINATOR_TOKEN", "FLEET_KNOBS_INFISICAL_TOKEN", "ECHO_ROUTINE_KEY"):
            self.assertNotIn(name, child)
        self.assertEqual(child[FI.ENV_PROJECT], self.env[FI.ENV_PROJECT])

    def test_the_shared_reader_loads_in_isolated_script_mode(self):
        import subprocess
        import sys

        code = "import runpy; m=runpy.run_path(%r); assert m['_infisical_reader']().__name__ == 'fleet_infisical'" % WA.__file__
        done = subprocess.run([sys.executable, "-I", "-c", code], cwd=self.temp.name,
                              env={}, capture_output=True, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_placeholders_in_values_are_not_expanded_again(self):
        argv = WA._hook_argv("run {topic} {seat}", "echo", {"topic": "{seat}{state}"})
        self.assertEqual(argv, ["run", "{seat}{state}", "echo"])

    def test_a_name_that_could_be_an_option_is_refused_before_any_call(self):
        def run(*a, **k):
            raise AssertionError("must not run")

        for name in ("--help", "-x", "A B", "", "A;B"):
            self.assertEqual(WA._read_key(name, run=run)[0], "")

    def test_the_loop_reads_the_key_every_poll(self):
        keys = iter(["key-one-0001", "key-two-0002", ""])
        used = []
        sleeps = []

        def fake_read(name, run=None):
            k = next(keys)
            return (k, "" if k else "fleet-secrets exited 1")

        class Stop(Exception):
            pass

        def sleep(n):
            sleeps.append(n)
            if len(sleeps) == 3:
                raise Stop

        saved = (WA._read_key, WA.poll_once, WA.time.sleep)
        WA._read_key = fake_read
        WA.poll_once = lambda b, s, k, *a, **kw: used.append(k) or 0
        WA.time.sleep = sleep
        self.addCleanup(lambda: (setattr(WA, "_read_key", saved[0]), setattr(WA, "poll_once", saved[1]), setattr(WA.time, "sleep", saved[2])))
        env = {"OPERATOR_SEAT": "echo", "OPERATOR_LISTENER_URL": "https://listener.example.test"}
        os.environ.update(env)
        for k in env:
            self.addCleanup(os.environ.pop, k, None)
        # first read is the startup check, then one per loop poll
        keys = iter(["key-startup-0", "key-one-0001", "key-two-0002", ""])
        with self.assertRaises(Stop):
            WA._run(["--loop", "1"])
        self.assertEqual(used, ["key-one-0001", "key-two-0002"])


if __name__ == "__main__":
    unittest.main()
