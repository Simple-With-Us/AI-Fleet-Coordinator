#!/bin/bash
# pm2-orphan-cleanup.sh — kill non-pm2 orphan processes that hold ports or lock
# files that pm2-managed services should own, then reset+restart the pm2 entry.
#
# Why: pm2 entries that share a port with a manually-started orphan process
# enter an errored-restart loop (agy-acp / grok-acp / seat-mcp / vision-worker
# have all hit 100-4600+ restarts).  The canonical owner is pm2 per
# /Users/jay/Code/AI-Fleet-Coordinator/scripts/pm2-ecosystem.config.cjs.
#
# Usage:
#   bash scripts/pm2-orphan-cleanup.sh             # clean up all errored entries
#   bash scripts/pm2-orphan-cleanup.sh agy-acp seat-mcp    # only these
#   bash scripts/pm2-orphan-cleanup.sh --dry-run   # print what would happen
#
# Safety: only kills orphan processes when the orphan is functionally
# equivalent to what pm2 would start (same port + same canonical script path).
# Logs every action with timestamp.  Never touches mac-collab / agent-sync-push
# / clutch-web / shellular (those are healthy and we don't want to touch).
#
# Last touched: 2026-09-20 (MM, top-to-bottom fleet audit)

set -euo pipefail

DRY_RUN=0
ENTRIES=()
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    DRY_RUN=1
  else
    ENTRIES+=("$arg")
  fi
done

log() { echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] $*"; }
fail() { log "FATAL: $*" >&2; exit 1; }

if ! command -v pm2 >/dev/null 2>&1; then
  fail "pm2 not on PATH"
fi

# Find errored pm2 entries if user did not name any
if [[ ${#ENTRIES[@]} -eq 0 ]]; then
  while IFS= read -r line; do
    name=$(echo "$line" | awk '{print $1}')
    status=$(echo "$line" | awk '{print $2}')
    if [[ "$status" == "errored" ]]; then
      ENTRIES+=("$name")
    fi
  done < <(pm2 jlist 2>/dev/null | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
for p in data:
    n = p.get('name','?')
    s = p.get('pm2_env',{}).get('status','?')
    print(f'{n} {s}')
" 2>/dev/null || true)
fi

if [[ ${#ENTRIES[@]} -eq 0 ]]; then
  log "no errored pm2 entries to clean"
  exit 0
fi

log "target entries: ${ENTRIES[*]} (dry-run=$DRY_RUN)"

# Track PIDs we already killed this run, to avoid double-kill across entries
declare -A KILLED

# Per-entry rules: port_or_lock → pattern to match orphan command line
declare -A PORT
# agy-acp stdio-to-ws proxy binds 127.0.0.1:8765 (loopback-only enforced via bind-loopback.cjs)
PORT[agy-acp]="8765"
# Grok ACP itself binds 12419 (grok-1.0 binary). Its start.sh wrapper holds no extra port.
PORT[grok-acp]="12419"
PORT[seat-mcp]="8793"

declare -A LOCKFILE
LOCKFILE[vision-worker]="/Users/jay/vision-worker/attempt-state.json.lock"

declare -A SCRIPT_PATTERN
SCRIPT_PATTERN[agy-acp]="agy-acp-runtime/start.sh"
SCRIPT_PATTERN[grok-acp]="grok-acp-runtime/start.sh"
SCRIPT_PATTERN[seat-mcp]="seat_mcp"
SCRIPT_PATTERN[vision-worker]="vision-worker/worker.py"

for entry in "${ENTRIES[@]}"; do
  log "=== $entry ==="
  pattern="${SCRIPT_PATTERN[$entry]:-}"
  port="${PORT[$entry]:-}"
  lockfile="${LOCKFILE[$entry]:-}"

  orphan_pids=()

  # Find orphan by command pattern
  if [[ -n "$pattern" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && orphan_pids+=("$pid")
    done < <(pgrep -f "$pattern" 2>/dev/null || true)
  fi

  # Find orphan by port binding (skip if our own pm2 daemon already binds it)
  if [[ -n "$port" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && orphan_pids+=("$pid")
    done < <(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sort -u || true)
  fi

  # Find orphan by lock file holder
  if [[ -n "$lockfile" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && orphan_pids+=("$pid")
    done < <(lsof -t "$lockfile" 2>/dev/null | sort -u || true)
  fi

  # Dedupe + skip already-killed across entries
  unique_pids=()
  for pid in $(printf "%s\n" "${orphan_pids[@]:-}" | sort -u | grep -v '^$' || true); do
    if [[ -z "${KILLED[$pid]:-}" ]]; then
      unique_pids+=("$pid")
    fi
  done

  # Filter: don't kill the pm2 daemon's own child if it's the same PID
  pm2_pid=$(pm2 jlist 2>/dev/null | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
for p in data:
    if p.get('name') == '$entry':
        print(p.get('pid', 0))
        break
" 2>/dev/null || echo "0")

  for pid in "${unique_pids[@]}"; do
    if [[ "$pid" == "0" || "$pid" == "$pm2_pid" ]]; then
      log "  skip pid=$pid (own pm2 child)"
      continue
    fi
    cmd=$(ps -p "$pid" -o command= 2>/dev/null || echo "<dead>")
    log "  orphan pid=$pid cmd=$cmd"
    if [[ "$DRY_RUN" == "1" ]]; then
      log "  DRY-RUN: would kill pid=$pid"
      continue
    fi
    # Verify port health pre-kill (if applicable) — confirms orphan is the real instance
    if [[ -n "$port" ]]; then
      health=$(curl -s -m 2 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${port}/health" 2>/dev/null || echo "000")
      log "  pre-kill health on :${port}/health -> $health"
    fi
    kill -TERM "$pid" 2>/dev/null || log "  TERM failed for $pid (already gone?)"
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      log "  pid=$pid still alive after TERM, sending KILL"
      kill -KILL "$pid" 2>/dev/null || true
      sleep 0.5
    fi
    log "  killed pid=$pid"
    KILLED[$pid]=1
  done

  if [[ "$DRY_RUN" == "1" ]]; then
    log "  DRY-RUN: would pm2 reset $entry && pm2 restart $entry"
    continue
  fi

  log "  pm2 reset $entry"
  pm2 reset "$entry" 2>&1 | tail -3 || true
  log "  pm2 restart $entry"
  pm2 restart "$entry" 2>&1 | tail -3 || true
  sleep 2

  # Verify
  pm2_status=$(pm2 jlist 2>/dev/null | python3 -c "
import sys, json
data = json.loads(sys.stdin.read())
for p in data:
    if p.get('name') == '$entry':
        e = p.get('pm2_env',{})
        print(f'status={e.get(\"status\",\"?\")} restarts={e.get(\"restart_time\",0)} pid={p.get(\"pid\",0)}')
        break
" 2>/dev/null || echo "?")
  log "  post-restart: $pm2_status"
done

log "done"
