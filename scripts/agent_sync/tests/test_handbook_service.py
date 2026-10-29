import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_sync import handbook_service as H
from agent_sync import zulip as Z


class Client:
    def __init__(self):
        self.me = {
            "user_id": 44,
            "email": "operator@example.test",
            "is_bot": True,
            "bot_owner_id": 11,
        }
        self.posts = []
        self.error = None

    def get(self, path, *args):
        if path == "users/me":
            return self.me
        if path == "messages":
            return {"messages": [], "found_oldest": True}
        return {
            "message": {
                "id": 22,
                "type": "stream",
                "display_recipient": H.CHANNEL,
                "subject": H.TOPIC,
                "reactions": [{"user_id": 11}],
            }
        }

    def post(self, path, params):
        self.posts.append((path, params))
        if self.error:
            raise self.error
        return {"id": 22}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.body = Path(self.home) / "body"
        self.body.write_text("Rule changed.  React ✅. @all")
        self.a = argparse.Namespace(
            owner_id=11,
            operator_id=44,
            operator_email="operator@example.test",
            site="https://example.zulipchat.com",
            action="post",
            repo="Simple-With-Us/Fleet-OPS",
            body_file=str(self.body),
            to_owner=True,
            message_id=22,
        )
        self.c = Client()
        self.creds = Z.Credentials(
            "operator@example.test",
            "test-key-value",
            "https://example.zulipchat.com",
            "test",
        )

    def run_service(self):
        return H.execute(
            self.a, env={"HOME": self.home}, client=self.c, credentials=self.creds
        )

    def test_remote_and_launched_seat_refused(self):
        for env in [
            {"SIMPLE_CHAT_REMOTE": "1"},
            {"AGENT_LAUNCHER": "1"},
            {"AGENT_LAUNCH_SEAT": "CLAUDE"},
        ]:
            with self.assertRaises(ValueError):
                H.execute(self.a, env=env, client=self.c, credentials=self.creds)

    def test_credential_realm_cannot_be_redirected(self):
        self.a.site = "https://attacker.invalid"
        with self.assertRaises(ValueError):
            self.run_service()
        self.assertFalse(self.c.posts)

    def test_foreign_repo_refused(self):
        self.a.repo = "o/other"
        with self.assertRaises(ValueError):
            self.run_service()

    def test_named_store_realm_checked_before_auth(self):
        env = {"HOME": self.home}
        with (
            patch.object(Z, "infisical_project_id", return_value="project"),
            patch.object(
                Z,
                "_default_infisical_fetch",
                return_value={"ZULIP_OPERATOR_SITE": "https://other.invalid"},
            ) as fetch,
        ):
            with self.assertRaises(ValueError):
                H.execute(self.a, env=env, client=self.c)
            self.assertEqual(fetch.call_args.args[3], "/zulip_operator")
        self.assertFalse(self.c.posts)

    def test_reservation_completion_race_returns_cached_id(self):
        def raced(path, key, now):
            H.record(path, key, {"at": now, "state": "ok", "message_id": 88})
            return False

        with patch.object(H, "reserve", side_effect=raced):
            self.assertEqual(self.run_service(), {"id": 88})
        self.assertFalse(self.c.posts)

    def test_stat_rejects_oversized_file_before_open(self):
        self.body.write_bytes(b"x" * 40001)
        with (
            patch.object(Path, "open", side_effect=AssertionError("must not read")),
            self.assertRaises(ValueError),
        ):
            self.run_service()
        self.assertFalse(self.c.posts)

    def test_boolean_ids_refused_before_api(self):
        for name in ("owner_id", "operator_id", "message_id"):
            original = getattr(self.a, name)
            self.a.action = "read"
            setattr(self.a, name, True)
            with self.assertRaises(ValueError):
                self.run_service()
            setattr(self.a, name, original)
        self.assertFalse(self.c.posts)

    def test_real_client_get_contract(self):
        real = Z.ZulipClient(self.creds, self.a.site)
        with patch.object(real, "request", return_value={"messages": []}) as request:
            params = {"narrow": '[{"operator":"channel","operand":"Work-In-Progress"}]'}
            real.get("messages", params)
            request.assert_called_once_with("GET", "messages", params)

    def test_5xx_and_rate_limit_never_release(self):
        for status in [429, 500, 502, 503]:
            with self.subTest(status=status):
                self.body.write_text("different head " + str(status))
                self.c.error = Z.ApiError("server response", status=status)
                with self.assertRaises(Z.ApiError):
                    self.run_service()
                self.c.error = None
                with self.assertRaises(ValueError):
                    self.run_service()

    def test_distinct_body_not_rate_capped(self):
        self.run_service()
        self.body.write_text("A distinct exact head abc123.")
        self.run_service()
        self.assertEqual(len(self.c.posts), 2)

    def test_unknown_reconciles_landed_marker_without_repost(self):
        self.c.error = Z.NetworkError("timeout", maybe_sent=True)
        with self.assertRaises(Z.NetworkError):
            self.run_service()
        marker = self.c.posts[0][1]["content"].split("handbook-ref:")[-1]
        orig = self.c.get

        def get(p, *args):
            if p == "messages":
                return {
                    "messages": [
                        {
                            "id": 99,
                            "sender_id": 44,
                            "content": "<p>handbook-ref:" + marker + "</p>",
                        }
                    ],
                    "found_oldest": True,
                }
            return orig(p)

        self.c.get = get
        self.c.error = None
        self.assertEqual(self.run_service(), {"id": 99})
        self.assertEqual(len(self.c.posts), 1)

    def test_neutralizes_stream_links_and_optional_owner(self):
        self.body.write_text("#**other>topic** @all")
        self.a.to_owner = False
        self.run_service()
        body = self.c.posts[0][1]["content"]
        self.assertNotIn("#**", body)
        self.assertNotIn("@**|11**", body)

    def test_overlong_body_refused(self):
        self.body.write_text("a" * 11000)
        with self.assertRaises(ValueError):
            self.run_service()
        self.assertFalse(self.c.posts)

    def test_identity_and_scanned_deduped_post(self):
        self.assertEqual(self.run_service(), {"id": 22})
        self.assertEqual(self.run_service(), {"id": 22})
        self.assertEqual(len(self.c.posts), 1)
        body = self.c.posts[0][1]["content"]
        self.assertIn("[OPERATOR·handbook-sync]", body)
        self.assertIn("@**|11**", body)
        self.assertNotIn("@all", body)
        self.assertEqual(self.c.posts[0][1]["to"], H.CHANNEL)

    def test_wrong_identity_and_owner_refused(self):
        for field, value in [
            ("user_id", 45),
            ("email", "other@example.test"),
            ("is_bot", False),
            ("bot_owner_id", 12),
        ]:
            old = self.c.me[field]
            self.c.me[field] = value
            with self.assertRaises(ValueError):
                self.run_service()
            self.c.me[field] = old
        self.assertFalse(self.c.posts)

    def test_secret_refused(self):
        self.body.write_text(self.creds.key)
        with self.assertRaises(ValueError):
            self.run_service()
        self.assertFalse(self.c.posts)

    def test_unsettled_send_not_replayed(self):
        self.c.error = Z.NetworkError("timeout", maybe_sent=True)
        with self.assertRaises(Z.NetworkError):
            self.run_service()
        self.c.error = None
        with self.assertRaises(ValueError):
            self.run_service()
        self.assertEqual(len(self.c.posts), 1)

    def test_read_legacy_author_no_sender_waiver(self):
        self.a.action = "read"
        out = self.run_service()
        self.assertEqual(out["message"]["id"], 22)
        self.assertFalse(self.c.posts)

    def test_read_outside_topic_refused(self):
        self.a.action = "read"
        orig = self.c.get

        def get(p):
            v = orig(p)
            if p != "users/me":
                v["message"]["subject"] = "other"
            return v

        self.c.get = get
        with self.assertRaises(ValueError):
            self.run_service()

    def test_missing_pins_and_plain_http_refused(self):
        self.a.owner_id = 0
        with self.assertRaises(ValueError):
            self.run_service()
        self.a.owner_id = 11
        self.a.site = "http://example.test"
        with self.assertRaises(ValueError):
            self.run_service()


if __name__ == "__main__":
    unittest.main()
