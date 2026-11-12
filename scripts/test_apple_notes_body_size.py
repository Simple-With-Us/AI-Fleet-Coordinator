"""A note body larger than the command line can hold must still reach the note.

macOS gives a process 1048576 bytes for the whole command line, the arguments and the environment
together.  The helper used to hand the whole note body to the step that wraps it in the note HTML
as an argument, so a body that did not fit died with "Argument list too long" (exit 126) before the
linkifier ran and before the note was written.  The pinned Background Jobs Master List is about
1 MB of HTML, so it silently went stale and nobody saw an error, because the failure was inside a
command substitution and the note kept the content it already had.

The body travels on stdin now, and the title is the only thing that travels as an argument.  These
tests drive the script through the preview hook, which prints the HTML the note would get, so
nothing touches Notes.
"""

from __future__ import annotations

import subprocess
import unittest

from test_apple_notes_linkify import (
    SCRIPT,
    SESSION,
    LinkifyTestCase,
)

#  The size of the body the big test uses.  It has to clear the 1048576 byte budget for the whole
#  command line, with room to spare, because the HTML a markdown body turns into is longer than the
#  body itself and the environment takes its share.
BODY_BYTES = 1100000

#  The last line of the body.  It has to survive into the HTML, so the tests can prove the body was
#  not cut short on the way.
END_MARKER = "END-OF-THE-LARGE-BODY-MARKER"

#  A line of filler with nothing in it the linkifier would touch:  no team key, no issue
#  identifier, no pull request reference.  The body here is about size, not about linking.
FILLER_LINE = (
    "The registry lists the helper, the seat that owns it, and every other seat that runs it."
)


class BodySizeTests(LinkifyTestCase):
    def large_body(self, size: int = BODY_BYTES) -> str:
        """A body over the command line budget, ending in a marker the test can look for."""
        line = FILLER_LINE + "\n"
        count = max(1, size // len(line))
        return (line * count) + END_MARKER + "\n"

    def preview_from_stdin(self, title: str, body: str, **env: str) -> subprocess.CompletedProcess:
        """The preview of the note, with the body arriving the way the registry refresh sends it.

        The registry refresh pipes the file into the script, so the body arrives on stdin and never
        as an argument.  That is the path this bug lived on.
        """
        return self.run_script(
            "--session",
            SESSION,
            title,
            APPLE_NOTES_PRINT_HTML="1",
            body=body,
            **env,
        )

    def test_a_body_larger_than_the_command_line_still_reaches_the_note(self):
        #  The body over the budget used to cost the note its whole body.  The preview must now come
        #  back whole, with the marker at the end of it.
        body = self.large_body()
        self.assertGreater(len(body), 1048576, "the fixture must be over the command line budget")

        p = self.preview_from_stdin("[AFC, MM] Big Note", body)

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Argument list too long", p.stderr)
        self.assertIn(END_MARKER, p.stdout)

    def test_the_body_is_not_cut_short(self):
        #  A body that arrives whole is not the same as a body that arrives in part.  The preview
        #  has to carry at least as much text as the body, because the HTML wraps each line.
        body = self.large_body()

        p = self.preview_from_stdin("[AFC, MM] Big Note", body)

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertGreaterEqual(
            len(p.stdout),
            len(body),
            "the preview lost text the body had",
        )
        self.assertIn(END_MARKER, p.stdout)

    def test_a_body_from_a_file_reaches_the_note_too(self):
        #  The other production path:  the body in a file, named by --html.  It reads the file, so
        #  it was never over the budget, and it has to keep working the same way.
        body = self.large_body(300000)
        path = self.tmp / "big.md"
        path.write_text(body)

        p = self.run_script(
            "--session",
            SESSION,
            "--html",
            str(path),
            "[AFC, MM] Big Note",
            APPLE_NOTES_PRINT_HTML="1",
        )

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Argument list too long", p.stderr)
        self.assertIn(END_MARKER, p.stdout)

    def test_the_helper_never_passes_the_body_as_an_argument(self):
        #  The pin on the shape of the fix, so the body cannot creep back onto the command line.
        #  The wrapper step reads the body from stdin and takes the title from the environment.
        source = SCRIPT.read_text(encoding="utf-8")
        marker = """printf '%s' "$BODY_HTML" | NOTE_TITLE="$TITLE" /usr/bin/python3"""

        self.assertIn(marker, source)
        self.assertNotIn('"$BODY_HTML")', source)

        #  The wrapper block on its own, so the check is about the body and not about the other
        #  places the helper runs a program with arguments.
        start = source.rindex("FULL_HTML=$(", 0, source.index(marker))
        end = source.index("\n')", source.index(marker)) + 3
        wrapper = source[start:end]

        self.assertNotIn("sys.argv[2]", wrapper)
        self.assertIn("sys.stdin.read()", wrapper)
        self.assertIn("NOTE_TITLE", wrapper)

    def test_the_command_line_still_takes_the_title(self):
        #  The title is short and stays an argument, so a caller can still name the note the way it
        #  always has.
        p = self.preview_from_stdin("[AFC, MM] Big Note", "A short body.")

        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("<h1>[AFC, MM] Big Note</h1>", p.stdout)


if __name__ == "__main__":
    unittest.main()
