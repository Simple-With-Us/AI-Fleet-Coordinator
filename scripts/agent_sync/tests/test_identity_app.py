"""strict_seat() and seat_app() (open-loops unit F2, 2026-10-11).

canonical_seat() is the lenient input boundary (#457) and stays exactly as it was:  it upper-cases anything.
strict_seat() runs it first and then refuses a name the registry does not know;  seat_app() says which app a
seat runs in, from the top-level `seatApps` key of fleet-apps.json (kept out of `seats`, which a dozen tools
read).  These tests pin both functions, the registry data they read, the check that guards the data, the
onboarding script that writes it, and the board script's own alias tables (it imports no package, so it keeps
its own copy and these tests keep it resolvable)."""
from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_sync import identity as I

REPO = Path(__file__).resolve().parents[3]
REGISTRY_PATH = REPO / "fleet-apps.json"
BOARD = REPO / "scripts" / "linear-board" / "board"
ONBOARD = REPO / "scripts" / "onboard-new-agent.sh"
HOST = "simplewithus.zulipchat.com"

# Every spelling the owner's GROK-BUILD rule accepts (the same list test_seat_alias.py holds).
GROK_BUILD_SPELLINGS = ("GROK-BUILD", "Grok-Build", "grok-build", "grok_build", "Grok Build", "GROK.BUILD",
                        "Grok (Build)", "grok/build/", "GrokBuild", "GROK--BUILD!!", "  grok build  ")


def registry_data() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def load_board():
    loader = importlib.machinery.SourceFileLoader("board_cli_under_test_f2", str(BOARD))
    spec = importlib.util.spec_from_loader("board_cli_under_test_f2", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def load_check():
    spec = importlib.util.spec_from_file_location("check_fleet_registry_f2", REPO / "scripts" / "check-fleet-registry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RealRegistryCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reg = I.load_seat_registry(str(REGISTRY_PATH))


class StrictSeatTests(RealRegistryCase):
    def test_every_grok_build_spelling_still_resolves_to_grok_through_both(self) -> None:
        for raw in GROK_BUILD_SPELLINGS:
            with self.subTest(raw=raw):
                self.assertEqual(I.canonical_seat(raw), "GROK")
                self.assertEqual(I.strict_seat(raw, registry=self.reg), "GROK")
                self.assertEqual(I.seat_app(raw, registry=self.reg), "grok-terminal")

    def test_minimax_raises_because_the_tag_is_mm(self) -> None:
        # identity has no MINIMAX alias (test_seat_alias.py keeps MINIMAX out of the alias pattern;  the board
        # script maps it to MM on its own).
        with self.assertRaises(I.UnknownSeat) as cm:
            I.strict_seat("MINIMAX", registry=self.reg)
        self.assertEqual((cm.exception.seat, cm.exception.retired), ("MINIMAX", False))
        self.assertEqual(I.canonical_seat("MINIMAX"), "MINIMAX")
        self.assertEqual(I.strict_seat("mm", registry=self.reg), "MM")

    def test_canonical_seat_is_still_lenient_and_never_reads_the_registry(self) -> None:
        old = os.environ.get(I.ENV_APPS_JSON)
        os.environ[I.ENV_APPS_JSON] = "/nonexistent/fleet-apps.json"
        try:
            for raw, want in (("nope", "NOPE"), ("  mm ", "MM"), ("Totally Unknown!", "TOTALLY UNKNOWN!"), ("", ""),
                              (None, ""), ("minimax", "MINIMAX"), ("monet", "MONET")):
                with self.subTest(raw=raw):
                    self.assertEqual(I.canonical_seat(raw), want)
        finally:
            if old is None:
                del os.environ[I.ENV_APPS_JSON]
            else:
                os.environ[I.ENV_APPS_JSON] = old

    def test_unknown_names_raise_in_both_functions(self) -> None:
        for raw in ("NOPE", "", None, "  ", "claude code", "../x", "BF-", "BF-*", "BF-A B", "XBF-PLUMBER", "GB",
                    "BF", "BF--", "GROKBUILDER", "grok-build-bot", "A" * 40):
            with self.subTest(raw=raw):
                with self.assertRaises(I.UnknownSeat):
                    I.strict_seat(raw, registry=self.reg)
                with self.assertRaises(I.UnknownSeat):
                    I.seat_app(raw, registry=self.reg)

    def test_a_retired_seat_is_unknown_but_says_it_is_retired(self) -> None:
        for raw in ("MONET", "RENOIR", "HARNESS", "DSH", "KIMI"):
            with self.subTest(raw=raw):
                with self.assertRaises(I.UnknownSeat) as cm:
                    I.strict_seat(raw, registry=self.reg)
                self.assertTrue(cm.exception.retired)
                self.assertIn("retired", str(cm.exception))

    def test_every_live_seat_row_is_known_with_an_app(self) -> None:
        live = [s["tag"] for s in registry_data()["seats"] if not s.get("retired")]
        self.assertTrue(live)
        for tag in live:
            with self.subTest(tag=tag):
                self.assertEqual(I.strict_seat(tag.lower(), registry=self.reg), tag)
                self.assertTrue(I.seat_app(tag, registry=self.reg))

    def test_the_message_is_short_and_names_no_more_than_the_seat(self) -> None:
        with self.assertRaises(I.UnknownSeat) as cm:
            I.strict_seat("x" * 500 + "\n$(rm -rf /)", registry=self.reg)
        self.assertLess(len(str(cm.exception)), 200)


class SeatAppTests(RealRegistryCase):
    def app(self, raw: str) -> str:
        return I.seat_app(raw, registry=self.reg)

    def test_botfleet_and_grok_bot_personas_come_from_the_patterns(self) -> None:
        self.assertEqual(self.app("BF-PLUMBER"), "botfleet")
        self.assertEqual(self.app("bf-director"), "botfleet")
        self.assertEqual(self.app("GB-PLUMBER"), "grok-bot")
        self.assertEqual(self.app("GB-COMPILER"), "grok-bot")
        self.assertEqual(self.app("GROK-BOT"), "grok-bot", "the fleet-wide row is the same app as its personas")

    def test_the_extra_seat_has_no_row_in_seats_and_still_has_an_app(self) -> None:
        self.assertEqual(self.app("JET"), "chatgpt")
        rows = {s["tag"] for s in registry_data()["seats"]}
        self.assertNotIn("JET", rows)

    def test_echo_and_instinct_are_regular_seats_with_rows_and_apps(self) -> None:
        # ECHO and WISE moved from extraSeats to the seats table (owner decision Sun, Oct 11, 2026).
        self.assertEqual(self.app("ECHO"), "instinct")
        self.assertEqual(self.app("WISE"), "instinct")
        rows = {s["tag"] for s in registry_data()["seats"]}
        self.assertIn("ECHO", rows)
        self.assertIn("WISE", rows)

    def test_grok_and_grok_web_are_two_apps_and_echo_with_instinct_is_one(self) -> None:
        self.assertNotEqual(self.app("GROK"), self.app("GROK-WEB"))
        self.assertEqual((self.app("GROK"), self.app("GROK-WEB")), ("grok-terminal", "grok-web"))
        self.assertEqual(self.app("ECHO"), self.app("WISE"))
        self.assertNotIn(self.app("GROK-WEB"), {self.app("GROK-BOT"), self.app("CURSOR")})

    def test_the_plan_table_is_what_the_registry_says(self) -> None:
        want = {"CLAUDE": "claude-code", "CODEX": "codex", "CURSOR": "cursor", "GROK": "grok-terminal",
                "GROK-WEB": "grok-web", "AG": "antigravity", "MM": "minimax-code", "MC": "muse-code",
                "MA": "muse-assist", "CLUTCH": "clutch", "FX": "fx", "OPENCODE": "opencode", "GROK-BOT": "grok-bot",
                "ECHO": "instinct", "WISE": "instinct"}
        self.assertEqual(dict(self.reg.by_seat), want)
        self.assertEqual(dict(self.reg.extra), {"AMP": "amp", "JET": "chatgpt", "JET-CHAT": "chatgpt", "OPERATOR": "operator"})

    def test_an_unknown_seat_is_never_a_new_app(self) -> None:
        for raw in ("MINIMAX", "MONET", "ACME", "BF-"):
            with self.subTest(raw=raw):
                with self.assertRaises(I.UnknownSeat):
                    self.app(raw)

    def test_the_pattern_emails_agree_with_how_a_bot_signs(self) -> None:
        # seat_tag_for() turns a bot email into the tag it signs;  the pattern's zulipEmail must be the
        # inverse, or a message from bf-<role>-bot@ would be a seat the registry does not know.
        by_app = {p["app"]: p["zulipEmail"] for p in registry_data()["seatApps"]["patterns"]}
        for role in ("plumber", "director", "housekeeper"):
            for app, template, prefix in (("botfleet", by_app["botfleet"], "BF-"), ("grok-bot", by_app["grok-bot"], "GB-")):
                with self.subTest(role=role, app=app):
                    email = template.replace("<role>", role)
                    self.assertTrue(email.endswith("-bot@" + HOST), email)
                    tag = I.seat_tag_for({"is_bot": True, "email": email, "full_name": "x"})
                    self.assertEqual(tag, prefix + role.upper())
                    self.assertEqual(I.strict_seat(tag, registry=self.reg), tag)
                    self.assertEqual(self.app(tag), app)

    def test_the_hosted_seat_signs_the_tag_the_registry_lists(self) -> None:
        # ECHO and WISE were hosted seats; they moved to the seats table (owner decision Sun, Oct 11, 2026).
        for local, tag, app in (("openai-dot", "JET", "chatgpt"), ("muse-assist", "MA", "muse-assist"),
                                ("grok-build", "GROK", "grok-terminal"), ("mm", "MM", "minimax-code")):
            with self.subTest(tag=tag):
                got = I.seat_tag_for({"is_bot": True, "email": "%s-bot@%s" % (local, HOST), "full_name": "x"})
                self.assertEqual(got, tag)
                self.assertEqual(self.app(got), app)

    def test_echo_and_instinct_bot_emails_still_map_to_their_tags(self) -> None:
        # Their bot emails keep working after the move from extraSeats to the seats table.
        for local, tag in (("instinct-bat", "ECHO"), ("instinct-owl", "WISE")):
            with self.subTest(tag=tag):
                got = I.seat_tag_for({"is_bot": True, "email": "%s-bot@%s" % (local, HOST), "full_name": "x"})
                self.assertEqual(got, tag)
                self.assertEqual(self.app(got), "instinct")


class RegistryDataTests(unittest.TestCase):
    """What F2 did to fleet-apps.json, and what it must not have done."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = registry_data()
        cls.seats = {s["tag"]: s for s in cls.data["seats"]}

    def test_no_bf_gb_or_hosted_seat_has_a_row_in_seats(self) -> None:
        for tag in self.seats:
            self.assertFalse(tag.startswith(("BF-", "GB-")), tag)
        # ECHO and WISE moved to the seats table (owner decision Sun, Oct 11, 2026); JET stays hosted.
        self.assertNotIn("JET", self.seats)
        self.assertIn("ECHO", self.seats)
        self.assertIn("WISE", self.seats)

    def test_no_seats_row_carries_an_app_field(self) -> None:
        # `app` would collide with the top-level `apps` (ST, CT, BF) and `--app`.
        for tag, row in self.seats.items():
            self.assertNotIn("app", row, tag)
            self.assertNotIn("seatApp", row, tag)

    def test_grok_bot_has_no_branch_prefix_and_no_live_seat_shares_one(self) -> None:
        self.assertEqual(self.seats["GROK-BOT"]["branchPrefixes"], [])
        self.assertEqual(self.seats["CURSOR"]["branchPrefixes"], ["cursor/"])
        seen: dict[str, str] = {}
        for tag, row in self.seats.items():
            if row.get("retired"):
                continue
            for prefix in row.get("branchPrefixes", []):
                # ECHO and WISE both claim instinct/ (owner decision Sun, Oct 11, 2026).
                if prefix == "instinct/" and tag in ("ECHO", "WISE") and seen.get(prefix) in ("ECHO", "WISE"):
                    continue
                self.assertNotIn(prefix, seen, "%s and %s both claim branch prefix %s" % (tag, seen.get(prefix), prefix))
                seen[prefix] = tag

    def test_the_seats_the_lane_tool_reads_are_otherwise_unchanged(self) -> None:
        # The GROK-BOT row keeps the cursor folder name:  `lane` still refuses it, and gb and grok-bot still
        # read as the cursor folder (layout.SEAT_ALIASES).
        self.assertEqual(self.seats["GROK-BOT"]["worktreeSuffix"], "cursor")
        self.assertEqual(self.seats["GROK-BOT"]["notesName"], "Grok Bot")
        self.assertEqual(self.seats["GROK"]["aliasPattern"], I.GROK_ALIAS_PATTERN)

    def test_the_key_is_top_level_beside_seats_and_has_the_three_parts(self) -> None:
        apps = self.data["seatApps"]
        self.assertEqual(set(apps) - {"notes"}, {"bySeat", "extraSeats", "patterns"})
        self.assertEqual([(p["match"], p["app"]) for p in apps["patterns"]], [("BF-*", "botfleet"), ("GB-*", "grok-bot")])

    def test_the_realm_host_in_the_pattern_emails_is_the_registry_realm(self) -> None:
        for pattern in self.data["seatApps"]["patterns"]:
            self.assertTrue(pattern["zulipEmail"].endswith("@" + HOST), pattern)
        self.assertEqual(self.data["zulipRealm"], "https://" + HOST)


class BoardAliasTests(RealRegistryCase):
    """The board script imports no package, so it keeps its own SEAT_ALIASES, SEAT_TAG_ALIASES (INSTINCT to
    WISE, owner 2026-10-15) and BF_ROLES.  Nothing merges them into the registry here;  this keeps every
    target resolvable through strict_seat so the two cannot drift apart unnoticed."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.board = load_board()

    def test_every_seat_alias_target_resolves(self) -> None:
        self.assertIn("MINIMAX", self.board.SEAT_ALIASES, "the board maps MINIMAX itself:  identity does not")
        for alias, target in self.board.SEAT_ALIASES.items():
            with self.subTest(alias=alias, target=target):
                self.assertEqual(I.strict_seat(target, registry=self.reg), target)
                self.assertTrue(I.seat_app(target, registry=self.reg))

    def test_every_seat_tag_alias_target_resolves(self) -> None:
        self.assertEqual(self.board.SEAT_TAG_ALIASES, {"INSTINCT": "WISE"})
        for alias, target in self.board.SEAT_TAG_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(I.strict_seat(target, registry=self.reg), target)
                self.assertEqual(I.seat_app(target, registry=self.reg), "instinct")
                self.assertEqual(I.strict_seat(alias, registry=self.reg), target)  # INSTINCT stays an accepted spelling (fleet-apps.json)

    def test_every_bf_role_gives_a_botfleet_seat(self) -> None:
        self.assertTrue(self.board.BF_ROLES)
        for role in sorted(self.board.BF_ROLES):
            with self.subTest(role=role):
                self.assertEqual(I.seat_app("BF-" + role, registry=self.reg), "botfleet")

    def test_every_board_roster_seat_and_clutch_lineage_target_resolves(self) -> None:
        for seat in sorted(self.board.SEAT_ROSTER):
            with self.subTest(seat=seat):
                self.assertEqual(I.strict_seat(seat, registry=self.reg), seat)
                self.assertTrue(I.seat_app(seat, registry=self.reg))
        self.assertEqual(I.strict_seat("CLUTCH", registry=self.reg), "CLUTCH")
        for retired in sorted(self.board.CLUTCH_LINEAGE):
            with self.subTest(retired=retired):
                with self.assertRaises(I.UnknownSeat):
                    I.strict_seat(retired, registry=self.reg)  # the board folds these into CLUTCH:  identity does not


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="seat-apps-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "fleet-apps.json"

    def write(self, data) -> str:
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return str(self.path)

    def small(self) -> dict:
        return {"seats": [{"tag": "AAA"}, {"tag": "OLD", "retired": True}],
                "seatApps": {"bySeat": {"AAA": "aaa-app"}, "extraSeats": {"HOSTED": "hosted"},
                             "patterns": [{"match": "ZZ-*", "app": "zz"}]}}

    def test_a_small_registry_answers_through_the_file(self) -> None:
        path = self.write(self.small())
        reg = I.load_seat_registry(path)
        self.assertEqual(I.seat_app("aaa", registry=reg), "aaa-app")
        self.assertEqual(I.seat_app("hosted", registry=reg), "hosted")
        self.assertEqual(I.seat_app("zz-one", registry=reg), "zz")
        with self.assertRaises(I.UnknownSeat) as cm:
            I.strict_seat("old", registry=reg)
        self.assertTrue(cm.exception.retired)

    def test_the_environment_variable_moves_the_registry_and_is_read_per_call(self) -> None:
        path = self.write(self.small())
        old = os.environ.get(I.ENV_APPS_JSON)
        os.environ[I.ENV_APPS_JSON] = path
        try:
            self.assertEqual(I.seat_registry_path(), path)
            self.assertEqual(I.seat_app("AAA"), "aaa-app")
            self.assertEqual(I.strict_seat("zz-two"), "ZZ-TWO")
        finally:
            if old is None:
                del os.environ[I.ENV_APPS_JSON]
            else:
                os.environ[I.ENV_APPS_JSON] = old

    def test_the_default_path_is_the_repo_registry(self) -> None:
        self.assertEqual(I.seat_registry_path({}), str(REGISTRY_PATH))

    def test_a_changed_file_is_read_again(self) -> None:
        path = self.write(self.small())
        self.assertEqual(I.seat_app("AAA", registry=I.load_seat_registry(path)), "aaa-app")
        data = self.small()
        data["seatApps"]["bySeat"]["AAA"] = "renamed-app-with-longer-name"
        self.write(data)
        self.assertEqual(I.seat_app("AAA", registry=I.load_seat_registry(path)), "renamed-app-with-longer-name")

    def test_an_unreadable_registry_is_not_an_unknown_seat(self) -> None:
        # A caller that turns an unknown claimant into an orphan must never turn a missing file into orphans.
        for setup in (lambda: str(self.tmp / "missing.json"),
                      lambda: (self.path.write_text("{not json", encoding="utf-8"), str(self.path))[1]):
            with self.subTest():
                path = setup()
                with self.assertRaises(I.SeatRegistryError):
                    I.load_seat_registry(path)
        self.assertFalse(issubclass(I.SeatRegistryError, I.UnknownSeat))
        self.assertFalse(issubclass(I.UnknownSeat, I.SeatRegistryError))

    def test_a_malformed_seat_apps_key_is_a_registry_error(self) -> None:
        def broken(mutate):
            data = self.small()
            mutate(data["seatApps"])
            return data
        cases = {
            "no key": {"seats": []},
            "not an object": {"seats": [], "seatApps": []},
            "bySeat missing": broken(lambda a: a.pop("bySeat")),
            "extraSeats a list": broken(lambda a: a.__setitem__("extraSeats", [])),
            "patterns a dict": broken(lambda a: a.__setitem__("patterns", {})),
            "app with a space": broken(lambda a: a["bySeat"].__setitem__("AAA", "Claude Code")),
            "app empty": broken(lambda a: a["bySeat"].__setitem__("AAA", "")),
            "app not a string": broken(lambda a: a["bySeat"].__setitem__("AAA", 7)),
            "pattern without app": broken(lambda a: a["patterns"].append({"match": "QQ-*"})),
            "pattern with a regex": broken(lambda a: a["patterns"].append({"match": "QQ.*", "app": "qq"})),
            "pattern lower case": broken(lambda a: a["patterns"].append({"match": "qq-*", "app": "qq"})),
            "pattern not an object": broken(lambda a: a["patterns"].append("QQ-*")),
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(I.SeatRegistryError):
                    I.parse_seat_registry(data)
        with self.assertRaises(I.SeatRegistryError):
            I.parse_seat_registry([])

    def test_a_live_seat_with_no_app_is_known_but_a_registry_error_for_seat_app(self) -> None:
        data = self.small()
        data["seats"].append({"tag": "NEWBIE"})
        reg = I.parse_seat_registry(data)
        self.assertEqual(I.strict_seat("newbie", registry=reg), "NEWBIE")
        with self.assertRaises(I.SeatRegistryError):
            I.seat_app("newbie", registry=reg)

    def test_the_first_matching_pattern_wins_and_a_star_needs_a_tail(self) -> None:
        data = self.small()
        data["seatApps"]["patterns"] = [{"match": "ZZ-*", "app": "first"}, {"match": "ZZ-A*", "app": "second"}]
        reg = I.parse_seat_registry(data)
        self.assertEqual(I.seat_app("ZZ-ALPHA", registry=reg), "first")
        with self.assertRaises(I.UnknownSeat):
            I.strict_seat("ZZ-", registry=reg)
        with self.assertRaises(I.UnknownSeat):
            I.strict_seat("ZZ", registry=reg)


class CheckTests(unittest.TestCase):
    """scripts/check-fleet-registry.py:  every live seat needs a bySeat app, and the key must be well formed."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.check = load_check()
        cls.data = registry_data()

    def errors(self, mutate=None) -> list[str]:
        data = copy.deepcopy(self.data)
        if mutate:
            mutate(data)
        return self.check.seat_apps_errors(data)

    def test_the_committed_registry_is_clean(self) -> None:
        self.assertEqual(self.errors(), [])

    def test_a_live_seat_without_an_app_is_reported(self) -> None:
        errors = self.errors(lambda d: d["seatApps"]["bySeat"].pop("CODEX"))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("live seat CODEX has no app in seatApps.bySeat", errors[0])

    def test_a_new_live_seat_row_without_an_app_is_reported(self) -> None:
        errors = self.errors(lambda d: d["seats"].append({"tag": "NEWSEAT", "worktreeSuffix": "newseat"}))
        self.assertTrue(any("NEWSEAT has no app" in e for e in errors), errors)

    def test_a_retired_seat_needs_no_app_and_may_not_have_one(self) -> None:
        self.assertEqual(self.errors(), [], "MONET and the other retired rows have no bySeat entry")
        errors = self.errors(lambda d: d["seatApps"]["bySeat"].__setitem__("MONET", "claude-code"))
        self.assertTrue(any("MONET" in e and "is retired" in e for e in errors), errors)

    def test_a_row_tag_is_read_upper_case_as_identity_reads_it(self) -> None:
        def lower_case_the_codex_row(d) -> None:
            next(r for r in d["seats"] if r["tag"] == "CODEX")["tag"] = " codex "
        self.assertEqual(self.errors(lower_case_the_codex_row), [], "no false 'has no app' or 'not a row' report")

        def lower_case_a_retired_row(d) -> None:
            next(r for r in d["seats"] if r["tag"] == "MONET")["tag"] = "monet"
        self.assertEqual(self.errors(lower_case_a_retired_row), [])
        errors = self.errors(lambda d: (lower_case_a_retired_row(d), d["seatApps"]["bySeat"].__setitem__("MONET", "x")))
        self.assertTrue(any("MONET" in e and "is retired" in e for e in errors), errors)

    def test_a_by_seat_entry_for_no_row_is_reported(self) -> None:
        errors = self.errors(lambda d: d["seatApps"]["bySeat"].__setitem__("GHOST", "ghost"))
        self.assertTrue(any("GHOST" in e and "not a row of seats" in e for e in errors), errors)

    def test_an_extra_seat_that_has_a_row_is_reported(self) -> None:
        errors = self.errors(lambda d: d["seatApps"]["extraSeats"].__setitem__("CODEX", "codex"))
        self.assertTrue(any("extraSeats lists CODEX" in e for e in errors), errors)

    def test_a_seats_row_that_a_pattern_also_matches_is_reported(self) -> None:
        errors = self.errors(lambda d: d["seats"].append({"tag": "BF-WIDGET", "worktreeSuffix": "bf-widget"}))
        self.assertTrue(any("BF-WIDGET" in e and "BF-*" in e for e in errors), errors)

    def test_a_lower_case_key_is_reported(self) -> None:
        errors = self.errors(lambda d: d["seatApps"]["extraSeats"].__setitem__("jet2", "chatgpt"))
        self.assertTrue(any("jet2" in e and "upper case" in e for e in errors), errors)

    def test_a_malformed_key_is_one_clear_error(self) -> None:
        def drop(d):
            del d["seatApps"]
        errors = self.errors(drop)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("seatApps:"), errors)
        errors = self.errors(lambda d: d["seatApps"]["bySeat"].__setitem__("CODEX", "Open AI"))
        self.assertEqual(len(errors), 1, errors)

    def test_a_pattern_email_off_the_realm_or_without_a_role_is_reported(self) -> None:
        for bad in ("bf-plumber-bot@" + HOST, "bf-<role>-bot@example.com", "<role>-<role>-bot@" + HOST, "bf-<role>@" + HOST):
            with self.subTest(bad=bad):
                errors = self.errors(lambda d: d["seatApps"]["patterns"][0].__setitem__("zulipEmail", bad))
                self.assertTrue(any("zulipEmail" in e for e in errors), errors)

    def test_main_runs_the_check(self) -> None:
        src = (REPO / "scripts" / "check-fleet-registry.py").read_text(encoding="utf-8")
        self.assertIn("errors.extend(seat_apps_errors(data))", src)


class OnboardScriptTests(unittest.TestCase):
    """onboard-new-agent.sh writes the seat's row and its seatApps.bySeat app, never guessing the app.  It runs
    in a temp tree (the script finds fleet-apps.json beside its own scripts/ folder) with stdin closed, so it can
    never wait at a prompt."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="onboard-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "scripts").mkdir()
        shutil.copy(ONBOARD, self.tmp / "scripts" / "onboard-new-agent.sh")
        os.symlink(REPO / "scripts" / "agent_sync", self.tmp / "scripts" / "agent_sync")
        self.registry = self.tmp / "fleet-apps.json"
        shutil.copy(REGISTRY_PATH, self.registry)

    def run_script(self, *args: str, shell: str = "bash") -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("AGENT_SEAT", "AGENT_LAUNCH_SEAT", "AGENT_LAUNCHER")}
        return subprocess.run([shell, str(self.tmp / "scripts" / "onboard-new-agent.sh"), *args],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=60)

    def before(self) -> str:
        return self.registry.read_text(encoding="utf-8")

    def test_without_an_app_a_new_seat_is_refused_and_the_apps_in_use_are_listed(self) -> None:
        was = self.before()
        got = self.run_script("--tag", "NEWSEAT")
        self.assertEqual(got.returncode, 1, got.stderr)
        self.assertIn("required: --app", got.stderr)
        for app in ("claude-code", "botfleet", "chatgpt"):
            self.assertIn(app, got.stderr)
        self.assertEqual(self.before(), was)

    def test_a_bad_app_name_is_refused(self) -> None:
        was = self.before()
        for bad in ("Claude Code", "NEW", "a_b", "-x", "x-", "a--b"):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_script("--tag", "NEWSEAT", "--app", bad).returncode, 1)
        self.assertEqual(self.before(), was)

    def test_a_dry_run_needs_no_app_and_changes_nothing(self) -> None:
        was = self.before()
        got = self.run_script("--tag", "NEWSEAT", "--dry-run")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("app=(not set)", got.stdout)
        self.assertEqual(self.before(), was)

    def test_a_new_seat_gets_a_row_and_an_app_and_the_check_passes(self) -> None:
        got = self.run_script("--tag", "NEWSEAT", "--notes-name", "Newseat", "--app", "newseat-code")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("set seatApps.bySeat[NEWSEAT] = newseat-code", got.stdout)
        data = json.loads(self.before())
        self.assertEqual(data["seatApps"]["bySeat"]["NEWSEAT"], "newseat-code")
        self.assertEqual([s["tag"] for s in data["seats"]][-1], "NEWSEAT")
        self.assertEqual(load_check().seat_apps_errors(data), [])
        reg = I.parse_seat_registry(data)
        self.assertEqual(I.seat_app("newseat", registry=reg), "newseat-code")

    def test_the_file_keeps_its_formatting(self) -> None:
        self.run_script("--tag", "NEWSEAT", "--app", "newseat-code")
        data = json.loads(self.before())
        self.assertEqual(self.before(), json.dumps(data, indent=2) + "\n")

    def test_a_seat_that_already_has_both_needs_no_app_and_is_left_alone(self) -> None:
        was = self.before()
        got = self.run_script("--tag", "CODEX")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("already has seat CODEX", got.stdout)
        self.assertIn("already gives CODEX the app codex", got.stdout)
        self.assertEqual(self.before(), was)

    def test_a_retired_seat_gets_no_app_even_when_one_is_passed(self) -> None:
        was = self.before()
        for extra in ((), ("--app", "kimi-code")):
            with self.subTest(extra=extra):
                got = self.run_script("--tag", "KIMI", *extra)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertIn("seat KIMI is retired", got.stdout)
                self.assertEqual(self.before(), was)
        self.assertEqual(load_check().seat_apps_errors(json.loads(self.before())), [])

    def test_the_tag_is_upper_cased_so_a_lower_case_tag_never_writes_a_duplicate_key(self) -> None:
        was = self.before()
        got = self.run_script("--tag", "codex")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("already gives CODEX the app codex", got.stdout)
        self.assertEqual(self.before(), was)
        got = self.run_script("--tag", "newseat", "--app", "newseat-code")
        self.assertEqual(got.returncode, 0, got.stderr)
        by_seat = json.loads(self.before())["seatApps"]["bySeat"]
        self.assertEqual(by_seat["NEWSEAT"], "newseat-code")
        self.assertNotIn("newseat", by_seat)

    def test_the_script_runs_under_the_system_bash(self) -> None:
        """macOS /bin/bash is 3.2, which has no ${VAR^^}.  The script is started by `#!/usr/bin/env bash`, so a
        PATH without Homebrew's bash must still work.  On Linux /bin/bash is a modern bash and this still holds."""
        if not os.path.exists("/bin/bash"):
            self.skipTest("no /bin/bash")
        got = self.run_script("--tag", "codex", shell="/bin/bash")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertNotIn("bad substitution", got.stderr)
        self.assertIn("already gives CODEX the app codex", got.stdout)

    def test_the_ignored_apps_flag_takes_a_value_only_when_one_follows(self) -> None:
        for args in (("--apps", "--tag", "CODEX"), ("--apps", "old-value", "--tag", "CODEX"), ("--tag", "CODEX", "--apps")):
            with self.subTest(args=args):
                got = self.run_script(*args)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertNotIn("unknown arg", got.stderr)
                self.assertIn("--apps is ignored", got.stderr)

    def test_a_seat_with_a_row_but_no_app_gets_its_app_from_the_flag(self) -> None:
        data = json.loads(self.before())
        del data["seatApps"]["bySeat"]["CODEX"]
        self.registry.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.run_script("--tag", "CODEX").returncode, 1)
        got = self.run_script("--tag", "CODEX", "--app", "codex")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads(self.before())["seatApps"]["bySeat"]["CODEX"], "codex")


if __name__ == "__main__":
    unittest.main()
