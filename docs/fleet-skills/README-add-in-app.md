# Fleet Skills Catalog (Universal Multi-Platform Pack)

Updated **2026-08-25**: the installer **specializes identity per seat** and **omits** skills that are not appropriate for that harness.  `docs/fleet-skills/` stays the Monet / Claude.app upload pack.  Do not copy Monet `SKILL.md` files into another seat's folder unchanged.  `ios-ship` is not installed anywhere — Compiler / `GB-COMPILE` owns GitHub-hosted `macos-latest` iOS ship.  DealDex's hosted Actions ship stays — do not disable it.

| Seat | Slack tag | Home / upload dir | Branches | Notes |
| :--- | :--- | :--- | :--- | :--- |
| Cursor IDE / Auto | `[CURSOR]` | `~/.cursor/skills` | `cursor/` | Cloud Grok Bot fork → `[GB-<NAME>]` (not `[GROK-BOT]`) |
| Grok Bot (Cursor cloud) | `[GB-<NAME>]` | `docs/fleet-skills/by-seat/grok-bot/` | `cursor/` | GB-CONDUCTOR / MONITOR / FIXER / DEPLOYER / COMPILE / NURSE / HOUSEKEEPER / ACCOUNTANT.  Not `[GROK-BOT]`, not `[CURSOR]`, not Mac Grok TUI |
| Antigravity / Gemini | `[AG]` | `~/.gemini/skills` | `ag/` | Lane folders `antigravity-<slug>` |
| Codex | `[CODEX]` | `~/.codex/skills` | `codex/` | |
| fx TUI | `[FX]` | `~/.fx/skills` | `fx/` | Quote-safe YAML `>-` descriptions; set `context_limits.skill_catalog_bytes` to `off` in `~/.fx/settings.json` so the full catalog is not truncated |
| Grok TUI | `[GROK]` | `~/.grok/skills` | `grok/` | Grok Build fork → `[GROK-BUILD]` |
| Grok Build | `[GROK-BUILD]` | `~/.grok-build/skills` | `grok-build/` | |
| Claude Code CLI | pin `AGENT_SEAT` | `~/.claude/skills` | `monet/` or `claude/` or `renoir/` | Shared library |
| Monet (Claude.app) | `[MONET]` | `~/Desktop/fleet-skills` + git zips | `monet/` | Upload on **MONET** login |
| Claude / Fable | `[CLAUDE]` | `docs/fleet-skills/by-seat/claude/` | `claude/` | Upload on **CLAUDE** login |
| Renoir | `[RENOIR]` | `~/.renoir/skills` | `renoir/` | Not yet active |
| DeepSeek Harness | `[DSH]` | `~/.deepseek/skills` | `deepseek/` | Former Slack tag `DEEPSEEK` is retired.  A DeepSeek *model* in Cursor is still Cursor |
| MiniMax Code / Mavis | `[MM]` | `~/.minimax/skills` | `minimax/` | Former Slack tag `MINIMAX` is retired.  Loaded on demand from `<available_skills>`, never auto-applied; the always-on fleet pointer is `~/.minimax/memory/user.md` |
| Kimi | `[KIMI]` | `~/.kimi/skills` | `kimi/` | Retired — do not take work |

These skills govern fleet operations across all apps (Socratic.Trade, Congress.Trade, Usage-Monitor, congress-trading-shared, DealDex, Personal-Site, Autorotate, ContactLogo, and AI-Fleet-Coordinator).

Having explicit fleet skills installed significantly improves agent compliance with procedures across all chats and tools, reinforcing the protocols defined in `AGENT-SYNC.md` and `AGENTS.md`.

---

## 📦 Complete Skills Catalog

| Skill | Purpose & Trigger |
| :--- | :--- |
| **`fleet-coordination`** | **Master Flagship Skill:** End-to-end multi-agent fleet operations (startup, triple-claim, secrets, sentence gap, Apple Notes, PR landing, closeout). |
| **`session-start`** | Systematic startup: poll Slack, check THE BOARD & live effort logs, lanes under `~/apps/lanes/` (made with `lane new`), claim before editing. |
| **`board-ops`** | Operating THE BOARD CLI (`board stats`, `board list`, `board claim`, `board file`) and `mac.jays.services/board`. |
| **`secret-handoff`** | Strict secret safety: canonical handoff file `/Users/jay/.secrets/global-api-keys`, Infisical runtime source of truth, grep-trap ban, safe helpers. |
| **`sentence-gap`** | Visible double-space between sentences (`&nbsp;` plus a space in Markdown chat panes, U+00A0 plus a space in GitHub and Zulip text, two literal spaces in source files and terminals). |
| **`owner-copy`** | Human-facing copy standards: two spaces, light theme default, Title Case headings, no agent names in ASC release notes. |
| **`apple-notes`** | Owner-facing review docs, plans, rollouts, and completion notes in the `Coding` folder (local on this Mac). |
| **`land-lane`** | App-specific verification gates, PR creation, auto-merge arming, and production deploy triggers. |
| **`unstick-pr`** | Diagnosing and unblocking stuck PRs (phantom vs real conflicts with 2-arg `git merge-tree`, bot review threads, flakes). |
| **`codex-triage`** | Triaging and resolving automated review bot comments (Codex, Bugbot, Copilot, human reviewers). |
| **`pickup-seat`** | Picking up capped or abandoned peer lanes safely with full attribution. |
| **`fleet-infra`** | Accessing the private infrastructure hub (`fleet-ops:ATTACK-MAP.md`) for host IPs, Tailscale mesh, Coolify UUIDs, and Infisical IDs without committing secrets. |
| **`dns-and-registrars`** | Cloudflare is DNS for every fleet domain.  Create each new app's own zone on account Usage.Jays.Services — not on hostname `usage.jays.services`.  Canonical: `docs/DNS-AND-REGISTRARS.md`. |
| **`deploy-verify`** | Post-merge verification across Coolify, Vercel, and public `/api/health` endpoints. |
| **`mac-cleanup`** | Mac / Hetzner disk cleanup (worktrees, Xcode caches, package caches).  Not an iOS ship loop.  Omitted from cloud Grok Bot. |
| **`housekeeper`** | Disk/RAM/CPU housekeeping playbook, CleanMyMac, resource-threshold wakes for BotFleet Housekeeper and `GB-HOUSEKEEPER`.  Installed on Grok Bot (cloud hop via `drive-grok-tui`). |
| **`drive-grok-tui`** | Any seat: list live Mac Grok TUI chats and inject a follow-up via `grok-drive.py` or seat-mcp.  Cloud seats use `https://agents.jays.services/mcp`.  Installed on `[GROK]` too (self-inject is refused unless `--self`). |
| **`closeout`** | End-of-task closeout: effort board Deployed/Completed, GitHub Issue closed, Slack `#agent-sync` closeout, Apple Notes stamp. |
| **`fleet-recall`** | Search the shared `fleet-agents` corpus before re-deriving a lesson; contribute one after you learn it.  CLI `recall`, MCP `fleet-recall`, cloud via `agents.jays.services/mcp`. |

**Per-seat install rule:** every listed skill is rewritten to that seat's Slack tag, Notes name, branch prefix, and worktree, then written to that seat's home / by-seat pack.  A skill that cannot be made appropriate for a harness is omitted (prefer omit over a wrong-voiced copy).  `ios-ship` is omitted from every seat.  DealDex's hosted Actions ship stays — do not disable it.  `mac-cleanup` is omitted from Grok Bot (cloud).  `housekeeper` is on every seat including Grok Bot.  `drive-grok-tui` is on every seat.  `codex-triage` stays on every seat that lands PRs — the name is historical; the body is GitHub review-thread triage.

---

## 🚀 Installation & Import Guide

### 1. Local skill dirs (specialized per seat)
Folders with `SKILL.md`.  Run:

```bash
python3 /Users/jay/Code/AI-Fleet-Coordinator/scripts/install-fleet-skills.py
```

Never rsync the Monet pack into another seat without that script.

### 2. Claude Desktop & Web App (UI Upload)
Use **that login's** zip pack:
- Monet login: `docs/fleet-skills/<skill>.zip` or `~/Desktop/fleet-skills`
- Claude login: `docs/fleet-skills/by-seat/claude/<skill>.zip`
- Renoir login: `docs/fleet-skills/by-seat/renoir/<skill>.zip`

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
