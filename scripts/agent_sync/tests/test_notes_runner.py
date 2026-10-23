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
        self.client.claims.append({"claim": 5, "seat": "CODEX", "request_id": "requestid-0001", "request": req("pin")})
        self.assertFalse(self.runner.once())
        self.assertEqual((self.runs, self.client.reports), ([], []))

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
                raise N.NotesError("unavailable", "the Operator notes API could not be reached (URLError)")
            return real(seat, request_id, claim, **outcome)

        self.client.report = report
        with self.assertRaises(N.NotesError):  # raised after the rest, so the loop backs off before new work
            self.runner.recover()
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
        self.assertEqual(self.client.reports[1]["error"]["code"], "unavailable")
        self.assertEqual(self.client.state()["inflight"], {})  # the runner let it go

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
