"""The Operator's session registry API (listener_registry.py, Path B):  every one of the
registry's 12 ops through the HTTP adapter against a temporary registry, the HMAC
discipline (bad signature 401, wrong method 405, unknown route 404), the Operator-only
register path, cross-seat refusal, and the client transport selection in
mm_session_letter.registry_call (URL unset falls back to the existing behaviour;  URL set
uses HTTPS;  an explicit transport wins over a local file).

RegistryService.handle is driven directly with a temporary registry file and a fake
clock, so no socket is involved except in the two tests that run the service behind a
real ListenerReadApi on an ephemeral port.  Every key is generated per run, so the
source holds no key-looking literal.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_listener_registry
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

from simple_tools import listener_api as API
from simple_tools import listener_registry as RG
from simple_tools import operator_registry as OR

SCRIPTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPTS))

OPERATOR_KEY = secrets.token_hex(24)
CLAUDE_KEY = secrets.token_hex(24)
CODEX_KEY = secrets.token_hex(24)
MM_KEY = secrets.token_hex(24)
KEYS = {
    "OPERATOR": OPERATOR_KEY,
    "CLAUDE": CLAUDE_KEY,
    "CODEX": CODEX_KEY,
    "MM": MM_KEY,
}
SID_SEED = "session-seed-0001"
SID_CLAUDE = "session-claude-0001"
SID_CODEX = "session-codex-0001"


class FakeClock:
    def __init__(self) -> None:
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now


def identity(sid: str) -> dict[str, str]:
    return {
        "platform": "minimax",
        "location": "mac",
        "scope": "default",
        "session_id": sid,
    }


class RegistryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "operator", "registry.sqlite3")
        self.clock = FakeClock()
        self.service = RG.RegistryService(self.db, KEYS, clock=self.clock)

    # ---- one signed call ---------------------------------------------------

    def call(
        self,
        principal: str,
        method: str,
        op: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        sign_body: bytes | None = None,
        now: float | None = None,
    ) -> tuple[int, dict[str, Any]]:
        path = RG.PREFIX + op
        if method == "GET":
            from urllib.parse import urlencode

            query = urlencode(payload or {})
            body = b""
        else:
            query = ""
            body = json.dumps(payload or {}).encode("utf-8")
        signed = sign_body if sign_body is not None else body
        headers = RG.headers(
            key if key is not None else KEYS[principal],
            method,
            path,
            query,
            principal,
            signed,
            self.clock.now if now is None else now,
        )
        raw = path + (("?" + query) if query else "")
        return self.service.handle(method, raw, headers, body)

    def create_store(self) -> None:
        # A store comes into being as a local act on the host that holds it, never
        # through the API:  opening the registry in write mode lays the file down.
        registry = OR.SessionRegistry(self.db)
        registry.close_db()

    def register(self, seat: str, sid: str) -> dict[str, Any]:
        status, answer = self.call(
            "OPERATOR",
            "POST",
            "register",
            {
                **identity(sid),
                "seat": seat,
                "source": "registry-api test",
                "title": "Test session",
                "letter_space": "double",
                "letter_pick": "primary",
            },
        )
        self.assertEqual(status, 200, answer)
        return answer

    # ---- the 12 ops ----------------------------------------------------------

    def test_all_twelve_ops(self) -> None:
        self.create_store()
        # register (Operator only) for two seats.
        claude = self.register("CLAUDE", SID_CLAUDE)
        codex = self.register("CODEX", SID_CODEX)
        self.assertEqual(claude["label"], "AA")
        self.assertEqual(codex["label"], "BB")
        record_id = claude["record_id"]

        # resolve:  the owner seat and the Operator read it;  the letter form matches the CLI's.
        status, found = self.call(
            "CLAUDE", "GET", "resolve", {"platform": "minimax", "letter": "AA"}
        )
        self.assertEqual(status, 200, found)
        self.assertTrue(found["found"])
        self.assertEqual(found["record_id"], record_id)
        status, miss = self.call(
            "CLAUDE", "GET", "resolve", {"platform": "minimax", "letter": "ZZ"}
        )
        self.assertEqual((status, miss["found"]), (200, False))

        # update + touch, by the owning seat.
        status, updated = self.call(
            "CLAUDE", "POST", "update", {**identity(SID_CLAUDE), "title": "Renamed"}
        )
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["outcome"], "updated")
        self.assertEqual(updated["title"], "Renamed")
        status, touched = self.call("CLAUDE", "POST", "touch", identity(SID_CLAUDE))
        self.assertEqual((status, touched["outcome"]), (200, "seen"))

        # held:  the letters this record has held, by record and by identity.
        status, held = self.call("CLAUDE", "GET", "held", {"record_id": record_id})
        self.assertEqual((status, held["letters"]), (200, ["AA"]))
        status, held = self.call("CLAUDE", "GET", "held", identity(SID_CLAUDE))
        self.assertEqual((status, held["record_id"]), (200, record_id))

        # history:  a seat sees only its own rows;  the Operator sees the platform's.
        status, history = self.call("CLAUDE", "GET", "history", {"platform": "minimax"})
        self.assertEqual((status, history["count"]), (200, 1))
        status, history = self.call("OPERATOR", "GET", "history", {"platform": "minimax"})
        self.assertEqual((status, history["count"]), (200, 2))

        # sweep:  nothing is stale yet, so nothing ends.
        status, swept = self.call(
            "CLAUDE", "POST", "sweep", {"platform": "minimax", "seat": "CLAUDE"}
        )
        self.assertEqual((status, swept["count"]), (200, 0))

        # pending-outbox + mark-exported (the Operator's own surface).
        status, pending = self.call("OPERATOR", "GET", "pending-outbox", {"limit": 5})
        self.assertEqual(status, 200, pending)
        self.assertGreaterEqual(pending["count"], 1)
        outbox_id = pending["items"][0]["outbox_id"]
        status, marked = self.call(
            "OPERATOR", "POST", "mark-exported", {"outbox_ids": [outbox_id]}
        )
        self.assertEqual((status, marked["marked"]), (200, 1))

        # relabel (Operator only):  forced, the session keeps a pair.
        status, relabelled = self.call(
            "OPERATOR",
            "POST",
            "relabel",
            {**identity(SID_CLAUDE), "seat": "CLAUDE", "source": "test", "force": True},
        )
        self.assertEqual(status, 200, relabelled)
        self.assertEqual(relabelled["outcome"], "relabelled")
        self.assertEqual(len(relabelled["label"]), 2)

        # close, then reopen:  the pair comes back (a new generation of it).
        status, closed = self.call(
            "CLAUDE", "POST", "close", {**identity(SID_CLAUDE), "reason": "test"}
        )
        self.assertEqual((status, closed["closed"]), (200, True))
        status, reopened = self.call(
            "CLAUDE", "POST", "reopen", {"record_id": record_id}
        )
        self.assertEqual(status, 200, reopened)
        self.assertEqual(reopened["outcome"], "reopened")
        self.assertEqual(len(reopened["label"]), 2)

    # ---- authority -------------------------------------------------------------

    def test_register_and_relabel_refuse_a_non_operator(self) -> None:
        payload = {
            **identity(SID_CLAUDE),
            "seat": "CLAUDE",
            "source": "registry-api test",
        }
        status, answer = self.call("CLAUDE", "POST", "register", payload)
        self.assertEqual(status, 403, answer)
        self.assertEqual(answer["error"], "issuer_refused")
        # Nothing was written:  the store was never created by the refused call.
        self.assertFalse(os.path.isfile(self.db))
        self.create_store()
        self.register("CLAUDE", SID_CLAUDE)
        status, answer = self.call(
            "CLAUDE", "POST", "relabel", {**payload, "force": True}
        )
        self.assertEqual(status, 403, answer)
        self.assertEqual(answer["error"], "issuer_refused")

    def test_register_and_relabel_require_an_existing_store(self) -> None:
        # No op brings a store into being, register and relabel included:  on a
        # host with no store both answer 503, and neither call creates the file.
        # A store is created as a local act on the host that holds it.
        payload = {
            **identity(SID_CLAUDE),
            "seat": "CLAUDE",
            "source": "registry-api test",
        }
        status, answer = self.call("OPERATOR", "POST", "register", payload)
        self.assertEqual((status, answer["error"]), (503, "unavailable"), answer)
        status, answer = self.call(
            "OPERATOR", "POST", "relabel", {**payload, "force": True}
        )
        self.assertEqual((status, answer["error"]), (503, "unavailable"), answer)
        self.assertFalse(os.path.isfile(self.db))

    def test_cross_seat_access_is_refused_with_an_unfolded_identity(self) -> None:
        # The registry stores the identity folded (the platform folded by
        # normalize_platform, the session id trimmed by session_ids.check) and
        # its mutation functions fold before their lookups, so the ownership
        # pre-check folds too:  an unfolded identity that missed the stored row
        # once read as "no row, no refusal" and the mutation landed on the other
        # seat's session.
        self.create_store()
        self.register("CLAUDE", SID_CLAUDE)
        unfolded = {
            "platform": "MiniMax",
            "location": "mac",
            "scope": "default",
            "session_id": " %s " % SID_CLAUDE,
        }
        for op, payload in (
            ("update", {**unfolded, "title": "Stolen"}),
            ("touch", unfolded),
            ("close", unfolded),
        ):
            status, answer = self.call("CODEX", "POST", op, payload)
            self.assertEqual(status, 403, (op, answer))
        # The owning seat with the same unfolded identity is still itself:  the
        # fold finds its row, the call goes through, and the row was not closed
        # by the refused close above.
        status, answer = self.call("CLAUDE", "POST", "touch", unfolded)
        self.assertEqual(status, 200, answer)

    def test_cross_seat_access_is_refused(self) -> None:
        self.create_store()
        claude = self.register("CLAUDE", SID_CLAUDE)
        self.register("CODEX", SID_CODEX)
        record_id = claude["record_id"]
        cases = [
            ("GET", "resolve", {"platform": "minimax", "letter": "AA"}),
            ("POST", "update", {**identity(SID_CLAUDE), "title": "Stolen"}),
            ("POST", "touch", identity(SID_CLAUDE)),
            ("POST", "close", identity(SID_CLAUDE)),
            ("POST", "reopen", {"record_id": record_id}),
            ("GET", "held", {"record_id": record_id}),
            ("POST", "sweep", {"platform": "minimax", "seat": "CLAUDE"}),
        ]
        for method, op, payload in cases:
            status, answer = self.call("CODEX", method, op, payload)
            self.assertEqual(status, 403, (op, answer))
        # The Operator may act for any seat, and the outbox is the Operator's alone.
        status, _found = self.call(
            "OPERATOR", "GET", "resolve", {"platform": "minimax", "letter": "AA"}
        )
        self.assertEqual(status, 200)
        for method, op, payload in (
            ("GET", "pending-outbox", {}),
            ("POST", "mark-exported", {"outbox_ids": []}),
        ):
            status, answer = self.call("CLAUDE", method, op, payload)
            self.assertEqual(status, 403, (op, answer))

    # ---- the HMAC discipline ------------------------------------------------------

    def test_hmac_discipline(self) -> None:
        self.create_store()
        self.register("CLAUDE", SID_CLAUDE)
        query = {"platform": "minimax", "letter": "AA"}
        # A bad signature is 401.
        status, answer = self.call("CLAUDE", "GET", "resolve", query, key=CODEX_KEY)
        self.assertEqual((status, answer["error"]), (401, "unauthorized"))
        # A stale timestamp is 401.
        status, answer = self.call(
            "CLAUDE", "GET", "resolve", query, now=self.clock.now - RG.MAX_SKEW_S - 60
        )
        self.assertEqual(status, 401, answer)
        # A body that is not the signed body is 401.
        status, answer = self.call(
            "CLAUDE",
            "POST",
            "touch",
            identity(SID_CLAUDE),
            sign_body=b"{}",
        )
        self.assertEqual(status, 401, answer)
        # An unknown principal is 404, and so is one whose key is too short to accept.
        status, answer = self.call("CLAUDE", "GET", "resolve", query, key=OPERATOR_KEY)
        # (OPERATOR_KEY is a real key, but not CLAUDE's:  that is a bad signature, 401.)
        self.assertEqual(status, 401, answer)
        ghost = RG.headers(
            secrets.token_hex(24), "GET", RG.PREFIX + "resolve",
            "platform=minimax&letter=AA", "GHOST", b"", self.clock.now,
        )
        status, answer = self.service.handle(
            "GET", RG.PREFIX + "resolve?platform=minimax&letter=AA", ghost, b""
        )
        self.assertEqual(status, 404, answer)
        short = RG.RegistryService(self.db, {"SHORTY": "x" * 10}, clock=self.clock)
        status, answer = short.handle(
            "GET",
            RG.PREFIX + "resolve?platform=minimax&letter=AA",
            RG.headers("x" * 10, "GET", RG.PREFIX + "resolve",
                       "platform=minimax&letter=AA", "SHORTY", b"", self.clock.now),
            b"",
        )
        self.assertEqual(status, 404, answer)

    def test_method_and_route_discipline(self) -> None:
        # A known op with the wrong method is 405, signed or not.
        status, answer = self.call("OPERATOR", "POST", "resolve", {"platform": "minimax"})
        self.assertEqual((status, answer["error"]), (405, "method_not_allowed"))
        status, answer = self.call("OPERATOR", "GET", "register", {})
        self.assertEqual(status, 405, answer)
        status, answer = self.service.handle("DELETE", RG.PREFIX + "resolve", {}, b"")
        self.assertEqual(status, 405, answer)
        # An unknown route is 404.
        status, answer = self.service.handle("GET", RG.PREFIX + "nope", {}, b"")
        self.assertEqual(status, 404, answer)
        status, answer = self.service.handle("GET", "/listener/v1/CLAUDE/status", {}, b"")
        self.assertEqual(status, 404, answer)

    # ---- behind the real listener server -------------------------------------------

    def _serve(self) -> API.ListenerReadApi:
        server = API.ListenerReadApi(
            "127.0.0.1", 0, self.tmp.name, {}, clock=self.clock, registry=self.service
        )
        server.start()
        self.addCleanup(server.close)
        return server

    def _http(
        self, server: API.ListenerReadApi, principal: str, method: str, op: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        from urllib.parse import urlencode

        host, port = server.address
        path = RG.PREFIX + op
        if method == "GET":
            query = urlencode(payload or {})
            body = b""
        else:
            query = ""
            body = json.dumps(payload or {}).encode("utf-8")
        url = "http://%s:%d%s%s" % (host, port, path, ("?" + query) if query else "")
        request = urllib.request.Request(
            url, data=body if method == "POST" else None, method=method
        )
        for name, value in RG.headers(
            KEYS[principal], method, path, query, principal, body, self.clock.now
        ).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_behind_the_listener_server(self) -> None:
        self.create_store()
        server = self._serve()
        status, answer = self._http(
            server, "OPERATOR", "POST", "register",
            {**identity(SID_CLAUDE), "seat": "CLAUDE", "source": "socket test",
             "letter_space": "double", "letter_pick": "primary"},
        )
        self.assertEqual((status, answer["label"]), (200, "AA"))
        status, answer = self._http(
            server, "CLAUDE", "GET", "resolve", {"platform": "minimax", "letter": "AA"}
        )
        self.assertEqual((status, answer["found"]), (200, True))
        # A request naming no principal is 404 (the notes queue's discipline:  an unknown
        # principal is not found, a known one with a bad signature is 401), and another
        # method entirely is 405.
        host, port = server.address
        request = urllib.request.Request(
            "http://%s:%d%sresolve?platform=minimax&letter=AA" % (host, port, RG.PREFIX),
            method="GET",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 404)
        request = urllib.request.Request(
            "http://%s:%d%sresolve" % (host, port, RG.PREFIX), method="DELETE"
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 405)


class RegistryTransportTest(unittest.TestCase):
    """mm_session_letter.registry_call:  transport selection."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "operator", "registry.sqlite3")
        self.clock = FakeClock()
        self.service = RG.RegistryService(self.db, KEYS, clock=self.clock)
        import mm_session_letter as M

        self.M = M

    def _env(self, **overrides: str) -> Any:
        env = {
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL": "",
            "MM_OPERATOR_REGISTRY_CMD": "",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL": "",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY_ENV": "",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY": self.db,
            "SIMPLE_TOOLS_STATE_DIR": os.path.join(self.tmp.name, "state"),
        }
        env.update(overrides)
        return mock.patch.dict(os.environ, env)

    def _serve(self) -> API.ListenerReadApi:
        server = API.ListenerReadApi(
            "127.0.0.1", 0, self.tmp.name, {}, clock=self.clock, registry=self.service
        )
        server.start()
        self.addCleanup(server.close)
        return server

    def test_url_unset_falls_back_to_the_local_file(self) -> None:
        OR.register_session(
            self.db,
            platform="minimax",
            verified_host="mac",
            profile_scope="default",
            native_session_id=SID_CLAUDE,
            seat="MM",
            source="transport test",
            issuer=OR.ISSUER_OPERATOR,
        )
        with self._env():
            answer = self.M.registry_call(
                ["resolve", "--platform=minimax", "--letter=AA"]
            )
        self.assertTrue(answer.get("found"), answer)
        self.assertEqual(answer.get("seat"), "MM")

    def test_command_transport_wins_over_a_missing_local_file(self) -> None:
        # This host keeps no registry (its SIMPLE_TOOLS_OPERATOR_REGISTRY names a file
        # that does not exist), so the call goes over the transport command (the hop
        # main's remote_registry_call makes, a plain command name the host
        # configuration supplies) to the host that holds the one registry.  The far
        # side's store already exists there:  the registry CLI never brings a store
        # into being, so the shim carries the far side's own path, the way the real
        # transport command carries its host's configuration.
        OR.register_session(
            self.db,
            platform="minimax",
            verified_host="mac",
            profile_scope="default",
            native_session_id=SID_SEED,
            seat="MM",
            source="transport test seed",
            issuer=OR.ISSUER_OPERATOR,
        )
        self.assertTrue(os.path.isfile(self.db))
        scripts = os.path.dirname(os.path.abspath(self.M.__file__))
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir)
        shim = os.path.join(bin_dir, "mm-operator-registry")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\nSIMPLE_TOOLS_OPERATOR_REGISTRY=%s PYTHONPATH=%s exec %s "
                "-m simple_tools.operator_registry \"$@\"\n"
                % (self.db, scripts, sys.executable)
            )
        os.chmod(shim, 0o755)
        with self._env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY=os.path.join(self.tmp.name, "missing.sqlite3"),
            MM_OPERATOR_REGISTRY_CMD="mm-operator-registry",
            PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""),
        ):
            answer = self.M.registry_call(
                [
                    "register",
                    "--platform=minimax",
                    "--location=mac",
                    "--scope=default",
                    "--session-id=" + SID_CLAUDE,
                    "--seat=MM",
                    "--source=transport test",
                ]
            )
        self.assertIn("label", answer, answer)
        self.assertTrue(os.path.isfile(self.db))

    def test_url_set_uses_https_transport(self) -> None:
        # The far side's store exists (a local act on that host, as always).
        registry = OR.SessionRegistry(self.db)
        registry.close_db()
        server = self._serve()
        host, port = server.address
        url = "http://%s:%d" % (host, port)  # loopback stub:  http is allowed there only
        with self._env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL=url,
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=OPERATOR_KEY,
            MM_OPERATOR_REGISTRY_CMD="false",  # the URL transport wins over the command
        ):
            answer = self.M.registry_call(
                [
                    "register",
                    "--platform=minimax",
                    "--location=mac",
                    "--scope=default",
                    "--session-id=" + SID_CLAUDE,
                    "--seat=MM",
                    "--source=transport test",
                    "--letter-space=double",
                    "--letter-pick=primary",
                ]
            )
        self.assertEqual(answer.get("label"), "AA", answer)
        # The same call as the owning seat reads its own session back.
        with self._env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL=url,
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="MM",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=MM_KEY,
        ):
            answer = self.M.registry_call(
                ["resolve", "--platform=minimax", "--letter=AA"]
            )
        self.assertTrue(answer.get("found"), answer)
        self.assertEqual(answer.get("seat"), "MM")

    def test_url_set_without_a_key_is_not_configured(self) -> None:
        with self._env(SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="https://operator.example.test"):
            answer = self.M.registry_call(["resolve", "--platform=minimax", "--letter=A"])
        self.assertEqual(answer.get("code"), "not_configured", answer)


if __name__ == "__main__":
    unittest.main()
