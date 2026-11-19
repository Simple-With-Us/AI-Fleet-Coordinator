"""Wake route suggestions (owner, Sat, Oct 10, 2026), what is left of them:  the route budget the
seat's `budget` table still sets (wakes.route_budget_block), and the router's reading of a route page
another bot posts.  The tool-less wakes that suggested a route (claude, muse, openrouter) were retired on
the owner's ruling of Nov 10, 2026 (the listener starts no model), and with them every test of a route
a wake suggested;  the Operator never posted one anyway (Nov 10, 2026:  "The operator should never be
answering on behalf of a platform")."""
from __future__ import annotations

from simple_tools import router as R
from simple_tools import wakes as W
from simple_tools.config import BUDGET_DEFAULTS
from simple_tools.tests.listener_harness import ListenerHarness


class RouteUnitTests(ListenerHarness):
    def test_the_route_budget_caps(self) -> None:
        now = self.clock.time()
        # The route caps have no default (owner, Nov 10, 2026), so a seat writes the ones it wants:  the
        # old defaults, 3 an hour, 10 a day and 30 minutes between routes in one topic.
        budget = dict(BUDGET_DEFAULTS, routes_per_hour=3, routes_per_day=10, route_topic_minutes=30)

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

    def test_a_route_cap_the_budget_does_not_carry_is_no_limit(self) -> None:
        now = self.clock.time()
        posted = {
            "w%d" % n: {"topic_key": "t", "route": {"status": "posted"}, "times": {"done": now - 60 * n}}
            for n in range(40)
        }
        self.assertIsNone(W.route_budget_block(posted, dict(BUDGET_DEFAULTS), key="t", now=now))
        self.assertEqual(
            W.route_budget_block(posted, dict(BUDGET_DEFAULTS, routes_per_hour=0), key="t", now=now),
            "routes_per_hour",
            "0 is reached at once, so a seat can still switch routes off",
        )

    def test_a_route_post_counts_like_a_wake_reply_but_still_wakes_its_target(self) -> None:
        codex = R.SeatIdentity("CODEX", 11, "Codex", "codex-bot@zulip.test")
        ctx = R.Context(owner_user_id=12, owner_clients=["website"], eligible_user_ids=[10], is_bot={10: True},
                        now=self.clock.time(), stale_after=7200, presence=[])
        message = {"id": 5, "type": "stream", "display_recipient": "simple-tools", "subject": "t", "sender_id": 10,
                   "content": "[CLAUDE·route→CODEX] re=4\n@**Codex|11** CLAUDE paged you", "flags": ["mentioned"],
                   "timestamp": self.clock.time(), "client": "ZulipPython"}
        c = R.classify(message, codex, ctx)
        self.assertTrue(c.route_tag)
        self.assertFalse(c.wake_tag)
        self.assertIn("route_tag", c.labels())
        self.assertEqual(R.prefilter(c), ("peer", None), "the paged seat wakes")
        wake_reply = dict(message, content="[CLAUDE·wake] re=4\n@_**Codex** hi")
        self.assertEqual(R.prefilter(R.classify(wake_reply, codex, ctx)), (None, "wake_tag"))
