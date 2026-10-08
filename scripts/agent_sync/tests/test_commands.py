"""Command behaviour against the fake server: post, reply, read, inbox, topics, follow, resolve, react."""
from __future__ import annotations

import json
import os
import shlex
import stat
import unittest

from agent_sync.tests.fake_zulip import BOT_EMAIL, EPOCH
from agent_sync.tests.harness import TAG, Harness

# 2026-10-07 23:41 UTC is 6:41pm Central (daylight time), a Wednesday.
OCT_7_2026_2341_UTC = 1_791_416_460


class WhoamiAndListingTests(Harness):
    def test_whoami_human(self) -> None:
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 0, result.err)
        for expected in ("Claude", BOT_EMAIL, "10", "CLAUDE", TAG, str(self.rc_path), self.fake.url):
            self.assertIn(expected, result.out)

    def test_whoami_without_a_session_says_so(self) -> None:
        result = self.run_cli("whoami", env=self.env(CLAUDE_CODE_SESSION_ID=None))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("none", result.out)

    def test_channels(self) -> None:
        result = self.run_cli("channels")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("#agent-sync", result.out)
        self.assertIn("the other channel", result.out)

    def test_topics_newest_first_with_max_id_and_limit(self) -> None:
        first = self.fake.add_message("Codex", "agent-sync", "old topic", "x")
        newest = self.fake.add_message("Codex", "agent-sync", "new topic", "x")
        self.fake.add_message("Codex", "other", "elsewhere", "x")
        result = self.run_cli("topics")
        self.assertEqual(result.code, 0, result.err)
        lines = [line.split(None, 1) for line in result.out.splitlines()]
        self.assertEqual(lines, [[str(newest), "new topic"], [str(first), "old topic"]])
        limited = self.run_cli("topics", "--limit", "1", "--json")
        self.assertEqual(json.loads(limited.out), [{"name": "new topic", "max_id": newest}])

    def test_subscribe_reports_joined_and_already(self) -> None:
        result = self.run_cli("subscribe", "--channel", "agent-sync")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("already subscribed to #agent-sync", result.out)

    def test_subscribe_is_a_plain_post_by_default(self) -> None:
        result = self.run_cli("subscribe", "--channel", "new-channel", "--channel", "agent-sync")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("subscribed to #new-channel", result.out)
        self.assertEqual(self.fake.requests_to("GET", "get_stream_id"), [])  # no pre-check unless asked for
        payload = json.loads(self.fake.requests_to("POST", "users/me/subscriptions")[0].form["subscriptions"])
        self.assertEqual(payload, [{"name": "new-channel"}, {"name": "agent-sync"}])

    def test_subscribe_must_exist_refuses_a_channel_that_cannot_be_found(self) -> None:
        refused = self.run_cli("subscribe", "--channel", "typo-channel", "--must-exist")
        self.assertEqual(refused.code, 2)
        self.assertIn("not found, or is not visible", refused.err)
        self.assertEqual(self.fake.requests_to("POST", "users/me/subscriptions"), [])
        allowed = self.run_cli("subscribe", "--channel", "other", "--must-exist")
        self.assertEqual(allowed.code, 0, allowed.err)
        self.assertEqual(len(self.fake.requests_to("POST", "users/me/subscriptions")), 1)


class PostTests(Harness):
    def last_post(self):
        return self.fake.requests_to("POST", "messages")[-1]

    def test_missing_topic_is_refused_before_any_request(self) -> None:
        result = self.run_cli("post", "hello")
        self.assertEqual(result.code, 2)
        self.assertIn("--topic", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_empty_and_blank_topics_are_refused(self) -> None:
        for topic in ("", "   "):
            with self.subTest(topic=topic):
                result = self.run_cli("post", "--topic", topic, "hello")
                self.assertEqual(result.code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_topic_over_60_characters_is_refused_with_its_length(self) -> None:
        result = self.run_cli("post", "--topic", "t" * 61, "hello")
        self.assertEqual(result.code, 2)
        self.assertIn("61", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_topic_of_exactly_60_characters_is_accepted(self) -> None:
        result = self.run_cli("post", "--topic", "t" * 60, "hello")
        self.assertEqual(result.code, 0, result.err)

    def test_text_is_required(self) -> None:
        self.assertEqual(self.run_cli("post", "--topic", "t").code, 2)

    def test_post_is_tagged_with_seat_and_session_and_goes_to_the_default_channel(self) -> None:
        result = self.run_cli("post", "--topic", "AFC 18f61cf4 Zulip cutover", "hello", "there")
        self.assertEqual(result.code, 0, result.err)
        form = self.last_post().form
        self.assertEqual(form["type"], "stream")
        self.assertEqual(form["to"], "agent-sync")
        self.assertEqual(form["topic"], "AFC 18f61cf4 Zulip cutover")
        self.assertEqual(form["content"], "[CLAUDE·%s] hello there" % TAG)
        message_id = self.fake.messages[-1]["id"]
        self.assertIn("posted id %d" % message_id, result.out)

    def test_post_records_the_id_in_the_session_ledger(self) -> None:
        self.run_cli("post", "--topic", "t", "hello")
        ledger = json.loads(self.state_path().read_text())["posted"]
        self.assertEqual(ledger, [self.fake.messages[-1]["id"]])

    def test_no_tag_and_no_session(self) -> None:
        self.run_cli("post", "--topic", "t", "--no-tag", "raw text")
        self.assertEqual(self.last_post().form["content"], "raw text")
        self.run_cli("post", "--topic", "t", "plain", env=self.env(CLAUDE_CODE_SESSION_ID=None))
        self.assertEqual(self.last_post().form["content"], "[CLAUDE] plain")
        self.assertTrue(self.state_path(None).exists())

    def test_to_adds_the_peer_to_the_tag_and_the_mention(self) -> None:
        result = self.run_cli("post", "--topic", "t", "--to", "codex", "please look")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["content"], "[CLAUDE·%s\u2192CODEX] @**Codex** please look" % TAG)

    def test_to_resolves_by_email_and_for_names_with_spaces(self) -> None:
        self.run_cli("post", "--topic", "t", "--to", "jay@zulip.test", "hi")
        self.assertEqual(self.last_post().form["content"], "[CLAUDE·%s\u2192JAY] @**Jay Wedgeworth** hi" % TAG)

    def test_peer_labels_come_from_the_bot_email_not_the_display_name(self) -> None:
        from agent_sync.cli import seat_tag_for
        bot = lambda email, name: {"email": email, "full_name": name, "is_bot": True}
        cases = {
            ("mm-bot@simplewithus.zulipchat.com", "MiniMax"): "MM",
            ("muse-assist-bot@simplewithus.zulipchat.com", "Rob (Muse)"): "MA",
            ("ag-bot@simplewithus.zulipchat.com", "Antigravity"): "AG",
            ("bf-builder-bot@simplewithus.zulipchat.com", "BF-Builder"): "BF-BUILDER",
            ("compiler-grok-bot@simplewithus.zulipchat.com", "GB-Compiler"): "GB-COMPILER",
            ("openai-dot-bot@simplewithus.zulipchat.com", "Jet (OpenAI dot)"): "JET",
            ("instinct-bat-bot@simplewithus.zulipchat.com", "Echo"): "ECHO",
            ("grok-build-bot@simplewithus.zulipchat.com", "GROK-BUILD"): "GROK-BUILD",
        }
        for (email, name), tag in cases.items():
            with self.subTest(email=email):
                self.assertEqual(seat_tag_for(bot(email, name)), tag)
        self.assertEqual(seat_tag_for({"email": "jay@x", "full_name": "Jay Wedgeworth", "is_bot": False}), "JAY")

    def test_two_recipients(self) -> None:
        self.run_cli("post", "--topic", "t", "--to", "Codex", "--to", "Cursor", "both")
        self.assertEqual(self.last_post().form["content"],
                         "[CLAUDE·%s\u2192CODEX,CURSOR] @**Codex** @**Cursor** both" % TAG)

    def test_unknown_recipient_lists_the_five_closest_names(self) -> None:
        result = self.run_cli("post", "--topic", "t", "--to", "Codx", "hi")
        self.assertEqual(result.code, 2)
        self.assertIn("Codex", result.err)
        listed = result.err.split("closest names:")[1].strip().split(", ")
        self.assertEqual(len(listed), 5)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_fleet_without_the_group_is_refused(self) -> None:
        result = self.run_cli("post", "--topic", "t", "--fleet", "wake up")
        self.assertEqual(result.code, 2)
        self.assertIn("owner must create", result.err)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_fleet_with_the_group_adds_the_group_mention(self) -> None:
        self.fake.user_groups.append({"id": 3, "name": "fleet"})
        result = self.run_cli("post", "--topic", "t", "--fleet", "wake up")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["content"], "[CLAUDE·%s] @*fleet* wake up" % TAG)

    def test_text_dash_reads_stdin(self) -> None:
        self.run_cli("post", "--topic", "t", "-", stdin="line one\nline two\n")
        self.assertEqual(self.last_post().form["content"], "[CLAUDE·%s] line one\nline two" % TAG)

    def test_empty_stdin_is_refused(self) -> None:
        self.assertEqual(self.run_cli("post", "--topic", "t", "-", stdin="  \n").code, 2)

    def test_too_long_message_is_refused(self) -> None:
        self.assertEqual(self.run_cli("post", "--topic", "t", "x" * 10001).code, 2)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_json_output(self) -> None:
        result = self.run_cli("post", "--topic", "t", "--json", "hi")
        self.assertEqual(json.loads(result.out), {"id": self.fake.messages[-1]["id"], "channel": "agent-sync", "topic": "t"})

    def test_other_channel(self) -> None:
        self.run_cli("post", "--topic", "t", "--channel", "other", "hi")
        self.assertEqual(self.last_post().form["to"], "other")


class ReplyTests(Harness):
    def test_reply_goes_to_the_channel_and_topic_of_the_original(self) -> None:
        original = self.fake.add_message("Codex", "other", "AFC 12345678 Some work", "question?")
        result = self.run_cli("reply", "--id", str(original), "answer")
        self.assertEqual(result.code, 0, result.err)
        sent = self.fake.requests_to("POST", "messages")[-1].form
        self.assertEqual((sent["to"], sent["topic"]), ("other", "AFC 12345678 Some work"))
        self.assertEqual(sent["content"], "[CLAUDE·%s] answer" % TAG)
        self.assertIn(self.fake.messages[-1]["id"], json.loads(self.state_path().read_text())["posted"])

    def test_reply_with_to_adds_the_mention(self) -> None:
        original = self.fake.add_message("Codex", "agent-sync", "t", "q")
        self.run_cli("reply", "--id", str(original), "--to", "Codex", "a")
        self.assertEqual(self.fake.requests_to("POST", "messages")[-1].form["content"],
                         "[CLAUDE·%s\u2192CODEX] @**Codex** a" % TAG)

    def test_the_check_hint_after_a_timeout_cannot_smuggle_a_command_in_through_a_topic(self) -> None:
        topic = "x' ; touch /tmp/agent-sync-pwned ; echo '"  # a topic any member of the realm can set
        original = self.fake.add_message("Codex", "other", topic, "question?")
        self.fake.inject("POST", "messages", delay=1.5)
        result = self.run_cli("reply", "--id", str(original), "answer", timeout=0.4)
        self.assertEqual(result.code, 6)
        hint = result.err.split("Check whether it posted: ", 1)[1].strip()
        self.assertEqual(shlex.split(hint),
                         ["agent-sync", "read", "--channel", "other", "--topic", topic, "--include-self"])

    def test_the_check_hint_quotes_an_ordinary_apostrophe_and_a_channel_with_spaces(self) -> None:
        self.fake.inject("POST", "messages", delay=1.5)
        result = self.run_cli("post", "--topic", "Jay's fix", "--channel", "my channel", "a", timeout=0.4)
        self.assertEqual(result.code, 6)
        hint = result.err.split("Check whether it posted: ", 1)[1].strip()
        self.assertEqual(shlex.split(hint),
                         ["agent-sync", "read", "--channel", "my channel", "--topic", "Jay's fix", "--include-self"])

    def test_the_success_line_shows_server_supplied_names_safely(self) -> None:
        original = self.fake.add_message("Codex", "agent-sync", "red\x1b[31mtopic", "q")
        result = self.run_cli("reply", "--id", str(original), "a")
        self.assertEqual(result.code, 0, result.err)
        self.assertNotIn("\x1b", result.out)
        self.assertIn("red\\x1b[31mtopic", result.out)

    def test_reply_refuses_direct_messages(self) -> None:
        dm = self.fake.add_direct_message("Codex", "psst")
        result = self.run_cli("reply", "--id", str(dm), "hi")
        self.assertEqual(result.code, 2)
        self.assertIn("channel-only", result.err)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_reply_to_a_missing_message_is_an_api_error(self) -> None:
        self.assertEqual(self.run_cli("reply", "--id", "424242", "hi").code, 5)


class ReadTests(Harness):
    def test_default_shows_the_newest_messages_in_order(self) -> None:
        ids = [self.fake.add_message("Codex", "agent-sync", "t", "message %d" % n) for n in range(5)]
        self.fake.add_message("Codex", "agent-sync", "other topic", "not me")
        result = self.run_cli("read", "--topic", "t", "--limit", "3")
        self.assertEqual(result.code, 0, result.err)
        shown = [int(line.rsplit("id ", 1)[1]) for line in result.out.splitlines() if line.startswith("#")]
        self.assertEqual(shown, ids[-3:])

    def test_human_format_is_two_lines_plus_body_and_a_blank_line(self) -> None:
        message_id = self.fake.add_message("Codex", "agent-sync", "my topic", "the body\nsecond line",
                                           timestamp=OCT_7_2026_2341_UTC)
        result = self.run_cli("read", "--topic", "my topic")
        self.assertEqual(result.out, "#agent-sync › my topic · id %d\nCodex [bot] · Wed, Oct 7, 6:41pm\n"
                                     "the body\nsecond line\n\n" % message_id)
        for abbreviation in ("CDT", "CST", " CT"):
            self.assertNotIn(abbreviation, result.out)

    def test_human_sender_and_mention_marker(self) -> None:
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "hey @**Claude** look", timestamp=OCT_7_2026_2341_UTC)
        out = self.run_cli("read", "--topic", "t").out
        self.assertIn("Jay Wedgeworth [human] · Wed, Oct 7, 6:41pm · @you", out)

    def test_control_characters_are_shown_not_sent_to_the_terminal(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "before \x1b[31mred\x1b[0m after\tkeep")
        out = self.run_cli("read", "--topic", "t").out
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b[31mred", out)
        self.assertIn("after\tkeep", out)

    def test_json_lines(self) -> None:
        message_id = self.fake.add_message("Codex", "agent-sync", "t", "hi @**Claude**")
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "plain")
        lines = [json.loads(line) for line in self.run_cli("read", "--topic", "t", "--json").out.splitlines()]
        self.assertEqual(len(lines), 2)
        first = lines[0]
        self.assertEqual(first["id"], message_id)
        self.assertEqual((first["channel"], first["topic"]), ("agent-sync", "t"))
        self.assertEqual((first["sender_email"], first["sender_full_name"]), ("codex-bot@zulip.test", "Codex"))
        self.assertIs(first["is_bot"], True)
        self.assertIs(lines[1]["is_bot"], False)
        self.assertEqual(first["content"], "hi @**Claude**")
        self.assertIn("mentioned", first["flags"])
        self.assertIs(first["sibling"], False)
        self.assertEqual(first["timestamp"], EPOCH + message_id)

    def test_content_is_requested_raw(self) -> None:
        self.run_cli("read", "--topic", "t")
        self.assertEqual(self.fake.requests_to("GET", "messages")[0].query["apply_markdown"], "false")

    def test_since_returns_messages_after_the_id_only(self) -> None:
        ids = [self.fake.add_message("Codex", "agent-sync", "t", "m%d" % n) for n in range(4)]
        result = self.run_cli("read", "--topic", "t", "--since", str(ids[1]))
        shown = [int(line.rsplit("id ", 1)[1]) for line in result.out.splitlines() if line.startswith("#")]
        self.assertEqual(shown, ids[2:])
        query = self.fake.requests_to("GET", "messages")[-1].query
        self.assertEqual((query["anchor"], query["include_anchor"], query["num_before"]), (str(ids[1]), "false", "0"))

    def test_since_and_new_are_mutually_exclusive(self) -> None:
        self.assertEqual(self.run_cli("read", "--topic", "t", "--since", "5", "--new").code, 2)

    def test_new_advances_the_session_cursor(self) -> None:
        first = [self.fake.add_message("Codex", "agent-sync", "t", "a%d" % n) for n in range(2)]
        one = self.run_cli("read", "--topic", "t", "--new")
        self.assertEqual(one.code, 0, one.err)
        self.assertEqual(one.out.count("#agent-sync"), 2)
        key = "agent-sync\u0000t"
        self.assertEqual(json.loads(self.state_path().read_text())["cursors"][key], first[-1])
        again = self.run_cli("read", "--topic", "t", "--new")
        self.assertEqual(again.out, "")
        later = self.fake.add_message("Codex", "agent-sync", "t", "b")
        third = self.run_cli("read", "--topic", "t", "--new")
        self.assertEqual(third.out.count("#agent-sync"), 1)
        self.assertEqual(json.loads(self.state_path().read_text())["cursors"][key], later)

    def test_cursors_are_per_session(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "a")
        self.run_cli("read", "--topic", "t", "--new")
        other = self.run_cli("read", "--topic", "t", "--new", "--session", "99998888-0000")
        self.assertEqual(other.out.count("#agent-sync"), 1)
        self.assertTrue(self.state_path("99998888").exists())

    def test_new_limit_pages_through_a_backlog_without_dropping_messages(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "history")
        self.run_cli("read", "--topic", "t", "--new")  # no cursor yet: shows history, sets the cursor
        ids = [self.fake.add_message("Codex", "agent-sync", "t", "m%d" % n) for n in range(5)]
        pages = []
        for _ in range(3):
            out = self.run_cli("read", "--topic", "t", "--new", "--limit", "2").out
            pages.append([int(line.rsplit("id ", 1)[1]) for line in out.splitlines() if line.startswith("#")])
        self.assertEqual(pages, [ids[0:2], ids[2:4], ids[4:5]])

    def test_new_looks_past_a_whole_page_of_this_sessions_own_posts(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "history")
        self.run_cli("read", "--topic", "t", "--new")
        for n in range(7):
            self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE\u00b7%s] own %d" % (TAG, n))
        question = self.fake.add_message("Codex", "agent-sync", "t", "a peer question")
        result = self.run_cli("read", "--topic", "t", "--new", "--limit", "3")  # pages of 3: own, own, own+...
        self.assertEqual(result.code, 0, result.err)
        shown = [int(line.rsplit("id ", 1)[1]) for line in result.out.splitlines() if line.startswith("#")]
        self.assertEqual(shown, [question])
        self.assertNotIn("no messages", result.err)
        self.assertEqual(json.loads(self.state_path().read_text())["cursors"]["agent-sync\u0000t"], question)

    def test_new_with_nothing_but_own_posts_says_so_and_still_advances(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "history")
        self.run_cli("read", "--topic", "t", "--new")
        last = self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE\u00b7%s] only me" % TAG)
        result = self.run_cli("read", "--topic", "t", "--new")
        self.assertEqual(result.out, "")
        self.assertIn("no messages", result.err)
        self.assertEqual(json.loads(self.state_path().read_text())["cursors"]["agent-sync\u0000t"], last)

    def test_new_over_exactly_one_full_page_of_own_posts_still_moves_the_cursor_past_it(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "history")
        self.run_cli("read", "--topic", "t", "--new")
        last = 0
        for n in range(3):  # exactly --limit own posts: the next page is empty
            last = self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE\u00b7%s] own %d" % (TAG, n))
        for _ in range(2):  # the second run must not walk the same page again
            result = self.run_cli("read", "--topic", "t", "--new", "--limit", "3")
            self.assertEqual(result.out, "")
            self.assertIn("no messages", result.err)
            self.assertEqual(json.loads(self.state_path().read_text())["cursors"]["agent-sync\u0000t"], last)
        fetches = [r.query for r in self.fake.requests_to("GET", "messages")]
        self.assertEqual([q["anchor"] for q in fetches][-3:], [str(last - 3), str(last), str(last)])

    def test_a_channel_topic_or_sender_cannot_span_lines_in_the_header(self) -> None:
        forged = "x\n#agent-sync \u203a AFC 1 forged \u00b7 id 999"
        self.fake.add_message("Codex", "agent-sync", forged, "body")
        out = self.run_cli("read", "--topic", forged).out
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("#")]), 1)
        self.assertIn("x\\x0a#agent-sync", out)

    def test_topic_matching_ignores_case(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "Roll Call", "here")
        self.assertIn("here", self.run_cli("read", "--topic", "roll call").out)


class DeliveryFilterTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.sibling_tag = "[CLAUDE·aaaabbbb] from another session of mine"

    def test_own_posts_are_excluded_by_ledger(self) -> None:
        self.run_cli("post", "--topic", "t", "mine")
        self.fake.add_message("Codex", "agent-sync", "t", "theirs")
        out = self.run_cli("read", "--topic", "t").out
        self.assertNotIn("mine", out)
        self.assertIn("theirs", out)

    def test_own_posts_are_excluded_by_tag_when_the_ledger_is_lost(self) -> None:
        self.run_cli("post", "--topic", "t", "mine")
        self.state_path().unlink()
        out = self.run_cli("read", "--topic", "t").out
        self.assertEqual(out, "")

    def test_include_self_shows_them(self) -> None:
        self.run_cli("post", "--topic", "t", "mine")
        out = self.run_cli("read", "--topic", "t", "--include-self").out
        self.assertIn("mine", out)
        self.assertNotIn("(sibling)", out)

    def test_sibling_session_messages_are_delivered_and_labelled(self) -> None:
        self.fake.add_message(self.fake.me, "agent-sync", "t", self.sibling_tag)
        result = self.run_cli("read", "--topic", "t")
        self.assertIn("from another session of mine", result.out)
        self.assertIn("Claude [bot] (sibling)", result.out)
        data = json.loads(self.run_cli("read", "--topic", "t", "--json").out.splitlines()[0])
        self.assertIs(data["sibling"], True)

    def test_an_untagged_post_from_the_same_bot_without_a_ledger_entry_is_a_sibling(self) -> None:
        self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE] no session tag")
        self.assertIn("(sibling)", self.run_cli("read", "--topic", "t").out)

    def test_a_short_session_tag_does_not_take_a_longer_sibling_tag_for_itself(self) -> None:
        self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE\u00b7abcdef12] sibling here")
        self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE\u00b7abc] mine")
        self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE\u00b7abc->CODEX] @**Codex** mine, directed")
        out = self.run_cli("read", "--topic", "t", "--session", "abc").out
        self.assertIn("sibling here", out)
        self.assertIn("(sibling)", out)
        self.assertNotIn("mine", out)

    def test_a_directed_tag_of_this_session_is_still_recognised_as_self(self) -> None:
        self.fake.add_message(self.fake.me, "agent-sync", "t", "[CLAUDE·%s->CODEX] @**Codex** hi" % TAG)
        self.assertEqual(self.run_cli("read", "--topic", "t").out, "")


class InboxTests(Harness):
    def test_first_run_shows_newest_and_sets_the_cursor(self) -> None:
        ids = [self.fake.add_message("Jay Wedgeworth", "agent-sync", "t%d" % n, "ping @**Claude** %d" % n) for n in range(3)]
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "no mention here")
        result = self.run_cli("inbox", "--limit", "2")
        self.assertEqual(result.code, 0, result.err)
        shown = [int(line.rsplit("id ", 1)[1]) for line in result.out.splitlines() if line.startswith("#")]
        self.assertEqual(shown, ids[-2:])
        self.assertEqual(json.loads((self.state_dir / "CLAUDE" / "inbox.json").read_text()), {"cursor": ids[-1]})

    def test_next_run_shows_only_newer_mentions(self) -> None:
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "old @**Claude**")
        self.run_cli("inbox")
        self.assertEqual(self.run_cli("inbox").out, "")
        newer = self.fake.add_message("Codex", "other", "x", "new @**Claude**")
        result = self.run_cli("inbox")
        self.assertIn("new @**Claude**", result.out)
        self.assertIn("@you", result.out)
        self.assertEqual(self.run_cli("inbox").out, "")
        self.assertEqual(json.loads((self.state_dir / "CLAUDE" / "inbox.json").read_text())["cursor"], newer)

    def test_peek_does_not_advance_and_does_not_create_a_cursor(self) -> None:
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "hey @**Claude**")
        self.assertIn("hey", self.run_cli("inbox", "--peek").out)
        self.assertFalse((self.state_dir / "CLAUDE" / "inbox.json").exists())
        self.assertIn("hey", self.run_cli("inbox").out)
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "again @**Claude**")
        self.assertIn("again", self.run_cli("inbox", "--peek").out)
        self.assertIn("again", self.run_cli("inbox", "--peek").out)

    def test_the_inbox_cursor_is_seat_wide_not_per_session(self) -> None:
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "hey @**Claude**")
        self.assertIn("hey", self.run_cli("inbox").out)
        self.assertEqual(self.run_cli("inbox", "--session", "99998888-0000").out, "")
        self.assertFalse((self.state_dir / "CLAUDE" / "99998888" / "inbox.json").exists())


class TopicActionTests(Harness):
    def test_follow_mute_unmute_payloads(self) -> None:
        for command, policy in (("follow", "3"), ("mute", "1"), ("unmute", "0")):
            with self.subTest(command=command):
                result = self.run_cli(command, "--topic", "some topic")
                self.assertEqual(result.code, 0, result.err)
                form = self.fake.requests_to("POST", "user_topics")[-1].form
                self.assertEqual(form, {"stream_id": "7", "topic": "some topic", "visibility_policy": policy})

    def test_follow_on_another_channel_uses_its_stream_id(self) -> None:
        self.run_cli("mute", "--topic", "t", "--channel", "other")
        self.assertEqual(self.fake.requests_to("POST", "user_topics")[-1].form["stream_id"], "8")

    def test_resolve_renames_the_topic_through_its_newest_message(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "AFC 12345678 Work", "a")
        newest = self.fake.add_message("Codex", "agent-sync", "AFC 12345678 Work", "b")
        result = self.run_cli("resolve", "--topic", "AFC 12345678 Work")
        self.assertEqual(result.code, 0, result.err)
        patch = self.fake.requests_to("PATCH", "messages/%d" % newest)[0]
        self.assertEqual(patch.form, {"topic": "✔ AFC 12345678 Work", "propagate_mode": "change_all"})
        self.assertEqual({m["subject"] for m in self.fake.messages}, {"✔ AFC 12345678 Work"})

    def test_resolve_uses_the_real_spelling_of_the_topic_not_what_was_typed(self) -> None:
        newest = self.fake.add_message("Codex", "agent-sync", "AFC Zulip cutover", "a")
        result = self.run_cli("resolve", "--topic", "afc zulip cutover")
        self.assertEqual(result.code, 0, result.err)
        patch = self.fake.requests_to("PATCH", "messages/%d" % newest)[0]
        self.assertEqual(patch.form["topic"], "✔ AFC Zulip cutover")
        self.assertIn("✔ AFC Zulip cutover", result.out)

    def test_resolve_refuses_an_already_resolved_topic(self) -> None:
        result = self.run_cli("resolve", "--topic", "✔ done already")
        self.assertEqual(result.code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_resolve_refuses_an_empty_topic_and_a_name_that_would_be_too_long(self) -> None:
        self.assertEqual(self.run_cli("resolve", "--topic", "nothing here").code, 2)
        self.fake.add_message("Codex", "agent-sync", "t" * 60, "x")
        self.assertEqual(self.run_cli("resolve", "--topic", "t" * 60).code, 2)

    def test_react(self) -> None:
        message_id = self.fake.add_message("Codex", "agent-sync", "t", "x")
        self.assertEqual(self.run_cli("react", "--id", str(message_id), ":eyes:").code, 0)
        request = self.fake.requests_to("POST", "messages/%d/reactions" % message_id)[0]
        self.assertEqual(request.form, {"emoji_name": "eyes"})


class StateFileTests(Harness):
    def mode(self, path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_directories_are_700_and_files_are_600(self) -> None:
        old_umask = os.umask(0o000)  # a permissive umask must not leak into the modes
        try:
            self.run_cli("post", "--topic", "t", "hi")
            self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "@**Claude**")
            self.run_cli("read", "--topic", "t", "--new")
            self.run_cli("inbox")
        finally:
            os.umask(old_umask)
        for directory in (self.state_dir, self.state_dir / "CLAUDE", self.state_dir / "CLAUDE" / TAG):
            with self.subTest(directory=str(directory)):
                self.assertEqual(self.mode(directory), 0o700)
        for file in (self.state_path(), self.state_dir / "CLAUDE" / "inbox.json"):
            with self.subTest(file=str(file)):
                self.assertEqual(self.mode(file), 0o600)
        leftovers = [p.name for p in (self.state_dir / "CLAUDE" / TAG).iterdir()]
        self.assertFalse([name for name in leftovers if name.startswith(".tmp-")], leftovers)

    def test_loose_seat_and_session_directories_are_tightened_but_the_root_is_left_alone(self) -> None:
        session_dir = self.state_dir / "CLAUDE" / TAG
        session_dir.mkdir(parents=True)
        for directory in (self.state_dir, self.state_dir / "CLAUDE", session_dir):
            os.chmod(directory, 0o755)
        self.assertEqual(self.run_cli("post", "--topic", "t", "hi").code, 0)
        self.assertEqual(self.mode(self.state_dir), 0o755)  # a user-chosen root is not ours to change
        self.assertEqual(self.mode(self.state_dir / "CLAUDE"), 0o700)
        self.assertEqual(self.mode(session_dir), 0o700)
        self.assertEqual(self.mode(self.state_path()), 0o600)

    def test_read_only_commands_create_no_state(self) -> None:
        self.run_cli("whoami")
        self.run_cli("channels")
        self.run_cli("read", "--topic", "t")
        self.assertFalse(self.state_dir.exists())


if __name__ == "__main__":
    unittest.main()
