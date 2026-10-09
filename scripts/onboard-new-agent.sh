#!/usr/bin/env bash
# onboard-new-agent.sh — register a new seat in fleet-apps.json and print the manual steps.
#
# Mechanical half of docs/ONBOARDING-NEW-AGENT.md.  Does not create lanes (a lane is
# made per task with `~/apps/lane new`), and does not configure chat tokens, MCP, or
# platform rules files and hooks.
#
# Usage:
#   ./scripts/onboard-new-agent.sh --tag GROK --notes-name Grok \
#       --worktree-suffix grok --branch-prefix grok/
#   ./scripts/onboard-new-agent.sh --tag NEWSEAT --dry-run

set -euo pipefail

TAG=""
NOTES_NAME=""
SUFFIX=""
PREFIX=""
DRY_RUN=0
here="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --notes-name) NOTES_NAME="${2:-}"; shift 2 ;;
    --worktree-suffix) SUFFIX="${2:-}"; shift 2 ;;
    --branch-prefix) PREFIX="${2:-}"; shift 2 ;;
    --apps|--include-fleet)
      # Accepted so older command lines keep working.  These used to pick which
      # integration trees got a lane; no lane is created here any more.
      echo "note: $1 is ignored; lanes are created per task with 'lane new'" >&2
      if [ "$1" = "--apps" ]; then shift 2; else shift; fi ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

if [ -z "$TAG" ]; then
  echo "required: --tag" >&2
  usage 1
fi
NOTES_NAME="${NOTES_NAME:-$TAG}"
SUFFIX="${SUFFIX:-$(echo "$TAG" | tr '[:upper:]' '[:lower:]')}"
PREFIX="${PREFIX:-$SUFFIX/}"

echo "== seat onboard: tag=$TAG notes=$NOTES_NAME suffix=$SUFFIX prefix=$PREFIX"

# Record the seat in fleet-apps.json if missing.
if [ "$DRY_RUN" -eq 0 ]; then
  python3 - "$here/fleet-apps.json" "$TAG" "$NOTES_NAME" "$SUFFIX" "$PREFIX" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
tag, notes, suffix, prefix = sys.argv[2:6]
data = json.loads(path.read_text())
seats = data.setdefault("seats", [])
if any(s.get("tag") == tag for s in seats):
    print(f"fleet-apps.json already has seat {tag}")
else:
    seats.append({
        "tag": tag,
        "notesName": notes,
        "worktreeSuffix": suffix,
        "branchPrefixes": [prefix],
    })
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"appended seat {tag} to fleet-apps.json")
PY
else
  echo "DRY: would record seat $TAG in fleet-apps.json"
fi

echo
if [ "$DRY_RUN" -eq 1 ]; then recorded="would be recorded"; else recorded="is recorded"; fi
echo "Seat $TAG $recorded.  This script creates NO lanes: a lane is made per task, with"
echo "  AGENT_SEAT=$TAG ~/apps/lane new <app> <slug>"
echo "which lands at ~/apps/lanes/<prefix>/$SUFFIX-<slug> on branch ${PREFIX}<slug>."
echo "(Layout and rules: docs/protocols/lane-map.md.  Folder names use the whole suffix.)"
echo
echo "Still do by hand (the owner approves each live install; read every plan first):"
echo "  1. PR the new fleet-apps.json row.  'lane' refuses an unknown seat until the row has"
echo "     merged and the stable copy is refreshed:"
echo "       cd scripts && python3 -m fleet_lanes.install_tools plan tools"
echo "       cd scripts && python3 -m fleet_lanes.install_tools apply tools"
echo "  2. Rules file and deny hook are installed per PLATFORM, not per seat.  If this seat runs"
echo "     on a covered platform (rules: claude codex fx grok antigravity minimax cursor;"
echo "     hook: claude codex grok antigravity cursor), confirm with:"
echo "       cd scripts && python3 -m fleet_lanes.install_rules verify <platform>"
echo "       cd scripts && python3 -m fleet_lanes.install_tools verify <platform>"
echo "     and, if either is missing, plan then apply it:"
echo "       python3 -m fleet_lanes.install_rules plan <platform>"
echo "       python3 -m fleet_lanes.install_rules apply <platform>   (add --create for a new file;"
echo "         --i-own-this-file for claude)"
echo "       python3 -m fleet_lanes.install_tools plan <platform>"
echo "       python3 -m fleet_lanes.install_tools apply <platform>"
echo "     A new platform needs an entry in scripts/fleet_lanes/install_rules.py and install_tools.py"
echo "     first; this script does not add one."
echo "  3. AGENT_SEAT=$TAG if the platform shares an account"
echo "  4. Intro + first claim on the chat channel (#agent-sync)"
echo "  5. Add a row to the AGENT-SYNC.md Agent Seat table if this is a standing seat"
echo "  6. See docs/ONBOARDING-NEW-AGENT.md"
echo
echo "Chat hints (work once the seat has a Zulip bot and its mode-600 ~/.secrets/Zulip/<Seat>-zuliprc):"
echo "Inbox: AGENT_SEAT=$TAG agent-sync inbox"
printf 'Intro: AGENT_SEAT=%s agent-sync post --topic "roll call" "intro: <platform>, cadence <listen|wait|per-turn read>, can: <what this session can do>"\n' "$TAG"
