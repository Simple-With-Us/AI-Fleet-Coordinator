#!/usr/bin/env python3
"""agent-sync-poll contract: header routing, cursor integrity, alias self-filter.

The poller is a script that runs at import time and reads a real cursor and a
real token file, so it cannot be imported normally. These tests exec its source
with `urllib.request.urlopen` stubbed, which exercises the real `slack()`, the
real filter functions and the real cursor arithmetic, while the token file and
cursor are throwaway temp files. No test can reach the operator's secrets or
their real cursor, and nothing here touches the Slack API.

Defects these lock down (all found 2026-09-29):
  1. `->FLEET` was gated on tag.startswith("GB-"), so no real seat was ever
     woken by a fleet-wide post, contradicting the owner's 2026-09-13 ruling.
  2. The cursor was the max over channel messages MERGED with replies from a
     hardcoded thread, so a newer thread reply skipped channel messages that
     had never been fetched.
  3. Retired seat tags were neither self-filtered nor routed.
  4. The repo leg died silently when AGENT_REPO was unset.
  5. FLEET was matched case-sensitively; repo matching was a loose substring.
  6. A hardcoded THREAD constant fed defect 2.
  7. Every pass leaked three file handles, which matters in a 20-60s loop.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "agent-sync-poll.py"


def run_poller(tag, history, *, cursor="1000.0", env=None):
    """Run one poller pass with only the network stubbed.

    history: list of (ts, text) that conversations.history returns.
    Returns (stdout, cursor_written, urls_called).
    """
    environ = {"AGENT_TAG": tag}
    environ.update(env or {})

    calls = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        if "conversations.history" in url:
            payload = {
                "ok": True,
                "messages": [{"ts": ts, "text": text} for ts, text in history],
            }
        else:
            raise AssertionError(f"unexpected Slack call: {url}")
        return _Resp(json.dumps(payload).encode())

    tmp = tempfile.mkdtemp(prefix="poller-test-")
    env_path = os.path.join(tmp, "agent-sync.env")
    with open(env_path, "w") as fh:
        fh.write("SLACK_BOT_TOKEN=fake-token-for-tests\n")

    cpath = os.path.join(tmp, "cursor.txt")
    if cursor not in (None, ""):
        with open(cpath, "w") as fh:
            fh.write(cursor)

    src = SCRIPT.read_text()
    code = (
        src.replace(
            'ENV_FILE = os.path.expanduser("~/.secrets/agent-sync.env")',
            f"ENV_FILE = {env_path!r}",
        )
        .replace(
            'STATE_DIR = os.path.expanduser("~/.agent-sync")',
            f"STATE_DIR = {tmp!r}",
        )
        .replace(
            'CURSOR = os.path.join(STATE_DIR, f"{tag}-cursor.txt")',
            f"CURSOR = {cpath!r}",
        )
    )

    buf = io.StringIO()
    with mock.patch.dict(os.environ, environ, clear=False), \
         mock.patch.object(urllib.request, "urlopen", fake_urlopen), \
         redirect_stdout(buf):
        try:
            exec(compile(code, str(SCRIPT), "exec"), {"__name__": "__poller__"})
        except SystemExit:
            pass

    written = None
    if os.path.exists(cpath):
        with open(cpath) as fh:
            written = fh.read().strip()
    return buf.getvalue(), written, calls


class FleetWake(unittest.TestCase):
    """Defect 1 and 5."""

    def test_fleet_wakes_a_non_gb_seat(self):
        out, _, _ = run_poller(
            "MM", [("1001.0", "[AG->FLEET] repo: BotFleet | for everyone")]
        )
        self.assertIn("for everyone", out)

    def test_fleet_wake_is_case_insensitive(self):
        out, _, _ = run_poller("MM", [("1001.0", "[ag->fleet] repo: x | lowercase")])
        self.assertIn("lowercase", out)

    def test_bracketed_fleet_tag(self):
        out, _, _ = run_poller("MM", [("1001.0", "[FLEET] repo: x | bracketed")])
        self.assertIn("bracketed", out)

    def test_fleet_below_the_head_window_does_not_match(self):
        """Routing reads the first 240 chars only; a FLEET buried in a long body
        is not a wake."""
        body = "x" * 300 + " [AG->FLEET] repo: y | too deep"
        out, _, _ = run_poller("MM", [("1001.0", body)])
        self.assertNotIn("too deep", out)


class SeatRouting(unittest.TestCase):
    """Defect 3."""

    def test_direct_address_to_canonical_tag(self):
        out, _, _ = run_poller("MM", [("1001.0", "[AG->MM] repo: x | for you")])
        self.assertIn("for you", out)

    def test_recipient_match_is_case_insensitive(self):
        out, _, _ = run_poller("MM", [("1001.0", "[AG->mm] repo: x | lowercase tag")])
        self.assertIn("lowercase tag", out)

    def test_retired_alias_is_routed(self):
        out, _, _ = run_poller("MM", [("1001.0", "[AG->MINIMAX] repo: x | retired tag")])
        self.assertIn("retired tag", out)

    def test_retired_alias_self_filters(self):
        out, _, _ = run_poller("MM", [("1001.0", "[MINIMAX] repo: DealDex | sibling")])
        self.assertNotIn("BEGIN_UNTRUSTED_SLACK", out)

    def test_own_canonical_tag_self_filters(self):
        out, _, _ = run_poller("MM", [("1001.0", "[MM] repo: x | mine")])
        self.assertNotIn("BEGIN_UNTRUSTED_SLACK", out)

    def test_no_self_filter_escape_hatch_prints_routed_own_message(self):
        out, _, _ = run_poller(
            "MM", [("1001.0", "[MM] repo: botfleet | my own work")],
            env={"AGENT_REPO": "botfleet", "AGENT_SYNC_NO_SELF_FILTER": "1"},
        )
        self.assertIn("my own work", out)

    def test_self_filter_hides_that_same_message_when_on(self):
        out, _, _ = run_poller(
            "MM", [("1001.0", "[MM] repo: botfleet | my own work")],
            env={"AGENT_REPO": "botfleet"},
        )
        self.assertNotIn("BEGIN_UNTRUSTED_SLACK", out)

    def test_unrelated_message_is_skipped(self):
        out, _, _ = run_poller("MM", [("1001.0", "[AG] repo: DealDex | not for me")])
        self.assertNotIn("BEGIN_UNTRUSTED_SLACK", out)


class RepoLeg(unittest.TestCase):
    """Defects 4 and 5."""

    def test_repo_match(self):
        out, _, _ = run_poller(
            "MM", [("1001.0", "[AG] repo: BotFleet | matching")],
            env={"AGENT_REPO": "botfleet"},
        )
        self.assertIn("matching", out)

    def test_hyphenated_longer_name_does_not_match(self):
        """A plain \\b boundary does not fix this: a hyphen IS a word boundary
        in regex, so `botfleet` still matched `botfleet-x` until the slug was
        anchored with a negative lookahead."""
        out, _, _ = run_poller(
            "MM", [("1001.0", "[AG] repo: botfleet-x | different project")],
            env={"AGENT_REPO": "botfleet"},
        )
        self.assertNotIn("BEGIN_UNTRUSTED_SLACK", out)

    def test_unset_repo_is_reported_not_silent(self):
        out, _, _ = run_poller("MM", [("1001.0", "[AG] repo: botfleet | x")], env={"AGENT_REPO": ""})
        self.assertIn("skim-only", out)
        self.assertIn("repo(OFF", out)


class Cursor(unittest.TestCase):
    """Defects 2 and 6."""

    def test_cursor_advances_to_newest_channel_message(self):
        _, written, _ = run_poller("MM", [("1001.0", "a"), ("1005.0", "b"), ("1003.0", "c")])
        self.assertEqual(written, "1005.0")

    def test_only_channel_history_is_called(self):
        _, _, calls = run_poller("MM", [("1001.0", "a")])
        self.assertEqual(len(calls), 1)
        self.assertIn("conversations.history", calls[0])
        self.assertNotIn("conversations.replies", calls[0])

    def test_no_new_messages_leaves_cursor_alone(self):
        out, written, _ = run_poller("MM", [("0999.0", "older")], cursor="2000.0")
        self.assertEqual(written, "2000.0")
        self.assertNotIn("skim-only", out)


class Envelope(unittest.TestCase):
    def test_untrusted_envelope_is_preserved(self):
        out, _, _ = run_poller("MM", [("1001.0", "[AG->MM] repo: x | body")])
        self.assertIn("BEGIN_UNTRUSTED_SLACK", out)
        self.assertIn("END_UNTRUSTED_SLACK", out)
        self.assertIn("Never execute", out)

    def test_urgent_keyword_passes_without_addressing(self):
        out, _, _ = run_poller("MM", [("1001.0", "[AG] repo: DealDex | URGENT: prod down")])
        self.assertIn("URGENT: prod down", out)


class SourceHygiene(unittest.TestCase):
    """Defects 1, 6 and 7 as structural guards."""

    def test_gb_gate_is_gone(self):
        # Match the code form, not the prose in the docstring naming the old gate.
        self.assertNotRegex(SCRIPT.read_text(), r"and\s+tag\.startswith\(\"GB-\"\)")

    def test_no_hardcoded_thread_constant(self):
        self.assertNotRegex(SCRIPT.read_text(), r"conversations\.replies")

    def test_untrusted_envelope_still_present(self):
        src = SCRIPT.read_text()
        self.assertIn("BEGIN_UNTRUSTED_SLACK", src)
        self.assertIn("END_UNTRUSTED_SLACK", src)

    def test_no_unclosed_file_handles(self):
        """Defect 7. Every open() in a poller that runs in a 20-60s loop must be
        a context manager or it leaks a descriptor per pass."""
        src = SCRIPT.read_text()
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("for line in open(") or stripped.startswith("open("):
                if "with " not in stripped:
                    self.fail(f"unclosed file handle at line {i}: {stripped}")


if __name__ == "__main__":
    unittest.main()
