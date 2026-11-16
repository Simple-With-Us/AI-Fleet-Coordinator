#!/usr/bin/env bash
# Armer-side hold gate: exits 0 when the PR is clear of active holds,
# exits 1 (with a reason on stdout) when held or when evaluation fails
# (fail closed - the armer's classify treats nonzero as do-not-arm).
# Env: REPO, PR_NUMBER.  GH_TOKEN must authorize issues:read.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="$(python3 "$DIR/hold_guard.py" "$REPO" "$PR_NUMBER")" || {
  echo "hold evaluation error: $out"; exit 1; }
state="$(printf '%s' "$out" | jq -r .state)"
if [ "$state" != "clear" ]; then
  printf '%s\n' "$(printf '%s' "$out" | jq -c '[.holds[]? | {label: .label, owner_login: .owner_login, unknown: .unknown}]')"
  exit 1
fi
exit 0
