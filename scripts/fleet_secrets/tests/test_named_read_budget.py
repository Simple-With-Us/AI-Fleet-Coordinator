"""Named reads must fit one deadline:  the knobs ask for every registered name at once."""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from unittest import mock

from fleet_secrets import core

PROJECT = str(uuid.uuid4())
TOKEN = ".".join(["st", "fixture", "fixture", "fixture"])


def record(name: str) -> dict:
    return {
        "type": "shared",
        "workspace": PROJECT,
        "environment": core.ENVIRONMENT,
        "secretPath": "/",
        "secretKey": name,
        "secretValue": "value-" + name,
    }


def name_of(path: str) -> str:
    return path.split("/v3/secrets/raw/")[1].split("?")[0]


class NamedReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.deadlines = []
        self.limits = []
        patcher = mock.patch.object(core, "verify_service_token", return_value=("/",))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_api(self, method, path, token, body, env, *, max_bytes, deadline):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.deadlines.append(deadline)
            self.limits.append(max_bytes)
        time.sleep(0.05)
        with self.lock:
            self.running -= 1
        name = name_of(path)
        if name == "ABSENT":
            return 404, {}
        return 200, {"secret": record(name)}

    def test_thirty_one_names_run_side_by_side_inside_one_deadline(self):
        names = {f"KNOB_{i:02d}" for i in range(30)} | {"ABSENT"}
        timeout = 8.0
        started = time.monotonic()
        with mock.patch.object(core, "_api_json", self.fake_api):
            got = core.fetch_project(PROJECT, "/", {}, token=TOKEN, names=names, timeout=timeout)
        elapsed = time.monotonic() - started
        self.assertEqual(set(got), names - {"ABSENT"})
        self.assertEqual(got["KNOB_07"], "value-KNOB_07")
        self.assertGreater(self.peak, 1)
        self.assertLessEqual(self.peak, core.MAX_NAMED_READS)
        self.assertLess(elapsed, 31 * 0.05)
        self.assertLess(elapsed, timeout)
        self.assertEqual(len(self.deadlines), 31)
        self.assertEqual(len(set(self.deadlines)), 1)
        self.assertLessEqual(self.deadlines[0], started + timeout + 1)
        self.assertGreater(self.deadlines[0], started)
        self.assertEqual(set(self.limits), {core.MAX_VERIFIED_BYTES})

    def test_a_failed_read_discards_the_good_answers(self):
        def mixed(method, path, token, body, env, *, max_bytes, deadline):
            if name_of(path) == "BAD":
                return 500, {}
            return 200, {"secret": record(name_of(path))}

        with mock.patch.object(core, "_api_json", mixed):
            with self.assertRaises(core.SecretsError):
                core.fetch_project(
                    PROJECT, "/", {}, token=TOKEN, names={"BAD", "GOOD"}, timeout=8.0
                )

    def test_the_first_failure_stops_reads_that_have_not_started(self):
        calls = []

        def failing(method, path, token, body, env, *, max_bytes, deadline):
            with self.lock:
                calls.append(path)
            time.sleep(0.02)
            return 500, {}

        names = {f"KNOB_{i:02d}" for i in range(60)}
        with mock.patch.object(core, "_api_json", failing):
            with self.assertRaises(core.SecretsError):
                core.fetch_project(PROJECT, "/", {}, token=TOKEN, names=names, timeout=8.0)
        # The first failure lands after one round of reads, so at most the reads already running
        # and one more round can start before every other read is cancelled.
        self.assertLessEqual(len(calls), 2 * core.MAX_NAMED_READS)

    def test_the_refusal_names_the_earliest_failing_read(self):
        def two_bad(method, path, token, body, env, *, max_bytes, deadline):
            name = name_of(path)
            if name == "A_FIRST":
                time.sleep(0.05)
                return 500, {}
            if name == "B_SECOND":
                return 200, {"secret": dict(record(name), secretValue=3)}
            return 200, {"secret": record(name)}

        for _ in range(5):
            with mock.patch.object(core, "_api_json", two_bad):
                with self.assertRaises(core.SecretsError) as caught:
                    core.fetch_project(
                        PROJECT, "/", {}, token=TOKEN, names={"A_FIRST", "B_SECOND"}, timeout=8.0
                    )
            self.assertEqual(str(caught.exception), "fetch-failed")


if __name__ == "__main__":
    unittest.main()
