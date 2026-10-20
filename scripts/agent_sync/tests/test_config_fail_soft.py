"""AFC-358:  a wake-config problem downgrades only that seat's WAKE to inbox.  The seat still
connects and captures, status shows it red, and a missing claude or muse binary raises a banner."""

from __future__ import annotations

import os
import tomllib
import unittest

from agent_sync import config as C
from agent_sync.tests.listener_harness import ListenerHarness

MENTION = "@**Claude** can you check the AFC cutover?"

BASE = """
[daemon]
owner_user_id = 12
eligible_user_ids = [10, 11]

[seat.CLAUDE]
bot = "Claude"
wake = "claude"
model = "sonnet"
%s
"""


def load(extra: str) -> C.Config:
    return C.from_dict(tomllib.loads(BASE % extra))


class FailSoftConfig(unittest.TestCase):
    def assertDowngraded(self, cfg: C.Config) -> C.SeatConfig:
        self.assertIn("CLAUDE", cfg.seats, "the seat still loads")
        self.assertEqual(cfg.skipped_seats, {})
        seat = cfg.seats["CLAUDE"]
        self.assertEqual(seat.wake, "inbox")
        self.assertTrue(seat.config_problems)
        self.assertTrue(cfg.errors, "the problem is still an error the owner sees")
        self.assertIsNone(seat.muse)
        self.assertIsNone(seat.fallback)
        return seat

    def test_a_muse_key_without_wake_muse_keeps_the_seat_and_drops_its_wake(self) -> None:
        # The exact typo that cost CLAUDE 41 minutes of mentions on 2026-10-18.
        seat = self.assertDowngraded(load('muse = "/opt/muse/bin/muse"'))
        self.assertIn("seat.CLAUDE.muse applies only to", seat.config_problems[0])

    def test_an_unknown_wake_value_downgrades_instead_of_dropping_the_seat(self) -> None:
        cfg = load("")
        self.assertEqual(cfg.seats["CLAUDE"].wake, "claude")
        self.assertEqual(cfg.seats["CLAUDE"].config_problems, [])
        seat = self.assertDowngraded(
            C.from_dict(tomllib.loads((BASE % "").replace('wake = "claude"', 'wake = "bogus"')))
        )
        self.assertIn("seat.CLAUDE.wake must be one of", seat.config_problems[0])

    def test_a_bad_budget_or_model_or_path_downgrades_only_the_wake(self) -> None:
        for extra in (
            'model = "no spaces allowed"',
            'claude = "relative/claude"',
            "budget = { nope = 1 }",
            'fallback = "zzz"',
        ):
            with self.subTest(extra=extra):
                text = BASE % extra
                text = (
                    text.replace('model = "sonnet"\n', "", 1) if extra.startswith("model") else text
                )
                seat = self.assertDowngraded(C.from_dict(tomllib.loads(text)))
                self.assertEqual(seat.bot, "Claude")
                self.assertTrue(C.re.fullmatch(r"[a-z0-9.\-\[\]]{1,60}", seat.model))

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
                (BASE % "").replace('wake = "claude"', 'wake = "http"')
                + '\n[seat.CLAUDE.routine]\nurl_env = "X_URL"\nkey_env = "X_URL"\n'
            )
        )
        self.assertTrue(cfg.errors)
        if "CLAUDE" in cfg.seats:  # the routine table's own validation may refuse it first
            self.assertEqual(cfg.seats["CLAUDE"].wake, "inbox")


class FailSoftDaemon(ListenerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.pin()

    def test_a_wake_typo_keeps_capturing_and_status_shows_it_red(self) -> None:
        seats = '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\nmodel = "sonnet"\nmuse = "/opt/muse/bin/muse"\n'
        self.write_config(seats=seats)
        daemon = self.daemon()
        self.assertIn("CLAUDE", daemon.seats, "the seat has a runner")
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual(len(self.inbox()), 1, "the mention was captured to the seat inbox")
        status = daemon.status()
        self.assertEqual(status["seats"]["CLAUDE"]["wake"], "inbox")
        self.assertTrue(
            any(
                "config problem" in red and "seat.CLAUDE.muse" in red
                for red in status["seats"]["CLAUDE"]["red"]
            ),
            status["seats"]["CLAUDE"]["red"],
        )
        self.assertTrue(any("wake config problem" in b[8] for b in self.banners), self.banners)

    def test_a_seat_the_config_cannot_load_shows_as_a_red_row(self) -> None:
        seats = (
            '[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n[seat.CODEX]\nbot = "bad bot name!"\n'
        )
        self.write_config(seats=seats)
        daemon = self.daemon()
        self.assertNotIn("CODEX", daemon.seats)
        status = daemon.status()
        self.assertIn("CODEX", status["skipped_seats"])
        self.assertTrue(any("is NOT loaded" in b[8] for b in self.banners), self.banners)

    def test_a_missing_claude_binary_raises_a_banner_naming_the_setting(self) -> None:
        self.write_config()
        os.unlink(self.claude)
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(25.0)
        daemon.tick()
        daemon.run_jobs("CLAUDE")
        self.assertEqual(self.ledger()[-1]["reason"], "no_claude")
        self.assertTrue(
            any(
                "no claude binary found" in b[8] and "seat.CLAUDE.claude" in b[8]
                for b in self.banners
            ),
            self.banners,
        )

    def test_the_binary_banner_is_rate_limited(self) -> None:
        self.write_config()
        os.unlink(self.claude)
        daemon = self.daemon()
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        for n in range(2):
            self.fake.add_message("Codex", "agent-sync", "t%d" % n, MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 2)
        self.clock.advance(25.0)
        daemon.tick()
        daemon.run_jobs("CLAUDE")
        self.assertEqual(
            sum("no claude binary found" in b[8] for b in self.banners), 1, self.banners
        )


if __name__ == "__main__":
    unittest.main()
