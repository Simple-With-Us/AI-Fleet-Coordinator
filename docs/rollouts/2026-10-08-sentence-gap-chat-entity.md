# 2026-10-08 — Sentence gap: the chat entity returns, GitHub and Zulip use U+00A0

Type: Fleet change  (board `86aa2176`)
> **Correction, 2026-10-20, and its own reversal the same day.**  A note here briefly said the MiniMax Code pane does NOT decode the `&nbsp;` entity and that the gap there is a real U+00A0 plus a normal space.  That was an inference, it was never owner-verified, and the owner reversed it:  the MiniMax Code chat pane decodes the entity, the same form as the Claude Code Code tab (owner-verified 2026-10-18, reaffirmed 2026-10-20).  See the `sentence-gap` skill for the per-surface table.

> **Superseded 2026-10-23 — the per-surface table below is the Oct 8 record, not the rule.**  The owner's ruling of 2026-10-23 replaced the split with one mechanism: a real U+00A0 plus a normal space between sentences on every surface, GitHub and Zulip included, and two plain ASCII spaces are never the rule.  The one exception is a chat reply in the Claude Code Code tab or the MiniMax Code chat pane, which take the `&nbsp;` entity plus a normal space, because those two panes normalize a raw U+00A0 away and collapse two plain ASCII spaces into one.  The owner confirmed it in Zulip `#Policy`, message 633873394, "NBSP (U+00A0) + ASCII space between sentences (w/exceptions)", posted Thu, Oct 22 and edited Sat, Oct 24 at 1:26 PM.  So the rows below that give the entity to every Markdown agent pane, and that give two ASCII spaces to terminals, cloud chat, commits and docs, are all superseded.  The live table is `docs/fleet-skills/sentence-gap/SKILL.md`: take the mechanism from there.


## Why

Owner, Thu, Oct 8, 2026:  "please fix it for every chat … and make the whole world do the
same if you can."  The owner wants a visibly wider gap, two spaces, between sentences on
every surface, and the gap has to survive the renderer.

Verified the same day:

- Chat UIs that render Markdown collapse two typed ASCII spaces into one.  That includes
  the Claude Code desktop app's Code tab and most agent chat panes.
- A model cannot reliably emit a raw U+00A0 in a chat reply.  It arrives as a plain space.
  The verified reply held 0 NBSPs.
- Typing the HTML entity `&nbsp;` followed by a normal space after each sentence, outside
  code spans, works in the Code tab.  The renderer decodes it.  The owner confirmed it with
  a screenshot.

This reverses the 2026-09-04 ruling that put two ASCII spaces in the Code tab.

## The rule, per surface

| Surface | Mechanism |
| --- | --- |
| Claude Code desktop app, Code tab (chat replies) | `&nbsp;` plus a normal space, outside code spans.  Owner-verified 2026-10-08. |
| Any other Markdown-rendering agent chat pane (Codex, Cursor, Antigravity, Grok, Kimi, MiniMax, DeepSeek, Fx, Muse) | The same.  By owner ruling, not individually verified. |
| Terminal TUI chat (Claude Code CLI, Grok TUI, Codex CLI, opencode, kimi-code, mcode) | Two ASCII spaces.  Unverified. |
| Cloud, BotFleet and OpenMausBot chat | Two ASCII spaces.  The backend maps them to U+00A0. |
| GitHub PR and issue titles, bodies and comments, review comments; Zulip posts | A real U+00A0 plus a space.  Never the entity: GitHub can copy a PR body into a plain-text squash commit, where it would show literally. |
| Commit messages, source files, repo docs read as source, terminal output, Slack | Two ASCII spaces. |
| HTML, JSX, SwiftUI product copy; Apple Notes `--html` | A real U+00A0 plus a space, or `SENTENCE_GAP`. |

The owner must never see the six characters `&nbsp;`.  If a surface shows them, stop using
the entity there, report the surface in #agent-sync, and treat it as unknown until it is
tested.  When a surface needs a mechanism to show two spaces, use it without asking.

Producing U+00A0 through a tool:  write two ASCII spaces after each sentence, then run
`perl -CSDA -pe 's/([.!?])  (?=\S)/$1\x{a0} /g' body.txt > body.nbsp.txt`, and check the
output holds a U+00A0 before `gh pr create --body-file` or `simple-chat post`.

> ⚠️ **Superseded 2026-10-18 — do not copy the perl line above.**  A run of whitespace written
> into a tool call or heredoc *sometimes* collapses to a single space before it lands, and that
> collapse is **intermittent, not deterministic**:  in one session a ~3 KB PR body kept all eight
> of its double spaces while a ~3 KB Zulip body lost every one, same machine, same tool.  So the
> pattern must match one *or more* spaces to be correct either way:
> `perl -CSDA -pe 's/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}])/$1$2\x{a0} /g' body.txt > body.nbsp.txt`.
> Note the single-space form is *not* a fix — it matches nothing on text that kept its two
> spaces.  Before posting run both checks: `grep -o $'\xc2\xa0' body.nbsp.txt | wc -l` must be
> `> 0` and `grep -cE '[^0-9][.!?] [A-Z"]' body.nbsp.txt` must be `0`, since a partly converted body can
> hold real U+00A0 and still carry one leftover single-space gap.  Source files (Swift, TypeScript,
> shell, markdown read as source) are unaffected and keep two literal ASCII spaces; never put a
> U+00A0 into a committed source file.  This file is a dated record of the 2026-10-08 ruling and
> is kept as-is above;  the live procedure is in `docs/fleet-skills/sentence-gap/SKILL.md`.

> 🔁 **Updated 2026-10-19 (AFC-415) — the 2026-10-18 pattern above still misses a shape.**  That
> pattern wants the space right after the sentence end, so it never matches a sentence end that
> sits *inside* a markup span:  `**Bold sentence.**  Next sentence.` has its space after the
> closing `**`, not after the period, and the gap is missed.  Neither check catches it, because
> the file still holds other U+00A0 characters and this shape is not a single space after a
> sentence end.  The live pattern adds the markup group ``([*_`)\]"]*)``, which captures the markup
> after the sentence end and re-emits it before the gap, so the span survives and the gap lands
> after the closing markup:
> ``perl -CSDA -pe 's/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}])/$1$2\x{a0} /g' body.txt > body.nbsp.txt``.
> The two checks carry the same markup group, because the gap they have to see is emitted as
> <punctuation><markup><U+00A0><space>:  a check that looks for the U+00A0 straight after the
> punctuation cannot see that gap, and a check that looks for a space straight after the
> punctuation cannot see the one that was missed.  Before posting run both checks:
> ``grep -o $'[.!?][]*_`)"]*\xc2\xa0' body.nbsp.txt | wc -l`` must be `> 0`, and
> ``grep -cE '[^0-9][.!?][]*_`)"]* +[A-Z"]' body.nbsp.txt`` must be `0`.
> The 2026-10-08 and 2026-10-18 commands are left as they were written;  the one home is
> `docs/fleet-skills/sentence-gap/SKILL.md`.  `AGENT-SYNC.md` is a generated mirror:  it is
> built from the Fleet-OPS handbook and rewritten by the handbook sync, so it carries the
> command as of its last sync and not necessarily this one.  Take the command from the one
> home, not from the mirror.
>
> 🔁 **Updated 2026-10-20 (AFC-415) — the pattern now carries the closing quotes.**  The group above
> captured the closing *markup* but not a closing *quote*, and the lookahead accepted an opening
> capital or a digit but not an opening quote.  So a sentence end inside a quoted span, as in
> `'Quoted sentence.'  Next sentence.`, was still missed:  the period is inside the quote, the quote
> stands between it and the space, and neither the converter nor the two checks could see the shape.
> A body with it passed both checks and reached GitHub with no gap.  The live pattern carries the
> three closing quotes in the group and the three opening ones in the lookahead, which is the set
> the fleet's own outbound converter now uses too:
> ``perl -CSDA -pe 's/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}\[])/$1$2\x{a0} /g' body.txt > body.nbsp.txt``.
> The two checks carry the same class, and the leftover check moved to bash's `$'...'` so the class
> can hold a straight quote at all —  in single quotes it cannot.  The follower class carries the
> opening quotes too, because a gap missed before a quoted sentence reads `Second. 'Quoted`:
> ``grep -o $'[.!?][]*_`~)"\'’”]*\xc2\xa0' body.nbsp.txt | wc -l`` must be `> 0`, and
> ``grep -cE $'[^0-9][.!?][]*_`~)"\'’”]* +[A-Z0-9"*`#@\'‘“]' body.nbsp.txt`` must be `0`.
> The 2026-10-08, 2026-10-18 and 2026-10-19 commands are left as they were written;  the one home is
> `docs/fleet-skills/sentence-gap/SKILL.md`.  Take the command from the one home.

## What landed (docs, in this PR)

- `AGENT-SYNC.md` § Sentence Gap (the authoritative per-surface table), the Zulip summary
  line, the Cursor and load-path notes, the skills catalog row, and the history entry.
- `FLEET-UI-COPY.md`, `TEMPLATE-AGENTS.md`, `docs/protocols/zulip-fleet-guide.md`
  (Writing Rules), and the retired-row text in `docs/protocols/agent-sync-rewrite-map.md`.
- `docs/SENTENCE-GAP-PORTABLE-SKILL.md` and the canonical pack skills `sentence-gap`,
  `owner-copy`, `fleet-coordination`, `land-lane`, `codex-triage`, re-rendered into
  `skills/`, `.claude/skills`, `.cursor/skills`, `.grok/skills`, and every
  `docs/fleet-skills/by-seat/*` pack with `scripts/install-fleet-skills.py --repo-only`.
- `.cursor/rules/sentence-gap.mdc` regenerated from the pack block.
- `scripts/fleet_rag/golden_multihop.jsonl` item A2 now expects `owner-verified 2026-10-08`.

## Not covered, owner or follow-up

- The Cursor Settings › Rules user rule "Sentence gap — two visible spaces" is an owner UI
  action.  It is not a file.
- Chat surfaces other than the Code tab are unverified.  A tester should run the
  self-test in the `sentence-gap` skill on each and record the answer in its table.
- `scripts/agent_sync/wake/wake-contract.md` still says "Two spaces between sentences".
  The wake responder is model-written text posted by the daemon, so it cannot emit U+00A0.
  A daemon-side conversion in `simple-chat post` would make the Zulip rule automatic.
- Several tests in `scripts/test_fleet_skill_identity.py` already fail on main (15 of 49).
  This change adds none.
