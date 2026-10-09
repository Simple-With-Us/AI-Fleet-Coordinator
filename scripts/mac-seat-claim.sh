#!/usr/bin/env bash
# mac-seat-claim.sh — Mac seat claims a needs-mac issue and starts a local agent chat.
#
# Run by hand or from launchd com.jay.mac-seat-watch (anytime, 7 days/week).
# Does not impersonate the Shellular phone client.  Does not claim compile passed.
#
# Usage:
#   ./scripts/mac-seat-claim.sh --by GROK --once
#   ./scripts/mac-seat-claim.sh --by CURSOR --repo Socratic.Trade --issue 42
#   ./scripts/mac-seat-claim.sh --by GROK --dry-run --once
#
# Options:
#   --by TAG             Mac seat tag, e.g. GROK or CURSOR (required)
#   --repo REPO          Only consider this repo (name or owner/repo)
#   --issue N            Claim a specific issue number in --repo
#   --once               One poll pass then exit (for launchd)
#   --no-spawn           Claim + comment only; do not start grok/cursor
#   --dry-run            Print actions without mutating GitHub or spawning
#   -h, --help           Show this help

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLEET_APPS="${REPO_ROOT}/fleet-apps.json"
OWNER="${GITHUB_OWNER:-jaywedgeworth22}"
LOG_DIR="${HOME}/Library/Logs/mac-seat-claim"
GROK_BIN="${GROK_BIN:-/Users/jay/.grok/bin/grok}"
CURSOR_AGENT="${CURSOR_AGENT:-cursor-agent}"
# Zulip posting needs no shared secret: the agent-sync CLI reads the seat's own
# ~/.secrets/Zulip/<file code>-zuliprc.  The old AGENT_SYNC_ENV / AGENT_SYNC_POST_URL
# relay is retired and deliberately not referenced here.
ZULIP_CHANNEL="${ZULIP_CHANNEL:-agent-sync}"

BY=""
REPO_FILTER=""
ISSUE_NUM=""
ONCE=0
NO_SPAWN=0
DRY_RUN=0

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
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
  echo "mac-seat-claim: $*" >&2
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

fleet_repos() {
  python3 - "$FLEET_APPS" "$REPO_FILTER" "$OWNER" <<'PY'
import json, sys
path, filt, owner = sys.argv[1:4]
data = json.loads(open(path).read())
repos = []
for app in data.get("apps", []):
    name = app.get("repo", "")
    if not name:
        continue
    full = f"{owner}/{name}"
    if filt:
        if filt in (name, full):
            repos.append(full)
    else:
        repos.append(full)
if filt and not repos:
    if "/" in filt:
        repos.append(filt)
    else:
        repos.append(f"{owner}/{filt}")
print("\n".join(repos))
PY
}

parse_needs_mac_block() {
  local body="$1"
  python3 - <<'PY' "$body"
import re, sys
body = sys.argv[1]
m = re.search(r"<!--\s*needs-mac:v1\s*(.*?)\s*-->", body, re.S)
if not m:
    raise SystemExit(1)
fields = {}
for line in m.group(1).splitlines():
    line = line.strip()
    if not line or "=" not in line:
        continue
    k, v = line.split("=", 1)
    fields[k.strip()] = v.strip()
for key in ("agent", "by", "repo", "branch", "worktree", "reason", "board_id", "prompt"):
    if key == "prompt":
        continue
    print(f"{key}={fields.get(key, '')}")
# prompt lives outside the HTML block; extract ### Prompt section
pm = re.search(r"### Prompt\s*\n+(.*?)(?:\n### |\Z)", body, re.S)
prompt = pm.group(1).strip() if pm else ""
print("prompt=" + prompt.replace("\n", "\\n"))
PY
}

post_agent_sync() {
  # Post to Zulip with the seat's own bot via the agent-sync CLI.
  # The CLI reads that seat's ~/.secrets/Zulip/<file code>-zuliprc itself, so
  # there is no shared bearer token to parse, rotate, or leak in this script.
  # (The old relay POST is retired; see AGENT-SYNC.md.)
  local text="$1"
  local topic="${2:-mac seat claims}"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY: agent-sync post --as %s --channel %s --topic %q: %s\n' "$BY" "$ZULIP_CHANNEL" "$topic" "$text"
    return 0
  fi
  command -v agent-sync >/dev/null 2>&1 || {
    echo "mac-seat-claim: agent-sync not on PATH; skipping Zulip post" >&2
    return 0
  }
  agent-sync post --as "$BY" --channel "$ZULIP_CHANNEL" --topic "$topic" "$text" >/dev/null 2>&1 || true
}

spawn_local_agent() {
  local agent="$1" prompt="$2" worktree="$3" issue_ref="$4"
  local cwd="${worktree/#\~/$HOME}"
  if [ -n "$cwd" ] && [ -d "$cwd" ]; then
    :
  else
    cwd="$HOME"
  fi

  mkdir -p "$LOG_DIR"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local logfile="${LOG_DIR}/${agent}-${stamp}.log"

  case "$agent" in
    grok)
      local cmd=("$GROK_BIN" -p "$prompt")
      ;;
    cursor)
      local cmd=("$CURSOR_AGENT" -p "$prompt")
      ;;
    *)
      die "unknown agent: $agent"
      ;;
  esac

  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY: cd %q && nohup' "$cwd"
    printf ' %q' "${cmd[@]}"
    printf ' >>%q 2>&1 &\n' "$logfile"
    return 0
  fi

  (
    cd "$cwd"
    nohup "${cmd[@]}" >>"$logfile" 2>&1 &
    echo $! >"${logfile}.pid"
  )
  echo "mac-seat-claim: spawned ${agent} pid $(cat "${logfile}.pid") log ${logfile}"
  post_agent_sync "repo: ${issue_ref} | mac-seat claimed ${issue_ref}; spawned ${agent} -p (log ${logfile}).  Not claiming compile passed." "mac seat claims ${REPO_FILTER:+$REPO_FILTER}"
}

pick_issue() {
  local full_repo="$1"
  if [ -n "$ISSUE_NUM" ]; then
    echo "$ISSUE_NUM"
    return 0
  fi
  gh issue list \
    --repo "$full_repo" \
    --label "needs-mac" \
    --state open \
    --json number,labels \
    --limit 20 \
    | python3 - <<'PY'
import json, sys
items = json.load(sys.stdin)
candidates = []
for item in items:
    labels = {l.get("name", "") for l in item.get("labels", [])}
    if "mac-seat-claimed" in labels:
        continue
    candidates.append(item["number"])
if not candidates:
    raise SystemExit(1)
print(min(candidates))
PY
}

while [ $# -gt 0 ]; do
  case "$1" in
    --by) BY="${2:-}"; shift 2 ;;
    --repo) REPO_FILTER="${2:-}"; shift 2 ;;
    --issue) ISSUE_NUM="${2:-}"; shift 2 ;;
    --once) ONCE=1; shift ;;
    --no-spawn) NO_SPAWN=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown arg: $1 (try --help)" ;;
  esac
done

[ -n "$BY" ] || die "--by is required"
if [ -n "$ISSUE_NUM" ] && [ -z "$REPO_FILTER" ]; then
  die "--issue requires --repo"
fi
command -v gh >/dev/null 2>&1 || die "gh CLI is required"

mapfile -t REPOS < <(fleet_repos)
[ "${#REPOS[@]}" -gt 0 ] || die "no repos to scan (fleet-apps.json missing?)"

claim_one() {
  local full_repo issue_num body issue_ref
  for full_repo in "${REPOS[@]}"; do
    [ -n "$full_repo" ] || continue
    if ! issue_num="$(pick_issue "$full_repo" 2>/dev/null)"; then
      continue
    fi
    issue_ref="${full_repo}#${issue_num}"
    body="$(gh issue view "$issue_num" --repo "$full_repo" --json body -q .body)"
    if ! mapfile -t META < <(parse_needs_mac_block "$body"); then
      echo "mac-seat-claim: skip ${issue_ref} (missing needs-mac block)" >&2
      continue
    fi

    local agent="grok" cloud_by="cloud" prompt="" worktree="" branch="" reason="xcodebuild"
    local line key val
    for line in "${META[@]}"; do
      key="${line%%=*}"
      val="${line#*=}"
      val="${val//\\n/$'\n'}"
      case "$key" in
        agent) agent="$val" ;;
        by) cloud_by="$val" ;;
        prompt) prompt="$val" ;;
        worktree) worktree="$val" ;;
        branch) branch="$val" ;;
        reason) reason="$val" ;;
      esac
    done

    echo "== mac-seat-claim =="
    echo "issue:    ${issue_ref}"
    echo "agent:    ${agent}"
    echo "cloud by: ${cloud_by}"
    echo "reason:   ${reason}"
    [ -n "$branch" ] && echo "branch:   ${branch}"
    [ -n "$worktree" ] && echo "worktree: ${worktree}"

    local claim_body
    claim_body="Mac seat **${BY}** claimed this needs-mac request for **${reason}**.

- Worktree: \`${worktree:-<unspecified>}\`
- Branch: \`${branch:-<unspecified>}\`
- Local spawn: \`${agent} -p\` (Shellular-host style; not Shellular phone ACP)
- grok-acp remains \`127.0.0.1:12419\` only

Not claiming compile passed — local agent must verify and report back."

    run gh issue comment "$issue_num" --repo "$full_repo" --body "$claim_body"
    run gh issue edit "$issue_num" --repo "$full_repo" --add-label "mac-seat-claimed" 2>/dev/null \
      || run gh label create "mac-seat-claimed" --repo "$full_repo" --description "Mac seat claimed via mac-seat-claim.sh" \
      && run gh issue edit "$issue_num" --repo "$full_repo" --add-label "mac-seat-claimed"

    if [ "$NO_SPAWN" -eq 0 ]; then
      [ -n "$prompt" ] || die "issue ${issue_ref} has no ### Prompt section"
      spawn_local_agent "$agent" "$prompt" "$worktree" "$issue_ref"
    fi

    return 0
  done
  return 1
}

if claim_one; then
  exit 0
fi

if [ "$ONCE" -eq 1 ]; then
  echo "mac-seat-claim: no open needs-mac issues"
  exit 0
fi

die "no open needs-mac issues found"
