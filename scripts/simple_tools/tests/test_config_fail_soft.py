"""AFC-358:  a wake-config problem downgrades only that seat's WAKE to inbox.  The seat still
connects and captures, and status shows it red.  A seat that still names a retired wake type
(`claude`, `muse` or `openrouter`, owner ruling of Nov 10, 2026:  the listener starts no model) is
refused the same way, and the retired runtimes' own settings are ignored with a warning."""

from __future__ import annotations

import tomllib
import unittest

from simple_tools import config as C
from simple_tools.tests.listener_harness import ListenerHarness

MENTION = "@**Claude** can you check the AFC cutover?"

BASE = """
[daemon]
owner_user_id = 12
eligible_user_ids = [10, 11]

[seat.CLAUDE]
bot = "Claude"
wake = "http"
routine = {}
%s
"""

LEFTOVER_KEYS = (
    "model",
    "muse",
    "mcode",
    "mcode_model",
    "provider",
    "fallback",
    "fallback_model",
    "fallback_uncontained_ok",
    "typing_indicator",
    "route_enabled",
)


def retired_message(value: str) -> str:
    return (
        'seat.CLAUDE.wake = "%s" is retired:  the listener starts no model (owner, Nov 10, 2026);  '
        'write wake = "http" with a routine, or wake = "inbox" to capture only;  until then the '
        "seat only captures" % value
    )


def load(extra: str) -> C.Config:
    return C.from_dict(tomllib.loads(BASE % extra))


class FailSoftConfig(unittest.TestCase):
    def test_wake_loop_prevention_is_explicit_boolean(self) -> None:
        # Off in the code (owner, Nov 10, 2026:  wakes are liberal, nothing throttles them), on only when
        # the config says so.
        self.assertFalse(C.from_dict({}).wake_loop_prevention)
        self.assertTrue(C.from_dict({"daemon": {"wake_loop_prevention": True}}).wake_loop_prevention)
        self.assertFalse(C.from_dict({"daemon": {"wake_loop_prevention": False}}).wake_loop_prevention)
        for value in ("false", "true", 0, 1, None):
            cfg = C.from_dict({"daemon": {"wake_loop_prevention": value}})
            self.assertFalse(cfg.wake_loop_prevention, "a malformed value falls back to the off default")
            self.assertIn("daemon.wake_loop_prevention must be true or false", cfg.errors)


    def assertDowngraded(self, cfg: C.Config) -> C.SeatConfig:
        self.assertIn("CLAUDE", cfg.seats, "the seat still loads")
        self.assertEqual(cfg.skipped_seats, {})
        seat = cfg.seats["CLAUDE"]
        self.assertEqual(seat.wake, "inbox")
        self.assertTrue(seat.config_problems)
        self.assertTrue(cfg.errors, "the problem is still an error the owner sees")
        self.assertIsNone(seat.routine)
        return seat

    def test_a_leftover_runtime_key_is_a_warning_and_the_seat_keeps_its_routine(self) -> None:
        # Once an error that dropped the wake (a muse key on a claude seat cost CLAUDE 41 minutes of
        # mentions on 2026-10-18);  now the key is read by nothing, so whatever its value it only
        # warns, and an http seat that still carries one keeps waking through its routine.
        for key in LEFTOVER_KEYS:
            for value in ('"zzz"', "true", '"/opt/muse/bin/muse"'):
                with self.subTest(key=key, value=value):
                    cfg = load("%s = %s" % (key, value))
                    self.assertEqual(cfg.errors, [])
                    seat = cfg.seats["CLAUDE"]
                    self.assertEqual(seat.wake, "http")
                    self.assertIsNotNone(seat.routine)
                    self.assertEqual(seat.config_problems, [])
                    self.assertFalse(hasattr(seat, key), "a leftover key is never read into the seat")
                    self.assertIn(
                        "seat.CLAUDE.%s is left over from the retired claude, muse and openrouter "
                        "wake types and is ignored;  it can be deleted" % key,
                        cfg.warnings,
                    )

    def test_an_unknown_wake_value_downgrades_instead_of_dropping_the_seat(self) -> None:
        cfg = load("")
        self.assertEqual(cfg.seats["CLAUDE"].wake, "http")
        self.assertEqual(cfg.seats["CLAUDE"].config_problems, [])
        self.assertEqual(cfg.errors, [])
        seat = self.assertDowngraded(
            C.from_dict(tomllib.loads((BASE % "").replace('wake = "http"', 'wake = "bogus"')))
        )
        self.assertEqual(seat.config_problems[0], "seat.CLAUDE.wake must be one of inbox, http")

    def test_a_retired_wake_type_is_refused_first_and_the_seat_only_captures(self) -> None:
        for value in ("claude", "muse", "openrouter"):
            with self.subTest(wake=value):
                text = (BASE % "").replace('wake = "http"\nroutine = {}', 'wake = "%s"' % value)
                seat = self.assertDowngraded(C.from_dict(tomllib.loads(text)))
                self.assertEqual(seat.config_problems, [retired_message(value)])
                self.assertEqual(seat.mode, "capture")
                self.assertFalse(seat.held_back["catch_up"], "an inbox seat never catches up")
                # Another wake problem found earlier in the section (bearer_locator is checked
                # before the wake) still comes second:  the banner and status show the first one.
                text = text.replace(
                    'bot = "Claude"', 'bot = "Claude"\nbearer_locator = "Fleet-OPS/x"'
                )
                seat = self.assertDowngraded(C.from_dict(tomllib.loads(text)))
                self.assertEqual(seat.config_problems[0], retired_message(value))
                self.assertIn("seat.CLAUDE.bearer_locator is retired", seat.config_problems[1])

    def test_a_retired_wake_with_a_routine_says_the_routine_needs_http(self) -> None:
        text = (BASE % "").replace('wake = "http"', 'wake = "claude"')
        seat = self.assertDowngraded(C.from_dict(tomllib.loads(text)))
        self.assertEqual(
            seat.config_problems,
            [retired_message("claude"), 'seat.CLAUDE.routine applies only to wake = "http"'],
        )

    def test_a_bad_budget_or_path_downgrades_only_the_wake(self) -> None:
        for extra in (
            'claude = "relative/claude"',
            "budget = { nope = 1 }",
        ):
            with self.subTest(extra=extra):
                seat = self.assertDowngraded(C.from_dict(tomllib.loads(BASE % extra)))
                self.assertEqual(seat.bot, "Claude")

    def test_a_credential_problem_still_skips_the_seat_but_status_can_name_it(self) -> None:
        cfg = C.from_dict(
            tomllib.loads((BASE % "").replace('bot = "Claude"', 'bot = "not a bot name!"'))
        )
        self.assertNotIn("CLAUDE", cfg.seats)
        self.assertIn("CLAUDE", cfg.skipped_seats)
        self.assertIn("seat.CLAUDE.bot", cfg.skipped_seats["CLAUDE"][0])

    def test_a_shared_routine_variable_is_a_wake_problem_not_a_capture_problem(self) -> None:
        cfg = C.from_dict(
            tomllib.loads(
                (BASE % "").replace(
                    "routine = {}", 'routine = { url_env = "X_URL", key_env = "X_URL" }'
                )
            )
        )
        self.assertTrue(cfg.errors)
        if "CLAUDE" in cfg.seats:  # the routine table's own validation may refuse it first
            self.assertEqual(cfg.seats["CLAUDE"].wake, "inbox")


class FailSoftDaemon(ListenerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.pin()

    def test_a_retired_wake_seat_keeps_capturing_starts_nothing_and_shows_red(self) -> None:
        # The fake claude is on the wake PATH and pinned, so a wake that still ran it would show.
        seats = '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\n'
        self.write_config(seats=seats)
        daemon = self.daemon()
        self.assertIn("CLAUDE", daemon.seats, "the seat has a runner")
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual(len(self.inbox()), 1, "the mention was captured to the seat inbox")
        self.clock.advance(25.0)
        daemon.tick()
        daemon.run_jobs("CLAUDE")
        self.assertEqual(self.ledger(), [], "a capture seat writes no wake row")
        self.assertEqual(self.dumps(), [], "no model was started")
        self.assertEqual(self.operator_posts(), [], "the Operator posts nothing")
        status = daemon.status()
        self.assertEqual(status["seats"]["CLAUDE"]["wake"], "inbox")
        self.assertTrue(
            any(
                "config problem" in red and 'seat.CLAUDE.wake = "claude" is retired' in red
                for red in status["seats"]["CLAUDE"]["red"]
            ),
            status["seats"]["CLAUDE"]["red"],
        )
        self.assertTrue(
            any(
                "wake config problem" in b[8] and 'seat.CLAUDE.wake = "claude"' in b[8]
                for b in self.banners
            ),
            self.banners,
        )

    def test_a_seat_the_config_cannot_load_shows_as_a_red_row(self) -> None:
        seats = (
            '[seat.CLAUDE]\nbot = "Claude"\nwake = "inbox"\n[seat.CODEX]\nbot = "bad bot name!"\n'
        )
        self.write_config(seats=seats)
        daemon = self.daemon()
        self.assertNotIn("CODEX", daemon.seats)
        status = daemon.status()
        self.assertIn("CODEX", status["skipped_seats"])
        self.assertTrue(any("is NOT loaded" in b[8] for b in self.banners), self.banners)


if __name__ == "__main__":
    unittest.main()


class OperatorAllowConfig(unittest.TestCase):
    """`[operator] dm_allow` (AFC-412):  the users besides the owner who may be handed a tagged
    address's candidate list.  A bad section is an error and fails closed to the owner alone."""

    def test_the_allowlist_is_empty_by_default(self) -> None:
        cfg = load("")
        self.assertEqual(cfg.operator_dm_allow, frozenset())
        self.assertEqual(cfg.errors, [])

    def test_a_list_of_user_ids_is_parsed(self) -> None:
        cfg = C.from_dict(tomllib.loads(BASE % "" + "\n[operator]\ndm_allow = [4242, 77]\n"))
        self.assertEqual(cfg.operator_dm_allow, frozenset({4242, 77}))
        self.assertEqual(cfg.errors, [])

    def test_a_malformed_list_is_an_error_and_allows_nobody(self) -> None:
        for bad in ('dm_allow = ["4242"]', "dm_allow = [true]", "dm_allow = 4242", "dm_allow = [0]"):
            with self.subTest(bad=bad):
                cfg = C.from_dict(tomllib.loads(BASE % "" + "\n[operator]\n%s\n" % bad))
                self.assertEqual(cfg.operator_dm_allow, frozenset())
                self.assertIn("operator.dm_allow must be a list of user ids", cfg.errors)

    def test_an_unknown_operator_setting_is_an_error(self) -> None:
        cfg = C.from_dict(tomllib.loads(BASE % "" + "\n[operator]\ndm_alow = [1]\n"))
        self.assertTrue(any("[operator] has unknown setting" in e for e in cfg.errors))
