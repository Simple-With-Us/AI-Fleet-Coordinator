"""`simple-chat daemon pin-claude`:  the one command that writes the lease-liveness pin.

The listener starts no model (owner ruling, Nov 10, 2026).  The pin only lets `Daemon.agent_kinds` run
`claude agents --json`, so these tests use a fake claude that answers that one listing and nothing else.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time

from simple_tools import adapters as A
from simple_tools import live as L
from simple_tools.tests.listener_harness import ListenerHarness


class PinClaudeTests(ListenerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.write_config()
        self.paths = self.seat_paths("CLAUDE")

    # ---- helpers ------------------------------------------------------------------------------
    def make_claude(self, name: str, body: str) -> str:
        path = self.bin / name
        path.write_text("#!%s\nimport json, os, sys\n%s\n" % (sys.executable, body))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def pin(self, *extra: str, claude: str | None = None):
        return self.run_cli(
            "daemon", "pin-claude", "--seat", "CLAUDE", "--claude", claude or str(self.claude), *extra
        )

    def pin_file(self) -> dict:
        return L.read_json(self.paths.wake_pin)

    # ---- the pin is written only after a listing the daemon reads -----------------------------
    def test_a_usable_listing_is_pinned_and_the_daemon_then_asks_it(self) -> None:
        self.assertFalse(A.agents_pinned(self.paths, str(self.claude)))
        result = self.pin()
        self.assertEqual(result.code, 0, result.err + result.out)
        self.assertIn("pinned %s for CLAUDE" % os.path.realpath(str(self.claude)), result.out)
        self.assertTrue(A.agents_pinned(self.paths, str(self.claude)))
        pin = self.pin_file()
        self.assertEqual(pin["claude"], os.path.realpath(str(self.claude)))
        self.assertIs(pin["agents_ok"], True)
        # The daemon, which asked nothing while the pin was missing, now reads the listing.
        self.assertEqual(self.daemon().agent_kinds(), {os.getpid(): "interactive"})

    def test_the_json_report_counts_the_sessions_and_never_describes_them(self) -> None:
        self.set_claude(
            agents=[
                {"pid": 11, "kind": "interactive", "cwd": "/private/place", "name": "a secret title"},
                {"pid": 12, "kind": "background"},
                {"pid": 13, "type": "Interactive"},
            ]
        )
        result = self.pin("--json")
        self.assertEqual(result.code, 0, result.err)
        report = json.loads(result.out)
        self.assertEqual(
            (report["ok"], report["seat"], report["agents"]), (True, "CLAUDE", 3)
        )
        self.assertEqual(report["kinds"], {"interactive": 2, "background": 1})
        self.assertEqual(report["claude"], os.path.realpath(str(self.claude)))
        for needle in ("/private/place", "a secret title"):
            self.assertNotIn(needle, result.out)

    def test_an_empty_listing_is_still_a_listing(self) -> None:
        self.set_claude(agents=[])
        result = self.pin()
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("(0 sessions)", result.out)
        self.assertTrue(A.agents_pinned(self.paths, str(self.claude)))

    def test_nothing_is_pinned_when_the_listing_fails_or_is_not_a_listing(self) -> None:
        bad = {
            "exits 1": "sys.exit(1)",
            "prints text": 'print("not json")',
            "prints a scalar": "print(json.dumps(5))",
            "prints rows that are not objects": "print(json.dumps([1, 2]))",
            "prints nothing": "pass",
        }
        for label, body in bad.items():
            with self.subTest(label):
                claude = self.make_claude("claude-bad", body)
                result = self.pin("--json", claude=claude)
                self.assertEqual(result.code, 1, label)
                report = json.loads(result.out)
                self.assertEqual((report["ok"], report["error"]), (False, "agents_unusable"))
                self.assertFalse(os.path.exists(self.paths.wake_pin), label)

    def test_a_listing_that_hangs_is_given_up_on_and_pins_nothing(self) -> None:
        # A real blocking binary, so the test dies if the 10-second give-up is ever
        # removed (a stubbed run_agents would pass with no timeout at all) - Kody, PR 2336.
        claude = self.make_claude("claude-hang", "import time; time.sleep(3600)")
        started = time.monotonic()
        result = self.pin(claude=claude)
        elapsed = time.monotonic() - started
        self.assertEqual(result.code, 1)
        self.assertIn("nothing was pinned", result.out)
        self.assertGreaterEqual(elapsed, 9.0, "the listing was given up on before its 10-second limit")
        self.assertLess(elapsed, 60.0, "the hang was not bounded")
        self.assertFalse(os.path.exists(self.paths.wake_pin))

    def test_a_seat_the_daemon_does_not_read_is_refused_and_pins_nothing(self) -> None:
        # Daemon.agent_kinds reads only cfg.claude_seat's pin, so a pin for another
        # seat would be a file the check never reads (Kody, PR 2336).
        marker = self.tmp / "ran-other-seat"
        claude = self.make_claude(
            "claude-marked-other", "open(%r, 'a').write('x')\nprint(json.dumps([]))" % str(marker)
        )
        result = self.pin("--json", "--seat", "OTHER", claude=claude)
        self.assertEqual(result.code, 1)
        report = json.loads(result.out)
        self.assertEqual((report["ok"], report["error"]), (False, "wrong_seat"))
        self.assertEqual(report["seat"], "OTHER")
        plain = self.pin("--seat", "OTHER", claude=claude)
        self.assertEqual(plain.code, 1)
        self.assertIn("the daemon reads (CLAUDE)", plain.out)
        self.assertFalse(marker.exists(), "the listing never ran")
        self.assertFalse(os.path.exists(self.paths.wake_pin))

    def test_a_binary_other_than_the_seat_configured_is_refused_and_pins_nothing(self) -> None:
        # Daemon.agent_kinds resolves the seat's own configured claude, so a pin for
        # any other binary would never be read (Kody, PR 2336).
        self.write_config(claude=str(self.claude))
        other = self.make_claude("claude-configured-other", "print(json.dumps([]))")
        result = self.pin("--json", claude=other)
        self.assertEqual(result.code, 1)
        report = json.loads(result.out)
        self.assertEqual((report["ok"], report["error"]), (False, "claude_mismatch"))
        self.assertEqual(report["daemon_claude"], os.path.realpath(str(self.claude)))
        self.assertFalse(os.path.exists(self.paths.wake_pin))
        # The configured binary itself is still the one pin that stands.
        self.assertEqual(self.pin().code, 0)
        self.assertTrue(A.agents_pinned(self.paths, str(self.claude)))

    def test_a_missing_or_relative_binary_pins_nothing(self) -> None:
        for claude in ("relative/claude", str(self.bin / "no-such-claude"), "claude"):
            with self.subTest(claude):
                result = self.pin("--json", claude=claude)
                self.assertEqual(result.code, 1)
                self.assertEqual(json.loads(result.out)["error"], "no_claude")
                self.assertFalse(os.path.exists(self.paths.wake_pin))

    # ---- --check runs and writes nothing --------------------------------------------------------
    def test_check_runs_nothing_and_writes_nothing_and_reports_the_pin(self) -> None:
        marker = self.tmp / "ran"
        claude = self.make_claude(
            "claude-marked", "open(%r, 'a').write('x')\nprint(json.dumps([]))" % str(marker)
        )
        before = self.pin("--check", claude=claude)
        self.assertEqual(before.code, 1)
        self.assertIn("not pinned", before.out)
        self.assertFalse(marker.exists(), "--check never runs claude")
        self.assertFalse(os.path.exists(self.paths.wake_pin))
        self.assertEqual(self.pin(claude=claude).code, 0)
        self.assertEqual(marker.read_text(), "x", "pinning ran the listing once")
        after = self.pin("--check", claude=claude)
        self.assertEqual(after.code, 0, after.out)
        self.assertIn("pinned and its listing is usable", after.out)
        self.assertEqual(marker.read_text(), "x", "--check still ran nothing")

    def test_a_pin_for_one_binary_does_not_stand_for_another(self) -> None:
        self.assertEqual(self.pin().code, 0)
        other = self.make_claude("claude-other", "print(json.dumps([]))")
        result = self.pin("--check", "--json", claude=other)
        self.assertEqual(result.code, 1)
        report = json.loads(result.out)
        self.assertEqual((report["pinned"], report["agents_ok"]), (False, False))

    def test_a_pin_whose_listing_was_not_usable_is_reported_as_such(self) -> None:
        A.record_pin(self.paths, str(self.claude), self.clock.time(), agents_ok=False)
        result = self.pin("--check")
        self.assertEqual(result.code, 1)
        self.assertIn("pinned, but its listing was not found usable", result.out)

    # ---- how the listing is run --------------------------------------------------------------
    def test_the_listing_runs_in_the_daemons_clean_environment(self) -> None:
        dump = self.tmp / "env.json"
        claude = self.make_claude(
            "claude-env",
            "open(%r, 'w').write(json.dumps({'env': dict(os.environ), 'argv': sys.argv[1:], "
            "'stdin_closed': not sys.stdin.read(1)}))\nprint(json.dumps([]))" % str(dump),
        )
        env = self.env(
            CLAUDE_CODE_SOCKET="s",
            AGENT_SEAT="X",
            ZULIP_KEY_NAME="z",
            ANTHROPIC_API_KEY="a",
        )
        result = self.run_cli("daemon", "pin-claude", "--seat", "CLAUDE", "--claude", claude, env=env)
        self.assertEqual(result.code, 0, result.err)
        seen = json.loads(dump.read_text())
        self.assertEqual(seen["argv"], ["agents", "--json"])
        self.assertTrue(seen["stdin_closed"])
        for planted in ("CLAUDE_CODE_SOCKET", "AGENT_SEAT", "ZULIP_KEY_NAME", "ANTHROPIC_API_KEY"):
            self.assertNotIn(planted, seen["env"])
        self.assertEqual(seen["env"]["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")
        self.assertEqual(seen["env"]["HOME"], str(self.home))
        self.assertEqual(seen["env"]["PATH"], A.default_wake_path(str(self.home)))

    def test_the_command_is_listed_under_daemon(self) -> None:
        result = self.run_cli("daemon", "--help")
        self.assertIn("pin-claude", result.out + result.err)


if __name__ == "__main__":
    import unittest

    unittest.main()
