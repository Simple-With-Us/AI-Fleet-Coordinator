"""fleet-recall-mcp.py serves a call only for the seat whose own bearer the hosted gateway accepts.

No network, no real bearer:  the gateway is a double and the one bearer file below holds a made-up token.
Run from scripts/:  python3 -m unittest fleet_rag.tests.test_recall_seat_auth -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import pathlib
import stat
import tempfile
import unittest
from unittest import mock

from fleet_rag import fleet_bearer as F
from fleet_rag import core, recall_api, seat_auth

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
SERVER = SCRIPTS / "fleet-recall-mcp.py"
SEAMS = ("load_config", "embed", "embedder_healthy", "rerank_healthy", "Qdrant", "gitleaks_flagged",
         "gitleaks_available")
LESSON = "Always restart pm2 with --update-env after changing the ecosystem file, or the old env stays cached."


public_fallback_real = None  # set in setUpModule


class Gateway:
    """The hosted gateway's whoami, as the bearer client sees it."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.asked = 0

    def __call__(self, path, expected_seat=None, identity_env=None):
        gateway = self
        seat = expected_seat

        class Client:
            def __init__(self):
                self.seat = seat

            def rpc(self, method, params, write=False):
                gateway.asked += 1
                if gateway.mode == "revoked":
                    raise F.FleetError("not_authorized", "gateway HTTP 401")
                if gateway.mode == "unavailable":
                    raise F.FleetError("unavailable", "gateway transport or response failed")
                answer = "OTHER" if gateway.mode == "other-seat" else seat
                return {"structuredContent": {"seat": answer, "authenticated_seat": answer}}

        return Client()


def setUpModule():
    global public_fallback_real
    from fleet_rag import public_fallback

    public_fallback_real = public_fallback.call_with_fallback


class GatedServerTests(unittest.TestCase):
    def setUp(self):
        self.gateway = Gateway()
        for patcher in (
            mock.patch.object(seat_auth, "client_factory", self.gateway),
            mock.patch.dict(os.environ, {"AGENT_SEAT": "CLAUDE"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("AGENT_LAUNCH_SEAT", "AGENT_TAG", "AGENT_LAUNCHER"):
            os.environ.pop(name, None)
        # Other tests install the fake corpus and may leave it behind:  these tests run on the real seams.
        saved = {k: getattr(recall_api, k) for k in SEAMS}
        for k in SEAMS:
            self.addCleanup(setattr, recall_api, k, saved[k])
        recall_api.Qdrant = core.Qdrant
        loader = importlib.machinery.SourceFileLoader("fleet_recall_mcp_seat_auth_test", str(SERVER))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)
        self.backend = []

        def fallback(name, args, local):
            self.backend.append((name, dict(args)))
            return {"hits": [], "mode": "test", "status": "stored"} if name != "recall_stats" else {"points": 1}

        patcher = mock.patch.object(self.mod.public_fallback, "call_with_fallback", fallback)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logged = []
        patcher = mock.patch.object(self.mod.search_log, "log_search", lambda **kw: self.logged.append(kw))
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, name, args):
        response = self.mod.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": args}})
        return response["result"]

    def test_an_accepted_bearer_serves_and_the_search_log_names_its_seat(self):
        result = self.call("recall_search", {"query": "pm2", "seat": "GROK", "per_doc": 2})
        self.assertIs(result["isError"], False)
        name, args = self.backend[0]
        self.assertEqual((name, args["seat"], args["per_doc"]), ("recall_search", "GROK", 2))  # a corpus filter
        self.assertEqual(self.logged[0]["seat"], "CLAUDE")  # the asker is the bearer's seat

    def test_contribute_is_stored_under_the_bearers_seat_only(self):
        self.call("recall_contribute", {"text": LESSON, "category": "lesson"})
        self.call("recall_contribute", {"text": LESSON, "category": "lesson", "seat": "claude", "force": True})
        self.assertEqual([a["seat"] for _, a in self.backend], ["CLAUDE", "CLAUDE"])
        self.backend.clear()
        refused = self.call("recall_contribute", {"text": LESSON, "category": "lesson", "seat": "GROK"})
        self.assertIs(refused["isError"], True)
        self.assertIn("own seat", refused["content"][0]["text"])
        self.assertEqual(self.backend, [])

    def test_every_call_is_checked_and_a_revoked_bearer_stops_the_next_one(self):
        self.call("recall_stats", {})
        self.call("recall_search", {"query": "pm2"})
        self.assertEqual(self.gateway.asked, 2)
        self.gateway.mode = "revoked"
        self.backend.clear()
        for name, args in (("recall_stats", {}), ("recall_search", {"query": "pm2"}),
                           ("recall_contribute", {"text": LESSON, "category": "lesson"})):
            with self.subTest(name):
                result = self.call(name, args)
                self.assertIs(result["isError"], True)
                self.assertIn("nothing was served", result["content"][0]["text"])
        self.assertEqual(self.backend, [])
        self.gateway.mode = "ok"
        self.assertIs(self.call("recall_stats", {})["isError"], False)

    def test_an_unreachable_or_other_seat_gateway_serves_nothing(self):
        for mode in ("unavailable", "other-seat"):
            with self.subTest(mode):
                self.gateway.mode = mode
                self.assertIs(self.call("recall_search", {"query": "x"})["isError"], True)
        self.assertEqual(self.backend, [])

    def test_no_seat_or_conflicting_launcher_seats_serve_nothing(self):
        for env in ({"AGENT_SEAT": ""}, {"AGENT_LAUNCH_SEAT": "GROK"}, {"AGENT_LAUNCHER": "botfleet", "AGENT_SEAT": "CLAUDE"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertIs(self.call("recall_search", {"query": "x"})["isError"], True)
        self.assertEqual((self.backend, self.gateway.asked), ([], 0))  # refused before any gateway call

    def test_initialize_and_tools_list_need_no_bearer(self):
        self.gateway.mode = "revoked"
        for method in ("initialize", "tools/list", "ping"):
            self.assertIn("result", self.mod.handle({"jsonrpc": "2.0", "id": 1, "method": method}))
        self.assertEqual(self.gateway.asked, 0)

    def test_a_leftover_fake_backend_never_turns_the_proof_off(self):
        recall_api.install_fake_backend()  # what a previous test, tool or import may leave behind
        self.assertTrue(recall_api.fake_backend_installed())
        self.gateway.mode = "revoked"
        self.assertIs(self.mod.FAKE_CORPUS, False)
        self.assertIs(self.call("recall_stats", {})["isError"], True)
        self.assertEqual(self.backend, [])

    def test_the_fake_flag_without_the_fake_backend_serves_nothing(self):
        self.mod.FAKE_CORPUS = True
        result = self.call("recall_stats", {})
        self.assertIs(result["isError"], True)
        self.assertEqual((self.backend, self.gateway.asked), ([], 0))

    def test_the_fake_corpus_is_not_gated_and_reaches_no_public_or_provider_path(self):
        recall_api.install_fake_backend()
        self.mod.FAKE_CORPUS = True
        self.gateway.mode = "revoked"
        boom = mock.Mock(side_effect=AssertionError("a real path was reached"))
        with mock.patch.object(self.mod.public_fallback, "call_with_fallback", public_fallback_real), \
             mock.patch.object(self.mod.public_fallback, "_fallback", boom), \
             mock.patch.object(core, "http_json", boom), \
             mock.patch("subprocess.run", boom):
            for name, args in (("recall_stats", {}), ("recall_search", {"query": "pm2"}),
                               ("recall_contribute", {"text": LESSON, "category": "lesson"})):
                with self.subTest(name):
                    self.assertIs(self.call(name, args)["isError"], False)
        boom.assert_not_called()
        self.assertEqual(self.gateway.asked, 0)


class SubprocessTests(unittest.TestCase):
    def test_a_real_server_without_the_seats_bearer_file_serves_nothing(self):
        import json
        import subprocess
        import sys

        home = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, home)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "AGENT_SEAT": "CLAUDE"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "recall_stats", "arguments": {}}}
        proc = subprocess.run([sys.executable, str(SERVER)], input=json.dumps(request) + "\n", env=env,
                              capture_output=True, text=True, timeout=30)
        result = json.loads(proc.stdout)["result"]
        self.assertIs(result["isError"], True)
        self.assertIn("not_configured", result["content"][0]["text"])


class RealClientChecks(unittest.TestCase):
    """The vendored client with a real private file and no network:  construction refuses before any call."""

    def bearer_file(self, seat="claude"):
        folder = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in folder.glob("*")] and folder.rmdir())
        path = folder / seat
        path.write_text("sbt_%s_%s" % (seat, "A" * 43))
        path.chmod(0o600)
        return path

    def test_another_seat_or_a_launcher_hint_for_one_is_refused(self):
        path = self.bearer_file()
        with self.assertRaises(F.FleetError):
            F.Client(path, expected_seat="GROK", identity_env={})
        with self.assertRaises(F.FleetError):
            F.Client(path, expected_seat="CLAUDE", identity_env={"AGENT_SEAT": "GROK"})
        self.assertEqual(F.Client(path, expected_seat="CLAUDE", identity_env={"AGENT_SEAT": "claude"}).seat, "CLAUDE")

    def test_a_group_readable_bearer_file_is_refused_and_the_path_is_per_seat(self):
        path = self.bearer_file()
        path.chmod(0o640)
        with self.assertRaises(F.FleetError):
            F.Client(path, expected_seat="CLAUDE", identity_env={})
        home = pathlib.Path("/h")
        self.assertEqual(seat_auth.bearer_path(home, "BF-PLUMBER"), home / ".secrets" / "fleet-tools" / "bf-plumber")
        with self.assertRaises(seat_auth.FleetRagError):
            seat_auth.bearer_path(home, "../x")

    def test_vendored_client_is_the_agent_sync_client_byte_for_byte(self):
        other = SCRIPTS / "agent_sync" / "fleet_bearer.py"
        if not other.exists():
            self.skipTest("agent_sync is not deployed beside fleet_rag")
        self.assertEqual((SCRIPTS / "fleet_rag" / "fleet_bearer.py").read_bytes(), other.read_bytes())


if __name__ == "__main__":
    unittest.main()
