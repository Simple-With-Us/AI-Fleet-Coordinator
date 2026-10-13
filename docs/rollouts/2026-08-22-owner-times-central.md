# Owner-facing times are Central Time (2026-08-22)

**Superseded 2026-10-05.**  Keep the conversion.  Drop the zone abbreviation.
Say `3:15am`, not `3:15am CT`.  Name a zone only when citing UTC.  Canonical:
`AGENT-SYNC.md` § Timestamps.

Owner: agents kept saying `00:00 UTC`.  Convert.  Binding for every seat,
every platform, including chat.

Two spaces between sentences in this file.

## Conversion that started this

- `00:00 UTC` during CDT (March–November) is **7:00 PM CT the previous
  calendar day**.
- `00:00 UTC` during CST is **6:00 PM CT the previous calendar day**.
- Example: `2026-08-23T00:00:00Z` is Sat, Aug 22, 2026 at 7:00 PM CT.

## Policy

When you tell the owner a time, write America/Chicago labeled `CT` / `CDT` /
`CST`.  Never UTC-only in owner-facing prose.  UTC may follow in parentheses
after the Central stamp.  Product UI stays the viewer's timezone.  Wire
formats (JSON, logs, DB) stay UTC.

## Files

- `AGENT-SYNC.md` § Timestamps (live `~/apps/AGENT-SYNC.md` + this repo)
- `FLEET-UI-COPY.md`
- `docs/ONBOARDING-NEW-AGENT.md` hard rule 8
- `TEMPLATE-AGENTS.md`
- `docs/fleet-skills/owner-copy/SKILL.md` and installed copies under
  `~/.grok/skills`, `~/.claude/skills`, `~/.cursor/skills`
- `.grok/skills/fleet-coordination/SKILL.md`

## Verify

```bash
python3 -c 'from datetime import datetime, timezone; from zoneinfo import ZoneInfo
print(datetime(2026,8,23,0,0,tzinfo=timezone.utc).astimezone(ZoneInfo("America/Chicago")).strftime("%a, %b %-d, %Y at %-I:%M %p %Z"))'
# Sat, Aug 22, 2026 at 7:00 PM CDT
```
