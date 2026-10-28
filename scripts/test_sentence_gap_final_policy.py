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

Three checks run here.

1.  No live line prescribes the superseded split:  GitHub text taking the
    `&nbsp;` entity, or two plain ASCII spaces as the rule.  A line that marks
    itself as superseded, or as history, records a ruling rather than prescribing
    one, so it is allowed to hold the old text.

2.  Every file that states the rule names the one exception, with both panes and
    the entity together, so the rule cannot be read as "never the entity" again,
    and no file may claim that every chat pane takes the real U+00A0.

3.  The published note takes a real U+00A0:  `scripts/fleet_facts/publish.py`
    writes its gap to GitHub, so the GAP constant is the real U+00A0 plus a
    space, never the entity (owner ruling 2026-10-23).
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# FLEET-PROTOCOL.md is excluded because it is a Fleet-OPS mirror:  it is generated from
# the Fleet-OPS handbook, which carries the same fix, so this repo does not edit it.
# FLEET-UI-COPY.md is excluded because it is owned by open PR #904, which carries the
# same fix there; editing it here would collide with that pull request.
# The Cursor rule is not listed here:  like the canonical sentence-gap skill it keeps
# the 2026-08-19/20 findings block, which records what was found that day rather than
# prescribing a rule, so the stale-split scan would read that block as a prescription.
# It is covered by EXCEPTION_FILES below, which is the check that matters for it.
# The files that must not prescribe the superseded 2026-10-22 GitHub-entity split.
STALE_RULE_FILES = (
    "docs/ZULIP-SWITCH-PROMPT.md",
    "docs/grok-web/INSTRUCTIONS.md",
    "docs/protocols/zulip-fleet-guide.md",
    "docs/fleet-skills/fleet-coordination/SKILL.md",
    "skills/fleet-coordination/SKILL.md",
    # The seat template every other fleet repo copies.  It held the one-mechanism
    # wording with no pane exception, so a seat that read only the template never
    # learned that its own chat pane takes the entity.  Owner ruling 2026-10-25.
    "TEMPLATE-AGENTS.md",
    # The onboarding prompts and the skill catalog each state the rule to a seat
    # that is reading them for the first time, so each has to name the exception.
    "docs/INSTINCT-ONBOARDING-PROMPT.md",
    "docs/OPENCODE-ONBOARDING-PROMPT.md",
    "docs/fleet-skills/README-add-in-app.md",
    # The dated rollout record for the 2026-10-08 ruling.  It carries the Oct 8
    # per-surface table as a record, under a superseded note that names the
    # current rule and points at the live table, so it states the rule and has
    # to carry the exception.
    "docs/rollouts/2026-10-08-sentence-gap-chat-entity.md",
    # The compliance inventory's AFC-P05 row states the mechanism for every
    # surface, so it states the exception with it.
    "docs/compliance/inventory.md",
)

# The packs whose own text states the rule.  The installer writes a copy of each
# one into skills/ and a per-seat copy into docs/fleet-skills/by-seat/, so the
# gate covers every copy through a glob instead of a hand-kept list of paths.
RULE_STATING_PACKS = ("fleet-coordination", "land-lane", "owner-copy", "sentence-gap")


def _pack_copies(pack: str) -> tuple[str, ...]:
    """Every copy of one rule-stating pack that this repository holds."""
    paths = [
        ROOT / "docs" / "fleet-skills" / pack / "SKILL.md",
        ROOT / "skills" / pack / "SKILL.md",
    ]
    paths += sorted(ROOT.glob("docs/fleet-skills/by-seat/*/%s/SKILL.md" % pack))
    return tuple(str(p.relative_to(ROOT)) for p in paths if p.is_file())


# The files that state the rule, and therefore must carry the pane exception.
# The Cursor rule and the Zulip guide all state the rule, so all of them must
# name the pane exception.
EXCEPTION_FILES = (
    "docs/SENTENCE-GAP-PORTABLE-SKILL.md",
    "docs/grok-web/INSTRUCTIONS.md",
    "docs/ZULIP-SWITCH-PROMPT.md",
    ".cursor/rules/sentence-gap.mdc",
    "docs/protocols/zulip-fleet-guide.md",
    # The seat template states the rule too, so it states the exception.
    "TEMPLATE-AGENTS.md",
    # The recall snippet is pasted into every app's instruction file, and the
    # onboarding prompts are the first instruction file a new seat reads.
    "docs/AGENTS-RECALL-SNIPPET.md",
    "docs/INSTINCT-ONBOARDING-PROMPT.md",
    "docs/OPENCODE-ONBOARDING-PROMPT.md",
    # The skill catalog row is how a seat finds the rule.
    "docs/fleet-skills/README-add-in-app.md",
    # The shared rules part renders into every platform's own rules file, so a
    # stale line here reaches every seat's prompt.
    "scripts/fleet_lanes/rules/fleet-common.md",
    # The rules the pr_sweep agent runs on, and the packs that state the rule
    # for a bot or a routine:  one mechanism everywhere, so the two chat panes
    # have to be named or a seat that reads only these writes the wrong gap.
    "scripts/pr_sweep/rules.md",
    "docs/grok-web/skills/grok-web-work-items/SKILL.md",
    "docs/grok-web/README.md",
    "docs/HOUSEKEEPER.md",
    "docs/open-loops/README.md",
    # The status log carries the rule as it stands, next to the dated entries
    # that record how it stood before.
    "STATUS.md",
    # The dated rollout record for the 2026-10-08 ruling, and the compliance
    # inventory's AFC-P05 row.  Both state the mechanism for every surface, so
    # both name the pane exception with it.
    "docs/rollouts/2026-10-08-sentence-gap-chat-entity.md",
    "docs/compliance/inventory.md",
) + tuple(path for pack in RULE_STATING_PACKS for path in _pack_copies(pack))

# The files whose fenced block is what a reader pastes.  A rule inside the
# block reaches every app, so it has to carry the exception with it.
PASTED_BLOCK_FILES = ("docs/AGENTS-RECALL-SNIPPET.md",)

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
    # A changelog row records a past ruling, so it may hold the old text:  the
    # guide's v4.7 row is the 2026-10-22 per-surface ruling, superseded since.
    re.compile(r"^\| v\d"),
)

# The panes have to be called an exception, not listed as one option among many.
# The wording moved with AFC-552:  there are two exceptions now, so a file may
# say "two exceptions" where it used to say "the one exception", so the check
# looks for the word and not for a count.
EXCEPTION_WORD_RE = re.compile(r"exception", re.IGNORECASE)


def _read(rel: str) -> str:
    """The text of one file, with a clear failure when it is missing."""
    path = ROOT / rel
    if not path.is_file():
        raise AssertionError("%s is missing" % rel)
    return path.read_text(encoding="utf-8")


def _fenced_blocks(text: str) -> tuple[str, ...]:
    """The fenced blocks in a file, which are the parts a reader pastes."""
    return tuple(re.findall(r"^```[^\n]*\n.*?^```", text, re.MULTILINE | re.DOTALL))


def _is_history(line: str) -> bool:
    """Whether a line records a ruling rather than prescribing one."""
    return any(marker.search(line) for marker in HISTORY_MARKERS)


def _states_the_exception(text: str) -> bool:
    """Whether the text calls the entity the exception for both panes.

    The entity is mentioned in several places, and only the mention that sits with
    both pane names is the exception itself.  The word "exception" has to sit in
    the same window, so the panes are called an exception rather than listed as
    one option among many.
    """
    for match in re.finditer(re.escape("`&nbsp;`"), text):
        window = text[max(0, match.start() - 600) : match.end() + 600]
        if "Claude Code" in window and "MiniMax Code" in window:
            if EXCEPTION_WORD_RE.search(window):
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
    """Every file that states the rule names the pane exception."""

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

    def test_a_pasted_block_states_no_rule_without_the_exception(self) -> None:
        """A pasted block that states the gap rule must name the two panes.

        The note above the block is for whoever pastes it, so the block is the
        text that reaches every app.  If it ever states the rule, it has to
        carry the exception with it.
        """
        for rel in PASTED_BLOCK_FILES:
            for block in _fenced_blocks(_read(rel)):
                if "U+00A0" not in block and "&nbsp;" not in block:
                    continue
                self.assertTrue(
                    _states_the_exception(block),
                    "%s has a pasted block that states the gap rule without the pane exception"
                    % rel,
                )


class PublishedGapTests(unittest.TestCase):
    """The published note takes a real U+00A0, GitHub included."""

    PUBLISH = "scripts/fleet_facts/publish.py"

    def test_publish_gap_is_the_real_nbsp_plus_a_space(self) -> None:
        text = _read(self.PUBLISH)
        match = re.search(r"^GAP = (.*)$", text, re.MULTILINE)
        self.assertIsNotNone(match, "%s has no GAP constant" % self.PUBLISH)
        value = ast.literal_eval(match.group(1).split("#")[0].strip())
        self.assertEqual(
            value,
            "\u00a0 ",
            "the published gap is a real U+00A0 plus a normal space, never the entity",
        )


if __name__ == "__main__":
    unittest.main()
