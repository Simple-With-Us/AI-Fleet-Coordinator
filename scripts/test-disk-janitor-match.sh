#!/bin/bash
# Contract: janitor must not treat "kimi" as a substring.  ST #3044
# (cursor/kimi-audit-def) is a Cursor salvage PR the owner kept.  Active
# nested worktrees must still go through the idle check (tested here via
# the classifier only — force_stale is gone).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
JANITOR="${ROOT}/disk-janitor.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

[ -f "$JANITOR" ] || fail "missing $JANITOR"

# The dangerous #90 matcher must stay gone.
if grep -nE 'force_stale=\s*yes' "$JANITOR"; then
  fail "force_stale must not skip the idle check"
fi
if grep -nE '\*kimi\*' "$JANITOR"; then
  fail "substring *kimi* reaps cursor/kimi-audit-def"
fi

# shellcheck disable=SC1090
JANITOR_LIB_ONLY=1
# shellcheck source=disk-janitor.sh
source "$JANITOR"

assert_yes() {
  local wt="$1" br="$2"
  if ! janitor_is_retired_kimi_or_scratch "$wt" "$br"; then
    fail "expected scratch/kimi-seat: wt=$wt br=$br"
  fi
}

assert_no() {
  local wt="$1" br="$2"
  if janitor_is_retired_kimi_or_scratch "$wt" "$br"; then
    fail "expected keep (not kimi-seat/scratch): wt=$wt br=$br"
  fi
}

# ST #3044 — owner-kept salvage on a living Cursor lane.
assert_no "/Users/jay/apps/trading-cursor-kimi-audit" "cursor/kimi-audit-def"
assert_no "/Users/jay/apps/trading-cursor-kimi-audit-def" "refs/heads/cursor/kimi-audit-def"
assert_no "/Users/jay/apps/socratic-cursor-kimi-audit" "refs/heads/cursor/kimi-audit-def"

# Living-seat feature lanes that merely mention kimi in the slug.
assert_no "/Users/jay/apps/trading-grok-kimi-notes" "grok/kimi-retired-notice"
assert_no "/Users/jay/apps/fleet-claude-kimi-docs" "claude/kimi-docs"
assert_no "/Users/jay/apps/trading-grok-litestream-cascade" "grok/litestream-cascade-rag"
assert_no "/Users/jay/Code/Socratic.Trade" "main"
assert_no "/Users/jay/apps/trading-claude" "claude/feature"

# KEEP_RE must keep the always-on BotFleet harness checkout (reaping
# node_modules there crash-loops com.jay.botfleet-server).
if ! grep -q 'botfleet-server' "$JANITOR"; then
  fail "disk-janitor KEEP_RE must mention botfleet-server"
fi

# Retired KIMI seat (unsuffixed and per-lane).
assert_yes "/Users/jay/apps/trading-kimi" "kimi/leftover"
assert_yes "/Users/jay/apps/trading-kimi-onboard" "kimi/autorotate-onboard"
assert_yes "/Users/jay/apps/dealdex-kimi" "refs/heads/kimi/x"
assert_yes "/Users/jay/apps/fleet-kimi-halfdone" "KIMI/old"

# Nested agent scratch + tmp (idle check still applies in the janitor body).
assert_yes "/Users/jay/Code/Socratic.Trade/.claude/worktrees/abc" "agent/foo"
assert_yes "/Users/jay/Code/Socratic.Trade/.grok/worktrees/abc" "grok/foo"
assert_yes "/private/tmp/scratch-wt" "tmp/foo"
assert_yes "/tmp/scratch-wt" "tmp/foo"

# janitor_github_repo: squash-safe origin URL parsing (no network).
origin_repo_tmp=$(mktemp -d)
trap 'rm -rf "$origin_repo_tmp"' EXIT
git -C "$origin_repo_tmp" init -q
assert_origin() {
  local remote="$1" want="$2"
  git -C "$origin_repo_tmp" remote remove origin >/dev/null 2>&1 || true
  git -C "$origin_repo_tmp" remote add origin "$remote"
  local got
  got=$(janitor_github_repo "$origin_repo_tmp")
  [ "$got" = "$want" ] || fail "origin $remote -> '$got' want '$want'"
}
assert_origin "git@github.com:Simple-With-Us/Socratic.Trade.git" "Simple-With-Us/Socratic.Trade"
assert_origin "https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git" "Simple-With-Us/AI-Fleet-Coordinator"
assert_origin "ssh://git@github.com/Simple-With-Us/BotFleet.git" "Simple-With-Us/BotFleet"

# 2026-09-30: memory/load pressure gate.  Above JANITOR_MAX_LOAD (40) or at/above
# JANITOR_MAX_SWAP_PCT (90) the janitor logs PRESSURE-SKIP, runs only the cheap
# truncations, and exits before any git/gh/find-over-worktrees phase or CleanMyMac call.
grep -qE '^JANITOR_MAX_LOAD=\$\{JANITOR_MAX_LOAD:-40\}' "$JANITOR" || fail "JANITOR_MAX_LOAD default 40 missing"
grep -qE '^JANITOR_MAX_SWAP_PCT=\$\{JANITOR_MAX_SWAP_PCT:-90\}' "$JANITOR" || fail "JANITOR_MAX_SWAP_PCT default 90 missing"
gate_line=$(grep -n "PRESSURE-SKIP load=" "$JANITOR" | head -1 | cut -d: -f1)
[ -n "$gate_line" ] || fail "PRESSURE-SKIP log line missing"
for fan in 'janitor_watchdog 30 "wt-fetch"' 'git -C "$r" worktree prune' 'cleanmymac clean dev' 'janitor_duk 10'; do
  fan_line=$(grep -nF "$fan" "$JANITOR" | head -1 | cut -d: -f1)
  [ -n "$fan_line" ] && [ "$gate_line" -lt "$fan_line" ] || fail "pressure gate must precede: $fan"
done
# `cleanmymac optimize ram` (RAM pressure -> swap) only ever behind the explicit opt-in.
if ! grep -B1 -F -- '-- cleanmymac optimize ram' "$JANITOR" | grep -q 'RESOURCE_ALLOW_RAM_OPTIMIZE'; then
  fail "cleanmymac optimize ram must be gated behind RESOURCE_ALLOW_RAM_OPTIMIZE=1"
fi
# Parsers, against synthetic sysctl output (no real sysctl, no real work).
(
  eval "$(sed -n '/^janitor_load1()/,/^load1=/p' "$JANITOR" | sed '$d')"
  sysctl() { case "$2" in vm.swapusage) printf '%s\n' "$SYN_SWAP" ;; vm.loadavg) printf '%s\n' "$SYN_LOAD" ;; esac; }
  assert_gate() {
    SYN_SWAP="$1"; SYN_LOAD="$2"
    [ "$(janitor_swap_pct)" = "$3" ] || fail "swap pct for [$1]: got $(janitor_swap_pct) want $3"
    [ "$(janitor_load1)" = "$4" ] || fail "load1 for [$2]: got $(janitor_load1) want $4"
  }
  assert_gate "total = 10240.00M  used = 9401.44M  free = 838.56M  (encrypted)" "{ 166.12 163.87 182.51 }" 92 166.12
  assert_gate "total = 4.00G  used = 3.60G  free = 0.40G" "{ 0.50 0.40 0.30 }" 90 0.50
  assert_gate "total = 0.00M  used = 0.00M  free = 0.00M  (encrypted)" "{ 0.50 0.40 0.30 }" 0 0.50
)

echo OK
