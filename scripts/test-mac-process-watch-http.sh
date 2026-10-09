#!/bin/bash
# Behaviour tests for the local HTTP health probe in mac-process-watch.sh
# ("slow is not dead", 2026-10-08).
#
# Runs the whole watch with HOME pointed at a temp dir and launchctl, pm2,
# pgrep and curl stubbed on PATH, so it never touches the live watch lock,
# state, logs, launchd, pm2 or the real servers.  macOS only (stat -f), like
# the other test-mac-process-watch-*.sh scripts.
#
# Regression: under heavy load the servers on :8792 / :8791 / :8787 stayed
# alive but answered slowly.  One timed-out probe ran `pm2 restart`, the
# starved process could not exit, pm2 SIGKILLed it ~60s later, and the cold
# start took 2-4 minutes -- that restart caused the real "connection
# refused" outages.  Contract:
#   curl rc 0            healthy, resets the streak
#   curl rc 7            refused = dead -> restart on the first run
#   any other non-zero   slow -> restart only on the Kth consecutive run
#                        (MAC_PROCESS_WATCH_HTTP_SLOW_STRIKES, default 3)
#
#   bash scripts/test-mac-process-watch-http.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WATCH="${ROOT}/mac-process-watch.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

failures=0
case_name="setup"

fail() {
  echo "FAIL [$case_name] $*" >&2
  failures=$((failures + 1))
}

# --- stubs ---
stubs="${tmp}/bin"
mkdir -p "$stubs"

# pm2 jlist must list every expected job as online, or the watch takes the
# bulk-restore path and floods the log.  Read the names from the watch.
names="$(awk '/^expect_pm2=\(/ {f=1; next} f && /^\)/ {f=0} f {print $1}' "$WATCH")"
[ -n "$names" ] || { echo "FAIL could not read expect_pm2 from $WATCH" >&2; exit 1; }
python3 - "${tmp}/jlist.json" $names <<'PY'
import json, sys
json.dump([{"name": n, "pm2_env": {"status": "online"}} for n in sys.argv[2:]],
          open(sys.argv[1], "w"))
PY

cat >"${stubs}/pm2" <<'SH'
#!/bin/bash
if [ "${1:-}" = "jlist" ]; then
  cat "${JLIST_JSON}"
  exit 0
fi
echo "pm2 $*" >>"${STUB_DIR}/calls"
exit 0
SH
# curl exits with the code in $STUB_DIR/curl-rc-<port> (default 0).
cat >"${stubs}/curl" <<'SH'
#!/bin/bash
url="${*: -1}"
port="${url#*://127.0.0.1:}"
port="${port%%/*}"
echo "curl ${port}" >>"${STUB_DIR}/curl-calls"
exit "$(cat "${STUB_DIR}/curl-rc-${port}" 2>/dev/null || echo 0)"
SH
cat >"${stubs}/launchctl" <<'SH'
#!/bin/bash
case "${1:-}" in
  print-disabled) echo "disabled services = {"; echo "}" ;;
  print) echo "${2##*/} = {"; echo "	pid = 4242"; echo "}" ;;
  *) echo "launchctl $*" >>"${STUB_DIR}/calls" ;;
esac
exit 0
SH
printf '#!/bin/bash\nexit 1\n' >"${stubs}/pgrep"
chmod +x "${stubs}/pm2" "${stubs}/curl" "${stubs}/launchctl" "${stubs}/pgrep"

new_case() {
  case_name="$1"
  home="${tmp}/${case_name}/home"
  STUB_DIR="${tmp}/${case_name}/stub"
  mkdir -p "${home}/.pm2" "${home}/Library/LaunchAgents" "${home}/Library/Logs" "$STUB_DIR"
  # pm2_daemon_up trusts the pid file + kill -0; this shell is alive.
  echo "$$" >"${home}/.pm2/pm2.pid"
  : >"${STUB_DIR}/calls"
  watch_runs=0
}

set_rc() {
  # set_rc <port> <curl-rc>
  echo "$2" >"${STUB_DIR}/curl-rc-$1"
}

# run_watch [ENV=VAL ...]: one watch pass; sets watch_log (whole log so
# far) and run_log (just this pass), plus the pm2 restart tally.
run_watch() {
  local before
  before=0
  if [ -f "${home}/Library/Logs/mac-process-watch.log" ]; then
    before="$(wc -l <"${home}/Library/Logs/mac-process-watch.log")"
  fi
  : >"${STUB_DIR}/calls"
  # Port 9 refuses at once: the BotFleet harness probe stays out of the way.
  env -i HOME="$home" PATH="${stubs}:/usr/bin:/bin:/usr/sbin:/sbin" \
    STUB_DIR="$STUB_DIR" JLIST_JSON="${tmp}/jlist.json" \
    MAC_PROCESS_WATCH_BOTFLEET_HEALTH_URL="http://127.0.0.1:9/health" \
    "$@" bash "$WATCH" >/dev/null 2>&1
  watch_runs=$((watch_runs + 1))
  watch_log="$(cat "${home}/Library/Logs/mac-process-watch.log" 2>/dev/null || true)"
  run_log="$(tail -n +"$((before + 1))" <<<"$watch_log")"
  calls="$(cat "${STUB_DIR}/calls")"
}

run_expect_log() { grep -qF -- "$1" <<<"$run_log" || fail "run ${watch_runs}: log missing: $1"$'\n'"$run_log"; }
run_reject_log() { if grep -qF -- "$1" <<<"$run_log"; then fail "run ${watch_runs}: log has: $1"$'\n'"$run_log"; fi; }
restarted() { grep -qxF -- "pm2 restart $1" <<<"$calls"; }
expect_restart() { restarted "$1" || fail "run ${watch_runs}: no 'pm2 restart $1'"$'\n'"calls: $calls"$'\n'"$run_log"; }
reject_restart() { if restarted "$1"; then fail "run ${watch_runs}: unexpected 'pm2 restart $1'"$'\n'"calls: $calls"; fi; }
no_other_restart() {
  # Only the named service may have been restarted this run.
  if grep -E '^pm2 restart ' <<<"$calls" | grep -qvxF -- "pm2 restart $1"; then
    fail "run ${watch_runs}: restarted something besides $1"$'\n'"calls: $calls"
  fi
}
expect_streak_file() {
  # expect_streak_file <name> <count>   (count 0 = no line)
  local got
  got="$(awk -v n="$1" '$1 == n {print $2}' "${home}/Library/Logs/mac-process-watch.state.http-slow" 2>/dev/null)"
  [ "${got:-0}" = "$2" ] || fail "run ${watch_runs}: streak file for $1 is '${got:-none}', want $2"
}

# 0. The probe really is the stubbed one (guards against a bare-curl path
#    change that would let the real curl hit the live servers).
new_case baseline
run_watch
grep -q '^curl 8792$' "${STUB_DIR}/curl-calls" 2>/dev/null || fail "stub curl was not called for :8792"
run_reject_log "SLOW  pm2:"
run_reject_log "status=http-"
no_other_restart none

# 1. rc 7 (connection refused): dead, restart on the very first run.
new_case refused
set_rc 8792 7
run_watch
run_expect_log "DOWN  pm2:mac-collab  status=http-dead curl=7"
run_reject_log "SLOW  pm2:mac-collab"
expect_restart mac-collab
no_other_restart mac-collab
expect_streak_file mac-collab 0

# 2. rc 28 (timeout): slow.  No restart on runs 1 and 2, restart on run 3,
#    then the streak starts over.
new_case timeout-three-strikes
set_rc 8792 28
run_watch
run_expect_log "SLOW  pm2:mac-collab  curl=28 streak=1/3"
run_reject_log "DOWN  pm2:mac-collab"
reject_restart mac-collab
expect_streak_file mac-collab 1
run_watch
run_expect_log "SLOW  pm2:mac-collab  curl=28 streak=2/3"
reject_restart mac-collab
expect_streak_file mac-collab 2
run_watch
run_expect_log "SLOW  pm2:mac-collab  curl=28 streak=3/3"
run_expect_log "DOWN  pm2:mac-collab  status=http-slow curl=28 streak=3/3"
expect_restart mac-collab
no_other_restart mac-collab
expect_streak_file mac-collab 0
run_watch
run_expect_log "SLOW  pm2:mac-collab  curl=28 streak=1/3"
reject_restart mac-collab

# 3. An rc 0 run in between resets the streak: 28, 28, 0, 28, 28 never
#    restarts; the third 28 after the reset does.
new_case healthy-resets
set_rc 8792 28
run_watch; reject_restart mac-collab
run_watch; reject_restart mac-collab
expect_streak_file mac-collab 2
set_rc 8792 0
run_watch
run_reject_log "SLOW  pm2:mac-collab"
reject_restart mac-collab
expect_streak_file mac-collab 0
set_rc 8792 28
run_watch; run_expect_log "streak=1/3"; reject_restart mac-collab
run_watch; run_expect_log "streak=2/3"; reject_restart mac-collab
run_watch; run_expect_log "status=http-slow"; expect_restart mac-collab

# 4. Every other non-zero rc is slow, and mixed ones add up.
new_case mixed-slow-codes
set_rc 8792 52
run_watch; run_expect_log "SLOW  pm2:mac-collab  curl=52 streak=1/3"; reject_restart mac-collab
set_rc 8792 22
run_watch; run_expect_log "SLOW  pm2:mac-collab  curl=22 streak=2/3"; reject_restart mac-collab
set_rc 8792 56
run_watch; run_expect_log "status=http-slow curl=56 streak=3/3"; expect_restart mac-collab

# 5. rc 7 in the middle of a slow streak restarts at once and clears it.
new_case refused-mid-streak
set_rc 8792 28
run_watch
run_watch
expect_streak_file mac-collab 2
set_rc 8792 7
run_watch
run_expect_log "status=http-dead curl=7"
expect_restart mac-collab
expect_streak_file mac-collab 0
set_rc 8792 28
run_watch; run_expect_log "streak=1/3"; reject_restart mac-collab

# 6. Streaks are per service, and only the slow one is touched.
new_case per-service
set_rc 8792 28
set_rc 8791 28
run_watch
run_expect_log "SLOW  pm2:mac-collab  curl=28 streak=1/3"
run_expect_log "SLOW  pm2:xcode-health  curl=28 streak=1/3"
run_reject_log "agent-sync-push"
set_rc 8792 0
run_watch
run_expect_log "SLOW  pm2:xcode-health  curl=28 streak=2/3"
run_reject_log "SLOW  pm2:mac-collab"
expect_streak_file mac-collab 0
expect_streak_file xcode-health 2
set_rc 8792 28
run_watch
run_expect_log "SLOW  pm2:mac-collab  curl=28 streak=1/3"
run_expect_log "status=http-slow"
expect_restart xcode-health
reject_restart mac-collab
reject_restart agent-sync-push

# 7. MAC_PROCESS_WATCH_HTTP_SLOW_STRIKES overrides K.
new_case strikes-override
set_rc 8792 28
run_watch MAC_PROCESS_WATCH_HTTP_SLOW_STRIKES=2
run_expect_log "streak=1/2"; reject_restart mac-collab
run_watch MAC_PROCESS_WATCH_HTTP_SLOW_STRIKES=2
run_expect_log "status=http-slow curl=28 streak=2/2"; expect_restart mac-collab

# 8. Junk or zero in the override falls back to 3.
new_case strikes-junk
set_rc 8792 28
run_watch MAC_PROCESS_WATCH_HTTP_SLOW_STRIKES=abc
run_expect_log "streak=1/3"; reject_restart mac-collab
run_watch MAC_PROCESS_WATCH_HTTP_SLOW_STRIKES=0
run_expect_log "streak=2/3"; reject_restart mac-collab

# 9. MAC_PROCESS_WATCH_RESTART=0 still counts strikes but never restarts.
new_case restart-off
set_rc 8792 28
run_watch MAC_PROCESS_WATCH_RESTART=0
run_watch MAC_PROCESS_WATCH_RESTART=0
run_watch MAC_PROCESS_WATCH_RESTART=0
run_expect_log "status=http-slow"
run_expect_log "SKIP  pm2:mac-collab-http  restart=off"
reject_restart mac-collab

if [ "$failures" -gt 0 ]; then
  echo "FAIL ${failures} assertion(s)" >&2
  exit 1
fi
echo OK
