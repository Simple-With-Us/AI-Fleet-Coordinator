"""Wake bookkeeping and adapters:  the documents that describe the listener, reply text, the secret
scanner, budgets and the usd ceiling, coalescing, the loop guard, ledger recovery and notify-owner
argv.  The headless wake's validator, prompt and Claude runner went with the retired wake types
(owner ruling, Nov 10, 2026:  the listener starts no model)."""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import unittest

from simple_tools import adapters as A
from simple_tools import live as L
from simple_tools import secretscan
from simple_tools import wakes as W
from simple_tools.config import BUDGET_DEFAULTS


class ContractTests(unittest.TestCase):
    """The documents that still describe what the listener does.

    The wake contract (wake/wake-contract.md) went with the retired wake types (owner ruling,
    Nov 10, 2026:  the listener starts no model), and with it the tests that compared it with the
    Zulip fleet guide's peer screen.  The guide is canonical for chat conduct, and the listener
    protocol says the daemon answers nobody."""

    @staticmethod
    def doc(name: str) -> str:
        path = os.path.join(
            os.path.dirname(os.path.abspath(W.__file__)), "..", "..", "docs", "protocols", name
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def zulip_guide(self) -> str:
        """The Zulip fleet guide, which is canonical for chat conduct and which this repository owns."""
        return self.doc("zulip-fleet-guide.md")

    def test_the_chat_guide_names_the_dm_command_the_cli_implements(self) -> None:
        import argparse

        from simple_tools import cli

        # The guide is canonical for chat conduct and this repository owns it, so the guide is
        # the document that has to name the command.  The CLI runs `_check_text_args` for `dm`
        # and refuses an invocation that carries no message text, so the documented invocation
        # carries the text placeholder.  The prefix on its own is documented too, for the
        # fleet-wake emergency case, and it is asserted on its own so it can never stand in for
        # the full invocation and let a guide that documents only a call the CLI rejects pass.
        guide = self.zulip_guide()
        spellings = ['%s dm --owner -- "<text>"' % prog for prog in ("simple-chat", "simple-tools")]
        self.assertTrue(
            any(spelling in guide for spelling in spellings),
            "the chat guide does not name the dm --owner invocation the CLI implements",
        )
        prefixes = ["%s dm --owner" % prog for prog in ("simple-chat", "simple-tools")]
        self.assertTrue(
            any(prefix in guide for prefix in prefixes),
            "the chat guide does not name the dm --owner prefix",
        )
        self.assertEqual(cli.build_parser().parse_args(["dm", "--owner", "--", "x"]).command, "dm")
        with self.assertRaises(cli.UsageError):
            cli._check_text_args(argparse.Namespace(text=[], body_file=None))

    def test_the_listener_protocol_says_the_daemon_posts_no_reply_and_no_ack(self) -> None:
        doc = self.doc("simple-tools-listener.md")
        self.assertNotIn("queued for confirmation in a Claude session", doc)
        self.assertNotIn("posts a fixed one-line ack", doc, "the owner ack is gone with the reply")
        self.assertIn("The operator should never be answering on behalf of a platform", doc)
        self.assertIn("operator_never_answers", doc)
        self.assertFalse(hasattr(W, "OWNER_ACK"), "the fixed ack the Operator used to post is gone")


class ReplyTextTests(unittest.TestCase):
    """What is left of the reply text helpers:  the mention neutralizer the Operator's notes still
    use, and the secret scanner.  Nothing builds a wake reply any more (`operator_never_answers`)."""

    def test_compose_sanitize_keeps_user_mentions_loud(self) -> None:
        self.assertEqual(
            W.sanitize_compose_mentions("ping @**Codex** please"),
            "ping @**Codex** please",
        )
        self.assertEqual(W.sanitize_compose_mentions("heads up @*fleet* now"), "heads up  now")
        self.assertEqual(W.sanitize_compose_mentions("@@*x***Bob**"), "@**Bob**")
        self.assertEqual(
            W.sanitize_compose_mentions("note @**all** and @**topic**"),
            "note @**all** and @**topic**",
        )
        self.assertEqual(W.sanitize_compose_mentions("hi @**everyone**"), "hi @**everyone**")
        self.assertEqual(W.sanitize_compose_mentions("@@*x***all**"), "@**all**")

    def test_mentions_are_neutralized_and_groups_removed(self) -> None:
        body = W.neutralize_mentions(
            "@**Codex** and @**Jay Wedgeworth|12**, ping @*fleet* and @_*ops* @**all**"
        )
        self.assertNotIn("@**", body)
        self.assertIn("@_**Codex**", body)
        self.assertIn("@_**Jay Wedgeworth|12**", body)
        self.assertNotIn("fleet", body)
        self.assertNotIn("ops", body)

    def test_nothing_builds_a_wake_reply_or_a_route_page_any_more(self) -> None:
        for name in ("compose_reply", "compose_route", "OWNER_ACK", "loud_mentions"):
            self.assertFalse(hasattr(W, name), name)

    def test_removing_a_group_mention_never_rebuilds_a_live_mention(self) -> None:
        cases = [
            "@@*x***Bob**",
            "@@*a*@*b***Bob**",
            "@@_*x***Jay Wedgeworth|12**",
            "@*" + "g" * 150 + "*",
            "@*unclosed group",
            "@@*x**@*y***Bob**",
            "@ @*x***Bob**",
            "@**all** @*fleet* @_**ok**",
        ]
        for text in cases:
            with self.subTest(text=text[:30]):
                out = W.neutralize_mentions(text)
                self.assertFalse(W.loud_mention(out), out)
                self.assertNotRegex(out, r"@(?!_)\*", out)
                self.assertEqual(W.neutralize_mentions(out), out, "neutralizing is stable")
        self.assertEqual(W.neutralize_mentions("@@*x***Bob**"), "@_**Bob**")

    def test_the_secret_scanner_flags_keys_and_loaded_credentials(self) -> None:
        loaded = secrets.token_hex(16)
        # Synthetic, obviously fake values in each provider's key SHAPE:  the scanner matches on
        # shape, so a marker that does not match would test nothing.  They are assembled at
        # runtime so gitleaks does not flag the test source; none is a credential.
        prefix = "gh" + "p_"
        cases = {
            "github": "here " + prefix + "A" * 36,
            "anthropic": "sk-" + "ant-" + "b" * 30,
            "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
            "private": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "zulip": "key " + "aB3" + "x" * 29,
            "assigned": "api_key = " + "Z" * 20,
            "loaded": "oops " + loaded,
            "basic": "Authorization: Basic " + "Y2xhdWRl" * 4,
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertIsNotNone(secretscan.scan(text, [loaded]))
        for text in (
            "commit 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
            "id 4821, see AFC#412",
            "a plain sentence.",
        ):
            self.assertIsNone(secretscan.scan(text, [loaded]), text)


class BudgetTests(unittest.TestCase):
    # The five wake count caps have no default since Nov 10, 2026 (an unset cap is no limit, see
    # test_seat_schema.py), so these tests set them, as a seat does, to the numbers they pin.
    budget = dict(
        BUDGET_DEFAULTS,
        wakes_per_hour=6,
        wakes_per_day=40,
        per_topic_per_hour=2,
        owner_per_day=20,
        owner_per_topic_per_hour=6,
    )

    def view(self, wake_id, at, *, owner=False, key="k", state="done", cost=0.03):
        return {
            "wake_id": wake_id,
            "state": state,
            "owner": owner,
            "topic_key": key,
            "cost_usd": cost,
            "ts": at,
            "times": {"accepted": at, "started": at, state: at},
        }

    def test_peer_limits(self) -> None:
        now = 100_000.0
        hour = {str(i): self.view(str(i), now - 60 * i, key="k%d" % i) for i in range(6)}
        self.assertEqual(
            W.budget_block(hour, self.budget, owner=False, key="new", now=now), "wakes_per_hour"
        )
        self.assertIsNone(W.budget_block(hour, self.budget, owner=True, key="new", now=now))
        topic = {str(i): self.view(str(i), now - 60 * i) for i in range(2)}
        self.assertEqual(
            W.budget_block(topic, self.budget, owner=False, key="k", now=now), "per_topic_per_hour"
        )
        day = {str(i): self.view(str(i), now - 3700 * (i + 1), key="k%d" % i) for i in range(23)}
        self.assertEqual(
            W.budget_block(
                day, dict(self.budget, wakes_per_day=20), owner=False, key="new", now=now
            ),
            "wakes_per_day",
        )

    def test_owner_limits(self) -> None:
        now = 100_000.0
        owner = {
            str(i): self.view(str(i), now - 4000 * i, owner=True, key="k%d" % i, cost=0.0)
            for i in range(20)
        }
        self.assertEqual(
            W.budget_block(owner, self.budget, owner=True, key="new", now=now), "owner_per_day"
        )
        topic = {str(i): self.view(str(i), now - 60 * i, owner=True, cost=0.0) for i in range(6)}
        self.assertEqual(
            W.budget_block(topic, self.budget, owner=True, key="k", now=now),
            "owner_per_topic_per_hour",
        )

    def test_dollars_no_longer_bound_a_seat(self) -> None:
        """Owner 2026-10-13:  wake accounting is frequency-only.

        The dollar axis never measured anything real -- a subscription-backed wake is 0.0 and a
        metered one's per-run maximum is approximate -- while what costs downstream is the fan-out.
        An arbitrary dollar total must not stop a wake."""
        now = 100_000.0
        spent = {
            str(i): self.view(str(i), now - 5000 * (i + 1), owner=True, key="k%d" % i, cost=999.99)
            for i in range(7)
        }
        self.assertIsNone(W.budget_block(spent, self.budget, owner=True, key="new", now=now))

    def test_the_count_caps_still_bound_a_seat(self) -> None:
        # Frequency IS the budget now, so prove the cap that replaced the dollar ceiling bites.
        now = 100_000.0
        cap = int(self.budget["owner_per_day"])
        many = {
            str(i): self.view(str(i), now - 60 * (i + 1) - 1, owner=True, key="k%d" % i)
            for i in range(cap)
        }
        self.assertEqual(
            W.budget_block(many, self.budget, owner=True, key="new", now=now), "owner_per_day"
        )

    def test_accepted_wakes_are_not_charged_a_reservation(self) -> None:
        # Nothing is reserved any more:  a queued wake costs nothing to hold, and the eviction
        # machinery that used to spend `reserve_usd` has nothing to spend.
        now = 100_000.0
        views = {"done": self.view("done", now - 100, owner=True, key="k0", cost=1.75)}
        for n in range(4):
            views["a%d" % n] = {
                "wake_id": "a%d" % n,
                "state": "accepted",
                "owner": True,
                "topic_key": "k%d" % (n + 1),
                "ts": now - 10,
                "times": {"queued": now - 15, "accepted": now - 10},
            }
        self.assertIsNone(W.budget_block(views, self.budget, owner=True, key="new", now=now))
        for n in range(3):
            self.assertIsNone(
                W.budget_block(
                    views, self.budget, owner=True, key="k%d" % (n + 1), now=now, exclude="a%d" % n
                )
            )

class CoalescerTests(unittest.TestCase):
    def make(self):
        ids = iter("w%d" % i for i in range(100))
        return W.Coalescer(window=20, max_window=90, owner_window=5, new_id=lambda: next(ids))

    def test_window_extends_up_to_the_max_and_owner_items_wait_five_seconds(self) -> None:
        c = self.make()
        item = {"channel": "simple-tools", "topic": "t"}
        p = c.offer("CLAUDE", dict(item, id=1), False, 0.0)
        self.assertEqual(p.due, 20.0)
        for t in (15.0, 30.0, 45.0, 60.0, 75.0, 85.0):
            c.offer("CLAUDE", dict(item, id=int(t)), False, t)
        self.assertEqual(p.due, 90.0)
        self.assertEqual(c.take_due(89.0), [])
        self.assertEqual([x.wake_id for x in c.take_due(90.0)], ["w0"])
        owner = c.offer("CLAUDE", dict(item, id=200), True, 100.0)
        self.assertEqual(owner.due, 105.0)
        c.offer("CLAUDE", dict(item, id=201), False, 101.0)
        self.assertEqual(owner.due, 105.0, "a peer item never delays an owner wake")
        peer = c.offer("CLAUDE", {"channel": "simple-tools", "topic": "u", "id": 300}, False, 100.0)
        c.offer("CLAUDE", {"channel": "simple-tools", "topic": "u", "id": 301}, True, 101.0)
        self.assertEqual(peer.due, 106.0)
        self.assertTrue(peer.owner)
        self.assertEqual(peer.owner_ids, [301])
        self.assertEqual([x.owner for x in c.take_due(200.0)], [True, True])


class LedgerAndGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="simple-tools-ledger-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_a_started_wake_with_no_outcome_is_failed_and_never_rerun(self) -> None:
        ledger = W.Ledger(os.path.join(self.dir, "wakes.jsonl"))
        p = W.Pending(
            "w1", "CLAUDE", {"channel": "simple-tools", "topic": "t"}, owner=True, now=1.0, due=6.0
        )
        p.add({"id": 5}, True)
        ledger.append(p.row("queued", 1.0))
        ledger.append(p.row("accepted", 6.0))
        ledger.append(p.row("started", 7.0, reserved_usd=0.25))
        q = W.Pending(
            "w2", "CLAUDE", {"channel": "simple-tools", "topic": "u"}, owner=False, now=8.0, due=28.0
        )
        q.add({"id": 6}, False)
        ledger.append(q.row("queued", 8.0))
        reload, crashed = ledger.recover(100.0)
        self.assertEqual([v["wake_id"] for v in reload], ["w2"])
        self.assertEqual([v["wake_id"] for v in crashed], ["w1"])
        views = ledger.wakes()
        self.assertEqual(views["w1"]["state"], "failed")
        self.assertNotIn("cost_usd", views["w1"], "a crashed wake is no longer costed")
        self.assertEqual(W.Pending.from_view(reload[0]).trigger_ids, [6])
        again, crashed_again = ledger.recover(200.0)
        self.assertEqual(crashed_again, [], "a second restart finds nothing new to fail")
        self.assertEqual([v["wake_id"] for v in again], ["w2"])

    def test_skip_leases_and_the_trigger_time_survive_a_restart(self) -> None:
        ledger = W.Ledger(os.path.join(self.dir, "wakes.jsonl"))
        r = W.Pending(
            "w3", "CLAUDE", {"channel": "simple-tools", "topic": "v"}, owner=False, now=9.0, due=29.0
        )
        r.add({"id": 7, "ts": 500.0, "released_from": "L1"}, False)
        r.add({"id": 8, "ts": 400.0, "returned_from": "L2"}, False)
        ledger.append(r.row("queued", 9.0))
        restored = W.Pending.from_view(ledger.wakes()["w3"])
        self.assertEqual((restored.skip_leases, restored.trigger_ts), (["L1", "L2"], 500.0))

    def test_loop_guard_can_be_disabled_without_erasing_history(self) -> None:
        guard = W.LoopGuard(os.path.join(self.dir, "loopguard.json"), enabled=False)
        for n in range(4):
            guard.note("channel\x00topic", wake_reply=True, owner=False, now=1000.0 + n)
        self.assertEqual(guard.count("channel\x00topic"), 4)
        self.assertFalse(guard.blocked("channel\x00topic"))
        self.assertEqual(guard.holding(), [])
        guard.enabled = True
        self.assertTrue(guard.blocked("channel\x00topic"))
        self.assertEqual(len(guard.holding()), 1)

    def test_loop_guard_is_off_unless_it_is_turned_on(self) -> None:
        """Owner, Nov 10, 2026:  nothing throttles a wake, so the guard's own default is off.  It still
        counts, and turning it on later blocks the topic from the history it kept."""
        guard = W.LoopGuard(os.path.join(self.dir, "loopguard.json"))
        self.assertFalse(guard.enabled)
        for n in range(4):
            guard.note("k", wake_reply=True, owner=False, now=1000.0 + n)
        self.assertEqual(guard.count("k"), 4)
        self.assertFalse(guard.blocked("k"))
        guard.enabled = True
        self.assertTrue(guard.blocked("k"))

    def test_loop_guard_resets_only_on_an_owner_post(self) -> None:
        guard = W.LoopGuard(os.path.join(self.dir, "loopguard.json"), enabled=True)
        for _ in range(3):
            guard.note("k", wake_reply=True, owner=False)
        self.assertTrue(guard.blocked("k"))
        guard.note("k", wake_reply=False, owner=False)
        self.assertTrue(guard.blocked("k"), "a peer post does not reset it")
        guard.note("k", wake_reply=False, owner=True)
        self.assertFalse(guard.blocked("k"))

    def test_a_topic_count_never_decays(self) -> None:
        guard = W.LoopGuard(os.path.join(self.dir, "loopguard.json"), enabled=True)
        for n in range(3):
            guard.note("k", wake_reply=True, owner=False, now=1000.0 + n)
        self.assertTrue(
            guard.blocked("k", now=1000.0 + 30 * W.DAY), "a topic waits for the owner, however long"
        )

    def test_a_bot_only_dm_count_is_rolling(self) -> None:
        """Owner 2026-10-09 made bot DMs wake.  The owner can never post in a DM between two bots, so a
        fixed count would block that pair for good:  there 3 wake replies within DM_WINDOW stop it, and it
        wakes again once the oldest of them ages out."""
        guard = W.LoopGuard(os.path.join(self.dir, "loopguard.json"), enabled=True)
        window = W.LoopGuard.DM_WINDOW
        for at in (1000.0, 1100.0):
            guard.note("dm", wake_reply=True, owner=False, rolling=True, now=at)
        self.assertFalse(guard.blocked("dm", now=1200.0))
        guard.note("dm", wake_reply=False, owner=False, rolling=True, now=1150.0)
        self.assertFalse(
            guard.blocked("dm", now=1200.0), "only wake replies count, never other posts"
        )
        guard.note("dm", wake_reply=True, owner=False, rolling=True, now=1200.0)
        self.assertTrue(guard.blocked("dm", now=1201.0), "the third wake reply stops the ping-pong")
        self.assertTrue(guard.blocked("dm", now=1000.0 + window - 1))
        self.assertFalse(guard.blocked("dm", now=1000.0 + window + 1), "the oldest reply aged out")
        guard.note("dm", wake_reply=True, owner=False, rolling=True, now=1000.0 + window + 2)
        self.assertTrue(
            guard.blocked("dm", now=1000.0 + window + 3),
            "a fresh reply inside the window blocks again",
        )
        guard.note("dm", wake_reply=False, owner=True, rolling=True, now=1000.0 + window + 4)
        self.assertFalse(
            guard.blocked("dm", now=1000.0 + window + 5), "an owner post still resets it"
        )


class NotifyAndBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="simple-tools-notify-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_hostile_text_reaches_osascript_only_as_argv(self) -> None:
        calls: list[list[str]] = []
        notifier = A.Notifier(self.root, clock=lambda: 1000.0, runner=calls.append)
        topic = 'x" & do shell script "touch /tmp/pwned" & "\nline two\x07'
        self.assertTrue(
            notifier.notify("CLAUDE", "simple-tools " + topic, "about " + topic, kind="k")
        )
        argv = calls[0]
        self.assertEqual(
            argv[:8],
            [
                "/usr/bin/osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 1 of argv) with title (item 2 of argv)",
                "-e",
                "end run",
                "--",
            ],
        )
        self.assertEqual(len(argv), 10)
        for value in argv[8:]:
            self.assertNotIn("\n", value)
            self.assertNotIn("\x07", value)
            self.assertLessEqual(len(value), A.NOTIFY_CUT)
        self.assertTrue(all("do shell script" not in a for a in argv[:8]))
        self.assertFalse(notifier.notify("CLAUDE", "t", "again", kind="k"), "once a day per key")
        queue = L.read_jsonl(L.SeatPaths(self.root, "CLAUDE").owner_queue)
        self.assertEqual(len(queue), 1)

    def test_banners_false_keeps_the_rate_limit_and_skips_the_banner(self) -> None:
        calls: list[list[str]] = []
        notifier = A.Notifier(self.root, clock=lambda: 1000.0, runner=calls.append, banners=True)
        self.assertTrue(
            notifier.notify(
                "CLAUDE",
                "simple-tools CLAUDE",
                "quota text",
                kind="wake-quota-hold",
                key="quota-hold:CLAUDE:11",
                banners=False,
            )
        )
        self.assertEqual(calls, [], "this call records the notice and does not display it")
        queue = L.read_jsonl(L.SeatPaths(self.root, "CLAUDE").owner_queue)
        self.assertEqual(
            [(row["kind"], row["text"]) for row in queue], [("wake-quota-hold", "quota text")]
        )
        self.assertFalse(
            notifier.notify(
                "CLAUDE",
                "simple-tools CLAUDE",
                "quota text",
                kind="wake-quota-hold",
                key="quota-hold:CLAUDE:11",
                banners=False,
            )
        )
        self.assertEqual(len(L.read_jsonl(L.SeatPaths(self.root, "CLAUDE").owner_queue)), 1)
        self.assertTrue(
            notifier.notify("CLAUDE", "simple-tools CLAUDE", "other", kind="other", key="other")
        )
        self.assertEqual(len(calls), 1, "a later notice still banners")

if __name__ == "__main__":
    unittest.main()


class LedgerCacheTests(unittest.TestCase):
    """status() reads every seat's ledger every 2 s; the file is parsed only when it changed."""

    def test_rows_reparse_only_after_the_file_changes(self) -> None:
        import tempfile
        from unittest import mock

        from simple_tools import live as live_mod
        from simple_tools import wakes as wakes_mod

        with tempfile.TemporaryDirectory() as d:
            ledger = wakes_mod.Ledger(os.path.join(d, "wakes.jsonl"))
            ledger.append({"wake_id": "w1", "state": "queued", "ts": 1.0})
            real = live_mod.read_jsonl
            with mock.patch.object(wakes_mod.L, "read_jsonl", side_effect=real) as spy:
                self.assertEqual(len(ledger.rows()), 1)
                self.assertEqual(len(ledger.rows()), 1)
                self.assertEqual(spy.call_count, 1, "an unchanged ledger must not be re-parsed")
                ledger.append({"wake_id": "w1", "state": "started", "ts": 2.0})
                self.assertEqual(len(ledger.rows()), 2)
                self.assertEqual(spy.call_count, 2, "an append must be seen on the next read")
                rows = ledger.rows()
                rows[0]["state"] = "mutated"
                self.assertEqual(
                    ledger.rows()[0]["state"], "queued", "callers get copies, not the cache"
                )
