#!/usr/bin/env bash
# Contract tests for cloud→Mac local-agent handoff scripts.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REQUEST="${ROOT}/request-mac-seat.sh"
CLAIM="${ROOT}/mac-seat-claim.sh"
PLIST="${ROOT}/launchd/com.jay.mac-seat-watch.plist"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[[ -x "$REQUEST" ]] || fail "request-mac-seat.sh must be executable"
[[ -x "$CLAIM" ]] || fail "mac-seat-claim.sh must be executable"
[[ -f "$PLIST" ]] || fail "missing launchd plist"

bash -n "$REQUEST" || fail "request-mac-seat.sh syntax"
bash -n "$CLAIM" || fail "mac-seat-claim.sh syntax"

"$REQUEST" --help >/dev/null || fail "request-mac-seat --help"
"$CLAIM" --help >/dev/null || fail "mac-seat-claim --help"

# Capture first: `grep -q` closes the pipe at the first match, and under
# pipefail the writer's SIGPIPE made this check fail intermittently.
request_out="$("$REQUEST" --dry-run \
  --repo AI-Fleet-Coordinator \
  --title "contract test" \
  --prompt "noop" \
  --by AFC \
  --agent grok \
  --no-zulip \
  2>&1)" || fail "request-mac-seat dry-run exited non-zero"
grep -q 'DRY: gh issue create' <<<"$request_out" || fail "request dry-run must show gh issue create"

"$CLAIM" --dry-run --by GROK --once --no-spawn 2>&1 \
  | grep -q 'no open needs-mac issues' \
  || fail "claim dry-run should exit cleanly with no issues"

grep -q 'com.jay.mac-seat-watch' "$PLIST" || fail "plist label"
grep -q 'mac-seat-claim.sh' "$PLIST" || fail "plist must call mac-seat-claim.sh"
grep -q '<integer>180</integer>' "$PLIST" || fail "plist interval"

grep -q '\-p' "$CLAIM" && grep -q 'GROK_BIN' "$CLAIM" || fail "claim must spawn grok -p"
grep -q 'CURSOR_AGENT' "$CLAIM" && grep -q '\-p' "$CLAIM" || fail "claim must spawn cursor-agent -p"
grep -q 'never claim compile passed' "$REQUEST" || fail "request must document no compile claims"
grep -q 'Not claiming compile passed' "$CLAIM" || fail "claim must not assert compile passed"
grep -q '127.0.0.1:12419' "$REQUEST" || fail "request must mention grok-acp bind"
grep -q '2419' "$REQUEST" && grep -q 'Never' "$REQUEST" || fail "request must warn against :2419"

grep -q 'com.jay.shellular' "$CLAIM" && fail "claim must not reference com.jay.shellular launchd"

echo OK
