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

4.  Apple Notes takes the same gap as every other surface, so no live line gives a
    note body its own character, and no live line counts a second exception.
    AFC-552 (Oct 26, 2026) gave note bodies the narrow no-break space on the claim
    that the renderer collapses the ordinary spaces Apple Notes turns a no-break
    space into.  That was never checked on screen, and the owner's screenshot of
    the gap sample note (Oct 29, 2026) shows the no-break line as wide as the en
    and em lines, so the exception was withdrawn.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# FLEET-PROTOCOL.md is excluded because it is a Fleet-OPS mirror:  it is generated from
# the Fleet-OPS handbook, which carries the same fix, so this repo does not edit it.
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
    # The compliance inventory's AFC-P05 row states the mechanism for every
    # surface, so it states the exception with it.
    "docs/compliance/inventory.md",
    # The converter and its own test describe the entity mode, and each one names the
    # two chat panes as the surfaces that take it.  They are scanned here so the
    # withdrawn 2026-10-22 GitHub-entity split can never come back into them.  The
    # mode itself is still the right form for the two chat panes, which is why these
    # two files are scanned rather than forbidden.
    "scripts/sentence_gap_convert.pl",
    "scripts/test_sentence_gap_program.py",
)

# Dated rollout records whose body is intentionally preserved; the stale-prescription
# scan skips them wholesale because their tables are history, not live guidance.
HISTORY_FILES = ("docs/rollouts/2026-10-08-sentence-gap-chat-entity.md",)

# The packs whose own text states the rule.
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
    # The UI copy standard states the mechanism for product copy, so it names the
    # pane exceptions with it; line 264 is marked history-only for the stale scan.
    "FLEET-UI-COPY.md",
) + tuple(path for pack in RULE_STATING_PACKS for path in _pack_copies(pack))

# The files whose fenced block is what a reader pastes.  A rule inside the
# block reaches every app, so it has to carry the exception with it.
PASTED_BLOCK_FILES = ("docs/AGENTS-RECALL-SNIPPET.md",)

# Every file that states the rule states the one exception, the two chat panes, and
# none of them gives an Apple Note body a character of its own.  One list, so a
# rule-stating file can never be left out of the Apple Notes gate.
APPLE_NOTES_FILES = EXCEPTION_FILES

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

# A heading that starts a history block:  the lines under it record what was found
# or ruled that day, so they are not a prescription.
HISTORY_HEADING_RE = re.compile(r"^#{1,6}\s.*\bHistory\b", re.IGNORECASE)

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
# The check looks for an explicit exception phrase and not for a bare "exception".
EXCEPTION_PHRASE_RE = re.compile(
    r"(only exception|one exception|single exception)",
    re.IGNORECASE,
)

# AFC-552 gave an Apple Note body the narrow no-break space (U+202F), and the Oct 29,
# 2026 measurement withdrew it:  Apple Notes turns a no-break space into an ordinary
# space and still shows the pair as a wide gap.  No live line may name this mechanism
# for a note body.
APPLE_NOTES_FORBIDDEN = ("narrow no-break space", "&#8239;", "U+202F")

# A live line that counts a second exception, while the rule has one:  the two chat
# panes.  The first of two panes is not a second exception, so only the count words
# are matched.
SECOND_EXCEPTION_CLAIMS = re.compile(
    r"\btwo exceptions\b|\bsecond exception\b|\bsecond of two\b", re.IGNORECASE
)

# The phrase is a common one and it is used about other things entirely:  a pipeline
# step with one exception, a seat with one, a rule that is not this rule.  A line only
# makes a claim about this rule when it also speaks about this rule, so the scan looks
# for a term from this list on the same line before it calls the line out.  The list
# holds every way the rule is named in these files.
GAP_RULE_TERMS = (
    "U+00A0",
    "&nbsp;",
    "Apple Note",
    "Claude Code",
    "MiniMax Code",
    "sentence gap",
    "sentence-gap",
    "gap rule",
    "the gap",
    "chat pane",
    "no-break space",
)


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
            if EXCEPTION_PHRASE_RE.search(window):
                return True
    return False


def _gives_notes_its_own_mechanism(line: str) -> bool:
    """Whether a line gives an Apple Note body the withdrawn narrow no-break space.

    A line names it when it speaks about Apple Notes and carries any form of the
    character:  the words, the numeric reference or the code point.
    """
    return "Apple Note" in line and any(form in line for form in APPLE_NOTES_FORBIDDEN)


def _claims_a_second_exception(line: str) -> bool:
    """Whether a line counts a second exception to the gap rule.

    The phrase alone is not enough:  it is used about pipeline steps and other rules,
    and a file that mentions the gap once is not a file about the gap on every line.
    So the line has to name the rule as well.
    """
    if not SECOND_EXCEPTION_CLAIMS.search(line):
        return False
    folded = line.casefold()
    return any(term.casefold() in folded for term in GAP_RULE_TERMS)


def _live_lines(text: str) -> tuple[tuple[int, str], ...]:
    """The lines that prescribe a rule, not the lines that record a past ruling.

    A "History" heading starts a block that records what was found or ruled that
    day, so the lines under it are history and are skipped.  A line that carries
    its own history marker is skipped too.
    """
    live: list[tuple[int, str]] = []
    in_history = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            in_history = bool(HISTORY_HEADING_RE.match(stripped))
        if in_history or _is_history(line):
            continue
        live.append((number, line))
    return tuple(live)


class StaleGuidanceTests(unittest.TestCase):
    """No live line may prescribe the superseded 2026-10-22 split."""

    def test_no_live_line_prescribes_the_github_entity_split(self) -> None:
        problems = []
        for rel in STALE_RULE_FILES:
            if rel in HISTORY_FILES:
                continue
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


class AppleNotesTakesTheHouseGapTests(unittest.TestCase):
    """Apple Notes takes the same gap as every other surface.

    AFC-552 (Oct 26, 2026) gave note bodies the narrow no-break space, because a read-back
    showed Apple Notes turning a no-break space into an ordinary space and the renderer was
    assumed to collapse the pair.  The owner's screenshot of the gap sample note (Oct 29,
    2026) shows the no-break line as wide as the en and em lines, so the pair is a gap on
    screen, and the exception was withdrawn.  A read-back that shows two ordinary spaces
    after a sentence end is the correct stored form.
    """

    def test_no_file_gives_apple_notes_its_own_mechanism(self) -> None:
        """No live line names the narrow no-break space for an Apple Note body.

        The history blocks are skipped:  they record the AFC-552 ruling and its withdrawal.
        """
        problems = []
        for rel in APPLE_NOTES_FILES:
            for number, line in _live_lines(_read(rel)):
                if _gives_notes_its_own_mechanism(line):
                    problems.append("%s:%d\n    %s" % (rel, number, line.strip()[:200]))
        self.assertEqual(
            problems,
            [],
            "live lines that give Apple Notes its own gap character:\n" + "\n".join(problems),
        )

    def test_no_live_line_counts_a_second_exception(self) -> None:
        """A live line may not count two exceptions while the rule has one."""
        problems = []
        for rel in APPLE_NOTES_FILES:
            for number, line in _live_lines(_read(rel)):
                match = SECOND_EXCEPTION_CLAIMS.search(line)
                if match and _claims_a_second_exception(line):
                    problems.append(
                        "%s:%d  %r\n    %s" % (rel, number, match.group(0), line.strip()[:200])
                    )
        self.assertEqual(
            problems,
            [],
            "live lines that still count a second exception:\n" + "\n".join(problems),
        )

    def test_the_mechanism_table_says_notes_takes_the_house_gap(self) -> None:
        """The two files that carry the mechanism table give Notes the real U+00A0 row."""
        for rel in ("docs/fleet-skills/sentence-gap/SKILL.md", "docs/SENTENCE-GAP-PORTABLE-SKILL.md"):
            with self.subTest(rel=rel):
                rows = [
                    line
                    for line in _read(rel).splitlines()
                    if line.startswith("| **Apple Notes**")
                ]
                self.assertEqual(len(rows), 1, "%s has no single Apple Notes row" % rel)
                self.assertIn("U+00A0", rows[0])
                self.assertIn("&#160;", rows[0])
                for form in APPLE_NOTES_FORBIDDEN:
                    self.assertNotIn(form, rows[0])

    def test_the_scans_catch_a_real_claim(self) -> None:
        """The scans have to catch a real claim, not only skip a false one.

        A scan that skips everything passes every file, so each is proved both ways:  a line
        that gives Notes its own character, or counts a second exception, is caught, and a
        line about something else is not.
        """
        self.assertTrue(
            _gives_notes_its_own_mechanism(
                "An Apple Note body takes the narrow no-break space plus a normal space."
            ),
            "the scan does not catch a line that gives Notes its own character",
        )
        self.assertTrue(
            _gives_notes_its_own_mechanism("Apple Notes writes the reference &#8239; for a gap."),
            "the scan misses the numeric reference",
        )
        self.assertFalse(
            _gives_notes_its_own_mechanism(
                "An Apple Note body takes the same U+00A0 plus a normal space as every other surface."
            ),
            "the scan reads the house gap as an exception",
        )
        self.assertTrue(
            _claims_a_second_exception(
                "There are two exceptions to the sentence gap:  the chat panes and Apple Notes."
            ),
            "the scan does not catch a claim about the rule",
        )
        self.assertTrue(
            _claims_a_second_exception("Sentence gaps have TWO EXCEPTIONS:  THE CHAT PANES."),
            "the scan misses a claim whose rule name is not lower case",
        )
        self.assertFalse(
            _claims_a_second_exception(
                "GB-Plumber has two exceptions at step 4: see below the list."
            ),
            "the scan reads a pipeline step as a claim about the rule",
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
