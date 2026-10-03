# Codex: how fleet RAG works, and how to prove you are using it

Owner-facing, paste-ready.  Give Codex (CLI, IDE, or cloud) the prompt in the box below, or link
it here: https://github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/CODEX-FLEET-RAG-PROMPT.md

## What Codex already has on the Mac

- MCP server `fleet-recall` in `~/.codex/config.toml` (stdio, `python3 ~/apps/fleet-rag/fleet-recall-mcp.py`).
  Tools: `recall_search`, `recall_contribute`, `recall_stats`.
- Skill `~/.codex/skills/fleet-recall/SKILL.md` (the seat copy of `docs/fleet-skills/by-seat/codex/fleet-recall/`).
- The "Fleet recall" stanza in `~/.codex/AGENTS.md` (from `docs/AGENTS-RECALL-SNIPPET.md`).
- `~/.codex/memories/*.md` is ingested nightly, so Codex's own memory is already searchable by every seat.

Codex cloud reaches the same three tools through `https://agents.jays.services/mcp` (Cloudflare Access + bearer).
`recall doctor --platforms` on the Mac confirms the registration and the skill copy (`mcp:codex`, `skill:codex`).

## The prompt

```
You are the CODEX seat.  Before anything else, read these two files and keep them in mind for
the whole session:

1. ~/.codex/skills/fleet-recall/SKILL.md  (what fleet recall is and the rules for using it)
2. ~/Code/AI-Fleet-Coordinator/docs/RAG-FLEET-INFRA.md  (how the corpus is built, searched, and ingested)

Then prove the tools work from your surface, in this order, and report each result:

a. Call recall_stats.  Report collection status and the point count.
b. Call recall_search with the query "how do I avoid leaking credentials when I look at the
   global-api-keys handoff file", limit 3.  Report the top hit's title, source, and score.
c. Call recall_search with a question about the task you are about to start.  Say whether a hit
   changed your plan.
d. If step a or b failed, run `recall doctor --platforms` in a shell and paste the FAIL rows.
   If the ingest:sentinel row says "direct path skipped", note that search still works through
   the recall.jays.services fallback; only ingest/eval need `recall-tunnel up`.
e. Post one comment on board item 02512901 in this exact shape:
   board comment 02512901 --by CODEX --env Mac --text "surface: stdio-mcp | search: ok|FAIL <detail> | contribute: ok|FAIL <detail> | stats points: <n>"

From now on in every session:
- Search recall before re-deriving a lesson, before debugging anything that smells familiar, and
  before asking the owner a question a past ruling probably answers.  A hit is a lead, not a
  verdict; open the board row, note, or doc it cites.
- At closeout, contribute every reusable lesson with recall_contribute: one idea, 40 to 4,000
  characters, category lesson | preference | infrastructure | decision | runbook, app slug set,
  two spaces between sentences.  Search first so you corroborate rather than duplicate; the
  near-duplicate guard refuses a >= 0.92 cosine match unless you pass force.
- Never paste transcripts, secrets, or chat logs into the corpus.
```

## How to tell it is really using it

- `recall digest --days 7` lists agent contributions by seat; a `CODEX` line means it contributed.
- `recall "<topic>" --seat CODEX` returns Codex's own contributions.
- The Stop hook nudge (`~/.claude/hooks/fleet-recall-stop.py`) is Claude Code only; Codex has no
  equivalent, so the AGENTS.md stanza and this prompt are what enforce the habit.
