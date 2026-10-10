---
name: owner-copy
description: Fleet human-facing prose — two spaces between sentences, light theme default, Title Case headings, no agent names in App Store/TestFlight notes, 12-hour am/pm, no timezone abbreviation. Use when writing UI strings, ASC listing fields, PR/commit/chat/Notes prose, release notes, or any paragraph a human will read. Also when changing theme defaults or taking screenshots.
---

# Owner-facing copy (Universal)

Canonical detail: `/Users/jay/apps/FLEET-UI-COPY.md`.  Policy: `/Users/jay/apps/AGENT-SYNC.md` § Two spaces, timestamps, TestFlight metadata.

## Two spaces between sentences

Full protocol (always follow, do not weaken): skill `sentence-gap`
(this pack's `sentence-gap` — Monet portable protocol).

Binding for every paragraph a human reads — in-app UI, ASC description / promotional text / What’s New / review notes, push, email, help, Apple Notes, effort boards, **chat replies**, PR titles/bodies, commit messages, Zulip.

- **Files** (repo docs, commit messages, code comments, config, terminal output): two literal ASCII spaces after `.` / `!` / `?` before the next sentence.  Do not write `&nbsp;` into files.
- **GitHub PR and issue titles, bodies and comments, and Zulip posts:** a real U+00A0 plus a space after each sentence (owner ruling 2026-10-08).  Never the `&nbsp;` entity there: GitHub can copy a PR body into a plain-text squash commit, where it would show literally.
- **BotFleet / OpenMausBot / cloud chat:** two ASCII spaces.  Never display the six characters `&nbsp;` (owner 2026-09-03).  Backend inserts a real U+00A0 if the renderer would collapse the gap.
- **Claude Code desktop app (Code tab) chat replies:** type the `&nbsp;` entity plus a normal space after each sentence, outside code spans (owner-verified 2026-10-08; the renderer decodes it, a raw U+00A0 from the model arrives as a plain space, and ASCII doubles collapse).  The 2026-09-04 ASCII ruling is withdrawn.
- **Any other Markdown-rendering agent chat pane** (Codex, Cursor, Antigravity, Grok, Kimi, MiniMax, DeepSeek, Fx, Muse): the same entity plus a space (owner ruling 2026-10-08), not individually verified.  If the six literal characters ever show, stop and report it in #agent-sync.  **Terminal TUIs** (Claude Code CLI, Grok TUI, Codex CLI): two ASCII spaces, unverified.

Headings / titles / buttons: **Title Case**.  Body: sentence case.  Values that are not a full sentence: lowercase or sentence case.

Single space stays correct after non-terminal abbreviations (`e.g.`, `v1.2.3`).  Two trailing spaces at the **end** of a Markdown line are a hard break — a different rule.

HTML/JSX/SwiftUI that collapse spaces: NBSP+space or `SENTENCE_GAP`.  Do not "fix" `Congress.Trade`, `Socratic.Trade`, URLs, emails, or `U.S.`.

Does not apply: identifiers, log lines, API enums, commit **subjects** that are fragments with no terminator.

## Theme default = light

First visit / no stored preference = **light**.  Do not boot dark from `prefers-color-scheme` unless the user chose System or Dark.  Dark is optional.  Screenshots, ASC, marketing: light unless the owner asked for dark.

## Headings vs values

- Headings / titles / buttons: **Title Case**.
- Values / secondary status: sentence case or lowercase (`not reported`, `ask-first`).
- Congress and Congressional take a capital C.  Brand **Congress.Trade**, **DealDex**, **Socratic.Trade**.
- Compact money suffixes lowercase (`$99.8k`).  Do not say “Live” on account rows; paper is `Alpaca (paper)`.
- CT latency: never print `+`/`−` on lead/lag.  Say **earlier** (green) or **later** (red).
- No All-Assets dropdown on CT web/iOS.

## Nothing is truncated without recourse (owner 2026-09-04)

Any text the UI clips owes its full value on hover — table cells, card titles, company
names, filenames, URLs, rationales, stack frames, error banners.  Native `title` is the
baseline; a real tooltip component is better where the app already has one.

- **Errors are the strict case.**  An error someone cannot finish reading is an error they
  cannot act on — no offending field, no request id, nothing to paste to an agent.  Long
  ones get an expand and a copy affordance too.
- Truncate in CSS (`text-overflow`, `line-clamp`), never `.slice(0, N)` / `substring` — a
  string shortened in code never reaches the DOM, so nothing downstream can surface it.
  If a payload genuinely must be capped, say so (`… 12 more lines`), never silently.
- Hover is the floor, not the ceiling.  Hover does not exist on touch and does not fire on
  keyboard focus, so the same value must also be reachable by tap and by focus.
- Never put a secret, token, session cookie, or signed URL in a `title`.  Redact and keep
  the shape (`sk-…4f2a`), or surface a request id instead.

## Product truth in listing copy

- Congress.Trade corpus is House, Senate, **and Executive Branch** (OGE 278-T) — never "Congress-only."
- Premium trial length must match the live ASC intro offer (**2 weeks** as of 2026-08-14), never a leftover 1-month.
- **No internal agent names** in TestFlight / App Store / public release notes.

## Timestamps (owner-facing agent writing)

Say every time on the owner's clock: 12-hour, with am or pm.  That clock is Central.  Do not type CDT, CST, or CT.  The owner assumes am/pm is their time.  Write `3:15am`, not `3:15am CDT` and not `08:15Z`.  When the day matters, write `Sun, Oct 5, 2026 at 3:15am`.  Name a zone only when the time is not that clock.  The usual case is UTC, in parentheses after the local time: `3:15am (2026-10-05T08:15:00Z)`.  Never lead with Zulu or a 24-hour UTC stamp in chat, Notes, Zulip, boards, or PRs.  `00:00 UTC` is 7:00pm the previous calendar day during daylight saving, and 6:00pm the previous calendar day after the fall-back.  Convert with `TZ=America/Chicago date` or Python `ZoneInfo("America/Chicago")`.  Printing a zone abbreviation on a local time is the failure mode.  Binding for every agent, bot, and platform.

Product UI times are the **viewer's** timezone except market-day accounting (Chicago) and session bells (`9:30 AM ET`).

## Canon

- `/Users/jay/apps/FLEET-UI-COPY.md`
- `/Users/jay/apps/AGENT-SYNC.md` § Two spaces; Theme via FLEET-UI-COPY; TestFlight template
- Skills: `apple-notes`, `sentence-gap`, `closeout`
