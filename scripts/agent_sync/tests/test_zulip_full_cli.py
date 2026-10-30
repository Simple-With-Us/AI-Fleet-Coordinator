"""AFC-330:  the CLI commands for the rest of Zulip, against the fake server.  show, edit, delete,
history, move, unresolve, unreact, read and star flags, search, channels --all, channel-info,
members, unsubscribe, users, presence, set-status, groups, emoji, upload, scheduled, drafts, typing,
dm-list and the guarded `api` escape hatch."""
from __future__ import annotations

import json
import os
import time

from agent_sync import zulip as Z
from agent_sync import zulip_ops as OPS
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.harness import TAG, Harness


class MessageCommandTests(Harness):
    def test_show_prints_one_message_with_its_flags(self) -> None:
        mid = self.fake.add_message("Codex", "agent-sync", "t", "hello @**Claude**")
        result = self.run_cli("show", "--id", str(mid), "--json")
        self.assertEqual(result.code, 0, result.err)
        row = json.loads(result.out)
        self.assertEqual((row["id"], row["topic"], row["mentioned"]), (mid, "t", True))

    def test_edit_own_message_is_tagged_and_scanned(self) -> None:
        posted = json.loads(self.run_cli("post", "--topic", "t", "first", "--json").out)["id"]
        result = self.run_cli("edit", "--id", str(posted), "second", "words")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.fake._find(posted)["content"], "[CLAUDE·%s] second words" % TAG)
        secret = self.run_cli("edit", "--id", str(posted), "key " + self.key)
        self.assertEqual(secret.code, 2)
        self.assertIn("refusing to post", secret.err)
        history = self.run_cli("history", "--id", str(posted), "--json")
        versions = [json.loads(line) for line in history.out.splitlines()]
        self.assertEqual(len(versions), 2)
        self.assertIn("second words", versions[-1]["content"])

    def test_edit_to_tags_a_peer(self) -> None:
        posted = json.loads(self.run_cli("post", "--topic", "t", "first", "--json").out)["id"]
        result = self.run_cli("edit", "--id", str(posted), "--to", "CODEX", "now for you")
        self.assertEqual(result.code, 0, result.err)
        content = self.fake._find(posted)["content"]
        self.assertIn("@**Codex**", content)
        self.assertIn("now for you", content)

    def test_a_delete_of_a_message_that_vanished_keeps_zulips_own_error(self) -> None:
        # Kody, PR #1148:  only a message that is still there was refused;  any other 400 is Zulip's own.
        class Client:
            def __init__(self) -> None:
                self.present = True

            def get(self, path: str, params: object = None) -> dict:
                if not self.present:
                    raise Z.ApiError("Invalid message(s)", code="BAD_REQUEST", status=400)
                return {"message": {"id": 7}}

            def delete(self, path: str, params: object = None) -> dict:
                self.present = False
                raise Z.ApiError("Invalid message(s)", code="BAD_REQUEST", status=400)

        agent = type("Agent", (), {"client": Client()})()
        with self.assertRaises(Z.ApiError) as caught:
            OPS.delete_message(agent, 7)
        self.assertNotIsInstance(caught.exception, Z.UsageError)
        self.assertEqual(caught.exception.msg, "Invalid message(s)")

    def test_edit_refuses_another_senders_message(self) -> None:
        theirs = self.fake.add_message("Codex", "agent-sync", "t", "not yours")
        before = len(self.fake.requests_to("PATCH", "messages/%d" % theirs))
        edit = self.run_cli("edit", "--id", str(theirs), "mine now")
        self.assertEqual(edit.code, 2)
        self.assertIn("was not sent by this bot", edit.err)
        self.assertEqual(len(self.fake.requests_to("PATCH", "messages/%d" % theirs)), before)

    def test_delete_leaves_another_senders_message_to_zulips_permission(self) -> None:
        theirs = self.fake.add_message("Codex", "agent-sync", "t", "not yours")
        refused = self.run_cli("delete", "--id", str(theirs))
        self.assertEqual(refused.code, 2)
        self.assertIn("no permission to delete it", refused.err)
        self.assertEqual(len(self.fake.requests_to("DELETE", "messages/%d" % theirs)), 1)
        self.fake.can_delete_any.add(self.fake.me["user_id"])
        result = self.run_cli("delete", "--id", str(theirs), "--json")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out), {"id": theirs, "deleted": True})
        self.assertFalse(any(m["id"] == theirs for m in self.fake.messages))

    def test_delete_own_message(self) -> None:
        posted = json.loads(self.run_cli("post", "--topic", "t", "bye", "--json").out)["id"]
        result = self.run_cli("delete", "--id", str(posted), "--json")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(result.out), {"id": posted, "deleted": True})
        self.assertFalse(any(m["id"] == posted for m in self.fake.messages))

    def test_move_renames_and_moves_and_unresolve(self) -> None:
        first = self.fake.add_message("Codex", "agent-sync", "old name", "a")
        self.fake.add_message("Codex", "agent-sync", "old name", "b")
        result = self.run_cli("move", "--topic", "old name", "--new-topic", "new name")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual({m["subject"] for m in self.fake.messages}, {"new name"})
        moved = self.run_cli("move", "--topic", "new name", "--new-channel", "other", "--mode", "change_one",
                             "--id", str(first))
        self.assertEqual(moved.code, 0, moved.err)
        self.assertEqual(self.fake._find(first)["display_recipient"], "other")
        self.assertEqual(self.run_cli("move", "--topic", "new name").code, 2)  # nothing to move
        self.assertEqual(self.run_cli("resolve", "--topic", "new name").code, 0)
        un = self.run_cli("unresolve", "--topic", "✔ new name", "--json")
        self.assertEqual(un.code, 0, un.err)
        self.assertEqual(json.loads(un.out)["topic"], "new name")
        self.assertEqual(self.run_cli("unresolve", "--topic", "new name").code, 2)  # not resolved
        # Unresolving by the plain name finds the check-marked topic (found live in #sandbox).
        self.assertEqual(self.run_cli("resolve", "--topic", "new name").code, 0)
        plain = self.run_cli("unresolve", "--topic", "new name", "--json")
        self.assertEqual(plain.code, 0, plain.err)
        self.assertEqual(json.loads(plain.out)["topic"], "new name")

    def test_unreact_is_idempotent(self) -> None:
        mid = self.fake.add_message("Codex", "agent-sync", "t", "x")
        self.assertEqual(self.run_cli("react", "--id", str(mid), "eyes").code, 0)
        self.assertEqual(self.run_cli("unreact", "--id", str(mid), ":eyes:").code, 0)
        again = self.run_cli("unreact", "--id", str(mid), "eyes")
        self.assertEqual(again.code, 0, again.err)

    def test_flags_by_id_and_by_topic(self) -> None:
        a = self.fake.add_message("Codex", "agent-sync", "t", "a")
        b = self.fake.add_message("Codex", "agent-sync", "t", "b")
        self.assertEqual(self.run_cli("star", "--id", str(a)).code, 0)
        self.assertIn("starred", self.fake._flags(self.fake._find(a)))
        self.assertEqual(self.run_cli("unstar", "--id", str(a)).code, 0)
        self.assertNotIn("starred", self.fake._flags(self.fake._find(a)))
        read = self.run_cli("mark-read", "--channel", "agent-sync", "--topic", "t", "--json")
        self.assertEqual(json.loads(read.out)["updated"], 2)
        self.assertIn("read", self.fake._flags(self.fake._find(b)))
        self.assertEqual(self.run_cli("mark-unread", "--id", str(b)).code, 0)
        self.assertNotIn("read", self.fake._flags(self.fake._find(b)))
        self.assertEqual(self.run_cli("mark-read").code, 2)  # neither ids nor a channel
        self.assertEqual(self.run_cli("mark-read", "--topic", "t").code, 2)


class SearchAndDirectoryTests(Harness):
    def test_search_words_sender_has_and_channel(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "deploy failed see https://x.test")
        self.fake.add_message("Cursor", "agent-sync", "t", "deploy fine")
        self.fake.add_message("Codex", "other", "u", "unrelated")
        found = self.run_cli("search", "deploy", "--sender", "Codex", "--has", "link", "--json")
        self.assertEqual(found.code, 0, found.err)
        rows = [json.loads(line) for line in found.out.splitlines()]
        self.assertEqual([r["sender_full_name"] for r in rows], ["Codex"])
        narrow = json.loads(self.fake.requests_to("GET", "messages")[-1].params["narrow"])
        self.assertEqual([t["operator"] for t in narrow], ["sender", "has", "search"])
        both = self.run_cli("search", "--channel", "other", "--dm-with", "Codex")
        self.assertEqual(both.code, 2)
        self.assertEqual(self.run_cli("search").code, 2)
        self.assertEqual(self.run_cli("search", "--topic", "t").code, 2)

    def test_channels_all_info_members_unsubscribe(self) -> None:
        listed = self.run_cli("channels", "--all", "--json")
        names = {row["name"]: row for row in json.loads(listed.out)}
        self.assertFalse(names["sandbox"]["subscribed"])
        info = json.loads(self.run_cli("channel-info", "--channel", "agent-sync", "--json").out)
        self.assertEqual((info["stream_id"], info["public"]), (7, True))
        members = self.run_cli("members", "--channel", "agent-sync", "--json")
        self.assertIn(BOT_EMAIL, members.out)
        gone = json.loads(self.run_cli("unsubscribe", "--channel", "other", "--json").out)
        self.assertEqual(gone, {"channel_id": 8, "removed": True})
        self.assertNotIn("other", self.fake.subscribed)

    def test_users_presence_status_groups_emoji(self) -> None:
        bots = [json.loads(line) for line in self.run_cli("users", "--bots", "--json").out.splitlines()]
        self.assertIn("CODEX", {b.get("seat") for b in bots})
        self.assertEqual(json.loads(self.run_cli("users", "--id", "12", "--json").out)["full_name"], "Jay Wedgeworth")
        self.fake.presence[12] = {"website": {"status": "active", "timestamp": int(time.time())}}
        presence = json.loads(self.run_cli("presence", "Jay Wedgeworth", "--json").out)
        self.assertEqual(presence["status"], "active")
        self.assertEqual(self.run_cli("set-status", "--text", "deploying", "--away").code, 0)
        self.assertEqual(self.fake.statuses[10]["status_text"], "deploying")
        self.assertEqual(self.run_cli("set-status", "--text", "key " + self.key).code, 2)
        self.assertEqual(self.run_cli("set-status").code, 2)
        self.fake.user_groups.append({"id": 5, "name": "fleet", "members": [10, 11], "description": ""})
        self.assertIn("fleet", self.run_cli("groups").out)
        self.assertIn("Codex", self.run_cli("groups", "--id", "5").out)
        emoji = self.run_cli("emoji").out
        self.assertIn(":fleet:", emoji)
        self.assertNotIn(":old:", emoji)
        self.assertIn(":old:", self.run_cli("emoji", "--all").out)


class UploadScheduledDraftsTypingTests(Harness):
    def test_upload_a_file_and_refuse_a_secret(self) -> None:
        path = self.tmp / "notes.txt"
        path.write_text("build log\n")
        result = self.run_cli("upload", str(path), "--json")
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertTrue(info["url"].startswith("/user_uploads/"))
        self.assertEqual(self.fake.uploads[0]["data"], b"build log\n")
        leaked = self.tmp / "leak.txt"
        leaked.write_text("token " + self.key)
        refused = self.run_cli("upload", str(leaked))
        self.assertEqual(refused.code, 2)
        self.assertEqual(len(self.fake.uploads), 1)
        self.assertEqual(self.run_cli("upload", str(self.tmp)).code, 2)  # not a regular file

    def test_scheduled_messages(self) -> None:
        #  The owner is in the conversation, so the scheduled direct message is not a bot to bot
        #  one (AFC-469).
        self.pin_owner()
        when = str(int(time.time()) + 3600)
        created = self.run_cli("scheduled", "create", "--topic", "later", "--at", when, "ping", "--json")
        self.assertEqual(created.code, 0, created.err)
        sid = json.loads(created.out)["id"]
        self.assertEqual(self.fake.scheduled[sid]["content"], "[CLAUDE·%s] ping" % TAG)
        self.assertEqual(self.run_cli("scheduled", "edit", "--id", str(sid), "pong").code, 0)
        self.assertIn("pong", self.fake.scheduled[sid]["content"])
        listed = [json.loads(line) for line in self.run_cli("scheduled", "list", "--json").out.splitlines()]
        self.assertEqual([r["id"] for r in listed], [sid])
        dm = self.run_cli("scheduled", "create", "--to", "Codex", "--to", "Jay Wedgeworth", "--at", when, "hi")
        self.assertEqual(dm.code, 0, dm.err)
        past = self.run_cli("scheduled", "create", "--topic", "x", "--at", "1000", "late")
        self.assertEqual(past.code, 2)
        both = self.run_cli("scheduled", "create", "--to", "Codex", "--to", "Jay Wedgeworth", "--topic", "x", "--at", when, "hi")
        self.assertEqual(both.code, 2)
        self.assertEqual(self.run_cli("scheduled", "delete", "--id", str(sid)).code, 0)
        self.assertNotIn(sid, self.fake.scheduled)

    def test_drafts_and_typing(self) -> None:
        #  The owner is in the conversation, so the direct draft edit is not a bot to bot one
        #  (AFC-469).
        self.pin_owner()
        made = json.loads(self.run_cli("drafts", "create", "--topic", "d", "draft text", "--json").out)
        self.assertEqual(self.fake.drafts[made["id"]]["content"], "draft text")
        self.assertEqual(self.run_cli("drafts", "edit", "--id", str(made["id"]), "--to", "Codex", "--to", "Jay Wedgeworth", "new").code, 0)
        self.assertEqual(self.fake.drafts[made["id"]]["type"], "private")
        self.assertIn("new", self.run_cli("drafts", "list").out)
        self.assertEqual(self.run_cli("drafts", "delete", "--id", str(made["id"])).code, 0)
        self.assertEqual(self.run_cli("typing", "start", "--topic", "t").code, 0)
        self.assertEqual(self.fake.typing[-1]["type"], "stream")
        self.assertEqual(self.run_cli("typing", "stop", "--to", "Codex").code, 0)
        self.assertEqual(json.loads(self.fake.typing[-1]["to"]), [11])
        self.assertEqual(self.run_cli("typing", "start").code, 2)

    def test_dm_list(self) -> None:
        self.fake.add_direct_message("Codex", "hi bot")
        result = self.run_cli("dm-list", "--json")
        self.assertEqual(result.code, 0, result.err)
        row = json.loads(result.out)
        self.assertEqual((row["user_ids"], row["unread"]), ([11], 1))


class ApiEscapeHatchTests(Harness):
    def test_allowlisted_endpoint_runs(self) -> None:
        mid = self.fake.add_message("Codex", "agent-sync", "t", "x")
        result = self.run_cli("api", "GET", "messages/%d/history" % mid)
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("message_history", result.out)
        posted = self.run_cli("api", "POST", "/api/v1/messages/%d/reactions" % mid, "--param", "emoji_name=eyes")
        self.assertEqual(posted.code, 0, posted.err)

    def test_admin_and_unknown_paths_are_refused_before_any_request(self) -> None:
        before = len(self.fake.requests)
        for method, path in (("POST", "users"), ("PATCH", "realm"), ("DELETE", "users/11"), ("GET", "realm/domains"),
                             ("PATCH", "streams/7"), ("GET", "messages/../users"), ("POST", "bots"), ("GET", "export/realm")):
            result = self.run_cli("api", method, path)
            self.assertEqual(result.code, 2, (method, path))
            self.assertIn("allowlist", result.err)
        self.assertEqual(len(self.fake.requests), before)

    def test_secret_in_a_parameter_is_refused(self) -> None:
        result = self.run_cli("api", "POST", "messages", "--param", "type=stream", "--param", "to=agent-sync",
                              "--param", "topic=t", "--param", "content=key " + self.key)
        self.assertEqual(result.code, 2)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])
        bad = self.run_cli("api", "POST", "messages/flags", "--json-param", "messages=[1")
        self.assertEqual(bad.code, 2)

    def test_allowlist_function(self) -> None:
        self.assertTrue(OPS.api_allowed("get", "users/me"))
        self.assertFalse(OPS.api_allowed("PATCH", "users/me"))
        self.assertFalse(OPS.api_allowed("GET", "users/me/../../admin"))
        self.assertFalse(OPS.api_allowed("GET", "messages?x=1"))
        for method, patterns in OPS.API_ALLOWLIST.items():
            for pattern in patterns:
                self.assertFalse(pattern.startswith(("realm/domains", "bots", "invites")), pattern)


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
