# Fleet-Wide PR Title Header Rule (OPS-1122)

## Context & Objective

Owner ruling, November 26, 2026: every PR title must begin with the compact lettered fleet header for the session that opens the PR -- `[ZC:` + three U+200A hair spaces + `RR.3.S]`, then one ASCII space, then the human title -- never omitted, never stacked under a formatter prefix.  PR descriptions and comments keep carrying the same header as their first line.  This change codifies the rule in the canonical handbook and the fleet `AGENTS.md` copies, and makes it fleet-wide for every multithreaded agent platform (Claude Code, Codex, Cursor, Antigravity, Grok, ZCode, and the rest).

## Changes Made

- `FLEET-PROTOCOL.md`: new `### GitHub PR Headers` subsection under `## Posting` (before `### Precedence`) defining the compact PR header, the title-first rule, the hair-space arithmetic, and the held/Kody forms.
- `AGENTS.md`: the `## Fleet post headers (owner ruling)` paragraph now states the title-first rule and that the opening session supplies the letter.
- `TEMPLATE-AGENTS.md`: the same two-sentence clarification mirrored into the fleet AGENTS template.
- `STATUS.md`: this stanza.

## Decisions & Trade-offs

I)  The new subsection lives in `## Posting` next to `### Message Shape` because `FLEET-PROTOCOL.md` previously defined the Zulip post header but never the GitHub PR header; the repo-level `AGENTS.md` sections were the only place it existed.  II)  Mirrors to the twelve fleet repos under the code root land as separate per-repo PRs off `origin/main`, each with its own lane.

## Verification State

Docs-only change.  No code gate applies to this repository; the diff was byte-checked (U+200A hair spaces only inside header examples, sentence gaps as U+00A0 + space, zero U+202F) with `python3 -I` and reviewed via `git diff`.

## Next Steps & Blockers

I)  Merge this handbook PR, then the per-repo `AGENTS.md` mirrors.  II)  The per-platform `fleet-coordination` SKILL.md copies under `.zcode/skills/`, `.cursor/skills/`, `.codex/skills/`, `.claude/skills/` and `.grok/skills/` still carry the older wording; refresh them in a follow-up or let the next template sync pick them up.
