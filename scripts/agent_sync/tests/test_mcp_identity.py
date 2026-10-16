"""Per-call identity on `agent-sync mcp` (AFC-331, docs/protocols/per-call-identity.md sections 2-4 and
the stdio rows of section 8).

The HOME of every test holds the credential files of several bots:  seats (Claude, Cursor), a BF-*
bot, an enabled GB-* persona and GB-ENGINEER (not enabled), the owner's own account and an integration
bot.  Expectations come from the GB table (identities.gb_identities()) and the seat registry, never
from a literal list of personas.
"""
from __future__ import annotations

import json
import unittest

from agent_sync import identities as X
from agent_sync.mcp import identity_router as R
from agent_sync.mcp import tools as T
from agent_sync.tests.harness import TAG, write_rc
from agent_sync.tests.mcp_harness import McpHarness, zulip_shaped

ENABLED = X.gb_identities()
PERSONA = ENABLED[1]  # any enabled persona works;  the table decides which
PERSONA_EMAIL = "%s-grok-bot@zulip.test" % PERSONA[len("GB-"):].lower()


class IdentityHarness(McpHarness):
    def setUp(self) -> None:
        super().setUp()
        secrets = self.home / ".secrets" / "Zulip"
        self.keys: dict[str, str] = {}
        bots = {
            "CURSOR": ("cursor-bot@zulip.test", "Cursor", "Cursor-zuliprc"),
            "BF-PLUMBER": ("bf-plumber-bot@zulip.test", "BF Plumber", "BF-Plumber-zuliprc"),
            PERSONA: (PERSONA_EMAIL, PERSONA.title(), "GB-%s-zuliprc" % PERSONA[3:].title()),
            "GB-ENGINEER": ("engineer-grok-bot@zulip.test", "Engineer", "GB-Engineer-zuliprc"),
            "SENTRY": ("sentry-bot@zulip.test", "Sentry", "Sentry-zuliprc"),
        }
        for name, (email, full_name, file_name) in bots.items():
            key = zulip_shaped()
            self.keys[name] = key
            self.extra_secrets.append(key)
            self.fake.add_bot(email, full_name, key)
            write_rc(secrets / file_name, email=email, key=key, site=self.fake.url)
        # The owner's own account:  a person, never in any set.
        owner_key = zulip_shaped()
        self.extra_secrets.append(owner_key)
        write_rc(secrets / "Jay-zuliprc", email="jay@zulip.test", key=owner_key, site=self.fake.url)
        self.expected = sorted({"CLAUDE", "CURSOR", "BF-PLUMBER"} | ({PERSONA, "GB-ENGINEER"} & set(ENABLED)))

    def error(self, result: dict) -> dict:
        self.assertIs(result.get("isError"), True, result)
        return json.loads(result["content"][0]["text"])


class AllowedSetTests(IdentityHarness):
    def test_identities_lists_every_agent_bot_with_a_credential_file_here(self):
        session = self.session()
        info = self.assert_structured("identities", session.call("identities"))
        names = [row["identity"] for row in info["identities"]]
        self.assertEqual(names, self.expected)
        self.assertEqual(info["default_identity"], "CLAUDE")
        self.assertEqual(info["transport"], "stdio")
        # Row 4:  GB-ENGINEER is absent while the table leaves it out;  the owner and integration bots never enter.
        self.assertEqual("GB-ENGINEER" in names, "GB-ENGINEER" in ENABLED)
        self.assertNotIn("GB-ENGINEER", ENABLED)
        for never in ("JAY", "SENTRY"):
            self.assertNotIn(never, names)
        self.assertEqual({n for n in names if n.startswith("GB-")}, {PERSONA} & set(ENABLED))

    def test_the_advertised_identity_schema_is_built_for_this_connection(self):
        """Row 6:  the enum is exactly the allowed set, required on writes, absent on identities."""
        session = self.session()
        listed = {t["name"]: t for t in session.request("tools/list")["result"]["tools"]}
        for name, tool in listed.items():
            properties = tool["inputSchema"].get("properties") or {}
            if name == "identities":
                self.assertNotIn("identity", properties)
                continue
            self.assertEqual(properties["identity"]["enum"], self.expected, name)
            self.assertIs("identity" in (tool["inputSchema"].get("required") or []), name in T.WRITE_TOOLS, name)

    def test_a_launcher_pin_narrows_the_set_to_its_seat(self):
        env = self.mcp_env(AGENT_SEAT=None, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="CLAUDE")
        session = self.session(env)
        info = self.assert_structured("identities", session.call("identities"))
        self.assertEqual([row["identity"] for row in info["identities"]], ["CLAUDE"])
        error = self.error(session.call("zulip_post", {"topic": "t", "text": "x", "identity": "CURSOR"}))
        self.assertEqual(error["code"], "identity_not_allowed")
        self.assertIn("launcher", error["message"])
        self.assertEqual(self.fake.messages, [])


class CallRuleTests(IdentityHarness):
    def test_copied_config_acts_as_the_named_identity_with_its_own_key(self):
        """Row 8:  a config with --as CLAUDE and a write naming CURSOR posts as CURSOR, with CURSOR's key;
        a write with no identity is refused."""
        session = self.session(self.mcp_env(AGENT_SEAT=None), argv=("--as", "CLAUDE"))
        refused = self.error(session.call("zulip_post", {"topic": "copied", "text": "no identity"}, identity=None))
        self.assertEqual(refused["code"], "identity_required")
        self.assertEqual(self.fake.messages, [])
        posted = self.assert_structured("zulip_post", session.call("zulip_post", {"topic": "copied", "text": "hello"},
                                                                   identity="CURSOR"))
        message = next(m for m in self.fake.messages if m["id"] == posted["id"])
        self.assertEqual(message["sender_email"], "cursor-bot@zulip.test")
        self.assertTrue(message["content"].startswith("[CURSOR·%s] " % TAG), message["content"])
        who = self.assert_structured("whoami", session.call("whoami", {"identity": "cursor"}))  # aliases resolve
        self.assertEqual((who["seat"], who["email"]), ("CURSOR", "cursor-bot@zulip.test"))

    def test_a_read_without_identity_uses_the_default(self):
        """Row 5:  a read with identity omitted acts as the default;  a write with it omitted is refused."""
        session = self.session()
        self.assertEqual(self.assert_structured("whoami", session.call("whoami"))["seat"], "CLAUDE")
        for name in sorted(T.WRITE_TOOLS):
            with self.subTest(name):
                error = self.error(session.call(name, {}, identity=None))
                self.assertEqual(error["code"], "identity_required")
                self.assertEqual(error["allowed"], self.expected)
        self.assertEqual(self.fake.messages, [])

    def test_no_default_refuses_reads_without_identity(self):
        session = self.session(self.mcp_env(AGENT_SEAT=None))
        self.assertEqual(self.error(session.call("whoami"))["code"], "identity_required")
        self.assertEqual(self.assert_structured("whoami", session.call("whoami", {"identity": PERSONA}))["seat"],
                         PERSONA)

    def test_an_identity_outside_the_set_is_refused(self):
        session = self.session()
        for name in ("SENTRY", "JAY", "GB-ENGINEER", "MONET", "not a seat", 7):
            with self.subTest(name=name):
                error = self.error(session.call("zulip_topics", {"identity": name}))
                self.assertEqual(error["code"], "identity_not_allowed")
                self.assertEqual(error["allowed"], self.expected)
        self.assertEqual(self.fake.messages, [])

    def test_a_credential_that_is_not_the_claimed_bot_is_refused_on_first_use(self):
        # Cursor's file holds BF-PLUMBER's key:  check_bot refuses it, and nothing is posted.
        write_rc(self.home / ".secrets" / "Zulip" / "Cursor-zuliprc", email="bf-plumber-bot@zulip.test",
                 key=self.keys["BF-PLUMBER"], site=self.fake.url)
        session = self.session()
        error = self.error(session.call("zulip_post", {"topic": "t", "text": "x"}, identity="CURSOR"))
        self.assertEqual(error["code"], "not_authorized")
        self.assertEqual(self.fake.messages, [])

    def test_spacing_is_per_identity(self):
        session = self.session()
        self.assert_structured("zulip_post", session.call("zulip_post", {"topic": "a", "text": "one"}))
        self.assert_structured("zulip_post", session.call("zulip_post", {"topic": "a", "text": "two"},
                                                          identity="BF-PLUMBER"))
        self.assertEqual([s for s in self.clock.sleeps if s > 0], [])  # two bots, two slots
        self.assert_structured("zulip_post", session.call("zulip_post", {"topic": "a", "text": "three"}))
        self.assertGreater(sum(self.clock.sleeps), 0)  # the same bot again waits for its slot

    def test_writes_are_audited_with_the_claimed_identity(self):
        session = self.session()
        posted = self.assert_structured("zulip_post", session.call("zulip_post", {"topic": "a", "text": "body"},
                                                                   identity=PERSONA))
        self.error(session.call("zulip_post", {"topic": "a", "text": "body"}, identity=None))
        self.error(session.call("zulip_post", {"topic": "a", "text": "body"}, identity="SENTRY"))
        session.call("zulip_topics")  # reads are not audited
        rows = [json.loads(line) for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()]
        self.assertEqual([(r["default_identity"], r["identity"], r["tool"], r["outcome"], r["message_id"]) for r in rows],
                         [("CLAUDE", PERSONA, "zulip_post", "ok", posted["id"]),
                          ("CLAUDE", None, "zulip_post", "identity_required", None),
                          ("CLAUDE", "SENTRY", "zulip_post", "identity_not_allowed", None)])
        self.assertEqual(rows[0]["client"], {"name": "test-client", "version": "1"})
        self.assertNotIn("body", json.dumps(rows))

    def test_a_removed_name_names_its_replacement(self):
        session = self.session()
        for old, new in R.renamed_tools().items():
            with self.subTest(old):
                error = self.error(session.call(old, {}))
                self.assertEqual((error["code"], error["replacement"]), ("unknown_tool", new))
        self.assertIn("error", session.request("tools/call", {"name": "no_such_tool", "arguments": {}}))

    def test_shared_fixtures(self):
        cases = [c for c in T.load_fixtures() if c["kind"] in ("renamed", "identity")]
        self.assertGreaterEqual(len(cases), 4)
        session = self.session()
        for case in cases:
            with self.subTest(case["case"]):
                result = session.call(case["tool"], case.get("arguments") or {}, identity=None)
                if case["expect"] == "ok":
                    self.assertNotIn("isError", result)
                    continue
                error = self.error(result)
                self.assertEqual(error["code"], case["expect"])
                if "replacement" in case:
                    self.assertEqual(error["replacement"], case["replacement"])
        self.assertEqual(self.fake.messages, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
