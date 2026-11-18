# 2026-09-04 — MiniMax Slack tag is MM; DeepSeek Harness is DSH

Owner ruling 2026-09-04: anywhere a MiniMax *acronym* is used (Slack, `AGENT_SEAT` /
`AGENT_TAG`, seat tables, skill identity), it is **`MM`**, not `MINIMAX`.  DeepSeek
Harness is noted as **`DSH`**.  On Slack: `[MM]`, `[DSH]`.

## Why

`MINIMAX` and `DEEPSEEK` were full-name Slack tags.  The owner wants the same short
acronym style as `AG`, `MM`, `DSH`.  DeepSeek Harness needed a distinct tag from a
DeepSeek *model* running inside Cursor (that spawn stays `[CURSOR]`).

## What changed

- `fleet-apps.json` seat tags: `MINIMAX` → `MM`, `DEEPSEEK` → `DSH`.  Notes names:
  `MiniMax`, `DeepSeek Harness`.  Branch prefixes stay `minimax/` and `deepseek/`.
  Worktree suffixes stay `minimax` and `deepseek`.
- `FLEET-PROTOCOL.md` (repo + live `~/apps/FLEET-PROTOCOL.md`) seat table, availability list,
  and fleet-skills home dirs.
- Skill identity (`scripts/fleet_skill_identity.py`) and Monet catalog
  (`docs/fleet-skills/fleet-coordination/SKILL.md`).  `python3 scripts/install-fleet-skills.py`
  regenerates `skills/`, `docs/fleet-skills/by-seat/`, and platform homes.
- Onboarding, recall access check, TEMPLATE-AGENTS, README, Apple Notes protocol,
  fleet RAG `SEAT_ALIASES` (`minimax`/`mm` → `MM`, `deepseek`/`dsh` → `DSH`).
- Platform custom instructions: `~/.minimax/memory/user.md` pins `AGENT_SEAT=MM`;
  GROK / Claude / Codex / Gemini / Cursor rules carry the Slack-tag stanza.

## What did not change

- Product names: MiniMax, DeepSeek, `mmx`, `dsh`.
- Vendor env vars: `MINIMAX_API_KEY`, `DEEPSEEK_API_KEY`.
- Historical effort-log rows and PR titles (those record who signed at the time).
- In-flight MiniMax BotFleet work still on branch `minimax/…` — they should start
  posting `[MM]` on Slack.

## Verification

```bash
python3 scripts/test_fleet_skill_identity.py
python3 scripts/check-fleet-registry.py
```

Board `f9df420d`.  Branch `grok/mm-dsh-acronyms`.
