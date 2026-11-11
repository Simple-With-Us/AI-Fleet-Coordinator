"""Step 1 of the one wake path (owner, Nov 10, 2026):  the `[seat.X]` key schema, the derived seat `mode`,
and wake count caps that have no default.

A seat either has a `routine` (an HTTP POST to a router that decides, delivers and wakes) or it only
captures into its inbox.  These tests hold the safe first step:  a deprecated key still behaves exactly
as before and only warns, a stray key never downgrades a seat or drops its routine, and a cap the seat
leaves unset is no limit while `0` still pauses wakes.
"""

from __future__ import annotations

import json
import time
import tomllib
import unittest
from typing import Any

from simple_tools import config as C
from simple_tools import listener_cli as CLI
from simple_tools import live as L
from simple_tools import muse_bridge_mac as MBM
from simple_tools import wakes as W
from simple_tools.tests.listener_harness import ListenerHarness

HEAD = """
[daemon]
owner_user_id = 12
eligible_user_ids = [10, 11]
"""

ROUTINE_SEAT = """
[seat.JET]
bot = "Jet"
wake = "http"
routine = { url_env = "JET_ROUTINE_URL", key_env = "JET_ROUTINE_KEY", method = "POST", auth = "bearer" }
%s
"""

LEGACY_CLAUDE_SEAT = """
[seat.CLAUDE]
bot = "Claude"
wake = "claude"
model = "sonnet"
claude = "/opt/claude/bin/claude"
mcode = "/opt/mcode/bin/mcode"
mcode_model = "minimax/MiniMax-M3.1-Flash"
fallback = "mcode"
fallback_uncontained_ok = true
provider = "Claude Code"
typing_indicator = true
route_enabled = false
"""

LEGACY_MUSE_SEAT = """
[seat.MM]
bot = "MM"
wake = "muse"
muse = "/opt/muse/bin/muse"
fallback = "openrouter"
fallback_model = "minimax/MiniMax-M3.1-Flash"
"""


def load(text: str) -> C.Config:
    return C.from_dict(tomllib.loads(HEAD + text))


def shape(seat: C.SeatConfig) -> dict[str, Any]:
    """Everything about a parsed seat that decides how it behaves."""
    return {
        "wake": seat.wake,
        "model": seat.model,
        "budget": dict(seat.budget),
        "live": dict(seat.live),
        "routine": seat.routine.describe() if seat.routine else None,
        "problems": list(seat.config_problems),
        "held_back": seat.held_back,
        "enabled_seat": seat.enabled,
        "fallback": seat.fallback,
        "route_enabled": seat.route_enabled,
        "typing_indicator": seat.typing_indicator,
        "mode": seat.mode,
    }


class SeatKeySchema(unittest.TestCase):
    def test_a_seat_using_only_the_new_keys_has_no_warnings(self) -> None:
        cfg = load(
            """
            [seat.CODEX]
            instance = "mac"
            enabled = true
            creds = "file"
            bot = "Codex"
            bearer_file = "/home/x/.secrets/fleet-tools/codex"
            muse_bridge = false
            budget = { wakes_per_hour = 4, usd_per_day = 2.0 }
            held_back = { max = 50 }
            live = { per_hour = 7 }
            """
        )
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.warnings, [])
        self.assertIn("CODEX", cfg.seats)

    def test_the_env_credential_names_belong_to_creds_and_do_not_warn(self) -> None:
        cfg = C.from_dict(
            {
                "daemon": {"instance": "server", "owner_user_id": 12},
                "seat": {
                    "GB-FIXER": {
                        "creds": "env",
                        "email_env": "ZULIP_GB_FIXER_EMAIL",
                        "key_env": "ZULIP_GB_FIXER_API_KEY",
                        "site_env": "ZULIP_SITE",
                    }
                },
            }
        )
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.warnings, [])

    def test_every_legacy_key_still_works_and_warns_naming_the_seat_and_key(self) -> None:
        cfg = load(LEGACY_CLAUDE_SEAT + LEGACY_MUSE_SEAT)
        self.assertEqual(cfg.errors, [], "a deprecated key is not an error")
        claude, muse = cfg.seats["CLAUDE"], cfg.seats["MM"]
        # Exactly the behavior the keys had before the schema:  nothing is downgraded or dropped.
        self.assertEqual(claude.config_problems, [])
        self.assertEqual(
            (claude.wake, claude.model, claude.claude, claude.mcode, claude.mcode_model),
            ("claude", "sonnet", "/opt/claude/bin/claude", "/opt/mcode/bin/mcode",
             "minimax/MiniMax-M3.1-Flash"),
        )
        self.assertEqual((claude.fallback, claude.fallback_uncontained_ok), ("mcode", True))
        self.assertEqual(claude.provider, "Claude Code")
        self.assertTrue(claude.typing_indicator)
        self.assertFalse(claude.route_enabled)
        self.assertEqual((muse.wake, muse.muse), ("muse", "/opt/muse/bin/muse"))
        self.assertEqual((muse.fallback, muse.fallback_model), ("openrouter", "minimax/MiniMax-M3.1-Flash"))
        for key in sorted(C.SEAT_KEYS_LEGACY):
            with self.subTest(key=key):
                named = [w for w in cfg.warnings if ("seat.CLAUDE.%s " % key) in w or ("seat.MM.%s " % key) in w]
                self.assertTrue(named, (key, cfg.warnings))
                for warning in named:
                    self.assertIn("is deprecated", warning)

    def test_the_new_and_legacy_lists_do_not_overlap_and_cover_what_the_parser_reads(self) -> None:
        self.assertEqual(C.SEAT_KEYS & C.SEAT_KEYS_LEGACY, frozenset())
        self.assertEqual(C.SEAT_KEYS & C.SEAT_KEYS_RETIRED, frozenset())
        self.assertEqual(C.SEAT_KEYS_LEGACY & C.SEAT_KEYS_RETIRED, frozenset())
        for key in ("instance", "enabled", "creds", "bot", "bearer_file", "muse_bridge", "budget",
                    "held_back", "live", "routine"):
            self.assertIn(key, C.SEAT_KEYS)
        for key in ("wake", "model", "claude", "muse", "mcode", "mcode_model", "provider", "fallback",
                    "fallback_model", "fallback_uncontained_ok", "typing_indicator", "route_enabled"):
            self.assertIn(key, C.SEAT_KEYS_LEGACY)

    def test_an_unknown_key_warns_and_changes_nothing(self) -> None:
        plain = load(LEGACY_CLAUDE_SEAT)
        stray = load(LEGACY_CLAUDE_SEAT + 'frobnicate = 1\n')
        self.assertEqual(stray.errors, [])
        self.assertIn("seat.CLAUDE.frobnicate is not a known seat setting and is ignored", stray.warnings)
        self.assertEqual(shape(stray.seats["CLAUDE"]), shape(plain.seats["CLAUDE"]))
        self.assertEqual(stray.seats["CLAUDE"].wake, "claude", "a stray key never downgrades the seat")

    def test_an_unknown_key_beside_a_valid_routine_keeps_the_routine_and_the_http_mode(self) -> None:
        plain = load(ROUTINE_SEAT % "")
        stray = load(ROUTINE_SEAT % 'frobnicate = true\nroutin = { url_env = "X_URL" }')
        self.assertEqual(plain.errors, [])
        self.assertEqual(stray.errors, [], "a stray key is not a wake problem")
        seat = stray.seats["JET"]
        self.assertIsNotNone(seat.routine, "the routine is not dropped")
        self.assertEqual(seat.routine.url_env, "JET_ROUTINE_URL")
        self.assertEqual(seat.wake, "http")
        self.assertEqual(seat.mode, "routine")
        self.assertEqual(seat.config_problems, [], "nothing rewrote the seat to inbox")
        self.assertEqual(shape(seat), shape(plain.seats["JET"]))
        self.assertTrue(any("seat.JET.frobnicate " in w for w in stray.warnings))
        self.assertTrue(any("seat.JET.routin " in w for w in stray.warnings))

    def test_a_seat_level_routes_key_is_deprecated_not_unknown(self) -> None:
        cfg = load('[seat.CODEX]\nbot = "Codex"\nroutes_per_day = 4\n')
        self.assertEqual(cfg.errors, [])
        self.assertEqual(len(cfg.warnings), 1)
        self.assertIn("seat.CODEX.routes_per_day is deprecated", cfg.warnings[0])

    def test_legacy_budget_keys_warn_but_the_wake_caps_and_usd_do_not(self) -> None:
        cfg = load(
            """
            [seat.CODEX]
            bot = "Codex"
            budget = { board_per_day = 1, routes_per_hour = 2, routes_per_day = 3, route_topic_minutes = 4, usd_per_day = 1.5, wakes_per_hour = 5, owner_per_day = 6 }
            """
        )
        self.assertEqual(cfg.errors, [])
        self.assertEqual(
            cfg.warnings,
            [
                "seat.CODEX.budget.%s is deprecated:  it still works as before, and goes away with the "
                "wake types it belongs to" % key
                for key in ("board_per_day", "route_topic_minutes", "routes_per_day", "routes_per_hour")
            ],
        )
        budget = cfg.seats["CODEX"].budget
        self.assertEqual((budget["board_per_day"], budget["routes_per_hour"]), (1, 2))
        self.assertEqual((budget["wakes_per_hour"], budget["owner_per_day"]), (5, 6))

    def test_the_retired_keys_keep_their_own_refusal_and_add_no_unknown_key_warning(self) -> None:
        cfg = load('[seat.CODEX]\nbot = "Codex"\nbearer_locator = "x"\nallow_admin = true\n')
        self.assertTrue(any("bearer_locator is retired" in e for e in cfg.errors))
        self.assertTrue(any("allow_admin is not supported" in e for e in cfg.errors))
        self.assertEqual(cfg.warnings, [])

    def test_warnings_never_become_errors_or_wake_problems_and_come_in_a_stable_order(self) -> None:
        cfg = load(LEGACY_CLAUDE_SEAT + 'zzz = 1\naaa = 2\n')
        self.assertTrue(cfg.ok, cfg.errors)
        self.assertEqual(cfg.seats["CLAUDE"].config_problems, [])
        keys = [w.split(" ")[0] for w in cfg.warnings]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(load(LEGACY_CLAUDE_SEAT + 'zzz = 1\naaa = 2\n').warnings, cfg.warnings)

    def test_a_seat_the_credentials_refuse_still_reports_its_key_warnings(self) -> None:
        cfg = load('[seat.CODEX]\nbot = "not a bot!"\nfrobnicate = 1\n')
        self.assertNotIn("CODEX", cfg.seats)
        self.assertIn("seat.CODEX.frobnicate is not a known seat setting and is ignored", cfg.warnings)

    def test_an_empty_config_has_no_warnings(self) -> None:
        self.assertEqual(C.from_dict({}).warnings, [])


class SeatMode(unittest.TestCase):
    def test_a_seat_with_a_routine_is_in_routine_mode_and_keeps_wake_http(self) -> None:
        seat = load(ROUTINE_SEAT % "").seats["JET"]
        self.assertEqual(seat.mode, "routine")
        self.assertEqual(seat.wake, "http", "wake stays as the alias every existing caller reads")

    def test_a_seat_without_a_routine_only_captures(self) -> None:
        cfg = load('[seat.CODEX]\nbot = "Codex"\n' + LEGACY_CLAUDE_SEAT + LEGACY_MUSE_SEAT)
        for name in ("CODEX", "CLAUDE", "MM"):
            with self.subTest(seat=name):
                self.assertEqual(cfg.seats[name].mode, "capture")

    def test_a_routine_that_does_not_parse_leaves_the_seat_capturing(self) -> None:
        cfg = load(ROUTINE_SEAT.replace('url_env = "JET_ROUTINE_URL"', 'url_env = "lower"') % "")
        self.assertTrue(cfg.errors)
        self.assertEqual((cfg.seats["JET"].wake, cfg.seats["JET"].mode), ("inbox", "capture"))

    def test_mode_follows_the_routine_alone(self) -> None:
        routine = load(ROUTINE_SEAT % "").seats["JET"].routine
        for wake, given, want in (
            ("http", None, "capture"),
            ("http", routine, "routine"),
            ("inbox", routine, "routine"),
            ("claude", None, "capture"),
        ):
            with self.subTest(wake=wake, routine=bool(given)):
                seat = C.SeatConfig("X", "X", wake, "sonnet", {}, {}, None, routine=given)
                self.assertEqual(seat.mode, want)

    def test_for_every_seat_the_config_can_produce_a_routine_and_wake_http_agree(self) -> None:
        """The reason the bridge may ask `routine is not None` instead of `wake == "http"`:  the parser
        never leaves an http seat without its routine, and never gives a routine to another wake."""
        shapes = (
            ROUTINE_SEAT % "",
            ROUTINE_SEAT.replace('url_env = "JET_ROUTINE_URL"', 'url_env = "lower"') % "",
            ROUTINE_SEAT.replace('wake = "http"', 'wake = "inbox"') % "",
            ROUTINE_SEAT.replace('wake = "http"', 'wake = "claude"') % "",
            ROUTINE_SEAT.replace("routine = {", "routine_x = {") % "",
            '[seat.CODEX]\nbot = "Codex"\nwake = "http"\n',
            LEGACY_CLAUDE_SEAT,
            LEGACY_MUSE_SEAT,
            '[seat.CODEX]\nbot = "Codex"\nwake = "openrouter"\n',
            '[seat.CODEX]\nbot = "Codex"\nenabled = false\nwake = "http"\nroutine = {}\n',
        )
        for text in shapes:
            cfg = load(text)
            for seat in list(cfg.seats.values()) + list(cfg.disabled.values()):
                with self.subTest(text=text.strip()[:60], seat=seat.seat):
                    self.assertEqual(seat.wake == "http", seat.routine is not None)
                    self.assertEqual(seat.mode, "routine" if seat.routine is not None else "capture")


class WakeCapsHaveNoDefault(unittest.TestCase):
    def test_the_five_wake_caps_have_no_default_and_the_rest_keep_theirs(self) -> None:
        self.assertEqual(
            C.WAKE_CAP_KEYS,
            ("wakes_per_hour", "wakes_per_day", "per_topic_per_hour", "owner_per_day",
             "owner_per_topic_per_hour"),
        )
        for key in C.WAKE_CAP_KEYS:
            self.assertNotIn(key, C.BUDGET_DEFAULTS)
        for key in ("usd_per_day", "board_per_day", "routes_per_hour", "routes_per_day", "route_topic_minutes"):
            self.assertIn(key, C.BUDGET_DEFAULTS)

    def test_a_seat_with_no_budget_table_has_no_caps(self) -> None:
        cfg = load(LEGACY_CLAUDE_SEAT)
        budget = cfg.seats["CLAUDE"].budget
        for key in C.WAKE_CAP_KEYS:
            self.assertNotIn(key, budget)
        self.assertEqual(budget, dict(C.BUDGET_DEFAULTS))
        self.assertEqual(cfg.errors, [])

    def test_an_empty_budget_table_is_the_same_as_none(self) -> None:
        self.assertEqual(load(LEGACY_CLAUDE_SEAT + "budget = {}\n").seats["CLAUDE"].budget, dict(C.BUDGET_DEFAULTS))

    def test_a_cap_the_seat_sets_is_kept_and_only_that_one(self) -> None:
        budget = load(LEGACY_CLAUDE_SEAT + "budget = { wakes_per_day = 9, owner_per_day = 0 }\n").seats[
            "CLAUDE"
        ].budget
        self.assertEqual((budget["wakes_per_day"], budget["owner_per_day"]), (9, 0))
        for key in ("wakes_per_hour", "per_topic_per_hour", "owner_per_topic_per_hour"):
            self.assertNotIn(key, budget)

    def test_a_bad_cap_or_an_unknown_budget_key_is_still_a_wake_problem(self) -> None:
        for given in ("wakes_per_hour = -1", "wakes_per_hour = true", 'wakes_per_hour = "6"', "nonsense = 1"):
            with self.subTest(given=given):
                cfg = load(LEGACY_CLAUDE_SEAT + "budget = { %s }\n" % given)
                self.assertTrue(cfg.errors)
                self.assertEqual(cfg.seats["CLAUDE"].wake, "inbox", "the existing downgrade is unchanged")


def views(
    count: int, *, owner: bool = False, same_topic: bool = False, now: float = 100_000.0
) -> dict[str, dict[str, Any]]:
    """`count` wakes accepted in the last ten minutes, in one topic or one each."""
    return {
        "w%d" % i: {
            "state": "accepted",
            "owner": owner,
            "topic_key": "k" if same_topic else "k%d" % i,
            "times": {"accepted": now - 60 - i},
        }
        for i in range(count)
    }


class WakeBudgetCaps(unittest.TestCase):
    now = 100_000.0

    def block(self, budget: dict[str, float], count: int, **kw: Any) -> str | None:
        return W.budget_block(
            views(count, now=self.now, **kw), budget, owner=kw.get("owner", False), key="k", now=self.now
        )

    def test_a_budget_with_no_caps_never_blocks_however_busy_the_seat_is(self) -> None:
        for owner in (False, True):
            for same_topic in (False, True):
                with self.subTest(owner=owner, same_topic=same_topic):
                    self.assertIsNone(self.block(dict(C.BUDGET_DEFAULTS), 400, owner=owner, same_topic=same_topic))
                    self.assertIsNone(self.block({}, 400, owner=owner, same_topic=same_topic))

    def test_a_cap_blocks_when_the_count_reaches_it_and_not_before(self) -> None:
        cases = (
            ("wakes_per_hour", False, False),
            ("wakes_per_day", False, False),
            ("per_topic_per_hour", False, True),
            ("owner_per_day", True, False),
            ("owner_per_topic_per_hour", True, True),
        )
        for cap, owner, same_topic in cases:
            with self.subTest(cap=cap):
                budget = {cap: 3}
                self.assertIsNone(self.block(budget, 2, owner=owner, same_topic=same_topic))
                self.assertEqual(self.block(budget, 3, owner=owner, same_topic=same_topic), cap)
                self.assertEqual(self.block(budget, 40, owner=owner, same_topic=same_topic), cap)

    def test_zero_is_a_pause_even_with_no_wake_yet(self) -> None:
        cases = (
            ("wakes_per_hour", False),
            ("wakes_per_day", False),
            ("per_topic_per_hour", False),
            ("owner_per_day", True),
            ("owner_per_topic_per_hour", True),
        )
        for cap, owner in cases:
            with self.subTest(cap=cap):
                self.assertEqual(self.block({cap: 0}, 0, owner=owner), cap)

    def test_each_cap_is_its_own_and_owner_and_peer_caps_do_not_cross(self) -> None:
        self.assertIsNone(self.block({"wakes_per_hour": 0}, 5, owner=True), "a peer cap never holds the owner")
        self.assertIsNone(self.block({"owner_per_day": 0}, 5, owner=False), "an owner cap never holds a peer")
        only_day = {"wakes_per_day": 4}
        self.assertIsNone(self.block(only_day, 3), "the unset hourly cap adds no limit of its own")
        self.assertEqual(self.block(only_day, 4), "wakes_per_day")

    def test_a_null_cap_from_a_bridge_export_is_no_limit(self) -> None:
        self.assertIsNone(self.block({"wakes_per_hour": None, "wakes_per_day": None}, 50))
        self.assertFalse(W.at_cap(10**6, {"x": None}, "x"))
        self.assertFalse(W.at_cap(10**6, {}, "x"))
        self.assertTrue(W.at_cap(0, {"x": 0}, "x"))
        self.assertFalse(W.at_cap(2, {"x": 3}, "x"))
        self.assertTrue(W.at_cap(3, {"x": 3}, "x"))

    def test_the_board_and_route_caps_keep_their_defaults(self) -> None:
        self.assertEqual(W.board_block({}, dict(C.BUDGET_DEFAULTS), self.now), "board_per_day")


class LiveOwnerCap(unittest.TestCase):
    """live.py's own table (`live = { owner_per_day = ... }`) keeps its default;  the comparison only
    learned to skip a limit a table does not carry."""

    now = 9000.0

    def owners(self, count: int) -> dict[str, Any]:
        return {"turns": [{"ts": 1000.0 + i * 100, "topic": "t%d" % i, "owner": True} for i in range(count)]}

    def test_the_live_table_still_defaults_to_twenty_owner_turns_a_day(self) -> None:
        self.assertEqual(L.LIVE_DEFAULTS["owner_per_day"], 20)
        self.assertEqual(L.live_limits({}, "CLAUDE")["owner_per_day"], 20)
        self.assertEqual(L.live_room(self.owners(20), "new", True, self.now, dict(L.LIVE_DEFAULTS)), "owner_per_day")
        self.assertIsNone(L.live_room(self.owners(19), "new", True, self.now, dict(L.LIVE_DEFAULTS)))

    def test_a_table_without_the_limit_does_not_hold_owner_turns(self) -> None:
        limits = {k: v for k, v in L.LIVE_DEFAULTS.items() if k != "owner_per_day"}
        self.assertIsNone(L.live_room(self.owners(500), "new", True, self.now, limits))

    def test_zero_blocks_at_once_and_a_positive_limit_blocks_at_the_limit(self) -> None:
        self.assertEqual(L.live_room(self.owners(0), "new", True, self.now, dict(L.LIVE_DEFAULTS, owner_per_day=0)),
                         "owner_per_day")
        limits = dict(L.LIVE_DEFAULTS, owner_per_day=3)
        self.assertIsNone(L.live_room(self.owners(2), "new", True, self.now, limits))
        self.assertEqual(L.live_room(self.owners(3), "new", True, self.now, limits), "owner_per_day")

    def test_peer_limits_are_untouched(self) -> None:
        limits = {k: v for k, v in L.LIVE_DEFAULTS.items() if k != "owner_per_day"}
        hour = {"turns": [{"ts": 1000.0 + i, "topic": "t%d" % i, "owner": False} for i in range(6)]}
        self.assertEqual(L.live_room(hour, "new", False, 1100.0, limits), "per_hour")


class StatusCapText(unittest.TestCase):
    def test_an_unset_cap_reads_as_no_limit_never_as_zero(self) -> None:
        text = CLI._wake_caps_text
        self.assertEqual(text({"wakes_per_day": 40, "wakes_per_hour": 6}), "cap 40/day, 6/hour")
        self.assertEqual(text({}), "no cap")
        self.assertEqual(text(None), "no cap")
        self.assertEqual(text(dict(C.BUDGET_DEFAULTS)), "no cap")
        self.assertEqual(text({"wakes_per_day": 40}), "cap 40/day, no limit/hour")
        self.assertEqual(text({"wakes_per_hour": 6}), "cap no limit/day, 6/hour")
        self.assertEqual(text({"wakes_per_day": 0, "wakes_per_hour": 0}), "cap 0/day, 0/hour", "0 is a pause")


class BridgeBudgetReason(unittest.TestCase):
    """The Mac bridge checks the budget the server exports for a seat.  The five wake caps may be absent
    (no limit);  the rest of the budget must still arrive, and a cap that is present must be a number."""

    item = {"source_seat": "JET", "owner": False, "channel": "c", "topic": "t"}

    def reason(self, budget: Any, decisions: dict[str, Any] | None = None) -> str | None:
        return MBM.MacBridge._budget_reason(self.item, budget, decisions or {})

    def test_a_budget_with_no_caps_is_usable_and_holds_nothing(self) -> None:
        self.assertIsNone(self.reason(dict(C.BUDGET_DEFAULTS)))

    def test_a_missing_rest_of_the_budget_or_a_damaged_value_is_still_unavailable(self) -> None:
        self.assertEqual(self.reason(None), "budget_unavailable")
        self.assertEqual(self.reason({}), "budget_unavailable")
        short = {k: v for k, v in C.BUDGET_DEFAULTS.items() if k != "usd_per_day"}
        self.assertEqual(self.reason(short), "budget_unavailable")
        for bad in (-1, True, "6", None):
            with self.subTest(bad=bad):
                self.assertEqual(self.reason(dict(C.BUDGET_DEFAULTS, wakes_per_hour=bad)), "budget_unavailable")

    def test_a_cap_that_is_present_still_holds(self) -> None:
        self.assertEqual(self.reason(dict(C.BUDGET_DEFAULTS, wakes_per_hour=0)), "wakes_per_hour")
        key = L.topic_key("c", "t")
        now = time.time()
        decisions = {
            "d%d" % i: {
                "source_seat": "JET",
                "model_attempted": True,
                "owner": False,
                "topic_key": key,
                "at": now - 10 - i,
            }
            for i in range(2)
        }
        self.assertIsNone(self.reason(dict(C.BUDGET_DEFAULTS), decisions))
        self.assertEqual(self.reason(dict(C.BUDGET_DEFAULTS, per_topic_per_hour=2), decisions), "per_topic_per_hour")


class CheckConfigWarnings(ListenerHarness):
    """`simple-chat daemon check-config` prints the warnings and still exits 0 when they are all it has."""

    def setUp(self) -> None:
        super().setUp()
        self.write_config()
        self.current = (self.state_dir / "listener.toml").read_text()
        self.candidate = self.tmp / "candidate.toml"

    def check(self, text: str, *flags: str) -> tuple[int, Any]:
        self.candidate.write_text(text)
        result = self.run_cli("daemon", "check-config", "--file", str(self.candidate), *flags, env=self.env())
        return result.code, result.out

    def test_warnings_are_reported_in_json_and_do_not_change_ok_or_the_exit_code(self) -> None:
        code, out = self.check(self.current + "\n[seat.CODEX]\nbot = \"Codex\"\nfrobnicate = 1\n", "--json")
        answer = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["new_errors"], [])
        self.assertIn("seat.CODEX.frobnicate is not a known seat setting and is ignored", answer["warnings"])
        self.assertTrue(any("seat.CLAUDE.wake is deprecated" in w for w in answer["warnings"]), answer["warnings"])

    def test_the_plain_output_prints_each_warning_and_still_takes_the_file(self) -> None:
        code, out = self.check(self.current + "\n[seat.CODEX]\nbot = \"Codex\"\nfrobnicate = 1\n")
        self.assertEqual(code, 0)
        self.assertIn("the listener would take", out)
        self.assertIn("  warning: seat.CODEX.frobnicate is not a known seat setting", out)

    def test_a_clean_candidate_has_an_empty_warning_list(self) -> None:
        code, out = self.check('[daemon]\nowner_user_id = 12\n[seat.CLAUDE]\nbot = "Claude"\n', "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["warnings"], [])

    def test_a_real_error_still_fails_whatever_the_warnings_say(self) -> None:
        code, out = self.check(self.current + '\n[seat.CODEX]\nwake = "inbox"\nfrobnicate = 1\n', "--json")
        answer = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn("seat.CODEX.bot", " ".join(answer["new_errors"]))
        self.assertTrue(answer["warnings"])


if __name__ == "__main__":
    unittest.main()
