#!/usr/bin/env bash
# Run inside the Aqua/gui session (no SessionCreate) so the login keychain
# and Apple Development identity are visible. Called by a one-shot LaunchAgent.
set -euo pipefail
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
LOG=/Users/jay/apps/logs/ios-ship-now.log
mkdir -p /Users/jay/apps/logs
exec >>"$LOG" 2>&1
echo "===== $(date) ship-now-gui start ====="
security list-keychains || true
security find-identity -v -p codesigning || true

FLEET=/Users/jay/apps/ios-fleet/ship-testflight.sh
# Apps are independent; continue after one failure so the phone still
# gets whatever did archive.  Skip a missing checkout rather than
# `cd` failing the whole login job (2026-09-17: grok-tf-runner paths).
run_one() {
  local key="$1" root="$2"
  if [[ ! -d "$root" ]]; then
    echo "skip ${key}: repo-root missing ${root}"
    return 0
  fi
  bash "$FLEET" "$key" --repo-root "$root" --force-ship
}

set +e
run_one socratic /Users/jay/Code/Socratic.Trade
st_rc=$?
run_one congress /Users/jay/Code/Congress.Trade
ct_rc=$?
run_one usage /Users/jay/Code/Usage-Monitor
um_rc=$?
run_one usage-local /Users/jay/Code/Usage-Monitor
ul_rc=$?
set -e
echo "rcs st=$st_rc ct=$ct_rc um=$um_rc ul=$ul_rc"
if [[ $st_rc -ne 0 || $ct_rc -ne 0 || $um_rc -ne 0 || $ul_rc -ne 0 ]]; then
  exit 1
fi
echo "===== $(date) ship-now-gui done ====="
