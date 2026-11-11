# Mine chat logs + extra markdown into fleet-agents — 2026-09-01

**Seat:** GROK.  **Board:** `ef4df7cb`.  **Worktree:** `~/apps/fleet-grok-rag-mine` @ `grok/rag-mine-chats-docs`.  **PR:** AFC #170.

Owner: use a team of Grok agents to mine chat logs and extra markdown across the Mac into fleet-agents.  Parse/scrub/chunk on the Mac.  Do not start a second ingest while the first `recall ingest --all` holds `ingest.lock`.  Do not mix OpenRouter/SiliconFlow vectors into `fleet-agents`.

## What landed (code)

- New ingest source `chat-log`: Claude / Grok / Cursor / Codex / Gemini user+assistant text only.  Tool dumps, queue-operation, events.jsonl, locks, and `~/.secrets` are skipped.  Sessions over ~80k chars split as `#partN`.
- Expanded `doc` walker: fleet app `README.md` / `AGENTS.md` / `STATUS.md` / `CLAUDE.md` / `docs/**/*.md`, top-level `~/apps/*.md` except effort logs, `~/.grok/docs`, `~/.grok/skills/**/SKILL.md`.
- `chat-log` registered on `sources.SOURCES`, `recall_api.SOURCES`, `fleet-rag.py` `KNOWN_SOURCES`.
- Unit tests: `python3 -m unittest fleet_rag.tests.test_sources fleet_rag.tests.test_recall` (63 OK).  Ingest/MCP tests also OK.

Runtime `~/apps/fleet-rag` was **not** overwritten mid-run.  After #170 merges, `scripts/install-fleet-rag.sh` then a **later** ingest (lock free) picks the new sources up.

## What was mined (staging, not git)

Scrubbed JSONL under `~/apps/fleet-rag/mined/` (`chmod 600`).  Validated 75,493 JSONL lines, 0 parse errors.  Secret leftover lines dropped by the scrubber.

| Stream | Docs | Notes |
|---|---:|---|
| Claude chats | 7224 | `~/.claude/projects` jsonl |
| Grok chats | 36783 | `chat_history.jsonl` only |
| Cursor | 1164 | `agent-transcripts` |
| Codex | 555 | session jsonl |
| Gemini / Antigravity transcripts | 4446 | `transcript.jsonl`; sqlite conversations not parsed |
| Kimi | 18 | tiny |
| BotFleet | 148 | plus Claude/Grok workspace overlap |
| Extra markdown | 2158 | app docs beyond the old AFC/fleet-ops walk |
| Distilled owner-rule lessons | 4012 | standing-rule regex on user turns |

A stricter second pass (more dump/synth drops) is kept as `*.strict.jsonl` beside the high-recall files.

## Verify

```
cd ~/apps/fleet-grok-rag-mine/scripts
python3 -m unittest fleet_rag.tests.test_sources fleet_rag.tests.test_recall
recall stats   # first ingest still board-heavy until lock releases
```

Do **not** `recall ingest --all` while pid 81666 holds the lock.

## Follow-ups

1. When the first ingest exits: `install-fleet-rag.sh`, then ingest `chat-log` + remaining `doc` / effort-log / skill / memory / apple-note.
2. Optional: `recall ingest ~/apps/fleet-rag/mined/docs/extra-markdown.jsonl` and the chat JSONL if the live walker is too slow on 4G of sessions.
3. Gaps: Kimi chats, BotFleet `events.ndjson`, Gemini `conversations/*.db`, Codex sqlite thread_history.
4. TEI is still 6 CPU / CPU-bound at ~1 chunk/s on long board text.  Raising TEI CPU needs owner say-so (recreates the container).
