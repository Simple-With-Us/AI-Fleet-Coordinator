#!/bin/bash
# Contract tests for mac-process-watch.sh --dump-covers.
# A poisoned dump (one leftover job after dsh pm2 kill + pm2 save) must
# not count as covering the expected set.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
WATCH="${ROOT}/mac-process-watch.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

grep -q '"app.botfleet.server app.botfleet.server.plist com.jay.botfleet-server"' "$WATCH" || { echo "FAIL watch must list app.botfleet.server (legacy com.jay.botfleet-server alias)" >&2; exit 1; }
grep -q '8799-healthy' "$WATCH" || { echo "FAIL watch must treat :8799 health as botfleet UP" >&2; exit 1; }

covers() {
  bash "$WATCH" --dump-covers "$@"
}

assert_ok() {
  if ! covers "$@"; then
    echo "FAIL expected cover: $*" >&2
    exit 1
  fi
}

assert_no() {
  if covers "$@"; then
    echo "FAIL expected reject: $*" >&2
    exit 1
  fi
}

# vision-worker-only dump (2026-08-21 crash reboot)
printf '%s\n' '[{"name":"vision-worker","pm2_env":{"name":"vision-worker","status":"online"}}]' \
  >"${tmp}/poisoned.json"
assert_no "${tmp}/poisoned.json" \
  shellular scout senate-relay senate-tunnel agent-sync-push \
  code-main-keeper vision-worker xcode-health cursor-slack-sync agy-acp \
  grok-leader grok-acp mac-collab mac-collab-sync

# same poisoned dump still fails the original 10-job list
assert_no "${tmp}/poisoned.json" \
  shellular scout senate-relay senate-tunnel agent-sync-push \
  code-main-keeper vision-worker xcode-health cursor-slack-sync agy-acp

# complete dump with top-level name
python3 - "${tmp}/complete.json" <<'PY'
import json, sys
path = sys.argv[1]
names = [
    "shellular", "scout", "senate-relay", "senate-tunnel", "agent-sync-push",
    "code-main-keeper", "vision-worker", "xcode-health", "cursor-slack-sync",
    "agy-acp", "grok-leader", "grok-acp", "mac-collab", "mac-collab-sync",
]
json.dump([{"name": n} for n in names], open(path, "w"))
PY
assert_ok "${tmp}/complete.json" \
  shellular scout senate-relay senate-tunnel agent-sync-push \
  code-main-keeper vision-worker xcode-health cursor-slack-sync agy-acp \
  grok-leader grok-acp mac-collab mac-collab-sync

# pm2_env.name only
python3 - "${tmp}/envonly.json" <<'PY'
import json, sys
json.dump([{"pm2_env": {"name": "scout"}}], open(sys.argv[1], "w"))
PY
assert_ok "${tmp}/envonly.json" scout
assert_no "${tmp}/envonly.json" scout shellular

# unreadable / missing / empty
printf 'not-json\n' >"${tmp}/bad.json"
assert_no "${tmp}/bad.json" scout
assert_no "${tmp}/missing.json" scout
printf '[]\n' >"${tmp}/empty.json"
assert_no "${tmp}/empty.json" scout
printf '{}\n' >"${tmp}/obj.json"
assert_no "${tmp}/obj.json" scout

# extra dump jobs are fine as long as every expected name is present
python3 - "${tmp}/extra.json" <<'PY'
import json, sys
json.dump([{"name": "scout"}, {"name": "other"}], open(sys.argv[1], "w"))
PY
assert_ok "${tmp}/extra.json" scout

echo "ok  dump-covers $(bash -n "$WATCH" && echo 'bash -n clean')"
