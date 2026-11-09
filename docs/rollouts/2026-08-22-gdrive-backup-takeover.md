# 2026-08-22 — Fleet coordinator takes over Drive source backups

## Why

Personal-Site `.github/workflows/backup-repos.yml` ran daily since 2026-08-13 and
failed every run.  `GH_BACKUP_TOKEN` was never set, so `GH_TOKEN` was empty and
`gh auth status` exited 1.  The original workflow was named "Backup GitHub Repos
to Google Drive" but the Drive upload step was a comment.  Artifacts never
landed.  New apps (ContactLogo, Autorotate, fleet-ops) were easy to miss in a
hardcoded list.

AI-Fleet-Coordinator already had `scripts/backup-fleet-to-gdrive.py` as an
on-demand helper.  Today's Drive folder
`Website & App Source Backups - 2026-08-22` is from that script.  Owner asked
to keep Drive backups working for new apps unless this repo takes over.

## What landed

- Backup script reads `fleet-apps.json` and also any other git checkout
  directly under `~/Code` (same skip list as code-main-keeper).
- Daily launchd `com.jay.fleet-gdrive-backup` at 06:00 local.  Live install
  `~/apps/fleet-gdrive-backup/`.
- GitHub Actions artifact backup moved here (`.github/workflows/backup-repos.yml`).
  Clones with anonymous HTTPS so an empty `GH_TOKEN` cannot poison `gh`.
  90-day artifacts are the secondary copy.  Drive is canonical.
- `onboard-new-app.sh` / `docs/ONBOARDING-NEW-APP.md`: adding the
  `fleet-apps.json` row is enough.  No hardcoded backup list to patch.
- `check-fleet-registry.py` fails if the backup script or GHA stops reading
  `fleet-apps.json`.
- `mac-process-watch` expects the new scheduled label.

## Verification

```bash
python3 scripts/backup-fleet-to-gdrive.py --list
python3 scripts/check-fleet-registry.py
launchctl print "gui/$(id -u)/com.jay.fleet-gdrive-backup" | head
```

## Not this PR

- Full ContactLogo fleet onboard (board file already exists; not in
  `fleet-apps.json` yet).  Drive still picks it up via `~/Code/ContactLogo`.
- Personal-Site hosting on personal Vercel (sibling PS PR).
