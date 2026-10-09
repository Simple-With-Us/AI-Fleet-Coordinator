# Sentence-gap skill (portable — paste into any LLM tool)

**What this is:** a drop-in instruction block that teaches an AI assistant to put a
**visible double gap between sentences** in everything a human reads.  Paste it into a
system prompt, custom instruction, `AGENTS.md`, `CLAUDE.md`, Cursor rule, Gemini gem,
Grok custom instruction, or a skill file.  Platform-agnostic.

**Why it exists:** the naive instruction ("use two spaces after a period") **silently
fails on most chat surfaces** and the assistant will insist it is complying while the
human sees no change.  This block front-loads the findings so no one re-derives them.

---

## PASTE FROM HERE

### Rule: two visible spaces between sentences

Put a **visibly wider gap** between sentences — after `.` `!` `?` when a new sentence
follows — in every piece of prose a human reads.  Not just product copy: chat replies,
commit messages, PR titles and bodies, code comments, docs, tickets, Zulip posts, release
notes, design docs.

Do **not** add a gap after a non-terminal abbreviation (`e.g.`, `i.e.`, `Dr.`, `v1.2.3`),
inside a URL, email, filename, or a brand name containing a period.

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
| **Chat / transcript UI that expands HTML entities** (Claude Code) | the literal entity text `&nbsp;` then a normal space → `End.&nbsp; Next.` | Only on surfaces where the renderer expands the entity so the owner never sees those six characters. |
| **Plain-text chat (no Markdown rendering)** | two literal ASCII spaces | Nothing collapses them; an entity would show as the ugly text `&nbsp;` |
| **Files read as source** — repo docs, commit messages, code comments, config, diffs | two literal ASCII spaces | Read in an editor/terminal/`git diff`, which preserve them verbatim; an entity would appear literally |
| **HTML / JSX / SwiftUI / any rendered product copy** | a real U+00A0 plus a space, or a shared `SENTENCE_GAP` constant | Raw double spaces collapse in HTML.  Source may use the entity only when the renderer expands it.  The owner must never see `&nbsp;` as text. |
| **Markdown source** | two literal spaces *between* sentences | ⚠️ Two spaces at the **end of a line** is the unrelated hard-line-break syntax — don't confuse the two |

### Verify, don't assume — run this self-test once per platform

The table above is a starting point, not gospel: renderers differ and change.  **On your
first run in a new tool, test it and ask the human what they actually see.**

> Output these two lines verbatim, then ask which shows a wider gap:
>
> A. `Sentence one.&nbsp; Sentence two.`
> B. `Sentence one.  Sentence two.`
>
> If A looks wider → use the `&nbsp;` entity on this surface.
> If B looks wider, or they look identical → use two literal spaces.
> If neither shows a gap → say so plainly and ask how they want it handled.
> Then keep using whichever won, for that surface, for the rest of the session.

### Already tested — do NOT burn time re-trying these

Verified on Claude Code (terminal + desktop), 2026-08-19/20:

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
- ✅ **The literal entity text `&nbsp;` + a space in chat** — renders as a visibly wider
  gap.  This is the one that worked.
- ✅ **Two literal ASCII spaces in files** — correct and simplest; leave file content alone.

### The transferable lesson

When an instruction *appears* not to take effect, **stop repeating the promise and
diagnose the rendering layer between you and the reader** — then ask them what is on their
screen.  Four rounds of "fixed it!" were spent here before anyone checked whether the
change could be seen at all.  Intent is not output; output is not what is displayed.

## PASTE TO HERE

---

## Import notes per platform

- **Cursor / Windsurf** — `.cursorrules` or a Project Rule; applies to chat + inline edits.
- **Codex / ChatGPT** — custom instructions, or an `AGENTS.md` at repo root.
- **Gemini** — a Gem's instructions, or `GEMINI.md` where supported.
- **Grok** — custom instructions.
- **Claude Code / Claude Desktop** — `CLAUDE.md` (user or project scope), or a skill file.
- **Any API/system-prompt integration** — paste the block verbatim into the system prompt.

Trim the "Already tested" section only if the target tool is not Claude Code — but keeping
it is cheap and stops an agent from repeating the raw-NBSP mistake, which is the one that
looks like success.
