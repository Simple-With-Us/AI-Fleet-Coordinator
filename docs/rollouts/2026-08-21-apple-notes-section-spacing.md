# Apple Notes section spacing

Owner: space sections apart; format Coding notes so they are readable on iPhone.

## Why

`apple-notes-coding.sh` dropped blank markdown lines.  Adjacent headings, paragraphs, and bullets rendered as one block in Notes.app.

## What landed

- MD converter emits `<div><br></div>` for blank lines, after headings/code/hr, after lists, and between consecutive list items.
- Skills (`docs/fleet-skills/apple-notes`, Grok `~/.grok/skills/apple-notes`, Desktop Monet pack) now have a Layout section: prefer `--html` with a spacer after every heading, paragraph, and bullet.
- FLEET-PROTOCOL + TEMPLATE-AGENTS match.
- Live helper copied to `~/apps/apple-notes-coding.sh`.
- Incident note `[FLEET, Grok] Mac total service degradation` rewritten as `--html` with spacers.

## Verify

```bash
python3 - <<'PY'
# extractor test is in the PR description; converter must include
# </li><div><br></div><li> for consecutive bullets
PY
```

Ran: sample markdown (`##` + three `-` items) produced 7 spacers and `</li><div><br></div><li>` between bullets.

## Follow-ups

Monet app library still needs the owner to re-upload `docs/fleet-skills/apple-notes` (or the Desktop pack) on the MONET login.  Grok already loads `~/.grok/skills/apple-notes`.
