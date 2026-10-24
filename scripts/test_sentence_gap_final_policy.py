#!/usr/bin/env python3
"""Tests for the FINAL owner sentence-gap policy (AFC-521).

Owner ruling 2026-10-23, final:  between sentences, a real U+00A0 (no-break space)
followed by a normal space, on every surface -- Zulip, GitHub titles, bodies and
comments, Linear, commit messages, repo docs and Markdown, skills, agent
instructions, UI copy, and chat replies.

The one exception:  the chat panes of Claude Code (the Code tab) and MiniMax Code
(the MiniMax Code pane) normalize a raw U+00A0 away, so a chat reply there uses the
`&nbsp;` entity plus a normal space, outside code spans.  The entity is never the
form anywhere else, and two plain ASCII spaces are never the rule.

The 2026-10-22 GitHub-entity split is superseded:  GitHub takes the real U+00A0
like every other surface, and no file may tell an agent to write two plain ASCII
spaces as the rule.

Two checks run here.

1.  No live line prescribes the superseded split:  GitHub text taking the
    `&nbsp;` entity, or two plain ASCII spaces as the rule.  A line that marks
    itself as superseded, or as history, records a ruling rather than prescribing
    one, so it is allowed to hold the old text.

2.  Every file that states the rule names the one exception, with both panes and
    the entity together, so the rule cannot be read as "never the entity" again,
    and no file may claim that every chat pane takes the real U+00A0.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The files that must not prescribe the superseded 2026-10-22 GitHub-entity split.
STALE_RULE_FILES = (
    "docs/ZULIP-SWITCH-PROMPT.md",
    "docs/grok-web/INSTRUCTIONS.md",
    "AGENT-SYNC.md",
)

# The files that state the rule, and therefore must carry the one exception.
EXCEPTION_FILES = (
    "docs/fleet-skills/sentence-gap/SKILL.md",
    "docs/fleet-skills/owner-copy/SKILL.md",
    "docs/SENTENCE-GAP-PORTABLE-SKILL.md",
    "docs/grok-web/INSTRUCTIONS.md",
    "docs/ZULIP-SWITCH-PROMPT.md",
    "AGENT-SYNC.md",
)

# The stale prescriptions, as they are written.  Each one is a live instruction a
# reader could follow:  GitHub takes the entity, or two plain ASCII spaces are the
# rule.  The windows are tight on purpose, so a file that names the entity for the
# two chat panes is not caught by them.
STALE_PATTERNS = (
    # GitHub takes the `&nbsp;` entity plus a space.
    re.compile(r"GitHub[^\n]{0,80}`&nbsp;` entity plus"),
    re.compile(r"`&nbsp;` entity plus[^\n]{0,60}GitHub"),
    re.compile(r"GitHub text takes `&nbsp;`"),
    re.compile(r"GitHub uses `&nbsp;`"),
    re.compile(r"on GitHub it is the form"),
    re.compile(r"the `&nbsp;` entity is for GitHub"),
    re.compile(r"GitHub body, run the same converter with the entity"),
    # Two plain ASCII spaces as the rule.
    re.compile(r"Two ASCII spaces after"),
    re.compile(r"Two literal ASCII spaces"),
)

# A blanket claim that every chat pane takes the real U+00A0.  The two panes below
# contradict it, so a file that says it has not absorbed the exception.
BLANKET_PANE_CLAIMS = (
    re.compile(r"chat replies in every pane"),
    re.compile(r"chat replies in any pane"),
)

# A line that marks itself as a record of a past ruling is not a prescription, so it
# is allowed to hold the old text.  The markers are phrases rather than the bare word
# "history":  the Zulip switch prompt says "your shell history" in a sentence about
# credentials, and that line carries the live rule.
HISTORY_MARKERS = (
    re.compile(r"superseded", re.IGNORECASE),
    re.compile(r"history only", re.IGNORECASE),
    re.compile(r"\bHistory\b"),
    re.compile(r"\bthe history\b", re.IGNORECASE),
    re.compile(r"\bin history\b", re.IGNORECASE),
    re.compile(r"history:", re.IGNORECASE),
)

# The exception has to be named as the only exception, not as one option among many.
ONLY_EXCEPTION_RE = re.compile(r"only exception|one exception|single exception", re.IGNORECASE)


def _read(rel: str) -> str:
    """The text of one file, with a clear failure when it is missing."""
    path = ROOT / rel
    if not path.is_file():
        raise AssertionError("%s is missing" % rel)
    return path.read_text(encoding="utf-8")


def _is_history(line: str) -> bool:
    """Whether a line records a ruling rather than prescribing one."""
    return any(marker.search(line) for marker in HISTORY_MARKERS)


def _states_the_exception(text: str) -> bool:
    """Whether the text names the entity next to both panes.

    The entity is mentioned in several places, and only the mention that sits with
    both pane names is the exception itself.
    """
    for match in re.finditer(re.escape("`&nbsp;`"), text):
        window = text[max(0, match.start() - 600) : match.end() + 600]
        if "Claude Code" in window and "MiniMax Code" in window:
            return True
    return False


class StaleGuidanceTests(unittest.TestCase):
    """No live line may prescribe the superseded 2026-10-22 split."""

    def test_no_live_line_prescribes_the_github_entity_split(self) -> None:
        problems = []
        for rel in STALE_RULE_FILES:
            for number, line in enumerate(_read(rel).splitlines(), start=1):
                if _is_history(line):
                    continue
                for pattern in STALE_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        problems.append(
                            "%s:%d  %r\n    %s" % (rel, number, match.group(0), line.strip()[:200])
                        )
        self.assertEqual(
            problems,
            [],
            "stale sentence-gap guidance:\n" + "\n".join(problems),
        )


class PaneExceptionTests(unittest.TestCase):
    """Every file that states the rule names the one exception."""

    def test_each_file_names_both_panes_and_the_entity_together(self) -> None:
        for rel in EXCEPTION_FILES:
            with self.subTest(rel=rel):
                text = _read(rel)
                self.assertIn(
                    "Claude Code", text, "%s does not name the Claude Code Code tab" % rel
                )
                self.assertIn(
                    "MiniMax Code", text, "%s does not name the MiniMax Code chat pane" % rel
                )
                self.assertIn("`&nbsp;`", text, "%s does not name the entity" % rel)
                self.assertIn("U+00A0", text, "%s does not state the real U+00A0 rule" % rel)
                self.assertTrue(
                    _states_the_exception(text),
                    "%s does not state the entity as the exception for the two panes" % rel,
                )
                self.assertTrue(
                    ONLY_EXCEPTION_RE.search(text),
                    "%s does not call the two panes the only exception" % rel,
                )

    def test_no_file_claims_every_chat_pane_takes_the_real_nbsp(self) -> None:
        problems = []
        for rel in EXCEPTION_FILES:
            for number, line in enumerate(_read(rel).splitlines(), start=1):
                if _is_history(line):
                    continue
                for pattern in BLANKET_PANE_CLAIMS:
                    match = pattern.search(line)
                    if match:
                        problems.append(
                            "%s:%d  %r\n    %s" % (rel, number, match.group(0), line.strip()[:200])
                        )
        self.assertEqual(
            problems,
            [],
            "blanket chat-pane claims that the two exception panes contradict:\n"
            + "\n".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
