"""The owner's @all reaches recently active chats (chat_fanout, owner 2026-10-19).

`@everyone` is inbox-only and does not fan out (Jay, 2026-10-24)."""

from __future__ import annotations

import time
import unittest
from unittest import mock

from agent_sync import chat_fanout as F
from agent_sync import router as R
from agent_sync.tests.test_daemon import DaemonHarness

NOW = 1_800_000_000.0
HOUR = 3600.0
ST_URL = "https://github.com/example-org/Socratic-Trade"


def chat(n, minutes_ago, platform="grok-leader", cwd=None, title="chat", repo=None):
    return {
        "id": f"id{n}",
        "session_id": f"s{n}",
        "platform": platform,
        "seat": "GROK",
        "host": "mac",
        "cwd": cwd,
        "repo": repo,
        "title": title,
        "updated_at": NOW - minutes_ago * 60,
        "capabilities": {"wake": True},
    }


def cloud(n, minutes_ago, repo_url):
    """A Cursor cloud chat:  its cwd is `cursor-cloud://<id>`, and the catalog carries the repo URL."""
    return chat(
        n, minutes_ago, platform="cursor-cloud", cwd=f"cursor-cloud://bc-{n}", repo=repo_url
    )


def ids(rows):
    return [r["session_id"] for r in rows]


class PickTests(unittest.TestCase):
    def test_four_or_fewer_are_all_picked_and_nothing_older_than_six_hours(self):
        rows = [chat(1, 10), chat(2, 200), chat(3, 359), chat(4, 361)]
        self.assertEqual(ids(F.pick_recent(rows, NOW)), ["s1", "s2", "s3"])

    def test_more_than_four_cut_at_the_longest_pause(self):
        # 5, 10 minutes, then a long pause, then three more:  the two before the pause.
        rows = [chat(1, 5), chat(2, 10), chat(3, 200), chat(4, 210), chat(5, 220)]
        self.assertEqual(ids(F.pick_recent(rows, NOW)), ["s1", "s2"])
        # Steady activity:  a tie keeps the most (four).
        rows = [chat(i, i * 10) for i in range(1, 8)]
        self.assertEqual(ids(F.pick_recent(rows, NOW)), ["s1", "s2", "s3", "s4"])
        # The pause after the third.
        rows = [chat(1, 5), chat(2, 6), chat(3, 7), chat(4, 120), chat(5, 121), chat(6, 122)]
        self.assertEqual(ids(F.pick_recent(rows, NOW)), ["s1", "s2", "s3"])

    def test_everyone_scope_picks_per_platform(self):
        rows = [chat(i, i) for i in range(1, 7)] + [chat(9, 30, platform="cursor-cloud")]
        picked = F.select(rows, NOW, F.Scope())
        self.assertEqual(sum(r["platform"] == "grok-leader" for r in picked), 4)
        self.assertIn("s9", ids(picked))


class ScopeTests(unittest.TestCase):
    def test_forms(self):
        st = frozenset({"socratic-trade"})
        for text in (
            "@**all** for ST: deploy now",
            "@**everyone** for Socratic Trade: x",
            "ST @**all** go",
            "@ALL for ST: x",
            "@all for https://github.com/x/Socratic-Trade/pull/9: x",
        ):
            self.assertEqual(F.scope(text, "misc").repos, st, text)
        self.assertEqual(F.scope("@**all** hi", "ST-9 deploy").repos, st)
        self.assertIsNone(
            F.scope("@**all** CT broke, please look", "misc").repos, "the body is never scanned"
        )
        self.assertIsNone(
            F.scope("@**all** for the record: deploy is out", "misc").repos,
            "a `for X:` that names no app is prose, so the alert keeps its everyone reach",
        )
        self.assertEqual(
            F.scope("@**all** for CT, UM: x", "misc").repos,
            frozenset({"congress.trade", "usage-monitor"}),
        )
        afc = F.scope("@**all** for AFC: x", "misc").repos
        self.assertIn("ai-fleet-coordinator", afc)
        issue = F.scope("@**all** for SWU-32: look", "misc")
        self.assertEqual(
            (issue.repos, issue.issues), (frozenset({"simple-with-us"}), frozenset({"SWU-32"}))
        )
        self.assertEqual(F.scope("SWU-32 @**all**", "misc").issues, frozenset({"SWU-32"}))
        url = "https://github.com/jaywedgeworth-example/Socratic-Trade/pull/12345"
        self.assertEqual(
            F.scope(url + " @**all** look", "misc").repos, st, "a full PR link fits the prefix form"
        )

    def test_prefix_names_are_complete_and_multiword(self):
        import re
        aliases = (("Example Trade", "example-trade"), ("Demo Market", "demo-market"), ("Sample With Friends", "sample-with-friends"))
        registry = {"EX": tuple(repo for _, repo in aliases)}
        resolvers = [(repo, re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)) for name, repo in aliases]
        with (
            mock.patch.object(F, "TEAM_REPOS", registry),
            mock.patch.object(F, "_NAME_RES", resolvers),
            mock.patch.object(F, "_ACRONYM_RE", re.compile(r"(?<![\w-])(EX)(?![\w-])")),
            mock.patch.object(F, "_REPO_TEAM", {repo: "EX" for _, repo in aliases}),
        ):
            for name, repo in aliases:
                self.assertEqual(F.scope(name + " @all look", "misc").repos, frozenset({repo}))
            self.assertIsNone(F.scope("EX deploy is stuck @all look", "misc").repos)
            self.assertIsNone(F.scope("EX deploy is stuck.  @all look", "misc").repos)

    def test_a_cloud_chats_id_is_never_taken_for_its_repo(self):
        row = cloud(1, 1, None)
        self.assertIsNone(F.ChatPlaces(lambda *a: "").repo(row))

    def test_a_slightly_future_timestamp_counts_as_now(self):
        rows = [chat(1, -2), chat(2, -60)]
        self.assertEqual(
            ids(F.select(rows, NOW, F.Scope())), ["s1"], "2 minutes ahead counts;  an hour does not"
        )

    def test_git_queries_share_one_budget(self):
        calls = []

        def slow(cwd, *args):
            calls.append(cwd)
            time.sleep(0.05)
            return ""

        places = F.ChatPlaces(slow, budget=0.12)
        rows = [chat(i, 1, platform="claude", cwd=f"/w/{i}") for i in range(10)]
        F.select(rows, NOW, F.Scope(frozenset({"example-trade"})), places)
        self.assertLess(len(calls), 5, "the budget stops further git queries")

    def test_repo_scope_takes_every_recent_chat_on_that_repo(self):
        rows = [cloud(i, i, ST_URL) for i in range(1, 7)]
        rows += [
            cloud(8, 1, "https://github.com/example-org/Congress.Trade"),
            cloud(9, 400, ST_URL),
        ]
        picked = F.select(rows, NOW, F.Scope(frozenset({"socratic-trade"})))
        self.assertEqual(
            ids(picked),
            ["s1", "s2", "s3", "s4", "s5", "s6"],
            "no cap, and none older than six hours",
        )

    def test_local_chats_are_matched_by_git_origin_and_branch(self):
        git = {
            ("/w/st", "config"): "git@github.com:example-user/example-trade.git",
            ("/w/st", "branch"): "claude/swu-32-thing",
            ("/w/ct", "config"): ST_URL.replace("Socratic-Trade", "Congress.Trade"),
            ("/w/ct", "branch"): "main",
        }
        places = F.ChatPlaces(lambda cwd, *args: git.get((cwd, args[0]), ""))
        rows = [
            chat(1, 5, platform="claude", cwd="/w/st"),
            chat(2, 5, platform="claude", cwd="/w/ct"),
            chat(3, 5, platform="claude", cwd="/w/ct", title="SWU-32 follow-up"),
        ]
        self.assertEqual(
            ids(F.select(rows, NOW, F.Scope(frozenset({"example-trade"})), places)), ["s1"]
        )
        on_issue = F.select(
            rows, NOW, F.Scope(frozenset({"simple-with-us"}), frozenset({"SWU-32"})), places
        )
        self.assertEqual(ids(on_issue), ["s1", "s3"], "branch or title carries the id")
        nobody = F.select(
            rows, NOW, F.Scope(frozenset({"example-trade"}), frozenset({"ST-1"})), places
        )
        self.assertEqual(ids(nobody), ["s1"], "no chat on the issue:  its repo instead")

    def test_router_flags_typed_and_autocompleted_mentions(self):
        for text in ("@**all** x", "@ALL x"):
            self.assertTrue(R.fleet_wake_mention(text, []), text)
        for text in ("@**everyone** x", "ST @everyone"):
            self.assertFalse(R.fleet_wake_mention(text, []), text)
            self.assertIn("everyone", R.broadcast_tokens(text))
        self.assertEqual(R.broadcast_tokens("mail@allstate.com and @alloy"), frozenset())
        c = R.classify(
            {"id": 1, "sender_id": 12, "content": "@everyone x", "flags": [], "timestamp": NOW,
             "client": "website", "type": "stream", "display_recipient": "misc", "subject": "t"},
            R.SeatIdentity("CLAUDE", 10, "Claude", "c@z"),
            R.Context(owner_user_id=12, owner_clients=["website"], eligible_user_ids=[], is_bot={},
                      now=NOW, stale_after=7200, presence=[]),
        )
        self.assertIn("everyone_inert", c.labels())


class FakeCatalog:
    def __init__(self, rows):
        self.rows = rows

    def discover(self, *args, **kwargs):
        return self.rows


class DaemonFanoutTests(DaemonHarness):
    def test_the_owners_repo_alert_queues_a_reminder_for_each_chat_once(self):
        daemon = self.started()
        now = self.clock.time()
        st = [
            dict(cloud(i, 0, ST_URL), seat="CLAUDE", updated_at=now - 60 * i) for i in range(1, 6)
        ]
        st[0]["capabilities"] = {
            "wake": False
        }  # busy:  still queued, and the queue holds it until idle
        other = dict(chat(9, 0, platform="claude", repo=ST_URL), seat="CLAUDE", updated_at=now - 60)
        daemon._chat_catalog = FakeCatalog(st + [other])
        message_id = self.fake.add_message(
            "Jay Wedgeworth", "agent-sync", "misc", "@**all** for ST: deploy is out"
        )
        queue = daemon.chat_queue("CLAUDE")
        self.pump_until(daemon, lambda: len(queue.snapshot()["items"]) >= 5)
        items = queue.snapshot()["items"]
        self.assertEqual(
            len(items), 5, "every Socratic-Trade chat, no cap;  the Claude chat has no transport"
        )
        self.assertTrue(
            all(item["message_id"] == str(message_id) and item["owner"] for item in items)
        )
        result = daemon.fanout(
            message_id, "agent-sync", "misc", F.Scope(frozenset({"socratic-trade"}))
        )
        self.assertEqual(
            (result["queued"], result["already"], result["no_transport"]), (0, 5, {"claude": 1})
        )

    def test_malformed_bindings_are_not_coerced_into_queue_identifiers(self):
        daemon = self.started()
        row = dict(cloud(1, 0, ST_URL), seat="CLAUDE", host=7, updated_at=self.clock.time())
        daemon._chat_catalog = FakeCatalog([row])
        result = daemon.fanout(123, "agent-sync", "misc", F.Scope())
        self.assertEqual(result.get("failed"), 1)
        self.assertEqual(daemon.chat_queue("CLAUDE").snapshot()["items"], [])

    def test_absent_cwd_is_refused_by_the_real_queue_contract(self):
        daemon = self.started()
        row = dict(cloud(1, 0, None), seat="CLAUDE", cwd=None, updated_at=self.clock.time())
        daemon._chat_catalog = FakeCatalog([row])
        result = daemon.fanout(123, "agent-sync", "misc", F.Scope())
        self.assertEqual(result.get("failed"), 1)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(daemon.chat_queue("CLAUDE").snapshot()["items"], [])

    def test_catalog_failure_does_not_log_external_exception_text(self):
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        with mock.patch.object(daemon._chat_catalog, "discover", side_effect=RuntimeError("private external body")):
            with mock.patch.object(daemon.log, "write") as logged:
                result = daemon.fanout(123, "agent-sync", "misc", F.Scope())
        self.assertEqual(result["error"], "catalog")
        self.assertNotIn("private external body", repr(logged.call_args_list))

    def test_the_owners_all_fans_out_without_the_marker(self):
        # The control for the marker test below:  the same owner `@all`, unmarked, does fan out.
        # Without this, the marker test could pass for the wrong reason (a broken catalog, a
        # peer sender, a harness that never routes) and prove nothing (AFC-468).
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog(
            [dict(cloud(1, 0, ST_URL), seat="CLAUDE", updated_at=self.clock.time())]
        )
        message_id = self.fake.add_message(
            "Jay Wedgeworth", "agent-sync", "misc", "@**all** for ST: hi"
        )
        self.pump_until(daemon, lambda: message_id in daemon._fanout_seen)
        self.assertIn(message_id, daemon._fanout_seen, "the unmarked owner @all fans out")

    def test_a_marker_message_does_not_start_a_fanout(self):
        # The owner's `@all` is exactly the case where the fanout would wake every seat, so the
        # marker has to stop it (AFC-468).  The sender is the owner:  a peer's `@all` never fans
        # out at all, so a peer here would make the test vacuous.
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog(
            [dict(cloud(1, 0, ST_URL), seat="CLAUDE", updated_at=self.clock.time())]
        )
        message_id = self.fake.add_message(
            "Jay Wedgeworth", "agent-sync", "misc", ":eyes: @**all** for ST: hi"
        )
        # The daemon has to have seen the message:  the wait is on the routing ring, not on a
        # delivery, because the delivery is exactly what must not happen.
        self.pump_until(daemon, lambda: message_id in daemon.global_ring)
        time.sleep(0.3)
        self.assertNotIn(message_id, daemon._fanout_seen, "the marker stops the fanout")
        self.assertEqual(daemon.chat_queue("CLAUDE").snapshot()["items"], [])
        row = self.route_row(message_id)
        self.assertIsNotNone(row, "the route was logged")
        self.assertEqual(row["decision"]["drop"], "no_wake", "the log row carries the reason")
        self.assertIn("no_wake", row["classes"], "the marker is in the class labels")

    def route_row(self, message_id: int) -> dict | None:
        """The listener's route log row for one message id, if it was routed."""
        import json

        log = self.state_dir / "logs" / "listener.log"
        if not log.exists():
            return None
        for line in log.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") == "route" and row.get("id") == message_id:
                return row
        return None

    def test_a_peers_all_reaches_the_working_chats(self):
        """Jay's Oct 27 2026:  a fleet bot's `@all` wakes like the owner's, so it reaches the
        working chats the same way."""
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog(
            [dict(cloud(1, 0, ST_URL), seat="CLAUDE", updated_at=self.clock.time())]
        )
        message_id = self.fake.add_message("Codex", "agent-sync", "misc", "@**all** for ST: hi")
        self.pump_until(daemon, lambda: message_id in daemon._fanout_seen or bool(self.inbox()))
        time.sleep(0.3)
        self.assertIn(message_id, daemon._fanout_seen, "a peer's @all fans out like the owner's")
        self.assertTrue(daemon.chat_queue("CLAUDE").snapshot()["items"])


if __name__ == "__main__":
    unittest.main()
