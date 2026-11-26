"""Delivery-time PR-binding recheck:  the ACTUAL router (0311 overlay bytes +
require_pr patch) drives a wake whose binding vanishes between pick and
delivery, and the ACTUAL review_alerts WakeClient reads the real receipt.

The terminal receipt keeps the successful pick reason in `reason` and the
failure in `code`:  the client must classify by code first, or the bounded
required-pr-unbound redrive never fires.
"""

from __future__ import annotations

import re
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review_alerts-parent"))

from simple_tools import wake_router as WR

SERVICE_KEY = "service-" + "x" * 48
ROUTINE_KEY = "k" * 48
SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
PR = "Simple-With-Us/HogHunter#7"
BOUND = [{"repo": "Simple-With-Us/HogHunter", "number": 7, "generation": 1,
          "ts": "2026-11-21T09:00:00Z"}]


def row(**over):
    base = {
        "native_session_id": SID_A,
        "letter": "AB",
        "title": "pr work",
        "verified_host": "server",
        "seat": "CLAUDE",
        "state": "active",
        "retired_at": None,
        "closed_at": None,
        "generation": 1,
    }
    base.update(over)
    return base


class RaceRegistry:
    """Pick-time view carries the binding;  the delivery-time fresh read does
    not (the binding was removed - or never landed - between the two)."""

    def __init__(self, resolve_bound: bool) -> None:
        self.resolve_bound = resolve_bound

    def __call__(self, env, op, params=None, *, timeout=25):
        if op == "history":
            return {"items": [row(bound_prs=BOUND)], "count": 1}
        if op == "resolve":
            return {**row(bound_prs=BOUND if self.resolve_bound else []),
                    "label": params["letter"], "found": True}
        raise AssertionError(op)


def platform() -> WR.Platform:
    return WR.Platform(
        seat="CLAUDE",
        platform="claude",
        mention="@claude",
        target_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$"),
        deliver=lambda target, wake, host="": WR.Delivery(outcome="completed", ack="confirmed"),
    )


class Base(unittest.TestCase):
    REGISTRY_BOUND = True

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="radr", dir="/tmp")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.registry = RaceRegistry(self.REGISTRY_BOUND)
        patcher = mock.patch.object(WR.registry_api, "call", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        config = dict(WR.ROUTER_DEFAULTS, delivery_enabled=True, workers=1,
                      max_attempts=1, retry_backoff=[0], busy_retry_seconds=1,
                      unreachable_retry_seconds=1)
        self.router = WR.Router(platform(), {}, config, Path(self.tmp),
                                lambda: (ROUTINE_KEY, ""), consult=None)
        self.router._review_key = lambda: (SERVICE_KEY, "")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          WR.make_handler(WR.Seats([self.router])))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        from review_alerts import wakes
        self.wakes = wakes
        port = self.server.server_address[1]
        self.client = wakes.WakeClient("http://127.0.0.1:%d" % port,
                                       lambda: SERVICE_KEY)

    def _drive(self) -> dict:
        submitted = self.client.submit("CLAUDE", "ra-recheck1", SID_A, "AB", 1, PR,
                                       "HogHunter#7: review ready", "https://example/pr/7")
        self.assertIn("wake_id", submitted)
        for _ in range(4):
            if not self.router.run_once():
                break
        receipt = self.client.receipt("CLAUDE", "ra-recheck1")
        self.assertIsNotNone(receipt)
        return receipt


class BindingVanishesBeforeDelivery(Base):
    REGISTRY_BOUND = False

    def test_terminal_receipt_is_redrivable_through_the_real_client(self):
        receipt = self._drive()
        # The trap the review found:  the receipt still names the successful
        # pick in `reason`;  the delivery-time failure is only in `code`.
        self.assertEqual(receipt["reason"], "required-session")
        self.assertEqual(receipt["code"], "required-pr-unbound")
        self.assertIs(receipt["delivered"], False)
        terminal, outcome = self.wakes.WakeClient.receipt_terminal(receipt)
        self.assertTrue(terminal)
        self.assertEqual(outcome, "held:required-pr-unbound")
        # ...so the service's bounded redrive (fresh epoch) actually fires.
        self.assertIn(outcome[5:], self.wakes.REDRIVE_REASONS)


class BindingStillThere(Base):
    REGISTRY_BOUND = True

    def test_delivered_receipt_stays_delivered(self):
        receipt = self._drive()
        self.assertIs(receipt["delivered"], True)
        terminal, outcome = self.wakes.WakeClient.receipt_terminal(receipt)
        self.assertEqual((terminal, outcome), (True, "delivered"))


if __name__ == "__main__":
    unittest.main()
