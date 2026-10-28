# Fleet Skills Catalog (Universal Multi-Platform Pack)

Updated **2026-10-08**: the installer **specializes identity per seat** (Zulip bot, credential file, `[SEAT·session8]` tag, Notes name, branch prefix) and **omits** skills that are not appropriate for that harness.  `docs/fleet-skills/` stays the canonical source pack (written with the retired MONET seat's tag and paths as template placeholders, which the installer rewrites for each seat; MONET itself takes no work) and the Claude.app zip pack.  Do not copy canonical `SKILL.md` files into another seat's folder unchanged.  `ios-ship` is not installed anywhere — Compiler / `GB-COMPILE` owns GitHub-hosted `macos-latest` iOS ship.  DealDex's hosted Actions ship stays — do not disable it.

| Seat | Chat tag | Home / upload dir | Branches | Notes |
| :--- | :--- | :--- | :--- | :--- |
| Cursor IDE / Auto | `[CURSOR]` | `~/.cursor/skills` | `cursor/` | Cloud Grok Bot fork → `[GB-<NAME>]` (not `[GROK-BOT]`) |
| Grok Bot (Cursor cloud) | `[GB-<NAME>]` | `docs/fleet-skills/by-seat/grok-bot/` | `cursor/` | GB-DIRECTOR / FIXER / DESIGNER / COMPILER / HOUSEKEEPER / PUBLISHER / DEPLOYER / MONITOR / PLUMBER / ORACLE / TRADER (from `scripts/agent_sync/mcp/gb-identities.json`).  Not `[GROK-BOT]`, not `[CURSOR]`, not Mac Grok TUI |
| Antigravity / Gemini | `[AG]` | `~/.gemini/skills` | `ag/` | Lane folders `antigravity-<slug>` |
| Codex | `[CODEX]` | `~/.codex/skills` | `codex/` | |
| fx TUI | `[FX]` | `~/.fx/skills` | `fx/` | Quote-safe YAML `>-` descriptions; set `context_limits.skill_catalog_bytes` to `off` in `~/.fx/settings.json` so the full catalog is not truncated |
| Grok TUI / Grok Build | `[GROK]` | `~/.grok/skills` (Grok Build pack at `~/.grok-build/skills` signs GROK too) | `grok/` (`grok-build/` branches stay readable) | GROK-BUILD in any spelling is an alias for GROK, not a seat tag |
| Claude Code CLI | `[CLAUDE]` | `~/.claude/skills` | `claude/` | Shared library.  Other tools (fx) scan it, so the copy carries a banner telling them to take identity from their own pack |
| Monet | `[MONET]` | `docs/fleet-skills/by-seat/monet/` (catalog only) | `monet/` | Retired 2026-10-07.  Never installed, do not take work |
| Claude / Fable | `[CLAUDE]` | `docs/fleet-skills/by-seat/claude/` | `claude/` | Upload on **CLAUDE** login |
| Renoir | `[RENOIR]` | `docs/fleet-skills/by-seat/renoir/` (catalog only) | `renoir/` | Retired 2026-10-07.  The seat never opened |
| DeepSeek Harness | `[DSH]` | `docs/fleet-skills/by-seat/deepseek/` (catalog only) | `deepseek/` | Retired 2026-09-19, use Clutch.  Former Slack tag `DEEPSEEK` is retired.  A DeepSeek *model* in Cursor is still Cursor |
| Clutch | `[CLUTCH]` | `~/.clutch/dsh/skills` (the DSH engine's skill root; a by-seat copy is kept in `docs/fleet-skills/by-seat/clutch/`) | `clutch/` | Replaces HARNESS and DSH.  One seat for every model run through Clutch |
| MiniMax Code / Mavis | `[MM]` | `~/.minimax/skills` | `minimax/` | Former Slack tag `MINIMAX` is retired.  Loaded on demand from `<available_skills>`, never auto-applied; the always-on fleet pointer is `~/.minimax/memory/user.md` |
| Kimi | `[KIMI]` | `docs/fleet-skills/by-seat/kimi/` (catalog only) | `kimi/` | Retired — do not take work |

These skills govern fleet operations across all apps (Socratic.Trade, Congress.Trade, Usage-Monitor, congress-trading-shared, DealDex, Personal-Site, Autorotate, ContactLogo, and AI-Fleet-Coordinator).

Having explicit fleet skills installed significantly improves agent compliance with procedures across all chats and tools, reinforcing the protocols defined in `FLEET-PROTOCOL.md` and `AGENTS.md`.

---

## 📦 Complete Skills Catalog

| Skill | Purpose & Trigger |
| :--- | :--- |
| **`fleet-coordination`** | **Master Flagship Skill:** End-to-end multi-agent fleet operations (startup, the Linear claim, secrets, sentence gap, Apple Notes, PR landing, closeout). |
| **`fleet-mode`** | The fleet's standing working style:  delegate by default to the cheapest sufficiently competent model, brief workers thoroughly, give them only the tools they need, spawn in the background, stay reachable, and never end a turn with a promise.  The default on every platform, not an opt-in. |
| **`session-start`** | Systematic startup: read the Zulip inbox (`simple-chat inbox`), read Linear (the `simple-board` CLI), lanes under `~/apps/lanes/<Repo>/` (made with `lane new`), claim before editing. |
| **`board-ops`** | Operating the `simple-board` CLI on Linear (`simple-board stats`, `simple-board list`, `simple-board claim`, `simple-board file`), ids like `BF-123`. |
| **`secret-handoff`** | Strict secret safety: `fleet-secrets` reads (the flat `global-api-keys` file is retired), Infisical runtime source of truth, grep-trap ban, safe helpers. |
| **`secret-safety`** | Mandatory safeguards for handling secrets, API keys, tokens and credentials:  redact inline, as part of the command that touches the secret, never as a judgment call about what to display after it ran.  Load before any command whose arguments or environment touch a credential. |
| **`sentence-gap`** | Visible gap between sentences:  a real U+00A0 plus a normal space on every surface (owner ruling 2026-10-23), never two plain ASCII spaces, and never the `&nbsp;` entity except in the one exception:  the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane) take the entity plus a normal space.  Product-copy source uses the per-language escapes. |
| **`owner-copy`** | Human-facing copy standards:  a real U+00A0 plus a normal space between sentences (the `sentence-gap` rule above), system theme default (owner ruling 2026-09-19), Title Case headings, no agent names in ASC release notes. |
| **`apple-notes`** | Owner-facing review docs, plans, rollouts, and completion notes in the `Coding` folder (local on this Mac). |
| **`land-lane`** | App-specific verification gates, PR creation, auto-merge arming, and production deploy triggers. |
| **`unstick-pr`** | Diagnosing and unblocking stuck PRs (phantom vs real conflicts with 2-arg `git merge-tree`, bot review threads, flakes). |
| **`codex-triage`** | Triaging and resolving automated review bot comments (Codex, Bugbot, Copilot, human reviewers). |
| **`pr-sweep`** | Sweeping and landing the open PRs across the fleet from any platform (`scripts/pr-sweep`: inventory, plan, status computed in code, one worker per PR, audit, deploy checks, lane retire).  Rules live only in `scripts/pr_sweep/rules.md`.  Canonical: `docs/PR-SWEEP.md`. |
| **`pickup-seat`** | Picking up capped or abandoned peer lanes safely with full attribution. |
| **`fleet-infra`** | Accessing the private infrastructure hub (`fleet-ops:ATTACK-MAP.md`) for host IPs, Tailscale mesh, Coolify UUIDs, and Infisical IDs without committing secrets. |
| **`dns-and-registrars`** | Cloudflare is DNS for every fleet domain.  Create each new app's own zone on account Usage.Jays.Services — not on hostname `usage.jays.services`.  Canonical: `docs/DNS-AND-REGISTRARS.md`. |
| **`deploy-verify`** | Post-merge verification across Coolify, Vercel, and public `/api/health` endpoints, plus TestFlight freshness for iOS apps. |
| **`mac-cleanup`** | Mac / Hetzner disk cleanup (worktrees, Xcode caches, package caches).  Not an iOS ship loop.  Omitted from cloud Grok Bot. |
| **`housekeeper`** | Disk/RAM/CPU housekeeping playbook, CleanMyMac, resource-threshold wakes for BotFleet Housekeeper and `GB-HOUSEKEEPER`.  Installed on Grok Bot (cloud hop via `drive-grok-tui`). |
| **`drive-grok-tui`** | Any seat: list live Mac Grok TUI chats and inject a follow-up via `grok-drive.py` or seat-mcp.  Cloud seats use `https://agents.jays.services/mcp`.  Installed on `[GROK]` too (self-inject is refused unless `--self`). |
| **`closeout`** | End-of-task closeout: Linear issue Deployed/Completed, the PR linked with `Fixes` or `Refs`, a closeout post in the Zulip work topic under `#Work-In-Progress`, Apple Notes stamp. |
| **`fleet-recall`** | Search the shared `fleet-agents` corpus before re-deriving a lesson; contribute one after you learn it.  CLI `recall`, MCP `fleet-recall`, cloud via `agents.jays.services/mcp`. |
| **`simple-tools-guide`** | Any question about the ST-MCP tools or CLI:  family names (`zulip_*`, `linear_*`, `notes_*`), the `identity` argument and its errors, old tool names, stdio versus hosted, Apple Notes from any host.  Points at `docs/protocols/simple-tools-guide.md`, the `guide` tool and https://fleetlink.online/simple-tools-guide/. |

**Per-seat install rule:** every listed skill is rewritten to that seat's chat tag, Zulip bot, Notes name, branch prefix, and worktree, then written to that seat's home / by-seat pack.  Retired seats (Monet, Renoir, DSH, Kimi) have no skill home: they get an inert by-seat catalog copy only, and the installer never writes their homes.  A skill that cannot be made appropriate for a harness is omitted (prefer omit over a wrong-voiced copy).  `ios-ship` is omitted from every seat.  DealDex's hosted Actions ship stays — do not disable it.  `mac-cleanup` is omitted from Grok Bot (cloud).  `housekeeper` is on every seat including Grok Bot.  `drive-grok-tui` is on every seat.  `codex-triage` stays on every seat that lands PRs — the name is historical; the body is GitHub review-thread triage.

---

## 🚀 Installation & Import Guide

### 1. Local skill dirs (specialized per seat)
Folders with `SKILL.md`.  Run:

```bash
python3 /Users/jay/Code/AI-Fleet-Coordinator/scripts/install-fleet-skills.py
```

To re-render only the copies tracked in this repo (`skills/`, the zips, `.claude/.cursor/.grok` skills, and `by-seat/`), and never write the home directory:

```bash
python3 scripts/install-fleet-skills.py --repo-only
```

Never rsync the canonical pack into another seat without that script.

### 2. Claude Desktop & Web App (UI Upload)
Use **that login's** zip pack:
- Claude login: `docs/fleet-skills/by-seat/claude/<skill>.zip`
- The Monet and Renoir logins are retired (owner 2026-10-07), so there is no upload for them.

1. Open **Settings → Capabilities → Skills**.
2. Click **Create / Upload skill**.
3. Select the zip for **that** seat.
4. Enable the imported skills.

---

## 🔒 Safety & Sanitization Guarantees

All skills in this directory are sanitized and safe for repository tracking:
- **No live API keys, tokens, or plaintext passwords.**
- **No insecure secret-dumping endpoints.**
- **Grep-trap compliance:** Instructions mandate names-only grep (`grep -oE '^[A-Z][A-Z0-9_]*'`) and direct single-variable extraction.
- **Runtime secrets:** Enforce Infisical as the sole source of truth for deployed application environments.
