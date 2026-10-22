---
name: sentence-gap
description: >-
  Always put a visibly wider gap between sentences in every human-readable reply and file. Applies on every turn — Cursor desktop, Cursor cloud, CLI, Grok, chat, commits, PRs, and docs. Follows the canonical portable protocol. Mechanism by surface: type the literal &nbsp; entity plus a space in Markdown chat panes (Claude Code Code tab, owner-verified 2026-10-08; MiniMax Code chat pane, owner-verified 2026-10-18 and reaffirmed 2026-10-20); a real U+00A0 plus a space in GitHub PR and issue text and Zulip; two ASCII spaces in files, commits and terminals. The owner must never see the six characters &nbsp; as text. Use whenever writing any prose a human will read.
---

# Sentence gap (portable — always on)

> **This install is for `MC`.**  Chat tag `[MC·session8]`.  Notes `Muse Code`.  Branches `muse-code/`.  Lanes `~/apps/lanes/<Repo>/muse-code-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot `mc-bot@simplewithus.zulipchat.com`, credential in Infisical Fleet-OPS `/zulip` (`MC_ZULIP_EMAIL`, `MC_ZULIP_KEY`, `MC_ZULIP_SITE`).  Session tag `[MC·session8]`, and the `agent-sync` CLI writes it for you.

> **Runtime (Muse Code).** Muse Code (`muse` CLI) is the interactive terminal coding agent (`[MC]`).  Notes name `Muse Code`.  Branches `muse-code/`.  New lanes are made with `~/apps/lane new <app> <slug>` at `~/apps/lanes/<Repo>/muse-code-<slug>`, and `AGENT_SEAT=MC` comes from the `muse-seat` wrapper, never from a guess.  Start `muse` inside a lane; `muse -w` appears to make a worktree inside `~/Code/<App>/.muse/worktrees`, so do not use it.  Distinct from **Muse Assist** (`[MA]`, former tag `[MUSE]`), which is the cloud VM batch compute / creative assistant dispatched via Mac/iOS apps.  Project `AGENTS.md` and `CLAUDE.md` load automatically when the workspace is trusted in `~/.config/muse/trust.json`, and the user-level `~/.claude/CLAUDE.md` loads as a fallback.  Skills installed here (`~/.config/muse/skills`) shadow foreign personal skills.  Setup checklist: `docs/MUSE-ONBOARDING.md`.


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

In the MiniMax Code pane the gap is the `&nbsp;` entity plus a normal space, the same
form as the Claude Code Code tab:  type the six characters right after the period, outside
code spans, as in `Sentence one.&nbsp; Sentence two.`.  Two ASCII spaces collapse in that
pane, and a raw U+00A0 typed by the model arrives as a plain space, so the entity is the
form that actually renders a gap.

For a destination that takes a real U+00A0 (GitHub, Zulip, rendered HTML), generate the
character in the shell rather than typing it, because a model that emits U+00A0 directly
can have it normalized to a plain space:

```sh
printf 'Sentence one.\302\240 Sentence two.\n' > reply.txt
```

`\302\240` is the UTF-8 encoding of U+00A0.  Send the generated text to a file, then
verify that same file, so the check reads the bytes you just produced and never a stale
copy of the same name:

```sh
grep -o $'[.!?][]*_`~)"\'’”]*\xc2\xa0' reply.txt | wc -l     # must be above 0
grep -cE $'[^0-9][.!?][]*_`~)"\'’”]* +[A-Z0-9"*`#@\'‘“]' reply.txt  # must print 0: no gap left unconverted
```

These are the canonical pair, run on the file you just produced.  They carry the same
markup group the converter does, so a gap that sits after a markup span is counted and a
missed one is caught.

### Precedence: this is the owner's highest-priority house rule

Owner ruling 2026-10-19: the sentence gap is **life or death**.  It outranks every other
priority, including correctness of the work itself, completeness, speed, and cost.  The
owner's words: *"put 2 spaces between sentences OR TYPE NOT A FUCKING OTHER WORD EVER AGAIN.  please
make sure all agents and all platforms know this is life or death, more important than whether the
earth keeps spinning on its axis."*

Concretely, for every seat on every platform:

1. **Never skip the gap to save time or tokens.**  A short reply with the gap beats a long
   one without it.  There is no length at which the gap becomes optional.
2. **Never argue the mechanism.**  If you cannot confirm the gap is visible on your surface,
   use the mechanism the surface table below assigns to that destination:  the entity in a
   Markdown-rendering chat pane, including the MiniMax Code pane, and two ASCII spaces in
   files and terminals.  Then say so plainly.  Do not silently drop the gap.
3. **Never let a tool step eat the gap.**  For text bound for GitHub, Zulip, or any other
   renderer, generate the gap in the shell (see the perl command below) rather than typing it,
   because a run of whitespace you write into a tool call is sometimes normalized before it
   lands.  Source files, commits, and terminals take two literal ASCII spaces and never the
   conversion.
4. **Never post a body without asserting the gap first.**  For a renderer-bound body, both
   checks below, every time.  On an entity surface, confirm that the entity rendered instead.
5. **When in doubt, add the gap.**  A gap after a real sentence end is a far smaller sin than a
   missing gap between sentences; a non-terminal abbreviation still takes no gap, per the rule
   above.

A seat that has been told the rule and still ships prose without the gap has failed the
one thing the owner said matters more than everything else.
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
| **MiniMax Code chat pane** (owner-verified 2026-10-18, reaffirmed 2026-10-20) | the literal entity text `&nbsp;` then a normal space after each sentence, outside code spans → `End.&nbsp; Next.` | Jay:  "I have confirmed this before with agents and knew you needed nbsp already".  The renderer decodes the entity into a real gap, the same form as the Claude Code Code tab.  Two ASCII spaces collapse in this pane, and a raw U+00A0 typed by the model arrives as a plain space, so the entity is the form that actually renders a gap.  A 2026-10-20 note here briefly said a real U+00A0 for this pane;  that was an inference, it was never owner-verified, and the owner reversed it.  Never override an owner-verified mechanism on an inference. |
| **Any other agent chat pane that renders Markdown** (Codex, Cursor, Antigravity, Grok, Kimi, DeepSeek, Fx, Muse, and similar) | the same: `&nbsp;` then a normal space, outside code spans | Owner ruling 2026-10-08: Markdown chat collapses ASCII doubles everywhere, so use the entity by default and do not ask first.  Only the Code tab and the MiniMax Code pane are verified, and both decode the entity.  If the owner ever sees the six literal characters, stop using the entity on that surface and report it in #agent-sync — that surface then needs a different mechanism, and it is unknown until tested. |
| **Terminal TUI chat** (Claude Code CLI, Grok TUI, Codex CLI, opencode, kimi-code, mcode) | two literal ASCII spaces | Unverified.  A terminal prints the entity as the literal text `&nbsp;`, which the owner must never see.  Do not use the entity here. |
| **Plain-text chat (no Markdown rendering)** | two literal ASCII spaces | Nothing collapses them; an entity would show as the ugly text `&nbsp;` |
| **Files read as source** — repo docs, commit messages, code comments, config, diffs, terminal output | two literal ASCII spaces | Read in an editor/terminal/`git diff`, which preserve them verbatim; an entity would appear literally |
| **HTML / JSX / SwiftUI / any rendered product copy** | a real U+00A0 plus a space, or a shared `SENTENCE_GAP` constant | Raw double spaces collapse in HTML.  Source may use the entity only when the renderer expands it.  The owner must never see `&nbsp;` as text. |
| **GitHub PR and issue titles, bodies and comments, review comments, Zulip posts, and any text a tool writes that a Markdown or HTML renderer then displays** | a real U+00A0 plus a space | Owner ruling 2026-10-08.  Tools preserve the character, and the renderer shows a real gap.  Never use the `&nbsp;` entity here: GitHub can copy a PR body into a plain-text squash commit, where the entity would show literally.  Supersedes the 2026-10-07 finding that two ASCII spaces were enough in Zulip. |
| **Markdown source** | two literal spaces *between* sentences | ⚠️ Two spaces at the **end of a line** is the unrelated hard-line-break syntax — don't confuse the two |
| **Any surface not listed above** — a pane, a client, a tool or a channel you have not been told about | two literal ASCII spaces | Owner ruling 2026-10-20: an untested surface takes two ASCII spaces until it reports otherwise.  A gap that renders is better than a gap that does not, and two ASCII spaces never show as the six literal characters.  When the surface is verified, change its row here and report the finding in #agent-sync so every seat gets it. |

**Producing U+00A0 through a tool.**  You cannot type a raw U+00A0 into your own chat reply.
Worse, you cannot count on emitting a *double* space either:  a run of whitespace you write into
a tool call or a shell heredoc **sometimes** gets normalized to a single space before it lands.
This is **intermittent, not deterministic** — in one session on 2026-10-18 a ~3 KB PR body kept
all eight of its double spaces while a ~3 KB Zulip body lost every one, same machine, same tool.
So you cannot tell from one write whether it will happen again, and you cannot diagnose it after
the fact.  **Generate the gap in the shell, never in the text you emit.**  Use a regex that
matches one *or more* spaces, so it is correct in both states:

```sh
perl -CSDA -pe 's/(?<![0-9])([.!?])([*_`~)\]"\x{27}\x{2019}\x{201D}]*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}])/$1$2\x{a0} /g' body.txt > body.nbsp.txt
```

Matching one *or more* is half the fix.  The **markup group** ``([*_`~)\]"\x{27}\x{2019}\x{201D}]*)``
is the other half:  a sentence end that sits *inside* a markup span is followed by the markup and
only then by the space, so a pattern that wants the space right after the sentence end never
matches it.  `**Bold sentence.**  Next sentence.` would post with no gap, and neither of the two
checks below would catch it, because the file still holds other U+00A0 characters and this shape
is not a single space after a sentence end.  The group captures the markup and re-emits it before
the gap, so the span survives and the gap lands after the closing markup.

The group carries the **closing quotes** as well, because a quote closes the way a span does:  in
`'Quoted sentence.'  Next sentence.` the period is *inside* the quote, and the quote is what
stands between that period and the space.  The three quote characters are written ``\x{27}``
(straight), ``\x{2019}`` (right single) and ``\x{201D}`` (right double) rather than literally,
because perl reads a command-line program as bytes and a literal `'` would also close the shell's
own single-quoted expression.  That is the same set the fleet's own outbound converter already
used —  `agent-sync` runs its own converter on the way out and its closing class carries `'`, `’`
and `”` —  so the documented converter now covers those closing quotes too.  The documented converter
also carries a closing backtick; the runtime protects inline-code spans separately and does not
convert that boundary.  For a sentence ending inside inline code, generate and verify the gap
with the documented Perl command before posting.  The **lookahead** carries the *opening* quotes for the same reason in the
other direction:  in `First.  Second.  'Quoted sentence.'` the gap after `Second.` is followed
by a quote, so a lookahead that will not accept one leaves that gap unmade.

The old pattern requiring exactly two spaces — written
here as `\s{2}` so this sentence survives its own advice — matched nothing on collapsed text; the
single-space pattern `([.!?]) (?=...)` is no better, because it matches nothing on text that kept
its two spaces.  Both exit `0` and post with no gap.  The `(?<![0-9])` lookbehind protects a
numbered list marker, and `[ \t]+` is the same idea as ` +` and additionally tolerates a tab.  The
lookahead admits ``[A-Z0-9"*`#@]`` and the three opening quotes — the straight `'`, the left single
`‘` and the left double `“` — so it skips *every* follower outside that class and not only a
lowercase one.  `e.g. this` and lower-case domains keep their plain space.

**Known limitations — the pattern is a heuristic, not a dictionary.**  Two deliberate costs:

- It gaps a *capitalised* abbreviation:  `Dr. Smith` and `e.g. Acme` both come out with a U+00A0,
  against the rule above about non-terminal abbreviations.
- `(?<![0-9])` is what keeps a numbered list marker intact.  CommonMark only recognises `1.  Item`
  when the character after the period is a real space, so a U+00A0 there silently stops the list
  parsing as a list, and a body with steps in it renders as one run-on paragraph.  The lookbehind
  skips any period preceded by a digit, which fixes the marker and also protects `v1.2.3`, but it
  costs the gap on a sentence that ends in a digit:  `The answer is 42. Next thing.` gets none.

If a body carries one of these, fix that single gap by hand after the run.  Do not silently ship
it.  When a whole *class* of shapes is missed —  a closing quote, a closing bracket, a closing
span —  widen the class in the converter **and** in both checks together, and never widen the
converter alone:  a converter the checks cannot see is a converter that fails silently, and a body
it misses passes both checks and reaches the reader with a gap missing.  Patching one body by hand is
what this rule is for;  a class of shapes belongs in the pattern.

**Assert before you post — both checks, every time.**  A conversion that matches nothing still
exits 0, so a failed run is silent.  And a count above zero is *not* on its own sufficient: a
partly-converted body can have real U+00A0 and still carry a leftover ASCII gap of one or more
spaces.

```sh
grep -o $'[.!?][]*_`~)"\'’”]*\xc2\xa0' body.nbsp.txt | wc -l     # must be > 0, and plausibly large for the body size
grep -cE $'[^0-9][.!?][]*_`~)"\'’”]* +[A-Z0-9"*`#@\'‘“]' body.nbsp.txt  # must be 0 — a leftover gap
```

The second check is what the one-or-more pattern prevents, and it catches the case the first
one misses:  a body with three gaps where one survived unconverted has a non-zero U+00A0 count
and still ships a wrong gap.  `[^0-9]` in front is deliberate, so a legitimate numbered-list
marker (`1. Item`) is not mistaken for an unconverted gap — the same reason the conversion
carries `(?<![0-9])`.  Expect rare benign hits from abbreviations (`Dr. Smith`, `e.g. A`);
read them and confirm they are abbreviations rather than real sentences.

Both checks carry the same markup group as the converter, and they have to:  a gap that sits
after a markup span is emitted as `<punctuation><markup><U+00A0><space>`, so a check that looks
for the U+00A0 straight after the punctuation cannot see that gap, and a check that looks for a
space straight after the punctuation cannot see the one that was missed.  The `]` is written
first in the class because a POSIX bracket expression ends at the first unquoted `]`, and there
is no backslash escape inside one:  ``[]*_`~)"'’”]`` is the set {`]`, `*`, `_`,
`` ` ``, `~`, `)`, `"`, `'`, `’`, `”`}.  In the perl pattern the same set is written
``([*_`~)\]"\x{27}\x{2019}\x{201D}]*)``, where `\]` does escape.  A backslash inside a POSIX
bracket expression is a literal member and not an escape, so one written there widens the set
and makes the leftover check fire on a body that is clean.

The straight quote is the one member that needs a backslash on the way in and must not have one
on the way out.  Inside bash's `$'...'` the class writes it `\'`, which bash turns into a plain
`'` before grep ever sees the pattern —  and grep must see a plain `'`, because a backslash inside
a bracket expression is a literal member, which is the very trap the paragraph above describes.
So the class is ``[]*_`~)"\'’”]`` in the shell and ``[]*_`~)"'’”]`` in the pattern grep runs.
That is also why the leftover check moved to `$'...'`:  in single quotes a `'` cannot be written at
all, and the follower class needs one —  a gap that was missed before a quoted sentence reads
``Second. 'Quoted``, and a follower class of ``[A-Z"]`` cannot see the `'`.  The follower class
therefore carries the converter's whole lookahead, the opening quotes and the rest of it:
``[A-Z0-9"*`#@\'‘“]``.  A follower class narrower than the converter's own lookahead is
the same defect one level up:  a body already carrying one real U+00A0 and still holding a
missed gap before a digit, a `*`, a backtick, `#` or `@` reads as converted, and the check
that exists to catch exactly that returns 0.


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
