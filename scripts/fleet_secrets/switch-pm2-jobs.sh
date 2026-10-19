#!/usr/bin/env bash
# Move the three pm2 jobs that were reverted to the flat secrets file onto `fleet-secrets` (OPS-818).
# Run it as the owner (no sudo), AFTER seed-system-keychain.sh has put the machine identity in the
# System keychain:
#
#   bash scripts/fleet_secrets/switch-pm2-jobs.sh            # prove, then switch
#   bash scripts/fleet_secrets/switch-pm2-jobs.sh --probe    # only prove the pm2 path, change nothing
#
# Step 1, proof:  `fleet-secrets doctor` is run INSIDE a pm2 one-shot (pm2 starts it from the launchd
# System session, like the real jobs), and its log must say `ok: true` and `auth_mode: system-keychain`.
# The log holds booleans and counts only.  If the proof fails, nothing is changed.
#
# Step 2, per job (mac-collab-litestream, vision-worker, mac-collab):  back the live file up to
# <file>.bak-ops818-pm2, install the fleet-secrets version, `pm2 restart <name>`, wait ~20 s, and
# check `pm2 jlist` for that name only.  If it is not online and steady, the backup is put back and
# the job restarted.  No environment is ever printed.
#
# Sources of the fleet-secrets versions (first that exists):
#   mac-collab-litestream  ~/apps/mac-collab/start_litestream.sh.new-ops818, then
#                          scripts/mac-collab/start_litestream.sh in this checkout
#   vision-worker          ~/vision-worker/run-vision-worker.sh.new-ops818
#   mac-collab             ~/apps/mac-collab/mac-collab-server.py.new-ops818;  if none exists and the live
#                          file already calls `fleet-secrets names`, only a restart is done.  The checkout's
#                          scripts/mac-collab/mac-collab-server.py is NOT installed:  the live server has
#                          moved on from it.
# Environment:  SWITCH_WAIT (seconds, default 20), FLEET_SECRETS_BIN, PM2_BIN.
set -uo pipefail

FS="${FLEET_SECRETS_BIN:-$HOME/.local/bin/fleet-secrets}"
PM2="${PM2_BIN:-$(command -v pm2 || echo /usr/local/bin/pm2)}"
WAIT="${SWITCH_WAIT:-20}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE_NAME="fs-doctor-probe"
probe_only=false
[ "${1:-}" = "--probe" ] && probe_only=true

say() { echo "switch-pm2-jobs: $*"; }
fail() { say "$*" >&2; exit "${2:-1}"; }

[ -x "$FS" ] || fail "fleet-secrets not installed at $FS (run scripts/install-fleet-secrets.sh)" 2
[ -x "$PM2" ] || fail "pm2 not found" 2

# --- pm2 helpers (never print env) -----------------------------------------------------------------
# "<status> <restart_time>" for every process with that exact name, one per line.
pm2_state() {
  "$PM2" jlist 2>/dev/null | python3 -c '
import json, sys
text = sys.stdin.read()
start = text.find("[")
while start != -1:
    try:
        procs = json.loads(text[start:])
        break
    except ValueError:
        start = text.find("[", start + 1)
else:
    procs = []
for p in procs:
    if p.get("name") == sys.argv[1]:
        env = p.get("pm2_env", {})
        print(env.get("status", "unknown"), env.get("restart_time", 0))
' "$1"
}

# 0 when every process of that name is online and the restart count is steady over a few seconds.
pm2_steady() {
  local name="$1" a b
  a="$(pm2_state "$name")"
  [ -n "$a" ] || return 1
  if echo "$a" | grep -qv '^online '; then return 1; fi
  sleep 5
  b="$(pm2_state "$name")"
  [ "$a" = "$b" ]
}

# --- step 1: prove the System-session path ---------------------------------------------------------
probe_dir="$(mktemp -d)"
trap 'rm -rf "$probe_dir"; "$PM2" delete "$PROBE_NAME" >/dev/null 2>&1 || true' EXIT
"$PM2" delete "$PROBE_NAME" >/dev/null 2>&1 || true
"$PM2" start "$FS" --name "$PROBE_NAME" --no-autorestart --interpreter none \
  --output "$probe_dir/out.log" --error "$probe_dir/err.log" -- doctor >/dev/null 2>&1 \
  || fail "could not start the pm2 probe"
for _ in $(seq 1 30); do
  grep -q '^ok: ' "$probe_dir/out.log" 2>/dev/null && break
  sleep 2
done
probe_ok=false; probe_mode="none"
grep -q '^ok: true$' "$probe_dir/out.log" 2>/dev/null && probe_ok=true
probe_mode="$(sed -n 's/^auth_mode: //p' "$probe_dir/out.log" 2>/dev/null | head -1)"
say "pm2 probe: ok=$probe_ok auth_mode=${probe_mode:-none}"
grep -E '^(keychain_|system_keychain_|session_login_valid|fetch_names_ok|names_count|failure_reason)' "$probe_dir/out.log" 2>/dev/null | sed 's/^/  /'
if [ "$probe_ok" != true ] || [ "$probe_mode" != "system-keychain" ]; then
  fail "the pm2 System-session probe did not pass;  seed the System keychain first (seed-system-keychain.sh) and nothing was changed" 3
fi
"$PM2" delete "$PROBE_NAME" >/dev/null 2>&1 || true
$probe_only && { say "probe passed;  --probe given, no job was changed"; exit 0; }

# --- step 2: switch each job -----------------------------------------------------------------------
results=()

# switch NAME LIVE_FILE SOURCE_FILE_OR_EMPTY [restart_only_reason]
switch_job() {
  local name="$1" live="$2" src="$3" restart_only="${4:-}" backup="$2.bak-ops818-pm2" installed=false
  if ! [ -f "$live" ]; then results+=("$name: SKIPPED (live file missing)"); return; fi
  if [ -n "$src" ]; then
    if [ -e "$backup" ]; then
      say "$name: keeping the existing backup $backup (a rerun must not overwrite the original)"
    else
      cp -p "$live" "$backup" || { results+=("$name: SKIPPED (could not back up)"); return; }
      say "$name: backed up the live file to $backup"
    fi
    if ! { cp "$src" "$live.new-switch" && chmod "$(stat -f '%Lp' "$live")" "$live.new-switch" \
        && mv "$live.new-switch" "$live"; }; then
      rm -f "$live.new-switch"; results+=("$name: FAILED (install)"); return
    fi
    installed=true
  elif [ -n "$restart_only" ]; then
    say "$name: $restart_only"
  else
    results+=("$name: SKIPPED (no fleet-secrets version found)"); return
  fi
  "$PM2" restart "$name" >/dev/null 2>&1
  sleep "$WAIT"
  if pm2_steady "$name"; then
    results+=("$name: online$($installed && echo ", switched" || echo ", restarted only")")
    return
  fi
  if $installed; then
    cp -p "$backup" "$live" && "$PM2" restart "$name" >/dev/null 2>&1
    sleep "$WAIT"
    if pm2_steady "$name"; then
      results+=("$name: NOT ONLINE after switch;  RESTORED the backup and it is online again")
    else
      results+=("$name: NOT ONLINE after switch;  restored the backup but it is STILL NOT online:  look at pm2 logs $name")
    fi
  else
    results+=("$name: NOT ONLINE after restart (file unchanged):  look at pm2 logs $name")
  fi
}

lite_src=""
for c in "$HOME/apps/mac-collab/start_litestream.sh.new-ops818" "$here/../mac-collab/start_litestream.sh"; do
  [ -f "$c" ] && { lite_src="$c"; break; }
done
collab_src=""; collab_note=""
if [ -f "$HOME/apps/mac-collab/mac-collab-server.py.new-ops818" ]; then
  collab_src="$HOME/apps/mac-collab/mac-collab-server.py.new-ops818"
elif grep -q 'fleet-secrets' "$HOME/apps/mac-collab/mac-collab-server.py" 2>/dev/null; then
  collab_note="the live file already calls fleet-secrets;  restart only so it is proven under the System-session identity"
fi

switch_job mac-collab-litestream "$HOME/apps/mac-collab/start_litestream.sh" "$lite_src"
switch_job vision-worker "$HOME/vision-worker/run-vision-worker.sh" "$HOME/vision-worker/run-vision-worker.sh.new-ops818"
switch_job mac-collab "$HOME/apps/mac-collab/mac-collab-server.py" "$collab_src" "$collab_note"

echo "---"
bad=0
for r in "${results[@]}"; do echo "$r"; case "$r" in *online*switched*|*online*restarted*) ;; *) bad=1;; esac; done
exit "$bad"
