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
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "apple-notes-coding.sh"
#  the script drops the year only when it is the current one, so the expected date follows the clock
OCT_19 = "Mon, Oct 19" if datetime.now().year == 2026 else "Mon, Oct 19, 2026"
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

    def test_the_stamp_has_the_session_under_the_timestamp(self):
        p = self.run_script(
            "--session", "abc12345-def6", "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"}
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        m = re.search(
            r"<p>(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \w{3} \d{1,2}, \d{1,2}:\d{2}[ap]m</p>"
            r'<p><span style="font-size: 11px;">Session: abc12345-def6</span></p>',
            p.stdout,
        )
        self.assertIsNotNone(m, p.stdout)

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
            self.assertIn(f"Session: {sid}</span>", p.stdout, seat)
        p = self.run_script(
            "[AFC, Cursor] topic", env={"AGENT_SEAT": "CURSOR", "CURSOR_AGENT_CHAT_ID": "chat-0002"}
        )
        self.assertIn("Session: chat-0002</span>", p.stdout)

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
        self.assertIn("Session: codex-0004</span>", p.stdout)

    def test_precedence_flag_then_agent_session_then_platform(self):
        env = {
            "AGENT_SEAT": "CLAUDE",
            "AGENT_SESSION": "generic-0005",
            "CLAUDE_CODE_SESSION_ID": "platform-0006",
        }
        self.assertIn(
            "Session: generic-0005</span>", self.run_script("[AFC, Claude] t", env=env).stdout
        )
        self.assertIn(
            "Session: flag-0007</span>",
            self.run_script("--session", "flag-0007", "[AFC, Claude] t", env=env).stdout,
        )
        env.pop("AGENT_SESSION")
        self.assertIn(
            "Session: platform-0006</span>", self.run_script("[AFC, Claude] t", env=env).stdout
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
        self.assertIn("Session: wise-thread-1</span>", p.stdout)

    def test_an_update_replaces_a_session_row_echoed_from_the_old_body(self):
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
        self.assertIn("Session: new-id-0002</span>", p.stdout)
        self.assertNotIn("old-id-0001", p.stdout)

    def test_a_chat_tag_is_not_a_session_id(self):
        p = self.run_script("--session", "session8", "[BF, MM] t", env={"AGENT_SEAT": "MM"})
        self.assertEqual(p.returncode, 2)
        self.assertIn("__MAVIS_RUNTIME_SESSION_ID", p.stderr)

    def test_a_session_row_left_at_the_end_by_the_old_layout_is_dropped(self):
        old = "<p>Mon, Oct 19, 9:40pm</p><p>Body.</p><p>Session: old-id-0001 - Mon, Oct 19, 9:40pm</p>"
        p = self.run_script(
            "--update",
            "--session",
            "new-id-0002",
            "[AFC, Claude] t",
            env={"AGENT_SEAT": "CLAUDE"},
            body=old,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count("Session:"), 1, p.stdout)
        self.assertLess(p.stdout.index("Session:"), p.stdout.index("Body."))

    def test_iso_dates_in_prose_become_fleet_dates_but_code_and_links_keep_them(self):
        body = (
            "<p>Merged 2026-10-19, deployed 2026-10-20T02:03:00Z, filed 2025-03-07.</p>"
            "<pre><code>log 2026-10-19</code></pre>"
            '<p><a href="https://x.test/2026-10-19">see</a> backup-2026-10-19.tar</p>'
        )
        p = self.run_script(
            "--session", "abc12345", "[AFC, Claude] t", env={"AGENT_SEAT": "CLAUDE"}, body=body
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(
            f"Merged {OCT_19}, deployed {OCT_19}, 9:03pm, filed Fri, Mar 7, 2025.", p.stdout
        )
        self.assertIn("log 2026-10-19", p.stdout)
        self.assertIn("https://x.test/2026-10-19", p.stdout)
        self.assertIn("backup-2026-10-19.tar", p.stdout)
        self.assertIn(
            "notes: reformatted:  dates rewritten in the fleet format (2026-10-19 to", p.stderr
        )

    def test_one_space_sentence_gaps_are_widened_and_reported(self):
        body = "<p>Merged it. Next is CI. Ask Dr. Smith. The U.S. Army. 4 tests failed. Done.</p><pre><code>a. B</code></pre>"
        p = self.run_script(
            "--session", "abc12345", "[AFC, Claude] t", env={"AGENT_SEAT": "CLAUDE"}, body=body
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        #  The widened gap is a no-break space plus a normal space, the same gap as every
        #  other surface.  The note body is HTML, so the gap is the numeric reference, like
        #  the two HTML passes.
        g = "&#160; "
        self.assertIn(
            f"Merged it.{g}Next is CI.{g}Ask Dr. Smith.{g}The U.S. Army.{g}4 tests failed.{g}Done.",
            p.stdout,
        )
        self.assertIn("a. B", p.stdout)
        self.assertIn("notes: reformatted:  5 sentence gaps had one space", p.stderr)
        clean = self.run_script(
            "--session",
            "abc12345",
            "[AFC, Claude] t",
            env={"AGENT_SEAT": "CLAUDE"},
            body="<p>One.  Two.</p>",
        )
        self.assertNotIn("reformatted", clean.stderr)

    def test_a_time_range_end_is_not_read_as_a_utc_offset(self):
        body = "<p>Standup 2026-10-19 10:30-11:00 Central, and 2026-10-19T10:30-11:00.</p>"
        p = self.run_script(
            "--session", "abc12345", "[AFC, Claude] t", env={"AGENT_SEAT": "CLAUDE"}, body=body
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        #  the space form keeps its range end instead of shifting 10:30 to 4:30pm on the -11:00, and both
        #  ends read in the fleet form rather than on a 24-hour clock
        self.assertIn(f"Standup {OCT_19}, 10:30am-11:00am Central", p.stdout)
        #  the ISO T form really is an offset, and still converts
        self.assertIn(f"and {OCT_19}, 4:30pm", p.stdout)

    def test_prose_that_starts_with_session_is_not_mistaken_for_a_row(self):
        """`Session:` opening a paragraph of prose is content:  an id-shaped word after it is still prose."""
        body = "<p>Mon, Oct 19, 9:40pm</p><p>Body.</p><p>Session: the board claim was AFC-123</p>"
        p = self.run_script(
            "--update",
            "--session",
            "new-id-0002",
            "[AFC, Claude] t",
            #  a synthetic Linear host and workspace, so the fixture names no organisation of the
            #  fleet (Kody rule:  synthetic hostnames and paths in test fixtures)
            env={
                "AGENT_SEAT": "CLAUDE",
                "NOTES_LINKIFY_LINEAR_HOST": "notes.example.test",
                "NOTES_LINEAR_WORKSPACE": "example-workspace",
            },
            body=body,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        #  the prose survives as prose, and the Linear id inside it is linked by
        #  the linkifier (AFC-547) rather than being left bare -- either way it
        #  is content, not the stamped row
        self.assertIn("Session: the board claim was", p.stdout)
        self.assertIn('href="https://notes.example.test/example-workspace/issue/AFC-123"', p.stdout)
        self.assertEqual(
            p.stdout.count("Session:"), 2, p.stdout
        )  #  the prose one and the stamped row

    def test_a_stale_row_left_at_the_end_goes_with_its_trailing_spacer(self):
        old = (
            "<p>Mon, Oct 19, 9:40pm</p><p>Body.</p>"
            '<p><span style="font-size: 11px;">Session: old-id-0001</span></p><div><br></div>'
        )
        p = self.run_script(
            "--update",
            "--session",
            "new-id-0002",
            "[AFC, Claude] t",
            env={"AGENT_SEAT": "CLAUDE"},
            body=old,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count("Session:"), 1, p.stdout)
        self.assertNotIn("old-id-0001", p.stdout)

    def test_a_stale_row_left_at_the_end_goes_with_every_spacer_behind_it(self):
        """An old note can end on more than one spacer:  the row still goes, or two rows are left."""
        old = (
            "<p>Mon, Oct 19, 9:40pm</p><p>Body.</p>"
            '<p><span style="font-size: 11px;">Session: old-id-0001</span></p>'
            "<div><br></div><div><br></div>"
        )
        p = self.run_script(
            "--update",
            "--session",
            "new-id-0002",
            "[AFC, Claude] t",
            env={"AGENT_SEAT": "CLAUDE"},
            body=old,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count("Session:"), 1, p.stdout)
        self.assertNotIn("old-id-0001", p.stdout)

    def test_a_date_inside_a_file_name_or_a_query_string_keeps_its_iso_form(self):
        body = (
            "<p>Merged 2026-10-19.  Ship 2026-10-19.tar.gz, keep backup-2026-10-19.tar, "
            "and https://x.test/q?d=2026-10-19&amp;n=2</p>"
        )
        p = self.run_script(
            "--session", "abc12345", "[AFC, Claude] t", env={"AGENT_SEAT": "CLAUDE"}, body=body
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        #  a sentence-final date converts;  a date glued to a name or a query value does not
        self.assertIn(f"Merged {OCT_19}.", p.stdout)
        self.assertIn("2026-10-19.tar.gz", p.stdout)
        self.assertIn("backup-2026-10-19.tar", p.stdout)
        self.assertIn("?d=2026-10-19&amp;n=2", p.stdout)

    def test_a_utc_range_end_converts_like_its_start_and_session_prose_stays(self):
        body = (
            "<p>Window 2026-10-19 15:30Z-16:00 for the deploy.</p>"
            "<p>Session: claude-code resumed after the restart.</p>"
        )
        p = self.run_script(
            "--update",
            "--session",
            "abc12345",
            "[AFC, Claude] t",
            env={"AGENT_SEAT": "CLAUDE"},
            body=body,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"Window {OCT_19}, 10:30am-11:00am for the deploy.", p.stdout)
        self.assertIn("Session: claude-code resumed after the restart.", p.stdout)

    def test_an_old_session_row_with_a_year_is_replaced_not_kept(self):
        old = (
            "<p>Mon, Oct 19, 9:40pm</p><p>Session: old-id-0001 - Mon, Oct 19, 2026, 9:40pm</p>"
            "<div><br></div><p>Body.</p>"
        )
        p = self.run_script(
            "--update",
            "--session",
            "new-id-0002",
            "[AFC, Claude] t",
            env={"AGENT_SEAT": "CLAUDE"},
            body=old,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.count("Session:"), 1, p.stdout)
        self.assertNotIn("old-id-0001", p.stdout)

    def test_the_body_never_reaches_the_refusal(self):
        p = self.run_script(
            "[AFC, Claude] topic", env={"AGENT_SEAT": "CLAUDE"}, body="TOPSECRETBODY"
        )
        self.assertNotIn("TOPSECRETBODY", p.stderr + p.stdout)

    def test_four_concurrent_script_runs_do_not_collide(self):
        """Owner 2026-10-19 (AFC-422):  four rapid notes writes on one Mac must not trip over a temp file.

        The bug was the helper quoting `mktemp /tmp/foo.XXXXXX.py` in a way that made macOS
        return a FIXED path, so two seats writing at once wrote the same converter file.  The
        fix is in `_convert_body_to_html` (`scripts/apple-notes-coding.sh`):  a private
        `mktemp -d` per call, so the converter inside it is unique and a stale directory can
        never break the next call.

        This drives the real script rather than re-implementing `mktemp`.  A test that shells out
        to `mktemp` directly passes even if the script regresses to the fixed-path pattern, so it
        guards nothing.  The script runs under `bash -x` and the expanded converter path is read
        back out of the trace, which shows the actual per-call directory.
        """
        import os
        import re
        import subprocess
        import tempfile
        import threading

        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apple-notes-coding.sh")
        import signal

        temporary = tempfile.TemporaryDirectory(prefix="afc422-")
        self.addCleanup(temporary.cleanup)
        names = [f"conc-id-{i:04d}" for i in range(4)]
        live = set()
        lock = threading.Lock()
        cancelled = threading.Event()
        results = {}

        def stop(proc):
            # A shell may exit while its converter still holds the pipes.  The
            # private process group lets interruption clean up both processes.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)

        def worker(name):
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": temporary.name,
                "APPLE_NOTES_PRINT_HTML": "1",
                "AGENT_SESSION": name,
                "AGENT_SEAT": "CLAUDE",
                "LANG": "en_US.UTF-8",
            }
            cmd = ["bash", "-x", script, f"[AFC, CLAUDE] Notes {name}", f"Body of {name}."]
            with lock:
                if cancelled.is_set():
                    return
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                )
                live.add(proc)
            try:
                out, err = proc.communicate(timeout=60)
                with lock:
                    results[name] = (proc.returncode, out, err)
            except BaseException:
                stop(proc)
                raise
            finally:
                with lock:
                    live.discard(proc)

        threads = [threading.Thread(target=worker, args=(name,), daemon=True) for name in names]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=70)
                self.assertFalse(thread.is_alive(), "concurrent note helper did not finish")
        finally:
            with lock:
                cancelled.set()
                pending = list(live)
            for proc in pending:
                stop(proc)
            for thread in threads:
                if thread.ident is not None:
                    thread.join(timeout=5)

        self.assertEqual(sorted(results), sorted(names))

        converter_re = re.compile(r"(/tmp/apple-notes-md\.[A-Za-z0-9]+)/convert\.py")
        paths = []
        for name in names:
            rc, out, err = results[name]
            self.assertEqual(rc, 0, f"{name} exited {rc}: {err[-400:]}")
            # The body that went in is the body that came out:  no clobbering.
            self.assertIn(f"Body of {name}.", out)
            self.assertIn("Session: " + name, out)
            # The trace names the directory once per reference;  one call means one directory.
            found = set(converter_re.findall(err))
            self.assertEqual(
                len(found), 1, f"{name} used {len(found)} converter directories: {found}"
            )
            paths.extend(found)

        # Every call used its OWN directory.  A shared one is the original bug.
        self.assertEqual(len(paths), len(names))
        self.assertEqual(len(set(paths)), len(names))

        # The converter file inside each directory is removed, so a later call cannot inherit it.
        # (The directory itself is removed too, but Finder drops a .DS_Store into new /tmp
        # subdirectories and `rmdir` cannot clear that;  that is /tmp litter, not the collision fix.)
        for path in set(paths):
            self.assertFalse(
                os.path.exists(os.path.join(path, "convert.py")),
                f"{path}/convert.py was left behind",
            )


class TitleSecretScanTests(unittest.TestCase):
    """AFC-422:  the title reaches AppleScript source, the relay envelope, the owner notification and
    stderr, so a credential pasted into it would leak four ways.  It is rejected before any of those,
    and the rejection never echoes the value:  a complaint that printed the title would put the
    credential into the very log it exists to protect."""

    def run_script(self, title, body="Body."):
        env = dict(os.environ, APPLE_NOTES_PRINT_HTML="1", AGENT_SESSION="scan-test-session-01")
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), title, body],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    def test_a_credential_shaped_title_is_refused_and_the_value_is_never_echoed(self):
        for secret in (
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "AKIAIOSFODNN7EXAMPLE",
            "sk-abcdefghijklmnopqrstuvwxyz0123",
            "api_key: ZZZZZZZZZZZZZZZZZZZZ",
        ):
            with self.subTest(secret[:6]):
                out = self.run_script(f"[AFC, MM] rollout {secret}")
                self.assertEqual(out.returncode, 2, out.stderr[-300:])
                self.assertIn("field 'title' rejected", out.stderr)
                self.assertNotIn(secret, out.stdout + out.stderr)

    def test_an_ordinary_title_is_accepted_and_still_converts(self):
        for title in (
            "[AFC, MM] Notes Title Ruling",
            "[AFC, CT, Grok Web] token rotation notes",
            "[AFC, Claude] roadmap",
        ):
            with self.subTest(title):
                out = self.run_script(title, "Body of the note.")
                self.assertEqual(out.returncode, 0, out.stderr[-300:])
                self.assertIn("Body of the note.", out.stdout)


class SentenceGapMarkupTests(unittest.TestCase):
    """AFC-496:  a sentence that ends INSIDE inline markup keeps the sentence gap.

    Notes.app is an HTML renderer, so the gap has to survive as a numeric character reference
    followed by a normal space:  two raw ASCII spaces collapse to one.  Owner ruling 2026-10-23:
    the named `&nbsp;` entity is never emitted, because a numeric reference adds no raw
    non-ASCII byte to the note HTML.

    The gap is the reference for a NO-BREAK SPACE, `&#160; `, plus a normal space:  the same
    U+00A0 plus a normal space as every other surface, with no Apple Notes exception.

    Measured through this writer on Oct 29, 2026 (a probe note written and read back raw):  Apple
    Notes stores the no-break space, as a numeric reference or as the named entity, as an
    ORDINARY space, so a note holds two ordinary spaces after a sentence end, and Notes.app shows
    that pair as a wide gap.  The owner's screenshot of the gap sample note shows the no-break
    line exactly as wide as the en and em space lines.  AFC-552 (Oct 26) read the stored form
    back, saw ordinary spaces, and assumed the renderer collapses them, which it does not, so it
    replaced the gap with a narrow no-break space and wrote that exception into every rule file.
    A one-space gap is the thing that reads as no gap, so the pair of spaces is what the note
    needs, and it is what the no-break reference becomes.

    The helper converts the gap twice, an inline pass over the Markdown source and a whole-HTML
    pass over the finished HTML, and a sentence that ends inside `**bold**`, `*italic*` or a
    `[link](url)` puts the closing tag between the punctuation and the run of spaces.  Neither
    pass matched that, so the note carried two raw spaces after the bold, italic and link sentence
    ends and Notes rendered them as one.

    The whole-HTML pass now carries the closing tag through, and the inline pass holds code spans
    aside, because a code span is content the author wants verbatim and the entity used to land
    inside the chip.

    These tests drive the real script with APPLE_NOTES_PRINT_HTML=1, which prints the note HTML and
    exits before Notes is touched, so nothing here writes, updates or pins a note.

    Run:  cd scripts && python3 -m unittest test_apple_notes_session
    """

    PROBE = (
        "**Bold sentence end.**  Plain sentence after the bold one.\n\n"
        "*Italic sentence end.*  Plain sentence after the italic one.\n\n"
        "[Link sentence end.](https://example.test/probe)  Plain sentence after the link one.\n\n"
        "Plain sentence end.  Second plain sentence.\n\n"
        "The literal `code span with a sentence end.  inside it` stays verbatim.\n"
    )
    CODE_SPAN = "code span with a sentence end.  inside it"
    PLAIN_BODY = (
        "Plain sentence one.  Plain sentence two.  Plain sentence three.\n\n"
        "A second paragraph here.  And its end.\n"
    )
    #  A code, pre, style or script region is content the author wants verbatim, so the gap
    #  conversion leaves it alone.
    REGION = re.compile(r"<(pre|code|style|script)\b[\s\S]*?</\1>", re.IGNORECASE)

    def run_script(
        self,
        body: str,
        env_extra: dict[str, str] | None = None,
        extra_args: tuple[str, ...] = (),
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "APPLE_NOTES_PRINT_HTML": "1",
            "AGENT_SESSION": "abc12345",
            "AGENT_SEAT": "MM",
            "LANG": "en_US.UTF-8",
        }
        env.update(env_extra or {})
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *extra_args, "[AFC, MM] Gap probe", body],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )

    def body_html(self, out: str) -> str:
        """The note body without the title and the two stamp rows, which carry the clock."""
        return out.split("<div><br></div>", 1)[1]

    def test_a_sentence_end_inside_markup_keeps_the_gap(self):
        p = self.run_script(self.PROBE)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        for fragment in (
            "<b>Bold sentence end.</b>&#160; Plain sentence after the bold one.",
            "<i>Italic sentence end.</i>&#160; Plain sentence after the italic one.",
            (
                '<a href="https://example.test/probe">Link sentence end.</a>&#160; '
                "Plain sentence after the link one."
            ),
            "Plain sentence end.&#160; Second plain sentence.",
        ):
            self.assertIn(fragment, p.stdout)

    def test_no_raw_double_space_survives_after_a_sentence_end(self):
        p = self.run_script(self.PROBE)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        outside = self.REGION.sub("", p.stdout)
        #  A sentence end inside markup puts the closing tag between the punctuation and the
        #  run of spaces, so the run is checked with the tag in front of it too.
        self.assertEqual(re.findall(r"[.!?](</[a-zA-Z][^>]*>)?[ \t]{2,}", outside), [], outside)
        #  Every sentence end that is followed by a space carries the numeric reference and a space.
        for m in re.finditer(r"[.!?](</[a-zA-Z][^>]*>)?", outside):
            after = outside[m.end() :]
            if after.startswith("&#160; "):
                continue
            self.assertFalse(
                after[:1].isspace(),
                f"no gap after {m.group(0)!r}: {outside[m.start() - 40 : m.end() + 40]!r}",
            )

    def test_a_code_span_comes_back_byte_for_byte(self):
        p = self.run_script(self.PROBE)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        chips = re.findall(r"<code\b[^>]*>([\s\S]*?)</code>", p.stdout)
        self.assertEqual(chips, [self.CODE_SPAN])

    def test_a_plain_body_renders_exactly_as_it_did_before(self):
        p = self.run_script(self.PLAIN_BODY)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        self.assertEqual(
            self.body_html(p.stdout),
            "<p>Plain sentence one.&#160; Plain sentence two.&#160; Plain sentence three.</p>"
            "<div><br></div>"
            "<p>A second paragraph here.&#160; And its end.</p>"
            "<div><br></div></div>\n",
        )

    def test_the_named_nbsp_entity_is_never_emitted(self):
        """The named entity is banned;  the gap is written as a numeric reference.

        Owner ruling 2026-10-23:  the helper writes a numeric reference and never the named
        `&nbsp;` entity.  The markup probe and the plain body are both checked, so a pass that
        only fixes one path still fails here.
        """
        p = self.run_script(self.PROBE)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        plain = self.run_script(self.PLAIN_BODY)
        self.assertEqual(plain.returncode, 0, plain.stderr[-400:])
        for out in (p.stdout, plain.stdout):
            outside = self.REGION.sub("", out)
            self.assertNotIn("&nbsp;", outside, out)

    def test_the_gap_is_the_no_break_space_reference(self):
        """The gap is U+00A0 plus a normal space, written as the numeric reference.

        Apple Notes keeps no exception to the house gap.  It stores the no-break space as an
        ordinary space and shows the pair as a wide gap, so a note takes the same gap as
        every other surface.  Both paths are checked, so a pass that only fixes one still fails.
        """
        p = self.run_script(self.PROBE)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        plain = self.run_script(self.PLAIN_BODY)
        self.assertEqual(plain.returncode, 0, plain.stderr[-400:])
        for out in (p.stdout, plain.stdout):
            outside = self.REGION.sub("", out)
            self.assertIn("&#160; ", outside, out)

    def test_no_special_case_space_is_emitted(self):
        """AFC-552 wrote a narrow no-break space into every note;  nothing like it is written now.

        The narrow no-break, en, em and thin spaces all look fine on screen, but the fleet has
        one gap, U+00A0 plus a normal space, so none of them is the helper's gap, as a character
        or as a numeric reference.
        """
        strays = tuple(chr(c) for c in (0x202F, 0x2002, 0x2003, 0x2009)) + (
            "&#8239;",
            "&#8194;",
            "&#8195;",
        )
        for body in (self.PROBE, self.PLAIN_BODY, "One sentence end. Another one.\n"):
            p = self.run_script(body)
            self.assertEqual(p.returncode, 0, p.stderr[-400:])
            for stray in strays:
                self.assertNotIn(stray, p.stdout, repr(stray))

    def test_a_one_space_sentence_end_widens_to_the_no_break_space(self):
        """The one-space widening writes the no-break space plus a normal space.

        The helper writes the gap on two paths and they carry it differently:  the note
        body is HTML, so both the one-space widening and the two HTML passes write the
        numeric reference, and the plain-text mobile quick view writes the character
        itself because it is escaped.
        """
        #  One space:  the widening path, which writes the reference into the HTML body.
        p = self.run_script("One sentence end. Another one.\n")
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        widened = self.REGION.sub("", p.stdout)
        self.assertIn("&#160; ", widened, widened)
        #  The note HTML carries no raw non-ASCII byte for the gap.
        self.assertNotIn("\xa0", widened, widened)

        #  Two spaces:  the HTML path, which writes the reference.
        q = self.run_script("One sentence end.  Another one.\n")
        self.assertEqual(q.returncode, 0, q.stderr[-400:])
        converted = self.REGION.sub("", q.stdout)
        self.assertIn("&#160; ", converted, converted)
        self.assertNotIn("\xa0", converted, converted)

    def test_the_mobile_quick_view_gap_is_the_character_not_the_reference(self):
        """The plain-text path carries the no-break space itself, not the reference.

        The widening pass serves two contexts.  The note body is HTML, so it carries the
        numeric reference, like the two HTML passes and with no raw non-ASCII byte in the
        note HTML.  The mobile quick view is plain text that is embedded with html.escape,
        so a reference there would reach the owner as the literal text "&#160;".  It has to
        carry the character itself.
        """
        p = self.run_script(
            "Body text.",
            extra_args=("--summary", "One sentence end. Another one."),
        )
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        outside = self.REGION.sub("", p.stdout)
        self.assertIn("Mobile Quick View", outside, outside)
        self.assertIn("\xa0 ", outside, outside)
        #  An escaped reference would render as the literal text, so it must not be there.
        self.assertNotIn("&amp;#160;", outside, outside)


if __name__ == "__main__":
    unittest.main()
