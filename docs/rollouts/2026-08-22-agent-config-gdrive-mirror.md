# Fleet agent config mirror to Google Drive

## Summary

Google Drive for desktop cannot add `~/.Gemini`, `~/.cursor`, `~/.claude`, or `~/.grok`
to native sync.  Added `scripts/sync-fleet-agent-config-to-gdrive.py` to mirror
fleet skills and Cursor rules into My Drive and hooked it into the existing daily
`com.jay.fleet-gdrive-backup` job.

## Drive layout

- `My Drive/fleet-agent-config/` — per-seat mirrors (`gemini/skills`, `cursor/skills`,
  `cursor/skills-cursor`, `cursor/rules`, `claude/skills`, `grok/skills`)
- `My Drive/fleet-skills/` — refreshed from `docs/fleet-skills/` (upload zips for Claude.app)

## Verification

```bash
python3 scripts/sync-fleet-agent-config-to-gdrive.py --list
python3 scripts/sync-fleet-agent-config-to-gdrive.py
ls "/Users/jay/Google Drive/My Drive/fleet-agent-config"
```

Live copy: `~/apps/fleet-gdrive-backup/sync-fleet-agent-config-to-gdrive.py`; `run.sh` runs
both repo backup and agent-config mirror.
