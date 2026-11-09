"""Unit tests for fleet_rag.core.http_json's retry/backoff behavior: 408 handling and the
RECALL_HTTP_RETRIES / RECALL_HTTP_BACKOFF_MAX env overrides used by `recall-tunnel env` to
widen the budget for the SSH-tunnel path.

    cd scripts && python3 -m unittest fleet_rag.tests.test_core_http -v

No network: urllib.request.urlopen is mocked in every test, and time.sleep is mocked so no
test actually waits out a backoff.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from fleet_rag import core
from fleet_rag.core import FleetRagError


class FakeResponse:
    """Stands in for the object urllib.request.urlopen() returns, used as a context manager."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def http_error(code: int) -> urllib.error.HTTPError:
    # A ResourceWarning about HTTPError's own fp can print here on an interim (retried) attempt
    # whose error is discarded before http_json's final `e.read(400)` -- cosmetic only, unittest
    # does not fail on it.
    return urllib.error.HTTPError(url="https://x.test/path", code=code, msg="err",
                                  hdrs=None, fp=io.BytesIO(b""))


class Http408RetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(core.RETRIES_ENV, None)
        os.environ.pop(core.BACKOFF_MAX_ENV, None)
        self.sleep = mock.patch.object(core.time, "sleep")
        self.sleep_mock = self.sleep.start()

    def tearDown(self) -> None:
        self.sleep.stop()
        self.env.stop()

    def test_408_is_retried_like_429_and_5xx(self) -> None:
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise http_error(408)
            return FakeResponse({"ok": True})

        with mock.patch.object(core.urllib.request, "urlopen", fake_urlopen):
            result = core.http_json("https://x.test/path")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls["n"], 3)

    def test_408_exhausts_retries_and_raises_naming_the_code(self) -> None:
        def always_408(req, timeout=None):
            raise http_error(408)

        with mock.patch.object(core.urllib.request, "urlopen", always_408):
            with self.assertRaisesRegex(FleetRagError, "HTTP 408"):
                core.http_json("https://x.test/path", retries=2)

    def test_default_retry_count_is_unchanged_when_env_unset(self) -> None:
        calls = {"n": 0}

        def always_500(req, timeout=None):
            calls["n"] += 1
            raise http_error(500)

        with mock.patch.object(core.urllib.request, "urlopen", always_500):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path")
        self.assertEqual(calls["n"], core.RETRIES + 1)   # RETRIES retries + the first attempt

    def test_a_4xx_that_is_not_408_or_429_is_never_retried(self) -> None:
        calls = {"n": 0}

        def always_404(req, timeout=None):
            calls["n"] += 1
            raise http_error(404)

        with mock.patch.object(core.urllib.request, "urlopen", always_404):
            with self.assertRaisesRegex(FleetRagError, "HTTP 404"):
                core.http_json("https://x.test/path")
        self.assertEqual(calls["n"], 1)


class HttpWriteRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(core.RETRIES_ENV, None)
        os.environ.pop(core.BACKOFF_MAX_ENV, None)
        self.sleep = mock.patch.object(core.time, "sleep")
        self.sleep.start()

    def tearDown(self) -> None:
        self.sleep.stop()
        self.env.stop()

    def test_a_disconnect_retries_the_same_bounded_write_and_counts_attempts(self) -> None:
        attempts = []

        def flaky(req, timeout=None):
            attempts.append(req.data)
            if len(attempts) < 3:
                raise http.client.RemoteDisconnected("closed during write")
            return FakeResponse({"status": "ok"})

        with mock.patch.object(core.urllib.request, "urlopen", flaky):
            result = core.http_json(
                "https://qdrant.example.test/collections/fleet/points?wait=true",
                {"points": [{"id": "point-1", "payload": {"doc_id": "x"}}]},
                method="PUT",
                retries=2,
            )
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(body == attempts[0] for body in attempts))

    def test_an_exhausted_reset_reports_the_shape_after_counted_write_attempts(self) -> None:
        calls = {"n": 0}

        def reset(req, timeout=None):
            calls["n"] += 1
            raise ConnectionResetError("connection reset by peer")

        with mock.patch.object(core.urllib.request, "urlopen", reset):
            with self.assertRaisesRegex(FleetRagError, "ConnectionResetError reaching"):
                core.http_json(
                    "https://qdrant.example.test/collections/fleet/points/payload?wait=true",
                    {"points": ["point-1"], "payload": {"retracted": True}},
                    method="POST",
                    retries=2,
                )
        self.assertEqual(calls["n"], 3)


class QdrantRetrieveValidationTests(unittest.TestCase):
    def qdrant(self):
        return core.Qdrant({
            "QDRANT_URL": "https://qdrant.example.test",
            "QDRANT_API_KEY": "key",
            "QDRANT_FLEET_COLLECTION": "fleet",
        })

    def test_retrieve_validates_the_third_party_envelope(self):
        q = self.qdrant()
        point = {"id": "point-1", "payload": {"doc_id": "x"}, "vector": [0.1, 0.2]}
        with mock.patch.object(core.Qdrant, "_call", return_value={"result": point}):
            self.assertEqual(q.retrieve("point-1"), point)

    def test_retrieve_fails_closed_on_malformed_third_party_shapes(self):
        q = self.qdrant()
        cases = [
            ("malformed response", []),
            ("malformed point", {"result": None}),
            ("valid id", {"result": {"id": "", "payload": {}}}),
            ("invalid payload", {"result": {"id": "point-1", "payload": []}}),
            ("invalid vector", {"result": {"id": "point-1", "payload": {}, "vector": "bad"}}),
        ]
        for message, response in cases:
            with self.subTest(message=message), mock.patch.object(core.Qdrant, "_call", return_value=response):
                with self.assertRaisesRegex(FleetRagError, message):
                    q.retrieve("point-1")


class RetryEnvOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(core.RETRIES_ENV, None)
        os.environ.pop(core.BACKOFF_MAX_ENV, None)
        self.sleep = mock.patch.object(core.time, "sleep")
        self.sleep_mock = self.sleep.start()

    def tearDown(self) -> None:
        self.sleep.stop()
        self.env.stop()

    def test_recall_http_retries_env_widens_the_budget(self) -> None:
        os.environ[core.RETRIES_ENV] = "6"
        calls = {"n": 0}

        def always_503(req, timeout=None):
            calls["n"] += 1
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path")
        self.assertEqual(calls["n"], 7)   # 6 retries + the first attempt

    def test_recall_http_retries_env_is_read_at_call_time_not_import_time(self) -> None:
        # Two calls in the same process, env changed in between -- proves it is not baked in
        # as a function default evaluated once at import/definition time.
        calls = {"n": 0}

        def always_503(req, timeout=None):
            calls["n"] += 1
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            os.environ[core.RETRIES_ENV] = "0"
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path")
            self.assertEqual(calls["n"], 1)

            calls["n"] = 0
            os.environ[core.RETRIES_ENV] = "2"
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path")
            self.assertEqual(calls["n"], 3)

    def test_explicit_retries_argument_overrides_the_env_var(self) -> None:
        os.environ[core.RETRIES_ENV] = "6"
        calls = {"n": 0}

        def always_503(req, timeout=None):
            calls["n"] += 1
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path", retries=1)
        self.assertEqual(calls["n"], 2)   # explicit retries=1 wins over the env var

    def test_recall_http_backoff_max_env_caps_the_sleep(self) -> None:
        os.environ[core.BACKOFF_MAX_ENV] = "3"

        def always_503(req, timeout=None):
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path", retries=5)
        # min(2**attempt, 3) for attempt 0..4 -> 1, 2, 3, 3, 3
        self.sleep_mock.assert_has_calls([mock.call(1), mock.call(2), mock.call(3),
                                          mock.call(3), mock.call(3)])

    def test_default_backoff_cap_is_ten_when_env_unset(self) -> None:
        def always_503(req, timeout=None):
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path", retries=5)
        # min(2**attempt, 10) for attempt 0..4 -> 1, 2, 4, 8, 10
        self.sleep_mock.assert_has_calls([mock.call(1), mock.call(2), mock.call(4),
                                          mock.call(8), mock.call(10)])

    def test_malformed_env_value_falls_back_to_the_default(self) -> None:
        os.environ[core.RETRIES_ENV] = "not-a-number"
        calls = {"n": 0}

        def always_503(req, timeout=None):
            calls["n"] += 1
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path")
        self.assertEqual(calls["n"], core.RETRIES + 1)


class EnvIntBoundsTests(unittest.TestCase):
    """Direct tests of _env_int's bounds handling -- no HTTP involved."""

    NAME = "FLEET_RAG_TEST_ENV_INT"

    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(self.NAME, None)

    def tearDown(self) -> None:
        self.env.stop()

    def test_negative_value_falls_back_to_default(self) -> None:
        os.environ[self.NAME] = "-1"
        self.assertEqual(core._env_int(self.NAME, 4, min_value=0, max_value=20), 4)

    def test_huge_value_is_clamped_to_max(self) -> None:
        os.environ[self.NAME] = "999999"
        self.assertEqual(core._env_int(self.NAME, 4, min_value=0, max_value=20), 20)

    def test_zero_is_allowed_when_min_value_is_zero(self) -> None:
        os.environ[self.NAME] = "0"
        self.assertEqual(core._env_int(self.NAME, 10, min_value=0, max_value=300), 0)

    def test_value_within_bounds_is_used_unchanged(self) -> None:
        os.environ[self.NAME] = "6"
        self.assertEqual(core._env_int(self.NAME, 4, min_value=0, max_value=20), 6)


class NegativeAndHugeRetriesAndBackoffEnvTests(unittest.TestCase):
    """Findings: a negative RECALL_HTTP_RETRIES made range(retries + 1) empty, so http_json
    returned "request failed: NoneType" without ever sending a request; a negative
    RECALL_HTTP_BACKOFF_MAX reached time.sleep(negative), a raw ValueError that broke the
    only-FleetRagError contract.  Both env vars now fall back to their default on a negative
    value and clamp a huge one to MAX_RETRIES / MAX_BACKOFF_MAX."""

    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(core.RETRIES_ENV, None)
        os.environ.pop(core.BACKOFF_MAX_ENV, None)
        self.sleep = mock.patch.object(core.time, "sleep")
        self.sleep_mock = self.sleep.start()

    def tearDown(self) -> None:
        self.sleep.stop()
        self.env.stop()

    def test_negative_retries_falls_back_to_default_and_still_sends_a_request(self) -> None:
        os.environ[core.RETRIES_ENV] = "-1"
        calls = {"n": 0}

        def always_503(req, timeout=None):
            calls["n"] += 1
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path")
        # Falls back to the default (RETRIES + 1 attempts), not range(0) (0 attempts, never
        # sent) which is what -1 produced before the fix.
        self.assertEqual(calls["n"], core.RETRIES + 1)

    def test_huge_retries_is_clamped_to_max_retries(self) -> None:
        os.environ[core.RETRIES_ENV] = "999999"
        calls = {"n": 0}

        def always_503(req, timeout=None):
            calls["n"] += 1
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path")
        self.assertEqual(calls["n"], core.MAX_RETRIES + 1)

    def test_negative_backoff_max_falls_back_to_default_instead_of_raising_valueerror(self) -> None:
        os.environ[core.BACKOFF_MAX_ENV] = "-5"

        def always_503(req, timeout=None):
            raise http_error(503)

        # Before the fix, time.sleep(min(2**attempt, -5)) == time.sleep(-5) raised a raw
        # ValueError here instead of the only-FleetRagError contract.
        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path", retries=2)
        self.sleep_mock.assert_has_calls([mock.call(1), mock.call(2)])

    def test_zero_backoff_max_is_allowed(self) -> None:
        os.environ[core.BACKOFF_MAX_ENV] = "0"

        def always_503(req, timeout=None):
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path", retries=2)
        self.sleep_mock.assert_has_calls([mock.call(0), mock.call(0)])

    def test_huge_backoff_max_is_clamped_to_max_backoff_max(self) -> None:
        os.environ[core.BACKOFF_MAX_ENV] = "999999"

        def always_503(req, timeout=None):
            raise http_error(503)

        with mock.patch.object(core.urllib.request, "urlopen", always_503):
            with self.assertRaises(FleetRagError):
                core.http_json("https://x.test/path", retries=10)
        # attempt 9 -> 2**9 == 512, which would exceed MAX_BACKOFF_MAX if it were honored
        # unclamped; every sleep call must be capped at MAX_BACKOFF_MAX.
        calls = self.sleep_mock.call_args_list
        self.assertEqual(calls[-1], mock.call(core.MAX_BACKOFF_MAX))
        self.assertTrue(all(c.args[0] <= core.MAX_BACKOFF_MAX for c in calls))


if __name__ == "__main__":
    unittest.main()
