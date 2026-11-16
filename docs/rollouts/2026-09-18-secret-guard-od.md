# Secret-guard Rule E: block od/hexdump/xxd of a loaded key

## Context & Objective

2026-09-17.  A Claude subagent loaded `SILICONFLOW_API_KEY` from the handoff
file correctly (quote-stripped, never echoed), then ran `od -c` on the variable
to look for stray quotes.  `od -c` prints every byte, so the live key landed in
the transcript.  Board `1e4a9d2f`.

The hook already denied `cat`/`grep`-style dumps of secret **files**.  It did
not deny byte-dump tools run against a **variable** loaded from one.

## Changes Made

- Tracked `scripts/hooks/secret-guard-pretooluse.py` (was live-only under
  `~/.claude/hooks/`).  Live copy updated in place.
- Rule A: `od` / `xxd` / `hexdump` / `hd` / `strings` / `base64` / `cut -c` of a
  secrets path are now `cat` for this purpose.
- Rule E: those tools, plus last-command `printf %s/%q/%b`, denied when the same
  pipeline references `$NAME` / `${NAME}` and NAME looks like KEY / TOKEN /
  SECRET / PASSWORD / PASSWD / DSN.  `${#NAME}` (length) is not a value ref.
- Tests: `scripts/fleet_rag/tests/test_secret_guard.py` (CI `unittest discover`).
- Canon: `FLEET-PROTOCOL.md` § Loaded-key byte dumps, `TEMPLATE-AGENTS.md`,
  onboarding hard rules, `docs/fleet-skills/secret-handoff`, housekeeper.
- Live machine copies: `~/apps/FLEET-PROTOCOL.md`, `~/.grok/GROK.md`,
  `~/.grok/rules/00-global.md`, `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
  `~/.gemini/config/AGENTS.md`, `~/.cursor/rules/fleet-standards.mdc`,
  `~/.claude/skills/secret-safety/SKILL.md`.

## Decisions & Trade-offs

- `printf '%s' "$TOKEN" | wc -c` and `printf … | curl -K -` stay allowed
  because printf is not the last pipeline stage.  Last-command printf of a key
  is denied.
- `echo "$KEY"` as a last command is **not** in Rule E.  That is a broader
  hole; this unit closes the incident class (byte dumps of a loaded var).
- Name match uses suffix / underscore parts (KEY, TOKEN, SECRET, …) so
  `KEYBOARD` / `TOKENIZER` / `HOME` are not treated as credentials.

## Verification State

`cd scripts && python3 -m unittest fleet_rag.tests.test_secret_guard -v` green
(53 tests).  Incident command `od -c <<< "$SILICONFLOW_API_KEY"` denies.

## Next Steps & Blockers

Product-repo `AGENTS.md` files pick up the TEMPLATE one-liner on their next
AGENTS edit.  Grok TUI has no Bash PreToolUse hook; the skill + GROK.md are
the Grok backstop.

## Zero-Code Findings

None.
