# Draft PR sweep verdicts — Nov 9 (AFC-678)

All 285 open draft PRs, classified from the PR inventory plus a local git fetch of every head branch (merge-tree vs base).

| Verdict | Count |
|---|---|
| CLOSE dead (merge changes nothing) | 6 |
| CLOSE dead (branch deleted on GitHub) | 5 |
| LEAVE active (branch commit <=3d) | 11 |
| WORK: unique content, 3-30d | 130 |
| WORK: unique content, >30d | 133 |

## CLOSE (11)

- ContactLogo#311 `codex/kody-fleet-caller-20261005` — CLOSE dead (branch deleted on GitHub): head branch deleted on GitHub
- ContactLogo#316 `minimax/fix-gitleaks-v3-license` — CLOSE dead (branch deleted on GitHub): head branch deleted on GitHub
- FleetLink#177 `autofix/reap-indexes` — CLOSE dead (branch deleted on GitHub): head branch deleted on GitHub
- FleetLink#179 `claude/fl-team-sharing-mcp` — CLOSE dead (branch deleted on GitHub): head branch deleted on GitHub
- FleetLink#186 `cursor/fl-1-passkey-auth-f736` — CLOSE dead (branch deleted on GitHub): head branch deleted on GitHub
- AI-Fleet-Coordinator#1627 `gb-director/post-intro-format` — CLOSE dead (merge changes nothing): merge-tree of head into base equals base tree (empty change)
- BotFleet#1656 `claude/kody-high-callview-visual` — CLOSE dead (merge changes nothing): merge-tree of head into base equals base tree (empty change)
- BotFleet#1701 `grok/designer-bot-chats-once` — CLOSE dead (merge changes nothing): merge-tree of head into base equals base tree (empty change)
- CodeCaps#400 `claude/linear-effort-log-archive` — CLOSE dead (merge changes nothing): merge-tree of head into base equals base tree (empty change)
- CodeCaps#402 `claude/salvage-reader-transport` — CLOSE dead (merge changes nothing): merge-tree of head into base equals base tree (empty change)
- CodeCaps#455 `cursor/cc-225-muse-copy-ffa7` — CLOSE dead (merge changes nothing): merge-tree of head into base equals base tree (empty change)

## LEAVE (11 active)

- AI-Fleet-Coordinator#1355 `instinct/jet-webhook-wake` — branch 1.4d old, seat active
- AI-Fleet-Coordinator#1417 `echo/afc-649-minimax-known-cap` — branch 3.0d old, seat active
- AI-Fleet-Coordinator#1556 `Muse-Assist/operator-session-registry` — branch 1.0d old, seat active
- AI-Fleet-Coordinator#1583 `claude/muster-release-feed-wait` — branch 2.3d old, seat active
- AI-Fleet-Coordinator#1619 `echo/afc-652-drain-soundness` — branch 3.0d old, seat active
- AI-Fleet-Coordinator#1626 `fix/zc-zulip-credential-stem` — branch 0.0d old, seat active
- AI-Fleet-Coordinator#1666 `echo/ops-1042-gb-linear-prepare` — branch 2.0d old, seat active
- CodeCaps#410 `codex/cc-205-release-ready` — branch 1.8d old, seat active
- CodeCaps#456 `cursor/cc-224-xai-widget-mark-3f31` — branch 0.3d old, seat active
- CodeCaps#457 `cursor/cc-223-widget-window-parity-8626` — branch 0.3d old, seat active
- Usage-Monitor#1923 `autofix-sentry-path-fallback` — branch 0.0d old, seat active

## MERGE-READY (5 — CLEAN + green + no threads + no changes-requested)

- AI-Fleet-Coordinator#1567 `chore/autofix-caller-token-grants` — Autofix Caller Token Grants
- AI-Fleet-Coordinator#1576 `claude/agent-sync-mcp-design` — Agent Sync Mcp Design
- Congress.Trade#2969 `antigravity/infisical-kill-timeout` — Infisical Kill Timeout
- HogHunter#280 `MM/HH-191-version-scheme` — HH 191 Version Scheme
- HogHunter#284 `ag/hh-sparkle-update-feed` — Hh Sparkle Update Feed

## COMBINE candidates (6 title clusters, 13 PRs)

- BotFleet#1678 (cursor/agent-docs-linear-board-9300) / BotFleet#1684 (cursor/linear-board-agent-docs-2c23)
- BotFleet#1694 (fix-dsh-codex-logos-2) / BotFleet#1695 (fix-dsh-codex-logos-3)
- BotFleet#1711 (minimax/bf6-phase2-triage) / BotFleet#1712 (minimax/bf6-phase3-triage) / BotFleet#1713 (minimax/bf6-phase6-triage)
- CodeCaps#447 (zcode/muse-salvage-alerts-ui) / CodeCaps#449 (zcode/muse-salvage-ci-hardening)
- Socratic-Trade#5073 (cursor/ci-autofix-automation-27b6) / Socratic-Trade#5074 (cursor/ci-autofix-automation-bcfd)
- Socratic-Trade#5084 (cursor/retire-slack-operational-callers-) / Socratic-Trade#5085 (cursor/retire-slack-ops-callers-889f)

## WORK queue (263 unique-content drafts)

249 conflict with main (mstate DIRTY); 121 of those are green with zero unresolved threads (finish = resolve conflicts, then normal merge path).
Every WORK PR needs the per-PR worker rules (`scripts/pr-sweep prompt <owner/repo#n>`).

Execution is blocked on ZC's relay write route (relay_refused reason=unauthenticated, re-tested Nov 9; see AFC-658).
