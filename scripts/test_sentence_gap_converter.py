#!/usr/bin/env python3
"""Tests for the fleet's sentence-gap converter, read from the one home.

The converter is a perl one-liner that lives in docs/fleet-skills/sentence-gap/SKILL.md
and is copied into every rendered seat tree and into the fleet's own guidance.  A copy
that drifts from the one home is a body that ships with no gap, and a gap that is
missing is invisible in the rendered text, so the pattern is pinned here rather than
retyped.

Two things are checked.  First, the shape:  the pattern matches one *or more* spaces,
carries the lookbehind that keeps a numbered list a list, and carries the markup group
that catches a sentence end inside a markup span.  Second, the behaviour:  the pattern
the one home actually holds is run over a body that holds every shape, and each one is
either gapped or deliberately left alone.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONE_HOME = ROOT / "docs" / "fleet-skills" / "sentence-gap" / "SKILL.md"

# The pattern a converter is written as:  a perl substitution with a sentence-end group.
_PATTERN_RE = re.compile(r"perl -CSDA -pe '(s/.*?/g)'")

# The markup group:  the characters that may sit between a sentence end and the space.
_MARKUP_GROUP = r"""([*_`)\]"]*)"""

# The markup group as the POSIX class the assertions write:  the ] comes first, because a
# POSIX bracket expression ends at the first unquoted ].
_ASSERT_MARKUP_GROUP = "[]*_"

# The assertion patterns, extracted from the text that holds them.  A count check and a
# leftover check, each in the shape the fleet writes it.
_COUNT_RE = re.compile(r"grep -o\s+\$'([^']*)'")
_LEFTOVER_RE = re.compile(r"grep -cE\s+'([^']*)'")

# The check the fleet used to write, before the markup group.  It cannot see a gap that
# sits after a markup span, which is the defect these tests exist to hold shut.
_BLIND_LEFTOVER = r"""[^0-9][.!?] +[A-Z"]"""

# Files that hold a dated record of a ruling and keep the form that ruling was written
# in.  The live procedure is in the one home;  a record is not a copy of it.
DATED_RECORDS = ("docs/rollouts/2026-10-08-sentence-gap-chat-entity.md",)

# Generated mirrors.  These are built from Fleet-OPS handbook/ and this repository cannot
# change them:  scripts/check-generated-mirror.py refuses any edit from a branch that is
# not handbook-sync/mirror, so the pattern reaches one only when the upstream handbook page
# changes and the sync job rewrites the mirror.  The checkout-wide sweep below skips them
# for the same reason DATED_RECORDS is skipped -- not a copy this repository may edit --
# and check-generated-mirror.py is the gate that holds them honest in the meantime.
GENERATED_MIRRORS = ("AGENT-SYNC.md",)

# This file holds the checks themselves, so the sweep below finds its own command
# templates.  A checker is not guidance.
CHECKER_FILES = ("scripts/test_sentence_gap_converter.py",)

# The rendered copies of the one home, and the fleet's own guidance, all carry the same
# pattern.  The one home is the source;  the rest are checked against it.
RENDERED_COPIES = (
    "skills/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/ag/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/claude/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/clutch/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/codex/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/cursor/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/deepseek/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/fx/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/grok-bot/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/grok-build/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/grok/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/kimi/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/minimax/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/monet/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/muse-assist/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/muse-code/sentence-gap/SKILL.md",
    "docs/fleet-skills/by-seat/renoir/sentence-gap/SKILL.md",
    ".claude/skills/sentence-gap/SKILL.md",
    ".clutch/dsh/skills/sentence-gap/SKILL.md",
    ".codex/skills/sentence-gap/SKILL.md",
    ".config/muse/skills/sentence-gap/SKILL.md",
    ".cursor/rules/sentence-gap.mdc",
    ".cursor/skills/sentence-gap/SKILL.md",
    ".fx/skills/sentence-gap/SKILL.md",
    ".gemini/skills/sentence-gap/SKILL.md",
    ".grok/skills/sentence-gap/SKILL.md",
    ".minimax/skills/sentence-gap/SKILL.md",
)

# The live guidance that quotes the pattern for a reader to copy.
GUIDANCE = (
    "FLEET-UI-COPY.md",
    "docs/SENTENCE-GAP-PORTABLE-SKILL.md",
    "docs/ZULIP-SWITCH-PROMPT.md",
    "docs/protocols/zulip-fleet-guide.md",
    "scripts/pr_sweep/rules.md",
)

# The shapes the converter has to get right.  Each is (body line, gapped?) and the gapped
# ones are the ones a body can hold without anyone noticing the missing gap in the render.
SHAPES = (
    ("Plain sentence.  Next sentence.", True),
    ("**Bold sentence.**  Next sentence.", True),
    ("*Italic sentence.*  Next sentence.", True),
    ("`Code sentence.`  Next sentence.", True),
    ("(**Paren sentence.**)  Next sentence.", True),
    ('He said "Quoted sentence."  Next sentence.', True),
    ("1. Numbered marker stays a marker.", False),
    ("The answer is 42. Next thing.", False),
    ("e.g. this keeps its plain space.", False),
)

NBSP = " "

# The converter runs through perl, because that is how the fleet produces the gap.
HAVE_PERL = shutil.which("perl") is not None
HAVE_BASH = shutil.which("bash") is not None


def assertion_patterns(text: str) -> tuple[str, str]:
    """The count pattern and the leftover pattern a text holds."""
    count = _COUNT_RE.search(text)
    leftover = _LEFTOVER_RE.search(text)
    return (
        count.group(1) if count else "",
        leftover.group(1) if leftover else "",
    )


def run_grep(command: str) -> int:
    """Run one grep the way the fleet runs it, and return the number it printed."""
    proc = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise AssertionError("command failed: %s\n%s" % (command, proc.stderr.strip()))
    out = proc.stdout.strip()
    return int(out) if out else 0


def patterns_in(text: str) -> list[str]:
    """Every converter pattern a text holds, in the order it holds them."""
    return _PATTERN_RE.findall(text)


def one_home_patterns() -> list[str]:
    """The patterns the one home holds.  It holds the canonical one, and the lookbehind
    variant it explains."""
    with open(ONE_HOME, encoding="utf-8") as fh:
        return patterns_in(fh.read())


def convert(pattern: str, body: str) -> str:
    """Run one converter pattern over a body, the way the skill says to.  A hung perl
    exits 124 and a missing perl exits 127, so neither leaves the runner waiting."""
    try:
        proc = subprocess.run(
            ["perl", "-CSDA", "-pe", pattern],
            input=body,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(124) from exc
    except FileNotFoundError as exc:
        raise SystemExit(127) from exc
    if proc.returncode != 0:
        raise AssertionError("perl failed: %s" % proc.stderr.strip())
    return proc.stdout


def tracked_files() -> list[Path]:
    """Every tracked file in the checkout that can hold guidance."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    files = []
    for line in out.stdout.splitlines():
        path = ROOT / line
        if path.suffix in (".md", ".py", ".mdc") and path.is_file():
            files.append(path)
    return files


@unittest.skipUnless(HAVE_PERL, "perl is not on PATH")
class ConverterShapeTests(unittest.TestCase):
    """The pattern the one home holds has the three parts that make it work."""

    def test_the_one_home_holds_a_converter(self) -> None:
        self.assertTrue(one_home_patterns(), "the one home holds no converter pattern")

    def test_the_pattern_matches_one_or_more_spaces(self) -> None:
        for pattern in one_home_patterns():
            self.assertIn(
                ") +(",
                pattern,
                "the pattern must match one or more spaces:  %s" % pattern,
            )
            self.assertNotIn(
                ")  (",
                pattern,
                "the pattern must not require exactly two spaces:  %s" % pattern,
            )

    def test_the_pattern_keeps_the_numbered_list_lookbehind(self) -> None:
        for pattern in one_home_patterns():
            self.assertIn(
                "(?<![0-9])",
                pattern,
                "the lookbehind keeps a numbered marker a marker:  %s" % pattern,
            )

    def test_the_pattern_carries_the_markup_group(self) -> None:
        for pattern in one_home_patterns():
            self.assertIn(
                _MARKUP_GROUP,
                pattern,
                "the markup group catches a sentence end inside a span:  %s" % pattern,
            )


@unittest.skipUnless(HAVE_PERL, "perl is not on PATH")
class ConverterBehaviourTests(unittest.TestCase):
    """The pattern the one home holds converts every shape it has to convert."""

    def setUp(self) -> None:
        self.pattern = one_home_patterns()[0]

    def test_every_shape_is_gapped_or_deliberately_left_alone(self) -> None:
        for line, gapped in SHAPES:
            with self.subTest(line=line):
                out = convert(self.pattern, line + "\n")
                self.assertEqual(
                    (NBSP in out),
                    gapped,
                    "expected %s, got %r" % ("a gap" if gapped else "no gap", out),
                )

    def test_the_gap_keeps_the_markup_span_intact(self) -> None:
        out = convert(self.pattern, "**Bold sentence.**  Next sentence.\n")
        self.assertIn("**Bold sentence.**" + NBSP + " Next sentence.", out)

    def test_a_collapsed_body_is_converted_too(self) -> None:
        """A run of whitespace sometimes collapses on the way to disk, so the pattern
        has to be right for a single space as well as for two."""
        out = convert(self.pattern, "**Bold sentence.** Next sentence.\n")
        self.assertIn("**Bold sentence.**" + NBSP + " Next sentence.", out)

    def test_a_body_with_no_gaps_at_all_still_converts(self) -> None:
        out = convert(self.pattern, "First.  Second.  Third.\n")
        self.assertEqual(out.count(NBSP), 2)


class OneSourceOfTruthTests(unittest.TestCase):
    """The one home is the source, and every copy carries the same pattern."""

    def test_every_rendered_copy_carries_the_one_home_pattern(self) -> None:
        canonical = one_home_patterns()[0]
        for rel in RENDERED_COPIES:
            path = ROOT / rel
            self.assertTrue(path.is_file(), "%s is missing" % rel)
            with open(path, encoding="utf-8") as fh:
                found = patterns_in(fh.read())
            self.assertIn(
                canonical,
                found,
                "%s does not carry the one home's converter" % rel,
            )

    def test_every_live_guidance_file_carries_the_markup_group(self) -> None:
        for rel in GUIDANCE:
            path = ROOT / rel
            self.assertTrue(path.is_file(), "%s is missing" % rel)
            with open(path, encoding="utf-8") as fh:
                found = patterns_in(fh.read())
            self.assertTrue(found, "%s holds no converter pattern" % rel)
            for pattern in found:
                self.assertIn(
                    _MARKUP_GROUP,
                    pattern,
                    "%s holds a converter without the markup group:  %s" % (rel, pattern),
                )

    def test_no_live_file_holds_a_converter_without_the_markup_group(self) -> None:
        """A copy that drifts is a body that ships with no gap, so every converter in the
        checkout carries the markup group.  A dated record of a ruling keeps the form it
        was written in and is listed as such, and a generated mirror is built elsewhere and
        cannot be edited here."""
        for path in tracked_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel in DATED_RECORDS or rel in GENERATED_MIRRORS or rel in CHECKER_FILES:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in patterns_in(text):
                self.assertIn(
                    _MARKUP_GROUP,
                    pattern,
                    "%s holds a converter without the markup group:  %s" % (rel, pattern),
                )


@unittest.skipUnless(HAVE_PERL and HAVE_BASH, "perl or bash is not on PATH")
class AssertionCommandTests(unittest.TestCase):
    """The assertion commands have to see the gaps the converter makes.

    The checks the fleet wrote before the markup group could not see a gap that sits
    after a markup span:  the converter emits that gap as
    <punctuation><markup><U+00A0><space>, so a check that looks for the U+00A0 straight
    after the punctuation does not find it, and a check that looks for a space straight
    after the punctuation does not find the one that was missed either.  A body with
    that shape passed both checks and shipped with no gap, so the group is in both
    commands now, and these tests hold it there.
    """

    def setUp(self) -> None:
        with open(ONE_HOME, encoding="utf-8") as fh:
            text = fh.read()
        self.count_pattern, self.leftover_pattern = assertion_patterns(text)
        self.pattern = one_home_patterns()[0]

    def _count_command(self, path: Path) -> str:
        return "grep -o $'%s' '%s' | wc -l" % (self.count_pattern, path)

    def _leftover_command(self, path: Path) -> str:
        return "grep -cE '%s' '%s'" % (self.leftover_pattern, path)

    def _write(self, tmp: str, body: str) -> Path:
        path = Path(tmp) / "body.nbsp.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def test_the_one_home_holds_both_assertions(self) -> None:
        self.assertTrue(self.count_pattern, "the one home holds no count assertion")
        self.assertTrue(self.leftover_pattern, "the one home holds no leftover assertion")

    def test_both_assertions_carry_the_markup_group(self) -> None:
        self.assertIn(
            _ASSERT_MARKUP_GROUP,
            self.count_pattern,
            "the count check must carry the markup group:  %s" % self.count_pattern,
        )
        self.assertIn(
            _ASSERT_MARKUP_GROUP,
            self.leftover_pattern,
            "the leftover check must carry the markup group:  %s" % self.leftover_pattern,
        )

    def test_a_converted_body_passes_both_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, convert(self.pattern, "**Bold sentence.**  Next sentence.\n"))
            count = run_grep(self._count_command(path))
            leftover = run_grep(self._leftover_command(path))
        self.assertGreater(count, 0, "the count check must not print 0")
        self.assertEqual(leftover, 0, "a converted body must carry no leftover gap")

    def test_the_leftover_check_sees_a_gap_the_blind_check_cannot(self) -> None:
        """This is the shape the old checks missed, and the reason the group is in them."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "**Bold sentence.** Next sentence.\n")
            leftover = run_grep(self._leftover_command(path))
            blind = run_grep(
                "grep -cE '%s' '%s'" % (_BLIND_LEFTOVER, path),
            )
        self.assertGreater(leftover, 0, "the markup-aware check must catch this gap")
        self.assertEqual(blind, 0, "the blind check is expected to miss it -- that is the defect")

    def test_every_live_guidance_file_holds_a_markup_aware_leftover_check(self) -> None:
        for rel in GUIDANCE:
            path = ROOT / rel
            self.assertTrue(path.is_file(), "%s is missing" % rel)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            count, leftover = assertion_patterns(text)
            self.assertTrue(leftover, "%s holds no leftover assertion" % rel)
            self.assertIn(
                _ASSERT_MARKUP_GROUP,
                leftover,
                "%s holds a blind leftover check:  %s" % (rel, leftover),
            )
            self.assertIn(
                _ASSERT_MARKUP_GROUP,
                count,
                "%s holds a blind count check:  %s" % (rel, count),
            )

    def test_no_live_file_holds_a_blind_leftover_check(self) -> None:
        """No live file may hold the check that cannot see a gap after a markup span.
        A dated record of a ruling keeps the form it was written in and is skipped."""
        for path in tracked_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel in DATED_RECORDS or rel in GENERATED_MIRRORS or rel in CHECKER_FILES:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in _LEFTOVER_RE.findall(text):
                self.assertIn(
                    _ASSERT_MARKUP_GROUP,
                    pattern,
                    "%s holds a blind leftover check:  %s" % (rel, pattern),
                )


if __name__ == "__main__":
    unittest.main()
