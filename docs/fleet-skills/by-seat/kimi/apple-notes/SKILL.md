---
name: apple-notes
description: >-
  Write, read and fix owner-facing Apple Notes in the `Coding` folder — plans, designs, reviews, handoffs, rollouts, living Completion notes, and static notes such as the Background Jobs Master List.  Use whenever an agent produces something the owner needs to read, not only when they say "Notes."  Works the same on the Mac, the server and cloud seats (simple-notes or the notes_* tools).  Title [TEAM_KEY, Agent] Topic (Title Case) with a refreshed timestamp.
---

# Apple Notes (Universal)

> **Retired seat.**  Owner directive 2026-08-21: do not assign or accept new Kimi work.  Do not start a Kimi session.  Do not take work.  This catalog copy is inactive — do not install to `~/.kimi`.


Every host has the same tool set: `simple-notes` (CLI) and the `notes_*` MCP tools, verb for verb: `create`, `update`, `pin`, `unpin`, `read`, `list`, `status`, `log` (AFC-486, `docs/protocols/operator-notes.md`).  On the Mac they run the helper below directly; on the server and from cloud seats they go through the Operator's notes queue to a runner on the Mac and wait for its answer.  No Notes request goes through Zulip.

```bash
simple-notes create "[AFC, Claude] Topic" --body-file body.md
simple-notes read "⭐️ Background Jobs Master List" --format html > list.html   # edit list.html, then:
simple-notes update "⭐️ Background Jobs Master List" --html list.html
simple-notes list --query "AFC"
simple-notes log --since-hours 24
```

- **Who may change what.**  A create's title carries your own agent name.  Update, pin, unpin, read and list work on any Coding note: fixing formatting across other agents' notes is expected.  An update never creates a note whose title is not yours (a missing one answers `not_found`).
- **Read before you rewrite.**  `read --format html` returns the content without the title, date and Session rows (the helper writes those on every save), ready to send back with `update --html`.
- **The helper formats and logs every write**, wherever it came from (date row, Session row, sentence gaps, fleet dates, links).  `simple-notes log` shows who changed which note and what was reformatted.

Notes.app does not render raw Markdown.  The helper converts MD → HTML.  Pass `--html` only when you already have Notes-safe HTML.

## Links (the helper does this for you)

Write the reference the way you would say it and the helper links it (owner 2026-10-25: "links everywhere and prefer apple notes have them even").

- `OPS-1000` becomes a link to that Linear issue.  `PR #957` becomes a link to that pull request, and the text is normalised to the house form `PR #N`.  A list works too: `PRs #380, #386 and #389` becomes one link per number, and a file count that follows a list is not one of them.
- A pound sign means a pull request and a hyphen means a Linear issue, so `AFC#957` is a pull request in `AI-Fleet-Coordinator` and `AFC-547` is the Linear issue of that team.  There is one Linear team per repository: `AFC` is `AI-Fleet-Coordinator`, `OPS` is `Fleet-OPS`, `CTS` is `congress-trading-shared`.
- A bare `PR #N` is read from the block it sits in: a repository named next to it, the subject of a table row, the one team the block names, the nearest repository named near it, the team key in the note title, and the fallback repository.  When the block names two repositories and the reference names none of them, it stays plain text, because a link to the wrong repository is worse than no link.
- A `CTS` issue is implemented in `Congress.Trade`, `Socratic-Trade` and `Usage-Monitor` as well, so the helper asks GitHub which of them holds the number, and leaves the reference alone when none of them does.
- Left alone: code spans, fenced blocks, an identifier inside a URL, a hand-written link (never doubled), and an identifier that is not a Linear issue, such as `SRV-S07`.
- The helper says what it linked on stderr (`notes: linkified:  2 project references and 1 pull request reference became links`) and logs the counts under `reformatted.links`.
- `NOTES_LINKIFY=0` turns the step off for one run.

## Helper (the Mac)

```bash
/Users/jay/apps/apple-notes-coding.sh "Title" "plain body"
/Users/jay/apps/apple-notes-coding.sh "Title" --html /path/to/body.html
/Users/jay/apps/apple-notes-coding.sh --update "Title" "body"
/Users/jay/apps/apple-notes-coding.sh --update "Title" --html /path/to/body.html
/Users/jay/apps/apple-notes-coding.sh --pin-only "Exact Title"
/Users/jay/apps/apple-notes-coding.sh --unpin-only "Exact Title"
```

Also: `--notify` / `--pushover`, `--needs-owner` / `--action-required`, `--summary "one sentence"`, `--pr "18"`.

Default is headless pin via the `Pin Coding Note` shortcut (no focus steal).  Do not `APPLE_NOTES_ACTIVATE=1` unless the owner is sitting at the Mac.

There is no GUI fallback.  When the Shortcut does not pin, the helper prints `pin skipped: ... note saved unpinned` on stderr and the create or update still succeeds (exit 0); `--pin-only` exits `4`.  Nothing brings Notes to the front except an explicit `--activate` or `APPLE_NOTES_ACTIVATE=1`.  `--pin` and `APPLE_NOTES_PIN=1` are accepted and change nothing.  Never loop on a failed pin: one pin or unpin Shortcut runs at a time across processes, a run that timed out is not repeated, and `APPLE_NOTES_PIN=0` skips pinning when you only need the write.  The `notes_create` and `notes_update` results and `simple-notes` carry a `pin_skipped` reason when that happened, so "done" with `pin_skipped` means the note is saved but not pinned.

**Pass your session id.**  Every `simple-board` write (`file`, `claim`, `comment`, `status`), every `notes_create` / `notes_update` call (and `simple-notes`), and `apple-notes-coding.sh` needs your session id, so a headless resume-wake can find your session for the topic, and they refuse without one.  Pass it as `--session <id>` (the `session` argument on the MCP tools), else export `AGENT_SESSION`, else the tool reads your platform's own session variable, which your global rules file names.  Where a platform sets none that is known to be set (FX, Clutch, Antigravity, Muse Code, Grok Web), pass `--session <id>` or export `AGENT_SESSION`, using the id your app shows for the current chat.  An MCP server may not inherit the platform variable, so pass `session` explicitly on `notes_create` / `notes_update`; cloud seats have no shell env and always pass `session`.  Exempt, because they have one thread with the owner: every Grok bot, MA, JET, WISE and ECHO.  Notes get a small `Session: <id>` line under the timestamp.  A chat tag such as `session8` is not a session id and is refused.

## Title

```
[TEAM_KEY, Agent] Topic
```

- Team key first, then the agent, then the topic in Title Case (owner 2026-10-19: never the app's long name, never the seat's long name).
- Agent is your verified seat’s canonical Title Case `notesName`, such as `Codex` or `Claude`; the exact uppercase seat tag is compatible.
- Multi-app: `[ST, CT, Agent] …` (impact order).
- No date in the title.  No word "session".  Do not repeat the title as an H1 in the body.

| Acronym | App |
|---------|-----|
| UM | Usage-Monitor |
| ST | Socratic.Trade |
| CT | Congress.Trade |
| CTS | congress-trading-shared |
| DD | DealDex |
| PS | Personal-Site |
| AFC | AI-Fleet-Coordinator (this repo / Mac collab / skill pack) |
| OPS | fleet-ops (sibling identity; do not invent a checkout here) |

## Second body row

The helper injects/refreshes:

```
Sun, Aug 9, 3:52pm · PR #18
```

Local Mac time, no leading zeros, lowercase am/pm.  Refresh on every `--update`.

Then: type line (`Completion` / `Plan` / `Review` / `Design` / `Handoff` / `Rollout` / `Incident` / `Fleet change` / `Work log`), then content.

Order: `Needs owner` first when applicable, then Problem → What was done → Decisions → Next steps.

A real U+00A0 plus a normal space between sentences in the body file you pass the helper (owner ruling 2026-10-23).  Never write the `&nbsp;` entity yourself.

## Layout (owner 2026-08-21; updated 2026-10-01 — binding)

Notes.app collapses adjacent top-level blocks.  A wall of text with no air is a bug.  The owner reads these on iPhone.

Both Markdown and HTML are supported natively by `apple-notes-coding.sh` (as an argument, piped via stdin, or via `--html /path/to/file.html`).

- `<h2>` never `<h1>` (the helper already wraps the title as `h1`)
- Spacers (`<div><br></div>`) belong ONLY *between* top-level sections (after headings, after paragraphs, after lists/tables, before the next section).
- **NEVER put `<div><br></div>` inside `<ul>` or `<ol>` lists**: WebKit converts block `<div>` inside lists into empty bullet points (`<li><br></li>`) and breaks numbered list sequences.  Keep list items as clean `<li>` elements.
- When generating Markdown lists, write standard bullets (`* Item`) and numbered steps (`1. Step`).  The helper automatically formats them with clean, tight typography without ghost bullet dots.
- In shell commands, ALWAYS quote your body variable (`"$BODY"`): unquoted `$BODY` causes bash to collapse all newlines into single spaces, creating unformatted single-line walls of text.

Never pass empty `- ` bullets (they render as blank dots).  Put identifiers with underscores in backticks (`merge_commit_sha`) so Markdown italic does not eat the underscores and mash the word (`mergecommitsha`).

## When

Do Notes: plans, design docs, reviews, handoffs, rollouts, **Completion / work-complete** for anything the owner might ask about.

Skip: pure #Work-In-Progress topic chatter, Linear issue edits, routine commit messages, peer-only PR nits.

Open a living work note when substantial work starts.  Always Completion at the end.  Update in place; unpin stale Completion notes when the lane closes (`--unpin-only`).

Background-jobs inventory note is `⭐️ Background Jobs Master List` — refresh that title exactly when `MAC-LOCAL-PROCESSES.md` changes.

## Canon

- `/Users/jay/apps/FLEET-PROTOCOL.md` § Apple Notes
- Skills: `closeout`, `owner-copy`

### Handoff Reports (Immediate & Living)
When generating a handoff report for a peer agent to take over (e.g. before hitting quota or upon owner stop request):
- **Title Format:** `⭐️ [TEAM_KEY, Agent] HANDOFF REPORT: Short topic` (or `*** [TEAM_KEY, Agent] HANDOFF REPORT: Short topic`)
- **6-Section Body:**
  1. Executive Summary & Objective
  2. Current Work State & Artifacts (worktree path, branch, commit SHA, PR status, dirty/stashed files)
  3. What Was Completed
  4. What Remains to Be Done (actionable numbered list for substitute agent)
  5. Gotchas, Blockers & Open Decisions
  6. Reproduction & Verification Commands
