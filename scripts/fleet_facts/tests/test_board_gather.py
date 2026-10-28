"""Linear through the board CLI:  read verbs only, claims and orphans, claim time, comment events."""
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from fleet_facts import board as B
from fleet_facts.runner import Result

from .builders import board_row, board_show, comment
from .fakes import FakeRunner, out

CENTRAL_FOOTER = ("**Provenance (migrated)**\n\n* Board row: `x`\n  * Reported by claude, addressed by codex\n"
                  "  * Created Thu, Oct 8, 2026, 10:44pm, updated Thu, Oct 8, 2026, 11:10pm, claimed Thu, Oct 8, 2026, 10:44pm")
LOOSE_FOOTER = "* Env mac, location claimed: mon, oct 5, 2026 gb-housekeeper working board batch"


class Fake:
    """A board CLI over dicts:  lists by key, shows by identifier."""

    def __init__(self, in_progress, needs_owner=(), shows=None, fail=()):
        self.in_progress, self.needs_owner = list(in_progress), list(needs_owner)
        self.shows, self.fail = shows or {}, set(fail)
        self.runner = FakeRunner(self.handle)

    def handle(self, argv):
        verb = argv[1]
        if verb == "list":
            rows = self.in_progress if "in_progress" in argv else self.needs_owner
            return out(json.dumps(rows))
        if verb == "show":
            if argv[2] in self.fail:
                return out("", rc=5, err="board: linear is unreachable")
            return out(json.dumps(self.shows[argv[2]]))
        return out("", rc=9)


def gather(fake, **kw):
    with mock.patch.object(B, "which", return_value="board"):
        return B.gather(fake.runner, **kw)


class ReadOnly(unittest.TestCase):
    def test_write_verbs_are_refused_before_anything_runs(self):
        runner = FakeRunner(lambda argv: out("{}"))
        for verb in ("comment", "claim", "status", "file", "label", "whoami", "stats", ""):
            with self.assertRaises(B.BoardWriteRefused):
                B._board(runner, "board", verb, ["FFX-1"], 5)
        self.assertEqual(runner.calls, [])

    def test_only_list_and_show_are_ever_called_and_writes_are_off_in_the_env(self):
        fake = Fake([board_row("FFX-1")], [board_row("FFX-9", labels=["needs-owner"])],
                    {"FFX-1": board_show("FFX-1")})
        gather(fake)
        verbs = {argv[1] for argv in fake.runner.argvs}
        self.assertEqual(verbs, {"list", "show"})
        for call in fake.runner.calls:
            self.assertNotIn("LINEAR_BOARD_WRITES", call["env"])
        lists = [a for a in fake.runner.argvs if a[1] == "list"]
        self.assertTrue(all("--json" in a for a in lists))
        self.assertIn(["board", "show", "FFX-1", "--json"], fake.runner.argvs)

    def test_a_cloud_host_can_point_at_the_board_script_in_its_clone(self):
        import os
        import stat
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "board")
            with open(script, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"FLEET_FACTS_BOARD": script}):
                self.assertEqual(B.board_command(), script)
            with mock.patch.dict(os.environ, {"FLEET_FACTS_BOARD": os.path.join(tmp, "missing")}):
                self.assertIsNone(B.board_command())

    def test_no_board_cli_is_a_failed_source(self):
        with mock.patch.object(B, "which", return_value=None):
            self.assertEqual(B.gather(FakeRunner(lambda a: out("")))["status"], "failed")


class Claims(unittest.TestCase):
    def test_claims_orphans_and_needs_owner(self):
        orphan = board_row("FFX-2", seats=())
        fake = Fake([board_row("FFX-1", seats=("CLAUDE",)), orphan], [board_row("FFX-9", seats=(), labels=["needs-owner"], n=9)],
                    {"FFX-1": board_show("FFX-1"), "FFX-2": board_show("FFX-2")})
        result = gather(fake)
        self.assertEqual(result["status"], "ok")
        data = result["data"]
        self.assertEqual([c["identifier"] for c in data["claims"]], ["FFX-1"])
        self.assertEqual(data["claims"][0]["seat"], "CLAUDE")
        self.assertEqual([c["identifier"] for c in data["orphans"]], ["FFX-2"])
        self.assertIsNone(data["orphans"][0]["seat"])
        self.assertEqual([i["identifier"] for i in data["needs_owner"]], ["FFX-9"])
        self.assertEqual(data["in_progress_total"], 2)
        self.assertEqual(data["detail_failed"], 0)

    def test_branch_comes_from_the_location(self):
        shows = {"FFX-1": board_show("FFX-1", location="Mac, ~/apps/lanes/Example/claude-x @ claude/FFX-1-todo-list"),
                 "FFX-2": board_show("FFX-2", location="awaiting owner/seat decision"),
                 "FFX-3": board_show("FFX-3", location="~/apps/old/x @ main (private example/repo)")}
        fake = Fake([board_row("FFX-1"), board_row("FFX-2", n=2), board_row("FFX-3", n=3)], shows=shows)
        by_id = {c["identifier"]: c for c in gather(fake)["data"]["claims"]}
        self.assertEqual(by_id["FFX-1"]["branch"], "claude/FFX-1-todo-list")
        self.assertTrue(by_id["FFX-1"]["branch_exact"])
        self.assertIsNone(by_id["FFX-2"]["branch"])
        self.assertEqual(by_id["FFX-3"]["branch"], "main")

    def test_updated_at_is_not_copied_anywhere(self):
        fake = Fake([board_row("FFX-1", updated="2026-10-11T11:59:00.000Z")], [board_row("FFX-9", seats=(), labels=["needs-owner"])],
                    {"FFX-1": board_show("FFX-1")})
        blob = json.dumps(gather(fake)["data"])
        self.assertNotIn("updated", blob.lower())
        self.assertNotIn("2026-10-11T11:59", blob)

    def test_free_text_is_cleaned_and_urls_are_checked(self):
        row = board_row("FFX-1", title="@**all** [x](https://evil.example/) `cmd`", url="https://evil.example/issue/FFX-1")
        data = gather(Fake([row], shows={"FFX-1": board_show("FFX-1")}))["data"]
        claim = data["claims"][0]
        self.assertNotIn("@", claim["title"])
        self.assertNotIn("`", claim["title"])
        self.assertIsNone(claim["url"])
        good = gather(Fake([board_row("FFX-1")], shows={"FFX-1": board_show("FFX-1")}))["data"]["claims"][0]
        self.assertTrue(good["url"].startswith("https://linear.app/"))

    def test_a_row_without_a_usable_identifier_is_unreadable_not_idle(self):
        row = board_row("FFX-1")
        row["identifier"] = "not an id; rm -rf"
        data = gather(Fake([row]))["data"]
        self.assertFalse(data["claims"][0]["linear_comments"]["ok"])
        self.assertIsNone(data["claims"][0]["identifier"])


def _has_tz_database() -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Chicago")
        return True
    except Exception:
        return False


class Holders(unittest.TestCase):
    """Who holds an In Progress issue follows the board's own rule, and not knowing is never the same as nobody."""

    def unlabelled(self, ident="FFX-2", n=2):
        return board_row(ident, seats=(), n=n)

    def test_an_issue_with_no_claimed_label_is_held_by_the_footers_seat(self):
        fake = Fake([self.unlabelled()], shows={"FFX-2": board_show("FFX-2", addressed_by="CODEX")})
        data = gather(fake)["data"]
        self.assertEqual(data["orphans"], [])
        claim = data["claims"][0]
        self.assertEqual((claim["seat"], claim["seats"], claim["seat_basis"]), ("CODEX", ["CODEX"], "footer"))

    def test_two_footer_seats_are_both_kept_in_order(self):
        fake = Fake([self.unlabelled()], shows={"FFX-2": board_show("FFX-2", addressed_by="CODEX,CURSOR")})
        claim = gather(fake)["data"]["claims"][0]
        self.assertEqual(claim["seats"], ["CODEX", "CURSOR"])

    def test_a_label_wins_over_the_footer(self):
        fake = Fake([board_row("FFX-1", seats=("CLAUDE",))], shows={"FFX-1": board_show("FFX-1", addressed_by="CODEX")})
        claim = gather(fake)["data"]["claims"][0]
        self.assertEqual((claim["seat"], claim["seat_basis"]), ("CLAUDE", "label"))

    def test_nobody_in_the_footer_either_is_an_orphan(self):
        fake = Fake([self.unlabelled()], shows={"FFX-2": board_show("FFX-2", addressed_by=None)})
        data = gather(fake)["data"]
        self.assertEqual([o["identifier"] for o in data["orphans"]], ["FFX-2"])
        self.assertEqual(data["orphans"][0]["seat_basis"], "none")
        self.assertEqual((data["claims"], data["unreadable"]), ([], []))

    def test_a_failed_show_on_an_unlabelled_issue_is_not_an_orphan(self):
        fake = Fake([self.unlabelled()], shows={}, fail=["FFX-2"])
        data = gather(fake)["data"]
        self.assertEqual(data["orphans"], [])
        self.assertEqual([u["identifier"] for u in data["unreadable"]], ["FFX-2"])
        self.assertFalse(data["unreadable"][0]["linear_comments"]["ok"])

    def test_a_row_with_no_labels_list_is_unreadable_not_an_orphan(self):
        for labels in (None, "claimed:CLAUDE", {"x": 1}):
            row = self.unlabelled()
            if labels is None:
                del row["labels"]
            else:
                row["labels"] = labels
            data = gather(Fake([row], shows={"FFX-2": board_show("FFX-2")}))["data"]
            self.assertEqual((data["orphans"], data["claims"]), ([], []), labels)
            self.assertEqual(len(data["unreadable"]), 1, labels)

    def test_the_budget_running_out_on_an_unlabelled_issue_is_unreadable(self):
        ticks = iter([0, 1000, 1001, 1002])
        fake = Fake([self.unlabelled()], shows={"FFX-2": board_show("FFX-2")})
        data = gather(fake, monotonic=lambda: next(ticks), detail_budget_s=600)["data"]
        self.assertEqual((data["orphans"], len(data["unreadable"])), ([], 1))

    def test_a_row_that_is_not_an_object_fails_the_source(self):
        fake = Fake([board_row("FFX-1"), "garbage"], shows={"FFX-1": board_show("FFX-1")})
        self.assertEqual(gather(fake)["status"], "failed")
        fake = Fake([board_row("FFX-1")], ["garbage"], shows={"FFX-1": board_show("FFX-1")})
        self.assertEqual(gather(fake)["status"], "failed")


class MalformedShow(unittest.TestCase):
    def test_a_show_that_is_not_a_document_is_unreadable_never_quiet(self):
        for shown in ([], "text", None, 7, {"identifier": "FFX-1"}, {"comments": None}, {"comments": "x"}, {"comments": {"a": 1}}):
            detail = B.read_detail(shown)
            self.assertFalse(detail["linear_comments"]["ok"], shown)
            self.assertFalse(detail["detail_ok"], shown)

    def test_an_empty_comment_list_is_a_real_answer(self):
        detail = B.read_detail({"identifier": "FFX-1", "comments": []})
        self.assertTrue(detail["linear_comments"]["ok"] and detail["detail_ok"])
        self.assertEqual(detail["linear_comments"]["events"], [])

    def test_a_malformed_show_in_a_run_marks_the_claim_unknown(self):
        fake = Fake([board_row("FFX-1")], shows={"FFX-1": {"identifier": "FFX-1"}})
        claim = gather(fake)["data"]["claims"][0]
        self.assertFalse(claim["linear_comments"]["ok"])
        from fleet_facts import activity as A
        self.assertEqual(A.activity_at({"linear_comments": claim["linear_comments"]}, launch_at="2026-10-01T00:00:00Z"), A.UNKNOWN)


class ClaimTime(unittest.TestCase):
    def detail(self, shown):
        return B.read_detail(shown)

    def test_newest_claimed_comment_wins(self):
        shown = board_show("FFX-1", comments=[
            comment("CLAUDE", "CLAIMED", "2026-10-01T10:00:00.000Z"),
            comment("CLAUDE", "just working", "2026-10-05T10:00:00.000Z"),
            comment("CODEX", "CLAIMED", "2026-10-03T10:00:00.000Z")], provenance=CENTRAL_FOOTER)
        d = self.detail(shown)
        self.assertEqual((d["claimed_at"], d["claimed_basis"]), ("2026-10-03T10:00:00Z", "claimed_comment"))

    @unittest.skipUnless(_has_tz_database(), "no time zone database on this host")
    def test_import_footer_is_read_in_central_time(self):
        d = self.detail(board_show("FFX-1", provenance=CENTRAL_FOOTER))
        self.assertEqual((d["claimed_at"], d["claimed_basis"]), ("2026-10-09T03:44:00Z", "import_footer"))   # 10:44pm CDT

    @unittest.skipUnless(_has_tz_database(), "no time zone database on this host")
    def test_footer_in_standard_time_and_noon_midnight(self):
        self.assertEqual(B.parse_footer_claimed("claimed Mon, Dec 7, 2026, 12:05am"), datetime(2026, 12, 7, 6, 5, tzinfo=timezone.utc))
        self.assertEqual(B.parse_footer_claimed("claimed Mon, Dec 7, 2026, 12:05pm"), datetime(2026, 12, 7, 18, 5, tzinfo=timezone.utc))

    def test_loose_footers_are_none_not_an_error(self):
        for footer in (LOOSE_FOOTER, "", None, "claimed Thu, Oct 8, 2026", "claimed Thu, Foo 8, 2026, 10:44pm",
                       "claimed Thu, Feb 31, 2026, 10:44pm", "claimed Thu, Oct 8, 2026, 13:44pm"):
            self.assertIsNone(B.parse_footer_claimed(footer), footer)
        d = self.detail(board_show("FFX-1", provenance=LOOSE_FOOTER))
        self.assertEqual((d["claimed_at"], d["claimed_basis"]), (None, "unknown"))
        self.assertTrue(d["linear_comments"]["ok"])      # an unreadable claim time is not an unreadable issue


class Comments(unittest.TestCase):
    def test_events_keep_author_and_time_and_drop_the_text(self):
        secret_text = "private words that must not travel"
        shown = board_show("FFX-1", comments=[comment("CLAUDE", "CLAIMED", "2026-10-01T10:00:00.000Z"),
                                              comment("Example Owner", secret_text, "2026-10-02T10:00:00.000Z")])
        d = B.read_detail(shown)
        self.assertEqual(d["linear_comments"]["events"], [
            {"at": "2026-10-01T10:00:00Z", "author": "CLAUDE", "sweeper": False, "claimed": True},
            {"at": "2026-10-02T10:00:00Z", "author": "Example Owner", "sweeper": False, "claimed": False}])
        self.assertNotIn(secret_text, json.dumps(d))

    def test_sweeper_comments_are_flagged_everything_else_is_not(self):
        shown = board_show("FFX-1", comments=[
            comment("CLAUDE", "[claim-expiry] 36h ping", "2026-10-01T10:00:00.000Z"),
            comment("CLAUDE", "[sweep] released", "2026-10-02T10:00:00.000Z"),
            comment("CODEX", "I am here too", "2026-10-03T10:00:00.000Z")])
        flags = [e["sweeper"] for e in B.read_detail(shown)["linear_comments"]["events"]]
        self.assertEqual(flags, [True, True, False])

    def test_a_comment_with_a_bad_time_makes_the_source_unreadable(self):
        shown = board_show("FFX-1", comments=[comment("CLAUDE", "x", "yesterday")])
        self.assertFalse(B.read_detail(shown)["linear_comments"]["ok"])

    def test_events_feed_the_activity_function(self):
        from fleet_facts import activity as A
        shown = board_show("FFX-1", comments=[comment("CLAUDE", "x", "2026-10-01T10:00:00.000Z"),
                                              comment("CLAUDE", "[sweep] note", "2026-10-09T10:00:00.000Z")])
        source = B.read_detail(shown)["linear_comments"]
        at = A.activity_at({"linear_comments": source})
        self.assertEqual(at, datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc))


class Failures(unittest.TestCase):
    def test_a_failed_show_marks_only_that_claim_unreadable(self):
        fake = Fake([board_row("FFX-1"), board_row("FFX-2", n=2)], shows={"FFX-1": board_show("FFX-1")}, fail=["FFX-2"])
        result = gather(fake)
        self.assertEqual(result["status"], "ok")
        by_id = {c["identifier"]: c for c in result["data"]["claims"]}
        self.assertTrue(by_id["FFX-1"]["linear_comments"]["ok"])
        self.assertFalse(by_id["FFX-2"]["linear_comments"]["ok"])
        self.assertIn("exited 5", by_id["FFX-2"]["linear_comments"]["reason"])
        self.assertEqual(result["data"]["detail_failed"], 1)

    def test_the_time_budget_stops_the_detail_pass(self):
        ticks = iter([0, 1, 1000, 1001, 1002])
        fake = Fake([board_row("FFX-1"), board_row("FFX-2", n=2)], shows={"FFX-1": board_show("FFX-1"), "FFX-2": board_show("FFX-2")})
        result = gather(fake, monotonic=lambda: next(ticks), detail_budget_s=600)
        by_id = {c["identifier"]: c for c in result["data"]["claims"]}
        self.assertTrue(by_id["FFX-1"]["linear_comments"]["ok"])
        self.assertFalse(by_id["FFX-2"]["linear_comments"]["ok"])
        self.assertIn("budget", by_id["FFX-2"]["linear_comments"]["reason"])
        shows = [a for a in fake.runner.argvs if a[1] == "show"]
        self.assertEqual(len(shows), 1)

    def test_a_failed_list_fails_the_source(self):
        for result in (Result(1, "", "boom"), Result(0, "not json"), Result(None, reason="timed out after 180s"), Result(0, "{}")):
            with mock.patch.object(B, "which", return_value="board"):
                r = B.gather(FakeRunner(lambda argv, res=result: res))
            self.assertEqual(r["status"], "failed", result)

    def test_a_truncated_list_fails_with_the_partial_data(self):
        rows = [board_row("FFX-%d" % i, n=i) for i in range(1, B.LIST_LIMIT + 1)]
        fake = Fake(rows, shows={r["identifier"]: board_show(r["identifier"]) for r in rows})
        result = gather(fake)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["data"]["truncated"])


if __name__ == "__main__":
    unittest.main()
