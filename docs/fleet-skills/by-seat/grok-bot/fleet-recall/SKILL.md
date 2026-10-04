---
name: fleet-recall
description: >-
  Search the fleet's shared knowledge corpus (board resolutions, Apple Notes, effort logs, protocol docs, seat memories) before re-deriving a lesson, and contribute a reusable lesson after you learn one.  Use at the start of any task that smells familiar (an incident, a deploy gotcha, an owner preference), whenever a peer says "we solved this before", and at closeout.  Backed by the self-hosted fleet-agents Qdrant collection on the Hetzner box.
---

# Fleet recall

> **This install is for Grok Bot roles.** Slack tag is `[GB-<NAME>]` — `[GB-CONDUCTOR]`, `[GB-MONITOR]`, `[GB-FIXER]`, `[GB-DEPLOYER]`, `[GB-COMPILER]` (Compiler), `[GB-NURSE]`, `[GB-HOUSEKEEPER]`, `[GB-ACCOUNTANT]`, `[GB-ORACLE]`.  Notes name is the role in Title Case (`Conductor`, `Monitor`, …).  Cloud branches are often `cursor/`.  Never `[GROK-BOT]`, `[CURSOR]`, `[GROK]`, or `[MONET]`.


The fleet has one shared memory: the `fleet-agents` collection in the self-hosted Qdrant on the
Hetzner box, embedded by the self-hosted bge-m3 endpoint.  It holds every board row with its
resolution, the Apple Notes archive, every effort log, the fleet protocol docs, the skills, and
each seat's memory files, refreshed nightly by the BotFleet bot Oracle.  Canonical doc:
`docs/RAG-FLEET-INFRA.md` in AI-Fleet-Coordinator.

## Pre-Flight Retrieval Triage (PFRT) — Search Before Diagnosing

**Owner strengthening 2026-10-03: Search recall at the START of the turn/task, not only when stuck.**
One targeted `recall_search` before you act costs seconds; re-deriving a lesson another seat already solved costs an entire investigation, burns dozens of turns across BotFleet and interactive chats, and risks creating duplicate lanes.

### The 4 Mandatory Search Triggers:
At the start of every turn or task kickoff, evaluate these 4 triggers.  If ANY trigger matches, running `recall_search` is **mandatory** before inspecting code or executing trial-and-error diagnostic commands:
1. **Error / Blocker Encounter**: Any 4xx/5xx HTTP error, test/build failure, timeout, crash trace, or Sentry issue.
2. **Infrastructure & Platform**: Touching Coolify, LaunchAgents, pm2, Cloudflare Access/tunnels, Tailscale, Sentry, Qdrant, TEI, Redis, or secrets/auth.
3. **Cross-Repo & Architectural Patterns**: Modifying shared protocols (AGENT-SYNC, effort logs, board sync) or touching unfamiliar sibling repositories.
4. **Pre-Owner Query**: Before asking the owner a question regarding preferred conventions, architectural decisions, or environment history.

### Permitted Bypass:
You may bypass `recall_search` ONLY for:
- Deterministic, local mechanical edits (fixing a specific typo, committing staged files, running a linter fix).
- Continuing an already-planned step of an active, verified multi-step plan.

## When to Contribute

- **After** you learn something reusable: a gotcha, a measured number, an owner preference, a
  runbook step.  Contribute it once, in one paragraph, with the app and category set.

## How

Mac seats (Claude, Codex, Cursor, Grok, Antigravity, Monet) have the `fleet-recall` MCP server
registered, so call the tools directly:

- `recall_search(query, limit, category, app, source, seat, since_days)`
- `recall_contribute(text, category, app, seat, title, url)`
- `recall_stats()`

The same tools are on the CLI (on PATH via `~/.local/bin/recall` and `~/apps/mac-collab/recall`, like `board`):

```bash
recall "pm2 orphan holds port"
recall "how do we rotate the Coolify token" --app fleet --limit 3
recall contribute "TEI warmup memory scales with --max-batch-tokens; 4096 under 10 GiB is stable." --category lesson --app fleet
recall stats
recall digest --days 7
recall doctor
recall doctor --platforms --box
```

**Duplicate guard.**  `recall contribute` and the MCP `recall_contribute` embed the candidate
first and look for an existing agent contribution scoring >= 0.92 cosine.  On a hit the CLI
prints `similar lesson already exists: <doc_id> (score 0.95) — use --force to add anyway` and
exits 1; the MCP tool returns `{"status": "duplicate", "existing": {...}}` and stores nothing.
Pass `--force` / `force: true` only when the new text really adds something (a corrected
number, a changed ruling); otherwise open the existing doc_id and leave it.

**Weekly digest.**  `recall digest [--days 7] [--app X] [--json]` lists the agent contributions
of the window grouped by app, then category, one line per lesson (date, seat, title or first
line, doc_id, url).  The Sunday Oracle routine pastes it into its health note and the owner
reads it in Apple Notes; use `--app` to scope a per-app closeout.

**Platform parity.**  `recall doctor --platforms [--box] [--json]` prints an OK/WARN/FAIL table:
`fleet-recall` registered in every platform config (Claude, Cursor, Gemini, Codex, Grok,
grok-acp), the skill copied to each skills dir, the Claude Code hooks installed, seat-mcp
listing the `/recall/*` routes, both BotFleet routines present, the last ingest younger than
30 h with `ok=true`, and the Qdrant sentinel age; `--box` adds the Hetzner health rows over
ssh.  Only file names, route names, ages, and booleans are printed.  Exit 1 on any FAIL.

**Tailscale down.**  `recall_search` / `recall_stats` / `recall_contribute` fall back to the
public twin (`recall.jays.services`) automatically when Tailscale is down on this Mac, but
`recall ingest`, `recall eval`, and the `doctor --platforms` sentinel row need the direct
Qdrant/TEI path -- there is no public route for ingest and the public twin drops the
rerank/per_doc knobs eval needs.  Those three fail fast with one actionable line instead of a
multi-minute retry storm.  Fix it with `tailscale login`, or run `recall-tunnel up` then
`eval "$(recall-tunnel env)"` to forward Qdrant/TEI over SSH to the box and try the direct path
first regardless of Tailscale's status.

**Claude Code hooks.**  `bash scripts/install-fleet-rag.sh --hooks` copies two hooks into
`~/.claude/hooks/` and appends one entry each to `hooks.SessionStart` and `hooks.Stop` in
`~/.claude/settings.json` (existing entries untouched, backup first, idempotent, `--uninstall`
removes only ours).  SessionStart adds one line of context (`fleet recall corpus N points; search
before re-deriving, contribute at closeout`) from a cached count in well under a second.  Stop
scans the transcript once: a session with 25+ tool uses that never called `recall_contribute`
(or `recall contribute` in Bash) and never replied `no lesson` is blocked once with a nudge to
contribute or say `no lesson`; a per-session marker under `~/apps/fleet-rag/state/hook-nudged/`
prevents repeats and `FLEET_RECALL_HOOKS=0` disables both hooks.

Cloud seats (Cursor cloud, Codex cloud, Claude cloud, Grok Bot personas) reach the same three
tools through the fleet MCP gateway at `https://agents.jays.services/mcp` (Cloudflare Access +
bearer, see `docs/fleet-skills/drive-grok-tui`).  Nothing else on the internet can reach the
corpus; both services bind to the Tailscale mesh only.

## Rules

- **Search first, then act.**  A hit with a board id or a note title is a lead, not a verdict:
  open the source (board show, the note, the doc) before relying on it.
- **Rank 1 is resemblance, not correctness.**  For a ruling, preference, or infra-fact question, read the top 5 and never act on rank 1 alone.  Open the source for its date — a hit's `created_at` is the file's, not the section's.  Found a hit that supersedes another?  Contribute the correction with `force: true` (CLI `--force`); a one-detail fix often reads as a near-duplicate and is refused otherwise.
- **Contribute lessons, not logs.**  40 to 4,000 characters, one idea, with `category` one of
  `lesson | preference | infrastructure | decision | runbook` and the app slug.  Do not paste
  transcripts, secrets, or anything a scrub would have to redact.  Contributions are scrubbed
  and gitleaks-gated, but the corpus is fleet-wide memory, so write it the way you would write a
  board resolution.
- **Contribute every reusable lesson.**  Owner 2026-09-02: `recall_contribute` is the highest-yield write path — the seat that just burned tokens on a trap is the only one that knows.  Search first so you corroborate rather than duplicate.  One idea, 40–4000 chars, category `lesson | preference | infrastructure | decision | runbook`.  Optional `url` is provenance, not a gate.  Board / Notes / effort logs / docs still hold facts that already have a home; they are not a reason to skip the lesson.
- **Do not bulk-ingest chat transcripts as lessons.**  Chat mining is a rare infra/policy scan (owner rulings, environment shifts).  Agents contribute lessons themselves; token-waste is often invisible in the thread.
- **Do not point Socratic.Trade's embed provider at the fleet endpoint.**  The two embedding
  spaces are not compatible (measured 2026-08-31; see the canonical doc).
- Two spaces between sentences in anything you contribute; it is owner-facing text.
