"""`platform-letter` (AFC-564): Operator session letters and "<letter>: <Title>" titles for the platforms other than
Claude and MiniMax.  The one assigner is the registry API, served in process over a temporary store (FakeRegistry);
the hook runs in process, and a test that counts or breaks registry calls hands it a transport that runs the same
registry side.  No test depends on which pair the registry hands out first, only on the letter being a pair that the
Operator, the note and the title agree on;  a chat that gets none is titled "??: <Title>"."""

from __future__ import annotations

import ast
import io
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import registry_api
from simple_tools.tests.registry_fake import FakeRegistry

SCRIPTS = Path(__file__).resolve().parents[2]
ENTRY = SCRIPTS / "platform-letter"
PAIR = r"^[A-Z]{2}$"
LABEL = r"^[A-Z]{1,2}$"
# Whether this checkout's registry gives a closed chat a pair again when its old pair went to another chat.  The
# registry of main before pair letters allocates a single letter there; the library refuses that (SpaceLost) and the
# chat waits for a letter, where the pair-aware registry (it takes `pick` on reopen) answers with a new pair.
REOPEN_KEEPS_SPACE = PL._accepts(OR.SessionRegistry.reopen, "pick")


def make_spec(**over) -> PL.PlatformSpec:
    base = dict(
        platform="codex",
        seat="CODEX",
        source="codex-letter-hook",
        display="Codex",
        title_in_output=True,
    )
    base.update(over)
    return PL.PlatformSpec(**base)


def secret_word() -> str:
    """A GitHub-shaped token built at run time, so the source holds no key-looking literal."""
    return "ghp_" + "A1b2C3d4" * 5


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.home)
        # The one assigner:  the registry API, served in process over a temporary store.
        self.fake = FakeRegistry(os.path.join(self.tmp.name, "assigner")).install()
        self.addCleanup(self.fake.uninstall)
        self.db = self.fake.db
        self.spec = make_spec()

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.state, **self.fake.env()}
        env.update(extra)
        return env

    def server_env(self, **extra: str) -> dict[str, str]:
        return self.env(SESSION_LETTER_LOCATION="server", **extra)

    def registry(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        code = PL.registry_main(list(argv), env=self.env(), stdout=out)
        return code, json.loads(out.getvalue())

    def reg_argv(
        self, op: str, sid: str, *, platform: str = "codex", location: str = "server", **flags: str
    ) -> list[str]:
        seats = {"codex": "CODEX", "opencode": "OPENCODE"}
        argv = [op, "--platform", platform, "--location", location, "--session-id", sid]
        argv += ["--seat", seats.get(platform, platform.upper()), "--source", platform + "-test"]
        for name, value in flags.items():
            argv.append("--%s=%s" % (name.replace("_", "-"), value))
        return argv

    def register_raw(
        self, sid: str, *, title: str = "", platform: str = "codex",
        location: str = "server", **flags: str,
    ) -> tuple[int, dict]:
        flags.setdefault("letter_pick", "lowest")
        if title:
            flags["title"] = title
        argv = self.reg_argv("register", sid, platform=platform, location=location, **flags)
        return self.registry(*argv)

    def register(self, sid: str, **kwargs) -> dict:
        return self.register_raw(sid, **kwargs)[1]

    def lookup(self, sid: str, location: str = "server", platform: str = "codex") -> dict | None:
        reg = OR.SessionRegistry(self.db)
        try:
            return reg.lookup(platform, location, PL.SCOPE, sid)
        finally:
            reg.close_db()

    def set_seen(self, sid: str, iso: str) -> None:
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE native_session_id = ?", (iso, sid))
        conn.close()


class TitleTests(unittest.TestCase):
    def test_bare_drops_only_what_it_is_told_to(self) -> None:
        self.assertEqual(PL.bare("BC: Fix Login"), "Fix Login")
        self.assertEqual(PL.bare("B: Fix Login"), "Fix Login")
        self.assertEqual(PL.bare("Fix: Login"), "Fix: Login")
        self.assertEqual(PL.bare("BC:"), "")
        # With the chat's own letters in hand an owner's acronym stays whole.
        self.assertEqual(PL.bare("PR: Review", ("BC",)), "PR: Review")
        self.assertEqual(PL.bare("BC: PR: Review", ("BC",)), "PR: Review")
        self.assertEqual(PL.bare("AI: Design", ("CC", "CD")), "AI: Design")
        self.assertEqual(PL.bare("CD: Design", ("CC", "CD")), "Design")
        self.assertEqual(PL.bare("CC:", ("CC",)), "")

    def test_compose(self) -> None:
        self.assertEqual(PL.compose("CC", "Fix  Login\n"), "CC: Fix Login")
        self.assertEqual(PL.compose("CC", ""), "CC: New Chat")

    def test_title_from_prompt(self) -> None:
        self.assertEqual(
            PL.title_from_prompt("help get all PR issues addressed from review findings."),
            "Help Get All PR Issues Addressed",
        )
        self.assertEqual(
            PL.title_from_prompt("```\nx = 1\n```\nfix the iOS build for AFC-564"),
            "Fix the iOS Build for AFC-564",
        )
        self.assertEqual(PL.title_from_prompt("see https://example.com/a/b and the docs"), "See and the Docs")
        self.assertEqual(PL.title_from_prompt("   \n"), "")
        self.assertEqual(PL.title_from_prompt("/review"), "")
        self.assertEqual(PL.title_from_prompt("/review fix the login page"), "Fix the Login Page")

    def test_secret_shaped_words_never_name_a_chat(self) -> None:
        key = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"  # Zulip-shaped: 32 mixed-case letters and digits
        self.assertEqual(PL.title_from_prompt("rotate %s today" % key), "Rotate Today")
        self.assertEqual(PL.clean_title("Rotate %s Today" % key), "Rotate Today")
        token = secret_word()
        self.assertEqual(PL.clean_title("use %s now" % token), "use now")
        self.assertNotIn(token, PL.title_from_prompt("export it as %s please" % token))

    def test_a_secret_after_its_keyword_never_names_a_chat(self) -> None:
        """The scanner's context rules need the keyword and the value together; a title is built word by word, so the
        words are scanned together too and the value is dropped."""
        value = "abcdefghijklmnop1234567"
        cases = [
            ("api" + "_key" + ": " + value + " fix login", value, "Api Key Fix Login"),
            ("my pass" + "word: " + "hunter2" * 3 + " please fix login", "hunter2" * 3, "My Password Please Fix Login"),
            ("author" + "ization: bea" + "rer " + "A1b2C3d4" * 4 + "wxyz fix it", "A1b2C3d4" * 4,
             "Authorization Bearer Fix It"),
            ("to" + "ken: " + "1234567890abcdef1234567890" + " fix", "1234567890abcdef1234567890", "Token Fix"),
            ("Linear client sec" + "ret = " + "q" * 30 + " rotate it", "q" * 30, "Linear Client Secret Rotate It"),
        ]
        for prompt, secret, title in cases:
            with self.subTest(title=title):
                got = PL.title_from_prompt(prompt)
                self.assertNotIn(secret, got)
                self.assertEqual(got, title)
        # Two of them on one line, and one split over a keyword the scanner needs six words back for.
        two = "pass" + "word: " + "x1" * 12 + " tok" + "en: " + "y2" * 12 + " fix login"
        self.assertEqual(PL.title_from_prompt(two), "Password Token Fix Login")
        self.assertEqual(PL.title_from_prompt("fix the login, " + "secret" + "=" + "z" * 20), "Fix the Login")

    def test_the_words_before_a_secret_do_not_hide_the_next_one(self) -> None:
        # A secret that was dropped is not part of what the next word is scanned with.
        a, b = "h1" * 12, "k3" * 12
        title = PL.title_from_prompt("pass" + "word: " + a + " pass" + "word: " + b)
        self.assertNotIn(a, title)
        self.assertNotIn(b, title)

    def test_titles_from_prompts_in_other_scripts(self) -> None:
        cases = {
            "Revisar la configuración de autenticación": "Revisar La Configuración De Autenticación",
            "Résumé einreichen für Müller": "Résumé Einreichen Für Müller",
            "请修复登录页面": "请修复登录页面",
            "Исправь страницу входа": "Исправь Страницу Входа",
            "fix the naïve café parser": "Fix the Naïve Café Parser",
        }
        for prompt, title in cases.items():
            with self.subTest(prompt=prompt):
                self.assertEqual(PL.title_from_prompt(prompt), title)
        self.assertEqual(PL.title_from_prompt("snake_case_name here"), "Snake Case Name Here")  # _ still breaks words

    def test_a_title_holds_no_control_characters_or_lone_surrogates(self) -> None:
        self.assertEqual(PL.clean_title("Fix \ud83d Login"), "Fix Login")
        self.assertEqual(PL.clean_title("Fix \x00 Login\x1b[0m"), "Fix Login[0m")
        self.assertEqual(PL.tidy("a\tb\nc \x00d"), "a b c d")
        self.assertEqual(PL.tidy(None), "")

    def test_clean_title_limits(self) -> None:
        self.assertLessEqual(len(PL.clean_title("word " * 30)), PL.TITLE_CHARS)
        self.assertEqual(PL.clean_title("a\nb"), "a b")
        self.assertLessEqual(len(PL.title_from_prompt("one two three four five six seven eight nine").split()), 7)

    def test_a_long_prompt_scans_a_bounded_number_of_words(self) -> None:
        calls = []
        real = PL.secretscan.scan

        def counting(text, *a, **k):
            calls.append(text)
            return real(text, *a, **k)

        with mock.patch.object(PL.secretscan, "scan", side_effect=counting):
            title = PL.title_from_prompt("token=" * 200000 + " fix login")
        self.assertLessEqual(len(calls), PL.PROMPT_SCAN_WORDS)
        self.assertIsInstance(title, str)


class SpecTests(unittest.TestCase):
    def test_a_spec_is_checked(self) -> None:
        # Claude and MiniMax are rows of the table like every other platform (owner, Nov 11, 2026).
        self.assertEqual(make_spec(platform="claude", seat="CLAUDE").platform, "claude")
        self.assertEqual(make_spec(platform="minimax", seat="MM").platform, "minimax")
        self.assertFalse(hasattr(make_spec(), "off_variable"))  # one off switch, SESSION_LETTER, for every platform
        for bad in (
            dict(platform="Codex"),
            dict(platform="x"),
            dict(seat="codex"),
            dict(seat="BAD SEAT"),
            dict(source=" "),
            dict(output="xml"),
            dict(events={"SessionStart": "begin"}),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_spec(**bad)

    def test_location(self) -> None:
        self.assertEqual(PL.location({}, "darwin"), "mac")
        self.assertIsNone(PL.location({}, "linux"))
        self.assertEqual(PL.location({PL.ENV_LOCATION: "Server"}, "linux"), "server")
        self.assertIsNone(PL.location({PL.ENV_LOCATION: "my-laptop"}, "linux"))
        self.assertIsNone(PL.location({"CLAUDE_LETTER_LOCATION": "server"}, "linux"))  # no seat-named variable
        self.assertEqual(PL.location({PL.ENV_LOCATION: "mac"}, "linux", flag="server"), "server")  # a flag wins
        self.assertIsNone(PL.location({}, "linux", flag="cloud"))

    def test_scope(self) -> None:
        spec = make_spec()
        self.assertTrue(PL.in_scope(spec, {}, {}))
        self.assertFalse(PL.in_scope(spec, {"SESSION_LETTER": "0"}, {}))
        self.assertTrue(PL.in_scope(spec, {"CODEX_LETTER": "0"}, {}))  # no per-platform switch
        # A launcher-run session never takes a platform letter: BotFleet, Grok Bot and the wake runs have their own.
        for env in (
            {"AGENT_LAUNCHER": "botfleet"},
            {"AGENT_LAUNCHER": "grok-bot"},
            {"AGENT_LAUNCHER": "simple-tools-wake"},
            {"AGENT_LAUNCH_SEAT": "BF-BUILDER"},
            {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-BUILDER"},
        ):
            with self.subTest(env=env):
                self.assertFalse(PL.in_scope(spec, env, {}))

    def test_a_spec_can_name_the_launcher_it_belongs_to(self) -> None:
        bot = make_spec(platform="botfleet", seat="BOTFLEET", launchers=("botfleet",))
        self.assertTrue(PL.in_scope(bot, {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-X"}, {}))
        self.assertFalse(PL.in_scope(bot, {"AGENT_LAUNCHER": "grok-bot"}, {}))
        self.assertFalse(PL.in_scope(bot, {"AGENT_LAUNCH_SEAT": "BF-X"}, {}))

    def test_headless_and_subagent_runs(self) -> None:
        def kind(payload, env):
            return str(payload.get("kind") or "chat")

        spec = make_spec(kind=kind, parent_id=PL.from_payload("parent_id"))
        self.assertTrue(PL.in_scope(spec, {}, {"kind": "chat"}))
        self.assertFalse(PL.in_scope(spec, {}, {"kind": "headless"}))
        self.assertFalse(PL.in_scope(spec, {}, {"kind": "subagent"}))  # no recorded parent chat
        self.assertTrue(PL.in_scope(spec, {}, {"kind": "subagent", "parent_id": "p-0001"}))
        self.assertFalse(PL.in_scope(spec, {}, {"kind": "something-new"}))

    def test_accessors(self) -> None:
        env = {"A_ID": " e-0001 ", "B_ID": ""}
        self.assertEqual(PL.from_payload("x", "y")({"x": 5, "y": " v-0001 "}, env), "v-0001")
        self.assertEqual(PL.from_payload("x")({"x": ["no"]}, env), "")
        self.assertEqual(PL.from_env("B_ID", "A_ID")({}, env), "e-0001")
        chain = PL.either(PL.from_payload("x"), PL.from_env("A_ID"))
        self.assertEqual(chain({}, env), "e-0001")
        self.assertEqual(chain({"x": "p-0001"}, env), "p-0001")

    def test_events(self) -> None:
        spec = make_spec(events={"session.created": "start", "session.deleted": "end"})
        self.assertEqual(PL.canonical_event(spec, {"hook_event_name": "session.created"}), "start")
        self.assertEqual(PL.canonical_event(spec, {"event": "session.deleted"}), "end")
        self.assertEqual(PL.canonical_event(spec, {"event": "prompt"}), "prompt")  # the library's own names pass
        self.assertEqual(PL.canonical_event(spec, {"event": "SessionStart"}), "")
        self.assertEqual(PL.canonical_event(spec, {}), "")
        self.assertEqual(PL.canonical_event(make_spec(), {"hook_event_name": "SessionStart"}), "start")

    def test_a_note_stays_under_the_state_root(self) -> None:
        env = {"SIMPLE_TOOLS_STATE_DIR": "/state"}
        self.assertEqual(PL.note_path(env, "CODEX", "s-0001"), "/state/CODEX/letters/s-0001.json")
        self.assertEqual(PL.note_path(env, "../../tmp/x", "s-0001"), "/state/UNKNOWN/letters/s-0001.json")

    def test_adapters_are_found_by_a_checked_name(self) -> None:
        spec = make_spec(platform="testbot", seat="TESTBOT")
        self.addCleanup(PL.SPECS.pop, "testbot", None)
        PL.register_spec(spec)
        self.assertIs(PL.load_spec("testbot"), spec)
        self.assertIsNone(PL.load_spec("nosuchplatform"))
        for name in ("", "../etc", "os", "Codex", "a;b"):
            self.assertIsNone(PL.load_adapter(name), name)
        for name in ("claude", "minimax"):  # rows of the one table
            self.assertEqual(PL.load_spec(name).platform, name)
        module = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"))
        with mock.patch.dict(sys.modules, {"simple_tools.platform_letter_faux": module}):
            self.assertEqual(PL.load_spec("faux").seat, "FAUX")
        wrong = types.SimpleNamespace(SPEC=make_spec(platform="other", seat="OTHER"))
        with mock.patch.dict(sys.modules, {"simple_tools.platform_letter_faux": wrong}):
            self.assertIsNone(PL.load_spec("faux"))  # an adapter names its own platform


class ContractTests(unittest.TestCase):
    """The one function that speaks `operator_registry`: it asks only for what the registry takes."""

    def test_the_pick(self) -> None:
        with_primary = SimpleNamespace(LETTER_PICKS=("lowest", "oldest", "primary"))
        self.assertEqual(PL.resolve_pick(with_primary, "auto"), "primary")
        self.assertEqual(PL.resolve_pick(SimpleNamespace(), "auto"), "lowest")
        self.assertEqual(PL.resolve_pick(with_primary, "oldest"), "oldest")

    def test_this_checkouts_registry(self) -> None:
        self.assertTrue(PL._accepts(OR.register_session, "letter_space"))
        self.assertTrue(PL._accepts(OR.SessionRegistry.reopen, "source"))

    # Two doubled pairs, and a second chat that held BB, so a freed pair can go to another chat:  a main chat takes the
    # free doubled pair issued the fewest times (owner, Nov 11 2026), so a freed AA is reissued only once BB caught up.
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA", "BB")})
    def test_a_reopen_that_would_turn_a_pair_chat_into_a_single_letter_one_is_refused_unwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "registry.sqlite3")
            first = self.make(db, "s-0001")
            filler = self.make(db, "s-0009")  # BB
            reg = OR.SessionRegistry(db)
            try:
                reg.close(first["record_id"], source="test")
                reg.close(filler["record_id"], source="test")
                self.make(db, "s-0002")  # takes the pair s-0001 held
                calls = []

                class Old:  # a reopen that predates pair letters: no pick
                    def reopen(self, record_id, *, actor, source):
                        calls.append(record_id)
                        return "A", 3, "AA"

                with self.assertRaises(PL.SpaceLost):
                    PL.operator_reopen(OR, db, Old(), first["record_id"], actor="CODEX", source="x", pick="primary")
                self.assertEqual(calls, [])  # nothing was asked of the registry, so nothing is left in its history
                # A pair-aware reopen (it takes a pick) keeps the space itself.
                class Aware:
                    def reopen(self, record_id, *, actor, source, pick):
                        calls.append(pick)
                        return "CD", 4, "AA"

                got = PL.operator_reopen(OR, db, Aware(), first["record_id"], actor="C", source="x", pick="primary")
                self.assertEqual((got, calls), (("CD", 4, "AA"), ["primary"]))
                # An old reopen is fine while the chat's own pair is free.
                reg.close(self.lookup_record(db, "s-0002"), source="test")
                got = PL.operator_reopen(OR, db, Old(), first["record_id"], actor="C", source="x", pick="primary")
                self.assertEqual((got, calls[-1]), (("A", 3, "AA"), first["record_id"]))
            finally:
                reg.close_db()

    def make(self, db: str, sid: str) -> dict:
        return OR.register_session(
            db, platform="codex", verified_host="server", profile_scope="default", native_session_id=sid,
            seat="CODEX", source="test", issuer=OR.ISSUER_OPERATOR, letter_space="double",
        )

    def lookup_record(self, db: str, sid: str) -> str:
        reg = OR.SessionRegistry(db)
        try:
            return reg.lookup("codex", "server", "default", sid)["record_id"]
        finally:
            reg.close_db()


class RegistrySideTests(Base):
    def test_a_host_with_no_registry_api_is_not_configured_and_writes_nothing(self) -> None:
        other = os.path.join(self.tmp.name, "elsewhere")
        out = io.StringIO()
        code = PL.registry_main(
            self.reg_argv("register", "s-0001"),
            env={"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": other},
            stdout=out,
        )
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out.getvalue())["code"], "not_configured")
        self.assertFalse(os.path.exists(other))  # no local registry, ever
        self.assertEqual(self.fake.requests, [])

    def test_every_platform_name_is_written_and_a_bad_name_is_refused(self) -> None:
        for platform in ("Codex", "", "../x"):
            with self.subTest(platform=platform):
                code, out = self.registry(*self.reg_argv("register", "s-0001", platform=platform))
                self.assertEqual(code, 2)
                self.assertIn("error", out)
        for platform, seat in (("claude", "CLAUDE"), ("minimax", "MM")):
            code, out = self.registry(*self.reg_argv("register", "s-0001", platform=platform, seat=seat))
            self.assertEqual(code, 0, out)
            self.assertIsNotNone(self.lookup("s-0001", platform=platform))

    def test_bad_arguments_and_session_ids(self) -> None:
        self.assertEqual(self.registry("register", "--bogus")[0], 2)
        self.assertEqual(self.registry(*self.reg_argv("register", "x"))[0], 4)  # not a session id
        no_seat = ["register", "--platform", "codex", "--location", "server", "--session-id", "s-0001"]
        self.assertEqual(self.registry(*no_seat)[0], 2)  # a registration names its seat

    def test_register_resume_title_close_reopen(self) -> None:
        first = self.register("s-0001", title="First Chat")
        self.assertRegex(first["label"], PAIR)
        self.assertEqual((first["outcome"], first["created"]), ("registered", True))
        label = first["label"]
        again = self.register("s-0001")
        self.assertEqual((again["label"], again["outcome"]), (label, "resumed"))
        code, out = self.registry(*self.reg_argv("title", "s-0001", title="Renamed"))
        self.assertEqual((code, out["label"]), (0, label))
        self.assertEqual(self.lookup("s-0001")["title"], "Renamed")
        code, out = self.registry(*self.reg_argv("title", "s-0001"))  # no words: a touch, the title stays
        self.assertEqual((code, self.lookup("s-0001")["title"]), (0, "Renamed"))
        code, out = self.registry(*self.reg_argv("close", "s-0001"))
        self.assertEqual((code, out["closed"]), (0, True))
        self.assertTrue(self.registry(*self.reg_argv("touch", "s-0001"))[1]["retired"])
        back = self.register("s-0001", title="Back Again")
        # its own pair back as the same generation:  a return is not a second issue of the pair
        self.assertEqual((back["outcome"], back["label"], back["generation"], back["previous"]), ("reopened", label, 1, label))
        self.assertEqual(self.lookup("s-0001")["title"], "Back Again")

    # Two doubled pairs, and a second chat that held BB, so a freed pair can go to another chat:  a main chat takes the
    # free doubled pair issued the fewest times (owner, Nov 11 2026), so a freed AA is reissued only once BB caught up.
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA", "BB")})
    def test_a_chat_whose_pair_went_to_another_chat_never_gets_a_single_letter(self) -> None:
        label = self.register("s-0001")["label"]
        self.register("s-0009")  # BB
        self.assertEqual(self.registry(*self.reg_argv("close", "s-0009"))[1]["closed"], True)
        self.assertEqual(self.registry(*self.reg_argv("close", "s-0001"))[1]["closed"], True)
        self.assertEqual(self.register("s-0002")["label"], label)  # the pair is free again, and taken
        code, back = self.register_raw("s-0001")
        if REOPEN_KEEPS_SPACE:
            self.assertEqual(code, 0)
            self.assertRegex(back["label"], PAIR)
            self.assertNotEqual(back["label"], label)
        else:
            # The registry of main before pair letters would allocate a single letter: the chat waits instead.
            self.assertEqual(code, 5)
            self.assertIn("pair letter", back["error"])
            self.assertEqual(self.lookup("s-0001")["state"], "closed")
            self.assertEqual(self.lookup("s-0001")["label"], "")
        conn = sqlite3.connect(self.db)
        try:
            live = [r[0] for r in conn.execute("SELECT letter FROM alias_assignments WHERE retired_at IS NULL")]
        finally:
            conn.close()
        self.assertTrue(live and all(len(letter) == 2 for letter in live), live)  # no single letter, ever
        if not REOPEN_KEEPS_SPACE:
            # Once its old pair is free the chat gets it back.
            self.registry(*self.reg_argv("close", "s-0002"))
            again = self.register("s-0001")
            self.assertEqual((again["label"], again["outcome"]), (label, "reopened"))

    def test_the_deploy_smoke_check_answers_with_one_json_line_and_writes_nothing(self) -> None:
        # The command the README gives for checking a host's registry API configuration before a hook is installed.
        code, out = self.registry(
            "show", "--platform", "smoke", "--location", "server", "--session-id", "smoke-check-0001"
        )
        self.assertEqual((code, out), (5, {"error": PL.NO_REGISTRATION}))
        self.assertIsNone(self.lookup("smoke-check-0001", platform="smoke"))

    def test_show_reads_and_changes_nothing(self) -> None:
        label = self.register("s-0001", title="First Chat")["label"]
        self.set_seen("s-0001", "2026-01-01T00:00:00.000Z")
        code, out = self.registry(*self.reg_argv("show", "s-0001"))
        self.assertEqual((code, out["found"], out["label"]), (0, True, label))
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute("SELECT last_seen_at FROM sessions").fetchone()[0], "2026-01-01T00:00:00.000Z"
        )
        conn.close()
        self.assertEqual(self.registry(*self.reg_argv("show", "s-0099"))[0], 5)

    def test_a_seat_that_is_not_the_registered_one_gets_a_mismatch(self) -> None:
        self.register("s-0001")
        argv = self.reg_argv("register", "s-0001")
        argv[argv.index("CODEX")] = "OPENCODE"
        code, out = self.registry(*argv)
        self.assertEqual(out["outcome"], "seat_mismatch")

    def test_each_platform_has_its_own_letters(self) -> None:
        one = self.register("s-0001", platform="codex")
        other = self.register("s-0001", platform="opencode")
        self.assertEqual(one["label"], other["label"])  # the same lowest pair, in two namespaces
        self.assertEqual(self.lookup("s-0001", platform="opencode")["label"], other["label"])

    def test_a_title_is_scanned_before_it_is_stored(self) -> None:
        token = secret_word()
        out = self.register("s-0001", title="use %s now" % token)
        self.assertEqual(self.lookup("s-0001")["title"], "use now")
        self.assertNotIn(token, json.dumps(out))

    def test_full_letters_are_the_assigners_refusal_and_no_chat_is_evicted(self) -> None:
        with mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA", "BB")}):
            for sid in ("s-0001", "s-0002"):
                self.register(sid)
            code, out = self.registry(*self.reg_argv("register", "s-0003", letter_pick="lowest"))
        self.assertEqual(code, 5)
        self.assertIn("are live", out["error"])
        for sid in ("s-0001", "s-0002"):
            self.assertEqual(self.lookup(sid)["state"], "active")

    @unittest.skipUnless(PL._accepts(OR.register_session, "parent_session_id"), "this registry has no parent")
    def test_a_sub_thread_takes_the_pair_after_its_parent(self) -> None:
        parent = self.register("p-0001", letter_pick="primary")
        child = self.register("c-0001", letter_pick="primary", parent_session_id="p-0001")
        self.assertEqual(parent["label"][0], child["label"][0])
        self.assertNotEqual(parent["label"], child["label"])
        again = self.register("c-0002", letter_pick="primary", parent_session_id="p-0001")
        self.assertEqual(again["label"][0], parent["label"][0])
        self.assertNotIn(again["label"], (parent["label"], child["label"]))

    def test_an_unknown_parent_is_no_parent(self) -> None:
        out = self.register("c-0001", parent_session_id="never-registered")
        self.assertRegex(out["label"], PAIR)
        out = self.register("c-0002", parent_session_id="x")  # not a session id at all
        self.assertRegex(out["label"], PAIR)


class Calls:
    """The registry calls a chat makes, as a transport:  each argv is recorded and run through the registry side in
    process, which makes its one API call to the test's assigner."""

    def __init__(self, test: Base, fail: Exception | None = None):
        self.test, self.fail, self.calls = test, fail, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail is not None:
            raise self.fail
        out = io.StringIO()
        PL.registry_main(list(argv), env=self.test.env(), stdout=out)
        return PL._last_json(out.getvalue()) or {"error": "the registry gave no answer"}

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]


class FlakyCalls(Calls):
    """Calls whose assigner does not answer the first `times` calls of the ops named."""

    def __init__(self, test: Base, ops: tuple[str, ...] = (), times: int = 1):
        super().__init__(test)
        self.failing, self.times, self.failed = set(ops), times, 0

    def __call__(self, argv):
        if argv[0] in self.failing and self.failed < self.times:
            self.failed += 1
            self.calls.append(list(argv))
            return {"error": "the registry did not answer in time", "code": "timeout"}
        return super().__call__(argv)


class HookTests(Base):
    def hook(
        self,
        payload: dict,
        *,
        env: dict | None = None,
        runner=None,
        now: float | None = None,
        platform: str = "linux",
        spec: PL.PlatformSpec | None = None,
        transport=None,
        **flags,
    ) -> dict | None:
        out = io.StringIO()
        env = env if env is not None else self.server_env()
        code = PL.hook(
            spec or self.spec, env, io.StringIO(json.dumps(payload)), out,
            now=now, platform=platform, transport=transport or runner or Calls(self), **flags,
        )
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"] if text else None

    def start(self, sid: str, source: str = "startup", **extra) -> dict | None:
        return self.hook(dict({"hook_event_name": "SessionStart", "session_id": sid, "source": source}, **extra))

    def prompt(self, sid: str, text: str, **extra) -> dict | None:
        return self.hook(dict({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text}, **extra))

    def end(self, sid: str, reason: str = "", **kw) -> dict | None:
        return self.hook({"hook_event_name": "SessionEnd", "session_id": sid, "reason": reason}, **kw)

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "CODEX", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def label(self, sid: str) -> str:
        return self.note(sid)["label"]

    def test_new_chat_gets_a_letter_then_a_title(self) -> None:
        out = self.start("s-0001")
        label = self.label("s-0001")
        self.assertRegex(label, PAIR)
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % label)
        self.assertIn("Codex %s" % label, out["additionalContext"])
        self.assertIn('"@**Codex** %s"' % label, out["additionalContext"])
        out = self.prompt("s-0001", "help get all PR issues addressed from review findings.",
                          session_title="%s: New Chat" % label)
        self.assertEqual(out["sessionTitle"], "%s: Help Get All PR Issues Addressed" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "Help Get All PR Issues Addressed")
        self.assertIsNone(self.prompt("s-0001", "next", session_title="%s: Help Get All PR Issues Addressed" % label))

    def test_the_note_is_private_and_where_the_claude_notes_are(self) -> None:
        self.start("s-0001")
        path = os.path.join(self.state, "CODEX", "letters", "s-0001.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        self.assertEqual(self.note("s-0001")["platform"], "codex")

    def test_mac_registers_through_the_api_like_every_host(self) -> None:
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"},
                        env=self.env(), platform="darwin")
        self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")
        self.assertRegex(self.lookup("m-0001", "mac")["label"], PAIR)
        self.assertTrue(self.fake.requests)  # the one assigner answered, over its API

    def test_the_flag_names_the_location(self) -> None:
        ssh = Calls(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        env=self.env(), transport=ssh, location_flag="server")
        self.assertIn("Codex", out["additionalContext"])
        self.assertEqual(len(ssh.calls), 1)

    def test_a_named_chat_keeps_its_words(self) -> None:
        out = self.start("s-0001", source="resume", session_title="Fleet PR Sweep")
        self.assertEqual(out["sessionTitle"], "%s: Fleet PR Sweep" % self.label("s-0001"))
        self.assertIsNone(self.prompt("s-0001", "anything", session_title=out["sessionTitle"]))

    def test_a_resumed_chat_is_titled_from_its_own_store(self) -> None:
        spec = make_spec(stored_title=lambda payload, env: "rotate the infisical tokens " + secret_word())
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "resume"}, spec=spec)
        self.assertEqual(out["sessionTitle"], "%s: rotate the infisical tokens" % self.label("s-0001"))
        # A fresh start asks the store for nothing.
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "startup"}, spec=spec)
        self.assertTrue(out["sessionTitle"].endswith(": New Chat"))

    def test_an_owners_acronym_survives_a_first_start(self) -> None:
        ssh = Calls(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "resume",
                         "session_title": "PR: Review"}, transport=ssh)
        label = self.label("s-0001")
        self.assertEqual(out["sessionTitle"], "%s: PR: Review" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "PR: Review")
        self.assertEqual(ssh.ops(), ["register", "title"])  # the first guess was wrong, so one more hop fixes it

    def test_a_chats_own_letter_comes_off_its_title(self) -> None:
        self.start("s-0001")
        label = self.label("s-0001")
        out = self.start("s-0001", source="resume", session_title="%s: Fleet Sweep" % label)
        self.assertNotIn("sessionTitle", out or {})  # already right
        self.assertEqual(self.lookup("s-0001")["title"], "Fleet Sweep")

    # Two doubled pairs, and a second chat that held BB, so a freed pair can go to another chat:  a main chat takes the
    # free doubled pair issued the fewest times (owner, Nov 11 2026), so a freed AA is reissued only once BB caught up.
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA", "BB")})
    def test_clear_closes_and_reopen_checks_again(self) -> None:
        self.spec = make_spec(close_reasons=("clear", "logout", "other"), title_in_output=True)
        self.start("s-0001", session_title="Old Name")
        old = self.label("s-0001")
        self.start("s-0009")  # BB
        self.end("s-0009", "clear")
        self.end("s-0001", "clear")
        self.assertEqual(self.lookup("s-0001")["state"], "closed")
        self.assertEqual(self.note("s-0001")["ended"]["reason"], "clear")
        self.start("s-0002")  # takes the letter /clear freed
        self.assertEqual(self.label("s-0002"), old)
        out = self.start("s-0001", source="resume", session_title="%s: Old Name" % old)
        if not REOPEN_KEEPS_SPACE:
            self.assertEqual(self.note("s-0001")["state"], "failed")  # no single letter for a pair chat
            self.assertIn("pair letter", out["additionalContext"])
            return
        new = self.label("s-0001")
        self.assertNotEqual(new, old)
        self.assertEqual(out["sessionTitle"], "%s: Old Name" % new)
        self.assertIn("earlier letter %s went to another chat" % old, out["additionalContext"])
        self.assertIn("  ", out["additionalContext"])  # rendered copy takes U+00A0 sentence gaps

    def test_exit_and_resume_keep_the_letter(self) -> None:
        self.spec = make_spec(close_reasons=("clear", "logout", "other"), title_in_output=True)
        self.start("s-0001")
        for reason in ("prompt_input_exit", "resume"):
            self.end("s-0001", reason)
            self.assertEqual(self.lookup("s-0001")["state"], "active")
        self.assertEqual(self.note("s-0001")["ended"]["reason"], "resume")

    def test_a_platform_with_no_reasons_closes_on_every_end(self) -> None:
        self.start("s-0001")
        self.end("s-0001", "archived")
        self.assertEqual(self.lookup("s-0001")["state"], "closed")
        self.assertEqual(self.note("s-0001")["state"], "closed")
        self.assertEqual(self.prompt("s-0001", "anything"), None)  # a closed chat's hook does nothing

    def test_a_host_can_name_its_own_reasons(self) -> None:
        self.start("s-0001")
        env = self.server_env(SESSION_LETTER_CLOSE_ON="clear")
        self.end("s-0001", "other", env=env)
        self.assertEqual(self.lookup("s-0001")["state"], "active")
        self.end("s-0001", "clear", env=env)
        self.assertEqual(self.lookup("s-0001")["state"], "closed")

    def test_clear_start_ignores_the_inherited_name(self) -> None:
        out = self.start("s-0001", source="clear", session_title="ZZ: Previous Chat")
        label = self.label("s-0001")
        self.assertNotIn("sessionTitle", out)  # a clear start does not apply a hook title
        self.assertFalse(self.note("s-0001")["titled"])
        out = self.prompt("s-0001", "write the tests", session_title="ZZ: Previous Chat")
        self.assertEqual(out["sessionTitle"], "%s: Write the Tests" % label)

    # Two doubled pairs, and a second chat that held BB, so a freed pair can go to another chat:  a main chat takes the
    # free doubled pair issued the fewest times (owner, Nov 11 2026), so a freed AA is reissued only once BB caught up.
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA", "BB")})
    def test_a_letter_retired_while_open_is_reissued(self) -> None:
        self.start("s-0001")
        old = self.label("s-0001")
        self.start("s-0009")  # BB
        self.prompt("s-0001", "first task", session_title="%s: New Chat" % old)
        reg = OR.SessionRegistry(self.db)
        try:
            reg.close(self.lookup("s-0009")["record_id"], source="test")
            reg.close(self.lookup("s-0001")["record_id"], source="test")
        finally:
            reg.close_db()
        self.start("s-0002")  # the freed letter goes to another chat
        self.assertEqual(self.label("s-0002"), old)
        later = time.time() + PL.TOUCH_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                         "session_title": "%s: First Task" % old}, now=later)
        if not REOPEN_KEEPS_SPACE:
            self.assertEqual(self.note("s-0001")["state"], "failed")  # no single letter for a pair chat
            self.assertEqual(out["sessionTitle"], "First Task")  # and the dead letter comes off its title
            return
        new = self.label("s-0001")
        self.assertNotEqual(new, old)
        self.assertEqual(out["sessionTitle"], "%s: First Task" % new)
        self.assertEqual(self.lookup("s-0001")["label"], new)

    def test_a_placeholder_is_never_taken_for_a_title(self) -> None:
        self.start("s-0001")
        label = self.label("s-0001")
        out = self.start("s-0001", source="resume", session_title="%s: New Chat" % label)
        self.assertNotIn("sessionTitle", out)
        self.assertFalse(self.note("s-0001")["titled"])
        out = self.prompt("s-0001", "fix the build", session_title="%s: New Chat" % label)
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % label)

    def test_a_bare_slash_command_waits_for_the_next_prompt(self) -> None:
        self.start("s-0001")
        label = self.label("s-0001")
        self.assertIsNone(self.prompt("s-0001", "/review", session_title="%s: New Chat" % label))
        self.assertFalse(self.note("s-0001")["titled"])
        out = self.prompt("s-0001", "fix the login bug", session_title="%s: New Chat" % label)
        self.assertEqual(out["sessionTitle"], "%s: Fix the Login Bug" % label)

    def test_a_prompt_with_a_secret_names_nothing_and_leaks_nothing(self) -> None:
        token = secret_word()
        ssh = Calls(self)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=ssh)
        label = self.label("s-0001")
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001",
                         "prompt": "rotate %s today" % token, "session_title": "%s: New Chat" % label}, transport=ssh)
        self.assertEqual(out["sessionTitle"], "%s: Rotate Today" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "Rotate Today")
        with open(os.path.join(self.state, "CODEX", "letters", "s-0001.json")) as fh:
            self.assertNotIn(token, fh.read())
        self.assertNotIn(token, json.dumps(ssh.calls))

    def test_steady_state_prompts_cost_no_hop_and_a_touch_is_rare(self) -> None:
        ssh = Calls(self)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=ssh)
        label = self.label("s-0001")
        title = "%s: Fix the Build" % label
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                   "session_title": "%s: New Chat" % label}, transport=ssh)
        self.assertEqual(ssh.ops(), ["register", "title"])
        for i in range(5):
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more %d" % i,
                       "session_title": title}, transport=ssh)
        self.assertEqual(len(ssh.calls), 2)
        later = time.time() + PL.TOUCH_SECONDS + 5
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "later",
                   "session_title": title}, transport=ssh, now=later)
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "later still",
                   "session_title": title}, transport=ssh, now=later + 60)
        self.assertEqual(ssh.ops(), ["register", "title", "touch"])

    def test_a_renamed_chat_keeps_its_letter_and_the_operator_follows(self) -> None:
        self.start("s-0001")
        label = self.label("s-0001")
        self.prompt("s-0001", "fix the build", session_title="%s: New Chat" % label)
        # The owner renames it and drops the letter: the letter goes back, and the Operator has the new words.
        out = self.prompt("s-0001", "next", session_title="Better Name")
        self.assertEqual(out["sessionTitle"], "%s: Better Name" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "Better Name")
        # The owner renames it and keeps the letter: only the Operator needs to hear.
        ssh = Calls(self)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x",
                         "session_title": "%s: Other Words" % label}, transport=ssh)
        self.assertIsNone(out)
        self.assertEqual(self.lookup("s-0001")["title"], "Other Words")
        self.assertEqual(ssh.ops(), ["title"])
        # A platform that ignores the title it was given is not asked again for the same name.
        for _ in range(3):
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x",
                       "session_title": "Stubborn Name"}, transport=ssh)
        self.assertEqual(ssh.ops(), ["title", "title"])

    # ---- a letter the registry changes in place: the title follows it ----

    def move_letter(self, sid: str, letter: str = "", generation: int = 0) -> None:
        """Change the live letter (or generation) of a chat's record in place, as a relabel or a restore does."""
        record = self.lookup(sid)["record_id"]
        conn = sqlite3.connect(self.db)
        with conn:
            if letter:
                conn.execute("UPDATE alias_assignments SET letter = ? WHERE record_id = ? AND retired_at IS NULL",
                             (letter, record))
            if generation:
                conn.execute("UPDATE alias_assignments SET generation = ? WHERE record_id = ? AND retired_at IS NULL",
                             (generation, record))
        conn.close()

    def titled_chat(self) -> str:
        self.start("s-0001")
        label = self.label("s-0001")
        self.prompt("s-0001", "fix the build", session_title="%s: New Chat" % label)
        return label

    def test_a_letter_the_registry_moved_retitles_the_chat_on_its_next_touch(self) -> None:
        old = self.titled_chat()
        self.move_letter("s-0001", "QX")
        ssh = Calls(self)
        later = time.time() + PL.TOUCH_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                         "session_title": "%s: Fix the Build" % old}, transport=ssh, now=later)
        self.assertEqual(ssh.ops(), ["touch"])
        self.assertEqual(out["sessionTitle"], "QX: Fix the Build")
        self.assertIn("letter changed from %s to QX" % old, out["additionalContext"])
        self.assertIn('"@**Codex** QX"', out["additionalContext"])
        note = self.note("s-0001")
        self.assertEqual((note["label"], note["placeholder"]), ("QX", "QX: New Chat"))
        self.assertIn(old, note["labels"])  # a title that still shows the old letter is read as this chat's
        # The next prompt has nothing to change, whether the platform shows the new title or still the old one.
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x",
                                     "session_title": "QX: Fix the Build"}, transport=ssh, now=later + 60))
        self.assertEqual(ssh.ops(), ["touch"])

    def test_a_title_answer_with_a_new_letter_retitles_the_chat(self) -> None:
        old = self.titled_chat()
        self.move_letter("s-0001", "QX")
        out = self.prompt("s-0001", "next", session_title="%s: Better Name" % old)  # a rename: a title call
        self.assertEqual(out["sessionTitle"], "QX: Better Name")
        self.assertIn("letter changed from %s to QX" % old, out["additionalContext"])
        self.assertEqual(self.lookup("s-0001")["title"], "Better Name")
        self.assertEqual(self.label("s-0001"), "QX")

    def test_the_same_letter_and_generation_change_nothing(self) -> None:
        label = self.titled_chat()
        before = self.note("s-0001")
        later = time.time() + PL.TOUCH_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                         "session_title": "%s: Fix the Build" % label}, now=later)
        self.assertIsNone(out)
        after = self.note("s-0001")
        self.assertEqual((after["label"], after["generation"]), (before["label"], before["generation"]))

    def test_a_generation_alone_is_folded_in_silently(self) -> None:
        label = self.titled_chat()
        self.move_letter("s-0001", generation=2)
        later = time.time() + PL.TOUCH_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                         "session_title": "%s: Fix the Build" % label}, now=later)
        self.assertIsNone(out)  # no title and nothing said
        note = self.note("s-0001")
        self.assertEqual((note["label"], note["generation"]), (label, 2))

    def test_an_old_registry_touch_answer_without_a_letter_changes_nothing(self) -> None:
        class OldRegistry(Calls):
            """A Mac on a runtime from before the touch named the letter."""

            def __call__(self, argv):
                answer = super().__call__(argv)
                if argv[0] == "touch":
                    answer = {k: v for k, v in answer.items() if k not in ("label", "generation")}
                return answer

        label = self.titled_chat()
        self.move_letter("s-0001", "QX")
        ssh = OldRegistry(self)
        later = time.time() + PL.TOUCH_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                         "session_title": "%s: Fix the Build" % label}, transport=ssh, now=later)
        self.assertEqual(ssh.ops(), ["touch"])
        self.assertIsNone(out)
        self.assertEqual(self.label("s-0001"), label)

    def test_follow_letter_reads_only_a_live_answer_with_a_letter(self) -> None:
        base = {"state": "open", "label": "AA", "generation": 1, "record_id": "r1"}
        for answer in ({"ok": True, "record_id": "r1"}, {"retired": True, "record_id": "r1"},
                       {"error": "the registry host did not answer"}, {"ok": True, "label": ""}, {}):
            note = dict(base)
            self.assertEqual(PL.follow_letter(note, answer), "", answer)
            self.assertEqual(note, base, answer)
        note = dict(base, state="closed")
        self.assertEqual(PL.follow_letter(note, {"ok": True, "label": "QX", "generation": 1}), "")
        self.assertEqual(note["label"], "AA")
        note = dict(base)
        self.assertEqual(PL.follow_letter(note, {"ok": True, "label": "QX", "generation": 3, "record_id": "r1"}), "AA")
        self.assertEqual((note["label"], note["generation"]), ("QX", 3))

    def test_a_new_letter_without_a_generation_is_the_first_generation(self) -> None:
        note = {"state": "open", "label": "AA", "generation": 3, "record_id": "r1"}
        self.assertEqual(PL.follow_letter(note, {"ok": True, "label": "QX"}), "AA")
        self.assertEqual((note["label"], note["generation"]), ("QX", 1))
        for bad in (None, 0, True, "2"):
            note = {"state": "open", "label": "AA", "generation": 3}
            PL.follow_letter(note, {"ok": True, "label": "QX", "generation": bad})
            self.assertEqual(note["generation"], 1, bad)
        note = {"state": "open", "label": "AA", "generation": 3}
        PL.follow_letter(note, {"ok": True, "label": "AA"})  # the same letter with none:  the note keeps its own
        self.assertEqual(note["generation"], 3)

    def test_a_title_answer_refreshes_the_record_id(self) -> None:
        self.titled_chat()
        code, answer = self.registry(*self.reg_argv("title", "s-0001", title="Words"))
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["record_id"], self.lookup("s-0001")["record_id"])
        note = {"state": "open", "label": "AA", "generation": 1, "record_id": "old"}
        PL.follow_letter(note, {"ok": True, "label": "QX", "generation": 1, "record_id": "r2"})
        self.assertEqual(note["record_id"], "r2")

    def test_a_rename_that_only_drops_the_letter_touches_on_schedule_and_follows_a_moved_letter(self) -> None:
        spec = make_spec(events={"session.created": "start", "session.renamed": "rename", "session.deleted": "end"},
                         output="json")
        start = time.time()

        def rename(title: str, now: float, ssh: Calls) -> dict | None:
            out = io.StringIO()
            PL.hook(spec, self.server_env(), io.StringIO(json.dumps(
                {"event": "session.renamed", "session_id": "s-0001", "title": title})), out, transport=ssh, now=now)
            text = out.getvalue().strip()
            return json.loads(text)["platform_letter"] if text else None

        PL.hook(spec, self.server_env(), io.StringIO(json.dumps({"event": "session.created", "session_id": "s-0001"})),
                io.StringIO(), transport=Calls(self), now=start)
        old = self.label("s-0001")
        rename("Fresh Name", start + 1, Calls(self))  # the words reach the Operator
        self.move_letter("s-0001", "QX")
        path = os.path.join(self.state, "CODEX", "letters", "s-0001.json")

        def forget_seen() -> None:  # the platform shows the bare words again, as a name it was not told
            note = self.note("s-0001")
            note.pop("seen", None)
            with open(path, "w") as fh:
                json.dump(note, fh)

        # Within TOUCH_SECONDS of the last call a letter-only rename costs no registry call, as a prompt would not.
        forget_seen()
        ssh = Calls(self)
        body = rename("Fresh Name", start + 60, ssh)
        self.assertEqual(ssh.ops(), [])
        self.assertEqual(body["title"], "%s: Fresh Name" % old)
        # Once a touch is due, the rename makes it and takes the letter the registry holds now.
        forget_seen()
        body = rename("Fresh Name", start + PL.TOUCH_SECONDS + 5, ssh)
        self.assertEqual(ssh.ops(), ["touch"])
        self.assertEqual((body["label"], body["title"]), ("QX", "QX: Fresh Name"))
        self.assertIn("letter changed from %s to QX" % old, body["context"])

    def test_the_api_touch_answer_passes_the_letter_on_and_an_old_one_names_none(self) -> None:
        new = {"record_id": "r1", "outcome": "seen", "label": "QX", "generation": 2}
        self.assertEqual(PL._touch_answer(new), {"ok": True, "record_id": "r1", "label": "QX", "generation": 2})
        self.assertEqual(PL._touch_answer({"record_id": "r1", "outcome": "seen"}), {"ok": True, "record_id": "r1"})

    def test_a_rename_event_follows_a_moved_letter(self) -> None:
        spec = make_spec(events={"session.created": "start", "session.renamed": "rename", "session.deleted": "end"},
                         output="json")
        PL.hook(spec, self.server_env(), io.StringIO(json.dumps({"event": "session.created", "session_id": "s-0001"})),
                io.StringIO(), transport=Calls(self))
        old = self.label("s-0001")
        self.move_letter("s-0001", "QX")
        out = io.StringIO()
        PL.hook(spec, self.server_env(), io.StringIO(json.dumps(
            {"event": "session.renamed", "session_id": "s-0001", "title": "Fresh Name"})), out, transport=Calls(self))
        body = json.loads(out.getvalue())["platform_letter"]
        self.assertEqual((body["label"], body["title"]), ("QX", "QX: Fresh Name"))
        self.assertIn("letter changed from %s to QX" % old, body["context"])

    # ---- AFC-694:  the Linear issues a turn touched go to the registry ----

    def transcript(self, *entries, name: str = "t.jsonl") -> str:
        """Append transcript lines (dicts as JSON, strings as they are) under the platform's home folder."""
        folder = os.path.join(self.home, ".codex", "sessions")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write((json.dumps(entry) if isinstance(entry, dict) else entry) + "\n")
        return path

    def issues_held(self, sid: str = "s-0001") -> list[str]:
        found = OR.resolve_label(self.db, "codex", self.label(sid)) or {}
        return sorted(i["id"] for i in found.get("issues", []))

    def turn(self, ssh, text: str = "go", **extra) -> dict | None:
        label = self.label("s-0001")
        return self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": text,
                          "session_title": "%s: Fix the Build" % label, **extra}, transport=ssh)

    def test_a_turn_records_the_issues_its_transcript_names_and_sends_only_a_change(self) -> None:
        self.titled_chat()
        path = self.transcript(
            {"type": "user", "message": {"role": "user", "content": "please fix AFC-694 today"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "input": {"command": 'simple-chat post --topic "OPS-12 rollout" --body x'}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "a file that names AFC-999 and posted id 5 to #WIP › CT-7 notes"}]},
             "toolUseResult": {"stdout": "AFC-998"}},
        )
        ssh = Calls(self)
        self.assertIsNone(self.turn(ssh, transcript_path=path))  # nothing changes on screen
        self.assertEqual(ssh.ops(), ["issues"])
        self.assertEqual(self.issues_held(), ["AFC-694", "CT-7", "OPS-12"])  # not what a tool's output read
        note = self.note("s-0001")
        self.assertEqual(note["issues_sent"], ["AFC-694", "CT-7", "OPS-12"])
        self.assertEqual(note["issues_scan"]["offset"], os.path.getsize(path))
        # No new lines, or only ids it already sent:  no registry call.
        self.turn(ssh, transcript_path=path)
        self.transcript({"type": "assistant", "message": {"content": "still on AFC-694"}})
        self.turn(ssh, transcript_path=path)
        self.assertEqual(ssh.ops(), ["issues"])
        # A new id is a change, and it goes first.
        self.transcript({"type": "assistant", "message": {"content": "now SWU-3"}})
        self.turn(ssh, transcript_path=path)
        self.assertEqual(ssh.ops(), ["issues", "issues"])
        self.assertEqual(self.issues_held(), ["AFC-694", "CT-7", "OPS-12", "SWU-3"])
        self.assertEqual(self.note("s-0001")["issues"][0]["id"], "SWU-3")

    def test_a_turn_records_the_issue_its_branch_names(self) -> None:
        self.titled_chat()
        repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(repo)
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(git + ["init", "-q", "-b", "codex/afc-701-letter-issues"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
        ssh = Calls(self)
        self.turn(ssh, cwd=repo)
        self.assertEqual(ssh.ops(), ["issues"])
        self.assertEqual(self.issues_held(), ["AFC-701"])
        # A folder that is no git tree, or no folder at all, gives nothing.
        self.assertEqual(PL.branch_issue_ids(self.tmp.name), set())
        self.assertEqual(PL.branch_issue_ids(os.path.join(self.tmp.name, "gone")), set())

    def test_hook_data_with_neither_a_transcript_nor_a_folder_sends_nothing(self) -> None:
        self.titled_chat()
        ssh = Calls(self)
        self.turn(ssh)
        self.assertEqual(ssh.ops(), [])
        self.assertNotIn("issues", self.note("s-0001"))

    def test_a_transcript_outside_the_home_folder_is_never_read(self) -> None:
        self.titled_chat()
        outside = os.path.join(self.tmp.name, "elsewhere.jsonl")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "AFC-694"}}) + "\n")
        ssh = Calls(self)
        self.turn(ssh, transcript_path=outside)
        self.assertEqual(ssh.ops(), [])

    def test_a_registry_that_fails_or_does_not_know_the_op_is_silent_and_asked_again_later(self) -> None:
        class OldMac(Calls):
            """An assigner from before the op:  its parser refuses the whole call."""

            def __call__(self, argv):
                if argv[0] == "issues":
                    self.calls.append(list(argv))
                    return {"error": "bad arguments"}
                return super().__call__(argv)

        label = self.titled_chat()
        path = self.transcript({"type": "user", "message": {"content": "AFC-694"}})
        ssh = OldMac(self)
        start = time.time()
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go",
                         "session_title": "%s: Fix the Build" % label, "transcript_path": path},
                        transport=ssh, now=start)
        self.assertIsNone(out)  # the prompt is untouched
        note = self.note("s-0001")
        self.assertEqual([i["id"] for i in note["issues"]], ["AFC-694"])
        self.assertNotIn("issues_sent", note)
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go",
                   "session_title": "%s: Fix the Build" % label, "transcript_path": path}, transport=ssh, now=start + 60)
        self.assertEqual(ssh.ops(), ["issues"])  # not again inside the wait
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go",
                   "session_title": "%s: Fix the Build" % label, "transcript_path": path},
                  transport=Calls(self), now=start + PL.RETRY_SECONDS + 5)
        self.assertEqual(self.issues_held(), ["AFC-694"])

    def test_issue_ids_in_lines_reads_the_chats_own_words_and_any_posted_topic(self) -> None:
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "AFC-1 and CTS-2"}]}}),
            json.dumps({"type": "function_call_output", "output": "OPS-3"}),
            json.dumps({"isMeta": True, "message": {"content": "OPS-4"}}),
            json.dumps({"type": "tool_result", "content": "simple-chat post --topic 'CC-5 x'"}),
            "plain text names UM-6, afc-7 and XAFC-8",
            json.dumps({"input": {"command": "simple-chat post --topic=FL-9"}}),
        ]
        self.assertEqual(PL.issue_ids_in_lines(lines), {"AFC-1", "CTS-2", "CC-5", "UM-6", "FL-9"})

    def test_issue_ids_in_lines_reads_past_a_type_that_is_not_a_string(self) -> None:
        # A tool input that carries a JSON schema has "type" holding a list or a mapping;  it must not
        # stop the scan (Sentry on #1762).
        lines = [
            json.dumps({"input": {"schema": {"type": ["string", "null"]}, "note": "AFC-11"}}),
            json.dumps({"type": {"nested": True}, "text": "CTS-12"}),
        ]
        self.assertEqual(PL.issue_ids_in_lines(lines), {"AFC-11", "CTS-12"})

    def test_read_new_lines_reads_only_new_whole_lines_within_its_cap(self) -> None:
        root = os.path.join(self.home, ".codex")
        path = self.transcript("one AFC-1", "two AFC-2")
        lines, scan = PL.read_new_lines(path, root, None)
        self.assertEqual(lines, ["one AFC-1", "two AFC-2"])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("three AFC-3\nfour, half writ")
        lines, scan = PL.read_new_lines(path, root, scan)
        self.assertEqual(lines, ["three AFC-3"])  # the line still being written waits
        self.assertEqual(PL.read_new_lines(path, root, scan)[0], [])
        with mock.patch.object(PL, "ISSUE_SCAN_BYTES", 16):
            big = self.transcript(*["line %d AFC-%d" % (n, n) for n in range(50)], name="big.jsonl")
            lines, scan = PL.read_new_lines(big, root, None)
        self.assertEqual(lines, ["line 49 AFC-49"])  # only the tail, from its first whole line
        self.assertEqual(scan["offset"], os.path.getsize(big))
        with self.assertRaises(ValueError):
            PL.read_new_lines(path, os.path.join(self.home, "other"), None)

    def test_branch_issue_ids_is_silent_on_every_failure(self) -> None:
        def answer(stdout: str = "", code: int = 0, raises: Exception | None = None):
            def run(*args, **kwargs):
                self.assertEqual(kwargs["timeout"], PL.ISSUE_GIT_TIMEOUT)
                if raises:
                    raise raises
                return SimpleNamespace(stdout=stdout, returncode=code)
            return run

        here = self.tmp.name
        self.assertEqual(PL.branch_issue_ids(here, answer("claude/ops-12-x\n")), {"OPS-12"})
        self.assertEqual(PL.branch_issue_ids(here, answer("HEAD\n")), set())
        self.assertEqual(PL.branch_issue_ids(here, answer("", 128)), set())
        self.assertEqual(PL.branch_issue_ids(here, answer(raises=subprocess.TimeoutExpired("git", 2))), set())
        self.assertEqual(PL.branch_issue_ids(here, answer(raises=OSError("no git"))), set())

    def test_sync_issues_sends_on_change_only_and_reads_an_old_answer_as_taken(self) -> None:
        sent: list[str] = []

        def send(text: str) -> dict:
            sent.append(text)
            return {"ok": True}  # an answer with no `issues` field:  the registry took what it was sent

        note: dict = {}
        path = self.transcript("AFC-1")
        PL.sync_issues(note, {"transcript_path": path}, self.home, 1000.0, send)
        self.assertEqual(len(sent), 1)
        self.assertEqual(json.loads(sent[0]), [{"id": "AFC-1", "seen_at": "1970-01-01T00:16:40Z"}])
        self.assertEqual(note["issues_sent"], ["AFC-1"])
        PL.sync_issues(note, {"transcript_path": path}, self.home, 2000.0, send)
        self.assertEqual(len(sent), 1)

        def broken(text: str) -> dict:
            raise RuntimeError("boom")

        self.transcript("AFC-2")
        PL.sync_issues(note, {"transcript_path": path}, self.home, 3000.0, broken)  # never raises
        self.assertEqual(note["issues_sent"], ["AFC-1"])

    # ---- AFC-702:  the end of a turn reports its issues and topics ----

    def turn_spec(self) -> PL.PlatformSpec:
        return make_spec(events={**PL.DEFAULT_EVENTS, "Stop": "turn"})

    def test_a_turn_end_event_reports_without_a_further_prompt_and_closes_nothing(self) -> None:
        spec = self.turn_spec()
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, spec=spec)
        path = self.transcript(
            {"type": "user", "message": {"content": "start on AFC-1"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": 'simple-chat post --topic "AFC-1 plan" --body x'}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                      "content": "posted id 3 to #Work-In-Progress › AFC-1 plan"}]}},
        )
        ssh = Calls(self)
        out = self.hook({"hook_event_name": "Stop", "session_id": "s-0001", "transcript_path": path},
                        spec=spec, transport=ssh)
        self.assertIsNone(out)  # a turn end prints nothing
        self.assertEqual(ssh.ops(), ["issues"])
        self.assertEqual(self.issues_held(), ["AFC-1"])
        note = self.note("s-0001")
        self.assertEqual(note["state"], "open")
        self.assertEqual([t["topic"] for t in note["topics"]], ["AFC-1 plan"])
        self.assertNotIn("ended", note)
        # The prompt after it reads from where the turn end left off:  nothing new, no second issues call (it may
        # still title the chat, which is the prompt's own work).
        self.turn(ssh, transcript_path=path)
        self.assertEqual(ssh.ops().count("issues"), 1)

    def test_a_turn_end_for_a_closed_or_unknown_chat_does_nothing(self) -> None:
        spec = self.turn_spec()
        path = self.transcript({"type": "user", "message": {"content": "AFC-1"}})
        ssh = Calls(self)
        self.assertIsNone(self.hook({"hook_event_name": "Stop", "session_id": "s-0009", "transcript_path": path},
                                    spec=spec, transport=ssh))
        self.assertEqual(ssh.calls, [])
        note = {"state": "closed", "label": "AA"}
        PL.end_of_turn(note, {"transcript_path": path}, self.home, 1000.0, lambda text: {"ok": True})
        self.assertEqual(note, {"state": "closed", "label": "AA"})

    def test_a_turn_end_with_the_registry_unreachable_is_silent_and_waits_to_ask_again(self) -> None:
        calls: list[str] = []

        def down(text: str) -> dict:
            calls.append(text)
            return {"error": "ssh to the Mac timed out"}

        note: dict = {"state": "open", "label": "AA"}
        path = self.transcript("AFC-1")
        PL.end_of_turn(note, {"transcript_path": path}, self.home, 1000.0, down)
        PL.end_of_turn(note, {"transcript_path": path}, self.home, 1000.0 + 60, down)
        self.assertEqual(len(calls), 1)
        self.assertEqual(note["issues_failed_at"], 1000.0)
        self.assertEqual([i["id"] for i in note["issues"]], ["AFC-1"])

    def test_the_turn_event_is_one_a_spec_may_map_to(self) -> None:
        self.assertIn("turn", PL.EVENTS)
        self.assertEqual(PL.canonical_event(self.turn_spec(), {"hook_event_name": "Stop"}), "turn")
        self.assertEqual(PL.canonical_event(make_spec(), {"hook_event_name": "Stop"}), "")  # none by default

    @staticmethod
    def calls(*uses: tuple[str, str, dict]) -> str:
        return json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": cid, "name": name, "input": args} for cid, name, args in uses]}})

    @staticmethod
    def results(*outs: tuple[str, object, bool]) -> str:
        return json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": cid, "content": content, "is_error": err}
            for cid, content, err in outs]}})

    def test_topics_in_lines_counts_a_post_only_when_its_own_result_comes_back(self) -> None:
        lines = [
            self.calls(
                ("a", "Bash", {"command": "cd x && simple-chat --as CLAUDE post --topic 'First one' -b 'a; b'"}),
                ("b", "mcp__26a3__zulip_post", {"channel": "Work-In-Progress", "topic": "  Second   one  "}),
                ("c", "mcp__26a3__zulip_read_topic", {"topic": "Only read"}),
                ("d", "Bash", {"command": 'TOPIC="Third one"; simple-chat post --topic "$TOPIC" --body x'}),
            ),
            self.results(
                ("a", "posted id 5 to #Work-In-Progress › First one", False),
                ("b", [{"type": "text", "text": "{\"id\": 6}"}], False),
                ("c", "a topic someone else posted", False),
                ("d", "posted id 7 to #Work-In-Progress › Third one", False),
            ),
            "{not json",
        ]
        self.assertEqual(PL.topics_in_lines(lines), ["First one", "Second one", "Third one"])
        self.assertEqual(PL.topics_in_lines(["x" * 10, json.dumps(5), json.dumps([{"name": 3}])]), [])
        long = "T" * 90
        cmd = self.calls(("a", "Bash", {"command": "simple-chat post --topic=%s -b x" % long}))
        self.assertEqual(PL.topics_in_lines([cmd, self.results(("a", "", False))]), ["T" * PL.TOPIC_CHARS])

    def test_topics_in_lines_refuses_what_is_not_a_post_this_chat_made(self) -> None:
        cases = {
            "a read": [self.calls(("a", "Bash", {"command": 'simple-chat read --topic "AFC-9 someone elses"'})),
                       self.results(("a", "posted id 1 to #x › AFC-9 someone elses", False))],
            "another program": [self.calls(("a", "Bash", {"command": "kafka-topics --describe --topic orders"})),
                                self.results(("a", "ok", False))],
            "a variable with no printed line": [
                self.calls(("a", "Bash", {"command": 'simple-chat post --topic "$TOPIC" --json -b x'})),
                self.results(("a", '{"id": 4}', False))],
            "user prose": [json.dumps({"type": "user", "message": {"content":
                                                                    "never post with --topic fleet unless told"}}),
                           "plain words: simple-chat post --topic=First"],
            "a reply's words": [json.dumps({"type": "assistant", "message": {"content":
                                                                             'simple-chat post --topic "Said"'}})],
            "a peer's log line": [self.calls(("a", "Bash", {"command": "tail -5 listener.log"})),
                                  self.results(("a", "posted id 7 to #Work-In-Progress › BF-3 peer topic", False))],
            "an error result": [self.calls(("a", "mcp__f__zulip_post", {"topic": "Denied"}),
                                           ("b", "Bash", {"command": "simple-chat post --topic Failed -b x"})),
                                self.results(("a", "permission denied", True), ("b", "error", True))],
            "no result": [self.calls(("a", "mcp__f__zulip_reply", {"topic": "Pending"}))],
            "no id": [json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__f__zulip_post", "input": {"topic": "No id"}}]}})],
        }
        for name, lines in cases.items():
            with self.subTest(name):
                self.assertEqual(PL.topics_in_lines(lines), [])
        # A body that quotes a printed line names no topic:  only the command's own output line counts.
        quoted = [self.calls(("a", "Bash", {"command": "simple-chat post --topic Real -b 'posted id 5 to #x › y'"})),
                  self.results(("a", "posted id 8 to #Work-In-Progress › Real", False))]
        self.assertEqual(PL.topics_in_lines(quoted), ["Real"])

    def test_a_topic_with_a_dollar_sign_counts_when_no_shell_read_it(self) -> None:
        lines = [
            self.calls(("a", "mcp__f__zulip_post", {"topic": "Price $5"}),
                       ("b", "Bash", {"command": "simple-chat post --topic 'Cost `x`' -b y"}),
                       ("c", "Bash", {"command": 'simple-chat post --topic "$T" -b y'})),
            self.results(("a", "{}", False), ("b", "posted id 3 to #Work-In-Progress › Cost `x`", False),
                         ("c", "posted id 4 to #Work-In-Progress › Price $6", False)),
        ]
        self.assertEqual(PL.topics_in_lines(lines), ["Price $5", "Cost `x`", "Price $6"])
        single = [self.calls(("a", "Bash", {"command": "simple-chat post --topic 'Price $5' -b y"})),
                  self.results(("a", "{}", False))]
        self.assertEqual(PL.topics_in_lines(single), ["Price $5"])
        # A quoted copy of the text elsewhere in the command does not make an expanding topic literal.
        other = [self.calls(("a", "Bash", {"command": "echo 'x $T y'; simple-chat post --topic $T -b 'a'; "
                                                      "echo '$T'"})),
                 self.results(("a", "{}", False))]
        self.assertEqual(PL.topics_in_lines(other), [])
        # A literal `$VAR` in the command is not the topic, and with no printed line it names nothing.
        bare = [self.calls(("a", "Bash", {"command": 'simple-chat post --topic "$T" -b y'})),
                self.results(("a", "{}", False))]
        self.assertEqual(PL.topics_in_lines(bare), [])

    def test_only_a_command_that_runs_the_program_posts(self) -> None:
        def topics(command: str) -> list[str]:
            return PL.topics_in_lines([self.calls(("a", "Bash", {"command": command})), self.results(("a", "", False))])

        for command in ("echo simple-chat post --topic Demo -b x", "grep -r 'x' simple-chat post --topic Demo",
                        "cat agent-sync post --topic Demo"):
            with self.subTest(command):
                self.assertEqual(topics(command), [])
        for command in ("sudo -u jay simple-chat post --topic Demo -b x", "env A=b simple-chat reply --topic Demo -b x",
                        "A=b ~/.local/bin/simple-chat post --topic Demo -b x",
                        "if true; then simple-chat post --topic Demo -b x; fi",
                        "/usr/bin/sudo simple-chat post --topic Demo -b x",
                        "timeout 10 simple-chat post --topic Demo -b x",
                        "nice -n 5 /usr/bin/env A=b simple-chat post --topic Demo -b x"):
            with self.subTest(command):
                self.assertEqual(topics(command), ["Demo"])

    def test_turn_note_keeps_what_another_hook_wrote_meanwhile(self) -> None:
        before = {"state": "open", "label": "AA", "issues_failed_at": 1.0, "title": "Old"}
        after = dict(before, issues=[{"id": "AFC-1"}], topics=[{"topic": "T", "at": 2.0}])
        after.pop("issues_failed_at")
        fresh = {"state": "closed", "label": "AA", "ended": {"reason": "clear"}, "issues_failed_at": 1.0,
                 "title": "New"}
        merged = PL.turn_note(before, after, fresh)
        self.assertEqual(merged["state"], "closed")
        self.assertEqual(merged["ended"], {"reason": "clear"})
        self.assertEqual(merged["title"], "New")
        self.assertEqual(merged["issues"], [{"id": "AFC-1"}])
        self.assertEqual(merged["topics"], [{"topic": "T", "at": 2.0}])
        self.assertNotIn("issues_failed_at", merged)
        self.assertIsNone(PL.turn_note(before, after, {}))

    def test_merge_topics_keeps_each_once_newest_last_and_bounded(self) -> None:
        old = [{"topic": "A", "at": 1.0}, {"topic": "B", "at": 2.0}, {"topic": "", "at": 3.0}, "junk",
               {"topic": "C", "at": True}]
        self.assertEqual(PL.merge_topics(old, ["A"], 5.0), [{"topic": "B", "at": 2.0}, {"topic": "A", "at": 5.0}])
        many = PL.merge_topics([], ["t%d" % i for i in range(PL.TOPIC_KEEP + 5)], 9.0)
        self.assertEqual(len(many), PL.TOPIC_KEEP)
        self.assertEqual(PL.merge_topics("not a list", [], 1.0), [])
        # What it is given is cleaned the same way:  one entry per topic, never longer than TOPIC_CHARS.
        merged = PL.merge_topics([{"topic": "A b", "at": 1.0}], ["  A   b ", "X" * 90, "", "Price $5"], 7.0)
        self.assertEqual(merged, [{"topic": "A b", "at": 7.0}, {"topic": "X" * PL.TOPIC_CHARS, "at": 7.0},
                                  {"topic": "Price $5", "at": 7.0}])
        # One parse serves both readers of the same lines.
        lines = ["{not json", json.dumps({"type": "user", "message": {"content": "AFC-3"}})]
        parsed = PL.parsed_lines(lines)
        self.assertEqual(PL.issue_ids_in_lines(lines, parsed), PL.issue_ids_in_lines(lines))
        self.assertEqual(PL.topics_in_lines(lines, parsed), [])

    def test_a_rename_event(self) -> None:
        spec = make_spec(events={"session.created": "start", "session.renamed": "rename", "session.deleted": "end"},
                         output="json")
        PL.hook(spec, self.server_env(), io.StringIO(json.dumps({"event": "session.created", "session_id": "s-0001"})),
                io.StringIO(), transport=Calls(self))
        label = self.label("s-0001")
        out = io.StringIO()
        PL.hook(spec, self.server_env(), io.StringIO(json.dumps(
            {"event": "session.renamed", "session_id": "s-0001", "title": "Fresh Name"})), out, transport=Calls(self))
        body = json.loads(out.getvalue())["platform_letter"]
        self.assertEqual((body["event"], body["label"], body["title"]), ("rename", label, "%s: Fresh Name" % label))
        self.assertEqual(self.lookup("s-0001")["title"], "Fresh Name")

    def test_a_malformed_answer_is_refused(self) -> None:
        for answer in (
            '{"outcome": "registered", "label": "ABC", "generation": 1}\n',
            '{"outcome": "registered", "label": "a1", "generation": 1}\n',
            '{"outcome": "registered", "label": "AA", "generation": 0}\n',
            '{"outcome": "registered", "label": 7, "generation": 1}\n',
            "not json at all\n",
            "",
        ):
            with self.subTest(answer=answer):
                def bad(argv, answer=answer):
                    # The answer the registry side would print, read the way the client reads it.
                    return PL._last_json(answer) or {"error": "the registry gave no answer"}

                out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                                transport=bad)
                self.assertIn("No Operator letter yet", out["additionalContext"])
                self.assertEqual(self.note("s-0001")["state"], "failed")

    def test_a_seat_mismatch_takes_no_letter(self) -> None:
        def answer(argv):
            return {"outcome": "seat_mismatch", "label": "", "record_id": "r1"}

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        transport=answer)
        self.assertIn("another seat", out["additionalContext"])
        self.assertNotIn("sessionTitle", out)  # the other seat's title is left as it is
        self.assertEqual(self.note("s-0001")["state"], "mismatch")
        self.assertIsNone(self.prompt("s-0001", "x"))

    def test_compact_only_reminds(self) -> None:
        self.start("s-0001")
        ssh = Calls(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "compact"}, transport=ssh)
        self.assertEqual(ssh.calls, [])
        self.assertNotIn("sessionTitle", out)
        self.assertIn("Codex %s" % self.label("s-0001"), out["additionalContext"])

    def test_unreachable_registry_never_blocks_and_retries_later(self) -> None:
        down = Calls(self, fail=TimeoutError("the registry did not answer"))
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=down)
        self.assertIn("No Operator letter yet", out["additionalContext"])
        self.assertEqual(out["sessionTitle"], "??: New Chat")
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                         "session_title": "??: New Chat"}, transport=down)
        self.assertEqual(out["sessionTitle"], "??: Fix the Build")  # still no letter, and the words are the prompt's
        self.assertEqual(len(down.calls), 1)  # inside the retry window nothing is tried
        later = time.time() + PL.RETRY_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "next",
                         "session_title": "??: Fix the Build"}, now=later)
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % self.label("s-0001"))
        self.assertIn("Operator session letter", out["additionalContext"])  # the letter is news to the model
        self.assertEqual(self.lookup("s-0001")["title"], "Fix the Build")

    def test_a_registry_answer_that_is_not_json_is_a_failed_registration(self) -> None:
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        transport=lambda argv: {"label": 7})
        self.assertIn("malformed label", out["additionalContext"])
        self.assertEqual(out["sessionTitle"], "??: New Chat")

    def test_nothing_that_goes_wrong_blocks_a_chat(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        PL.register_spec(self.spec)
        self.addCleanup(PL.SPECS.pop, "codex", None)

        def boom(cmd, **kwargs):
            raise RuntimeError("anything at all")

        for stdin in ("not json", "[]", ""):
            code = PL.main(["hook", "--platform", "codex"], env=self.server_env(), stdin=io.StringIO(stdin),
                           stdout=out, stderr=err, transport=boom)
            self.assertEqual(code, 0, stdin)
        self.assertEqual(out.getvalue(), "")
        # A registry call that blows up is an error answer like any other: a line of context, exit 0.
        code = PL.main(["hook", "--platform", "codex"], env=self.server_env(), stderr=err, transport=boom, stdout=out,
                       stdin=io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "s-0001"})))
        self.assertEqual(code, 0)
        self.assertIn("No Operator letter yet (the registry call failed (RuntimeError))", out.getvalue())
        # An adapter's own accessor that blows up is caught by the hook itself.
        def broken_accessor(payload, env):
            raise KeyError("the adapter has a bug")

        PL.register_spec(make_spec(session_id=broken_accessor))
        out = io.StringIO()
        code = PL.main(["hook", "--platform", "codex"], env=self.server_env(), stdout=out, stderr=err,
                       stdin=io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "s-0001"})))
        self.assertEqual((code, out.getvalue()), (0, ""))
        self.assertIn("KeyError", err.getvalue())
        spec = make_spec(session_id=broken_accessor)  # and the library's own entry, called directly, never raises
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            code = PL.hook(spec, self.server_env(),
                           io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "s-0001"})),
                           io.StringIO())
        self.assertEqual(code, 0)
        self.assertIn("KeyError", stderr.getvalue())
        for argv in (["hook"], ["hook", "--platform", "nosuch"], ["hook", "--bogus"], ["hook", "--platform"]):
            self.assertEqual(PL.main(argv, env=self.env(), stdin=io.StringIO("{}"), stdout=out, stderr=err), 0, argv)

    def test_an_adapter_can_own_its_hook_entry(self) -> None:
        """A platform whose payload has no event name or whose answer has a shape of its own defines `hook_main`; the
        library entry calls it with what `hook` takes, minus the spec.  A retired flag (`--remote`, from before the ssh
        hop was deleted) is a command line the hook does not read:  it does nothing and exits 0."""
        seen = {}

        def hook_main(env, stdin, stdout, **kwargs):
            seen.update(kwargs, stdin=stdin.read())
            stdout.write('{"own": true}\n')
            return 0

        module = types.ModuleType("simple_tools.platform_letter_owned")
        module.SPEC = make_spec(platform="owned", seat="CODEX", source="owned-hook")
        module.hook_main = hook_main
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"simple_tools.platform_letter_owned": module}):
            code = PL.main(["hook", "--platform", "owned", "--location", "server", "--remote", "mac"],
                           env=self.env(), stdin=io.StringIO("payload"), stdout=out, stderr=err)
            self.assertEqual((code, out.getvalue(), seen), (0, "", {}))
            code = PL.main(["hook", "--platform", "owned", "--location", "server"],
                           env=self.env(), stdin=io.StringIO("payload"), stdout=out, stderr=err)
        self.assertEqual((code, out.getvalue(), seen["stdin"]), (0, '{"own": true}\n', "payload"))
        self.assertEqual(seen["location_flag"], "server")

    def test_a_session_id_that_is_not_one_is_ignored(self) -> None:
        ssh = Calls(self)
        for sid in ("", "x", "../../etc/passwd", "a b c d", 5):
            self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"}, transport=ssh)
        self.assertEqual(ssh.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "CODEX")))

    def test_out_of_scope_and_unknown_hosts(self) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}
        ssh = Calls(self)
        # BotFleet and every other launcher: no call, no note, no output.
        for extra in (
            {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-BUILDER"},
            {"AGENT_LAUNCHER": "simple-tools-wake"},
            {"AGENT_LAUNCH_SEAT": "BF-BUILDER"},
            {"SESSION_LETTER": "0"},
        ):
            with self.subTest(extra=extra):
                self.assertIsNone(self.hook(payload, env=self.server_env(**extra), transport=ssh))
        self.assertEqual(ssh.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "CODEX")))
        # A host that does not state its location:  no call, and the chat is a "??" chat until it does.
        out = self.hook(payload, env=self.env(), transport=ssh)
        self.assertIn("No Operator letter yet", out["additionalContext"])
        self.assertIn("location is not stated", out["additionalContext"])
        self.assertEqual(out["sessionTitle"], "??: New Chat")
        self.assertEqual(ssh.calls, [])
        self.assertIsNone(self.hook(dict(payload, hook_event_name="Stop"), transport=ssh))  # not an event of ours

    def test_a_launchers_own_spec_asks_for_its_own_platform_letter(self) -> None:
        # BotFleet runs Claude as its engine: its bots never trigger the Claude hook, and a spec of its own asks for
        # a "botfleet" letter under the bot's own seat and session id.
        bot = make_spec(platform="botfleet", seat=PL.from_env("AGENT_LAUNCH_SEAT"), source="botfleet-letter-hook",
                        display="BotFleet", launchers=("botfleet",), session_id=PL.from_env("BOTFLEET_SESSION_ID"))
        seen = []

        def answer(argv):
            seen.append(argv)
            return {"outcome": "registered", "label": "AA", "generation": 1, "record_id": "r1"}

        env = self.server_env(AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="bf-builder", BOTFLEET_SESSION_ID="bf-0001")
        out = self.hook({"hook_event_name": "SessionStart", "source": "startup"}, spec=bot, env=env, transport=answer)
        self.assertEqual(out["sessionTitle"], "AA: New Chat")
        self.assertEqual(seen[0][seen[0].index("--platform") + 1], "botfleet")
        self.assertEqual(seen[0][seen[0].index("--seat") + 1], "BF-BUILDER")
        self.assertTrue(os.path.exists(os.path.join(self.state, "BF-BUILDER", "letters", "bf-0001.json")))
        # Another launcher, or no seat at all, takes nothing.
        seen.clear()
        for extra in ({"AGENT_LAUNCHER": "grok-bot"}, {"AGENT_LAUNCH_SEAT": "not a seat!"}, {"AGENT_LAUNCH_SEAT": ""}):
            self.hook({"hook_event_name": "SessionStart", "source": "startup"}, spec=bot,
                      env=dict(env, **extra), transport=answer)
        self.assertEqual(seen, [])

    def test_a_platform_that_runs_only_on_the_mac_takes_no_server_letter(self) -> None:
        spec = make_spec(platform="grok", seat="GROK", locations=("mac",))
        ssh = Calls(self)
        self.assertIsNone(self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001"}, spec=spec, transport=ssh))
        self.assertEqual(ssh.calls, [])
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001"}, spec=spec, env=self.env(),
                        platform="darwin", transport=ssh)
        self.assertTrue(out["sessionTitle"].endswith(": New Chat"))
        for bad in ((), ("cloud",), ("mac", "moon")):
            with self.assertRaises(ValueError):
                make_spec(locations=bad)

    def test_headless_and_subagent_runs_take_no_letter(self) -> None:
        spec = make_spec(kind=lambda p, e: str(p.get("kind") or "chat"), parent_id=PL.from_payload("parent_id"))
        ssh = Calls(self)
        for kind in ("headless", "subagent"):
            self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "kind": kind}, spec=spec, transport=ssh)
        self.assertEqual(ssh.calls, [])

    def test_a_sub_thread_asks_for_the_pair_after_its_parent(self) -> None:
        spec = make_spec(kind=lambda p, e: str(p.get("kind") or "chat"), parent_id=PL.from_payload("parent_id"))
        seen = []

        def answer(argv):
            seen.append(argv)
            return {"outcome": "registered", "label": "CD", "generation": 1, "record_id": "r1"}

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "c-0001", "kind": "subagent",
                         "parent_id": "p-0001"}, spec=spec, transport=answer)
        self.assertEqual(out["sessionTitle"], "CD: New Chat")
        self.assertIn("--parent-session-id=p-0001", seen[0])
        self.assertIn("--platform", seen[0])
        self.assertEqual(seen[0][seen[0].index("--platform") + 1], "codex")
        self.assertEqual(self.note("c-0001")["parent"], "p-0001")
        # A later retry of a failed registration still names the parent.
        seen.clear()
        answers = iter([
            {"error": "down"},
            {"outcome": "registered", "label": "CD", "generation": 1},
            {"ok": True, "label": "CD", "generation": 1},
        ])
        self.hook({"hook_event_name": "SessionStart", "session_id": "c-0002", "kind": "subagent",
                   "parent_id": "p-0001"}, spec=spec, transport=lambda argv: seen.append(argv) or next(answers))
        later = time.time() + PL.RETRY_SECONDS + 5
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "c-0002", "prompt": "go"}, spec=spec,
                  transport=lambda argv: seen.append(argv) or next(answers), now=later)
        self.assertEqual([a[0] for a in seen], ["register", "register", "title"])
        self.assertTrue(all("--parent-session-id=p-0001" in a for a in seen if a[0] == "register"))

    def test_the_registry_arguments(self) -> None:
        chat = PL.Chat(self.spec, self.env(), "server", "s-0001", now=0.0)
        self.assertEqual(
            chat.argv("register", "--dash Title", "p-0001"),
            ["register", "--platform", "codex", "--location", "server", "--session-id", "s-0001",
             "--seat", "CODEX", "--source", "codex-letter-hook", "--title=--dash Title",
             "--parent-session-id=p-0001"],
        )
        self.assertNotIn("--parent-session-id=p-0001", chat.argv("touch", "", "p-0001"))
        self.assertFalse(any(a.startswith("--title") for a in chat.argv("touch")))
        self.assertEqual(self.registry(*chat.argv("register", "--dash Title"))[0], 0)  # a dash title still parses
        self.assertEqual(self.lookup("s-0001")["title"], "--dash Title")

    def test_a_title_writer_names_the_chat_natively(self) -> None:
        seen = []

        def writer(request: PL.TitleRequest) -> bool:
            seen.append((request.reason, request.title, request.words, request.current, request.session_id))
            return True

        spec = make_spec(title_writer=writer, title_in_output=False)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, spec=spec)
        label = self.label("s-0001")
        self.assertNotIn("sessionTitle", out)  # the writer did it, so the output has no title
        self.assertEqual(seen, [("start", "%s: New Chat" % label, "New Chat", "", "s-0001")])
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                   "session_title": "%s: New Chat" % label}, spec=spec)
        self.assertEqual(seen[-1][:3], ("prompt", "%s: Fix the Build" % label, "Fix the Build"))
        self.assertFalse(self.note("s-0001")["pending"])

    def test_a_failing_title_writer_is_retried_and_never_blocks(self) -> None:
        results = iter([False, RuntimeError("no thread yet"), True])
        reasons = []

        def writer(request: PL.TitleRequest) -> bool:
            reasons.append(request.reason)
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        spec = make_spec(title_writer=writer, title_in_output=False)
        t0 = time.time()
        step = PL.RETRY_SECONDS + 5

        def prompt(now: float) -> None:
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "/review"},
                      spec=spec, now=now)

        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, spec=spec, now=t0)
        label = self.label("s-0001")
        self.assertTrue(self.note("s-0001")["pending"])
        prompt(t0 + step)
        self.assertTrue(self.note("s-0001")["pending"])  # the writer raised: still pending, still exit 0
        prompt(t0 + 2 * step)
        self.assertFalse(self.note("s-0001")["pending"])
        self.assertEqual(self.note("s-0001")["shown"], "%s: New Chat" % label)
        self.assertEqual(reasons, ["start", "retry", "retry"])

    def test_a_failing_title_writer_is_not_asked_again_on_every_prompt(self) -> None:
        calls = []

        def writer(request: PL.TitleRequest) -> bool:
            calls.append(request.reason)
            return False

        spec = make_spec(title_writer=writer, title_in_output=False)
        t0 = time.time()
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, spec=spec, now=t0)
        for i in range(1, 6):  # five prompts inside the ten minutes: the writer is left alone
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "/review"},
                      spec=spec, now=t0 + i * 60)
        self.assertEqual(calls, ["start"])
        self.assertTrue(self.note("s-0001")["pending"])
        for i in range(3):  # past the ten minutes it is tried once, and that failure starts a new wait
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "/review"},
                      spec=spec, now=t0 + PL.RETRY_SECONDS + 5 + i)
        self.assertEqual(calls, ["start", "retry"])
        # A new title is not held back by the wait: it is a different request.
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build"},
                  spec=spec, now=t0 + PL.RETRY_SECONDS + 9)
        self.assertEqual(calls, ["start", "retry", "prompt"])

    def test_a_writer_that_fails_falls_back_to_the_output(self) -> None:
        spec = make_spec(title_writer=lambda request: False, title_in_output=True)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, spec=spec)
        self.assertTrue(out["sessionTitle"].endswith(": New Chat"))
        self.assertFalse(self.note("s-0001")["pending"])

    def test_the_title_is_applied_only_after_the_sources_the_platform_honors(self) -> None:
        out = self.start("s-0001", source="clear")
        self.assertNotIn("sessionTitle", out)
        spec = make_spec(title_sources=("startup", "resume", "fork", "clear"))
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "clear"}, spec=spec)
        self.assertIn("sessionTitle", out)

    def test_output_styles(self) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}
        out = io.StringIO()
        PL.hook(make_spec(output="text"), self.server_env(), io.StringIO(json.dumps(payload)), out, transport=Calls(self))
        text = out.getvalue()
        self.assertTrue(text.startswith("Operator session letter: Codex "))
        self.assertNotIn("{", text)
        out = io.StringIO()
        PL.hook(make_spec(output="json"), self.server_env(),
                io.StringIO(json.dumps(dict(payload, session_id="s-0002"))), out, transport=Calls(self))
        body = json.loads(out.getvalue())["platform_letter"]
        self.assertEqual((body["event"], body["platform"]), ("start", "codex"))
        self.assertRegex(body["label"], PAIR)
        self.assertTrue(body["title"].endswith(": New Chat"))
        out = io.StringIO()
        PL.hook(make_spec(title_in_output=False), self.server_env(),
                io.StringIO(json.dumps(dict(payload, session_id="s-0003"))), out, transport=Calls(self))
        self.assertNotIn("sessionTitle", out.getvalue())

    def test_a_secret_after_its_keyword_reaches_neither_the_title_nor_the_operator(self) -> None:
        value = "abcdefghijklmnop1234567"
        ssh = Calls(self)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=ssh)
        label = self.label("s-0001")
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001",
                         "prompt": "api" + "_key: " + value + " fix login", "session_title": "%s: New Chat" % label},
                        transport=ssh)
        self.assertEqual(out["sessionTitle"], "%s: Api Key Fix Login" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "Api Key Fix Login")
        with open(os.path.join(self.state, "CODEX", "letters", "s-0001.json")) as fh:
            self.assertNotIn(value, fh.read())
        self.assertNotIn(value, json.dumps(ssh.calls))

    def test_a_fork_does_not_keep_the_letter_of_the_chat_it_was_made_from(self) -> None:
        self.start("s-0001")
        source = self.label("s-0001")
        ssh = Calls(self)
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "fork",
                   "session_title": "%s: Fix Login" % source}
        out = self.hook(payload, transport=ssh)
        label = self.label("s-0002")
        self.assertNotEqual(label, source)
        self.assertEqual(out["sessionTitle"], "%s: Fix Login" % label)  # one letter, the fork's own
        self.assertEqual(self.note("s-0002")["title"], "Fix Login")
        self.assertEqual(self.lookup("s-0002")["title"], "Fix Login")  # the Operator has the words, not the prefix
        self.assertEqual(ssh.ops(), ["register"])
        # A fork of the fork: still one prefix, each time.
        again = self.hook(dict(payload, session_id="s-0003", session_title=out["sessionTitle"]))
        self.assertEqual(again["sessionTitle"], "%s: Fix Login" % self.label("s-0003"))
        # A platform that has not applied the hook's title shows the inherited one on the next prompt: no rename.
        self.assertIsNone(self.prompt("s-0002", "go on", session_title="%s: Fix Login" % source))
        self.assertEqual(self.lookup("s-0002")["title"], "Fix Login")

    def test_a_fork_of_an_untitled_chat_is_titled_from_its_own_first_prompt(self) -> None:
        self.start("s-0001")
        source = self.label("s-0001")
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "fork",
                         "session_title": "%s: New Chat" % source})
        label = self.label("s-0002")
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % label)
        out = self.prompt("s-0002", "write the tests", session_title="%s: New Chat" % source)
        self.assertEqual(out["sessionTitle"], "%s: Write the Tests" % label)

    def test_a_cleared_chat_still_showing_the_old_name_is_not_renamed_by_a_bare_command(self) -> None:
        self.start("s-0001", source="clear", session_title="ZZ: Previous Chat")
        ssh = Calls(self)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "/review",
                         "session_title": "ZZ: Previous Chat"}, transport=ssh)
        self.assertIsNone(out)
        self.assertEqual(ssh.calls, [])
        self.assertFalse(self.note("s-0001")["titled"])

    def test_a_title_the_operator_did_not_get_is_sent_again(self) -> None:
        ssh = FlakyCalls(self, ops=("title",))
        t0 = time.time()
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=ssh, now=t0)
        label = self.label("s-0001")
        title = "%s: Fix the Login Page" % label

        def prompt(text: str, shown: str, now: float) -> dict | None:
            return self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": text,
                              "session_title": shown}, transport=ssh, now=now)

        out = prompt("fix the login page", "%s: New Chat" % label, t0 + 1)
        self.assertEqual(out["sessionTitle"], title)  # the chat is titled at once
        self.assertEqual(self.lookup("s-0001")["title"], "")  # but the Mac did not answer
        self.assertEqual(self.note("s-0001")["unsent"], "Fix the Login Page")
        for i in range(5):  # the next prompts, inside the ten minutes: no hop at all
            prompt("more %d" % i, title, t0 + 10 + i)
        self.assertEqual(ssh.ops(), ["register", "title"])
        prompt("later", title, t0 + PL.RETRY_SECONDS + 5)  # then the words go again
        self.assertEqual(ssh.ops(), ["register", "title", "title"])
        self.assertEqual(self.lookup("s-0001")["title"], "Fix the Login Page")
        self.assertNotIn("unsent", self.note("s-0001"))
        prompt("and later", title, t0 + PL.RETRY_SECONDS + 6)
        self.assertEqual(len(ssh.calls), 3)

    def test_a_touch_the_operator_did_not_get_is_tried_again_after_the_wait(self) -> None:
        ssh = FlakyCalls(self, ops=("touch",))
        t0 = time.time()
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=ssh, now=t0)
        title = "%s: Fix the Build" % self.label("s-0001")
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                   "session_title": title}, transport=ssh, now=t0)

        def prompt(now: float) -> None:
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x",
                       "session_title": title}, transport=ssh, now=now)

        late = t0 + PL.TOUCH_SECONDS + 5
        prompt(late)
        self.assertEqual(self.note("s-0001")["touched_at"], t0)  # not marked: the Mac did not answer
        prompt(late + 30)
        self.assertEqual(ssh.ops(), ["register", "title", "touch"])  # and not tried on every prompt
        prompt(late + PL.RETRY_SECONDS + 1)
        self.assertEqual(ssh.ops(), ["register", "title", "touch", "touch"])
        self.assertEqual(self.note("s-0001")["touched_at"], late + PL.RETRY_SECONDS + 1)

    def test_a_registry_restored_from_an_older_copy_registers_the_chat_again(self) -> None:
        self.start("s-0001")
        old = self.label("s-0001")
        self.prompt("s-0001", "fix the build", session_title="%s: New Chat" % old)
        for suffix in ("", "-wal", "-shm", "-journal"):  # the Mac's registry file is replaced by an older copy
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)
        OR.SessionRegistry(self.db).close_db()
        self.start("s-0002")  # a new chat takes the letter in the restored registry
        self.assertEqual(self.label("s-0002"), old)
        later = time.time() + PL.TOUCH_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                         "session_title": "%s: Fix the Build" % old}, now=later)
        new = self.label("s-0001")
        self.assertNotEqual(new, old)  # two live chats never share a letter
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % new)
        self.assertIn('"@**Codex** %s"' % new, out["additionalContext"])  # and the model is told the new one
        self.assertEqual((self.lookup("s-0001")["label"], self.lookup("s-0001")["title"]), (new, "Fix the Build"))
        self.assertEqual(self.lookup("s-0002")["label"], old)

    def test_a_title_the_registry_has_no_chat_for_registers_the_chat_again(self) -> None:
        self.start("s-0001")
        old = self.label("s-0001")
        for suffix in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)
        OR.SessionRegistry(self.db).close_db()
        out = self.prompt("s-0001", "fix the build", session_title="%s: New Chat" % old)  # the title op finds nothing
        label = self.label("s-0001")
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "Fix the Build")
        self.assertNotIn("unsent", self.note("s-0001"))

    def test_a_close_for_a_chat_the_registry_forgot_closes_the_note(self) -> None:
        self.start("s-0001")
        for suffix in ("", "-wal", "-shm", "-journal"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)
        OR.SessionRegistry(self.db).close_db()
        self.end("s-0001", "archived")
        self.assertEqual(self.note("s-0001")["state"], "closed")

    def test_a_title_that_cannot_cross_ssh_never_stops_the_hook(self) -> None:
        inner = Calls(self)

        def strict(cmd, **kwargs):  # what subprocess does with an argument it cannot encode
            for arg in cmd:
                arg.encode("utf-8")  # UnicodeEncodeError on a lone surrogate
                if "\x00" in arg:
                    raise ValueError("embedded null byte")
            return inner(cmd, **kwargs)

        for n, bad in enumerate(("Fix \ud83d Login", "Fix \x00 Login")):
            sid = "s-%04d" % (n + 1)
            out = self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "resume",
                             "session_title": bad}, runner=strict)
            label = self.label(sid)
            self.assertEqual(out["sessionTitle"], "%s: Fix Login" % label)
            self.assertEqual(self.lookup(sid)["title"], "Fix Login")
            for i in range(3):  # the prompts after it work, and the registry is touched in time
                self.hook({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "more %d" % i,
                           "session_title": bad}, runner=strict, now=time.time() + PL.TOUCH_SECONDS * (i + 1))
            self.assertGreaterEqual(inner.ops().count("touch"), n + 1)

    def test_a_state_folder_that_cannot_be_written_still_gives_the_chat_its_letter(self) -> None:
        os.makedirs(os.path.join(self.state, "CODEX"))
        with open(os.path.join(self.state, "CODEX", "letters"), "w") as fh:
            fh.write("a file where the folder should be")
        ssh = Calls(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=ssh)
        label = self.lookup("s-0001")["label"]  # the registration was made...
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % label)  # ...and the chat was told and titled
        self.assertIn('"@**Codex** %s"' % label, out["additionalContext"])
        # With no note to go on, the next prompt registers again, which only resumes the chat.
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                         "session_title": "%s: New Chat" % label}, transport=ssh)
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % label)
        self.assertEqual(self.lookup("s-0001")["label"], label)
        self.assertFalse(PL.Chat(self.spec, self.server_env(), "server", "s-0001", transport=ssh, now=0.0).write({}))

    def test_the_note_is_never_written_through_a_planted_symlink(self) -> None:
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as fh:
            fh.write("keep me")
        chat = PL.Chat(self.spec, self.server_env(), "server", "s-0001", now=0.0)
        os.makedirs(os.path.dirname(chat.path), mode=0o700, exist_ok=True)
        # the name the writer used to take for its temporary file, which anyone who can write the folder could guess
        os.symlink(victim, "%s.%d.tmp" % (chat.path, os.getpid()))
        self.assertTrue(chat.write({"state": "open"}))
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertEqual(chat.read().get("state"), "open")

    def test_a_letter_that_arrives_on_a_later_prompt_is_announced(self) -> None:
        down = Calls(self, fail=subprocess.TimeoutExpired("ssh", 12))
        t0 = time.time()
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        transport=down, now=t0)
        self.assertIn("at most every ten minutes", out["additionalContext"])
        self.assertNotIn("next prompt", out["additionalContext"])
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build"},
                        now=t0 + PL.RETRY_SECONDS + 5)
        label = self.label("s-0001")
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % label)
        self.assertIn('"@**Codex** %s"' % label, out["additionalContext"])  # the model learns its letter
        self.assertIn('starts with "%s: "' % label, out["additionalContext"])
        # A platform that names its chats through a writer has no title in the output: the letter is still said.
        spec = make_spec(title_writer=lambda request: True, title_in_output=False)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "startup"}, spec=spec,
                  transport=down, now=t0)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "go"}, spec=spec,
                        now=t0 + PL.RETRY_SECONDS + 5)
        self.assertNotIn("sessionTitle", out)
        self.assertIn('"@**Codex** %s"' % self.label("s-0002"), out["additionalContext"])
        # A later prompt says nothing more.
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "x"},
                                    spec=spec, now=t0 + PL.RETRY_SECONDS + 9))

    def test_a_quiet_event_runs_and_prints_nothing(self) -> None:
        spec = make_spec(events={"SubagentStop": "prompt", "UserPromptSubmit": "prompt"},
                         quiet_events=("SubagentStop",))
        out = self.hook({"hook_event_name": "SubagentStop", "session_id": "s-0001", "prompt": "go"}, spec=spec)
        self.assertIsNone(out)
        self.assertRegex(self.lookup("s-0001")["label"], PAIR)  # the chat was registered all the same
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "go"}, spec=spec)
        self.assertIn("additionalContext", out)  # and an event that is not quiet still speaks

    def parent_spec(self, **over) -> PL.PlatformSpec:
        """A platform whose payload names the chat's parent in `parent`, and that can look a parent up by its id."""
        families = {"c-0001": "p-0001", "c-0002": "c-0001", "loop-a": "loop-b", "loop-b": "loop-a", "q-child": "q-0001"}
        return make_spec(
            parent_id=lambda payload, env: payload.get("parent") or families.get(payload.get("session_id"), ""),
            kind=lambda payload, env: "subagent" if payload.get("parent") else "chat",
            chat_payload=lambda sid: {"hook_event_name": "SessionStart", "source": "resume", "session_id": sid},
            title_in_output=True,
            **over,
        )

    def child_start(self, sid: str, parent: str, spec: PL.PlatformSpec, **kw) -> dict | None:
        return self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "startup", "parent": parent},
                         spec=spec, **kw)

    def test_a_parent_that_is_not_registered_is_registered_before_its_sub_thread(self) -> None:
        spec = self.parent_spec()
        ssh = Calls(self)
        self.child_start("c-0001", "p-0001", spec, transport=ssh)
        self.assertEqual(ssh.ops(), ["register", "register"])  # the parent's, then the child's
        parent, child = self.label("p-0001"), self.label("c-0001")
        self.assertRegex(parent, PAIR)
        self.assertLess(parent, child)  # the parent has the earlier pair, whatever the registry's rule
        if PL._accepts(OR.register_session, "parent_session_id"):
            self.assertEqual(parent[0], child[0])  # and the child sits in its row
        note = self.note("p-0001")
        self.assertEqual((note["state"], note["titled"], note["announce"]), ("open", False, True))
        self.assertEqual(self.lookup("p-0001")["state"], "active")

    def test_the_parents_own_prompt_titles_it_and_tells_the_model_its_letter(self) -> None:
        spec = self.parent_spec()
        self.child_start("c-0001", "p-0001", spec)
        label = self.label("p-0001")
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "p-0001", "prompt": "fix the build"},
                        spec=spec)
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % label)
        self.assertIn('"@**Codex** %s"' % label, out["additionalContext"])
        self.assertNotIn("announce", self.note("p-0001"))
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "p-0001", "prompt": "more",
                                     "session_title": "%s: Fix the Build" % label}, spec=spec))

    def test_a_parent_that_is_registered_costs_no_hop_and_a_failed_one_is_tried_again(self) -> None:
        spec = self.parent_spec()
        self.hook({"hook_event_name": "SessionStart", "session_id": "p-0001", "source": "startup"}, spec=spec)
        ssh = Calls(self)
        self.child_start("c-0001", "p-0001", spec, transport=ssh)
        self.assertEqual(ssh.ops(), ["register"])  # only the child
        # A parent whose own registration was refused a moment ago (not a silent host: the registry answered) is not
        # waited for: the sub-thread would otherwise take a main pair ahead of it.
        t0 = time.time()

        def refuses(argv):
            return {"error": "no Operator registry on this host"}

        self.hook({"hook_event_name": "SessionStart", "session_id": "q-0001", "source": "startup"}, spec=spec,
                  runner=refuses, now=t0)
        self.assertEqual(self.note("q-0001")["state"], "failed")
        self.child_start("q-child", "q-0001", spec, now=t0 + 5)
        self.assertEqual(self.note("q-0001")["state"], "open")
        self.assertLess(self.label("q-0001"), self.label("q-child"))

    def test_grandparents_are_registered_first_and_a_loop_ends(self) -> None:
        spec = self.parent_spec()
        ssh = Calls(self)
        self.child_start("c-0002", "c-0001", spec, transport=ssh)  # c-0001 is itself a child of p-0001
        self.assertEqual(ssh.ops(), ["register", "register", "register"])
        labels = [self.label(sid) for sid in ("p-0001", "c-0001", "c-0002")]
        self.assertEqual(labels, sorted(labels))
        self.assertEqual(len(set(labels)), 3)
        ssh = Calls(self)
        self.child_start("loop-c", "loop-a", spec, transport=ssh)  # loop-a and loop-b each name the other
        self.assertLessEqual(len(ssh.calls), PL.PARENT_DEPTH + 1)
        self.assertRegex(self.label("loop-c"), PAIR)

    def test_a_spec_that_cannot_look_a_parent_up_leaves_it_alone(self) -> None:
        spec = self.parent_spec()
        spec = make_spec(parent_id=spec.parent_id, kind=spec.kind, title_in_output=True)
        ssh = Calls(self)
        self.child_start("c-0001", "p-0001", spec, transport=ssh)
        self.assertEqual(ssh.ops(), ["register"])
        self.assertIsNone(self.lookup("p-0001"))

    def test_a_parent_that_is_no_chat_or_has_another_seat_is_left_alone(self) -> None:
        spec = self.parent_spec()
        headless = make_spec(
            parent_id=spec.parent_id, chat_payload=spec.chat_payload, title_in_output=True,
            kind=lambda payload, env: "subagent" if payload.get("parent") else "headless",
        )
        self.child_start("c-0001", "p-0001", headless)
        self.assertIsNone(self.lookup("p-0001"))
        self.assertRegex(self.label("c-0001"), PAIR)
        PL.Chat(spec, self.server_env(), "server", "m-0001", transport=Calls(self), now=0.0).write(
            {"state": "mismatch", "label": ""}
        )
        self.child_start("c-0003", "m-0001", spec)
        self.assertIsNone(self.lookup("m-0001"))  # a chat held by another seat is not registered by this one

    def test_show_reads_the_note_without_a_network(self) -> None:
        self.start("s-0001")
        out = io.StringIO()
        self.assertEqual(PL.show(self.spec, ["--session-id", "s-0001"], self.env(), out), 0)
        self.assertEqual(json.loads(out.getvalue())["label"], self.label("s-0001"))
        out = io.StringIO()
        self.assertEqual(PL.show(self.spec, ["--session-id", "s-9999"], self.env(), out), 1)
        out = io.StringIO()
        self.assertEqual(PL.show(self.spec, [], self.env(CODEX_THREAD_ID="s-0001"), out), 2)  # no accessor for env
        spec = make_spec(session_id=PL.from_env("CODEX_THREAD_ID"))
        out = io.StringIO()
        self.assertEqual(PL.show(spec, [], self.env(CODEX_THREAD_ID="s-0001"), out), 0)

    def test_end_to_end_through_the_entry_point(self) -> None:
        """The hook through `main`, as a platform's hook command runs it, with the assigner served in process."""
        spec = make_spec(platform="faux", source="faux-hook")
        out = io.StringIO()
        with mock.patch.dict(PL.SPECS, {"faux": spec}):
            code = PL.main(
                ["hook", "--platform", "faux", "--location", "server"],
                env=self.env(),
                stdin=io.StringIO(json.dumps({"hook_event_name": "SessionStart", "session_id": "s-0001",
                                              "source": "startup"})),
                stdout=out,
            )
        self.assertEqual(code, 0)
        label = self.label("s-0001")
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["sessionTitle"], "%s: New Chat" % label)
        self.assertEqual(self.lookup("s-0001", platform="faux")["label"], label)

    # ---- No letter:  "??: <Title>", asking again, and `simple-chat letter request` (owner, Nov 11, 2026) ----

    def no_letters(self):
        """Every pair live:  the assigner refuses the next chat."""
        return mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA",)})

    def test_every_way_of_getting_no_letter_titles_the_chat_with_the_mark(self) -> None:
        start = {"hook_event_name": "SessionStart", "source": "startup"}
        with self.subTest("the assigner refuses: every pair is live"), self.no_letters():
            self.start("s-0001")
            out = self.hook(dict(start, session_id="s-0002"))
            self.assertEqual(out["sessionTitle"], "??: New Chat")
            self.assertIn("are live", out["additionalContext"])
        with self.subTest("not configured"):
            bare_env = {k: v for k, v in self.server_env().items() if k != registry_api.URL_ENV}

            def unconfigured(argv):
                out = io.StringIO()
                PL.registry_main(list(argv), env=bare_env, stdout=out)
                return PL._last_json(out.getvalue())

            out = self.hook(dict(start, session_id="s-0003"), transport=unconfigured)
            self.assertEqual(out["sessionTitle"], "??: New Chat")
            self.assertIn("no registry API URL is configured", out["additionalContext"])
        with self.subTest("the assigner does not answer"):
            down = Calls(self, fail=TimeoutError("down"))
            out = self.hook(dict(start, session_id="s-0004", session_title="Fix Login"), transport=down)
            self.assertEqual(out["sessionTitle"], "??: Fix Login")
        with self.subTest("an error answer"):
            out = self.hook(dict(start, session_id="s-0005"), transport=lambda argv: {"error": "boom"})
            self.assertEqual(out["sessionTitle"], "??: New Chat")
            self.assertIn("(boom)", out["additionalContext"])
        for sid in ("s-0002", "s-0003", "s-0004", "s-0005"):
            note = self.note(sid)
            self.assertEqual((note["state"], note["label"]), ("failed", ""))

    def test_the_mark_is_never_doubled(self) -> None:
        down = Calls(self, fail=TimeoutError("down"))
        for n, current in enumerate(("??: Fix Login", "??: ??: Fix Login", "AB: Fix Login", "Fix Login")):
            with self.subTest(current=current):
                sid = "s-%04d" % (n + 1)
                out = self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "resume",
                                 "session_title": current}, transport=down)
                # A chat already showing the mark is left as it is:  no title in the output.
                self.assertEqual(out.get("sessionTitle", current), "??: Fix Login")
                self.assertEqual(self.note(sid)["want"], "??: Fix Login")
        self.assertEqual(PL.bare("??: ??: Fix Login"), "Fix Login")
        self.assertEqual(PL.bare("??: Fix Login", ("CD",)), "Fix Login")
        self.assertEqual(PL.bare("??:"), "")
        self.assertEqual(PL.compose(PL.UNLETTERED, ""), "??: New Chat")

    def test_a_chat_already_on_the_mark_is_not_retitled_on_every_prompt(self) -> None:
        down = Calls(self, fail=TimeoutError("down"))
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, transport=down)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix login",
                         "session_title": "??: New Chat"}, transport=down)
        self.assertEqual(out["sessionTitle"], "??: Fix Login")
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                                     "session_title": "??: Fix Login"}, transport=down))
        self.assertEqual(len(down.calls), 1)  # the stamp holds every prompt inside the wait

    def test_a_no_letter_chat_whose_platform_keeps_its_own_title_is_retitled_once_per_name(self) -> None:
        # A platform that will not take the hook's title goes on showing its own:  the mark is sent once for each
        # name it shows, never on every prompt.
        down = Calls(self, fail=TimeoutError("down"))
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup",
                         "session_title": "Review PR"}, transport=down)
        self.assertEqual(out["sessionTitle"], "??: Review PR")
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go on"}
        for _ in range(3):
            self.assertIsNone(self.hook(dict(prompt, session_title="Review PR"), transport=down))
        out = self.hook(dict(prompt, session_title="Another Name"), transport=down)
        self.assertEqual(out["sessionTitle"], "??: Another Name")
        self.assertIsNone(self.hook(dict(prompt, session_title="Another Name"), transport=down))
        self.assertEqual(len(down.calls), 1)

    # ---- A title the platform will not take from the hook:  the adapter's `title_advice` ----

    def test_title_advice_goes_to_the_model_with_the_start_and_is_not_repeated_on_every_prompt(self) -> None:
        asked: list[PL.TitleRequest] = []
        line = ["Set this chat's title yourself."]

        def advise(request: PL.TitleRequest) -> str:
            asked.append(request)
            return line[0]

        spec = make_spec(title_advice=advise)
        t0 = time.time()
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        spec=spec, now=t0)
        label = self.label("s-0001")
        # The reminder is still there, and the advice follows it after a sentence gap.
        self.assertIn('"@**Codex** %s"' % label, out["additionalContext"])
        self.assertTrue(out["additionalContext"].endswith("renamed.  " + line[0]), out["additionalContext"])
        request = asked[0]
        self.assertEqual((request.label, request.title, request.reason, request.session_id),
                         (label, "%s: New Chat" % label, "start", "s-0001"))
        self.assertIn(label, request.held)
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix login",
                  "session_title": "%s: New Chat" % label}
        first = self.hook(prompt, spec=spec, now=t0 + 1)
        self.assertEqual(first["sessionTitle"], "%s: Fix Login" % label)
        self.assertNotIn("additionalContext", first)  # asked at the start:  not again on its first prompt
        self.assertEqual((asked[-1].title, asked[-1].reason), ("%s: Fix Login" % label, "prompt"))
        steady = dict(prompt, session_title="%s: Fix Login" % label)
        self.assertIsNone(self.hook(steady, spec=spec, now=t0 + 2))
        later = self.hook(steady, spec=spec, now=t0 + PL.RETRY_SECONDS + 1)
        self.assertEqual(later, {"hookEventName": "UserPromptSubmit", "additionalContext": line[0]})
        self.assertIsNone(self.hook(steady, spec=spec, now=t0 + PL.RETRY_SECONDS + 2))
        # A line that differs is news, and goes at once.
        line[0] = "Set it to something else."
        self.assertEqual(self.hook(steady, spec=spec, now=t0 + PL.RETRY_SECONDS + 3)["additionalContext"], line[0])
        # Nothing to say:  nothing is said, and the note keeps what was asked last.
        line[0] = ""
        self.assertIsNone(self.hook(steady, spec=spec, now=t0 + 3 * PL.RETRY_SECONDS))
        self.assertEqual(self.note("s-0001")["title_fix_for"], "Set it to something else.")

    def test_title_advice_for_a_chat_with_no_letter_is_held_back_the_same_way(self) -> None:
        asked: list[PL.TitleRequest] = []

        def advise(request: PL.TitleRequest) -> str:
            asked.append(request)
            return "Name this chat %s." % request.title

        spec = make_spec(title_advice=advise)
        down = Calls(self, fail=TimeoutError("down"))
        t0 = time.time()
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup",
                         "session_title": "Review PR"}, spec=spec, transport=down, now=t0)
        self.assertEqual(out["sessionTitle"], "??: Review PR")
        self.assertIn("No Operator letter yet", out["additionalContext"])
        self.assertTrue(out["additionalContext"].endswith("asks now.  Name this chat ??: Review PR."))
        self.assertEqual((asked[0].label, asked[0].title, asked[0].held), ("", "??: Review PR", ()))
        # The platform keeps its own title:  neither the title nor the same advice goes again on every prompt.
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go on",
                  "session_title": "Review PR"}
        for n in range(1, 4):
            self.assertIsNone(self.hook(prompt, spec=spec, transport=down, now=t0 + n))
        # Once the wait is over the chat asks for a letter again, and is told again, once.
        later = self.hook(prompt, spec=spec, transport=down, now=t0 + PL.RETRY_SECONDS + 1)
        self.assertEqual(later, {"hookEventName": "UserPromptSubmit",
                                 "additionalContext": "Name this chat ??: Review PR."})
        self.assertIsNone(self.hook(prompt, spec=spec, transport=down, now=t0 + PL.RETRY_SECONDS + 2))

    def test_an_adapter_whose_title_advice_fails_says_nothing_and_blocks_nothing(self) -> None:
        def broken(request: PL.TitleRequest) -> str:
            raise RuntimeError("the platform's store is gone")

        for n, advise in enumerate((broken, lambda request: None, lambda request: 7, lambda request: "  ")):
            sid = "s-%04d" % (n + 1)
            with self.subTest(sid=sid):
                out = self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"},
                                spec=make_spec(title_advice=advise))
                self.assertTrue(out["additionalContext"].endswith("renamed."), out["additionalContext"])
                self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")
                self.assertNotIn("title_fix_for", self.note(sid))

    def test_a_chat_another_seat_holds_is_asked_nothing(self) -> None:
        asked: list[PL.TitleRequest] = []
        spec = make_spec(title_advice=lambda request: asked.append(request) or "Set the title.")
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, spec=spec,
                        transport=lambda argv: {"outcome": "seat_mismatch"})
        self.assertNotIn("Set the title.", out["additionalContext"])
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go"},
                                    spec=spec))
        # A chat that learns it on a prompt (its hooks were installed after it started) is asked nothing either.
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "go"}, spec=spec,
                  transport=lambda argv: {"outcome": "seat_mismatch"})
        self.assertEqual(self.note("s-0002")["state"], "mismatch")
        self.assertEqual(asked, [])

    def test_a_refused_chat_gets_its_letter_once_one_is_free(self) -> None:
        with self.no_letters():
            self.start("s-0001")
            self.start("s-0002")
            self.assertEqual(self.note("s-0002")["state"], "failed")
            self.end("s-0001", "logout")  # the owner archives a chat:  its pair is free
            later = time.time() + PL.RETRY_SECONDS + 5
            out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "fix login",
                             "session_title": "??: New Chat"}, now=later)
        self.assertEqual(out["sessionTitle"], "AA: Fix Login")
        self.assertEqual((self.note("s-0002")["state"], self.label("s-0002")), ("open", "AA"))

    def test_a_turn_end_asks_again_and_a_writer_swaps_the_mark(self) -> None:
        written = []
        spec = make_spec(events={**PL.DEFAULT_EVENTS, "Stop": "turn"}, title_in_output=False,
                         title_writer=lambda request: written.append(request.title) or True)
        down = Calls(self, fail=TimeoutError("down"))
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "resume",
                   "session_title": "Fix Login"}, spec=spec, transport=down)
        self.assertEqual(written, ["??: Fix Login"])
        self.hook({"hook_event_name": "Stop", "session_id": "s-0001"}, spec=spec, transport=down)
        self.assertEqual(len(down.calls), 1)  # inside the wait:  not asked again
        later = time.time() + PL.RETRY_SECONDS + 5
        self.assertIsNone(self.hook({"hook_event_name": "Stop", "session_id": "s-0001"}, spec=spec, now=later))
        label = self.label("s-0001")
        self.assertEqual(written[-1], "%s: Fix Login" % label)
        self.assertTrue(self.note("s-0001")["titled"])
        self.assertEqual(self.lookup("s-0001")["label"], label)

    def test_a_turn_end_letter_reaches_a_hook_output_title_at_the_next_prompt(self) -> None:
        spec = make_spec(events={**PL.DEFAULT_EVENTS, "Stop": "turn"})
        down = Calls(self, fail=TimeoutError("down"))
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                  spec=spec, transport=down)
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix login",
                   "session_title": "??: New Chat"}, spec=spec, transport=down)
        later = time.time() + PL.RETRY_SECONDS + 5
        self.hook({"hook_event_name": "Stop", "session_id": "s-0001"}, spec=spec, now=later)
        label = self.label("s-0001")
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "next",
                         "session_title": "??: Fix Login"}, spec=spec, now=later + 1)
        self.assertEqual(out["sessionTitle"], "%s: Fix Login" % label)
        self.assertIn("Operator session letter", out["additionalContext"])

    def request(self, *argv: str, env: dict | None = None, now: float | None = None) -> tuple[int, str]:
        out = io.StringIO()
        code = PL.request_main(list(argv), env if env is not None else self.server_env(), out, now=now,
                               platform="linux")
        return code, out.getvalue()

    def test_request_asks_now_whatever_the_stamp_says_and_answers_in_one_line(self) -> None:
        with mock.patch.dict(PL.SPECS, {"codex": self.spec}):
            with self.no_letters():
                self.start("s-0001")
                self.start("s-0002")
                code, line = self.request("--session-id", "s-0002")
                self.assertEqual(code, 1)
                self.assertEqual(line.count("\n"), 1)
                self.assertTrue(line.startswith("no letter: all 26 codex main-chat pairs (AA to ZZ) are live"), line)
                self.end("s-0001", "logout")
                code, line = self.request("--session-id", "s-0002")  # inside the wait, and asked anyway
            self.assertEqual((code, line), (0, "letter AA: Codex chat s-0002\n"))
            note = self.note("s-0002")
            self.assertEqual((note["state"], note["label"], note["titled"]), ("open", "AA", False))
            out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "fix login",
                             "session_title": "??: New Chat"})
            self.assertEqual(out["sessionTitle"], "AA: Fix Login")  # the next prompt swaps the mark
            code, line = self.request("--session-id", "s-0002")  # a lettered chat:  the assigner confirms it
            self.assertEqual((code, line), (0, "letter AA: Codex chat s-0002\n"))

    def test_request_finds_the_chat_from_its_own_shell(self) -> None:
        spec = make_spec(session_id=PL.either(PL.from_payload("session_id"), PL.from_env("FAUX_SESSION_ID")))
        with mock.patch.dict(PL.SPECS, {"codex": spec}):
            self.start("s-0001")
            code, line = self.request(env=self.server_env(FAUX_SESSION_ID="s-0001"))
            self.assertEqual((code, line), (0, "letter %s: Codex chat s-0001\n" % self.label("s-0001")))
            code, line = self.request(env=self.server_env(FAUX_SESSION_ID="s-0404"))  # no note:  never registered
            self.assertEqual(code, 1)
            self.assertIn("pass --session-id", line)
        self.assertIsNone(self.lookup("s-0404"))
        code, line = self.request("--session-id", "s-0999")
        self.assertEqual(code, 1)
        self.assertIn("no chat with the session id s-0999", line)
        self.assertEqual(self.request("--bogus")[0], 2)


class GoneReportTests(Base):
    """Chats that are gone for good (a platform's app archived them) release their letters:  a chat's start reports
    them to the assigner in one `report-gone` call per host and platform, at most once a window, and nothing it does
    can fail the start.  The platform here is a made-up row of the table whose adapter names its gone chats, so nothing
    in the report is one seat's."""

    def setUp(self) -> None:
        super().setUp()
        self.gone: set[str] = set()
        self.asked: list[frozenset[str]] = []

        def gone_sessions(env, session_ids):
            self.asked.append(frozenset(session_ids))
            return self.gone

        self.spec = make_spec(platform="faux", source="faux-hook", display="Faux", gone_sessions=gone_sessions)
        patch = mock.patch.dict(PL.SPECS, {"faux": self.spec})
        patch.start()
        self.addCleanup(patch.stop)
        self.t0 = time.time() - 5 * 3600  # every time below is in the past, as a note's stamps are
        self.window = PL.GONE_REPORT_SECONDS

    def start(self, sid: str, at: float, *, spec=None, transport=None, source: str = "startup", **flags) -> dict:
        out, err = io.StringIO(), io.StringIO()
        flags.setdefault("platform", "darwin")
        code = PL.hook(spec or self.spec, self.env(), io.StringIO(json.dumps(
            {"hook_event_name": "SessionStart", "session_id": sid, "source": source})), out, now=at,
            transport=transport, stderr=err, **flags)
        self.assertEqual((code, err.getvalue()), (0, ""))  # nothing the report does is even an error line
        return json.loads(out.getvalue())["hookSpecificOutput"] if out.getvalue().strip() else {}

    def prompt(self, sid: str, at: float, text: str = "go on") -> dict:
        out = io.StringIO()
        PL.hook(self.spec, self.env(), io.StringIO(json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": text})), out, now=at,
            platform="darwin")
        return json.loads(out.getvalue())["hookSpecificOutput"] if out.getvalue().strip() else {}

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "CODEX", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def row_state(self, sid: str, location: str = "mac") -> str:
        return self.lookup(sid, location, platform="faux")["state"]

    def stamp(self) -> dict:
        with open(PL.gone_stamp_path(self.env(), "CODEX", "faux", "mac")) as fh:
            return json.load(fh)

    @contextmanager
    def reports(self):
        """The body of every report-gone request the client makes while this is open."""
        seen: list[dict] = []
        real = registry_api._open

        def capture(request, timeout):
            if request.full_url.endswith("/report-gone"):
                seen.append(json.loads(request.data.decode("utf-8")))
            return real(request, timeout)

        with mock.patch.object(registry_api, "_open", capture):
            yield seen

    def test_a_start_reports_this_hosts_gone_chats_in_one_call(self) -> None:
        with self.reports() as sent:
            for sid in ("s-0001", "s-0002", "s-0003"):
                self.start(sid, self.t0)
            self.assertEqual((sent, self.asked), ([], []))  # no chat here has been quiet:  nobody is asked
            self.gone = {"s-0001", "s-0002"}
            out = self.start("s-0004", self.t0 + self.window + 1)
        # One call, in the assigner's own request shape:  the platform, this host, the scope and the ids.  No title.
        self.assertEqual(sent, [{"platform": "faux", "verified_host": "mac", "profile_scope": "default",
                                 "native_session_ids": ["s-0001", "s-0002"]}])
        self.assertEqual(self.asked, [frozenset({"s-0001", "s-0002", "s-0003"})])
        self.assertEqual([self.row_state(sid) for sid in ("s-0001", "s-0002", "s-0003", "s-0004")],
                         ["closed", "closed", "active", "active"])
        # The chat that started is answered as ever.
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % self.note("s-0004")["label"])
        # A chat the report named is marked, so it is not named again, and its next touch is due.
        for sid in ("s-0001", "s-0002"):
            note = self.note(sid)
            self.assertEqual((note["state"], note["gone_at"], note["touched_at"]),
                             ("open", self.t0 + self.window + 1, 0))
        self.assertNotIn("gone_at", self.note("s-0003"))
        self.assertEqual(self.stamp(), {"at": self.t0 + self.window + 1})
        with self.reports() as sent:
            self.start("s-0005", self.t0 + 2 * self.window + 5)
        self.assertEqual(sent, [])  # the adapter still calls them gone, and they are not reported every window
        self.assertEqual(self.asked[-1], frozenset({"s-0003", "s-0004"}))

    def test_a_second_start_inside_the_window_sends_nothing(self) -> None:
        self.start("s-0001", self.t0)
        self.start("s-0002", self.t0)
        self.gone = {"s-0001", "s-0002"}
        with self.reports() as sent:
            self.start("s-0003", self.t0 + self.window - 5)  # the stamp of the first start still holds
            self.assertEqual((sent, self.asked), ([], []))
            self.start("s-0004", self.t0 + self.window + 1)  # due:  s-0001 and s-0002 have been quiet an hour
            self.assertEqual(len(sent), 1)
            self.gone = {"s-0003"}
            self.start("s-0005", self.t0 + 2 * self.window)  # inside the window of that report
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(self.asked), 1)  # the adapter is not even asked
            self.start("s-0006", self.t0 + 2 * self.window + 2)
            self.assertEqual([body["native_session_ids"] for body in sent], [["s-0001", "s-0002"], ["s-0003"]])

    def test_nothing_gone_makes_no_call_and_still_stamps_the_window(self) -> None:
        self.start("s-0001", self.t0)
        with self.reports() as sent:
            self.start("s-0002", self.t0 + self.window + 1)
            self.start("s-0003", self.t0 + self.window + 2)
        self.assertEqual(sent, [])
        self.assertEqual(self.asked, [frozenset({"s-0001"})])
        self.assertEqual(self.stamp(), {"at": self.t0 + self.window + 1})

    def test_only_this_hosts_own_quiet_lettered_chats_of_the_platform_are_candidates(self) -> None:
        self.start("s-0001", self.t0)
        self.start("c-0001", self.t0, spec=make_spec())  # another platform's chat, in the same seat's notes
        self.start("s-0002", self.t0, platform="linux", location_flag="server")  # registered at another location
        self.start("s-0003", self.t0, transport=Calls(self, fail=TimeoutError("down")))  # holds no letter
        ending = make_spec(platform="faux", source="faux-hook", display="Faux")  # every end closes
        self.start("s-0004", self.t0, spec=ending)
        PL.hook(ending, self.env(), io.StringIO(json.dumps(
            {"hook_event_name": "SessionEnd", "session_id": "s-0004", "reason": "logout"})), io.StringIO(),
            now=self.t0 + 1, platform="darwin")
        self.assertEqual(self.note("s-0004")["state"], "closed")
        self.start("s-0005", self.t0)
        self.prompt("s-0005", self.t0 + self.window - 600)  # its hook ran ten minutes before the hour was up
        # The adapter names everything it is asked about, and more:  only what was asked counts.
        self.gone = {"s-0001", "s-0002", "s-0003", "s-0004", "s-0005", "c-0001", "s-9999"}
        with self.reports() as sent:
            self.start("s-0009", self.t0 + self.window + 1)
        self.assertEqual(self.asked[-1], frozenset({"s-0001"}))
        self.assertEqual([body["native_session_ids"] for body in sent], [["s-0001"]])
        self.assertEqual(self.row_state("s-0005"), "active")
        self.assertEqual(self.row_state("s-0002", "server"), "active")
        self.assertEqual(self.lookup("c-0001", "mac")["state"], "active")

    def test_a_chat_archived_days_after_its_last_hook_event_is_still_reported(self) -> None:
        # A chat is usually archived well after its last prompt, and the host may see no start for days after that.
        self.start("s-0001", self.t0 - 30 * 86400)
        self.start("s-0002", self.t0 - 3 * 86400)
        self.start("s-0003", self.t0)
        self.gone = {"s-0001", "s-0002"}
        with self.reports() as sent:
            self.start("s-0004", self.t0 + self.window + 1)
        self.assertEqual(self.asked[-1], frozenset({"s-0001", "s-0002", "s-0003"}))
        self.assertEqual([body["native_session_ids"] for body in sent], [["s-0001", "s-0002"]])  # longest quiet first
        self.assertEqual([self.row_state(sid) for sid in ("s-0001", "s-0002", "s-0003")],
                         ["closed", "closed", "active"])

    def test_the_scan_is_bounded_by_count_and_never_by_how_long_a_chat_has_been_quiet(self) -> None:
        for n in range(1, 6):
            self.start("s-%04d" % n, self.t0 - n * 86400)
        self.start("s-0006", self.t0)
        now = self.t0 + self.window + 1
        every = PL.quiet_chats(self.env(), "CODEX", "faux", "mac", now)
        self.assertEqual(sorted(every), ["s-%04d" % n for n in range(1, 7)])
        with mock.patch.object(PL, "GONE_REPORT_IDS", 2):  # the most recently quiet chats are the ones kept
            self.assertEqual(sorted(PL.quiet_chats(self.env(), "CODEX", "faux", "mac", now)), ["s-0001", "s-0006"])
        with mock.patch.object(PL, "GONE_SCAN_NOTES", 1):  # and only the newest note files are read at all
            self.assertEqual(len(PL.quiet_chats(self.env(), "CODEX", "faux", "mac", now)), 1)
        # A chat that was reported is not a candidate again, so a long-kept note costs nothing after its report.
        self.gone = {"s-0005"}
        self.start("s-0007", now)
        self.assertNotIn("s-0005", PL.quiet_chats(self.env(), "CODEX", "faux", "mac", now + 10))

    def test_a_refused_report_changes_no_note_and_the_next_one_names_what_the_assigner_allows(self) -> None:
        for sid in ("s-0001", "s-0002", "s-0003", "s-0004"):
            self.start(sid, self.t0)
        self.gone = {"s-0001", "s-0002", "s-0003"}
        before = {sid: self.note(sid) for sid in self.gone}
        with self.reports() as sent:
            out = self.start("s-0005", self.t0 + self.window + 1)  # 3 of the host's 5 live rows:  over the share
        self.assertEqual(sent[0]["native_session_ids"], ["s-0001", "s-0002", "s-0003"])
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % self.note("s-0005")["label"])
        for sid in sorted(self.gone):
            self.assertEqual(self.row_state(sid), "active", sid)
            self.assertEqual(self.note(sid), before[sid], sid)
        self.assertEqual(self.stamp(), {"at": self.t0 + self.window + 1, "allowed": 2})
        with self.reports() as sent:
            self.start("s-0006", self.t0 + self.window + 60)  # refused or not, the window holds
            self.assertEqual(sent, [])
            self.start("s-0007", self.t0 + 2 * self.window + 2)
        self.assertEqual([body["native_session_ids"] for body in sent], [["s-0001", "s-0002"]])
        self.assertEqual([self.row_state(sid) for sid in ("s-0001", "s-0002", "s-0003")],
                         ["closed", "closed", "active"])
        self.assertEqual(self.stamp(), {"at": self.t0 + 2 * self.window + 2})
        with self.reports() as sent:
            self.start("s-0008", self.t0 + 3 * self.window + 3)
        self.assertEqual([body["native_session_ids"] for body in sent], [["s-0003"]])
        self.assertEqual(self.row_state("s-0003"), "closed")

    def test_a_start_still_succeeds_when_the_report_fails(self) -> None:
        self.start("s-0001", self.t0)
        self.gone = {"s-0001"}
        calls = Calls(self)

        def broken(argv):
            if argv[0] == "report-gone":
                raise RuntimeError("the report blew up")
            return calls(argv)

        out = self.start("s-0002", self.t0 + self.window + 1, transport=broken)
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % self.note("s-0002")["label"])
        self.assertIn('"@**Faux** %s"' % self.note("s-0002")["label"], out["additionalContext"])
        self.assertEqual(self.row_state("s-0001"), "active")
        self.assertNotIn("gone_at", self.note("s-0001"))
        # It is tried again after the window, never on every start.
        with self.reports() as sent:
            self.start("s-0003", self.t0 + self.window + 30)
            self.assertEqual(sent, [])
            self.start("s-0004", self.t0 + 2 * self.window + 2)
        self.assertEqual([body["native_session_ids"] for body in sent], [["s-0001"]])

    def test_nothing_an_adapter_or_the_assigner_does_fails_a_start(self) -> None:
        def raises(env, session_ids):
            raise OSError("the app's store went away")

        answers = (raises, lambda env, ids: None, lambda env, ids: 7, lambda env, ids: [7, None, {"a": 1}])
        for n, accessor in enumerate(answers):
            with self.subTest(n=n):
                spec = make_spec(platform="faux", source="faux-hook", display="Faux", gone_sessions=accessor)
                self.start("a-%04d" % n, self.t0 + n * (self.window + 10), spec=spec)
                with self.reports() as sent:
                    out = self.start("b-%04d" % n, self.t0 + n * (self.window + 10) + self.window + 5, spec=spec)
                self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")
                self.assertEqual(sent, [])
        # The assigner is unreachable:  the chat is a "??" chat, and the report is one more failed call.
        self.gone = {"a-0000"}
        down = Calls(self, fail=TimeoutError("down"))
        out = self.start("s-0009", self.t0 + 4 * self.window, transport=down)
        self.assertEqual(out["sessionTitle"], "??: New Chat")
        self.assertEqual([call[0] for call in down.calls], ["register", "report-gone"])
        # A state folder that cannot hold the stamp sends nothing at all:  with no stamp there is no throttle.
        with mock.patch.object(PL, "_save_json", return_value=False), self.reports() as sent:
            self.start("s-0010", self.t0 + 4 * self.window + 2 * self.window)
        self.assertEqual(sent, [])

    def test_a_compact_start_and_a_platform_with_no_accessor_report_nothing(self) -> None:
        self.start("s-0001", self.t0)
        self.gone = {"s-0001"}
        with self.reports() as sent:
            self.start("s-0001", self.t0 + self.window + 1, source="compact")
            self.start("c-0001", self.t0 + self.window + 2, spec=make_spec())
        self.assertEqual((sent, self.asked), ([], []))
        self.assertFalse(os.path.exists(PL.gone_stamp_path(self.env(), "CODEX", "codex", "mac")))
        self.assertIsNone(make_spec().gone_sessions)  # no platform reports gone chats unless its adapter says how

    def test_a_chat_reported_gone_that_prompts_again_registers_again(self) -> None:
        self.start("s-0001", self.t0)
        self.start("s-0002", self.t0)
        self.gone = {"s-0001"}
        self.start("s-0003", self.t0 + self.window + 1)
        self.assertEqual(self.row_state("s-0001"), "closed")
        out = self.prompt("s-0001", self.t0 + self.window + 20, "fix login")  # alive after all
        note = self.note("s-0001")
        self.assertEqual((self.row_state("s-0001"), note["state"]), ("active", "open"))
        self.assertNotIn("gone_at", note)
        self.assertEqual(out["sessionTitle"], "%s: Fix Login" % note["label"])
        self.assertIn('"@**Faux** %s"' % note["label"], out["additionalContext"])  # told how it is addressed now

    # ---- `platform-letter gone`:  what would be reported, and `--apply` to report it now ----

    def verb(self, *argv: str, at: float | None = None, platform: str = "darwin") -> tuple[int, str]:
        out = io.StringIO()
        code = PL.gone_main(list(argv), self.env(), out, now=at, platform=platform)
        return code, out.getvalue()

    def test_the_dry_run_names_each_chat_by_letter_and_eight_characters_and_sends_nothing(self) -> None:
        long_id = "0123456789abcdef-long-session-id"
        self.start(long_id, self.t0)
        self.start("s-0002", self.t0)
        self.prompt(long_id, self.t0 + 1, "a secret title for this chat")
        label = self.note(long_id)["label"]
        self.gone = {long_id}
        stamp = self.stamp()
        with self.reports() as sent:
            code, out = self.verb("--platform", "faux", at=self.t0 + self.window + 5)
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [
            "would report Faux %s (01234567, mac)" % label,
            "Faux gone report, mac:  1 gone  (dry run:  nothing sent;  --apply reports them now)",
        ])
        self.assertNotIn("Secret", out)
        self.assertEqual(sent, [])
        self.assertEqual(self.stamp(), stamp)  # a look is not a report:  the hooks' window is as it was
        self.assertEqual(self.row_state(long_id), "active")

    def test_apply_reports_now_whatever_the_stamp_says(self) -> None:
        self.start("s-0001", self.t0)
        self.start("s-0002", self.t0)
        self.start("s-0003", self.t0 + self.window + 1)  # the hooks' report for this window is spent
        self.gone = {"s-0001"}
        label = self.note("s-0001")["label"]
        with self.reports() as sent:
            code, out = self.verb("--platform", "faux", "--apply", at=self.t0 + self.window + 30)
            self.assertEqual(code, 0, out)
            self.assertEqual(out.splitlines(), ["reported Faux %s (s-0001, mac)" % label,
                                                "Faux gone report, mac:  1 gone, 1 closed now"])
            self.assertEqual(self.row_state("s-0001"), "closed")
            code, out = self.verb("--platform", "faux", "--apply", at=self.t0 + self.window + 40)
            self.assertEqual((code, out), (0, "Faux gone report, mac:  0 gone, 0 closed now\n"))  # idempotent
        self.assertEqual(len(sent), 1)

    def test_a_chat_the_assigner_closed_already_is_not_named_twice(self) -> None:
        self.start("s-0001", self.t0)
        self.start("s-0002", self.t0)
        code, answer = self.registry("close", "--platform", "faux", "--location", "mac", "--session-id", "s-0001")
        self.assertEqual(code, 0, answer)
        self.gone = {"s-0001"}
        code, out = self.verb("--platform", "faux", "--apply", at=self.t0 + self.window + 5)
        self.assertEqual(code, 0, out)
        self.assertIn("not open Faux %s (s-0001, mac)" % self.note("s-0001")["label"], out)
        self.assertIn("1 gone, 0 closed now", out)
        self.assertEqual(self.verb("--platform", "faux", at=self.t0 + self.window + 9)[1].splitlines()[0],
                         "Faux gone report, mac:  0 gone  (dry run:  nothing sent;  --apply reports them now)")

    def test_a_refused_or_failed_apply_says_so(self) -> None:
        for sid in ("s-0001", "s-0002", "s-0003"):
            self.start(sid, self.t0)
        self.gone = {"s-0001", "s-0002", "s-0003"}
        code, out = self.verb("--platform", "faux", "--apply", at=self.t0 + self.window + 5)
        self.assertEqual(code, 1, out)
        self.assertIn("Faux gone report, mac:  3 gone, not reported:  the report would close 3 of 3 live rows", out)
        self.assertEqual(self.row_state("s-0001"), "active")
        broken = make_spec(platform="faux", source="faux-hook", display="Faux",
                           gone_sessions=mock.Mock(side_effect=OSError("gone")))
        with mock.patch.dict(PL.SPECS, {"faux": broken}):
            for argv in (["--platform", "faux"], ["--platform", "faux", "--apply"]):
                code, out = self.verb(*argv, at=self.t0 + 2 * self.window + 9)
                self.assertEqual((code, out), (1, "Faux gone report, mac:  could not read this host's chats (OSError)\n"))

    def test_the_verb_checks_its_arguments(self) -> None:
        code, out = self.verb("--platform", "codex")
        self.assertEqual((code, out), (2, "gone report:  the codex adapter names no gone chats\n"))
        code, out = self.verb("--platform", "nosuch")
        self.assertEqual((code, out), (2, "gone report:  no adapter for the platform 'nosuch'\n"))
        for bad in (["--bogus"], ["--platform"], ["--apply", "extra"], ["--platform", "../x"]):
            self.assertEqual(self.verb(*bad)[0], 2, bad)
        # A host that is not the Mac must say where it is, as its hooks do.
        code, out = self.verb("--platform", "faux", platform="linux")
        self.assertEqual(code, 2)
        self.assertIn("fleet location is not stated", out)
        code, out = self.verb("--platform", "faux", "--location", "server", platform="linux")
        self.assertEqual((code, out.splitlines()[-1].split("  (")[0]), (0, "Faux gone report, server:  0 gone"))

    def test_without_a_platform_every_platform_that_names_its_gone_chats_is_shown(self) -> None:
        self.start("s-0001", self.t0)
        self.start("s-0002", self.t0)
        self.gone = {"s-0001"}
        label = self.note("s-0001")["label"]
        out = io.StringIO()
        # Through the entry point, under both names:  `sweep-archived` is what the verb was called before.
        for name in ("gone", "sweep-archived"):
            out = io.StringIO()
            self.assertEqual(PL.main([name, "--location", "mac"], env=self.env(), stdout=out, stderr=out), 0)
            self.assertIn("would report Faux %s (s-0001, mac)" % label, out.getvalue())
        shown = {line.split(" gone report, ")[0] for line in out.getvalue().splitlines() if " gone report, " in line}
        self.assertIn("Faux", shown)
        self.assertNotIn("Codex", shown)
        for name in PL.adapter_platforms():
            spec = PL.load_spec(name)
            if spec is not None:
                names = spec.gone_sessions is not None or spec.gone_elsewhere is not None
                self.assertEqual(spec.display in shown, names, name)
        self.assertIn("gone [--platform P] [--location L] [--apply]", PL.USAGE)

    # ---- the registry side's `report-gone` op ----

    def test_the_registry_side_reports_gone_chats_in_one_api_call(self) -> None:
        for sid in ("s-0001", "s-0002", "s-0003", "s-0004"):
            self.start(sid, self.t0)
        argv = ["report-gone", "--platform", "faux", "--location", "mac"]
        with self.reports() as sent:
            code, answer = self.registry(*argv, "--gone=" + json.dumps(["s-0001", "s-0001", "s-7777"]))
        self.assertEqual((code, answer), (0, {"ok": True, "gone": ["s-0001"], "count": 1,
                                              "labels": {"s-0001": self.note("s-0001")["label"]}}))
        self.assertEqual(sent, [{"platform": "faux", "verified_host": "mac", "profile_scope": "default",
                                 "native_session_ids": ["s-0001", "s-7777"]}])
        self.assertEqual(self.row_state("s-0001"), "closed")
        with self.reports() as sent:
            # An empty list makes no call, and a list that is not session ids is refused whole, before any call.
            self.assertEqual(self.registry(*argv), (0, {"ok": True, "gone": [], "count": 0}))
            for bad in ("{}", "not json", '["s-0002", 7]', '["s-0002", "../x"]', '"s-0002"',
                        json.dumps(["s-%04d" % n for n in range(PL.GONE_REPORT_IDS + 1)])):
                code, answer = self.registry(*argv, "--gone=" + bad)
                self.assertEqual(code, 5, bad[:40])
                self.assertIn("error", answer)
        self.assertEqual(sent, [])
        self.assertEqual(self.row_state("s-0002"), "active")
        # The assigner's own refusal comes back as it is, with what it would take now.
        code, answer = self.registry(*argv, "--gone=" + json.dumps(["s-0002", "s-0003", "s-0004"]))
        self.assertEqual((code, answer["code"]), (5, "gone_share_exceeded"))
        self.assertIsInstance(answer["allowed_now"], int)
        # Every other op is about one chat, and still needs its session id.
        for op in ("register", "title", "touch", "close", "show", "issues"):
            code, answer = self.registry(op, "--platform", "faux", "--location", "mac", "--seat", "CODEX")
            self.assertEqual((code, answer), (2, {"error": "bad arguments"}), op)


class GoneElsewhereTests(Base):
    """A platform's app on one host can run, and archive, chats on another, whose notes this host does not hold.
    The adapter names those from its own records (`gone_elsewhere`), and they go in a report of their own for the
    location they run at, with its own stamp, through the same `report-gone` call."""

    def setUp(self) -> None:
        super().setUp()
        self.gone: set[str] = set()
        self.elsewhere: dict[str, list] = {}
        self.asked: list[str] = []

        def gone_elsewhere(env, there):
            self.asked.append(there)
            return self.elsewhere.get(there)

        self.spec = make_spec(platform="faux", source="faux-hook", display="Faux",
                              gone_sessions=lambda env, ids: self.gone, gone_elsewhere=gone_elsewhere)
        patch = mock.patch.dict(PL.SPECS, {"faux": self.spec})
        patch.start()
        self.addCleanup(patch.stop)
        self.t0 = time.time() - 5 * 3600
        self.window = PL.GONE_REPORT_SECONDS

    def start(self, sid: str, at: float, *, transport=None, **flags) -> dict:
        out, err = io.StringIO(), io.StringIO()
        flags.setdefault("platform", "darwin")
        code = PL.hook(self.spec, self.env(), io.StringIO(json.dumps(
            {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"})), out, now=at,
            transport=transport, stderr=err, **flags)
        self.assertEqual((code, err.getvalue()), (0, ""))
        return json.loads(out.getvalue())["hookSpecificOutput"] if out.getvalue().strip() else {}

    def server_chat(self, sid: str) -> str:
        """A chat registered at the server, by its own hook there:  this host holds no note of it."""
        code, answer = self.registry("register", "--platform", "faux", "--location", "server", "--session-id", sid,
                                     "--seat", "CODEX", "--source", "faux-hook")
        self.assertEqual(code, 0, answer)
        return answer["label"]

    def row_state(self, sid: str, location: str) -> str:
        return self.lookup(sid, location, platform="faux")["state"]

    def stamp(self, there: str = "server") -> dict:
        with open(PL.gone_stamp_path(self.env(), "CODEX", "faux", there)) as fh:
            return json.load(fh)

    @contextmanager
    def reports(self):
        seen: list[dict] = []
        real = registry_api._open

        def capture(request, timeout):
            if request.full_url.endswith("/report-gone"):
                seen.append(json.loads(request.data.decode("utf-8")))
            return real(request, timeout)

        with mock.patch.object(registry_api, "_open", capture):
            yield seen

    def test_a_mac_start_sends_one_report_for_its_own_chats_and_one_for_the_other_location(self) -> None:
        for sid in ("m-0001", "m-0002"):
            self.start(sid, self.t0)
        for sid in ("r-0001", "r-0002", "r-0003"):
            self.server_chat(sid)
        self.gone = {"m-0001"}
        self.elsewhere = {"server": ["r-0001", "r-9999"]}  # the second is not registered anywhere
        with self.reports() as sent:
            out = self.start("m-0003", self.t0 + self.window + 1)
        self.assertEqual(sent, [
            {"platform": "faux", "verified_host": "mac", "profile_scope": "default",
             "native_session_ids": ["m-0001"]},
            {"platform": "faux", "verified_host": "server", "profile_scope": "default",
             "native_session_ids": ["r-0001", "r-9999"]},
        ])
        self.assertEqual(set(self.asked), {"server"})  # only the location that is not this host's
        self.assertEqual((self.row_state("m-0001", "mac"), self.row_state("m-0002", "mac")), ("closed", "active"))
        self.assertEqual([self.row_state(sid, "server") for sid in ("r-0001", "r-0002", "r-0003")],
                         ["closed", "active", "active"])
        self.assertIsNone(self.lookup("r-9999", "server", platform="faux"))  # an id nobody holds open costs nothing
        self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")
        # Each location has its own stamp, and the other location's keeps what was reported.
        self.assertEqual(self.stamp("mac"), {"at": self.t0 + self.window + 1})
        self.assertEqual(self.stamp("server"), {"at": self.t0 + self.window + 1, "sent": ["r-0001", "r-9999"]})

    def test_a_chat_is_named_once_while_it_stays_gone_and_again_if_it_goes_again(self) -> None:
        for n in range(1, 9):  # enough live rows that the assigner's share guard is not what is tested here
            self.server_chat("r-%04d" % n)
        self.elsewhere = {"server": ["r-0001"]}
        with self.reports() as sent:
            self.start("m-0001", self.t0)
            self.start("m-0002", self.t0 + self.window + 1)
            self.assertEqual(len(sent), 1)  # still named by the adapter:  not reported every window
            self.elsewhere = {"server": ["r-0002", "r-0001"]}
            self.start("m-0003", self.t0 + 2 * self.window + 2)
            self.assertEqual(sent[-1]["native_session_ids"], ["r-0002"])
            self.assertEqual(self.stamp()["sent"], ["r-0001", "r-0002"])
            # r-0001 is no longer named (it was unarchived and came back):  it is forgotten, and named again later.
            self.server_chat("r-0001")
            self.elsewhere = {"server": ["r-0002"]}
            self.start("m-0004", self.t0 + 3 * self.window + 3)
            self.assertEqual((len(sent), self.stamp()["sent"]), (2, ["r-0002"]))
            self.elsewhere = {"server": ["r-0001", "r-0002"]}
            self.start("m-0005", self.t0 + 4 * self.window + 4)
        self.assertEqual([body["native_session_ids"] for body in sent], [["r-0001"], ["r-0002"], ["r-0001"]])
        self.assertEqual(self.row_state("r-0001", "server"), "closed")

    def test_the_other_locations_report_has_its_own_window(self) -> None:
        self.server_chat("r-0001")
        self.server_chat("r-0002")
        with self.reports() as sent:
            self.start("m-0001", self.t0)
            self.elsewhere = {"server": ["r-0001"]}
            self.start("m-0002", self.t0 + self.window - 5)  # inside the window:  the adapter is not even asked
            self.assertEqual((sent, self.asked), ([], ["server"]))
            self.start("m-0003", self.t0 + self.window + 1)
            self.assertEqual(len(sent), 1)
            self.elsewhere = {"server": ["r-0001", "r-0002"]}
            self.start("m-0004", self.t0 + self.window + 30)
            self.assertEqual(len(sent), 1)
        # A host that is the other location asks about this one in turn.
        self.asked.clear()
        self.start("s-0001", self.t0, platform="linux", location_flag="server")
        self.assertEqual(self.asked, ["mac"])

    def test_a_host_is_never_asked_about_its_own_location(self) -> None:
        # Its own chats are `gone_sessions`' to name, from its notes.  With no such accessor there is no stamp of
        # this host's own to hold the question back, so this is the rule itself.
        self.spec = make_spec(platform="faux", source="faux-hook", display="Faux",
                              gone_elsewhere=lambda env, there: self.asked.append(there))
        self.start("m-0001", self.t0)
        self.start("s-0001", self.t0, platform="linux", location_flag="server")
        self.assertEqual(self.asked, ["server", "mac"])

    def test_a_refused_report_keeps_nothing_as_sent_and_the_next_names_what_the_assigner_allows(self) -> None:
        for sid in ("r-0001", "r-0002", "r-0003", "r-0004"):
            self.server_chat(sid)
        self.elsewhere = {"server": ["r-0001", "r-0002", "r-0003"]}
        with self.reports() as sent:
            self.start("m-0001", self.t0)  # 3 of the server's 4 live rows:  over the share
            self.assertEqual(sent[0]["native_session_ids"], ["r-0001", "r-0002", "r-0003"])
            self.assertEqual([self.row_state(sid, "server") for sid in ("r-0001", "r-0002", "r-0003")],
                             ["active"] * 3)
            self.assertEqual(self.stamp(), {"at": self.t0, "sent": [], "allowed": 2})
            self.start("m-0002", self.t0 + 60)  # refused or not, the window holds
            self.assertEqual(len(sent), 1)
            self.start("m-0003", self.t0 + self.window + 1)
        self.assertEqual(sent[-1]["native_session_ids"], ["r-0001", "r-0002"])
        self.assertEqual([self.row_state(sid, "server") for sid in ("r-0001", "r-0002", "r-0003")],
                         ["closed", "closed", "active"])
        self.assertEqual(self.stamp(), {"at": self.t0 + self.window + 1, "sent": ["r-0001", "r-0002"]})

    def test_nothing_the_adapter_or_the_assigner_does_fails_a_start(self) -> None:
        self.server_chat("r-0001")
        self.server_chat("r-0002")
        for n, answer in enumerate((None, 7, [7, None, "../x", {"a": 1}], OSError("the store went away"))):
            with self.subTest(n=n):
                self.elsewhere = {"server": answer}
                if isinstance(answer, Exception):
                    self.spec = make_spec(platform="faux", source="faux-hook", display="Faux",
                                          gone_elsewhere=mock.Mock(side_effect=answer))
                with self.reports() as sent:
                    out = self.start("a-%04d" % n, self.t0 + n * (self.window + 5))
                self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")
                self.assertEqual(sent, [])
        # A report that fails changes nothing, records nothing as sent, and is tried again after the window.
        self.spec = PL.SPECS["faux"]
        self.elsewhere = {"server": ["r-0001"]}
        calls = Calls(self)

        def broken(argv):
            if argv[0] == "report-gone":
                raise RuntimeError("the report blew up")
            return calls(argv)

        at = self.t0 + 5 * self.window
        out = self.start("m-0001", at, transport=broken)
        self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")
        self.assertEqual((self.row_state("r-0001", "server"), self.stamp()), ("active", {"at": at, "sent": []}))
        with self.reports() as sent:
            self.start("m-0002", at + 30)
            self.assertEqual(sent, [])
            self.start("m-0003", at + self.window + 1)
        self.assertEqual([body["native_session_ids"] for body in sent], [["r-0001"]])

    def test_the_verb_shows_and_reports_the_other_locations_chats(self) -> None:
        label = self.server_chat("0123456789abcdef-remote")
        self.server_chat("r-0002")
        self.elsewhere = {"server": ["0123456789abcdef-remote", "r-7777"]}
        out = io.StringIO()
        with self.reports() as sent:
            code = PL.gone_main(["--platform", "faux"], self.env(), out, now=self.t0, platform="darwin")
            self.assertEqual(code, 0, out.getvalue())
            # This host does not know the letter of a chat that runs elsewhere:  a dash stands for it.
            self.assertEqual(out.getvalue().splitlines(), [
                "Faux gone report, mac:  0 gone  (dry run:  nothing sent;  --apply reports them now)",
                "would report Faux - (01234567, server)",
                "would report Faux - (r-7777, server)",
                "Faux gone report, server:  2 gone  (dry run:  nothing sent;  --apply reports them now)",
            ])
            self.assertEqual(sent, [])
            self.assertFalse(os.path.exists(PL.gone_stamp_path(self.env(), "CODEX", "faux", "server")))
            out = io.StringIO()
            code = PL.gone_main(["--platform", "faux", "--apply"], self.env(), out, now=self.t0, platform="darwin")
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().splitlines(), [
            "Faux gone report, mac:  0 gone, 0 closed now",
            "reported Faux %s (01234567, server)" % label,  # the assigner's answer names the letter it freed
            "not open Faux - (r-7777, server)",
            "Faux gone report, server:  2 gone, 1 closed now",
        ])
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.row_state("0123456789abcdef-remote", "server"), "closed")
        # A platform that names only chats elsewhere is a platform the verb takes.
        only = make_spec(platform="faux", source="faux-hook", display="Faux", gone_elsewhere=lambda env, there: [])
        with mock.patch.dict(PL.SPECS, {"faux": only}):
            out = io.StringIO()
            self.assertEqual(PL.gone_main(["--platform", "faux"], self.env(), out, platform="darwin"), 0)
            self.assertEqual(out.getvalue().splitlines(), [
                "Faux gone report, server:  0 gone  (dry run:  nothing sent;  --apply reports them now)"])


class RetiredNoteTests(Base):
    """A note the seat's retired client wrote (no `platform`, earlier letters under `held_labels`) is put in this
    client's shape the first time it is read, where it lies."""

    def old_note(self, sid: str, **over) -> dict:
        """The shape the retired Claude client wrote (its `Chat.write` and `_follow_letter` on origin/main)."""
        note = {
            "ended": {"at": 1.0, "reason": "other"}, "generation": 2, "held_labels": ["QP", "QQ", "QR"],
            "inherited": "", "label": "QR", "location": "mac", "placeholder": "QR: New Chat", "prior_label": "QQ",
            "record_id": "rec-1", "session_id": sid, "state": "open", "title": "Fix Login", "titled": True,
            "touched_at": 5.0, "updated_at": 7.0,
        }
        note.update(over)
        return note

    def put(self, sid: str, note: dict, seat: str = "CODEX") -> str:
        folder = os.path.join(self.state, seat, "letters")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(note, fh)
        return path

    def on_disk(self, path: str) -> dict:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_first_read_upgrades_it_in_place(self) -> None:
        path = self.put("s-0001", self.old_note("s-0001"))
        chat = PL.Chat(self.spec, self.env(), "mac", "s-0001", now=99.0)
        note = chat.read()
        self.assertEqual((note["platform"], note["labels"]), ("codex", ["QR", "QQ", "QP"]))  # its own first, newest next
        self.assertNotIn("held_labels", note)
        self.assertNotIn("prior_label", note)
        self.assertEqual(self.on_disk(path), note)
        self.assertEqual(note["updated_at"], 7.0)  # a read is not a hook event:  the old client's stamp stays
        self.assertEqual({k: note[k] for k in ("label", "state", "title", "record_id", "generation")},
                         {"label": "QR", "state": "open", "title": "Fix Login", "record_id": "rec-1", "generation": 2})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # A second read changes nothing, and a note of this client is never touched.
        before = os.stat(path).st_mtime_ns
        self.assertEqual(chat.read(), note)
        self.assertEqual(os.stat(path).st_mtime_ns, before)
        self.assertIsNone(PL.upgraded_note(note, "codex"))
        self.assertIsNone(PL.upgraded_note({}, "codex"))

    def test_odd_old_notes_upgrade_without_raising(self) -> None:
        for over, want in (
            ({"held_labels": None, "prior_label": None}, ["QR"]),
            ({"held_labels": "QP", "prior_label": 7}, ["QR"]),
            ({"held_labels": ["qq", "TOOLONG", 5, "QP"]}, ["QR", "QQ", "QP"]),
            ({"label": "", "state": "failed", "held_labels": [], "prior_label": None}, []),
        ):
            note = PL.upgraded_note(self.old_note("s-0001", **over), "codex")
            self.assertEqual(note["labels"], want, over)

    def test_bare_knows_the_letters_the_old_client_remembered(self) -> None:
        # The chat still shows a title under a letter it held two letters ago:  the hook takes that letter off, where
        # a note that had lost its history would stack the new letter in front of it.
        self.put("s-0001", self.old_note("s-0001", updated_at=time.time()))
        out = io.StringIO()
        PL.hook(self.spec, self.env(), io.StringIO(json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "go",
             "session_title": "QP: Fix Login"})), out, platform="darwin",
            transport=lambda argv: {"ok": True, "record_id": "rec-1"})
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["sessionTitle"], "QR: Fix Login")

    def test_an_old_note_becomes_a_gone_report_candidate(self) -> None:
        gone: set[str] = set()
        spec = make_spec(platform="faux", source="faux-hook", display="Faux", gone_sessions=lambda env, ids: gone)
        now = time.time()
        path = self.put("s-0001", self.old_note("s-0001", updated_at=now - 3 * 86400))
        self.assertEqual(PL.quiet_chats(self.env(), "CODEX", "faux", "mac", now), {})  # not asked to adopt it
        self.assertNotIn("platform", self.on_disk(path))
        self.assertEqual([row["session_id"] for row in PL.gone_chats(spec, self.env(), "CODEX", "mac", now)], [])
        self.assertEqual(self.on_disk(path)["platform"], "faux")  # read by the seat's own platform:  adopted
        gone.add("s-0001")
        self.assertEqual([(row["session_id"], row["label"]) for row in
                          PL.gone_chats(spec, self.env(), "CODEX", "mac", now)], [("s-0001", "QR")])
        # The dry run names such a chat too, and leaves its note exactly as the old client wrote it.
        third = self.put("s-0003", self.old_note("s-0003", updated_at=now - 86400))
        gone.add("s-0003")
        out = io.StringIO()
        with mock.patch.dict(PL.SPECS, {"faux": spec}):
            self.assertEqual(PL.gone_main(["--platform", "faux"], self.env(), out, now=now, platform="darwin"), 0)
        self.assertIn("would report Faux QR (s-0003, mac)", out.getvalue())
        self.assertNotIn("platform", self.on_disk(third))
        # A spec whose seat is read from the environment (a launcher's) adopts nobody's notes.
        other = self.put("s-0002", self.old_note("s-0002", updated_at=now - 86400))
        launcher = make_spec(platform="faux", source="faux-hook", display="Faux", gone_sessions=lambda env, ids: gone,
                             seat=PL.from_env("AGENT_LAUNCH_SEAT"))
        PL.gone_chats(launcher, self.env(), "CODEX", "mac", now)
        self.assertNotIn("platform", self.on_disk(other))


class ParentTests(Base):
    """A chat's parent is asked for once:  an adapter's lookup can cost a call to another host.  And `parent` is the
    read-only verb such a lookup runs on the host that holds the records."""

    def setUp(self) -> None:
        super().setUp()
        self.parents: dict[str, str] = {}
        self.lookups: list[dict] = []

        def parent_id(payload, env):
            self.lookups.append({"payload": dict(payload), "mac": env.get(PL.ENV_MAC_SSH, "")})
            return self.parents.get(payload.get("session_id"), "")

        self.spec = make_spec(platform="faux", source="faux-hook", display="Faux", parent_id=parent_id)
        patch = mock.patch.dict(PL.SPECS, {"faux": self.spec})
        patch.start()
        self.addCleanup(patch.stop)

    def run_hook(self, payload: dict, at: float, transport=None) -> None:
        self.assertEqual(PL.hook(self.spec, self.env(), io.StringIO(json.dumps(payload)), io.StringIO(), now=at,
                                 platform="darwin", transport=transport), 0)

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "CODEX", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def test_a_lookup_that_found_no_parent_is_not_repeated_inside_the_window(self) -> None:
        down = Calls(self, fail=TimeoutError("down"))
        start = {"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "startup"}
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "c-0001", "prompt": "go"}
        t0 = time.time()
        self.run_hook(start, t0, transport=down)
        self.assertEqual(len(self.lookups), 1)
        self.assertEqual((self.note("c-0001")["parent"], self.note("c-0001")["parent_asked_at"]), ("", t0))
        # A chat with no letter asks for one on its prompts:  each of them would look the parent up again.
        for n in range(1, 4):
            self.run_hook(prompt, t0 + n, transport=down)
        self.run_hook(dict(start, source="resume"), t0 + 5, transport=down)
        self.assertEqual(len(self.lookups), 1)
        # After the window it is asked again, and a parent that is found then is used and kept.
        self.parents["c-0001"] = "p-0001"
        self.run_hook(prompt, t0 + PL.RETRY_SECONDS + 10)
        self.assertEqual(len(self.lookups), 2)
        self.assertEqual(self.note("c-0001")["parent"], "p-0001")
        # A parent the note knows is never asked for again.
        self.run_hook(dict(start, source="resume"), t0 + 10 * PL.RETRY_SECONDS)
        self.run_hook(prompt, t0 + 20 * PL.RETRY_SECONDS)
        self.assertEqual(len(self.lookups), 2)
        self.assertEqual(self.note("c-0001")["parent"], "p-0001")

    def test_a_parent_found_at_the_start_places_the_chat_after_it(self) -> None:
        t0 = time.time()
        self.run_hook({"hook_event_name": "SessionStart", "session_id": "p-0001", "source": "startup"}, t0)
        self.parents["c-0001"] = "p-0001"
        calls = Calls(self)
        self.run_hook({"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "startup"}, t0,
                      transport=calls)
        self.assertIn("--parent-session-id=p-0001", calls.calls[0])
        parent, child = self.note("p-0001")["label"], self.note("c-0001")["label"]
        self.assertEqual((child[0], child != parent), (parent[0], True))

    def test_an_adapter_whose_lookup_raises_has_found_no_parent(self) -> None:
        spec = make_spec(parent_id=mock.Mock(side_effect=OSError("the other host is asleep")))
        chat = PL.Chat(spec, self.env(), "mac", "c-0001", now=50.0)
        note: dict = {}
        self.assertEqual(PL.parent_of(chat, note, {"session_id": "c-0001"}), "")
        self.assertEqual(note, {"parent_asked_at": 50.0})
        # A parent that is found is kept in the note at once, whether or not a registration follows now.
        found = make_spec(parent_id=lambda payload, env: "p-0001")
        note = {"parent_asked_at": 50.0 - PL.RETRY_SECONDS}
        self.assertEqual(PL.parent_of(PL.Chat(found, self.env(), "mac", "c-0001", now=50.0), note, {}), "p-0001")
        self.assertEqual(note, {"parent_asked_at": 50.0, "parent": "p-0001"})

    # ---- `platform-letter parent`:  what another host's hook asks this one ----

    def verb(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        return PL.parent_main(list(argv), self.env(), out), out.getvalue()

    def test_the_verb_prints_the_parents_session_id_and_nothing_else(self) -> None:
        self.parents["c-0001"] = "p-0001"
        self.assertEqual(self.verb("--platform", "faux", "--session-id", "c-0001", "--app-id", "local_abc-1",
                                   "--source", "clear"), (0, "p-0001\n"))
        # The adapter is asked about a start payload made from the arguments, marked as this verb's question.
        self.assertEqual(self.lookups[-1]["payload"], {
            "hook_event_name": "SessionStart", "session_id": "c-0001", "source": "clear",
            PL.PARENT_VERB: True, PL.PARENT_APP_ID: "local_abc-1"})
        self.assertEqual(self.verb("--platform", "faux", "--session-id", "x-0001"), (0, ""))  # no parent:  no line
        self.assertEqual(self.lookups[-1]["payload"], {
            "hook_event_name": "SessionStart", "session_id": "x-0001", "source": "startup", PL.PARENT_VERB: True})
        # Through the entry point, and it reads only:  no note, no stamp, no registry call.
        before = list(self.fake.requests)
        out = io.StringIO()
        self.assertEqual(PL.main(["parent", "--platform", "faux", "--session-id", "c-0001"], env=self.env(),
                                 stdout=out, stderr=out), 0)
        self.assertEqual(out.getvalue(), "p-0001\n")
        self.assertEqual(self.fake.requests, before)
        self.assertFalse(os.path.exists(self.state))

    def test_the_verb_prints_only_what_is_a_session_id_and_not_the_chat_itself(self) -> None:
        for answer in ("c-0001", "not a session id", "../../etc", "", "x\ny-0001", 7, None):
            self.parents["c-0001"] = answer
            self.assertEqual(self.verb("--platform", "faux", "--session-id", "c-0001"), (0, ""), answer)
        spec = make_spec(platform="faux", source="faux-hook", display="Faux",
                         parent_id=mock.Mock(side_effect=RuntimeError("the store changed")))
        with mock.patch.dict(PL.SPECS, {"faux": spec}):
            self.assertEqual(self.verb("--platform", "faux", "--session-id", "c-0001"), (0, ""))

    def test_the_verb_refuses_arguments_it_cannot_check(self) -> None:
        for bad in (
            [],
            ["--platform", "faux"],
            ["--platform", "nosuch", "--session-id", "c-0001"],
            ["--platform", "../x", "--session-id", "c-0001"],
            ["--platform", "faux", "--session-id", "../x"],
            ["--platform", "faux", "--session-id", "c-0001; rm -rf /"],
            ["--platform", "faux", "--session-id", "c-0001", "--app-id", "$(id)"],
            ["--platform", "faux", "--session-id", "c-0001", "--app-id", "a b"],
            ["--platform", "faux", "--session-id", "c-0001", "--app-id", "x" * 200],
            ["--platform", "faux", "--session-id", "c-0001", "--source", "compact"],
            ["--platform", "faux", "--session-id", "c-0001", "--bogus", "1"],
        ):
            self.assertEqual(self.verb(*bad), (2, ""), bad)
        self.assertEqual(self.lookups, [])

    # ---- SESSION_LETTER_MAC_SSH:  where the Mac is, for a hook on another host ----

    def test_the_mac_destination_is_a_setting_that_is_checked(self) -> None:
        self.assertEqual(PL.mac_ssh({PL.ENV_MAC_SSH: "jay@mac.example"}), "jay@mac.example")
        self.assertEqual(PL.mac_ssh({PL.ENV_MAC_SSH: " mac-mini "}), "mac-mini")
        for bad in ("", "-oProxyCommand=evil", "mac mini", "mac;id", "$(id)", "a/b", "x" * 300):
            self.assertEqual(PL.mac_ssh({PL.ENV_MAC_SSH: bad}), "", bad)
        self.assertEqual(PL.mac_ssh({}), "")
        self.assertEqual(PL.ENV_MAC_SSH, "SESSION_LETTER_MAC_SSH")  # no platform's name is in the setting

    def test_the_hooks_flag_is_the_setting(self) -> None:
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "c-0001", "source": "startup"})
        out = io.StringIO()
        code = PL.main(["hook", "--platform", "faux", "--location", "server", "--mac-ssh", "jay@mac"],
                       env=self.env(), stdin=io.StringIO(payload), stdout=out, stderr=out)
        self.assertEqual((code, self.lookups[-1]["mac"]), (0, "jay@mac"))
        # In the process environment it is set in place:  the registry API reads its settings from that same object.
        with mock.patch.dict(os.environ, self.env(), clear=False):
            os.environ.pop(PL.ENV_MAC_SSH, None)
            code = PL.main(["hook", "--platform", "faux", "--location", "server", "--mac-ssh=other@mac"],
                           stdin=io.StringIO(payload.replace("c-0001", "c-0002")), stdout=io.StringIO(),
                           stderr=io.StringIO())
            self.assertEqual((code, os.environ.get(PL.ENV_MAC_SSH)), (0, "other@mac"))
            self.assertEqual(self.note("c-0002")["state"], "open")  # the registry call was made, through that env
        # A flag that names no destination is not the setting, and the hook still runs.
        code = PL.main(["hook", "--platform", "faux", "--location", "server", "--mac-ssh", "-oProxyCommand=x"],
                       env=self.env(), stdin=io.StringIO(payload.replace("c-0001", "c-0003")), stdout=io.StringIO(),
                       stderr=io.StringIO())
        self.assertEqual((code, self.lookups[-1]["mac"]), (0, ""))

    def test_install_writes_the_destination_on_the_hook_command_and_keeps_it(self) -> None:
        installer = PL.JsonHookInstaller(path=os.path.join(".faux", "hooks.json"), events=PL.HOOK_TIMEOUTS)

        def install(*argv: str) -> tuple[int, dict]:
            out = io.StringIO()
            with mock.patch.object(PL, "load_adapter", return_value=SimpleNamespace(INSTALL=installer, SPEC=self.spec)):
                code = PL.install_main(["--platform", "faux", *argv], self.env(), out)
            return code, json.loads(out.getvalue())

        def commands() -> set[str]:
            with open(os.path.join(self.home, ".faux", "hooks.json"), encoding="utf-8") as fh:
                hooks = json.load(fh)["hooks"]
            return {h["command"] for groups in hooks.values() for group in groups for h in group["hooks"]}

        entry = shlex.quote(os.path.join(self.home, ".local", "bin", "platform-letter"))
        code, answer = install("--location", "server", "--mac-ssh", "jay@mac.example")
        self.assertEqual(code, 0, answer)
        want = entry + " hook --platform faux --location server --mac-ssh jay@mac.example"
        self.assertEqual((commands(), answer["command"]), ({want}, want))
        # An install that does not name it keeps the one the host has, as it keeps the location.
        code, answer = install()
        self.assertEqual((code, answer["changed"], commands()), (0, [], {want}))
        code, answer = install("--mac-ssh", "mini")
        self.assertEqual(commands(), {entry + " hook --platform faux --location server --mac-ssh mini"})
        # Something ssh could read as an option is refused, and nothing is written.
        for bad in ("-oProxyCommand=evil", "a b", "x;y"):
            code, answer = install("--mac-ssh=" + bad)
            self.assertEqual((code, answer), (2, {"error": "--mac-ssh must name an ssh destination"}), bad)
        self.assertEqual(commands(), {entry + " hook --platform faux --location server --mac-ssh mini"})
        self.assertEqual(PL.command_for("/h", "faux", "server", "-bad"), "/h/.local/bin/platform-letter hook "
                         "--platform faux --location server")


class DurableWriteTests(Base):
    """A file renamed into place survives a crash only when its folder is flushed too:  the note, the gone stamp and
    a platform's hook file are each fsynced, renamed, and then their folder is fsynced."""

    @contextmanager
    def writes(self):
        """What happens to files while this is open, in order:  ("file", None) and ("folder", its (device, inode))
        for an fsync, ("replace", the destination) for a rename."""
        seen: list[tuple[str, object]] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            info = os.fstat(fd)
            is_folder = stat.S_ISDIR(info.st_mode)
            seen.append(("folder", (info.st_dev, info.st_ino)) if is_folder else ("file", None))
            return real_fsync(fd)

        def replace(src, dst):
            seen.append(("replace", dst))
            return real_replace(src, dst)

        with mock.patch.object(PL.os, "fsync", fsync), mock.patch.object(PL.os, "replace", replace):
            yield seen

    def folder_id(self, path: str) -> tuple[int, int]:
        info = os.stat(os.path.dirname(path))
        return info.st_dev, info.st_ino

    def test_a_note_is_fsynced_renamed_and_then_its_folder_is_fsynced(self) -> None:
        chat = PL.Chat(self.spec, self.env(), "mac", "s-0001", now=1.0)
        with self.writes() as seen:
            self.assertTrue(chat.write({"state": "open", "label": "AA"}))
        self.assertEqual(seen, [("file", None), ("replace", chat.path), ("folder", self.folder_id(chat.path))])
        self.assertEqual(chat.read()["label"], "AA")

    def test_a_note_whose_folder_cannot_be_flushed_is_not_called_saved(self) -> None:
        chat = PL.Chat(self.spec, self.env(), "mac", "s-0001", now=1.0)
        with mock.patch.object(PL, "_sync_folder", side_effect=OSError("no fsync here")):
            self.assertFalse(chat.write({"state": "open", "label": "AA"}))  # and nothing is raised into the hook
        self.assertEqual(os.listdir(os.path.dirname(chat.path)), ["s-0001.json"])  # no temporary file is left

    def test_the_gone_stamp_is_written_the_same_way(self) -> None:
        path = PL.gone_stamp_path(self.env(), "CODEX", "codex", "mac")
        with self.writes() as seen:
            self.assertTrue(PL._save_json(path, {"at": 1.0}))
        self.assertEqual(seen, [("file", None), ("replace", path), ("folder", self.folder_id(path))])

    def test_a_hook_file_is_fsynced_renamed_and_then_its_folder_is_fsynced(self) -> None:
        path = os.path.join(self.home, ".claude", "settings.json")
        out = io.StringIO()
        with self.writes() as seen:
            self.assertEqual(PL.install_main(["--platform", "claude"], self.env(), out), 0)
        self.assertEqual(seen[-3:], [("file", None), ("replace", path), ("folder", self.folder_id(path))])
        # The same holds when the file is there already (it is backed up first, then replaced).
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {}}, fh)
        with self.writes() as seen:
            self.assertEqual(PL.install_main(["--platform", "claude"], self.env(), out), 0)
        self.assertEqual(seen[-2:], [("replace", path), ("folder", self.folder_id(path))])
        # A folder that cannot be flushed is an install that failed, not one reported as done.
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hooks": {}}, fh)
        with mock.patch.object(PL, "_sync_folder", side_effect=OSError("no fsync here")), self.assertRaises(OSError):
            PL.install_main(["--platform", "claude"], self.env(), out)


class InstallTests(Base):
    def installer(self, **over) -> PL.JsonHookInstaller:
        args = dict(path=".codex/hooks.json", events={"SessionStart": 20, "UserPromptSubmit": 15, "SessionEnd": 15},
                    trust="re-trust the new hook entries")
        args.update(over)
        return PL.JsonHookInstaller(**args)

    def run_install(self, installer=None, **opts) -> tuple[int, dict]:
        out = io.StringIO()
        options = PL.InstallOptions(**dict({"platform": "codex"}, **opts))
        code = PL.install_json_hooks(installer or self.installer(), options, {"HOME": self.home}, out)
        return code, json.loads(out.getvalue())

    def hooks_path(self) -> str:
        return os.path.join(self.home, ".codex", "hooks.json")

    def load(self) -> dict:
        with open(self.hooks_path()) as fh:
            return json.load(fh)

    def seed(self, doc: dict, mode: int = 0o640) -> None:
        os.makedirs(os.path.dirname(self.hooks_path()), exist_ok=True)
        with open(self.hooks_path(), "w") as fh:
            json.dump(doc, fh)
        os.chmod(self.hooks_path(), mode)

    def test_install_is_idempotent_keeps_other_hooks_and_removes_cleanly(self) -> None:
        other = {"hooks": [{"type": "command", "command": "/x/guard.py", "timeout": 5}]}
        self.seed({"features": {"hooks": True}, "hooks": {"SessionStart": [other]}})
        code, out = self.run_install(location="server")
        self.assertEqual((code, sorted(out["changed"])), (0, ["SessionEnd", "SessionStart", "UserPromptSubmit"]))
        self.assertEqual(out["trust"], "re-trust the new hook entries")
        self.assertEqual(os.stat(out["backup"]).st_mode & 0o777, 0o600)  # a backup is private whatever the source
        self.assertEqual(os.stat(self.hooks_path()).st_mode & 0o777, 0o640)  # the file keeps its own mode
        doc = self.load()
        self.assertEqual(doc["features"], {"hooks": True})
        self.assertEqual(doc["hooks"]["SessionStart"][0], other)
        entry = doc["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(
            entry["command"],
            "%s hook --platform codex --location server"
            % os.path.join(self.home, ".local", "bin", "platform-letter"),
        )
        self.assertEqual(entry["timeout"], 15)
        again = self.run_install(location="server")[1]
        self.assertEqual((again["changed"], "trust" in again), ([], False))
        bare_again = self.run_install()[1]  # no flags: the installed ones are kept
        self.assertEqual(bare_again["changed"], [])
        code, check = self.run_install(check=True)
        self.assertEqual((code, all(check["installed"].values()), all(check["current"].values())), (0, True, True))
        self.run_install(remove=True)
        self.assertEqual(self.load()["hooks"], {"SessionStart": [other]})
        self.assertEqual(self.run_install(check=True)[0], 1)
        self.assertEqual(self.run_install(remove=True)[1]["changed"], [])

    def test_a_new_hook_added_after_ours_is_not_reordered(self) -> None:
        self.run_install()
        doc = self.load()
        later = {"hooks": [{"type": "command", "command": "/x/later.py"}]}
        doc["hooks"]["SessionStart"].append(later)
        self.seed(doc)
        self.assertEqual(self.run_install()[1]["changed"], [])
        self.assertEqual(self.load()["hooks"]["SessionStart"][1], later)

    def test_an_outdated_entry_is_replaced_in_place(self) -> None:
        self.run_install()
        doc = self.load()
        first = {"hooks": [{"type": "command", "command": "/x/first.py"}]}
        doc["hooks"]["SessionStart"].insert(0, first)
        doc["hooks"]["SessionStart"][1]["hooks"][0]["timeout"] = 1
        self.seed(doc)
        code, check = self.run_install(check=True)
        self.assertEqual((code, check["installed"]["SessionStart"], check["current"]["SessionStart"]), (0, True, False))
        self.assertEqual(self.run_install()[1]["changed"], ["SessionStart"])
        entries = self.load()["hooks"]["SessionStart"]
        self.assertEqual(entries[0], first)
        self.assertEqual(entries[1]["hooks"][0]["timeout"], 20)
        self.assertEqual(len(entries), 2)

    def test_only_this_platforms_entries_are_ours(self) -> None:
        self.run_install(platform="opencode", installer=self.installer(path=".codex/hooks.json"))
        self.run_install()
        self.assertEqual(len(self.load()["hooks"]["SessionStart"]), 2)
        self.run_install(remove=True)
        entries = self.load()["hooks"]["SessionStart"]
        self.assertEqual(len(entries), 1)
        self.assertIn("--platform opencode", entries[0]["hooks"][0]["command"])
        self.assertIsNone(PL.installed_command({"command": "platform-letter hook --platform codexx"}, "codex"))
        self.assertIsNone(PL.installed_command({"command": "platform-letter registry --platform codex"}, "codex"))
        self.assertIsNone(PL.installed_command({"command": "'unclosed"}, "codex"))
        self.assertIsNotNone(PL.installed_command({"deep": [{"cmd": "/p/platform-letter hook --platform=codex"}]}, "codex"))

    def test_a_file_the_installer_does_not_understand_is_refused_untouched(self) -> None:
        for bad in ({"hooks": ["x"]}, {"hooks": {"SessionStart": "x"}}, ["x"]):
            self.seed(bad)
            for opts in ({"location": "server"}, {"check": True}, {"remove": True}):
                code, out = self.run_install(**opts)
                self.assertEqual(code, 2, (bad, opts))
                self.assertIn("error", out)
            self.assertEqual(self.load(), bad)
        with open(self.hooks_path(), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.run_install()[0], 2)
        for opts in ({"location": "cloud"},):
            self.assertEqual(self.run_install(**opts)[0], 2, opts)

    def test_a_null_hooks_field_reads_as_empty(self) -> None:
        self.seed({"hooks": None})
        self.assertEqual(self.run_install()[0], 0)

    def test_install_creates_a_private_folder_and_file(self) -> None:
        fresh = os.path.join(self.tmp.name, "fresh-home")
        out = io.StringIO()
        self.assertEqual(
            PL.install_json_hooks(self.installer(), PL.InstallOptions("codex", "mac"), {"HOME": fresh}, out), 0
        )
        path = os.path.join(fresh, ".codex", "hooks.json")
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(out.getvalue())["backup"], "")  # nothing to back up

    def test_the_hook_file_is_never_written_through_a_planted_symlink(self) -> None:
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as fh:
            fh.write("keep me")
        fresh = os.path.join(self.tmp.name, "planted-home")
        path = os.path.join(fresh, ".codex", "hooks.json")
        os.makedirs(os.path.dirname(path))
        os.symlink(victim, path + ".letter.tmp")  # the fixed name the installer used to write through
        out = io.StringIO()
        self.assertEqual(
            PL.install_json_hooks(self.installer(), PL.InstallOptions("codex", "mac"), {"HOME": fresh}, out), 0
        )
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_check_and_remove_on_an_absent_file_write_nothing(self) -> None:
        self.assertEqual(self.run_install(check=True)[0], 1)
        self.assertEqual(self.run_install(remove=True), (0, {"platform": "codex", "changed": [], "path": self.hooks_path()}))
        self.assertFalse(os.path.exists(self.hooks_path()))

    def test_a_file_of_named_groups(self) -> None:
        installer = PL.JsonHookInstaller(
            path=".gemini/config/hooks.json", events={"SessionStart": 20, "SessionEnd": 15},
            locate=PL.named_group("session-letter", enabled=True),
            build=lambda command, timeout, event: {"command": command, "timeout_ms": timeout * 1000},
        )
        path = os.path.join(self.home, ".gemini", "config", "hooks.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            json.dump({"lane-guard": {"enabled": True, "PreToolUse": [{"command": "/x"}]}}, fh)
        self.assertEqual(self.run_install(installer, platform="antigravity")[0], 0)
        with open(path) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["lane-guard"], {"enabled": True, "PreToolUse": [{"command": "/x"}]})
        self.assertTrue(doc["session-letter"]["enabled"])
        self.assertEqual(doc["session-letter"]["SessionEnd"][0]["timeout_ms"], 15000)
        self.assertEqual(self.run_install(installer, platform="antigravity")[1]["changed"], [])
        self.run_install(installer, platform="antigravity", remove=True)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["lane-guard"]["PreToolUse"], [{"command": "/x"}])

    def test_a_file_that_is_all_ours_goes_when_the_last_hook_does(self) -> None:
        installer = self.installer(path=".grok/hooks/session-letter.json", whole_file=True)
        path = os.path.join(self.home, ".grok", "hooks", "session-letter.json")
        self.assertEqual(self.run_install(installer, platform="grok")[0], 0)
        self.assertTrue(os.path.exists(path))
        out = self.run_install(installer, platform="grok", remove=True)[1]
        self.assertTrue(out["removed_file"])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.stat(out["backup"]).st_mode & 0o777, 0o600)

    def ours(self, timeout: int = 1, platform: str = "codex") -> dict:
        command = "%s hook --platform %s" % (os.path.join(self.home, ".local", "bin", "platform-letter"), platform)
        return {"type": "command", "command": command, "timeout": timeout}

    def test_a_group_that_holds_another_tools_hook_keeps_it_and_its_matcher(self) -> None:
        foreign = {"type": "command", "command": "/opt/other/keep-me.sh", "timeout": 5}
        group = {"matcher": "startup|resume", "hooks": [foreign, self.ours()]}
        self.seed({"hooks": {"SessionStart": [group]}})
        check = self.run_install(check=True)[1]
        self.assertEqual((check["installed"]["SessionStart"], check["current"]["SessionStart"]), (True, False))
        self.assertEqual(self.run_install(location="server")[0], 0)
        entries = self.load()["hooks"]["SessionStart"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["matcher"], "startup|resume")  # the owner's matcher stays
        self.assertEqual(entries[0]["hooks"][0], foreign)  # and so does the other tool's hook, first
        self.assertEqual(entries[0]["hooks"][1]["timeout"], 20)  # ours is replaced where it stood
        self.assertTrue(entries[0]["hooks"][1]["command"].endswith("--location server"))
        self.assertEqual(self.run_install()[1]["changed"], [])
        code, check = self.run_install(check=True)
        self.assertEqual((code, check["current"]["SessionStart"]), (0, True))
        self.run_install(remove=True)
        self.assertEqual(self.load()["hooks"], {"SessionStart": [{"matcher": "startup|resume", "hooks": [foreign]}]})

    def test_a_group_with_only_our_hook_under_the_owners_matcher_keeps_the_matcher(self) -> None:
        self.seed({"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [self.ours()]}]}})
        self.run_install()
        self.assertEqual(self.load()["hooks"]["SessionStart"][0]["matcher"], "startup")
        self.run_install(remove=True)
        self.assertNotIn("SessionStart", self.load()["hooks"])  # a group with no hook left is dropped

    def test_a_second_copy_of_our_hook_in_another_group_goes_and_the_foreign_hook_there_stays(self) -> None:
        foreign = {"type": "command", "command": "/opt/other/keep-me.sh"}
        self.seed({"hooks": {"SessionStart": [{"hooks": [self.ours()]}, {"hooks": [self.ours(), foreign]}]}})
        self.run_install()
        self.assertEqual(
            [[h["command"].split()[0] for h in g["hooks"]] for g in self.load()["hooks"]["SessionStart"]],
            [[os.path.join(self.home, ".local", "bin", "platform-letter")], ["/opt/other/keep-me.sh"]],
        )

    def test_a_whole_file_adapter_keeps_a_file_that_still_holds_other_hooks(self) -> None:
        installer = self.installer(path=".grok/hooks/session-letter.json", whole_file=True)
        path = os.path.join(self.home, ".grok", "hooks", "session-letter.json")
        os.makedirs(os.path.dirname(path))
        foreign = {"hooks": [{"type": "command", "command": "/opt/other/keep-me.sh"}]}
        with open(path, "w") as fh:
            json.dump({"hooks": {"PreToolUse": [foreign]}}, fh)
        self.assertEqual(self.run_install(installer, platform="grok")[0], 0)
        out = self.run_install(installer, platform="grok", remove=True)[1]
        self.assertNotIn("removed_file", out)
        with open(path) as fh:
            self.assertEqual(json.load(fh), {"hooks": {"PreToolUse": [foreign]}})

    def test_two_rewrites_in_one_moment_keep_the_original_backup(self) -> None:
        foreign = {"hooks": [{"type": "command", "command": "/opt/other/keep-me.sh"}]}
        self.seed({"features": {"hooks": True}, "hooks": {"SessionStart": [foreign]}})
        with open(self.hooks_path(), "rb") as fh:
            original = fh.read()
        real = PL.datetime
        frozen = types.SimpleNamespace(now=lambda tz=None: real(2026, 10, 29, 12, 0, 0, 123456, tzinfo=PL.UTC))
        with mock.patch.object(PL, "datetime", frozen):
            first = self.run_install()[1]["backup"]
            second = self.run_install(remove=True)[1]["backup"]  # the same microsecond
            third = self.run_install(location="server")[1]["backup"]
        self.assertEqual(len({first, second, third}), 3)
        with open(first, "rb") as fh:
            self.assertEqual(fh.read(), original)  # the first still holds the file as the owner left it
        with open(second, "rb") as fh:
            self.assertIn(b"platform-letter", fh.read())
        for name in (first, second, third):
            self.assertEqual(os.stat(name).st_mode & 0o777, 0o600)

    def test_the_command_names_this_hosts_own_script(self) -> None:
        self.assertEqual(PL.command_for("/h", "codex"), "/h/.local/bin/platform-letter hook --platform codex")
        self.assertEqual(
            PL.command_for("/my home", "codex", "server"),
            "'/my home/.local/bin/platform-letter' hook --platform codex --location server",
        )

    def test_install_main_finds_the_adapter(self) -> None:
        module = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"), INSTALL=self.installer())
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"simple_tools.platform_letter_faux": module}):
            code = PL.install_main(["--platform", "faux", "--location", "server"],
                                   {"HOME": self.home}, out)
        self.assertEqual(code, 0)
        self.assertIn("--platform faux --location server", json.dumps(self.load()))
        # An adapter with an installer of its own is called with the parsed options.
        seen = []
        own = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"),
                                    install=lambda opts, env, stdout: seen.append(opts) or 7)
        with mock.patch.dict(sys.modules, {"simple_tools.platform_letter_faux": own}):
            code = PL.install_main(["--platform", "faux", "--check"], {"HOME": self.home}, io.StringIO())
        self.assertEqual((code, seen[0].check, seen[0].platform), (7, True, "faux"))
        bare = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"))
        with mock.patch.dict(sys.modules, {"simple_tools.platform_letter_faux": bare}):
            self.assertEqual(PL.install_main(["--platform", "faux"], {"HOME": self.home}, io.StringIO()), 2)
        self.assertEqual(PL.install_main(["--platform", "nosuch"], {"HOME": self.home}, io.StringIO()), 2)


class EntryScriptTests(unittest.TestCase):
    def run_entry(self, *argv: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", "-B", str(ENTRY), *argv], input=stdin, capture_output=True, text=True,
            timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )

    def test_the_script_is_executable_and_valid_python_3_9(self) -> None:
        self.assertTrue(os.access(ENTRY, os.X_OK))
        source = ENTRY.read_text()
        self.assertTrue(source.startswith("#!/usr/bin/env python3"))
        ast.parse(source, feature_version=(3, 9))  # the part that runs first must parse on the system Python

    def test_a_hook_that_cannot_run_says_nothing_and_exits_0(self) -> None:
        for argv in (["hook"], ["hook", "--platform", "nosuch"], ["hook", "--platform", "claude"]):
            result = self.run_entry(*argv, stdin="{}")
            self.assertEqual((result.returncode, result.stdout), (0, ""), argv)

    def test_usage(self) -> None:
        result = self.run_entry("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("platform-letter", result.stdout)
        self.assertEqual(self.run_entry().returncode, 2)
        self.assertEqual(self.run_entry("nonsense").returncode, 2)

    def test_the_registry_side_on_a_host_with_no_registry_api_is_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(ENTRY), "registry", "touch", "--platform", "codex",
                 "--location", "server", "--session-id", "s-0001"],
                capture_output=True, text=True, timeout=60,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "SIMPLE_TOOLS_STATE_DIR": tmp},
            )
            self.assertEqual(result.returncode, 5)
            self.assertIn("not_configured", result.stdout)
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
