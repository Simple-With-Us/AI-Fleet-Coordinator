"""One `#alerts` post for every seat's held-back wakes on the server (AFC-257).

The server listener held a wake back, wrote it to `/data/<SEAT>/owner-queue.jsonl`, and nothing read
that queue, so the hold was durable and invisible, and an eviction was written to the same unread
place.  These tests drive the real daemon over the fake Zulip and the fake clock:  one post for the
whole instance per window, none at all when nothing is held, and every line naming the seat, the
reason and the count.
"""
from __future__ import annotations

import re
import unittest

from agent_sync import config as C
from agent_sync import heldalert as HELDALERT
from agent_sync import heldback as HELD
from agent_sync import live as L
from agent_sync import router as R
from agent_sync import zulip as Z
from agent_sync.tests.test_heldback import HeldBackRoutineTests, HeldMixin
from agent_sync.tests.test_router import ctx as router_ctx
from agent_sync.tests.test_router import lease as router_lease
from agent_sync.tests.test_router import msg as router_msg
from agent_sync.tests.test_server import CLOUD_SEATS, GB_EMAIL, SEAT, CloudSeatHarness
from agent_sync.tests.test_server import SERVER_DIR
SAMPLE_CONFIG = SERVER_DIR / "listener.toml"


class HeldAlertTests(HeldBackRoutineTests):
    """The held-back harness (fake clock, fake Zulip, a server config) plus the `#alerts` post."""

    def alerts(self):
        """Every message posted to `#alerts`, whichever bot sent it."""
        return [m for m in self.fake.messages
                if m.get("type") == "stream" and m.get("display_recipient") == HELDALERT.CHANNEL]

    def alert_texts(self):
        return [m["content"] for m in self.alerts()]

    def alert_daemon(self, **kw):
        """A server daemon with the digest on and no catch-up, so only the digest acts."""
        self.write_server_config(budget="wakes_per_hour = 0", seat_extra="held_back = { catch_up = false }", **kw)
        return self.started()

    def hold_for(self, seat: str, message_id: int, reason: str = "wakes_per_day", **kw) -> None:
        """One held-back record for `seat`, as the budget refusals write it."""
        HELD.hold(L.SeatPaths(self.root, seat), [{"message_id": message_id, "reason": reason,
                                                  "at": self.clock.time(), "channel": "work-in-progress", "topic": "t",
                                                  "stream_id": 642232, "sender_full_name": "Codex"}],
                  cap=kw.pop("cap", 200), overflow_cap=kw.pop("overflow_cap", 1000), now=self.clock.time())

    def hold_one(self, daemon, topic: str = "batch") -> None:
        """One mention the budget refuses, so exactly one record is held back."""
        self.mention(daemon, topic)
        self.wake_cycle(daemon)
        self.mention(daemon, topic)

    # ---- one post for the whole instance ---------------------------------------------------------
    def test_one_post_names_the_seat_the_reason_and_the_count(self) -> None:
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        texts = self.alert_texts()
        self.assertEqual(len(texts), 1, "a hold on the server must be visible somewhere")
        self.assertEqual([(m["display_recipient"], m["subject"]) for m in self.alerts()],
                         [(HELDALERT.CHANNEL, HELDALERT.TOPIC)])
        self.assertIn("**%s**: 1 held (wakes_per_hour 1)" % SEAT, texts[0])

    def test_a_seat_with_nothing_held_is_not_named(self) -> None:
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        for name in CLOUD_SEATS:
            if name != SEAT:
                self.assertNotIn("**%s**" % name, self.alert_texts()[0])

    def edits(self, message_id: int) -> int:
        """How many times that message's content has been rewritten."""
        return max(0, len(self.fake.histories.get(message_id, [])) - 1)

    # ---- the window ------------------------------------------------------------------------------
    def test_at_most_one_write_per_window_and_the_next_one_edits(self) -> None:
        """One standing message (AFC-569):  the window that follows edits it rather than posting."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        self.assertEqual(len(self.alert_texts()), 1)
        first = self.alerts()[0]["id"]
        self.step(daemon, 600)
        self.assertEqual(self.edits(first), 0, "still inside the hour")
        self.step(daemon, 4000)
        self.assertEqual(len(self.alert_texts()), 1, "the next window edits, it never posts again")
        self.assertEqual(self.edits(first), 1, "and the edit lands on the same message")
        self.assertEqual(HELDALERT.standing(self.root), {"seat": SEAT, "message_id": first, "clear": False})

    def test_nothing_held_means_no_post_at_all(self) -> None:
        """An all-clear a minute after the last alert teaches the eye to skip the channel."""
        daemon = self.alert_daemon()
        self.step(daemon, 31)
        self.step(daemon, 4000)
        self.assertEqual(self.alert_texts(), [], "an idle instance stays silent")

    def test_the_window_off_posts_nothing(self) -> None:
        daemon = self.alert_daemon(daemon_extra="held_alert_minutes = 0")
        self.hold_one(daemon)
        self.step(daemon, 31)
        self.assertEqual(self.alert_texts(), [], "0 turns the digest off")

    def test_the_window_and_the_standing_message_survive_a_restart(self) -> None:
        """`last_post` and the standing pair are the whole state, so a restart inside the window does
        not re-post, and a restart after it edits the same message rather than starting a new one."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        self.assertEqual(len(self.alert_texts()), 1)
        first = self.alerts()[0]["id"]
        daemon.stop.set()  # a restart:  the same root and state, a fresh daemon object
        again = self.started()
        self.step(again, 31)
        self.assertEqual(self.edits(first), 0, "the window is not reset by a restart")
        self.step(again, 4000)
        self.assertEqual(len(self.alert_texts()), 1)
        self.assertEqual(self.edits(first), 1)

    # ---- the standing message ----------------------------------------------------------------------
    def test_an_all_clear_is_an_edit_of_the_standing_message(self) -> None:
        """When every hold clears the list is rewritten to say so, once, and never as a fresh post."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        first = self.alerts()[0]["id"]
        HELD.surface(self.seat_paths(SEAT), message_ids=[r["message_id"] for r in self.held()], lost=0)
        self.step(daemon, 4000)
        self.assertEqual(len(self.alerts()), 1, "an all-clear is never a new post")
        self.assertIn("Nothing is held back now", self.alert_texts()[0])
        self.assertNotIn("**%s**" % SEAT, self.alert_texts()[0], "the stale list is gone")
        self.assertTrue(HELDALERT.standing(self.root)["clear"])
        self.step(daemon, 4000)
        self.step(daemon, 4000)
        self.assertEqual(self.edits(first), 1, "a cleared message is left alone while nothing is held")

    def test_a_new_hold_after_the_clear_edits_the_same_message(self) -> None:
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        first = self.alerts()[0]["id"]
        HELD.surface(self.seat_paths(SEAT), message_ids=[r["message_id"] for r in self.held()], lost=0)
        self.step(daemon, 4000)
        self.hold_for(SEAT, 990)
        self.step(daemon, 4000)
        self.assertEqual([m["id"] for m in self.alerts()], [first])
        self.assertIn("**%s**: 1 held" % SEAT, self.alert_texts()[0])
        self.assertFalse(HELDALERT.standing(self.root)["clear"])

    def test_a_refused_edit_posts_a_fresh_standing_message(self) -> None:
        """The message is gone (or this bot may not edit it):  Zulip refuses, nothing can bring it
        back, so the same attempt posts a fresh one and that becomes the standing message."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        first = self.alerts()[0]
        with self.fake.cond:
            self.fake.messages.remove(first)
        self.step(daemon, 4000)
        self.assertEqual(len(self.alerts()), 1)
        fresh = self.alerts()[0]["id"]
        self.assertNotEqual(fresh, first["id"])
        self.assertEqual(HELDALERT.standing(self.root)["message_id"], fresh)

    def test_an_ambiguous_edit_failure_never_posts_a_second_copy(self) -> None:
        """A timeout may have landed, so it is retried as an edit next window, not turned into a post."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        first = self.alerts()[0]["id"]
        self.fail_post_for(daemon, SEAT, error=Z.NetworkError("timed out", timeout=True, maybe_sent=True))
        self.step(daemon, 4000)
        self.assertEqual(self.count_attempts(daemon), 1)
        self.assertEqual([m["id"] for m in self.alerts()], [first], "no second copy of the list")
        self.assertEqual(HELDALERT.standing(self.root)["message_id"], first)
        for _ in range(60):
            self.step(daemon, 30)
        self.assertEqual(self.count_attempts(daemon), 1, "a failed edit spends the window too")
        self.fail_post_for(daemon, SEAT, fail=False)
        self.step(daemon, 2400)
        self.assertEqual(self.edits(first), 1, "the next window edits the same message")

    def test_a_seat_that_cannot_edit_hands_the_digest_to_a_fresh_post(self) -> None:
        """Only the bot that posted a message may edit it.  A standing message owned by a seat that is
        not running here cannot be edited, so the digest is posted fresh by a seat that can."""
        daemon = self.alert_daemon()
        HELDALERT.note_standing(self.root, "NOT-HERE", 123456, clear=False)
        self.hold_one(daemon)
        self.step(daemon, 31)
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(HELDALERT.standing(self.root),
                         {"seat": SEAT, "message_id": self.alerts()[0]["id"], "clear": False})

    def test_an_all_clear_waits_for_the_bot_that_owns_the_message(self) -> None:
        """With nothing held, a standing list another seat owns is left for that seat:  a fresh
        all-clear post would be the noise the no-post rule forbids."""
        daemon = self.alert_daemon()
        HELDALERT.note_standing(self.root, "NOT-HERE", 123456, clear=False)
        self.step(daemon, 31)
        self.step(daemon, 4000)
        self.assertEqual(self.alerts(), [])
        self.assertEqual(HELDALERT.standing(self.root)["message_id"], 123456, "kept for its owner")

    # ---- what the post says ----------------------------------------------------------------------
    def test_every_hold_reason_is_named_with_its_count(self) -> None:
        daemon = self.alert_daemon()
        records = [{"message_id": 700 + n, "reason": reason, "at": self.clock.time(), "channel": "work-in-progress",
                    "topic": "t", "stream_id": 642232, "sender_full_name": "Codex"}
                   for n, reason in enumerate(["wakes_per_day", "wakes_per_day", "usd_per_day", "overflow"])]
        HELD.hold(self.seat_paths(SEAT), records, cap=200, now=self.clock.time())
        self.step(daemon, 31)
        text = self.alert_texts()[0]
        self.assertIn("wakes_per_day 2", text)
        self.assertIn("usd_per_day 1", text)
        self.assertIn("overflow 1", text)

    def test_an_evicted_id_is_called_out_apart_from_the_holds(self) -> None:
        """Ids lost past both caps left the file and nothing can surface them, so they are named, and
        they still stand after every record is gone."""
        daemon = self.alert_daemon()
        for n in (800, 801, 802):  # the third overflows the overflow and is evicted
            self.hold_for(SEAT, n, reason="overflow", cap=1, overflow_cap=1)
        self.assertEqual(HELD.summary(self.seat_paths(SEAT), self.clock.time())["evicted"], 1)
        self.step(daemon, 31)
        self.assertIn("**1 lost past the caps**", self.alert_texts()[0])
        # And the eviction alone, with nothing held any more, is still worth a post.
        HELD.surface(self.seat_paths(SEAT), message_ids=[r["message_id"] for r in self.held()], lost=0)
        self.assertEqual(len(self.held()), 0)
        self.step(daemon, 4000)
        self.assertEqual(len(self.alerts()), 1, "edited in place")
        self.assertIn("**1 lost past the caps**", self.alert_texts()[0],
                      "a lost id is still owed an answer after the holds are gone")
        self.assertNotIn("Nothing is held back now", self.alert_texts()[0])

    def test_the_post_is_actionable_and_does_not_blame(self) -> None:
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        text = self.alert_texts()[0]
        self.assertIn("Nothing was dropped", text)
        self.assertIn("listener.toml", text, "says which budget to raise")
        self.assertIn("in that seat's inbox", text, "says where the message actually is")

    def test_two_spaces_between_sentences_in_the_post(self) -> None:
        """Fleet prose carries two:  the Kody autofix bot collapses a single one."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        for line in self.alert_texts()[0].splitlines():
            for match in re.finditer(r"[.!?:] [A-Za-z`]", line):
                self.fail("a single space between sentences in %r" % line)

    # ---- it must not become a second reason wakes are held back -----------------------------------
    def test_the_post_never_wakes_a_seat(self) -> None:
        """`#alerts` is already a presence topic, so router.route keeps this PASSIVE for every seat.
        An alert that woke its own subjects would hold back more wakes, in the very channel meant
        to report that."""
        me = R.SeatIdentity(SEAT, self.gb["user_id"], "GB-Compiler", GB_EMAIL)
        post = router_msg(sender=self.gb["user_id"], channel=HELDALERT.CHANNEL, topic=HELDALERT.TOPIC,
                          content="**%s**: 1 held (wakes_per_hour 1)" % SEAT, flags=["mentioned"])
        leases = [router_lease(topics=[(HELDALERT.CHANNEL, HELDALERT.TOPIC)])]
        for presence in ([(HELDALERT.CHANNEL, "*")], []):
            with self.subTest(presence=presence):
                _, decision = R.route(post, me, router_ctx(presence=presence), leases)
                self.assertNotIn(R.INTERRUPT, [item[1] for item in decision.lease_items],
                                 "a `#alerts` post must never interrupt the seat it names")

    def test_a_mention_in_the_alerts_topic_is_not_wake_worthy(self) -> None:
        """The composer writes bold names, never `@**Seat**`;  this pins that an actual mention in
        that topic would not become a wake either, so a future edit cannot smuggle one in."""
        me = R.SeatIdentity(SEAT, self.gb["user_id"], "GB-Compiler", GB_EMAIL)
        post = router_msg(sender=self.gb["user_id"], channel=HELDALERT.CHANNEL, topic=HELDALERT.TOPIC,
                          content="@**GB-Compiler** held back", flags=["mentioned"])
        _, decision = R.route(post, me, router_ctx(presence=[(HELDALERT.CHANNEL, "*")]), [])
        self.assertFalse(decision.wake_worthy)

    def test_the_post_carries_no_mention_of_a_seat(self) -> None:
        """A `@**Seat**` would be a wake, whatever the topic.  Names are bold, never mentions."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        self.step(daemon, 31)
        self.assertNotIn("@**", self.alert_texts()[0])
        self.assertIn("**%s**" % SEAT, self.alert_texts()[0])

    # ---- it must survive its own failure ---------------------------------------------------------
    def test_a_refused_post_waits_a_window_and_still_retries(self) -> None:
        """A failed post is an attempt, so it consumes the window:  a key that stays broken must not
        cost a post every 30-second tick.  The hold is untouched and the next window retries, so the
        signal survives without becoming the flood this design exists to prevent."""
        self.write_server_config(budget="wakes_per_hour = 0", seat_extra="held_back = { catch_up = false }")
        daemon = self.started()
        self.fail_post_for(daemon, SEAT)  # before the hold, which is what triggers the first attempt
        self.hold_one(daemon)
        self.step(daemon, 31)
        self.assertEqual(self.alert_texts(), [], "the post was refused")
        self.assertEqual(self.count_attempts(daemon), 1)
        self.assertIsNotNone(HELDALERT.load(self.root).get("last_post"), "an attempt consumes the window")
        for _ in range(60):  # half an hour of held ticks
            self.step(daemon, 30)
        self.assertEqual(self.count_attempts(daemon), 1,
                         "a refused post must not retry every tick inside the window")
        self.fail_post_for(daemon, SEAT, fail=False)
        self.step(daemon, 2400)
        self.assertEqual(self.count_attempts(daemon), 2, "the next window retries exactly once")
        self.assertEqual(len(self.alert_texts()), 1, "and the hold is reported then")

    def test_a_long_broken_key_costs_one_attempt_a_window(self) -> None:
        """The bound itself:  an hour of ticks against a refused key is ONE attempt, not 120."""
        self.write_server_config(budget="wakes_per_hour = 0", seat_extra="held_back = { catch_up = false }")
        daemon = self.started()
        self.hold_one(daemon)
        self.fail_post_for(daemon, SEAT)
        self.step(daemon, 31)  # the first attempt, which fails and consumes the window
        for _ in range(120):  # an hour of HELD_TICK_SECONDS
            self.step(daemon, 30)
        self.assertEqual(self.count_attempts(daemon), 1, "one attempt an hour, not one a tick")

    def fail_post_for(self, daemon, seat: str, *, fail: bool = True, error: Exception | None = None) -> None:
        """Make that seat's bot refuse a stream post or a message edit, the way a refused or expired
        key does (or raise `error` instead).  The wrappers hold the ORIGINAL methods and a live flag,
        so neither re-calling this nor layering the counter on top stacks guards or leaves a stale
        refusal behind."""
        client = daemon.seats[seat].client
        real_post = getattr(client, "_alert_orig_post", None) or client.post
        real_patch = getattr(client, "_alert_orig_patch", None) or client.patch
        client._alert_orig_post, client._alert_orig_patch = real_post, real_patch
        counter = getattr(client, "_alert_counter", None)
        if counter is None:
            counter = client._alert_counter = {"n": 0}
        refusing = {"fail": fail}

        def attempt() -> None:
            counter["n"] += 1
            if refusing["fail"]:
                raise error or Z.AgentSyncError("fake refused the post")

        def guarded_post(path, params=None, **kw):
            if path == "messages" and (params or {}).get("type") == "stream":
                attempt()
            return real_post(path, params, **kw)

        def guarded_patch(path, params=None, **kw):
            if re.fullmatch(r"messages/\d+", path):
                attempt()
            return real_patch(path, params, **kw)

        client.post, client.patch = guarded_post, guarded_patch

    def count_attempts(self, daemon) -> int:
        """How many stream posts and message edits that seat's bot has attempted since the first
        refusal in this test."""
        client = daemon.seats[SEAT].client
        return int(getattr(client, "_alert_counter", {}).get("n", 0))

    # ---- the records themselves are untouched ------------------------------------------------------
    def test_the_digest_does_not_let_records_expire(self) -> None:
        """A `#alerts` post the owner has not opened is not the owner being shown a hold, so it must
        not advance `told_seq`:  server records keep AFC-253's no-TTL behaviour."""
        daemon = self.alert_daemon()
        self.hold_one(daemon)
        before = [r["message_id"] for r in self.held()]
        self.step(daemon, 31)
        self.assertTrue(set(before) <= {r["message_id"] for r in self.held()},
                        "a `#alerts` post must not remove a hold:  the catch-up still owes it")
        self.assertFalse(HELD.load(self.seat_paths(SEAT)).get("told_seq"),
                         "no entry may expire unsurfaced from a `#alerts` post")


class HeldAlertMultiSeatTests(HeldMixin, CloudSeatHarness):
    """Coalescing across seats, over the shipped sample config:  the five cloud seats plus the
    persona, all connected on one instance, which is the fleet shape the decision was made about.
    Only the cross-seat tests live here;  the single-seat ones are on `HeldAlertTests`."""

    def alerts(self):
        return [m for m in self.fake.messages
                if m.get("type") == "stream" and m.get("display_recipient") == HELDALERT.CHANNEL]

    def alert_texts(self):
        return [m["content"] for m in self.alerts()]

    def hold_for(self, seat: str, message_id: int, reason: str = "wakes_per_day", **kw) -> None:
        HELD.hold(L.SeatPaths(self.root, seat), [{"message_id": message_id, "reason": reason,
                                                  "at": self.clock.time(), "channel": "work-in-progress", "topic": "t",
                                                  "stream_id": 642232, "sender_full_name": "Codex"}],
                  cap=kw.pop("cap", 200), overflow_cap=kw.pop("overflow_cap", 1000), now=self.clock.time())

    def connected(self, daemon):
        seats = [SEAT] + sorted(CLOUD_SEATS)
        for seat in seats:
            self.assertTrue(daemon.connect_seat(seat), "%s: %s" % (seat, daemon.seats.get(seat)))
        return seats

    def test_many_seats_holding_at_once_are_one_post(self) -> None:
        """The whole reason for coalescing at the listener and not per seat:  the post count is
        bounded by the window, never by the number of seats holding.  The seats must be RUNNING on
        this instance, since a digest reports what this instance holds, not the whole fleet."""
        daemon = self.server_daemon(env=self.cloud_env())
        running = self.connected(daemon)
        for n, seat in enumerate(running):
            self.hold_for(seat, 9000 + n)
        self.step(daemon, 31)
        texts = self.alert_texts()
        self.assertEqual(len(texts), 1, "%d seats holding at once must not be %d posts"
                         % (len(running), len(running)))
        for seat in running:
            self.assertIn("**%s**:" % seat, texts[0], "every holding seat is named")
        self.assertIn("%d seats have" % len(running), texts[0])

    def test_one_holding_seat_alone_names_only_itself(self) -> None:
        daemon = self.server_daemon(env=self.cloud_env())
        self.connected(daemon)
        self.hold_for("JET", 9101)
        self.step(daemon, 31)
        text = self.alert_texts()[0]
        self.assertIn("**JET**:", text)
        self.assertIn("1 seat has", text)
        for seat in CLOUD_SEATS:
            if seat != "JET":
                self.assertNotIn("**%s**" % seat, text)

    def test_the_posting_seat_can_be_named(self) -> None:
        """`held_alert_seat` picks whose bot carries the digest, so a busy seat's bot is not made
        the voice of the whole instance.  The sample ships `MA`;  this overrides it, which also
        proves the key is read."""
        text = re.sub(r'(?m)^held_alert_seat = "MA".*$', 'held_alert_seat = "JET"', self.config_file.read_text())
        self.assertIn('held_alert_seat = "JET"', text)
        self.config_file.write_text(text)
        daemon = self.server_daemon(env=self.cloud_env())
        self.assertEqual(daemon.config.held_alert_seat, "JET")
        self.connected(daemon)
        self.hold_for("JET", 9102)
        self.hold_for("MA", 9103)
        self.step(daemon, 31)
        self.assertEqual([m["sender_email"] for m in self.alerts()], [CLOUD_SEATS["JET"][0]],
                         "the named seat's bot posts, naming every seat either way")

    def test_the_bot_that_posted_is_the_one_that_edits(self) -> None:
        """Zulip lets only the sender edit, so the standing pair names MA's bot and MA edits.  When
        MA's key is refused, the next seat that can post takes over with a fresh message, and then it
        is the one that edits."""
        daemon = self.server_daemon(env=self.cloud_env())
        self.connected(daemon)
        self.hold_for("JET", 9201)
        self.step(daemon, 31)
        first = self.alerts()[0]
        self.assertEqual(first["sender_email"], CLOUD_SEATS["MA"][0])
        self.assertEqual(HELDALERT.standing(self.root)["seat"], "MA")
        self.step(daemon, 4000)
        self.assertEqual(len(self.alerts()), 1, "MA edited its own message")
        daemon.seats["MA"].fatal = True
        self.step(daemon, 4000)
        self.assertEqual(len(self.alerts()), 2, "MA cannot edit any more, so another seat posts fresh")
        taken = HELDALERT.standing(self.root)
        self.assertNotEqual(taken["seat"], "MA")
        self.assertEqual(taken["message_id"], self.alerts()[1]["id"])
        self.step(daemon, 4000)
        self.assertEqual(len(self.alerts()), 2, "and that seat edits from then on")

    def test_the_sample_carries_the_digest_on(self) -> None:
        """The shipped sample is what the container starts from, so the decision has to be in it."""
        import tomllib
        cfg = tomllib.loads(SAMPLE_CONFIG.read_text())
        self.assertEqual(cfg["daemon"]["held_alert_minutes"], 60)
        self.assertEqual(cfg["daemon"]["held_alert_seat"], "MA")
        self.assertEqual(HELDALERT.CHANNEL, "alerts", "the channel the owner chose, created by nothing here")
        self.assertIn(["alerts", "*"], cfg["daemon"]["presence_topics"],
                      "the post lands in a presence topic, so it never wakes a seat")


class HeldAlertConfigTests(unittest.TestCase):
    def parse(self, daemon_extra: str) -> C.Config:
        import tomllib
        return C.from_dict(tomllib.loads("[daemon]\nowner_user_id = 12\nnotify_banners = false\n%s\n"
                                        % daemon_extra))

    def test_the_window_defaults_to_an_hour(self) -> None:
        self.assertEqual(self.parse("").held_alert_minutes, 60.0)

    def test_the_window_is_read_and_bounded(self) -> None:
        self.assertEqual(self.parse("held_alert_minutes = 15").held_alert_minutes, 15.0)
        for bad in ("held_alert_minutes = 5000", "held_alert_minutes = -1", "held_alert_minutes = \"yes\""):
            with self.subTest(bad=bad):
                self.assertFalse(self.parse(bad).ok, bad)

    def test_the_posting_seat_is_optional_and_validated(self) -> None:
        self.assertIsNone(self.parse("").held_alert_seat)
        self.assertEqual(self.parse("held_alert_seat = \"jet\"").held_alert_seat, "JET")
        self.assertFalse(self.parse("held_alert_seat = \"not a seat\"").ok)


class HeldAlertUnitTests(unittest.TestCase):
    """The composer and the window, with no daemon:  the rules that decide what a post says."""

    def held(self, **kw):
        base = {"count": 1, "overflow": 0, "evicted": 0, "oldest_age": 60, "reasons": {"wakes_per_hour": 1}}
        base.update(kw)
        return base

    def test_compose_is_empty_without_a_seat(self) -> None:
        self.assertEqual(HELDALERT.compose([]), "")

    def test_compose_names_every_seat_sorted(self) -> None:
        text = HELDALERT.compose([("JET", self.held(count=2, reasons={"wakes_per_hour": 2})),
                                  ("GB-COMPILER", self.held(oldest_age=30))])
        self.assertLess(text.index("GB-COMPILER"), text.index("JET"), "seats are sorted")
        self.assertIn("2 seats have", text)

    def test_compose_agrees_with_itself_about_one_seat(self) -> None:
        self.assertIn("1 seat has a wake the budget would not run", HELDALERT.compose([("JET", self.held())]))
        self.assertIn("2 seats have wakes the budget would not run",
                      HELDALERT.compose([("JET", self.held()), ("MA", self.held())]))

    def test_holding_counts_evictions_alone(self) -> None:
        self.assertTrue(HELDALERT.holding({"count": 0, "evicted": 2}), "lost ids are still owed an answer")
        self.assertFalse(HELDALERT.holding({"count": 0, "evicted": 0}))
        self.assertFalse(HELDALERT.holding({}))

    def test_an_unknown_reason_is_reported_not_dropped(self) -> None:
        """A reason this build does not know is still a hold somebody owes an answer for."""
        self.assertIn("brand_new_cap 1", HELDALERT.seat_line("JET", self.held(reasons={"brand_new_cap": 1})))

    def test_a_seat_with_only_evictions_says_only_that(self) -> None:
        """No "0 held" and no age for a seat that has nothing held:  an empty hold reported as if it
        were a hold is the noise that gets a channel ignored."""
        line = HELDALERT.seat_line("MA", {"count": 0, "overflow": 0, "evicted": 2, "oldest_age": 0,
                                          "reasons": {}})
        self.assertEqual(line, "**MA**: **2 lost past the caps**")

    def test_age_text(self) -> None:
        self.assertEqual([HELDALERT.age_text(n) for n in (5, 65, 3700, 90000)], ["5s", "1m", "1h", "1d"])

    def test_the_window_is_a_rate_limit_and_zero_is_off(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(HELDALERT.due(tmp, every=3600, now=1000.0), "the first hold posts at once")
            HELDALERT.note_post(tmp, 1000.0)
            self.assertFalse(HELDALERT.due(tmp, every=3600, now=2000.0))
            self.assertTrue(HELDALERT.due(tmp, every=3600, now=4600.0))
            self.assertFalse(HELDALERT.due(tmp, every=0, now=99999.0), "0 is off")

    def test_the_standing_pair_round_trips_and_rejects_junk(self) -> None:
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(HELDALERT.standing(tmp))
            HELDALERT.note_post(tmp, 1000.0)
            HELDALERT.note_standing(tmp, "MA", 42, clear=False)
            self.assertEqual(HELDALERT.standing(tmp), {"seat": "MA", "message_id": 42, "clear": False})
            self.assertEqual(HELDALERT.load(tmp)["last_post"], 1000.0, "the window is kept beside it")
            HELDALERT.note_standing(tmp, "MA", 42, clear=True)
            self.assertTrue(HELDALERT.standing(tmp)["clear"])
            HELDALERT.drop_standing(tmp)
            self.assertIsNone(HELDALERT.standing(tmp))
            for junk in ({"seat": "MA", "message_id": True}, {"seat": "", "message_id": 4},
                         {"seat": "MA", "message_id": -1}, {"seat": "MA"}, "MA:42"):
                with self.subTest(junk=junk):
                    with open(HELDALERT.path_for(tmp), "w", encoding="utf-8") as handle:
                        json.dump({"standing": junk}, handle)
                    self.assertIsNone(HELDALERT.standing(tmp), "a damaged record costs one fresh post")

    def test_the_all_clear_says_so_and_names_no_one(self) -> None:
        text = HELDALERT.compose_clear(updated="Thu, Oct 29, 9:50am")
        self.assertIn("Nothing is held back now", text)
        self.assertIn("_Updated Thu, Oct 29, 9:50am.", text)
        self.assertNotIn("@**", text)
        for line in text.splitlines():
            self.assertIsNone(re.search(r"[.!?:] [A-Za-z`]", line), "two spaces between sentences in %r" % line)

    def test_the_digest_says_when_it_was_updated(self) -> None:
        text = HELDALERT.compose([("JET", self.held())], updated="Thu, Oct 29, 9:50am")
        self.assertTrue(text.endswith("_Updated Thu, Oct 29, 9:50am.  This message is edited in place each "
                                      "window while anything is held._"))
        self.assertNotIn("_Updated", HELDALERT.compose([("JET", self.held())]), "no time, no line")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()