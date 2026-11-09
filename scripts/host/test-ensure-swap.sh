#!/usr/bin/env bash
# Unit checks for ensure-swap.sh helpers.  Does not touch the live host.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/scripts/host/ensure-swap.sh"

grep -q 'TARGET_SWAP_GIB:-16' "$SRC"
grep -q 'SWAPPINESS:-20' "$SRC"
grep -q '/swapfile.extra' "$SRC"
grep -q 'Does not swapoff the existing /swapfile' "$SRC"
# refuse to run as non-root unless dry-run
if bash "$SRC" >/tmp/ensure-swap-test.out 2>/tmp/ensure-swap-test.err; then
  echo "expected non-root without --dry-run to fail" >&2
  exit 1
fi
grep -q 'run as root' /tmp/ensure-swap-test.err
bash "$SRC" --dry-run >/tmp/ensure-swap-dry.out
grep -q 'swappiness_target 20' /tmp/ensure-swap-dry.out
echo "ok test-ensure-swap"
