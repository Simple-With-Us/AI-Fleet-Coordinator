#!/usr/bin/env bash
# Edge + in-cluster health verification for fleet apps.
# Logs only except: on FAIL, rate-limited Pushover (same env as congress-health-recover).
# Restarts are fleet-health-recover@ / congress-health-recover, not this cron.
set -euo pipefail
FAIL=0
if [[ -f /etc/congress-health-recover.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/congress-health-recover.env
  set +a
fi
check() {
  local name="$1" url="$2"
  local expect="${3:-200}"
  code=$(curl -sS -o /tmp/fh.out -w "%{http_code}" --max-time 20 -A "fleet-health-verify/1.0" "$url" || echo 000)
  body=$(head -c 120 /tmp/fh.out 2>/dev/null | tr "\n" " ")
  if [ "$expect" = "302_ok" ]; then
    if [ "$code" = "200" ] || [ "$code" = "302" ] || [ "$code" = "301" ]; then
      echo "OK  $name $code"
      return 0
    fi
    echo "FAIL $name $code body=$body"
    FAIL=1
    return 0
  fi
  if [ "$code" = "200" ] && echo "$body" | grep -qiE 'ok|true|healthy|ready|live'; then
    echo "OK  $name $code"
  elif [ "$code" = "200" ]; then
    echo "WARN $name $code body=$body"
  else
    echo "FAIL $name $code body=$body"
    FAIL=1
  fi
}
check socratic "https://socratictrade.com/api/health"
check congress "https://congress.trade/api/health"
check usage "https://usage.jays.services/api/health"
check coolify_host "https://host.jays.services/" 302_ok
# local docker
if docker ps --format '{{.Names}} {{.Status}}' | grep -qi unhealthy; then
  echo "FAIL docker_unhealthy_present"
  docker ps --format '{{.Names}} {{.Status}}' | grep -i unhealthy || true
  FAIL=1
else
  echo "OK  docker_no_unhealthy"
fi
# latest local sqlite backups age
for d in socratic congress usage-monitor; do
  latest=$(ls -1t /data/backups/$d/*.db 2>/dev/null | head -1 || true)
  if [ -z "$latest" ]; then
    echo "WARN backup_$d none_yet"
  else
    age=$(( $(date +%s) - $(stat -c %Y "$latest") ))
    hrs=$(( age / 3600 ))
    echo "OK  backup_$d age_hours=$hrs file=$(basename "$latest")"
    if [ "$hrs" -gt 36 ]; then
      echo "FAIL backup_$d stale>${hrs}h"
      FAIL=1
    fi
  fi
done

# Qdrant snapshot age + fleet-agents ingest sentinel.  No-op until the helper
# is installed next to this script as /usr/local/sbin/fleet-qdrant-health.sh.
if [ -x /usr/local/sbin/fleet-qdrant-health.sh ]; then
  if ! /usr/local/sbin/fleet-qdrant-health.sh; then
    FAIL=1
  fi
fi

if [ "$FAIL" -eq 1 ]; then
  mkdir -p /var/lib/fleet-health-verify
  stamp=/var/lib/fleet-health-verify/last_notify
  last=0
  [[ -f "$stamp" ]] && last=$(cat "$stamp" 2>/dev/null || echo 0)
  now=$(date +%s)
  if [[ $((now - last)) -ge 1800 ]]; then
    echo "$now" > "$stamp"
    if [[ -n "${PUSHOVER_APP_TOKEN:-}" && -n "${PUSHOVER_USER_KEY:-}" ]]; then
      curl -sS -m 15 -o /dev/null \
        --form-string "token=${PUSHOVER_APP_TOKEN}" \
        --form-string "user=${PUSHOVER_USER_KEY}" \
        --form-string "title=fleet-health-verify FAIL" \
        --form-string "message=host.jays.services health-verify reported FAIL. Check /var/log/fleet-backup/cron-health.log. Recover units should bounce ST/UM/CT." \
        --form-string "priority=0" \
        https://api.pushover.net/1/messages.json >/dev/null 2>&1 || echo "WARN pushover failed"
    fi
  fi
fi
exit $FAIL
