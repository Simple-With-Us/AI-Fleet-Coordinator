#!/usr/bin/env bash
# request-mac-seat.sh — cloud agent files a needs-mac GitHub issue for Mac xcodebuild work.
#
# Cloud agents cannot run xcodebuild.  File a structured needs-mac issue, post to
# #agent-sync, and let the Mac launchd poller (mac-seat-claim.sh) pick it up.
#
# The post goes to Zulip in a `needs-mac <repo>` topic, sent as the claiming
# seat's own bot.  The old agent-sync relay POST is retired.
#
# Usage:
#   ./scripts/request-mac-seat.sh \
#     --repo Socratic.Trade \
#     --title "Verify iOS archive on cursor/ios-fix" \
#     --prompt "Run xcodebuild for ST iOS.  Branch cursor/ios-fix in ~/apps/trading-cursor-ios-fix." \
#     --by GB-COMPILER \
#     --agent grok \
#     --branch cursor/ios-fix \
#     --worktree ~/apps/trading-cursor-ios-fix
#
# Options:
#   --repo REPO          GitHub repo name or owner/repo (required)
#   --title TEXT         Issue title without the [needs-mac] prefix (required)
#   --prompt TEXT        Initial prompt for the Mac local agent (required)
#   --by TAG             Cloud seat tag, e.g. GB-COMPILER or CURSOR (required)
#   --agent grok|cursor  Local agent to spawn on the Mac (default: grok)
#   --branch NAME        Git branch the Mac seat should use
#   --worktree PATH      Worktree path on Jay's Mac (~/apps/...)
#   --reason TEXT        Why Mac is needed (default: xcodebuild)
#   --board-id ID        Optional THE BOARD item id to link
#   --no-zulip           Skip the Zulip work-topic post
#   --dry-run            Print actions without creating the issue
#   -h, --help           Show this help

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLEET_APPS="${REPO_ROOT}/fleet-apps.json"
OWNER="${GITHUB_OWNER:-jaywedgeworth22}"
# Zulip posting needs no shared secret: the agent-sync CLI reads the seat's own
# ~/.secrets/Zulip/<file code>-zuliprc.  The old AGENT_SYNC_POST_URL / AGENT_SYNC_ENV
# relay is retired and deliberately not referenced here.
ZULIP_CHANNEL="${ZULIP_CHANNEL:-agent-sync}"

REPO=""
TITLE=""
PROMPT=""
BY=""
AGENT="grok"
BRANCH=""
WORKTREE=""
REASON="xcodebuild"
BOARD_ID=""
NO_ZULIP=0
DRY_RUN=0

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY:'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

die() {
  echo "request-mac-seat: $*" >&2
  exit 1
}

normalize_repo() {
  local raw="$1"
  if [[ "$raw" == */* ]]; then
    echo "$raw"
    return
  fi
  echo "${OWNER}/${raw}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:-}"; shift 2 ;;
    --title) TITLE="${2:-}"; shift 2 ;;
    --prompt) PROMPT="${2:-}"; shift 2 ;;
    --by) BY="${2:-}"; shift 2 ;;
    --agent) AGENT="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --worktree) WORKTREE="${2:-}"; shift 2 ;;
    --reason) REASON="${2:-}"; shift 2 ;;
    --board-id) BOARD_ID="${2:-}"; shift 2 ;;
    --no-zulip) NO_ZULIP=1; shift ;;
    --no-slack) NO_ZULIP=1; shift ;;   # deprecated alias, same as --no-zulip
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown arg: $1 (try --help)" ;;
  esac
done

[ -n "$REPO" ] || die "--repo is required"
[ -n "$TITLE" ] || die "--title is required"
[ -n "$PROMPT" ] || die "--prompt is required"
[ -n "$BY" ] || die "--by is required"

case "$AGENT" in
  grok|cursor) ;;
  *) die "--agent must be grok or cursor" ;;
esac

FULL_REPO="$(normalize_repo "$REPO")"
REPO_NAME="${FULL_REPO##*/}"
ISSUE_TITLE="[needs-mac] ${TITLE}"

read -r -d '' ISSUE_BODY <<EOF || true
Cloud seat **${BY}** needs a Mac local agent for **${REASON}**.

<!-- needs-mac:v1
agent=${AGENT}
by=${BY}
repo=${REPO_NAME}
branch=${BRANCH}
worktree=${WORKTREE}
reason=${REASON}
board_id=${BOARD_ID}
-->

| Field | Value |
| --- | --- |
| agent | \`${AGENT}\` |
| cloud seat | \`${BY}\` |
| repo | \`${REPO_NAME}\` |
| branch | \`${BRANCH:-<unspecified>}\` |
| worktree | \`${WORKTREE:-<unspecified>}\` |
| reason | \`${REASON}\` |
| board | \`${BOARD_ID:-<none>}\` |

### Prompt

${PROMPT}

### Notes

- Mac poller: \`mac-seat-claim.sh\` (launchd \`com.jay.mac-seat-watch\`).
- Spawn uses Shellular-host argv: Grok \`grok -p\`, Cursor \`cursor-agent -p\` — not Shellular phone ACP.
- grok-acp stays on \`127.0.0.1:12419\` only.  Never \`:2419\`.
- Do **not** close this issue until the Mac seat posts results.  Scripts never claim compile passed.
EOF

echo "== request-mac-seat =="
echo "repo:     ${FULL_REPO}"
echo "title:    ${ISSUE_TITLE}"
echo "agent:    ${AGENT}"
echo "by:       ${BY}"
echo "reason:   ${REASON}"
[ -n "$BRANCH" ] && echo "branch:   ${BRANCH}"
[ -n "$WORKTREE" ] && echo "worktree: ${WORKTREE}"
[ "$DRY_RUN" -eq 1 ] && echo "mode:     dry-run"

if ! command -v gh >/dev/null 2>&1; then
  die "gh CLI is required to file the needs-mac issue"
fi

run gh issue create \
  --repo "$FULL_REPO" \
  --title "$ISSUE_TITLE" \
  --body "$ISSUE_BODY" \
  --label "needs-mac"

if [ "$NO_ZULIP" -eq 0 ]; then
  # Post to Zulip with the seat's own bot via the agent-sync CLI.  The CLI reads
  # that seat's ~/.secrets/Zulip/<file code>-zuliprc itself, so there is no
  # shared bearer token to parse, rotate, or leak in this script.  (The old relay
  # POST is retired; see AGENT-SYNC.md.)
  TEXT="repo: ${REPO_NAME} | needs-mac ${REASON}: ${TITLE}"
  [ -n "$BRANCH" ] && TEXT="${TEXT} | branch ${BRANCH}"
  [ -n "$WORKTREE" ] && TEXT="${TEXT} | ${WORKTREE}"
  TEXT="${TEXT} | agent ${AGENT}"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY: agent-sync post --as %s --channel %s --topic %q: %s\n' \
      "$BY" "$ZULIP_CHANNEL" "needs-mac ${REPO_NAME}" "$TEXT"
  elif ! command -v agent-sync >/dev/null 2>&1; then
    echo "request-mac-seat: agent-sync not on PATH; skipping Zulip post (issue still filed)" >&2
  else
    agent-sync post --as "$BY" --channel "$ZULIP_CHANNEL" \
      --topic "needs-mac ${REPO_NAME}" "$TEXT" >/dev/null \
      || echo "request-mac-seat: Zulip post failed (issue still filed)" >&2
  fi
fi

echo "request-mac-seat: filed needs-mac issue on ${FULL_REPO}"
