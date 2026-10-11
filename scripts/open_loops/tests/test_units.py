"""Smaller units:  Central time, URLs, the C1/C6/C3 readers, the Zulip signal reader, claim bands, file helpers."""
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleet_facts import activity, safety
from fleet_facts.runner import Result
from open_loops import central, claims, files, urls, views, zulip_topics

from .builders import NOW, Sandbox, World, ago


class CentralTime(unittest.TestCase):
    def test_the_manual_rule_agrees_with_the_time_zone_database_for_every_hour_of_2026(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for hour in range(0, 24 * 366):
            moment = start + timedelta(hours=hour)
            self.assertEqual(central.stamp(moment), central.stamp(moment, use_zoneinfo=False), moment)

    def test_and_for_the_change_days_of_other_years(self):
        for year in (2025, 2027, 2028, 2030):
            for month in (3, 11):
                for day in range(1, 15):
                    for hour in range(0, 24):
                        moment = datetime(year, month, day, hour, 30, tzinfo=timezone.utc)
                        self.assertEqual(central.clock(moment), central.clock(moment, use_zoneinfo=False), moment)

    def test_twelve_hour_clock_with_lower_case_am_and_pm_and_no_zone(self):
        cases = {(5, 0): "12:00am", (5, 5): "12:05am", (11, 0): "6:00am", (17, 0): "12:00pm", (18, 59): "1:59pm", (4, 59): "11:59pm"}
        for (hour, minute), want in cases.items():
            self.assertEqual(central.clock(datetime(2026, 7, 1, hour, minute, tzinfo=timezone.utc)), want)     # July:  UTC-5
        self.assertEqual(central.stamp(datetime(2026, 8, 9, 20, 52, tzinfo=timezone.utc)), "Sun, Aug 9, 3:52pm")
        for text in (central.stamp(NOW), central.clock(NOW)):
            for zone in ("CDT", "CST", "CT", "UTC", "Z"):
                self.assertNotIn(zone, text)

    def test_a_time_with_no_zone_is_refused(self):
        with self.assertRaises(ValueError):
            central.stamp(datetime(2026, 10, 11, 12, 0))

    def test_ages(self):
        self.assertEqual([central.age_text(h) for h in (None, 0.2, 1, 5.9, 47.9, 48, 100)],
                         ["age unknown", "under an hour", "1 hour", "5 hours", "47 hours", "2 days", "4 days"])


class Urls(unittest.TestCase):
    def test_hash_encoding_matches_the_listeners(self):
        from agent_sync import adapters
        for text in ("OWNER FFX-1 pick a color", "a.b c", "100% sure", "FFX-284 open-loops gather, render, send", "ü and 日本"):
            self.assertEqual(urls.hash_part(text), adapters._hash_part(text), text)

    def test_zulip_links_are_built_from_ids(self):
        self.assertEqual(urls.topic_url("OWNER FFX-1 x"), "%s/#narrow/channel/%d-agent-sync/topic/OWNER.20FFX-1.20x" % (urls.REALM, urls.STREAM_ID))
        self.assertTrue(urls.message_url("t", 12).endswith("/topic/t/near/12"))

    def test_pr_and_linear_links_are_validated(self):
        self.assertEqual(urls.pr_url("Simple-With-Us/example-repo", 3), "https://github.com/Simple-With-Us/example-repo/pull/3")
        for repo, number in (("no-slash", 1), ("a/b c", 1), ("a/b", 0), ("a/b", True), (None, 1)):
            self.assertIsNone(urls.pr_url(repo, number))
        good = "https://linear.app/example-workspace/issue/FFX-1/example"
        self.assertEqual(urls.linear_url(good), good)
        for bad in ("http://linear.app/x/issue/FFX-1", "https://evil.test/x/issue/FFX-1", "https://linear.app/x/issue/FFX-1 junk", None, 5):
            self.assertIsNone(urls.linear_url(bad), bad)

    def test_the_book_keeps_what_it_is_given_and_ignores_nothing(self):
        book = urls.UrlBook()
        self.assertEqual(book.add("https://x.test/b"), "https://x.test/b")
        book.add("https://x.test/a")
        book.add("https://x.test/a")
        book.add(None)
        self.assertEqual(book.sorted(), ["https://x.test/a", "https://x.test/b"])


class Views(unittest.TestCase):
    def test_autopush_shapes(self):
        row = {"path": "/work/lanes/Example/claude-snap", "repo": "Simple-With-Us/example-repo", "branch": "claude/snap", "snapshot_only": True,
               "idle_hours": 2, "unpushed_commits": 1}
        for doc in ([row], {"lanes": [row]}, {"rows": [row]}, {"checkouts": [row]}):
            got = views.autopush_view({"document": doc})
            self.assertTrue(got["ok"], doc)
            self.assertEqual(got["rows"][0]["lane"], "claude-snap")                       # a folder name, never the path
            self.assertNotIn("/work", str(got))
        self.assertTrue(views.autopush_view({"document": {"refused": []}})["ok"])         # refusals only is a readable (quiet) status
        self.assertFalse(views.autopush_view({"document": {"hello": 1}})["ok"])
        self.assertFalse(views.autopush_view({"document": 7})["ok"])
        self.assertFalse(views.autopush_view({"document": {"lanes": "no"}})["ok"])
        self.assertFalse(views.autopush_view(None)["ok"])

    def test_a_blocked_dirty_row_is_a_refusal_and_listed_once(self):
        rows = [{"path": "/w/a", "blocked_reason": "allowlist", "dirty_unsnapshotted": 2},
                {"path": "/w/b", "blocked_reason": "allowlist", "dirty_unsnapshotted": 0},
                {"path": "/w/c", "blocked_reason": "armed", "unpushed_commits": 1}]
        got = views.autopush_view({"document": {"lanes": rows, "refused": [{"path": "/w/c", "reason": "armed PR"}]}})
        self.assertEqual([(r["lane"], r["reason"]) for r in got["refusals"]], [("c", "armed PR"), ("a", "allowlist")])

    def test_armed_state_counts_as_armed_local_work(self):
        got = views.autopush_view({"document": [{"path": "/w/a", "state": "armed_local_work"}]})
        self.assertTrue(got["rows"][0]["armed_local_work"])

    def test_text_is_cleaned(self):
        got = views.autopush_view({"document": {"refused": [{"path": "/w/a", "reason": "@**all** [x](https://evil.test) `rm`"}]}})
        reason = got["refusals"][0]["reason"]
        self.assertNotIn("@**", reason)
        self.assertNotIn("://", reason)
        self.assertNotIn("`", reason)

    def test_needs_owner_shapes(self):
        rows = [{"identifier": "FFX-1", "seat": "CLAUDE", "low_stakes": [2, 1, 2], "topic": "OWNER FFX-1 x", "state": "open", "over_five": True},
                {"id": "FFX-2", "state": "replied"}, {"identifier": "nope"}, {"seat": "CLAUDE"},
                {"identifier": "FFX-3", "low_stakes": [{"n": 3}, {"number": 1}], "replied": {"kind": "reaction"}},
                {"identifier": "FFX-4", "topic": "x" * 61}]
        for doc in (rows, {"items": rows}, {"issues": rows}):
            got = views.needs_owner_view({"document": doc})
            self.assertTrue(got["ok"])
            self.assertEqual(sorted(got["by_ident"]), ["FFX-1", "FFX-2", "FFX-3", "FFX-4"])
            self.assertEqual(got["by_ident"]["FFX-1"]["low_stakes"], [1, 2])
            self.assertTrue(got["by_ident"]["FFX-2"]["replied"])
            self.assertTrue(got["by_ident"]["FFX-3"]["replied"])
            self.assertEqual(got["by_ident"]["FFX-3"]["low_stakes"], [1, 3])
            self.assertIsNone(got["by_ident"]["FFX-4"]["topic"])                          # too long for a Zulip topic
            self.assertEqual(got["over_five_seats"], ["CLAUDE"])
        self.assertFalse(views.needs_owner_view({"document": {"x": 1}})["ok"])

    def test_asks(self):
        got = views.asks_view({"document": {"open": [{"title": "Fix the thing", "identifier": "FFX-9", "age_hours": 30}, {"text": "x"}]}})
        self.assertTrue(got["ok"])
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["items"][0]["identifier"], "FFX-9")
        self.assertFalse(views.asks_view({"document": {"x": 1}})["ok"])


class ZulipSignals(Sandbox):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.world.programs(self.bin, ["agent-sync"])

    def read(self, wanted, **kw):
        return zulip_topics.read_signals(self.world.runner(), wanted, **kw)

    def message(self, seat, hours, **extra):
        return dict({"timestamp": int((NOW - timedelta(hours=hours)).timestamp()), "seat": seat}, **extra)

    def test_only_messages_that_name_a_seat_count(self):
        self.world.agent_sync["topics"] = [{"name": "FFX-1 thing", "max_id": 3}]
        self.world.agent_sync["read"] = {"FFX-1 thing": [self.message("CLAUDE", 2), self.message(None, 1), {"seat": "CODEX"}, self.message("", 1)]}
        got = self.read([{"identifier": "FFX-1", "seat": "CLAUDE"}])
        self.assertEqual(got["by_ident"]["FFX-1"], {"ok": True, "events": [{"at": ago(2), "by": "CLAUDE"}]})

    def test_the_topic_must_start_with_the_identifier_and_a_space(self):
        self.world.agent_sync["topics"] = [{"name": "FFX-10 other", "max_id": 3}, {"name": "FFX-1", "max_id": 2}, {"name": "FFX-1 thing", "max_id": 1},
                                           {"name": "xFFX-1 thing", "max_id": 9}]
        self.world.agent_sync["read"] = {"FFX-1": [self.message("CLAUDE", 5)], "FFX-1 thing": [self.message("CLAUDE", 4)]}
        got = self.read([{"identifier": "FFX-1", "seat": "CLAUDE"}])
        self.assertEqual(len(got["by_ident"]["FFX-1"]["events"]), 2)
        self.assertEqual(got["reads"], 2)

    def test_a_failed_read_is_ok_false(self):
        self.world.agent_sync["topics"] = [{"name": "FFX-1 thing", "max_id": 3}]
        self.world.agent_sync["read_rc"] = {"FFX-1 thing": 6}
        self.assertEqual(self.read([{"identifier": "FFX-1", "seat": "X"}])["by_ident"]["FFX-1"], {"ok": False})

    def test_a_topic_list_that_may_be_cut_off_makes_a_missing_topic_unknown(self):
        self.world.agent_sync["topics"] = [{"name": "T%d" % i, "max_id": i} for i in range(zulip_topics.TOPICS_LIMIT)]
        self.assertEqual(self.read([{"identifier": "FFX-1", "seat": "X"}])["by_ident"]["FFX-1"], {"ok": False})

    def test_the_read_budget_skips_the_rest_without_failing(self):
        self.world.agent_sync["topics"] = [{"name": "FFX-%d t" % i, "max_id": i} for i in range(1, 200)]
        wanted = [{"identifier": "FFX-%d" % i, "seat": "X"} for i in range(1, 150)]
        got = self.read(wanted)
        self.assertEqual(got["reads"], zulip_topics.MAX_READS)
        self.assertEqual(got["skipped"], 149 - zulip_topics.MAX_READS)
        self.assertEqual(len(got["by_ident"]), zulip_topics.MAX_READS)
        self.assertEqual(got["status"], "ok")

    def test_the_time_budget_stops_the_reads(self):
        self.world.agent_sync["topics"] = [{"name": "FFX-%d t" % i, "max_id": i} for i in range(1, 5)]
        clock = iter([0, 0, 1000, 1000, 1000, 1000, 1000])
        got = self.read([{"identifier": "FFX-%d" % i, "seat": "X"} for i in range(1, 5)], monotonic=lambda: next(clock))
        self.assertGreaterEqual(got["skipped"], 1)

    def test_a_topic_list_that_fails_or_is_not_a_list(self):
        original = self.world.handle
        self.world.handle = lambda argv: Result(5, "", "x") if argv[1] == "topics" else original(argv)
        got = self.read([{"identifier": "FFX-1", "seat": "X"}])
        self.assertEqual(got["status"], "failed")
        self.world.handle = lambda argv: Result(0, "{}", "")
        self.assertEqual(self.read([{"identifier": "FFX-1", "seat": "X"}])["status"], "failed")

    def test_nothing_wanted_runs_nothing(self):
        self.assertEqual(self.read([])["status"], "ok")
        self.assertEqual(self.world.calls, [])


class ClaimBands(unittest.TestCase):
    def claim(self, ident, seat, comment_hours, branch=None, claimed_hours=200):
        return {"identifier": ident, "url": "https://linear.app/example-workspace/issue/%s/x" % ident, "title": "t", "seat": seat,
                "branch": branch, "branch_exact": True, "claimed_at": ago(claimed_hours), "claimed_basis": "claimed_comment",
                "linear_comments": {"ok": True, "events": [{"at": ago(h), "author": seat, "sweeper": False} for h in comment_hours]}}

    def block(self, claim_list, **kw):
        linear = {"status": "ok", "reason": None, "data": {"claims": claim_list, "orphans": [], "unreadable": []}}
        github = kw.pop("github", {"status": "absent", "reason": "x", "data": None})
        autopush = kw.pop("autopush", {"status": "absent", "reason": "x", "data": None})
        return claims.build(linear, github, autopush, kw.pop("rows", []), launch_at=kw.pop("launch_at", None), now=NOW,
                            zulip=kw.pop("zulip", None), book=urls.UrlBook())

    def test_sweeper_comments_are_not_activity(self):
        c = self.claim("FFX-1", "CLAUDE", [60])
        c["linear_comments"]["events"].append({"at": ago(1), "author": "CLAUDE", "sweeper": True})
        got = self.block([c])
        self.assertEqual(got["idle_48h"][0]["idle_hours"], 60.0)

    def test_the_owner_is_not_banded(self):
        got = self.block([self.claim("FFX-1", "OWNER", [100]), self.claim("FFX-2", "JAY", [100]), self.claim("FFX-3", "CLAUDE", [1])])
        self.assertEqual(got["owner_held"], 2)
        self.assertEqual(got["total"], 3)
        self.assertEqual(got["counts"]["none"], 1)
        self.assertEqual(got["idle_48h"], [])

    def test_the_boundaries(self):
        got = self.block([self.claim("FFX-%d" % h, "CLAUDE", [h]) for h in (35, 36, 47, 48)])
        by = {c["identifier"]: c["band"] for c in got["idle_36h"] + got["idle_48h"]}
        self.assertEqual(by, {"FFX-36": "36h", "FFX-47": "36h", "FFX-48": "48h"})
        self.assertEqual(got["counts"], {"none": 1, "36h": 2, "48h": 1, "unknown": 0})

    def test_the_launch_floor_and_the_claim_start_are_baselines(self):
        got = self.block([self.claim("FFX-1", "CLAUDE", [], claimed_hours=100)], launch_at=NOW - timedelta(hours=40))
        self.assertEqual(got["idle_36h"][0]["basis"], "launch_at")
        self.assertEqual(got["idle_36h"][0]["idle_hours"], 40.0)
        self.assertTrue(got["clock_started"])
        got = self.block([self.claim("FFX-1", "CLAUDE", [], claimed_hours=100)])
        self.assertEqual(got["idle_48h"][0]["basis"], "claimed_at")

    def test_c1_snapshots_count_when_c1_is_readable(self):
        rows = [{"branch": "claude/a", "branch_exact": True, "last_snapshot_push_at": ago(2)}]
        c = self.claim("FFX-1", "CLAUDE", [60], branch="claude/a")
        got = self.block([c], autopush={"status": "ok", "reason": None, "data": {}}, rows=rows)
        self.assertEqual(got["counts"]["none"], 1)
        got = self.block([c], autopush={"status": "failed", "reason": "boom", "data": None}, rows=rows)
        self.assertEqual(got["counts"]["unknown"], 1)

    def test_a_branch_that_is_not_exact_matches_nothing(self):
        c = self.claim("FFX-1", "CLAUDE", [60], branch="claude/a")
        c["branch_exact"] = False
        github = {"status": "ok", "reason": None, "data": {"prs": [{"same_repo": True, "head_ref": "claude/a", "head_ref_exact": True,
                                                                     "updated_at": ago(1), "head_commit": {"committed_at": ago(1)}}]}}
        got = self.block([c], github=github)
        self.assertEqual(got["idle_48h"][0]["identifier"], "FFX-1")
        self.assertIn("pr_update", got["idle_48h"][0]["unchecked"])

    def test_a_pr_update_is_activity_and_its_head_time_is_the_push(self):
        c = self.claim("FFX-1", "CLAUDE", [60], branch="claude/a")
        github = {"status": "ok", "reason": None, "data": {"prs": [{"same_repo": True, "head_ref": "claude/a", "head_ref_exact": True,
                                                                     "updated_at": ago(50), "head_commit": {"committed_at": ago(3)}}]}}
        got = self.block([c], github=github)
        self.assertEqual(got["counts"]["none"], 1)

    def test_the_module_uses_the_one_activity_definition(self):
        self.assertIs(claims.activity, activity)
        for module in Path(claims.__file__).parent.glob("*.py"):
            text = module.read_text(encoding="utf-8")
            self.assertNotIn("def activity_at", text, module.name)
            self.assertNotIn("def idle_band", text, module.name)


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def put(self, name, data, mode=0o600):
        path = self.root / name
        path.write_bytes(data)
        path.chmod(mode)
        return str(path)

    def refused(self, path):
        with self.assertRaises(files.Refusal) as caught:
            files.read_private_file(path, "the file")
        self.assertEqual(caught.exception.code, 2)
        return str(caught.exception)

    def test_reading(self):
        self.assertEqual(files.read_private_file(self.put("a", "café".encode()), "x"), "café")
        self.assertEqual(files.read_private_file(self.put("b", b"\xef\xbb\xbfhi"), "x"), "hi")           # a byte-order mark is dropped
        self.assertEqual(files.read_private_file(self.put("c", b"ok", 0o644), "x"), "ok")             # readable by others is fine
        self.assertIn("written by others", self.refused(self.put("d", b"x", 0o666)))
        self.assertIn("written by others", self.refused(self.put("e", b"x", 0o620)))
        self.assertIn("NUL", self.refused(self.put("f", b"a\x00b")))
        self.assertIn("UTF-8", self.refused(self.put("g", b"\xff\xfe\xfa")))
        self.assertIn("larger than", self.refused(self.put("h", b"x" * (files.MAX_FILE_BYTES + 1))))
        self.assertIn("cannot open", self.refused(str(self.root / "missing")))
        self.assertIn("not a regular file", self.refused(str(self.root)))

    def test_a_symlink_and_a_fifo_are_refused(self):
        real = self.put("real", b"x")
        link = self.root / "link"
        link.symlink_to(real)
        self.assertIn("cannot open", self.refused(str(link)))
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.assertIn("not a regular file", self.refused(str(fifo)))

    def test_writing_is_private_and_atomic(self):
        target = self.root / "new" / "deep" / "out.json"
        files.write_private_file(str(target), "café\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "café\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["out.json"])

    def test_writing_over_a_symlink_replaces_the_link_and_never_writes_through_it(self):
        victim = self.root / "victim"
        victim.write_text("keep")
        link = self.root / "out"
        link.symlink_to(victim)
        files.write_private_file(str(link), "new")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), "new")

    def test_load_facts_checks_the_shape(self):
        good = '{"schema": 1, "facts_id": "ol-20261011-0700-mac", "urls": [], "full_list_url": "x", "missing": [], "sources": {}}'
        self.assertEqual(files.load_facts(self.put("ok.json", good.encode()))["facts_id"], "ol-20261011-0700-mac")
        for bad in ('{"schema": 1}', "[1]", "nope", good.replace("ol-20261011-0700-mac", "ol-bad")):
            with self.assertRaises(files.Refusal):
                files.load_facts(self.put("bad.json", bad.encode()))


class HelperAccessor(unittest.TestCase):
    def test_textfmt_is_loaded_by_path_like_the_other_helpers(self):
        fmt = safety.textfmt()
        self.assertEqual(fmt.sentence_gap("One.  Two."), "One.  Two.")
        self.assertIs(safety.textfmt(), fmt)


if __name__ == "__main__":
    unittest.main()
