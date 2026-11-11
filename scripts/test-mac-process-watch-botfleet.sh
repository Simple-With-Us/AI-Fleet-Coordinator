#!/bin/bash
# Behaviour tests for the BotFleet harness entry in mac-process-watch.sh.
#
# Runs the whole watch with HOME pointed at a temp dir and launchctl, pm2
# and pgrep stubbed on PATH, so it never touches the live watch lock,
# state, logs, launchd or pm2.  macOS only (stat -f, plutil), like the
# other test-mac-process-watch-*.sh scripts.
#
# Regression: on 2026-10-01 the watch probed com.jay.botfleet-server (a
# label the 2026-09-22 rename retired), saw "not-loaded", and bootstrapped
# the legacy-named plist -- which declares app.botfleet.server -- in the
# middle of a BotFleet update.  The updater's rollback bootstrap was then
# refused as a same-label import.
#
#   bash scripts/test-mac-process-watch-botfleet.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WATCH="${ROOT}/mac-process-watch.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

BF="app.botfleet.server"
LEGACY="com.jay.botfleet-server"
TOKEN="lock-token-must-not-be-logged"
failures=0

fail() {
  echo "FAIL [$case_name] $*" >&2
  failures=$((failures + 1))
}

# --- stubs ---
stubs="${tmp}/bin"
mkdir -p "$stubs"
cat >"${stubs}/launchctl" <<'SH'
#!/bin/bash
# State lives in $STUB_DIR:
#   loaded/<label>   label is loaded (contents = pid; empty = no pid)
#   disabled         lines for print-disabled
#   bootstrap-mode   ok | eio-loaded | fail
# Labels other than the BotFleet ones are always loaded with a pid.
set -u
calls="${STUB_DIR}/calls"
is_bf() { case "$1" in app.botfleet.server|com.jay.botfleet-server) return 0 ;; esac; return 1; }
case "${1:-}" in
  print-disabled)
    echo "disabled services = {"
    cat "${STUB_DIR}/disabled" 2>/dev/null || true
    echo "}"
    ;;
  print)
    label="${2##*/}"
    if is_bf "$label"; then
      [ -f "${STUB_DIR}/loaded/${label}" ] || { echo "Could not find service \"${label}\"" >&2; exit 113; }
      pid="$(cat "${STUB_DIR}/loaded/${label}")"
    else
      pid=4242
    fi
    echo "${label} = {"
    [ -n "$pid" ] && echo "	pid = ${pid}"
    echo "}"
    ;;
  bootstrap)
    echo "bootstrap ${3##*/}" >>"$calls"
    declared="$(/usr/bin/plutil -extract Label raw -o - "$3" 2>/dev/null)"
    mode="$(cat "${STUB_DIR}/bootstrap-mode" 2>/dev/null || echo ok)"
    case "$mode" in
      ok) echo 777 >"${STUB_DIR}/loaded/${declared}"; exit 0 ;;
      eio-loaded)
        echo 777 >"${STUB_DIR}/loaded/${declared}"
        echo "Bootstrap failed: 5: Input/output error" >&2
        exit 5 ;;
      *) echo "Bootstrap failed: 5: Input/output error" >&2; exit 5 ;;
    esac
    ;;
  kickstart)
    echo "kickstart ${2##*/}" >>"$calls"
    ;;
  *)
    echo "UNEXPECTED launchctl $*" >>"$calls"
    exit 99
    ;;
esac
SH
printf '#!/bin/bash\necho "pm2 $*" >>"${STUB_DIR}/calls"\nexit 0\n' >"${stubs}/pm2"
printf '#!/bin/bash\nexit 1\n' >"${stubs}/pgrep"
chmod +x "${stubs}/launchctl" "${stubs}/pm2" "${stubs}/pgrep"

write_plist() {
  # write_plist <file-basename> <Label>
  cat >"${home}/Library/LaunchAgents/$1" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$2</string>
  <key>ProgramArguments</key><array><string>/usr/bin/true</string></array>
</dict></plist>
XML
}

write_lock() {
  # write_lock <pid> <mode> <started-seconds-ago>
  mkdir -p "$lock"
  python3 - "$lock/owner.json" "$1" "$2" "$3" "$TOKEN" <<'PY'
import json, sys, time
path, pid, mode, ago, token = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5]
json.dump({"version": 1, "pid": pid, "token": token, "mode": mode,
           "startedAt": int((time.time() - ago) * 1000)}, open(path, "w"))
PY
}

new_case() {
  case_name="$1"
  home="${tmp}/${case_name}/home"
  STUB_DIR="${tmp}/${case_name}/stub"
  lock="${home}/Library/Caches/BotFleet/update.lock"
  mkdir -p "${home}/Library/LaunchAgents" "${home}/Library/Logs" "${STUB_DIR}/loaded"
  : >"${STUB_DIR}/calls"
  # The 2026-10-01 Mac: both plists present, both declare the new label.
  write_plist "${BF}.plist" "$BF"
  write_plist "${LEGACY}.plist" "$BF"
}

run_watch() {
  # Port 9 refuses at once: the harness is never "8799-healthy" here.
  env -i HOME="$home" PATH="${stubs}:/usr/bin:/bin:/usr/sbin:/sbin" \
    STUB_DIR="$STUB_DIR" \
    MAC_PROCESS_WATCH_BOTFLEET_HEALTH_URL="http://127.0.0.1:9/health" \
    "$@" bash "$WATCH" >/dev/null 2>&1
  watch_log="$(cat "${home}/Library/Logs/mac-process-watch.log" 2>/dev/null || true)"
  calls="$(cat "${STUB_DIR}/calls")"
}

expect_log() { grep -qF -- "$1" <<<"$watch_log" || fail "log missing: $1"$'\n'"$watch_log"; }
reject_log() { if grep -qF -- "$1" <<<"$watch_log"; then fail "log has: $1"$'\n'"$watch_log"; fi; }
expect_call() { grep -qxF -- "$1" <<<"$calls" || fail "call missing: $1"$'\n'"calls: $calls"; }
no_bf_bootstrap() {
  if grep -qE "^bootstrap (${BF}|${LEGACY})\.plist$" <<<"$calls"; then
    fail "bootstrapped the harness: $calls"
  fi
}
no_unexpected() {
  if grep -q '^UNEXPECTED' <<<"$calls"; then fail "unexpected launchctl call: $calls"; fi
}

dead_pid() {
  sleep 0 &
  local p=$!
  wait "$p" 2>/dev/null
  echo "$p"
}

# 1. Live update lock: never bootstrap, log pid + phase, never the token.
new_case live-lock
write_lock "$$" apply 60
run_watch
expect_log "SKIP  launchd:${BF}  update-in-progress pid=$$ phase=apply"
reject_log "$TOKEN"
reject_log "DOWN  launchd:${BF}"
no_bf_bootstrap
no_unexpected

# 2. Prepare holds the same lock; skip there too.
new_case live-lock-prepare
write_lock "$$" prepare 30
run_watch
expect_log "update-in-progress pid=$$ phase=prepare"
no_bf_bootstrap

# 3. Lock dir created, owner.json not written yet: still in progress.
new_case lock-no-owner
mkdir -p "$lock"
run_watch
expect_log "SKIP  launchd:${BF}  update-in-progress pid=unknown"
no_bf_bootstrap

# 4. Dead owner pid: the update is over (or crashed) -- watch as usual.
new_case dead-owner
write_lock "$(dead_pid)" apply 60
run_watch
reject_log "update-in-progress"
expect_log "DOWN  launchd:${BF}  status=not-loaded"
expect_call "bootstrap ${BF}.plist"
expect_log "RESTART  launchd:${BF}  ok"

# 5. Live pid but older than the cap (pid reuse): ignored, logged.
new_case stale-lock
write_lock "$$" apply 10800
run_watch
expect_log "WARN  launchd:${BF}  update-lock-stale-ignored pid=$$ phase=apply"
expect_call "bootstrap ${BF}.plist"

# 6. Already loaded under the new label: nothing to do (the 2026-10-01
#    false DOWN, where the probe used the retired label).
new_case loaded
echo 555 >"${STUB_DIR}/loaded/${BF}"
run_watch
reject_log "launchd:${BF}"
reject_log "launchd:${LEGACY}"
no_bf_bootstrap
no_unexpected

# 7. Bootstrap refused because the label got loaded meanwhile (EIO 5):
#    healthy, not a failure.
new_case eio-already-loaded
echo eio-loaded >"${STUB_DIR}/bootstrap-mode"
run_watch
expect_call "bootstrap ${BF}.plist"
expect_log "OK    launchd:${BF}  already-loaded label=${BF} bootstrap-rc=5"
reject_log "FAIL  launchd:${BF}"

# 8. A real bootstrap failure is still a FAIL.
new_case bootstrap-fails
echo fail >"${STUB_DIR}/bootstrap-mode"
run_watch
expect_log "FAIL  launchd:${BF}  cmd-failed"

# 9. Disabling the legacy label still silences the harness watch.
new_case legacy-disabled
echo "	\"${LEGACY}\" => disabled" >"${STUB_DIR}/disabled"
run_watch
expect_log "SKIP  launchd:${BF}  disabled-as=${LEGACY}"
no_bf_bootstrap

# 10. Disabling the new label skips too.
new_case disabled
echo "	\"${BF}\" => disabled" >"${STUB_DIR}/disabled"
run_watch
expect_log "SKIP  launchd:${BF}  disabled"
no_bf_bootstrap

# 11. Pre-rename Mac: only the legacy plist, declaring the legacy label,
#     and that label loaded -> healthy, no bootstrap.
new_case legacy-only-loaded
rm -f "${home}/Library/LaunchAgents/${BF}.plist"
write_plist "${LEGACY}.plist" "$LEGACY"
echo 556 >"${STUB_DIR}/loaded/${LEGACY}"
run_watch
reject_log "launchd:${BF}"
no_bf_bootstrap

# 12. Pre-rename Mac, nothing loaded: bootstrap falls back to the legacy
#     plist (mirrors the updater's harnessBootstrapPlist).
new_case legacy-only-down
rm -f "${home}/Library/LaunchAgents/${BF}.plist"
write_plist "${LEGACY}.plist" "$LEGACY"
run_watch
expect_call "bootstrap ${LEGACY}.plist"
expect_log "RESTART  launchd:${BF}  ok"

# 13. Loaded but no pid: kickstart the label that is actually loaded.
new_case loaded-no-pid
: >"${STUB_DIR}/loaded/${BF}"
run_watch
expect_log "DOWN  launchd:${BF}  status=no-pid"
expect_call "kickstart ${BF}"

# 14. ...but not while an update runs.
new_case loaded-no-pid-updating
: >"${STUB_DIR}/loaded/${BF}"
write_lock "$$" apply 60
run_watch
expect_log "update-in-progress"
if grep -q "^kickstart ${BF}$" <<<"$calls"; then fail "kickstarted mid-update"; fi

if [ "$failures" -gt 0 ]; then
  echo "FAIL ${failures} assertion(s)" >&2
  exit 1
fi
echo OK
