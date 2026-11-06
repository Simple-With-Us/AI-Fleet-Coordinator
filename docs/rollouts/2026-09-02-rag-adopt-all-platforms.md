# Fleet RAG adoption across platforms — 2026-09-02

Historical identifiers have been redacted. These records do not certify current deployment state.

**Seat:** GROK.  **Board:** `03ee6d8b`.  **Branch:** `grok/rag-adopt`.

Owner: continue making the system and engineer transition/adoption on all platforms.

## What was already live

MCP `fleet-recall` on Claude, Cursor, Codex, Grok, Gemini/AG, grok-acp.  CLI `recall` on PATH.
Cloud hop `https://agents.jays.services` lists `/recall/stats|search|contribute`.  Oracle
nightly ingest + weekly eval routines exist.  First corpus ingest still running (apple-note).

## What this unit changes (habit, not more plumbing)

- `session-start` § 2b: `recall "<task>"` before re-deriving.
- `closeout`: `recall_contribute` every reusable lesson (owner 2026-09-02).
- `fleet-coordination` and `pickup-seat` the same.
- `ONBOARDING-NEW-AGENT.md` hard rule 9; `TEMPLATE-AGENTS.md` contribute-every-lesson.
- Paste snippet: `docs/AGENTS-RECALL-SNIPPET.md` for product-app `AGENTS.md` (ST/CT/UM/DD/PS/CTS/BotFleet still missing; next wave).
- `scripts/install-fleet-skills.py` ran so every seat home dir and by-seat pack got the habit.

## Not in this PR

- Bulk ingest of staged chat JSONL (forbidden).  Extra markdown after ingest lock drops.
- [retired identifier] byte diet (board `0f9a13b1` leftover).
- Product-app `AGENTS.md` PRs (snippet is ready).
- BotFleet native MCP consumer: Oracle already runs ingest/eval; bots that spawn Mac CLIs inherit stdio MCP.  Native BotFleet config.json has no mcpServers key — REST hop is the cloud/phone path.

## Verify

```
grep -n "2b. Fleet recall" ~/.grok/skills/session-start/SKILL.md
grep -n "Fleet recall (every closeout)" ~/.grok/skills/closeout/SKILL.md
curl -sS https://agents.jays.services/health   # lists /recall/*
recall stats
```
