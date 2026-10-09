# Title + structure standard (binding — all seats, all apps; owner 2026-08-09)

> Moved from `AGENT-SYNC.md` on 2026-09-01 (Plan B slice 2, doc diet).  Still binding for every agent on every platform.  Canonical pointer stays in AGENT-SYNC.md; this file is the full text and is ingested into the fleet-agents corpus (`recall`).

**Title (note name / first heading row) — ALWAYS start with app acronym(s) + agent:**

```
[APP, Agent] short topic title
```

Examples:
- `[UM, Grok] TestFlight first ship + export compliance`
- `[ST, Monet] Pinecone WU breaker and embed staging`
- `[CT, Claude] stuck-filing recovery (deterministic)`
- `[ST, CT, Grok] R2 free-tier labels and peer checks`  ← multi-app
- `[AFC, Grok] Apple Notes title/timestamp standard`

Rules:
- **App acronyms FIRST, then agent name**, comma-separated inside `[]`, then a space,
  then the short topic. **No** bare `App — topics` titles; **no** "session" in the title.
- **Multiple apps** when more than one is impacted: list each acronym
  (`[ST, CT, UM, Grok] …`). Order = impact order (primary first).
- **Agent display name** (Title Case, not the ALL-CAPS chat tag):  
  `Grok` | `Grok Build` | `Monet` | `Claude` | `Codex` | `Cursor` | `AG` | `Kimi` | `Copilot` | `MiniMax` | `DeepSeek Harness` | …
- **Never put the date in the title** — date lives on the **second row** (body).
- **Never repeat the title as an H1 inside the body** — Notes already shows the title.

**App acronym table (generalized — extend when new apps join the fleet):**

| Acronym | App / Scope | Repo / Details |
|---------|-------------|----------------|
| `ST` | Socratic.Trade | `Simple-With-Us/Socratic-Trade` |
| `CT` | Congress.Trade | `Simple-With-Us/Congress.Trade` |
| `UM` | Usage-Monitor | `Simple-With-Us/Usage-Monitor` |
| `DD` | DealDex | `Simple-With-Us/DealDex` |
| `CL` | ContactLogo | `Simple-With-Us/ContactLogo` |
| `BF` | BotFleet | `Simple-With-Us/BotFleet` |
| `AR` | Autorotate (formerly TopSpin) | `Simple-With-Us/Autorotate` |
| `AFC` | AI-Fleet-Coordinator (this repo / Mac collab / skill pack talking as the coordinator).  Former aliases `AFL` / `FLEET` / `AIFC` / `FC` are retired — `FLEET` especially, because `[SEAT->FLEET]` is a broadcast wake that costs every seat time. | `Simple-With-Us/AI-Fleet-Coordinator` |
| `OPS` | fleet-ops (sibling identity; do not invent a checkout here) | `Simple-With-Us/fleet-ops` |
| `PS` | Personal-Site | `Simple-With-Us/Personal-Site` |
| `CTS` | congress-trading-shared | `Simple-With-Us/congress-trading-shared` |
| `FLEET` | Zulip wake (`@*fleet*` in #agent-sync, topic `fleet`): every listening seat on every platform must spend time (Grok Bot included, largely superseded by BotFleet — owner 2026-09-13).  Not the coordinator.  Not OPS. | A recipient only.  Never a SENDER tag. |

**Second row of the note (first body line) — ALWAYS the local create/update stamp + optional PR numbers:**

```
Sun, Aug 9, 3:52pm · PR #18
```

- Format: `Day, Mon D, h:mmam|pm · PR #<num>` — **no leading zero** on day or hour; **lowercase** `am`/`pm`; local Mac timezone. Append related PR numbers on the same line separated by a divider (` · PR #18` or ` · PR #18, PR #19`).
- This is the **created or last-updated** time. On every `--update`, **refresh this line** to now (do not leave a stale create-only stamp when the note changed). Pass `--pr "18"` to `apple-notes-coding.sh` to auto-inject the PR numbers on the timestamp line.
- After the timestamp line: blank line, then optional type line (`Completion` / `Plan` / `Review` / `Design` / `Handoff` / `Rollout` / `Incident` / `Fleet change` / `Work log`), then content.
- Helper auto-injects/refreshes the timestamp line and preserves PR numbers.

**Body format (owner 2026-08-08, still binding; spacing & aesthetics strengthened 2026-08-22):**
- Prefer **HTML** via `--html` (`<h2>` sections with descriptive emojis — never `<h1>`; `<ul>/<li>`; `<b>` for key terms; explicit `line-height: 1.5` on paragraphs/bullets; `<p style="line-height: 2;">&nbsp;</p>` spacers between sections).  Owner reads on iPhone — adjacent blocks collapse without explicit line-height and spacers.
- **Line Spacing / Leading Standards:**
  - **1.5x line spacing** (`line-height: 1.5;`) within paragraphs and list items for scannability and reading comfort.
  - **2.0x line spacing / section break** (`line-height: 2;` or `<p style="line-height: 2;">&nbsp;</p>`) between major sections.
- **Aesthetics & Readability Standards:**
  - **Tables & Matrices:** Use clean HTML tables (`<table border="1" cellpadding="6" cellspacing="0" style="border-collapse: collapse; width: 100%;">`) with subtle header backgrounds (`#f0f0f0`) for multi-column metrics, verification results, environment parameters, and platform parity matrices.
  - **Visual Hierarchy:** Use clear section icon emojis (`🌐 Web`, `🍎 iOS`, `🤖 Android`, `✅ Verification`, `📋 Summary`, `⚠️ Needs Owner`, `🚀 Deploy`) on `<h2>` headings.
  - **Diagrams / ASCII Flows:** When explaining architectural data flows, multi-agent state machines, or CI/CD pipelines, include structured ASCII or preformatted flow blocks (`<pre>...</pre>`).
- Plain markdown path: blank line between sections **and** bullets.  The helper turns those blanks (and consecutive list items) into `<div><br></div>`.  Do not pass a packed markdown blob.
- **Order:** lead with `Needs owner` / actions when applicable, then Problem/Context → What was done → Decisions → Next steps.
- One note per deliverable; **update in place** (`--update`) rather than near-duplicates.

