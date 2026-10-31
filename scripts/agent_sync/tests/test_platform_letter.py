"""`platform-letter` (AFC-564): Operator session letters and "<letter>: <Title>" titles for the platforms other than
Claude and MiniMax.  The registry side runs against a temporary registry in the double (pair) letter space, which main's
registry has; the hook runs in process, with the ssh leg faked by a runner that hands the remote command to the same
registry side.  No test depends on which pair the registry hands out first (main takes the lowest free pair, the pair
registry takes the main-chat order), only on the letter being a pair that the Operator, the note and the title agree on."""

from __future__ import annotations

import ast
import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL

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
        self.db = os.path.join(self.state, "operator", "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()  # the Operator's registry exists, as it does on the Mac
        self.spec = make_spec()

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "AGENT_SYNC_STATE_DIR": self.state}
        env.update(extra)
        return env

    def server_env(self, **extra: str) -> dict[str, str]:
        return self.env(SESSION_LETTER_LOCATION="server", SESSION_LETTER_REMOTE="mac", **extra)

    def registry(self, *argv: str, now: float | None = None) -> tuple[int, dict]:
        out = io.StringIO()
        code = PL.registry_main(list(argv), env=self.env(), stdout=out, now=now)
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
        self, sid: str, *, title: str = "", now: float | None = None, platform: str = "codex",
        location: str = "server", **flags: str,
    ) -> tuple[int, dict]:
        flags.setdefault("letter_pick", "lowest")
        if title:
            flags["title"] = title
        argv = self.reg_argv("register", sid, platform=platform, location=location, **flags)
        return self.registry(*argv, now=now)

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
        self.assertEqual(make_spec().off_variable, "CODEX_LETTER")
        self.assertEqual(make_spec(platform="mc-code", seat="MC").off_variable, "MC_CODE_LETTER")
        for bad in (
            dict(platform="claude", seat="CLAUDE"),  # reserved to its own hook
            dict(platform="minimax", seat="MM"),
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

    def test_location_and_remote(self) -> None:
        self.assertEqual(PL.location({}, "darwin"), "mac")
        self.assertIsNone(PL.location({}, "linux"))
        self.assertEqual(PL.location({PL.ENV_LOCATION: "Server"}, "linux"), "server")
        self.assertIsNone(PL.location({PL.ENV_LOCATION: "my-laptop"}, "linux"))
        self.assertEqual(PL.location({PL.ENV_LOCATION_FALLBACK: "server"}, "linux"), "server")
        both = {PL.ENV_LOCATION: "mac", PL.ENV_LOCATION_FALLBACK: "server"}
        self.assertEqual(PL.location(both, "linux"), "mac")  # the generic name wins over the Claude one
        self.assertEqual(PL.location(both, "linux", flag="server"), "server")  # a flag wins over both
        self.assertIsNone(PL.location({}, "linux", flag="cloud"))
        self.assertEqual(PL.remote_destination({PL.ENV_REMOTE_FALLBACK: "mac"}), "mac")
        self.assertEqual(
            PL.remote_destination({PL.ENV_REMOTE: "a", PL.ENV_REMOTE_FALLBACK: "b"}, flag="c"), "c"
        )

    def test_scope(self) -> None:
        spec = make_spec()
        self.assertTrue(PL.in_scope(spec, {}, {}))
        self.assertFalse(PL.in_scope(spec, {"SESSION_LETTER": "0"}, {}))
        self.assertFalse(PL.in_scope(spec, {"CODEX_LETTER": "0"}, {}))
        self.assertTrue(PL.in_scope(spec, {"OPENCODE_LETTER": "0"}, {}))
        # A launcher-run session never takes a platform letter: BotFleet, Grok Bot and the wake runs have their own.
        for env in (
            {"AGENT_LAUNCHER": "botfleet"},
            {"AGENT_LAUNCHER": "grok-bot"},
            {"AGENT_LAUNCHER": "agent-sync-wake"},
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
        env = {"AGENT_SYNC_STATE_DIR": "/state"}
        self.assertEqual(PL.note_path(env, "CODEX", "s-0001"), "/state/CODEX/letters/s-0001.json")
        self.assertEqual(PL.note_path(env, "../../tmp/x", "s-0001"), "/state/UNKNOWN/letters/s-0001.json")

    def test_adapters_are_found_by_a_checked_name(self) -> None:
        spec = make_spec(platform="testbot", seat="TESTBOT")
        self.addCleanup(PL.SPECS.pop, "testbot", None)
        PL.register_spec(spec)
        self.assertIs(PL.load_spec("testbot"), spec)
        self.assertIsNone(PL.load_spec("nosuchplatform"))
        for name in ("", "../etc", "os", "claude", "Codex", "a;b"):
            self.assertIsNone(PL.load_adapter(name), name)
        module = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"))
        with mock.patch.dict(sys.modules, {"agent_sync.platform_letter_faux": module}):
            self.assertEqual(PL.load_spec("faux").seat, "FAUX")
        wrong = types.SimpleNamespace(SPEC=make_spec(platform="other", seat="OTHER"))
        with mock.patch.dict(sys.modules, {"agent_sync.platform_letter_faux": wrong}):
            self.assertIsNone(PL.load_spec("faux"))  # an adapter names its own platform


class ContractTests(unittest.TestCase):
    """The one function that speaks `operator_registry`: it asks only for what the registry takes."""

    def registry_of(self, register_session):
        return SimpleNamespace(register_session=register_session, ISSUER_OPERATOR="OPERATOR")

    def ask(self, OR_, **extra):
        return PL.operator_register(
            OR_, "db", platform="codex", location="server", sid="s-0001", seat="CODEX", source="x",
            title="T", space="double", pick="primary", **extra,
        )

    def test_an_old_registry_gets_neither_pick_nor_parent(self) -> None:
        seen = {}

        def register_session(db, *, platform, verified_host, profile_scope, native_session_id, seat, source,
                             title, issuer, letter_space):
            seen.update(letter_space=letter_space)
            return {"outcome": "registered"}

        self.ask(self.registry_of(register_session), parent="p-0001")
        self.assertEqual(seen, {"letter_space": "double"})

    def test_a_registry_with_neither_issuer_nor_space_still_registers(self) -> None:
        seen = {}

        def register_session(db, *, platform, verified_host, profile_scope, native_session_id, seat, source, title):
            seen.update(title=title)
            return {"outcome": "registered"}

        self.ask(self.registry_of(register_session), parent="p-0001")
        self.assertEqual(seen, {"title": "T"})

    def test_a_pair_registry_gets_both(self) -> None:
        seen = {}

        def register_session(db, *, platform, verified_host, profile_scope, native_session_id, seat, source,
                             title, issuer, letter_space, letter_pick, parent_session_id=""):
            seen.update(letter_pick=letter_pick, parent=parent_session_id)
            return {"outcome": "registered"}

        registry = self.registry_of(register_session)
        self.ask(registry, parent="p-0001")
        self.assertEqual(seen, {"letter_pick": "primary", "parent": "p-0001"})
        self.ask(registry)
        self.assertEqual(seen["parent"], "")  # no parent, no argument

    def test_a_renamed_parent_parameter_is_one_change(self) -> None:
        seen = {}

        def register_session(db, *, platform, verified_host, profile_scope, native_session_id, seat, source,
                             title, issuer, letter_space, letter_pick, parent=""):
            seen.update(parent=parent)
            return {"outcome": "registered"}

        self.ask(self.registry_of(register_session), parent="p-0001")
        self.assertEqual(seen, {"parent": "p-0001"})

    def test_the_pick(self) -> None:
        with_primary = SimpleNamespace(LETTER_PICKS=("lowest", "oldest", "primary"))
        self.assertEqual(PL.resolve_pick(with_primary, "auto"), "primary")
        self.assertEqual(PL.resolve_pick(SimpleNamespace(), "auto"), "lowest")
        self.assertEqual(PL.resolve_pick(with_primary, "oldest"), "oldest")

    def test_this_checkouts_registry(self) -> None:
        self.assertTrue(PL._accepts(OR.register_session, "letter_space"))
        self.assertTrue(PL._accepts(OR.SessionRegistry.reopen, "source"))

    def test_a_reopen_that_would_turn_a_pair_chat_into_a_single_letter_one_is_refused_unwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "registry.sqlite3")
            first = self.make(db, "s-0001")
            reg = OR.SessionRegistry(db)
            try:
                reg.close(first["record_id"], source="test")
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
    def test_refuses_a_host_without_a_registry(self) -> None:
        other = os.path.join(self.tmp.name, "elsewhere")
        out = io.StringIO()
        code = PL.registry_main(
            self.reg_argv("register", "s-0001"),
            env={"HOME": self.home, "AGENT_SYNC_STATE_DIR": other},
            stdout=out,
        )
        self.assertEqual(code, 3)
        self.assertIn("no Operator registry", out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(other, "operator")))

    def test_the_claude_and_minimax_namespaces_are_not_this_sides_to_write(self) -> None:
        for platform in ("claude", "minimax", "Codex", "", "../x"):
            with self.subTest(platform=platform):
                code, out = self.registry(*self.reg_argv("register", "s-0001", platform=platform))
                self.assertEqual(code, 2)
                self.assertIn("error", out)
        self.assertIsNone(self.lookup("s-0001", platform="claude"))
        self.assertIsNone(self.lookup("s-0001", platform="minimax"))

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
        self.assertEqual((back["outcome"], back["label"], back["generation"], back["previous"]), ("reopened", label, 2, label))
        self.assertEqual(self.lookup("s-0001")["title"], "Back Again")

    def test_a_chat_whose_pair_went_to_another_chat_never_gets_a_single_letter(self) -> None:
        label = self.register("s-0001")["label"]
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
        # The command the README gives for checking the hop before a server hook is installed.
        before = os.listdir(os.path.dirname(self.db))
        code, out = self.registry(
            "show", "--platform", "smoke", "--location", "server", "--session-id", "smoke-check-0001"
        )
        self.assertEqual((code, out), (5, {"error": PL.NO_REGISTRATION}))
        self.assertEqual(os.listdir(os.path.dirname(self.db)), before)
        self.assertIsNone(self.lookup("smoke-check-0001", platform="smoke"))

    def test_show_reads_and_changes_nothing(self) -> None:
        self.register("s-0001", title="First Chat")
        self.set_seen("s-0001", "2026-01-01T00:00:00.000Z")
        code, out = self.registry(*self.reg_argv("show", "s-0001"))
        self.assertEqual((code, out["found"], out["title"]), (0, True, "First Chat"))
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

    def test_sweep_closes_only_this_adapters_stale_chats(self) -> None:
        self.register("s-0001")
        OR.register_session(
            self.db, platform="codex", verified_host="mac", profile_scope="default",
            native_session_id="op-0001", seat="CODEX", source="operator-registration", issuer=OR.ISSUER_OPERATOR,
            letter_space="double",
        )
        OR.register_session(
            self.db, platform="minimax", verified_host="server", profile_scope="default",
            native_session_id="mm-0001", seat="MM", source="test", issuer=OR.ISSUER_OPERATOR,
        )
        for sid in ("s-0001", "op-0001", "mm-0001"):
            self.set_seen(sid, "2026-01-01T00:00:00.000Z")
        out = self.register("s-0002")
        self.assertEqual(out["swept"], 1)
        self.assertEqual(self.lookup("s-0001")["state"], "closed")
        self.assertEqual(self.lookup("op-0001", "mac")["state"], "active")  # the Operator's own, never swept
        self.assertEqual(self.lookup("mm-0001", platform="minimax")["state"], "active")

    def fill_the_letters_and_ask_for_one_more(self) -> None:
        pairs = ("AA", "BB", "CC")
        # Times are relative to the clock:  the registry's stale sweep reads the real clock, so fixed dates fall more
        # than 36 hours behind it and the rows are closed as stale before the least recently seen can be evicted.
        now = time.time()

        def seen(hours_ago: float) -> str:
            return OR.datetime.fromtimestamp(now - hours_ago * 3600, OR.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        with mock.patch.dict(OR.LETTER_SPACES, {"double": pairs}):
            for i in range(3):
                self.register("s-%04d" % i)
                self.set_seen("s-%04d" % i, seen(2 - i))
            self.set_seen("s-0001", seen(3))  # the oldest
            held = self.lookup("s-0001")["label"]
            out = self.register("s-0099", now=now)
        self.assertEqual((out["evicted"], out["label"], out["generation"]), (1, held, 2))
        self.assertEqual(self.lookup("s-0001")["state"], "closed")

    def test_full_letters_evict_the_least_recently_seen(self) -> None:
        self.fill_the_letters_and_ask_for_one_more()

    def test_the_eviction_does_not_depend_on_the_calendar(self) -> None:
        """The same scenario with the real clock years away from the fixture's: the test cannot go red with the date."""
        years_on = OR.datetime(2031, 6, 1, tzinfo=OR.UTC).timestamp()
        with mock.patch("time.time", return_value=years_on):
            self.fill_the_letters_and_ask_for_one_more()

    def test_the_operators_own_chats_are_never_evicted(self) -> None:
        with mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA", "BB")}):
            for sid in ("op-0001", "op-0002"):
                OR.register_session(
                    self.db, platform="codex", verified_host="mac", profile_scope="default",
                    native_session_id=sid, seat="CODEX", source="operator-registration",
                    issuer=OR.ISSUER_OPERATOR, letter_space="double",
                )
                self.set_seen(sid, "2026-01-01T00:00:00.000Z")
            code, out = self.registry(*self.reg_argv("register", "s-0001", letter_pick="lowest"))
        self.assertEqual(code, 5)
        self.assertIn("in use", out["error"])
        for sid in ("op-0001", "op-0002"):
            self.assertEqual(self.lookup(sid, "mac")["state"], "active")

    def test_one_bad_record_does_not_stop_the_others_closing(self) -> None:
        self.register("s-0001")
        rid = self.lookup("s-0001")["record_id"]
        self.assertEqual(PL._close_records(OR, self.db, ["no-such-record", rid], "test", "test"), 1)
        self.assertEqual(self.lookup("s-0001")["state"], "closed")

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


class FakeSsh:
    """Stands in for the SSH call to the Mac: checks the command's shape and runs the remote registry side in process."""

    def __init__(self, test: Base, fail: Exception | None = None):
        self.test, self.fail, self.calls = test, fail, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.fail is not None:
            raise self.fail
        self.test.assertEqual(cmd[:7], ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "--"])
        self.test.assertEqual(cmd[7], "mac")
        self.test.assertEqual(kwargs["timeout"], PL.REMOTE_TIMEOUT)
        words = shlex.split(cmd[8])
        self.test.assertEqual(words[:2], ["$HOME/.local/bin/platform-letter", "registry"])
        out = io.StringIO()
        code = PL.registry_main(words[2:], env=self.test.env(), stdout=out)
        return SimpleNamespace(stdout=out.getvalue(), returncode=code)

    def ops(self) -> list[str]:
        return [shlex.split(c[8])[2] for c in self.calls]


class FlakySsh(FakeSsh):
    """FakeSsh whose Mac does not answer the first `times` calls of the ops named (exit 255, no result)."""

    def __init__(self, test: Base, ops: tuple[str, ...] = (), times: int = 1):
        super().__init__(test)
        self.failing, self.times, self.failed = set(ops), times, 0

    def __call__(self, cmd, **kwargs):
        if shlex.split(cmd[8])[2] in self.failing and self.failed < self.times:
            self.failed += 1
            self.calls.append(cmd)
            return SimpleNamespace(stdout="", returncode=255)
        return super().__call__(cmd, **kwargs)


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
            runner=runner or FakeSsh(self), now=now, platform=platform, transport=transport, **flags,
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

    def test_mac_registers_in_process(self) -> None:
        def no_ssh(*a, **k):
            raise AssertionError("the Mac never uses ssh")

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "m-0001", "source": "startup"},
                        env=self.env(), runner=no_ssh, platform="darwin")
        self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")
        self.assertRegex(self.lookup("m-0001", "mac")["label"], PAIR)

    def test_flags_name_the_location_and_the_registry_host(self) -> None:
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        env=self.env(), runner=ssh, location_flag="server", remote_flag="mac")
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
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "resume",
                         "session_title": "PR: Review"}, runner=ssh)
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

    def test_clear_closes_and_reopen_checks_again(self) -> None:
        self.spec = make_spec(close_reasons=("clear", "logout", "other"), title_in_output=True)
        self.start("s-0001", session_title="Old Name")
        old = self.label("s-0001")
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

    def test_a_letter_retired_while_open_is_reissued(self) -> None:
        self.start("s-0001")
        old = self.label("s-0001")
        self.prompt("s-0001", "first task", session_title="%s: New Chat" % old)
        reg = OR.SessionRegistry(self.db)
        try:
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
        ssh = FakeSsh(self)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=ssh)
        label = self.label("s-0001")
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001",
                         "prompt": "rotate %s today" % token, "session_title": "%s: New Chat" % label}, runner=ssh)
        self.assertEqual(out["sessionTitle"], "%s: Rotate Today" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "Rotate Today")
        with open(os.path.join(self.state, "CODEX", "letters", "s-0001.json")) as fh:
            self.assertNotIn(token, fh.read())
        self.assertNotIn(token, json.dumps(ssh.calls))

    def test_steady_state_prompts_cost_no_hop_and_a_touch_is_rare(self) -> None:
        ssh = FakeSsh(self)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=ssh)
        label = self.label("s-0001")
        title = "%s: Fix the Build" % label
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                   "session_title": "%s: New Chat" % label}, runner=ssh)
        self.assertEqual(ssh.ops(), ["register", "title"])
        for i in range(5):
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more %d" % i,
                       "session_title": title}, runner=ssh)
        self.assertEqual(len(ssh.calls), 2)
        later = time.time() + PL.TOUCH_SECONDS + 5
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "later",
                   "session_title": title}, runner=ssh, now=later)
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "later still",
                   "session_title": title}, runner=ssh, now=later + 60)
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
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x",
                         "session_title": "%s: Other Words" % label}, runner=ssh)
        self.assertIsNone(out)
        self.assertEqual(self.lookup("s-0001")["title"], "Other Words")
        self.assertEqual(ssh.ops(), ["title"])
        # A platform that ignores the title it was given is not asked again for the same name.
        for _ in range(3):
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x",
                       "session_title": "Stubborn Name"}, runner=ssh)
        self.assertEqual(ssh.ops(), ["title", "title"])

    def test_a_rename_event(self) -> None:
        spec = make_spec(events={"session.created": "start", "session.renamed": "rename", "session.deleted": "end"},
                         output="json")
        PL.hook(spec, self.server_env(), io.StringIO(json.dumps({"event": "session.created", "session_id": "s-0001"})),
                io.StringIO(), runner=FakeSsh(self))
        label = self.label("s-0001")
        out = io.StringIO()
        PL.hook(spec, self.server_env(), io.StringIO(json.dumps(
            {"event": "session.renamed", "session_id": "s-0001", "title": "Fresh Name"})), out, runner=FakeSsh(self))
        body = json.loads(out.getvalue())["platform_letter"]
        self.assertEqual((body["event"], body["label"], body["title"]), ("rename", label, "%s: Fresh Name" % label))
        self.assertEqual(self.lookup("s-0001")["title"], "Fresh Name")

    def test_the_remote_must_be_named_and_plain(self) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}
        ssh = FakeSsh(self)
        for remote in ("", "-oProxyCommand=touch /tmp/x"):
            out = self.hook(payload, env=self.env(SESSION_LETTER_LOCATION="server", SESSION_LETTER_REMOTE=remote),
                            runner=ssh)
            self.assertIn("SESSION_LETTER_REMOTE does not name an ssh destination", out["additionalContext"])
        self.assertEqual(ssh.calls, [])
        out = self.hook(payload, env=self.env(SESSION_LETTER_LOCATION="server"), runner=ssh, remote_flag="--x")
        self.assertIn("does not name an ssh destination", out["additionalContext"])

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
                def bad(cmd, **kwargs):
                    return SimpleNamespace(stdout=answer, returncode=0)

                out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                                runner=bad)
                self.assertIn("No Operator letter yet", out["additionalContext"])
                self.assertEqual(self.note("s-0001")["state"], "failed")

    def test_a_seat_mismatch_takes_no_letter(self) -> None:
        def answer(argv):
            return {"outcome": "seat_mismatch", "label": "", "record_id": "r1"}

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        transport=answer)
        self.assertIn("another seat", out["additionalContext"])
        self.assertEqual(self.note("s-0001")["state"], "mismatch")
        self.assertIsNone(self.prompt("s-0001", "x"))

    def test_compact_only_reminds(self) -> None:
        self.start("s-0001")
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "compact"}, runner=ssh)
        self.assertEqual(ssh.calls, [])
        self.assertNotIn("sessionTitle", out)
        self.assertIn("Codex %s" % self.label("s-0001"), out["additionalContext"])

    def test_unreachable_registry_never_blocks_and_retries_later(self) -> None:
        down = FakeSsh(self, fail=subprocess.TimeoutExpired("ssh", 12))
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=down)
        self.assertIn("No Operator letter yet", out["additionalContext"])
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x"},
                                    runner=down))
        self.assertEqual(len(down.calls), 1)  # inside the retry window nothing is tried
        later = time.time() + PL.RETRY_SECONDS + 5
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build"},
                        now=later)
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % self.label("s-0001"))

    def test_a_registry_host_without_the_command_is_a_failed_registration(self) -> None:
        def missing(cmd, **kwargs):
            return SimpleNamespace(stdout="", returncode=127)

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        runner=missing)
        self.assertIn("exit 127 with no result", out["additionalContext"])

    def test_nothing_that_goes_wrong_blocks_a_chat(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        PL.register_spec(self.spec)
        self.addCleanup(PL.SPECS.pop, "codex", None)

        def boom(cmd, **kwargs):
            raise RuntimeError("anything at all")

        for stdin in ("not json", "[]", ""):
            code = PL.main(["hook", "--platform", "codex"], env=self.server_env(), stdin=io.StringIO(stdin),
                           stdout=out, stderr=err, runner=boom)
            self.assertEqual(code, 0, stdin)
        self.assertEqual(out.getvalue(), "")
        # A registry call that blows up is an error answer like any other: a line of context, exit 0.
        code = PL.main(["hook", "--platform", "codex"], env=self.server_env(), stderr=err, runner=boom, stdout=out,
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
        library entry calls it with what `hook` takes, minus the spec."""
        seen = {}

        def hook_main(env, stdin, stdout, **kwargs):
            seen.update(kwargs, stdin=stdin.read())
            stdout.write('{"own": true}\n')
            return 0

        module = types.ModuleType("agent_sync.platform_letter_owned")
        module.SPEC = make_spec(platform="owned", seat="CODEX", source="owned-hook")
        module.hook_main = hook_main
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"agent_sync.platform_letter_owned": module}):
            code = PL.main(["hook", "--platform", "owned", "--location", "server", "--remote", "mac"],
                           env=self.env(), stdin=io.StringIO("payload"), stdout=out, stderr=err)
        self.assertEqual((code, out.getvalue(), seen["stdin"]), (0, '{"own": true}\n', "payload"))
        self.assertEqual((seen["location_flag"], seen["remote_flag"]), ("server", "mac"))

    def test_a_session_id_that_is_not_one_is_ignored(self) -> None:
        ssh = FakeSsh(self)
        for sid in ("", "x", "../../etc/passwd", "a b c d", 5):
            self.hook({"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"}, runner=ssh)
        self.assertEqual(ssh.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "CODEX")))

    def test_out_of_scope_and_unknown_hosts(self) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}
        ssh = FakeSsh(self)
        # BotFleet and every other launcher: no hop, no note, no output.
        for extra in (
            {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-BUILDER"},
            {"AGENT_LAUNCHER": "agent-sync-wake"},
            {"AGENT_LAUNCH_SEAT": "BF-BUILDER"},
            {"CODEX_LETTER": "0"},
            {"SESSION_LETTER": "0"},
        ):
            with self.subTest(extra=extra):
                self.assertIsNone(self.hook(payload, env=self.server_env(**extra), runner=ssh))
        self.assertEqual(ssh.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "CODEX")))
        out = self.hook(payload, env=self.env(), runner=ssh)
        self.assertIn("No Operator letter", out["additionalContext"])
        self.assertFalse(os.path.exists(os.path.join(self.state, "CODEX")))
        self.assertIsNone(self.hook(dict(payload, hook_event_name="Stop"), runner=ssh))  # not an event of ours

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
        # The Claude platform's own spec cannot be built, so no adapter can write its rows from here.
        with self.assertRaises(ValueError):
            make_spec(platform="claude", seat=PL.from_env("AGENT_LAUNCH_SEAT"))

    def test_a_platform_that_runs_only_on_the_mac_takes_no_server_letter(self) -> None:
        spec = make_spec(platform="grok", seat="GROK", locations=("mac",))
        ssh = FakeSsh(self)
        self.assertIsNone(self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001"}, spec=spec, runner=ssh))
        self.assertEqual(ssh.calls, [])
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001"}, spec=spec, env=self.env(),
                        platform="darwin", runner=ssh)
        self.assertTrue(out["sessionTitle"].endswith(": New Chat"))
        for bad in ((), ("cloud",), ("mac", "moon")):
            with self.assertRaises(ValueError):
                make_spec(locations=bad)

    def test_headless_and_subagent_runs_take_no_letter(self) -> None:
        spec = make_spec(kind=lambda p, e: str(p.get("kind") or "chat"), parent_id=PL.from_payload("parent_id"))
        ssh = FakeSsh(self)
        for kind in ("headless", "subagent"):
            self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "kind": kind}, spec=spec, runner=ssh)
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
        chat = PL.Chat(self.spec, self.env(), "server", "s-0001", runner=None, now=0.0)
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
        PL.hook(make_spec(output="text"), self.server_env(), io.StringIO(json.dumps(payload)), out, runner=FakeSsh(self))
        text = out.getvalue()
        self.assertTrue(text.startswith("Operator session letter: Codex "))
        self.assertNotIn("{", text)
        out = io.StringIO()
        PL.hook(make_spec(output="json"), self.server_env(),
                io.StringIO(json.dumps(dict(payload, session_id="s-0002"))), out, runner=FakeSsh(self))
        body = json.loads(out.getvalue())["platform_letter"]
        self.assertEqual((body["event"], body["platform"]), ("start", "codex"))
        self.assertRegex(body["label"], PAIR)
        self.assertTrue(body["title"].endswith(": New Chat"))
        out = io.StringIO()
        PL.hook(make_spec(title_in_output=False), self.server_env(),
                io.StringIO(json.dumps(dict(payload, session_id="s-0003"))), out, runner=FakeSsh(self))
        self.assertNotIn("sessionTitle", out.getvalue())

    def test_a_secret_after_its_keyword_reaches_neither_the_title_nor_the_operator(self) -> None:
        value = "abcdefghijklmnop1234567"
        ssh = FakeSsh(self)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=ssh)
        label = self.label("s-0001")
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001",
                         "prompt": "api" + "_key: " + value + " fix login", "session_title": "%s: New Chat" % label},
                        runner=ssh)
        self.assertEqual(out["sessionTitle"], "%s: Api Key Fix Login" % label)
        self.assertEqual(self.lookup("s-0001")["title"], "Api Key Fix Login")
        with open(os.path.join(self.state, "CODEX", "letters", "s-0001.json")) as fh:
            self.assertNotIn(value, fh.read())
        self.assertNotIn(value, json.dumps(ssh.calls))

    def test_a_fork_does_not_keep_the_letter_of_the_chat_it_was_made_from(self) -> None:
        self.start("s-0001")
        source = self.label("s-0001")
        ssh = FakeSsh(self)
        payload = {"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "fork",
                   "session_title": "%s: Fix Login" % source}
        out = self.hook(payload, runner=ssh)
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
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "/review",
                         "session_title": "ZZ: Previous Chat"}, runner=ssh)
        self.assertIsNone(out)
        self.assertEqual(ssh.calls, [])
        self.assertFalse(self.note("s-0001")["titled"])

    def test_a_title_the_operator_did_not_get_is_sent_again(self) -> None:
        ssh = FlakySsh(self, ops=("title",))
        t0 = time.time()
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=ssh, now=t0)
        label = self.label("s-0001")
        title = "%s: Fix the Login Page" % label

        def prompt(text: str, shown: str, now: float) -> dict | None:
            return self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": text,
                              "session_title": shown}, runner=ssh, now=now)

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
        ssh = FlakySsh(self, ops=("touch",))
        t0 = time.time()
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=ssh, now=t0)
        title = "%s: Fix the Build" % self.label("s-0001")
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                   "session_title": title}, runner=ssh, now=t0)

        def prompt(now: float) -> None:
            self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "x",
                       "session_title": title}, runner=ssh, now=now)

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
        inner = FakeSsh(self)

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
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=ssh)
        label = self.lookup("s-0001")["label"]  # the registration was made...
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % label)  # ...and the chat was told and titled
        self.assertIn('"@**Codex** %s"' % label, out["additionalContext"])
        # With no note to go on, the next prompt registers again, which only resumes the chat.
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                         "session_title": "%s: New Chat" % label}, runner=ssh)
        self.assertEqual(out["sessionTitle"], "%s: Fix the Build" % label)
        self.assertEqual(self.lookup("s-0001")["label"], label)
        self.assertFalse(PL.Chat(self.spec, self.server_env(), "server", "s-0001", runner=ssh, now=0.0).write({}))

    def test_the_note_is_never_written_through_a_planted_symlink(self) -> None:
        victim = os.path.join(self.tmp.name, "victim")
        with open(victim, "w") as fh:
            fh.write("keep me")
        chat = PL.Chat(self.spec, self.server_env(), "server", "s-0001", runner=None, now=0.0)
        os.makedirs(os.path.dirname(chat.path), mode=0o700, exist_ok=True)
        # the name the writer used to take for its temporary file, which anyone who can write the folder could guess
        os.symlink(victim, "%s.%d.tmp" % (chat.path, os.getpid()))
        self.assertTrue(chat.write({"state": "open"}))
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertEqual(chat.read().get("state"), "open")

    def test_a_letter_that_arrives_on_a_later_prompt_is_announced(self) -> None:
        down = FakeSsh(self, fail=subprocess.TimeoutExpired("ssh", 12))
        t0 = time.time()
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        runner=down, now=t0)
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
                  runner=down, now=t0)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "go"}, spec=spec,
                        now=t0 + PL.RETRY_SECONDS + 5)
        self.assertNotIn("sessionTitle", out)
        self.assertIn('"@**Codex** %s"' % self.label("s-0002"), out["additionalContext"])
        # A later prompt says nothing more.
        self.assertIsNone(self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0002", "prompt": "x"},
                                    spec=spec, now=t0 + PL.RETRY_SECONDS + 9))

    def down_file(self, dest: str = "mac") -> str:
        return os.path.join(self.state, PL.HOST_DOWN_DIR, dest)

    def test_a_silent_registry_host_is_asked_once_for_all_the_chats(self) -> None:
        # A chat that starts many sub-threads in a row must not pay the ssh timeout for each of them.
        down = FakeSsh(self, fail=subprocess.TimeoutExpired("ssh", 12))
        t0 = time.time()
        for n in range(1, 5):
            out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-%04d" % n, "source": "startup"},
                            runner=down, now=t0 + n)
            self.assertIn("No Operator letter yet", out["additionalContext"])
            self.assertEqual(self.note("s-%04d" % n)["state"], "failed")
        self.assertEqual(len(down.calls), 1)
        self.assertGreater(PL.registry_down_until(self.server_env(), "mac"), t0 + 4)
        self.assertEqual(os.stat(self.down_file()).st_mode & 0o777, 0o600)
        # After the wait the host is asked again, by whichever chat comes first, and its answer clears the mark.
        later = t0 + PL.RETRY_SECONDS + 5
        ssh = FakeSsh(self)
        out = self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0003", "prompt": "fix the build"},
                        runner=ssh, now=later)
        self.assertEqual(ssh.ops()[0], "register")
        self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: Fix the Build$")
        self.assertFalse(os.path.exists(self.down_file()))

    def test_a_mark_holds_every_hop_of_a_chat_until_it_runs_out(self) -> None:
        self.start("s-0001")
        label = self.label("s-0001")
        title = "%s: New Chat" % label
        t0 = time.time()
        PL.mark_registry(self.server_env(), "mac", t0 + 100)
        ssh = FakeSsh(self)
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "fix the build",
                   "session_title": title}, runner=ssh, now=t0 + 10)
        self.assertEqual(ssh.calls, [])  # the title waits in the note, and the chat is still titled
        self.assertEqual(self.note("s-0001")["unsent"], "Fix the Build")
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-0001", "prompt": "more",
                   "session_title": "%s: Fix the Build" % label}, runner=ssh, now=t0 + PL.RETRY_SECONDS + 20)
        self.assertEqual(ssh.ops(), ["title"])
        self.assertFalse(os.path.exists(self.down_file()))

    def test_a_host_that_answers_with_an_error_is_not_a_silent_host(self) -> None:
        def refuses(cmd, **kwargs):
            refuses.calls += 1
            return SimpleNamespace(stdout=json.dumps({"error": "no Operator registry on this host"}), returncode=3)

        refuses.calls = 0
        for n in (1, 2):
            out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-%04d" % n, "source": "startup"},
                            runner=refuses)
            self.assertIn("no Operator registry", out["additionalContext"])
        self.assertEqual(refuses.calls, 2)  # it answered, so the next chat asks it too
        self.assertFalse(os.path.exists(self.down_file()))

    def test_a_host_with_no_command_is_silent_too(self) -> None:
        calls = []

        def missing(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(stdout="", returncode=127)

        for n in (1, 2, 3):
            self.hook({"hook_event_name": "SessionStart", "session_id": "s-%04d" % n, "source": "startup"},
                      runner=missing)
        self.assertEqual(len(calls), 1)

    def test_a_mark_further_off_than_any_wait_holds_nothing(self) -> None:
        t0 = time.time()
        PL.mark_registry(self.server_env(), "mac", t0 + 10 * PL.RETRY_SECONDS)  # the clock moved back since
        ssh = FakeSsh(self)
        self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, runner=ssh, now=t0)
        self.assertEqual(ssh.ops(), ["register"])
        self.assertFalse(os.path.exists(self.down_file()))  # and its answer cleared the stale mark

    def test_the_mark_is_per_registry_host_and_the_mac_has_none(self) -> None:
        PL.mark_registry(self.server_env(), "mac", time.time() + 600)
        ssh = FakeSsh(self)
        env = self.env(SESSION_LETTER_LOCATION="server", SESSION_LETTER_REMOTE="elsewhere")
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"}, env=env,
                        runner=lambda cmd, **kw: SimpleNamespace(stdout="", returncode=255))
        self.assertIn("exit 255 with no result", out["additionalContext"])  # another host: asked, and found silent
        self.assertTrue(os.path.exists(self.down_file("elsewhere")))
        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0002", "source": "startup"},
                        env=self.env(), runner=ssh, platform="darwin")
        self.assertRegex(out["sessionTitle"], r"^[A-Z]{2}: New Chat$")  # the Mac registers in process
        self.assertEqual(ssh.calls, [])

    def test_a_mark_that_cannot_be_read_or_written_is_no_mark_and_no_error(self) -> None:
        env = self.server_env()
        PL.mark_registry(env, "mac", time.time() + 600)
        for body in ("not json", '["until"]', '{"until": "soon"}', "{}"):
            Path(self.down_file()).write_text(body)
            self.assertEqual(PL.registry_down_until(env, "mac"), 0.0, body)
        self.assertEqual(PL.registry_down_until(env, "never-marked"), 0.0)
        if os.geteuid() == 0:
            return  # a folder cannot be made unwritable to root
        folder = os.path.dirname(self.down_file())
        os.chmod(folder, 0o500)
        self.addCleanup(os.chmod, folder, 0o700)
        PL.mark_registry(env, "mac", time.time() + 900)  # cannot be written: still no error
        PL.mark_registry(env, "mac")  # nor removed

    def test_the_down_mark_is_never_written_through_a_planted_symlink(self) -> None:
        env = self.server_env()
        victim = os.path.join(self.tmp.name, "victim")
        Path(victim).write_text("keep me")
        os.makedirs(os.path.dirname(self.down_file()), mode=0o700, exist_ok=True)
        os.symlink(victim, "%s.%d.tmp" % (self.down_file(), os.getpid()))  # the name the writer used to take
        until = time.time() + 600
        PL.mark_registry(env, "mac", until)
        self.assertEqual(Path(victim).read_text(), "keep me")
        self.assertAlmostEqual(PL.registry_down_until(env, "mac"), until, places=3)
        leftovers = [n for n in os.listdir(os.path.dirname(self.down_file())) if n.endswith(".tmp") and n != "mac"]
        self.assertEqual([n for n in leftovers if n.startswith(".down-")], [])  # no temporary file is left behind

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
        ssh = FakeSsh(self)
        self.child_start("c-0001", "p-0001", spec, runner=ssh)
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
        ssh = FakeSsh(self)
        self.child_start("c-0001", "p-0001", spec, runner=ssh)
        self.assertEqual(ssh.ops(), ["register"])  # only the child
        # A parent whose own registration was refused a moment ago (not a silent host: the registry answered) is not
        # waited for: the sub-thread would otherwise take a main pair ahead of it.
        t0 = time.time()

        def refuses(cmd, **kwargs):
            return SimpleNamespace(stdout=json.dumps({"error": "no Operator registry on this host"}), returncode=3)

        self.hook({"hook_event_name": "SessionStart", "session_id": "q-0001", "source": "startup"}, spec=spec,
                  runner=refuses, now=t0)
        self.assertEqual(self.note("q-0001")["state"], "failed")
        self.child_start("q-child", "q-0001", spec, now=t0 + 5)
        self.assertEqual(self.note("q-0001")["state"], "open")
        self.assertLess(self.label("q-0001"), self.label("q-child"))

    def test_grandparents_are_registered_first_and_a_loop_ends(self) -> None:
        spec = self.parent_spec()
        ssh = FakeSsh(self)
        self.child_start("c-0002", "c-0001", spec, runner=ssh)  # c-0001 is itself a child of p-0001
        self.assertEqual(ssh.ops(), ["register", "register", "register"])
        labels = [self.label(sid) for sid in ("p-0001", "c-0001", "c-0002")]
        self.assertEqual(labels, sorted(labels))
        self.assertEqual(len(set(labels)), 3)
        ssh = FakeSsh(self)
        self.child_start("loop-c", "loop-a", spec, runner=ssh)  # loop-a and loop-b each name the other
        self.assertLessEqual(len(ssh.calls), PL.PARENT_DEPTH + 1)
        self.assertRegex(self.label("loop-c"), PAIR)

    def test_a_spec_that_cannot_look_a_parent_up_leaves_it_alone(self) -> None:
        spec = self.parent_spec()
        spec = make_spec(parent_id=spec.parent_id, kind=spec.kind, title_in_output=True)
        ssh = FakeSsh(self)
        self.child_start("c-0001", "p-0001", spec, runner=ssh)
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
        PL.Chat(spec, self.server_env(), "server", "m-0001", runner=FakeSsh(self), now=0.0).write(
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

    def test_end_to_end_through_the_entry_script(self) -> None:
        """The hook on the server, the real `platform-letter registry` on the "Mac" (a subprocess of the entry script
        against the temporary registry): what the ssh hop will run, minus ssh."""

        def run_remote(cmd, **kwargs):
            remote = shlex.split(cmd[8])
            self.assertEqual(remote[0], "$HOME/.local/bin/platform-letter")
            proc = subprocess.run(
                [sys.executable, str(ENTRY), *remote[1:]], capture_output=True, text=True, timeout=60,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.home,
                     "AGENT_SYNC_STATE_DIR": self.state},
            )
            return SimpleNamespace(stdout=proc.stdout, returncode=proc.returncode)

        out = self.hook({"hook_event_name": "SessionStart", "session_id": "s-0001", "source": "startup"},
                        runner=run_remote)
        label = self.label("s-0001")
        self.assertEqual(out["sessionTitle"], "%s: New Chat" % label)
        self.assertEqual(self.lookup("s-0001")["label"], label)
        self.end("s-0001", "archived", runner=run_remote)
        self.assertEqual(self.lookup("s-0001")["state"], "closed")


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
        code, out = self.run_install(location="server", remote="mac")
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
            "%s hook --platform codex --location server --remote mac"
            % os.path.join(self.home, ".local", "bin", "platform-letter"),
        )
        self.assertEqual(entry["timeout"], 15)
        again = self.run_install(location="server", remote="mac")[1]
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
        for opts in ({"remote": "-oProxyCommand=x"}, {"remote": "a b"}, {"location": "cloud"}):
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
        self.assertEqual(self.run_install(location="server", remote="mac")[0], 0)
        entries = self.load()["hooks"]["SessionStart"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["matcher"], "startup|resume")  # the owner's matcher stays
        self.assertEqual(entries[0]["hooks"][0], foreign)  # and so does the other tool's hook, first
        self.assertEqual(entries[0]["hooks"][1]["timeout"], 20)  # ours is replaced where it stood
        self.assertIn("--location server --remote mac", entries[0]["hooks"][1]["command"])
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
            third = self.run_install(location="server", remote="mac")[1]["backup"]
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
            PL.command_for("/my home", "codex", "server", "mac"),
            "'/my home/.local/bin/platform-letter' hook --platform codex --location server --remote mac",
        )

    def test_install_main_finds_the_adapter(self) -> None:
        module = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"), INSTALL=self.installer())
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"agent_sync.platform_letter_faux": module}):
            code = PL.install_main(["--platform", "faux", "--location", "server", "--remote", "mac"],
                                   {"HOME": self.home}, out)
        self.assertEqual(code, 0)
        self.assertIn("--platform faux --location server --remote mac", json.dumps(self.load()))
        # An adapter with an installer of its own is called with the parsed options.
        seen = []
        own = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"),
                                    install=lambda opts, env, stdout: seen.append(opts) or 7)
        with mock.patch.dict(sys.modules, {"agent_sync.platform_letter_faux": own}):
            code = PL.install_main(["--platform", "faux", "--check"], {"HOME": self.home}, io.StringIO())
        self.assertEqual((code, seen[0].check, seen[0].platform), (7, True, "faux"))
        bare = types.SimpleNamespace(SPEC=make_spec(platform="faux", seat="FAUX"))
        with mock.patch.dict(sys.modules, {"agent_sync.platform_letter_faux": bare}):
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

    def test_the_registry_side_refuses_a_host_with_no_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(ENTRY), "registry", "touch", "--platform", "codex",
                 "--location", "server", "--session-id", "s-0001"],
                capture_output=True, text=True, timeout=60,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tmp, "AGENT_SYNC_STATE_DIR": tmp},
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("no Operator registry", result.stdout)
            self.assertEqual(os.listdir(tmp), [])


if __name__ == "__main__":
    unittest.main()
