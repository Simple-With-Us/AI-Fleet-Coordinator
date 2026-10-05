---
name: owner-copy
description: Fleet human-facing prose — two spaces between sentences, light theme default, Title Case headings, no agent names in App Store/TestFlight notes, Central Time labels. Use when writing UI strings, ASC listing fields, PR/commit/Slack/Notes prose, release notes, or any paragraph a human will read. Also when changing theme defaults or taking screenshots.
---

# Owner-facing copy (MC)

> **This install is for `MC`.** Slack `[MC]`.  Notes `Muse Code`.  Branches `muse-code/`.  Worktrees `~/apps/<app>-muse-code`.  Do not inherit another seat's tag from a shared template.

> **Runtime (Muse Code).** Muse Code (`muse` CLI) is the interactive terminal coding agent (`[MC]`).  Notes name `Muse Code`.  Branches `muse-code/`.  Worktrees `~/apps/<app>-muse-code`.  Distinct from **Muse Assistant** (`[MA]`, former tag `[MUSE]`), which is the cloud VM batch compute / creative assistant dispatched via Mac/iOS apps.  Project `AGENTS.md` and `CLAUDE.md` load automatically when the workspace is trusted in `~/.config/muse/trust.json`.  Skills installed here (`~/.config/muse/skills`) shadow foreign personal skills.


Canonical detail: `/Users/jay/apps/FLEET-UI-COPY.md`.  Policy: `/Users/jay/apps/AGENT-SYNC.md` § Two spaces, timestamps, TestFlight metadata.

## Two spaces between sentences

Full protocol (always follow, do not weaken): skill `sentence-gap`
(this pack's `sentence-gap` — Monet portable protocol).

Binding for every paragraph a human reads — in-app UI, ASC description / promotional text / What’s New / review notes, push, email, help, Apple Notes, effort boards, **chat replies**, PR titles/bodies, commit messages, Slack.

- **Files** (repo docs, commit/PR/Slack/Notes source): two literal ASCII spaces after `.` / `!` / `?` before the next sentence.  Do not write `&nbsp;` into files.
- **BotFleet / OpenMausBot / cloud chat:** two ASCII spaces.  Never display the six characters `&nbsp;` (owner 2026-09-03).  Backend inserts a real U+00A0 if the renderer would collapse the gap.
- **Claude Code desktop app (Code tab):** two literal ASCII spaces (owner-verified 2026-09-04; the 2026-08-19 entity advice is withdrawn).

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

When you tell the owner a time, say it in Central Time.  Write `Sat, Aug 22, 2026 at 7:00 PM CT`.  Always label `CT` / `CDT` / `CST`.  Never UTC-only in chat, Notes, Slack, boards, or PRs.  UTC may follow in parentheses after the Central stamp.  `00:00 UTC` is 7:00 PM CT the previous calendar day in CDT (6:00 PM CT in CST).  Convert with `TZ=America/Chicago date` or Python `ZoneInfo("America/Chicago")`.  Unlabeled local time is the failure mode.

Product UI times are the **viewer's** timezone except market-day accounting (Chicago) and session bells (`9:30 AM ET`).

## Canon

- `/Users/jay/apps/FLEET-UI-COPY.md`
- `/Users/jay/apps/AGENT-SYNC.md` § Two spaces; Theme via FLEET-UI-COPY; TestFlight template
- Skills: `apple-notes`, `sentence-gap`, `closeout`
