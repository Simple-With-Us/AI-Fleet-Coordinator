"""The Operator's session registry API (listener_registry.py, Path B):  every one of the
registry's 12 ops through the HTTP adapter against a temporary registry, the HMAC
discipline (bad signature 401, wrong method 405, unknown route 404), the Operator-only
register path, cross-seat refusal, the one-writer pieces (operator_register, report-gone, the
in-process reopen and the one Sweeper, AFC-721).

RegistryService.handle is driven directly with a temporary registry file and a fake
clock, so no socket is involved except in the two tests that run the service behind a
real ListenerReadApi on an ephemeral port.  Every key is generated per run, so the
source holds no key-looking literal.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_listener_registry
"""

from __future__ import annotations

import ast
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
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
    # A platform no seat owns (fleet-apps.json `seatApps` names none for it), so these tests may
    # file any seat under it:  the registry refuses a known platform under another seat
    # (AFC-772, the seat tests of RegistryApiTest below).
    return {
        "platform": "testbed",
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
        # The one stale sweeper (AFC-721), which the `sweep` op runs.
        RG.Sweeper(self.service, interval_minutes=15, stale_after_hours=36)

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
            "CLAUDE", "GET", "resolve", {"platform": "testbed", "letter": "AA"}
        )
        self.assertEqual(status, 200, found)
        self.assertTrue(found["found"])
        self.assertEqual(found["record_id"], record_id)
        status, miss = self.call(
            "CLAUDE", "GET", "resolve", {"platform": "testbed", "letter": "ZZ"}
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
        status, history = self.call("CLAUDE", "GET", "history", {"platform": "testbed"})
        self.assertEqual((status, history["count"]), (200, 1))
        status, history = self.call("OPERATOR", "GET", "history", {"platform": "testbed"})
        self.assertEqual((status, history["count"]), (200, 2))

        # sweep:  nothing is stale yet, so nothing ends.
        status, swept = self.call(
            "CLAUDE", "POST", "sweep", {"platform": "testbed", "seat": "CLAUDE"}
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

        # relabel (Operator only):  forced with no parent, a pair holder keeps its pair (no pair of the lap is burned).
        status, relabelled = self.call(
            "OPERATOR",
            "POST",
            "relabel",
            {**identity(SID_CLAUDE), "seat": "CLAUDE", "source": "test", "force": True},
        )
        self.assertEqual(status, 200, relabelled)
        self.assertEqual(relabelled["outcome"], "unchanged")
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
            "platform": "TestBed",
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
            ("GET", "resolve", {"platform": "testbed", "letter": "AA"}),
            ("POST", "update", {**identity(SID_CLAUDE), "title": "Stolen"}),
            ("POST", "touch", identity(SID_CLAUDE)),
            ("POST", "close", identity(SID_CLAUDE)),
            ("POST", "reopen", {"record_id": record_id}),
            ("GET", "held", {"record_id": record_id}),
            ("POST", "sweep", {"platform": "testbed", "seat": "CLAUDE"}),
        ]
        for method, op, payload in cases:
            status, answer = self.call("CODEX", method, op, payload)
            self.assertEqual(status, 403, (op, answer))
        # The Operator may act for any seat, and the outbox is the Operator's alone.
        status, _found = self.call(
            "OPERATOR", "GET", "resolve", {"platform": "testbed", "letter": "AA"}
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
        query = {"platform": "testbed", "letter": "AA"}
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
            "platform=testbed&letter=AA", "GHOST", b"", self.clock.now,
        )
        status, answer = self.service.handle(
            "GET", RG.PREFIX + "resolve?platform=testbed&letter=AA", ghost, b""
        )
        self.assertEqual(status, 404, answer)
        short = RG.RegistryService(self.db, {"SHORTY": "x" * 10}, clock=self.clock)
        status, answer = short.handle(
            "GET",
            RG.PREFIX + "resolve?platform=testbed&letter=AA",
            RG.headers("x" * 10, "GET", RG.PREFIX + "resolve",
                       "platform=testbed&letter=AA", "SHORTY", b"", self.clock.now),
            b"",
        )
        self.assertEqual(status, 404, answer)

    def test_method_and_route_discipline(self) -> None:
        # A known op with the wrong method is 405, signed or not.
        status, answer = self.call("OPERATOR", "POST", "resolve", {"platform": "testbed"})
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

    # ---- a platform's chat is filed under one of its own seats (AFC-772) -----------

    def ask(
        self, op: str, platform: str, seat: str, sid: str = SID_SEED, **extra: Any
    ) -> tuple[int, dict[str, Any]]:
        return self.call(
            "OPERATOR",
            "POST",
            op,
            {
                "platform": platform,
                "location": "mac",
                "scope": "default",
                "session_id": sid,
                "seat": seat,
                "source": "seat guard test",
                **extra,
            },
        )

    def legacy(self, platform: str, seat: str, sid: str = SID_SEED) -> dict[str, Any]:
        """A row as one was filed before the check existed:  the pair went in unquestioned."""
        with mock.patch.object(OR, "check_seat_platform", lambda *_: None):
            status, answer = self.ask("register", platform, seat, sid)
        self.assertEqual(status, 200, answer)
        return answer

    def rows(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return sum(
                conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                for table in ("sessions", "alias_assignments", "events", "export_outbox")
            )
        finally:
            conn.close()

    def test_a_claude_chat_under_zc_is_400_with_its_own_code_and_no_row(self) -> None:
        self.create_store()
        lines: list[tuple[str, dict[str, Any]]] = []
        self.service.log = lambda event, **fields: lines.append((event, fields))
        status, answer = self.ask("register", "claude", "ZC")
        self.assertEqual(status, 400, answer)
        self.assertEqual(
            answer,
            {
                "error": "platform claude registers under seat CLAUDE, not ZC",
                "code": "seat_platform_mismatch",
                "platform": "claude",
                "seat": "ZC",
                "expected": ["CLAUDE"],
            },
        )
        self.assertEqual(self.rows(), 0)
        status, history = self.call("OPERATOR", "GET", "history", {"platform": "claude"})
        self.assertEqual((status, history["count"]), (200, 0))
        self.assertEqual(
            [fields for event, fields in lines if event == "registry-api-refused"],
            [
                {
                    "principal": "OPERATOR",
                    "op": "register",
                    "reason": "seat_platform_mismatch",
                    "platform": "claude",
                    "seat": "ZC",
                }
            ],
        )

    def test_each_platforms_own_seat_registers(self) -> None:
        self.create_store()
        for platform, seat in (("claude", "CLAUDE"), ("zcode", "ZC"), ("minimax", "MM")):
            status, answer = self.ask("register", platform, seat)
            self.assertEqual((status, answer.get("label")), (200, "AA"), answer)

    def test_a_platform_the_table_does_not_know_registers_under_any_seat(self) -> None:
        self.create_store()
        for index, seat in enumerate(("ZC", "CLAUDE", "SOMETHING-NEW")):
            status, answer = self.ask("register", "newplatform", seat, "new-%d" % index)
            self.assertEqual((status, answer.get("outcome")), (200, "registered"), answer)

    def test_a_launchers_bot_is_checked_against_its_pattern(self) -> None:
        self.create_store()
        status, answer = self.ask("register", "botfleet", "BF-FIXER", "bf-0001")
        self.assertEqual((status, answer.get("label")), (200, "AA"), answer)
        status, answer = self.ask("register", "botfleet", "bf-oracle", "bf-0002")
        self.assertEqual((status, answer.get("label")), (200, "BB"), answer)
        status, answer = self.ask("register", "botfleet", "CLAUDE", "bf-0003")
        self.assertEqual((status, answer.get("code")), (400, "seat_platform_mismatch"), answer)
        self.assertEqual(answer["expected"], ["BF-*"])
        status, answer = self.ask("register", "grok-bot", "GB-COMPILER", "gb-0001")
        self.assertEqual(status, 200, answer)

    def test_case_aliases_and_adapter_names_do_not_get_around_it(self) -> None:
        self.create_store()
        for platform, seat in (("Claude", "zc"), ("cursor-cloud", " ZC "), ("grok-build", "MM")):
            status, answer = self.ask("register", platform, seat)
            self.assertEqual((status, answer.get("code")), (400, "seat_platform_mismatch"), answer)
        self.assertEqual(self.rows(), 0)
        for index, (platform, seat, namespace, tag) in enumerate(
            (
                ("Claude", "claude", "claude", "CLAUDE"),
                ("claude", " CLAUDE ", "claude", "CLAUDE"),
                ("cursor-cloud", "Cursor", "cursor", "CURSOR"),
                ("grok-build", "Grok Build", "grok-leader", "GROK"),
            )
        ):
            sid = "right-%d" % index
            status, answer = self.ask("register", platform, seat, sid)
            self.assertEqual((status, answer.get("outcome")), (200, "registered"), answer)
            # The row is filed under the tag, not as typed, so the same chat resumes under the tag and the wake
            # router reads the row's seat.
            status, found = self.call(
                "OPERATOR", "GET", "resolve", {"platform": namespace, "letter": answer["label"]}
            )
            self.assertEqual((status, found.get("seat")), (200, tag), found)
            status, again = self.ask("register", namespace, tag, sid)
            self.assertEqual(
                (status, again.get("outcome"), again.get("record_id")), (200, "resumed", answer["record_id"]), again
            )

    def test_a_relabel_names_the_seat_in_any_spelling_the_check_accepts(self) -> None:
        self.create_store()
        status, made = self.ask("register", "claude", "CLAUDE")
        self.assertEqual(status, 200, made)
        status, answer = self.ask("relabel", "claude", "claude", force=True)
        self.assertEqual(
            (status, answer.get("record_id"), answer.get("outcome")), (200, made["record_id"], "unchanged"), answer
        )

    def test_a_relabel_into_a_mismatch_is_400_and_moves_no_letter(self) -> None:
        self.create_store()
        made = self.legacy("claude", "ZC")
        before = self.rows()
        status, answer = self.ask("relabel", "claude", "ZC", force=True)
        self.assertEqual((status, answer.get("code")), (400, "seat_platform_mismatch"), answer)
        self.assertEqual(self.rows(), before)
        status, found = self.call(
            "OPERATOR", "GET", "resolve", {"platform": "claude", "letter": made["label"]}
        )
        self.assertEqual((status, found["seat"]), (200, "ZC"))

    def test_a_row_filed_under_a_wrong_seat_earlier_can_still_be_touched_and_closed(self) -> None:
        self.create_store()
        made = self.legacy("claude", "ZC")
        where = {"platform": "claude", "location": "mac", "scope": "default", "session_id": SID_SEED}
        # While the row is live, a resume under the seat it is stored under still answers its letter:  a refused
        # resume left the chat's hook unable to end the row.  A new chat under that seat is refused as before.
        status, answer = self.ask("register", "claude", "ZC")
        self.assertEqual(
            (status, answer.get("outcome"), answer.get("label")), (200, "resumed", made["label"]), answer
        )
        status, answer = self.ask("register", "claude", "ZC", "session-new-0001")
        self.assertEqual((status, answer.get("code")), (400, "seat_platform_mismatch"), answer)
        status, answer = self.call("OPERATOR", "POST", "touch", where)
        self.assertEqual((status, answer.get("outcome")), (200, "seen"), answer)
        status, answer = self.call("OPERATOR", "POST", "update", {**where, "title": "Still Here"})
        self.assertEqual((status, answer.get("outcome")), (200, "updated"), answer)
        status, answer = self.call("OPERATOR", "POST", "close", {**where, "reason": "session ended"})
        self.assertEqual((status, answer.get("closed"), answer.get("label")), (200, True, made["label"]))
        # Closed, it is not brought back under the wrong seat, and nothing about the row changes.
        before = self.rows()
        status, answer = self.ask("register", "claude", "ZC")
        self.assertEqual((status, answer.get("code")), (400, "seat_platform_mismatch"), answer)
        self.assertEqual(self.rows(), before)
        # ... and a host may still report such a chat gone.
        self.legacy("claude", "ZC", "session-gone-0001")
        status, answer = self.call(
            "OPERATOR",
            "POST",
            "report-gone",
            {"platform": "claude", "verified_host": "mac", "native_session_ids": ["session-gone-0001"]},
        )
        self.assertEqual((status, answer.get("count")), (200, 1), answer)

    def test_a_register_under_the_platforms_own_seat_heals_a_row_filed_under_another(self) -> None:
        # The three Mac chats of AFC-772:  platform claude, seat ZC, open.  Their hook, once its seat is
        # right, registers as CLAUDE and gets the letter it already holds, now filed under CLAUDE.
        self.create_store()
        made = self.legacy("claude", "ZC")
        status, answer = self.ask("register", "claude", "CLAUDE")
        self.assertEqual(
            (status, answer.get("outcome"), answer.get("label"), answer.get("generation")),
            (200, "resumed", made["label"], made["generation"]),
            answer,
        )
        self.assertEqual(answer.get("record_id"), made["record_id"])
        status, found = self.call("OPERATOR", "GET", "resolve", {"platform": "claude", "letter": made["label"]})
        self.assertEqual((status, found.get("seat")), (200, "CLAUDE"), found)
        # The principal CLAUDE can now act on the row (a seat acts only on its own sessions).
        where = {"platform": "claude", "location": "mac", "scope": "default", "session_id": SID_SEED}
        status, answer = self.call("CLAUDE", "POST", "touch", where)
        self.assertEqual((status, answer.get("outcome")), (200, "seen"), answer)
        # Never the other way:  the wrong seat against the healed row is 400 and the row stays.
        before = self.rows()
        status, answer = self.ask("register", "claude", "ZC")
        self.assertEqual((status, answer.get("code")), (400, "seat_platform_mismatch"), answer)
        self.assertEqual(self.rows(), before)
        status, found = self.call("OPERATOR", "GET", "resolve", {"platform": "claude", "letter": made["label"]})
        self.assertEqual(found.get("seat"), "CLAUDE", found)

    def test_an_unreadable_seat_table_refuses_nothing_and_is_logged_once(self) -> None:
        self.create_store()
        lines: list[tuple[str, dict[str, Any]]] = []
        self.service.log = lambda event, **fields: lines.append((event, fields))
        missing = os.path.join(self.tmp.name, "no-such", "fleet-apps.json")
        with mock.patch.dict(os.environ, {"FLEET_APPS_JSON": missing}):
            for index in range(2):
                status, answer = self.ask("register", "claude", "ZC", "blind-%d" % index)
                self.assertEqual((status, answer.get("outcome")), (200, "registered"), answer)
        table = [fields for event, fields in lines if event == "registry-seat-table"]
        self.assertEqual(len(table), 1, lines)
        self.assertFalse(table[0]["ok"])
        self.assertIn("cannot read", table[0]["error"])
        # Readable again:  the check is back, and the log says so once.
        status, answer = self.ask("register", "claude", "ZC", "blind-2")
        self.assertEqual((status, answer.get("code")), (400, "seat_platform_mismatch"), answer)
        table = [fields for event, fields in lines if event == "registry-seat-table"]
        self.assertEqual([fields["ok"] for fields in table], [False, True])

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
            server, "CLAUDE", "GET", "resolve", {"platform": "testbed", "letter": "AA"}
        )
        self.assertEqual((status, answer["found"]), (200, True))
        # A request naming no principal is 404 (the notes queue's discipline:  an unknown
        # principal is not found, a known one with a bad signature is 401), and another
        # method entirely is 405.
        host, port = server.address
        request = urllib.request.Request(
            "http://%s:%d%sresolve?platform=testbed&letter=AA" % (host, port, RG.PREFIX),
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


class OneWriterTest(RegistryApiTest):
    """AFC-721:  the Eavesdropper is the only registry writer.  Its register path refuses a
    missing store, report-gone closes only what a host names (within the share guard), the
    reopen no longer imports a client module, and the one Sweeper ends stale replaced letters
    for every platform."""

    def setUp(self) -> None:
        super().setUp()
        # The share tests below report rows the moment they are registered.  A row seen that lately is one the
        # handler keeps (its chat is in use), so that rule is off here;  the tests of the rule turn it on.
        self.service.gone_report_live_minutes = 0

    def seen(self, sid: str, minutes_ago: float) -> None:
        """Set when a row was last seen (its chat's hook last touched it)."""
        when = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="milliseconds")
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE native_session_id = ?",
                    (when.replace("+00:00", "Z"), sid),
                )
        finally:
            conn.close()

    def gone(self, sids: list[str], **extra: Any) -> tuple[int, dict[str, Any]]:
        return self.call(
            "OPERATOR",
            "POST",
            "report-gone",
            {"platform": "testbed", "verified_host": "mac", "native_session_ids": sids, **extra},
        )

    def age(self, sid: str, hours: float) -> None:
        stamp = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ?, registered_at = ?"
                    " WHERE native_session_id = ?",
                    (stamp, stamp, sid),
                )
        finally:
            conn.close()

    def open_sids(self) -> set[str]:
        conn = sqlite3.connect(self.db)
        try:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT native_session_id FROM sessions WHERE closed_at IS NULL"
                )
            }
        finally:
            conn.close()

    # ---- operator_register ----------------------------------------------------

    def test_operator_register_never_creates_a_store(self) -> None:
        with self.assertRaises(OR.RegistryUnavailable):
            self.service.operator_register(
                platform="testbed",
                verified_host="mac",
                profile_scope="default",
                native_session_id=SID_CLAUDE,
                seat="CLAUDE",
                source="test",
            )
        self.assertFalse(os.path.exists(self.db))

    def test_operator_register_always_issues_as_the_operator(self) -> None:
        self.create_store()
        out = self.service.operator_register(
            platform="testbed",
            verified_host="mac",
            profile_scope="default",
            native_session_id=SID_CLAUDE,
            seat="CLAUDE",
            source="test",
            issuer="CLAUDE",
        )
        self.assertEqual(out["outcome"], "registered", out)

    # ---- reopen ---------------------------------------------------------------

    def test_reopen_does_not_import_the_client_module(self) -> None:
        source = Path(RG.__file__).read_text(encoding="utf-8")
        self.assertNotIn("platform_letter", source)
        self.create_store()
        record_id = self.register("CLAUDE", SID_CLAUDE)["record_id"]
        self.call("CLAUDE", "POST", "close", {**identity(SID_CLAUDE), "reason": "test"})
        with mock.patch.dict(sys.modules, {"simple_tools.platform_letter": None}):
            status, reopened = self.call("CLAUDE", "POST", "reopen", {"record_id": record_id})
        self.assertEqual(status, 200, reopened)
        self.assertEqual(len(reopened["label"]), 2)

    # ---- report-gone ------------------------------------------------------------

    def test_report_gone_is_the_operators_alone(self) -> None:
        self.create_store()
        self.register("CLAUDE", SID_CLAUDE)
        status, answer = self.call(
            "CLAUDE",
            "POST",
            "report-gone",
            {"platform": "testbed", "verified_host": "mac", "native_session_ids": [SID_CLAUDE]},
        )
        self.assertEqual(status, 403, answer)
        self.assertIn(SID_CLAUDE, self.open_sids())

    def test_report_gone_closes_only_the_named_rows_of_that_host(self) -> None:
        self.create_store()
        for sid in ("gone-1", "keep-1", "keep-2", "keep-3"):
            self.register("CLAUDE", sid)
        status, answer = self.gone(["gone-1", "not-registered"])
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["count"], 1)
        self.assertEqual(answer["closed"][0]["native_session_id"], "gone-1")
        self.assertEqual(self.open_sids(), {"keep-1", "keep-2", "keep-3"})
        conn = sqlite3.connect(self.db)
        try:
            sources = [
                row[0] for row in conn.execute("SELECT source FROM events WHERE event = 'close'")
            ]
        finally:
            conn.close()
        self.assertIn(RG.SOURCE_HOST_REPORT, sources)
        # Another host's rows are never touched by this host's report.
        status, answer = self.call(
            "OPERATOR",
            "POST",
            "report-gone",
            {"platform": "testbed", "verified_host": "server", "native_session_ids": ["keep-1"]},
        )
        self.assertEqual((status, answer["count"]), (200, 0), answer)
        self.assertIn("keep-1", self.open_sids())

    def test_report_gone_refuses_more_than_the_share_and_writes_nothing(self) -> None:
        self.create_store()
        for sid in ("sess-1", "sess-2", "sess-3", "sess-4"):
            self.register("CLAUDE", sid)
        status, answer = self.gone(["sess-1", "sess-2", "sess-3"])
        self.assertEqual(status, 409, answer)
        self.assertEqual(answer["code"], "gone_share_exceeded")
        self.assertEqual(answer["allowed_now"], 2, answer)
        self.assertEqual(self.open_sids(), {"sess-1", "sess-2", "sess-3", "sess-4"})
        # Half is allowed at the default share.
        status, answer = self.gone(["sess-1", "sess-2"])
        self.assertEqual((status, answer["count"]), (200, 2), answer)

    def test_report_gone_keeps_a_row_its_chat_touched_lately(self) -> None:
        # A chat in use is not gone, whoever says so:  the reporting host is often not the host the chat runs on.
        del self.service.gone_report_live_minutes  # the service's own rule:  45 minutes
        self.assertEqual(RG.DEFAULT_GONE_REPORT_LIVE_MINUTES, 45)
        self.create_store()
        others = ["other-%d" % n for n in range(1, 5)]
        for sid in ("busy-1", "busy-2", "quiet-1", "quiet-2", *others):
            self.register("CLAUDE", sid)
        self.seen("busy-2", 44)  # inside the window
        self.seen("quiet-1", 46)  # just outside it
        self.seen("quiet-2", 600)
        status, answer = self.gone(["busy-1", "busy-2", "quiet-1", "quiet-2", "never-registered"])
        self.assertEqual(status, 200, answer)
        self.assertEqual(sorted(row["native_session_id"] for row in answer["closed"]), ["quiet-1", "quiet-2"])
        self.assertEqual((answer["kept"], answer["kept_count"]), (["busy-1", "busy-2"], 2))
        # The keys a caller from before this rule reads are as they were.
        self.assertEqual((answer["count"], answer["live"]), (2, 8))
        self.assertEqual(self.open_sids(), {"busy-1", "busy-2", *others})
        # Once its hook has been quiet for longer than the window, the same report closes it.
        self.seen("busy-1", 50)
        status, answer = self.gone(["busy-1", "busy-2"])
        self.assertEqual((status, answer["count"], answer["kept"]), (200, 1, ["busy-2"]), answer)

    def test_the_window_is_the_services_own_clock_and_kept_is_a_sorted_list_of_ids_once(self) -> None:
        self.create_store()
        for sid in ("busy-1", "busy-2", *("other-%d" % n for n in range(1, 9))):
            self.register("CLAUDE", sid)
        # The same chat id held by two rows (two profile scopes) is one id in the answer, and both rows stay open.
        for scope in ("one", "two"):
            OR.register_session(
                self.db, platform="minimax", verified_host="mac", profile_scope=scope, native_session_id="twice-1",
                seat="CLAUDE", source="test", issuer=OR.ISSUER_OPERATOR,
            )
        for sid in ("busy-2", "busy-1", "twice-1"):
            self.seen(sid, 1)
        del self.service.gone_report_live_minutes
        self.service.gone_report_live_minutes = 2  # a window of two minutes
        status, answer = self.gone(["busy-2", "twice-1", "busy-1"])
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["kept"], answer["kept_count"], answer["count"]), (["busy-1", "busy-2", "twice-1"], 3, 0))
        # With the service's own clock four minutes on (inside the signature skew), the same rows were last seen five
        # minutes ago:  outside the window, so the report closes them.  The wall clock would have kept them.
        self.service.clock = lambda: time.time() + 4 * 60
        status, answer = self.gone(["busy-2", "twice-1", "busy-1"])
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["kept"], answer["kept_count"]), ([], 0))
        self.assertEqual(sorted({row["native_session_id"] for row in answer["closed"]}), ["busy-1", "busy-2", "twice-1"])

    def test_report_gone_counts_no_kept_row_against_the_share(self) -> None:
        del self.service.gone_report_live_minutes
        self.create_store()
        for sid in ("sess-1", "sess-2", "sess-3", "sess-4"):
            self.register("CLAUDE", sid)
        self.seen("sess-1", 120)
        # Three of four live rows are named, which the share would refuse:  two are in use, so one is asked of it.
        status, answer = self.gone(["sess-1", "sess-2", "sess-3"])
        self.assertEqual((status, answer["count"], answer["kept"]), (200, 1, ["sess-2", "sess-3"]), answer)
        # A report that names only rows in use closes nothing and is not a refusal.
        status, answer = self.gone(["sess-2", "sess-3", "sess-4"])
        self.assertEqual((status, answer["closed"], answer["kept_count"]), (200, [], 3), answer)
        # A row with no time at all is not known to be in use:  it is closed as before.
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute("UPDATE sessions SET last_seen_at = '' WHERE native_session_id = 'sess-4'")
        finally:
            conn.close()
        status, answer = self.gone(["sess-4"])
        self.assertEqual((status, answer["count"], answer["kept"]), (200, 1, []), answer)

    def test_report_gone_may_close_a_hosts_last_chat(self) -> None:
        self.create_store()
        self.register("CLAUDE", "only-1")
        status, answer = self.gone(["only-1"])
        self.assertEqual((status, answer["count"]), (200, 1), answer)
        self.assertEqual(self.open_sids(), set())

    def test_report_gone_validates_its_body(self) -> None:
        self.create_store()
        for body in (
            {"platform": "testbed", "verified_host": "mac", "native_session_ids": "x"},
            {"platform": "testbed", "verified_host": "mac", "native_session_ids": [""]},
            {"platform": "testbed", "native_session_ids": ["x"]},
            {"platform": "testbed", "verified_host": "laptop", "native_session_ids": ["x"]},
            {
                "platform": "testbed",
                "verified_host": "mac",
                "native_session_ids": ["x"] * (RG.MAX_GONE_IDS + 1),
            },
        ):
            status, answer = self.call("OPERATOR", "POST", "report-gone", body)
            self.assertEqual(status, 400, (body, answer))

    def test_report_gone_never_creates_a_store(self) -> None:
        status, answer = self.gone(["x"])
        self.assertEqual(status, 503, answer)
        self.assertFalse(os.path.exists(self.db))

    # ---- the one sweeper ----------------------------------------------------------

    def sweeper(self, **overrides: Any) -> RG.Sweeper:
        settings: dict[str, Any] = {"interval_minutes": 15, "stale_after_hours": 36}
        settings.update(overrides)
        return RG.Sweeper(self.service, **settings)

    def test_the_sweeper_ends_stale_replaced_letters_on_every_platform(self) -> None:
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 40)
        self.register("CLAUDE", "new-1")
        self.register("CODEX", "quiet-1")
        self.age("quiet-1", 40)  # stale, but nobody replaced it:  it keeps its letter
        OR.register_session(
            self.db,
            platform="cursor",
            verified_host="mac",
            profile_scope="default",
            native_session_id="cursor-old",
            seat="CURSOR",
            source="test",
            issuer=OR.ISSUER_OPERATOR,
        )
        self.age("cursor-old", 40)
        OR.register_session(
            self.db,
            platform="cursor",
            verified_host="mac",
            profile_scope="default",
            native_session_id="cursor-new",
            seat="CURSOR",
            source="test",
            issuer=OR.ISSUER_OPERATOR,
        )
        sweeper = self.sweeper()
        result = sweeper.maybe_run(self.clock.now)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ended"], 2)
        self.assertEqual(set(result["platforms"]), {"testbed", "cursor"})
        self.assertEqual(self.open_sids(), {"new-1", "quiet-1", "cursor-new"})
        self.assertEqual(sweeper.last, result)
        # Not due again until the interval has passed.
        self.assertIsNone(sweeper.maybe_run(self.clock.now + 60))
        self.assertIsNotNone(sweeper.maybe_run(self.clock.now + 15 * 60))

    def test_the_sweeper_keeps_letters_inside_the_window(self) -> None:
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 30)
        self.register("CLAUDE", "new-1")
        result = self.sweeper().run(self.clock.now)
        self.assertEqual(result["ended"], 0)
        self.assertEqual(self.open_sids(), {"old-1", "new-1"})

    def test_the_sweeper_never_creates_a_store(self) -> None:
        result = self.sweeper().run(self.clock.now)
        self.assertFalse(result["ok"])
        self.assertFalse(os.path.exists(self.db))

    def register_at(self, seat: str, sid: str, host: str, platform: str = "testbed") -> None:
        OR.register_session(
            self.db,
            platform=platform,
            verified_host=host,
            profile_scope="default",
            native_session_id=sid,
            seat=seat,
            source="test",
            issuer=OR.ISSUER_OPERATOR,
        )

    def test_a_new_chat_on_one_host_never_ends_another_hosts_chats(self) -> None:
        # The review's case:  eight quiet Mac chats, then one new chat of the seat on the
        # server.  The replaced test is scoped to the host, so none of the Mac chats end.
        self.create_store()
        mac = ["mac-%d" % n for n in range(8)]
        for sid in mac:
            self.register_at("CLAUDE", sid, "mac")
        stamp = (datetime.now(UTC) - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                # One quiet moment for all eight, so none of them replaced another.
                conn.execute(
                    "UPDATE sessions SET last_seen_at = ?, registered_at = ?", (stamp, stamp)
                )
        finally:
            conn.close()
        self.register_at("CLAUDE", "server-new", "server")
        result = self.sweeper().run(self.clock.now)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ended"], 0, result)
        self.assertEqual(self.open_sids(), set(mac) | {"server-new"})

    def test_the_sweeper_refuses_a_pass_that_would_end_most_of_a_host(self) -> None:
        # After an outage or a weekend every chat of a host looks stale at once.  A pass
        # that would end more than half of a (platform, host)'s open rows ends none.
        self.create_store()
        old = ["old-%d" % n for n in range(8)]
        for sid in old:
            self.register_at("CLAUDE", sid, "mac")
            self.age(sid, 40)
        self.register_at("CLAUDE", "new-1", "mac")
        result = self.sweeper().run(self.clock.now)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"], "sweep_share_exceeded")
        self.assertEqual(result["ended"], 0)
        self.assertEqual(
            result["refused"], [{"platform": "testbed", "host": "mac", "would_end": 8, "open": 9}]
        )
        self.assertEqual(self.open_sids(), set(old) | {"new-1"})
        # A share set to allow it ends them.
        result = self.sweeper(max_share=1.0).run(self.clock.now)
        self.assertEqual((result["ok"], result["ended"]), (True, 8), result)

    def test_the_sweeper_records_a_sqlite_error_and_never_raises(self) -> None:
        os.makedirs(os.path.dirname(self.db), exist_ok=True)
        Path(self.db).write_bytes(b"this is not a registry" * 64)
        sweeper = self.sweeper()
        result = sweeper.run(self.clock.now)
        self.assertFalse(result["ok"], result)
        self.assertIn("Error", result["error"])
        self.assertEqual(sweeper.last, result)

    def test_the_sweep_op_runs_the_one_sweep_with_its_window(self) -> None:
        # A caller's stale_after_minutes is ignored:  one window (36 hours) for every caller.
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 3)
        self.register("CLAUDE", "new-1")
        self.register("CODEX", "other-1")
        status, answer = self.call(
            "CLAUDE",
            "POST",
            "sweep",
            {"platform": "testbed", "seat": "CLAUDE", "stale_after_minutes": 60},
        )
        self.assertEqual((status, answer["count"]), (200, 0), answer)
        self.age("old-1", 40)
        status, answer = self.call(
            "CLAUDE", "POST", "sweep", {"platform": "testbed", "seat": "CLAUDE"}
        )
        self.assertEqual((status, answer["count"]), (200, 1), answer)
        self.assertEqual(self.open_sids(), {"new-1", "other-1"})

    def test_the_sweep_op_matches_the_seat_in_any_case(self) -> None:
        # kody 4544698048:  the authority check folds the seat, so the filter does too.
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 40)
        self.register("CLAUDE", "new-1")
        status, answer = self.call(
            "CLAUDE", "POST", "sweep", {"platform": "testbed", "seat": " claude "}
        )
        self.assertEqual((status, answer["count"]), (200, 1), answer)
        self.assertEqual(self.open_sids(), {"new-1"})

    def test_a_refused_sweep_op_still_names_what_it_ended(self) -> None:
        # kody 4544698314:  one host refused, another swept in the same pass;  the 409 says
        # which rows the pass did end.
        self.create_store()
        old = ["old-%d" % n for n in range(3)]
        for sid in old:
            self.register_at("CLAUDE", sid, "mac")
            self.age(sid, 40)
        self.register_at("CLAUDE", "new-mac", "mac")
        self.register_at("CLAUDE", "srv-old", "server")
        self.age("srv-old", 40)
        self.register_at("CLAUDE", "srv-new", "server")
        status, answer = self.call(
            "CLAUDE", "POST", "sweep", {"platform": "testbed", "seat": "CLAUDE"}
        )
        self.assertEqual(status, 409, answer)
        self.assertEqual(answer["code"], "sweep_share_exceeded")
        self.assertEqual(answer["count"], 1, answer)
        self.assertEqual(answer["ended"][0]["native_session_id"], "srv-old")
        self.assertEqual(self.open_sids(), set(old) | {"new-mac", "srv-new"})

    def test_the_sweep_op_answers_503_when_the_sweep_is_off(self) -> None:
        self.create_store()
        self.service.sweeper = None
        status, answer = self.call(
            "CLAUDE", "POST", "sweep", {"platform": "testbed", "seat": "CLAUDE"}
        )
        self.assertEqual((status, answer.get("code")), (503, "sweep_off"), answer)

    def test_report_gone_share_holds_across_split_reports(self) -> None:
        # The review's bypass:  six one-id reports used to close all six rows.  The share is
        # measured over a window, so the fourth report is refused and three rows stay open.
        self.create_store()
        sids = ["sess-%d" % n for n in range(6)]
        for sid in sids:
            self.register("CLAUDE", sid)
        answers = [self.gone([sid]) for sid in sids]
        self.assertEqual([status for status, _ in answers], [200, 200, 200, 409, 409, 409])
        self.assertEqual(answers[3][1]["allowed_now"], 0, answers[3])
        self.assertEqual(answers[3][1]["recent"], 3, answers[3])
        self.assertEqual(self.open_sids(), set(sids[3:]))
        # Once the window has passed, the host may report again.
        stamp = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute("UPDATE events SET recorded_at = ? WHERE event = 'close'", (stamp,))
        finally:
            conn.close()
        status, answer = self.gone([sids[3]])
        self.assertEqual((status, answer["count"]), (200, 1), answer)

    def test_report_gone_folds_the_host(self) -> None:
        self.create_store()
        for sid in ("gone-1", "keep-1", "keep-2"):
            self.register("CLAUDE", sid)
        status, answer = self.call(
            "OPERATOR",
            "POST",
            "report-gone",
            {"platform": "testbed", "verified_host": " MAC ", "native_session_ids": ["gone-1"]},
        )
        self.assertEqual((status, answer["count"]), (200, 1), answer)


class OneWriterGuardTest(unittest.TestCase):
    """AFC-721 guard:  the files this change made single-writer stay that way.  The daemon and
    the Operator's registration CLI write the registry only through listener_registry:  an AST
    walk finds no call to a registry write function under any spelling (`OR.x(`, an imported
    `x(`, an alias), no import of one, and no SessionRegistry built without read_only=True, so a
    handle in either file can never write.  The fleet-wide guard (every client module) is
    test_one_assigner, added when the clients become API-only."""

    WRITE_FUNCTIONS = frozenset(
        {
            "register_session",
            "relabel_session",
            "update_session",
            "close_session",
            "touch_session",
            "sweep_stale",
            "mark_exported",
            "record_export_attempt",
            "record_export_attempts",
        }
    )

    def tree(self, name: str) -> ast.AST:
        path = Path(RG.__file__).resolve().parent / name
        return ast.parse(path.read_text(encoding="utf-8"), filename=name)

    def offences(self, tree: ast.AST) -> list[str]:
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("operator_registry"):
                for alias in node.names:
                    if alias.name in self.WRITE_FUNCTIONS or alias.name == "*":
                        found.append("imports %s (line %d)" % (alias.name, node.lineno))
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in self.WRITE_FUNCTIONS:
                found.append("calls %s (line %d)" % (name, node.lineno))
            if name == "SessionRegistry":
                read_only = [kw for kw in node.keywords if kw.arg == "read_only"]
                if not (
                    read_only
                    and isinstance(read_only[0].value, ast.Constant)
                    and read_only[0].value.value is True
                ):
                    found.append("a write-mode SessionRegistry (line %d)" % node.lineno)
        return found

    def test_the_daemon_writes_the_registry_only_through_listener_registry(self) -> None:
        self.assertEqual(self.offences(self.tree("daemon.py")), [])

    def test_the_registration_cli_never_writes_the_registry(self) -> None:
        self.assertEqual(self.offences(self.tree("operator_registration.py")), [])
        # The CLI issues no letter at all:  only the Eavesdropper takes an answer.
        source = ast.unparse(self.tree("operator_registration.py"))
        self.assertNotIn("operator_register", source)

    def test_the_guard_catches_every_spelling(self) -> None:
        snippet = ast.parse(
            "from .operator_registry import register_session as reg\n"
            "reg(db)\n"
            "register_session(db)\n"
            "OR.sweep_stale(db)\n"
            "x = OR.SessionRegistry(\n    db,\n)\n"
            "y = OR.SessionRegistry(db, read_only=False)\n"
            "z = OR.SessionRegistry(db, read_only=True)\n"
        )
        found = self.offences(snippet)
        self.assertEqual(len(found), 5, found)


if __name__ == "__main__":
    unittest.main()
