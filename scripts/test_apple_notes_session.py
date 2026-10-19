"""AFC-361:  apple-notes-coding.sh needs a session id to create or update a note, stamps it as a `Session:` row,
and lets a seat with one thread with the owner (Grok bots, MA, JET, WISE, ECHO) write without one.

The script never touches Notes under APPLE_NOTES_PRINT_HTML=1:  it prints the note's HTML and exits, after the
session check and the stamp, so these tests run anywhere bash and /usr/bin/python3 exist.
Run:  python3 -m unittest scripts/test_apple_notes_session.py  (or cd scripts && python3 -m unittest test_apple_notes_session)
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "apple-notes-coding.sh"
PLATFORM_VARS = (
    "AGENT_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CURSOR_CONVERSATION_ID",
    "CURSOR_AGENT_CHAT_ID",
    "GROK_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "__MAVIS_RUNTIME_SESSION_ID",
)


@unittest.skipUnless(
    SCRIPT.is_file() and Path("/usr/bin/python3").exists(), "needs the script and /usr/bin/python3"
)
class SessionIdTests(unittest.TestCase):
    def run_script(
        self, *args: str, env: dict[str, str] | None = None, body: str = "Body text."
    ) -> subprocess.CompletedProcess:
        clean = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "APPLE_NOTES_PRINT_HTML": "1",
            "LANG": "en_US.UTF-8",
        }
        clean.update(env or {})
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            input=body,
            capture_output=True,
            text=True,
            env=clean,
            timeout=60,
            check=False,
        )

    def test_refuses_without_a_session_and_names_the_way_to_pass_one(self):
        p = self.run_script("[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"})
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")
        for needle in (
            "no session id",
            "--session <id>",
            "AGENT_SESSION",
            "CLAUDE_CODE_SESSION_ID",
        ):
            self.assertIn(needle, p.stderr)

    def test_refuses_for_every_non_exempt_seat_by_env_or_by_title(self):
        for seat in (
            "CLAUDE",
            "CODEX",
            "CURSOR",
            "GROK",
            "GROK-WEB",
            "OPENCODE",
            "MM",
            "MC",
            "FX",
            "AG",
            "CLUTCH",
            "BF-FIXER",
        ):
            p = self.run_script("[AFC, Agent] topic", env={"AGENT_SEAT": seat})
            self.assertEqual(p.returncode, 2, seat)
        for agent in (
            "Claude",
            "Codex",
            "Cursor",
            "Grok",
            "Grok Web",
            "MiniMax",
            "Fx",
            "Clutch",
            "Muse Code",
        ):
            p = self.run_script(f"[AFC, {agent}] topic")
            self.assertEqual(p.returncode, 2, agent)

    def test_update_needs_one_too_but_pin_and_unpin_do_not(self):
        p = self.run_script("--update", "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"})
        self.assertEqual(p.returncode, 2)
        self.assertIn("no session id", p.stderr)

    def test_exempt_seats_write_without_a_session_by_env(self):
        for seat in (
            "GROK-BOT",
            "GB-FIXER",
            "GB-DIRECTOR",
            "MA",
            "ROB",
            "MUSE-ASSIST",
            "JET",
            "WISE",
            "INSTINCT",
            "ECHO",
        ):
            p = self.run_script("[AFC, X] topic", env={"AGENT_SEAT": seat})
            self.assertEqual(p.returncode, 0, f"{seat}: {p.stderr}")
            self.assertNotIn("Session:", p.stdout, seat)

    def test_exempt_identities_are_found_from_the_title_over_ssh(self):
        """Instinct's Echo and Wise call the script over ssh with no seat in the environment."""
        for agent in (
            "Echo",
            "Wise",
            "Rob",
            "Muse Assist",
            "Jet",
            "Jet (OpenAI dot)",
            "Grok Bot",
            "GB-FIXER",
        ):
            p = self.run_script(f"[AFC, {agent}] topic")
            self.assertEqual(p.returncode, 0, f"{agent}: {p.stderr}")
            self.assertNotIn("Session:", p.stdout, agent)

    def test_the_environment_seat_beats_the_title(self):
        """A CLAUDE session cannot become exempt by writing Wise in the title."""
        p = self.run_script("[AFC, Wise] topic", env={"AGENT_SEAT": "CLAUDE"})
        self.assertEqual(p.returncode, 2)

    def test_a_launchers_seat_beats_agent_seat(self):
        env = {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-FIXER", "AGENT_SEAT": "WISE"}
        self.assertEqual(self.run_script("[AFC, Wise] topic", env=env).returncode, 2)

    def test_there_is_no_flag_that_waives_the_check(self):
        for flag in ("--exempt", "--no-session", "--session="):
            p = self.run_script(flag, "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"})
            self.assertEqual(p.returncode, 2, flag)
        p = self.run_script("--session", "", "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"})
        self.assertEqual(p.returncode, 2)

    def test_the_stamp_has_the_session_as_the_last_row(self):
        # Owner 2026-10-19:  the Session row is the LAST row of the note, not the third one.
        p = self.run_script(
            "--session", "abc12345-def6", "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"}
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        m = re.search(
            r"<p>((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w{3} \d{1,2}, \d{1,2}:\d{2}[ap]m)</p>"
            r".*<p>Session: abc12345-def6 - \1</p></div>\s*$",
            p.stdout,
            re.S,
        )
        self.assertIsNotNone(m, p.stdout)
        # It is not the row under the timestamp any more.
        self.assertIsNone(
            re.search(
                r"<p>(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w{3} \d{1,2}, \d{1,2}:\d{2}[ap]m</p>"
                r"\s*<p>Session:",
                p.stdout,
            ),
            p.stdout,
        )

    def test_platform_variables_are_found_per_seat(self):
        cases = {
            "CLAUDE": "CLAUDE_CODE_SESSION_ID",
            "CODEX": "CODEX_THREAD_ID",
            "GROK": "GROK_SESSION_ID",
            "OPENCODE": "OPENCODE_SESSION_ID",
            "CURSOR": "CURSOR_CONVERSATION_ID",
            "MM": "__MAVIS_RUNTIME_SESSION_ID",
        }
        for seat, var in cases.items():
            sid = f"{seat.lower()}-sess-0001"
            p = self.run_script("[AFC, Agent] topic", env={"AGENT_SEAT": seat, var: sid})
            self.assertEqual(p.returncode, 0, f"{seat}: {p.stderr}")
            self.assertIn(f"Session: {sid} -", p.stdout, seat)
        p = self.run_script(
            "[AFC, Cursor] topic", env={"AGENT_SEAT": "CURSOR", "CURSOR_AGENT_CHAT_ID": "chat-0002"}
        )
        self.assertIn("Session: chat-0002 -", p.stdout)

    def test_a_seat_with_its_own_variable_ignores_other_platforms(self):
        p = self.run_script(
            "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE", "CODEX_THREAD_ID": "codex-0003"}
        )
        self.assertEqual(p.returncode, 2)

    def test_a_seat_with_no_variable_of_its_own_tries_every_platform_variable(self):
        p = self.run_script(
            "[AFC, Agent] topic", env={"AGENT_SEAT": "BF-FIXER", "CODEX_THREAD_ID": "codex-0004"}
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Session: codex-0004 -", p.stdout)

    def test_precedence_flag_then_agent_session_then_platform(self):
        env = {
            "AGENT_SEAT": "CLAUDE",
            "AGENT_SESSION": "generic-0005",
            "CLAUDE_CODE_SESSION_ID": "platform-0006",
        }
        self.assertIn("Session: generic-0005 -", self.run_script("[AFC, Claude] t", env=env).stdout)
        self.assertIn(
            "Session: flag-0007 -",
            self.run_script("--session", "flag-0007", "[AFC, Claude] t", env=env).stdout,
        )
        env.pop("AGENT_SESSION")
        self.assertIn(
            "Session: platform-0006 -", self.run_script("[AFC, Claude] t", env=env).stdout
        )

    def test_a_malformed_id_is_refused(self):
        for bad in ("a b c d", "x", "-leading", "semi;colon", "q" * 129, "<b>tag</b>"):
            p = self.run_script(
                "--session", bad, "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"}
            )
            self.assertEqual(p.returncode, 2, bad)
            self.assertIn("not valid", p.stderr)

    def test_an_exempt_seat_with_an_id_still_stamps_it(self):
        p = self.run_script("--session", "wise-thread-1", "[AFC, Wise] topic")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Session: wise-thread-1 -", p.stdout)

    def test_an_update_replaces_a_session_row_echoed_from_the_old_body(self):
        # The old note had the row under the timestamp;  the new one has it at the end.  Both
        # shapes are dropped, so an update never leaves two Session rows behind.
        old = "<p>Mon, Oct 19, 9:40pm</p><p>Session: old-id-0001 - Mon, Oct 19, 9:40pm</p><p>Body.</p>"
        p = self.run_script(
            "--update",
            "--session",
            "new-id-0002",
            "[AFC, Claude] topic",
            env={"AGENT_SEAT": "CLAUDE"},
            body=old,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count("Session:"), 1, p.stdout)
        self.assertIn("Session: new-id-0002 -", p.stdout)
        self.assertNotIn("old-id-0001", p.stdout)

    def test_an_update_replaces_a_session_row_echoed_from_the_end_of_the_old_body(self):
        old = "<p>Mon, Oct 19, 9:40pm</p><p>Body.</p><p>Session: old-id-0001 - Mon, Oct 19, 9:40pm</p>"
        p = self.run_script(
            "--update",
            "--session",
            "new-id-0002",
            "[AFC, Claude] topic",
            env={"AGENT_SEAT": "CLAUDE"},
            body=old,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count("Session:"), 1, p.stdout)
        self.assertIn("Session: new-id-0002 -", p.stdout)
        self.assertNotIn("old-id-0001", p.stdout)

    def test_the_body_never_reaches_the_refusal(self):
        p = self.run_script(
            "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"}, body="TOPSECRETBODY"
        )
        self.assertNotIn("TOPSECRETBODY", p.stderr + p.stdout)


if __name__ == "__main__":
    unittest.main()
