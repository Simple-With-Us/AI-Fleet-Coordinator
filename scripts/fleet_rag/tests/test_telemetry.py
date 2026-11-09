"""Unit tests for fleet_rag.telemetry: the optional Sentry instrumentation.

    cd scripts && python3 -m unittest fleet_rag.tests.test_telemetry -v
"""
from __future__ import annotations

import contextlib
import os
import sys
import unittest
from unittest import mock

from fleet_rag import telemetry


class _FakeScope:
    def __init__(self, sink: dict) -> None:
        self.sink = sink

    def set_tag(self, key: str, value: str) -> None:
        self.sink[key] = value


class _FakeSentrySdk:
    """Records every init/capture call; push_scope is a real context manager."""

    def __init__(self, fail_init: bool = False, fail_capture: bool = False) -> None:
        self.fail_init = fail_init
        self.fail_capture = fail_capture
        self.init_calls: list[dict] = []
        self.messages: list[tuple[str, str, dict]] = []
        self.exceptions: list[tuple[BaseException, dict]] = []
        self._pending_tags: dict = {}

    def init(self, **kwargs) -> None:
        if self.fail_init:
            raise RuntimeError("bad dsn")
        self.init_calls.append(kwargs)

    @contextlib.contextmanager
    def push_scope(self):
        tags: dict = {}
        self._pending_tags = tags
        yield _FakeScope(tags)

    def capture_message(self, message: str, level: str = "info") -> None:
        if self.fail_capture:
            raise RuntimeError("transport down")
        self.messages.append((message, level, dict(self._pending_tags)))

    def capture_exception(self, exc: BaseException) -> None:
        if self.fail_capture:
            raise RuntimeError("transport down")
        self.exceptions.append((exc, dict(self._pending_tags)))


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        telemetry.reset()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SENTRY_DSN", None)
        os.environ.pop("SENTRY_ENVIRONMENT", None)
        self._had_sentry_sdk = "sentry_sdk" in sys.modules
        self._saved_sentry_sdk = sys.modules.get("sentry_sdk")

    def tearDown(self) -> None:
        telemetry.reset()
        self._env.stop()
        if self._had_sentry_sdk:
            sys.modules["sentry_sdk"] = self._saved_sentry_sdk
        else:
            sys.modules.pop("sentry_sdk", None)

    def _install_fake_sdk(self, **kwargs) -> _FakeSentrySdk:
        fake = _FakeSentrySdk(**kwargs)
        sys.modules["sentry_sdk"] = fake  # duck-typed: any object with init/push_scope/capture_*
        return fake

    # -------------------------------------------------------------- no DSN configured

    def test_no_dsn_is_a_silent_noop(self) -> None:
        # No sentry_sdk installed at all -- must not raise, must not import anything.
        telemetry.capture_message("should not go anywhere")
        telemetry.capture_exception(ValueError("boom"))

    def test_dsn_set_but_sdk_missing_is_a_silent_noop(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        sys.modules.pop("sentry_sdk", None)
        # No fake installed and the real package is not a dependency of this repo, so the
        # lazy import fails; capture_* must still not raise.
        telemetry.capture_message("no crash please")

    # -------------------------------------------------------------- configured

    def test_dsn_set_initializes_once_and_tags_component(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        fake = self._install_fake_sdk()
        telemetry.capture_message("rerank fallback: FleetRagError", level="warning",
                                  operation="rerank-fallback")
        telemetry.capture_message("second call")
        self.assertEqual(len(fake.init_calls), 1)                     # init is lazy and cached
        self.assertEqual(fake.init_calls[0]["dsn"], "https://key@example.invalid/1")
        self.assertEqual(fake.init_calls[0]["environment"], "production")
        self.assertEqual(fake.init_calls[0]["traces_sample_rate"], 0.0)
        self.assertEqual(len(fake.messages), 2)
        message, level, tags = fake.messages[0]
        self.assertEqual(message, "rerank fallback: FleetRagError")
        self.assertEqual(level, "warning")
        self.assertEqual(tags, {"component": "fleet-rag", "operation": "rerank-fallback"})

    def test_environment_override(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        os.environ["SENTRY_ENVIRONMENT"] = "staging"
        fake = self._install_fake_sdk()
        telemetry.capture_message("x")
        self.assertEqual(fake.init_calls[0]["environment"], "staging")

    def test_capture_exception_carries_tags_and_the_real_exception(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        fake = self._install_fake_sdk()
        exc = RuntimeError("db locked")
        telemetry.capture_exception(exc, operation="ingest", source="doc")
        self.assertEqual(len(fake.exceptions), 1)
        captured_exc, tags = fake.exceptions[0]
        self.assertIs(captured_exc, exc)
        self.assertEqual(tags, {"component": "fleet-rag", "operation": "ingest", "source": "doc"})

    def test_tags_are_stringified(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        fake = self._install_fake_sdk()
        telemetry.capture_message("x", count=3)
        self.assertEqual(fake.messages[0][2]["count"], "3")

    # -------------------------------------------------------------- failure modes never propagate

    def test_init_failure_disables_telemetry_without_raising(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        self._install_fake_sdk(fail_init=True)
        telemetry.capture_message("no crash even though init blew up")

    def test_capture_failure_never_propagates(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        self._install_fake_sdk(fail_capture=True)
        telemetry.capture_message("no crash please")
        telemetry.capture_exception(ValueError("x"))

    # -------------------------------------------------------------- reset()

    def test_reset_forces_a_fresh_dsn_check(self) -> None:
        os.environ["SENTRY_DSN"] = "https://key@example.invalid/1"
        fake1 = self._install_fake_sdk()
        telemetry.capture_message("first")
        self.assertEqual(len(fake1.init_calls), 1)

        telemetry.reset()
        os.environ.pop("SENTRY_DSN", None)
        telemetry.capture_message("second, after DSN removed")     # back to a silent no-op
        self.assertEqual(len(fake1.messages), 1)                    # no new capture reached fake1


if __name__ == "__main__":
    unittest.main()
