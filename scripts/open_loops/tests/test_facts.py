"""build_facts:  the whole gather, with the outside world scripted."""
import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from fleet_facts.runner import Result

from open_loops import central, urls, zulip_topics
from open_loops.facts import build_facts

from .builders import NOW, SCRIPTS, Sandbox, ago, board_row
from .scenes import lane, standard_world

C6_GOLDEN = SCRIPTS / "needs_owner" / "tests" / "fixtures" / "list_golden.json"        # what C6's `list --json` really prints


class Facts(Sandbox):
    def facts(self, world=None, **kw):
        self.world = world or standard_world(self.home, self.bin)
        kw.setdefault("now", NOW)
        kw.setdefault("sleep", lambda s: None)
        return build_facts(run=self.world.runner(), **kw)

    def test_identity_and_the_owner_clock(self):
        f = self.facts()
        self.assertEqual(f["schema"], 1)
        self.assertRegex(f["facts_id"], r"^ol-20261011-0700-[a-z0-9-]+$")
        self.assertEqual(f["built_at_local"], "Sun, Oct 11, 7:00am")
        self.assertEqual(f["built_at"], "2026-10-11T12:00:00Z")
        self.assertNotIn("CDT", json.dumps(f))
        for name in ("github", "linear", "lanes", "autopush", "needs_owner"):
            self.assertEqual(f["sources"][name]["status"], "ok", name)
        self.assertEqual(f["sources"]["asks"]["status"], "absent")
        self.assertIn("not built yet", f["sources"]["asks"]["reason"])
        self.assertEqual(f["missing"], [])
        self.assertEqual(f["owner_asks"]["count"], None)

    def test_dst_correct_central_time(self):
        winter = datetime(2026, 12, 15, 13, 0, tzinfo=timezone.utc)      # 7:00am CST
        f = self.facts(now=winter)
        self.assertEqual(f["built_at_local"], "Tue, Dec 15, 7:00am")
        self.assertTrue(f["facts_id"].startswith("ol-20261215-0700-"))
        spring = datetime(2026, 3, 8, 7, 59, tzinfo=timezone.utc)        # the last minute before the clocks jump
        self.assertEqual(central.stamp(spring), "Sun, Mar 8, 1:59am")
        self.assertEqual(central.stamp(datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)), "Sun, Mar 8, 3:00am")
        self.assertEqual(central.stamp(datetime(2026, 11, 1, 6, 59, tzinfo=timezone.utc)), "Sun, Nov 1, 1:59am")
        self.assertEqual(central.stamp(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)), "Sun, Nov 1, 1:00am")

    def test_stuck_prs_use_head_age_with_a_48_hour_floor(self):
        f = self.facts()
        stuck = f["stuck_prs"]
        self.assertEqual(stuck["count"], 2)
        self.assertEqual([p["number"] for p in stuck["items"]], [1, 3])            # oldest head first
        self.assertEqual(stuck["items"][0]["head_age_hours"], 60.0)                # updated half an hour ago, head 60 hours old
        self.assertEqual(stuck["items"][1]["head_age_hours"], 48.0)
        self.assertEqual(stuck["items"][0]["state"], ["conflicting"])
        self.assertNotIn(2, [p["number"] for p in stuck["items"]])                 # 47.9 hours
        self.assertEqual(stuck["mergeable_pending"], 1)
        self.assertIn("head commit", stuck["note"])
        self.assertNotIn("fork text", json.dumps(f))                               # the outside fork is a count only

    def test_a_failed_source_is_missing_and_never_zero(self):
        world = standard_world(self.home, self.bin)
        world.github_fail = True
        f = self.facts(world)
        self.assertEqual(f["sources"]["github"]["status"], "failed")
        self.assertIn("github", [m["source"] for m in f["missing"]])
        self.assertIsNone(f["stuck_prs"]["count"])
        self.assertEqual(f["stuck_prs"]["items"], [])
        # every claim that has a branch needs the PR source, so each is "unknown", not idle and not fine
        self.assertEqual(f["claims"]["counts"]["unknown"] + f["claims"]["counts"]["none"] + f["claims"]["counts"]["36h"] + f["claims"]["counts"]["48h"], 5)
        ffx1 = [c for c in f["claims"]["unknown"] if c["identifier"] == "FFX-1"]
        self.assertTrue(ffx1)

    def test_linear_failure_is_missing_with_no_counts(self):
        world = standard_world(self.home, self.bin)
        original = world.handle

        def broken(argv):
            from fleet_facts.runner import Result
            return Result(5, "", "board: unreachable") if argv[0].endswith("board") else original(argv)
        world.handle = broken
        f = self.facts(world)
        self.assertEqual(f["sources"]["linear"]["status"], "failed")
        self.assertIsNone(f["decisions"]["count"])
        self.assertIsNone(f["claims"]["counts"])
        self.assertIn("linear", [m["source"] for m in f["missing"]])

    def test_linear_updated_at_is_not_activity(self):
        f = self.facts()
        by_id = {c["identifier"]: c for c in f["claims"]["idle_36h"] + f["claims"]["idle_48h"]}
        self.assertIn("FFX-5", by_id)                       # updated_at 6 minutes ago, newest comment 70 hours ago
        self.assertEqual(by_id["FFX-5"]["band"], "48h")
        self.assertEqual(by_id["FFX-5"]["basis"], "linear_comments")

    def test_claim_bands(self):
        f = self.facts()
        claims = f["claims"]
        bands = {c["identifier"]: c["band"] for c in claims["idle_36h"] + claims["idle_48h"] + claims["unknown"]}
        self.assertEqual(bands["FFX-2"], "36h")             # 40 hours
        self.assertEqual(bands["FFX-5"], "48h")             # 70 hours
        self.assertNotIn("FFX-1", bands)                    # its newest comment is 50 hours old but its PR moved half an hour ago
        self.assertNotIn("FFX-3", bands)                    # 5 hours:  none
        self.assertEqual(bands["FFX-4"], "unknown")         # board show failed
        self.assertEqual(claims["orphans"], 1)
        self.assertEqual(claims["counts"]["none"], 2)       # FFX-3 and FFX-1
        self.assertFalse(claims["clock_started"])
        self.assertIn("ages, not releases", claims["note"])

    def test_a_pr_update_counts_as_activity(self):
        # FFX-1's branch has PR 1, updated half an hour ago:  the claim is not idle even though its comments are old
        f = self.facts()
        ffx1 = [c for c in f["claims"]["idle_36h"] + f["claims"]["idle_48h"] + f["claims"]["unknown"] if c["identifier"] == "FFX-1"]
        self.assertEqual(ffx1, [])
        self.assertEqual(f["claims"]["counts"]["none"], 2)   # FFX-1 and FFX-3

    def test_the_launch_time_is_a_floor(self):
        f = self.facts(launch_at=ago(10))
        claims = f["claims"]
        self.assertTrue(claims["clock_started"])
        self.assertIsNone(claims["note"])
        self.assertEqual(claims["idle_36h"] + claims["idle_48h"], [])      # nobody has been idle for 36 hours since launch
        self.assertEqual(claims["counts"]["unknown"], 1)
        future = self.facts(launch_at=ago(-10))                           # a launch time in the future has not started the clock
        self.assertFalse(future["claims"]["clock_started"])
        self.assertIn("ages, not releases", future["claims"]["note"])

    def test_snapshot_only_work_has_no_age_floor(self):
        f = self.facts()
        off = f["not_on_github"]
        self.assertEqual(off["snapshot_only"]["count"], 1)
        item = off["snapshot_only"]["items"][0]
        self.assertEqual(item["lane"], "claude-snap")
        self.assertEqual(item["idle_hours"], 1.0)           # an hour old and listed
        self.assertTrue(item["repo"] == "Simple-With-Us/example-repo")
        self.assertEqual(off["armed_local_work"]["count"], 1)
        self.assertTrue(off["armed_local_work"]["items"][0]["pr_url"].endswith("/pull/7"))
        self.assertEqual(off["refusals"]["count"], 1)
        self.assertIn("gate failure", off["refusals"]["items"][0]["reason"])
        self.assertEqual(off["count"], 3)

    def test_unpushed_work_older_than_a_day_leaves_out_what_is_active_or_already_listed(self):
        f = self.facts()
        un = f["unpushed_over_a_day"]
        names = [i["lane"] for i in un["items"]]
        self.assertEqual(names, ["claude-never", "claude-stale"])      # both 30 hours:  by name
        self.assertNotIn("claude-busy", names)                           # changed an hour ago:  active
        self.assertNotIn("review-pr-9", names)
        self.assertNotIn("claude-snap", names)                           # section 3 has it
        self.assertEqual(un["active"], 2)                                # changed an hour ago, and one the doctor calls ACTIVE
        self.assertEqual((un["landed"], un["tool_cache"]), (1, 1))       # the doctor's verdicts:  landed and a tool's cache
        for gone in ("claude-landed", "claude-cache", "claude-running"):
            self.assertNotIn(gone, names)
        stale = [i for i in un["items"] if i["lane"] == "claude-stale"][0]
        self.assertEqual(stale["age_hours"], 30.0)

    def test_decisions(self):
        f = self.facts()
        d = f["decisions"]
        self.assertEqual(d["count"], 2)                                  # FFX-12 answered, FFX-13 closed
        self.assertEqual(d["answered_awaiting_close"], 1)
        self.assertEqual([i["identifier"] for i in d["items"]], ["FFX-10", "FFX-11"])
        first = d["items"][0]
        self.assertEqual(first["question"], "Pick a color")
        self.assertEqual(first["low_stakes"], [1, 2])
        self.assertTrue(first["tap_to_answer"])
        self.assertTrue(first["topic_url"].startswith(urls.REALM + "/#narrow/channel/%d-agent-sync/topic/" % urls.STREAM_ID))
        self.assertEqual(d["low_stakes"], 1)
        self.assertEqual(d["over_five"], ["CLAUDE"])
        self.assertTrue(first["url"].startswith("https://linear.app/"))

    def test_c6_is_asked_for_every_row_because_the_default_lists_only_the_open_ones(self):
        self.facts()
        asked = [c["argv"][1:] for c in self.world.calls if c["argv"][0].endswith("needs-owner")]
        self.assertEqual(asked, [["list", "--all", "--json"]])

    def test_an_answered_item_is_counted_from_the_c6_state_in_the_real_list_shape(self):
        golden = json.loads(C6_GOLDEN.read_text(encoding="utf-8"))
        replied = [r["identifier"] for r in golden if r["state"] == "replied"]
        awaiting = sorted(r["identifier"] for r in golden if r["state"] != "replied")
        self.assertTrue(replied and awaiting)
        world = standard_world(self.home, self.bin)
        world.needs_owner = [board_row(r["identifier"], seats=(), n=100 + i, title="OWNER DECISION:  " + r["question"], status="open")
                             for i, r in enumerate(golden)]
        world.docs["needs-owner"] = golden                                 # the very document C6's own golden test pins
        d = self.facts(world)["decisions"]
        self.assertEqual(d["answered_awaiting_close"], len(replied))
        self.assertEqual(sorted(i["identifier"] for i in d["items"]), awaiting)
        self.assertEqual(d["count"], len(awaiting))

    def test_a_replied_item_whose_filer_has_not_closed_it_is_counted_not_listed(self):
        d = self.facts()["decisions"]
        self.assertEqual(d["answered_awaiting_close"], 1)                  # FFX-12:  state replied in the C6 list
        self.assertNotIn("FFX-12", [i["identifier"] for i in d["items"]])

    def test_a_c6_closed_item_is_neither_listed_nor_counted_even_with_a_reply_on_it(self):
        world = standard_world(self.home, self.bin)
        world.needs_owner.append(board_row("FFX-14", seats=("CODEX",), n=14, title="OWNER DECISION:  Closed in C6 first", status="open"))
        world.docs["needs-owner"]["items"].append({"identifier": "FFX-14", "seat": "CODEX", "state": "closed", "replied": {"kind": "reaction", "id": "1", "option": 1}})
        d = self.facts(world)["decisions"]
        self.assertNotIn("FFX-14", [i["identifier"] for i in d["items"]])
        self.assertEqual(d["answered_awaiting_close"], 1)                  # still only FFX-12

    def test_an_item_missing_from_the_c6_list_is_listed_never_called_answered(self):
        world = standard_world(self.home, self.bin)
        world.needs_owner.append(board_row("FFX-15", seats=(), n=15, title="OWNER DECISION:  Labelled by hand", status="open"))
        d = self.facts(world)["decisions"]
        self.assertIn("FFX-15", [i["identifier"] for i in d["items"]])
        hand = [i for i in d["items"] if i["identifier"] == "FFX-15"][0]
        self.assertIsNone(hand["tap_to_answer"])                           # C6 knows nothing about it
        self.assertEqual(d["answered_awaiting_close"], 1)

    def test_lanes_whose_age_cannot_be_told_are_missing_not_none(self):
        world = standard_world(self.home, self.bin)
        world.lane_report["checkouts"].append(lane("/work/lanes/Example/claude-unaged", unpushed=2, safety="NEEDS-REVIEW"))
        f = self.facts(world)
        self.assertEqual(f["unpushed_over_a_day"]["unknown_age"], 1)
        self.assertEqual(f["unpushed_over_a_day"]["count"], 2)             # the two lanes whose age is known are still listed
        self.assertEqual([m for m in f["missing"] if m["source"] == "lanes"],
                         [{"source": "lanes", "reason": "ages unavailable for 1 lane with unpushed or dirty work"}])
        self.assertEqual(f["sources"]["lanes"]["status"], "ok")            # the source itself was read

    def test_a_lanes_source_that_is_already_missing_is_not_listed_twice(self):
        f = self.facts(no_mac=True)
        self.assertEqual([m["source"] for m in f["missing"]].count("lanes"), 1)

    def test_without_c6_low_stakes_and_over_five_are_unknown_not_zero(self):
        world = standard_world(self.home, self.bin)
        (self.bin / "needs-owner").unlink()
        f = self.facts(world)
        self.assertEqual(f["sources"]["needs_owner"]["status"], "absent")
        self.assertIsNone(f["decisions"]["low_stakes"])
        self.assertIsNone(f["decisions"]["over_five"])
        self.assertEqual(f["decisions"]["count"], 3)                    # nothing says FFX-12 was answered
        self.assertNotIn("needs_owner", [m["source"] for m in f["missing"]])

    def test_at_all_is_inert_everywhere(self):
        world = standard_world(self.home, self.bin)
        f = self.facts(world)
        text = json.dumps(f, ensure_ascii=False)
        self.assertNotIn("@**", text)
        self.assertIn("＠**all**", text)                              # the full-width at sign
        question = [i for i in f["decisions"]["items"] if i["identifier"] == "FFX-11"][0]["question"]
        self.assertTrue(question.startswith("＠**all**"))

    def test_every_url_in_the_facts_is_in_the_urls_list(self):
        f = self.facts()
        found = set()

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key != "urls":
                        walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str) and "://" in node:
                found.add(node)
        walk(f)
        self.assertTrue(found)
        self.assertLessEqual(found, set(f["urls"]))
        self.assertIn(f["full_list_url"], f["urls"])
        self.assertEqual(f["urls"], sorted(set(f["urls"])))

    def test_a_secret_shaped_title_is_withheld(self):
        world = standard_world(self.home, self.bin)
        token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        world.pages[""]["data"]["search"]["nodes"][3]["title"] = "Clean and old " + token
        f = self.facts(world)
        self.assertNotIn(token, json.dumps(f))
        self.assertEqual(f["redacted"], ["fleet-facts:$.github.prs[3].title"])

    def test_no_mac_runs_nothing_local_and_reports_the_mac_parts_missing(self):
        f = self.facts(no_mac=True)
        names = [c["argv"][0].rsplit("/", 1)[-1] for c in self.world.calls]
        self.assertNotIn("lane", names)
        self.assertNotIn("fleet-autopush", names)
        self.assertNotIn("needs-owner", names)
        self.assertEqual(f["sources"]["lanes"]["status"], "absent")
        self.assertIn("no Mac facts file", f["sources"]["lanes"]["reason"])
        self.assertEqual({m["source"] for m in f["missing"]}, {"lanes", "autopush"})
        self.assertIsNone(f["not_on_github"]["count"])
        self.assertIsNone(f["unpushed_over_a_day"]["count"])
        self.assertTrue(f["no_mac"])

    def test_a_c1_document_this_version_cannot_read_is_a_failed_source(self):
        world = standard_world(self.home, self.bin)
        world.docs["fleet-autopush"] = {"something": "else"}
        f = self.facts(world)
        self.assertEqual(f["sources"]["autopush"]["status"], "failed")
        self.assertIn("autopush", [m["source"] for m in f["missing"]])
        self.assertIsNone(f["not_on_github"]["count"])


class Zulip(Sandbox):
    def facts(self, world, **kw):
        self.world = world
        return build_facts(run=world.runner(), now=NOW, sleep=lambda s: None, **kw)

    def world_with_topics(self):
        world = standard_world(self.home, self.bin)
        world.pages = {"": world.pages[""]}
        world.agent_sync["topics"] = [{"name": "FFX-2 forty hours", "max_id": 9}, {"name": "FFX-5 old signals", "max_id": 8},
                                       {"name": "FFX-20 not this one", "max_id": 7}]
        return world

    def posts(self, seat, hours):
        from datetime import timedelta
        return {"timestamp": int((NOW - timedelta(hours=hours)).timestamp()), "seat": seat, "topic": "x", "content": "hello"}

    def band(self, f, ident):
        for key in ("idle_36h", "idle_48h", "unknown"):
            for c in f["claims"][key]:
                if c["identifier"] == ident:
                    return c
        return None

    def test_the_claimants_own_post_in_its_topic_counts(self):
        world = self.world_with_topics()
        world.agent_sync["read"] = {"FFX-2 forty hours": [self.posts("MM", 3)]}
        f = self.facts(world)
        self.assertIsNone(self.band(f, "FFX-2"))                         # moved out of the 36 hour band
        self.assertEqual(f["sources"]["zulip"]["status"], "ok")

    def test_another_seats_post_does_not_count(self):
        world = self.world_with_topics()
        world.agent_sync["read"] = {"FFX-2 forty hours": [self.posts("CODEX", 3)]}
        f = self.facts(world)
        self.assertEqual(self.band(f, "FFX-2")["band"], "36h")

    def test_a_topic_read_that_fails_makes_the_claim_unknown(self):
        world = self.world_with_topics()
        world.agent_sync["read_rc"] = {"FFX-2 forty hours": 5}
        f = self.facts(world)
        self.assertEqual(self.band(f, "FFX-2")["band"], "unknown")

    def breaking_topics(self, world, result):
        original = world.handle
        world.handle = lambda argv: result if len(argv) > 1 and argv[0].endswith("agent-sync") and argv[1] == "topics" else original(argv)

    def idle_or_unknown(self, f):
        return {c["identifier"]: c["band"] for key in ("idle_36h", "idle_48h", "unknown") for c in f["claims"][key]}

    def test_a_topic_list_that_could_not_be_read_makes_idle_claims_unknown_never_idle(self):
        world = self.world_with_topics()
        self.breaking_topics(world, Result(3, "", "TLS handshake timeout"))
        f = self.facts(world)
        self.assertEqual(f["sources"]["zulip"]["status"], "failed")
        self.assertEqual(f["claims"]["idle_36h"] + f["claims"]["idle_48h"], [])
        bands = self.idle_or_unknown(f)
        self.assertEqual((bands["FFX-2"], bands["FFX-5"]), ("unknown", "unknown"))
        self.assertIn("zulip_topic", self.band(f, "FFX-2")["unknown_sources"])
        self.assertNotIn("zulip_topic", self.band(f, "FFX-2")["unchecked"])

    def test_a_topic_list_that_is_not_a_list_is_the_same_failure(self):
        world = self.world_with_topics()
        self.breaking_topics(world, Result(0, "{}", ""))
        f = self.facts(world)
        self.assertEqual(f["sources"]["zulip"]["status"], "failed")
        self.assertEqual(self.band(f, "FFX-2")["band"], "unknown")
        self.assertEqual(self.band(f, "FFX-5")["band"], "unknown")

    def test_a_claim_the_read_budget_skipped_is_unknown_never_idle(self):
        world = self.world_with_topics()
        with mock.patch.object(zulip_topics, "MAX_READS", 1):
            f = self.facts(world)
        self.assertEqual(f["claims"]["zulip"]["skipped"], 1)
        self.assertEqual(self.band(f, "FFX-2")["band"], "36h")             # it got its read
        self.assertEqual(self.band(f, "FFX-5")["band"], "unknown")         # it did not
        self.assertIn("zulip_topic", self.band(f, "FFX-5")["unknown_sources"])

    def test_a_failed_zulip_source_does_not_touch_a_claim_that_is_plainly_moving(self):
        world = self.world_with_topics()
        self.breaking_topics(world, Result(3, "", "x"))
        f = self.facts(world)
        self.assertNotIn("FFX-3", self.idle_or_unknown(f))                 # five hours:  never needed a Zulip read
        self.assertNotIn("FFX-1", self.idle_or_unknown(f))

    def test_a_claim_with_no_topic_is_checked_and_stays_idle(self):
        world = self.world_with_topics()
        f = self.facts(world)
        self.assertEqual(self.band(f, "FFX-5")["band"], "48h")
        self.assertIn("zulip_topic", self.band(f, "FFX-5")["checked"])

    def test_no_agent_sync_means_the_source_is_unchecked_not_failed(self):
        world = self.world_with_topics()
        (self.bin / "agent-sync").unlink()
        f = self.facts(world)
        self.assertEqual(f["sources"]["zulip"]["status"], "absent")
        self.assertIn("zulip_topic", self.band(f, "FFX-2")["unchecked"])
        self.assertEqual(self.band(f, "FFX-2")["band"], "36h")

    def test_only_idle_claims_cost_a_read(self):
        world = self.world_with_topics()
        self.facts(world)
        reads = [c["argv"][c["argv"].index("--topic") + 1] for c in world.calls if len(c["argv"]) > 1 and c["argv"][1] == "read"]
        self.assertEqual(sorted(reads), ["FFX-2 forty hours", "FFX-5 old signals"])


if __name__ == "__main__":
    unittest.main()
