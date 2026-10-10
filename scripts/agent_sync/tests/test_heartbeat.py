"""The Sentry Crons heartbeat against a fake Sentry ingest server on loopback:  the envelope (monitor slug,
interval, margin, status), degraded becoming `error` with one reasoned event, the DSN never reaching a log,
the status file or an error, backoff after a failure, and the heartbeat staying off without a DSN."""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent_sync import heartbeat as HB
from agent_sync.tests.listener_harness import FakeClock, ListenerHarness

KEY = "0123456789abcdef0123456789abcdef"
PROJECT = "4511678688854016"


class FakeSentry:
    """Records every envelope POST and answers with a settable status."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":"x"}')

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def dsn(self) -> str:
        return "http://%s@127.0.0.1:%d/%s" % (KEY, self.server.server_address[1], PROJECT)

    def items(self, index: int = -1) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        lines = self.requests[index]["body"].decode().splitlines()
        out = []
        for i in range(1, len(lines), 2):
            out.append((json.loads(lines[i]), json.loads(lines[i + 1])))
        return out

    def checkin(self, index: int = -1) -> dict[str, Any]:
        return [payload for header, payload in self.items(index) if header["type"] == "check_in"][0]

    def events(self, index: int = -1) -> list[dict[str, Any]]:
        return [payload for header, payload in self.items(index) if header["type"] == "event"]


class PureTests(unittest.TestCase):
    def test_parse_dsn(self) -> None:
        dsn = HB.parse_dsn("https://%s@o123.ingest.us.sentry.io/%s" % (KEY, PROJECT))
        assert dsn is not None
        self.assertEqual(dsn.envelope_url, "https://o123.ingest.us.sentry.io/api/%s/envelope/" % PROJECT)
        self.assertNotIn(KEY, repr(dsn))
        self.assertNotIn(KEY, str(dsn))
        for bad in ("", "nope", "https://o123.ingest.us.sentry.io/%s" % PROJECT, "https://%s@host/" % KEY,
                    "https://%s@host/abc" % KEY, "http://%s@sentry.example.com/1" % KEY, "ftp://%s@host/1" % KEY):
            self.assertIsNone(HB.parse_dsn(bad), bad)
        self.assertIsNotNone(HB.parse_dsn("http://%s@127.0.0.1:9000/1" % KEY))

    def test_resolve_dsn_environment_beats_file_and_only_named_keys_are_read(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".secrets").mkdir()
            path = home / ".secrets" / "agent-sync.env"
            path.write_text('SLACK_BOT_TOKEN=xoxb-never-read\nexport SENTRY_FLEET_DSN="https://k@h/1"\n# c\n')
            self.assertEqual(HB.resolve_dsn({}, str(home)), "https://k@h/1")
            self.assertEqual(HB.resolve_dsn({"AGENT_SYNC_SENTRY_DSN": "https://e@h/2"}, str(home)), "https://e@h/2")
            self.assertEqual(HB.resolve_dsn({"SENTRY_FLEET_DSN": "https://f@h/3"}, str(home)), "https://f@h/3")
            self.assertEqual(HB.read_env_file(str(path)), {"SENTRY_FLEET_DSN": "https://k@h/1"})
            self.assertEqual(HB.resolve_dsn({}, str(home / "nowhere")), "")

    def test_monitor_slug_per_instance(self) -> None:
        self.assertEqual(HB.monitor_slug("mac"), "agent-sync-listener-mac")
        self.assertEqual(HB.monitor_slug("server"), "agent-sync-listener-server")
        self.assertEqual(HB.monitor_slug("Odd Name!"), "agent-sync-listener-odd-name")

    def test_degraded_reasons(self) -> None:
        healthy = {"owner_user_id": 12, "config_errors": [], "seats": {"CLAUDE": {"red": []}}}
        self.assertEqual(HB.degraded_reasons(healthy), [])
        sick = {"owner_user_id": 0, "config_errors": ["bad\nline"],
                "seats": {"CLAUDE": {"red": ["down for 31 minutes"]}, "OK": {"red": []}}}
        reasons = HB.degraded_reasons(sick, scrub=lambda s: s.replace("31", "NN"))
        self.assertEqual(reasons, ["seat CLAUDE red: down for NN minutes", "owner not pinned",
                                   "1 config error(s): bad line"])

    def test_envelope_shape(self) -> None:
        body = HB.build_envelope(slug="agent-sync-listener-server", instance="server", environment="production",
                                 ok=False, reasons=["owner not pinned"], send_event=True, now=1_790_000_000)
        lines = body.decode().splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(json.loads(lines[1])["length"], len(lines[2].encode()))
        check_in = json.loads(lines[2])
        self.assertEqual(check_in["monitor_slug"], "agent-sync-listener-server")
        self.assertEqual(check_in["status"], "error")
        config = check_in["monitor_config"]
        self.assertEqual(config["schedule"], {"type": "interval", "value": 5, "unit": "minute"})
        self.assertEqual((config["checkin_margin"], config["failure_issue_threshold"]), (10, 2))
        event = json.loads(lines[4])
        self.assertEqual(event["fingerprint"], ["agent-sync-listener", "server", "degraded"])
        self.assertIn("owner not pinned", event["message"])


class BeatTests(unittest.TestCase):
    """SentryHeartbeat alone, synchronous, on a fake clock."""

    def setUp(self) -> None:
        self.sentry = FakeSentry()
        self.sentry.start()
        self.addCleanup(self.sentry.stop)
        self.clock = FakeClock(1_790_000_000)
        self.logs: list[tuple[str, dict[str, Any]]] = []
        self.status: dict[str, Any] = {"instance": "mac", "owner_user_id": 12, "config_errors": [],
                                       "seats": {"CLAUDE": {"red": []}}}

    def heartbeat(self, **kw: Any) -> HB.SentryHeartbeat:
        env = {"AGENT_SYNC_SENTRY_DSN": self.sentry.dsn}
        env.update(kw.pop("env", {}))
        return HB.SentryHeartbeat(env=env, home="/nonexistent", clock=self.clock.time,
                                  log=lambda event, **f: self.logs.append((event, f)), background=False, **kw)

    def beat(self, hb: HB.SentryHeartbeat) -> bool:
        return hb.tick(lambda: self.status)

    def test_checkin_payload_and_interval(self) -> None:
        hb = self.heartbeat()
        self.assertTrue(self.beat(hb))
        request = self.sentry.requests[0]
        self.assertEqual(request["path"], "/api/%s/envelope/" % PROJECT)
        self.assertIn("sentry_key=%s" % KEY, request["headers"]["X-Sentry-Auth"])
        self.assertEqual(request["headers"]["Content-Type"], "application/x-sentry-envelope")
        self.assertNotIn(KEY.encode(), request["body"])
        check_in = self.sentry.checkin()
        self.assertEqual((check_in["monitor_slug"], check_in["status"], check_in["environment"]),
                         ("agent-sync-listener-mac", "ok", "production"))
        self.assertEqual(check_in["monitor_config"]["schedule"], {"type": "interval", "value": 5, "unit": "minute"})
        self.assertEqual(self.sentry.events(), [])
        # Not due again until five minutes pass, then one beat per interval.
        self.clock.advance(299)
        self.assertFalse(self.beat(hb))
        self.clock.advance(2)
        self.assertTrue(self.beat(hb))
        self.assertEqual(len(self.sentry.requests), 2)
        self.assertEqual([e for e, _ in self.logs if e == "sentry-heartbeat-ok"], ["sentry-heartbeat-ok"])

    def test_server_instance_slug_and_environment_override(self) -> None:
        self.status["instance"] = "server"
        hb = self.heartbeat(env={"AGENT_SYNC_SENTRY_ENVIRONMENT": "staging"})
        self.beat(hb)
        check_in = self.sentry.checkin()
        self.assertEqual((check_in["monitor_slug"], check_in["environment"]), ("agent-sync-listener-server", "staging"))

    def test_degraded_is_error_and_one_event_per_distinct_problem(self) -> None:
        self.status["owner_user_id"] = 0
        hb = self.heartbeat()
        self.beat(hb)
        # The first degraded beat is an error check-in only:  one blip is not worth a second issue.
        self.assertEqual(self.sentry.checkin()["status"], "error")
        self.assertEqual(self.sentry.events(), [])
        # Still degraded the next beat:  error again, and now one event that names the reason.
        self.clock.advance(300)
        self.beat(hb)
        self.assertEqual(self.sentry.checkin()["status"], "error")
        events = self.sentry.events()
        self.assertEqual(len(events), 1)
        self.assertIn("owner not pinned", events[0]["message"])
        self.assertEqual(events[0]["level"], "warning")
        # The same problem again:  no more events, however long it lasts.
        for _ in range(3):
            self.clock.advance(300)
            self.beat(hb)
            self.assertEqual(self.sentry.events(), [])
        # A new reason sends a new event once it has lasted two beats.
        self.status["seats"]["CLAUDE"]["red"] = ["boom"]
        self.clock.advance(300)
        self.beat(hb)
        self.assertEqual(self.sentry.events(), [])
        self.clock.advance(300)
        self.beat(hb)
        self.assertEqual(len(self.sentry.events()), 1)
        # Healthy again:  ok, no event;  degrading again later sends an event again after two beats.
        self.status["owner_user_id"] = 12
        self.status["seats"]["CLAUDE"]["red"] = []
        self.clock.advance(300)
        self.beat(hb)
        self.assertEqual((self.sentry.checkin()["status"], self.sentry.events()), ("ok", []))
        self.status["owner_user_id"] = 0
        for expected in (0, 1):
            self.clock.advance(300)
            self.beat(hb)
            self.assertEqual(len(self.sentry.events()), expected)

    def test_a_growing_downtime_is_one_problem_not_one_event_per_beat(self) -> None:
        hb = self.heartbeat()
        for minutes in (31, 36, 41, 46, 51):
            self.status["seats"]["CLAUDE"]["red"] = ["down for %d minutes" % minutes]
            self.beat(hb)
            self.clock.advance(300)
        sent = [i for i in range(len(self.sentry.requests)) if self.sentry.events(i)]
        self.assertEqual(sent, [1], "one event, on the second degraded beat")
        self.assertIn("down for 36 minutes", self.sentry.events(1)[0]["message"])

    def test_a_failed_send_does_not_count_as_a_beat_seen(self) -> None:
        self.status["owner_user_id"] = 0
        hb = self.heartbeat()
        self.beat(hb)                    # delivered:  seen
        self.sentry.status = 500
        self.clock.advance(300)
        self.beat(hb)                    # carries the event but fails
        self.assertEqual(len(self.sentry.events()), 1)
        self.sentry.status = 200
        self.clock.advance(30)
        self.beat(hb)                    # retried with the event again
        self.assertEqual(len(self.sentry.events()), 1)
        self.clock.advance(300)
        self.beat(hb)                    # delivered once:  no more
        self.assertEqual(self.sentry.events(), [])

    def test_backoff_then_recovery_and_rate_limited_failure_log(self) -> None:
        self.sentry.status = 500
        hb = self.heartbeat()
        delays = []
        for _ in range(7):
            started = self.clock.time()
            self.assertTrue(self.beat(hb))
            delays.append(int(hb.next_at - started))
            self.clock.advance(hb.next_at - started)
        self.assertEqual(delays, [30, 60, 120, 240, 300, 300, 300])
        failed = [f for e, f in self.logs if e == "sentry-heartbeat-failed"]
        self.assertEqual(len(failed), 1, "failure logs are rate limited")
        self.assertEqual(failed[0]["error"], "HTTP 500")
        self.assertNotIn(KEY, json.dumps(self.logs))
        # Sentry comes back:  the next beat succeeds, the backoff resets and recovery is logged.
        self.sentry.status = 200
        self.assertTrue(self.beat(hb))
        self.assertEqual(hb.failures, 0)
        self.assertEqual(int(hb.next_at - self.clock.time()), 300)
        ok = [f for e, f in self.logs if e == "sentry-heartbeat-ok"]
        self.assertEqual(ok, [{"monitor": "agent-sync-listener-mac", "state": "ok", "recovered_after_failures": True}])
        # An hour of failures logs again after the rate-limit window.
        self.sentry.status = 503
        for _ in range(12):
            self.beat(hb)
            self.clock.advance(max(0.0, hb.next_at - self.clock.time()))
        self.assertGreaterEqual(len([e for e, _ in self.logs if e == "sentry-heartbeat-failed"]), 2)

    def test_connection_refused_is_a_logged_failure_without_the_dsn(self) -> None:
        dsn = "http://%s@127.0.0.1:1/%s" % (KEY, PROJECT)  # nothing listens on port 1
        hb = self.heartbeat(env={"AGENT_SYNC_SENTRY_DSN": dsn}, scrub=lambda s: s)
        self.assertTrue(self.beat(hb))
        failed = [f for e, f in self.logs if e == "sentry-heartbeat-failed"]
        self.assertEqual(len(failed), 1)
        self.assertNotIn(KEY, json.dumps(failed))
        self.assertNotIn("127.0.0.1", json.dumps(failed))

    def test_no_dsn_is_off_and_says_so_once(self) -> None:
        hb = HB.SentryHeartbeat(env={}, home="/nonexistent", clock=self.clock.time, background=False,
                                log=lambda event, **f: self.logs.append((event, f)))
        for _ in range(3):
            self.assertFalse(self.beat(hb))
            self.clock.advance(300)
        self.assertEqual(self.sentry.requests, [])
        self.assertEqual([e for e, _ in self.logs], ["sentry-heartbeat-off"])

    def test_off_switch_and_bad_dsn(self) -> None:
        hb = self.heartbeat(env={"AGENT_SYNC_SENTRY_HEARTBEAT": "off"})
        self.assertFalse(self.beat(hb))
        hb = self.heartbeat(env={"AGENT_SYNC_SENTRY_DSN": "https://not-a-dsn"})
        self.assertFalse(self.beat(hb))
        self.assertEqual(self.sentry.requests, [])
        self.assertNotIn("not-a-dsn", json.dumps(self.logs))

    def test_a_slow_send_does_not_block_or_double_up(self) -> None:
        release = threading.Event()
        calls: list[int] = []

        def slow(url: str, body: bytes, headers: Any, timeout: float) -> int:
            calls.append(1)
            release.wait(5)
            return 200

        hb = HB.SentryHeartbeat(env={"AGENT_SYNC_SENTRY_DSN": self.sentry.dsn}, home="/nonexistent",
                                clock=self.clock.time, log=lambda *a, **k: None, send=slow)
        self.assertTrue(self.beat(hb))          # returns at once, the send is on its own thread
        self.clock.advance(400)
        self.assertFalse(self.beat(hb))         # the first is still in flight
        release.set()
        hb.wait()
        self.assertEqual(len(calls), 1)


class DaemonTests(ListenerHarness):
    """The daemon's tick drives the heartbeat;  nothing the daemon writes carries the DSN."""

    def setUp(self) -> None:
        super().setUp()
        self.sentry = FakeSentry()
        self.sentry.start()
        self.addCleanup(self.sentry.stop)
        self.write_config()
        self.pin()

    def everything_written(self, daemon: Any) -> str:
        daemon.write_status()
        daemon.log.write("probe")
        text = [daemon.stderr.getvalue()]
        for path in self.state_dir.rglob("*"):
            if path.is_file():
                text.append(path.read_text(errors="replace"))
        return "\n".join(text)

    def test_tick_reports_ok_and_never_writes_the_dsn(self) -> None:
        daemon = self.daemon(env=self.env(AGENT_SYNC_SENTRY_DSN=self.sentry.dsn))
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        daemon.tick()
        daemon.heartbeat.wait()
        self.assertEqual(len(self.sentry.requests), 1)
        self.assertEqual((self.sentry.checkin()["monitor_slug"], self.sentry.checkin()["status"]),
                         ("agent-sync-listener-mac", "ok"))
        written = self.everything_written(daemon)
        self.assertNotIn(KEY, written)
        self.assertNotIn(self.sentry.dsn, written)
        self.assertIn("sentry-heartbeat-ok", written)
        # A scrubbed log line quoting the DSN or its key is redacted like a bot key.
        self.assertNotIn(KEY, daemon.scrub("failed for %s" % self.sentry.dsn))
        self.assertNotIn(KEY, daemon.scrub("key %s" % KEY))

    def test_unpinned_owner_is_error(self) -> None:
        self.write_config(owner=0)
        daemon = self.daemon(env=self.env(AGENT_SYNC_SENTRY_DSN=self.sentry.dsn))
        daemon.tick()
        daemon.heartbeat.wait()
        self.assertEqual(self.sentry.checkin()["status"], "error")
        self.clock.advance(300)
        daemon.tick()
        daemon.heartbeat.wait()
        self.assertIn("owner not pinned", self.sentry.events()[0]["message"])

    def test_dsn_from_the_secrets_file_and_no_dsn_means_silence(self) -> None:
        daemon = self.daemon()
        daemon.tick()
        daemon.heartbeat.wait()
        self.assertEqual(self.sentry.requests, [])
        self.assertIn("sentry-heartbeat-off", self.everything_written(daemon))
        secrets = self.home / ".secrets"
        secrets.mkdir()
        (secrets / "agent-sync.env").write_text("SENTRY_FLEET_DSN=%s\n" % self.sentry.dsn)
        (secrets / "agent-sync.env").chmod(0o600)
        daemon = self.daemon()
        daemon.tick()
        daemon.heartbeat.wait()
        self.assertEqual(len(self.sentry.requests), 1)


if __name__ == "__main__":
    unittest.main()
