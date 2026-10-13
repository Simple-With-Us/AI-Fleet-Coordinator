#!/bin/bash
# Contract: the disk janitor's CleanMyMac block stays safe to run unattended.
#
# Separate from test-disk-janitor-match.sh because that suite is red on main
# (it still asserts the pre-#318 pressure-gate defaults and the removed
# `cleanmymac clean dev` call) and anything added there would never run.
#
# CleanMyMac CLI is a free public beta that this Mac reinstalled on 2026-10-04
# (board e30a863e).  A beta that deletes files, wired into a job that runs every
# 30 minutes under launchd, is exactly where a quiet safety regression would do
# the most damage.  These assertions exist so the rules below cannot erode.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
JANITOR="${ROOT}/disk-janitor.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

[ -f "$JANITOR" ] || fail "missing $JANITOR"

# 1. `junk` must never be swept.
#
# The 2026-09-13 bug: `cleanmymac clean --force` ran every module including
# `junk`, which unlinked a log file that launchd still held open.  The daemon
# kept writing to an unlinked inode, so the space was never actually freed and
# the log vanished from disk.  The CLI has no in-place truncation rule, so the
# only safe path for logs is the Hog Hunter `logs` rule.
if grep -E 'cleanmymac[[:space:]]+clean[[:space:]]+junk' "$JANITOR" | grep -qv '^\s*#'; then
  fail "cleanmymac clean junk must never run (2026-09-13 unlinked-live-log bug)"
fi

# 2. `optimize ram` must never run unattended.
#
# It purges resident pages into swapfiles on the same APFS container as user
# data, converting RAM pressure into disk consumption.  Measured on this Mac
# during the re-adoption: swap was already 94% used (13.4 of 14 GB) with the
# data volume at 97% full.  Under those conditions it makes the disk problem
# worse.  The ban is structural -- do not "fix" this by adding a gate.
if grep -E 'cleanmymac[[:space:]]+optimize[[:space:]]+ram' "$JANITOR" | grep -qv '^\s*#'; then
  fail "cleanmymac optimize ram must never run (purge-to-swap worsens disk pressure)"
fi

# 3. Every CleanMyMac call must be watchdog-bounded on the same line.
#
# Matched per-line rather than by extracting subcommands, because the janitor
# sweeps a loop (`for cmm_mod in ...; cleanmymac clean "$cmm_mod" --force`) and a
# subcommand-extracting matcher finds nothing there and passes vacuously.
# The CLI is a beta TUI; a hung clean holds the janitor's slot, and the job runs
# every 30 minutes, so an unbounded hang compounds.
cmmswept=$(grep -nE '^[[:space:]]*[^#].*cleanmymac[[:space:]]+clean' "$JANITOR" || true)
[ -n "$cmmswept" ] || fail "expected the janitor to sweep cleanmymac somewhere"
while IFS= read -r line; do
  [ -n "$line" ] || continue
  case "$line" in
    *janitor_watchdog*) ;;
    *) fail "cleanmymac call must be on a janitor_watchdog line: $line" ;;
  esac
done <<< "$cmmswept"

# 4. The modules actually swept must be dev/ai/trash -- no others.
swept=$(grep -oE 'for cmm_mod in [a-z ]+' "$JANITOR" | head -1 | sed 's/.*in //' | tr -d ' ')
[ "$swept" = "devaitrash" ] || fail "expected modules 'dev ai trash', got '$swept'"

# 5. The block must be reachable at all.
#
# The reason this file exists at all: a 0-byte cask receipt made
# `command -v cleanmymac` false, so the block was a silent no-op for weeks while
# still logging as if it had cleaned something.  If the guard is dropped, the
# same silence comes back.  Anchored to `if command -v` so the many prose
# mentions of the phrase in comments cannot satisfy this.
grep -qE '^[[:space:]]*if[[:space:]]+command -v cleanmymac' "$JANITOR" \
  || fail "CleanMyMac block must stay guarded by 'if command -v cleanmymac' so a missing binary is loud"

echo OK
