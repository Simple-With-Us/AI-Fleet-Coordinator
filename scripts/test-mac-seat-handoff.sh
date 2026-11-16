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

if grep -E -q 'gh issue (list|view)' "$CLAIM"; then
  fail "claim reads must use the relay, not gh issue list or view"
fi

stub="$(mktemp -d)"
trap 'rm -rf "$stub"' EXIT
cat >"$stub/gh" <<'EOF'
#!/usr/bin/env bash
printf 'gh %s\n' "$*"
exit 0
EOF
cat >"$stub/simple-git" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
tool, payload = sys.argv[2], json.loads(sys.argv[5])
log = os.environ.get("SIMPLE_GIT_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(tool + "\n")
mode = os.environ.get("SIMPLE_GIT_STUB", "empty")
repo = "Simple-With-Us/AI-Fleet-Coordinator"
if tool == "gr_wf_issue_list":
    if mode == "fixture" and payload.get("repo") == repo:
        numbers = [7] if "mac-seat-claimed" in payload.get("labels", "") else [7, 3]
        body = {"issues": [{"number": n, "state": "open"} for n in numbers], "complete": False}
    else:
        body = {"issues": [], "complete": False}
elif tool == "gr_wf_issue_get":
    body = {
        "issue": {
            "number": payload["number"],
            "state": "open",
            "body": os.environ.get("SIMPLE_GIT_BODY", ""),
            "body_withheld": False,
        }
    }
else:
    raise SystemExit(1)
wrapped = {"structured": body, "text": json.dumps(body)}
json.dump(wrapped, sys.stdout)
EOF
chmod 755 "$stub/gh" "$stub/simple-git"
export PATH="$stub:$PATH"
export SIMPLE_GIT_STUB=empty

"$CLAIM" --dry-run --by GROK --once --no-spawn 2>&1 \
  | grep -q 'no open needs-mac issues' \
  || fail "claim dry-run should exit cleanly with no issues"

log="$(mktemp)"
export SIMPLE_GIT_LOG="$log"
export SIMPLE_GIT_STUB=fixture
export SIMPLE_GIT_BODY="$(cat <<'EOF'
<!-- needs-mac:v1
agent=grok
by=cloud
repo=AI-Fleet-Coordinator
branch=cursor/example
worktree=/tmp/mac-seat
reason=xcodebuild
board_id=AFC-630
-->
### Prompt
noop
EOF
)"
claim_out="$("$CLAIM" --dry-run --by GROK --once --no-spawn --repo AI-Fleet-Coordinator 2>&1)" \
  || fail "fixture claim dry-run exited non-zero"
grep -q 'gr_wf_issue_list' "$log" || fail "claim must list through gr_wf_issue_list"
grep -q 'gr_wf_issue_get' "$log" || fail "claim must read the body through gr_wf_issue_get"
grep -q 'AI-Fleet-Coordinator#3' <<<"$claim_out" || fail "claim must pick the unclaimed issue"
grep -q 'DRY: gh issue comment' <<<"$claim_out" || fail "claim comment stays on gh"
if grep -E -q 'gh issue (list|view)' <<<"$claim_out"; then
  fail "claim output must not list or view through gh"
fi
rm -f "$log"

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
