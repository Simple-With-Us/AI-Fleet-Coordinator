---
name: grok-web-work-items
description: How GROK-WEB reserves, releases, and closes fleet work (THE BOARD until the Linear cutover, then Linear), hands Mac-only work to a Mac seat, and opens PRs.  Use before claiming any item, before asking a Mac seat to implement, before opening or landing a PR, and at closeout.
---

# Work Items, Handoff, and PRs (GROK-WEB)

## Reservation
Linear replaces THE BOARD (owner 2026-10-08):  workspace `simple-with-us`, ids like `AFC-123`, reached through your Linear connector.  Until the cutover is announced in `#Work-In-Progress` › `fleet`, THE BOARD is the system of record and a Mac seat or Jay reserves on it for you.

After cutover:
- Claim:  move the issue to In Progress, add label `claimed:GROK-WEB`, comment `CLAIMED`.
- Release:  move it back to Todo.
- Close:  Done plus a `DONE` comment.

Linear runs under Jay's login (his one exception), so the first line of every Linear comment and issue body is `[GROK-WEB·<session>]`, typed by you, with the same session value you use in Zulip.  Never take an issue another seat holds, and never claim or comment as Jay.  A topic post is not a reservation;  the item is.

## Mac Handoff
You have no Mac shell or checkout.  Work that must land on the Mac, and anything needing Xcode, the Simulator, or Apple Notes, goes to a Mac seat:  one ask in the work topic with `to`, the constraints, and what done looks like.  Prefer the app's BotFleet engine when it has one.

## PRs
Only if this chat itself can open a PR:
- Branch `grok-web/<slug>`;  after cutover `grok-web/<issue-id>-<slug>`.  Never `main`;  nobody pushes `main`.
- Body:  `Fixes AFC-123`, or `Refs AFC-123` for partial work.
- Claim first.  Title and body use the sentence gap:  a real U+00A0 plus a normal space, owner ruling 2026-10-23, one mechanism on every surface, with one exception:  the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane) take the `&nbsp;` entity plus a normal space;  the entity is never the form anywhere else.
- If you cannot arm auto-merge, ask in the work topic.
- Post `DONE` with the PR link.
- Never invent a GitHub repo.
