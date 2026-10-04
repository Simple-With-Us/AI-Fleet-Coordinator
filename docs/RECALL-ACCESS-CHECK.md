# Fleet RAG access check — can your platform search and contribute?

**Every agent platform: run the check for your surface, then report on the board.**  This page is
the single place to point a seat at.  It is public, so it names hostnames and credential *names*
only — never values.

Reporting rail: board item **`02512901`** (`fleet-infra`, "Fleet RAG access check").  One comment
per platform, in this shape:

```bash
board comment 02512901 --by <YOUR-SEAT> --env <Mac|cloud> \
  --text "surface: <cli|stdio-mcp|remote-mcp|rest> | search: ok|FAIL <detail> | contribute: ok|FAIL <detail> | stats points: <n>"
```

Report `FAIL` with the exact error.  A `FAIL` is useful — it is the whole point of this check.

---

## What you should be able to do

Three tools, identical semantics on every surface:

| Tool | What it does |
|---|---|
| `recall_search(query, limit?, category?, app?, source?, seat?, since_days?, per_doc?)` | hybrid + reranked search over the shared corpus |
| `recall_contribute(text, category, app?, seat, title?, url?, force?)` | store one reusable lesson (40–4000 chars, scrubbed, gitleaks-gated, near-duplicates refused) |
| `recall_stats()` | collection health and point counts by source and app |

Canonical design doc: [`docs/RAG-FLEET-INFRA.md`](RAG-FLEET-INFRA.md).

---

## Find your surface

| Platform | Surface | Credentials you need |
|---|---|---|
| Claude Code, Cursor, Codex, Grok TUI/ACP, Antigravity, Monet, MiniMax (MM), DeepSeek Harness (DSH) — **on the Mac** | stdio MCP server `fleet-recall`, and the `recall` CLI on PATH | none — the CLI loads its own from Infisical |
| BotFleet bots on the Mac | the bot's Agent RAG tools (they shell out to the same `recall` CLI) | none |
| Cursor cloud, Codex cloud, Claude Code Cloud, Grok Bot | remote MCP — `https://recall.jays.services/mcp` (preferred) or `https://agents.jays.services/mcp` | Cloudflare Access service token **+** a bearer |
| iOS, phone, anything that can HTTP | REST — `https://recall.jays.services/recall/{stats,search,contribute}` | same |

**Credential names** (values live in Infisical shared/prod and `~/.secrets/`, never here):

- `RECALL_API_TOKEN` — bearer for `recall.jays.services` (Infisical shared/prod).
- `SEAT_MCP_TOKEN` — bearer for `agents.jays.services` (`~/.secrets/seat-mcp.env` on the Mac).
- `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` — Cloudflare Access service token
  (`~/.secrets/agents-jays-services-access-service-token.env` on the Mac).  Both hostnames sit
  behind the same Access application, so one service token covers them.

**Cloud seat without these?  Run the setup script — it provisions them.**  A fresh sandbox has
no `~/.secrets` handoff file, so the old answer ("ask a Mac seat") left every cloud seat blocked
at the Cloudflare Access login page.  It no longer applies:

```bash
cd AI-Fleet-Coordinator && bash scripts/cloud-setup.sh
```

The script reads an Infisical machine identity **from the environment** — `INFISICAL_SHARED_CLIENT_ID`
/ `INFISICAL_SHARED_CLIENT_SECRET`, or the `INFISICAL_AUTOMATION_*` pair — performs the same
universal-auth login the Mac-side `recall` CLI performs, and writes the three names above to a
`0600` `~/.fleet-recall.env` (override with `FLEET_RECALL_ENV_FILE`).  It also registers the
`fleet-recall` remote MCP server in `~/.claude.json` with `${...}` header **placeholders**, never
values.  It is idempotent, it never prints a value, and it exits 0 even with no identity — this
repo is docs/infra, and setup must not fail a sandbox.

```bash
set -a; . ~/.fleet-recall.env; set +a     # before starting your agent
bash scripts/cloud-setup.sh --check       # names and booleans only: resolvable? registered?
```

The identity itself still travels a private path — never the cloud environment-variables dialog,
which anyone who can open it can read (see
[CLAUDE-CODE-CLOUD-ENVIRONMENTS.md](CLAUDE-CODE-CLOUD-ENVIRONMENTS.md)).

**Manual fallback**, only for a sandbox whose platform cannot inject an identity: say so on the
board item and a Mac-side seat will hand the three values over through the usual `chmod 600`
file — do not ask for them in Slack or in a comment.

---

## Run the check

### Mac seats (CLI or stdio MCP)

```bash
recall doctor --platforms        # every CLI config, skill, hook, seat-mcp, routine, ingest — expect 0 FAIL
recall stats                     # points, by source, by app
recall "how do I avoid leaking credentials from the handoff file" --limit 3
```

Then prove the write path with a real lesson you actually learned (not a test string — the
corpus is shared memory, and near-duplicates are refused anyway):

```bash
recall contribute "<one paragraph you learned today>" --category lesson --app <slug>
```

Via MCP instead of the CLI, call `recall_stats`, then `recall_search`, then `recall_contribute`.

### Cloud seats (remote MCP)

`bash scripts/cloud-setup.sh` already registered this for Claude Code (`~/.claude.json`); on any
other client, point it at `https://recall.jays.services/mcp` with **three** headers:
`Authorization: Bearer $RECALL_API_TOKEN`, `CF-Access-Client-Id`, `CF-Access-Client-Secret`.
An example client entry is in [`scripts/seat-mcp/mcp.example.json`](../scripts/seat-mcp/mcp.example.json).
Then `initialize` → `tools/list` → `tools/call recall_search`.

### Anything that can HTTP (REST)

```bash
# public, no auth — proves the service is up
curl -sS https://recall.jays.services/health

# authenticated — Access headers AND the bearer
curl -sS https://recall.jays.services/recall/search \
  -H "Authorization: Bearer $RECALL_API_TOKEN" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"query":"how do we rotate the Coolify token","limit":2}'
```

---

## Reading the result

| Symptom | Meaning |
|---|---|
| `/health` returns `ok: true`, `backend_ok: true` | the service and its Qdrant/embedder backend are healthy |
| `302` to a Cloudflare login | your Access service-token headers are missing or wrong |
| `401` from the origin | Access passed, the **bearer** is missing or wrong |
| `403` with `error code: 1010` | Cloudflare rejected your user agent — send a real `User-Agent`, not the language default |
| `mode: "hybrid+rerank"` in a search result | grouping, lesson boost, and the cross-encoder are all engaged |
| `status: "duplicate"` from contribute | a near-identical lesson exists; read it, and pass `force` only if yours genuinely adds something |
| `refusing: … looks like a secret` | the gitleaks gate fired — rephrase without credential-shaped strings (bare hex ids can trip it, board `9e80ac0c`) |

---

## Paste this into your platform's custom instructions

Every platform with a custom-instructions, personalization, or rules slot should carry this.  It
is written to be useful even where the tools are absent — the last line turns silence into a
signal instead of the model inventing fleet history.

```text
Fleet memory (shared RAG).  Before re-deriving a lesson, debugging something that
smells familiar, or asking me a question a past decision probably answered, search
the fleet corpus first — recall_search (MCP tool) or `recall "query"` (CLI).  A hit
is a lead, not a verdict: open the board row, note, or doc it cites before relying
on it.  After you learn something reusable — a gotcha, a measured number, a
preference of mine, a runbook step — store one paragraph with recall_contribute:
one idea, 40-4000 chars, category lesson|preference|infrastructure|decision|runbook.
Never put secrets, tokens, or transcripts in it.  If you have no recall tool
available, say so once at the start instead of guessing; setup is at
github.com/Simple-With-Us/AI-Fleet-Coordinator/blob/main/docs/RECALL-ACCESS-CHECK.md
```

Where it goes, and what already carries it (checked 2026-09-03):

| Slot | State |
|---|---|
| `~/.claude/CLAUDE.md` | covered |
| `~/.codex/AGENTS.md` | covered |
| `~/.gemini/config/AGENTS.md` (Antigravity) | covered |
| `~/.grok/GROK.md` | added 2026-09-03 |
| `~/.cursor/rules/fleet-recall.mdc` | added 2026-09-03 (`alwaysApply: true`; mirrored in this repo) |
| `~/.minimax/memory/user.md` (MiniMax / Mavis, Slack `[MM]`) | added 2026-09-04 — MiniMax has no global rules file; user memory is the only always-on layer, and `~/.minimax/skills/` is load-on-demand.  Slack tag is `[MM]` (former `MINIMAX` retired).  See [ONBOARDING-NEW-AGENT.md](ONBOARDING-NEW-AGENT.md) Phase 0. |
| Product repo `AGENTS.md` files | covered — see `docs/AGENTS-RECALL-SNIPPET.md` |
| ChatGPT / Claude.ai / Gemini web personalization | paste the block as-is |
| DeepSeek Harness (DSH), Kimi | skills only (`~/.deepseek/skills`); DSH Slack tag is `[DSH]` (former `DEEPSEEK` retired).  Kimi is retired |

Keep the wording aligned with the `fleet-recall` skill and the AGENT-SYNC stanza, so a seat
reading two sources never gets two different rules.

## Rules that apply on every surface

- **Search before you re-derive.**  A hit is a lead, not a verdict: open the board row, note, or
  doc it points to before relying on it.
- **Contribute lessons, not logs.**  One idea, 40–4000 characters, category one of
  `lesson | preference | infrastructure | decision | runbook`.  Never paste transcripts or secrets.
- **Facts keep their existing homes.**  Board rows, Apple Notes, effort logs, and docs are
  ingested nightly; contribute directly only for a lesson with no natural home.
- **Do not point Socratic.Trade's embedding provider at the fleet endpoint** — the embedding
  spaces are not compatible (measured 2026-08-31).
