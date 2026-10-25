"""Named fleet seat aliases resolve to their canonical keys.

The rule, as the docs state it:  GROK followed by BUILD, any letter case, with any non-letter, non-digit
symbols between or after (GROK-BUILD, Grok (Build), grok_build), is an alias for GROK.  One pattern,
`^grok[^a-z0-9]*build[^a-z0-9]*$`, matched case-insensitively after trimming.  Every input boundary
canonicalizes it to GROK (--as, --to, whoami, AGENT_SEAT, AGENT_LAUNCH_SEAT, AGENT_TAG, the hooks' platform
default), and everything the tools write (labels, state, credentials) is GROK's:  `[GROK·...]`, state/GROK, and
Grok-Build-zuliprc, the GROK seat's own credential.  WISE and MA accept their published aliases."""
from __future__ import annotations

import copy
import importlib.util
import json
import secrets
import unittest
from pathlib import Path

from agent_sync import identity as I
from agent_sync import zulip as Z
from agent_sync.tests.harness import TAG, Harness, write_rc

REPO = Path(__file__).resolve().parents[3]
GROK_EMAIL = "grok-build-bot@zulip.test"

# What matches (the owner's examples, plus trimming).
ALIASES = ("GROK-BUILD", "Grok-Build", "grok-build", "grok_build", "Grok Build", "GROK.BUILD", "Grok (Build)",
           "grok/build/", "GrokBuild", "GROK--BUILD!!", "  grok build  ")
# What must not:  other seats, near misses, a bot email (matched separately), the retired tags with no alias.
NOT_ALIASES = ("GROKBUILDER", "grok-build-bot", "grok2build", "GROK-WEB", "GROK-BOT", "GB-COMPILER", "BF-BUILDER",
               "Grok (Web/iOS)", "MONET", "RENOIR", "HARNESS", "MINIMAX", "DEEPSEEK", "GROK", "")


class PatternTests(unittest.TestCase):
    def test_every_spelling_is_the_grok_seat(self) -> None:
        for raw in ALIASES:
            with self.subTest(raw=raw):
                self.assertTrue(I.is_grok_alias(raw))
                self.assertEqual(I.canonical_seat(raw), "GROK")
                self.assertEqual(Z.normalise_seat(raw), "GROK")
                self.assertEqual(I.valid_seat(raw), "GROK")

    def test_near_misses_and_other_seats_are_not_aliases(self) -> None:
        for raw in NOT_ALIASES:
            with self.subTest(raw=raw):
                self.assertFalse(I.is_grok_alias(raw))
                self.assertEqual(I.canonical_seat(raw), raw.strip().upper())

    def test_named_aliases_resolve_to_wise_and_muse_assist(self) -> None:
        for alias, canonical in (("Wise-Instinct", "WISE"), ("Echo-Instinct", "WISE"),
                                 ("Instinct", "WISE"), ("Muse-Assist", "MA"), ("Rob", "MA"),
                                 ("Rob-Muse", "MA")):
            with self.subTest(alias=alias):
                self.assertEqual(I.canonical_seat(alias), canonical)
                self.assertEqual(Z.normalise_seat(alias), canonical)

    def test_a_non_alias_keeps_the_strict_token_check(self) -> None:
        for raw in ("Grok (Web/iOS)", "", "a b", "../x"):
            with self.subTest(raw=raw):
                with self.assertRaises(Z.UsageError):
                    Z.normalise_seat(raw)
        self.assertEqual(Z.normalise_seat("grokbuilder"), "GROKBUILDER")  # a token, so unknown later, at the credential
        self.assertIsNone(I.valid_seat("Grok (Web/iOS)"))

    def test_the_pattern_only_counts_ascii_letters(self) -> None:
        # Under Unicode case folding a Kelvin sign (U+212A) matches k, so a different name would be an alias;
        # the ASCII flag keeps the letters ASCII.  Any other non-ASCII character is a symbol, like a dash.
        self.assertFalse(I.is_grok_alias("GRO\u212a-BUILD"))
        self.assertFalse(I.is_grok_alias("grok-bu\u00efld"))
        self.assertTrue(I.is_grok_alias("grok\u2014build\u2014"))  # an em dash is a symbol

    def test_the_registry_carries_the_grok_retired_tag_pattern(self) -> None:
        seats = json.loads((REPO / "fleet-apps.json").read_text(encoding="utf-8"))["seats"]
        carrying = {s["tag"]: s["aliasPattern"] for s in seats if "aliasPattern" in s}
        self.assertEqual(carrying, {"GROK": I.GROK_ALIAS_PATTERN})
        tags = [s["tag"] for s in seats]
        self.assertIn("GROK-BUILD", tags, "the retired row is what the pattern aliases")
        for tag in tags:
            if tag != "GROK-BUILD":
                self.assertFalse(I.is_grok_alias(tag), "%s must not match the alias pattern" % tag)


class ResolveTests(unittest.TestCase):
    def test_every_name_source_canonicalizes(self) -> None:
        for raw in ALIASES:
            with self.subTest(raw=raw):
                self.assertEqual(I.resolve_seat({}, flag=raw).seat, "GROK")
                self.assertEqual(I.resolve_seat({"AGENT_SEAT": raw}).seat, "GROK")
                self.assertEqual(I.resolve_seat({"AGENT_TAG": raw}).seat, "GROK")
                self.assertEqual(I.resolve_seat({}, default=raw).seat, "GROK")

    def test_a_launcher_seat_and_an_alias_are_one_seat_not_a_conflict(self) -> None:
        for launch, named in (("GROK", "Grok-Build"), ("GROK-BUILD", "GROK"), ("Grok (Build)", "grok_build")):
            with self.subTest(launch=launch, named=named):
                got = I.resolve_seat({"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": launch, "AGENT_SEAT": named},
                                     flag=named)
                self.assertEqual((got.seat, got.source, got.problem), ("GROK", "launcher", None))

    def test_a_launcher_seat_still_refuses_another_seat(self) -> None:
        for launch, named in (("GROK-BUILD", "CODEX"), ("GROK", "GROKBUILDER"), ("CODEX", "Grok-Build")):
            with self.subTest(launch=launch, named=named):
                got = I.resolve_seat({"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": launch}, flag=named)
                self.assertIsNone(got.seat)
                self.assertIn("a launched session keeps its launcher's seat", got.problem)

    def test_the_other_retired_tags_get_no_alias(self) -> None:
        for raw in ("MONET", "RENOIR", "HARNESS", "MINIMAX", "DEEPSEEK", "KIMI", "DSH"):
            with self.subTest(raw=raw):
                self.assertEqual(I.resolve_seat({"AGENT_SEAT": raw}).seat, raw)


class CliAliasTests(Harness):
    """The commands, end to end, against the fake server.  The GROK bot has its own credential file,
    Grok-Build-zuliprc, and the default Claude credential sits beside it:  an alias must pick GROK's."""

    def setUp(self) -> None:
        super().setUp()
        self.fake.users.remove(self.fake.user_named("Grok"))  # the harness's stand-in grok-bot@ also signs GROK
        self.grok_key = secrets.token_hex(16)
        self.fake.add_bot(GROK_EMAIL, "Grok", self.grok_key)
        self.grok_rc = write_rc(self.secrets_dir / "Grok-Build-zuliprc", email=GROK_EMAIL, key=self.grok_key,
                                site=self.fake.url)

    def tearDown(self) -> None:
        super().tearDown()
        self.assertNotIn(self.grok_key, "\n".join(self.transcript), "the GROK key reached the output")

    def post(self, *extra: str, env: dict[str, str]):
        return self.run_cli("post", "--topic", "alias test", *extra, "hello", env=env)

    def assert_posted_as_grok(self, result) -> None:
        self.assertEqual(result.code, 0, result.err)
        message = self.fake.messages[-1]
        self.assertEqual(message["sender_email"], GROK_EMAIL, "GROK's own credential, never another seat's")
        self.assertTrue(message["content"].startswith("[GROK·%s]" % TAG), message["content"])

    def test_as_in_every_spelling_posts_as_grok_with_groks_credential(self) -> None:
        for raw in ALIASES:
            with self.subTest(raw=raw):
                self.assert_posted_as_grok(self.post("--as", raw, env=self.env(AGENT_SEAT=None)))
        self.assertTrue((self.state_dir / "GROK").exists(), "state is GROK's")
        self.assertFalse((self.state_dir / "GROK-BUILD").exists())

    def test_the_environment_in_every_spelling_posts_as_grok(self) -> None:
        for raw in ALIASES:
            with self.subTest(AGENT_SEAT=raw):
                self.assert_posted_as_grok(self.post(env=self.env(AGENT_SEAT=raw)))
            with self.subTest(AGENT_TAG=raw):
                self.assert_posted_as_grok(self.post(env=self.env(AGENT_SEAT=None, AGENT_TAG=raw)))
            with self.subTest(default=raw):
                self.assert_posted_as_grok(self.post("--default-seat", raw, env=self.env(AGENT_SEAT=None)))

    def test_a_launched_grok_may_name_itself_either_way(self) -> None:
        for launch, named in (("GROK", "GROK-BUILD"), ("Grok-Build", "GROK"), ("GROK-BUILD", "Grok (Build)")):
            with self.subTest(launch=launch, named=named):
                env = self.env(AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT=launch, AGENT_SEAT=named)
                self.assert_posted_as_grok(self.post(env=env))
        refused = self.post(env=self.env(AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="Grok-Build", AGENT_SEAT="CODEX"))
        self.assertEqual(refused.code, 3, refused.err)

    def test_whoami_names_the_grok_seat(self) -> None:
        for raw in ALIASES:
            with self.subTest(raw=raw):
                result = self.run_cli("whoami", "--json", "--as", raw, env=self.env(AGENT_SEAT=None))
                self.assertEqual(result.code, 0, result.err)
                info = json.loads(result.out)
                self.assertEqual((info["seat"], info["bot_seat"], info["verified"]), ("GROK", "GROK", True))

    def test_an_alias_never_borrows_another_seats_credential(self) -> None:
        # Claude's file named explicitly is refused as not GROK's bot ...
        result = self.post("--as", "GROK-BUILD", env=self.env(AGENT_SEAT=None, ZULIP_RC=str(self.rc_path)))
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("not GROK", result.err)
        # ... and with GROK's file gone, with Claude's right there, the alias fails instead of falling back.
        self.grok_rc.unlink()
        for raw in ("GROK-BUILD", "Grok (Build)"):
            with self.subTest(raw=raw):
                before = len(self.fake.messages)
                result = self.post("--as", raw, env=self.env(AGENT_SEAT=None))
                self.assertNotEqual(result.code, 0)
                self.assertEqual(len(self.fake.messages), before)

    def test_a_name_that_is_not_an_alias_is_refused(self) -> None:
        for raw in ("GROKBUILDER", "grok2build", "grok-build-bot", "Grok (Web/iOS)"):
            with self.subTest(raw=raw):
                before = len(self.fake.messages)
                result = self.post("--as", raw, env=self.env(AGENT_SEAT=None))
                self.assertNotEqual(result.code, 0, raw)
                self.assertEqual(len(self.fake.messages), before)

    def test_to_resolves_every_spelling_the_display_name_and_the_email_to_grok(self) -> None:
        for raw in ALIASES + ("GROK", "Grok", "grok", GROK_EMAIL):
            with self.subTest(raw=raw):
                result = self.run_cli("post", "--topic", "alias test", "--to", raw, "hi")
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(self.fake.messages[-1]["content"], "[CLAUDE·%s→GROK] @**Grok** hi" % TAG)

    def test_to_still_refuses_a_name_that_is_nobody(self) -> None:
        for raw in ("GROKBUILDER", "grok2build", "grok-build-bot", "NOSUCHSEAT", "GROK-WEB"):
            with self.subTest(raw=raw):
                before = len(self.fake.messages)
                result = self.run_cli("post", "--topic", "alias test", "--to", raw, "hi")
                self.assertEqual(result.code, 2, result.err)
                self.assertEqual(len(self.fake.messages), before)

    def test_dm_to_takes_the_alias_too(self) -> None:
        #  The owner is in the conversation, so this is not a bot to bot direct message (AFC-469).
        self.pin_owner()
        result = self.run_cli("dm", "--owner", "--to", "Grok-Build", "--json", "ping")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.fake.messages[-1]["type"], "private")


class RegistryCheckTests(unittest.TestCase):
    """scripts/check-fleet-registry.py (CI) guards the pattern the lane tool and the board scripts read."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("check_fleet_registry", REPO / "scripts" / "check-fleet-registry.py")
        cls.check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.check)
        cls.seats = json.loads((REPO / "fleet-apps.json").read_text(encoding="utf-8"))["seats"]

    def errors_with(self, pattern) -> list:
        seats = copy.deepcopy(self.seats)
        for seat in seats:
            if seat["tag"] == "GROK":
                seat["aliasPattern"] = pattern
        return self.check.alias_pattern_errors(seats)

    def test_the_registry_as_committed_is_clean(self) -> None:
        self.assertEqual(self.check.alias_pattern_errors(self.seats), [])

    def test_a_pattern_that_does_not_compile_is_reported(self) -> None:
        self.assertTrue(any("does not compile" in e for e in self.errors_with("(")))

    def test_a_pattern_that_differs_from_the_cli_copy_is_reported(self) -> None:
        self.assertTrue(any("disagrees with" in e for e in self.errors_with(r"^grok[^a-z0-9]*build[^a-z0-9]*")))

    def test_a_pattern_that_is_too_wide_is_reported(self) -> None:
        errors = self.errors_with(r"^grok.*$")
        self.assertTrue(any("matches the tag GROK-WEB" in e for e in errors), errors)

    def test_a_pattern_on_a_retired_row_or_a_seat_with_no_alias_row_is_reported(self) -> None:
        seats = copy.deepcopy(self.seats)
        for seat in seats:
            if seat["tag"] == "CODEX":
                seat["aliasPattern"] = I.GROK_ALIAS_PATTERN
        self.assertTrue(any("aliases nothing" in e for e in self.check.alias_pattern_errors(seats)))


if __name__ == "__main__":
    unittest.main()
