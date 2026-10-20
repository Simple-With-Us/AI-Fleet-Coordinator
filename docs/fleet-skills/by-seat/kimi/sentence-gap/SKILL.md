---
name: sentence-gap
description: >-
  Always put a visibly wider gap between sentences in every human-readable reply and file. Applies on every turn — Cursor desktop, Cursor cloud, CLI, Grok, chat, commits, PRs, and docs. Follows the canonical portable protocol. Mechanism by surface: type the literal &nbsp; entity plus a space in Markdown chat panes (Claude Code Code tab, owner-verified 2026-10-08); in the MiniMax Code chat pane use a real U+00A0 plus a space (owner-verified 2026-10-20 — the entity renders there as the six literal characters); a real U+00A0 plus a space in GitHub PR and issue text and Zulip; two ASCII spaces in files, commits and terminals. The owner must never see the six characters &nbsp; as text. Use whenever writing any prose a human will read.
---
---

# Sentence gap (portable — always on)

> **Retired seat.** Owner directive 2026-08-21: do not assign or accept new Kimi work.  Do not start a Kimi session.  Do not take work.  This catalog copy is inactive — do not install to `~/.kimi`.


Source of truth: `/Users/jay/Code/AI-Fleet-Coordinator/docs/SENTENCE-GAP-PORTABLE-SKILL.md`

The block below is Monet's protocol, pasted verbatim. Follow it exactly. Do not weaken it. Cloud agents without the Mac filesystem still have the full protocol in this file.

### Rule: two visible spaces between sentences

Put a **visibly wider gap** between sentences — after `.` `!` `?` when a new sentence
follows — in every piece of prose a human reads.  Not just product copy: chat replies,
commit messages, PR titles and bodies, code comments, docs, tickets, Zulip posts, release
notes, design docs.

Do **not** add a gap after a non-terminal abbreviation (`e.g.`, `i.e.`, `Dr.`, `v1.2.3`),
inside a URL, email, filename, or a brand name containing a period.

### The gap covers your CHAT PROSE, not only your artifacts

Owner ruling 2026-10-20, after a session where every file, commit, PR, board row and
Zulip post carried the gap correctly while the chat messages in the same turn did not.
Jay's words on that session:  "NEED TWO SPACES BETWEEN SENTENCES", "OR I AM GOING TO
FLIP OUT", "EVEN FUCKING THERE", "I think you need to do nbsp for chats or something".

Every message you write to Jay needs the gap, including a one-line progress note and an
acknowledgement.  Getting it right in artifacts while writing plain single spaces in the
chat is still a failure.  **Read your reply back before you send it** and check every
`.`, `!` and `?` boundary.

In the MiniMax Code pane the gap is a REAL U+00A0 plus a normal space.  Generate it in
the shell rather than typing it, because a model that emits U+00A0 directly can have it
normalized to a plain space:

```sh
printf 'Sentence one.\302\240 Sentence two.\n' > reply.txt
```

`\302\240` is the UTF-8 encoding of U+00A0.  Send the generated text to a file, then
verify that same file, so the check reads the bytes you just produced and never a stale
copy of the same name:

```sh
grep -o $'[.!?]\xc2\xa0' reply.txt | wc -l    # must be above 0
grep -cE '[^0-9][.!?] +[A-Z"]' reply.txt      # must print 0: no gap left unconverted
```

### The catch that wastes everyone's time

**Typing two literal spaces usually does nothing visible.**  HTML and most Markdown
renderers collapse runs of whitespace to a single space.  So the assistant "complies,"
the raw text really does contain two spaces, and the human still sees one.  Both sides
then argue about whether the instruction was followed.

**The gap must survive the renderer between you and the reader.**  Which mechanism works
depends on the surface, so pick by destination:

| Destination | Use | Why |
|---|---|---|
| **Cloud / BotFleet / OpenMausBot chat** | two literal ASCII spaces | Owner 2026-09-03: never display the six characters `&nbsp;` in cloud text.  The backend maps doubles (or the entity) to a real U+00A0 before paint. |
| **Claude Code — desktop app (Code tab)** | the literal entity text `&nbsp;` then a normal space after each sentence, outside code spans → `End.&nbsp; Next.` | Owner-verified 2026-10-08 with a screenshot.  The renderer decodes the entity into a real gap.  A raw U+00A0 from the model arrives as a plain space, and two ASCII spaces collapse.  Supersedes the 2026-09-04 ASCII ruling and the 2026-08-19 finding below for this surface — see History. |
| **MiniMax Code chat pane** (owner-verified 2026-10-20) | a REAL U+00A0, the nbsp character, then a normal space, outside code spans | Jay:  "I think you need to do nbsp for chats or something" and "I have confirmed this before with agents and knew you needed nbsp already".  Two ASCII spaces collapse in this pane, and the `&nbsp;` entity renders as the six literal characters, which is worse than no gap.  The 2026-10-18 entry for this pane was wrong and is corrected here. |
| **Any other agent chat pane that renders Markdown** (Codex, Cursor, Antigravity, Grok, Kimi, DeepSeek, Fx, Muse, and similar) | the same: `&nbsp;` then a normal space, outside code spans | Owner ruling 2026-10-08: Markdown chat collapses ASCII doubles everywhere, so use the entity by default and do not ask first.  Only the Code tab and the MiniMax Code pane are verified, and they differ: MiniMax needs a real U+00A0, the Code tab needs the entity.  If the owner ever sees the six literal characters, stop using the entity on that surface and report it in #agent-sync — that surface then needs a different mechanism, and it is unknown until tested. |
| **Terminal TUI chat** (Claude Code CLI, Grok TUI, Codex CLI, opencode, kimi-code, mcode) | two literal ASCII spaces | Unverified.  A terminal prints the entity as the literal text `&nbsp;`, which the owner must never see.  Do not use the entity here. |
| **Plain-text chat (no Markdown rendering)** | two literal ASCII spaces | Nothing collapses them; an entity would show as the ugly text `&nbsp;` |
| **Files read as source** — repo docs, commit messages, code comments, config, diffs, terminal output | two literal ASCII spaces | Read in an editor/terminal/`git diff`, which preserve them verbatim; an entity would appear literally |
| **HTML / JSX / SwiftUI / any rendered product copy** | a real U+00A0 plus a space, or a shared `SENTENCE_GAP` constant | Raw double spaces collapse in HTML.  Source may use the entity only when the renderer expands it.  The owner must never see `&nbsp;` as text. |
| **GitHub PR and issue titles, bodies and comments, review comments, Zulip posts, and any text a tool writes that a Markdown or HTML renderer then displays** | a real U+00A0 plus a space | Owner ruling 2026-10-08.  Tools preserve the character, and the renderer shows a real gap.  Never use the `&nbsp;` entity here: GitHub can copy a PR body into a plain-text squash commit, where the entity would show literally.  Supersedes the 2026-10-07 finding that two ASCII spaces were enough in Zulip. |
| **Markdown source** | two literal spaces *between* sentences | ⚠️ Two spaces at the **end of a line** is the unrelated hard-line-break syntax — don't confuse the two |

**Producing U+00A0 through a tool.**  You cannot type a raw U+00A0 into your own chat reply.
Worse, you cannot count on emitting a *double* space either:  a run of whitespace you write into
a tool call or a shell heredoc **sometimes** gets normalized to a single space before it lands.
This is **intermittent, not deterministic** — in one session on 2026-10-18 a ~3 KB PR body kept
all eight of its double spaces while a ~3 KB Zulip body lost every one, same machine, same tool.
So you cannot tell from one write whether it will happen again, and you cannot diagnose it after
the fact.  **Generate the gap in the shell, never in the text you emit.**  Use a regex that
matches one *or more* spaces, so it is correct in both states:

```sh
perl -CSDA -pe 's/(?<![0-9])([.!?]) +(?=[A-Z0-9"*`#@])/$1\x{a0} /g' body.txt > body.gap.txt
```

**A second pass is required, and the first pass alone is what ships the wrong gap.**  The
conversion above only touches a run of spaces that follows `.` `!` `?`.  Every OTHER run of two
or more spaces survives it, so a colon or a semicolon in prose keeps two spaces:
`They are now published:  1BfhJbWX` and `already decided;  PR #714` both come out of the first
pass unchanged.  The owner ruling is that a colon, a semicolon, a comma or a dash gets **one
ordinary space**;  a gap after a semicolon is "very weird" and is not what was asked for.  So
collapse every remaining run, on every line except the house closeout header and any fenced code
block:

```sh
perl -CSDA -ne '
  if (/^\s*```/) { $fence = !$fence; print; next }
  if (!$fence && !/^\s*repo:/) { s/(?<=\S) {2,}/ /g }
  print
' body.gap.txt > body.nbsp.txt
```

The header line is exempt on purpose.  `repo:  <Project>  |  DONE` is a template, not prose:
the handbook's own examples write it with two spaces (`handbook/appendix/examples.md`), and the
fleet tooling parses it as `re.match(r"^\s*repo:\s*\S", line)`, so one space would also work
but two is the house form.  Leave that line exactly as the template has it.

The fenced code block is exempt for a different reason:  **code is not prose.**  A line of shell
in a PR body often carries aligned spacing, and collapsing it silently rewrites the code.  A
one-line version of this pass, `s/(?<=\S) {2,}/ /g`, turns `wc -l     # must be > 0` into
`wc -l # must be > 0` and turns `repo:  <Project>  |  DONE` inside a quoted example into
`repo: <Project> | DONE`.  Both changes are invisible to every assertion, because the assertion
runs after the damage.  The fence-aware version above leaves the code exactly as the author wrote
it, and it leaves the house header alone on the same pass.

Run the two passes in order, first into a scratch file and then into the file you post.  The
second pass never touches the U+00A0 the first pass produced, because a U+00A0 plus one space
is a run of one space.

Matching one *or more* is the whole fix.  The old pattern requiring exactly two spaces — written
here as `\s{2}` so this sentence survives its own advice — matched nothing on collapsed text; the
single-space pattern `([.!?]) (?=...)` is no better, because it matches nothing on text that kept
its two spaces.  Both exit `0` and post with no gap.  The `(?<![0-9])` lookbehind protects a
numbered list marker, and `[ \t]+` is the same idea as ` +` and additionally tolerates a tab.  The
lookahead class skips a lowercase follower, so `e.g. this` and lower-case domains keep their plain
space.

**Known limitations — the pattern is a heuristic, not a dictionary.**  Three deliberate costs:

- It gaps a *capitalised* abbreviation:  `Dr. Smith` and `e.g. Acme` both come out with a U+00A0,
  against the rule above about non-terminal abbreviations.
- `(?<![0-9])` is what keeps a numbered list marker intact.  CommonMark only recognises `1.  Item`
  when the character after the period is a real space, so a U+00A0 there silently stops the list
  parsing as a list, and a body with steps in it renders as one run-on paragraph.  The lookbehind
  skips any period preceded by a digit, which fixes the marker and also protects `v1.2.3`, but it
  costs the gap on a sentence that ends in a digit:  `The answer is 42. Next thing.` gets none.
- The first pass does not know about fenced code blocks, so a line of code that reads like a
  sentence picks up a U+00A0 inside the code:  `echo "Done. Next"` gains a U+00A0 where the plain
  space was, and the code no longer says exactly what it said.  The second pass skips fences, so it
  cannot make this worse, but it cannot undo it either.  When a body quotes code with a
  sentence-like line in it, keep that one line out of the conversion.

If a body carries one of these, fix that single gap by hand after the run.  Do not silently ship
it, and do not widen the pattern to chase it.

**Assert before you post — all three checks, every time.**  A conversion that matches nothing still
exits 0, so a failed run is silent.  And a count above zero is *not* on its own sufficient: a
partly-converted body can have real U+00A0 and still carry a leftover single-space gap.

```sh
grep -o $'\xc2\xa0' body.nbsp.txt | wc -l     # must be > 0, and plausibly large for the body size
grep -cE '[^0-9][.!?] [A-Z"]' body.nbsp.txt         # must be 0 — a leftover single-space gap
perl -CSDA -ne 'if (/^\s*```/) { $fence = !$fence; next } print if !$fence && !/^\s*repo:/ && /(?<=\S) {2,}/' body.nbsp.txt | wc -l   # must be 0 — a double space left in prose
```

The second check is what the one-or-more pattern prevents, and it catches the case the first
one misses:  a body with three gaps where one survived unconverted has a non-zero U+00A0 count
and still ships a wrong gap.  `[^0-9]` in front is deliberate, so a legitimate numbered-list
marker (`1. Item`) is not mistaken for an unconverted gap — the same reason the conversion
carries `(?<![0-9])`.  Expect rare benign hits from abbreviations (`Dr. Smith`, `e.g. A`);
read them and confirm they are abbreviations rather than real sentences.

**The third check is the one that catches a whole class of wrong body.**  The first two checks
only look at sentence-ending marks, so a body can pass both of them and still carry a double
space after a colon or a semicolon — `published:  1BfhJbWX`, `already decided;  PR #714`.  That
is a gap where the owner asked for one ordinary space, and it is invisible to the first two
checks, which is exactly why it ships.  The `^repo:` filter drops the house closeout header,
whose two spaces are the template's own, and the fence filter drops fenced code blocks, whose
spacing is the author's.

The third check has to be fence-aware for the same reason the second pass is:  a plain
`grep -cE '[^[:space:]] {2,}'` flags the aligned spacing inside a code block and reads as a
failure that is not there, which trains people to ignore it.  The filter has to match the pass
exactly, or the assertion measures something the conversion never touched.

A `0` from the first check means the run changed nothing.  Fix it before posting, never after.
Then pass the file to `gh pr create --body-file`, `gh pr comment --body-file`, or
`agent-sync post --body-file` — which refuses any file whose mode is not `0600` or `0400`, so
run `chmod 600` on it first.  There is no override flag.

**Source files are a different surface and are NOT affected.**  Swift, TypeScript, shell, and
markdown read as source want **two literal ASCII spaces**, and in the same session those
two-space gaps came through the write tool completely intact.  Do **not** "fix" a source file by
inserting U+00A0 on the strength of this chat-surface problem:  plain source wants plain ASCII,
and a U+00A0 committed into a source file is a real defect.  The perl conversion above is for
text bound for GitHub, Zulip, or any renderer.  Never run it over source.

Two more traps, both hit 2026-10-18:

- **Never type a raw U+00A0 into a message body or tool call.**  It is normalized the same way
  a double space is.  Let perl emit it.
- The first `agent-sync` call in a session can die with "Parent-death guardian target
  registration timed out".  Retry verbatim.  If the retry fails with "cannot open it (No such
  file or directory)", the first attempt died before writing the file — the perl step never
  ran.  That is not evidence the post went out.

### Verify, don't assume — run this self-test only on a NEW, unlisted surface

The table above already answers every surface listed in it — follow the row, don't re-test
it.  For a surface the table does **not** cover, test it and ask the human what they
actually see before relying on either mechanism.

> Output these two lines verbatim, then ask which shows a wider gap:
>
> A. `Sentence one.&nbsp; Sentence two.`
> B. `Sentence one.  Sentence two.`
>
> If A looks wider → use the `&nbsp;` entity on this surface, and add the surface to the table.
>   Owner ruling 2026-10-08: when a surface collapses two typed spaces, use its working
>   mechanism without asking.
> If B looks wider, or they look identical → use two literal spaces.
> If neither shows a gap → say so plainly and ask how they want it handled.
> Then keep using whichever won, for that surface, for the rest of the session — and add
> the surface to the table above so nobody re-tests it.

### History — 2026-08-19/20 findings on Claude Code (terminal + desktop), do NOT re-run these tests

**2026-09-04 update — desktop app superseded.**  The entity finding below for the desktop
app (Code tab) was superseded by an owner-verified ruling on 2026-09-04: use **two literal
ASCII spaces** there.  The 2026-08-19 entity advice for the desktop app was withdrawn.

**2026-10-08 update — desktop app superseded again.**  On the Code tab, type the `&nbsp;`
entity plus a space after each sentence (outside code spans); the renderer decodes it.  A raw
U+00A0 from the model arrives as a plain space (0 NBSPs in the verified reply), and two
ASCII spaces collapse.  Owner-verified 2026-10-08 with a screenshot.  The 2026-09-04 ASCII
ruling is withdrawn.

**Terminal CLI — no newer ruling.**  Last checked 2026-08-19/20; not re-verified since.
Default to two literal ASCII spaces there, and do not use the `&nbsp;` entity on a terminal
TUI, which would print it literally.

What was found 2026-08-19/20:

- ❌ **Two literal ASCII spaces in chat** — collapsed by the Markdown renderer.  Invisible.
- ❌ **A raw U+00A0 character typed directly into chat** — normalized away in the
  transcript view.  **Especially deceptive: copy-pasting the reply out can still show two
  spaces, so it looks fixed when the human still sees one.**  Do not trust copy-paste as
  proof.
- ❌ **App/output settings** — no toggle governs inter-sentence spacing.  Output-style
  settings change tone only; headless output-format flags don't apply to interactive chat;
  screen-reader modes only drop borders.
- ❌ **Patching the client** — compiled and signed; breaks code signing and is wiped by
  auto-update.  Never attempt.
- ✅ The literal entity text `&nbsp;` + a space rendered as a visibly wider gap, and does
  again on the desktop app (re-verified 2026-10-08, see above).  Not re-verified on the
  terminal CLI since, so use two ASCII spaces there.
- ✅ **Two literal ASCII spaces in files** — correct and simplest; leave file content alone.

### The transferable lesson

When an instruction *appears* not to take effect, **stop repeating the promise and
diagnose the rendering layer between you and the reader** — then ask them what is on their
screen.  Four rounds of "fixed it!" were spent here before anyone checked whether the
change could be seen at all.  Intent is not output; output is not what is displayed.
