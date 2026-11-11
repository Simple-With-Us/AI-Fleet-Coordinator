"""The shared wake router and Claude's delivery (OPS-1048).  The registry is a fake, the inbox is a
real unix socket a test thread listens on, and a resume is a fake runner:  no test reaches a live
chat, the live registry or a real `claude`."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simple_tools import claude_wake as CW
from simple_tools import wake_router as WR

SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"
SID_C = "cccccccc-0000-4000-8000-000000000003"
BODY_MARK = "THE-MESSAGE-BODY-MUST-NOT-TRAVEL"


def row(sid: str, letter: str, title: str, host: str = "server", **over) -> dict:
    base = {
        "native_session_id": sid,
        "letter": letter,
        "title": title,
        "verified_host": host,
        "seat": "CLAUDE",
        "state": "active",
        "retired_at": None,
        "closed_at": None,
        "generation": 1,
    }
    base.update(over)
    return base


class FakeRegistry:
    """Stands in for registry_api.call."""

    def __init__(self, rows: list[dict] | None = None, letters: dict | None = None) -> None:
        self.rows = rows or []
        self.letters = letters or {}
        self.down = False
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, env, op, params=None, *, timeout=25):
        self.calls.append((op, dict(params or {})))
        if self.down:
            return {"error": "the registry did not answer in time", "code": "timeout"}
        if op == "resolve":
            found = self.letters.get(params["letter"])
            if found is None:  # as the registry does:  a live alias answers, whoever asked for it
                live = [r for r in self.rows if r["letter"] == params["letter"]]
                found = next((r for r in live if not r["retired_at"]), None)
            if found is None:
                return {"label": params["letter"], "found": False}
            return {**found, "label": params["letter"], "found": True}
        if op == "history":
            return {"items": list(self.rows), "count": len(self.rows)}
        raise AssertionError(op)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Killed(BaseException):
    """Stands in for an abrupt kill:  no `except Exception` in the router may see it."""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        # Under /tmp, not $TMPDIR:  a unix socket path is limited to about 104 bytes.
        self.tmp = tempfile.mkdtemp(prefix="wr", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = Path(self.tmp) / "state"
        self.state.mkdir(mode=0o700)
        self.key = secrets.token_hex(24)
        self.key_file = self.state / "routine-key"
        self.key_file.write_text(self.key + "\n", encoding="utf-8")
        os.chmod(self.key_file, 0o600)
        self.registry = FakeRegistry()
        patcher = mock.patch.object(WR.registry_api, "call", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.delivered: list[tuple[str, str]] = []
        self.results: list[WR.Delivery] = []
        stderr = contextlib.redirect_stderr(io.StringIO())
        self.stderr = stderr.__enter__()
        self.addCleanup(stderr.__exit__, None, None, None)

    def config(self, **over) -> dict:
        return {**WR.ROUTER_DEFAULTS, "delivery_enabled": True, **over}

    def fake_deliver(self, target: WR.Target, wake: WR.Wake) -> WR.Delivery:
        self.delivered.append((target.session_id, wake.wake_id))
        if self.results:
            return self.results.pop(0)
        return WR.Delivery("completed", "fake", ack="confirmed")

    def router(self, deliver=None, consult=None, **config) -> WR.Router:
        platform = WR.Platform(
            seat="CLAUDE",
            platform="claude",
            mention="Claude",
            target_re=CW.SESSION_RE,
            deliver=deliver or self.fake_deliver,
        )
        return WR.Router(
            platform,
            {"HOME": self.tmp},
            self.config(**config),
            self.state,
            self.key_file,
            consult=consult,
            clock=self.clock,
        )

    def wake(self, wake_id: str = "w-1", **over) -> WR.Wake:
        base = {"wake_id": wake_id, "seat": "CLAUDE", "topic": "unrelated", "channel": "wip"}
        base.update(over)
        return WR.Wake(**base)

    def done(self, wake_id: str = "w-1") -> dict:
        return json.loads((self.state / "done" / (wake_id + ".json")).read_text(encoding="utf-8"))

    def pending(self, wake_id: str = "w-1") -> Path:
        return self.state / "pending" / (wake_id + ".json")

    def drain(self, router: WR.Router) -> None:
        while router.run_once():
            pass


class LetterParse(unittest.TestCase):
    def test_mention_forms(self) -> None:
        cases = {
            "@**Claude** AA please look": "AA",
            "@**Claude|1212574** BB status?": "BB",
            "[MM: HH.1.M → @**Claude** CC] hello": "CC",
            "Claude DD, can you check": "DD",
            "no letter here": "",
            "@**Claude** please look": "",
        }
        for text, want in cases.items():
            self.assertEqual(WR.letter_of({"excerpt": text}, "Claude"), want, text)

    def test_the_excerpt_marker_line_is_skipped(self) -> None:
        excerpt = "BEGIN_UNTRUSTED_ZULIP nonce=1\nClaude EE look\nEND_UNTRUSTED_ZULIP nonce=1"
        self.assertEqual(WR.letter_of({"excerpt": excerpt}, "Claude"), "EE")

    def test_a_reference_is_not_an_address(self) -> None:
        text = "@**Claude** as Claude AA said earlier"
        self.assertEqual(WR.letter_of({"excerpt": text}, "Claude"), "")

    def test_a_silent_mention_addresses_nobody(self) -> None:
        self.assertEqual(WR.letter_of({"excerpt": "cc @_**Claude** AA"}, "Claude"), "")

    def test_an_explicit_field_wins(self) -> None:
        self.assertEqual(WR.letter_of({"letter": "Q", "excerpt": "@**Claude** AA"}, "Claude"), "Q")


class Picking(Base):
    def test_a_letter_beats_topic_and_crosses_hosts(self) -> None:
        self.registry.rows = [row(SID_B, "BB", "Exact Topic")]
        self.registry.letters = {"AA": row(SID_A, "AA", "Other", host="mac")}
        pick = self.router().pick(self.wake(letter="AA", topic="Exact Topic"))
        self.assertEqual((pick.target.session_id, pick.picked), (SID_A, "letter"))
        self.assertEqual(pick.target.host, "mac")

    def test_a_dead_letter_falls_back(self) -> None:
        self.registry.rows = [row(SID_B, "BB", "Router contract work")]
        pick = self.router().pick(self.wake(letter="PR", topic="Router contract"))
        self.assertEqual((pick.target.session_id, pick.picked), (SID_B, "topic-match"))
        self.assertFalse(pick.letter_live)

    def test_topic_match_takes_the_single_best(self) -> None:
        self.registry.rows = [
            row(SID_A, "AA", "Wake router letters"),
            row(SID_B, "BB", "Wake unrelated"),
        ]
        pick = self.router().pick(self.wake(topic="wake router"))
        self.assertEqual((pick.target.session_id, pick.picked), (SID_A, "topic-match"))

    def test_a_topic_tie_goes_to_the_seam_with_only_the_tied(self) -> None:
        seen: list[list[str]] = []

        def consult(candidates, wake):
            seen.append([t.session_id for t in candidates])
            return None

        self.registry.rows = [
            row(SID_A, "AA", "Wake router"),
            row(SID_B, "BB", "Wake router"),
            row(SID_C, "CC", "Something else"),
        ]
        pick = self.router(consult=consult).pick(self.wake(topic="wake router"))
        self.assertIsNone(pick.target)
        self.assertEqual((pick.reason, pick.undecided), ("consultant-none", "topic-tie"))
        self.assertEqual(seen, [[SID_A, SID_B]])

    def test_recency_decides_only_for_a_sole_live_chat(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "Nothing shared")]
        pick = self.router().pick(self.wake(topic="other words"))
        self.assertEqual((pick.target.session_id, pick.picked), (SID_A, "most-recent"))
        self.assertEqual(pick.reason, "sole-live-chat")

    def test_two_live_chats_with_no_match_are_not_guessed(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "One"), row(SID_B, "BB", "Two")]
        pick = self.router().pick(self.wake(topic="other words"))
        self.assertIsNone(pick.target)
        self.assertEqual((pick.reason, pick.undecided), ("consultant-missing", "ambiguous"))
        self.assertEqual(pick.candidates, ("AA", "BB"))

    def test_a_closed_chat_is_never_picked_by_its_letter(self) -> None:
        for over in ({"state": "closed"}, {"closed_at": "2026-11-01T00:00:00Z"}):
            self.registry.letters = {"AA": row(SID_A, "AA", "Other", **over)}
            pick = self.router().pick(self.wake(letter="AA"))
            self.assertIsNone(pick.target)
            self.assertEqual((pick.reason, pick.letter_live), ("no-live-session", False))

    def test_no_live_chat(self) -> None:
        self.assertEqual(self.router().pick(self.wake()).reason, "no-live-session")

    def test_a_registry_outage_is_not_no_live_chat(self) -> None:
        self.registry.down = True
        pick = self.router().pick(self.wake())
        self.assertTrue(pick.retry)
        self.assertEqual(pick.reason, "registry-unavailable")

    def test_rows_that_can_never_be_targets(self) -> None:
        self.registry.rows = [
            row("-p", "AA", "unrelated"),
            row(SID_A, "BB", "unrelated", seat="ZC"),
            row(SID_B, "CC", "unrelated", retired_at="2026-11-01T00:00:00Z"),
            row(SID_C, "DD", "unrelated", closed_at="2026-11-01T00:00:00Z"),
        ]
        self.assertEqual(self.router().pick(self.wake()).reason, "no-live-session")

    def test_an_excluded_chat_is_never_picked(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "One"), row(SID_B, "BB", "Two")]
        router = self.router()
        router.platform = WR.Platform(
            seat="CLAUDE",
            platform="claude",
            mention="Claude",
            target_re=CW.SESSION_RE,
            deliver=self.fake_deliver,
            exclude=lambda targets: {SID_A},
        )
        pick = router.pick(self.wake(topic="other"))
        self.assertEqual((pick.target.session_id, pick.picked), (SID_B, "most-recent"))

    def seam(self, answer) -> WR.Pick:
        self.registry.rows = [row(SID_A, "AA", "One"), row(SID_B, "BB", "Two")]
        return self.router(consult=answer).pick(self.wake(topic="other"))

    def test_the_seam_can_pick(self) -> None:
        pick = self.seam(lambda c, w: {"session_id": SID_B})
        self.assertEqual((pick.target.session_id, pick.picked), (SID_B, "consultant"))

    def test_the_seam_cannot_pick_outside_the_candidates(self) -> None:
        pick = self.seam(lambda c, w: {"session_id": SID_C})
        self.assertIsNone(pick.target)
        self.assertEqual(pick.reason, "consultant-invalid")

    def test_the_seam_can_ask(self) -> None:
        pick = self.seam(lambda c, w: {"question": "Which chat?"})
        self.assertEqual((pick.reason, pick.question), ("consultant-asked", "Which chat?"))

    def test_a_failing_seam_holds(self) -> None:
        def broken(candidates, wake):
            raise RuntimeError("down")

        self.assertEqual(self.seam(broken).reason, "consultant-unavailable")

    def test_a_held_wake_writes_a_receipt_and_delivers_nothing(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "One"), row(SID_B, "BB", "Two")]
        router = self.router()
        router.accept(self.wake(topic="other"))
        self.drain(router)
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["reason"]), ("held", "consultant-missing"))
        self.assertEqual(self.delivered, [])
        self.assertFalse(self.pending().exists())


class Durability(Base):
    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "Only chat")]

    def test_the_wake_is_on_disk_before_the_answer(self) -> None:
        status, answer = self.router().accept(self.wake())
        self.assertEqual(status, 202)
        saved = json.loads(self.pending().read_text(encoding="utf-8"))
        self.assertEqual(saved["receipt_id"], answer["receipt_id"])
        self.assertEqual(self.pending().stat().st_mode & 0o777, 0o600)

    def test_a_failed_write_is_503_so_the_listener_retries(self) -> None:
        router = self.router()
        with mock.patch.object(WR, "atomic_write", side_effect=OSError):
            status, _ = router.accept(self.wake())
        self.assertEqual(status, 503)
        self.assertFalse(router.run_once())

    def test_a_restart_replays_and_delivers(self) -> None:
        self.router().accept(self.wake())  # this router "crashes":  its workers never ran
        fresh = self.router()
        self.assertEqual(fresh.replay(), 1)
        self.drain(fresh)
        self.assertEqual(self.delivered, [(SID_A, "w-1")])
        receipt = self.done()
        self.assertTrue(receipt["replayed"])
        self.assertEqual(receipt["status"], "delivered")
        self.assertFalse(self.pending().exists())

    def test_replay_keeps_the_pick_and_counts(self) -> None:
        self.results = [WR.Delivery("failed", "fake", code="boom")]
        first = self.router()
        first.accept(self.wake())
        self.drain(first)
        # A fresh pick would now be undecided, so a delivery proves the saved pick was kept.
        self.registry.rows = [row(SID_A, "AA", "Only chat"), row(SID_B, "BB", "Another")]
        self.clock.now += 1000
        fresh = self.router()
        fresh.replay()
        self.drain(fresh)
        self.assertEqual(self.delivered, [(SID_A, "w-1"), (SID_A, "w-1")])
        self.assertEqual(self.done()["attempts"], 2)

    def test_a_crash_after_the_receipt_only_cleans_up(self) -> None:
        router = self.router()
        router.accept(self.wake())
        saved = self.pending().read_text(encoding="utf-8")
        self.drain(router)
        self.pending().write_text(saved, encoding="utf-8")
        fresh = self.router()
        self.assertEqual(fresh.replay(), 0)
        self.assertFalse(self.pending().exists())
        self.assertEqual(len(self.delivered), 1)

    def test_a_corrupt_pending_file_stays_and_is_named(self) -> None:
        self.pending().parent.mkdir(parents=True, exist_ok=True)
        self.pending("w-bad").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.router().replay(), 0)
        self.assertTrue(self.pending("w-bad").exists())
        self.assertIn("w-bad pending-unreadable", self.stderr.getvalue())

    def test_a_receipt_that_cannot_be_written_keeps_the_wake(self) -> None:
        router = self.router()
        router.accept(self.wake())
        real = WR.atomic_write

        def fail_done(path, obj):
            if path.parent.name == "done":
                raise OSError
            real(path, obj)

        with mock.patch.object(WR, "atomic_write", side_effect=fail_done):
            router.run_once()
        self.assertTrue(self.pending().exists())

    def test_a_failed_receipt_write_never_delivers_again(self) -> None:
        router = self.router()
        router.accept(self.wake())
        real = WR.atomic_write

        def fail_done(path, obj):
            if path.parent.name == "done":
                raise OSError
            real(path, obj)

        with mock.patch.object(WR, "atomic_write", side_effect=fail_done):
            for _ in range(4):
                router.run_once()
                self.clock.now += 100
        self.assertEqual(len(self.delivered), 1)
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.done()["status"], "delivered")
        self.assertFalse(self.pending().exists())

    def test_a_restart_with_an_unwritten_receipt_does_not_redeliver(self) -> None:
        router = self.router()
        router.accept(self.wake())
        real = WR.atomic_write

        def fail_done(path, obj):
            if path.parent.name == "done":
                raise OSError
            real(path, obj)

        with mock.patch.object(WR, "atomic_write", side_effect=fail_done):
            router.run_once()
        self.clock.now += 100
        fresh = self.router()
        self.assertEqual(fresh.replay(), 1)
        self.drain(fresh)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.done()["status"], "delivered")

    def test_a_kill_during_delivery_is_final_and_never_runs_again(self) -> None:
        marks: list[bool] = []

        def killed(target, wake):
            self.delivered.append((target.session_id, wake.wake_id))
            marks.append(json.loads(self.pending().read_text(encoding="utf-8"))["in_flight"])
            raise Killed

        router = self.router(deliver=killed)
        router.accept(self.wake())
        with self.assertRaises(Killed):
            router.run_once()
        self.assertEqual(marks, [True])  # durable before the delivery began
        fresh = self.router()
        self.assertEqual(fresh.replay(), 1)
        self.drain(fresh)
        self.assertEqual(len(self.delivered), 1)
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["code"]), ("dead", WR.INTERRUPTED))
        self.assertIsNone(receipt["delivered"])  # it may have run:  nobody knows
        self.assertTrue(receipt["replayed"])
        self.assertEqual(receipt["session_id"], SID_A)
        self.assertFalse(self.pending().exists())

    def test_no_durable_mark_means_no_delivery(self) -> None:
        router = self.router()
        router.accept(self.wake())
        with mock.patch.object(WR, "atomic_write", side_effect=OSError):
            router.run_once()
        self.assertEqual(self.delivered, [])
        self.clock.now += 100
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.done()["attempts"], 1)

    def test_a_mark_that_cannot_be_cleared_still_never_runs_again(self) -> None:
        ran: list[int] = []

        def deliver(target, wake):
            ran.append(1)
            patcher.start()  # the disk fails after the delivery, for the outcome and the receipt
            return WR.Delivery("completed")

        patcher = mock.patch.object(WR, "atomic_write", side_effect=OSError)
        router = self.router(deliver=deliver)
        router.accept(self.wake())
        try:
            router.run_once()
        finally:
            patcher.stop()
        fresh = self.router(deliver=deliver)
        fresh.replay()
        self.drain(fresh)
        self.assertEqual(ran, [1])
        self.assertEqual(self.done()["code"], WR.INTERRUPTED)

    def test_a_fault_after_the_delivery_does_not_retry_it(self) -> None:
        router = self.router()
        router.accept(self.wake())
        with mock.patch.object(router, "_settle", side_effect=RuntimeError):
            router.run_once()
        self.clock.now += 5000
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.done()["code"], WR.INTERRUPTED)

    def test_a_pending_file_missing_a_field_is_set_aside(self) -> None:
        router = self.router()
        router.accept(self.wake("w-good"))
        router.accept(self.wake("w-bad"))
        bad = json.loads(self.pending("w-bad").read_text(encoding="utf-8"))
        del bad["accepted_at"]
        self.pending("w-bad").write_text(json.dumps(bad), encoding="utf-8")
        fresh = self.router()
        self.assertEqual(fresh.replay(), 1)
        self.drain(fresh)
        self.assertEqual(self.delivered, [(SID_A, "w-good")])
        self.assertTrue(self.pending("w-bad").exists())
        self.assertIn("w-bad pending-unreadable", self.stderr.getvalue())

    def test_a_worker_survives_a_fault_in_its_loop(self) -> None:
        router = self.router(workers=1)
        real = router._claim
        faults: list[int] = []

        def claim_once_broken():
            if not faults:
                faults.append(1)
                raise KeyError("accepted_at")
            return real()

        router._claim = claim_once_broken
        router.start()
        self.addCleanup(router.stop)
        router.accept(self.wake())
        deadline = time.time() + 10
        while time.time() < deadline and not (self.state / "done" / "w-1.json").exists():
            time.sleep(0.05)
        self.assertEqual(self.done()["status"], "delivered")


class Shutdown(Base):
    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "Only chat")]
        self.registry.letters = {"BB": row(SID_B, "BB", "Other")}
        patcher = mock.patch.object(WR.registry_api, "base_url", return_value="http://r.invalid")
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, router: WR.Router, port: int = 0) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return WR.serve(router, "127.0.0.1", port)

    def sigterm_during(self, router: WR.Router, started, then) -> None:
        """Run `serve` on this thread (a signal handler needs the main thread) and send SIGTERM
        once a delivery has started, then call `then` while the router is stopping."""

        def driver() -> None:
            self.assertTrue(started.wait(timeout=20))
            os.kill(os.getpid(), signal.SIGTERM)
            deadline = time.time() + 10
            while time.time() < deadline and not router._stopping:
                time.sleep(0.02)
            then()

        thread = threading.Thread(target=driver, daemon=True)
        thread.start()
        self.assertEqual(self.serve(router), 0)
        thread.join(timeout=20)

    def test_sigterm_finishes_the_running_delivery_and_starts_no_more(self) -> None:
        started = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def deliver(target, wake):
            order.append(wake.wake_id)
            started.set()
            release.wait(timeout=20)
            return WR.Delivery("completed")

        router = self.router(deliver=deliver, workers=2, shutdown_wait_seconds=30)
        router.accept(self.wake("w-1"))

        def while_stopping() -> None:
            self.clock.now += 1
            router.accept(self.wake("w-2", letter="BB"))
            time.sleep(0.4)
            release.set()

        self.sigterm_during(router, started, while_stopping)
        self.assertTrue(release.is_set())  # serve did not return before the delivery ended
        self.assertEqual(order, ["w-1"])
        self.assertEqual(self.done("w-1")["status"], "delivered")
        self.assertTrue(self.pending("w-2").exists())
        fresh = self.router()  # the restart:  what never started is delivered, once
        self.assertEqual(fresh.replay(), 1)
        self.drain(fresh)
        self.assertEqual(self.delivered, [(SID_B, "w-2")])

    def test_a_delivery_outlasting_the_wait_is_closed_by_the_replay(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def deliver(target, wake):
            started.set()
            release.wait(timeout=20)
            return WR.Delivery("completed")

        router = self.router(deliver=deliver, workers=1, shutdown_wait_seconds=1)
        router.accept(self.wake())
        self.sigterm_during(router, started, lambda: None)
        self.assertFalse(release.is_set())
        fresh = self.router()
        fresh.replay()
        self.drain(fresh)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.done()["code"], WR.INTERRUPTED)
        release.set()
        router.stop()

    def test_a_second_instance_touches_no_pending_wake(self) -> None:
        self.router().accept(self.wake())
        fresh = self.router()
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            with self.assertRaises(SystemExit):
                self.serve(fresh, taken.getsockname()[1])
        self.assertEqual(fresh._threads, [])
        self.assertEqual(fresh._entries, {})
        self.assertEqual(self.delivered, [])
        self.assertTrue(self.pending().exists())

    def test_the_unit_gives_the_wait_room_and_spares_the_children(self) -> None:
        unit = (Path(CW.__file__).parent / "server" / "claude-wake-router.service").read_text()
        stop = int(re.search(r"^TimeoutStopSec=(\d+)$", unit, re.M).group(1))
        wait = CW.DEFAULTS["shutdown_wait_seconds"]
        longest = CW.DEFAULTS["resume_timeout_seconds"] + CW.SSH_MARGIN_SECONDS
        self.assertGreaterEqual(wait, longest + CW.SSH_PREFLIGHT_SECONDS)
        self.assertGreater(stop, wait)
        self.assertRegex(unit, r"(?m)^KillMode=mixed$")


class Generations(Base):
    """A pick can go stale while its wake waits.  Each test picks, waits on a busy answer, changes
    the registry, and lets the retry reach the check before delivery."""

    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "Only chat", generation=3)]

    def queued(self, wake: WR.Wake | None = None, **config) -> WR.Router:
        self.results = [WR.Delivery("busy", code="session-locked")]
        router = self.router(busy_retry_seconds=30, **config)
        router.accept(wake or self.wake())
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)
        self.clock.now += 31
        return router

    def lettered(self, **config) -> WR.Router:
        self.registry.letters = {"AA": row(SID_A, "AA", "Only chat", generation=3)}
        return self.queued(self.wake(letter="AA"), **config)

    def assert_released(self, router: WR.Router) -> None:
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)  # only the try before the wait
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["code"]), ("dead", WR.RELEASED))
        self.assertIs(receipt["delivered"], False)
        self.assertEqual((receipt["session_id"], receipt["picked_generation"]), (SID_A, 3))
        self.assertFalse(self.pending().exists())

    def test_a_target_carries_the_generation_on_the_letter_path(self) -> None:
        self.registry.letters = {"AA": row(SID_A, "AA", "Other", generation=7)}
        pick = self.router().pick(self.wake(letter="AA"))
        self.assertEqual((pick.picked, pick.target.generation), ("letter", 7))

    def test_a_target_carries_the_generation_on_the_history_path(self) -> None:
        pick = self.router().pick(self.wake())
        self.assertEqual(pick.target.generation, 3)

    def test_a_target_defaults_to_no_generation(self) -> None:
        self.assertEqual(WR.Target(SID_A).generation, 0)

    def test_a_row_with_no_generation_is_not_a_target(self) -> None:
        for bad in (None, 0, "3", True):
            self.registry.rows = [row(SID_A, "AA", "Only chat", generation=bad)]
            self.assertEqual(self.router().pick(self.wake()).reason, "no-live-session")

    def test_the_pick_and_receipt_keep_the_generation(self) -> None:
        router = self.queued()
        saved = json.loads(self.pending().read_text(encoding="utf-8"))
        self.assertEqual(saved["pick"]["target"]["generation"], 3)
        self.drain(router)
        self.assertEqual(self.done()["picked_generation"], 3)

    def test_a_held_receipt_names_the_generation(self) -> None:
        router = self.router(delivery_enabled=False)
        router.accept(self.wake())
        self.drain(router)
        self.assertEqual(self.done()["picked_generation"], 3)

    def test_a_receipt_with_no_target_has_no_generation(self) -> None:
        self.registry.rows = []
        router = self.router()
        router.accept(self.wake())
        self.drain(router)
        self.assertEqual(self.done()["picked_generation"], 0)

    def test_an_unchanged_chat_is_delivered(self) -> None:
        router = self.queued()
        self.drain(router)
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(self.done()["status"], "delivered")

    def test_the_check_comes_before_the_in_flight_mark(self) -> None:
        seen: list[tuple[str, bool]] = []
        real = self.registry

        def call(env, op, params=None, *, timeout=25):
            saved = json.loads(self.pending().read_text(encoding="utf-8"))
            seen.append((op, "in_flight" in saved))
            return real(env, op, params, timeout=timeout)

        router = self.router()
        router.accept(self.wake())
        with mock.patch.object(WR.registry_api, "call", call):
            self.drain(router)
        self.assertEqual(seen, [("history", False), ("resolve", False)])
        self.assertEqual(len(self.delivered), 1)

    def test_a_lettered_wake_on_a_closed_chat_is_final(self) -> None:
        router = self.lettered()
        self.registry.letters = {}
        self.registry.rows = []
        self.assert_released(router)

    def test_a_lettered_wake_on_a_chat_marked_closed_is_final(self) -> None:
        router = self.lettered()
        self.registry.letters = {"AA": row(SID_A, "AA", "x", generation=3, state="closed")}
        self.assert_released(router)

    def test_a_lettered_wake_on_a_new_generation_is_final(self) -> None:
        router = self.lettered()
        self.registry.letters = {"AA": row(SID_A, "AA", "x", generation=4)}
        self.assert_released(router)

    def test_a_lettered_wake_on_a_letter_now_held_by_another_is_final(self) -> None:
        router = self.lettered()
        self.registry.letters = {"AA": row(SID_B, "AA", "x", generation=3)}
        self.assert_released(router)

    def test_a_letterless_wake_on_a_closed_chat_is_picked_again(self) -> None:
        router = self.queued()
        self.registry.rows = [row(SID_B, "BB", "Replacement", generation=2)]
        self.drain(router)
        self.assertEqual(self.delivered[-1], (SID_B, "w-1"))
        receipt = self.done()
        self.assertEqual((receipt["session_id"], receipt["picked_generation"]), (SID_B, 2))
        self.assertEqual(receipt["status"], "delivered")

    def test_a_letterless_wake_on_a_new_generation_is_picked_again(self) -> None:
        router = self.queued()
        self.registry.rows = [row(SID_A, "AA", "Only chat", generation=4)]
        self.drain(router)
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(self.done()["picked_generation"], 4)

    def test_a_letterless_wake_with_nowhere_to_go_is_held(self) -> None:
        router = self.queued()
        self.registry.rows = []
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["reason"]), ("held", "no-live-session"))

    def test_picking_again_uses_no_attempt(self) -> None:
        router = self.queued()
        self.registry.rows = [row(SID_A, "AA", "Only chat", generation=4)]
        self.drain(router)
        receipt = self.done()
        self.assertEqual(receipt["picked_generation"], 4)  # it did go back through the pick
        self.assertEqual(receipt["attempts"], 2)  # the busy try and the delivery, and nothing more

    def test_two_registry_reads_that_disagree_cannot_spin(self) -> None:
        self.registry.letters = {"AA": row(SID_A, "AA", "Only chat", generation=9)}
        router = self.router(max_attempts=3)
        router.accept(self.wake())
        self.drain(router)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.done()["code"], WR.RELEASED)
        resolves = [op for op, _ in self.registry.calls if op == "resolve"]
        self.assertEqual(len(resolves), 4)

    def test_an_unreadable_registry_waits_and_does_not_deliver(self) -> None:
        router = self.queued()
        self.registry.down = True
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)
        saved = json.loads(self.pending().read_text(encoding="utf-8"))
        self.assertNotIn("in_flight", saved)
        self.assertEqual(saved["failures"], 1)
        self.assertEqual(saved["pick"]["target"]["session_id"], SID_A)
        self.registry.down = False
        self.clock.now += 2000
        self.drain(router)
        self.assertEqual(len(self.delivered), 2)
        self.assertEqual(self.done()["status"], "delivered")

    def test_an_unreadable_registry_is_bounded(self) -> None:
        router = self.queued(max_attempts=2)
        self.registry.down = True
        for _ in range(6):
            self.drain(router)
            self.clock.now += 2000
        self.assertEqual(len(self.delivered), 1)
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["code"]), ("dead", "registry-unavailable"))
        self.assertEqual(receipt["picked_generation"], 3)

    def test_a_replay_is_checked_too(self) -> None:
        self.queued()
        self.registry.rows = [row(SID_A, "AA", "Only chat", generation=4)]
        fresh = self.router()
        fresh.replay()
        self.drain(fresh)
        self.assertEqual(self.done()["picked_generation"], 4)


class Dedupe(Base):
    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "Only chat")]

    def test_a_repeat_while_pending_starts_nothing(self) -> None:
        router = self.router()
        first = router.accept(self.wake())
        second = router.accept(self.wake())
        self.assertEqual(first, second)
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)

    def test_a_repeat_after_the_receipt_answers_the_record(self) -> None:
        router = self.router()
        router.accept(self.wake())
        self.drain(router)
        status, answer = router.accept(self.wake())
        self.assertEqual(status, 200)
        self.assertEqual(answer["session_id"], SID_A)
        self.assertFalse(router.run_once())

    def test_a_repeat_after_a_restart_is_still_a_repeat(self) -> None:
        router = self.router()
        router.accept(self.wake())
        self.drain(router)
        status, _ = self.router().accept(self.wake())
        self.assertEqual(status, 200)

    def test_concurrent_posts_reserve_once(self) -> None:
        router = self.router()
        answers: list[tuple[int, dict]] = []
        threads = [
            threading.Thread(target=lambda: answers.append(router.accept(self.wake())))
            for _ in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len({a["receipt_id"] for _, a in answers}), 1)
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)


class Retries(Base):
    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "Only chat")]

    def run_all(self, router: WR.Router, rounds: int = 10) -> None:
        for _ in range(rounds):
            self.drain(router)
            self.clock.now += 2000

    def test_failed_tries_are_retried_then_delivered(self) -> None:
        self.results = [WR.Delivery("failed", code="x"), WR.Delivery("failed", code="x")]
        router = self.router()
        router.accept(self.wake())
        self.run_all(router)
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["attempts"]), ("delivered", 3))

    def test_a_retry_waits_its_backoff(self) -> None:
        self.results = [WR.Delivery("failed", code="x")]
        router = self.router(retry_backoff=[30])
        router.accept(self.wake())
        self.drain(router)
        self.assertEqual(len(self.delivered), 1)
        self.clock.now += 29
        self.assertFalse(router.run_once())
        self.clock.now += 2
        self.assertTrue(router.run_once())

    def test_retries_are_bounded_and_end_dead(self) -> None:
        self.results = [WR.Delivery("failed", "fake", code="x")] * 20
        router = self.router(max_attempts=3)
        router.accept(self.wake())
        self.run_all(router)
        receipt = self.done()
        self.assertEqual(len(self.delivered), 3)
        self.assertEqual((receipt["status"], receipt["delivered"]), ("dead", False))
        self.assertEqual(receipt["code"], "x")
        self.assertFalse(self.pending().exists())

    def test_refused_is_final_at_once(self) -> None:
        self.results = [WR.Delivery("refused", code="no-transcript")]
        router = self.router()
        router.accept(self.wake())
        self.run_all(router)
        self.assertEqual(len(self.delivered), 1)
        self.assertEqual(self.done()["status"], "dead")

    def test_an_exception_is_a_failed_try(self) -> None:
        calls: list[int] = []

        def explode(target, wake):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("no")
            return WR.Delivery("completed")

        router = self.router(deliver=explode)
        router.accept(self.wake())
        self.run_all(router)
        self.assertEqual(self.done()["status"], "delivered")

    def test_unreachable_uses_no_attempt(self) -> None:
        self.results = [WR.Delivery("unreachable", code="ssh")] * 8
        router = self.router(max_attempts=2, unreachable_retry_seconds=10)
        router.accept(self.wake())
        self.run_all(router, 12)
        self.assertEqual(self.done()["status"], "delivered")
        self.assertEqual(len(self.delivered), 9)

    def test_waiting_is_bounded_by_age(self) -> None:
        self.results = [WR.Delivery("unreachable", "ssh", code="ssh-unreachable")] * 50
        router = self.router(max_age_seconds=5000)
        router.accept(self.wake())
        self.run_all(router, 12)
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["code"]), ("dead", WR.EXPIRED))
        self.assertEqual(len(self.delivered), 3)  # tried at 0, 2000 and 4000;  not at 6000

    def test_a_wake_too_old_is_not_delivered_after_an_outage(self) -> None:
        router = self.router(max_age_seconds=100)
        router.accept(self.wake())
        self.clock.now += 101
        self.drain(router)
        self.assertEqual(self.delivered, [])
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["code"]), ("dead", WR.EXPIRED))
        self.assertIs(receipt["delivered"], False)

    def test_a_registry_outage_retries_the_pick(self) -> None:
        self.registry.down = True
        router = self.router()
        router.accept(self.wake())
        self.drain(router)
        self.assertTrue(self.pending().exists())
        self.registry.down = False
        self.clock.now += 2000
        self.drain(router)
        self.assertEqual(self.done()["status"], "delivered")

    def test_a_handover_with_no_proof_is_not_called_delivered(self) -> None:
        self.results = [WR.Delivery("sent", "socket", note="n")]
        router = self.router()
        router.accept(self.wake())
        self.drain(router)
        receipt = self.done()
        self.assertEqual(receipt["status"], "delivered")
        self.assertIsNone(receipt["delivered"])
        self.assertEqual((receipt["ack"], receipt["note"]), ("unknown", "n"))

    def test_delivery_disabled_holds_and_delivers_nothing(self) -> None:
        router = self.router(delivery_enabled=False)
        router.accept(self.wake())
        self.drain(router)
        receipt = self.done()
        self.assertEqual((receipt["status"], receipt["reason"]), ("held", "delivery-disabled"))
        self.assertEqual(receipt["session_id"], SID_A)
        self.assertEqual(self.delivered, [])

    def test_delivery_is_off_by_default(self) -> None:
        self.assertIs(WR.ROUTER_DEFAULTS["delivery_enabled"], False)
        self.assertIs(CW.DEFAULTS["delivery_enabled"], False)


class BusySessions(Base):
    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "Only chat")]

    def test_a_later_wake_waits_and_runs_after_in_order(self) -> None:
        release = threading.Event()
        started = threading.Event()
        order: list[str] = []
        overlap: list[bool] = []
        running: list[str] = []

        def deliver(target, wake):
            overlap.append(bool(running))
            running.append(wake.wake_id)
            order.append(wake.wake_id)
            started.set()
            if wake.wake_id == "w-1":
                release.wait(timeout=10)
            running.remove(wake.wake_id)
            return WR.Delivery("completed")

        router = self.router(deliver=deliver, workers=3)
        router.start()
        self.addCleanup(router.stop)
        router.accept(self.wake("w-1"))
        self.assertTrue(started.wait(timeout=10))
        self.clock.now += 1
        router.accept(self.wake("w-2"))
        self.clock.now += 1
        router.accept(self.wake("w-3"))
        time.sleep(0.5)
        self.assertEqual(order, ["w-1"])  # the others wait;  neither has failed
        self.assertTrue(self.pending("w-2").exists())
        release.set()
        deadline = time.time() + 15
        while time.time() < deadline and not (self.state / "done" / "w-3.json").exists():
            time.sleep(0.05)
        self.assertEqual(order, ["w-1", "w-2", "w-3"])
        self.assertEqual(overlap, [False, False, False])
        self.assertEqual(self.done("w-2")["status"], "delivered")
        self.assertEqual(self.done("w-2")["attempts"], 1)

    def test_a_later_wake_does_not_overtake_one_waiting_for_the_same_chat(self) -> None:
        self.results = [WR.Delivery("busy", code="session-locked")]
        router = self.router(busy_retry_seconds=30)
        router.accept(self.wake("w-1"))
        self.drain(router)
        self.clock.now += 1
        router.accept(self.wake("w-2"))
        self.drain(router)
        self.assertEqual(self.delivered, [(SID_A, "w-1")])
        saved = json.loads(self.pending("w-2").read_text(encoding="utf-8"))
        self.assertEqual(saved["pick"]["target"]["session_id"], SID_A)  # the pick is kept
        self.clock.now += 31
        self.drain(router)
        self.assertEqual([w for _, w in self.delivered], ["w-1", "w-1", "w-2"])

    def test_the_chat_stays_held_until_the_outcome_is_settled(self) -> None:
        router = self.router()
        settling = threading.Event()
        release = threading.Event()
        real = router._settle

        def slow(entry, pick, result):
            settling.set()
            release.wait(timeout=10)
            real(entry, pick, result)

        router._settle = slow
        router.accept(self.wake("w-1"))
        worker = threading.Thread(target=router.run_once, daemon=True)
        worker.start()
        self.assertTrue(settling.wait(timeout=10))
        self.clock.now += 1
        router.accept(self.wake("w-2"))
        router.run_once()  # it picks the chat and goes back behind w-1
        self.assertEqual(self.delivered, [(SID_A, "w-1")])
        self.assertIsNone(router._claim())  # and now that its chat is known, it is not claimed
        release.set()
        worker.join(timeout=10)
        self.drain(router)
        self.assertEqual([w for _, w in self.delivered], ["w-1", "w-2"])

    def test_a_wake_still_being_picked_is_not_overtaken(self) -> None:
        picking = threading.Event()
        release = threading.Event()
        real = self.registry
        first: list[int] = []

        def call(env, op, params=None, *, timeout=25):
            if not first:
                first.append(1)
                picking.set()
                release.wait(timeout=10)
            return real(env, op, params, timeout=timeout)

        router = self.router()
        router.accept(self.wake("w-1"))
        with mock.patch.object(WR.registry_api, "call", call):
            worker = threading.Thread(target=router.run_once, daemon=True)
            worker.start()
            self.assertTrue(picking.wait(timeout=10))
            self.clock.now += 1
            router.accept(self.wake("w-2"))
            router.run_once()
            self.assertEqual(self.delivered, [])
            release.set()
            worker.join(timeout=10)
        self.drain(router)
        self.assertEqual([w for _, w in self.delivered], ["w-1", "w-2"])

    def test_a_held_lock_never_fails_the_wake(self) -> None:
        self.results = [WR.Delivery("busy", "resume", code="session-locked")] * 8
        router = self.router(max_attempts=2, busy_retry_seconds=10)
        router.accept(self.wake())
        for _ in range(12):
            self.drain(router)
            self.clock.now += 20
        self.assertEqual(self.done()["status"], "delivered")

    def test_a_different_chat_is_not_held_up(self) -> None:
        self.registry.letters = {"BB": row(SID_B, "BB", "Other")}
        self.results = [WR.Delivery("busy", code="x")]
        router = self.router()
        router.accept(self.wake("w-1"))
        self.clock.now += 1
        router.accept(self.wake("w-2", letter="BB"))
        self.drain(router)
        self.assertEqual(self.done("w-2")["session_id"], SID_B)
        self.assertTrue(self.pending("w-1").exists())


class Http(Base):
    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "Only chat")]

    def serve(self, router: WR.Router) -> int:
        server = ThreadingHTTPServer(("127.0.0.1", 0), WR.make_handler(router))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def request(self, port, path="/wake/CLAUDE", body=None, key=None, method="POST"):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        headers = {"Authorization": "Bearer %s" % key} if key else {}
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (port, path), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def body(self, **over) -> dict:
        base = {
            "contract": WR.CONTRACT,
            "seat": "CLAUDE",
            "wake_id": "w-1",
            "message_id": 7,
            "topic": "unrelated",
            "channel": "wip",
            "zulip_link": "https://zulip.example/#narrow/near/7",
            "sender_full_name": "Someone",
            "excerpt": BODY_MARK,
        }
        base.update(over)
        return base

    def test_health_needs_no_key_and_says_nothing(self) -> None:
        port = self.serve(self.router())
        status, answer = self.request(port, "/health", method="GET")
        self.assertEqual((status, answer), (200, {"seat": "CLAUDE", "healthy": True}))

    def test_refusals(self) -> None:
        port = self.serve(self.router())
        self.assertEqual(self.request(port, body=self.body(), key="wrong")[0], 401)
        self.assertEqual(self.request(port, body=self.body())[0], 401)
        self.assertEqual(self.request(port, "/wake/ZC", self.body(), self.key)[0], 404)
        self.assertEqual(self.request(port, body=self.body(contract="x"), key=self.key)[0], 400)
        self.assertEqual(self.request(port, body=self.body(seat="ZC"), key=self.key)[0], 403)
        self.assertEqual(self.request(port, body=self.body(wake_id="../x"), key=self.key)[0], 400)
        self.assertEqual(self.request(port, body=b"not json", key=self.key)[0], 400)
        self.assertEqual(self.request(port, body=b"x" * (WR.MAX_BODY + 1), key=self.key)[0], 413)

    def test_a_loose_key_file_is_503(self) -> None:
        port = self.serve(self.router())
        os.chmod(self.key_file, 0o644)
        self.assertEqual(self.request(port, body=self.body(), key=self.key)[0], 503)

    def test_the_answer_does_not_wait_for_the_delivery(self) -> None:
        release = threading.Event()

        def slow(target, wake):
            release.wait(timeout=20)
            return WR.Delivery("completed", "fake", ack="confirmed")

        router = self.router(deliver=slow)
        router.start()
        self.addCleanup(router.stop)
        self.addCleanup(release.set)
        port = self.serve(router)
        began = time.monotonic()
        status, answer = self.request(port, body=self.body(), key=self.key)
        self.assertEqual(status, 202)
        self.assertLess(time.monotonic() - began, 5)
        self.assertEqual(answer["status"], "accepted")
        self.assertNotIn("ok", answer)  # the listener would read it as the hosted Worker's answer
        repeat = self.request(port, body=self.body(), key=self.key)
        self.assertEqual(repeat[0], 202)
        self.assertEqual(self.request(port, "/receipt/w-1", key=self.key, method="GET")[0], 200)
        release.set()
        deadline = time.time() + 15
        while time.time() < deadline and not (self.state / "done" / "w-1.json").exists():
            time.sleep(0.05)
        status, record = self.request(port, "/receipt/w-1", key=self.key, method="GET")
        self.assertEqual((status, record["status"]), (200, "delivered"))
        self.assertEqual(record["receipt_id"], answer["receipt_id"])

    def test_a_client_that_never_finishes_its_headers_is_cut_off(self) -> None:
        with mock.patch.object(WR, "REQUEST_TIMEOUT", 0.2):
            port = self.serve(self.router())
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"POST /wake/CLAUDE HTTP/1.1\r\nHost: x\r\n")
            self.assertEqual(client.recv(1024), b"")

    def test_a_receipt_needs_the_key(self) -> None:
        port = self.serve(self.router())
        self.assertEqual(self.request(port, "/receipt/w-1", method="GET")[0], 401)
        self.assertEqual(self.request(port, "/receipt/none", key=self.key, method="GET")[0], 404)

    def test_the_test_flag_answers_and_delivers_nothing(self) -> None:
        asked: list[int] = []
        router = self.router(consult=lambda c, w: asked.append(1))
        port = self.serve(router)
        status, answer = self.request(port, body=self.body(test=True), key=self.key)
        self.assertEqual(status, 200)
        self.assertIs(answer["test"], True)
        self.assertEqual(answer["session_id"], SID_A)
        self.assertEqual(answer["picked_letter"], "AA")
        self.assertEqual((answer["picked"], answer["reason"]), ("most-recent", "sole-live-chat"))
        self.assertTrue(answer["receipt_id"])
        self.assertIs(answer["delivered"], False)
        self.assertFalse(router.run_once())
        self.assertEqual(self.delivered, [])
        self.assertFalse(self.pending().exists())
        log = (self.state / "wakes.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertIs(json.loads(log[-1])["test"], True)
        # It reserved nothing:  a real wake with the same id is still accepted.
        self.assertEqual(self.request(port, body=self.body(), key=self.key)[0], 202)
        self.assertEqual(asked, [])

    def test_the_test_flag_reports_the_generation_and_touches_nothing(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "Only chat", generation=5)]
        router = self.router()
        port = self.serve(router)
        _, answer = self.request(port, body=self.body(test=True), key=self.key)
        self.assertEqual(answer["picked_generation"], 5)
        self.assertEqual([op for op, _ in self.registry.calls], ["history"])  # no check, no write
        self.assertEqual(router._entries, {})
        self.assertFalse(self.pending().exists())
        self.assertFalse((self.state / "done").exists())
        self.assertEqual(self.delivered, [])

    def test_a_test_wake_never_calls_the_consultant(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "One"), row(SID_B, "BB", "Two")]
        asked: list[int] = []
        port = self.serve(self.router(consult=lambda c, w: asked.append(1)))
        _, answer = self.request(port, body=self.body(test=True), key=self.key)
        self.assertEqual((answer["reason"], answer["session_id"]), ("needs-consultant", ""))
        self.assertEqual(asked, [])

    def test_only_a_real_true_is_a_test(self) -> None:
        port = self.serve(self.router())
        self.assertEqual(self.request(port, body=self.body(test="true"), key=self.key)[0], 202)

    def test_nothing_secret_is_kept(self) -> None:
        secret_topic = "ghp_" + "a1B2" * 9
        router = self.router()
        port = self.serve(router)
        self.request(port, body=self.body(topic=secret_topic), key=self.key)
        self.drain(router)
        kept = self.stderr.getvalue()
        for path in self.state.rglob("*"):
            if path.is_file() and path != self.key_file:
                kept += path.read_text(encoding="utf-8")
        self.assertNotIn(self.key, kept)
        self.assertNotIn(BODY_MARK, kept)
        self.assertNotIn(secret_topic, kept)
        self.assertIn(WR.secretscan.WITHHELD, kept)


class Config(Base):
    def write(self, text: str) -> Path:
        path = Path(self.tmp) / "config.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_missing_default_file_is_defaults(self) -> None:
        config = WR.load_config(Path(self.tmp) / "none.json", CW.DEFAULTS)
        self.assertEqual(config, CW.DEFAULTS)

    def test_a_named_missing_file_refuses(self) -> None:
        with self.assertRaises(SystemExit):
            WR.load_config(Path(self.tmp) / "none.json", CW.DEFAULTS, required=True)

    def test_bad_files_refuse(self) -> None:
        for text in (
            "{not json",
            "[]",
            '{"delivery_enable": true}',
            '{"delivery_enabled": 1}',
            '{"workers": 0}',
            '{"workers": true}',
            '{"retry_backoff": []}',
            '{"mac_ssh": 3}',
        ):
            with self.assertRaises(SystemExit, msg=text):
                WR.load_config(self.write(text), CW.DEFAULTS)

    def test_a_good_file_overlays(self) -> None:
        config = WR.load_config(self.write('{"delivery_enabled": true}'), CW.DEFAULTS)
        self.assertIs(config["delivery_enabled"], True)
        self.assertEqual(config["workers"], CW.DEFAULTS["workers"])


class FakeInbox:
    """A real unix socket, standing in for one chat's inbox."""

    def __init__(self, path: str, on_line=None) -> None:
        self.path = path
        self.received = b""
        self.done = threading.Event()
        self.on_line = on_line
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(4)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        connection, _ = self.server.accept()
        with connection:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                self.received += chunk
        if self.on_line:
            self.on_line(self.received)
        self.done.set()

    def close(self) -> None:
        self.server.close()


#: The Mac has no /proc, so a record's start time cannot be compared there and a live pid counts.
HAS_PROC = os.path.exists("/proc/self/stat")


def proc_start(pid: int) -> str:
    if not HAS_PROC:
        return ""
    with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
        return fh.read().rsplit(")", 1)[1].split()[19]


class ClaudeBase(Base):
    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.tmp) / "h"
        self.sessions = self.home / ".claude" / "sessions"
        self.sessions.mkdir(parents=True)
        self.project = self.home / ".claude" / "projects" / "-work"
        self.project.mkdir(parents=True)
        self.work = Path(self.tmp) / "work"
        self.work.mkdir()
        self.env = {
            "HOME": str(self.home),
            "CLAUDE_WAKE_STATE": str(self.state),
            "PATH": str(self.home / "bin"),
            "CLAUDE_CODE_MESSAGING_TOKEN": "tok-must-never-travel-" + self.key,
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "reg-must-never-travel-" + self.key,
        }
        binary = self.home / "bin" / "claude"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        self.runs: list[tuple[list, dict]] = []
        self.exit_code = 0
        self.cfg = {**CW.DEFAULTS, "confirm_seconds": 0, "delivery_enabled": True}
        self.w = self.wake(
            wake_id="w-claude",
            message_id=42,
            sender="Some One",
            channel="wip",
            topic="OPS-1048 letters",
            zulip_link="https://zulip.example/#narrow/near/42",
        )

    def runner(self, argv, **kwargs):
        self.runs.append((argv, kwargs))
        return SimpleNamespace(returncode=self.exit_code, stdout="", stderr="")

    def holder_file(self, sid=SID_A, pid=None, sock="", **over) -> Path:
        pid = pid or os.getpid()
        data = {
            "pid": pid,
            "sessionId": sid,
            "messagingSocketPath": sock,
            "status": "idle",
            "cwd": str(self.work),
            "entrypoint": "claude-desktop",
            "kind": "interactive",
        }
        data.update(over)
        if "procStart" not in data:
            data["procStart"] = proc_start(pid)
        path = self.sessions / ("%d.json" % pid)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def transcript(self, sid=SID_A, mode="default") -> Path:
        path = self.project / (sid + ".jsonl")
        lines = [
            json.dumps({"type": "custom-title", "sessionId": sid}),
            json.dumps({"type": "user", "cwd": str(self.work), "permissionMode": mode}, separators=(",", ":")),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def inbox(self, on_line=None) -> FakeInbox:
        inbox = FakeInbox(str(Path(self.tmp) / "inbox.sock"), on_line)
        self.addCleanup(inbox.close)
        return inbox

    def deliver(self):
        return CW.deliver_local(
            self.env, self.cfg, SID_A, self.w, runner=self.runner, sleep=lambda s: None
        )


class Socket(ClaudeBase):
    def test_a_live_chat_gets_one_user_line_and_no_auth(self) -> None:
        inbox = self.inbox()
        self.holder_file(sock=inbox.path)
        self.transcript()
        result = self.deliver()
        self.assertTrue(inbox.done.wait(timeout=5))
        lines = inbox.received.decode("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        message = json.loads(lines[0])
        self.assertEqual(sorted(message), ["message", "type"])
        self.assertEqual(message["type"], "user")
        self.assertEqual(message["message"]["role"], "user")
        self.assertNotIn(self.key, inbox.received.decode("utf-8"))
        self.assertEqual((result.outcome, result.route, result.ack), ("sent", "socket", "unknown"))
        self.assertEqual(self.runs, [])

    def test_the_connection_is_held_a_second_before_closing(self) -> None:
        inbox = self.inbox()
        held: list[float] = []
        CW.post_to_socket(inbox.path, "hello", sleep=held.append)
        self.assertEqual(held, [CW.SOCKET_HOLD_SECONDS])
        self.assertTrue(inbox.done.wait(timeout=5))
        self.assertTrue(inbox.received.endswith(b"\n"))

    def test_the_wake_text_is_metadata_only(self) -> None:
        text = CW.wake_text(self.w)
        self.assertIn("w-claude", text)
        self.assertIn("message_id 42", text)
        fields = json.loads(text.splitlines()[-1])
        self.assertEqual(
            fields,
            {
                "sender": "Some One",
                "channel": "wip",
                "topic": "OPS-1048 letters",
                "dm": False,
                "link": "https://zulip.example/#narrow/near/42",
            },
        )
        self.assertFalse(hasattr(self.w, "excerpt"))

    def test_a_key_file_is_never_read(self) -> None:
        # A .key file shaped like a live session record would be a holder if it were read.
        decoy = self.sessions / ("%d.abc123.key" % os.getpid())
        decoy.write_text(json.dumps({"pid": os.getpid(), "sessionId": SID_A}), encoding="utf-8")
        self.assertIsNone(CW.find_holder(self.env, SID_A))

    def test_the_transcript_can_confirm(self) -> None:
        transcript = self.transcript()

        def record(_data):
            with open(transcript, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "user", "note": "w-claude"}) + "\n")

        inbox = self.inbox(record)
        self.holder_file(sock=inbox.path)
        self.cfg["confirm_seconds"] = 5
        result = CW.deliver_local(self.env, self.cfg, SID_A, self.w, runner=self.runner)
        self.assertEqual((result.outcome, result.ack), ("sent", "confirmed"))

    def test_an_earlier_mention_of_the_id_is_not_proof(self) -> None:
        transcript = self.transcript()
        with open(transcript, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"note": "w-claude"}) + "\n")
        inbox = self.inbox()
        self.holder_file(sock=inbox.path)
        self.assertEqual(self.deliver().ack, "unknown")

    def test_a_bypassing_receiver_is_named_in_the_note(self) -> None:
        inbox = self.inbox()
        self.holder_file(sock=inbox.path)
        self.transcript(mode="bypassPermissions")
        result = self.deliver()
        self.assertEqual((result.ack, result.note), ("unknown", CW.BYPASS_NOTE))

    def test_accept_clears_the_bypass_note_but_not_the_unknown(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.write_text(json.dumps({"crossSessionInbound": "accept"}), encoding="utf-8")
        inbox = self.inbox()
        self.holder_file(sock=inbox.path)
        self.transcript(mode="bypassPermissions")
        result = self.deliver()
        self.assertEqual((result.ack, result.note), ("unknown", ""))

    def test_a_symlink_is_not_a_socket(self) -> None:
        inbox = self.inbox()
        link = Path(self.tmp) / "link.sock"
        link.symlink_to(inbox.path)
        self.holder_file(sock=str(link))
        result = self.deliver()
        self.assertEqual((result.outcome, result.code), ("failed", "socket-PermissionError"))


class Resume(ClaudeBase):
    def test_no_holder_resumes_in_the_chats_folder(self) -> None:
        self.transcript()
        result = self.deliver()
        self.assertEqual((result.outcome, result.route, result.ack), ("completed", "resume", "confirmed"))
        argv, kwargs = self.runs[0]
        self.assertEqual(argv[1:4], ["--resume", SID_A, "-p"])
        self.assertTrue(argv[4].startswith("[CLAUDE"))
        self.assertEqual(kwargs["cwd"], str(self.work))

    def test_the_child_gets_no_secrets(self) -> None:
        self.transcript()
        self.deliver()
        child = self.runs[0][1]["env"]
        self.assertEqual(
            sorted(child), ["AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "HOME", "PATH", "SIMPLE_TOOLS_ATTACH"]
        )
        self.assertNotIn(self.key, json.dumps(child))

    def test_the_child_gets_the_config_folder_the_chat_was_found_in(self) -> None:
        config = self.home / ".claude"
        self.env["CLAUDE_CONFIG_DIR"] = str(config)
        self.transcript()
        self.deliver()
        self.assertEqual(self.runs[0][1]["env"]["CLAUDE_CONFIG_DIR"], str(config))

    def test_a_resume_is_refused_while_a_process_holds_the_chat(self) -> None:
        self.holder_file(sock=str(Path(self.tmp) / "absent.sock"))
        self.transcript()
        result = self.deliver()
        self.assertEqual((result.outcome, result.route), ("failed", "socket"))
        self.assertEqual(self.runs, [])

    def test_a_holder_with_no_socket_is_still_a_holder(self) -> None:
        self.holder_file(sock="")
        self.transcript()
        result = self.deliver()
        self.assertEqual((result.outcome, result.code), ("failed", "holder-has-no-socket"))
        self.assertEqual(self.runs, [])

    def test_a_chat_opened_during_the_lock_is_not_resumed(self) -> None:
        inbox = self.inbox()
        self.transcript()
        real = CW.find_holder
        calls: list[int] = []

        def appears(env, sid):
            calls.append(1)
            if len(calls) == 2:
                self.holder_file(sock=inbox.path)
            return real(env, sid)

        with mock.patch.object(CW, "find_holder", appears):
            result = self.deliver()
        self.assertEqual((result.outcome, result.route), ("sent", "socket"))
        self.assertEqual(self.runs, [])

    def test_a_dead_process_does_not_hold(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.holder_file(pid=child.pid, procStart="1")
        self.transcript()
        self.assertEqual(self.deliver().route, "resume")

    @unittest.skipUnless(HAS_PROC, "a reused pid is only detectable through /proc")
    def test_a_reused_pid_does_not_hold(self) -> None:
        self.holder_file(procStart="1")
        self.transcript()
        self.assertEqual(self.deliver().route, "resume")

    def test_a_locked_session_is_busy(self) -> None:
        import fcntl

        self.transcript()
        folder = self.state / "claude-wake" / "locks"
        folder.mkdir(parents=True)
        with open(folder / ("session-%s.lock" % SID_A), "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            result = self.deliver()
        self.assertEqual((result.outcome, result.code), ("busy", "session-locked"))
        self.assertEqual(self.runs, [])

    def test_a_started_resume_that_exits_non_zero_is_final(self) -> None:
        self.transcript()
        for code in (3, -9):
            self.exit_code = code
            result = self.deliver()
            self.assertEqual((result.outcome, result.code), ("refused", "resume-exit-%d" % code))

    def test_a_resume_that_could_not_start_can_retry(self) -> None:
        self.transcript()

        def cannot_start(argv, **kwargs):
            raise FileNotFoundError(argv[0])

        result = CW.deliver_local(self.env, self.cfg, SID_A, self.w, runner=cannot_start)
        self.assertEqual((result.outcome, result.code), ("failed", "resume-FileNotFoundError"))

    def test_an_unclear_error_from_a_started_resume_is_final(self) -> None:
        self.transcript()

        def broken(argv, **kwargs):
            raise RuntimeError("after it started")

        result = CW.deliver_local(self.env, self.cfg, SID_A, self.w, runner=broken)
        self.assertEqual((result.outcome, result.code), ("refused", "resume-error-RuntimeError"))

    def test_the_router_runs_a_failing_resume_once(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "OPS-1048 letters")]
        self.transcript()
        self.exit_code = 1
        platform = CW.make_platform(self.env, self.cfg, runner=self.runner, sleep=lambda s: None)
        router = WR.Router(platform, self.env, self.cfg, self.state, self.key_file, clock=self.clock)
        router.accept(self.w)
        for _ in range(8):
            self.drain(router)
            self.clock.now += 2000
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.done("w-claude")["status"], "dead")

    def test_a_timed_out_resume_is_not_retried(self) -> None:
        self.transcript()

        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1)

        result = CW.deliver_local(self.env, self.cfg, SID_A, self.w, runner=slow)
        self.assertEqual((result.outcome, result.code), ("refused", "resume-timeout"))

    def test_no_transcript_and_a_retired_folder_are_final(self) -> None:
        self.assertEqual(self.deliver().code, "no-transcript")
        self.transcript()
        shutil.rmtree(self.work)
        self.assertEqual(self.deliver().code, "session-cwd-gone")

    def test_a_crafted_session_id_is_refused(self) -> None:
        result = CW.deliver_local(self.env, self.cfg, "--help", self.w, runner=self.runner)
        self.assertEqual((result.outcome, result.code), ("refused", "bad-session-id"))
        self.assertEqual(self.runs, [])


class HolderFailsClosed(ClaudeBase):
    """Not being sure that no process holds a chat must never start a resume."""

    def setUp(self) -> None:
        super().setUp()
        self.transcript()

    def assert_busy(self) -> None:
        result = self.deliver()
        self.assertEqual((result.outcome, result.code), ("busy", "holder-unknown"))
        self.assertEqual(self.runs, [])

    def record(self, text: str) -> Path:
        path = self.sessions / ("%d.json" % os.getpid())
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_torn_record_of_a_live_pid(self) -> None:
        self.record('{"pid": 1, "sessionId": "aaaa')
        self.assert_busy()

    def test_an_oversized_record_of_a_live_pid(self) -> None:
        self.record(" " * (CW.SESSION_FILE_BYTES + 1))
        self.assert_busy()

    def test_a_record_that_cannot_be_read(self) -> None:
        self.holder_file()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            self.assert_busy()

    def test_a_record_with_no_session_id(self) -> None:
        self.record(json.dumps({"pid": os.getpid()}))
        self.assert_busy()

    def test_this_chats_record_with_no_usable_pid(self) -> None:
        self.record(json.dumps({"pid": "x", "sessionId": SID_A}))
        self.assert_busy()

    def test_an_unlistable_folder(self) -> None:
        with mock.patch.object(CW.os, "listdir", side_effect=PermissionError):
            self.assert_busy()

    def test_a_missing_folder(self) -> None:
        shutil.rmtree(self.sessions)
        self.assert_busy()

    def test_a_folder_whose_files_cannot_be_examined(self) -> None:
        self.record("{}")
        with mock.patch.object(CW.os, "lstat", side_effect=PermissionError):
            self.assert_busy()

    def test_a_torn_leftover_of_a_dead_pid_blocks_nothing(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        (self.sessions / ("%d.json" % child.pid)).write_text("{torn", encoding="utf-8")
        self.assertEqual(self.deliver().route, "resume")

    def test_a_record_gone_while_reading_is_no_doubt(self) -> None:
        self.record("{}")
        with mock.patch.object(CW, "_read_record", side_effect=FileNotFoundError):
            self.assertEqual(self.deliver().route, "resume")

    def test_a_live_holder_is_an_answer_whatever_else_is_torn(self) -> None:
        inbox = self.inbox()
        self.holder_file(sock=inbox.path)
        (self.sessions / ("%d.json" % os.getppid())).write_text("{torn", encoding="utf-8")
        self.assertEqual(self.deliver().route, "socket")

    def test_a_holder_with_a_socket_is_preferred(self) -> None:
        for bare, wired in ((os.getpid(), os.getppid()), (os.getppid(), os.getpid())):
            self.holder_file(pid=bare, sock="")
            self.holder_file(pid=wired, sock="/tmp/wired.sock")
            self.assertEqual(CW.find_holder(self.env, SID_A), CW.Holder(wired, "/tmp/wired.sock"))


class Remote(ClaudeBase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg["mac_ssh"] = "mac-host"
        self.answer = SimpleNamespace(
            returncode=0, stdout=json.dumps({"outcome": "sent", "route": "socket"}) + "\n"
        )

    def remote(self, argv, **kwargs):
        self.runs.append((argv, kwargs))
        return self.answer

    def deliver_remote(self):
        return CW.deliver_remote(self.env, self.cfg, SID_A, self.w, runner=self.remote)

    def test_the_command_is_fixed_and_the_wake_rides_stdin(self) -> None:
        result = self.deliver_remote()
        self.assertEqual(self.runs[0][0][-2:], ["mac-host", "true"])  # the preflight goes first
        argv, kwargs = self.runs[1]
        self.assertEqual(argv[-1], CW.REMOTE_COMMAND)
        self.assertEqual(argv[-2], "mac-host")
        self.assertNotIn("OPS-1048", " ".join(argv))
        self.assertEqual(json.loads(kwargs["input"])["session_id"], SID_A)
        self.assertEqual((result.outcome, result.route, result.ack), ("sent", "ssh:socket", "unknown"))

    def test_a_sleeping_mac_is_unreachable_and_nothing_is_sent(self) -> None:
        self.answer = SimpleNamespace(returncode=255, stdout="")
        self.assertEqual(self.deliver_remote().outcome, "unreachable")
        self.assertEqual(len(self.runs), 1)

    def scripted(self, *answers):
        """A runner whose calls answer in turn;  an exception in the list is raised."""
        queue = list(answers)

        def run(argv, **kwargs):
            self.runs.append((argv, kwargs))
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        return run

    def test_a_255_after_a_good_preflight_is_final(self) -> None:
        good = SimpleNamespace(returncode=0, stdout="")
        dropped = SimpleNamespace(returncode=255, stdout="")
        runner = self.scripted(good, dropped)
        result = CW.deliver_remote(self.env, self.cfg, SID_A, self.w, runner=runner)
        self.assertEqual((result.outcome, result.code), ("refused", "ssh-dropped"))

    def test_a_preflight_that_times_out_is_unreachable(self) -> None:
        runner = self.scripted(subprocess.TimeoutExpired("ssh", 1))
        result = CW.deliver_remote(self.env, self.cfg, SID_A, self.w, runner=runner)
        self.assertEqual((result.outcome, len(self.runs)), ("unreachable", 1))

    def test_a_bad_answer_is_final(self) -> None:
        good = SimpleNamespace(returncode=0, stdout="")
        bad = SimpleNamespace(returncode=1, stdout="not json\n")
        runner = self.scripted(good, bad)
        result = CW.deliver_remote(self.env, self.cfg, SID_A, self.w, runner=runner)
        self.assertEqual((result.outcome, result.code), ("refused", "remote-exit-1"))

    def test_an_unnamed_or_flag_shaped_destination_is_refused(self) -> None:
        for dest in ("", "-oProxyCommand=x"):
            self.cfg["mac_ssh"] = dest
            self.assertEqual(self.deliver_remote().code, "no-ssh-destination")
        self.assertEqual(len(self.runs), 0)

    def test_routing_by_host(self) -> None:
        deliver = CW.make_deliver(self.env, self.cfg, runner=self.remote)
        self.assertEqual(deliver(WR.Target(SID_A, host="mac"), self.w).route, "ssh:socket")
        self.assertEqual(deliver(WR.Target(SID_A, host="cloud"), self.w).code, "unknown-host")

    def test_the_remote_side_round_trip(self) -> None:
        self.transcript()
        request = json.dumps({"session_id": SID_A, "wake": WR.asdict(self.w)})
        out = io.StringIO()
        with mock.patch.object(subprocess, "run", self.runner):
            with mock.patch.object(CW, "deliver_local", wraps=CW.deliver_local) as spy:
                code = CW.wake_deliver_main(io.StringIO(request), out, self.env)
        self.assertEqual(code, 0)
        self.assertEqual(spy.call_args.args[2], SID_A)
        self.assertIn(json.loads(out.getvalue())["outcome"], WR.OUTCOMES)

    def test_the_remote_side_refuses_a_bad_request(self) -> None:
        for text in ("not json", "{}", json.dumps({"session_id": SID_A, "wake": {"wake_id": "../x"}})):
            out = io.StringIO()
            self.assertEqual(CW.wake_deliver_main(io.StringIO(text), out, self.env), 2)
            self.assertEqual(json.loads(out.getvalue())["code"], "bad-request")


class Exclude(ClaudeBase):
    def test_a_non_interactive_local_session_is_excluded(self) -> None:
        self.holder_file(kind="print")
        exclude = CW.exclude_non_interactive(self.env, "server")
        self.assertEqual(exclude([WR.Target(SID_A, host="server")]), {SID_A})
        self.assertEqual(exclude([WR.Target(SID_A, host="mac")]), set())

    def test_a_stale_record_excludes_nothing(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.holder_file(pid=child.pid, procStart="1", kind="print")
        exclude = CW.exclude_non_interactive(self.env, "server")
        self.assertEqual(exclude([WR.Target(SID_A, host="server")]), set())

    @unittest.skipUnless(HAS_PROC, "a reused pid is only detectable through /proc")
    def test_a_reused_pid_excludes_nothing(self) -> None:
        self.holder_file(procStart="1", kind="print")
        exclude = CW.exclude_non_interactive(self.env, "server")
        self.assertEqual(exclude([WR.Target(SID_A, host="server")]), set())


class EndToEnd(ClaudeBase):
    def test_a_wake_reaches_the_inbox_through_the_router(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "OPS-1048 letters")]
        inbox = self.inbox()
        self.holder_file(sock=inbox.path)
        self.transcript()
        platform = CW.make_platform(self.env, self.cfg, runner=self.runner, sleep=lambda s: None)
        router = WR.Router(platform, self.env, self.cfg, self.state, self.key_file, clock=self.clock)
        payload = {
            "wake_id": "w-e2e",
            "seat": "CLAUDE",
            "topic": "OPS-1048 letters",
            "excerpt": BODY_MARK,
        }
        status, _ = router.accept(WR.wake_from_payload(payload, "Claude"))
        self.assertEqual(status, 202)
        self.drain(router)
        self.assertTrue(inbox.done.wait(timeout=5))
        self.assertNotIn(BODY_MARK, inbox.received.decode("utf-8"))
        receipt = self.done("w-e2e")
        self.assertEqual((receipt["route"], receipt["delivered"]), ("socket", None))
        self.assertIsNotNone(re.search("w-e2e", inbox.received.decode("utf-8")))


if __name__ == "__main__":
    unittest.main()
