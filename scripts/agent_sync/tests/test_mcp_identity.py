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



class CliTests(IdentityHarness):
    """The CLI side:  `agent-sync identities` lists the same set, and every write is audited."""

    def cli_env(self, **overrides: str | None) -> dict[str, str]:
        return self.env(AGENT_SYNC_SECRETS_DIR=None, **overrides)  # the files under HOME, like the server

    def test_identities_lists_the_same_set(self):
        result = self.run_cli("identities", "--json", env=self.cli_env())
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual([row["identity"] for row in info["identities"]], self.expected)
        self.assertEqual(info["default_identity"], "CLAUDE")
        pinned = self.run_cli("identities", "--json", env=self.cli_env(AGENT_SEAT=None, AGENT_LAUNCHER="botfleet",
                                                                        AGENT_LAUNCH_SEAT="CLAUDE"))
        self.assertEqual([row["identity"] for row in json.loads(pinned.out)["identities"]], ["CLAUDE"])

    def test_cli_writes_are_audited_and_reads_are_not(self):
        posted = self.run_cli("post", "--as", "CURSOR", "--topic", "cli", "hello", env=self.cli_env(AGENT_SEAT=None))
        self.assertEqual(posted.code, 0, posted.err)
        self.assertEqual(self.run_cli("topics", env=self.cli_env()).code, 0)
        rows = [json.loads(line) for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()]
        self.assertEqual([(r["transport"], r["identity"], r["tool"], r["outcome"]) for r in rows],
                         [("cli", "CURSOR", "post", "ok")])
        self.assertNotIn("hello", json.dumps(rows))

    def test_an_unreadable_registry_refuses_every_identity_on_the_cli_naming_the_file(self):
        missing = self.cli_env(AGENT_SEAT=None, FLEET_APPS_JSON="/nonexistent/fleet-apps.json")
        for argv in (("post", "--as", "SENTRY", "--topic", "cli", "x"), ("post", "--as", "CURSOR", "--topic", "cli", "x"),
                     ("topics", "--as", "CLAUDE")):
            with self.subTest(argv):
                refused = self.run_cli(*argv, env=missing)
                self.assertEqual(refused.code, 3, refused.err)
                self.assertIn("seat registry", refused.err)
                self.assertIn("FLEET_APPS_JSON or fleet-apps.json", refused.err)
        self.assertEqual(self.fake.messages, [])

    def test_whoami_and_identities_report_an_unreadable_registry_instead_of_crashing(self):
        missing = self.cli_env(FLEET_APPS_JSON="/nonexistent/fleet-apps.json")
        who = self.run_cli("whoami", "--json", env=missing)
        self.assertEqual(who.code, 3, who.err)
        info = json.loads(who.out)
        self.assertIn("cannot read the seat registry", info["registry_problem"])
        self.assertIs(info["verified"], False)
        self.assertNotIn("Traceback", who.err)
        listed = self.run_cli("identities", "--json", env=missing)
        self.assertEqual(listed.code, 3, listed.err)
        shown = json.loads(listed.out)
        self.assertEqual(shown["identities"], [])
        self.assertIn("FLEET_APPS_JSON or fleet-apps.json", shown["registry_problem"])
        ok = self.run_cli("whoami", "--json", env=self.cli_env())
        self.assertEqual(ok.code, 0, ok.err)
        self.assertIsNone(json.loads(ok.out)["registry_problem"])

    def test_the_cli_never_acts_as_an_integration_bot_or_a_disabled_persona(self):
        for name in ("SENTRY", "GB-ENGINEER"):
            with self.subTest(name):
                refused = self.run_cli("post", "--as", name, "--topic", "cli", "x", env=self.cli_env(AGENT_SEAT=None))
                self.assertEqual(refused.code, 3, refused.err)
        self.assertEqual(self.fake.messages, [])


class EnvBackedTests(McpHarness):
    """The server instance (Codex P1 on PR #588):  cloud seats and GB personas have no zuliprc there;  their
    keys come from their own ZULIP_<CODE>_EMAIL and ZULIP_<CODE>_API_KEY with ZULIP_SITE, which Infisical
    syncs into the container.  The allowed set and the opener read that source too, with the same exclusions,
    and no value reaches argv, a log or the output."""

    def setUp(self) -> None:
        super().setUp()
        self.vars: dict[str, str] = {"ZULIP_SITE": self.fake.url}
        bots = {"GROK-WEB": ("grok-web-bot@zulip.test", "Grok Web"),
                PERSONA: (PERSONA_EMAIL, PERSONA.title()),
                "BF-PLUMBER": ("bf-plumber-bot@zulip.test", "BF Plumber"),
                "SENTRY": ("sentry-bot@zulip.test", "Sentry"),
                "GB-ENGINEER": ("engineer-grok-bot@zulip.test", "Engineer")}
        for name, (email, full_name) in bots.items():
            key = zulip_shaped()
            self.extra_secrets.append(key)
            self.fake.add_bot(email, full_name, key)
            code = X.env_code(name)
            self.vars["ZULIP_%s_EMAIL" % code] = email
            self.vars["ZULIP_%s_API_KEY" % code] = '"%s"' % key  # quote-wrapped, as the synced values are
        self.env_only = sorted({"GROK-WEB", PERSONA, "BF-PLUMBER"})

    def server_env(self, **overrides: str | None) -> dict[str, str]:
        return {**self.mcp_env(**overrides), **self.vars}

    def error(self, result: dict) -> dict:
        self.assertIs(result.get("isError"), True, result)
        return json.loads(result["content"][0]["text"])

    def test_a_host_with_only_env_backed_seats(self):
        self.home_rc.unlink()  # no credential file at all
        session = self.session(self.server_env(AGENT_SEAT=None))
        info = self.assert_structured("identities", session.call("identities"))
        self.assertEqual([row["identity"] for row in info["identities"]], self.env_only)
        self.assertIsNone(info["default_identity"])
        posted = self.assert_structured("zulip_post", session.call("zulip_post", {"topic": "env", "text": "hi"},
                                                                   identity=PERSONA))
        message = next(m for m in self.fake.messages if m["id"] == posted["id"])
        self.assertEqual(message["sender_email"], PERSONA_EMAIL)
        who = self.assert_structured("whoami", session.call("whoami", {"identity": "GROK-WEB"}))
        self.assertEqual((who["seat"], who["email"]), ("GROK-WEB", "grok-web-bot@zulip.test"))

    def test_a_mixed_host_lists_files_and_env_seats(self):
        session = self.session(self.server_env())
        info = self.assert_structured("identities", session.call("identities"))
        self.assertEqual([row["identity"] for row in info["identities"]], sorted(["CLAUDE", *self.env_only]))
        self.assertEqual(self.assert_structured("whoami", session.call("whoami"))["seat"], "CLAUDE")
        self.assertEqual(self.assert_structured("whoami", session.call("whoami", {"identity": "BF-PLUMBER"}))["seat"],
                         "BF-PLUMBER")

    def test_as_names_an_env_backed_seat(self):
        self.home_rc.unlink()
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}}
        code, lines, stderr = self.spawn(self.server_env(AGENT_SEAT=None), [call], argv=("--as", "GROK-WEB"))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(lines[0]["result"]["structuredContent"]["seat"], "GROK-WEB")
        self.assertIn("serving seat GROK-WEB", stderr)
        for value in self.extra_secrets:
            self.assertNotIn(value, stderr)

    def test_an_integration_bot_or_disabled_persona_in_env_is_never_an_identity(self):
        session = self.session(self.server_env())
        names = [row["identity"] for row in self.assert_structured("identities", session.call("identities"))["identities"]]
        for never in ("SENTRY", "GB-ENGINEER"):
            self.assertNotIn(never, names)
            self.assertEqual(self.error(session.call("zulip_topics", {"identity": never}))["code"], "identity_not_allowed")
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        code, lines, stderr = self.spawn(self.server_env(AGENT_SEAT=None), [ping], argv=("--as", "SENTRY"))
        self.assertEqual((code, lines), (3, []))
        self.assertIn("SENTRY is not an agent bot identity", stderr)
        self.assertEqual(self.fake.messages, [])

    def test_an_unreadable_seat_registry_serves_no_identity_not_even_the_default(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        for argv in ((), ("--as", "MONET")):  # the ambient default, and a retired seat that has a credential file
            code, lines, stderr = self.spawn(self.server_env(FLEET_APPS_JSON="/nonexistent/fleet-apps.json"), [ping],
                                             argv=argv)
            self.assertEqual((code, lines), (3, []), stderr)
            self.assertIn("seat registry", stderr)
            self.assertIn("none is served", stderr)
        self.assertEqual(self.fake.messages, [])

    def test_a_key_in_another_seats_variable_is_refused_on_first_use(self):
        self.vars["ZULIP_GROK_WEB_EMAIL"] = "bf-plumber-bot@zulip.test"
        self.vars["ZULIP_GROK_WEB_API_KEY"] = self.vars["ZULIP_BF_PLUMBER_API_KEY"]
        session = self.session(self.server_env())
        error = self.error(session.call("zulip_post", {"topic": "t", "text": "x"}, identity="GROK-WEB"))
        self.assertEqual(error["code"], "not_authorized")
        self.assertIn("env ZULIP_GROK_WEB_API_KEY", error["message"])
        self.assertEqual(self.fake.messages, [])

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
