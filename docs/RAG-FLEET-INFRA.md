# Fleet RAG infrastructure — self-hosted embeddings + the shared agent memory

Owner-directed 2026-08-31 (stand-up, board `7dbd6228`) and 2026-09-01 (make it fully operational
and reachable from every platform, boards `17f4547c` `9c75471c` `c799b564` `0f9a13b1`), CLAUDE
seat.  This is the knowledge layer described as "Plan B" in
`docs/reviews/2026-08-27-fleet-ops-review.md`: one place every agent, on every platform, can look
up lessons learned, owner preferences, how the infrastructure is wired, and what a past board row
resolved — and contribute to — instead of bulk-loading every protocol document into every session.

Both services run on the shared Hetzner box (`host.jays.services`, 16 vCPU / 30 GiB) as Coolify
services, bound to the Tailscale mesh only.  Neither is reachable from the public interface.

## What exists

| Service | Coolify UUID | Endpoint (mesh only) | Caps | Auth |
|---|---|---|---|---|
| `qdrant-st` | `ookh0qmlgrbxlwbbe6lolx6g` | `http://100.69.77.26:6333` | 4 CPU / 12 GiB | `api-key` header (write key, or the read-only key for reads) |
| `tei-bge-m3` | `cday9viyj6mwlfr8egnoknoa` | `http://100.69.77.26:8081` | 6 CPU / 10 GiB | `Authorization: Bearer` |
| `tei-reranker` | `xltcpxtquhyrjbc5em6dio8j` | `http://100.69.77.26:8082` | 4 CPU / 8 GiB | `Authorization: Bearer` (`TEI_RERANK_API_KEY`) |
| `recall-api` | see the rollout doc | `https://recall.jays.services` (public, Cloudflare Access + bearer `RECALL_API_TOKEN`) | 1 CPU / 1 GiB | bearer on every route but `/health` |

`tei-bge-m3` is Hugging Face `text-embeddings-inference:cpu-1.8` serving `BAAI/bge-m3`
(1024-dim, CLS pooling, L2-normalized) on CPU via the ONNX backend.  `tei-reranker` is the
same image serving the cross-encoder `cross-encoder/ms-marco-MiniLM-L-6-v2` (`/rerank`, about
0.5 s for 30 candidates; `BAAI/bge-reranker-v2-m3` has no ONNX export and took 54 s for 20 pairs
on CPU, so it was swapped out on 2026-09-02).  `recall-api` is the Mac-independent recall
service described under *From any other device*.  Despite the name, `qdrant-st` hosts two
collections and is no longer ST-only.

### Collections

| Collection | Points | Purpose |
|---|---|---|
| `socratic-trade` | ~801k | ST RAG, copied from Pinecone.  Embedded by **OpenRouter** `baai/bge-m3`.  **Serving ST production reads since 2026-08-31** (board `9e19673a`, stage 1 read-path cutover). |
| `fleet-agents` | grows nightly | The shared agent memory.  Embedded by the **self-hosted** endpoint. |

`fleet-agents` is 1024-dim cosine, vectors in RAM, with payload indexes on `source`, `app`,
`category`, `seat`, `doc_id`, `content_hash`, `created_at`, and a full-text index on `text` so
keyword and vector search are fused (see *Search*).

### Payload schema (v2)

Every point carries:

```jsonc
{
  "text":         "[Title › Section]\nthe chunk",
  "source":       "board | effort-log | apple-note | doc | skill | memory | chat-log | agent-contribution | meta",
  "app":          "fleet | socratic-trade | congress-trade | usage-monitor | dealdex | botfleet | ...",
  "category":     "lesson | preference | infrastructure | decision | runbook | finding | note | doc",
  "seat":         "CLAUDE | MONET | GROK | CODEX | AG | CURSOR | OWNER | FLEET",
  "doc_id":       "board/<uuid> | note/<id> | doc/<repo>/<path> | effort-log/<APP> | memory/<seat>/<file> | skill/<name> | chat/<platform>/<id>[#partN] | contrib/<SEAT>/<date>/<hash8>",
  "chunk_index":  0, "chunk_count": 3,
  "heading":      "Title › Section", "title": "...", "url": "https://...", "path": "/Users/jay/...",
  "created_at":   1788213600000, "updated_at": 1788213600000,
  "content_hash": "sha256 of text", "embed_model": "BAAI/bge-m3-selfhosted",
  "ingest_run":   "20260901T203000Z", "scrubbed": ["github-token"]
}
```

Point ids are `uuid5` over the content hash, so identical text anywhere collapses to one point
and re-ingesting unchanged content is a no-op.  One extra point, `doc_id = meta/ingest-status`
(`source = meta`), is the ingest sentinel described under *Health rows*; search excludes it.

## Sources (what gets ingested)

| Source | Where it comes from | Notes |
|---|---|---|
| `board` | `~/apps/mac-collab/findings.db` (title, body, resolution, status, severity, comments) | category `lesson` when resolved, else `finding`; url is the board |
| `apple-note` | every note in the iCloud folder **Coding**, exported by AppleScript without stealing focus, cached by modification date | app and seat parsed from the `[APP, Agent]` title convention |
| `doc` | markdown in fleet app repos under `~/Code` (`README.md`, `AGENTS.md`, `STATUS.md`, `CLAUDE.md`, `docs/**/*.md`), plus the broader walk of AI-Fleet-Coordinator and fleet-ops, top-level `~/apps/*.md` except effort logs, `~/.claude/CLAUDE.md`, `~/.grok/docs/**/*.md`, and `~/.grok/skills/**/SKILL.md` | GitHub blob URL when the file is in a repo.  Skip `node_modules`, `.git`, backups, dist, build, vendor, and `reviews/raw`.  Deduped by resolved path. |
| `effort-log` | `~/apps/*-EFFORT-LOG.md` and the protocol | one doc per file, app from the filename |
| `skill` | `~/.claude/skills/*/SKILL.md`, `~/.cursor/skills/*/SKILL.md` | deduped by content |
| `memory` | `~/.claude/projects/*/memory/*.md`, `~/.codex/memories/*.md` | the per-seat silos, so their lessons are shared |
| `chat-log` | infrequent policy scan of agent transcripts (not in `ingest --all`) | **Owner 2026-09-02:** only user turns that look like an owner ruling or infra/policy shift.  Not a lesson dump.  Agents contribute lessons via `recall_contribute`; chat review is sparingly for policy/infra drift.  Explicit `recall ingest --source chat-log`. |
| `agent-contribution` | `recall contribute` / `recall_contribute` | **Highest-yield write path.**  Scrubbed, gitleaks-gated, 40–4,000 chars.  Search first, then contribute the lesson at the moment it was learned. |

Mirror rules keep one copy of each document: the live `~/apps/*.md` copy wins over its
repo mirror (AGENT-SYNC, MAC-LOCAL-PROCESSES, FLEET-UI-COPY, TEMPLATE-AGENTS), the by-seat and
repo copies of skills are skipped in favour of the `skill` source, effort-log mirrors are skipped
in favour of the live boards, and checkouts reachable under two spellings are walked once.
`recall ingest --prune` deletes the chunks of documents a source no longer yields.

Every chunk goes through the secret scrub (`scripts/fleet_rag/scrub.py`: well-known token shapes,
assignment patterns, basic-auth URLs) and then a gitleaks gate over the staged rows; anything
gitleaks still flags is dropped and counted in the run report.  Documents are chunked with a
markdown-aware chunker (~1,600 chars, 200 overlap, heading trail prepended) so a fragment still
carries its section context.

## Search

Hybrid with grouping, a lesson boost, and a reranker (all defaults; every one can be turned off):

1. The query is embedded and Qdrant's Query API fuses three prefetches with reciprocal rank
   fusion — plain dense, dense restricted to points whose full-text index matches any of the
   query's keyword terms, and dense restricted to agent contributions in the lesson categories
   (`prefer_lessons`, score threshold 0.6 so an unrelated lesson never squats in the top-5).
2. Candidates are grouped in-process by `doc_id` so one document yields one hit (`per_doc` 1..3
   widens it); the window grows adaptively when one document dominates.  Each hit carries
   `group_hits`, how many chunks of that document matched.
3. When `TEI_RERANK_URL` / `TEI_RERANK_API_KEY` are configured, the top `max(4*limit, 20)`
   documents are reranked by the cross-encoder (`mode` becomes `hybrid+rerank`, hits carry
   `rerank_score` and `fused_score`); any error or an 8 s budget overrun falls back silently to
   the fused order.  Reranking costs about two seconds per query.

Filters on `category` / `app` / `source` / `seat` and a `since_days` window are exact-match
payload filters; the ingest sentinel (`source=meta`) is always excluded.  A 75-question golden
set (`scripts/fleet_rag/golden.jsonl`: board rows, Apple Notes, contributions, owner rulings,
docs) and `recall eval --k 5 [--compare]` report Recall@1 / Recall@5 / MRR per source.
Measured 2026-09-02 on 36.9k points: fused only 0.71 / 0.84 / 0.77; with lessons + rerank
**0.76 / 0.92 / 0.83**.

### Chunk-level rerank bake-off (2026-09-17) — no ranking change shipped

Diagnosis of the 5 golden queries every reranker configuration still missed: 4 of them fail
because the right document ranks well (fused/doc order #1, #1, #4, #7) but `per_doc=1` keeps a
different, wrong chunk of that document — the chunk is chosen by fused score *before* the
cross-encoder ever sees it, so the right text never gets a chance to win the rerank.  Measured
in-process inside the `recall-api` container against the real production Qdrant/TEI backend
(unmodified `RERANK_TIMEOUT=8s` budget, no fake backend), one variant at a time, golden.jsonl
(75 rows):

| Variant | R@1 | R@5 | MRR | search p50/p95 | rerank p50/p95 | avg pairs | fallbacks | avg hit chars |
|---|---|---|---|---|---|---|---|---|
| **A** — prod (`per_doc=1`) | 0.707 | 0.920 | 0.796 | 1275/1453 ms | 1135/1308 ms | 20.0 | 0 | 4615 |
| B — `per_doc=2` | 0.693 | 0.933 | 0.782 | 1826/2250 ms | 1700/2094 ms | 30.5 | 0 | 4585 |
| D1 — chunk-rerank N=20, G=2 | 0.707 | 0.933 | 0.792 | 1836/2197 ms | 1721/2044 ms | 30.5 | 0 | 4615 |
| D2 — chunk-rerank N=15, G=3 | 0.707 | 0.933 | 0.786 | 1613/2085 ms | 1476/1928 ms | 26.0 | 0 | 4460 |
| D3 — chunk-rerank N=20, G=3 | 0.707 | 0.933 | 0.791 | 2109/2697 ms | 1958/2534 ms | 35.5 | 0 | 4594 |

D-variants: for each of the top-N fused doc groups, rerank up to G of that doc's already-
fetched `GROUP_DEPTH` best-fused chunks in one cross-encoder call, score each doc by its best
reranked chunk, and return the best chunk of the top-`limit` docs by that score.  N=20 is
production's own `candidate_count(5)`.

**Decision (pre-registered rule, applied as measured):** the best-MRR D variant with 0
fallbacks and search p95 ≤ 3.0 s is D1 (MRR 0.792) — but that is *below* A's own MRR (0.796)
and only +0.013 R@5, short of the ≥0.03 bar on either metric, so no D variant ships.  B also
falls short on R@5 (+0.013 < 0.03).  All three D variants and B recovered exactly **one** of
the four diagnosed "right doc, wrong chunk" queries (the Coolify-deploy-token one); the other
three (fleet vector database port, model tier for mechanical edits, Completed vs. Deployed)
stayed misses in every variant — the correct chunk for those three is not among that
document's own top-2/top-3 fused-scored chunks even before rerank sees it, so a bigger G would
be needed, trading more rerank latency (and pairs sent) for an unproven win.  **Net: no
ranking-pipeline change shipped this round.**  The one change that did ship is that
`recall.jays.services` (the public fallback / cloud path) now honors `per_doc`, `rerank`, and
`prefer_lessons` the same as the direct path — those three were previously silently dropped
whenever a caller fell back to the public route (`scripts/fleet_rag/public_fallback.py`
`PUBLIC_ALLOWED_ARGS`, `scripts/fleet-recall-service/server.py` `recall_search` tool schema).

## Using it

**Write path (owner 2026-09-02).**  `recall_contribute` is the highest-yield source — the seat
that just burned tokens on a trap is the only one that knows, and that is often invisible in
the chat log.  Search first, then contribute one paragraph.  Chat transcripts are *not* bulk
ingested as lessons; `ingest --all` skips `chat-log`.  An explicit `--source chat-log` pass is
a rare infra/policy scan of owner rulings only.

### From a Mac seat (Claude, Codex, Cursor, Grok, Antigravity, Monet)

The `fleet-recall` MCP server is registered in every CLI's global MCP config by
`scripts/install-fleet-rag.sh` (stdio, `python3 ~/apps/fleet-rag/fleet-recall-mcp.py`, no
tokens in any config file).  OpenCode, Kimi Code, Copilot CLI, Muse Code, MiniMax and fx have their own formats, so the same script
calls `python3 -m fleet_lanes.install_mcp` for them (each tool's native shape, a tool that is not installed is skipped, a
hand-registered entry is left alone).  Clutch is not in the list: it carries `fleet-recall` as a native agent preset
(`~/.clutch/dsh/.agent-presets/fleet-recall`), and a second copy would double the tools.  Tools:

- `recall_search(query, limit=5, category?, app?, source?, seat?, since_days?)`
- `recall_contribute(text, category, app="fleet", seat, title?, url?)`
- `recall_stats()`

The CLI is `recall`, on PATH through `~/.local/bin/recall` (and `~/apps/mac-collab/recall`) like `board`:

```bash
recall "pm2 orphan holds port"                       # ranked hits with source, date, board id / url
recall "rotate the Coolify token" --app fleet --limit 3 --since-days 60
recall contribute "TEI warmup memory scales with --max-batch-tokens; 4096 under 10 GiB is stable." --category lesson --app fleet
recall stats            # points, by source / app, embedder health
recall doctor           # credentials found (names only), key mode, service health
recall eval --k 5 [--compare]   # golden-set retrieval quality, with/without lessons and rerank
recall digest --days 7          # this week's contributions grouped by app (the weekly owner note)
recall doctor --platforms --box # parity table: every CLI config, skills, hooks, seat-mcp, routines, box rows
recall ingest --all --prune     # what the nightly routine runs (idempotent, resumable)
```

`recall contribute` refuses a near-duplicate (cosine >= 0.92 against existing contributions)
unless `--force`; the MCP tool returns `status: duplicate` with the existing lesson instead.
`--per-doc`, `--no-rerank`, and `--no-lessons` expose the search knobs; the MCP tool takes
`per_doc`, `rerank`, and `prefer_lessons`.

**Claude Code hooks** (installed by `install-fleet-rag.sh --hooks`, tracked under
`scripts/hooks/`): a SessionStart hook adds one line of context ("corpus N points; search before
re-deriving, contribute at closeout"), and a Stop hook nudges once per substantial session
(25+ tool uses, no contribution yet) to call `recall_contribute` or reply "no lesson".
`FLEET_RECALL_HOOKS=0` disables both; the Stop hook honours `stop_hook_active` so it can never
loop.

**Automatic fallback when Tailscale is down** (`scripts/fleet_rag/public_fallback.py`, added
2026-09-08).  The `recall` CLI and the stdio `fleet-recall-mcp.py` both call `recall_api`
directly against `TEI_URL` / `QDRANT_URL` -- Tailscale mesh addresses -- so a Mac that is
logged out of Tailscale used to fail every `recall_search` / `recall_stats` /
`recall_contribute` with a slow, opaque `RemoteDisconnected` / `ConnectionError` traceback
(`core.http_json` retries a connection failure 4x with backoff before giving up).  Both
surfaces now route every call through `public_fallback.call_with_fallback`, which:

1. Checks `tailscale status` up front (~0.1-3s); a positive "Tailscale is stopped." or "Logged
   out" skips the slow local retry storm entirely and goes straight to the public path.  A
   machine with no `Tailscale.app` (or any other inconclusive result) is treated as "assume
   up", never as "down" -- **except** on macOS, where that "unknown" result gets one more,
   independent check: `private_route_bypasses_tailscale()` asks the OS routing table
   (`/sbin/route -n get <qdrant-host>`, ~2s timeout) whether the configured `QDRANT_URL` host
   (or the documented default, `100.69.77.26`) is even reachable through a Tailscale (`utun`)
   interface.  It only ever adds a "blocked" verdict on top of "assume up" -- a route failure,
   a non-CGNAT host (an operator's `Run your own` deployment), or a non-Darwin platform all
   still fall through to "assume up" unchanged.  `direct_path_blocked(status_text)` is the one
   function both `call_with_fallback` and `recall doctor --platforms`'s `blocked` computation
   call, so the two can never drift apart on what "blocked" means.  This exists because Tailscale.app
   can be missing entirely (removed, or a relocated binary) while this Mac's network runs a
   transparent TCP proxy: a direct connect to a dead 100.64.0.0/10 address "succeeds" in
   ~0.2s and then hangs on the read until `core.DEFAULT_TIMEOUT` (120s) -- see
   *Troubleshooting* below.
2. Otherwise still tries the local path first (Tailscale can flap), catches a
   connection-level failure specifically (never an HTTP 4xx/5xx, which is a real answer), and
   retries once against `https://recall.jays.services`'s REST twin (`GET /recall/stats`,
   `POST /recall/search`, `POST /recall/contribute` -- see *From any other device* below).
3. The public retry needs `RECALL_API_TOKEN` and `CF_ACCESS_CLIENT_ID` /
   `CF_ACCESS_CLIENT_SECRET` (the Cloudflare Access service token).  A value already in the
   environment wins; otherwise it is read straight from the handoff file that already holds it
   -- `RECALL_API_TOKEN` from `~/.secrets/global-api-keys` (same file as `TEI_URL` /
   `QDRANT_URL`), the Access pair from
   `~/.secrets/agents-jays-services-access-service-token.env` -- so the fallback works from the
   stock `python3 fleet-recall-mcp.py` MCP registration with no shell wrapper and no MCP config
   changes on any platform.  See *Credentials* and `docs/RECALL-ACCESS-CHECK.md`.  The request
   also carries a real `User-Agent` (`fleet-recall-fallback/<version>`): `core.http_json` sends
   none, and `urllib`'s default (`Python-urllib/3.x`) trips Cloudflare's bot management in
   front of `recall.jays.services` (403, `error code: 1010` -- see *Reading the result* in
   `docs/RECALL-ACCESS-CHECK.md`).  A missing credential name, or a failure on the public path
   too (a rejected bearer, Access rejecting the service token, the box itself down), produces
   one plain-English, actionable line instead of a second opaque exception.

The public tool contract is a subset of the local one (no `per_doc` / `rerank` /
`prefer_lessons` / `force` -- see `scripts/fleet-recall-service/server.py` TOOLS), so those
knobs are silently dropped on a call that actually falls back, and the local near-duplicate
contribute guard (`contribute_guard.near_duplicate`, a Qdrant call of its own, not part of the
shared tool contract) is skipped on a believed-down Tailscale rather than run against an
unreachable backend.  `recall doctor`, with or without `--platforms`, is unaffected -- it still
reports `UNREACHABLE` / `ERROR` / `FAIL` truthfully; a diagnostic tool should never paper over
what it is diagnosing.  The Hetzner-side `fleet-recall-service` imports `recall_api` directly
and never goes through this module: it already sits next to Qdrant/TEI with no Tailscale hop
to lose.
Tests: `fleet_rag/tests/test_public_fallback.py` (the fallback decision, the routing-table
check with an injected `run` -- utun / en0 / route-failure / non-Darwin / non-CGNAT-host, all
with the local connection error, the Tailscale-down check, and the public call mocked -- no
network, no real subprocess).

**Troubleshooting: the transparent proxy makes SSH/direct-connect failures look like
successes.**  `nc -z -G 6 -w 6 167.233.254.55 47123` (a closed port on the Hetzner box)
"succeeding" in well under a second -- instead of refusing the connection or timing out --
means this network's client-side transparent TCP proxy is intercepting the connect, not that
the port is actually open.  Seeing that: stop retrying SSH by hand (it will keep "connecting"
and then hang or drop during the protocol handshake) and use the public path
(`recall.jays.services`) or `recall-tunnel up`'s own bounded retry loop instead of a manual
loop -- see *Operational notes* below for its retry settings.

`scripts/fleet-rag.py` remains as the thin compatibility CLI (`stats` / `search` / `ingest`)
and does not go through the fallback.

### From BotFleet bots

BotFleet mounts its own first-party recall MCP proxy (`server/drivers/qdrant-proxy.ts`, over
`server/recall-transport.ts`), exposing `recall_search`, `recall_contribute`, and `recall_stats`
on the Claude, Codex, and Antigravity drivers, and on all nine ACP engines (Grok ACP, Cursor,
DeepSeek Harness, DeepSeek, Kimi, Qwen, Droid, Hermes, OpenCode).  The Claude driver runs with
`--strict-mcp-config`, so a bot never sees the user's global MCP servers.  The Codex driver
layers `-c mcp_servers.<name>` overrides onto `~/.codex/config.toml` with no `CODEX_HOME`
isolation, so a Codex bot sees both the user's global fleet-recall MCP and BotFleet's proxy.
`mcp_servers.github` / `mcp_servers.render` in that file must call `~/apps/mcp-servers/github-mcp-launch.sh`
and `render-mcp-launch.sh` (no inline `mcp-remote --header`; JSON-style `\\"` is a TOML parse
error and Codex refuses the whole file).
There is no Grok driver that reads a global MCP config -- `server/drivers/grok.ts` is the xAI
HTTP driver with no MCP client; `server/drivers/acp/grok.ts` is the one BotFleet mounts.  The
HTTP engines (MiniMax, OpenAI-compatible, Grok HTTP), the pi engine (until BotFleet PR
`claude/rag-basics` lands), and the Computer engine cannot reach the corpus.  The transport is
chosen by `qdrant.url`: set, a bot uses the cloud service at `https://recall.jays.services` with
a Cloudflare Access service token pair; empty, it uses the local `recall` CLI on the Mac.
Oracle owns the ongoing work (see *Routines*).

Verified Wed, Sep 16, 2026 against BotFleet e8fb9a97.

iOS / a phone / a bot that is not on this Mac should use the public hop below, not stdio
Python.  Do not paste Infisical keys into a BotFleet room.

### From any other device (cloud seats, phone, Claude Code Cloud, Cursor cloud)

Two public hops, both Cloudflare Access + bearer, both serving the same three tools and the
same REST twins.  Health is public; everything else needs `Authorization: Bearer` plus the
Access service-token headers when you are not already on the Mac.

| Hop | Runs on | Bearer | Use it when |
|---|---|---|---|
| **`https://recall.jays.services`** — `scripts/fleet-recall-service` | Hetzner box (Coolify), next to Qdrant + TEI | `RECALL_API_TOKEN` (Infisical shared/prod) | default for recall; **works while the Mac is asleep** |
| `https://agents.jays.services` — `seat-mcp` | the Mac, over the named tunnel | `SEAT_MCP_TOKEN` | you also need the seat / Grok TUI tools; falls over when the Mac sleeps |

| Surface | How |
|---|---|
| Mac CLI (Claude, Codex, Cursor, Grok, AG, Monet) | stdio MCP `fleet-recall` (installer) or `recall` CLI |
| BotFleet on this Mac | inherits that stdio MCP from the engine config |
| Cursor cloud / Claude Code Cloud / Codex cloud / Grok Bot | remote MCP `https://recall.jays.services/mcp` (entry `fleet-recall` in `scripts/seat-mcp/mcp.example.json`); `https://agents.jays.services/mcp` when you need seat tools too |
| Anything that can HTTP | REST: `GET https://recall.jays.services/recall/stats`, `POST .../recall/search`, `POST .../recall/contribute` (same paths on agents.jays.services) |
| iOS BotFleet | same REST or remote MCP; the companion talks to the Mac harness when the Mac is the computer, otherwise `recall.jays.services` |

MCP tools on `/mcp`: `recall_search`, `recall_stats`, `recall_contribute` (`seat` is required
for contributions on both hops; a remote caller has no `AGENT_SEAT`).  REST bodies are the same
JSON as the MCP arguments.  The Hetzner service is a stdlib-only Python process
(`scripts/fleet-recall-service/server.py`) that imports `fleet_rag.recall_api` unchanged and
reads its configuration from the environment only (`QDRANT_*`, `TEI_*`, `RECALL_API_TOKEN`);
it reaches Qdrant and TEI over the host's Tailscale address (`100.69.77.26`, verified reachable
from a container on the `coolify` network 2026-09-02).  Deploy steps, the Coolify / DNS /
Access checklist, and client snippets for Claude Code Cloud, Cursor, and Codex are in
`scripts/fleet-recall-service/README.md`.

```bash
# REST, from any network that can reach recall.jays.services
curl -sS https://recall.jays.services/health          # public: {ok, version, collection, points, backend_ok}

curl -sS https://recall.jays.services/recall/stats \
  -H "Authorization: Bearer $RECALL_API_TOKEN" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"

curl -sS https://recall.jays.services/recall/search \
  -H "Authorization: Bearer $RECALL_API_TOKEN" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"query":"handoff file grep trap","limit":3}'
```

## Credentials

Canonical in **Infisical shared / prod** (project `18f563a3-9c88-454c-96eb-28fc9678f3ba`):
`TEI_URL`, `TEI_API_KEY`, `TEI_EMBED_MODEL`, `QDRANT_URL`, `QDRANT_API_KEY`,
`QDRANT_READONLY_API_KEY`, `QDRANT_FLEET_COLLECTION`.  Coolify holds synced copies; Infisical is
the source of truth per the standing rule.

- **Read/write split.**  `QDRANT_READONLY_API_KEY` is loaded by `qdrant-st` as
  `QDRANT__SERVICE__READ_ONLY_API_KEY` and is what search / stats / eval use.  Only ingest and
  `contribute` use the write key.  Until the service has restarted with the new key, the client
  falls back to the write key on a 401/403 so reads never break.
- Every client loads credentials from the environment first, then from Infisical via the machine
  identity in `~/.secrets/global-api-keys`.  Values are never printed.

ST keeps its own `QDRANT_URL` / `QDRANT_API_KEY` in the ST project for the app runtime.  Rotating
the Qdrant write key means updating both places.

## Run your own

None of the hosts above are required.  `recall`, `fleet_rag/` and the MCP server read every
endpoint from the environment, so anyone can point them at their own stack with five variables
and no code edit, no Infisical account and no handoff file:

    export QDRANT_URL=https://qdrant.example.com QDRANT_API_KEY=...
    export TEI_URL=https://tei.example.com TEI_API_KEY=...
    export QDRANT_FLEET_COLLECTION=my-agents

A minimal pair of containers (add your own TLS / auth in front):

    services:
      qdrant:
        image: qdrant/qdrant:latest
        environment: [ "QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY}" ]
        ports: [ "6333:6333" ]
        volumes: [ "qdrant:/qdrant/storage" ]
      tei:
        image: ghcr.io/huggingface/text-embeddings-inference:cpu-1.8
        command: [ "--model-id", "BAAI/bge-m3", "--api-key", "${TEI_API_KEY}" ]
        ports: [ "8081:80" ]
    volumes: { qdrant: {} }

Then `bash scripts/install-fleet-rag.sh` (set `FLEET_RAG_HOME` to install somewhere other than
`~/apps/fleet-rag`) and `recall doctor`.  For the HTTP + MCP surface,
`scripts/fleet-recall-service/` already ships a `Dockerfile`, `compose.example.yaml` and
`bootstrap.sh` that take the same five variables.  Running your own Infisical instead is also
env-only: `FLEET_RAG_INFISICAL_API`, `FLEET_RAG_INFISICAL_PROJECT`, `FLEET_RAG_INFISICAL_ENV`
and `FLEET_RAG_HANDOFF_FILE` override the defaults this repo ships for its owner.

## Routines (BotFleet bot Oracle, owner direction 2026-09-01)

| Routine | Schedule | What it does |
|---|---|---|
| Fleet RAG nightly ingest | daily 02:30 local | `recall ingest --all --prune`, reads `~/apps/fleet-rag/state/last-run.json`, retries once, files a P1 on the board on repeated failure |
| Fleet RAG weekly health + recall eval | Sundays 06:30 local | `recall doctor --platforms --box` / `stats` / `eval` / `digest --days 7`, checks `recall.jays.services/health` and yesterday's snapshot, writes the owner note "[FLEET, Oracle] Weekly recall digest", files a P1 on regressions |

Both routines start with a preflight (added Tue, Sep 8, 2026): `recall doctor --platforms`, and when the `ingest:sentinel` (or `tei:rerank`) row says the direct path is skipped because Tailscale is logged out, `recall-tunnel up` plus the three tunnel URLs on every recall command, then `recall-tunnel down`.  Without it the nightly failed every night from Sep 3 to Sep 8 while the corpus itself stayed green.

Routines live in `~/.botfleet/routines.json` and are managed through BotFleet's loopback API
(`POST http://127.0.0.1:8799/api/routines`).  Create payload: `name`, `prompt`, `botId`
(Oracle `79a3a7f8-e35f-4604-9e41-54e4af28c04a`), `runOn: maus`, `schedule`, `durationMinutes`.

## Backup

`/etc/cron.d/fleet-qdrant` on the box runs `/usr/local/sbin/fleet-qdrant-snapshot.sh` daily at
03:20 UTC (tracked here under `scripts/hetzner/`).  It asks Qdrant for a consistent snapshot of
each collection, moves the file off the storage volume to `/data/backups/qdrant/<collection>/`,
checksums it, copies it to Backblaze B2 (`jays-socratic-trade-eu/qdrant/socratic-trade`,
`jays-fleet-shared-eu/qdrant/fleet-agents`), keeps two locally and fourteen days remotely.  The
API key is read from the running container's environment at run time and never written to disk.
A full `socratic-trade` snapshot is ~6.4 GB and takes about a minute to create.

## Health rows

`/usr/local/sbin/fleet-health-verify.sh` (every 15 minutes, pages through the existing
rate-limited Pushover path on FAIL) now calls `fleet-qdrant-health.sh`, which fails when:

- the latest local snapshot of either collection is older than 36 hours, or
- the **ingest sentinel** (`doc_id = meta/ingest-status`, written by every ingest run with
  `updated_at` and `ok`) is older than 30 hours or its last run failed.

The sentinel is the dead-man switch for the Oracle routine: if BotFleet stops running it, the
box pages.  No credential leaves the box for this check.

## Observability (added 2026-09-10)

Prompted by a Sentry review that found **zero** instrumentation in this package (the only "rag"
hit in the org's Sentry was Socratic-Trade's unrelated SEC-disclosure ingest budget, a different
app with its own pipeline).  Two gaps stood out against everything else the fleet watches through
Sentry:

1. **The reranker's silent fallback had no signal at all.**  `recall_search`'s cross-encoder
   rerank falls back to the fused order on any failure (by design -- a flaky reranker must never
   break a query), but nothing logged, counted, or reported it.  A reranker outage would silently
   run every query in the fleet at degraded quality (Recall@5 0.84 without rerank vs. 0.92 with
   it, see *Search* above) with the only chance of detection being the next `recall eval` --
   weekly, per the Oracle routine.
2. **`recall doctor --platforms` never checked the reranker endpoint.**  It checks Qdrant (the
   ingest sentinel) but nothing calls the TEI reranker's `/health`, so `tei-reranker` could be
   down for a week with every doctor row green.

Fixes, both additive and opt-in where a live backend is involved:

- **`SENTRY_DSN`** (optional; `scripts/fleet_rag/telemetry.py`).  Unset by default -- exactly
  today's behavior for anyone who has not opted in, and `sentry_sdk` is not a hard dependency
  (imported lazily, only when `SENTRY_DSN` is set).  When set, every rerank fallback
  (`recall_api._apply_rerank`) and every ingest-side failure that was already caught and logged
  (a source's own exception, a prune failure, a sentinel write failure) also reports to Sentry,
  tagged `component=fleet-rag` plus `operation`/`source`.  Never sends query text, doc content,
  or credentials -- exception types and tags only, same rule as everywhere else in this package.
  `SENTRY_ENVIRONMENT` overrides the default `production` tag.
- **`tei:rerank` row in `recall doctor --platforms`** (`doctor.default_rerank_check`): OK when
  `/health` on `TEI_RERANK_URL` answers, FAIL when configured but unreachable, WARN when
  `TEI_RERANK_URL`/`TEI_RERANK_API_KEY` are unset.  Like `ingest:sentinel`, when the caller has
  already established the direct path is blocked (Tailscale believed down, no operator
  override -- `doctor.platforms_report`'s `direct_path_blocked`), this row is WARN "direct path
  skipped" instead of probing `TEI_RERANK_URL` directly: that address is Tailscale-mesh-only
  too, so probing it here would falsely FAIL a reranker that's actually healthy behind
  `recall.jays.services` (added 2026-09-17).
- **`rerank_healthy` in `recall stats`** (`core.rerank_healthy`, mirrors `embedder_healthy`):
  `true`/`false` when configured, `null` when not -- so an ad hoc `recall stats` shows reranker
  health the same way it already shows embedder health.

## Measured behavior

Steady state on 6 vCPU, verified 2026-08-31:

- Single query embed: **120–300 ms**.
- Batched short texts: **~24 texts/sec** (the ONNX backend caps a batch at 8 requests).
- First boot downloads ~2.3 GB of ONNX weights and takes **~9 minutes** to become healthy.
  The weights persist in the `/var/lib/tei-data` bind mount, so redeploys start in ~30 s.
- Resident usage once warm: ~5.7 GiB of its 10 GiB cap, including page cache for the model.

Bulk re-embedding is the one workload to keep off this box: at ~24 texts/sec, re-embedding
the ~800k-vector ST corpus would run for roughly half a day while competing with the
money-path apps for cores.  The nightly fleet ingest only embeds changed chunks.

## Important: the self-hosted endpoint is NOT a drop-in for the ST corpus

Measured before deployment, comparing self-hosted vectors against stored `socratic-trade`
vectors for the identical payload text:

- cosine similarity mean **0.869**, min **0.685** (mismatched-pair control: 0.478)
- self-retrieval with a self-hosted query vector: **Recall@1 64%**, Recall@5 84%
- the same test using the stored vector itself: **Recall@1 88%** (the ceiling is below 100%
  because the corpus contains near-duplicate boilerplate chunks)

So self-hosted `bge-m3` produces vectors in a *related but different* space from whatever
OpenRouter actually serves under `baai/bge-m3`.  Pointing ST queries at this endpoint would
cost roughly a quarter of Recall@1 against the existing corpus.  Tracked as board `b99cba29`.

Consequences:

1. **`fleet-agents` is unaffected** — that corpus is embedded and queried entirely by this
   endpoint, so it is internally consistent and carries no migration risk.
2. **Do not swap ST's embedding provider to this endpoint** without re-embedding the full
   corpus and passing the Recall@8 gate.  ST's `embedSpaceFilterForModel` would also need
   the new model id added to the bge lineage, or every existing vector becomes invisible.
3. The root cause was not isolated.  Both candidates remain open: the ONNX export in the
   `BAAI/bge-m3` repo may differ from the reference weights, or the text stored in the
   payload may not be byte-identical to what was originally sent to the embedder.

## Operational notes

- **Warmup is the memory peak, and it scales with `--max-batch-tokens`.**  At 8192 tokens
  the container was OOM-killed at warmup under an 8 GiB limit, exiting cleanly enough
  (code 137, `OOMKilled=true`) that it looked like a crash loop.  4096 tokens under a
  10 GiB limit is stable.  Raising `--max-batch-tokens` again means raising the cap too.
- Inputs longer than 4096 tokens are truncated rather than split by TEI; the chunker keeps
  every chunk far below that.
- `/health` is intentionally unauthenticated so Coolify and uptime probes can reach it.
  `/embed` and `/v1/embeddings` require the bearer token.
- Coolify's `${SERVICE_PASSWORD_*}` magic interpolates inside `environment:` but **not**
  inside `command:` — a key passed as a command flag silently becomes an empty string, which
  disables auth.  Pass it as `API_KEY=${SERVICE_PASSWORD_TEIKEY}` in `environment:` instead.
- Qdrant restarts cold: the first deep ST queries after a restart can hit the 15 s timeout
  (bounded, `lookup_failed`); `QDRANT_QUERY_TIMEOUT_MS=60000` is set on ST for that reason.
  Restart `qdrant-st` outside market hours only.
- When Tailscale is down on the Mac, `recall_search` / `recall_stats` / `recall_contribute`
  fall back to the public twin (`recall.jays.services`), but `ingest`, `eval`, and the doctor
  sentinel have no public route and need the direct Qdrant/TEI path.  `recall-tunnel up` then
  `eval "$(recall-tunnel env)"` forwards Qdrant 6333, TEI embed 8081, and TEI rerank 8082 over
  SSH to the box; an explicit `QDRANT_URL` + `TEI_URL` override (the tunnel or any other direct
  deployment) always tries the direct path first, regardless of what `tailscale status` says.
- **On macOS, an "unknown" Tailscale status is corroborated against the routing table, not
  just assumed up.**  If `Tailscale.app` is missing entirely (moved, removed, or a relocated
  binary), `tailscale_status_text()` returns None, which used to be read as "assume up" and
  send `recall doctor --platforms` (and any direct-path call) straight at the private Qdrant
  address -- on a network with a transparent TCP proxy, the connect "succeeds" in ~0.2s and
  then hangs on the read for `core.DEFAULT_TIMEOUT` (120s), which is exactly what stalled the
  nightly ingest routine's `recall doctor --platforms` preflight past 120s on 2026-09-17
  (board `ce7ecd46`).  `private_route_bypasses_tailscale()` (`scripts/fleet_rag/
  public_fallback.py`) now runs `/sbin/route -n get <qdrant-host>` (~2s timeout) in that one
  case; a resolved interface that is not a Tailscale `utun` device is positive evidence the
  private hop is gone, so `direct_path_blocked()` reports blocked in seconds instead of
  hanging.  A prior fix on board `da9324b0` that string-matched `tailscale status` output was
  withdrawn because "unknown = assume up" is intentional there; this is a different, narrower
  signal -- positive evidence from the OS routing table, macOS-only, and only consulted when
  the status probe itself came back unknown.
- **`recall-tunnel up` retries the SSH connect.**  On this network, about half of fresh SSH
  attempts to the box fail with "Connection timed out during banner exchange" or "Connection
  closed by 167.233.254.55 port 22" even though ping RTT is a steady 330-680ms (the transparent
  proxy again: the sshd journal on the box shows those attempts never arrive, so retrying is
  not a fail2ban / PerSourcePenalties risk).  `up` now passes `-o ConnectTimeout=60 -o
  ServerAliveInterval=15 -o ServerAliveCountMax=8` (the CountMax tolerates about 2 minutes of a
  stalled *established* link, since the same proxy can stall a session mid-flight, not just a
  fresh connect) and retries up to `RECALL_TUNNEL_ATTEMPTS` times (default 5) with a
  `RECALL_TUNNEL_RETRY_SLEEP`-second gap (default 60s) between attempts, logging every failed
  attempt to stderr and staying idempotent throughout via `master_alive()`; it exits non-zero
  only after the last attempt fails.  See `fleet_rag/tests/test_recall_tunnel.sh` for the
  retry/give-up/idempotency coverage (a fake `ssh` on `PATH`, no real network).
- **`recall-tunnel up` also starts a background supervisor** (`RECALL_TUNNEL_SUPERVISE=0`
  disables it) so a long-running caller -- the nightly ingest routine runs `recall-tunnel up`
  then `eval "$(recall-tunnel env)"` then `recall ingest --all --prune` without changing that
  prompt -- keeps a live tunnel across the whole run instead of needing to be babysat by hand.
  It polls `master_alive` every `RECALL_TUNNEL_SUPERVISE_INTERVAL` seconds (default 10) and
  reruns the same retried SSH open (`open_tunnel`) the moment the master dies; its pid lives in
  `<socket>.supervisor.pid`, its stop signal in `<socket>.supervisor.stop`, and its log in
  `<socket>.supervisor.log`.  `recall-tunnel down` stops the supervisor first (SIGTERM, then
  SIGKILL after a bounded ~2s grace period) before closing the master, and `recall-tunnel
  status` reports whether the supervisor is running.  `start_supervisor()` is serialized by a
  portable `mkdir`-based lock (macOS has no `flock` binary) with stale-lock cleanup, so two
  overlapping `up` calls (the nightly routine racing a manual one) never start two supervisors
  for the same pid file; as a second, independent layer, the supervisor loop itself exits
  within one poll interval if the pid file ever stops naming its own pid, so an orphan from any
  other path can never poll forever.  Tests: `fleet_rag/tests/test_recall_tunnel.sh`.
- **The HTTP layer retries a stall through the tunnel, not just the SSH connect.**  A 408
  (Request Timeout) from Qdrant is now retried exactly like 429/5xx in
  `fleet_rag/core.http_json` -- Qdrant's upsert-by-id and delete-by-id are idempotent, so
  retrying one after the tunnel reconnects is safe, and it is what lets an in-flight `recall
  ingest` call ride out a tunnel stall instead of failing the whole run (root-caused
  2026-09-17: catch-up ingest pass 1 hit `HTTP 408 from 127.0.0.1:16333/collections/fleet-
  agents/points/delete` and a similar 408 on the upsert path, then the tunnel master itself
  died and every subsequent call hit `URLError reaching 127.0.0.1:...`; running the same golden
  eval directly on the box had zero 408s, so the box was fine and the problem was the tunnel
  path).  The retry budget and backoff cap are overridable at call time via
  `RECALL_HTTP_RETRIES` (default 4, same as `core.RETRIES`) and `RECALL_HTTP_BACKOFF_MAX`
  (default 10s, same as `core.BACKOFF_MAX`); `recall-tunnel env` exports `RECALL_HTTP_RETRIES=8`
  and `RECALL_HTTP_BACKOFF_MAX=60`, which widens one request's worst case from about 15s to
  about 4-5 minutes -- long enough for the supervisor to notice the dead master and reopen the
  tunnel mid-retry.  Both env vars are bounds-checked by `core._env_int()`: a negative value
  (which used to make `RECALL_HTTP_RETRIES` skip the request entirely via `range(0)`, or reach
  a raw `time.sleep(negative)` `ValueError` for `RECALL_HTTP_BACKOFF_MAX`) falls back to the
  default just like a non-numeric value, and an unreasonably large one is clamped to
  `core.MAX_RETRIES` / `core.MAX_BACKOFF_MAX` instead of being honored outright; zero remains a
  valid `RECALL_HTTP_BACKOFF_MAX`.  Tests: `fleet_rag/tests/test_core_http.py` (408 retried, env
  overrides read at call time, explicit `retries=` still wins over the env var, malformed/
  negative/huge env values, zero backoff -- `urllib.request.urlopen` and `time.sleep` both
  mocked, no network, no real waiting).

## Files

- `scripts/fleet_rag/` — `core` (creds, embed, rerank, Qdrant client), `scrub`, `chunk`,
  `sources`, `ingest`, `notes_export`, `recall_api`, `contribute_guard`, `doctor`, `health`,
  `eval`, `telemetry` (optional Sentry reporting, see *Observability*), `golden.jsonl`, `tests/`
- `scripts/hooks/` — the Claude Code SessionStart and Stop hooks
- `scripts/fleet-recall-service/` — the Hetzner-side recall service (`server.py`, `bootstrap.sh`,
  `compose.example.yaml`, `Dockerfile`, `README.md`)
- `scripts/recall` — the CLI; `scripts/fleet-recall-mcp.py` — the stdio MCP server
- `scripts/recall-tunnel` — opens/checks/closes the SSH tunnel that forwards Qdrant/TEI over
  SSH to the Hetzner box when Tailscale is down on the Mac (`up`, `status`, `down`, `env`)
- `scripts/fleet-rag.py` — compatibility CLI
- `scripts/install-fleet-rag.sh` — installs to `~/apps/fleet-rag`, symlinks `recall` and
  `recall-tunnel`, registers the MCP server in every CLI config (`--dry-run`, `--uninstall`,
  `--with-seat-mcp`, `--hooks`)
- `scripts/seat-mcp/seat_mcp/recall_bridge.py` — the cloud-facing tools
- `scripts/hetzner/fleet-qdrant-snapshot.sh`, `fleet-qdrant-health.sh`, `fleet-qdrant.cron`
- `docs/fleet-skills/fleet-recall/SKILL.md` — the skill every seat loads
