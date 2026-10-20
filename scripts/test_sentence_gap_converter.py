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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONE_HOME = ROOT / "docs" / "fleet-skills" / "sentence-gap" / "SKILL.md"

# The pattern a converter is written as:  a perl substitution with a sentence-end group.
_PATTERN_RE = re.compile(r"perl -CSDA -pe '(s/.*?/g)'")

# The markup group:  the characters that may sit between a sentence end and the space.
_MARKUP_GROUP = r"""([*_`)\]"]*)"""

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
            if rel in DATED_RECORDS or rel in GENERATED_MIRRORS:
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


if __name__ == "__main__":
    unittest.main()
