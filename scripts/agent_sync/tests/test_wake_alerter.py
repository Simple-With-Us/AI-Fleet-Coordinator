"""Tests for the Mac wake alerter (hosted-listener thin client)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error

from agent_sync import wake_alerter as WA

KEY = "r" * 40
SEAT = "ECHO"
BASE = "http://listener.example.ts.net:8788"


def payload(items):
    return {"contract": "agent-sync-read/1", "seat": SEAT, "record": "wakes",
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
        stamp = req.headers["X-agent-sync-timestamp"]
        from agent_sync.listener_api import sign

        self.assertEqual(req.headers["X-agent-sync-signature"], sign(KEY, path, WA.QUERY, stamp))
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
    def test_key_comes_from_fleet_secrets_get_by_name_only(self):
        seen = []

        class Done:
            returncode = 0
            stdout = b" the-key-0123\n"

        def run(argv, **kw):
            seen.append(argv)
            return Done()

        os.environ["FLEET_SECRETS_BIN"] = "/opt/fs"
        os.environ["OPERATOR_SECRET_PATH"] = "/zulip"
        self.addCleanup(os.environ.pop, "FLEET_SECRETS_BIN", None)
        self.addCleanup(os.environ.pop, "OPERATOR_SECRET_PATH", None)
        self.assertEqual(WA._fleet_secrets_get("ECHO_ROUTINE_KEY", run=run), "the-key-0123")
        self.assertEqual(seen, [["/opt/fs", "get", "ECHO_ROUTINE_KEY", "--path", "/zulip"]])

    def test_a_failing_or_missing_lookup_is_empty(self):
        class Bad:
            returncode = 3
            stdout = b"x"

        os.environ["FLEET_SECRETS_BIN"] = "/opt/fs"
        self.addCleanup(os.environ.pop, "FLEET_SECRETS_BIN", None)
        self.assertEqual(WA._fleet_secrets_get("N", run=lambda *a, **k: Bad()), "")

        def boom(*a, **k):
            raise OSError("x")

        self.assertEqual(WA._fleet_secrets_get("N", run=boom), "")

    def _run_with(self, returncode, stdout, exc=None):
        class Done:
            pass

        done = Done()
        done.returncode, done.stdout = returncode, stdout

        def run(argv, **kw):
            if exc:
                raise exc
            return done

        os.environ["FLEET_SECRETS_BIN"] = "/opt/fs"
        self.addCleanup(os.environ.pop, "FLEET_SECRETS_BIN", None)
        return run

    def test_failures_keep_their_reason_and_never_the_value(self):
        import subprocess

        for rc, out, exc, want in (
            (3, b"secret-value-xyz", None, "exited 3"),
            (0, b"", None, "empty"),
            (0, b"has space in it", None, "does not look like"),
            (0, b"short", None, "does not look like"),
            (0, b"x", subprocess.TimeoutExpired("fs", 30), "timed out"),
            (0, b"x", OSError("no"), "could not run"),
        ):
            key, why = WA._read_key("ECHO_ROUTINE_KEY", run=self._run_with(rc, out, exc))
            self.assertEqual(key, "")
            self.assertIn(want, why)
            self.assertNotIn("secret-value-xyz", why)

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
