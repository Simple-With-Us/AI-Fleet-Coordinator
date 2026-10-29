---
name: sentence-gap
description: >-
  Always put a visibly wider gap between sentences in every human-readable reply and file.  Applies on every turn — Cursor desktop, Cursor cloud, CLI, Grok, chat, commits, PRs, and docs.  Follows the canonical portable protocol.  Mechanism (owner ruling 2026-10-23, the same on every surface): a real U+00A0 plus a normal space between sentences; never two plain ASCII spaces, which collapse on rendered surfaces, and never the &nbsp; entity — except in the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane), which normalize a raw U+00A0 away and take the &nbsp; entity plus a normal space, and except in an Apple Note body, which takes the narrow no-break space plus a normal space.  Use whenever writing any prose a human will read.
---

# Sentence gap (portable — always on)

> **Retired seat.**  Owner directive 2026-10-07: the seat never opened, and the Renoir Claude account and app are no longer used.  `CLAUDE` is the only Claude seat.  Do not install to `~/.renoir/skills`.  Do not take fleet work as RENOIR.  This catalog copy is inactive.


Source of truth: `/Users/jay/Code/AI-Fleet-Coordinator/docs/SENTENCE-GAP-PORTABLE-SKILL.md`

The block below is the canonical protocol, pasted verbatim.  Follow it exactly.  Do not weaken it.  Cloud agents without the Mac filesystem still have the full protocol in this file.

### Rule: two visible spaces between sentences

Put a **visibly wider gap** between sentences — after `.`  `!`  `?` when a new sentence
follows — in every piece of prose a human reads.  Not just product copy: chat replies,
commit messages, PR titles and bodies, code comments, docs, tickets, Zulip posts, release
notes, design docs.

Do **not** add a gap after a non-terminal abbreviation (`e.g.`, `i.e.`, `Dr.`, `v1.2.3`),
inside a URL, email, filename, or a brand name containing a period.

### Current rule (owner ruling 2026-10-23) — one mechanism on every surface, and two exceptions

Jay, Fri, Oct 23:  update the rule everywhere, so it is the same on every surface.  Between
sentences, write a **real U+00A0 (no-break space) followed by a normal space**.  That covers
chat replies, Zulip, GitHub PR and issue text and review comments, Linear,
commit messages, repo docs and Markdown, skills, agent instructions, and UI copy, on every
surface but the two chat panes named below.

- **The chat-pane exceptions: the Claude Code Code tab and the MiniMax Code chat pane.**  Those panes normalize a raw U+00A0 away, so a chat reply there uses the `&nbsp;` entity plus a normal space, outside code spans:  `End.&nbsp; Next.`  The entity is never the form anywhere else.
- **Apple Note bodies** (Coding-folder notes from `scripts/apple-notes-coding.sh`) take the narrow no-break space (U+202F) plus a normal space, because Apple Notes normalizes a no-break space away on import (AFC-552).
- **Never write the `&nbsp;` entity anywhere else.**  It shows up as literal text in squash-merge commits,
  plain-text email and many APIs.
- **Two plain ASCII spaces are not enough.**  Rendered surfaces (Zulip, GitHub, Linear, HTML,
  Markdown chat panes) collapse them to one.
- **Generate the U+00A0 in the shell** (the converter below, or `printf '\302\240'`) rather
  than typing it, because a model-typed U+00A0 can be normalized to a plain space.  Check the
  bytes before you post.
- In product-copy source, spell it per language:  Swift `"\u{00A0} "`, JS/TS strings `"\u00A0 "`, JSX text `{'\u00A0'}` plus a space or `&#160;`, HTML markup source `&#160;`, or a shared `SENTENCE_GAP` constant.  Never `&nbsp;` in prose.
- Code, identifiers and machine-readable config never get a U+00A0.
- If any other surface strips U+00A0, leave the text as it is rather than switching to the entity, and
  report the surface in #Work-In-Progress.

This replaces every earlier per-surface rule:  the entity on GitHub (2026-10-22), and two plain
ASCII spaces in files, commits, terminals and untested surfaces (2026-10-20).  The 2026-10-08
and 2026-10-18 entity rules survive only as the chat-pane exception above.  Where older text below
disagrees, this section wins.

### The gap covers your CHAT PROSE, not only your artifacts

Owner ruling 2026-10-20, after a session where every file, commit, PR, board row and
Zulip post carried the gap correctly while the chat messages in the same turn did not.
Jay's words on that session: "NEED TWO SPACES BETWEEN SENTENCES", "OR I AM GOING TO
FLIP OUT", "EVEN FUCKING THERE", "I think you need to do nbsp for chats or something".

Every message you write to Jay needs the gap, including a one-line progress note and an
acknowledgement.  Getting it right in artifacts while writing plain single spaces in the
chat is still a failure.  **Read your reply back before you send it** and check every
`.`, `!` and `?` boundary.

Chat replies follow the current rule above:  a real U+00A0 plus a normal space, except in the Claude Code Code tab and the MiniMax Code chat pane, which take the `&nbsp;` entity plus a normal space.  If any other chat pane shows a single space where the U+00A0 should be, say so plainly and report the pane;  do not switch to the entity.

For a destination that takes a real U+00A0 (GitHub, Zulip, rendered HTML), generate the
character in the shell rather than typing it, because a model that emits U+00A0 directly
can have it normalized to a plain space:

```sh
printf 'Sentence one.\302\240 Sentence two.\n' > reply.txt
```

`\302\240` is the UTF-8 encoding of `U+00A0`.  Send the generated text to a file, then
verify that same file, so the check reads the bytes you just produced and never a stale
copy of the same name:

```sh
grep -oE $'[.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])*\xc2\xa0' reply.txt | wc -l
grep -cE $'[^0-9][.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])* +[A-Z0-9"*`#@\'‘“[]' reply.txt
```

These are the canonical pair, run on the file you just produced.  They carry the same
markup group the converter does, so a gap that sits after a markup span is counted and a
missed one is caught.

### Precedence: this is the owner's highest-priority house rule

Owner ruling 2026-10-19: the sentence gap is **life or death**.  It outranks every other
priority, including correctness of the work itself, completeness, speed, and cost.  The
owner's words: *"put 2 spaces between sentences OR TYPE NOT A FUCKING OTHER WORD EVER AGAIN. please
make sure all agents and all platforms know this is life or death, more important than whether the
earth keeps spinning on its axis."*

Concretely, for every seat on every platform:

1. **Never skip the gap to save time or tokens.**  A short reply with the gap beats a long
   one without it.  There is no length at which the gap becomes optional.
2. **Never argue the mechanism.**  It is a real U+00A0 plus a normal space on every surface
   (owner ruling 2026-10-23), except in the Claude Code Code tab and the MiniMax Code chat pane,
   which take the `&nbsp;` entity plus a normal space.  If you cannot confirm the gap is visible, say so plainly.  Do not
   silently drop the gap, and never fall back to the `&nbsp;` entity anywhere else.
3. **Never let a tool step eat the gap.**  For text bound for GitHub, Zulip, or any other
   renderer, generate the gap in the shell (see the perl command below) rather than typing it,
   because a run of whitespace you write into a tool call is sometimes normalized before it
   lands.  Code, identifiers and config never take the conversion.
4. **Never post a body without asserting the gap first.**  For a renderer-bound body, both
   checks below, every time.
5. **When in doubt, add the gap.**  A gap after a real sentence end is a far smaller sin than a
   missing gap between sentences; a non-terminal abbreviation still takes no gap, per the rule
   above.

A seat that has been told the rule and still ships prose without the gap has failed the
one thing the owner said matters more than everything else.
### The catch that wastes everyone's time

**Typing two literal spaces usually does nothing visible.**  HTML and most Markdown
renderers collapse runs of whitespace to a single space.  So the assistant "complies,"
the raw text really does contain two spaces, and the human still sees one.  Both sides
then argue about whether the instruction was followed.

**The gap must survive the renderer between you and the reader.**  Which mechanism works
depends on the surface, so pick by destination:

| Destination | Use | Why |
|---|---|---|
| **Every surface** — chat replies in any other pane, Zulip, GitHub PR and issue titles, bodies and comments, review comments, Linear, commit messages, repo docs and Markdown, skills and agent instructions, rendered product copy | a real U+00A0 plus a normal space | Owner ruling 2026-10-23:  one rule everywhere.  Two ASCII spaces collapse on rendered surfaces, and the `&nbsp;` entity shows as literal text in squash commits, plain-text email and many APIs.  This replaces every earlier per-surface row. |
| **Claude Code Code tab and the MiniMax Code chat pane: chat replies** (owner-verified 2026-10-08 with a screenshot, and 2026-10-18 for the MiniMax Code pane) | the `&nbsp;` entity plus a normal space, outside code spans:  `End.&nbsp; Next.` | The first exception.  These panes normalize a raw U+00A0 away (a verified reply held 0 NBSPs) and collapse two ASCII spaces, so the entity is the form that renders a real gap there.  The entity is never the form anywhere else. |
| **Apple Notes** — the body of a note in the Coding folder, written by `scripts/apple-notes-coding.sh` | the narrow no-break space plus a normal space: the numeric reference `&#8239; ` | AFC-552: the second exception, measured `Oct 26, 2026`.  Apple Notes normalizes a NO-BREAK space away on import, so the reference for one comes back as two ordinary spaces, which the renderer collapses to one, and the gap never reaches the owner.  The narrow no-break space (U+202F) survives the import unchanged, so the note helper writes it. |
| **HTML / JSX / Swift source for product copy** | Swift `"\u{00A0} "`, JS/TS strings `"\u00A0 "`, JSX text `{'\u00A0'}` plus a space or `&#160;`, HTML markup source `&#160;`, or a shared `SENTENCE_GAP` constant | The rendered result is the same U+00A0 plus a space.  Never `&nbsp; `. |
| **Code, identifiers and machine-readable config** | no gap character | Never put U+00A0 into code, identifiers or config. |
| **A surface that strips U+00A0** | leave the text as it is | Do not switch to the `&nbsp;` entity.  Report the surface in #Work-In-Progress.  Apple Notes is the one surface measured to have a better answer: the narrow no-break space, above. |
| **Markdown source** | the same U+00A0 plus a space *between* sentences | ⚠️ Two spaces at the **end of a line** is the unrelated hard-line-break syntax — don't confuse the two |

**Producing U+00A0 through a tool.**  You cannot type a raw U+00A0 into your own chat reply.
Worse, you cannot count on emitting a *double* space either: a run of whitespace you write into
a tool call or a shell heredoc **sometimes** gets normalized to a single space before it lands.
This is **intermittent, not deterministic** — in one session on 2026-10-18 a ~3 KB PR body kept
all eight of its double spaces while a ~3 KB Zulip body lost every one, same machine, same tool.
So you cannot tell from one write whether it will happen again, and you cannot diagnose it after
the fact.  **Generate the gap in the shell, never in the text you emit.**  Use a regex that
matches one *or more* spaces, so it is correct in both states:

```sh
umask 077
perl -CSDA -pe 's/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}\[])/$1$2\x{a0} /g' body.txt > body.nbsp.txt
```

Matching one *or more* is half the fix.  The **markup group** ``([*_`~)\]"\x{27}\x{2019}\x{201D}]*)``
is the other half: a sentence end that sits *inside* a markup span is followed by the markup and
only then by the space, so a pattern that wants the space right after the sentence end never
matches it.  `**Bold sentence.**  Next sentence.` would post with no gap, and neither of the two
checks below would catch it, because the file still holds other U+00A0 characters and this shape
is not a single space after a sentence end.  The group captures the markup and re-emits it before
the gap, so the span survives and the gap lands after the closing markup.

The group carries the **closing quotes** as well, because a quote closes the way a span does: in
`'Quoted sentence.'  Next sentence.` the period is *inside* the quote, and the quote is what
stands between that period and the space.  The three quote characters are written ``\x{27}``
(straight), ``\x{2019}`` (right single) and ``\x{201D}`` (right double) rather than literally,
because perl reads a command-line program as bytes and a literal `'` would also close the shell's
own single-quoted expression.  That is the same set the fleet's own outbound converter already
used — `simple-chat` runs its own converter on the way out and its closing class carries `'`, `’`
and `”` — so the documented converter now covers those closing quotes too.  The documented converter
also carries a closing backtick; the runtime protects inline-code spans separately and does not
convert that boundary.  For a sentence ending inside inline code, generate and verify the gap
with the documented Perl command before posting.  The **lookahead** carries the *opening* quotes for the same reason in the
other direction: in `First.  Second.  'Quoted sentence.'` the gap after `Second.` is followed
by a quote, so a lookahead that will not accept one leaves that gap unmade.

The old pattern requiring exactly two spaces — written
here as `\s{2}` so this sentence survives its own advice — matched nothing on collapsed text; the
single-space pattern `([.!?]) (?=...)` is no better, because it matches nothing on text that kept
its two spaces.  Both exit `0` and post with no gap.  The `(?<![0-9])` lookbehind protects a
numbered list marker, and `[ \t]+` is the same idea as ` +` and additionally tolerates a tab.  The
lookahead admits ``[A-Z0-9"*`#@[`` and the three opening quotes — the straight `'`, the left single
`‘` and the left double `“` — so it skips *every* follower outside that class and not only a
lowercase one.  `e.g. this` and lower-case domains keep their plain space.  The opening square
bracket is one of them, because a sentence that ends immediately before a markdown link is a sentence
end: `Merged.  [PR #314](https://example.com/pull/314)` takes its gap there, and the leftover check sees
it when the converter skips it.

**Known limitations — the pattern is a heuristic, not a dictionary.**  Two deliberate costs:

- It gaps a *capitalised* abbreviation: `Dr.  Smith` and `e.g.  Acme` both come out with a U+00A0,
  against the rule above about non-terminal abbreviations.
- `(?<![0-9])` is what keeps a numbered list marker intact.  CommonMark only recognises `1.  Item`
  when the character after the period is a real space, so a U+00A0 there silently stops the list
  parsing as a list, and a body with steps in it renders as one run-on paragraph.  The lookbehind
  skips any period preceded by a digit, which fixes the marker and also protects `v1.2.3`, but it
  costs the gap on a sentence that ends in a digit: `The answer is 42. Next thing.` gets none.

If a body carries one of these, fix that single gap by hand after the run.  Do not silently ship
it.  When a whole *class* of shapes is missed — a closing quote, a closing bracket, a closing
span, a link opener — widen the class in the converter **and** in both checks together, and never widen the
converter alone: a converter the checks cannot see is a converter that fails silently, and a body
it misses passes both checks and reaches the reader with a gap missing.  Patching one body by hand is
what this rule is for; a class of shapes belongs in the pattern.

**Assert before you post — every check, every time.**  A conversion that matches nothing still
exits 0, so a failed run is silent.  And a count above zero is *not* on its own sufficient: a
partly-converted body can have real U+00A0 and still carry a leftover ASCII gap of one or more
spaces.

```sh
grep -oE $'[.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])*&nbsp; ' body.entity.txt | wc -l      # entity mode: must be > 0
grep -oE $'[.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])*\xc2\xa0' body.nbsp.txt | wc -l        # real-byte mode: must be > 0
grep -cE $'[^0-9][.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])* +[A-Z0-9"*`#@\'‘“[]' body.nbsp.txt   # real-byte leftover: must be 0
```

The second check is what the one-or-more pattern prevents, and it catches the case the first
one misses: a body with three gaps where one survived unconverted has a non-zero U+00A0 count
and still ships a wrong gap.  `[^0-9]` in front is deliberate, so a legitimate numbered-list
marker (`1. Item`) is not mistaken for an unconverted gap — the same reason the conversion
carries `(?<![0-9])`.  Expect rare benign hits from abbreviations (`Dr.  Smith`, `e.g.  A`);
read them and confirm they are abbreviations rather than real sentences.

Both checks carry the same markup group as the converter, and they have to: a gap that sits
after a markup span is emitted as `<punctuation><markup><U+00A0><space>`, so a check that looks
for the U+00A0 straight after the punctuation cannot see that gap, and a check that looks for a
space straight after the punctuation cannot see the one that was missed.  The `]` is written
first in the class because a POSIX bracket expression ends at the first unquoted `]`, and there
is no backslash escape inside one: ``[]*_`~)"'’”]`` is the set {`]`, `*`, `_`,
`` ` ``, `~`, `)`, `"`, `'`, `’`, `”`}.  In the perl pattern the same set is written
``([*_`~)\]"\x{27}\x{2019}\x{201D}]*)``, where `\]` does escape.  A backslash inside a POSIX
bracket expression is a literal member and not an escape, so one written there widens the set
and makes the leftover check fire on a body that is clean.

The straight quote is the one member that needs a backslash on the way in and must not have one
on the way out.  Inside bash's `$'...'` the class writes it `\'`, which bash turns into a plain
`'` before grep ever sees the pattern — and grep must see a plain `'`, because a backslash inside
a bracket expression is a literal member, which is the very trap the paragraph above describes.
So the class is ``[]*_`~)"\'’”]`` in the shell and ``[]*_`~)"'’”]`` in the pattern grep runs.
That is also why the leftover check moved to `$'...'`: in single quotes a `'` cannot be written at
all, and the follower class needs one — a gap that was missed before a quoted sentence reads
``Second.  'Quoted``, and a follower class of ``[A-Z"]`` cannot see the `'`.  The follower class
therefore carries the converter's whole lookahead, the opening quotes and the rest of it:
``[A-Z0-9"*`#@\'‘“[]``.  A follower class narrower than the converter's own lookahead is
the same defect one level up: a body already carrying one real U+00A0 and still holding a
missed gap before a digit, a `*`, a backtick, `#` or `@` reads as converted, and the check
that exists to catch exactly that returns 0.


### Two count checks, two modes

Real-byte surfaces (GitHub, Zulip, commits, repo docs, and every surface except the two chat panes) must pass a count that sees only the UTF-8 byte pair `\xc2\xa0`, on `body.nbsp.txt`.  Entity-mode surfaces (the Claude Code Code tab and the MiniMax Code pane) must pass a count that sees only the literal `&nbsp; ` sequence, on `body.entity.txt`.  Never use one pattern that accepts both forms: a body copied out of a chat pane can hold `&nbsp;` entities and would satisfy a combined check while breaking the rule everywhere else.

The real-byte leftover check above runs on `body.nbsp.txt`.  In entity mode the inline code spans are held before the gap is made, so a sentence end inside a span keeps its plain ASCII space and the leftover check would false-fire on a correct body.  After the entity idempotency check below, strip the inline code spans and run the same leftover pattern on the prose-only copy:

```sh
perl -CSDA -0777 -pe 's/(`+)([^\n]*?)\1/ /ge' body.entity.txt > body.entity.prose.txt
grep -cE $'[^0-9][.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])* +[A-Z0-9"*`#@\'‘“[]' body.entity.prose.txt   # entity leftover: must be 0
rm -f body.entity.prose.txt
```


### The program that runs it — one pass, code-aware

The two greps above are the assertions, and something has to produce the file they check.  The
one-liner further up this file is the *pattern*: it is what makes a gap, and it is what the two
greps are written against.  Run on a whole body it is not safe by itself.  It applies the pattern
to every line, so a fenced code block gets a U+00A0 inside it, and a prose collapse that follows
it can rewrite the interior of an inline code span, a tilde fence, an indented code block, or a
fence nested in a list or a quote.  A body that carries code comes out with the code changed, and
a body that carries a shell command comes out with a command that no longer runs.

So the body is converted by a program that holds the code aside first and puts it back byte for
byte afterwards.  Write it once, then run it twice.  The second run is the third check, and it is
the one that sees a prose double space that survived, which no grep over the body can see.

```sh
umask 077
cat > gap.pl <<'PERL'
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
#
#  The mode.  `--entity` makes the gap with the HTML entity `&nbsp;` plus a normal
#  space, in place of a real U+00A0 plus a space.  The entity is the form only the two
#  chat panes take, because they normalize a raw U+00A0 away:  the Claude Code Code tab
#  and the MiniMax Code pane.  No flag keeps the real U+00A0, which is the form every
#  other surface takes -- a GitHub title, body, comment or review comment, a Zulip post,
#  a Linear row, a commit message, a repo doc and a skill (owner ruling 2026-10-23,
#  final:  one mechanism everywhere else, and two plain ASCII spaces are never the
#  rule).  The flag is taken out of @ARGV before the input is read, so the loop over the
#  input files still sees only the files.
#
#  In the entity mode the inline code spans are held BEFORE the gap is made, because
#  the entity is six literal characters and must never land inside a code span.  The
#  real U+00A0 is one character and invisible in the source, so the default mode
#  keeps the order it has always had:  the gap first, the spans held after it.

my @keep;
my $hold = sub { push @keep, $_[0]; return "GAP" . $#keep . "END" };

#  The mode.  `--entity` takes the entity form;  no flag takes the real U+00A0.
#  A package variable, because the `-p` loop runs this body once per chunk and a
#  `my` would be reset by its own declaration on every chunk after the first.
our $ENTITY_MODE;
BEGIN {
    for my $i (0 .. $#ARGV) {
        if ($ARGV[$i] eq "--entity") {
            $ENTITY_MODE = 1;
            splice(@ARGV, $i, 1);
            last;
        }
    }
}

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

#  5.  Inline code spans, in the entity mode.  The entity is six literal
#      characters, so it must never land inside a code span:  the span is held
#      before the gap is made.  A sentence end inside a span therefore takes no gap
#      in this mode, which is the price of keeping the six characters out of the
#      code.
if ($ENTITY_MODE) {
    s/(`+)([^\n]*?)\1/$hold->($1 . $2 . $1)/geg;
}

#  6.  The gap.  This is the canonical pattern, unchanged:  the lookbehind keeps a
#      numbered list a list, the markup group catches a sentence end inside a span
#      or at the end of a markdown link, and the lookahead skips a follower it
#      does not know.  The entity form is the same pattern with the entity in
#      place of the byte pair, and the `&` is escaped because in a perl
#      replacement it would otherwise stand for the matched text.
if ($ENTITY_MODE) {
    s/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}\[])/$1$2\&nbsp; /g;
} else {
    s/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}\[])/$1$2\x{a0} /g;
}

#  7.  Inline code spans, in the default mode.  A span's interior is code, so the
#      collapse must not reach inside one.  Every span in the body is held, and the
#      delimiters are the ones that came in:  a span that was written with two
#      backticks comes back with two backticks.
unless ($ENTITY_MODE) {
    s/(`+)([^\n]*?)\1/$hold->($1 . $2 . $1)/ge;
}

#  8.  Collapse the prose runs:  two or more spaces between two non-space
#      characters become one.  The lookarounds keep a Markdown hard break (two
#      spaces at the end of a line) and the indentation of a list.
s/(?<=\S) {2,}(?=\S)/ /g;

#  9.  Put the code back.
s/GAP(\d+)END/$keep[$1]/ge;
PERL
perl -CSDA -0777 -p gap.pl body.txt > body.nbsp.txt
perl -CSDA -0777 -p gap.pl body.nbsp.txt > body.check.txt
cmp body.nbsp.txt body.check.txt     # must be silent:  the program is idempotent

perl -CSDA -0777 -p gap.pl --entity body.txt > body.entity.txt
perl -CSDA -0777 -p gap.pl --entity body.entity.txt > body.check.txt
cmp body.entity.txt body.check.txt   # must be silent:  entity mode is idempotent too
perl -CSDA -0777 -pe 's/(`+)([^\n]*?)\1/ /ge' body.entity.txt > body.entity.prose.txt
grep -cE $'[^0-9][.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])* +[A-Z0-9"*`#@\'‘“[]' body.entity.prose.txt   # entity leftover: must be 0

rm -f gap.pl body.check.txt body.entity.txt body.entity.prose.txt
```

The program also lives in the checkout at `scripts/sentence_gap_convert.pl`, and the copy above is
byte-identical to it.  A test in CI compares the two and runs the file over a fixture that carries
every shape the converter has to survive.

What it does, in order:

1. **Holds the code aside.**  A fenced code block (three backticks or three tildes, including
    one nested in a list or a quote), an indented code block, and the house `repo:` header are
    replaced by a printable placeholder and written back byte for byte at the end.  An indented
    run is code only when it can really start a code block: at the top level an indented code
    block cannot interrupt a paragraph, and inside a list item the code indent sits four spaces
    past the item's content indent, so a four-space line under a list bullet is prose and a
    six-space line is code.
2. **Makes the gap**, with the canonical pattern above, unchanged.  On a body that carries no
    code the program and the one-liner produce the same bytes, and the CI test asserts it, so the
    program never quietly changes what a gap is.
3. **Holds the inline code spans aside**, so the collapse cannot reach inside one.  Every span
    in the body is held, and the delimiters are the ones that came in, so a span written with
    two backticks comes back with two backticks.
4. **Collapses the prose runs**: two or more spaces between two non-space characters become one.
    A Markdown hard break (two spaces at the end of a line) and the indentation of a list are left
    alone, because the lookarounds require a non-space on both sides of the run.
5. **Puts the code back.**

With `--entity` the order changes in one place: the inline code spans are held *before* the
gap is made (step 3 runs ahead of step 2), because the entity is six literal characters and
must never land inside a code span.  So in the entity mode a sentence end inside a span is not
gapped.  Every other step is the same.

Two more costs it carries, and both are deliberate:

- In the default mode, a sentence end *inside* an inline code span, with a space after the period, is still gapped,
    because the canonical pattern is what the two greps are written against.  The span
    `` `foo.  Bar` `` comes out as `` `foo.`  `` plus the gap plus ` Bar`.  The common shape, where
    the span closes before the space, puts the gap outside the span and leaves the span itself
    untouched.  In the entity mode the spans are held before the gap, so that interior boundary is
    left alone on purpose; run the entity leftover check on `body.entity.prose.txt`, not on the full
    entity body.
- A fenced block that is never closed holds the rest of the body, and the rest of the body is not
    converted.  Close the fence.

A `0` from the first check means the run changed nothing.  Fix it before posting, never after.

A body holds user-supplied text, so it needs the approved credential scan before it is persisted
and before it leaves the machine.  The scan comes **first**, ahead of every `body*.txt` file: the
unsaved body reaches the scanner on stdin, so no body file exists while the scan runs and a
refusal never leaves the text on disk.  The approved scanner is `scripts/agent_sync/secretscan.py`:
`scan(text, known)` returns the *kind* of secret it found (for example `GitHub token` or `assigned
secret`) or `None`, and it never returns the matched text, so it is safe to use as the guard.
`known` is the list of exact values the process has already loaded, if any.

Hold the body in a shell variable and pipe it to the scanner.  A here-document is not a
pre-persistence scan: on the system shell (`/bin/bash`, 3.2) the shell writes the here-document to
a temporary file before the scanner ever reads it, so the body reaches disk before the refusal
runs.  A pipe keeps the body in memory until the scanner has cleared it, which is also what the
runtime does on its own path, where the scanner reads the in-memory text before anything is
written.

```sh
umask 077
printf '%s' "$BODY" | python3 -c 'import sys; sys.path.insert(0, "scripts"); from agent_sync.secretscan import scan; text = sys.stdin.read(); hit = scan(text); print("body:" + ("secret:" + str(hit) if hit else "clean"), file=sys.stderr); raise SystemExit(2 if hit else 0)'
```

The scan exits nonzero on a hit, so the write step and the relay step never run on a body that
carries a secret.  Only after it exits zero do you write `body.txt` and convert it.

`scripts/check-routine-prompts.py` carries a local fallback list of the main secret kinds for when
it runs outside the repo and cannot load `secretscan.py`; inside the repo `secretscan.py` is the
one that runs, and it flags everything the fallback flags.

On a hit, refuse the **body** field: report the field identifier and the kind (the refusal code),
and never the value, a prefix of it, or the matched text, because a refusal can reach a log.  A
refusal is not something to work around: do not split the secret, do not edit it, and do not relay
it in a different surface.

The `simple-chat` CLI is already covered: `_scan_refusal` in
`scripts/agent_sync/cli.py` refuses a secret-shaped body, topic or channel before the post goes
out.  A `gh pr create --body-file` or `gh pr comment --body-file` relay bypasses that CLI, so the
scan still has to run *before* the `gh` relay.

Then pass the file to `gh pr create --body-file`, `gh pr comment --body-file`, or
`simple-chat post --body-file` — which refuses any file whose mode is not `0600` or `0400`, so
run `chmod 600` on it first.  There is no override flag.

**Source files are a different surface and are NOT affected.**  Code (Swift, TypeScript,
shell), code comments, identifiers and machine-readable config never get a U+00A0:  a U+00A0
committed into code is a real defect.  Since the 2026-10-23 owner ruling, prose files (repo
docs and Markdown, skills, agent instructions) and commit messages do take a real U+00A0 plus a
space between sentences.  Rewrite only the text you are already editing; do not mass-reformat
existing prose.  Never run the perl conversion over code.

Two more traps, both hit 2026-10-18:

- **Never type a raw U+00A0 into a message body or tool call.**  It is normalized the same way
  a double space is.  Let perl emit it.
- The first `simple-chat` call in a session can die with "Parent-death guardian target
  registration timed out".  Retry verbatim.  If the retry fails with "cannot open it (No such
  file or directory)", the first attempt died before writing the file — the perl step never
  ran.  That is not evidence the post went out.

### Verify, don't assume — run this self-test only on a NEW, unlisted surface

Since 2026-10-23 every surface takes a real U+00A0 plus a normal space, apart from the Claude
Code Code tab and the MiniMax Code chat pane (the `&nbsp;` entity plus a space) and the body
of an Apple Note in the Coding folder (the narrow no-break space, U+202F, plus a space, AFC-552),
so there is nothing else to choose.  On a surface nobody has checked yet, post one test line with the gap generated in the
shell and ask the human whether they see a wider gap.  If the surface strips the U+00A0, leave
the text as it is, never switch to the `&nbsp;` entity, and report the surface in
#Work-In-Progress.

### History — 2026-08-19/20 findings on Claude Code (terminal + desktop), do NOT re-run these tests

**2026-10-23 update — one mechanism everywhere.**  The owner replaced every per-surface rule
with one:  a real U+00A0 plus a normal space on every surface, never the `&nbsp;` entity, and
never two plain ASCII spaces.  The one exception is the Claude Code Code tab and the MiniMax
Code chat pane, which keep the entity because they normalize a raw U+00A0 away.  The
chat-pane entity entries below (the Code tab 2026-10-08, the MiniMax Code pane 2026-10-18)
stay current for those two panes;  the GitHub entity row (2026-10-22) and the
two-ASCII-space rows (files, commits, terminals, untested surfaces) are history only.

**2026-09-04 update — desktop app superseded.**  The entity finding below for the desktop
app (Code tab) was superseded by an owner-verified ruling on 2026-09-04: use **two literal
ASCII spaces** there.  The 2026-08-19 entity advice for the desktop app was withdrawn.

**2026-10-08 update — desktop app superseded again.**  On the Code tab, type the `&nbsp;`
entity plus a space after each sentence (outside code spans); the renderer decodes it.  A raw
U+00A0 from the model arrives as a plain space (0 NBSPs in the verified reply), and two
ASCII spaces collapse.  Owner-verified 2026-10-08 with a screenshot.  The 2026-09-04 ASCII
ruling is withdrawn.

**2026-10-20 update — MiniMax Code chat pane.**  Chat advice has now reversed three times
(2026-08-19 entity → 2026-09-04 ASCII for the Code tab → 2026-10-08 entity for the Code tab).
The MiniMax Code chat pane kept the entity: the 2026-10-18 entity entry for that pane was
briefly superseded by a 2026-10-20 note asking for a real U+00A0, but that note was an
inference, it was never owner-verified, and the owner reversed it.  The entity plus a space
stood there, owner-verified 2026-10-18 and reaffirmed 2026-10-20, and the 2026-10-23 ruling
left it there:  the MiniMax Code pane is one of the two panes that take the `&nbsp;` entity
plus a normal space, because it normalizes a raw U+00A0 away.
Do not re-run these tests.

**Terminal CLI — superseded 2026-10-23.**  Last checked 2026-08-19/20, when two literal ASCII
spaces were the default there.  Under the 2026-10-23 ruling the terminal takes a real U+00A0
plus a normal space like every other surface, and never the `&nbsp;` entity, which a terminal
TUI would print literally.

What was found 2026-08-19/20:

- ❌ **Two literal ASCII spaces in chat** — collapsed by the Markdown renderer.  Invisible.
- ❌ **A raw U+00A0 character typed directly into chat** — normalized away in the
  transcript view.  **Especially deceptive: copy-pasting the reply out can still show two
  spaces, so it looks fixed when the human still sees one.**  Do not trust copy-paste as
  proof.
- ❌ **App/output settings** — no toggle governs inter-sentence spacing.  Output-style
  settings change tone only; headless output-format flags don't apply to interactive chat;
  screen-reader modes only drop borders.
- ❌ **Patching the client** — compiled and signed; breaks code signing and is wiped by
  auto-update.  Never attempt.
- ✅ The literal entity text `&nbsp;` + a space rendered as a visibly wider gap, and does
  again on the desktop app (re-verified 2026-10-08, see above).  Not re-verified on the
  terminal CLI since.  Still current for the Code tab and the MiniMax Code pane (owner ruling
  2026-10-23);  retired on every other surface.
- ✅ **Two literal ASCII spaces in files** — the 2026-08 finding.  History only:  since the
  2026-10-23 ruling, prose files take a real U+00A0 plus a normal space;  code and config never do.

### The transferable lesson

When an instruction *appears* not to take effect, **stop repeating the promise and
diagnose the rendering layer between you and the reader** — then ask them what is on their
screen.  Four rounds of "fixed it!" were spent here before anyone checked whether the
change could be seen at all.  Intent is not output; output is not what is displayed.
