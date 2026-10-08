"""The listener daemon end to end against the fake Zulip server, a fake clock and a fake claude:
routing, backfill, leases, wakes, budgets, the kill switch, recovery and secrecy."""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import adapters as A
from agent_sync import live as L
from agent_sync import wakes as W
from agent_sync.daemon import LIVE_DIR_KEEP
from agent_sync.tests.fake_zulip import EPOCH
from agent_sync.tests.harness import write_rc
from agent_sync.tests.listener_harness import ListenerHarness, fake_watcher, reply_output

MENTION = "@**Claude** can you check the AFC cutover?"
TOPIC_KEY = L.topic_key("agent-sync", "t")


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class DaemonHarness(ListenerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.write_config()
        self.pin()

    def started(self, **kw):
        daemon = self.daemon(**kw)
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        return daemon

    def wake_cycle(self, daemon, seconds: float = 25.0) -> None:
        self.clock.advance(seconds)
        daemon.tick()
        daemon.run_jobs("CLAUDE")

    def bot_posts(self):
        return [m for m in self.fake.messages if m["sender_id"] == 10]


class RoutingTests(DaemonHarness):
    def test_first_connect_starts_at_the_newest_message_and_replays_nothing(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        daemon = self.started()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(json.loads(open(self.seat_paths().cursor).read())["last_id"], self.fake.messages[-1]["id"])
        registration = self.fake.registrations[-1]
        self.assertEqual(json.loads(registration["params"]["event_types"]),
                         ["message", "update_message", "user_topic", "realm_user"])
        self.assertNotIn("narrow", registration["params"])
        self.assertEqual(registration["params"]["apply_markdown"], "false")

    def test_a_peer_mention_lands_in_the_inbox_and_plain_chatter_does_not(self) -> None:
        daemon = self.started()
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.fake.add_message("Codex", "agent-sync", "t", "just chatter")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        daemon.pump("CLAUDE", timeout=0.5)
        rows = self.inbox()
        self.assertEqual([r["id"] for r in rows], [mid])
        self.assertEqual(rows[0]["seq"], 1)
        self.assertIn("direct", rows[0]["classes"])
        self.assertEqual(self.ledger()[0]["state"], "queued")
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertNotIn("can you check", log, "the log never holds a body")

    def test_bad_event_queue_id_then_backfill_from_the_cursor(self) -> None:
        daemon = self.started()
        self.fake.expire_queues()
        missed = self.fake.add_message("Codex", "agent-sync", "t", MENTION, deliver=False)
        dm = self.fake.add_direct_message("Jay Wedgeworth", "are you there?")
        daemon.pump("CLAUDE", timeout=1.0)
        self.assertFalse(daemon.seats["CLAUDE"].connected)
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        self.assertEqual(sorted(r["id"] for r in self.inbox()), [missed, dm])
        self.assertEqual(len(self.fake.registrations), 2)

    def test_events_that_arrive_during_the_backfill_are_neither_lost_nor_doubled(self) -> None:
        daemon = self.started()
        self.fake.expire_queues()
        daemon.pump("CLAUDE", timeout=1.0)
        before = len(self.fake.requests_to("GET", "messages"))
        self.fake.when(lambda: len(self.fake.requests_to("GET", "messages")) > before,
                       lambda: self.fake.add_message("Codex", "agent-sync", "t", MENTION))
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertEqual(len(self.inbox()), 1)

    def test_a_backlog_over_the_topic_cap_keeps_the_newest_with_a_gap_marker(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "busy")], mentions=False)
        self.fake.expire_queues()
        daemon.pump("CLAUDE", timeout=1.0)
        ids = [self.fake.add_message("Codex", "agent-sync", "busy", "m%d" % n, deliver=False) for n in range(205)]
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        items = self.live_items("L1")
        messages = [i["id"] for i in items if i["kind"] == "message"]
        self.assertEqual(messages, ids[-200:])
        gaps = [i for i in items if i["kind"] == "gap"]
        self.assertEqual(len(gaps), 1)

    def test_one_message_reaches_each_seat_once(self) -> None:
        codex_key = secrets.token_hex(16)
        self.extra_keys.append(("codex-bot@zulip.test", codex_key))
        self.fake.add_bot("codex-bot@zulip.test", "Codex", codex_key)
        write_rc(self.secrets_dir / "Codex-zuliprc", email="codex-bot@zulip.test", key=codex_key, site=self.fake.url)
        self.write_config(seats='[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n\n[seat.CODEX]\nbot = "Codex"\nwake = "inbox"\n')
        daemon = self.started()
        self.assertTrue(daemon.connect_seat("CODEX"))
        mid = self.fake.add_message("Cursor", "agent-sync", "t", "@**Claude** and @**Codex** please both look")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.pump_until(daemon, lambda: len(self.inbox("CODEX")) >= 1, seat="CODEX")
        daemon.route_message(daemon.seats["CLAUDE"], dict(self.fake.messages[-1], flags=["mentioned"]))
        self.assertEqual([r["id"] for r in self.inbox()], [mid])
        self.assertEqual([r["id"] for r in self.inbox("CODEX")], [mid])
        self.assertEqual([r["state"] for r in self.ledger("CODEX")], [], "an inbox seat never wakes")

    def test_own_posts_reach_sibling_leases_only_and_topics_match_without_case(self) -> None:
        daemon = self.started()
        self.write_lease("poster", topics=[("agent-sync", "AFC Work")], session_id="aaaa1111-0000")
        self.write_lease("sibling", topics=[("AGENT-SYNC", "afc work")], session_id="bbbb2222-0000")
        self.fake.add_message(self.fake.me, "agent-sync", "afc WORK", "[CLAUDE·aaaa1111] working on it")
        self.pump_until(daemon, lambda: len(self.live_items("sibling")) >= 1)
        self.assertEqual([i["kind"] for i in self.live_items("sibling")], ["sibling"])
        self.assertEqual(self.live_items("poster"), [])
        self.assertEqual(self.inbox(), [])

    def test_a_rename_rewrites_leases_and_cli_cursors(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "old name")])
        state = self.seat_paths().seat_dir + "/11112222/state.json"
        L.write_json(state, {"cursors": {L.topic_key("agent-sync", "old name"): 120}})
        self.fake.add_message("Codex", "agent-sync", "old name", "first", deliver=False)
        self.fake.rename_topic("agent-sync", "old name", "✔ old name")
        self.pump_until(daemon, lambda: L.load_lease(self.seat_paths(), "L1")["topics"][0]["topic"] != "old name")
        self.assertEqual(L.load_lease(self.seat_paths(), "L1")["topics"][0]["topic"], "✔ old name")
        self.assertEqual(L.read_json(state)["cursors"], {L.topic_key("agent-sync", "✔ old name"): 120})

    def test_a_content_only_edit_is_ignored(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "t")])
        mid = self.fake.add_message("Codex", "agent-sync", "t", "hello")
        self.pump_until(daemon, lambda: len(self.live_items("L1")) == 1)
        self.fake.edit_content(mid, "@**Claude** edited in a mention")
        daemon.pump("CLAUDE", timeout=0.5)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertEqual(len(self.live_items("L1")), 1)
        self.assertEqual(self.inbox(), [])

    def test_a_muted_topic_drops_group_mentions_but_not_direct_ones(self) -> None:
        daemon = self.started()
        self.fake.set_topic_policy(self.fake.me, "agent-sync", "noisy", 1)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertIn(L.topic_key("agent-sync", "noisy"), daemon.seats["CLAUDE"].muted)
        self.fake.add_message("Codex", "agent-sync", "noisy", "@**all** heads up")
        direct = self.fake.add_message("Codex", "agent-sync", "noisy", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual([r["id"] for r in self.inbox()], [direct])

    def test_an_owner_api_post_is_not_the_owner_and_is_flagged_once(self) -> None:
        daemon = self.started()
        jay = self.fake.user_named("Jay Wedgeworth")
        self.fake.add_message(jay, "agent-sync", "t", MENTION, client="ZulipPython")
        self.fake.add_message(jay, "agent-sync", "t", MENTION + " again", client="ZulipPython")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 2)
        rows = self.inbox()
        self.assertTrue(all(r["owner_api"] and not r["owner"] for r in rows))
        self.assertEqual(self.ledger(), [], "never a wake")
        flagged = [b for b in self.banners if "API client" in b[8]]
        self.assertEqual(len(flagged), 1)

    def test_a_mention_goes_to_one_wake_capable_session_instead_of_a_wake(self) -> None:
        self.write_config(rewake=True)
        daemon = self.started()
        self.write_lease("old", last_prompt=10.0)
        self.write_lease("new", last_prompt=50.0)
        for lease_id in ("old", "new"):
            self.addCleanup(os.close, fake_watcher(self.seat_paths(), lease_id))
        mid = self.fake.add_message("Codex", "other", "elsewhere", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual([(i["id"], i["class"]) for i in self.live_items("new")], [(mid, "interrupt")])
        self.assertEqual(self.live_items("old"), [])
        self.assertEqual(self.inbox()[0]["delivered_to"], "new")
        self.assertEqual(self.ledger(), [])

    def test_an_owner_follow_up_soon_after_a_live_turn_is_still_an_interrupt(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "t")])
        now = self.clock.time()
        L.write_json(self.seat_paths().budget("L1"), {"turns": [{"ts": now - 60, "topic": TOPIC_KEY, "owner": True},
                                                               {"ts": now - 30, "topic": TOPIC_KEY, "owner": False}]})
        owner = self.fake.add_message(self.fake.user_named("Jay Wedgeworth"), "agent-sync", "t", "and one more thing")
        peer = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.live_items("L1")) >= 2)
        classes = {i["id"]: i["class"] for i in self.live_items("L1") if i["kind"] == "message"}
        self.assertEqual(classes, {owner: "interrupt", peer: "passive"})
        self.assertEqual([i for i in self.live_items("L1") if i["kind"] == "throttle"], [], "routine spacing is silent")
        self.assertFalse(any("live budget" in " ".join(b) for b in self.banners))

    def test_without_a_watcher_the_session_is_not_wake_capable(self) -> None:
        self.write_config(rewake=True)
        daemon = self.started()
        self.write_lease("nowatcher")
        self.fake.add_message("Codex", "other", "elsewhere", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual(self.live_items("nowatcher"), [])
        self.assertIsNone(self.inbox()[0]["delivered_to"])


class LeaseLifecycleTests(DaemonHarness):
    def test_claude_agents_decides_liveness_and_a_dead_lease_returns_its_items(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "t")])
        self.set_claude(mode="ok", output=reply_output(), agents=[{"pid": os.getpid(), "kind": "interactive"}])
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.live_items("L1")) >= 1)
        self.assertEqual(self.inbox(), [])
        self.set_claude(mode="ok", output=reply_output(), agents=[{"pid": os.getpid(), "kind": "background"}])
        self.clock.advance(31)
        daemon.tick()
        self.assertFalse(os.path.exists(self.seat_paths().lease("L1")))
        rows = self.inbox()
        self.assertEqual([(r["id"], r["returned_from"]) for r in rows], [(mid, "L1")])
        self.assertEqual(self.ledger()[0]["trigger_ids"], [mid], "a returned item goes back through the wake decision")

    def test_only_positive_evidence_from_claude_agents_kills_a_lease(self) -> None:
        daemon = self.started()
        self.write_lease("L1")
        lease = L.load_lease(self.seat_paths(), "L1")
        for agents in ([], [{"pid": 1, "kind": "interactive"}], {"unexpected": "shape"}, [{"PID": os.getpid()}]):
            with self.subTest(agents=agents):
                self.set_claude(mode="ok", output=reply_output(), agents=agents)
                daemon.agents_cache = (-1e9, None)
                self.assertEqual(daemon.lease_alive(self.seat_paths(), lease), (True, "ok"))
        self.set_claude(mode="ok", output=reply_output(), agents=[{"pid": os.getpid(), "kind": "Background"}])
        daemon.agents_cache = (-1e9, None)
        self.assertFalse(daemon.lease_alive(self.seat_paths(), lease)[0])

    def test_a_reused_pid_kills_the_lease(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "t")], platform="codex", pid_start=1000)
        self.starts[os.getpid()] = 1000
        alive, why = daemon.lease_alive(self.seat_paths(), L.load_lease(self.seat_paths(), "L1"))
        self.assertTrue(alive, why)
        self.starts[os.getpid()] = 5000
        daemon.start_cache.clear()
        alive, why = daemon.lease_alive(self.seat_paths(), L.load_lease(self.seat_paths(), "L1"))
        self.assertEqual((alive, why), (False, "pid reused"))
        self.write_lease("L2", platform="codex", pid=dead_pid())
        self.assertEqual(daemon.lease_alive(self.seat_paths(), L.load_lease(self.seat_paths(), "L2"))[1], "pid gone")

    def test_a_silent_lease_releases_wake_worthy_items_after_ten_minutes(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "t")], platform="codex", heartbeat=self.clock.time() + 3600)
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.fake.add_message("Codex", "agent-sync", "t", "chatter only")
        self.pump_until(daemon, lambda: len(self.live_items("L1")) >= 2)
        self.clock.advance(300)
        daemon.tick()
        self.assertEqual(self.inbox(), [])
        self.clock.advance(301)
        daemon.last_tick["grace"] = -1e9
        daemon.tick()
        self.assertEqual([(r["id"], r["released_from"]) for r in self.inbox()], [(mid, "L1")])
        self.assertEqual(len(L.pending_items(self.seat_paths(), "L1")), 1, "the chatter stays with the lease")


    def test_a_deaf_lease_releases_a_mention_once_and_the_headless_wake_answers_it(self) -> None:
        daemon = self.started()
        self.write_lease("deaf", topics=[("agent-sync", "t")])  # no watcher and rewake not verified:  cannot wake
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.live_items("deaf")) >= 1)
        self.assertEqual(self.live_items("deaf")[0]["class"], "interrupt")
        for _ in range(40):  # 20 minutes at the real cadence:  a grace check every 30 seconds, a 20-second window
            self.clock.advance(30)
            daemon.tick()
            daemon.run_jobs("CLAUDE")
        self.assertEqual(len([i for i in self.live_items("deaf") if i.get("id") == mid]), 1, "never copied back")
        self.assertEqual([r["id"] for r in self.inbox()], [mid], "one seat-inbox row")
        self.assertEqual(len(self.dumps()), 1, "the grace release ends in exactly one headless wake")
        posts = self.bot_posts()
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0]["content"].startswith("[CLAUDE·wake] re=%d" % mid))
        self.assertEqual([r["state"] for r in self.ledger()], ["queued", "accepted", "started", "done"])
        self.assertEqual(self.ledger()[0]["skip_leases"], ["deaf"])

    def test_a_lease_written_again_after_a_reap_is_not_treated_as_reaped(self) -> None:
        daemon = self.started()
        self.write_lease("L1", topics=[("agent-sync", "t")], platform="codex", heartbeat=self.clock.time() + 10 * 86400)
        returned = os.path.join(self.seat_paths().live_dir("L1"), "returned.json")
        L.write_json(returned, {"ts": self.clock.time() - LIVE_DIR_KEEP - 100})  # a reap from before this lease
        daemon.reap(daemon.seats["CLAUDE"])
        self.assertFalse(os.path.exists(returned))
        self.assertTrue(os.path.exists(self.seat_paths().lease("L1")))
        self.assertTrue(os.path.isdir(self.seat_paths().live_dir("L1")), "a live lease's directory is never removed")
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.live_items("L1")) >= 1)
        self.assertEqual(self.inbox(), [])

    def test_a_reap_that_races_a_hook_rewriting_the_lease_leaves_it_alone(self) -> None:
        daemon = self.started()
        for lease_id, removed in (("L2", False), ("L3", True)):
            with self.subTest(lease_file_removed_first=removed):
                self.write_lease(lease_id, topics=[("agent-sync", "t")])
                L.append_live(self.seat_paths(), lease_id, [{"kind": "message", "class": "interrupt", "id": 5,
                                                             "channel": "agent-sync", "topic": "t", "classes": ["direct"],
                                                             "wake_worthy": True}])
                ended = os.path.join(self.seat_paths().live_dir(lease_id), "ended.json")
                L.write_json(ended, {"ts": self.clock.time()})
                if removed:  # SessionEnd ran:  ended.json written and the lease file unlinked
                    os.unlink(self.seat_paths().lease(lease_id))
                real = daemon.lease_alive

                def racing(paths, lease, lease_id=lease_id, ended=ended):
                    result = real(paths, lease)  # dead, and then the SessionStart hook writes the lease again
                    os.unlink(ended)
                    L.update_lease(paths, lease_id, lambda d: d.update(lease_id=lease_id, created=self.clock.time() + 1,
                                                                       pid=os.getpid(), platform="claude-code"))
                    return result

                with mock.patch.object(daemon, "lease_alive", racing):
                    daemon.reap(daemon.seats["CLAUDE"])
                self.assertTrue(os.path.exists(self.seat_paths().lease(lease_id)))
                self.assertEqual([i["id"] for i in L.pending_items(self.seat_paths(), lease_id)], [5],
                                 "nothing was taken back")
                self.assertEqual(self.inbox(), [])
                os.unlink(self.seat_paths().lease(lease_id))  # each scenario reaps only its own lease
                shutil.rmtree(self.seat_paths().live_dir(lease_id))


class WakeTests(DaemonHarness):
    def test_a_peer_mention_wakes_claude_and_the_daemon_posts_the_reply(self) -> None:
        daemon = self.started()
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(19)
        daemon.tick()
        self.assertEqual([r["state"] for r in self.ledger()], ["queued"], "still inside the 20-second window")
        self.wake_cycle(daemon, 2)
        states = [r["state"] for r in self.ledger()]
        self.assertEqual(states, ["queued", "accepted", "started", "done"])
        done = self.ledger()[-1]
        self.assertEqual((done["action"], done["cost_usd"]), ("reply", 0.03))
        post = self.bot_posts()[-1]
        self.assertEqual(post["id"], done["posted_id"])
        self.assertEqual((post["display_recipient"], post["subject"]), ("agent-sync", "t"))
        self.assertTrue(post["content"].startswith("[CLAUDE·wake] re=%d\n" % mid))
        prompt = self.dumps()[0]["stdin"]
        self.assertIn("Trigger message ids: %d" % mid, prompt)
        self.assertIn("BEGIN_UNTRUSTED_ZULIP nonce=", prompt)
        self.assertIn("can you check", prompt)

    def test_an_owner_dm_is_answered_by_dm_only(self) -> None:
        daemon = self.started()
        dm = self.fake.add_direct_message("Jay Wedgeworth", "status of the cutover?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 6)
        posts = self.bot_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["type"], "private")
        self.assertEqual(sorted(r["id"] for r in posts[0]["display_recipient"]), [10, 12])
        self.assertTrue(posts[0]["content"].startswith("[CLAUDE·wake] re=%d" % dm))
        self.assertIn("direct message thread", self.dumps()[0]["stdin"])
        self.assertTrue(self.ledger()[0]["owner"])

    def test_an_unpinned_binary_or_unpinned_owner_never_runs_claude(self) -> None:
        os.unlink(self.seat_paths().wake_pin)
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual(self.ledger()[-1]["reason"], "not_pinned")
        self.assertEqual(self.dumps(), [])
        self.assertTrue(any("not_pinned" in b[8] for b in self.banners))
        self.pin()
        self.write_config(owner=0)
        daemon.reload()
        self.fake.add_message("Codex", "agent-sync", "u", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 3)
        self.wake_cycle(daemon)
        self.assertEqual(self.ledger()[-1]["reason"], "owner_not_pinned")
        self.assertEqual(self.dumps(), [])

    def test_usd_per_day_stops_even_owner_wakes(self) -> None:
        daemon = self.started()
        now = self.clock.time()
        for n in range(8):
            daemon.seats["CLAUDE"].ledger.append({"ts": now - 100, "wake_id": "old%d" % n, "seat": "CLAUDE",
                                                  "state": "started", "owner": True, "topic_key": "k%d" % n})
        self.fake.add_direct_message("Jay Wedgeworth", "please answer", deliver=True)
        self.pump_until(daemon, lambda: any(r["state"] == "queued" for r in self.ledger()))
        self.wake_cycle(daemon, 6)
        self.assertEqual(self.ledger()[-1]["reason"], "usd_per_day")
        self.assertEqual(self.dumps(), [])

    def test_usd_per_day_counts_waiting_wakes_so_two_owner_wakes_cannot_overshoot(self) -> None:
        self.write_config(budget="usd_per_day = 0.30")  # each run costs 0.03, so only the reservation stops the second
        daemon = self.started()
        jay = self.fake.user_named("Jay Wedgeworth")
        self.fake.add_message(jay, "agent-sync", "one", MENTION)
        self.fake.add_message(jay, "agent-sync", "two", MENTION)
        self.pump_until(daemon, lambda: sum(1 for r in self.ledger() if r["state"] == "queued") >= 2)
        self.wake_cycle(daemon, 6)
        self.assertEqual(len(self.dumps()), 1)
        self.assertEqual([r.get("reason") for r in self.ledger() if r["state"] == "dropped"], ["usd_per_day"])

    def test_a_restored_accepted_wake_is_checked_again_before_it_runs(self) -> None:
        now = self.clock.time()
        rows = [{"ts": now - 100, "wake_id": "spent", "seat": "CLAUDE", "state": "started", "owner": True,
                 "topic_key": "k0", "reserved_usd": 0.25},
                {"ts": now - 50, "wake_id": "spent", "seat": "CLAUDE", "state": "done", "owner": True,
                 "topic_key": "k0", "cost_usd": 1.70}]
        for n in range(2):
            pending = W.Pending("acc%d" % n, "CLAUDE", {"channel": "agent-sync", "topic": "r%d" % n}, owner=True,
                                now=now - 30, due=now - 25)
            pending.add({"id": 900 + n, "ts": now - 30}, True)
            rows += [pending.row("queued", now - 30), pending.row("accepted", now - 25)]
        L.append_jsonl(self.seat_paths().wakes, rows)
        daemon = self.started()
        self.assertEqual(len(daemon.seats["CLAUDE"].jobs), 2)
        daemon.run_jobs("CLAUDE")
        self.assertEqual(len(self.dumps()), 1, "1.70 spent + 0.25 waiting + 0.25 is over 2.00, so only one runs")
        self.assertEqual([r.get("reason") for r in self.ledger() if r["state"] == "dropped"], ["usd_per_day"])

    def test_the_wake_runs_the_pinned_realpath_not_the_symlink(self) -> None:
        versions = self.tmp / "versions"
        versions.mkdir()
        real = versions / "claude-2.1.290"
        os.replace(self.claude, real)
        os.symlink(real, self.claude)
        self.pin()
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual(self.dumps()[0]["argv"][0], os.path.realpath(real))

    def test_claude_agents_runs_only_when_pinned_with_agents_ok_and_never_while_paused(self) -> None:
        calls = []

        def agents(argv):
            calls.append(list(argv))
            return json.dumps([{"pid": os.getpid(), "kind": "interactive"}])

        self.pin(agents_ok=False)
        daemon = self.started(agents_runner=agents)
        self.write_lease("L1")
        lease = L.load_lease(self.seat_paths(), "L1")
        self.assertEqual(daemon.lease_alive(self.seat_paths(), lease), (True, "ok"))
        self.assertEqual(calls, [], "no agents_ok in the pin:  liveness rests on the pid and its start time")
        self.pin(agents_ok=True)
        daemon.agents_cache = (-1e9, None)
        daemon.lease_alive(self.seat_paths(), lease)
        self.assertEqual(calls, [[os.path.realpath(self.claude), "agents", "--json"]])
        daemon.pause(seat=None, wakes_only=True, by="test")
        daemon.agents_cache = (-1e9, None)
        daemon.lease_alive(self.seat_paths(), lease)
        self.assertEqual(len(calls), 1, "never while paused")
        with mock.patch.object(A.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "[]", "")
            A.run_agents(["claude", "agents", "--json"], {}, str(self.tmp))
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_peer_budget_per_topic(self) -> None:
        daemon = self.started()
        for n in range(3):
            self.fake.add_message("Codex", "agent-sync", "t", MENTION + " %d" % n)
            self.pump_until(daemon, lambda n=n: sum(1 for r in self.ledger() if r["state"] == "queued") >= n + 1)
            self.wake_cycle(daemon, 95)
        reasons = [r.get("reason") for r in self.ledger() if r["state"] == "dropped"]
        self.assertEqual(reasons, ["per_topic_per_hour"])
        self.assertEqual(len(self.dumps()), 2)

    def test_the_kill_switch_and_the_owner_dm_pause(self) -> None:
        daemon = self.started()
        self.assertEqual(self.run_cli("daemon", "pause", "--wakes-only").code, 0)
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual(self.ledger()[-1]["reason"], "paused")
        self.assertEqual(self.run_cli("daemon", "resume").code, 0)
        self.fake.add_direct_message("Jay Wedgeworth", "  Agent-Sync Pause ", deliver=True)
        self.pump_until(daemon, lambda: "*" in L.read_json(os.path.join(self.root, "listener", "pause.json")))
        pause = L.read_json(os.path.join(self.root, "listener", "pause.json"))["*"]
        self.assertEqual((pause["by"], pause["wakes_only"]), ("owner-dm", False))
        self.assertEqual(len([r for r in self.ledger() if r["state"] == "queued"]), 1, "the pause DM itself never wakes")
        codex = self.fake.user_named("Codex")
        self.fake.add_direct_message(codex, "agent-sync resume", recipients=[self.fake.me], deliver=True)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertTrue(L.paused(self.root, "CLAUDE"), "no message ever resumes")

    def test_single_flight_fifo_overflow(self) -> None:
        daemon = self.started()
        for n in range(7):
            self.fake.add_message(self.fake.user_named("Jay Wedgeworth"), "agent-sync", "topic %d" % n, MENTION)
        self.pump_until(daemon, lambda: len([r for r in self.ledger() if r["state"] == "queued"]) >= 7)
        self.clock.advance(6)
        daemon.tick()
        states = [r["state"] for r in self.ledger() if r["state"] != "queued"]
        self.assertEqual(states.count("accepted"), 5)
        self.assertEqual([r.get("reason") for r in self.ledger() if r["state"] == "dropped"], ["overflow", "overflow"])
        self.assertEqual(daemon.run_jobs("CLAUDE", limit=1), 1)
        self.assertEqual(len(daemon.seats["CLAUDE"].jobs), 4)

    def test_a_session_that_takes_the_topic_before_the_spawn_gets_the_trigger(self) -> None:
        daemon = self.started()
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(25)
        daemon.tick()
        self.write_lease("late", topics=[("agent-sync", "t")])
        daemon.run_jobs("CLAUDE")
        self.assertEqual((self.ledger()[-1]["state"], self.ledger()[-1]["reason"]), ("skipped", "leased"))
        self.assertEqual([(i["id"], i["class"]) for i in self.live_items("late")], [(mid, "interrupt")])
        self.assertEqual(self.live_items("late")[0]["routed_at"], self.clock.time(), "the grace period starts now")
        self.assertEqual(self.inbox()[0]["delivered_to"], "late")
        self.assertEqual(self.dumps(), [])

    def test_a_takeover_after_the_seat_inbox_rotated_still_hands_the_trigger_over(self) -> None:
        daemon = self.started()
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(25)
        daemon.tick()
        paths = self.seat_paths()
        os.replace(paths.seat_inbox, paths.seat_inbox + ".1")
        self.write_lease("late", topics=[("agent-sync", "t")])
        daemon.run_jobs("CLAUDE")
        self.assertEqual([(i["id"], i["class"]) for i in self.live_items("late")], [(mid, "interrupt")])
        self.assertEqual(self.dumps(), [])

    def test_a_session_that_takes_the_topic_during_the_run_gets_a_draft(self) -> None:
        harness = self

        class Racing(A.ClaudeRunner):
            def run(self, *args, **kw):
                result = super().run(*args, **kw)
                harness.write_lease("racer", topics=[("agent-sync", "t")])
                return result

        daemon = self.started(claude_runner=Racing(timeout=20))
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual(self.bot_posts(), [])
        drafts = [i for i in self.live_items("racer") if i["kind"] == "draft"]
        self.assertEqual(len(drafts), 1)
        self.assertIn("The cutover is in review", drafts[0]["content"])
        self.assertEqual((drafts[0]["trigger_sender_id"], drafts[0]["trigger_owner"]), (11, False))
        meta = json.loads(L.wrap_block(drafts, "n", 1500).splitlines()[1])
        self.assertEqual((meta["kind"], meta["trigger_sender_id"]), ("draft", 11))
        self.assertEqual(self.ledger()[-1]["draft_to"], "racer")

    def test_schema_violations_secrets_and_tools_never_post(self) -> None:
        daemon = self.started()
        cases = [
            ({"mode": "ok", "output": dict(reply_output(), extra="field")}, "keys extra extra"),
            ({"mode": "ok", "output": reply_output("here you go: " + self.key)}, None),
            ({"mode": "tool", "hang": 30, "output": reply_output()}, None),
        ]
        for n, (control, invalid) in enumerate(cases):
            self.set_claude(**control)
            self.fake.add_message("Codex", "agent-sync", "topic %d" % n, MENTION)
            self.pump_until(daemon, lambda n=n: sum(1 for r in self.ledger() if r["state"] == "queued") >= n + 1)
            self.wake_cycle(daemon)
        self.assertEqual(self.bot_posts(), [])
        finals = [r for r in self.ledger() if r["state"] in ("done", "failed")]
        self.assertEqual(finals[0]["invalid"], "keys extra extra")
        self.assertEqual(finals[0]["action"], "none")
        self.assertTrue(finals[1]["refused"].startswith("secret scanner"))
        self.assertEqual((finals[2]["state"], finals[2]["reason"]), ("failed", "the init event listed tools or MCP servers"))

    def test_escalation_notifies_the_owner_and_acks_an_owner_trigger(self) -> None:
        daemon = self.started()
        self.set_claude(mode="ok", output={"action": "escalate", "reply": None, "board": None,
                                           "owner_note": "Jay asked for a production deploy."})
        self.fake.add_message(self.fake.user_named("Jay Wedgeworth"), "agent-sync", "t", MENTION + " deploy now")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 6)
        posts = self.bot_posts()
        self.assertEqual(len(posts), 1)
        self.assertIn(W.OWNER_ACK, posts[0]["content"])
        queue = L.read_jsonl(self.seat_paths().owner_queue)
        self.assertEqual(queue[-1]["note"], "Jay asked for a production deploy.")
        self.assertTrue(all("production deploy" not in arg for banner in self.banners for arg in banner),
                        "a banner never shows the note")

    def test_board_filing_is_off_by_default_and_a_list_argv_when_on(self) -> None:
        board_calls = []

        def board_runner(argv, **kw):
            board_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.set_claude(mode="ok", output={"action": "board", "reply": None, "owner_note": None,
                                           "board": {"title": "Fix it", "severity": "P2", "desc": "details"}})
        daemon = self.started(board_runner=board_runner, board_bin="/fake/board")
        self.fake.add_message("Codex", "agent-sync", "CT#2316 sentry", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertEqual(board_calls, [])
        self.assertEqual(self.ledger()[-1]["board_skipped"], "board_per_day")
        self.write_config(budget="board_per_day = 3")
        daemon.reload()
        mid = self.fake.add_message("Codex", "agent-sync", "CT#2316 sentry", MENTION + " again")
        self.pump_until(daemon, lambda: sum(1 for r in self.ledger() if r["state"] == "queued") >= 2)
        self.wake_cycle(daemon, 100)
        self.assertEqual(len(board_calls), 1)
        argv = board_calls[0]
        self.assertEqual(argv[:2], ["/fake/board", "file"])
        self.assertIn("--app=CT", argv)
        self.assertIn("zulip:CLAUDE:%d" % mid, argv)
        self.assertEqual(self.ledger()[-1]["board_uid"], "zulip:CLAUDE:%d" % mid)

    def test_the_loop_guard_resets_only_on_an_owner_post(self) -> None:
        daemon = self.started()
        jay = self.fake.user_named("Jay Wedgeworth")
        for n in range(3):
            self.fake.add_message("Codex", "agent-sync", "t", "[CODEX·wake] re=%d\nreply" % n)
        self.fake.add_message("Codex", "agent-sync", "t", "a peer post")
        self.fake.add_message(jay, "agent-sync", "t", "API post from Jay's account", client="ZulipPython")
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertTrue(daemon.loopguard.blocked(TOPIC_KEY))
        self.assertEqual(self.ledger(), [])
        self.fake.add_message(jay, "agent-sync", "t", "Jay typing in the app")
        self.pump_until(daemon, lambda: not daemon.loopguard.blocked(TOPIC_KEY))
        self.fake.add_message("Codex", "agent-sync", "t", MENTION + " now")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)


class RecoveryTests(DaemonHarness):
    def test_queued_wakes_survive_a_restart(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        daemon.shutdown()
        again = self.started()
        self.wake_cycle(again)
        self.assertEqual([r["state"] for r in self.ledger()], ["queued", "accepted", "started", "done"])

    def test_a_crash_between_started_and_done_never_reruns(self) -> None:
        paths = self.seat_paths()
        L.append_jsonl(paths.wakes, [{"ts": self.clock.time(), "wake_id": "w1", "seat": "CLAUDE", "state": "started",
                                      "owner": True, "reserved_usd": 0.25, "trigger_ids": [5], "topic_key": TOPIC_KEY}])
        daemon = self.started()
        self.wake_cycle(daemon)
        self.assertEqual([r["state"] for r in self.ledger()], ["started", "failed"])
        self.assertEqual(self.dumps(), [])
        self.assertTrue(any("stopped mid-run" in b[8] for b in self.banners))

    def test_a_peer_wake_that_went_stale_while_the_daemon_was_down_never_runs(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        daemon.shutdown()
        self.clock.advance(5 * 3600)
        again = self.started()
        self.wake_cycle(again)
        self.assertEqual([(r["state"], r.get("reason")) for r in self.ledger()], [("queued", None), ("dropped", "stale")])
        self.assertEqual(self.dumps(), [])

    def test_a_clock_jump_reregisters_and_stale_backfill_wakes_the_owner_once(self) -> None:
        daemon = self.started()
        jay = self.fake.user_named("Jay Wedgeworth")
        old = EPOCH - 20000
        self.fake.add_message(jay, "agent-sync", "t", MENTION + " one", deliver=False, timestamp=old)
        self.fake.add_message(jay, "agent-sync", "t", MENTION + " two", deliver=False, timestamp=old + 1)
        self.fake.add_message("Codex", "agent-sync", "u", MENTION, deliver=False, timestamp=old)
        self.clock.jump_wall(600)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertFalse(daemon.seats["CLAUDE"].connected)
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        self.assertEqual(len(self.inbox()), 3)
        self.assertTrue(all(r["stale"] for r in self.inbox()))
        self.wake_cycle(daemon, 10)
        queued = {r["wake_id"] for r in self.ledger() if r["state"] == "queued"}
        self.assertEqual(len(queued), 1, "one stale owner wake for the topic; none for the stale peer mention")
        self.fake.add_message(jay, "agent-sync", "t", MENTION + " three", deliver=False, timestamp=old + 2)
        self.fake.expire_queues()
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        self.assertEqual(len({r["wake_id"] for r in self.ledger() if r["state"] == "queued"}), 1)

    def test_a_dead_queue_with_no_heartbeat_reconnects(self) -> None:
        daemon = self.started()
        self.clock.advance(200)
        daemon.pump("CLAUDE", timeout=0.1)
        daemon.pump("CLAUDE", timeout=0.1)
        self.assertFalse(daemon.seats["CLAUDE"].connected)

    def test_429_retry_after_is_honoured(self) -> None:
        daemon = self.started()
        self.fake.inject("GET", "events", status=429, body={"result": "error", "code": "RATE_LIMIT_HIT", "msg": "slow"},
                         headers={"Retry-After": "7"})
        daemon.pump("CLAUDE", timeout=1.0)
        self.assertIn(7.0, self.clock.slept)
        self.assertTrue(daemon.seats["CLAUDE"].connected)

    def test_network_errors_back_off_5_30_60_120(self) -> None:
        daemon = self.daemon()
        self.fake.inject("GET", "users/me", status=500, body={"result": "error", "msg": "boom"}, times=10)
        delays = []
        for _ in range(5):
            daemon.connect_seat("CLAUDE")
            runner = daemon.seats["CLAUDE"]
            delays.append(round(runner.next_attempt - self.clock.monotonic()))
        self.assertEqual(delays, [5, 30, 60, 120, 120])


class SecurityTests(DaemonHarness):
    def test_admin_and_owner_bot_keys_are_refused_and_moderators_accepted(self) -> None:
        for role, ok in ((200, False), (100, False), (300, True), (400, True), (600, False)):
            with self.subTest(role=role):
                self.fake.me["role"] = role
                self.fake.me["is_admin"] = role in (100, 200)
                self.fake.me["is_owner"] = role == 100
                daemon = self.daemon()
                self.assertEqual(daemon.connect_seat("CLAUDE"), ok, daemon.seats["CLAUDE"].error)
                if not ok:
                    self.assertIn("refuses admin and owner keys", daemon.seats["CLAUDE"].error)
                daemon.shutdown()
        self.fake.me.update(role=400, is_admin=False, is_owner=False)

    def test_a_bot_promoted_to_admin_loses_its_queue_at_once(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        self.fake.set_role(self.fake.me["user_id"], 200)
        self.pump_until(daemon, lambda: runner.fatal)
        self.assertFalse(runner.connected)
        self.assertIn("refuses admin and owner keys", runner.error)
        self.assertTrue(self.fake.deleted_queues, "the queue is deleted, not left to expire")
        self.assertTrue(any("refuses admin" in " ".join(b) for b in self.banners))
        self.assertEqual(daemon._wake_block(runner, W.Pending("w", "CLAUDE", {"channel": "agent-sync", "topic": "t"},
                                                                owner=True, now=0, due=0), self.clock.time()),
                         "seat_refused")
        self.fake.me.update(role=400, is_admin=False, is_owner=False)

    def test_a_crashing_thread_with_two_keys_leaks_neither(self) -> None:
        codex_key = secrets.token_hex(16)
        self.extra_keys.append(("codex-bot@zulip.test", codex_key))
        self.fake.add_bot("codex-bot@zulip.test", "Codex", codex_key)
        write_rc(self.secrets_dir / "Codex-zuliprc", email="codex-bot@zulip.test", key=codex_key, site=self.fake.url)
        self.write_config(seats='[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n\n[seat.CODEX]\nbot = "Codex"\nwake = "inbox"\n')
        err = io.StringIO()
        daemon = self.started(stderr=err)
        self.assertTrue(daemon.connect_seat("CODEX"))
        tokens = [self.key, codex_key] + [__import__("base64").b64encode(("%s:%s" % pair).encode()).decode()
                                          for pair in (("claude-bot@zulip.test", self.key), ("codex-bot@zulip.test", codex_key))]

        def explode(*args, **kw):
            raise RuntimeError("boom " + " ".join(tokens))

        daemon._route = explode
        self.fake.add_message("Cursor", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertTrue(self.inbox()[0]["quarantined"], "three crashes quarantine the message to the inbox")
        daemon.install_excepthooks()
        self.addCleanup(setattr, threading, "excepthook", threading.__excepthook__)
        self.addCleanup(setattr, sys, "excepthook", sys.__excepthook__)
        thread = threading.Thread(target=explode, name="worker")
        thread.start()
        thread.join()
        self.transcript.append(err.getvalue())
        self.assertIn("boom [redacted]", err.getvalue())
        log = (self.state_dir / "logs" / "listener.log").read_text()
        self.assertIn("[redacted]", log)

    def test_files_and_dirs_are_private(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        daemon.write_status()
        for path in self.state_dir.rglob("*"):
            if path.name == "listener.toml":
                continue
            self.assertEqual(os.stat(path).st_mode & 0o077, 0, "%s is not private" % path)

    def test_status_output(self) -> None:
        daemon = self.started()
        daemon.write_status()
        result = self.run_cli("daemon", "status", "--json")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertFalse(info["running"])
        self.assertTrue(info["status"]["seats"]["CLAUDE"]["connected"])
        self.assertTrue(info["status"]["seats"]["CLAUDE"]["pinned"])
        self.assertEqual(info["plugin"]["repo"], "1.0.0")
        human = self.run_cli("status")
        self.assertIn("CLAUDE", human.out)
        self.assertIn("(pinned)", human.out)


class ThreadedRunTests(DaemonHarness):
    """The production loop:  poller, router and wake worker threads, then a clean stop."""

    def test_run_routes_wakes_and_stops_cleanly(self) -> None:
        from agent_sync.daemon import Clock

        self.write_config(coalesce=0.2, owner_coalesce=0.1)
        daemon = self.daemon(clock=Clock())
        self.assertEqual(daemon.config.errors, [])
        self.addCleanup(setattr, threading, "excepthook", threading.__excepthook__)
        self.addCleanup(setattr, sys, "excepthook", sys.__excepthook__)
        result: list[int] = []
        runner = threading.Thread(target=lambda: result.append(daemon.run()), daemon=True)
        runner.start()
        deadline = time.monotonic() + 20
        while not self.fake.queues and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.fake.queues, "the poller never registered")
        self.fake.add_message("Codex", "agent-sync", "t", MENTION, timestamp=int(time.time()))  # real clock: not stale
        while not any(r["state"] == "done" for r in self.ledger()) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.ledger()[-1]["state"], "done")
        self.assertEqual(len(self.bot_posts()), 1)
        second = self.daemon(clock=Clock())
        self.assertEqual(second.run(), 1, "a second listener refuses to start")
        daemon.stop.set()
        runner.join(10)
        self.assertEqual(result, [0])
        self.assertTrue(self.fake.deleted_queues, "the queue is deleted on the way out")
        status = L.read_json(os.path.join(self.root, "listener", "status.json"))
        self.assertTrue(status["seats"]["CLAUDE"]["cursor"])

    def test_a_seat_removed_by_a_reload_stops_polling_and_one_added_back_routes_again(self) -> None:
        from agent_sync.daemon import Clock

        self.write_config(coalesce=0.2, owner_coalesce=0.1)
        daemon = self.daemon(clock=Clock())
        self.addCleanup(setattr, threading, "excepthook", threading.__excepthook__)
        self.addCleanup(setattr, sys, "excepthook", sys.__excepthook__)
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(daemon.stop.set)

        def wait(predicate, seconds=20.0):
            deadline = time.monotonic() + seconds
            while not predicate() and time.monotonic() < deadline:
                time.sleep(0.05)
            return predicate()

        self.assertTrue(wait(lambda: self.fake.queues), "the poller never registered")
        old = daemon.seats["CLAUDE"]
        self.assertTrue(wait(lambda: "poll" in old.threads))
        self.write_config(seats="")
        daemon.reload_requested = True
        self.assertTrue(wait(lambda: not old.threads["poll"].is_alive()), "the removed seat's poller kept running")
        registrations = len(self.fake.registrations)
        time.sleep(1.5)
        self.assertEqual(len(self.fake.registrations), registrations, "a removed seat never registers again")
        self.write_config(coalesce=0.2, owner_coalesce=0.1)
        daemon.reload_requested = True
        self.assertTrue(wait(lambda: daemon.seats.get("CLAUDE") not in (None, old) and daemon.seats["CLAUDE"].connected))
        self.assertTrue(daemon.seats["CLAUDE"].threads["poll"].is_alive())
        self.fake.add_message("Codex", "agent-sync", "t", MENTION, timestamp=int(time.time()))
        self.assertTrue(wait(lambda: len(self.inbox()) == 1), "the seat added back routes its messages")

    def test_the_entry_script_runs_the_daemon_and_sigterm_stops_it(self) -> None:
        import signal

        process_env = self.env()
        process_env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[2] / "agent-sync"), "daemon", "run"],
                                   env=process_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out = err = ""
        try:
            deadline = time.monotonic() + 30
            while not self.fake.queues and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.fake.queues, "the daemon never registered")
            self.fake.add_message("Codex", "agent-sync", "t", MENTION)
            while not self.inbox() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(len(self.inbox()), 1)
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.transcript.extend([out, err])
        self.assertEqual(process.returncode, 0, err)
        self.assertTrue(self.fake.deleted_queues)


if __name__ == "__main__":
    unittest.main()
