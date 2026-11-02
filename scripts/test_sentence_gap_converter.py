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

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ONE_HOME = ROOT / "docs" / "fleet-skills" / "sentence-gap" / "SKILL.md"

# The pattern a converter is written as:  a perl substitution with a sentence-end group.
_PATTERN_RE = re.compile(r"perl -CSDA -pe '(s/.*?/g)'")

# Follow-on substitutions that run on a body that is already converted and create no
# gap of their own, so they are not converters and carry no markup group.  Each one is
# named exactly:  any other substitution is still counted as a converter and asserted,
# so a gap-writer spelled some new way cannot slip past the markup-group checks.
_NON_GAP_STEPS = frozenset(
    {
        # Apple Notes:  turn an existing NBSP gap into a narrow NBSP.
        r"s/\x{a0} /\x{202f} /g",
    }
)

# The markup group:  the characters that may sit between a sentence end and the space.
_MARKUP_GROUP = r'((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*)'

# The markup group as the POSIX class the assertions write:  the ] comes first, because a
# POSIX bracket expression ends at the first unquoted ].  The straight quote is written
# \' for bash, which turns it into a plain ' before grep sees the pattern --  grep must
# see a plain ', because a backslash inside a bracket expression is a literal member.
_ASSERT_MARKUP_GROUP = "[]*_`~)\"\\'’”]"

# The assertion patterns, extracted from the text that holds them.  A count check and a
# leftover check, each in the shape the fleet writes it.  Both are written in bash's
# ANSI-C form, because that is the only form that can carry a straight quote, and the
# class needs one -- so the extraction has to read the escaped quote instead of stopping
# at it.
_COUNT_RE = re.compile(r"grep -oE?\s+\$'(?P<ansi>(?:[^'\\]|\\.)*)'")
_LEFTOVER_RE = re.compile(r"grep -cE\s+(?:\$'(?P<ansi>(?:[^'\\]|\\.)*)'|'(?P<plain>[^']*)')")

# The mirror ships its leftover check in the plain, non -E form, so the shared extraction
# does not see it.  This one takes both forms, and is used for the mirrors alone:  the
# plain form matches unrelated commands elsewhere in the checkout.
_MIRROR_LEFTOVER_RE = re.compile(
    r"grep -c[E]?\s+(?:\$'(?P<ansi>(?:[^'\\]|\\.)*)'|'(?P<plain>[^']*)')"
)

# The check the fleet used to write, before the markup group.  It cannot see a gap that
# sits after a markup span, which is the defect these tests exist to hold shut.
_BLIND_LEFTOVER = r"""[^0-9][.!?] +[A-Z"]"""

# The check the fleet wrote before the quotes joined the class.  It cannot see a missed
# gap in a quoted sentence, which is the defect the widened class exists to close.
_QUOTE_BLIND_LEFTOVER = r'[^0-9][.!?][]*_`)"]* +[A-Z"]'

# Files that hold a dated record of a ruling and keep the form that ruling was written
# in.  The live procedure is in the one home;  a record is not a copy of it.
DATED_RECORDS = ("docs/rollouts/2026-10-08-sentence-gap-chat-entity.md",)

# Generated mirrors.  These are built from Fleet-OPS handbook/ and this repository cannot
# change them:  scripts/check-generated-mirror.py refuses any edit from a branch that is
# not handbook-sync/mirror, so the pattern reaches one only when the upstream handbook page
# changes and the sync job rewrites the mirror.  The checkout-wide sweep below skips them
# for the same reason DATED_RECORDS is skipped -- not a copy this repository may edit --
# and check-generated-mirror.py is the gate that holds their bytes honest in the meantime.
#
# The mirror is still the file every seat reads first, so it does not stay a blind
# exemption:  GeneratedMirrorTests below asserts every mirror this registry names, so a
# mirror added here is asserted too and not silently left unchecked.
# The document is being renamed:  both names are generated mirrors, and exactly one exists at a time.
# Remove the old name when the rename is complete.
GENERATED_MIRRORS = ("FLEET-PROTOCOL.md",)

# This file holds the checks themselves, so the sweep below finds its own command
# templates.  A checker is not guidance.
CHECKER_FILES = ("scripts/test_sentence_gap_converter.py",)

# The rendered copies of the one home, and the fleet's own guidance, all carry the same
# pattern.  The one home is the source;  the rest are checked against it.
#
# The list is derived from the seat registry rather than named by hand, so a new seat
# cannot be forgotten.  Catalog packs, including retired seats, remain generated
# outputs; only active seats receive platform and tool-home installations.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_skill_identity as _identity


def _active_by_seat_copies() -> tuple[str, ...]:
    """The by-seat upload packs of the one home, for the seats that are still live.

    The path is derived the way the installer derives it, `seat_key or tag.lower()`,
    so a seat added without an explicit `seat_key` is found under the name the
    installer wrote it to.
    """
    return tuple(
        "docs/fleet-skills/by-seat/%s/sentence-gap/SKILL.md" % (seat.seat_key or seat.tag.lower())
        for seat in _identity.catalog_seats()
        if not seat.retired
    )


def _retired_by_seat_copies() -> tuple[str, ...]:
    """The by-seat upload packs of the retired seats.

    A retired seat's catalog pack is rendered from the canonical pack like every
    other seat's (owner, Oct 22 2026):  the rename guard's skip of the retired
    seats' catalog copies went away, because a pack that names a channel the
    realm no longer has is a stale consumer and the skip let it through
    unnoticed.  The retirement notice comes from the renderer and not from a hand
    edit, so a retired pack carries it and names the live channel too.

    So a retired pack is a generated output like any other:  the gates below read
    it and hold it to the one home, the same as every other copy.
    """
    return tuple(
        "docs/fleet-skills/by-seat/%s/sentence-gap/SKILL.md" % (seat.seat_key or seat.tag.lower())
        for seat in _identity.catalog_seats()
        if seat.retired
    )


def _platform_copies() -> tuple[str, ...]:
    """The repo-tracked platform trees of the one home.  The registry already skips the
    retired seats here, so this list cannot keep one."""
    out = []
    for dest, _seat in _identity.repo_platform_copies(str(ROOT)):
        out.append(Path(dest).relative_to(ROOT).as_posix() + "/sentence-gap/SKILL.md")
    return tuple(out)


RENDERED_COPIES = (
    (
        # The universal copy, and the Cursor rules file, which is a hand-written paste of
        # the same protocol.
        "skills/sentence-gap/SKILL.md",
        ".cursor/rules/sentence-gap.mdc",
    )
    + _active_by_seat_copies()
    + _retired_by_seat_copies()
    + _platform_copies()
)

# The live guidance that quotes the pattern for a reader to copy.
GUIDANCE = (
    "FLEET-UI-COPY.md",
    "docs/SENTENCE-GAP-PORTABLE-SKILL.md",
    "docs/ZULIP-SWITCH-PROMPT.md",
    "docs/protocols/zulip-fleet-guide.md",
    "scripts/pr_sweep/rules.md",
)

# The three files that carry the protocol, and the content each of them must keep.  The
# source-files warning and the two traps were dropped once, by a commit that made the
# protocol block byte-identical across the three, and nothing said so.
PROTOCOL_FILES = (
    "docs/fleet-skills/sentence-gap/SKILL.md",
    "docs/SENTENCE-GAP-PORTABLE-SKILL.md",
    ".cursor/rules/sentence-gap.mdc",
)

REQUIRED_BLOCKS = (
    "**Source files are a different surface and are NOT affected.**",
    "Two more traps, both hit 2026-10-18:",
    "**Never type a raw U+00A0 into a message body or tool call.**",
    "Parent-death guardian target",
)

# The region the three files must share byte for byte.
PROTOCOL_REGION_START = "Both checks carry the same markup group"
PROTOCOL_REGION_END = "### Verify, don't assume"

# The shapes the converter has to get right.  Each is (body line, gapped?) and the gapped
# ones are the ones a body can hold without anyone noticing the missing gap in the render.
SHAPES = (
    ("Plain sentence.  Next sentence.", True),
    ("[Sentence.](https://example.com) Next sentence.", True),
    ("**[Sentence.](https://example.com/path?q=one#two)** Next sentence.", True),
    ("**Bold sentence.**  Next sentence.", True),
    ("~~Deleted sentence.~~  Next sentence.", True),
    ("*Italic sentence.*  Next sentence.", True),
    ("`Code sentence.`  Next sentence.", True),
    ("(**Paren sentence.**)  Next sentence.", True),
    ('He said "Quoted sentence."  Next sentence.', True),
    ("'Quoted sentence.'  Next sentence.", True),
    ("“Curly quoted sentence.”  Next sentence.", True),
    ("‘Curly single quoted sentence.’  Next sentence.", True),
    ("First.  Second.  'Quoted sentence.'", True),
    ("Merged.  [PR #314](https://example.com/pull/314)  Next sentence.", True),
    ("1. Numbered marker stays a marker.", False),
    ("The answer is 42. Next thing.", False),
    ("e.g. this keeps its plain space.", False),
)

NBSP = " "

# AFC-510 (owner Nov 1 2026):  no gap when a sentence ends at a line break, a blank line,
# or the end of the text.  The composite body holds (a) a newline between sentences,
# (b) a blank line between paragraphs, and a same-line pair that must still convert.
LINE_END_UNCHANGED_PREFIX = (
    "First sentence.\nNext sentence.\n\nEnd of paragraph.\n\nNew paragraph.\n"
)
LINE_END_SAME_LINE_PAIR = "Same line.  Gapped here.\n"
LINE_END_COMPOSITE_BODY = LINE_END_UNCHANGED_PREFIX + LINE_END_SAME_LINE_PAIR

EOF_LINE_END_BODIES = (
    ("no_trailing_newline", "Ends at text."),
    ("trailing_newline", "Ends at text.\n"),
)

# The converter runs through perl, because that is how the fleet produces the gap.
HAVE_PERL = shutil.which("perl") is not None
HAVE_BASH = shutil.which("bash") is not None
HAVE_GIT = shutil.which("git") is not None


def assertion_patterns(text: str) -> tuple[str, str]:
    """The count pattern and the leftover pattern a text holds."""
    # Earlier surface self-tests are not the canonical publishing assertions.
    counts = list(_COUNT_RE.finditer(text))
    count = counts[-1] if counts else None
    leftovers = leftover_checks(text)
    return (
        count.group("ansi") if count else "",
        leftovers[0] if leftovers else "",
    )


def leftover_checks(text: str) -> list[str]:
    """Every leftover check a text holds, in either quoting.

    The canonical form is bash's ANSI-C quoting, because it is the only form that can
    carry a straight quote, and the class needs one.  The plain form is still
    recognised, so a file that falls back to it is caught rather than skipped.
    """
    out = []
    for match in _LEFTOVER_RE.finditer(text):
        ansi = match.group("ansi")
        out.append(ansi if ansi is not None else match.group("plain"))
    return out


def mirror_leftover_checks(text: str) -> list[str]:
    """Every leftover check a mirror holds, in either quoting and in either grep form.

    The mirror ships its check in the plain, non -E form, so the shared extraction does
    not see it and would report the mirror clean while it ships a blind check.
    """
    out = []
    for match in _MIRROR_LEFTOVER_RE.finditer(text):
        ansi = match.group("ansi")
        out.append(ansi if ansi is not None else match.group("plain"))
    return out


def leftover_quoting(text: str) -> str:
    """The quoting the first leftover check in a text is written in."""
    match = _LEFTOVER_RE.search(text)
    if not match:
        return ""
    return "ansi" if match.group("ansi") is not None else "plain"


def run_grep(command: str) -> int:
    """Run one grep the way the fleet runs it, and return the number it printed."""
    proc = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode not in (0, 1):
        raise AssertionError("command failed: %s\n%s" % (command, proc.stderr.strip()))
    out = proc.stdout.strip()
    return int(out) if out else 0


def patterns_in(text: str) -> list[str]:
    """Every converter pattern a text holds, in the order it holds them.  The named
    follow-on steps in _NON_GAP_STEPS are skipped;  every other substitution counts."""
    return [p for p in _PATTERN_RE.findall(text) if p not in _NON_GAP_STEPS]


def one_home_patterns() -> list[str]:
    """The patterns the one home holds.  It holds the canonical one, and the lookbehind
    variant it explains."""
    with open(ONE_HOME, encoding="utf-8") as fh:
        return patterns_in(fh.read())


def convert(pattern: str, body: str) -> str:
    """Run one converter pattern over a body, the way the skill says to.

    A hung perl and a missing perl are both a test failure with the reason in the
    message.  They are not a SystemExit:  SystemExit derives from BaseException, and
    unittest's bare except swallows it, so the run would report a plain test error and
    lose which of the two happened.  The timeout is what keeps a hung perl from
    holding the build.
    """
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
        raise AssertionError("perl did not finish within 30 seconds") from exc
    except FileNotFoundError as exc:
        raise AssertionError("perl is not on PATH") from exc
    if proc.returncode != 0:
        raise AssertionError("perl failed: %s" % proc.stderr.strip())
    return proc.stdout


def tracked_files() -> list[Path]:
    """Every tracked file in the checkout that can hold guidance."""
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("git ls-files did not finish within 30 seconds") from exc
    except FileNotFoundError as exc:
        raise AssertionError("git is not on PATH") from exc
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

    def test_each_opening_quote_gets_the_second_gap(self) -> None:
        for quote in ("'", "‘", "“"):
            with self.subTest(quote=quote):
                body = "First.  Second.  " + quote + "Quoted sentence."
                self.assertEqual(
                    convert(self.pattern, body),
                    "First." + NBSP + " Second." + NBSP + " " + quote + "Quoted sentence.",
                )

    def test_a_sentence_before_a_link_opener_gets_its_gap(self) -> None:
        """A sentence that ends immediately before a markdown link is a sentence end.

        The owner's rule is that every pull request number and every issue number in a
        message is an explicit link, so the natural shape is a link at the head of the next
        sentence.  The follower class carries the opening square bracket, so the gap is made
        there and the leftover check can see it when it is missing.
        """
        out = convert(self.pattern, "Merged.  [PR #314](https://example.com/pull/314)  Then.\n")
        self.assertIn("Merged." + NBSP + " [PR #314]", out)

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

    def test_line_end_closes_stay_byte_for_byte_and_same_line_still_gaps(self) -> None:
        """AFC-510:  newline and blank-line sentence ends take no gap;  same-line still does."""
        body = LINE_END_COMPOSITE_BODY
        out = convert(self.pattern, body)
        self.assertEqual(
            out[: len(LINE_END_UNCHANGED_PREFIX)],
            LINE_END_UNCHANGED_PREFIX,
            "line-break and paragraph-end closes must stay byte for byte",
        )
        self.assertEqual(out.count(NBSP), 1, "only the same-line pair may gain a gap")
        self.assertIn(
            "Same line." + NBSP + " Gapped here.",
            out,
            "the same-line pair in the same body must still convert",
        )

    def test_eof_sentence_closes_stay_byte_for_byte(self) -> None:
        """AFC-510:  a sentence at end of text, with or without a trailing newline, stays alone."""
        for name, body in EOF_LINE_END_BODIES:
            with self.subTest(case=name):
                out = convert(self.pattern, body)
                self.assertEqual(out, body, "end-of-text closes must not gain a gap")
                self.assertNotIn(NBSP, out)


@unittest.skipUnless(HAVE_PERL and HAVE_BASH, "perl or bash is not on PATH")
class LineEndAssertionTests(unittest.TestCase):
    """AFC-510:  the shipped leftover check must not flag line-end closes as missed gaps."""

    def setUp(self) -> None:
        with open(ONE_HOME, encoding="utf-8") as fh:
            text = fh.read()
        self.pattern = one_home_patterns()[0]
        _, self.leftover_pattern = assertion_patterns(text)

    def _leftover_command(self, path: Path) -> str:
        return "grep -cE $'%s' '%s'" % (self.leftover_pattern, path)

    def test_converted_line_end_bodies_carry_no_leftover(self) -> None:
        bodies = (
            LINE_END_COMPOSITE_BODY,
            "First.\nSecond.\n\nThird.\n",
        ) + tuple(body for _, body in EOF_LINE_END_BODIES)
        for body in bodies:
            with self.subTest(body=body.split("\n", 1)[0]):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "body.nbsp.txt"
                    path.write_text(convert(self.pattern, body), encoding="utf-8")
                    leftover = run_grep(self._leftover_command(path))
                self.assertEqual(
                    leftover,
                    0,
                    "line-end closes must not read as a missed same-line gap",
                )


@unittest.skipUnless(HAVE_GIT, "git is not on PATH")
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

    def test_every_dated_record_ends_with_the_one_home_pattern(self) -> None:
        """A dated record keeps the form its ruling was written in, but the live notice
        it ends with must carry the one home's converter.

        The record file is exempt from the sweep, because its dated parts are history.
        Its live notice is not history:  it repeats the converter for a reader, and a
        notice that repeats a stale one is the one copy that no gate compares, so it
        would serve the stale converter after the one home changed.
        """
        canonical = one_home_patterns()[0]
        for rel in DATED_RECORDS:
            path = ROOT / rel
            with open(path, encoding="utf-8") as fh:
                found = patterns_in(fh.read())
            self.assertTrue(found, "%s holds no converter pattern" % rel)
            self.assertEqual(
                found[-1],
                canonical,
                "%s does not end with the one home's converter:  %s" % (rel, found[-1]),
            )

    def test_no_live_file_holds_a_converter_without_the_markup_group(self) -> None:
        """A copy that drifts is a body that ships with no gap, so every converter in the
        checkout carries the markup group.  A dated record of a ruling keeps the form it
        was written in and is listed as such, and a generated mirror is built elsewhere and
        cannot be edited here, so it is asserted on its own."""
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


class RetiredCatalogPackRenderTests(unittest.TestCase):
    """A retired seat's catalog pack is a current render of the canonical pack.

    The owner ruled on Oct 22 2026 that the retired seats' catalog copies are
    rendered from the canonical pack like every other seat's are, so the rename
    guard's skip of them went away.

    So the pin here is a staleness check and not a freeze.  A render over the
    checkout must leave every retired pack byte for byte as it was, because the
    committed pack is already a fresh render of the one home.  A pack left behind
    by an older one home fails this, and so does a hand edit to one.
    """

    def test_the_renderer_leaves_the_retired_packs_current(self) -> None:
        packs = _retired_by_seat_copies()
        self.assertTrue(packs, "the registry names no retired seat")

        def digests() -> dict:
            out = {}
            for rel in packs:
                path = ROOT / rel
                self.assertTrue(path.is_file(), "%s is missing" % rel)
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            return out

        before = digests()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "install-fleet-skills.py"), "--repo-only"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        self.assertEqual(proc.returncode, 0, "the renderer failed:  %s" % proc.stderr.strip())
        self.assertEqual(
            before,
            digests(),
            "a retired seat's pack is not a current render of the canonical pack",
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
        return "grep -oE $'%s' '%s' | wc -l" % (self.count_pattern, path)

    def _leftover_command(self, path: Path) -> str:
        return "grep -cE $'%s' '%s'" % (self.leftover_pattern, path)

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
                "grep -cE $'%s' '%s'" % (_BLIND_LEFTOVER, path),
            )
        self.assertGreater(leftover, 0, "the markup-aware check must catch this gap")
        self.assertEqual(blind, 0, "the blind check is expected to miss it -- that is the defect")

    def test_the_leftover_check_sees_a_missed_gap_in_a_quoted_sentence(self) -> None:
        """The shapes the old class could not see:  a sentence end inside a quote.

        The period sits inside the quote and the quote stands between it and the space,
        so the check has to carry the quote in the class.  The follower shape matters
        too:  a gap missed before a quoted sentence reads `Second. 'Quoted`, and the
        follower class has to carry the opening quote as well.
        """
        for line in (
            "'Quoted sentence.' Next sentence.",
            "Second. 'Quoted sentence.'",
            "“Curly quoted sentence.” Next sentence.",
            "Second. “Curly quoted sentence.”",
            "‘Curly single quoted sentence.’ Next sentence.",
            "Second. ‘Curly single quoted sentence.’",
        ):
            with self.subTest(line=line):
                with tempfile.TemporaryDirectory() as tmp:
                    path = self._write(tmp, line + "\n")
                    leftover = run_grep(self._leftover_command(path))
                    blind = run_grep(
                        "grep -cE $'%s' '%s'" % (_QUOTE_BLIND_LEFTOVER, path),
                    )
                self.assertGreater(leftover, 0, "the check must see this missed gap")
                self.assertEqual(
                    blind,
                    0,
                    "the quote-blind check is expected to miss it -- that is the defect",
                )

    def test_the_leftover_check_catches_a_missed_gap_before_any_converter_follower(self) -> None:
        """The follower class has to be the converter's own lookahead.

        A body that already holds one real gap and still holds a plain-space gap before a
        follower the converter accepts reads as converted, so a narrower follower class
        returned 0 on exactly the partly-converted body the check exists to catch.
        """
        for follower in ("4", "*", "`", "#", "@"):
            with self.subTest(follower=follower):
                with tempfile.TemporaryDirectory() as tmp:
                    path = self._write(
                        tmp,
                        "Converted." + NBSP + " Next sentence. %sBold next.**" % follower,
                    )
                    leftover = run_grep(self._leftover_command(path))
                self.assertGreater(
                    leftover,
                    0,
                    "the check cannot see a missed gap before %r:  %s"
                    % (follower, self.leftover_pattern),
                )

    def test_every_live_guidance_file_writes_the_leftover_check_in_the_ansi_form(self) -> None:
        """The plain form cannot carry a straight quote, and the class needs one."""
        for rel in GUIDANCE:
            with open(ROOT / rel, encoding="utf-8") as fh:
                text = fh.read()
            self.assertEqual(
                leftover_quoting(text),
                "ansi",
                "%s writes the leftover check outside bash's ANSI-C form" % rel,
            )

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

    def test_every_live_guidance_file_passes_its_own_checks(self) -> None:
        """A guidance file's own commands have to work, not merely look right.

        The shape of a command can be right while the command still matches nothing:  a
        stray bracket after the class closes the expression early, the count prints 0,
        and the pair passes a look at the text.  So the commands are run.

        The leftover check is run both ways.  It has to be quiet on a converted body and
        loud on a body with a missed gap, because a check that cannot see the gap is
        quiet on both and passes the one-way test while being blind.
        """
        pattern = one_home_patterns()[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.nbsp.txt"
            path.write_text(
                convert(pattern, "**Bold sentence.**  Next sentence.\n"),
                encoding="utf-8",
            )
            missed = Path(tmp) / "missed.txt"
            # The gap after a markup span was never made:  one space, so the converter's
            # pattern does not match it and the body ships with no gap.
            missed.write_text("**Bold sentence.** Next sentence.\n", encoding="utf-8")
            for rel in GUIDANCE:
                with open(ROOT / rel, encoding="utf-8") as fh:
                    text = fh.read()
                count, leftover = assertion_patterns(text)
                self.assertTrue(count, "%s holds no count assertion" % rel)
                self.assertTrue(leftover, "%s holds no leftover assertion" % rel)
                got_count = run_grep("grep -oE $'%s' '%s' | wc -l" % (count, path))
                got_leftover = run_grep("grep -cE $'%s' '%s'" % (leftover, path))
                self.assertGreater(
                    got_count,
                    0,
                    "%s holds a count check that matches nothing:  %s" % (rel, count),
                )
                self.assertEqual(
                    got_leftover,
                    0,
                    "%s holds a leftover check that fires on a converted body:  %s"
                    % (rel, leftover),
                )
                self.assertGreater(
                    run_grep("grep -cE $'%s' '%s'" % (leftover, missed)),
                    0,
                    "%s holds a leftover check that cannot see a missed gap:  %s" % (rel, leftover),
                )

    def test_no_live_file_holds_a_blind_leftover_check(self) -> None:
        """No live file may hold the check that cannot see a gap after a markup span.
        A dated record of a ruling keeps the form it was written in and is skipped, and a
        generated mirror is built elsewhere and cannot be edited here, so it is asserted
        on its own."""
        for path in tracked_files():
            rel = path.relative_to(ROOT).as_posix()
            if rel in DATED_RECORDS or rel in GENERATED_MIRRORS or rel in CHECKER_FILES:
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for pattern in leftover_checks(text):
                self.assertIn(
                    _ASSERT_MARKUP_GROUP,
                    pattern,
                    "%s holds a blind leftover check:  %s" % (rel, pattern),
                )


class PatternExtractionTests(unittest.TestCase):
    """Only the named follow-on NBSP step is skipped;  every other substitution counts."""

    def test_only_the_named_follow_on_step_is_not_a_converter(self) -> None:
        text = (
            "perl -CSDA -pe 's/(?<![0-9])([.!?]) +(?=[A-Z])/$1\\x{a0} /g' body.txt\n"
            "perl -CSDA -pe 's/\\x{a0} /\\x{202f} /g' body.nbsp.txt\n"
            "perl -CSDA -pe 's/([?!.])\\s+(?=[A-Z])/$1\\x{a0} /g' body.txt\n"
            "perl -CSDA -pe 's/ +/\\x{a0} /g' body.txt\n"
        )
        self.assertEqual(
            patterns_in(text),
            [
                "s/(?<![0-9])([.!?]) +(?=[A-Z])/$1\\x{a0} /g",
                "s/([?!.])\\s+(?=[A-Z])/$1\\x{a0} /g",
                "s/ +/\\x{a0} /g",
            ],
        )


class GeneratedMirrorTests(unittest.TestCase):
    """FLEET-PROTOCOL.md is the file every seat reads first, so it is asserted, not skipped.

    The mirror is built from the Fleet-OPS handbook and this repository cannot change it:
    scripts/check-generated-mirror.py refuses any hand edit from a branch that is not
    handbook-sync/mirror.  It is also the copy a seat reads before it reads anything
    else, so a stale converter in it ships a body with no gap.  The assertions below
    hold every mirror in the registry to a known state and fail with a pointer to the
    handbook sync, rather than letting the mirror drift unnoticed behind a blanket
    exemption.

    The mirror is still behind the one home:  the handbook page behind it carries the
    last sync's converter, which has the markup group but not the opening square bracket,
    and that fix belongs to Fleet-OPS, not here.  So
    every mirror in the registry is held to one of two known states -- the one home's
    converter, or the pinned converter the mirror ships until the sync job rewrites it --
    and to nothing else.  A mirror that carries anything else fails here, with the
    pointer.  The strict shape of the converter is asserted the moment the mirror
    carries the one home's converter.
    """

    # The mirrors come from the registry, so a mirror added to it is asserted here too
    # and not silently left unchecked.  Exactly one of the two names exists during the
    # rename, so the absent one is skipped;  a checkout with neither fails below.
    MIRRORS = tuple(ROOT / rel for rel in GENERATED_MIRRORS if (ROOT / rel).is_file())

    # The converter the mirror ships until the upstream sync rewrites it.  The mirror is
    # built from the Fleet-OPS handbook, so it lags the one home by one sync:  this is the
    # one home's converter before the opening square bracket joined the follower class, and
    # it is the state the pointer is about.  The mirror holds exactly one converter command,
    # so this is the whole set it may hold while it waits.
    PRE_SYNC_CONVERTERS = (
        's/(?<![0-9])([.!?])((?:\\]\\([^()\\s]{1,255}\\)|[*_`~)\\]"\\x{27}\\x{2019}\\x{201D}])*)'
        ' +(?=[A-Z0-9"*`#@\\x{27}\\x{2018}\\x{201C}])/$1$2\\x{a0} /g',
    )

    POINTER = (
        "A generated mirror is built from the Fleet-OPS handbook, so it cannot be fixed "
        "here:  change the handbook page behind it (for FLEET-PROTOCOL.md, "
        "handbook/owner/writing-for-owner.md) in a Fleet-OPS pull request and the sync "
        "job writes the mirror from the merge.  A hand edit is refused by "
        "scripts/check-generated-mirror.py."
    )

    def test_a_generated_mirror_exists(self) -> None:
        """Exactly one protocol name exists during the rename;  neither is a failure."""
        self.assertTrue(
            self.MIRRORS,
            "no generated mirror exists under %s;  the mirror is the file every seat "
            "reads first, so it is asserted, not skipped.  %s" % (GENERATED_MIRRORS, self.POINTER),
        )

    def _mirror_texts(self) -> list[tuple[str, str]]:
        """Every mirror in the registry, with the text it holds."""
        out = []
        for path in self.MIRRORS:
            with open(path, encoding="utf-8") as fh:
                out.append((path.relative_to(ROOT).as_posix(), fh.read()))
        return out

    def _is_pre_sync(self, found: list[str]) -> bool:
        """Whether the mirror still ships the converter it holds until the upstream sync
        rewrites it."""
        return sorted(found) == sorted(self.PRE_SYNC_CONVERTERS)

    def test_the_mirror_holds_the_one_homes_converter(self) -> None:
        """The mirror carries the same converter the one home ships, so a seat that
        reads only the mirror reads a converter that can see a markup gap.  Until the
        upstream sync lands it carries the pinned converter instead, and the pointer
        says where that fix goes."""
        canonical = one_home_patterns()[0]
        for rel, text in self._mirror_texts():
            found = patterns_in(text)
            self.assertTrue(found, "%s holds no converter pattern" % rel)
            if canonical in found:
                continue
            if self._is_pre_sync(found):
                self.skipTest(
                    "%s still ships the converter it holds until the upstream sync "
                    "rewrites it:  %s\n%s" % (rel, found, self.POINTER)
                )
            self.fail(
                "%s holds neither the one home's converter nor the pinned pre-sync "
                "converter:  %s\n%s" % (rel, found, self.POINTER)
            )

    def test_the_mirror_holds_no_converter_without_the_markup_group(self) -> None:
        """Every converter the mirror holds can see a gap that sits after a markup span.
        The markup group reaches the mirror with the upstream sync, so the mirror that
        still ships the pinned pre-sync converter waits for it."""
        for rel, text in self._mirror_texts():
            found = patterns_in(text)
            self.assertTrue(found, "%s holds no converter pattern" % rel)
            if self._is_pre_sync(found):
                self.skipTest(
                    "%s still ships the pre-sync converter, so the markup group has not "
                    "reached it yet:  %s\n%s" % (rel, found, self.POINTER)
                )
            for pattern in found:
                self.assertIn(
                    _MARKUP_GROUP,
                    pattern,
                    "%s holds a converter without the markup group:  %s\n%s"
                    % (rel, pattern, self.POINTER),
                )

    def test_the_mirror_holds_no_blind_leftover_check(self) -> None:
        """The leftover check the mirror ships must be able to see a gap that was never
        converted, including one that sits after a markup span."""
        for rel, text in self._mirror_texts():
            found = patterns_in(text)
            if self._is_pre_sync(found):
                self.skipTest(
                    "%s still ships the pre-sync converter, so its leftover check has "
                    "not been widened yet:  %s\n%s" % (rel, found, self.POINTER)
                )
            checks = mirror_leftover_checks(text)
            self.assertTrue(checks, "%s holds no leftover check" % rel)
            for pattern in checks:
                self.assertIn(
                    _ASSERT_MARKUP_GROUP,
                    pattern,
                    "%s holds a blind leftover check:  %s\n%s" % (rel, pattern, self.POINTER),
                )


def entity_body_from_nbsp(body: str) -> str:
    """The entity-mode bytes that match a real-byte converted body."""
    return body.replace("\u00a0 ", "&nbsp; ")


RAW_LINK_BODY = "[Sentence.](https://example.com) Next sentence.\n"


def write_assertion_fixtures(tmp: str, nbsp_body: str) -> None:
    """The files the documented assertion blocks name."""
    root = Path(tmp)
    for name in ("reply.txt", "body.nbsp.txt"):
        (root / name).write_text(nbsp_body, encoding="utf-8")
    # The raw body the documented `gap.pl` recipe converts.
    (root / "body.txt").write_text(RAW_LINK_BODY, encoding="utf-8")
    (root / "body.entity.txt").write_text(entity_body_from_nbsp(nbsp_body), encoding="utf-8")


@unittest.skipUnless(HAVE_BASH and HAVE_PERL, "bash and perl are required")
class DocumentationExecutionTests(unittest.TestCase):
    def test_actual_documented_grep_blocks_execute_as_copied(self):
        canonical = one_home_patterns()[0]
        for rel in ("FLEET-UI-COPY.md",) + PROTOCOL_FILES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            blocks = re.findall(r"(?m)^([ ]{0,3})```sh\n(.*?)^\1```", text, re.DOTALL)
            checked = 0
            for indent, block in blocks:
                if "grep -cE" not in block:
                    continue
                checked += 1
                script = "\n".join(line.removeprefix(indent) for line in block.splitlines())
                with tempfile.TemporaryDirectory() as tmp:
                    body = convert(canonical, "[Sentence.](https://example.com) Next sentence.\n")
                    write_assertion_fixtures(tmp, body)
                    if "body.entity.prose.txt" in block:
                        entity = (Path(tmp) / "body.entity.txt").read_text(encoding="utf-8")
                        prose = re.sub(r"(`+)([^\n]*?)\1", " ", entity)
                        (Path(tmp) / "body.entity.prose.txt").write_text(prose, encoding="utf-8")
                    proc = subprocess.run(
                        ["bash", "-c", script],
                        cwd=tmp,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertIn(proc.returncode, (0, 1), rel + ": " + proc.stderr)
                    self.assertEqual(proc.stderr, "", rel)
                    self.assertTrue(
                        all(line.strip().isdigit() for line in proc.stdout.splitlines()), rel
                    )
                    if "cat > gap.pl" in block:
                        # The recipe's cmp lines are silent and its entity leftover is 0.
                        self.assertEqual(proc.stdout.split(), ["0"], rel + ": gap.pl recipe")
            self.assertGreater(checked, 0, rel + " has no executable assertion block")

    def test_each_documented_grep_pair_detects_link_boundaries(self):
        canonical = one_home_patterns()[0]
        raw = "[Sentence.](https://example.com) Next sentence.\n"
        for rel in ("FLEET-UI-COPY.md",) + PROTOCOL_FILES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            blocks = re.findall(r"(?m)^([ ]{0,3})```sh\n(.*?)^\1```", text, re.DOTALL)
            for _, block in blocks:
                if "cat > gap.pl" in block:
                    continue
                commands = [
                    line.strip().split(" #", 1)[0].rstrip()
                    for line in block.splitlines()
                    if line.strip().startswith("grep ")
                ]
                if not commands:
                    continue
                if len(commands) not in (2, 3):
                    continue
                with tempfile.TemporaryDirectory() as tmp:
                    for fixed in (False, True):
                        body = convert(canonical, raw) if fixed else raw
                        write_assertion_fixtures(tmp, body)
                        if "body.entity.prose.txt" in block:
                            entity = (Path(tmp) / "body.entity.txt").read_text(encoding="utf-8")
                            prose = re.sub(r"(`+)([^\n]*?)\1", " ", entity)
                            (Path(tmp) / "body.entity.prose.txt").write_text(
                                prose, encoding="utf-8"
                            )
                        values = []
                        for command in commands:
                            proc = subprocess.run(
                                ["bash", "-c", command],
                                cwd=tmp,
                                capture_output=True,
                                text=True,
                                timeout=30,
                            )
                            self.assertIn(proc.returncode, (0, 1), rel + proc.stderr)
                            self.assertEqual(proc.stderr, "", rel)
                            values.append(int(proc.stdout.strip()))
                        if len(commands) == 2:
                            if fixed:
                                self.assertGreater(values[0], 0, rel)
                                self.assertEqual(values[1], 0, rel)
                            else:
                                self.assertEqual(values[0], 0, rel)
                                self.assertGreater(values[1], 0, rel)
                        else:
                            if fixed:
                                self.assertGreater(values[0], 0, rel)
                                self.assertGreater(values[1], 0, rel)
                                self.assertEqual(values[2], 0, rel)
                            else:
                                self.assertEqual(values[0], 0, rel)
                                self.assertEqual(values[1], 0, rel)
                                self.assertGreater(values[2], 0, rel)

    def test_link_conversion_and_both_assertions_agree(self):
        text = ONE_HOME.read_text(encoding="utf-8")
        count, leftover = assertion_patterns(text)
        for suffix in ("", "**", "’"):
            raw = "**[Sentence.](https://example.com/path?q=one#two)" + suffix + " Next sentence.\n"
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "body.txt"
                path.write_text(raw, encoding="utf-8")
                self.assertGreater(run_grep("grep -cE $'%s' '%s'" % (leftover, path)), 0)
                fixed = convert(one_home_patterns()[0], raw)
                path.write_text(fixed, encoding="utf-8")
                self.assertGreater(run_grep("grep -oE $'%s' '%s' | wc -l" % (count, path)), 0)
                self.assertEqual(run_grep("grep -cE $'%s' '%s'" % (leftover, path)), 0)
                self.assertIn("https://example.com/path?q=one#two)", fixed)


class ProtocolContentTests(unittest.TestCase):
    """The protocol files must keep the content a reader needs, not only the converter.

    One commit made the protocol block byte-identical across the three files and, in
    doing so, dropped the source-files warning and the two 2026-10-18 traps from all of
    them.  Nothing said so:  the rule that a source file wants plain ASCII spaces then
    existed only in the guidance files, and an agent reading only the source of truth
    would commit U+00A0 into a source file.
    """

    def test_every_protocol_file_keeps_the_source_files_warning(self) -> None:
        """Each of the three must keep the warning and both traps."""
        for rel in PROTOCOL_FILES:
            with self.subTest(file=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                for block in REQUIRED_BLOCKS:
                    self.assertIn(
                        block,
                        text,
                        "%s no longer carries %r" % (rel, block),
                    )

    def test_the_protocol_region_is_byte_identical_across_the_three(self) -> None:
        """Making the three agree must not cost any of them content."""
        regions = {}
        for rel in PROTOCOL_FILES:
            text = (ROOT / rel).read_text(encoding="utf-8")
            start = text.index(PROTOCOL_REGION_START)
            end = text.index(PROTOCOL_REGION_END, start)
            regions[rel] = text[start:end]
        self.assertEqual(
            len(set(regions.values())),
            1,
            "the protocol region differs between the files:  %s"
            % sorted((rel, len(text)) for rel, text in regions.items()),
        )

    def test_every_rendered_copy_carries_the_source_files_warning(self) -> None:
        """The rendered trees carry the warning too, or a seat reads a file without it."""
        for rel in RENDERED_COPIES:
            path = ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=rel):
                self.assertIn(
                    REQUIRED_BLOCKS[0],
                    text,
                    "%s no longer carries the source-files warning" % rel,
                )


if __name__ == "__main__":
    unittest.main()
