"""The Mac runner for the Operator's notes queue (AFC-486, docs/protocols/operator-notes.md, "The Mac Runner"):  the
claim, the second check, the run, the state file that survives a crash, the lock, and shipping the action log.

The client, the script finder and the runner are fakes, and the home folder is a temp dir:  no network, no osascript.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_notes_runner
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import notes as N
from agent_sync import notes_runner as RUN

SESSION = "11112222-3333-4444-5555-666677778888"
SCRIPT = Path("/fake/apple-notes-coding.sh")
MASTER = "⭐️ Background Jobs Master List"


def req(action, identity="CODEX", rid="requestid-0001", **kwargs):
    defaults: dict[str, Any] = {}
    if action == "create":
        defaults = {"title": "[AFC, Codex] Runner Check", "markdown": "PRIVATE BODY WORDS", "session": SESSION}
    elif action == "update":
        defaults = {"title": MASTER, "markdown": "PRIVATE BODY WORDS", "session": SESSION}
    elif action != "list":
        defaults = {"title": MASTER}
    defaults.update(kwargs)
    return N.make_request(action, identity, request_id=rid, **defaults)


class FakeClient:
    """The RUNNER principal's calls.  `report` and `send_log` look at the state file when they are called, so a test
    can say what had been recorded by then."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.claims: list[dict[str, Any] | None] = []
        self.claim_waits: list[int] = []
        self.reports: list[dict[str, Any]] = []
        self.batches: list[list[dict[str, Any]]] = []
        self.fail_log_at: int | None = None  # the 1-based send_log call that fails
        self.log_calls = 0
        self.report_statuses: list[int] = []  # what the next report calls answer;  200 once empty

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def claim(self, wait):
        self.claim_waits.append(wait)
        return self.claims.pop(0) if self.claims else None

    def report(self, seat, request_id, claim, *, result=None, error=None):
        self.reports.append(
            {
                "seat": seat,
                "request_id": request_id,
                "claim": claim,
                "result": result,
                "error": error,
                "inflight_then": self.state().get("inflight", {}).get("%s/%s" % (seat, request_id)),
                "state_text_then": self.state_path.read_text() if self.state_path.exists() else "",
            }
        )
        return self.report_statuses.pop(0) if self.report_statuses else 200

    def send_log(self, lines):
        self.log_calls += 1
        if self.fail_log_at is not None and self.log_calls == self.fail_log_at:
            raise N.NotesError("unavailable", "the Operator notes API could not be reached (URLError)")
        self.batches.append(list(lines))
        return len(lines)


class RunnerHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.log_path = self.home / "logs" / "apple-notes-actions.jsonl"
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin", "APPLE_NOTES_ACTION_LOG": str(self.log_path)}
        self.state_path = self.home / RUN.STATE_REL
        self.client = FakeClient(self.state_path)
        self.runs: list[dict[str, Any]] = []
        self.answer: Any = {"status": "done", "note_id": "x-coredata://n1"}
        self.runner = self.make()

    def make(self, **kwargs):
        return RUN.Runner(
            self.client,
            env=self.env,
            home=self.home,
            script_finder=kwargs.pop("script_finder", lambda env, home: SCRIPT),
            run=self.fake_run,
            clock=lambda: 1761170400.0,
            **kwargs,
        )

    def fake_run(self, request, script, **kwargs):
        self.runs.append({"request": dict(request), "script": script, **kwargs})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer)

    def claim(self, request, seat="CODEX", claim="claim-1"):
        self.client.claims.append({"claim": claim, "seat": seat, "request_id": request["request_id"], "request": request})


class OnceTests(RunnerHarness):
    def test_nothing_queued_is_a_quiet_pass(self):
        self.assertFalse(self.runner.once(7))
        self.assertEqual(self.client.claim_waits, [7])
        self.assertEqual(self.client.state()["last_poll_at"], "2025-10-22T22:00:00Z")
        self.assertEqual((self.runs, self.client.reports), ([], []))

    def test_a_write_is_checked_run_for_its_requester_and_reported(self):
        self.claim(req("update"))
        self.assertTrue(self.runner.once())
        (run,) = self.runs
        self.assertEqual((run["script"], run["caller"], run["requester"]), (SCRIPT, "runner", "CODEX"))
        self.assertEqual(run["request"], req("update"))
        self.assertEqual(run["env"], self.env)
        (report,) = self.client.reports
        self.assertEqual((report["seat"], report["request_id"], report["claim"]), ("CODEX", "requestid-0001", "claim-1"))
        self.assertEqual((report["result"], report["error"]), ({"status": "done", "note_id": "x-coredata://n1"}, None))
        # recorded before it was reported, and cleared after
        self.assertEqual(report["inflight_then"]["result"], {"status": "done", "note_id": "x-coredata://n1"})
        self.assertEqual(report["inflight_then"]["action"], "update")
        self.assertNotIn("request", report["inflight_then"], "a write's request (and body) is never kept")
        self.assertNotIn("PRIVATE BODY", report["state_text_then"])
        state = self.client.state()
        self.assertEqual(state["inflight"], {})
        self.assertEqual((state["handled"], state["last_ok_at"]), (1, "2025-10-22T22:00:00Z"))
        self.assertEqual(stat.S_IMODE(os.stat(self.state_path).st_mode), 0o600)

    def test_a_create_carries_the_requester_and_a_pin_does_not(self):
        self.claim(req("create"), claim="c1")
        self.claim(req("pin", rid="requestid-0002"), claim="c2")
        self.claim(req("read", rid="requestid-0003"), claim="c3")
        for _ in range(3):
            self.runner.once()
        self.assertEqual([r["requester"] for r in self.runs], ["CODEX", None, None])
        self.assertEqual([r["request"]["action"] for r in self.runs], ["create", "pin", "read"])

    def test_the_runner_checks_again_and_never_runs_what_the_seat_may_not_send(self):
        bad = {
            "theirs": req("create", title="[AFC, Claude] Not Codex"),
            "identity": req("pin", identity="CLAUDE"),
            "no session": dict(req("create"), session=None),
            "unknown field": dict(req("pin"), transport="relay"),
            "old version": dict(req("pin"), v=1),
        }
        for name, request in bad.items():
            request = {k: v for k, v in request.items() if v is not None}
            with self.subTest(name):
                self.client.reports.clear()
                self.claim(request)
                self.assertTrue(self.runner.once())
                (report,) = self.client.reports
                self.assertIsNone(report["result"])
                self.assertEqual(report["error"]["code"], "invalid_argument")
                self.assertLessEqual(len(report["error"]["message"]), 400)
        self.assertEqual(self.runs, [], "nothing ran")

    def test_a_refusal_from_the_script_is_reported_as_the_error(self):
        self.answer = N.NotesError("not_found", "no note with that title in the Coding folder")
        self.claim(req("pin"))
        self.runner.once()
        self.assertEqual(self.client.reports[0]["error"], {"code": "not_found", "message": "no note with that title in the Coding folder"})

    def test_a_mac_that_cannot_run_the_script_writes_nothing(self):
        runner = self.make(script_finder=lambda env, home: None)
        self.claim(req("update"))
        runner.once()
        self.assertEqual(self.client.reports[0]["error"]["code"], "unavailable")
        self.assertEqual(self.runs, [])

    def test_a_claim_with_a_bad_shape_is_passed_over(self):
        # A claim that is not text, a seat or request_id outside the Operator's own patterns, or one that is not the
        # request's:  each is passed over, and recorded for the doctor with none of the values it was handed.
        pin = req("pin")
        for claim, seat, rid in (
            (5, "CODEX", pin["request_id"]),
            ("claim-1", "../x", pin["request_id"]),
            ("claim-1", "CODEX", "../../escape"),
            ("claim-1", "CODEX", "other-request-01"),
        ):
            with self.subTest(seat=seat, rid=rid):
                self.client.claims.append({"claim": claim, "seat": seat, "request_id": rid, "request": pin})
                self.assertFalse(self.runner.once())
                error = self.client.state()["last_error"]
                self.assertIn("malformed", error)
                for value in (seat, rid):
                    self.assertNotIn(value, error)
        self.assertEqual((self.runs, self.client.reports), ([], []))
        self.assertEqual(self.client.state().get("inflight", {}), {})

    def test_a_claimed_write_whose_text_looks_like_a_secret_is_refused_unrun(self):
        # The runner's second check scans a write's text as the Operator did, so a queue that let one through (an older
        # Operator) still never has it written.  The scan comes first:  a title that also names another seat's agent is
        # refused by the scan, never by the agent check, whose message names the agent.
        leak = "token " + "ghp_" + "EXAMPLE0" * 5
        for title in ("[AFC, Codex] Runner Check", "[AFC, Claude] Not Codex Own"):
            with self.subTest(title=title):
                self.client.reports.clear()
                request = req("create")
                request["title"] = title
                request["markdown"] = leak  # as a queue that never scanned it would hand it out
                self.claim(request)
                self.assertTrue(self.runner.once())
                self.assertEqual(self.runs, [])
                error = self.client.reports[0]["error"]
                self.assertEqual(error["code"], "invalid_argument")
                self.assertIn("looks like it contains", error["message"])
                self.assertNotIn(leak, json.dumps(self.client.reports))

    def test_a_read_is_kept_whole_while_it_runs_so_a_crash_can_run_it_again(self):
        def crash(request, script, **kwargs):
            raise KeyboardInterrupt()

        runner = RUN.Runner(self.client, env=self.env, home=self.home, script_finder=lambda e, h: SCRIPT, run=crash)
        self.claim(req("read"))
        with self.assertRaises(KeyboardInterrupt):
            runner.once()
        kept = self.client.state()["inflight"]["CODEX/requestid-0001"]
        self.assertEqual(kept["request"], req("read"))
        self.assertNotIn("result", kept)
        self.answer = {"note": {"title": MASTER, "body": "<div>x</div>"}}
        self.runner.recover()
        self.assertEqual(self.client.reports[0]["result"], self.answer)
        self.assertEqual(self.client.state()["inflight"], {})


class RecoverTests(RunnerHarness):
    def seed(self, **entries):
        state = {"inflight": entries}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state))

    def test_a_recorded_outcome_is_reported_again_and_a_write_without_one_is_unknown(self):
        self.seed(
            **{
                "CODEX/done-request-01": {"seat": "CODEX", "request_id": "done-request-01", "claim": "c1", "action": "create", "result": {"status": "done", "note_id": "n1"}},
                "CODEX/fail-request-01": {"seat": "CODEX", "request_id": "fail-request-01", "claim": "c2", "action": "pin", "error": {"code": "not_found", "message": "m"}},
                "CODEX/lost-request-01": {"seat": "CODEX", "request_id": "lost-request-01", "claim": "c3", "action": "update"},
                "CODEX/read-request-01": {"seat": "CODEX", "request_id": "read-request-01", "claim": "c4", "action": "read", "request": req("read", rid="read-request-01")},
                "junk": "not an entry",
            }
        )
        self.answer = {"note": {"title": MASTER, "body": "b"}}
        self.runner.recover()
        got = {r["request_id"]: (r["claim"], r["result"], r["error"]) for r in self.client.reports}
        self.assertEqual(got["done-request-01"], ("c1", {"status": "done", "note_id": "n1"}, None))
        self.assertEqual(got["fail-request-01"], ("c2", None, {"code": "not_found", "message": "m"}))
        self.assertEqual(got["lost-request-01"][:2], ("c3", None))
        self.assertEqual(got["lost-request-01"][2]["code"], "outcome_unknown")
        self.assertIn("may or may not have been written", got["lost-request-01"][2]["message"])
        self.assertEqual(got["read-request-01"], ("c4", {"note": {"title": MASTER, "body": "b"}}, None))
        self.assertEqual([r["request"]["action"] for r in self.runs], ["read"], "only the read ran again")
        self.assertEqual(self.client.state()["inflight"], {})

    def test_a_read_that_fails_to_run_again_is_reported_by_its_type_and_let_go(self):
        self.seed(
            **{
                "CODEX/read-request-01": {"seat": "CODEX", "request_id": "read-request-01", "claim": "c1", "action": "read", "request": req("read", rid="read-request-01")},
                "CODEX/pin-request-001": {"seat": "CODEX", "request_id": "pin-request-001", "claim": "c2", "action": "pin", "result": {"status": "done"}},
            }
        )
        self.answer = ValueError("secret-looking text")  # running the read again fails in a way nobody planned for
        self.runner.recover()  # _execute never raises:  the read gets a report, so its entry never blocks a claim
        got = {r["request_id"]: r["error"] for r in self.client.reports}
        self.assertEqual(got["read-request-01"]["code"], "internal")
        self.assertIn("ValueError", got["read-request-01"]["message"])
        self.assertNotIn("secret-looking", got["read-request-01"]["message"])  # the type name only
        self.assertIsNone(got["pin-request-001"])
        self.assertEqual(self.client.state()["inflight"], {})

    def test_an_unexpected_failure_in_a_create_is_outcome_unknown(self):
        self.answer = RuntimeError("boom")
        self.claim(req("create"))
        self.assertTrue(self.runner.once(0))
        self.assertEqual(self.client.reports[0]["error"]["code"], "outcome_unknown")
        self.assertIn("may or may not have been written", self.client.reports[0]["error"]["message"])

    def test_a_recovery_entry_with_malformed_ids_is_dropped_unreported(self):
        self.seed(
            **{
                "a": {"seat": "../x", "request_id": "requestid-0001", "claim": "c1", "action": "pin", "result": {"status": "done"}},
                "b": {"seat": "CODEX", "request_id": "../../escape", "claim": "c2", "action": "pin"},
                "c": {"seat": "CODEX", "request_id": "requestid-0003", "claim": "", "action": "pin"},
            }
        )
        self.runner.recover()
        self.assertEqual((self.client.reports, self.runs), ([], []))
        self.assertEqual(self.client.state()["inflight"], {})

    def test_a_failed_recovery_report_keeps_only_its_own_entry_and_the_rest_still_go_out(self):
        self.seed(
            **{
                "CODEX/first-request-1": {"seat": "CODEX", "request_id": "first-request-1", "claim": "c1", "action": "pin", "result": {"status": "done"}},
                "CODEX/second-request1": {"seat": "CODEX", "request_id": "second-request1", "claim": "c2", "action": "pin", "result": {"status": "done"}},
            }
        )
        real = self.client.report

        def report(seat, request_id, claim, **outcome):
            if request_id == "first-request-1":
                # Any failure, not only a NotesError (a bug, say), keeps to its own entry.
                raise ValueError("unexpected")
            return real(seat, request_id, claim, **outcome)

        self.client.report = report
        # Raised after the rest, as a NotesError the loop backs off on (never one that ends the runner).
        with self.assertRaises(N.NotesError) as caught:
            self.runner.recover()
        self.assertIn("ValueError", caught.exception.message)
        self.assertEqual([r["request_id"] for r in self.client.reports], ["second-request1"])
        self.assertEqual(list(self.client.state()["inflight"]), ["CODEX/first-request-1"])

    def test_once_recovers_first(self):
        self.seed(**{"CODEX/lost-request-01": {"seat": "CODEX", "request_id": "lost-request-01", "claim": "c3", "action": "create"}})
        self.assertFalse(self.runner.once())
        self.assertEqual(self.client.reports[0]["error"]["code"], "outcome_unknown")


class LockTests(RunnerHarness):
    def test_one_runner_at_a_time(self):
        self.runner.acquire()
        self.addCleanup(self.runner.release)
        second = self.make()
        with self.assertRaises(RUN.AlreadyRunning):
            second.acquire()
        self.runner.release()
        second.acquire()
        second.release()
        lock = self.state_path.parent / RUN.LOCK_NAME
        self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state_path.parent).st_mode), 0o700)

    def test_a_lock_failure_other_than_held_is_raised_as_itself(self):
        with mock.patch.object(RUN.fcntl, "flock", side_effect=PermissionError("not permitted")):
            with self.assertRaises(PermissionError):
                self.make().acquire()


class ShipLogTests(RunnerHarness):
    def line(self, n):
        return json.dumps({"id": "e%04d" % n, "ts": "2026-10-22T22:00:00Z", "mode": "create", "seat": "CLAUDE", "body": "never shipped"}) + "\n"

    def write(self, *numbers, path=None, raw=""):
        target = path or self.log_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as fh:
            fh.write("".join(self.line(n) for n in numbers) + raw)

    def shipped(self):
        return [entry["id"] for batch in self.client.batches for entry in batch]

    def test_whole_lines_go_in_batches_and_nothing_is_sent_twice(self):
        self.write(*range(1, 451))
        self.assertEqual(self.runner.ship_log(), 450)
        self.assertEqual([len(b) for b in self.client.batches], [200, 200, 50])
        self.assertEqual(self.shipped(), ["e%04d" % n for n in range(1, 451)])
        self.assertTrue(all("body" not in entry for batch in self.client.batches for entry in batch), "only known fields")
        self.assertEqual(self.runner.ship_log(), 0)
        cursor = self.client.state()["log"]
        self.assertEqual((cursor["inode"], cursor["offset"]), (os.stat(self.log_path).st_ino, os.path.getsize(self.log_path)))

    def test_a_torn_last_line_waits_for_the_next_pass(self):
        torn = self.line(3)
        self.write(1, 2, raw=torn[:20])
        self.assertEqual(self.runner.ship_log(), 2)
        self.assertEqual(self.client.state()["log"]["offset"], len(self.line(1)) + len(self.line(2)))
        with open(self.log_path, "a") as fh:
            fh.write(torn[20:])
        self.assertEqual(self.runner.ship_log(), 1)
        self.assertEqual(self.shipped(), ["e0001", "e0002", "e0003"])

    def test_lines_that_are_not_entries_are_passed_over_but_not_read_again(self):
        self.write(1, raw="not json\n[1]\n")
        self.write(2)
        self.assertEqual(self.runner.ship_log(), 2)
        self.assertEqual(self.client.state()["log"]["offset"], os.path.getsize(self.log_path))
        self.assertEqual(self.runner.ship_log(), 0)

    def test_a_rotation_is_followed_the_old_file_first(self):
        self.write(1, 2, 3)
        self.assertEqual(self.runner.ship_log(), 3)
        self.write(4, 5)  # written after the last pass, before the script rotated the file
        os.rename(self.log_path, self.log_path.with_name(self.log_path.name + ".1"))
        self.write(6, 7)
        self.assertEqual(self.runner.ship_log(), 4)
        self.assertEqual(self.shipped(), ["e%04d" % n for n in range(1, 8)])
        cursor = self.client.state()["log"]
        self.assertEqual((cursor["inode"], cursor["offset"]), (os.stat(self.log_path).st_ino, os.path.getsize(self.log_path)))

    def test_two_rotations_while_the_runner_was_off_are_followed_oldest_first(self):
        self.write(1, 2)
        self.assertEqual(self.runner.ship_log(), 2)
        self.write(3)  # the rest of the file the cursor is in, which then rotates twice
        dot = lambda n: self.log_path.with_name("%s.%d" % (self.log_path.name, n))  # noqa: E731
        os.rename(self.log_path, dot(1))
        self.write(4, 5)
        os.rename(dot(1), dot(2))
        os.rename(self.log_path, dot(1))
        self.write(6)
        self.assertEqual(self.runner.ship_log(), 4)
        self.assertEqual(self.shipped(), ["e%04d" % n for n in range(1, 7)])

    def test_a_cursor_file_rotated_out_of_reach_sends_every_copy_still_kept(self):
        self.write(1)
        self.assertEqual(self.runner.ship_log(), 1)
        # The cursor's file is gone from every copy (moved aside, not deleted, so no new file can reuse its inode).
        os.rename(self.log_path, self.log_path.with_name("rotated-away"))
        self.write(2, path=self.log_path.with_name(self.log_path.name + ".2"))
        self.write(3, path=self.log_path.with_name(self.log_path.name + ".1"))
        self.write(4)
        self.assertEqual(self.runner.ship_log(), 3)
        self.assertEqual(self.shipped(), ["e%04d" % n for n in range(1, 5)])

    def test_a_replaced_file_is_resumed_just_after_the_last_line_sent(self):
        # PR #876 review:  a replaced file sent whole could hold more lines than the Operator's id window, so the
        # runner looks up the id of the last line it sent and goes on just after it.
        self.write(1, 2, 3)
        self.assertEqual(self.runner.ship_log(), 3)
        self.assertEqual(self.client.state()["log"]["id"], "e0003")
        os.rename(self.log_path, self.log_path.with_name("replaced-away"))
        self.write(1, 2, 3, 4, path=self.log_path.with_name(self.log_path.name + ".1"))
        self.write(5)
        self.client.batches.clear()
        self.assertEqual(self.runner.ship_log(), 2)
        self.assertEqual(self.shipped(), ["e0004", "e0005"])

    def test_a_file_rewritten_in_place_is_resumed_by_the_id_not_the_offset(self):
        # Same inode and a long enough file, but the line before the offset is another one:  the offset is not trusted.
        self.write(1, 2)
        self.runner.ship_log()
        with open(self.log_path, "w") as fh:  # truncated and written again in place:  the inode stays
            fh.write(self.line(5) + self.line(1) + self.line(2) + self.line(6))
        self.client.batches.clear()
        self.assertEqual(self.runner.ship_log(), 1)
        self.assertEqual(self.shipped(), ["e0006"])

    def test_with_no_cursor_every_kept_copy_is_sent_oldest_first(self):
        # A first run, or a state file that was lost:  the rotated copies go too, and the Operator drops the lines it
        # already holds by their ids.
        dot = lambda n: self.log_path.with_name("%s.%d" % (self.log_path.name, n))  # noqa: E731
        self.write(1, 2, path=dot(3))
        self.write(3, path=dot(1))
        self.write(4, 5)
        self.assertEqual(self.runner.ship_log(), 5)
        self.assertEqual(self.shipped(), ["e0001", "e0002", "e0003", "e0004", "e0005"])
        self.assertEqual(self.runner.ship_log(), 0)

    def test_a_lost_place_never_skips_a_line_whatever_its_ts(self):
        # A clock that stepped back after the last pass:  the new lines are older than the last one sent, and the file
        # the cursor named has rotated out of reach.  Every kept line still goes.  (Every ts is before the harness
        # clock, 2025-10-22T22:00:00Z, so a ts floor would have dropped e0002 and e0003.)
        line = lambda n, ts: json.dumps({"id": "e%04d" % n, "ts": ts, "mode": "pin"}) + "\n"  # noqa: E731
        self.write(raw=line(1, "2025-10-22T21:30:00Z"))
        self.runner.ship_log()
        os.rename(self.log_path, self.log_path.with_name("rotated-away"))
        self.write(raw=line(2, "2025-10-22T21:00:00Z"), path=self.log_path.with_name(self.log_path.name + ".1"))
        self.write(raw=line(3, "2025-10-22T21:00:01Z"))
        self.client.batches.clear()
        self.assertEqual(self.runner.ship_log(), 2)
        self.assertEqual(self.shipped(), ["e0002", "e0003"])

    def test_a_line_only_a_carriage_return_ends_never_stops_the_reader(self):
        self.write(1, raw="not\ran entry\n")
        self.write(2)
        self.assertEqual(self.runner.ship_log(), 2)
        self.assertEqual(self.shipped(), ["e0001", "e0002"])

    def test_a_line_nested_too_deep_is_passed_over(self):
        self.write(1, raw="[" * 100000 + "]" * 100000 + "\n")
        self.write(2)
        self.assertEqual(self.runner.ship_log(), 2)

    def test_a_file_that_rotates_after_it_is_listed_waits_for_the_next_pass(self):
        dot = lambda n: self.log_path.with_name("%s.%d" % (self.log_path.name, n))  # noqa: E731
        self.write(1, 2)
        self.runner.ship_log()
        cursor = self.client.state()["log"]
        self.write(3)
        listed = RUN.Runner._kept_files(self.log_path)  # [(the log, its inode)]

        def rotate_then_list(path):
            os.rename(self.log_path, dot(1))  # the script rotates after the runner listed the files
            self.write(4)
            return listed

        with mock.patch.object(RUN.Runner, "_kept_files", staticmethod(rotate_then_list)):
            self.assertEqual(self.runner.ship_log(), 0)
        self.assertEqual(self.client.state()["log"], cursor, "the cursor never moves on a stale listing")
        self.assertEqual(self.runner.ship_log(), 2)  # the next pass lists again and goes on from the same place
        self.assertEqual(self.shipped(), ["e0001", "e0002", "e0003", "e0004"])

    def test_the_files_are_listed_under_the_scripts_own_log_lock(self):
        import fcntl

        self.write(1)
        # The lock file the script takes, named as its writer names it (apple-notes-coding.sh:  the folder, then "." and
        # the log's name and ".lock").
        script = (Path(__file__).resolve().parents[2] / "apple-notes-coding.sh").read_text()
        self.assertIn('os.path.join(folder, "." + os.path.basename(path) + ".lock")', script)
        name = os.path.join(os.path.dirname(str(self.log_path)), "." + os.path.basename(str(self.log_path)) + ".lock")
        lock = os.open(name, os.O_WRONLY | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)  # the script, rotating
        with mock.patch.object(RUN, "LOG_LOCK_TRIES", 2), mock.patch.object(RUN, "LOG_LOCK_PAUSE_S", 0.01):
            with self.assertRaises(N.NotesError) as caught:
                self.runner.ship_log()
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(self.client.batches, [])
        fcntl.flock(lock, fcntl.LOCK_UN)
        # While the runner lists the names, the script could not take its lock to rotate.
        blocked: list[bool] = []
        real_list = RUN.Runner._list_kept

        def probing(path):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
                blocked.append(False)
            except BlockingIOError:
                blocked.append(True)
            return real_list(path)

        with mock.patch.object(RUN.Runner, "_list_kept", staticmethod(probing)):
            self.assertEqual(self.runner.ship_log(), 1)
        self.assertEqual(blocked, [True])

    def test_a_lock_file_the_runner_cannot_make_never_stops_the_log(self):
        # No lock file can be made or opened in the log's folder:  no script can take it either, so the names are
        # listed without it.
        self.write(1)
        real_open = os.open

        def refuse(path, *args, **kwargs):
            if str(path).endswith(".lock"):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(RUN.os, "open", refuse):
            self.assertEqual(self.runner.ship_log(), 1)

    def test_an_idle_pass_writes_nothing_and_a_trailing_line_that_is_no_entry_costs_no_search(self):
        self.write(1, 2, raw="not an entry\n")
        self.assertEqual(self.runner.ship_log(), 2)
        before = (self.state_path.read_bytes(), os.stat(self.state_path).st_ino)  # each save replaces the file
        with mock.patch.object(RUN.Runner, "_after_id", side_effect=AssertionError("searched")):
            self.assertEqual(self.runner.ship_log(), 0)
        self.assertEqual((self.state_path.read_bytes(), os.stat(self.state_path).st_ino), before)
        self.write(3)
        self.assertEqual(self.runner.ship_log(), 1)
        self.assertEqual(self.shipped(), ["e0001", "e0002", "e0003"])

    def test_a_place_found_by_its_id_is_saved_so_the_next_pass_needs_no_search(self):
        self.write(1, 2)
        self.runner.ship_log()
        copy = self.log_path.with_name("copy")
        copy.write_bytes(self.log_path.read_bytes())
        os.replace(copy, self.log_path)  # an identical file under a new inode (a restore, an editor's save)
        self.assertEqual(self.runner.ship_log(), 0)
        self.assertEqual(self.client.state()["log"]["inode"], os.stat(self.log_path).st_ino)
        with mock.patch.object(RUN.Runner, "_after_id", side_effect=AssertionError("searched")):
            self.assertEqual(self.runner.ship_log(), 0)

    def test_the_cursor_never_moves_into_a_file_with_no_entry(self):
        dot = lambda n: self.log_path.with_name("%s.%d" % (self.log_path.name, n))  # noqa: E731
        self.write(1)
        self.runner.ship_log()
        os.rename(self.log_path, dot(1))
        self.write(raw="not an entry\n")
        self.assertEqual(self.runner.ship_log(), 0)
        self.assertEqual(self.client.state()["log"]["inode"], os.stat(dot(1)).st_ino)  # still where it can be checked
        before = os.stat(self.state_path).st_ino
        with mock.patch.object(RUN.Runner, "_after_id", side_effect=AssertionError("searched")):
            self.assertEqual(self.runner.ship_log(), 0)
        self.assertEqual(os.stat(self.state_path).st_ino, before)
        self.write(2)
        self.assertEqual(self.runner.ship_log(), 1)
        self.assertEqual(self.shipped(), ["e0001", "e0002"])

    def test_a_line_with_an_empty_id_is_no_entry(self):
        self.write(1, raw=json.dumps({"id": "", "ts": "2025-10-22T21:00:00Z"}) + "\n")
        self.assertEqual(self.runner.ship_log(), 1)
        self.assertEqual(self.client.state()["log"]["id"], "e0001")
        self.assertEqual(self.runner.ship_log(), 0)  # never every line again

    def test_a_cursor_with_no_entry_to_check_is_never_trusted_by_its_inode_alone(self):
        # A cursor at the start of a file (or one with no entry behind it) cannot be checked, and an inode the
        # filesystem gave to a new file would skip every older copy;  so the place is found by the id, or every kept
        # line goes.
        dot = lambda n: self.log_path.with_name("%s.%d" % (self.log_path.name, n))  # noqa: E731
        self.write(1, 2, path=dot(1))
        self.write(3)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"log": {"inode": os.stat(self.log_path).st_ino, "offset": 0, "id": "e0001"}}))
        self.assertEqual(self.runner.ship_log(), 2)
        self.assertEqual(self.shipped(), ["e0002", "e0003"])

    def test_a_copy_that_cannot_be_read_stops_the_pass_with_an_error_and_skips_nothing(self):
        dot = lambda n: self.log_path.with_name("%s.%d" % (self.log_path.name, n))  # noqa: E731
        self.write(1, path=dot(1))
        self.write(2)
        real_open = open

        def refuse(path, *args, **kwargs):
            if Path(path) == dot(1):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(RUN, "open", refuse, create=True):
            with self.assertRaises(PermissionError):
                self.runner.ship_log()
        self.assertEqual(self.client.batches, [], "nothing after the copy went ahead of it")
        self.assertEqual(self.runner.ship_log(), 2)
        self.assertEqual(self.shipped(), ["e0001", "e0002"])

    def test_a_line_longer_than_one_read_is_passed_over_and_the_rest_of_the_copy_still_goes(self):
        dot = lambda n: self.log_path.with_name("%s.%d" % (self.log_path.name, n))  # noqa: E731
        self.write(1, raw="z" * 3000 + "\n", path=dot(1))
        self.write(2, path=dot(1))
        self.write(3)
        with mock.patch.object(RUN, "LOG_READ", 1000):
            self.assertEqual(self.runner.ship_log(), 3)
        self.assertEqual(self.shipped(), ["e0001", "e0002", "e0003"])

    def test_an_id_in_the_state_that_utf8_cannot_hold_is_not_used(self):
        self.write(1, 2)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text('{"log": {"inode": 1, "offset": 5, "at": 5, "id": "\\ud800"}}')
        self.assertEqual(self.runner.ship_log(), 2)  # no place to trust:  every kept line, nothing raised
        self.assertEqual(RUN._error_text(N.NotesError("unavailable", "bad \ud800 text")), "unavailable: bad ? text")

    def test_a_file_larger_than_one_read_is_sent_whole(self):
        self.write(*range(1, 61))
        with mock.patch.object(RUN, "LOG_READ", 1000):  # a few lines per read
            self.assertEqual(self.runner.ship_log(), 60)
        self.assertEqual(self.shipped(), ["e%04d" % n for n in range(1, 61)])

    def test_a_rotated_copy_is_followed_by_position_so_an_out_of_order_line_is_never_lost(self):
        # Two script runs can finish in the other order, and a clock can step back:  a line older than the newest one
        # sent, written after it, must still go once its file rotates.
        line = lambda n, ts: json.dumps({"id": "e%04d" % n, "ts": ts, "mode": "pin"}) + "\n"  # noqa: E731
        self.write(raw=line(1, "2025-10-22T21:00:05Z"))
        self.assertEqual(self.runner.ship_log(), 1)
        self.write(raw=line(2, "2025-10-22T21:00:03Z"))  # older than e0001, written after it
        os.rename(self.log_path, self.log_path.with_name(self.log_path.name + ".1"))
        self.write(raw=line(3, "2025-10-22T21:00:06Z"))
        self.runner.ship_log()
        self.assertEqual(self.shipped(), ["e0001", "e0002", "e0003"])

    def test_a_log_that_cannot_be_sent_never_blocks_a_claim(self):
        import threading

        stamps = iter([("old",), ("old",), ("new",)])
        self.claim(req("pin"))
        with mock.patch.object(self.runner, "ship_log", side_effect=ValueError("a line it cannot send")):
            self.runner.loop(threading.Event(), 0, code_stamp=lambda: next(stamps))
        self.assertEqual(len(self.client.reports), 1)  # the request was still handled
        self.assertIn("log:  internal: ValueError", self.client.state()["last_error"])

    def test_a_file_truncated_in_place_is_read_from_its_start(self):
        self.write(1, 2, 3)
        self.runner.ship_log()
        with open(self.log_path, "w") as fh:
            fh.write(self.line(9))
        self.assertEqual(self.runner.ship_log(), 1)
        self.assertEqual(self.shipped()[-1], "e0009")

    def test_a_failed_send_resumes_from_the_saved_offset(self):
        self.write(*range(1, 451))
        self.client.fail_log_at = 2
        with self.assertRaises(N.NotesError):
            self.runner.ship_log()
        self.assertEqual(self.shipped(), ["e%04d" % n for n in range(1, 201)])
        self.assertEqual(self.client.state()["log"]["offset"], sum(len(self.line(n)) for n in range(1, 201)))
        self.assertEqual(self.runner.ship_log(), 250)
        self.assertEqual(self.shipped(), ["e%04d" % n for n in range(1, 451)], "each line once, in order")

    def test_no_log_or_a_log_switched_off_ships_nothing(self):
        self.assertEqual(self.runner.ship_log(), 0)
        self.env["APPLE_NOTES_ACTION_LOG"] = "off"
        self.write(1)
        self.assertEqual(self.make().ship_log(), 0)
        self.assertEqual(self.client.batches, [])


class ReviewFixTests(RunnerHarness):
    """PR #876 review:  a read's text never goes to the state file, and a result the Operator refuses for good is
    replaced by an error report instead of stalling the runner."""

    def test_a_read_result_is_reported_but_never_written_to_the_state_file(self):
        self.answer = {"note": {"title": MASTER, "body": "SECRET NOTE TEXT", "format": "html"}}
        self.claim(req("read"))
        self.assertTrue(self.runner.once(0))
        report = self.client.reports[0]
        self.assertEqual(report["result"]["note"]["body"], "SECRET NOTE TEXT")
        self.assertNotIn("SECRET NOTE TEXT", report["state_text_then"])
        self.assertNotIn("SECRET NOTE TEXT", self.state_path.read_text())

    def test_a_result_the_operator_refuses_for_good_becomes_an_error_report(self):
        self.answer = {"note": {"title": MASTER, "body": "x", "format": "html"}}
        self.client.report_statuses = [413]
        self.claim(req("read"))
        self.assertTrue(self.runner.once(0))
        self.assertEqual([("result" in r, r["error"]) for r in self.client.reports][0], (True, None))
        self.assertEqual(self.client.reports[1]["error"]["code"], "internal")  # a read changed nothing
        self.assertEqual(self.client.state()["inflight"], {})  # the runner let it go
        # A write's refused result is outcome_unknown:  the script ran, so a caller must check, never just resend.
        self.client.reports.clear()
        self.client.report_statuses = [400]
        self.claim(req("pin"))
        self.assertTrue(self.runner.once(0))
        self.assertEqual(self.client.reports[1]["error"]["code"], "outcome_unknown")
        self.assertIn("most likely written", self.client.reports[1]["error"]["message"])

    def test_recover_lets_go_of_a_report_the_operator_will_never_take(self):
        state = {"inflight": {"CODEX/requestid-0009": {"seat": "CODEX", "request_id": "requestid-0009", "claim": "c9", "action": "pin", "result": {"status": "done"}}}}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state))
        self.client.report_statuses = [400]
        self.runner.recover()
        self.assertEqual(self.client.state()["inflight"], {})


class LoopTests(RunnerHarness):
    def test_the_loop_returns_between_requests_once_the_code_changes(self):
        import threading

        stamps = iter([("old",), ("old",), ("old",), ("new",)])
        stop = threading.Event()
        self.claim(req("pin"))
        self.runner.loop(stop, 0, code_stamp=lambda: next(stamps))
        self.assertFalse(stop.is_set())  # it returned by itself, so launchd starts the new code
        self.assertEqual(len(self.client.reports), 1)  # the request it held was finished first
        self.assertEqual(len(self.client.claim_waits), 2)

    def test_the_package_stamp_names_every_module(self):
        names = [entry[0] for entry in RUN.package_stamp()]
        self.assertIn("notes_runner.py", names)
        self.assertIn("simple-notes", names)


class RunnerForTests(unittest.TestCase):
    def test_the_runner_signs_as_the_runner_principal(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = RUN.runner_for({"OPERATOR_API_URL": "https://ops.example.test"}, Path(tmp))
            self.assertEqual((runner.client.principal, runner.client.key.name), ("RUNNER", "OPERATOR_NOTES_RUNNER_KEY"))
            self.assertEqual(runner.state_path, Path(tmp) / ".local" / "state" / "operator" / "notes-runner.json")
            with self.assertRaises(N.NotesError) as caught:
                RUN.runner_for({"OPERATOR_API_URL": "http://insecure.example.test"}, Path(tmp))
            self.assertEqual(caught.exception.code, "not_configured")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
