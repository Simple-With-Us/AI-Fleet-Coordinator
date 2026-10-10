#!/bin/bash
# infisical-secrets-sync.sh — one-way sync of designated Infisical secrets into
# ~/.secrets/global-api-keys. Infisical is the source of truth; this file is a
# derived convenience copy. Safe to run on a schedule via LaunchAgent.
# Requires: Infisical CLI authenticated (login keychain) in the GUI session.
# To add a key: append "LOCAL_KEY|PROJECT_ID|ENV|SECRET_NAME" to MAP below.
#
# Tracked source of ~/bin/infisical-secrets-sync.sh (LaunchAgent com.jay.infisical-secrets-sync,
# plist in scripts/launchd/).  Install:  install -m 755 scripts/infisical-secrets-sync.sh ~/bin/
#
# Prod is the only Infisical environment (owner 2026-10-10).  ENV in MAP must be prod;  the
# script refuses any other value, and always passes --env because the CLI's own default is dev.
# The value is read with --plain --silent (just the value, no table).  The old code took the
# last field of the default table, which is now a 3-byte box-drawing border, so the ticks since
# 2026-09-29 logged "implausible (got_len=3)" even though the secret exists.
#
# Test hooks (hermetic tests only):  SYNC_INFISICAL_BIN, SYNC_FILE and SYNC_LOG.
set -u
INFISICAL="${SYNC_INFISICAL_BIN:-/opt/homebrew/bin/infisical}"
FILE="${SYNC_FILE:-$HOME/.secrets/global-api-keys}"
LOG="${SYNC_LOG:-$HOME/.secrets/infisical-sync.log}"
MAP=(
  "CLOUDFLARE_API_TOKEN|18f563a3-9c88-454c-96eb-28fc9678f3ba|prod|CLOUDFLARE_API_TOKEN"
  # fleet-work-backup (scripts/fleet-work-backup):  the restic password and a bucket-scoped B2 key,
  # all in project "AI Fleet Coordinator".  Values must be 16+ characters with no whitespace and no
  # backslash (the plausibility check below, and awk -v, need that);  a urlsafe token satisfies it.
  # Until a secret exists in Infisical the tick logs one WARN for it and changes nothing.
  "RESTIC_FLEET_WORK_PASSWORD|9bf7417a-fbbb-42ca-870c-2b45207233f5|prod|RESTIC_FLEET_WORK_PASSWORD"
  "B2_FLEET_WORK_KEY_ID|9bf7417a-fbbb-42ca-870c-2b45207233f5|prod|B2_FLEET_WORK_KEY_ID"
  "B2_FLEET_WORK_APPLICATION_KEY|9bf7417a-fbbb-42ca-870c-2b45207233f5|prod|B2_FLEET_WORK_APPLICATION_KEY"
)
log() { printf "%s %s\n" "$(date "+%F %T")" "$*" >> "$LOG"; }
strip_ansi() { sed "s/$(printf "\033")\[[0-9;?]*[a-zA-Z]//g"; }
upsert() {
  local key="$1" value="$2" tmp
  tmp=$(mktemp) || return 1
  if grep -q "^${key}=" "$FILE" 2>/dev/null; then
    awk -v k="$key" -v v="$value" 'BEGIN{done=0} index($0,k"=")==1 && !done {$0=k"="v; done=1} {print}' "$FILE" > "$tmp" && mv "$tmp" "$FILE"
  else
    printf "%s=%s\n" "$key" "$value" >> "$FILE"
    rm -f "$tmp"
  fi
  chmod 600 "$FILE"
}
[ -f "$FILE" ] || { log "ERROR: $FILE not found"; exit 1; }
cp "$FILE" "$FILE.bak-sync-$(date +%Y%m%d-%H%M%S)"
ls -t "$FILE".bak-sync-* 2>/dev/null | tail -n +6 | xargs -r rm -f
changed=0
for entry in "${MAP[@]}"; do
  key="${entry%%|*}"; rest="${entry#*|}"
  proj="${rest%%|*}"; rest="${rest#*|}"
  env="${rest%%|*}"; name="${rest#*|}"
  if [ "$env" != "prod" ]; then
    log "ERROR: refusing env '$env' for $key; prod is the only Infisical environment"; continue
  fi
  val=$("$INFISICAL" secrets get "$name" --projectId="$proj" --env="$env" --plain --silent 2>/dev/null | strip_ansi | tail -n 1)
  # sanity: value must be a plausible secret (long, no spaces); never write fragments
  case "$val" in *[[:space:]]*) val="" ;; esac
  if [ -z "$val" ] || [ "${#val}" -lt 16 ] || [ "$val" = "$name" ]; then
    log "WARN: fetch failed or implausible for $key (got_len=${#val})"; continue
  fi
  cur=$(grep "^${key}=" "$FILE" 2>/dev/null | cut -d= -f2- | tail -1)
  if [ "$cur" != "$val" ]; then upsert "$key" "$val"; log "updated $key (new_len=${#val})"; changed=1; fi
done
[ "$changed" -eq 0 ] && log "no changes"
exit 0
