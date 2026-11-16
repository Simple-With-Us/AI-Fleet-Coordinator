---
name: owner-copy
description: Fleet human-facing prose — two spaces between sentences (a real U+00A0 plus a space on every surface, except the Claude Code Code tab, the MiniMax Code chat pane and the Z Code chat pane, which take the &nbsp; entity plus a space), Light theme default, Title Case headings, no agent names in App Store/TestFlight notes, 12-hour AM/PM, no timezone abbreviation.  Use when writing UI strings, ASC listing fields, PR/commit/chat/Notes prose, release notes, or any paragraph a human will read.  Also when changing theme defaults or taking screenshots.
---

# Owner-facing copy (CODEX)

> **This install is for `CODEX`.**  Chat tag `[CODEX·session8]`.  Notes `Codex`.  Branches `codex/`.  Lanes `~/apps/lanes/<Repo>/codex-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot for seat `CODEX`, credential in Infisical Fleet-OPS `/zulip` (`CODEX_ZULIP_EMAIL`, `CODEX_ZULIP_KEY`, `CODEX_ZULIP_SITE`).  Session tag `[CODEX·session8]`, and the `simple-chat` CLI writes it for you.


Canonical detail: `~/apps/FLEET-UI-COPY.md`.  Policy: `~/apps/FLEET-PROTOCOL.md` § Two spaces, timestamps, TestFlight metadata.

## Two spaces between sentences

Full protocol (always follow, do not weaken): skill `sentence-gap`
(this pack's `sentence-gap` — Monet portable protocol).

Binding for every paragraph a human reads — in-app UI, ASC description / promotional text / What’s New / review notes, push, email, help, Apple Notes, Linear issues and comments, **chat replies**, PR titles/bodies, commit messages, Zulip.

Every message to Jay needs the gap, including a one-line progress note and an acknowledgement: getting it right in artifacts while writing plain single spaces in the chat is still a failure (owner ruling 2026-10-20).  **Read the reply back before sending it** and check every `.`, `!` and `?` boundary.

- **Every surface** (owner ruling 2026-10-23):  chat replies in any other pane, Zulip, GitHub PR and issue text and review comments, Linear, commit messages, repo docs, skills, agent instructions and UI copy all take a real U+00A0 plus a normal space after each sentence.  Generate it in the shell rather than typing it.
- **The one exception:  the Claude Code Code tab, the MiniMax Code chat pane and the Z Code chat pane.**  Those panes normalize a raw U+00A0 away, so a chat reply there uses the `&nbsp;` entity plus a normal space, outside code spans.  The entity is never the form anywhere else.  An Apple Note body takes the same U+00A0 plus a normal space as every other surface:  Notes stores it as an ordinary space and still shows a wide gap.
- **Never the `&nbsp;` entity anywhere else.**  It shows up as literal text in squash-merge commits, plain-text email and many APIs.
- **Never two plain ASCII spaces.**  Rendered surfaces collapse them to one.
- Code, identifiers and machine-readable config never get a U+00A0.  If any other surface strips it, leave the text as it is and report the surface;  do not switch to the entity.

Headings / titles / buttons: **Title Case**.  Body: sentence case.  Values that are not a full sentence: lowercase or sentence case.

Single space stays correct after non-terminal abbreviations (`e.g.`, `v1.2.3`).  Two trailing spaces at the **end** of a Markdown line are a hard break — a different rule.

HTML/JSX/SwiftUI source: Swift `"\u{00A0} "`, JS/TS strings `"\u00A0 "`, JSX text `{'\u00A0'}` plus a space or `&#160;`, HTML markup source `&#160;`, or a shared `SENTENCE_GAP` constant;  never `&nbsp;` in prose.  Do not "fix" `Congress.Trade`, `Socratic.Trade`, URLs, emails, or `U.S.`.

Does not apply: identifiers, log lines, API enums, commit **subjects** that are fragments with no terminator.

## Theme Default = Light

First visit / no stored preference = **light**, under the current owner rule.  This supersedes the older System default; the development machine's appearance does not choose the product's first paint.

- **Stored preferences are never rewritten.**  A returning user who chose Light or Dark stays
  on it; only a first paint with no record defaults to Light.
- **Light, Dark and System all remain available** wherever a picker ships.  Light is the
  default, not the only choice.
- **Screenshots, App Store Connect, marketing, design previews: light**, unless the owner asks
  for dark.  The product default is Light; the capture default is also light, because those
  assets need a deterministic palette regardless of who is developing.
- Applies to every fleet app (web, iOS, Android, BotFleet chrome, CodeCaps).  Each seat lands
  the default flip in its own app; this file is the canonical signal, not the per-app change.

## Headings vs values

- Headings / titles / buttons: **Title Case**.
- Values / secondary status: sentence case or lowercase (`not reported`, `ask-first`).
- Congress and Congressional take a capital C.  Brand **Congress.Trade**, **DealDex**, **Socratic.Trade**.
- Compact money suffixes lowercase (`$99.8k`).  Do not say “Live” on account rows; paper is `Alpaca (paper)`.
- CT latency: never print `+`/`−` on lead/lag.  Say **earlier** (green) or **later** (red).
- No All-Assets dropdown on CT web/iOS.

## Nothing is truncated without recourse (owner 2026-09-04)

Any text the UI clips owes its full value on hover — table cells, card titles, company
names, filenames, URLs, rationales, stack frames, error banners.  Native `title` is the
baseline; a real tooltip component is better where the app already has one.

- **Errors are the strict case.**  An error someone cannot finish reading is an error they
  cannot act on — no offending field, no request id, nothing to paste to an agent.  Long
  ones get an expand and a copy affordance too.
- Truncate in CSS (`text-overflow`, `line-clamp`), never `.slice(0, N)` / `substring` — a
  string shortened in code never reaches the DOM, so nothing downstream can surface it.
  If a payload genuinely must be capped, say so (`… 12 more lines`), never silently.
- Hover is the floor, not the ceiling.  Hover does not exist on touch and does not fire on
  keyboard focus, so the same value must also be reachable by tap and by focus.
- Never put a secret, token, session cookie, or signed URL in a `title`.  Redact and keep
  the shape (`sk-…4f2a`), or surface a request id instead.

## Product truth in listing copy

- Congress.Trade corpus is House, Senate, **and Executive Branch** (OGE 278-T) — never "Congress-only."
- Premium trial length must match the live ASC intro offer (**2 weeks** as of 2026-08-14), never a leftover 1-month.
- **No internal agent names** in TestFlight / App Store / public release notes.

## Timestamps (Owner-Facing Agent Writing)

**The current fleet format, one form for owner-facing agent writing:**

| What | Write | Never |
|---|---|---|
| A time | `3:42 PM` | `15:42`, `3:42pm`, `3:42 PM CDT`, `20:42Z` |
| A date | `Mon, Oct 19` | `2026-10-19`, `10/19`, `19 Oct`, `October 19th` |
| Both | `Mon, Oct 19, 3:42 PM` | `2026-10-19T20:42:00Z`, `Oct 19 at 15:42` |
| Another year | `Fri, Mar 7, 2025` and `Fri, Mar 7, 2025, 9:05 AM` | `2025-03-07` |

The year appears only when it is not the current year.  An ISO date is for machines: file names, branch names, code, logs inside a code block, and source-file comments.  The Apple Notes script rewrites any ISO date it finds in a note's prose into this form, so a note always reads the same whichever agent wrote it.

Use the owner's `America/Chicago` clock, 12-hour with a normal space before uppercase AM or PM.  Write `3:15 AM`, with no zone label or adjacent UTC stamp.  Convert times read from logs, APIs, CI and dashboards before writing owner-facing prose.  A raw machine timestamp stays unchanged inside machine output; quote it only when the owner asks or when the exact machine field is needed in a code block.  Convert with `TZ=America/Chicago date` or Python `ZoneInfo("America/Chicago")`; never append a zone name or abbreviation to the result.  Keep build numbers and commit provenance in their required machine format.  This rule covers chat, Notes, Zulip, boards, PRs and other agent prose on every platform.

Product UI times are the **viewer's** timezone except market-day accounting (Chicago) and session bells (`9:30 AM ET`).

## Canon

- `~/apps/FLEET-UI-COPY.md`
- `~/apps/FLEET-PROTOCOL.md` § Two spaces; Theme via FLEET-UI-COPY; TestFlight template
- Skills: `apple-notes`, `sentence-gap`, `closeout`
