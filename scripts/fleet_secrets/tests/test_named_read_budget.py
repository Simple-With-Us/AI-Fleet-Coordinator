"""Named reads must fit one deadline:  the knobs ask for every registered name at once."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from fleet_secrets import core

PROJECT = "11111111-2222-3333-4444-555555555555"
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


class NamedReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        patcher = mock.patch.object(core, "verify_service_token", return_value=("/",))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_api(self, method, path, token, body, env, *, max_bytes, deadline):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.05)
        with self.lock:
            self.running -= 1
        name = path.split("/v3/secrets/raw/")[1].split("?")[0]
        if name == "ABSENT":
            return 404, {}
        return 200, {"secret": record(name)}

    def test_thirty_one_names_run_side_by_side_and_keep_their_answers(self):
        names = {f"KNOB_{i:02d}" for i in range(30)} | {"ABSENT"}
        started = time.monotonic()
        with mock.patch.object(core, "_api_json", self.fake_api):
            got = core.fetch_project(PROJECT, "/", {}, token=TOKEN, names=names, timeout=8.0)
        elapsed = time.monotonic() - started
        self.assertEqual(set(got), names - {"ABSENT"})
        self.assertEqual(got["KNOB_07"], "value-KNOB_07")
        self.assertGreater(self.peak, 1)
        self.assertLessEqual(self.peak, core.MAX_NAMED_READS)
        self.assertLess(elapsed, 31 * 0.05)

    def test_one_failed_read_still_fails_the_whole_fetch(self):
        def failing(method, path, token, body, env, *, max_bytes, deadline):
            return 500, {}

        with mock.patch.object(core, "_api_json", failing):
            with self.assertRaises(core.SecretsError):
                core.fetch_project(PROJECT, "/", {}, token=TOKEN, names={"A", "B"}, timeout=8.0)


if __name__ == "__main__":
    unittest.main()
