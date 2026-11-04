"""`simple-chat daemon check-config`:  the listener's own verdict on a candidate listener.toml.

Muster's Wake & Listener panel runs this before it writes the file, so these tests hold the exact
failures that panel used to cause:  a section for a seat this instance may not hold (the listener
then refuses every reload and its next start), and a typo that drops a seat to inbox.
"""

from __future__ import annotations

import json
import os
import tomllib
import unittest

from agent_sync import live as L
from agent_sync.tests.listener_harness import ListenerHarness


class CheckConfig(ListenerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.write_config()
        self.current = (self.state_dir / "listener.toml").read_text()
        self.candidate = self.tmp / "candidate.toml"

    def check(self, text: str | None, **env: str) -> tuple[int, dict]:
        if text is not None:
            self.candidate.write_text(text)
        result = self.run_cli(
            "daemon", "check-config", "--file", str(self.candidate), "--json", env=self.env(**env)
        )
        return result.code, json.loads(result.out)

    def test_the_current_file_unchanged_is_ok(self) -> None:
        code, answer = self.check(self.current)
        self.assertEqual(code, 0)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["new_errors"], [])
        self.assertEqual(answer["refused"], [])

    def test_a_section_for_a_server_seat_is_refused(self) -> None:
        code, answer = self.check(self.current + '\n[seat.MA]\nbudget = { wakes_per_hour = 9 }\n')
        self.assertEqual(code, 1)
        self.assertFalse(answer["ok"])
        self.assertIn("seat MA is configured here", " ".join(answer["refused"]))

    def test_a_section_for_an_unlisted_seat_is_refused(self) -> None:
        code, answer = self.check(self.current + '\n[seat.KIMI]\nwake = "inbox"\n')
        self.assertEqual(code, 1)
        self.assertIn("does not list it", " ".join(answer["refused"]))

    def test_a_section_for_a_none_seat_is_refused(self) -> None:
        code, answer = self.check(self.current + '\n[seat.BF-BUILDER]\nwake = "inbox"\n')
        self.assertEqual(code, 1)
        self.assertIn("no listener instance holds it", " ".join(answer["refused"]))

    def test_an_existing_refusal_blocks_an_edit_but_is_not_new(self) -> None:
        """A file the listener already refuses cannot take another edit, since no reload of it would
        land.  The repair itself reads as having no new error, and that is how Muster allows it."""
        bad = self.current + '\n[seat.MA]\nbudget = { wakes_per_hour = 9 }\n'
        (self.state_dir / "listener.toml").write_text(bad)
        code, answer = self.check(bad)
        self.assertEqual(code, 1)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["new_errors"], [])
        self.assertTrue(answer["refused"])

    def test_a_bad_fallback_is_a_new_error(self) -> None:
        edited = self.current.replace('model = "sonnet"', 'model = "sonnet"\nfallback = "muse"')
        code, answer = self.check(edited)
        self.assertEqual(code, 1)
        self.assertIn("fallback must be one of", " ".join(answer["new_errors"]))

    def test_a_bare_binary_name_is_a_new_error(self) -> None:
        edited = self.current.replace('wake = "claude"', 'wake = "muse"\nmuse = "muse-bin"')
        code, answer = self.check(edited)
        self.assertEqual(code, 1)
        self.assertIn("muse must be an absolute path", " ".join(answer["new_errors"]))

    def test_a_section_with_no_bot_is_a_new_error(self) -> None:
        """A Mac seat the file does not list yet needs its bot, so a bare cap is not enough."""
        code, answer = self.check(self.current + '\n[seat.CODEX]\nwake = "inbox"\n')
        self.assertEqual(code, 1)
        self.assertIn("seat.CODEX.bot", " ".join(answer["new_errors"]))

    def test_an_existing_soft_error_on_another_seat_does_not_block(self) -> None:
        soft = self.current + '\n[seat.CODEX]\nbot = "Codex"\nwake = "nonsense"\n'
        (self.state_dir / "listener.toml").write_text(soft)
        code, answer = self.check(soft.replace('model = "sonnet"', 'model = "opus"'))
        self.assertEqual(code, 0)
        self.assertTrue(answer["ok"])
        self.assertTrue(answer["errors"])
        self.assertEqual(answer["new_errors"], [])

    def test_a_duplicate_key_is_a_parse_error_rather_than_an_empty_config(self) -> None:
        """A dotted key beside an inline table is a duplicate, and tomllib rejects the whole file.
        The running listener would then read an empty config and drop every seat."""
        edited = self.current.replace(
            'model = "sonnet"',
            'model = "sonnet"\nbudget.wakes_per_hour = 5\nbudget = { wakes_per_hour = 6 }',
        )
        code, answer = self.check(edited)
        self.assertEqual(code, 1)
        self.assertTrue(answer["parse_error"])
        self.assertNotIn(edited, answer["parse_error"])

    def test_an_empty_or_missing_candidate_is_refused(self) -> None:
        code, answer = self.check("")
        self.assertEqual(code, 1)
        self.assertIn("holds no settings", answer["parse_error"])
        self.candidate.unlink()
        code, answer = self.check(None)
        self.assertEqual(code, 1)
        self.assertIn("holds no settings", answer["parse_error"])

    def test_an_unreadable_partition_refuses_everything(self) -> None:
        code, answer = self.check(self.current, AGENT_SYNC_PARTITION=str(self.tmp / "nope.toml"))
        self.assertEqual(code, 1)
        self.assertIn("cannot check the seat partition", " ".join(answer["refused"]))

    def test_it_changes_nothing(self) -> None:
        before = (self.state_dir / "listener.toml").read_bytes()
        self.check(self.current + '\n[seat.MA]\nwake = "inbox"\n')
        self.assertEqual((self.state_dir / "listener.toml").read_bytes(), before)

    def test_the_plain_output_names_the_problem(self) -> None:
        self.candidate.write_text(self.current + '\n[seat.MA]\nwake = "inbox"\n')
        result = self.run_cli("daemon", "check-config", "--file", str(self.candidate))
        self.assertEqual(result.code, 1)
        self.assertIn("would not take", result.out)
        self.assertIn("seat MA", result.out)


class ReloadAnswer(ListenerHarness):
    """Muster reads these two fields to tell an accepted reload from a refused one."""

    def status(self) -> dict:
        return L.read_json(os.path.join(self.root, "listener", "status.json"))

    def test_a_refused_reload_is_published_on_its_own_with_its_time(self) -> None:
        self.write_config()
        daemon = self.daemon()
        daemon.write_status()
        self.assertEqual(self.status()["reload_refused"], [])
        self.assertIsNone(self.status()["reload_at"])
        good = (self.state_dir / "listener.toml").read_text()

        (self.state_dir / "listener.toml").write_text(good + '\n[seat.MA]\nwake = "inbox"\n')
        daemon.reload()
        daemon.write_status()
        status = self.status()
        self.assertEqual(status["reload_at"], self.clock.time())
        self.assertIn("seat MA is configured here", " ".join(status["reload_refused"]))
        self.assertIn("seat MA is configured here", " ".join(status["config_errors"]))

        self.clock.advance(5)
        (self.state_dir / "listener.toml").write_text(good)
        daemon.reload()
        daemon.write_status()
        status = self.status()
        self.assertEqual(status["reload_at"], self.clock.time())
        self.assertEqual(status["reload_refused"], [])

    def test_a_requested_reload_publishes_in_the_same_tick(self) -> None:
        self.write_config()
        daemon = self.daemon()
        daemon.tick()
        self.clock.advance(0.1)
        daemon.reload_requested = True
        daemon.tick()
        self.assertEqual(self.status()["reload_at"], self.clock.time())


class MusterFileShapes(unittest.TestCase):
    """The exact lines Muster's writer produces, pinned in MusterTests, read by the real parser.  No
    Python 3.11 is on the Mac CI runner's system path, so this is where tomllib sees them."""

    def test_a_cap_added_to_an_existing_table_parses(self) -> None:
        for line, expected in (
            (
                "budget = { wakes_per_hour = 5, wakes_per_day = 80, owner_per_day = 9 }",
                {"wakes_per_hour": 5, "wakes_per_day": 80, "owner_per_day": 9},
            ),
            ("budget = { owner_per_day = 9 }", {"owner_per_day": 9}),
        ):
            self.assertEqual(tomllib.loads(line)["budget"], expected)

    def test_a_text_with_control_characters_parses_back(self) -> None:
        parsed = tomllib.loads('model = "a\\r\\nb\\u0001c\\u007Fd\\te"\n')
        self.assertEqual(parsed["model"], "a\r\nb\x01c\x7fd\te")


if __name__ == "__main__":
    unittest.main()
