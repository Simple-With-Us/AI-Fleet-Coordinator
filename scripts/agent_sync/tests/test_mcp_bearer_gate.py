"""No real credentials or live services: the own-bearer gate for `simple-chat mcp`."""
import argparse
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_sync import fleet_bearer as F
from agent_sync.mcp import bearer_gate as G


class FakeClient:
    seen = []

    def __init__(self, path, expected_seat=None, identity_env=None, *, seat=None, who=None, error=None):
        FakeClient.seen.append((Path(path), expected_seat, dict(identity_env or {})))
        self.seat = seat or expected_seat
        self._who = who
        self._error = error

    def rpc(self, method, params, write=False):
        assert method == "tools/call" and params == {"name": "whoami", "arguments": {}}
        if self._error:
            raise self._error
        return {"structuredContent": self._who if self._who is not None else {"seat": self.seat, "authenticated_seat": self.seat}}


def factory(**kw):
    return lambda path, expected_seat=None, identity_env=None: FakeClient(path, expected_seat, identity_env, **kw)


def private_bearer(home, seat, token, mode=0o600):
    """`home/.secrets/fleet-tools/<seat>` created with owner-only modes at creation, never chmod-after."""
    folder = home / ".secrets"
    for part in (folder, folder / "fleet-tools"):
        if part.is_symlink():
            raise ValueError("refusing symlink: %s" % part)
        part.mkdir(mode=0o700)
    fd = os.open(folder / "fleet-tools" / seat, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
        os.fchmod(handle.fileno(), mode)


def runtime(env, home="/home/x"):
    return SimpleNamespace(env=env, home=Path(home))


def args(**kw):
    return argparse.Namespace(**{"as_seat": None, "default_seat": None, **kw})


class BearerGateTests(unittest.TestCase):
    def setUp(self):
        FakeClient.seen = []

    def test_a_confirmed_own_bearer_serves_that_seat_from_its_own_file(self):
        seat = G.verify(runtime({}), args(default_seat="codex"), client_factory=factory())
        self.assertEqual(seat, "CODEX")
        self.assertEqual(FakeClient.seen[0][0], Path("/home/x/.secrets/fleet-tools/codex"))
        self.assertEqual(FakeClient.seen[0][1], "CODEX")

    def test_no_seat_is_refused_before_any_bearer_is_read(self):
        with self.assertRaises(G.Refused) as exc:
            G.verify(runtime({}), args(), client_factory=factory())
        self.assertEqual(exc.exception.exit_code, 3)
        self.assertEqual(FakeClient.seen, [])

    def test_an_environment_bearer_never_selects_or_replaces_the_file(self):
        env = {"SIMPLE_TOOLS_BEARER": "sbt_clutch_" + "x" * 43, "ASSIGNED_MCP_BEARER": "sbt_clutch_" + "x" * 43}
        G.verify(runtime(env), args(default_seat="CODEX"), client_factory=factory())
        self.assertEqual(FakeClient.seen[0][0].name, "codex")

    def test_a_launcher_seat_wins_and_a_conflicting_claim_is_refused(self):
        env = {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "CODEX"}
        self.assertEqual(G.verify(runtime(env), args(default_seat="CLUTCH"), client_factory=factory()), "CODEX")
        with self.assertRaises(G.Refused) as exc:
            G.verify(runtime(env), args(as_seat="CLUTCH"), client_factory=factory())
        self.assertEqual(exc.exception.exit_code, 3)
        with self.assertRaises(G.Refused):
            G.verify(runtime({"AGENT_LAUNCHER": "botfleet"}), args(default_seat="CODEX"), client_factory=factory())

    def test_a_gateway_answer_for_another_seat_is_refused(self):
        for who in ({"seat": "CLUTCH", "authenticated_seat": "CLUTCH"}, {"seat": "CODEX", "authenticated_seat": "CLUTCH"}, "x", {}):
            with self.assertRaises(G.Refused) as exc:
                G.verify(runtime({}), args(default_seat="CODEX"), client_factory=factory(who=who))
            self.assertEqual(exc.exception.exit_code, 3)

    def test_gateway_errors_map_to_exit_codes_and_never_carry_the_bearer(self):
        for code, exit_code in (("not_configured", 3), ("not_authorized", 3), ("unavailable", 6), ("outcome_unknown", 6)):
            with self.assertRaises(G.Refused) as exc:
                G.verify(runtime({}), args(default_seat="CODEX"), client_factory=factory(error=F.FleetError(code, "sbt_codex_" + "y" * 43)))
            self.assertEqual(exc.exception.exit_code, exit_code)
            self.assertNotIn("sbt_", exc.exception.message)

    def test_a_bad_seat_name_is_exit_2_with_no_path(self):
        with self.assertRaises(G.Refused) as exc:
            G.verify(runtime({}), args(default_seat="../x"), client_factory=factory())
        self.assertIn(exc.exception.exit_code, (2, 3))
        self.assertEqual(FakeClient.seen, [])

    def test_the_real_client_refuses_another_seats_token_in_this_seats_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            private_bearer(home, "codex", "sbt_clutch_" + "z" * 43)
            with self.assertRaises(G.Refused) as exc:
                G.verify(runtime({}, home), args(default_seat="CODEX"))
            self.assertEqual(exc.exception.exit_code, 3)

    def test_a_missing_or_unsafe_file_is_refused_with_no_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(G.Refused) as exc:
                G.verify(runtime({}, home), args(default_seat="CODEX"))
            self.assertEqual(exc.exception.exit_code, 3)
            private_bearer(home, "codex", "sbt_codex_" + "z" * 43, mode=0o644)
            with self.assertRaises(G.Refused):
                G.verify(runtime({}, home), args(default_seat="CODEX"))


class CallGuardTests(unittest.TestCase):
    """Every tools/call is re-checked, so a revoked bearer stops the next call before a tool runs."""

    def server(self, guard):
        from agent_sync.mcp import stdio as S

        calls = []

        class Tools:
            seat = "CODEX"
            spec = {"tools": []}

            def call(self, name, arguments, meta=None):
                calls.append(name)
                return {"content": [], "isError": False}

        return S.Server(Tools(), call_guard=guard), calls

    def test_refused_call_never_reaches_the_tool(self):
        server, calls = self.server(lambda: "bearer check failed:  x;  nothing ran")
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "whoami"}})
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(calls, [])

    def test_accepted_call_runs_and_the_guard_is_asked_each_time(self):
        asked = []
        server, calls = self.server(lambda: asked.append(1))
        for i in (1, 2):
            server.handle({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": "whoami"}})
        self.assertEqual((len(asked), calls), (2, ["whoami", "whoami"]))


class WiringTests(unittest.TestCase):
    """The gate sits inside the stdio server's own entry, so no caller starts it without the bearer."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_run_verifies_before_it_enumerates_or_serves(self):
        text = (self.ROOT / "mcp" / "stdio.py").read_text(encoding="utf-8")
        run = text.index("\ndef run(")
        gate = text.index("_bearer_verify(rt, args)", run)
        self.assertLess(gate, text.index("X.allowed_identities(", run))
        self.assertLess(gate, text.index("launcher_pinned=launcher_pinned", run))
        self.assertLess(text.index("launcher_pinned = True", gate), text.index("X.allowed_identities(", run))
        self.assertEqual(text.count("_bearer_verify = BG.verify"), 1)
        self.assertIn("call_guard=call_guard", text)

    def test_cli_has_no_second_entry_to_the_server(self):
        text = (self.ROOT / "cli.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("mcp_stdio.run("), 1)
        self.assertNotIn("bearer_verified_seat", text)


class GatewayTimeoutTests(unittest.TestCase):
    """The real client over a fake HTTPS connection:  one slow whoami is cut off and refuses the call."""

    def run_with(self, behaviour):
        seen = []

        class Conn:
            def __init__(self, host, timeout=None):
                seen.append(timeout)

            def request(self, *a, **k):
                if behaviour == "timeout":
                    raise socket.timeout("slow")

            def getresponse(self):
                raise AssertionError("not reached")

            def close(self):
                pass

        original = F.http.client.HTTPSConnection
        F.http.client.HTTPSConnection = Conn
        self.addCleanup(setattr, F.http.client, "HTTPSConnection", original)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            private_bearer(home, "codex", "sbt_codex_" + "z" * 43)
            with self.assertRaises(G.Refused) as exc:
                G.verify(runtime({}, home), args(default_seat="CODEX"))
        return seen, exc.exception

    def test_a_slow_gateway_refuses_within_the_bound_and_serves_nothing(self):
        seen, refused = self.run_with("timeout")
        self.assertEqual(seen, [G.GATEWAY_TIMEOUT_SECONDS])
        self.assertEqual(G.GATEWAY_TIMEOUT_SECONDS, 4)
        self.assertEqual(refused.exit_code, 6)

    def test_a_double_without_the_real_transport_is_left_alone(self):
        client = FakeClient("/x", "CODEX", {})
        self.assertIs(G.bound(client), client)
        self.assertFalse(hasattr(client, "_transport"))


if __name__ == "__main__":
    unittest.main()
