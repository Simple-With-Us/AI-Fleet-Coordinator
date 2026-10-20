#!/usr/bin/perl
#
#  The fleet's code-aware sentence-gap converter.
#
#  One run does four things, in this order:
#
#    1.  hold aside the regions whose bytes must not change:  a fenced code block
#        (three backticks or three tildes, including one nested in a list or a
#        quote), an indented code block, and the house `repo:` header;
#    2.  make the gap, with the canonical pattern, unchanged;
#    3.  hold aside the inline code spans, so the collapse cannot reach inside one;
#    4.  collapse the prose runs, and put every held region back byte for byte.
#
#  A held region is replaced by a printable placeholder, so no control character can
#  reach the output, and it is written back exactly as it came in.  A body that
#  carries code comes out with the code untouched.
#
#  The program is idempotent:  running it on its own output changes nothing.  That
#  is the third check, and it is the one that sees a prose double space that
#  survived, which no grep over the body can see.

my @keep;
my $hold = sub { push @keep, $_[0]; return "GAP" . $#keep . "END" };

#  1.  Fenced code blocks that are closed.  The closing fence carries the same
#      marker, and it may sit in a list or a quote like the opening one.
s/^([ \t]*(?:>[ \t]*)*(?:[-+*]|\d{1,9}[.)])?[ \t]*)((?:`{3,})|(?:~{3,}))[^\n]*\n(?:.*?^[ \t]*(?:>[ \t]*)*(?:[-+*]|\d{1,9}[.)])?[ \t]*\2[ \t]*$)/$hold->($&)/gems;

#  2.  A fence that is never closed holds the rest of the body.  This is a second
#      substitution on purpose:  one alternation cannot express it, because the
#      branch that looks for the closing fence scans to the end of the body before
#      it fails, and the fallback branch is only reached once that scan is over --
#      at which point the position is back at the opening fence and the end of the
#      body is behind it.  So the two cases are two substitutions.
s/^([ \t]*(?:>[ \t]*)*(?:[-+*]|\d{1,9}[.)])?[ \t]*)((?:`{3,})|(?:~{3,}))[^\n]*\n.*\z/$hold->($&)/gmes;

#  3.  The house closeout header.  It is a template, not prose.
s/^([ \t]*repo:.*)$/$hold->($1)/gme;

#  4.  Indented code blocks.
s/((?:^(?:>[ \t]*)*(?: {4,}|\t).*\n?)+)/$hold->($1)/gme;

#  5.  The gap.  This is the canonical pattern, unchanged:  the lookbehind keeps a
#      numbered list a list, the markup group catches a sentence end inside a span
#      or at the end of a markdown link, and the lookahead skips a follower it
#      does not know.
s/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}])/$1$2\x{a0} /g;

#  6.  Inline code spans.  A span's interior is code, so the collapse must not
#      reach inside one.
s/(`+)([^\n]*?)\1/$hold->("`$2`")/ge;

#  7.  Collapse the prose runs:  two or more spaces between two non-space
#      characters become one.  The lookarounds keep a Markdown hard break (two
#      spaces at the end of a line) and the indentation of a list.
s/(?<=\S) {2,}(?=\S)/ /g;

#  8.  Put the code back.
s/GAP(\d+)END/$keep[$1]/ge;
