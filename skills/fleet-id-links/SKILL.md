---
name: fleet-id-links
description: Make every Linear issue, Linear project and PR you mention something Jay can tap, the way Zulip linkifiers work.  Use whenever a message names an ID such as AFC-637, AFC#123 or P-BF-6.  Each seat picks the link format that works on its own surface and reports what it learned on the tracking issue.
---

# Fleet ID links

Jay wants every Linear issue, PR and Linear project in a message to be a link Jay can tap.  Zulip does this on its server.  Other surfaces differ, and most have not been tested, so each seat chooses the format that works where it writes.

## Choose your format

Find your surface below and use the first option that shows up as a working link.  These are starting guesses, not verified facts, unless the row says verified.

| Surface | Try first | Then | Last resort |
| -- | -- | -- | -- |
| Desktop chat pane (Claude Code tab: verified Markdown; Cursor, Codex app, Antigravity, MiniMax Code, Z Code, Clutch, Muse Code: untested) | Markdown link | HTML anchor | Full URL |
| Terminal app (Claude Code CLI, Codex CLI, Grok TUI, fx, OpenCode: untested) | Full URL | Markdown link | Bare ID |
| Zulip (verified, server-side) | Bare ID | Markdown link | Full URL |
| GitHub and Linear text | Markdown link | Full URL | Bare ID |
| Apple Notes | Bare ID through the Notes helper | HTML anchor with `--html` | Full URL |
| Plain text (commit messages, iMessage, plain email) | Full URL | Bare ID | |
| Any surface not listed | Full URL | Markdown link | Bare ID |

The formats:

- Markdown link:  `[AFC-637](https://linear.app/simple-with-us/issue/AFC-637)`
- HTML anchor:  `<a href="https://linear.app/simple-with-us/issue/AFC-637">AFC-637</a>`
- Full URL:  `https://linear.app/simple-with-us/issue/AFC-637`
- Bare ID:  `AFC-637`

If you cannot see how your output renders, use the full URL.  It stays readable even when it is not clickable, while a Markdown link or HTML anchor that does not render shows up as raw markup.

## Report what you learn

When you find out what works on your surface, or that a row here is wrong, add a comment to [AFC-698](https://linear.app/simple-with-us/issue/AFC-698) with your seat, the surface, the format you tried, and whether it rendered as a working link.  Jay's confirmation counts as verified;  your own guess does not.  This table gets updated from those comments.

## Building the URL

Source of truth:  `scripts/zulip_linkifiers.py` (`TARGET` and `LINEAR_TEAMS`) in Simple-With-Us/AI-Fleet-Coordinator.  If a prefix is not below, read that file;  if it is still unknown, leave the ID plain and say so.

### Linear issues:  `KEY-n`

URL:  `https://linear.app/simple-with-us/issue/KEY-n`

Team keys:  AFC, CTS, OPS, SWU, AR, BF, CC, CL, CT, DD, FL, HH, PS, ST, UM, C.

### PRs:  `ACRONYM#n`

URL:  `https://github.com/<owner>/<repo>/pull/n`

| Acronym | Repo |
| -- | -- |
| AFC | Simple-With-Us/ai-fleet-coordinator |
| BF | Simple-With-Us/BotFleet |
| CT | Simple-With-Us/Congress.Trade |
| ST | Simple-With-Us/Socratic-Trade |
| UM | Simple-With-Us/Usage-Monitor |
| DD | Simple-With-Us/DealDex |
| CL | Simple-With-Us/ContactLogo |
| PS | Simple-With-Us/Personal-Site |
| HH | Simple-With-Us/HogHunter |
| CC | Simple-With-Us/CodeCaps |
| C | Simple-With-Us/Clutch |
| CTS | Simple-With-Us/congress-trading-shared |
| OPS | Simple-With-Us/Fleet-OPS |
| AR | Simple-With-Us/Autorotate |
| FL | Simple-With-Us/FleetLink |
| FLL | Simple-With-Us/fleetlink-legacy |
| SWU | Simple-With-Us/Simple-With-Us |

Any other `Repo#n`, for example `Autorotate#4`:  `https://github.com/Simple-With-Us/Repo/pull/n`.

Every app answers to its acronym and its repo name:  `HH#n` or `HogHunter#n`, `C#n` or `Clutch#n`, `CC#n` or `CodeCaps#n`.  `C-n` is a Clutch Linear issue.

### Linear projects:  `P-KEY-n`

Project links use the project's slug id, so they cannot be built from the code.  Take the URL from the linear-projects block in `scripts/zulip_linkifiers.py`, or from the project's `url` in Linear.  The form is `https://linear.app/simple-with-us/project/<slug>`.  When naming a project by its name, link the name the same way.

## Rules

- Link an ID once per message, where it first appears.
- Only link an ID whose number came from a tool, the board or Jay.  Never invent a number.
- Keep the link text as the ID itself (AFC-637, AFC#123) so it reads like Zulip.
