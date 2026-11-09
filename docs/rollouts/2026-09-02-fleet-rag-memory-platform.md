# Fleet RAG as a memory platform every agent commits lessons to — 2026-09-02

**Seat:** CLAUDE.  **Board:** `5c27dffd` (this lane), `0f9a13b1` (doc diet, completed by PR #175).
**Worktree:** `~/apps/fleet-claude-rag` @ `claude/fleet-rag-platform`.  **Owner direction
(2026-09-02):** "Grok and others have done lots of work on this already but I'm sure it needs much
more work in order to become a RAG platform integrated for all agents across coding platforms as a
source of memory that they commit key lessons to."

Starting point (verified 18:10Z): 38,716 points, MCP on every Mac CLI, seat-mcp REST + MCP hop,
Oracle routines, snapshots and health rows, adoption stanzas in every product `AGENTS.md`,
31 contributions from 8 seats, golden eval Recall@5 0.77 on 30 questions, cloud seats dependent
on the Mac being awake.

## What shipped

| Piece | Where | Status |
|---|---|---|
| Result grouping (one hit per document, adaptive window), lesson-boost prefetch, cross-encoder rerank hook | `scripts/fleet_rag/core.py`, `recall_api.py` | landed |
| Reranker service `tei-reranker` (MiniLM cross-encoder, 0.5 s / 30 candidates) | Coolify `xltcpxtquhyrjbc5em6dio8j`, mesh `100.69.77.26:8082`; keys in Infisical shared/prod | live |
| Mirror dedupe at the source (live copy over repo mirror, skill copies, effort-log mirrors, case-variant checkouts) + `--prune` | `scripts/fleet_rag/sources.py`, `ingest.py` | landed; one-off prune run removed ~1,850 mirrored chunks |
| Golden set 30 → 75 questions, per-source eval, `--compare` | `scripts/fleet_rag/golden.jsonl`, `eval.py` | landed |
| Contribute near-duplicate guard (cosine ≥ 0.92, `--force`) | `scripts/fleet_rag/contribute_guard.py`, `recall`, `fleet-recall-mcp.py` | landed |
| `recall digest`, `recall doctor --platforms [--box]` | `scripts/fleet_rag/doctor.py`, `recall` | landed |
| Claude Code SessionStart + Stop hooks (once-per-session nudge to commit a lesson) | `scripts/hooks/`, `install-fleet-rag.sh --hooks` | landed, installed on the Mac |
| Hetzner-side recall service (REST + MCP, bearer, gitleaks-gated contribute) | `scripts/fleet-recall-service/` → Coolify `recall-api` at `https://recall.jays.services` | landed; deploy recorded in the effort log |
| Cloudflare: `recall.jays.services` under the agents.jays.services Access app (same policies and service token), `/health` bypass twin, proxied DNS | Access apps `1e1a5fc4`, `1ca3b21f`; zone `jays.services` | live |
| Oracle routines updated (nightly `--prune`; weekly `doctor --platforms --box` + `digest` → owner note) | `~/.botfleet/routines.json` | live |
| Doc diet slice (four protocols → `docs/protocols/`) | PR #175 | merged |

## Measured

| Metric | Before | After |
|---|---|---|
| Golden Recall@1 / @5 / MRR (75 questions) | 0.71 / 0.84 / 0.77 (fused only) | **0.76 / 0.92 / 0.83** (lessons + rerank) |
| Query latency | ~1.2 s | ~3 s with rerank (`--no-rerank` keeps 1.2 s) |
| Corpus | 38,719 points incl. mirrored docs | 36,877 after prune |
| Contributions | 31 from 8 seats | 35 and climbing; Housekeeper near-duplicates now refused |

## Design decisions

- **Grouping runs in-process, not in Qdrant.**  Qdrant 1.19's groups query fuses per group, so
  every group scores 1.0 and the order is lost (Recall@1 0.39 measured).  Fusing flat and
  grouping client-side keeps the global order.
- **Lessons get a prefetch with a score floor.**  Without the 0.6 threshold RRF hands the best
  lesson a top-5 slot on every query, relevant or not.
- **MiniLM over bge-reranker-v2-m3.**  The multilingual reranker has no ONNX export; on CPU it
  took 54 s for 20 pairs.  The English MiniLM cross-encoder answers in 0.5 s and separates
  unrelated candidates cleanly (score 0.0).
- **The nudge is a Stop hook, once per session, with an opt-out.**  It fires only after 25 tool
  uses with no contribution, honours `stop_hook_active`, and writes a marker before printing, so
  it can never loop.  "no lesson" satisfies it.
- **The Hetzner service fetches `main` at start** from the public tarball, so there is no build
  pipeline and a Coolify restart is a deploy; `RECALL_REF` pins a sha when needed.

## How to verify

```bash
recall doctor --platforms --box
recall eval --k 5 --compare
recall "never idle-watch a PR" --limit 3            # mode hybrid+rerank, one hit per doc
recall contribute "…" --category lesson            # refuses a near-duplicate without --force
curl -s https://recall.jays.services/health         # ok:true, backend_ok:true, public
```

## Follow-ups

- Kimi is retired; Fx and DeepSeek headless remain read-only by design.
- Cursor, Codex, and Monet seats have not contributed yet; the hooks cover Claude Code only.
  Cursor hooks and a Codex equivalent are the next adoption lever.
- The corpus-wide dedupe of near-identical chunks (same text under different seat names in
  skill copies) is handled by the source rules; a periodic content-hash audit is cheap to add.
