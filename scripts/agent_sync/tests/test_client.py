"""HTTP client behaviour: retries, rate limits, timeouts, redirects, headers, secrecy of errors."""
from __future__ import annotations

import base64
import json
import socket
import time
import unittest

from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.harness import Harness, write_rc

RATE_LIMITED = {"result": "error", "msg": "API usage exceeded rate limit", "code": "RATE_LIMIT_HIT", "retry-after": 0.01}


class RateLimitTests(Harness):
    def test_429_honours_the_retry_after_header_then_succeeds(self) -> None:
        self.fake.inject("POST", "messages", status=429, body=RATE_LIMITED, headers={"Retry-After": "0.05"})
        result = self.run_cli("post", "--topic", "t", "hello")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.sleeps, [0.05])
        self.assertEqual(len(self.fake.requests_to("POST", "messages")), 2)
        self.assertEqual(len([m for m in self.fake.messages]), 1)

    def test_429_really_waits_when_sleep_is_real(self) -> None:
        import time as real_time

        self.fake.inject("POST", "messages", status=429, body=RATE_LIMITED, headers={"Retry-After": "0.2"})
        started = time.monotonic()
        result = self.run_cli("post", "--topic", "t", "hello", sleep=real_time.sleep)
        self.assertEqual(result.code, 0, result.err)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_429_falls_back_to_the_json_field_and_caps_at_30_seconds(self) -> None:
        self.fake.inject("GET", "users/me", status=429, body={**RATE_LIMITED, "retry-after": 0.25})
        self.fake.inject("GET", "users/me", status=429, body=RATE_LIMITED, headers={"Retry-After": "9999"})
        self.assertEqual(self.run_cli("whoami").code, 0)
        self.assertEqual(self.sleeps, [0.25, 30.0])

    def test_gives_up_after_three_retries(self) -> None:
        self.fake.inject("POST", "messages", status=429, body=RATE_LIMITED, headers={"Retry-After": "0"}, times=10)
        result = self.run_cli("post", "--topic", "t", "hello")
        self.assertEqual(result.code, 5)
        self.assertIn("rate limit", result.err)
        self.assertEqual(len(self.fake.requests_to("POST", "messages")), 4)
        self.assertEqual(self.sleeps, [0.0, 0.0, 0.0])


class NetworkFailureTests(Harness):
    def test_post_is_never_retried_on_a_timeout_and_the_uncertainty_is_reported(self) -> None:
        self.fake.inject("POST", "messages", delay=1.0)
        result = self.run_cli("post", "--topic", "t", "hello", timeout=0.2)
        self.assertEqual(result.code, 6)
        self.assertIn("may or may not", result.err)
        self.assertIn("--include-self", result.err)
        self.assertEqual(len(self.fake.requests_to("POST", "messages")), 1)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.state_path().exists(), "no id is known, so nothing goes in the ledger")

    def test_post_is_never_retried_after_a_dropped_connection(self) -> None:
        self.fake.inject("POST", "messages", drop=True)
        result = self.run_cli("post", "--topic", "t", "hello")
        self.assertEqual(result.code, 6)
        self.assertEqual(len(self.fake.requests_to("POST", "messages")), 1)

    def test_a_post_that_gets_a_gateway_timeout_says_it_may_have_posted_and_is_not_retried(self) -> None:
        for status in (502, 503, 504):
            with self.subTest(status=status):
                before = len(self.fake.requests_to("POST", "messages"))
                self.fake.inject("POST", "messages", status=status, body={"result": "error", "msg": "Gateway Timeout"})
                result = self.run_cli("post", "--topic", "t", "hello")
                self.assertEqual(result.code, 5)
                self.assertIn("Gateway Timeout", result.err)
                self.assertIn("may or may not", result.err)
                self.assertIn("--include-self", result.err)
                self.assertEqual(len(self.fake.requests_to("POST", "messages")) - before, 1)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.state_path().exists())

    def test_a_plain_server_error_on_a_post_is_just_an_error(self) -> None:
        self.fake.inject("POST", "messages", status=400, body={"result": "error", "msg": "Topic is too long", "code": "BAD_REQUEST"})
        result = self.run_cli("post", "--topic", "t", "hello")
        self.assertEqual(result.code, 5)
        self.assertNotIn("may or may not", result.err)

    def test_get_retries_a_gateway_error_twice_with_the_same_backoff(self) -> None:
        self.fake.inject("GET", "users/me", status=502, body={}, times=2)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.sleeps, [1.0, 3.0])
        self.assertEqual(len(self.fake.requests_to("GET", "users/me")), 3)

    def test_get_gives_up_on_a_gateway_error_after_the_second_retry(self) -> None:
        self.fake.inject("GET", "users/me", status=503, body={}, times=5)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 5)
        self.assertEqual(len(self.fake.requests_to("GET", "users/me")), 3)

    def test_get_does_not_retry_an_ordinary_server_error(self) -> None:
        self.fake.inject("GET", "users/me", status=500, body={}, times=5)
        self.assertEqual(self.run_cli("whoami").code, 5)
        self.assertEqual(len(self.fake.requests_to("GET", "users/me")), 1)

    def test_get_retries_twice_with_1s_then_3s_backoff(self) -> None:
        self.fake.inject("GET", "users/me", drop=True, times=2)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.sleeps, [1.0, 3.0])
        self.assertEqual(len(self.fake.requests_to("GET", "users/me")), 3)

    def test_get_gives_up_after_the_second_retry(self) -> None:
        self.fake.inject("GET", "users/me", drop=True, times=5)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 6)
        self.assertEqual(len(self.fake.requests_to("GET", "users/me")), 3)

    def test_get_timeouts_are_retried_too(self) -> None:
        self.fake.inject("GET", "users/me", delay=0.6, times=1)
        result = self.run_cli("whoami", timeout=0.2)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.sleeps, [1.0])

    def test_nothing_listening_is_exit_6(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = "http://127.0.0.1:%d" % probe.getsockname()[1]
        write_rc(self.rc_path, email=BOT_EMAIL, key=self.key, site=dead)
        result = self.run_cli("whoami", env=self.env(AGENT_SYNC_REALM=dead))
        self.assertEqual(result.code, 6)
        self.assertEqual(self.sleeps, [1.0, 3.0])

    def test_redirects_are_not_followed_so_the_auth_header_cannot_travel(self) -> None:
        self.fake.inject("GET", "users/me", status=302, body={}, headers={"Location": "http://127.0.0.1:1/steal"})
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 5)
        self.assertIn("302", result.err)


class ProtocolTests(Harness):
    def test_requests_carry_basic_auth_and_the_user_agent(self) -> None:
        self.run_cli("whoami")
        headers = self.fake.requests_to("GET", "users/me")[0].headers
        expected = "Basic " + base64.b64encode(("%s:%s" % (BOT_EMAIL, self.key)).encode()).decode()
        self.assertEqual(headers["Authorization"], expected)
        self.assertEqual(headers["User-Agent"], "agent-sync/1 (fleet)")

    def test_api_errors_surface_the_message_and_exit_5(self) -> None:
        result = self.run_cli("react", "--id", "999999", "eyes")
        self.assertEqual(result.code, 5)
        self.assertIn("Invalid message(s)", result.err)

    def test_a_rejected_key_is_an_api_error_not_a_credential_error(self) -> None:
        write_rc(self.rc_path, email=BOT_EMAIL, key=self.key + "0", site=self.fake.url)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 5)
        self.assertIn("Invalid API key", result.err)

    def test_server_echoing_the_auth_header_is_scrubbed(self) -> None:
        self.fake.echo_auth = True
        result = self.run_cli("react", "--id", "999999", "eyes")
        self.assertEqual(result.code, 5)
        self.assertIn("[redacted]", result.err)
        self.assertNotIn(self.key, result.err)

    def test_a_credential_repeated_in_the_message_or_the_code_is_scrubbed_from_the_error_text(self) -> None:
        leaky = {"result": "error", "msg": "bad thing " + self.key, "code": "CODE_" + self.key}
        self.fake.inject("GET", "users/me", status=400, body=leaky)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 5)
        self.assertNotIn(self.key, result.err)
        self.assertIn("[redacted]", result.err)
        creds = Z.Credentials(email=BOT_EMAIL, key=self.key, site=self.fake.url, source="env")
        client = Z.ZulipClient(creds, self.fake.url, timeout=2)
        self.fake.inject("GET", "users/me", status=400, body=leaky)
        with self.assertRaises(Z.ApiError) as caught:
            client.get("users/me")
        self.assertNotIn(self.key, str(caught.exception))
        self.assertNotIn(self.key, caught.exception.code or "")

    def test_api_error_keeps_the_code(self) -> None:
        creds = Z.Credentials(email=BOT_EMAIL, key=self.key, site=self.fake.url, source="env")
        client = Z.ZulipClient(creds, self.fake.url, timeout=2)
        with self.assertRaises(Z.ApiError) as caught:
            client.get("events", {"queue_id": "nope", "last_event_id": -1})
        self.assertEqual(caught.exception.code, "BAD_EVENT_QUEUE_ID")
        self.assertEqual(caught.exception.status, 400)

    def test_encode_params(self) -> None:
        encoded = Z.encode_params({"narrow": [["channel", "a b"]], "flag": True, "off": False, "n": 3, "skip": None,
                                   "text": "x&y=z", "anchor": "newest", "subs": [{"name": "c"}]})
        from urllib.parse import parse_qs

        parsed = {k: v[0] for k, v in parse_qs(encoded).items()}
        self.assertEqual(json.loads(parsed["narrow"]), [["channel", "a b"]])
        self.assertEqual((parsed["flag"], parsed["off"], parsed["n"]), ("true", "false", "3"))
        self.assertNotIn("skip", parsed)
        self.assertEqual((parsed["text"], parsed["anchor"]), ("x&y=z", "newest"))
        self.assertEqual(json.loads(parsed["subs"]), [{"name": "c"}])

    def test_non_ascii_survives_the_round_trip(self) -> None:
        self.run_cli("post", "--topic", "café ✔", "naïve — text")
        self.assertEqual(self.fake.messages[-1]["subject"], "café ✔")
        self.assertTrue(self.fake.messages[-1]["content"].endswith("naïve — text"))


if __name__ == "__main__":
    unittest.main()
