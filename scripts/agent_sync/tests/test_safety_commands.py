"""F3 on the command line:  post and reply --to-owner, --body-file on post, reply and dm, verify-owner-msg,
and the seat and seat_app fields of read --json."""
from __future__ import annotations

import argparse
import io
import json
import os
import stat
import unittest
from unittest import mock

from agent_sync import bodyfile, cli
from agent_sync.tests.fake_zulip import FAKE_OWNER_ID
from agent_sync.tests.harness import TAG, Harness

OWNER_ID = FAKE_OWNER_ID  # Jay Wedgeworth, role 100, in the fake realm
AT = "@"


class OwnerHarness(Harness):
    def last_post(self):
        return self.fake.requests_to("POST", "messages")[-1]

    def posts(self) -> int:
        return len(self.fake.requests_to("POST", "messages"))


# --------------------------------------------------------------------------------------------
# --to-owner
# --------------------------------------------------------------------------------------------

class ToOwnerTests(OwnerHarness):
    def test_post_to_owner_mentions_the_pinned_id_and_labels_the_tag_owner(self) -> None:
        self.pin_owner()
        result = self.run_cli("post", "--topic", "OWNER AFC-1 question", "--to-owner", "--", "Which option?")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["content"],
                         "[CLAUDE·%s→OWNER] %s**Jay Wedgeworth|%d** Which option?" % (TAG, AT, OWNER_ID))

    def test_the_mention_comes_from_the_live_name_for_that_id(self) -> None:
        self.pin_owner()
        self.fake.user_by_id(OWNER_ID)["full_name"] = "Jay W."
        self.run_cli("post", "--topic", "t", "--to-owner", "hi")
        self.assertIn("%s**Jay W.|%d**" % (AT, OWNER_ID), self.last_post().form["content"])

    def test_another_user_with_the_owners_name_cannot_take_the_mention(self) -> None:
        self.pin_owner()
        twin = self.fake.add_user("twin@zulip.test", "Jay Wedgeworth", is_bot=False)
        self.run_cli("post", "--topic", "t", "--to-owner", "hi")
        content = self.last_post().form["content"]
        self.assertIn("|%d**" % OWNER_ID, content)
        self.assertNotIn("|%d**" % twin["user_id"], content)

    def test_an_unpinned_owner_exits_2_and_sends_nothing(self) -> None:
        for toml in (None, "[daemon]\nowner_user_id = 0\n"):
            with self.subTest(toml=toml):
                self.state_dir.mkdir(parents=True, exist_ok=True)
                path = self.state_dir / "listener.toml"
                if toml is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(toml)
                result = self.run_cli("post", "--topic", "t", "--to-owner", "hello")
                self.assertEqual(result.code, 2)
                self.assertIn("owner_user_id is not pinned", result.err)
        self.assertEqual(self.posts(), 0)
        self.assertEqual(self.fake.requests, [])  # not even a user list was fetched

    def test_the_no_owner_message_names_the_config_path_then_the_program(self) -> None:
        #  The two %s were once swapped (Sentry 18337810), so the suggestion read as a path to run as a
        #  command and named the program as a file.  Assert the slots, not just the prefix both orders share.
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "listener.toml").write_text("[daemon]\nowner_user_id = 0\n")
        result = self.run_cli("post", "--topic", "t", "--to-owner", "hello")
        self.assertEqual(result.code, 2)
        self.assertIn("not pinned in %s (run simple-chat daemon init)"
                      % (self.state_dir / "listener.toml"), result.err)

    def test_a_pinned_id_that_is_not_an_active_human_is_refused(self) -> None:
        cases = ((9999, "not a user in this realm"), (11, "is a bot"))
        for owner_id, fragment in cases:
            with self.subTest(owner_id=owner_id):
                self.pin_owner(owner_id)
                result = self.run_cli("post", "--topic", "t", "--to-owner", "hello")
                self.assertEqual(result.code, 2)
                self.assertIn(fragment, result.err)
        self.fake.user_by_id(OWNER_ID)["is_active"] = False
        self.pin_owner()
        gone = self.run_cli("post", "--topic", "t", "--to-owner", "hello")
        self.assertEqual(gone.code, 2)
        self.assertIn("deactivated", gone.err)
        self.assertEqual(self.posts(), 0)

    def test_an_owner_id_on_the_command_line_is_not_accepted(self) -> None:
        self.pin_owner()
        result = self.run_cli("post", "--topic", "t", "--to-owner", str(OWNER_ID), "hello")
        # "12" is taken as part of the message text, never as who the owner is
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("|%d** %d hello" % (OWNER_ID, OWNER_ID), self.last_post().form["content"])

    def test_to_owner_with_peers_orders_owner_first_and_mentions_nobody_twice(self) -> None:
        self.pin_owner()
        result = self.run_cli("post", "--topic", "t", "--to-owner", "--to", "Codex", "--to", "Jay Wedgeworth", "go")
        self.assertEqual(result.code, 0, result.err)
        content = self.last_post().form["content"]
        self.assertEqual(content, "[CLAUDE·%s→OWNER,CODEX] %s**Jay Wedgeworth|%d** %s**Codex** go" % (TAG, AT, OWNER_ID, AT))
        self.assertEqual(content.count("Jay Wedgeworth"), 1)

    def test_to_owner_works_with_stdin_text_and_the_fleet_wake_check_still_runs_first(self) -> None:
        self.pin_owner()
        ok = self.run_cli("post", "--topic", "t", "--to-owner", "-", stdin="from stdin\n")
        self.assertEqual(ok.code, 0, ok.err)
        self.assertTrue(self.last_post().form["content"].endswith("from stdin"))
        before = self.posts()
        bad = self.run_cli("post", "--topic", "elsewhere", "--to-owner", "--fleet", "x")
        self.assertEqual((bad.code, self.posts()), (2, before))

    def test_reply_to_owner(self) -> None:
        self.pin_owner()
        original = self.fake.add_message("Codex", "agent-sync", "OWNER AFC-2 thread", "question")
        result = self.run_cli("reply", "--id", str(original), "--to-owner", "Done.")
        self.assertEqual(result.code, 0, result.err)
        form = self.last_post().form
        self.assertEqual((form["to"], form["topic"]), ("agent-sync", "OWNER AFC-2 thread"))
        self.assertEqual(form["content"], "[CLAUDE·%s→OWNER] %s**Jay Wedgeworth|%d** Done." % (TAG, AT, OWNER_ID))

    def test_reply_to_owner_unpinned_exits_2_before_any_request(self) -> None:
        original = self.fake.add_message("Codex", "agent-sync", "t", "question")
        result = self.run_cli("reply", "--id", str(original), "--to-owner", "Done.")
        self.assertEqual(result.code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_without_the_flag_nothing_changes(self) -> None:
        self.pin_owner()
        self.run_cli("post", "--topic", "t", "plain")
        self.assertEqual(self.last_post().form["content"], "[CLAUDE·%s] plain" % TAG)

    def test_the_mention_wakes_the_owner_in_the_fake_realm(self) -> None:
        self.pin_owner()
        self.run_cli("post", "--topic", "t", "--to-owner", "hi")
        message = self.fake.messages[-1]
        self.assertIn("mentioned", self.fake._flags(message, OWNER_ID))


# --------------------------------------------------------------------------------------------
# --body-file
# --------------------------------------------------------------------------------------------

class BodyFileHarness(OwnerHarness):
    def body(self, text: str | bytes = "A body\nof two lines.\n", mode: int = 0o600, name: str = "body.txt") -> str:
        path = self.tmp / name
        data = text if isinstance(text, bytes) else text.encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(path, mode)
        return str(path)


class BodyFileTests(BodyFileHarness):
    def test_post_reads_the_body_from_the_file(self) -> None:
        result = self.run_cli("post", "--topic", "t", "--body-file", self.body("From a file.  Two spaces.\nSecond line.\n"))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_post().form["content"],
                         "[CLAUDE·%s] From a file.  Two spaces.\nSecond line." % TAG)

    def test_reply_and_dm_read_the_body_from_the_file(self) -> None:
        self.pin_owner()
        original = self.fake.add_message("Codex", "agent-sync", "thread", "q")
        reply = self.run_cli("reply", "--id", str(original), "--body-file", self.body("reply body"))
        self.assertEqual(reply.code, 0, reply.err)
        self.assertTrue(self.last_post().form["content"].endswith("reply body"))
        dm = self.run_cli("dm", "--owner", "--body-file", self.body("dm body", name="dm.txt"))
        self.assertEqual(dm.code, 0, dm.err)
        self.assertEqual((self.last_post().form["type"], json.loads(self.last_post().form["to"])), ("direct", [OWNER_ID]))
        self.assertTrue(self.last_post().form["content"].endswith("dm body"))

    def test_a_0400_file_is_accepted(self) -> None:
        self.assertEqual(self.run_cli("post", "--topic", "t", "--body-file", self.body(mode=0o400)).code, 0)

    def test_files_that_others_can_read_or_write_are_refused(self) -> None:
        for mode in (0o644, 0o640, 0o604, 0o660, 0o666, 0o700, 0o777, 0o500, 0o444, 0o640):
            with self.subTest(mode=oct(mode)):
                result = self.run_cli("post", "--topic", "t", "--body-file", self.body(mode=mode, name="m%o.txt" % mode))
                self.assertEqual(result.code, 2)
                self.assertIn("not 0600 or 0400", result.err)
        for mode in (0o200, 0o000):  # the owner cannot even read these:  also refused
            with self.subTest(mode=oct(mode)):
                result = self.run_cli("post", "--topic", "t", "--body-file", self.body(mode=mode, name="m%o.txt" % mode))
                self.assertEqual(result.code, 2)
                self.assertIn("cannot open it", result.err)
        self.assertEqual(self.posts(), 0)

    def test_a_body_file_conflicts_with_a_text_argument(self) -> None:
        path = self.body()
        for argv in (("post", "--topic", "t", "--body-file", path, "also text"),
                     ("post", "--topic", "t", "--body-file", path, "-"),
                     ("dm", "--owner", "--body-file", path, "text"),
                     ("reply", "--id", "100", "--body-file", path, "text")):
            with self.subTest(argv=argv[0]):
                self.pin_owner()
                result = self.run_cli(*argv, stdin="stdin text")
                self.assertEqual(result.code, 2)
                self.assertIn("exclusive", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_neither_text_nor_file_is_a_usage_error_with_no_request(self) -> None:
        self.pin_owner()
        for argv in (("post", "--topic", "t"), ("post", "--topic", "t", "--"), ("reply", "--id", "100"), ("dm",), ("dm", "--owner")):
            with self.subTest(argv=argv):
                result = self.run_cli(*argv)
                self.assertEqual(result.code, 2)
                self.assertIn("no message text", result.err)
        self.assertEqual(self.fake.requests, [])

    def test_a_usage_error_needs_no_credential(self) -> None:
        env = self.env(AGENT_SYNC_SECRETS_DIR=str(self.tmp / "nowhere"))
        result = self.run_cli("post", "--topic", "t", env=env)
        self.assertEqual(result.code, 2)

    def test_refusals_for_the_file_itself(self) -> None:
        good = self.body()
        cases = {
            "missing": (str(self.tmp / "no-such-file"), "cannot open it"),
            "a directory": (str(self.tmp), "not a regular file"),
            "empty path": ("", "needs a path"),
        }
        for label, (path, fragment) in cases.items():
            with self.subTest(case=label):
                result = self.run_cli("post", "--topic", "t", "--body-file", path)
                self.assertEqual(result.code, 2)
                self.assertIn(fragment, result.err)
        link = self.tmp / "link.txt"
        os.symlink(good, link)
        linked = self.run_cli("post", "--topic", "t", "--body-file", str(link))
        self.assertEqual(linked.code, 2)
        self.assertIn("symbolic link", linked.err)
        self.assertEqual(self.posts(), 0)

    def test_a_fifo_is_refused_without_hanging(self) -> None:
        fifo = self.tmp / "pipe"
        os.mkfifo(fifo, 0o600)
        result = self.run_cli("post", "--topic", "t", "--body-file", str(fifo))
        self.assertEqual(result.code, 2)
        self.assertIn("not a regular file", result.err)

    def test_a_file_someone_else_owns_is_refused(self) -> None:
        path = self.body()
        with mock.patch.object(bodyfile.os, "geteuid", return_value=os.geteuid() + 1):
            result = self.run_cli("post", "--topic", "t", "--body-file", path)
        self.assertEqual(result.code, 2)
        self.assertIn("not owned by you", result.err)

    def test_size_cap_is_64_kb(self) -> None:
        at_cap = self.run_cli("post", "--topic", "t", "--body-file", self.body("x" * bodyfile.MAX_BYTES, name="cap.txt"))
        # exactly 64 KB is readable but the message itself is over Zulip's own 10,000-character limit
        self.assertEqual(at_cap.code, 2)
        self.assertIn("10000", at_cap.err)
        over = self.run_cli("post", "--topic", "t", "--body-file", self.body("x" * (bodyfile.MAX_BYTES + 1), name="over.txt"))
        self.assertEqual(over.code, 2)
        self.assertIn("larger than 64 KB", over.err)
        with self.assertRaises(Exception) as caught:  # the reader on its own: the cap itself is exact
            bodyfile.read_body_file(self.body("x" * (bodyfile.MAX_BYTES + 1), name="over2.txt"))
        self.assertIn("64 KB", str(caught.exception))
        self.assertEqual(len(bodyfile.read_body_file(self.body("y" * bodyfile.MAX_BYTES, name="exact.txt"))), bodyfile.MAX_BYTES)
        self.assertEqual(self.posts(), 0)

    def test_text_that_is_not_utf8_or_holds_a_nul_is_refused(self) -> None:
        bad = self.run_cli("post", "--topic", "t", "--body-file", self.body(b"caf\xe9 not utf-8", name="latin.txt"))
        self.assertEqual(bad.code, 2)
        self.assertIn("not valid UTF-8", bad.err)
        nul = self.run_cli("post", "--topic", "t", "--body-file", self.body(b"a\x00b", name="nul.txt"))
        self.assertEqual(nul.code, 2)
        self.assertIn("NUL", nul.err)

    def test_a_byte_order_mark_is_dropped_and_an_empty_file_is_refused(self) -> None:
        self.run_cli("post", "--topic", "t", "--body-file", self.body(b"\xef\xbb\xbfwith bom", name="bom.txt"))
        self.assertEqual(self.last_post().form["content"], "[CLAUDE·%s] with bom" % TAG)
        empty = self.run_cli("post", "--topic", "t", "--body-file", self.body("  \n\n", name="empty.txt"))
        self.assertEqual(empty.code, 2)
        self.assertIn("empty", empty.err)

    def test_the_file_goes_through_the_secret_scanner_and_its_text_is_never_echoed(self) -> None:
        # Built at run time on purpose:  a whole AKIA-shaped literal would trip gitleaks.  It is a fake, and the
        # test asserts the scanner refuses it and never echoes it.
        FAKE_LEAKED_AWS_KEY = "AK" + "IA" + "ABCDEFGHIJ012345"
        result = self.run_cli("post", "--topic", "t", "--body-file", self.body("the key is %s" % FAKE_LEAKED_AWS_KEY, name="secret.txt"))
        self.assertEqual(result.code, 2)
        self.assertIn("refusing to post", result.err)
        self.assertNotIn(FAKE_LEAKED_AWS_KEY, result.err + result.out)
        self.assertEqual(self.posts(), 0)
        bad_mode = self.run_cli("post", "--topic", "t", "--body-file", self.body("secret words here", mode=0o644, name="s2.txt"))
        self.assertNotIn("secret words here", bad_mode.err + bad_mode.out)

    def test_a_path_with_a_newline_cannot_forge_an_error_line(self) -> None:
        result = self.run_cli("post", "--topic", "t", "--body-file", str(self.tmp / "a\nagent-sync: forged"))
        self.assertEqual(result.code, 2)
        self.assertEqual(len([line for line in result.err.splitlines() if line.startswith("simple-chat:")]), 1)

    def test_the_hand_built_namespaces_of_the_mcp_layer_still_work(self) -> None:
        # mcp/tools.py builds its own Namespace:  no body_file, no to_owner, text as a one-item list
        runtime = cli.Runtime(env=self.env(), stdin=io.StringIO(""), stdout=io.StringIO(), stderr=io.StringIO(),
                              home=self.home, timeout=5.0)
        agent = cli.Agent(runtime, argparse.Namespace(json=True))
        post = argparse.Namespace(channel="agent-sync", topic="t", to=[], fleet=False, no_tag=False, text=["via mcp"], json=True)
        self.assertEqual(cli.cmd_post(agent, post), 0)
        self.assertEqual(self.last_post().form["content"], "[CLAUDE\u00b7%s] via mcp" % TAG)
        first = self.fake.messages[-1]["id"]
        reply = argparse.Namespace(id=first, to=[], no_tag=False, text=["via mcp reply"], json=True)
        self.assertEqual(cli.cmd_reply(agent, reply), 0)
        self.assertTrue(self.last_post().form["content"].endswith("via mcp reply"))


# --------------------------------------------------------------------------------------------
# verify-owner-msg
# --------------------------------------------------------------------------------------------

class VerifyOwnerMsgTests(OwnerHarness):
    TOPIC = "OWNER AFC-9 Which option"

    def setUp(self) -> None:
        super().setUp()
        self.pin_owner()

    def owner_says(self, text: str = "Option 2", *, topic: str | None = None, client: str | None = None,
                   channel: str = "agent-sync") -> int:
        return self.fake.add_message("Jay Wedgeworth", channel, topic or self.TOPIC, text, client=client)

    def verify(self, message_id: int, *extra: str):
        result = self.run_cli("verify-owner-msg", str(message_id), "--topic", self.TOPIC, "--json", *extra)
        return result, json.loads(result.out) if result.out.strip() else None

    def test_an_owner_message_from_the_website_in_the_right_topic_verifies(self) -> None:
        notice = self.fake.add_message("Claude", "agent-sync", self.TOPIC, "question")
        message = self.owner_says(client="website")
        result, verdict = self.verify(message, "--after", str(notice))
        self.assertEqual(result.code, 0, result.err)
        self.assertIs(verdict["ok"], True)
        self.assertEqual(verdict["reasons"], [])
        self.assertEqual(verdict["checks"], {"found": True, "sender_is_owner": True, "client_is_human": True,
                                             "place": True, "newer": True})
        self.assertEqual((verdict["sender_id"], verdict["client"], verdict["owner_user_id"]), (OWNER_ID, "website", OWNER_ID))
        self.assertEqual((verdict["place"], verdict["channel"], verdict["topic"]), ("stream", "agent-sync", self.TOPIC))
        self.assertIs(verdict["edited"], False)

    def test_every_default_human_client_is_accepted(self) -> None:
        for client in ("website", "ZulipMobile", "ZulipFlutter", "ZulipElectron", "ZulipDesktop"):
            with self.subTest(client=client):
                self.assertEqual(self.verify(self.owner_says(client=client))[0].code, 0)

    def test_an_api_client_holding_the_owners_account_is_rejected(self) -> None:
        for client in ("ZulipPython", "ZulipCLI", "zulip-api", "", "curl"):
            with self.subTest(client=client):
                message = self.owner_says(client=client or "unknown")
                result, verdict = self.verify(message)
                self.assertEqual(result.code, 7)
                self.assertIs(verdict["ok"], False)
                self.assertIs(verdict["checks"]["client_is_human"], False)
                self.assertIs(verdict["checks"]["sender_is_owner"], True)

    def test_a_bot_is_rejected_whatever_it_claims(self) -> None:
        for sender in ("Codex", "Claude", "Cursor"):
            with self.subTest(sender=sender):
                message = self.fake.add_message(sender, "agent-sync", self.TOPIC, "Jay says yes", client="website")
                result, verdict = self.verify(message)
                self.assertEqual(result.code, 7)
                self.assertIs(verdict["checks"]["sender_is_owner"], False)
                self.assertIs(verdict["checks"]["client_is_human"], True)  # a bot with a human client name still fails

    def test_another_human_is_rejected(self) -> None:
        other = self.fake.add_user("guest@zulip.test", "Guest Person", is_bot=False)
        message = self.fake.add_message(other, "agent-sync", self.TOPIC, "yes", client="website")
        result, verdict = self.verify(message)
        self.assertEqual(result.code, 7)
        self.assertIs(verdict["checks"]["sender_is_owner"], False)

    def test_the_wrong_topic_or_channel_is_rejected(self) -> None:
        wrong_topic = self.owner_says(topic="OWNER AFC-10 something else")
        wrong_channel = self.owner_says(channel="other")
        for message in (wrong_topic, wrong_channel):
            with self.subTest(message=message):
                result, verdict = self.verify(message)
                self.assertEqual(result.code, 7)
                self.assertIs(verdict["checks"]["place"], False)
                self.assertIs(verdict["checks"]["sender_is_owner"], True)
        ok = self.run_cli("verify-owner-msg", str(wrong_channel), "--topic", self.TOPIC, "--channel", "other", "--json")
        self.assertEqual(ok.code, 0, ok.err)

    def test_topic_and_channel_match_without_regard_to_case_and_a_resolved_topic_counts(self) -> None:
        shouted = self.owner_says(topic=self.TOPIC.upper())
        self.assertEqual(self.run_cli("verify-owner-msg", str(shouted), "--topic", self.TOPIC.lower(), "--channel", "AGENT-sync").code, 0)
        resolved = self.owner_says(topic="✔ " + self.TOPIC)
        self.assertEqual(self.verify(resolved)[0].code, 0)
        similar = self.owner_says(topic=self.TOPIC + " and more")
        self.assertEqual(self.verify(similar)[0].code, 7)

    def test_after_must_be_strictly_older(self) -> None:
        message = self.owner_says()
        self.assertEqual(self.verify(message, "--after", str(message - 1))[0].code, 0)
        for after in (message, message + 1, message + 500):
            with self.subTest(after=after):
                result, verdict = self.verify(message, "--after", str(after))
                self.assertEqual(result.code, 7)
                self.assertIs(verdict["checks"]["newer"], False)
        self.assertEqual(self.verify(message)[0].code, 0)  # no --after:  no age check

    def test_a_message_that_does_not_exist_is_a_verdict_not_an_api_error(self) -> None:
        result, verdict = self.verify(99999)
        self.assertEqual(result.code, 7)
        self.assertIs(verdict["ok"], False)
        self.assertIs(verdict["checks"]["found"], False)
        self.assertIn("not found", verdict["reasons"][0])

    def test_a_server_error_is_not_a_verdict(self) -> None:
        message = self.owner_says()
        self.fake.inject("GET", "messages/%d" % message, status=500, body={"result": "error", "msg": "boom"})
        result = self.run_cli("verify-owner-msg", str(message), "--topic", self.TOPIC, "--json")
        self.assertEqual(result.code, 5)
        self.assertEqual(result.out, "")

    def test_a_direct_message_between_exactly_the_owner_and_the_peer_verifies(self) -> None:
        me = self.fake.me
        dm = self.fake.add_direct_message("Jay Wedgeworth", "approved", recipients=[me], client="ZulipMobile")
        result = self.run_cli("verify-owner-msg", str(dm), "--dm-with", "CLAUDE", "--json")
        self.assertEqual(result.code, 0, result.err)
        verdict = json.loads(result.out)
        self.assertEqual((verdict["ok"], verdict["place"]), (True, "dm"))

    def test_a_group_dm_or_the_wrong_peers_never_verify(self) -> None:
        codex = self.fake.user_named("Codex")
        group = self.fake.add_direct_message("Jay Wedgeworth", "go", recipients=[self.fake.me, codex], client="website")
        result = self.run_cli("verify-owner-msg", str(group), "--dm-with", "CLAUDE", "--json")
        self.assertEqual(result.code, 7)
        self.assertIs(json.loads(result.out)["checks"]["place"], False)
        wrong_peer = self.fake.add_direct_message("Jay Wedgeworth", "go", recipients=[self.fake.me], client="website")
        self.assertEqual(self.run_cli("verify-owner-msg", str(wrong_peer), "--dm-with", "Codex", "--json").code, 7)

    def test_a_dm_from_a_bot_or_with_an_api_client_is_rejected(self) -> None:
        from_bot = self.fake.add_direct_message("Codex", "jay approved", recipients=[self.fake.me], client="website")
        self.assertEqual(self.run_cli("verify-owner-msg", str(from_bot), "--dm-with", "Codex", "--json").code, 7)
        via_api = self.fake.add_direct_message("Jay Wedgeworth", "approved", recipients=[self.fake.me], client="ZulipPython")
        self.assertEqual(self.run_cli("verify-owner-msg", str(via_api), "--dm-with", "CLAUDE", "--json").code, 7)

    def test_a_dm_cannot_satisfy_topic_and_a_channel_message_cannot_satisfy_dm_with(self) -> None:
        dm = self.fake.add_direct_message("Jay Wedgeworth", "x", recipients=[self.fake.me], client="website")
        self.assertEqual(self.verify(dm)[0].code, 7)
        stream = self.owner_says()
        self.assertEqual(self.run_cli("verify-owner-msg", str(stream), "--dm-with", "CLAUDE", "--json").code, 7)

    def test_owner_clients_come_from_the_installed_config_not_a_fixed_list(self) -> None:
        self.pin_owner(extra='owner_clients = ["website"]\n')
        self.assertEqual(self.verify(self.owner_says(client="website"))[0].code, 0)
        self.assertEqual(self.verify(self.owner_says(client="ZulipMobile"))[0].code, 7)

    def test_the_owner_comes_from_the_config_not_from_the_realm(self) -> None:
        other = self.fake.add_user("second@zulip.test", "Second Human", is_bot=False)
        self.pin_owner(other["user_id"])
        self.assertEqual(self.verify(self.owner_says())[0].code, 7)  # Jay is not the pinned owner now
        message = self.fake.add_message(other, "agent-sync", self.TOPIC, "yes", client="website")
        self.assertEqual(self.verify(message)[0].code, 0)

    def test_an_unpinned_owner_is_a_usage_error_and_nothing_verifies(self) -> None:
        message = self.owner_says()
        for toml in (None, "[daemon]\nowner_user_id = 0\n"):
            with self.subTest(toml=toml):
                path = self.state_dir / "listener.toml"
                if toml is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(toml)
                result = self.run_cli("verify-owner-msg", str(message), "--topic", self.TOPIC, "--json")
                self.assertEqual(result.code, 2)
                self.assertIn("not pinned", result.err)
                self.assertEqual(result.out, "")

    def test_a_place_is_required_and_the_two_are_exclusive(self) -> None:
        self.assertEqual(self.run_cli("verify-owner-msg", "100").code, 2)
        self.assertEqual(self.run_cli("verify-owner-msg", "100", "--topic", "t", "--dm-with", "Codex").code, 2)
        self.assertEqual(self.run_cli("verify-owner-msg", "abc", "--topic", "t").code, 2)
        self.assertEqual(self.run_cli("verify-owner-msg", "100", "--topic", "t", "--after", "-3").code, 2)
        self.assertEqual(self.run_cli("verify-owner-msg", "100", "--dm-with", "Jay Wedgeworth").code, 2)  # the owner is not the peer
        self.assertEqual(self.fake.requests_to("GET", "messages/100"), [])

    def test_an_edited_message_is_reported_as_edited(self) -> None:
        message = self.owner_says()
        next(m for m in self.fake.messages if m["id"] == message)["last_edit_timestamp"] = 1_790_000_999
        result, verdict = self.verify(message)
        self.assertEqual(result.code, 0)
        self.assertIs(verdict["edited"], True)

    def test_the_verdict_never_contains_the_message_text(self) -> None:
        marker = "UNIQUE-OWNER-WORDS-12345"
        message = self.owner_says(marker, client="ZulipPython")
        for flags in (("--json",), ()):
            result = self.run_cli("verify-owner-msg", str(message), "--topic", self.TOPIC, *flags)
            self.assertNotIn(marker, result.out + result.err)

    def test_the_human_form_says_which_checks_failed(self) -> None:
        good = self.run_cli("verify-owner-msg", str(self.owner_says()), "--topic", self.TOPIC)
        self.assertEqual(good.code, 0)
        self.assertIn("verified", good.out)
        bad = self.run_cli("verify-owner-msg", str(self.owner_says(client="ZulipPython")), "--topic", self.TOPIC)
        self.assertEqual(bad.code, 7)
        self.assertIn("NOT verified", bad.out)
        self.assertIn("client", bad.out)

    def test_the_command_only_reads(self) -> None:
        self.run_cli("verify-owner-msg", str(self.owner_says()), "--topic", self.TOPIC)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])
        self.assertEqual(self.fake.requests_to("PATCH", "messages/100"), [])


# --------------------------------------------------------------------------------------------
# read --json:  seat and seat_app
# --------------------------------------------------------------------------------------------

class SeatFieldsTests(Harness):
    def lines(self, *argv: str) -> list[dict]:
        result = self.run_cli(*argv)
        self.assertEqual(result.code, 0, result.err)
        return [json.loads(line) for line in result.out.splitlines()]

    def test_a_fleet_bot_carries_its_seat_and_app(self) -> None:
        self.fake.add_user("bf-builder-bot@zulip.test", "Builder", is_bot=True)
        self.fake.add_user("compiler-grok-bot@zulip.test", "Compiler", is_bot=True)
        self.fake.add_user("muse-assist-bot@zulip.test", "Muse Assistant", is_bot=True)
        for sender, seat, app in (("Codex", "CODEX", "codex"), ("Cursor", "CURSOR", "cursor"), ("Builder", "BF-BUILDER", "botfleet"),
                                  ("Compiler", "GB-COMPILER", "grok-bot"), ("Muse Assistant", "MA", "muse-assist"),
                                  ("Grok", "GROK", "grok-terminal")):
            with self.subTest(seat=seat):
                self.fake.add_message(sender, "agent-sync", "t-" + seat, "hi")
                (row,) = self.lines("read", "--topic", "t-" + seat, "--json")
                self.assertEqual((row["seat"], row["seat_app"]), (seat, app))

    def test_the_seat_comes_from_the_email_not_the_display_name(self) -> None:
        self.fake.add_user("sneaky-bot@zulip.test", "Claude", is_bot=True)
        self.fake.add_message("Claude", "agent-sync", "t", "I am Claude")  # the fake user_named finds the real bot first
        liar = next(u for u in self.fake.users if u["email"] == "sneaky-bot@zulip.test")
        self.fake.add_message(liar, "agent-sync", "t2", "I am also Claude")
        (row,) = self.lines("read", "--topic", "t2", "--json")
        self.assertEqual((row["seat"], row["seat_app"]), ("SNEAKY", None))

    def test_a_person_has_no_seat(self) -> None:
        self.fake.add_message("Jay Wedgeworth", "agent-sync", "t", "hello")
        (row,) = self.lines("read", "--topic", "t", "--json")
        self.assertEqual((row["seat"], row["seat_app"], row["is_bot"]), (None, None, False))
        # a person whose first name is a seat name still has none
        guest = self.fake.add_user("claude.person@zulip.test", "Claude Person", is_bot=False)
        self.fake.add_message(guest, "agent-sync", "t3", "hello")
        (row,) = self.lines("read", "--topic", "t3", "--json")
        self.assertEqual((row["seat"], row["seat_app"]), (None, None))

    def test_a_bot_outside_the_fleet_has_a_seat_but_no_app(self) -> None:
        webhook = self.fake.add_user("sentry-bot@zulip.test", "Sentry", is_bot=True)
        self.fake.add_message(webhook, "agent-sync", "t", "alert")
        (row,) = self.lines("read", "--topic", "t", "--json")
        self.assertEqual((row["seat"], row["seat_app"]), ("SENTRY", None))

    def test_a_retired_seat_has_no_app(self) -> None:
        old = self.fake.add_user("monet-bot@zulip.test", "Monet", is_bot=True)
        self.fake.add_message(old, "agent-sync", "t", "still here")
        (row,) = self.lines("read", "--topic", "t", "--json")
        self.assertEqual((row["seat"], row["seat_app"]), ("MONET", None))

    def test_an_unreadable_registry_refuses_the_read_rather_than_trusting_the_identity(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "hi")
        env = self.env(FLEET_APPS_JSON=str(self.tmp / "missing.json"))
        result = self.run_cli("read", "--topic", "t", "--json", env=env)
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("seat registry", result.err)
        self.assertEqual(result.out, "")

    def test_the_other_json_fields_are_unchanged(self) -> None:
        mid = self.fake.add_message("Codex", "agent-sync", "t", "hi @**Claude**")
        (row,) = self.lines("read", "--topic", "t", "--json")
        self.assertEqual(set(row), {"id", "sender_id", "client", "channel", "topic", "dm", "sender_email", "sender_full_name",
                                    "is_bot", "seat", "seat_app", "timestamp", "content", "flags", "sibling", "mentioned",
                                    "reactions"})
        self.assertEqual(row["id"], mid)

    def test_the_fields_also_reach_inbox_and_dm_read(self) -> None:
        self.fake.add_direct_message("Codex", "ping", recipients=[self.fake.me])
        (row,) = self.lines("inbox", "--json", "--peek")
        self.assertEqual((row["seat"], row["seat_app"]), ("CODEX", "codex"))
        (row,) = self.lines("dm-read", "--with", "Codex", "--json")
        self.assertEqual((row["seat"], row["seat_app"]), ("CODEX", "codex"))


# --------------------------------------------------------------------------------------------
# read --json:  reactions, and whoami --json:  owner_user_id (open-loops C6-2, the one-tap answer)
# --------------------------------------------------------------------------------------------

class ReactionFieldsTests(OwnerHarness):
    def lines(self, *argv: str) -> list[dict]:
        result = self.run_cli(*argv)
        self.assertEqual(result.code, 0, result.err)
        return [json.loads(line) for line in result.out.splitlines()]

    def react(self, message_id: int, *reactions: dict) -> None:
        for message in self.fake.messages:
            if message["id"] == message_id:
                message["reactions"] = list(reactions)
                return
        raise AssertionError("no message %d" % message_id)

    def test_a_message_carries_who_reacted_and_with_which_emoji(self) -> None:
        mid = self.fake.add_message("Claude", "agent-sync", "t", "pick one")
        self.react(mid, {"emoji_name": "one", "emoji_code": "0031-20e3", "reaction_type": "unicode_emoji", "user_id": OWNER_ID,
                         "user": {"id": OWNER_ID, "email": "jay@zulip.test", "full_name": "Jay Wedgeworth"}},
                   {"emoji_name": "two", "emoji_code": "0032-20e3", "reaction_type": "unicode_emoji", "user_id": 77})
        (row,) = self.lines("read", "--topic", "t", "--json", "--include-self")
        self.assertEqual(row["reactions"], [
            {"user_id": OWNER_ID, "reaction_type": "unicode_emoji", "emoji_code": "0031-20e3", "emoji_name": "one"},
            {"user_id": 77, "reaction_type": "unicode_emoji", "emoji_code": "0032-20e3", "emoji_name": "two"}])
        self.assertNotIn("full_name", json.dumps(row["reactions"]), "the nested user object is not passed on")

    def test_a_message_without_reactions_has_an_empty_list(self) -> None:
        self.fake.add_message("Claude", "agent-sync", "t", "quiet")
        (row,) = self.lines("read", "--topic", "t", "--json", "--include-self")
        self.assertEqual(row["reactions"], [])

    def test_a_custom_emoji_keeps_its_type_so_a_consumer_can_refuse_it(self) -> None:
        mid = self.fake.add_message("Claude", "agent-sync", "t", "pick one")
        self.react(mid, {"emoji_name": "one", "emoji_code": "12", "reaction_type": "realm_emoji", "user_id": OWNER_ID})
        (row,) = self.lines("read", "--topic", "t", "--json", "--include-self")
        self.assertEqual(row["reactions"][0]["reaction_type"], "realm_emoji")

    def test_odd_rows_are_dropped_or_blanked_never_crash(self) -> None:
        mid = self.fake.add_message("Claude", "agent-sync", "t", "pick one")
        self.react(mid, "not a dict", {"emoji_code": "0031-20e3"}, {"user_id": True, "emoji_code": "0031-20e3"},
                   {"user_id": "5", "emoji_code": "0031-20e3"}, {"user": {"id": 9}, "emoji_code": 31, "emoji_name": None},
                   {"user_id": 8, "emoji_code": "0033-20e3", "reaction_type": ["unicode_emoji"]})
        (row,) = self.lines("read", "--topic", "t", "--json", "--include-self")
        self.assertEqual(row["reactions"], [
            {"user_id": 9, "reaction_type": None, "emoji_code": None, "emoji_name": None},
            {"user_id": 8, "reaction_type": None, "emoji_code": "0033-20e3", "emoji_name": None}])

    def test_the_list_is_capped(self) -> None:
        mid = self.fake.add_message("Claude", "agent-sync", "t", "pick one")
        self.react(mid, *[{"user_id": 1000 + n, "emoji_code": "0031-20e3", "reaction_type": "unicode_emoji"} for n in range(250)])
        (row,) = self.lines("read", "--topic", "t", "--json", "--include-self")
        self.assertEqual(len(row["reactions"]), cli.MAX_REACTIONS_SHOWN)

    def test_whoami_names_the_pinned_owner(self) -> None:
        self.pin_owner()
        info = json.loads(self.run_cli("whoami", "--json").out)
        self.assertEqual(info["owner_user_id"], OWNER_ID)
        self.assertIn("owner id:", self.run_cli("whoami").out)

    def test_whoami_says_null_when_the_owner_is_not_pinned_or_the_file_is_damaged(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / "listener.toml"
        for text in (None, "[daemon]\nowner_user_id = 0\n", "this is = not [toml", "[daemon]\nowner_user_id = \"12\"\n"):
            with self.subTest(text=text):
                if text is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(text)
                result = self.run_cli("whoami", "--json")
                self.assertEqual(result.code, 0, result.err)
                self.assertIsNone(json.loads(result.out)["owner_user_id"])
        self.assertIn("not pinned", self.run_cli("whoami").out)


class ReadmeCoversTheNewSurfaceTests(unittest.TestCase):
    def test_every_new_flag_and_command_is_in_the_readme(self) -> None:
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        for needle in ("verify-owner-msg", "--to-owner", "--body-file", "render_untrusted", "redact_fields", "seat_app",
                       "| 7 |", "7 when not", "heuristic", "reactions", "owner_user_id"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_the_mode_bits_the_reader_accepts_are_exactly_0600_and_0400(self) -> None:
        self.assertEqual(bodyfile._ALLOWED_MODES, (stat.S_IRUSR | stat.S_IWUSR, stat.S_IRUSR))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
