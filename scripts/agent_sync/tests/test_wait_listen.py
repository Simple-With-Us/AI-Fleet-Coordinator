"""wait and listen: backfill, events, request ordering, queue recovery and cleanup."""
from __future__ import annotations

import io
import json
import threading
import time
import unittest
from unittest import mock

from agent_sync import cli
from agent_sync.tests.harness import TAG, Harness

OWN = "[CLAUDE\u00b7%s] " % TAG  # what every post of the test session starts with
GATEWAY = {"result": "error", "msg": "Bad Gateway"}


def shown_ids(output: str) -> list[int]:
    return [int(line.rsplit("id ", 1)[1]) for line in output.splitlines() if line.startswith("#")]


class WaitTests(Harness):
    def seed_cursor(self, topic: str = "t") -> int:
        """Give this session a saved cursor for the topic by reading it once."""
        seed = self.fake.add_message("Codex", "agent-sync", topic, "seed")
        self.assertEqual(self.run_cli("read", "--topic", topic, "--new").code, 0)
        return seed

    def cursor(self, topic: str = "t") -> int:
        return json.loads(self.state_path().read_text())["cursors"]["agent-sync\u0000" + topic]

    def test_backfill_path_delivers_what_arrived_while_away_and_exits_0(self) -> None:
        self.seed_cursor()
        missed = self.fake.add_message("Codex", "agent-sync", "t", "while you were out")
        started = time.monotonic()
        result = self.run_cli("wait", "--topic", "t", "--timeout", "30")
        self.assertEqual(result.code, 0, result.err)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(shown_ids(result.out), [missed])
        self.assertIn("while you were out", result.out)
        self.assertEqual(self.cursor(), missed)
        self.assertEqual(len(self.fake.registrations), 1)
        self.assertEqual(self.fake.deleted_queues, ["q1"])

    def test_register_happens_before_the_backfill_fetch(self) -> None:
        self.seed_cursor()
        self.fake.add_message("Codex", "agent-sync", "t", "pending")
        before = len(self.fake.requests)
        self.assertEqual(self.run_cli("wait", "--topic", "t", "--timeout", "30").code, 0)
        log = self.fake.request_log()[before:]
        self.assertEqual(log[0], ("POST", "register"))
        self.assertLess(log.index(("POST", "register")), log.index(("GET", "messages")))
        self.assertEqual(log[-1], ("DELETE", "events"))

    def test_register_narrow_is_pair_shaped_and_events_are_raw(self) -> None:
        self.run_cli("wait", "--topic", "t", "--timeout", "0.2")
        registration = self.fake.registrations[0]
        self.assertEqual(registration["narrow"], [["channel", "agent-sync"], ["topic", "t"]])
        self.assertEqual(registration["params"]["event_types"], '["message"]')
        self.assertEqual(registration["params"]["apply_markdown"], "false")

    def test_event_path_delivers_a_message_that_arrives_while_waiting(self) -> None:
        self.seed_cursor()
        # Added once wait's long poll is open, so the backfill is over and only the event can carry it.
        self.fake.when(self.fake.polling(), lambda: self.fake.add_message("Codex", "agent-sync", "t", "late news"))
        result = self.run_cli("wait", "--topic", "t", "--timeout", "20")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("late news", result.out)
        self.assertEqual(self.cursor(), self.fake.messages[-1]["id"])
        self.assertEqual(self.fake.deleted_queues, ["q1"])
        self.assertTrue(self.fake.requests_to("GET", "events"))

    def test_timeout_exits_4_prints_nothing_and_still_deletes_the_queue(self) -> None:
        self.seed_cursor()
        result = self.run_cli("wait", "--topic", "t", "--timeout", "0.5")
        self.assertEqual(result.code, 4)
        self.assertEqual(result.out, "")
        self.assertIn("timed out", result.err)
        self.assertEqual(self.fake.deleted_queues, ["q1"])

    def test_no_saved_cursor_sets_it_to_the_newest_and_prints_no_history(self) -> None:
        newest = 0
        for n in range(3):
            newest = self.fake.add_message("Codex", "agent-sync", "t", "history %d" % n)
        result = self.run_cli("wait", "--topic", "t", "--timeout", "0.5")
        self.assertEqual(result.code, 4)
        self.assertEqual(result.out, "")
        self.assertEqual(self.cursor(), newest)

    def test_own_posts_do_not_wake_wait_but_a_peer_after_them_does(self) -> None:
        self.seed_cursor()

        def chatter() -> None:
            time.sleep(0.3)
            self.run_cli("post", "--topic", "t", "my own words")
            time.sleep(0.3)
            self.fake.add_message("Codex", "agent-sync", "t", "a real answer")

        thread = threading.Thread(target=chatter, daemon=True)
        thread.start()
        result = self.run_cli("wait", "--topic", "t", "--timeout", "20")
        thread.join(5)
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("a real answer", result.out)
        self.assertNotIn("my own words", result.out)

    def test_only_own_posts_means_timeout(self) -> None:
        self.seed_cursor()
        threading.Timer(0.2, lambda: self.run_cli("post", "--topic", "t", "talking to myself")).start()
        self.assertEqual(self.run_cli("wait", "--topic", "t", "--timeout", "1.2").code, 4)

    def test_sibling_session_wakes_wait_and_is_labelled(self) -> None:
        self.seed_cursor()
        threading.Timer(0.3, lambda: self.fake.add_message(
            self.fake.me, "agent-sync", "t", "[CLAUDE·aaaabbbb] hello from my other session")).start()
        result = self.run_cli("wait", "--topic", "t", "--timeout", "20")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("(sibling)", result.out)

    def test_other_topics_do_not_wake_wait(self) -> None:
        self.seed_cursor()
        threading.Timer(0.2, lambda: self.fake.add_message("Codex", "agent-sync", "elsewhere", "noise")).start()
        self.assertEqual(self.run_cli("wait", "--topic", "t", "--timeout", "1.0").code, 4)

    def test_bad_event_queue_id_recovers_by_registering_again_and_backfilling(self) -> None:
        seed = self.seed_cursor()

        def expire_then_post() -> None:
            with self.fake.cond:
                self.fake.expire_queues()  # the server forgets the queue ...
                self.fake.add_message("Codex", "agent-sync", "t", "sent while the queue was gone", deliver=False)

        self.fake.when(self.fake.polling(), expire_then_post)  # ... once the first long poll is open
        result = self.run_cli("wait", "--topic", "t", "--timeout", "20")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("sent while the queue was gone", result.out)
        self.assertEqual(len(self.fake.registrations), 2)
        log = self.fake.request_log()
        second_register = [i for i, entry in enumerate(log) if entry == ("POST", "register")][1]
        backfill_after = [i for i, entry in enumerate(log) if entry == ("GET", "messages") and i > second_register]
        self.assertTrue(backfill_after, "no backfill after the second register")
        backfill = [r for r in self.fake.requests_to("GET", "messages")][-1]
        self.assertEqual(backfill.query["anchor"], str(seed))
        self.assertEqual(backfill.query["include_anchor"], "false")
        self.assertEqual(self.fake.deleted_queues, ["q2"])

    def test_a_fresh_session_that_posts_then_waits_sees_a_reply_that_came_in_early(self) -> None:
        self.assertEqual(self.run_cli("post", "--topic", "t", "question?").code, 0)
        reply = self.fake.add_message("Codex", "agent-sync", "t", "the quick answer")  # before wait even starts
        result = self.run_cli("wait", "--topic", "t", "--timeout", "10")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(shown_ids(result.out), [reply])
        self.assertEqual(self.cursor(), reply)

    def test_post_seeds_the_cursor_only_when_there_is_none(self) -> None:
        seed = self.seed_cursor()
        self.run_cli("post", "--topic", "t", "later")
        self.assertEqual(self.cursor(), seed)
        self.run_cli("post", "--topic", "fresh topic", "first post")
        self.assertEqual(self.cursor("fresh topic"), self.fake.messages[-1]["id"])

    def test_a_message_landing_between_register_and_the_first_baseline_is_not_lost(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "old history")
        self.fake.inject("GET", "messages", delay=0.5)  # the baseline fetch is slow ...
        # ... and the message is added the moment the queue exists, so it lands before that fetch returns
        self.fake.when(lambda: self.fake.registrations,
                       lambda: self.fake.add_message("Codex", "agent-sync", "t", "arrived in the gap"))
        result = self.run_cli("wait", "--topic", "t", "--timeout", "10")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("arrived in the gap", result.out)
        self.assertNotIn("old history", result.out)

    def test_backfill_pages_past_a_full_page_of_this_sessions_own_posts(self) -> None:
        self.seed_cursor()
        for n in range(cli.BACKFILL_LIMIT):  # a whole page the delivery filter skips
            self.fake.add_message(self.fake.me, "agent-sync", "t", OWN + "own %d" % n)
        peer = self.fake.add_message("Codex", "agent-sync", "t", "the real question")
        result = self.run_cli("wait", "--topic", "t", "--timeout", "3")
        self.assertEqual(result.code, 0, result.err)  # without paging this is a timeout, or a later message
        self.assertEqual(shown_ids(result.out), [peer])
        self.assertEqual(self.cursor(), peer)
        self.assertEqual(self.fake.requests_to("GET", "events"), [])  # found in the backfill, never reached the stream

    def test_wait_survives_a_gateway_error_on_the_long_poll(self) -> None:
        self.seed_cursor()
        self.fake.inject("GET", "events", status=502, body=GATEWAY)
        self.fake.when(self.fake.polling(2), lambda: self.fake.add_message("Codex", "agent-sync", "t", "after the blip"))
        result = self.run_cli("wait", "--topic", "t", "--timeout", "20")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("after the blip", result.out)
        self.assertEqual(self.sleeps, [1.0])
        self.assertEqual(self.fake.deleted_queues, ["q1"])

    def test_wait_survives_a_gateway_error_on_the_backfill(self) -> None:
        self.seed_cursor()
        self.fake.add_message("Codex", "agent-sync", "t", "waiting for you")
        self.fake.inject("GET", "messages", status=503, body=GATEWAY)
        result = self.run_cli("wait", "--topic", "t", "--timeout", "20")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("waiting for you", result.out)

    def test_a_failed_print_leaves_the_message_unseen_for_the_next_read(self) -> None:
        seed = self.seed_cursor()
        missed = self.fake.add_message("Codex", "agent-sync", "t", "must not be lost")
        code = self.run_with_closed_stdout("wait", "--topic", "t", "--timeout", "5")
        self.assertEqual(code, 0)  # a closed pipe ends the command quietly
        self.assertEqual(self.cursor(), seed)  # the cursor did not pass the message that was never shown
        again = self.run_cli("read", "--topic", "t", "--new")
        self.assertEqual(shown_ids(again.out), [missed])

    def run_with_closed_stdout(self, *argv: str) -> int:
        class ClosedPipe(io.StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError(32, "Broken pipe")

        err = io.StringIO()
        code = cli.main(list(argv), env=self.env(), stdin=io.StringIO(""), stdout=ClosedPipe(), stderr=err,
                        home=self.home, sleep=self.sleeps.append, timeout=5.0, events_timeout=3.0)
        self.transcript.append(err.getvalue())
        return code

    def test_a_failed_print_during_the_listen_backfill_leaves_the_message_unseen(self) -> None:
        seed = self.seed_cursor()
        missed = self.fake.add_message("Codex", "agent-sync", "t", "also must not be lost")
        self.assertEqual(self.run_with_closed_stdout("listen", "--topic", "t"), 0)
        self.assertEqual(self.cursor(), seed)
        self.assertEqual(shown_ids(self.run_cli("read", "--topic", "t", "--new").out), [missed])

    def test_a_failed_queue_delete_is_a_warning_not_a_failure(self) -> None:
        self.seed_cursor()
        self.fake.add_message("Codex", "agent-sync", "t", "news")
        self.fake.inject("DELETE", "events", status=500, body={"result": "error", "msg": "boom", "code": "INTERNAL"})
        result = self.run_cli("wait", "--topic", "t", "--timeout", "10")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("could not delete the event queue", result.err)

    def test_keyboard_interrupt_still_deletes_the_queue(self) -> None:
        from unittest import mock
        from agent_sync import zulip as Z

        self.seed_cursor()
        with mock.patch.object(Z.EventQueue, "poll", side_effect=KeyboardInterrupt):
            result = self.run_cli("wait", "--topic", "t", "--timeout", "20")
        self.assertEqual(result.code, 130)
        self.assertEqual(self.fake.deleted_queues, ["q1"])

    def test_json_mode_prints_json_lines(self) -> None:
        self.seed_cursor()
        self.fake.add_message("Codex", "agent-sync", "t", "as json")
        result = self.run_cli("wait", "--topic", "t", "--timeout", "10", "--json")
        self.assertEqual(json.loads(result.out)["content"], "as json")

    def test_wait_requires_a_topic(self) -> None:
        self.assertEqual(self.run_cli("wait").code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_a_dead_realm_is_a_network_error(self) -> None:
        import socket
        from agent_sync.tests.fake_zulip import BOT_EMAIL
        from agent_sync.tests.harness import write_rc

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = "http://127.0.0.1:%d" % probe.getsockname()[1]
        write_rc(self.rc_path, email=BOT_EMAIL, key=self.key, site=dead)
        result = self.run_cli("wait", "--topic", "t", "--timeout", "1", env=self.env(AGENT_SYNC_REALM=dead))
        self.assertEqual(result.code, 6)
        self.assertIn("POST register failed", result.err)


class ListenTests(Harness):
    def start_listen(self, *argv: str, stop: threading.Event | None = None):
        return self.run_in_thread("listen", *argv, stop=stop, events_timeout=3.0)

    def wait_for_queues(self, count: int, scopes: int | None = None, timeout: float = 5.0) -> None:
        """Wait until `count` queues exist and every scope has finished its baseline fetch.  A
        message that lands between a register and the first-run baseline counts as history, so
        the tests must not add messages before then."""
        scopes = count if scopes is None else scopes
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.fake.queues) >= count and len(self.fake.requests_to("GET", "messages")) >= scopes:
                break
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.fake.queues), count)
        self.assertGreaterEqual(len(self.fake.requests_to("GET", "messages")), scopes)

    def wait_for_cursor(self, topic: str, at_least: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                cursors = json.loads(self.state_path().read_text())["cursors"]
                if cursors.get("agent-sync\u0000" + topic, 0) >= at_least:
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.02)
        self.fail("the cursor for %r never reached %d" % (topic, at_least))

    def test_streams_messages_from_each_topic_and_exits_after_max_messages(self) -> None:
        run = self.start_listen("--topic", "a", "--topic", "b", "--max-messages", "2")
        self.wait_for_queues(2, scopes=2)
        self.fake.add_message("Codex", "agent-sync", "a", "news in a")
        self.fake.add_message("Cursor", "agent-sync", "b", "news in b")
        self.fake.add_message("Codex", "agent-sync", "c", "unwatched")
        result = run.join()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("news in a", result.out)
        self.assertIn("news in b", result.out)
        self.assertNotIn("unwatched", result.out)
        self.assertEqual(len(self.fake.registrations), 2)  # one queue per topic
        self.assertEqual(sorted(r["narrow"][1][1] for r in self.fake.registrations), ["a", "b"])
        self.assertCountEqual(self.fake.deleted_queues, ["q1", "q2"])

    def test_stop_event_ends_it_and_deletes_the_queues(self) -> None:
        stop = threading.Event()
        run = self.start_listen("--topic", "a", stop=stop)
        self.wait_for_queues(1)
        stop.set()
        result = run.join()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.fake.deleted_queues, ["q1"])

    def test_own_posts_are_not_echoed_but_siblings_are(self) -> None:
        run = self.start_listen("--topic", "a", "--max-messages", "1")
        self.wait_for_queues(1)
        self.run_cli("post", "--topic", "a", "my own words")
        self.run_cli("post", "--topic", "a", "--no-tag", "my own untagged words")  # only the ledger knows it is mine
        self.fake.add_message(self.fake.me, "agent-sync", "a", "[CLAUDE\u00b7aaaabbbb] other session here")
        result = run.join()
        self.assertEqual(result.code, 0, result.err)
        self.assertNotIn("my own words", result.out)
        self.assertNotIn("my own untagged words", result.out)
        self.assertIn("other session here", result.out)
        self.assertIn("(sibling)", result.out)

    def test_mentions_anywhere_are_printed_once_and_marked(self) -> None:
        run = self.start_listen("--topic", "a", "--mentions", "--max-messages", "2")
        self.wait_for_queues(2, scopes=2)
        mention_registration = [r for r in self.fake.registrations if r["narrow"] is None]
        self.assertEqual(len(mention_registration), 1)
        self.fake.add_message("Codex", "other", "elsewhere", "plain chatter, nobody mentioned")
        self.fake.add_message("Jay Wedgeworth", "other", "elsewhere", "ping @**Claude** over here")
        self.fake.add_message("Codex", "agent-sync", "a", "also @**Claude** in a watched topic")
        result = run.join()
        self.assertEqual(result.code, 0, result.err)
        self.assertNotIn("plain chatter", result.out)
        self.assertEqual(result.out.count("ping @**Claude** over here"), 1)
        self.assertEqual(result.out.count("also @**Claude** in a watched topic"), 1)  # topic and mention queue, one print
        self.assertEqual(result.out.count("@you"), 2)

    def test_more_than_three_topics_share_one_channel_queue_with_client_side_filtering(self) -> None:
        run = self.start_listen("--topic", "a", "--topic", "b", "--topic", "c", "--topic", "d", "--max-messages", "2")
        self.wait_for_queues(1, scopes=4)
        self.assertEqual(len(self.fake.registrations), 1)
        self.assertEqual(self.fake.registrations[0]["narrow"], [["channel", "agent-sync"]])
        self.fake.add_message("Codex", "agent-sync", "zzz", "not a listed topic")
        self.fake.add_message("Codex", "agent-sync", "d", "in d")
        self.fake.add_message("Codex", "agent-sync", "b", "in b")
        result = run.join()
        self.assertNotIn("not a listed topic", result.out)
        self.assertIn("in d", result.out)
        self.assertIn("in b", result.out)

    def test_expired_queue_is_re_registered_and_backfilled(self) -> None:
        run = self.start_listen("--topic", "a", "--max-messages", "2")
        self.wait_for_queues(1)
        first = self.fake.add_message("Codex", "agent-sync", "a", "first")
        self.wait_for_cursor("a", first)  # the first message is printed before the queue goes away
        with self.fake.cond:
            self.fake.expire_queues()
            self.fake.add_message("Codex", "agent-sync", "a", "second, while the queue was gone", deliver=False)
        result = run.join()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("first", result.out)
        self.assertIn("second, while the queue was gone", result.out)
        self.assertEqual(len(self.fake.registrations), 2)

    def test_startup_backfills_from_a_saved_cursor(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "a", "seed")
        self.run_cli("read", "--topic", "a", "--new")
        self.fake.add_message("Codex", "agent-sync", "a", "missed while down")
        result = self.run_in_thread("listen", "--topic", "a", "--max-messages", "1", events_timeout=3.0).join()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("missed while down", result.out)

    def test_startup_backfill_pages_through_a_backlog_bigger_than_one_page(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "a", "seed")
        self.run_cli("read", "--topic", "a", "--new")
        ids = [self.fake.add_message("Codex", "agent-sync", "a", "backlog %d" % n) for n in range(cli.BACKFILL_LIMIT + 50)]
        result = self.run_in_thread("listen", "--topic", "a", "--max-messages", str(len(ids)), events_timeout=3.0).join(60)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(shown_ids(result.out), ids)

    def test_a_server_error_that_repeats_a_credential_is_scrubbed_in_the_retry_notice(self) -> None:
        stop = threading.Event()
        leaky = {"result": "error", "msg": "internal error near " + self.key, "code": "CODE_" + self.key}
        self.fake.inject("GET", "events", status=500, body=leaky)
        with mock.patch.object(cli, "LISTEN_BACKOFF", (0.05, 0.05)):
            run = self.start_listen("--topic", "a", stop=stop)
            # the second long poll comes after the retry notice was printed
            self.fake.when(self.fake.polling(2), stop.set)
            result = run.join()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("retrying", result.err)
        self.assertIn("[redacted]", result.err)
        self.assertNotIn(self.key, result.err)

    def test_cursor_advances_as_messages_print(self) -> None:
        run = self.start_listen("--topic", "a", "--max-messages", "1")
        self.wait_for_queues(1)
        message_id = self.fake.add_message("Codex", "agent-sync", "a", "x")
        run.join()
        cursors = json.loads(self.state_path().read_text())["cursors"]
        self.assertEqual(cursors["agent-sync\u0000a"], message_id)

    def test_json_lines(self) -> None:
        run = self.start_listen("--topic", "a", "--max-messages", "1", "--json")
        self.wait_for_queues(1)
        self.fake.add_message("Codex", "agent-sync", "a", "structured")
        data = json.loads(run.join().out.splitlines()[0])
        self.assertEqual((data["topic"], data["content"], data["is_bot"]), ("a", "structured", True))

    def test_needs_a_topic_or_mentions(self) -> None:
        self.assertEqual(self.run_cli("listen").code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_a_fatal_api_error_ends_listen_with_exit_5(self) -> None:
        self.fake.inject("POST", "register", status=400, body={"result": "error", "msg": "Invalid narrow", "code": "BAD_REQUEST"})
        result = self.run_cli("listen", "--topic", "a")
        self.assertEqual(result.code, 5)
        self.assertIn("Invalid narrow", result.err)


if __name__ == "__main__":
    unittest.main()
