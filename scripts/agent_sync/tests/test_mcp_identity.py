"""Per-call identity on `simple-chat mcp` (AFC-331, docs/protocols/per-call-identity.md sections 2-4 and
the stdio rows of section 8).

The HOME of every test holds the credential files of several bots:  seats (Claude, Cursor), a BF-*
bot, an enabled GB-* persona and GB-RETIRED (not enabled), the owner's own account and an integration
bot.  Expectations come from the GB table (identities.gb_identities()) and the seat registry, never
from a literal list of personas.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from agent_sync import identities as X
from agent_sync.mcp import identity_router as R
from agent_sync.mcp import tools as T
from agent_sync.tests.harness import TAG, write_rc
from agent_sync.tests.mcp_harness import McpHarness, seat_bearer_locator, zulip_shaped

ENABLED = X.gb_identities()
PERSONA = ENABLED[1]  # any enabled persona works;  the table decides which
PERSONA_EMAIL = "%s-grok-bot@zulip.test" % PERSONA[len("GB-") :].lower()


class IdentityHarness(McpHarness):
    def setUp(self) -> None:
        super().setUp()
        # Every bot's keys sit in the fake Infisical /zulip (AFC-359:  no credential file is read).
        self.keys: dict[str, str] = {}
        bots = {
            "CURSOR": ("cursor-bot@zulip.test", "Cursor"),
            "BF-PLUMBER": ("bf-plumber-bot@zulip.test", "BF Plumber"),
            PERSONA: (PERSONA_EMAIL, PERSONA.title()),
            "GB-RETIRED": ("retired-grok-bot@zulip.test", "Retired"),
            "SENTRY": ("sentry-bot@zulip.test", "Sentry"),
        }
        for name, (email, full_name) in bots.items():
            key = zulip_shaped()
            self.keys[name] = key
            self.extra_secrets.append(key)
            self.fake.add_bot(email, full_name, key)
            self.add_infisical_bot(name, email=email, key=key)
        # The owner's own account and the realm-admin bot:  in /zulip-shaped names, never in any set.
        for prefix, email in (
            ("JAY", "jay@zulip.test"),
            ("REALM_ADMIN", "realm-admin-bot@zulip.test"),
        ):
            key = zulip_shaped()
            self.extra_secrets.append(key)
            self.infisical_values.update(
                {
                    "%s_ZULIP_EMAIL" % prefix: email,
                    "%s_ZULIP_KEY" % prefix: key,
                    "%s_ZULIP_SITE" % prefix: self.fake.url,
                }
            )
        self.expected = sorted(
            {"CLAUDE", "CURSOR", "BF-PLUMBER"} | ({PERSONA, "GB-RETIRED"} & set(ENABLED))
        )
        # board proves its seat from the bearer at SIMPLE_BOARD_BEARER_LOCATOR and never reads AGENT_SEAT,
        # so every seat this suite acts as gets its own locator in the installed listener.toml.  Without
        # one a Linear write is refused before board runs.
        self.pin_seat_bearers(
            {
                seat: seat_bearer_locator(seat)
                for seat in ("CLAUDE", "CURSOR", "BF-PLUMBER", PERSONA)
            }
        )

    def error(self, result: dict) -> dict:
        self.assertIs(result.get("isError"), True, result)
        return json.loads(result["content"][0]["text"])


class AllowedSetTests(IdentityHarness):
    def test_identities_lists_every_agent_bot_with_credentials_in_infisical(self):
        session = self.session()
        info = self.assert_structured("identities", session.call("identities"))
        names = [row["identity"] for row in info["identities"]]
        self.assertEqual(names, self.expected)
        self.assertEqual(info["default_identity"], "CLAUDE")
        self.assertEqual(info["transport"], "stdio")
        # Row 4:  GB-RETIRED is absent while the table leaves it out;  the owner and integration bots never enter.
        self.assertEqual("GB-RETIRED" in names, "GB-RETIRED" in ENABLED)
        self.assertNotIn("GB-RETIRED", ENABLED)
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
            self.assertIs(
                "identity" in (tool["inputSchema"].get("required") or []),
                name in T.WRITE_TOOLS,
                name,
            )

    def test_a_launcher_pin_narrows_the_set_to_its_seat(self):
        env = self.mcp_env(AGENT_SEAT=None, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="CLAUDE")
        session = self.session(env)
        info = self.assert_structured("identities", session.call("identities"))
        self.assertEqual([row["identity"] for row in info["identities"]], ["CLAUDE"])
        error = self.error(
            session.call("zulip_post", {"topic": "t", "text": "x", "identity": "CURSOR"})
        )
        self.assertEqual(error["code"], "identity_not_allowed")
        self.assertIn("launcher", error["message"])
        self.assertEqual(self.fake.messages, [])


class CallRuleTests(IdentityHarness):
    def test_copied_config_acts_as_the_named_identity_with_its_own_key(self):
        """Row 8:  a config with --as CLAUDE and a write naming CURSOR posts as CURSOR, with CURSOR's key;
        a write with no identity is refused."""
        session = self.session(self.mcp_env(AGENT_SEAT=None), argv=("--as", "CLAUDE"))
        refused = self.error(
            session.call("zulip_post", {"topic": "copied", "text": "no identity"}, identity=None)
        )
        self.assertEqual(refused["code"], "identity_required")
        self.assertEqual(self.fake.messages, [])
        posted = self.assert_structured(
            "zulip_post",
            session.call("zulip_post", {"topic": "copied", "text": "hello"}, identity="CURSOR"),
        )
        message = next(m for m in self.fake.messages if m["id"] == posted["id"])
        self.assertEqual(message["sender_email"], "cursor-bot@zulip.test")
        self.assertTrue(message["content"].startswith("[CURSOR·%s] " % TAG), message["content"])
        who = self.assert_structured(
            "whoami", session.call("whoami", {"identity": "cursor"})
        )  # aliases resolve
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
        self.assertEqual(
            self.assert_structured("whoami", session.call("whoami", {"identity": PERSONA}))["seat"],
            PERSONA,
        )

    def test_an_identity_outside_the_set_is_refused(self):
        session = self.session()
        for name in ("SENTRY", "JAY", "REALM-ADMIN", "GB-RETIRED", "MONET", "not a seat", 7):
            with self.subTest(name=name):
                error = self.error(session.call("zulip_topics", {"identity": name}))
                self.assertEqual(error["code"], "identity_not_allowed")
                self.assertEqual(error["allowed"], self.expected)
        self.assertEqual(self.fake.messages, [])

    def test_a_credential_that_is_not_the_claimed_bot_is_refused_on_first_use(self):
        # Cursor's Infisical names hold BF-PLUMBER's key:  check_bot refuses it, and nothing is posted.
        self.add_infisical_bot(
            "CURSOR", email="bf-plumber-bot@zulip.test", key=self.keys["BF-PLUMBER"]
        )
        session = self.session()
        error = self.error(
            session.call("zulip_post", {"topic": "t", "text": "x"}, identity="CURSOR")
        )
        self.assertEqual(error["code"], "not_authorized")
        self.assertEqual(self.fake.messages, [])

    def test_spacing_is_per_identity(self):
        session = self.session()
        self.assert_structured(
            "zulip_post", session.call("zulip_post", {"topic": "a", "text": "one"})
        )
        self.assert_structured(
            "zulip_post",
            session.call("zulip_post", {"topic": "a", "text": "two"}, identity="BF-PLUMBER"),
        )
        self.assertEqual([s for s in self.clock.sleeps if s > 0], [])  # two bots, two slots
        self.assert_structured(
            "zulip_post", session.call("zulip_post", {"topic": "a", "text": "three"})
        )
        self.assertGreater(sum(self.clock.sleeps), 0)  # the same bot again waits for its slot

    def test_writes_are_audited_with_the_claimed_identity(self):
        session = self.session()
        posted = self.assert_structured(
            "zulip_post",
            session.call("zulip_post", {"topic": "a", "text": "body"}, identity=PERSONA),
        )
        self.error(session.call("zulip_post", {"topic": "a", "text": "body"}, identity=None))
        self.error(session.call("zulip_post", {"topic": "a", "text": "body"}, identity="SENTRY"))
        session.call("zulip_topics")  # reads are not audited
        rows = [
            json.loads(line)
            for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [
                (r["default_identity"], r["identity"], r["tool"], r["outcome"], r["message_id"])
                for r in rows
            ],
            [
                ("CLAUDE", PERSONA, "zulip_post", "ok", posted["id"]),
                ("CLAUDE", None, "zulip_post", "identity_required", None),
                ("CLAUDE", "SENTRY", "zulip_post", "identity_not_allowed", None),
            ],
        )
        self.assertEqual(rows[0]["client"], {"name": "test-client", "version": "1"})
        self.assertNotIn("body", json.dumps(rows))

    def test_an_interrupted_write_is_audited_as_interrupted_and_a_read_is_not(self):
        session = self.session()

        def interrupted(*_a, **_k):
            raise KeyboardInterrupt()

        with mock.patch.object(T.CLI, "cmd_post", interrupted):
            with self.assertRaises(AssertionError, msg="the interrupt ends the server"):
                session.call("zulip_post", {"topic": "a", "text": "body"}, identity=PERSONA)
        rows = [
            json.loads(line)
            for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(r["default_identity"], r["identity"], r["tool"], r["outcome"]) for r in rows],
            [("CLAUDE", PERSONA, "zulip_post", "interrupted")],
        )
        self.assertNotIn("body", json.dumps(rows))

    def test_a_removed_name_names_its_replacement(self):
        session = self.session()
        for old, new in R.renamed_tools().items():
            with self.subTest(old):
                error = self.error(session.call(old, {}))
                self.assertEqual((error["code"], error["replacement"]), ("unknown_tool", new))
        self.assertIn(
            "error", session.request("tools/call", {"name": "no_such_tool", "arguments": {}})
        )

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
    """The CLI side:  `simple-chat identities` lists the same set, and every write is audited."""

    def cli_env(self, **overrides: str | None) -> dict[str, str]:
        return self.env(
            AGENT_SYNC_SECRETS_DIR=None, **overrides
        )  # the files under HOME, like the server

    def test_identities_lists_the_same_set(self):
        result = self.run_cli("identities", "--json", env=self.cli_env())
        self.assertEqual(result.code, 0, result.err)
        info = json.loads(result.out)
        self.assertEqual([row["identity"] for row in info["identities"]], self.expected)
        self.assertEqual(info["default_identity"], "CLAUDE")
        pinned = self.run_cli(
            "identities",
            "--json",
            env=self.cli_env(
                AGENT_SEAT=None, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="CLAUDE"
            ),
        )
        self.assertEqual(
            [row["identity"] for row in json.loads(pinned.out)["identities"]], ["CLAUDE"]
        )

    def test_identities_includes_the_defaults_explicit_credential_outside_the_secrets_directory(
        self,
    ):
        elsewhere = self.tmp / "elsewhere.rc"
        write_rc(elsewhere, email="codex-bot@zulip.test", key=zulip_shaped(), site=self.fake.url)
        listed = self.run_cli(
            "identities", "--json", env=self.cli_env(AGENT_SEAT="CODEX", ZULIP_RC=str(elsewhere))
        )
        self.assertEqual(listed.code, 0, listed.err)
        info = json.loads(listed.out)
        self.assertEqual(info["default_identity"], "CODEX")
        self.assertEqual(
            [row["identity"] for row in info["identities"]], sorted([*self.expected, "CODEX"])
        )
        # The same exclusions as at stdio startup:  an integration bot or the owner's account never enters.
        for never in ("SENTRY", "JAY", "REALM-ADMIN"):
            refused = self.run_cli(
                "identities", "--json", env=self.cli_env(AGENT_SEAT=never, ZULIP_RC=str(elsewhere))
            )
            self.assertNotIn(
                never, [row["identity"] for row in json.loads(refused.out)["identities"]], never
            )
        plain = self.run_cli("identities", "--json", env=self.cli_env(AGENT_SEAT="CODEX"))
        self.assertNotIn(
            "CODEX",
            [row["identity"] for row in json.loads(plain.out)["identities"]],
            "no rc, no entry",
        )

    def audit_after(self, raiser, *argv):
        """Run `post` with a command that verifies the key and then does `raiser`;  the audit rows it left."""
        from agent_sync import cli as CLI

        def command(agent, args):
            agent.verify()
            return raiser(agent, args)

        path = self.state_dir / "logs" / "identity.jsonl"
        before = path.read_text().splitlines() if path.exists() else []
        with mock.patch.dict(CLI.COMMANDS, {"post": command}):
            result = self.run_cli("post", "--topic", "cli", "x", *argv, env=self.cli_env())
        rows = (
            [json.loads(line) for line in path.read_text().splitlines()[len(before) :]]
            if path.exists()
            else []
        )
        return result, rows

    def test_a_write_that_fails_unexpectedly_after_it_landed_is_audited_once_with_only_the_exception_type(
        self,
    ):
        for exc in (
            UnicodeEncodeError(
                "ascii", "caf\u00e9 secret-looking text", 3, 4, "ordinal not in range"
            ),
            OSError(28, "No space left on device /private/path"),
        ):
            with self.subTest(type(exc).__name__):

                def boom(agent, args, exc=exc):
                    raise exc

                result, rows = self.audit_after(boom)
                self.assertEqual(result.code, 1)
                self.assertEqual(
                    [(r["tool"], r["outcome"]) for r in rows],
                    [("post", "outcome_unknown:%s" % type(exc).__name__)],
                )
                self.assertNotIn("secret-looking", json.dumps(rows))
                self.assertNotIn("/private/path", json.dumps(rows))

    def test_every_exit_of_a_verified_write_yields_exactly_one_audit_row(self):
        from agent_sync import zulip as Z

        def ok(agent, args):
            return 0

        def agent_error(agent, args):
            raise Z.UsageError("bad argument")

        def interrupted(agent, args):
            raise KeyboardInterrupt()

        def broken_pipe(agent, args):
            raise BrokenPipeError()

        cases = {
            "ok": (ok, 0, "ok"),
            "agent_sync_error": (agent_error, 2, "exit_2"),
            "interrupt": (interrupted, 130, "interrupted"),
            "broken_pipe": (broken_pipe, 0, "ok"),
        }
        for name, (raiser, code, outcome) in cases.items():
            with self.subTest(name):
                result, rows = self.audit_after(raiser)
                self.assertEqual(result.code, code, result.err)
                self.assertEqual(
                    [(r["tool"], r["outcome"]) for r in rows],
                    [("post", outcome)],
                    "exactly one row",
                )

    def test_a_failure_before_the_key_is_verified_leaves_no_audit_row(self):
        def never_verified(agent, args):
            raise OSError("before anything was sent")

        from agent_sync import cli as CLI

        path = self.state_dir / "logs" / "identity.jsonl"
        before = path.read_text().splitlines() if path.exists() else []
        with mock.patch.dict(CLI.COMMANDS, {"post": never_verified}):
            self.assertEqual(
                self.run_cli("post", "--topic", "cli", "x", env=self.cli_env()).code, 1
            )
        self.assertEqual(path.read_text().splitlines() if path.exists() else [], before)

    def test_a_write_that_lands_and_then_loses_its_pipe_is_still_audited(self):
        from agent_sync import cli as CLI

        def lands_then_pipe_breaks(agent, args):
            agent.verify()
            raise BrokenPipeError()

        with mock.patch.dict(
            CLI.COMMANDS, {"post": lands_then_pipe_breaks, "topics": lands_then_pipe_breaks}
        ):
            post = self.run_cli("post", "--topic", "cli", "x", env=self.cli_env())
            read = self.run_cli("topics", env=self.cli_env())
        self.assertEqual((post.code, read.code), (0, 0))
        rows = [
            json.loads(line)
            for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(r["tool"], r["outcome"]) for r in rows], [("post", "ok")], "the read leaves no row"
        )

    def test_cli_writes_are_audited_and_reads_are_not(self):
        posted = self.run_cli(
            "post", "--as", "CURSOR", "--topic", "cli", "hello", env=self.cli_env(AGENT_SEAT=None)
        )
        self.assertEqual(posted.code, 0, posted.err)
        self.assertEqual(self.run_cli("topics", env=self.cli_env()).code, 0)
        rows = [
            json.loads(line)
            for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(r["transport"], r["identity"], r["tool"], r["outcome"]) for r in rows],
            [("cli", "CURSOR", "post", "ok")],
        )
        self.assertNotIn("hello", json.dumps(rows))

    def test_a_mutating_api_call_is_audited_with_its_method_and_path_and_never_its_body(self):
        mid = self.fake.add_message("Codex", "agent-sync", "t", "hi")
        env = self.cli_env()
        self.assertEqual(self.run_cli("api", "GET", "messages/%d/history" % mid, env=env).code, 0)
        posted = self.run_cli(
            "api", "POST", "messages/%d/reactions" % mid, "--param", "emoji_name=eyes", env=env
        )
        self.assertEqual(posted.code, 0, posted.err)
        failed = self.run_cli("api", "DELETE", "messages/999999", env=env)
        self.assertNotEqual(failed.code, 0)
        rows = [
            json.loads(line)
            for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(r["tool"], r["outcome"]) for r in rows],
            [
                ("api POST messages/%d/reactions" % mid, "ok"),
                ("api DELETE messages/999999", "exit_%d" % failed.code),
            ],
        )
        self.assertNotIn("eyes", json.dumps(rows), "the body is never logged")

    def test_an_interrupted_generic_write_is_audited_as_interrupted_and_exits_130(self):
        from agent_sync import cli as CLI

        def interrupted(agent, args):
            agent.verify()  # the key is checked before any request, so a write may be in flight
            raise KeyboardInterrupt()

        env = self.cli_env()
        with mock.patch.dict(
            CLI.COMMANDS, {"post": interrupted, "api": interrupted, "topics": interrupted}
        ):
            post = self.run_cli("post", "--topic", "cli", "x", env=env)
            api_write = self.run_cli(
                "api", "POST", "messages/1/reactions", "--param", "emoji_name=eyes", env=env
            )
            read = self.run_cli("topics", env=env)  # a read leaves no row
        self.assertEqual((post.code, api_write.code, read.code), (130, 130, 130))
        rows = [
            json.loads(line)
            for line in (self.state_dir / "logs" / "identity.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            [(r["tool"], r["outcome"]) for r in rows],
            [("post", "interrupted"), ("api POST messages/1/reactions", "interrupted")],
        )

    def test_an_unreadable_registry_refuses_every_identity_on_the_cli_naming_the_file(self):
        missing = self.cli_env(AGENT_SEAT=None, FLEET_APPS_JSON="/nonexistent/fleet-apps.json")
        for argv in (
            ("post", "--as", "SENTRY", "--topic", "cli", "x"),
            ("post", "--as", "CURSOR", "--topic", "cli", "x"),
            ("topics", "--as", "CLAUDE"),
        ):
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
        for name in ("SENTRY", "GB-RETIRED"):
            with self.subTest(name):
                refused = self.run_cli(
                    "post", "--as", name, "--topic", "cli", "x", env=self.cli_env(AGENT_SEAT=None)
                )
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
        bots = {
            "GROK-WEB": ("grok-web-bot@zulip.test", "Grok Web"),
            PERSONA: (PERSONA_EMAIL, PERSONA.title()),
            "BF-PLUMBER": ("bf-plumber-bot@zulip.test", "BF Plumber"),
            "SENTRY": ("sentry-bot@zulip.test", "Sentry"),
            "GB-RETIRED": ("retired-grok-bot@zulip.test", "Retired"),
        }
        for name, (email, full_name) in bots.items():
            key = zulip_shaped()
            self.extra_secrets.append(key)
            self.fake.add_bot(email, full_name, key)
            code = X.env_code(name)
            self.vars["ZULIP_%s_EMAIL" % code] = email
            self.vars["ZULIP_%s_API_KEY" % code] = (
                '"%s"' % key
            )  # quote-wrapped, as the synced values are
        self.env_only = sorted({"GROK-WEB", PERSONA, "BF-PLUMBER"})

    def server_env(self, **overrides: str | None) -> dict[str, str]:
        return {**self.mcp_env(**overrides), **self.vars}

    def error(self, result: dict) -> dict:
        self.assertIs(result.get("isError"), True, result)
        return json.loads(result["content"][0]["text"])

    def test_a_host_with_only_env_backed_seats(self):
        self.drop_infisical_bot("CLAUDE")  # no Infisical credential at all
        session = self.session(self.server_env(AGENT_SEAT=None))
        info = self.assert_structured("identities", session.call("identities"))
        self.assertEqual([row["identity"] for row in info["identities"]], self.env_only)
        self.assertIsNone(info["default_identity"])
        posted = self.assert_structured(
            "zulip_post",
            session.call("zulip_post", {"topic": "env", "text": "hi"}, identity=PERSONA),
        )
        message = next(m for m in self.fake.messages if m["id"] == posted["id"])
        self.assertEqual(message["sender_email"], PERSONA_EMAIL)
        who = self.assert_structured("whoami", session.call("whoami", {"identity": "GROK-WEB"}))
        self.assertEqual((who["seat"], who["email"]), ("GROK-WEB", "grok-web-bot@zulip.test"))

    def test_a_mixed_host_lists_files_and_env_seats(self):
        session = self.session(self.server_env())
        info = self.assert_structured("identities", session.call("identities"))
        self.assertEqual(
            [row["identity"] for row in info["identities"]], sorted(["CLAUDE", *self.env_only])
        )
        self.assertEqual(self.assert_structured("whoami", session.call("whoami"))["seat"], "CLAUDE")
        self.assertEqual(
            self.assert_structured("whoami", session.call("whoami", {"identity": "BF-PLUMBER"}))[
                "seat"
            ],
            "BF-PLUMBER",
        )

    def test_as_names_an_env_backed_seat(self):
        self.drop_infisical_bot("CLAUDE")
        call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        }
        code, lines, stderr = self.spawn(
            self.server_env(AGENT_SEAT=None), [call], argv=("--as", "GROK-WEB")
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(lines[0]["result"]["structuredContent"]["seat"], "GROK-WEB")
        self.assertIn("serving seat GROK-WEB", stderr)
        for value in self.extra_secrets:
            self.assertNotIn(value, stderr)

    def test_an_integration_bot_or_disabled_persona_in_env_is_never_an_identity(self):
        session = self.session(self.server_env())
        names = [
            row["identity"]
            for row in self.assert_structured("identities", session.call("identities"))[
                "identities"
            ]
        ]
        for never in ("SENTRY", "GB-RETIRED"):
            self.assertNotIn(never, names)
            self.assertEqual(
                self.error(session.call("zulip_topics", {"identity": never}))["code"],
                "identity_not_allowed",
            )
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        code, lines, stderr = self.spawn(
            self.server_env(AGENT_SEAT=None), [ping], argv=("--as", "SENTRY")
        )
        self.assertEqual((code, lines), (3, []))
        self.assertIn("SENTRY is not an agent bot identity", stderr)
        self.assertEqual(self.fake.messages, [])

    def test_an_unreadable_seat_registry_serves_no_identity_not_even_the_default(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        for argv in (
            (),
            ("--as", "MONET"),
        ):  # the ambient default, and a retired seat that has a credential file
            code, lines, stderr = self.spawn(
                self.server_env(FLEET_APPS_JSON="/nonexistent/fleet-apps.json"), [ping], argv=argv
            )
            self.assertEqual((code, lines), (3, []), stderr)
            self.assertIn("seat registry", stderr)
            self.assertIn("none is served", stderr)
        self.assertEqual(self.fake.messages, [])

    def test_a_key_in_another_seats_variable_is_refused_on_first_use(self):
        self.vars["ZULIP_GROK_WEB_EMAIL"] = "bf-plumber-bot@zulip.test"
        self.vars["ZULIP_GROK_WEB_API_KEY"] = self.vars["ZULIP_BF_PLUMBER_API_KEY"]
        session = self.session(self.server_env())
        error = self.error(
            session.call("zulip_post", {"topic": "t", "text": "x"}, identity="GROK-WEB")
        )
        self.assertEqual(error["code"], "not_authorized")
        self.assertIn("env ZULIP_GROK_WEB_API_KEY", error["message"])
        self.assertEqual(self.fake.messages, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
