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

#  4.  Indented code blocks.  A run of indented lines is code only when it can
#      really start a code block, and how far it has to be indented depends on
#      where it sits:
#
#        -  at the top level an indented code block cannot interrupt a paragraph,
#           so a run that follows a paragraph line is a lazy continuation of it and
#           stays prose;
#        -  inside a list item the code indent is four spaces past the item's
#           content indent, so under a "- " bullet a four-space line is prose and
#           a six-space line is code.
#
#      A line at the wrong indent is prose, and prose is what the gap and the
#      collapse belong to.
{
    my $cols = sub {
        my ($text) = @_;
        my $width = 0;
        for my $ch (split //, $text) {
            $width += ($ch eq "\t") ? 4 : 1;
        }
        return $width;
    };

    #  The blockquote depth of a line, the indent of its content, and the content
    #  with the quote markers taken off.
    my $measure = sub {
        my ($line) = @_;
        my $depth = 0;
        my $rest = $line;
        while ($rest =~ /^([ \t]*)(>)([ \t]?)/) {
            $depth++;
            $rest = substr($rest, length($1) + 1 + length($3));
        }
        my ($lead) = $rest =~ /^([ \t]*)/;
        return ($depth, $cols->($lead), $rest);
    };

    my @lines = split /(?<=\n)/, $_;
    my @out;
    my @list;             #  the content indents of the open list items
    my $paragraph = 0;    #  is a top-level paragraph open?
    my $depth = -1;       #  the blockquote depth of the last line
    my $i = 0;
    while ($i < @lines) {
        my $line = $lines[$i];
        if ($line !~ /\S/) {
            #  A blank line ends the paragraph and does not end a list item.
            $paragraph = 0;
            push @out, $line;
            $i++;
            next;
        }
        my ($qdepth, $width, $rest) = $measure->($line);
        if ($qdepth != $depth) {
            #  A different blockquote context:  the paragraph does not carry over.
            $depth = $qdepth;
            $paragraph = 0;
        }
        #  A line that is indented less than the innermost open item leaves it.
        while (@list && $width < $list[-1]) {
            pop @list;
        }
        my $base = @list ? $list[-1] : 0;
        if ($width - $base >= 4) {
            if ($paragraph && !$base) {
                #  An indented code block cannot interrupt a top-level paragraph, so
                #  this line continues it and stays prose.
                push @out, $line;
                $i++;
                next;
            }
            #  Hold the whole run, byte for byte.
            my $run = $line;
            $i++;
            while ($i < @lines) {
                my $next = $lines[$i];
                last if $next !~ /\S/;
                my (undef, $nextwidth) = $measure->($next);
                last if $nextwidth - $base < 4;
                $run .= $next;
                $i++;
            }
            push @out, $hold->($run);
            $paragraph = 0;
            next;
        }
        if ($line =~ /^GAP\d+END\r?\n?$/) {
            #  A held fence or header is a block of its own, not a paragraph.
            $paragraph = 0;
            push @out, $line;
            $i++;
            next;
        }
        if ($rest =~ /^([ \t]*)([-*+]|\d{1,9}[.)])([ \t]+|$)/) {
            #  A list marker opens an item, and the item's content starts after
            #  the marker.
            my $marker = $2;
            my $spaces = $3 eq "" ? 1 : $cols->($3);
            $spaces = 1 if $spaces > 4;
            push @list, $width + length($marker) + $spaces;
            $paragraph = 1;
            push @out, $line;
            $i++;
            next;
        }
        #  Ordinary prose.
        $paragraph = 1;
        push @out, $line;
        $i++;
    }
    $_ = join "", @out;
}

#  5.  The gap.  This is the canonical pattern, unchanged:  the lookbehind keeps a
#      numbered list a list, the markup group catches a sentence end inside a span
#      or at the end of a markdown link, and the lookahead skips a follower it
#      does not know.
s/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}])/$1$2\x{a0} /g;

#  6.  Inline code spans.  A span's interior is code, so the collapse must not
#      reach inside one.  Every span in the body is held, and the delimiters are
#      the ones that came in:  a span that was written with two backticks comes
#      back with two backticks.
s/(`+)([^\n]*?)\1/$hold->($1 . $2 . $1)/ge;

#  7.  Collapse the prose runs:  two or more spaces between two non-space
#      characters become one.  The lookarounds keep a Markdown hard break (two
#      spaces at the end of a line) and the indentation of a list.
s/(?<=\S) {2,}(?=\S)/ /g;

#  8.  Put the code back.
s/GAP(\d+)END/$keep[$1]/ge;
