# Fleet Skills Catalog (Universal Multi-Platform Pack)

Updated **2026-10-08**: the installer **specializes identity per seat** (Zulip bot, credential file, `[SEAT·session8]` tag, Notes name, branch prefix) and **omits** skills that are not appropriate for that harness.  `docs/fleet-skills/` stays the canonical source pack (Monet-voiced, which the installer rewrites per seat) and the Claude.app zip pack.  Do not copy canonical `SKILL.md` files into another seat's folder unchanged.  `ios-ship` is not installed anywhere — Compiler / `GB-COMPILE` owns GitHub-hosted `macos-latest` iOS ship.  DealDex's hosted Actions ship stays — do not disable it.

| Seat | Chat tag | Home / upload dir | Branches | Notes |
| :--- | :--- | :--- | :--- | :--- |
| Cursor IDE / Auto | `[CURSOR]` | `~/.cursor/skills` | `cursor/` | Cloud Grok Bot fork → `[GB-<NAME>]` (not `[GROK-BOT]`) |
| Grok Bot (Cursor cloud) | `[GB-<NAME>]` | `docs/fleet-skills/by-seat/grok-bot/` | `cursor/` | GB-CONDUCTOR / MONITOR / FIXER / DEPLOYER / COMPILE / NURSE / HOUSEKEEPER / ACCOUNTANT.  Not `[GROK-BOT]`, not `[CURSOR]`, not Mac Grok TUI |
| Antigravity / Gemini | `[AG]` | `~/.gemini/skills` | `ag/` | Lane folders `antigravity-<slug>` |
| Codex | `[CODEX]` | `~/.codex/skills` | `codex/` | |
| fx TUI | `[FX]` | `~/.fx/skills` | `fx/` | Quote-safe YAML `>-` descriptions; set `context_limits.skill_catalog_bytes` to `off` in `~/.fx/settings.json` so the full catalog is not truncated |
| Grok TUI | `[GROK]` | `~/.grok/skills` | `grok/` | Grok Build fork → `[GROK-BUILD]` |
| Grok Build | `[GROK-BUILD]` | `~/.grok-build/skills` | `grok-build/` | |
| Claude Code CLI | `[CLAUDE]` | `~/.claude/skills` | `claude/` | Shared library.  Other tools (fx) scan it, so the copy carries a banner telling them to take identity from their own pack |
| Monet | `[MONET]` | `docs/fleet-skills/by-seat/monet/` (catalog only) | `monet/` | Retired 2026-10-07.  Never installed, do not take work |
| Claude / Fable | `[CLAUDE]` | `docs/fleet-skills/by-seat/claude/` | `claude/` | Upload on **CLAUDE** login |
| Renoir | `[RENOIR]` | `docs/fleet-skills/by-seat/renoir/` (catalog only) | `renoir/` | Retired 2026-10-07.  The seat never opened |
| DeepSeek Harness | `[DSH]` | `docs/fleet-skills/by-seat/deepseek/` (catalog only) | `deepseek/` | Retired 2026-09-19, use Clutch.  Former Slack tag `DEEPSEEK` is retired.  A DeepSeek *model* in Cursor is still Cursor |
| Clutch | `[CLUTCH]` | `docs/fleet-skills/by-seat/clutch/` (catalog only, no skill home yet) | `clutch/` | Replaces HARNESS and DSH.  One seat for every model run through Clutch |
| MiniMax Code / Mavis | `[MM]` | `~/.minimax/skills` | `minimax/` | Former Slack tag `MINIMAX` is retired.  Loaded on demand from `<available_skills>`, never auto-applied; the always-on fleet pointer is `~/.minimax/memory/user.md` |
| Kimi | `[KIMI]` | `~/.kimi/skills` | `kimi/` | Retired — do not take work |

These skills govern fleet operations across all apps (Socratic.Trade, Congress.Trade, Usage-Monitor, congress-trading-shared, DealDex, Personal-Site, Autorotate, ContactLogo, and AI-Fleet-Coordinator).

Having explicit fleet skills installed significantly improves agent compliance with procedures across all chats and tools, reinforcing the protocols defined in `AGENT-SYNC.md` and `AGENTS.md`.

---

## 📦 Complete Skills Catalog

| Skill | Purpose & Trigger |
| :--- | :--- |
| **`fleet-coordination`** | **Master Flagship Skill:** End-to-end multi-agent fleet operations (startup, triple-claim, secrets, sentence gap, Apple Notes, PR landing, closeout). |
| **`session-start`** | Systematic startup: read the Zulip inbox (`agent-sync inbox`), check THE BOARD & live effort logs, lanes under `~/apps/lanes/` (made with `lane new`), claim before editing. |
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
| **`closeout`** | End-of-task closeout: effort board Deployed/Completed, GitHub Issue closed, a closeout post in the Zulip work topic under `#agent-sync`, Apple Notes stamp. |
| **`fleet-recall`** | Search the shared `fleet-agents` corpus before re-deriving a lesson; contribute one after you learn it.  CLI `recall`, MCP `fleet-recall`, cloud via `agents.jays.services/mcp`. |

**Per-seat install rule:** every listed skill is rewritten to that seat's chat tag, Zulip bot, Notes name, branch prefix, and worktree, then written to that seat's home / by-seat pack.  Retired seats (Monet, Renoir, DSH, Kimi) and Clutch have no skill home: they get an inert by-seat catalog copy only, and the installer never writes their homes.  A skill that cannot be made appropriate for a harness is omitted (prefer omit over a wrong-voiced copy).  `ios-ship` is omitted from every seat.  DealDex's hosted Actions ship stays — do not disable it.  `mac-cleanup` is omitted from Grok Bot (cloud).  `housekeeper` is on every seat including Grok Bot.  `drive-grok-tui` is on every seat.  `codex-triage` stays on every seat that lands PRs — the name is historical; the body is GitHub review-thread triage.

---

## 🚀 Installation & Import Guide

### 1. Local skill dirs (specialized per seat)
Folders with `SKILL.md`.  Run:

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
