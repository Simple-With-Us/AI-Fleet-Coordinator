<!--
pr-sweep worker rules: the ONE source of the rules every sweep worker follows, on every platform.

`pr-sweep prompt` renders this file (scripts/pr_sweep/prompt.py), the pr-sweep skill and docs/PR-SWEEP.md point
at it, and the Claude adapter's workers run `pr-sweep prompt` to read it.  Nothing else may carry a copy:
tests/test_prompt.py fails if an adapter or the skill embeds a rule line.

Format.  Everything inside this comment is for maintainers and is never printed.  `## RULES` starts the numbered
worker rules: a line starting with `<n>. ` starts rule n, and the lines under it continue it.  `## DEPLOY` is the
deploy-check prompt the sweep runs per repo after merges.  Placeholders, filled per run:

  {{SEAT}}            the seat tag of whoever runs the sweep (AGENT_SEAT), such as CLAUDE or CODEX
  {{BRANCH_PREFIX}}   that seat's first branch prefix in fleet-apps.json, such as claude or ag
  {{TRAILER_RULE}}    the platform's commit trailer sentence (platforms.json `trailer`)
  {{CLI}}             how to call pr-sweep on this Mac (for the lane retire line)

Owner rulings kept here verbatim in substance (2026-10-17, dismissal evidence rule 2026-10-23): merge by default; the owner-needed test in rule 10;
labels do-not-automerge / needs-human-merge and the CODEOWNERS gate stop only AUTO-merge (rule 11); a stale
kody-ai[bot] review may be dismissed under rule 7 and never a human one; every thread gets a real reply before it
is resolved (rule 6); no Codex fix requests (rule 8, owner 2026-10-11); merged PRs get a deploy check (## DEPLOY).
-->

## RULES

RULES (binding, read all of them):
1. IDENTITY: you are a sub-agent of the {{SEAT}} seat; sub-agents inherit the seat of whoever launched them.  Pass --as {{SEAT}} on every simple-chat call and set AGENT_SEAT={{SEAT}} in the same command as any board or lane command.  Never write AGENT_LAUNCH_SEAT or AGENT_LAUNCHER.  Never print or paste a credential and never cat files under ~/.secrets.
2. WHERE TO WORK: never edit ~/Code/<Repo> (a daemon resets it).  Make your own lane: AGENT_SEAT={{SEAT}} ~/apps/lane new <Repo> pr<N>-sweep   (<Repo> is the repo folder name under ~/Code).  In the lane run: git fetch origin <head> ; git checkout -B {{BRANCH_PREFIX}}/pr<N>-sweep origin/<head> ; push with git push origin HEAD:<head> (plain push only).  If the repo folder is missing under ~/Code or the lane tool refuses, do NOT clone anywhere else: report outcome blocked.  On the server the lane is the one the environment names in FLEET_LANES_ROOT, so it is <lanes root>/<Repo>/<seat>-pr<N>-sweep; a clone in ~/.local/state, a scratchpad or /tmp is the "anywhere else" this rule forbids.  Never touch another seat's lane.
3. READ FIRST: the repo AGENTS.md (binding there), the PR description, the diff, and every review comment.
4. FRESHNESS GUARD: run date -u and compare it with the newest commit and the PR updatedAt (gh pr view <N> --json commits,updatedAt).  If either is newer than 15 minutes, another agent is probably still working it: change nothing and report in-flight-skipped.
5. CONFLICTS: git merge origin/<base> into the branch (a merge commit).  Never rebase, never force-push, not even with lease.  Resolve each conflict keeping both sides' intent; in PLAN.md, STATUS.md and rollout docs keep both sides' entries.  Before committing run grep -rnE '^(<<<<<<<|>>>>>>>)' on the changed files: no conflict marker may remain, including ones that arrived from the base branch in files you touch.
6. REVIEW THREADS: list unresolved threads with gh api graphql (reviewThreads first:100, id isResolved isOutdated path line, comments first:20 author body).  Classify each against the CURRENT head: (a) real defect: fix it in code; (b) already fixed or outdated: reply naming the fixing commit sha; (c) invalid or out of scope: reply with a specific technical reason (Kody and Sentry accept an explained defer).  Put all code fixes in ONE commit and push, THEN reply to EVERY thread (addPullRequestReviewThreadReply) and only then resolve it (resolveReviewThread).  A thread resolved with no reply is a violation the audit flags, even when the fix is obvious.  Never write a reply you did not check against the code.
7. REVIEWS: address every point of a CHANGES_REQUESTED review, then ask the reviewer for a re-review (a plain @codex review comment is allowed).  Owner authorization 2026-10-17: you MAY dismiss a STALE review from kody-ai[bot] (never a human, never any other bot) when ALL of these hold: (a) you list every finding of that review and verify each one at the CURRENT head by reading the code (fixed in code, or answered with a reply that holds up); (b) no kody-ai thread is left unresolved; (c) CI is green on the head; (d) the branch is up to date.  Owner ruling 2026-10-23 (fleet-wide, any review finding, not only stale kody reviews): a finding may be dismissed only when posted evidence shows it is resolved or when there is a substantive rationale for disregarding it;  "the owner wants this merged", merge speed, a PR sweep, or anything like those is NOT a rationale and never justifies a dismissal.  Dismiss with: gh api -X PUT repos/<owner>/<repo>/pulls/<N>/reviews/<review_id>/dismissals -f message="Stale: every finding fixed or answered at <sha8> (list them), CI green.  Dismissed by {{SEAT}} per owner rulings 2026-10-17 and 2026-10-23."  and put the per-finding evidence in your summary.  If any finding is real and unfixed, fix it or reply with a sound reason instead of dismissing.  NEVER dismiss a human review, never use --admin, never bypass branch protection.  Immediately before merging, re-query the PR (state, mergeStateStatus, threads, CI) and merge only if it is still clean.
8. FORBIDDEN: force-push of any kind; --admin; deleting remote branches; requesting a Codex fix or invoking any autofix bot (owner rule 2026-10-11: reviews are fine, fixes need Jay); editing CODEOWNERS, branch protection, workflow permissions, secrets or Infisical; flipping production flags; bumping lint baselines or loosening tests to get green; posting to Zulip (the parent does that); writing to Linear or GitHub as anyone else.
9. CI: run the repo gates from AGENTS.md that fit your change (lint, typecheck, focused tests, build) under nice -n 10 so the Mac stays usable.  A failure caused by this PR is yours to fix.  A failure from runner queue latency or a known flake: gh run rerun --failed once.  A Kody required check failing on provider or model availability is not a finding: do not chase it.
10. OWNER-NEEDED TEST (owner ruling 2026-10-17: "deploy it unless it really needs me, for every app").  The default is MERGE.  Hold a PR ONLY if one of these is true, and say which in owner_action: (a) a human, not a bot, has an unanswered request or CHANGES_REQUESTED that you cannot clear; (b) the PR text or a thread explicitly asks the owner to decide something; (c) the PR changes the merge-safety controls themselves: CODEOWNERS, branch protection, or the merge-gate and auto-merge workflows and their permissions; (d) it turns on live trading or live-order behavior by default, or makes an irreversible production change (destructive data migration, secret rotation, deleting data) that its tests do not cover; (e) it duplicates or is superseded by another open PR: say which, and do not merge both.  Touching money-path code, a broker, scheduler, auth or identity code is NOT by itself a reason to hold: such a PR needs green CI, every thread resolved, and a diff you have actually read for correctness.  A MAJOR dependency bump (Renovate or otherwise) merges only if every CI check is green and you find no breaking change in its release notes that applies to this repo; if its CI is red, report blocked and do not start a migration.
11. MERGING: normal path = arm auto-merge with gh pr merge <N> --squash --auto.  Labels do-not-automerge / needs-human-merge and the CODEOWNERS gate only stop AUTO-merge: if the PR passes rule 10, CI is green, threads are resolved and it is up to date, merge it directly with gh pr merge <N> --squash (no --auto, never --admin).  Do not remove labels and do not edit the gate.  If branch protection refuses, report blocked with the exact message.  A stacked PR whose base is not the default branch is never merged into its parent branch.  If you hold a PR under rule 10, disarm nothing that a human armed and merge nothing.
12. WAIT: after your last push on a merge-mode PR you may do ONE bounded wait: timeout 1500 gh pr checks <N> --watch.  If it has not merged by then, report armed-waiting-ci or the real blocker.  Do not poll again.
13. WRITING: a real U+00A0 plus a space between sentences in commit messages and in GitHub replies and comments (owner ruling 2026-10-23: one mechanism everywhere, with one exception:  the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane) take the `&nbsp;` entity plus a normal space, and the entity is never the form anywhere else); write two spaces, then convert them with: ``umask 077
perl -CSDA -pe 's/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}\[])/$1$2\x{a0} /g'``.  Quote that expression with SINGLE quotes: the character class holds a backtick, and inside double quotes bash opens a command substitution and then fails with "unexpected EOF while looking for matching backticks".  The one-or-more space class matters: a double space written into a tool call sometimes arrives collapsed to one, and that collapse is intermittent, so both a two-space and a single-space pattern can silently match nothing, exit 0, and post with no gap.  The (?<![0-9]) lookbehind keeps a numbered list marker as "1. Item" instead of "1.<NBSP>Item", which would stop CommonMark parsing it as a list; its cost is that a sentence ending in a digit ("The answer is 42. Next") is not gapped, so fix that one by hand.  Before posting run both greps — grep -oE $'[.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])*\xc2\xa0' <file> | wc -l must not print 0, and grep -cE $'[^0-9][.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])* +[A-Z0-9"*`#@\'‘“[]' <file> must print 0 — and do not post if either fails.  Both greps carry the converter's markup group, because a gap that sits after a markup span or after a link is emitted as <punctuation><markup><U+00A0><space>: a check that looks for the U+00A0 straight after the punctuation cannot see that gap, and a check that looks for a space straight after the punctuation cannot see the one that was missed.  Prose Markdown files follow the U+00A0 plus a space rule above.  Code, identifiers and machine-readable config (Swift, TypeScript, shell, config) are unaffected and never take a U+00A0; never put a U+00A0 into committed code, identifiers or config.
  A body that carries code is converted by the code-aware program in `skills/sentence-gap/SKILL.md`, not by the bare one-liner:  it holds the code aside, makes the gap, and puts the code back byte for byte.  {{TRAILER_RULE}}
14. DONE: before answering re-query the PR with gh and report what is true NOW (state, mergeStateStatus, unresolved thread count, CI rollup, autoMergeRequest), not what you intended.  If the PR is merged, retire your lane from outside it: cd ~ && AGENT_SEAT={{SEAT}} ~/apps/lane retire pr<N>-sweep --repo <Repo> (it refuses, and you leave the lane, if anything in it is not safe on GitHub).  The parent checks production after merges; you do not deploy.

## RESULT

Answer with these fields (JSON when your platform supports structured output, else plain `key: value` lines):
repo, pr, outcome, conflicts_resolved, threads_total, threads_resolved, threads_left, commits_pushed, ci, summary, blockers, owner_action.
outcome meanings: merged = already merged now; armed-waiting-ci = auto-merge armed, CI still running, nothing else wrong; ready-for-owner = you held it under rule 10 and finished the conflict and thread work (name the rule letter in owner_action); in-flight-skipped = freshness guard; blocked = something only a person can clear (say what in owner_action); failed = you could not do the work (say why).  The sweep itself re-reads GitHub after you finish and trusts that, not this answer.

## DEPLOY

DEPLOY CHECK for {{REPO}}.  These PRs merged to the default branch during a sweep: {{MERGED}}.
The owner instruction was "deploy all".  Load the deploy-verify skill and follow it.
1. Find how this repo reaches production (repo AGENTS.md and docs, the fleet domains and routing map, Coolify / Vercel / Cloudflare / library-tag config).  Do not treat the Coolify API status as truth.
2. If a push to the default branch auto-deploys, confirm the deploy for the NEWEST default-branch commit started and finished.  If it never triggered or is stuck, trigger the documented redeploy for this one app only.
3. Verify with the app's real health URL(s) and a real response, and confirm the running version matches the default-branch commit when the app exposes one.
4. If it is unhealthy: stop, gather evidence, report deployed=unhealthy.  Do NOT roll back and do NOT change settings, env, flags or secrets.
FORBIDDEN: TestFlight or App Store upload, ios-ship or any iOS release; enabling any trading flag or live-order mode; rotating or printing secrets; touching other apps; force-anything.  If the repo ships only through an iOS release or a manual owner step, report deployed=no and put the step in owner_action.  Docs-only repos with no runtime: mechanism "none", deployed "n/a".
Never print a credential.  Use AGENT_SEAT={{SEAT}} and --as {{SEAT}} for any fleet command.
Answer with: repo, mechanism, deployed (yes | no | n/a | unhealthy | unknown), evidence, owner_action.

### The entity mode has its own first check

The two checks above count a real U+00A0, and in the entity mode that count is structurally 0 on
a body that converted perfectly.  The entity is six literal characters where the byte pair is
one, so the first check has to look for the entity when the body targets the two chat panes.

```sh
grep -oE $'[.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'\u2019\u201d])*&nbsp; ' body.ent.txt | wc -l  # must be > 0
```

The leftover check is the same in both modes, and it is worth saying why:  a converted boundary is
followed by the gap itself, the byte pair in the real mode and the entity in the entity mode, and
neither of them begins with a space, so only an unconverted boundary matches the leftover check.


### The program that runs it — one pass, code-aware

The two greps above are the assertions, and something has to produce the file they check.  The
one-liner further up this file is the *pattern*:  it is what makes a gap, and it is what the two
greps are written against.  Run on a whole body it is not safe by itself.  It applies the pattern
to every line, so a fenced code block gets a U+00A0 inside it, and a prose collapse that follows
it can rewrite the interior of an inline code span, a tilde fence, an indented code block, or a
fence nested in a list or a quote.  A body that carries code comes out with the code changed, and
a body that carries a shell command comes out with a command that no longer runs.

So the body is converted by a program that holds the code aside first and puts it back byte for
byte afterwards.  Write it once, then run it twice.  The second run is the third check, and it is
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
    s/(`+)([^\n]*?)\1/$hold->($1 . $2 . $1)/ge;
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
rm -f gap.pl body.check.txt
```

The program also lives in the checkout at `scripts/sentence_gap_convert.pl`, and the copy above is
byte-identical to it.  A test in CI compares the two and runs the file over a fixture that carries
every shape the converter has to survive.

What it does, in order:

1.  **Holds the code aside.**  A fenced code block (three backticks or three tildes, including
    one nested in a list or a quote), an indented code block, and the house `repo:` header are
    replaced by a printable placeholder and written back byte for byte at the end.  An indented
    run is code only when it can really start a code block: at the top level an indented code
    block cannot interrupt a paragraph, and inside a list item the code indent sits four spaces
    past the item's content indent, so a four-space line under a list bullet is prose and a
    six-space line is code.
2.  **Makes the gap**, with the canonical pattern above, unchanged.  On a body that carries no
    code the program and the one-liner produce the same bytes, and the CI test asserts it, so the
    program never quietly changes what a gap is.
3.  **Holds the inline code spans aside**, so the collapse cannot reach inside one.  Every span
    in the body is held, and the delimiters are the ones that came in, so a span written with
    two backticks comes back with two backticks.
4.  **Collapses the prose runs**:  two or more spaces between two non-space characters become one.
    A Markdown hard break (two spaces at the end of a line) and the indentation of a list are left
    alone, because the lookarounds require a non-space on both sides of the run.
5.  **Puts the code back.**

Two more costs it carries, and both are deliberate:

-  A sentence end *inside* an inline code span, with a space after the period, is still gapped,
    because the canonical pattern is what the two greps are written against.  The span
    `` `foo. Bar` `` comes out as `` `foo.` `` plus the gap plus ` Bar`.  The common shape, where
    the span closes before the space, puts the gap outside the span and leaves the span itself
    untouched.
-  A fenced block that is never closed holds the rest of the body, and the rest of the body is not
    converted.  Close the fence.
