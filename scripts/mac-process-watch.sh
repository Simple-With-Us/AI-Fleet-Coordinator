#!/bin/bash
# Check expected Mac always-on jobs.  Restart the ones that are down.
# Scheduled; must not stay up.  Writes only names and status - never env
# or secret values.
#
#   bash ~/apps/mac-process-watch.sh
#   MAC_PROCESS_WATCH_RESTART=0 bash ~/apps/mac-process-watch.sh   # log only
#   tail -f ~/Library/Logs/mac-process-watch.log
#
# launchd: com.jay.mac-process-watch every 120s.
# Tracked copy: AI-Fleet-Coordinator/scripts/mac-process-watch.sh
# Live copy launchd runs: ~/apps/mac-process-watch.sh
#
# Restarts (always-on only):
#   - pm2 daemon dead or 3+ jobs missing -> pm2 resurrect ONLY when
#     ~/.pm2/dump.pm2 lists every expected job.  A short/poisoned dump
#     must not be resurrected.  Incomplete dump -> start from ecosystem
#     then `pm2 save`.
#   - one/two pm2 jobs down -> pm2 restart, or pm2 start ecosystem --only
#   - pm2 jlist hangs >8s -> kill stray CLIs.  If God pid is still alive,
#     skip inventory (slow RPC under CPU load).  Only kill God + restore_bulk
#     when the pidfile is dead.  Killing a live God orphans listeners and
#     the resurrect crash-loops on AddrInUse.
#     (dump-complete resurrect OR ecosystem start — never poison dump)
#   - grok-leader down while ~/.grok/leader.sock is bound -> SKIP (do not
#     start a second leader; TUI/Grok Code may hold the lock).  Use
#     /usr/sbin/lsof (LaunchAgent PATH historically omitted /usr/sbin, so
#     bare `lsof` was a no-op and watch treated lock-held as DOWN).
#   - grok-leader status=errored while lock-held -> pm2 stop (not restart)
#     so the job is stopped instead of a 355-restart storm.
#   - local /health not 200 for mac-collab/xcode-health/agent-sync
#     -> pm2 restart that job
#   - shellular ioreg-missing / retry-without-Connected -> bounce pid
#   - shellular process up but relay 1006/handshake-fail -> kill pid (God autorestarts)
#   - launchd always-on not-loaded -> bootstrap plist (if not disabled)
#   - launchd always-on loaded, no pid -> kickstart (not -k)
#   - com.jay.botfleet-server is UP when 127.0.0.1:8799 /health is 200
#     (BotFleet.app often owns the harness; launchd must not steal the port)
# Scheduled / on-trigger (must stay loaded, must NOT stay running):
#   - if not-loaded and not disabled -> bootstrap so the timer can fire
#   - idle (no pid) is correct -- do not kickstart
#   - never bootstrap com.jay.ios-ship-now (RunAtLoad would ship TestFlight)
#   - never bootstrap com.PM2 (LaunchOnlyOnce)
#   - steal stale run-locks for disk-janitor / merge-shepherd (>2h)
# Does NOT touch: disabled labels (com.jay.shellular, com.jay.imessage-grok,
# retired launchd), vendor timers, com.PM2, cloudflared (root).
# Backoff: at most 4 restarts per key per hour.  After that, log SKIP.

set -uo pipefail

LOG="${HOME}/Library/Logs/mac-process-watch.log"
CMDLOG="${HOME}/Library/Logs/mac-process-watch.cmd.log"
STATE="${HOME}/Library/Logs/mac-process-watch.state"
LOCKDIR="${HOME}/Library/Logs/mac-process-watch.lockdir"
ECOSYSTEM="${HOME}/apps/pm2-ecosystem.config.cjs"
DUMP="${HOME}/.pm2/dump.pm2"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
NOW="$(date +%s)"
RESTART="${MAC_PROCESS_WATCH_RESTART:-1}"
MAX_RESTARTS=4
WINDOW_SEC=3600
LOCK_STALE_SEC=180
LSOF="${LSOF:-/usr/sbin/lsof}"
mkdir -p "$(dirname "$LOG")"

# True when a live process (usually this TUI) already owns the leader
# socket.  Bare `lsof` is /usr/sbin/lsof on macOS; launchd PATH without
# /usr/sbin makes `command -v lsof` fail, which is how the skip became
# DOWN + restart-storm on 2026-08-21.
grok_leader_lock_held() {
  local sock="${HOME}/.grok/leader.sock"
  [ -S "$sock" ] || return 1
  if [ -x "$LSOF" ] && "$LSOF" "$sock" >/dev/null 2>&1; then
    return 0
  fi
  if command -v lsof >/dev/null 2>&1 && lsof "$sock" >/dev/null 2>&1; then
    return 0
  fi
  pgrep -f '/[.]grok/bin/grok .* leader' >/dev/null 2>&1
}

# Deprecated / off-Mac (do not watch or resurrect): scout, senate-relay,
# senate-tunnel, residential-proxy, Congress/Socratic/Usage mac-xcode runners.
expect_pm2=(
  shellular
  agent-sync-push
  code-main-keeper
  vision-worker
  xcode-health
  cursor-slack-sync
  agy-acp
  grok-leader
  grok-acp
  mac-collab
  mac-collab-sync
  mac-collab-writeback
  mac-collab-litestream
  clutch-web
  seat-mcp
  botfleet-mcp
)

# "label plist-basename"  (plists live in ~/Library/LaunchAgents)
expect_launchd=(
  "com.jay.claude-remote-control com.jay.claude-remote-control.plist"
  "com.jay.slack-agent-inbox com.jay.slack-agent-inbox.plist"
  "homebrew.mxcl.moshi-hook homebrew.mxcl.moshi-hook.plist"
  "com.jay.botfleet-server com.jay.botfleet-server.plist"
)

# Timers / calendar / interval jobs.  Must be loaded so they can fire.
# Idle (no pid) is expected.  Do not add ios-ship-now or com.PM2.
expect_scheduled=(
  "com.jay.disk-janitor com.jay.disk-janitor.plist"
  "com.jay.merge-shepherd com.jay.merge-shepherd.plist"
  "com.jay.mac-process-watch com.jay.mac-process-watch.plist"
  "com.jays.mac-server-watchdog com.jays.mac-server-watchdog.plist"
  "com.jays.antigravity-usage-collector com.jays.antigravity-usage-collector.plist"
  "com.jays.subscription-quota-collector com.jays.subscription-quota-collector.plist"
  "com.jay.mac-cleanup com.jay.mac-cleanup.plist"
  "com.jay.mac-resource-watch com.jay.mac-resource-watch.plist"
  "com.jay.provider-knob-sync com.jay.provider-knob-sync.plist"
  "com.jay.fleet-gdrive-backup com.jay.fleet-gdrive-backup.plist"
  "com.jay.grok-idle-unload com.jay.grok-idle-unload.plist"
)

# Program paths that must exist for a trigger to succeed.
expect_files=(
  "${HOME}/apps/mac-process-watch.sh"
  "${HOME}/apps/pm2-ecosystem.config.cjs"
  "${HOME}/.claude-disk-janitor/janitor.sh"
  "${HOME}/.claude-merge-shepherd/run.sh"
  "${HOME}/.claude-merge-shepherd/merge-shepherd.sh"
  "${HOME}/Code/Usage-Monitor/scripts/ops/mac-server-watchdog.sh"
  "${HOME}/Code/Usage-Monitor/scripts/antigravity-usage-collector.mjs"
  "${HOME}/Code/Usage-Monitor/scripts/subscription-quota-collector.mjs"
  "${HOME}/apps/mac-auto-cleanup.sh"
  "${HOME}/apps/mac-resource-watch.py"
  "${HOME}/apps/mac-resource-watch.sh"
  "${HOME}/apps/check-hetzner-cx43.sh"
  "${HOME}/Code/Socratic.Trade/scripts/sync-provider-knobs.sh"
  "${HOME}/apps/ios-fleet/ship-now-gui.sh"
  "${HOME}/apps/slack-agent-listen.py"
  "${HOME}/apps/slack-agent-listen-start.sh"
  "${HOME}/apps/grok-acp-runtime/start.sh"
  "${HOME}/apps/grok-acp-runtime/grok-idle-unload.py"
  "${HOME}/apps/clutch-runtime/scripts/start-web.sh"
  "${HOME}/apps/clutch-runtime/scripts/serve-tailscale.sh"
  "${HOME}/apps/fleet-gdrive-backup/run.sh"
  "${HOME}/apps/fleet-gdrive-backup/backup-fleet-to-gdrive.py"
  "${HOME}/apps/fleet-gdrive-backup/sync-fleet-agent-config-to-gdrive.py"
  "${HOME}/apps/botfleet-server-start.sh"
)

log() {
  echo "$STAMP  $*" >>"$LOG"
}

# Return 0 if dump.pm2 lists every name in "$@".  1 = missing file,
# unreadable JSON, empty need-list, or any expected name absent.
# pm2 save writes a JSON array of process objects (name and/or pm2_env.name).
dump_covers_expected() {
  dump_path="$1"
  shift
  [ -f "$dump_path" ] || return 1
  [ "$#" -gt 0 ] || return 1
  python3 -c '
import json, sys
path = sys.argv[1]
need = sys.argv[2:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    raise SystemExit(1)
if isinstance(data, dict):
    apps = data.get("apps") or data.get("processes") or []
elif isinstance(data, list):
    apps = data
else:
    raise SystemExit(1)
names = set()
for p in apps:
    if not isinstance(p, dict):
        continue
    n = p.get("name")
    if n:
        names.add(n)
    env = p.get("pm2_env")
    if isinstance(env, dict):
        en = env.get("name")
        if en:
            names.add(en)
missing = [n for n in need if n not in names]
raise SystemExit(0 if not missing else 1)
' "$dump_path" "$@"
}

# Test hook: does not take the watch lock or talk to pm2.
#   bash mac-process-watch.sh --dump-covers <dump.json> name [name...]
if [ "${1:-}" = "--dump-covers" ]; then
  shift
  dump_covers_expected "$@"
  exit $?
fi

# mkdir lock.  Steal if older than LOCK_STALE_SEC (crash leftover).
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  lock_mtime="$(stat -f %m "$LOCKDIR" 2>/dev/null || echo 0)"
  if [ $((NOW - lock_mtime)) -gt "$LOCK_STALE_SEC" ]; then
    rmdir "$LOCKDIR" 2>/dev/null || true
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
      exit 0
    fi
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# Return 0 if this key may restart; increment count.  1 = backoff.
allow_restart() {
  key="$1"
  start=""
  count=0
  tmp="${STATE}.tmp.$$"
  : >"$tmp"
  if [ -f "$STATE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      [ -z "$line" ] && continue
      k="${line%% *}"
      rest="${line#* }"
      s="${rest%% *}"
      c="${rest#* }"
      if [ "$k" = "$key" ]; then
        start="$s"
        count="$c"
      else
        printf '%s\n' "$line" >>"$tmp"
      fi
    done <"$STATE"
  fi
  if [ -z "$start" ] || [ $((NOW - start)) -ge "$WINDOW_SEC" ]; then
    start="$NOW"
    count=0
  fi
  if [ "$count" -ge "$MAX_RESTARTS" ]; then
    printf '%s %s %s\n' "$key" "$start" "$count" >>"$tmp"
    mv "$tmp" "$STATE"
    return 1
  fi
  count=$((count + 1))
  printf '%s %s %s\n' "$key" "$start" "$count" >>"$tmp"
  mv "$tmp" "$STATE"
  return 0
}

try_restart() {
  key="$1"
  shift
  if [ "$RESTART" != "1" ]; then
    log "SKIP  $key  restart=off"
    return 1
  fi
  if ! allow_restart "$key"; then
    log "SKIP  $key  backoff=${MAX_RESTARTS}/${WINDOW_SEC}s"
    return 1
  fi
  {
    echo "----- ${STAMP} ${key} -----"
    "$@"
  } >>"$CMDLOG" 2>&1
  if [ $? -eq 0 ]; then
    log "RESTART  $key  ok"
    return 0
  fi
  log "FAIL  $key  cmd-failed"
  return 1
}


# Timed pm2 jlist.  A wedged RPC makes `pm2 jlist` hang forever and the
# 120s watch never restarts anything.  8s is enough for a healthy God.
pm2_jlist_timed() {
  python3 - <<'PY'
import subprocess, sys
try:
    r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=8)
except subprocess.TimeoutExpired:
    sys.exit(124)
sys.stdout.write(r.stdout)
sys.exit(r.returncode)
PY
}

pm2_kill_stray_cli() {
  python3 - <<'PY'
import os, subprocess
try:
    out = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
except Exception:
    raise SystemExit(0)
for line in out.splitlines():
    line = line.strip()
    if "pm2" not in line:
        continue
    if "God Daemon" in line or "PM2 v" in line:
        continue
    if not any(x in line for x in ("pm2 jlist", "pm2 list", "pm2 status", "pm2 resurrect")):
        continue
    pid = int(line.split(None, 1)[0])
    try:
        os.kill(pid, 15)
    except OSError:
        pass
PY
}

# Shellular can stay "online" in pm2 while its cloud relay is dead
# (1006 / ECONNRESET).  Bounce the node pid so God autorestarts it.
shellular_relay_dead() {
  python3 - <<'PY'
import re, time
from pathlib import Path
err = Path.home() / ".pm2/logs/shellular-error.log"
out = Path.home() / ".pm2/logs/shellular-out.log"
now = time.time()

def tail(p, n=80):
    if not p.exists():
        return []
    return p.read_text(errors="ignore").splitlines()[-n:]

err_lines = tail(err)
out_lines = tail(out)

# 2026-08-21: ecosystem PATH omitted /usr/sbin so ioreg failed and the
# process stayed "online" with zero TCP sockets.
if err.exists() and now - err.stat().st_mtime <= 180:
    if any("ioreg" in x and "not found" in x for x in err_lines):
        raise SystemExit(0)
    if any("IOPlatformExpertDevice" in x for x in err_lines):
        raise SystemExit(0)

handshake = re.compile(r"Closed before handshake|Relay wss://.*failed|No relay responded")
if err.exists() and now - err.stat().st_mtime <= 180 and any(handshake.search(x) for x in err_lines):
    if out.exists() and out.stat().st_mtime >= err.stat().st_mtime:
        if any(("Reconnected to server" in x) or ("Connected to server" in x) or ("connected on" in x) for x in out_lines[-40:]):
            raise SystemExit(1)
    raise SystemExit(0)

# Retry loop with no recent Connected (post-reboot stall).
retrying = any("Retrying in" in x for x in out_lines[-15:])
connected = any(("Connected to server" in x) or ("Reconnected to server" in x) for x in out_lines[-30:])
if retrying and not connected:
    raise SystemExit(0)
raise SystemExit(1)
PY
}

# Resurrect a complete dump; otherwise start the ecosystem and save.
# Never `pm2 save` a one-job leftover.  Never start `trading`.
pm2_restore_bulk() {
  if dump_covers_expected "$DUMP" "${expect_pm2[@]}"; then
    try_restart "pm2:resurrect" pm2 resurrect
    return $?
  fi
  if [ -f "$ECOSYSTEM" ]; then
    log "SKIP  pm2:resurrect  dump-incomplete"
    try_restart "pm2:ecosystem" bash -lc "pm2 start \"$ECOSYSTEM\" && pm2 save"
    return $?
  fi
  log "FAIL  pm2:restore  no-dump-no-ecosystem"
  return 1
}

pm2_daemon_up() {
  # Do not use pgrep -f here: on this Mac it misses the God Daemon even
  # while pm2 jlist works (false DOWN + resurrect every 2 min).
  # Do not call `pm2 jlist` / `pm2 ping` when the daemon is dead -- those
  # spawn an empty daemon.  Trust the pid file + kill -0.
  pidfile="${HOME}/.pm2/pm2.pid"
  [ -f "$pidfile" ] || return 1
  pid="$(tr -d '[:space:]' < "$pidfile")"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

pm2_status_of() {
  name="$1"
  json="$2"
  printf '%s' "$json" | python3 -c '
import json,sys
name=sys.argv[1]
try:
    data=json.load(sys.stdin)
except Exception:
    print("unknown")
    raise SystemExit
for p in data:
    if p.get("name")==name:
        print((p.get("pm2_env") or {}).get("status") or "unknown")
        raise SystemExit
print("missing")
' "$name"
}

launchd_disabled() {
  label="$1"
  uid="$2"
  launchctl print-disabled "gui/${uid}" 2>/dev/null | grep -F "\"${label}\"" | grep -q '=> disabled'
}

down=0
did_pm2_bulk=0
uid="$(id -u)"

# --- pm2 ---
if ! pm2_daemon_up; then
  down=1
  log "DOWN  pm2:daemon  status=missing"
  if pm2_restore_bulk; then
    did_pm2_bulk=1
  fi
else
  pm2_json=""
  jlist_rc=0
  pm2_json="$(pm2_jlist_timed)" || jlist_rc=$?
  skip_pm2_inventory=0
  if [ "$jlist_rc" -eq 124 ]; then
    down=1
    log "DOWN  pm2:rpc  status=jlist-timeout"
    pm2_kill_stray_cli
    if pm2_daemon_up; then
      # Slow RPC under load is not a dead God.  Killing it orphans
      # listeners; resurrect then crash-loops on AddrInUse.
      log "SKIP  pm2:rpc  god-alive-slow-jlist"
      skip_pm2_inventory=1
    else
      try_restart "pm2:rpc" bash -lc 'pid=$(tr -d "[:space:]" < "$HOME/.pm2/pm2.pid"); [ -n "$pid" ] && kill "$pid"; sleep 2; kill -0 "$pid" 2>/dev/null && kill -9 "$pid"; true'
      # God is dead.  Restore from dump only if it lists expected names;
      # otherwise ecosystem start (2026-08-21 poison dump hole).
      pm2_restore_bulk
      did_pm2_bulk=1
      skip_pm2_inventory=1
    fi
  elif [ "$jlist_rc" -ne 0 ] || [ -z "$pm2_json" ]; then
    pm2_json="[]"
  fi
  if [ "$skip_pm2_inventory" -eq 0 ]; then
  missing_names=""
  missing_n=0
  restart_names=""
  for name in "${expect_pm2[@]}"; do
    status="$(pm2_status_of "$name" "$pm2_json")"
    if [ "$status" = "online" ]; then
      continue
    fi
    if [ "$name" = "grok-leader" ] && grok_leader_lock_held; then
      log "SKIP  pm2:grok-leader  lock-held"
      if [ "$status" = "errored" ] && [ "$RESTART" = "1" ]; then
        # pm2 autorestart already gave up.  Leave it stopped, not
        # errored, so the next watch pass does not keep trying.
        pm2 stop grok-leader >>"$CMDLOG" 2>&1 || true
        log "STOP  pm2:grok-leader  lock-held-stop-storm"
      fi
      continue
    fi
    down=1
    log "DOWN  pm2:$name  status=$status"
    if [ "$status" = "stopped" ] || [ "$status" = "errored" ] || [ "$status" = "stopping" ]; then
      restart_names="${restart_names}${name} "
    else
      missing_names="${missing_names}${name} "
      missing_n=$((missing_n + 1))
    fi
  done

  if [ "$missing_n" -ge 3 ]; then
    if pm2_restore_bulk; then
      did_pm2_bulk=1
    fi
  fi

  if [ "$did_pm2_bulk" -eq 0 ]; then
    for name in $restart_names; do
      try_restart "pm2:$name" pm2 restart "$name"
    done
    for name in $missing_names; do
      if [ -f "$ECOSYSTEM" ]; then
        try_restart "pm2:$name" pm2 start "$ECOSYSTEM" --only "$name"
      else
        log "FAIL  pm2:$name  no-ecosystem"
      fi
    done
  fi
  fi
fi

botfleet_harness_healthy() {
  /usr/bin/curl -sf -m 2 "http://127.0.0.1:8799/health" >/dev/null 2>&1
}

# --- launchd always-on ---
for spec in "${expect_launchd[@]}"; do
  label="${spec%% *}"
  plist_name="${spec#* }"
  plist="${HOME}/Library/LaunchAgents/${plist_name}"

  if launchd_disabled "$label" "$uid"; then
    log "SKIP  launchd:$label  disabled"
    continue
  fi

  # Packaged BotFleet.app often owns :8799.  Do not bootstrap a second
  # node server on top of a healthy harness.
  if [ "$label" = "com.jay.botfleet-server" ] && botfleet_harness_healthy; then
    log "OK    launchd:$label  8799-healthy"
    continue
  fi

  if ! launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; then
    down=1
    log "DOWN  launchd:$label  status=not-loaded"
    if [ -f "$plist" ]; then
      try_restart "launchd:$label" launchctl bootstrap "gui/${uid}" "$plist"
    else
      log "FAIL  launchd:$label  no-plist"
    fi
    continue
  fi

  pid="$(launchctl print "gui/${uid}/${label}" 2>/dev/null | awk '/^[[:space:]]*pid = / {print $3; exit}')"
  if [ -z "${pid:-}" ] || [ "$pid" = "0" ]; then
    down=1
    log "DOWN  launchd:$label  status=no-pid"
    try_restart "launchd:$label" launchctl kickstart "gui/${uid}/${label}"
  fi
done

# --- scheduled: keep loaded so the timer can fire.  do not kickstart idle. ---
for spec in "${expect_scheduled[@]}"; do
  label="${spec%% *}"
  plist_name="${spec#* }"
  plist="${HOME}/Library/LaunchAgents/${plist_name}"

  if launchd_disabled "$label" "$uid"; then
    log "SKIP  scheduled:$label  disabled"
    continue
  fi

  if ! launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; then
    down=1
    log "DOWN  scheduled:$label  status=not-loaded"
    if [ -f "$plist" ]; then
      try_restart "scheduled:$label" launchctl bootstrap "gui/${uid}" "$plist"
    else
      log "FAIL  scheduled:$label  no-plist"
    fi
  fi
done

# --- trigger bodies must exist ---
for path in "${expect_files[@]}"; do
  if [ ! -e "$path" ]; then
    down=1
    log "FAIL  file:$path  missing"
  fi
done

# --- stale run-locks that make a timer exit 0 without doing work ---
steal_stale_lock() {
  lockdir="$1"
  key="$2"
  max_age="${3:-7200}"
  [ -d "$lockdir" ] || return 0
  mtime="$(stat -f %m "$lockdir" 2>/dev/null || echo 0)"
  age=$((NOW - mtime))
  if [ "$age" -gt "$max_age" ]; then
    if rmdir "$lockdir" 2>/dev/null; then
      log "UNLOCK  $key  stale=${age}s"
    else
      log "FAIL  $key  stale-lock-busy"
    fi
  fi
}
steal_stale_lock "${HOME}/.claude-disk-janitor/.lock" "disk-janitor"
steal_stale_lock "${HOME}/.claude-merge-shepherd/.lock" "merge-shepherd"

# --- shellular relay liveness (process up, cloud dead) ---
if pgrep -f 'shellular-runtime/node_modules/shellular/dist/main.js' >/dev/null 2>&1; then
  if shellular_relay_dead; then
    down=1
    log "DOWN  pm2:shellular  status=relay-dead"
    spid="$(pgrep -n -f 'shellular-runtime/node_modules/shellular/dist/main.js' || true)"
    if [ -n "${spid:-}" ]; then
      try_restart "pm2:shellular-relay" kill "$spid"
    fi
  fi
fi

# --- local HTTP health (pm2 "online" with a dead/orphan port) ---
# Skip grok-leader (unix socket; this TUI may hold the lock).
expect_http=(
  "mac-collab http://127.0.0.1:8792/health"
  "xcode-health http://127.0.0.1:8791/health"
  "agent-sync-push http://127.0.0.1:8787/health"
)
if pm2_daemon_up; then
  for spec in "${expect_http[@]}"; do
    name="${spec%% *}"
    url="${spec#* }"
    if ! curl -fsS -m 3 -o /dev/null "$url" 2>/dev/null; then
      down=1
      log "DOWN  pm2:$name  status=http-dead"
      try_restart "pm2:$name-http" pm2 restart "$name"
    fi
  done
fi

if [ "$down" -eq 0 ]; then
  # Quiet when healthy - one heartbeat line an hour at most.
  last="$(tail -n 1 "$LOG" 2>/dev/null || true)"
  case "$last" in
    *"OK    all expected jobs online"*)
      last_hour="${last:0:13}"
      now_hour="${STAMP:0:13}"
      if [ "$last_hour" = "$now_hour" ]; then
        exit 0
      fi
      ;;
  esac
  log "OK    all expected jobs online"
fi
