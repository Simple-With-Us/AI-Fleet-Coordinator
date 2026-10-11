"""subscribe_all against the fake Zulip server:  public channels only, bots only, dry run by
default, names-only output, and never a key in what it prints."""
from __future__ import annotations

import io
import json
import secrets
import unittest

from agent_sync import subscribe_all as S
from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import FakeZulip


class SubscribeAllTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = secrets.token_hex(16)
        self.fake = FakeZulip(self.key)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.fake.streams["private-ops"] = 50
        self.human_key = secrets.token_hex(16)
        self.fake.keys[self.human_key] = self.fake.user_by_id(12)  # Jay, a human:  never touched
        self.admin_key = secrets.token_hex(16)
        self.fake.add_bot("boss-bot@zulip.test", "Boss", self.admin_key, role=200)

    def creds(self, key: str, email: str) -> Z.Credentials:
        return Z.Credentials(email=email, key=key, site=self.fake.url, source="test")

    def all_creds(self) -> list[Z.Credentials]:
        return [self.creds(self.key, "claude-bot@zulip.test"), self.creds(self.human_key, "jay@zulip.test"),
                self.creds(self.admin_key, "boss-bot@zulip.test")]

    def test_dry_run_changes_nothing_and_names_the_missing_public_channel(self) -> None:
        out = io.StringIO()
        self.assertEqual(S.run(self.all_creds(), self.fake.url, apply=False, out=out), 0)
        text = out.getvalue()
        self.assertIn("would join #sandbox", text)
        self.assertNotIn("private-ops", text)
        self.assertEqual(self.fake.requests_to("POST", "users/me/subscriptions"), [])
        self.assertIn("1 bots checked", text, "the human and the admin bot are skipped")
        for secret in (self.key, self.human_key, self.admin_key):
            self.assertNotIn(secret, text)
        self.assertNotIn("@zulip.test", text, "seat tags only, never an email")

    def test_apply_joins_public_channels_quietly_and_only_for_bots(self) -> None:
        out = io.StringIO()
        self.assertEqual(S.run(self.all_creds(), self.fake.url, apply=True, out=out), 0)
        self.assertIn("sandbox", self.fake.subscribed)
        self.assertNotIn("private-ops", self.fake.subscribed)
        posts = self.fake.requests_to("POST", "users/me/subscriptions")
        self.assertEqual([r.user_id for r in posts], [10])
        self.assertEqual(json.loads(posts[0].params["subscriptions"]), [{"name": "sandbox"}])
        self.assertTrue(all(p["value"] is False for p in self.fake.subscription_properties))
        again = io.StringIO()
        S.run(self.all_creds(), self.fake.url, apply=True, out=again)
        self.assertIn("joined nothing", again.getvalue(), "a second run is a no-op")


if __name__ == "__main__":
    unittest.main()
