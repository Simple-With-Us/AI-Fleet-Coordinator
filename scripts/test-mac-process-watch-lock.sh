#!/bin/bash
# Contract tests: grok-leader lock-held must not depend on bare `lsof`
# being on PATH.  LaunchAgent PATH historically omitted /usr/sbin, which
# is the only place lsof lives on macOS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WATCH="${ROOT}/mac-process-watch.sh"
LEADER="${ROOT}/grok-leader.sh"
PLIST="${ROOT}/launchd/com.jay.mac-process-watch.plist"
ECOSYSTEM="${ROOT}/pm2-ecosystem.config.cjs"

fail() { echo "FAIL $*" >&2; exit 1; }

[ -f "$WATCH" ] || fail "missing $WATCH"
[ -f "$LEADER" ] || fail "missing $LEADER"
[ -f "$PLIST" ] || fail "missing $PLIST"
[ -f "$ECOSYSTEM" ] || fail "missing $ECOSYSTEM"

grep -q '/usr/sbin/lsof' "$WATCH" || fail "watch must default LSOF=/usr/sbin/lsof"
grep -q 'grok_leader_lock_held' "$WATCH" || fail "watch must define grok_leader_lock_held"
grep -q 'lock-held-stop-storm' "$WATCH" || fail "watch must stop an errored lock-held grok-leader"
if grep -nE 'if lsof "\$\{HOME\}/.grok/leader.sock"' "$WATCH"; then
  fail "watch still uses bare lsof on the leader socket"
fi

grep -q 'exit 75' "$LEADER" || fail "leader.sh must exit 75 when lock held"
grep -q '/usr/sbin/lsof' "$LEADER" || fail "leader.sh must use /usr/sbin/lsof"

grep -q 'stop_exit_codes: \[75\]' "$ECOSYSTEM" || fail "ecosystem must stop_exit_codes 75 for grok-leader"

if ! grep -q '/usr/sbin' "$PLIST"; then
  fail "LaunchAgent PATH must include /usr/sbin"
fi

# The 2026-08-21 launchd PATH cannot find lsof.
slim="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
if PATH="$slim" command -v lsof >/dev/null 2>&1; then
  fail "unexpected lsof on slim PATH (bug was that launchd lacked /usr/sbin)"
fi
if ! PATH="$slim" /usr/sbin/lsof -v >/dev/null 2>&1; then
  fail "/usr/sbin/lsof is not executable"
fi

echo OK
