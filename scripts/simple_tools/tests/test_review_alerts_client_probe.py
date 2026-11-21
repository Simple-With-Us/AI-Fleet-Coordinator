"""Combined integration:  the review-alerts WakeClient against the ACTUAL
0311 credential-overlay router (plus the require_pr patch), over real HTTP.

Proves the client probe shape passes the overlay (channel review-alerts,
ra- id, exact session/letter, positive generation), the require_pr startup
gate fails closed against a router without the patch's validation, and a
real alert submit lands scoped and durable.
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


def platform() -> WR.Platform:
    return WR.Platform(
        seat="CLAUDE",
        platform="claude",
        mention="@claude",
        target_re=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$"),
        deliver=lambda target, wake: WR.Delivery(outcome="ok", detail=""),
    )


class FakeRegistry:
    rows = []

    def __call__(self, *a, **k):
        return {"items": list(self.rows)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="raprobe", dir="/tmp")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.registry = FakeRegistry()
        patcher = mock.patch.object(WR.registry_api, "call", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.router = WR.Router(platform(), {}, {}, Path(self.tmp),
                                lambda: (ROUTINE_KEY, ""), consult=None)
        self.router._review_key = lambda: (SERVICE_KEY, "")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          WR.make_handler(WR.Seats([self.router])))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        port = self.server.server_address[1]
        from review_alerts import wakes
        self.wakes = wakes
        self.client = wakes.WakeClient("http://127.0.0.1:%d" % port,
                                       lambda: SERVICE_KEY)


class ProbeShapeTests(Base):
    def test_probe_passes_the_overlay(self):
        answer = self.client.test_submit("CLAUDE", "ra-probe1",
                                         "zz0reviewalerts0probe0noresuchsession")
        self.assertTrue(str(answer.get("reason") or "").startswith("required-session-"))

    def test_startup_gate_passes_against_patched_router(self):
        self.client.check_required_session_support("CLAUDE", probe_id="it1")

    def test_startup_gate_fails_closed_without_require_pr_validation(self):
        # Simulate an overlay router WITHOUT the require_pr patch by
        # stubbing REQUIRE_PR_RE to match everything (validation never
        # refuses):  the gate must refuse to run.
        with mock.patch.object(WR, "REQUIRE_PR_RE", re.compile(r".*")):
            with self.assertRaises(self.wakes.WakeError) as ctx:
                self.client.check_required_session_support("CLAUDE", probe_id="it2")
            self.assertIn("require_pr", str(ctx.exception))

    def test_real_submit_lands_scoped_and_durable(self):
        status, answer = self.client._call("GET", "/health")
        self.assertIn(status, (200, 503))  # health reflects registry state, not auth
        submitted = self.client.submit("CLAUDE", "ra-real1", SID_A, "AB", 1,
                                       "Simple-With-Us/HogHunter#7",
                                       "HogHunter#7: review ready",
                                       "https://example/pr/7")
        self.assertIn("wake_id", submitted)
        self.assertEqual(self.router._entries["ra-real1"]["wake"]["credential_scope"],
                         "review-alerts")

    def test_routine_key_cannot_use_reserved_namespace(self):
        routine = self.wakes.WakeClient("http://127.0.0.1:%d" % self.server.server_address[1],
                                        lambda: ROUTINE_KEY)
        with self.assertRaises(self.wakes.WakeError):
            routine.submit("CLAUDE", "ra-nope1", SID_A, "AB", 1,
                           "Simple-With-Us/HogHunter#7", "t", "")


class ScopeRequiresPrTests(Base):
    def test_service_scope_without_require_pr_is_403(self):
        import urllib.error
        payload = {
            "contract": WR.CONTRACT, "seat": "CLAUDE", "wake_id": "ra-nopr1",
            "require_session": SID_A, "require_letter": "AB", "require_generation": 1,
            "channel": "review-alerts", "excerpt": "x", "dm": False,
        }
        import json as _json
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            "http://127.0.0.1:%d/wake/CLAUDE" % self.server.server_address[1],
            data=_json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + SERVICE_KEY,
                     "Content-Type": "application/json"})
        try:
            __import__("urllib.request", fromlist=["urlopen"]).urlopen(req, timeout=5)
            self.fail("expected 403")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 403)
