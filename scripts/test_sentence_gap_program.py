#!/usr/bin/env python3
"""Tests for the fleet's code-aware sentence-gap program.

The program in `scripts/sentence_gap_convert.pl` is what converts a body that carries
code:  it holds the code aside, makes the gap with the canonical pattern, collapses the
prose runs, and puts the code back byte for byte.  The same program is embedded in every
surface that documents the recipe, so a seat without the checkout can still run it.

Three things are checked here.  First, that every surface carries the same program, so a
copy that drifts is not a body that ships with a different converter.  Second, that the
program does what it says:  the code comes out untouched (every inline span, every fenced
block, and an indented block that really is one), the gaps are made, the prose runs
collapse, a hard break survives, a numbered marker stays a numbered marker, and the
whole thing is idempotent, which is the check that sees a prose double space that no
grep over the body can see.  Third, that the two gap forms the program produces are the
two the owner ruling of 2026-10-23 names:  a real U+00A0 plus a space with no flag, on
every surface, and the `&nbsp;` entity plus a normal space with `--entity`, which is the
form the two chat panes take.  (The body of an Apple Note takes the narrow no-break space
plus a normal space, AFC-552; the note helper writes that one, not this program.)
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ROOT / "scripts" / "sentence_gap_convert.pl"

#  The surfaces that document the recipe.  Each one embeds the program, so each one can
#  be read by a seat that has no checkout of its own.
SURFACES = (
    "docs/fleet-skills/sentence-gap/SKILL.md",
    "docs/SENTENCE-GAP-PORTABLE-SKILL.md",
    ".cursor/rules/sentence-gap.mdc",
    "scripts/pr_sweep/rules.md",
)

#  The heredoc the documentation writes the program with.  A quoted heredoc, so the
#  backticks and the dollars in the program stay literal.
_HEREDOC_RE = re.compile(r"cat > gap\.pl <<'PERL'\n(?P<program>.*?)\nPERL\n", re.DOTALL)

#  The one home's converter, as it is written in the documentation:  a perl
#  substitution with a sentence-end group.
_ONE_LINER_RE = re.compile(r"perl -CSDA -pe '(s/.*?/g)'")

#  The canonical one-liner:  the pattern the two greps are written against, and the
#  pattern the program applies.  The program must not quietly change what a gap is.
_CANONICAL = (
    r"""s/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +"""
    r"""(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}\[])/$1$2\x{a0} /g"""
)

#  The canonical pattern with the entity in place of the byte pair:  the same
#  substitution, for the text the two chat panes take (owner ruling 2026-10-23).  The `&` is
#  escaped, because in a perl replacement it would otherwise stand for the matched text.
_ENTITY_CANONICAL = _CANONICAL.replace(r"\x{a0} ", r"\&nbsp; ")

#  The entity the two chat panes take, and the space that follows it.
ENTITY = "&nbsp; "

NBSP = "\u00a0"

HAVE_PERL = shutil.which("perl") is not None

#  The code regions the program has to give back byte for byte.
CODE_REGIONS = (
    "```js\nconst x = 1.  // a comment.  Another.\nreturn x.  More\n```",
    "~~~python\ndef f():\n    return True.  Something\n~~~",
    '  ```sh\n  echo "hello.  world"\n  ```',
    "> ```\n> quoted fence body.  More\n> ```",
    "    indented code line.  More\n    second indented line.  More",
    "repo:  Fixture-Repo  |  DONE",
    "`inline span with  two spaces`",
    "``double backtick span with  two spaces``",
    "      code line inside a list item.  More",
)

#  The prose that is not code, and that the program has to convert like any other
#  prose.  A four-space line under a "- " bullet is a nested list item, not an
#  indented code block:  CommonMark gives that bullet a content indent of two, so
#  the code indent inside it is six spaces.
NESTED_LIST_PROSE = "    Nested prose sentence.  Another sentence."
LIST_CODE_BLOCK = "      code line inside a list item.  More"

#  The bodies the two masking defects hid in.
INLINE_SPANS_BODY = (
    "First `span one with  two spaces` and second `span two with  two spaces` here.  Next.\n"
)
MULTI_BACKTICK_BODY = "Here is ``code with ` inside and  two spaces`` and more.  Next.\n"
NESTED_LIST_BODY = "- A list item.\n\n    Nested prose.  Another sentence.\n"
LIST_CODE_BODY = "- A list item.\n\n      Code line.  Another line.\n"

HARD_BREAK = "A hard break at the end of this line follows.  \n"

BODY = """repo:  Fixture-Repo  |  DONE

A plain sentence.  A second plain sentence.

**Bold sentence.**  Next sentence.

`Code span here.`  Next sentence.

`inline span with  two spaces`

``double backtick span with  two spaces``

```js
const x = 1.  // a comment.  Another.
return x.  More
```

~~~python
def f():
    return True.  Something
~~~

- List item with a fence:

  ```sh
  echo "hello.  world"
  ```

- List item with a nested item:

    Nested prose sentence.  Another sentence.

- List item with a code block:

      code line inside a list item.  More

> Quoted prose.  Next sentence.

> ```
> quoted fence body.  More
> ```

    indented code line.  More
    second indented line.  More

A hard break at the end of this line follows.
And the next line continues.

1. Numbered marker stays a marker.
2. Second item here.

The answer is 42. Next thing.

e.g. this keeps its plain space.

A colon:  two spaces after a colon.
A semicolon;  two spaces after a semicolon.
A dash -  two spaces after a dash.

Trailing text with a double  space inside a sentence.
""".replace(
    "A hard break at the end of this line follows.\n",
    HARD_BREAK,
)


def run_program(program: str, body: str, *args: str) -> str:
    """Run the program over a body, the way the documentation says to.

    The flags go after the program, so `--entity` reaches the program and not perl.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gap.pl"
        path.write_text(program, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["perl", "-CSDA", "-0777", "-p", str(path), *args],
                input=body,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("the program did not finish within 30 seconds") from exc
        if proc.returncode != 0:
            raise AssertionError("the program failed: %s" % proc.stderr.strip())
        return proc.stdout


def program_text() -> str:
    """The program, from the checkout."""
    return PROGRAM.read_text(encoding="utf-8").rstrip("\n")


class SurfaceTests(unittest.TestCase):
    """Every surface carries the same program."""

    def test_the_program_file_exists(self) -> None:
        self.assertTrue(PROGRAM.is_file(), "%s is missing" % PROGRAM)

    def test_every_surface_embeds_the_same_program(self) -> None:
        """Every surface carries the same program, so a seat without the checkout runs
        the same converter.

        The surfaces are the pack's own copies of the program.  The copy in the
        checkout is one release ahead of them:  it carries the `--entity` mode (owner
        ruling 2026-10-23), which the pack's copy does not have yet, because the pack is
        rewritten by its own pull request.  So the pin that still holds between the
        checkout and the pack's copy is the behaviour, and it is pinned in
        `test_the_checkout_program_agrees_with_the_pack_copy`.
        """
        programs = {}
        for rel in SURFACES:
            path = ROOT / rel
            self.assertTrue(path.is_file(), "%s is missing" % rel)
            text = path.read_text(encoding="utf-8")
            match = _HEREDOC_RE.search(text)
            self.assertIsNotNone(match, "%s embeds no program" % rel)
            programs[rel] = match.group("program")
        self.assertEqual(
            len(set(programs.values())),
            1,
            "the surfaces do not carry one program:  %s" % ", ".join(sorted(programs)),
        )

    def test_the_pack_copy_of_the_program_is_present(self) -> None:
        """The pack carries a copy of the program, so the pin has something to hold."""
        for rel in SURFACES:
            path = ROOT / rel
            self.assertTrue(path.is_file(), "%s is missing" % rel)
            match = _HEREDOC_RE.search(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(match, "%s embeds no program" % rel)

    def test_the_program_applies_the_one_home_pattern(self) -> None:
        """The program applies the one home's pattern, so a change to the one home
        cannot leave the program converting with an older rule.

        The one home's pattern is the one the two greps are written against, so a
        program that carried a different one would make gaps the checks cannot see.
        """
        one_home = (ROOT / "docs/fleet-skills/sentence-gap/SKILL.md").read_text(encoding="utf-8")
        match = _ONE_LINER_RE.search(one_home)
        self.assertIsNotNone(match, "the one home holds no converter pattern")
        self.assertIn(
            match.group(1),
            program_text(),
            "the program does not carry the one home's pattern",
        )


@unittest.skipUnless(HAVE_PERL, "perl is not on PATH")
class ProgramBehaviourTests(unittest.TestCase):
    """The program does what the documentation says it does."""

    def setUp(self) -> None:
        self.program = program_text()
        self.out = run_program(self.program, BODY)

    def test_every_code_region_comes_back_byte_for_byte(self) -> None:
        for region in CODE_REGIONS:
            with self.subTest(region=region[:40]):
                self.assertIn(region, self.out)

    def test_the_gaps_are_made(self) -> None:
        for line in (
            "A plain sentence." + NBSP + " A second plain sentence.",
            "**Bold sentence.**" + NBSP + " Next sentence.",
            "`Code span here.`" + NBSP + " Next sentence.",
            "> Quoted prose." + NBSP + " Next sentence.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, self.out)

    def test_the_prose_runs_collapse(self) -> None:
        for line in (
            "A colon: two spaces after a colon.",
            "A semicolon; two spaces after a semicolon.",
            "A dash - two spaces after a dash.",
            "Trailing text with a double space inside a sentence.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, self.out)

    def test_the_repo_header_keeps_its_template_spacing(self) -> None:
        self.assertIn("repo:  Fixture-Repo  |  DONE", self.out)

    def test_every_inline_span_is_held(self) -> None:
        """Every inline span in the body is held, not only the first one.

        The masking substitution carried no `/g` flag, so the second span in a body
        was not held:  the prose collapse reached inside it and rewrote its interior.
        """
        for region in (
            "`Code span here.`",
            "`inline span with  two spaces`",
            "``double backtick span with  two spaces``",
        ):
            with self.subTest(region=region):
                self.assertIn(region, self.out)

    def test_a_multi_backtick_span_keeps_its_delimiters(self) -> None:
        """A span is put back with the delimiters it came with.

        The old masking rebuilt every span with one hard-coded backtick on each side,
        so a span written with two backticks came back with one and the bytes of the
        body changed.
        """
        self.assertIn("``double backtick span with  two spaces``\n", self.out)

    def test_a_four_space_nested_list_item_is_prose(self) -> None:
        """A four-space line under a `- ` bullet is a nested list item, not code.

        The old masking held any run of four-space indented lines, so the nested item
        was neither gapped nor collapsed, and the shipped body kept its double space.
        """
        self.assertIn(
            "    Nested prose sentence." + NBSP + " Another sentence.",
            self.out,
        )
        self.assertNotIn(NESTED_LIST_PROSE, self.out)

    def test_a_six_space_block_inside_a_list_item_is_code(self) -> None:
        """Six spaces inside a `- ` bullet is the code indent of that item.

        A `- ` bullet has a content indent of two, so the code indent inside it is six
        spaces, and that block is held and comes back byte for byte.
        """
        self.assertIn(LIST_CODE_BLOCK + "\n", self.out)

    def test_a_list_item_separates_its_prose_from_its_code(self) -> None:
        """The two list cases in one body:  the nested item is prose, the six-space
        block is code."""
        body = NESTED_LIST_BODY + "\n" + LIST_CODE_BODY
        out = run_program(self.program, body)
        self.assertIn("    Nested prose." + NBSP + " Another sentence.", out)
        self.assertNotIn("    Nested prose.  Another sentence.", out)
        self.assertIn("      Code line.  Another line.\n", out)

    def test_the_program_is_idempotent_on_the_new_fixtures(self) -> None:
        """The third check, on the fixtures that found the two masking defects."""
        for body in (
            INLINE_SPANS_BODY,
            MULTI_BACKTICK_BODY,
            NESTED_LIST_BODY,
            LIST_CODE_BODY,
        ):
            with self.subTest(body=body.split("\n")[0]):
                out = run_program(self.program, body)
                again = run_program(self.program, out)
                self.assertEqual(
                    again,
                    out,
                    "the program is not idempotent on this fixture:  the second run"
                    " changed the body",
                )

    def test_a_markdown_hard_break_survives(self) -> None:
        self.assertIn("A hard break at the end of this line follows.  \n", self.out)

    def test_a_numbered_marker_stays_a_numbered_marker(self) -> None:
        self.assertIn("1. Numbered marker stays a marker.\n", self.out)

    def test_a_digit_ended_sentence_is_left_alone(self) -> None:
        """The known cost of the lookbehind:  a sentence that ends in a digit is not
        gapped, because the lookbehind that keeps a numbered marker a marker also skips
        a period that follows a digit."""
        self.assertIn("The answer is 42. Next thing.", self.out)

    def test_the_program_is_idempotent(self) -> None:
        """The third check.  A body that differs on the second run carried a prose double
        space the collapse did not reach, or a code region the mask did not hold."""
        again = run_program(self.program, self.out)
        self.assertEqual(
            again,
            self.out,
            "the program is not idempotent:  the second run changed the body",
        )

    def test_the_program_agrees_with_the_canonical_pattern(self) -> None:
        """On a body that carries no code, the program and the one-liner must produce the
        same bytes, so the program never quietly changes what a gap is."""
        prose = "\n".join(
            [
                "Plain sentence.  Next sentence.",
                "**Bold sentence.**  Next sentence.",
                "~~Deleted sentence.~~  Next sentence.",
                "*Italic sentence.*  Next sentence.",
                "`Code sentence.`  Next sentence.",
                "(**Paren sentence.**)  Next sentence.",
                'He said "Quoted sentence."  Next sentence.',
                "'Quoted sentence.'  Next sentence.",
                "\u201cCurly quoted sentence.\u201d  Next sentence.",
                "\u2018Curly single quoted sentence.\u2019  Next sentence.",
                "First.  Second.  'Quoted sentence.'",
                "1. Numbered marker stays a marker.",
                "The answer is 42. Next thing.",
                "e.g. this keeps its plain space.",
                "Plain sentence. Next sentence.",
            ]
        )
        one = subprocess.run(
            ["perl", "-CSDA", "-pe", _CANONICAL],
            input=prose,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(one.returncode, 0, "the canonical one-liner failed")
        self.assertEqual(
            run_program(self.program, prose),
            one.stdout,
            "the program and the canonical one-liner disagree on a body with no code",
        )

    def test_the_entity_mode_holds_every_inline_span(self) -> None:
        """Every inline span is held before the gap, not only the first one."""
        out = run_program(self.program, INLINE_SPANS_BODY, "--entity")
        self.assertIn("&nbsp; Next.", out)
        self.assertNotIn("&nbsp; Bar", out.split("`", 1)[0])
        for region in ("`span one with  two spaces`", "`span two with  two spaces`"):
            self.assertIn(region, out)
        for span in re.findall(r"`[^`\n]*`", out):
            self.assertNotIn("&nbsp;", span, "the entity landed inside a span")

    def test_the_entity_mode_holds_a_fenced_block_byte_for_byte(self) -> None:
        """The entity mode holds the code aside exactly as the default mode does.

        The entity is six literal characters, so one that landed inside a code block
        would show inside the block as text.  The block comes back byte for byte, and
        the entity appears only in the prose.
        """
        body = (
            "A sentence.  Next sentence.\n\n"
            "```js\nconst x = 1.  // a comment.  Another.\n```\n\n"
            "Another sentence.  Next one.\n"
        )
        out = run_program(self.program, body, "--entity")
        self.assertIn("```js\nconst x = 1.  // a comment.  Another.\n```\n", out)
        block = out.split("```js\n", 1)[1].split("\n```", 1)[0]
        self.assertNotIn("&nbsp;", block, "the entity landed inside a code block")
        self.assertIn("A sentence.&nbsp; Next sentence.", out)
        self.assertIn("Another sentence.&nbsp; Next one.", out)

    def test_the_entity_mode_is_the_nbsp_mode_with_the_entity_in_place(self) -> None:
        """On a body with no code, the entity mode produces the bytes the default mode
        produces, with the entity in place of the byte pair."""
        prose = "Plain sentence.  Next sentence.\n**Bold sentence.**  Next sentence.\n"
        nbsp = run_program(self.program, prose)
        entity = run_program(self.program, prose, "--entity")
        self.assertEqual(entity, nbsp.replace(NBSP + " ", ENTITY))

    def test_the_entity_mode_agrees_with_the_entity_one_liner(self) -> None:
        """The entity mode is the canonical pattern with the entity in place of the byte
        pair, so on a body with no code it produces the bytes the bare entity one-liner
        produces."""
        prose = "Plain sentence.  Next sentence.\n**Bold sentence.**  Next sentence.\n"
        one = subprocess.run(
            ["perl", "-CSDA", "-pe", _ENTITY_CANONICAL],
            input=prose,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(one.returncode, 0, "the entity one-liner failed")
        self.assertEqual(
            run_program(self.program, prose, "--entity"),
            one.stdout,
            "the entity mode and the entity one-liner disagree on a body with no code",
        )

    def test_the_no_flag_mode_still_writes_the_real_nbsp(self) -> None:
        """The no-flag behaviour must not move:  a GitHub title, body and comment and a
        Zulip post take a real U+00A0 plus a space."""
        out = run_program(self.program, "Plain sentence.  Next sentence.\n")
        self.assertIn("Plain sentence." + NBSP + " Next sentence.", out)
        self.assertNotIn("&nbsp;", out)

    def test_the_entity_mode_is_idempotent(self) -> None:
        """The third check, in the entity form.  A second run must not double the entity
        and must not leave a bare `&nbsp;` behind a second one."""
        out = run_program(self.program, BODY, "--entity")
        again = run_program(self.program, out, "--entity")
        self.assertEqual(again, out, "the entity mode is not idempotent")
        self.assertNotIn("&nbsp;&nbsp;", out, "the entity mode doubled the entity")
        self.assertNotIn("&nbsp;&nbsp;", again, "the entity mode doubled the entity")
        self.assertIsNone(
            re.search(r"&nbsp;(?![ ])", out),
            "the entity mode left a bare entity without its space",
        )

    def test_the_checkout_program_agrees_with_the_pack_copy(self) -> None:
        """The checkout's program, with no flag, produces the same bytes as the program
        the pack carries:  the `--entity` mode is an addition, not a change."""
        match = _HEREDOC_RE.search((ROOT / SURFACES[0]).read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "%s embeds no program" % SURFACES[0])
        body = (
            "Plain sentence.  Next sentence.\n\n```js\nconst x = 1.  // a comment.  Another.\n```\n"
        )
        self.assertEqual(
            run_program(program_text(), body),
            run_program(match.group("program"), body),
            "the checkout's program and the pack's copy no longer agree",
        )

    def test_the_checkout_program_agrees_with_the_pack_copy_in_entity_mode(self) -> None:
        """The pack's embedded program must match the checkout in entity mode too."""
        match = _HEREDOC_RE.search((ROOT / SURFACES[0]).read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "%s embeds no program" % SURFACES[0])
        body = "Plain sentence.  Next sentence.\n"
        self.assertEqual(
            run_program(program_text(), body, "--entity"),
            run_program(match.group("program"), body, "--entity"),
            "the checkout's program and the pack's copy disagree in entity mode",
        )

    def test_the_program_holds_a_fence_that_is_never_closed(self) -> None:
        """An unclosed fence holds the rest of the body, so the rest is not converted.

        The prose in front of the fence is still converted:  the fence is code, and
        what came before it is prose.
        """
        body = "A sentence.  Next sentence.\n```js\nconst x = 1.  More\n"
        out = run_program(self.program, body)
        self.assertIn("```js\nconst x = 1.  More\n", out)
        after = out.split("```js\n", 1)[1]
        self.assertNotIn(NBSP, after)


if __name__ == "__main__":
    unittest.main()
