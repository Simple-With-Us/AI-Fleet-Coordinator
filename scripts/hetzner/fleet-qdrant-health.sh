#!/usr/bin/env bash
# Qdrant health rows for fleet-health-verify.sh (called from it every 15 minutes; on FAIL the
# caller pages via the existing rate-limited Pushover path).  Prints OK/WARN/FAIL lines in the
# same shape as the caller and exits 1 when anything is FAIL.
#
#   1. latest local Qdrant snapshot per collection is younger than MAX_SNAPSHOT_HOURS
#   2. the fleet-agents ingest sentinel (doc_id meta/ingest-status, written by the nightly
#      Oracle routine on the Mac) is younger than MAX_INGEST_HOURS -- this is the dead-man switch
#      for the ingest routine; if Oracle stops running it, this row pages.
set -uo pipefail

CONTAINER="${QDRANT_CONTAINER:-qdrant-ookh0qmlgrbxlwbbe6lolx6g}"
API="${QDRANT_API:-http://100.69.77.26:6333}"
ROOT="${QDRANT_BACKUP_ROOT:-/data/backups/qdrant}"
MAX_SNAPSHOT_HOURS="${MAX_SNAPSHOT_HOURS:-36}"
MAX_INGEST_HOURS="${MAX_INGEST_HOURS:-30}"
FAIL=0
now=$(date +%s)

for coll in socratic-trade fleet-agents; do
  latest=$(ls -1t "$ROOT/$coll"/*.snapshot 2>/dev/null | head -1 || true)
  if [ -z "$latest" ]; then
    echo "WARN qdrant_backup_$coll none_yet"
    continue
  fi
  hrs=$(( (now - $(stat -c %Y "$latest")) / 3600 ))
  if [ "$hrs" -gt "$MAX_SNAPSHOT_HOURS" ]; then
    echo "FAIL qdrant_backup_$coll stale>${hrs}h file=$(basename "$latest")"
    FAIL=1
  else
    echo "OK  qdrant_backup_$coll age_hours=$hrs file=$(basename "$latest")"
  fi
done

KEY="$(docker inspect "$CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
       | sed -n 's/^QDRANT__SERVICE__API_KEY=//p' | head -1)"
if [ -z "$KEY" ]; then
  echo "FAIL qdrant_ingest_sentinel no_api_key"
  exit 1
fi
resp=$(curl -sS --max-time 20 -H "api-key: $KEY" -H 'Content-Type: application/json' \
        -X POST "$API/collections/fleet-agents/points/scroll" \
        -d '{"limit":1,"with_payload":["updated_at","ingest_run","ok"],"filter":{"must":[{"key":"doc_id","match":{"value":"meta/ingest-status"}}]}}' 2>/dev/null || true)
upd=$(printf '%s' "$resp" | jq -r '.result.points[0].payload.updated_at // empty' 2>/dev/null || true)
okflag=$(printf '%s' "$resp" | jq -r '.result.points[0].payload.ok // empty' 2>/dev/null || true)
if [ -z "$upd" ]; then
  echo "WARN qdrant_ingest_sentinel none_yet"
else
  hrs=$(( (now - upd / 1000) / 3600 ))
  if [ "$hrs" -gt "$MAX_INGEST_HOURS" ]; then
    echo "FAIL qdrant_ingest_sentinel stale>${hrs}h"
    FAIL=1
  elif [ "$okflag" = "false" ]; then
    echo "FAIL qdrant_ingest_sentinel last_run_failed age_hours=$hrs"
    FAIL=1
  else
    echo "OK  qdrant_ingest_sentinel age_hours=$hrs"
  fi
fi
exit $FAIL
