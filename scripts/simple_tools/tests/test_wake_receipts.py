"""AFC-824:  the listener reads the wake router's receipt for every wake the router accepted, and
records the terminal outcome as its own wake-ledger row.

A 2xx from the one wake router only says the router stored the wake.  These tests drive the real
`http` wake path (Daemon.run_routine, and once the whole Zulip route) against a fake router
(FakeRoutine:  POSTs from its script, receipt GETs from `health`), then the daemon's tick, which
starts the receipt pass on its own thread;  `pass_at` waits for that thread.  Every test fails on
the code before AFC-824, where a `done` row was the last word and no receipt was ever read."""

from __future__ import annotations

import errno
import json
import os
import secrets
import socket
import textwrap
import threading
import time
import types
import unittest
from typing import Any
from unittest import mock

from simple_tools import adapters as A
from simple_tools import config as C
from simple_tools import heldback as HELD
from simple_tools import listener_records as LR
from simple_tools import live as L
from simple_tools import wake_router as WR
from simple_tools import wakes as W
from simple_tools.tests.fake_routine import FakeRoutine
from simple_tools.tests.listener_harness import ListenerHarness

MENTION = "@**Claude** can you check the AFC cutover?"
SESSION = "aaaaaaaa-0000-4000-8000-000000000001"
OUTCOMES = ("delivered", "undelivered", "unknown")
DEADLINE = 26 * 3600.0


def router_seat(
    seat: str, bot: str, *, key_env: str = C.ROUTER_BEARER_ENV, auth: str = "bearer"
) -> str:
    """A seat whose wakes go to the one wake router by its config:  the router's bearer as a
    `bearer` routine key.  Nothing about it depends on the seat's name."""
    return (
        '[seat.%s]\nbot = "%s"\nwake = "http"\n'
        'routine = { url_env = "%s_ROUTINE_URL", key_env = "%s", auth = "%s" }\n'
        % (seat, bot, C.env_code(seat), key_env, auth)
    )


class ReceiptHarness(ListenerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.routine = FakeRoutine()
        self.routine.start()
        self.addCleanup(self.routine.stop)
        self.router_key = secrets.token_hex(24)
        self.extra_keys.append(("router", self.router_key))
        self.message_id = 7000
        # The pending read's join is cached across calls (listener_records._receipt_cache):  each
        # test starts and ends with it empty, and within a test only the ledger's own change may
        # refresh it.
        LR._clear_receipt_cache()
        self.addCleanup(LR._clear_receipt_cache)

    def start(self, seats: str, **env: str) -> Any:
        self.write_config(seats=seats)
        overrides = {C.ROUTER_BEARER_ENV: self.router_key}
        overrides.update(env)
        return self.daemon(env=self.env(**overrides))

    def url(self, seat: str = "CLAUDE") -> str:
        return self.routine.url + "/wake/%s" % seat

    def wake(self, daemon: Any, seat: str = "CLAUDE", answer: int = 202) -> str:
        """One wake of `seat` through Daemon.run_routine, the `http` wake path, which the routine
        answers `answer`.  Returns the wake id."""
        runner = daemon.seats[seat]
        self.message_id += 1
        now = self.clock.time()
        row = {
            "seq": self.message_id,
            "id": self.message_id,
            "kind": "message",
            "type": "stream",
            "channel": "simple-tools",
            "topic": "t",
            "sender": "Codex",
            "sender_id": 11,
            "is_bot": True,
            "content": MENTION,
            "classes": ["eligible", "direct"],
            "wake_worthy": True,
        }
        L.append_jsonl(runner.paths.seat_inbox, [row])
        wake_id = "w%d-%08x" % (int(now), self.message_id)
        pending = W.Pending(wake_id, seat, row, owner=False, now=now, due=now)
        pending.add(row, False)
        runner.ledger.append(pending.row("queued", now))
        self.routine.script.append(answer)
        daemon.run_routine(runner, pending, now)
        return wake_id

    def receipt(
        self, wake_id: str, seat: str = "CLAUDE", status: str = "delivered", **over: Any
    ) -> dict[str, Any]:
        """A final receipt as wake_router.Router._receipt writes it."""
        found = {
            "receipt_id": "0123456789abcdef",
            "wake_id": wake_id,
            "seat": seat,
            "at": self.clock.time(),
            "test": False,
            "status": status,
            "letter": "",
            "letter_live": False,
            "letter_other_seat": "",
            "picked": "recency",
            "picked_letter": "AA",
            "picked_generation": 3,
            "session_id": SESSION,
            "host": "S",
            "thread_link": "",
            "reason": "most-recent",
            "candidates": [SESSION],
            "link_note": "The router could not link this wake to any chat.",
            "delivered": status == "delivered",
            "route": "resume",
            "ack": "",
            "code": "",
            "note": "",
            "attempts": 1,
            "replayed": False,
            "channel": "simple-tools",
            "topic": "t",
            "zulip_link": "https://zulip.example/#narrow/near/1",
        }
        found.update(over)
        return found

    def answer(
        self, wake_id: str, body: dict[str, Any], seat: str = "CLAUDE", status: int = 200
    ) -> None:
        self.routine.health["/receipt/%s/%s" % (seat, wake_id)] = (status, body)

    def pending_answer(self, wake_id: str, seat: str = "CLAUDE") -> None:
        """What the router answers for a wake it holds and has not settled."""
        self.answer(
            wake_id, {"wake_id": wake_id, "receipt_id": "0123456789abcdef", "status": "accepted"}, seat
        )

    def receipt_gets(self, contains: str = "/receipt/") -> list[str]:
        return [r.path for r in self.routine.health_requests if contains in r.path]

    def pass_at(self, daemon: Any, seconds: float) -> None:
        """Move the clock, tick, and wait for the receipt pass the tick started (if any)."""
        self.clock.advance(seconds)
        daemon.tick()
        thread = getattr(daemon, "_receipt_thread", None)
        if thread is not None:
            thread.join(10)
            self.assertFalse(thread.is_alive(), "the receipt pass never ended")

    def outcomes(self, seat: str = "CLAUDE") -> list[dict[str, Any]]:
        return [r for r in self.ledger(seat) if r.get("state") in OUTCOMES]

    def outcome(self, seat: str = "CLAUDE") -> dict[str, Any]:
        """The seat's one outcome row."""
        rows = self.outcomes(seat)
        self.assertEqual(len(rows), 1, "expected one outcome row for %s, found %r" % (seat, rows))
        return rows[0]

    def done_row(self, wake_id: str, seat: str = "CLAUDE") -> dict[str, Any]:
        return [r for r in self.ledger(seat) if r["wake_id"] == wake_id and r["state"] == "done"][0]

    def log_text(self) -> str:
        return (self.state_dir / "logs" / "listener.log").read_text()


class ReceiptReadTests(ReceiptHarness):
    def test_a_router_wake_answered_202_is_read_and_a_worker_wake_is_not(self) -> None:
        # CLAUDE goes to the router by its config;  CODEX is an hmac-sha256 routine (the hosted
        # Worker's kind) whose URL even ends the same way, and is never read.
        daemon = self.start(
            router_seat("CLAUDE", "Claude")
            + router_seat("CODEX", "Codex", key_env="CODEX_ROUTINE_KEY", auth="hmac-sha256"),
            CLAUDE_ROUTINE_URL=self.url("CLAUDE"),
            CODEX_ROUTINE_URL=self.url("CODEX"),
            CODEX_ROUTINE_KEY="h" * 40,
        )
        claude = self.wake(daemon, "CLAUDE")
        codex = self.wake(daemon, "CODEX")
        self.assertEqual(self.done_row(claude)["http_status"], 202)
        self.assertIs(
            self.done_row(claude).get("receipt_awaited"),
            True,
            "the router's 2xx did not mark the wake as awaiting its receipt",
        )
        self.assertNotIn("receipt_awaited", self.done_row(codex, "CODEX"))
        self.answer(claude, self.receipt(claude))
        self.answer(codex, self.receipt(codex, "CODEX"), "CODEX")
        self.pass_at(daemon, 10)
        self.assertEqual(self.receipt_gets(), [], "the first read is 30 seconds after the 2xx")
        self.pass_at(daemon, 21)
        self.assertEqual(
            self.receipt_gets(),
            ["/receipt/CLAUDE/%s" % claude],
            "the router seat's receipt was never read, or the Worker seat's was",
        )
        (seen,) = [r for r in self.routine.health_requests if "/receipt/" in r.path]
        self.assertEqual(seen.header("Authorization"), "Bearer " + self.router_key)
        self.assertEqual([(r["wake_id"], r["state"]) for r in self.outcomes()], [(claude, "delivered")])
        self.assertEqual(self.outcomes("CODEX"), [])

    def test_a_delivered_receipt_writes_one_row_of_whitelisted_fields_only(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        # Flagged text in every field that must never be copied, and in one whitelisted field
        # (`ack`), where its shape check passes and the secret scan must drop it.  The reader's
        # scan is stubbed so that plain markers stand in for credentials;  secretscan's own tests
        # cover what the real scan matches.
        anthropic = "PLAIN-MARKER-ONE-NOT-A-CREDENTIAL"
        github = "PLAIN-MARKER-TWO-NOT-A-CREDENTIAL"
        zulip_like = "PLAIN-MARKER-THREE-NOT-A-CREDENTIAL"
        markers = (anthropic, github, zulip_like)
        flagged = types.SimpleNamespace(
            scan=lambda value, known=(): any(marker in value for marker in markers)
        )
        body = self.receipt(
            wake_id,
            topic="deploy " + anthropic,
            channel=github,
            zulip_link="https://zulip.example/#narrow/" + github,
            thread_link="https://chat.example/" + zulip_like,
            link_note="linked by " + anthropic,
            note=anthropic,
            candidates=[anthropic, zulip_like],
            ack=anthropic,
            letter="AA",
            picked="letter",
        )
        self.answer(wake_id, body)
        with mock.patch.object(W, "secretscan", flagged):
            self.pass_at(daemon, 31)
        row = self.outcome()
        self.assertEqual(
            sorted(row),
            sorted(
                [
                    "ts", "wake_id", "seat", "state", "receipt_status", "receipt_id",
                    "picked_letter", "picked_generation", "host", "route", "attempts",
                    "delivered", "reason", "session_id", "receipt_at",
                ]
            ),
        )
        self.assertEqual(
            (row["state"], row["receipt_status"], row["delivered"], row["picked_letter"]),
            ("delivered", "delivered", True, "AA"),
        )
        self.assertEqual(
            (row["session_id"], row["picked_generation"], row["reason"], row["route"]),
            (SESSION, 3, "most-recent", "resume"),
        )
        for name in ("topic", "zulip_link", "thread_link", "link_note", "note", "candidates",
                     "channel", "letter", "picked", "test", "ack", "receipt_code"):
            self.assertNotIn(name, row)
        written = (self.state_dir / "CLAUDE" / "wakes.jsonl").read_text() + self.log_text()
        for secret in (anthropic, github, zulip_like):
            self.assertNotIn(secret, written)
        # Settled:  read once, written once, and never read again.
        self.pass_at(daemon, 200)
        self.pass_at(daemon, 3600)
        self.assertEqual(len(self.outcomes()), 1)
        self.assertEqual(len(self.receipt_gets()), 1)
        self.assertEqual(self.log_events("wake-outcome"), [], "a delivered wake is not logged")

    def test_a_dead_receipt_is_undelivered_and_logged_once(self) -> None:
        # The whole route:  a Zulip mention, the coalescer, the wake worker, the POST, the receipt.
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        self.routine.script.append(202)
        mid = self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.clock.advance(25)
        daemon.tick()
        daemon.run_jobs("CLAUDE")
        self.assertTrue(self.routine.wait_for(1, timeout=10), "the router was never posted to")
        wake_id = self.routine.received[0].json()["wake_id"]
        self.assertEqual(self.routine.received[0].path, "/wake/CLAUDE")
        self.answer(
            wake_id,
            self.receipt(wake_id, status="dead", delivered=False, code="session-cwd-gone"),
        )
        self.pass_at(daemon, 31)
        row = self.outcome()
        self.assertEqual(
            (row["wake_id"], row["state"], row["receipt_status"], row["receipt_code"]),
            (wake_id, "undelivered", "dead", "session-cwd-gone"),
        )
        self.assertIs(row["delivered"], False)
        self.pass_at(daemon, 200)
        self.pass_at(daemon, 2000)
        logged = self.log_events("wake-outcome")
        self.assertEqual(len(logged), 1, "one log line per outcome that is not delivered")
        self.assertEqual(
            {k: logged[0].get(k) for k in ("seat", "wake_id", "outcome", "receipt_status", "receipt_code")},
            {"seat": "CLAUDE", "wake_id": wake_id, "outcome": "undelivered",
             "receipt_status": "dead", "receipt_code": "session-cwd-gone"},
        )
        self.assertEqual(len(self.receipt_gets()), 1)
        self.assertNotIn("/receipt/", self.log_text(), "the log never holds the receipt URL")
        self.assertEqual(self.inbox()[0]["id"], mid)

    def test_a_pending_receipt_is_read_on_the_backoff_and_the_deadline_records_unknown(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        self.pending_answer(wake_id)
        # Seconds after the 2xx -> reads so far:  30 s, then 2 minutes, then 10 minutes, then every 30.
        elapsed = 0.0
        for at, reads in ((10, 0), (31, 1), (100, 1), (151, 2), (700, 2), (751, 3), (2500, 3),
                          (2551, 4), (4000, 4), (4351, 5)):
            self.pass_at(daemon, at - elapsed)
            elapsed = at
            self.assertEqual(len(self.receipt_gets()), reads, "after %d seconds" % at)
        self.assertEqual(self.outcomes(), [], "an accepted receipt settles nothing")
        # The deadline:  one last read, still pending, so the outcome is unknown and never delivered.
        self.pass_at(daemon, DEADLINE - elapsed)
        self.assertEqual(len(self.receipt_gets()), 6)
        row = self.outcome()
        self.assertEqual((row["state"], row.get("receipt_last")), ("unknown", "accepted"))
        self.assertNotIn("receipt_status", row)
        self.assertNotIn("delivered", [r["state"] for r in self.ledger()])
        logged = self.log_events("wake-outcome")
        self.assertEqual([(e["wake_id"], e["outcome"]) for e in logged], [(wake_id, "unknown")])
        self.pass_at(daemon, 3600)
        self.assertEqual((len(self.receipt_gets()), len(self.outcomes())), (6, 1))

    def test_the_schedule_is_30s_2m_10m_then_every_30m_until_the_deadline(self) -> None:
        self.assertTrue(hasattr(W, "receipt_next_read"), "the ledger has no receipt read schedule")
        self.assertEqual(W.receipt_next_read(1000.0, None), 1030.0)
        self.assertEqual(W.receipt_next_read(1000.0, 1031.0), 1150.0)
        self.assertEqual(W.receipt_next_read(1000.0, 1151.0), 1750.0)
        self.assertEqual(W.receipt_next_read(1000.0, 1760.0), 3550.0)
        self.assertEqual(W.receipt_next_read(1000.0, 3560.0), 5350.0)
        self.assertEqual(W.receipt_next_read(1000.0, 1000.0 + DEADLINE - 10), 1000.0 + DEADLINE)
        # A read at the deadline that settled nothing (its row could not be written) waits a slot.
        self.assertEqual(
            W.receipt_next_read(1000.0, 1000.0 + DEADLINE + 50), 1000.0 + DEADLINE + 50 + 1800
        )

    def test_awaited_receipts_survive_a_restart_and_a_settled_one_is_never_read_again(self) -> None:
        env = {"CLAUDE_ROUTINE_URL": self.url()}
        first = self.start(router_seat("CLAUDE", "Claude"), **env)
        held, unknown = self.wake(first), self.wake(first)
        self.pending_answer(held)  # `unknown` answers 404 until its receipt exists
        self.pass_at(first, 31)
        self.assertEqual(len(self.receipt_gets()), 2)
        self.assertEqual(self.outcomes(), [])
        first.shutdown()
        # A restart:  nothing but the ledger says these two still wait on the router.
        second = self.daemon(env=self.env(**{C.ROUTER_BEARER_ENV: self.router_key}, **env))
        self.answer(held, self.receipt(held))
        self.answer(unknown, self.receipt(unknown, status="held", delivered=False))
        self.pass_at(second, 20)
        self.assertEqual(len(self.receipt_gets()), 4, "the restarted listener never read on")
        self.assertEqual(
            sorted((r["wake_id"], r["state"]) for r in self.outcomes()),
            sorted([(held, "delivered"), (unknown, "undelivered")]),
        )
        second.shutdown()
        third = self.daemon(env=self.env(**{C.ROUTER_BEARER_ENV: self.router_key}, **env))
        self.pass_at(third, 120)
        self.pass_at(third, 5000)
        self.assertEqual(len(self.receipt_gets()), 4, "a settled wake was read again after a restart")
        self.assertEqual(len(self.outcomes()), 2)


class ReceiptPassBoundsTests(ReceiptHarness):
    def test_a_pass_reads_at_most_its_cap_oldest_first_and_the_rest_wait_for_the_next(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        ids = []
        for _ in range(25):
            ids.append(self.wake(daemon))
            self.pending_answer(ids[-1])
            self.clock.advance(1)
        self.pass_at(daemon, 31)
        first = self.receipt_gets()
        self.assertEqual(len(first), 20, "one pass made more or fewer than its 20 reads")
        self.assertEqual(sorted(first), sorted("/receipt/CLAUDE/%s" % i for i in ids[:20]))
        self.pass_at(daemon, 15)
        self.assertEqual(
            sorted(self.receipt_gets()[20:]), sorted("/receipt/CLAUDE/%s" % i for i in ids[20:])
        )

    def test_a_pass_stops_starting_reads_once_its_time_budget_is_spent(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        ids = [self.wake(daemon) for _ in range(5)]
        reads: list[str] = []

        def slow(target: Any, url: str, **_kw: Any) -> Any:
            reads.append(url.rsplit("/", 1)[-1])
            self.clock.advance(8)  # each read takes 8 seconds of the pass's 20
            return types.SimpleNamespace(
                status=200, body={"wake_id": reads[-1], "status": "accepted"}, error=None
            )

        with mock.patch.object(daemon.routine_runner, "read_receipt", side_effect=slow, create=True):
            self.pass_at(daemon, 31)
            self.assertEqual(len(reads), 3, "reads started after the budget was spent")
            self.pass_at(daemon, 15)
        self.assertEqual(sorted(reads[3:]), sorted(ids[3:]), "the rest waited for the next pass")
        self.assertEqual(len(reads), 5)

    def test_failed_reads_break_neither_the_pass_nor_another_seat_and_are_read_again(self) -> None:
        daemon = self.start(
            router_seat("CLAUDE", "Claude") + router_seat("CODEX", "Codex"),
            CLAUDE_ROUTINE_URL=self.url("CLAUDE"),
            CODEX_ROUTINE_URL=self.url("CODEX"),
        )
        refused, unknown, broken, slow = (self.wake(daemon) for _ in range(4))
        good = self.wake(daemon, "CODEX")
        # A 401 (the router's bearer check), a 404 (no such wake there), a read that raises and one
        # that times out:  all four are read again.  A 200 with a terminal status is final.
        self.answer(refused, {"error": "bad bearer"}, status=401)
        self.answer(good, self.receipt(good, "CODEX"), "CODEX")
        real = getattr(daemon.routine_runner, "read_receipt", None)
        failing = {broken: "raise", slow: "timeout"}  # each fails its first read only

        def flaky(target: Any, url: str, **kw: Any) -> Any:
            how = failing.pop(url.rsplit("/", 1)[-1], None)
            if how == "raise":
                raise RuntimeError("the read blew up")
            if how == "timeout":
                return types.SimpleNamespace(status=None, body=None, error="timeout")
            return real(target, url, **kw)

        with mock.patch.object(daemon.routine_runner, "read_receipt", side_effect=flaky, create=True):
            self.pass_at(daemon, 31)
            self.assertEqual([(r["wake_id"], r["state"]) for r in self.outcomes("CODEX")],
                             [(good, "delivered")], "the other seat's receipt was not recorded")
            self.assertEqual(self.outcomes(), [])
            self.assertEqual(self.log_events("wake-receipt-error"), [], "the pass broke")
            for wake_id in (refused, unknown, broken, slow):
                self.answer(wake_id, self.receipt(wake_id))
            # Every failed read's next slot is two minutes later:  all four are read again then.
            self.pass_at(daemon, 120)
        self.assertEqual(
            sorted(r["wake_id"] for r in self.outcomes() if r["state"] == "delivered"),
            sorted([refused, unknown, broken, slow]),
        )
        self.assertEqual(len(self.receipt_gets("/receipt/CODEX/")), 1, "a final receipt was read again")


class ReceiptUrlTests(ReceiptHarness):
    def test_the_receipt_url_replaces_the_final_wake_path_and_any_other_shape_is_refused_once(self) -> None:
        token = "tok" + secrets.token_hex(8)
        daemon = self.start(
            router_seat("CLAUDE", "Claude") + router_seat("CODEX", "Codex"),
            CLAUDE_ROUTINE_URL=self.routine.url + "/router/wake/CLAUDE",
            CODEX_ROUTINE_URL=self.routine.url + "/hook?token=" + token,
        )
        claude, codex = self.wake(daemon, "CLAUDE"), self.wake(daemon, "CODEX")
        self.routine.health["/router/receipt/CLAUDE/%s" % claude] = (200, self.receipt(claude))
        self.pass_at(daemon, 31)
        self.assertEqual(self.receipt_gets(), ["/router/receipt/CLAUDE/%s" % claude])
        self.assertEqual([r["state"] for r in self.outcomes()], ["delivered"])
        refused = self.log_events("wake-receipt-url-refused")
        self.assertEqual([(e["seat"], e["url_env"]) for e in refused], [("CODEX", "CODEX_ROUTINE_URL")])
        self.pass_at(daemon, 200)
        self.assertEqual(len(self.log_events("wake-receipt-url-refused")), 1, "logged more than once")
        self.assertNotIn(token, self.log_text())
        self.pass_at(daemon, DEADLINE)
        row = self.outcome("CODEX")
        self.assertEqual((row["wake_id"], row["state"], row["receipt_last"]),
                         (codex, "unknown", "receipt_url_refused"))
        self.assertEqual(self.receipt_gets("/receipt/CODEX/"), [])
        # The derivation itself.
        self.assertEqual(A.receipt_url("https://r.example/wake/CLAUDE", "CLAUDE", "w1-ab"),
                         "https://r.example/receipt/CLAUDE/w1-ab")
        self.assertEqual(A.receipt_url("http://127.0.0.1:8794/x/wake/GB-FIXER", "GB-FIXER", "w2"),
                         "http://127.0.0.1:8794/x/receipt/GB-FIXER/w2")
        for url, seat, wake_id in (
            ("https://r.example/wake/CLAUDE?k=v", "CLAUDE", "w1"),
            ("https://r.example/wake/CLAUDE#f", "CLAUDE", "w1"),
            ("https://r.example/wake/CODEX", "CLAUDE", "w1"),
            ("https://r.example/notwake/CLAUDE", "CLAUDE", "w1"),
            ("https://r.example/wake/CLAUDE/", "CLAUDE", "w1"),
            ("https://r.example/hook", "CLAUDE", "w1"),
            ("https://r.example/wake/CLAUDE", "CLAUDE", "../w1"),
            ("/wake/CLAUDE", "CLAUDE", "w1"),
        ):
            self.assertIsNone(A.receipt_url(url, seat, wake_id), url)


class ReceiptUrlTypeTests(unittest.TestCase):
    def test_a_seat_wake_id_or_url_that_is_not_text_is_refused_and_never_raises(self) -> None:
        url = "https://r.example/wake/CLAUDE"
        for value in (
            (url, 5, "w1"),
            (url, "CLAUDE", 7),
            (url, ["CLAUDE"], "w1"),
            (url, "CLAUDE", b"w1"),
            (None, "CLAUDE", "w1"),
            (url.encode(), "CLAUDE", "w1"),
        ):
            self.assertIsNone(A.receipt_url(*value), value)
        self.assertEqual(
            A.receipt_url(url, "CLAUDE", "w1"), "https://r.example/receipt/CLAUDE/w1"
        )


class ReceiptReaderTests(ReceiptHarness):
    def test_a_delivered_wake_leaves_operator_inbox_pending_and_a_dead_one_stays(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        delivered, dead = self.wake(daemon), self.wake(daemon)
        dead_message = self.message_id
        self.assertEqual(LR.pending_inbox_view(self.root, "CLAUDE")["count"], 2)
        self.answer(delivered, self.receipt(delivered))
        self.answer(dead, self.receipt(dead, status="dead", delivered=False, code="no-codex-desktop"))
        self.pass_at(daemon, 31)
        view = LR.pending_inbox_view(self.root, "CLAUDE")
        self.assertEqual(view["count"], 1, "a delivered wake's message still counted as pending")
        self.assertEqual([i["id"] for i in view["items"]], [dead_message])
        self.assertEqual(view["items"][0]["receipt"]["state"], "undelivered")
        self.assertEqual(LR.pending_inbox_view(self.root, "CLAUDE", limit=0)["count"], 1)

    def test_the_status_counts_the_router_outcomes_of_the_last_day(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        delivered, dead, unsure, waiting = (self.wake(daemon) for _ in range(4))
        self.answer(delivered, self.receipt(delivered))
        self.answer(dead, self.receipt(dead, status="dead", delivered=False, code="not-live"))
        self.answer(unsure, self.receipt(unsure, status="unknown", delivered=None))
        self.pending_answer(waiting)
        self.pass_at(daemon, 31)
        view = LR.status_view(self.root, "CLAUDE", now=self.clock.time())
        self.assertEqual(
            view.get("wake_outcomes_24h"),
            {"delivered": 1, "undelivered": 1, "unknown": 1, "awaiting": 1},
        )
        self.assertEqual(
            {k: view["wakes_by_state"].get(k) for k in ("delivered", "undelivered", "unknown", "done")},
            {"delivered": 1, "undelivered": 1, "unknown": 1, "done": 1},
        )
        items = {i["wake_id"]: i for i in LR.wakes_view(self.root, "CLAUDE", now=self.clock.time())["items"]}
        self.assertEqual(
            (items[dead]["state"], items[dead].get("receipt_status"), items[dead].get("receipt_code")),
            ("undelivered", "dead", "not-live"),
        )
        self.assertIs(items[waiting].get("receipt_awaited"), True)
        self.assertIn("delivered", items[unsure], "the router's null `delivered` was not kept")
        self.assertIsNone(items[unsure]["delivered"])
        self.assertNotIn("most-recent", json.dumps(items[waiting]), "an awaited wake shows a receipt")

    def test_a_wake_shows_only_the_receipt_fields_of_its_own_outcome_row(self) -> None:
        # The `done` row counts the POST's attempts;  a receipt that names none must not show that
        # count as the router's in the wakes view.
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        self.assertEqual(self.done_row(wake_id)["attempts"], 1)
        body = self.receipt(wake_id)
        del body["attempts"]
        self.answer(wake_id, body)
        self.pass_at(daemon, 31)
        self.assertNotIn("attempts", self.outcome())
        (item,) = LR.wakes_view(self.root, "CLAUDE", now=self.clock.time())["items"]
        self.assertEqual(
            (item["wake_id"], item["state"], item.get("receipt_status")),
            (wake_id, "delivered", "delivered"),
        )
        self.assertNotIn("attempts", item, "the done row's POST attempts showed as the router's")

    def test_a_wake_with_a_router_outcome_still_counts_against_the_budget(self) -> None:
        ledger = W.Ledger(os.path.join(str(self.tmp), "budget-wakes.jsonl"))
        now = self.clock.time()
        for state, at in (("queued", 60), ("accepted", 59), ("started", 58), ("done", 57), ("delivered", 20)):
            ledger.append({"ts": now - at, "wake_id": "w1", "seat": "CLAUDE", "state": state,
                           "topic_key": "k", "owner": False})
        self.assertEqual(
            W.budget_block(ledger.wakes(), {"wakes_per_day": 1}, owner=False, key="k", now=now),
            "wakes_per_day",
            "a wake the router delivered no longer counted against the seat's budget",
        )


class AnySeatTests(ReceiptHarness):
    def test_a_seat_with_any_name_routed_to_the_router_by_config_gets_receipts(self) -> None:
        partition = self.tmp / "partition-any-seat.toml"
        partition.write_text(textwrap.dedent("""\
            [seats]
            QV-9 = "mac"
            """))
        daemon = self.start(
            router_seat("QV-9", "Qv9"),
            QV_9_ROUTINE_URL=self.url("QV-9"),
            SIMPLE_TOOLS_PARTITION=str(partition),
        )
        self.assertIn("QV-9", daemon.seats, daemon.refusal or daemon.config.errors)
        wake_id = self.wake(daemon, "QV-9")
        self.answer(wake_id, self.receipt(wake_id, "QV-9", status="dead", code="resume-exit-1"), "QV-9")
        self.pass_at(daemon, 31)
        self.assertEqual(self.receipt_gets(), ["/receipt/QV-9/%s" % wake_id])
        row = self.outcome("QV-9")
        self.assertEqual((row["state"], row["receipt_code"]), ("undelivered", "resume-exit-1"))


def write_done(paths: L.SeatPaths, wake_id: str, at: float, **extra: Any) -> None:
    """A finished `http` wake as the ledger held it before the listener read receipts:  a `done`
    row with the router's 202 and no `receipt_awaited`, unless `extra` says otherwise."""
    ledger = W.Ledger(paths.wakes)
    for state in ("queued", "accepted", "started"):
        ledger.append(
            {"ts": at - 5, "wake_id": wake_id, "seat": paths.seat, "state": state,
             "trigger_ids": [], "adapter": "http"}
        )
    ledger.append(
        {"ts": at, "wake_id": wake_id, "seat": paths.seat, "state": "done", "adapter": "http",
         "http_status": 202, "trigger_ids": [], **extra}
    )


class ReceiptRecoveryTests(ReceiptHarness):
    def test_done_wakes_from_before_receipts_are_read_while_the_router_keeps_them(self) -> None:
        # A ledger written before the listener read receipts:  `done` rows with the router's 202
        # and no `receipt_awaited`.  A start on a seat the router serves by its config reads each
        # one the router can still answer (30 days);  a hosted Worker's answer, a seat that is not
        # the router's, and a wake older than the router keeps are left alone.
        self.write_config(
            seats=router_seat("CLAUDE", "Claude")
            + router_seat("CODEX", "Codex", key_env="CODEX_ROUTINE_KEY", auth="hmac-sha256")
        )
        now = self.clock.time()
        recent, old, lost = "w-recent-1", "w-old-2", "w-lost-3"
        write_done(self.seat_paths(), recent, now - 3600)
        write_done(self.seat_paths(), old, now - 5 * 86400)
        write_done(self.seat_paths(), lost, now - 5 * 86400)
        write_done(self.seat_paths(), "w-ancient-4", now - 31 * 86400)
        write_done(self.seat_paths(), "w-worker-5", now - 3600, http_status=200,
                   routine_delivered=1, routine_outcome="delivered")
        write_done(self.seat_paths("CODEX"), "w-codex-6", now - 3600)
        daemon = self.daemon(env=self.env(**{
            C.ROUTER_BEARER_ENV: self.router_key,
            "CLAUDE_ROUTINE_URL": self.url("CLAUDE"),
            "CODEX_ROUTINE_URL": self.url("CODEX"),
            "CODEX_ROUTINE_KEY": "h" * 40,
        }))
        self.answer(recent, self.receipt(recent))
        self.answer(old, self.receipt(old, status="dead", delivered=False, code="not-live"))
        # Those reads are guesses that the wake went to the router, so the status does not count
        # them as awaiting its receipt (wakes.outcome_counts).
        self.assertEqual(sorted(daemon.seats["CLAUDE"].receipt_waits), sorted([recent, old, lost]))
        self.assertEqual(
            LR.status_view(self.root, "CLAUDE", now=now)["wake_outcomes_24h"]["awaiting"], 0
        )
        self.pass_at(daemon, 15)
        self.assertEqual(
            sorted(self.receipt_gets()),
            sorted("/receipt/CLAUDE/%s" % w for w in (recent, old, lost)),
            "the router's receipts of the wakes from before the listener read them were not read",
        )
        # Nothing on those rows names the routine that answered them, so only the router's own
        # terminal receipt settles one:  `lost`, which the router has no receipt of, stays `done`.
        self.assertEqual(
            sorted((r["wake_id"], r["state"]) for r in self.outcomes()),
            sorted([(recent, "delivered"), (old, "undelivered")]),
        )
        self.assertEqual(W.Ledger(self.seat_paths().wakes).wakes()[lost]["state"], "done")
        self.assertEqual(
            [(e["wake_id"], e["receipt_last"]) for e in self.log_events("wake-receipt-unclaimed")],
            [(lost, "http_404")],
        )
        # Each was read once:  the old ones were past the deadline, so they had their last read.
        self.pass_at(daemon, 3600)
        self.assertEqual(len(self.receipt_gets()), 3)
        self.assertEqual(self.outcomes("CODEX"), [])

    def test_a_202_the_router_never_stored_is_never_given_an_outcome(self) -> None:
        # A `done` 202 from before the listener read receipts, with no hosted Worker answer on its
        # row:  the router's, or a Worker's (or another routine's) whose 202 body was not the
        # Worker's answer.  The row cannot tell them apart, so the router's 404 at every read,
        # the last one included, leaves the wake as it was instead of recording `unknown`.
        self.write_config(seats=router_seat("CLAUDE", "Claude"))
        write_done(self.seat_paths(), "w-worker-7", self.clock.time() - 3600)
        daemon = self.daemon(env=self.env(**{
            C.ROUTER_BEARER_ENV: self.router_key, "CLAUDE_ROUTINE_URL": self.url(),
        }))
        self.pass_at(daemon, 15)
        self.assertEqual(self.receipt_gets(), ["/receipt/CLAUDE/w-worker-7"])
        self.pass_at(daemon, DEADLINE)  # past its deadline:  the last read
        self.assertEqual(len(self.receipt_gets()), 2)
        self.assertEqual(self.outcomes(), [], "a wake the router never claimed got an outcome")
        view = W.Ledger(self.seat_paths().wakes).wakes()["w-worker-7"]
        self.assertEqual(view["state"], "done")
        self.assertEqual(
            [(e["wake_id"], e["receipt_last"]) for e in self.log_events("wake-receipt-unclaimed")],
            [("w-worker-7", "http_404")],
        )
        self.pass_at(daemon, 3600)
        self.assertEqual(len(self.receipt_gets()), 2, "a wake left as it was was read again")

    def test_a_seat_a_reload_adds_back_reads_on_from_its_ledger(self) -> None:
        both = router_seat("CLAUDE", "Claude") + router_seat("CODEX", "Codex")
        daemon = self.start(
            both, CLAUDE_ROUTINE_URL=self.url("CLAUDE"), CODEX_ROUTINE_URL=self.url("CODEX")
        )
        wake_id = self.wake(daemon, "CODEX")
        self.write_config(seats=router_seat("CLAUDE", "Claude"))
        daemon.reload()
        self.assertNotIn("CODEX", daemon.seats)
        self.write_config(seats=both)
        daemon.reload()
        self.assertIn("CODEX", daemon.seats)
        self.answer(wake_id, self.receipt(wake_id, "CODEX"), "CODEX")
        self.pass_at(daemon, 31)
        self.assertEqual(
            self.receipt_gets(),
            ["/receipt/CODEX/%s" % wake_id],
            "a seat a reload added back never read the receipts its ledger still waits on",
        )
        self.assertEqual(
            [(r["wake_id"], r["state"]) for r in self.outcomes("CODEX")], [(wake_id, "delivered")]
        )

    def test_a_reload_that_moves_the_seat_off_the_router_still_reads_what_it_sent(self) -> None:
        daemon = self.start(
            router_seat("CLAUDE", "Claude"),
            CLAUDE_ROUTINE_URL=self.url(),
            CLAUDE_ROUTINE_KEY="k" * 40,
        )
        wake_id = self.wake(daemon)
        self.write_config(
            seats=router_seat("CLAUDE", "Claude", key_env="CLAUDE_ROUTINE_KEY", auth="hmac-sha256")
        )
        daemon.reload()
        self.assertFalse(C.routes_to_router(daemon.seats["CLAUDE"].cfg.routine))
        self.answer(wake_id, self.receipt(wake_id))
        self.pass_at(daemon, 31)
        seen = [r for r in self.routine.health_requests if "/receipt/" in r.path]
        self.assertEqual(len(seen), 1, "the wake was not read with the routine it was sent with")
        self.assertEqual(seen[0].header("Authorization"), "Bearer " + self.router_key)
        self.assertEqual(
            [(r["wake_id"], r["state"]) for r in self.outcomes()], [(wake_id, "delivered")]
        )

    def off_router_restart(self, wake_id: str, url: str) -> Any:
        """After the first listener shut down:  move CLAUDE off the router in the config and start
        again, with the router's bearer still set and CLAUDE_ROUTINE_URL holding `url`."""
        self.write_config(
            seats=router_seat("CLAUDE", "Claude", key_env="CLAUDE_ROUTINE_KEY", auth="hmac-sha256")
        )
        second = self.daemon(env=self.env(**{
            C.ROUTER_BEARER_ENV: self.router_key,
            "CLAUDE_ROUTINE_URL": url,
            "CLAUDE_ROUTINE_KEY": "k" * 40,
        }))
        self.assertFalse(C.routes_to_router(second.seats["CLAUDE"].cfg.routine))
        self.assertIn(wake_id, second.seats["CLAUDE"].receipt_waits)
        return second

    def test_a_restart_after_the_seat_moved_off_the_router_still_reads_what_it_sent(self) -> None:
        # The `done` row records the routine the wake went by (`receipt_routine`), so a restart
        # whose config no longer sends the seat to the router reads the receipt with it, instead
        # of never reading it and recording `unknown` at the deadline.
        first = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(first)
        self.assertEqual(
            self.done_row(wake_id).get("receipt_routine", {}).get("url_env"), "CLAUDE_ROUTINE_URL"
        )
        self.assertNotIn(self.url(), json.dumps(self.done_row(wake_id)), "the row holds the URL")
        first.shutdown()
        second = self.off_router_restart(wake_id, self.url())
        self.answer(wake_id, self.receipt(wake_id))
        self.pass_at(second, 31)
        seen = [r for r in self.routine.health_requests if "/receipt/" in r.path]
        self.assertEqual(len(seen), 1, "the restart did not read with the routine the wake went by")
        self.assertEqual(seen[0].header("Authorization"), "Bearer " + self.router_key)
        self.assertEqual(
            [(r["wake_id"], r["state"]) for r in self.outcomes()], [(wake_id, "delivered")]
        )

    def test_a_restored_routine_is_not_read_once_its_variable_names_another_host(self) -> None:
        # The router's bearer goes only where the wake went:  when the recorded URL variable now
        # names another host or port, the restart does not read with the restored routine.
        first = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(first)
        first.shutdown()
        other = FakeRoutine()
        other.start()
        self.addCleanup(other.stop)
        other.health["/receipt/CLAUDE/%s" % wake_id] = (200, self.receipt(wake_id))
        second = self.off_router_restart(wake_id, other.url + "/wake/CLAUDE")
        self.pass_at(second, 31)
        self.assertEqual(
            [r.path for r in other.health_requests], [], "the router's bearer went to another host"
        )
        self.assertEqual((self.receipt_gets(), self.outcomes()), ([], []))


class ReceiptDurabilityTests(ReceiptHarness):
    def test_an_outcome_row_that_landed_before_its_append_failed_is_written_once(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        self.answer(wake_id, self.receipt(wake_id))
        ledger = daemon.seats["CLAUDE"].ledger
        real = ledger.append

        def fsync_fails(row: Any) -> None:
            real(row)  # the row is written, and the fsync after it fails
            if row.get("state") in OUTCOMES:
                raise OSError(errno.EIO, "fsync failed")

        with mock.patch.object(ledger, "append", side_effect=fsync_fails):
            self.pass_at(daemon, 31)
            self.pass_at(daemon, 120)
            self.pass_at(daemon, 600)
        self.assertEqual(
            [(r["wake_id"], r["state"]) for r in self.outcomes()], [(wake_id, "delivered")]
        )
        self.assertEqual(len(self.receipt_gets()), 1, "a wake whose outcome row landed was read again")
        self.assertEqual(self.log_events("wake-receipt-error"), [])

    def test_a_ledger_that_cannot_be_written_is_not_read_on_every_pass(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        self.pending_answer(wake_id)
        ledger = daemon.seats["CLAUDE"].ledger
        real = ledger.append

        def unwritable(row: Any) -> None:
            if row.get("state") in OUTCOMES:
                raise OSError(errno.ENOSPC, "no space left")
            real(row)

        with mock.patch.object(ledger, "append", side_effect=unwritable):
            self.pass_at(daemon, DEADLINE + 1)  # the last read, and its row cannot be written
            self.assertEqual(len(self.receipt_gets()), 1)
            for _ in range(4):
                self.pass_at(daemon, 15)
            self.assertEqual(
                len(self.receipt_gets()), 1, "a wake past its deadline was read on every pass"
            )
            self.pass_at(daemon, 1800)
            self.assertEqual(len(self.receipt_gets()), 2)
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(len(self.log_events("wake-receipt-error")), 2)
        self.pass_at(daemon, 1800)  # the ledger can be written again
        self.assertEqual(
            [(r["wake_id"], r["state"], r["receipt_last"]) for r in self.outcomes()],
            [(wake_id, "unknown", "accepted")],
        )

    def test_a_read_in_flight_at_shutdown_settles_nothing_and_shutdown_waits(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        self.answer(wake_id, self.receipt(wake_id))
        entered, release = threading.Event(), threading.Event()
        real = daemon.routine_runner.read_receipt

        def held(target: Any, url: str, **kw: Any) -> Any:
            entered.set()
            release.wait(5)
            return real(target, url, **kw)

        with mock.patch.object(daemon.routine_runner, "read_receipt", side_effect=held):
            self.clock.advance(31)
            daemon.tick()
            self.assertTrue(entered.wait(5), "the receipt pass never started its read")
            timer = threading.Timer(0.3, release.set)
            timer.start()
            daemon.shutdown()
            timer.join()
            self.assertFalse(
                daemon._receipt_thread.is_alive(),
                "shutdown returned while a receipt read was still running",
            )
        self.assertEqual(self.outcomes(), [], "a read that ended after shutdown wrote an outcome")
        self.assertIn(wake_id, W.awaiting_receipts(W.Ledger(self.seat_paths().wakes).wakes()))


class ReceiptTimeTests(ReceiptHarness):
    def test_an_outcome_row_does_not_move_the_time_the_stale_once_rule_reads(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        runner = daemon.seats["CLAUDE"]
        ran = self.clock.time()
        settled = ran + 25 * 3600

        def stale_owner_wake(wake_id: str, topic: str, at: float) -> None:
            for state, back in (("queued", 30), ("accepted", 20), ("started", 10), ("done", 0)):
                runner.ledger.append(
                    {"ts": at - back, "wake_id": wake_id, "seat": "CLAUDE", "state": state,
                     "topic_key": L.topic_key("simple-tools", topic), "stale": True,
                     "owner": True, "trigger_ids": [], "adapter": "http"}
                )

        # A stale owner wake that ran at `ran` and whose router outcome came 25 hours later, and,
        # as the rule's control, one in another topic that ran at that later time.
        stale_owner_wake("w-ran-1", "t", ran)
        runner.ledger.append(
            {"ts": settled, "wake_id": "w-ran-1", "seat": "CLAUDE", "state": "unknown",
             "receipt_last": "accepted"}
        )
        stale_owner_wake("w-control-2", "t2", settled)
        view = runner.ledger.wakes()["w-ran-1"]
        self.assertEqual((view["state"], view["ts"]), ("unknown", ran), "the outcome moved `ts`")
        self.assertEqual(view["times"]["unknown"], settled)
        self.clock.advance(25 * 3600 + 60)
        for message_id, topic in ((6001, "t"), (6002, "t2")):
            daemon.offer_wake(
                runner,
                {"id": message_id, "channel": "simple-tools", "topic": topic, "type": "stream",
                 "stale": True, "ts": self.clock.time()},
                owner=True,
            )
        dropped = [
            r["topic_key"] for r in self.ledger()
            if r.get("state") == "dropped" and r.get("reason") == "stale_once"
        ]
        self.assertEqual(
            dropped,
            [L.topic_key("simple-tools", "t2")],
            "the stale-once day was measured from the router's outcome, not from the run",
        )

    def test_a_wake_still_awaited_after_a_day_is_counted_as_awaiting(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        self.pending_answer(wake_id)
        self.pass_at(daemon, 31)
        view = LR.status_view(self.root, "CLAUDE", now=self.clock.time() + 25 * 3600)
        self.assertEqual(view["wakes_24h"], 0)
        self.assertEqual(
            view["wake_outcomes_24h"]["awaiting"],
            1,
            "a wake still waiting on its receipt after 24 hours fell out of the count",
        )


class Trickle:
    """A server on 127.0.0.1 that answers a request with a 200 head at once, then one body byte
    every `gap` seconds for at most `seconds`, then closes."""

    def __init__(self, gap: float = 0.05, seconds: float = 3.0) -> None:
        self.gap, self.seconds = gap, seconds
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.sock.getsockname()[1]

    def _serve(self) -> None:
        while not self.done.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                try:
                    conn.settimeout(1.0)
                    conn.recv(65536)
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 100000\r\nConnection: close\r\n\r\n"
                    )
                    end = time.monotonic() + self.seconds
                    while time.monotonic() < end and not self.done.is_set():
                        conn.sendall(b" ")
                        time.sleep(self.gap)
                except OSError:
                    pass

    def close(self) -> None:
        self.done.set()
        self.sock.close()
        self.thread.join(5)


class ReceiptHttpTests(ReceiptHarness):
    def target(self) -> A.RoutineTarget:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        target, why, _detail = daemon.routine(daemon.seats["CLAUDE"])
        self.assertIsNotNone(target, why)
        return target

    def test_a_redirected_receipt_read_is_never_followed(self) -> None:
        # Following it would re-send the router's bearer to another host;  a followed redirect
        # here would end in a refused connection (`network`), never in `http_302`.
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        wake_id = self.wake(daemon)
        self.routine.health["/receipt/CLAUDE/%s" % wake_id] = (
            "redirect", "http://127.0.0.2:9/stolen"
        )
        self.pass_at(daemon, 31)
        self.assertEqual(self.outcomes(), [])
        self.assertEqual(daemon.seats["CLAUDE"].receipt_waits[wake_id]["last"], "http_302")
        self.pass_at(daemon, DEADLINE)
        row = self.outcome()
        self.assertEqual((row["state"], row["receipt_last"]), ("unknown", "http_302"))

    def test_a_read_nobody_answers_times_out(self) -> None:
        target = self.target()
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(4)  # the kernel completes the handshake, and nothing ever answers
        self.addCleanup(silent.close)
        url = "http://127.0.0.1:%d/receipt/CLAUDE/w1" % silent.getsockname()[1]
        began = time.monotonic()
        read = A.RoutineRunner().read_receipt(target, url, timeout=0.3)
        self.assertEqual((read.status, read.body, read.error), (None, None, "timeout"))
        self.assertLess(time.monotonic() - began, 3.0)

    def test_a_body_sent_slowly_cannot_hold_a_read_past_its_deadline(self) -> None:
        target = self.target()
        server = Trickle()
        self.addCleanup(server.close)
        began = time.monotonic()
        read = A.RoutineRunner().read_receipt(
            target, server.url + "/receipt/CLAUDE/w1", timeout=0.3
        )
        spent = time.monotonic() - began
        self.assertEqual((read.status, read.error), (None, "timeout"))
        self.assertLess(spent, 1.5, "a body sent a byte at a time held the read for %.1f s" % spent)

    def test_a_pass_resolves_each_routine_once_however_many_wakes_are_due(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        for _ in range(25):
            self.pending_answer(self.wake(daemon))
        self.clock.advance(31)
        with mock.patch.object(A, "resolve_routine", wraps=A.resolve_routine) as resolve:
            counts = daemon.receipt_pass()
        self.assertEqual((counts["due"], counts["reads"]), (25, 20))
        self.assertEqual(resolve.call_count, 1, "the routine was resolved for every due wake")


class ReceiptWordTests(unittest.TestCase):
    def test_a_read_that_settled_nothing_is_recorded_as_a_fixed_word(self) -> None:
        cases = [
            ((200, {"status": "accepted"}, None), "accepted"),
            ((200, {"status": "0123456789abcdef0123456789abcdef"}, None), "other"),
            ((200, {"status": "replaying"}, None), "other"),
            ((200, {"status": "delivered"}, None), "mismatched"),
            ((200, {"status": 3}, None), "malformed"),
            ((200, None, None), "malformed"),
            ((302, None, None), "http_302"),
            ((None, None, "timeout"), "timeout"),
            ((None, None, None), "no_answer"),
        ]
        for args, word in cases:
            self.assertEqual(W.receipt_read_word(*args), word, args)

    def test_a_receipt_for_another_wake_or_seat_or_a_test_one_settles_nothing(self) -> None:
        good = {"wake_id": "w1", "seat": "CLAUDE", "status": "delivered", "delivered": True,
                "test": False}
        self.assertEqual(W.receipt_outcome(200, good, seat="CLAUDE", wake_id="w1")[0], "delivered")
        for changed in ({"wake_id": "w2"}, {"seat": "CODEX"}, {"test": True}):
            body = dict(good, **changed)
            self.assertIsNone(W.receipt_outcome(200, body, seat="CLAUDE", wake_id="w1"), changed)
            self.assertEqual(W.receipt_read_word(200, body, None), "mismatched", changed)
        for status in (201, 202, 404, None):
            self.assertIsNone(W.receipt_outcome(status, good, seat="CLAUDE", wake_id="w1"), status)
        self.assertIsNone(
            W.receipt_outcome(200, dict(good, status="accepted"), seat="CLAUDE", wake_id="w1")
        )

    def test_a_whitelisted_field_of_the_wrong_type_or_shape_is_dropped(self) -> None:
        receipt = {
            "status": "dead", "code": "session-cwd-gone", "receipt_id": "0123456789abcdef",
            "picked_letter": "AA", "picked_generation": 3, "host": "S", "route": "resume",
            "ack": "unknown", "attempts": 2, "delivered": False, "reason": "most-recent",
            "session_id": SESSION, "at": 1790000000.5,
        }
        self.assertEqual(
            set(W.receipt_row_fields(receipt)),
            {"receipt_status", "receipt_code", "receipt_id", "picked_letter", "picked_generation",
             "host", "route", "ack", "attempts", "delivered", "reason", "session_id",
             "receipt_at"},
        )
        bad = {
            "status": "maybe", "code": "two words", "receipt_id": "x" * 200,
            "picked_letter": "aa", "picked_generation": "3", "host": "", "route": ["resume"],
            "ack": "ok;rm", "attempts": True, "delivered": "yes", "reason": "has spaces in it",
            "session_id": "s" * 500, "at": "soon",
        }
        self.assertEqual(W.receipt_row_fields(bad), {})
        self.assertEqual(
            W.receipt_row_fields(
                {"attempts": -1, "picked_generation": 10**10, "at": float("nan")}
            ),
            {},
        )

    def test_the_router_shapes_and_keep_time_match_the_listener_copies(self) -> None:
        # adapters keeps its own copy of the router's path shapes (receipt_url), and wakes the
        # time the router keeps a receipt (the backfill window):  a router change must show here.
        self.assertEqual(A._ROUTER_SEAT_RE.pattern, WR.SEAT_RE.pattern.strip("^$"))
        self.assertEqual(A._ROUTER_WAKE_ID_RE.pattern, WR.WAKE_ID_RE.pattern.strip("^$"))
        self.assertEqual(W.RECEIPT_BACKFILL, WR.DONE_KEEP_SECONDS)


class ReceiptSurfacedTests(ReceiptHarness):
    """A router wake surfaces nothing at accept time (AFC-841):  its 202 only stored the wake, so
    the held-back items it listed wait for the terminal receipt -- they surface when it ends
    delivered, and stay held back on any other ending, with a test for each."""

    def seed(self, message_id: int, seat: str = "CLAUDE") -> None:
        HELD.hold(
            self.seat_paths(seat),
            {
                "wake_ids": ["w-seed-%d" % message_id],
                "message_id": message_id,
                "owner": False,
                "topic_key": L.topic_key("simple-tools", "earlier"),
                "dm": False,
                "channel": "simple-tools",
                "topic": "earlier",
                "stream_id": 3,
                "sender_full_name": "Codex",
                "zulip_link": "",
                "reason": "wakes_per_hour",
                "at": int(self.clock.time()),
            },
            cap=200,
            now=self.clock.time(),
        )

    def held_ids(self, seat: str = "CLAUDE") -> list[int]:
        data = HELD.load(self.seat_paths(seat))
        return [e["message_id"] for e in data["records"] + data["overflow"]]

    def test_a_router_wake_surfaces_its_held_back_items_only_on_a_delivered_receipt(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        self.seed(900001)
        self.seed(900002)
        wake_id = self.wake(daemon)
        row = self.done_row(wake_id)
        # At accept time nothing is surfaced, and the row carries what the receipt needs:
        # the wake's own trigger id plus the two held-back items it listed, and the lost count.
        self.assertFalse(row["surfaced"])
        self.assertEqual(row["held_listed"], sorted([self.message_id, 900001, 900002]))
        self.assertEqual(row["held_lost"], 0)
        self.assertEqual(self.held_ids(), [900001, 900002], "accept alone must not surface")
        self.assertEqual(
            [r for r in self.ledger() if r["state"] == "surfaced"], [], "no closing rows yet"
        )
        self.assertEqual(self.log_events("held-back-surfaced"), [])
        # The delivered receipt is what surfaces them.
        self.answer(wake_id, self.receipt(wake_id))
        self.pass_at(daemon, 31)
        self.assertEqual(self.outcome()["state"], "delivered")
        self.assertEqual(self.held_ids(), [], "a delivered receipt surfaces the listed items")
        self.assertEqual(
            [(e["wake_id"], e["surfaced"]) for e in self.log_events("held-back-surfaced")],
            [(wake_id, 2)],
        )
        closed = [r for r in self.ledger() if r["state"] == "surfaced"]
        self.assertEqual(
            [(r["wake_id"], r.get("message_id"), r.get("by")) for r in closed],
            [("w-seed-900001", 900001, wake_id), ("w-seed-900002", 900002, wake_id)],
        )

    def test_a_router_wake_that_ends_dead_leaves_its_held_back_items_held(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        self.seed(900001)
        wake_id = self.wake(daemon)
        self.assertFalse(self.done_row(wake_id)["surfaced"])
        self.answer(wake_id, self.receipt(wake_id, status="dead"))
        self.pass_at(daemon, 31)
        self.assertEqual(self.outcome()["state"], "undelivered")
        self.assertEqual(
            self.held_ids(), [900001], "a dead wake's items are listed for the next wake"
        )
        self.assertEqual(self.log_events("held-back-surfaced"), [])
        self.assertEqual([r for r in self.ledger() if r["state"] == "surfaced"], [])

    def test_a_router_wake_at_its_deadline_records_unknown_and_leaves_its_items_held(self) -> None:
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        self.seed(900001)
        wake_id = self.wake(daemon)
        self.pending_answer(wake_id)
        self.pass_at(daemon, DEADLINE + 10)
        row = self.outcome()
        self.assertEqual((row["state"], row.get("receipt_last")), ("unknown", "accepted"))
        self.assertEqual(self.held_ids(), [900001], "an unknown ending never surfaces")
        self.assertEqual(self.log_events("held-back-surfaced"), [])

    def test_a_stale_lost_snapshot_is_never_replayed_once_another_wake_surfaced_the_items(self) -> None:
        # Wake W1 lists the held item and carries lost=2 (two earlier evictions).  Before its
        # receipt settles, another wake surfaces that item -- clearing the lost count with its
        # own report -- and two newer evictions accrue.  W1's delivered receipt must then do
        # nothing at all:  replaying its lost=2 would erase the two newer evictions' count
        # (Kody, PR 2446).
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        # Four holds under cap 1 / overflow cap 1 leave lost=2 and two held ids (overflow first).
        for mid in (900001, 900002, 900003, 900004):
            HELD.hold(self.seat_paths(), self._record(mid), cap=1, overflow_cap=1, now=self.clock.time())
        self.assertEqual(HELD.load(self.seat_paths())["lost"], 2, "two evicted entries are the lost count")
        self.assertEqual(sorted(self.held_ids()), [900003, 900004])
        wake_id = self.wake(daemon)
        self.assertEqual(self.done_row(wake_id)["held_lost"], 2)
        # Another wake surfaces the items the router wake listed, with its own lost report.
        HELD.surface(self.seat_paths(), message_ids=[900003, 900004], lost=2)
        self.assertEqual(HELD.load(self.seat_paths())["lost"], 0)
        # Two newer evictions accrue afterwards, never reported to any wake yet.
        for mid in (910001, 910002, 910003, 910004):
            HELD.hold(self.seat_paths(), self._record(mid), cap=1, overflow_cap=1, now=self.clock.time())
        self.assertEqual(HELD.load(self.seat_paths())["lost"], 2)
        # The router wake's delivered receipt settles:  its items are already gone, so the
        # replay must not touch the store.
        self.answer(wake_id, self.receipt(wake_id))
        self.pass_at(daemon, 31)
        self.assertEqual(self.outcome()["state"], "delivered")
        self.assertEqual(
            HELD.load(self.seat_paths())["lost"], 2, "the stale lost snapshot was replayed"
        )
        self.assertEqual(
            [e for e in self.log_events("held-back-surfaced") if e.get("by") == "receipt"],
            [],
        )

    def _record(self, message_id: int) -> dict[str, Any]:
        return {
            "wake_ids": ["w-seed-%d" % message_id],
            "message_id": message_id,
            "owner": False,
            "topic_key": L.topic_key("simple-tools", "earlier"),
            "dm": False,
            "channel": "simple-tools",
            "topic": "earlier",
            "stream_id": 3,
            "sender_full_name": "Codex",
            "zulip_link": "",
            "reason": "wakes_per_hour",
            "at": int(self.clock.time()),
        }

    def test_a_wake_that_listed_no_items_still_clears_the_lost_count_it_carried(self) -> None:
        # A done row whose held_listed is empty (its items were all evicted before it) still
        # clears the lost count it reported (Kody, PR 2446).
        daemon = self.start(router_seat("CLAUDE", "Claude"), CLAUDE_ROUTINE_URL=self.url())
        HELD.hold(self.seat_paths(), self._record(900001), cap=1, overflow_cap=1, now=self.clock.time())
        HELD.hold(self.seat_paths(), self._record(900002), cap=1, overflow_cap=1, now=self.clock.time())
        HELD.hold(self.seat_paths(), self._record(900003), cap=1, overflow_cap=1, now=self.clock.time())
        self.assertEqual(HELD.load(self.seat_paths()).get("lost", 0), 1)
        runner = daemon.seats["CLAUDE"]
        runner.ledger.append(
            {
                "ts": self.clock.time(), "wake_id": "w-empty", "seat": "CLAUDE", "state": "done",
                "adapter": "http", "surfaced": False, "receipt_awaited": True,
                "held_listed": [], "held_lost": 1,
            }
        )
        daemon._surface_from_receipt(runner, "w-empty")
        self.assertEqual(HELD.load(self.seat_paths()).get("lost", 0), 0, "the carried count is cleared")
        (event,) = self.log_events("held-back-surfaced")
        self.assertEqual((event["surfaced"], event["lost"], event["by"]), (0, 1, "receipt"))

    def test_a_worker_wake_keeps_surfacing_at_accept_time(self) -> None:
        # The Worker's own delivered answer still surfaces at once:  the receipt wait is the
        # router's alone, and a non-router seat's accepted wake is not held for a receipt.
        daemon = self.start(
            router_seat("CODEX", "Codex", key_env="CODEX_ROUTINE_KEY", auth="hmac-sha256"),
            CODEX_ROUTINE_URL=self.url("CODEX"),
            CODEX_ROUTINE_KEY="h" * 40,
        )
        self.seed(900001, "CODEX")
        # A Worker 202 that says it delivered one subscriber (the POST is answered from the script).
        self.routine.script.append((202, {"subscribers": 1, "delivered": 1, "outcome": "delivered"}))
        runner = daemon.seats["CODEX"]
        row = {
            "seq": 8001, "id": 8001, "kind": "message", "type": "stream",
            "channel": "simple-tools", "topic": "t", "sender": "Codex", "sender_id": 11,
            "is_bot": True, "content": MENTION, "classes": ["eligible", "direct"],
            "wake_worthy": True,
        }
        L.append_jsonl(runner.paths.seat_inbox, [row])
        now = self.clock.time()
        pending = W.Pending("w-worker-1", "CODEX", row, owner=False, now=now, due=now)
        pending.add(row, False)
        runner.ledger.append(pending.row("queued", now))
        daemon.run_routine(runner, pending, now)
        done = [r for r in self.ledger("CODEX") if r["state"] == "done"][0]
        self.assertTrue(done["surfaced"], "the Worker's delivered answer surfaces at once")
        self.assertNotIn("receipt_awaited", done)
        self.assertNotIn("held_listed", done)
        self.assertEqual(self.held_ids("CODEX"), [], "the Worker wake surfaced the held item")
