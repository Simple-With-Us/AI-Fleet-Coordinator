# Handoff-file grep trap (do not print KEY=value lines)

## Context & Objective

A session listed keys in `~/.secrets/global-api-keys` with
`grep '^[A-Z0-9_]+='`, which prints **values**.  Owner asked that agent
rules forbid the next seat from doing the same.

## Changes Made

- `FLEET-PROTOCOL.md` — new § Handoff-file grep trap (forbidden greps, names-only
  `grep -oE`, one-key extract).
- `TEMPLATE-AGENTS.md` — one-liner under Secrets.
- `docs/ONBOARDING-NEW-AGENT.md` hard rule 6 and `docs/ONBOARDING-NEW-APP.md`
  hard rule 5 now name the trap.
- Live machine copies (not in this repo): `~/apps/FLEET-PROTOCOL.md`,
  `~/.grok/GROK.md`, `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
  `~/.gemini/config/AGENTS.md`, `~/.cursor/rules/fleet-standards.mdc`,
  `~/.claude/skills/secret-safety/SKILL.md`.

## Decisions & Trade-offs

- Allowed name listing must use `grep -oE '^[A-Z][A-Z0-9_]*'` so the match
  cannot include `=value`.
- This change is preventative only.

## Verification State

Docs-only.  Text review of the trap section in FLEET-PROTOCOL vs the live
`~/apps/FLEET-PROTOCOL.md` copy (sections match).

## Next Steps & Blockers

Socratic.Trade `AGENTS.md` is landing separately on
`grok/secret-file-grep-ban`.  Other product repos should pick up the
TEMPLATE one-liner on their next AGENTS edit.

## Zero-Code Findings

None.
