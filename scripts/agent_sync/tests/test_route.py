"""Wake route suggestions (owner, Sat, Oct 10, 2026):  the tool-less CLAUDE wake may suggest paging
one fleet seat.  The daemon checks it deterministically and honors it only for an owner trigger in a
channel topic:  it posts `[CLAUDE·route→SEAT] re=<owner trigger>` with one live mention, counts it on
the loop guard, records it in the ledger and tells the owner.  Everything else is dropped and noted."""
from __future__ import annotations

import json
import textwrap

from agent_sync import live as L
from agent_sync import router as R
from agent_sync import wakes as W
from agent_sync.config import BUDGET_DEFAULTS
from agent_sync.tests.listener_harness import reply_output
from agent_sync.tests.test_daemon import TOPIC_KEY, DaemonHarness

OWNER_ASK = "@**Claude** get Codex to look at the CT build failure"
PARTITION = textwrap.dedent("""\
    [seats]
    CLAUDE = "mac"
    CODEX = "mac"
    CURSOR = "mac"
    MA = "server"
    BF-FIXER = "none"
    """)
SEATS = textwrap.dedent("""\
    [seat.CLAUDE]
    bot = "Claude"
    wake = "claude"
    model = "sonnet"
    {claude_extra}

    [seat.CODEX]
    bot = "Codex"
    wake = "inbox"
    """)


def routed(seat: str = "CODEX", reason: str = "Jay wants a look at the CT build failure.", **extra):
    return reply_output("Paging Codex for you.", route={"seat": seat, "reason": reason}, **extra)


class RouteHarness(DaemonHarness):
    def setUp(self) -> None:
        super().setUp()
        partition = self.tmp / "partition-route.toml"
        partition.write_text(PARTITION)
        self.partition_env = self.env(AGENT_SYNC_PARTITION=str(partition))
        self.configure()
        self.ma = self.fake.add_user("muse-assist-bot@zulip.test", "Muse Assist", is_bot=True, user_id=30)
        self.bf = self.fake.add_user("bf-fixer-bot@zulip.test", "BF Fixer", is_bot=True, user_id=31)

    def configure(self, claude_extra: str = "", extra_daemon: str = "") -> None:
        self.write_config(seats=SEATS.format(claude_extra=claude_extra), extra_daemon=extra_daemon)

    def start(self):
        return self.started(env=self.partition_env)

    def owner_says(self, daemon, content: str = OWNER_ASK, topic: str = "t") -> int:
        mid = self.fake.add_message("Jay Wedgeworth", "agent-sync", topic, content)
        self.pump_until(daemon, lambda: any(mid in r.get("trigger_ids", []) for r in self.ledger()))
        return mid

    def done(self):
        return [r for r in self.ledger() if r["state"] == "done"][-1]

    def stream_posts(self):
        return [m for m in self.bot_posts() if m["type"] == "stream"]

    def owner_dms(self):
        return [m for m in self.bot_posts() if m["type"] == "private"
                and any(r["id"] == 12 for r in m["display_recipient"])]

    def owner_queue(self):
        return L.read_jsonl(self.seat_paths().owner_queue)


class RouteHonoredTests(RouteHarness):
    def test_an_owner_triggered_route_posts_the_reply_then_the_route_and_notes_the_owner(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        mid = self.owner_says(daemon)
        self.wake_cycle(daemon, 6)
        self.assertIn("Routing: enabled", self.dumps()[0]["stdin"])
        posts = self.stream_posts()
        self.assertEqual(len(posts), 2, [p["content"] for p in posts])
        reply, route = posts
        self.assertTrue(reply["content"].startswith("[CLAUDE·wake] re=%d\n" % mid))
        self.assertTrue(route["content"].startswith("[CLAUDE·route→CODEX] re=%d\n" % mid), route["content"])
        self.assertEqual((route["display_recipient"], route["subject"]), ("agent-sync", "t"))
        self.assertIn("@**Codex|11** CLAUDE paged you on Jay's behalf:", route["content"])
        self.assertEqual(W.loud_mentions(route["content"]), 1, "only the daemon's mention is live")
        self.assertIn("get Codex to look at the CT build failure", route["content"], "the owner's ask is quoted")
        self.assertIn("@_**Claude**", route["content"], "the mention in the quoted ask is silent")
        done = self.done()
        self.assertEqual(done["route"]["status"], "posted")
        self.assertEqual((done["route"]["seat"], done["route"]["mode"], done["route"]["posted_id"]),
                         ("CODEX", "inbox only", route["id"]))
        dms = self.owner_dms()
        self.assertEqual(len(dms), 1)
        self.assertTrue(dms[0]["content"].startswith("[CLAUDE·note] re=%d\n" % mid))
        self.assertIn("Routed: CODEX (inbox only)", dms[0]["content"])
        self.assertIn("Jay wants a look at the CT build failure.", dms[0]["content"])
        self.assertTrue(any(q["kind"] == "route" and "CODEX" in (q["note"] or "") for q in self.owner_queue()))
        self.assertEqual(daemon.status()["seats"]["CLAUDE"]["routes_24h"], 1)
        self.assertTrue(daemon.status()["seats"]["CLAUDE"]["route_enabled"])

    def test_a_server_seat_is_routed_as_held_by_the_server_listener(self) -> None:
        self.set_claude(mode="ok", output=routed("MA", "Jay wants Muse Assist on the copy."))
        daemon = self.start()
        self.owner_says(daemon, "@**Claude** hand this copy to Muse Assist")
        self.wake_cycle(daemon, 6)
        route = self.done()["route"]
        self.assertEqual((route["status"], route["mode"]), ("posted", "server listener"))
        self.assertIn("@**Muse Assist|30**", self.stream_posts()[-1]["content"])

    def test_mentions_in_the_reason_are_neutralized(self) -> None:
        self.set_claude(mode="ok", output=routed(reason="loop in @**Cursor** and @*everyone* and @**all**"))
        daemon = self.start()
        self.owner_says(daemon)
        self.wake_cycle(daemon, 6)
        post = self.stream_posts()[-1]["content"]
        self.assertTrue(post.startswith("[CLAUDE·route→CODEX]"))
        self.assertEqual(W.loud_mentions(post), 1)
        self.assertIn("@_**Cursor**", post)
        self.assertNotIn("@*everyone*", post)

    def test_route_posts_count_on_the_loop_guard_and_never_route_again(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        self.owner_says(daemon)
        self.wake_cycle(daemon, 6)
        route_id = self.stream_posts()[-1]["id"]
        self.pump_until(daemon, lambda: daemon.global_ring.__contains__(route_id))
        guard = L.read_json(daemon.loopguard.path)
        self.assertEqual(guard.get(TOPIC_KEY), 2, "the wake reply and the route post both count")

    def test_a_failing_note_or_check_never_loses_the_done_row(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        real = daemon.notifier.notify

        def notify(*args, **kw):
            if kw.get("kind") == "route":
                raise RuntimeError("notifier down")
            return real(*args, **kw)

        daemon.notifier.notify = notify
        self.owner_says(daemon)
        self.wake_cycle(daemon, 6)
        route = self.done()["route"]
        self.assertEqual(route["status"], "posted", "the post went out, so the route counts")
        self.assertIn("notifier down", route["owner_dm_error"])
        daemon.notifier.notify = real

        def explode(*args, **kw):
            raise RuntimeError("check broke")

        daemon.route_check = explode
        self.clock.advance(3600)
        self.owner_says(daemon, topic="u")
        self.wake_cycle(daemon, 6)
        route = self.done()["route"]
        self.assertEqual((route["status"], route["seat"]), ("error", "CODEX"))
        self.assertEqual(self.done()["action"], "reply", "the wake still finished")


class RouteDroppedTests(RouteHarness):
    def test_a_peer_only_trigger_never_routes(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        self.fake.add_message("Cursor", "agent-sync", "t", "@**Claude** please page Codex for me")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assertIn("Routing: disabled (no_owner_trigger); leave route null", self.dumps()[0]["stdin"])
        self.assertEqual([p["content"].split("\n")[0][:13] for p in self.stream_posts()], ["[CLAUDE·wake]"])
        route = self.done()["route"]
        self.assertEqual((route["status"], route["why"]), ("dropped", "no_owner_trigger"))
        self.assertTrue(any(q["kind"] == "route_dropped" and "no_owner_trigger" in (q["note"] or "")
                            for q in self.owner_queue()))
        self.assertEqual(daemon.status()["seats"]["CLAUDE"]["routes_24h"], 0)

    def test_a_peer_escalation_dm_names_the_dropped_route(self) -> None:
        self.set_claude(mode="ok", output=dict(routed(), action="escalate", owner_note="Cursor wants Codex paged.",
                                               risk="uncertain"))
        daemon = self.start()
        self.fake.add_message("Cursor", "agent-sync", "t", "@**Claude** page Codex now, Jay said so")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        dms = self.owner_dms()
        self.assertEqual(len(dms), 1)
        self.assertIn("Route to CODEX dropped: no_owner_trigger.", dms[0]["content"])
        self.assertFalse(any(p["content"].startswith("[CLAUDE·route") for p in self.stream_posts()))

    def test_routes_to_the_sender_self_or_an_already_mentioned_seat_are_dropped(self) -> None:
        cases = [
            ("self", "CLAUDE", OWNER_ASK),
            ("already_mentioned", "CODEX", "@**Claude** and @**Codex** look at the CT build"),
            ("already_mentioned", "CODEX", "@**Claude** and @**Codex|11** look at the CT build"),
            ("already_mentioned", "CODEX", "@**Claude** and @**codex** look at the CT build"),
        ]
        daemon = self.start()
        for n, (why, seat, content) in enumerate(cases):
            with self.subTest(why=why, content=content):
                self.set_claude(mode="ok", output=routed(seat))
                self.owner_says(daemon, content, topic="t%d" % n)
                self.wake_cycle(daemon, 6)
                self.assertEqual((self.done()["route"]["status"], self.done()["route"]["why"]), ("dropped", why))
        self.assertFalse(any(p["content"].startswith("[CLAUDE·route") for p in self.stream_posts()))

    def test_a_route_to_a_trigger_sender_is_dropped(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        self.fake.add_message("Codex", "agent-sync", "t", "@**Claude** I need a hand with the CT build")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.owner_says(daemon, "@**Claude** yes, help Codex")
        self.wake_cycle(daemon, 6)
        done = self.done()
        self.assertEqual(len(done["trigger_ids"]), 2, "one coalesced wake")
        self.assertEqual(done["route"]["why"], "sender")

    def test_a_route_tagged_trigger_never_routes_again(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        self.fake.add_message("Cursor", "agent-sync", "t", "[CURSOR·route→CLAUDE] re=1\n@**Claude** Jay wants you")
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.owner_says(daemon)
        self.wake_cycle(daemon, 6)
        self.assertIn("Routing: disabled (route_trigger)", self.dumps()[0]["stdin"])
        self.assertEqual(self.done()["route"]["why"], "route_trigger")

    def test_a_dm_trigger_refuses_routing(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        self.fake.add_direct_message("Jay Wedgeworth", "get Codex on the CT build", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon, 6)
        self.assertIn("Routing: disabled (dm_trigger)", self.dumps()[0]["stdin"])
        self.assertEqual(self.done()["route"]["why"], "dm_trigger")
        self.assertEqual(self.stream_posts(), [], "nothing reaches a channel from a DM trigger")

    def test_the_seat_kill_switch(self) -> None:
        self.configure(claude_extra="route_enabled = false")
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        self.owner_says(daemon)
        self.wake_cycle(daemon, 6)
        self.assertIn("Routing: disabled (disabled)", self.dumps()[0]["stdin"])
        self.assertEqual(self.done()["route"]["why"], "disabled")
        self.assertFalse(daemon.status()["seats"]["CLAUDE"]["route_enabled"])

    def test_the_daemon_kill_switch_and_a_bad_value(self) -> None:
        self.configure(extra_daemon="route_enabled = false")
        daemon = self.start()
        self.assertEqual(daemon.route_gate(daemon.seats["CLAUDE"], self.pending()), "disabled")
        self.configure(claude_extra='route_enabled = "yes"')
        daemon.reload()
        self.assertTrue(any("route_enabled must be true or false" in e for e in daemon.config.errors))

    def test_the_topic_spacing_drops_a_second_route(self) -> None:
        self.set_claude(mode="ok", output=routed())
        daemon = self.start()
        self.owner_says(daemon)
        self.wake_cycle(daemon, 6)
        self.assertEqual(self.done()["route"]["status"], "posted")
        self.clock.advance(60)
        self.set_claude(mode="ok", output=routed("MA", "and Muse Assist too"))
        self.owner_says(daemon, "@**Claude** also hand it to Muse Assist")
        self.wake_cycle(daemon, 6)
        self.assertEqual(self.done()["route"]["why"], "route_topic_minutes")

    # ---- the validator, driven directly ------------------------------------------------------
    def pending(self, *, owner: bool = True, dm: bool = False, triggers=((900, 12),)) -> W.Pending:
        item = {"channel": "DM" if dm else "agent-sync", "topic": "10,12" if dm else "t",
                "type": "private" if dm else "stream", "recipients": [10, 12] if dm else []}
        pending = W.Pending("w1", "CLAUDE", item, owner=owner, now=self.clock.time(), due=self.clock.time())
        for message_id, sender in triggers:
            pending.add({"id": message_id}, owner and sender == 12)
        return pending

    def check(self, daemon, seat, *, obj=None, triggers=None, lease=None, pending=None):
        pending = pending or self.pending()
        triggers = triggers if triggers is not None else {900: {"id": 900, "sender_id": 12, "content": OWNER_ASK}}
        return daemon.route_check(daemon.seats["CLAUDE"], pending, obj or routed(seat), seat, triggers, lease)[0]

    def test_the_validator_matrix(self) -> None:
        daemon = self.start()
        self.assertIsNone(self.check(daemon, "CODEX"))
        self.assertIsNone(self.check(daemon, "MA"))
        self.assertEqual(self.check(daemon, "NOPE"), "unknown_seat")
        self.assertEqual(self.check(daemon, "CLAUDE"), "self")
        self.assertEqual(self.check(daemon, "CURSOR"), "not_held", "a Mac partition seat with no queue here")
        self.assertEqual(self.check(daemon, "BF-FIXER"), "no_listener", "a `none` seat has no listener")
        self.assertEqual(self.check(daemon, "FX"), "bot_inactive", "no FX bot in this realm")
        self.assertEqual(self.check(daemon, "CODEX", lease="lease-1"), "live_session")
        self.assertEqual(self.check(daemon, "CODEX", obj=routed(risk="high")), "risk_screened")
        self.assertEqual(self.check(daemon, "CODEX", triggers={}), "trigger_unreadable")
        self.assertEqual(self.check(daemon, "CODEX", pending=self.pending(owner=False)), "no_owner_trigger")
        self.assertEqual(self.check(daemon, "CODEX", pending=self.pending(dm=True)), "dm_trigger")
        self.assertEqual(self.check(daemon, "CODEX", triggers={900: {"id": 900, "sender_id": 12,
                                                                     "content": "`@**Codex**` in code is fine"}}),
                         None, "a mention inside code pages no one")
        self.ma["is_active"] = False
        daemon.handle_events(daemon.seats["CLAUDE"], [{"type": "realm_user", "op": "update",
                                                        "person": {"user_id": 30, "is_active": False}}])
        self.assertEqual(self.check(daemon, "MA"), "bot_inactive")

    def test_a_live_lease_marks_the_target_as_a_live_session(self) -> None:
        daemon = self.start()
        self.write_lease("codex-live", seat="CODEX", platform="codex", topics=[("agent-sync", "x")])
        self.assertEqual(daemon.route_mode("CODEX"), ("live session", None))
        self.assertEqual(daemon.route_mode("MA"), ("server listener", None))


class RouteUnitTests(RouteHarness):
    def test_validate_drops_only_a_malformed_route(self) -> None:
        bad = [{"seat": "CODEX"}, {"seat": "codex", "reason": "x"}, {"seat": "CODEX", "reason": ""},
               {"seat": "CODEX", "reason": "r" * 201}, {"seat": "CODEX", "reason": "x", "extra": 1}, "CODEX", ["CODEX"]]
        for route in bad:
            with self.subTest(route=str(route)[:40]):
                result, why = W.validate(reply_output(route=route))
                self.assertIsNone(why)
                self.assertEqual(result["action"], "reply", "the reply still acts")
                self.assertIsNone(result["route"])
                self.assertTrue(result["route_invalid"])
        ok, why = W.validate(routed())
        self.assertEqual((ok, why), (routed(), None))
        old, why = W.validate(reply_output())
        self.assertEqual((old, why), (reply_output(), None), "a result without route still validates unchanged")
        self.assertEqual(W.validate(reply_output(route=None)), (reply_output(route=None), None))

    def test_the_schema_seat_enum_is_the_live_tag_set_sorted(self) -> None:
        schema = json.loads(W.schema_text(["codex", "CLAUDE", "GB-FIXER"]))
        self.assertEqual(schema["properties"]["route"]["properties"]["seat"], {"enum": ["CLAUDE", "CODEX", "GB-FIXER"]})
        self.assertIn("route", schema["required"])
        self.assertEqual(schema["properties"]["route"]["type"], ["object", "null"])
        plain = json.loads(W.schema_text())
        self.assertEqual(plain["properties"]["route"]["properties"]["seat"], {"type": "string"})

    def test_the_route_budget_caps(self) -> None:
        now = self.clock.time()
        budget = dict(BUDGET_DEFAULTS)

        def view(n: int, age: float, key: str = "k%d") -> tuple[str, dict]:
            return "w%d" % n, {"topic_key": key % n if "%" in key else key, "route": {"status": "posted"},
                               "times": {"done": now - age}}

        self.assertIsNone(W.route_budget_block({}, budget, key="t", now=now))
        hour = dict(view(n, 60 * n) for n in range(3))
        self.assertEqual(W.route_budget_block(hour, budget, key="t", now=now), "routes_per_hour")
        day = dict(view(n, 3600 * (n + 1) + 60) for n in range(10))
        self.assertEqual(W.route_budget_block(day, budget, key="t", now=now), "routes_per_day")
        older = dict(view(n, 2 * W.DAY) for n in range(20))
        self.assertIsNone(W.route_budget_block(older, budget, key="t", now=now), "a day-old route no longer counts")
        topic = dict([view(1, 29 * 60, key="t")])
        self.assertEqual(W.route_budget_block(topic, budget, key="t", now=now), "route_topic_minutes")
        self.assertIsNone(W.route_budget_block(topic, budget, key="other", now=now))
        spaced = dict([view(1, 31 * 60, key="t")])
        self.assertIsNone(W.route_budget_block(spaced, budget, key="t", now=now))
        dropped = {"w1": {"topic_key": "t", "route": {"status": "dropped"}, "times": {"done": now}}}
        self.assertIsNone(W.route_budget_block(dropped, budget, key="t", now=now), "a dropped route costs nothing")
        self.assertEqual(W.routes_in(hour.values(), now), 3)

    def test_a_route_post_counts_like_a_wake_reply_but_still_wakes_its_target(self) -> None:
        codex = R.SeatIdentity("CODEX", 11, "Codex", "codex-bot@zulip.test")
        ctx = R.Context(owner_user_id=12, owner_clients=["website"], eligible_user_ids=[10], is_bot={10: True},
                        now=self.clock.time(), stale_after=7200, presence=[])
        message = {"id": 5, "type": "stream", "display_recipient": "agent-sync", "subject": "t", "sender_id": 10,
                   "content": "[CLAUDE·route→CODEX] re=4\n@**Codex|11** CLAUDE paged you", "flags": ["mentioned"],
                   "timestamp": self.clock.time(), "client": "ZulipPython"}
        c = R.classify(message, codex, ctx)
        self.assertTrue(c.route_tag)
        self.assertFalse(c.wake_tag)
        self.assertIn("route_tag", c.labels())
        self.assertEqual(R.prefilter(c), ("peer", None), "the paged seat wakes")
        wake_reply = dict(message, content="[CLAUDE·wake] re=4\n@_**Codex** hi")
        self.assertEqual(R.prefilter(R.classify(wake_reply, codex, ctx)), (None, "wake_tag"))

    def test_compose_route_keeps_one_line_fields_and_a_closed_quote(self) -> None:
        text = W.compose_route("CLAUDE", "CODEX", "Co*dex|", 11, 77, "a\nb ```x``` @**Jay**",
                               "```\n@**all** break out\n```", "https://z/near/77")
        lines = text.split("\n")
        self.assertEqual(lines[0], "[CLAUDE·route→CODEX] re=77")
        self.assertTrue(lines[1].startswith("@**Codex|11** CLAUDE paged you on Jay's behalf:  a b '''x''' @_**Jay**"))
        self.assertEqual((lines[2], lines[4]), ("```quote", "```"))
        self.assertNotIn("`", lines[3])
        self.assertEqual(W.loud_mentions(text), 1)
        self.assertEqual(len(lines), 6)
