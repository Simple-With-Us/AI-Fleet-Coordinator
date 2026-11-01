#!/usr/bin/env bash
# fleet-health-recover.sh — parameterized ST/UM (and similar) recover on the
# Coolify/Hetzner host.  Pattern from congress-health-recover.sh.
#
# Probe the container's *internal* health (docker IP + INTERNAL_HEALTH_URL)
# so Traefik/Cloudflare flaps do not restart a healthy app.  After consecutive
# failures: docker restart the Coolify-labeled container, then Coolify API
# restart if the container is gone.  Never host-reboot.  Never docker daemon
# restart.  Skip while Coolify is deploying.
#
# systemd: fleet-health-recover@.service + /etc/fleet-health-recover.d/%i.env
# Shared secrets: EnvironmentFile=/etc/congress-health-recover.env
#
# Required env: APP_NAME APP_UUID RESOURCE_NAME INTERNAL_HEALTH_URL
# Optional: HEALTH_URL CHECK_INTERVAL_SEC FAIL_THRESHOLD RESTART_COOLDOWN_SEC
#           MAX_RESTARTS_PER_HOUR COOLIFY_BASE_URL COOLIFY_TOKEN NOTIFY_TITLE

set -euo pipefail

# Top-level health only.  ST /api/health is {ok:true, checks.dependencies.*.ok}
# and a nested "ok":false (congress.trade dependency) must not count as down —
# that substring used to docker-restart production during RTH.
health_body_ok() {
  python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if not isinstance(data, dict) or "ok" not in data:
    raise SystemExit(1)
ok = data.get("ok")
if ok is True or ok == "ok" or ok == "true":
    pass
else:
    raise SystemExit(1)
if data.get("db") is False:
    raise SystemExit(1)
raise SystemExit(0)
'
}

# Coolify GET/POST /applications/{uuid}/restart queues a deployment.
# After a successful docker bounce that is a second production outage.
coolify_fallback_needed() {
  local docker_ok="$1"
  [[ "$docker_ok" -eq 0 ]]
}

# Test hook: does not need app env or docker.
#   bash fleet-health-recover.sh --check-body <file.json>
#   bash fleet-health-recover.sh --coolify-fallback <0|1>   # 0 = docker failed
if [ "${1:-}" = "--check-body" ]; then
  shift
  file="${1:-}"
  [ -n "$file" ] && [ -f "$file" ] || exit 1
  health_body_ok < "$file"
  exit $?
fi
if [ "${1:-}" = "--coolify-fallback" ]; then
  docker_ok="${2:-}"
  [ "$docker_ok" = "0" ] || [ "$docker_ok" = "1" ] || exit 2
  coolify_fallback_needed "$docker_ok"
  exit $?
fi
APP_NAME="${APP_NAME:?set APP_NAME}"
APP_UUID="${APP_UUID:?set APP_UUID}"
RESOURCE_NAME="${RESOURCE_NAME:?set RESOURCE_NAME}"
INTERNAL_HEALTH_URL="${INTERNAL_HEALTH_URL:?set INTERNAL_HEALTH_URL}"
HEALTH_URL="${HEALTH_URL:-}"
CHECK_INTERVAL_SEC="${CHECK_INTERVAL_SEC:-30}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"
RESTART_COOLDOWN_SEC="${RESTART_COOLDOWN_SEC:-300}"
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-4}"
COOLIFY_BASE_URL="${COOLIFY_BASE_URL:-https://host.jays.services}"
STATE_DIR="${STATE_DIR:-/var/lib/fleet-health-recover/${APP_NAME}}"
LOG_TAG="${LOG_TAG:-fleet-health-recover-${APP_NAME}}"
STARTUP_GRACE_SEC="${STARTUP_GRACE_SEC:-90}"
NOTIFY_TITLE="${NOTIFY_TITLE:-$APP_NAME}"

mkdir -p "$STATE_DIR"
FAILS=0
STARTED_AT=$(date +%s)

log() {
  local msg="$*"
  # stderr only: find_app_container and other helpers print ids on stdout for $(...) capture.
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$msg" >&2
  if command -v logger >/dev/null 2>&1; then
    logger -t "$LOG_TAG" -- "$msg" || true
  fi
}

now() { date +%s; }

notify() {
  local msg="$1" title="${2:-$NOTIFY_TITLE}"
  if [[ -z "${PUSHOVER_APP_TOKEN:-}" || -z "${PUSHOVER_USER_KEY:-}" ]]; then
    return 0
  fi
  local stamp_file="$STATE_DIR/last_notify"
  local last=0
  [[ -f "$stamp_file" ]] && last=$(cat "$stamp_file" 2>/dev/null || echo 0)
  if [[ $(( $(now) - last )) -lt "${NOTIFY_MIN_INTERVAL_SEC:-1800}" ]]; then
    return 0
  fi
  now > "$stamp_file"
  curl -sS -m 15 -o /dev/null \
    -K <(printf 'form-string = "token=%s"\nform-string = "user=%s"\n' "$PUSHOVER_APP_TOKEN" "$PUSHOVER_USER_KEY") \
    --form-string "title=${title}" \
    --form-string "message=${msg}" \
    --form-string "priority=1" \
    https://api.pushover.net/1/messages.json >/dev/null 2>&1 \
    || log "warn: pushover notify failed"
}

is_coolify_deploy_active() {
  [[ -z "${COOLIFY_TOKEN:-}" ]] && return 1
  command -v python3 >/dev/null 2>&1 || return 1
  local json
  json=$(curl -fsS -m 8 \
    -H @<(printf 'Authorization: Bearer %s\n' "$COOLIFY_TOKEN") \
    -H "Accept: application/json" \
    "${COOLIFY_BASE_URL%/}/api/v1/deployments" 2>/dev/null) || return 1
  printf '%s' "$json" | APP_UUID="$APP_UUID" python3 -c '
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
if isinstance(d, list):
    rows = d
elif isinstance(d, dict):
    inner = d.get("data", d.get("deployments"))
    if isinstance(inner, list):
        rows = inner
    elif isinstance(inner, dict):
        rows = list(inner.values())
    elif d and all(str(k).isdigit() for k in d.keys()):
        rows = list(d.values())
    else:
        rows = []
else:
    rows = []
app_uuid = os.environ.get("APP_UUID", "")
def mine(x):
    return (x.get("application_id") == app_uuid
            or x.get("application_uuid") == app_uuid)
active = [x for x in rows if mine(x) and x.get("status") in
          ("in_progress", "building", "running", "queued")]
raise SystemExit(0 if active else 1)
'
}

is_deploy_active() {
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eqi 'nixpacks|coolify-builder|buildkit'; then
    return 0
  fi
  local id
  id=$(find_app_container 2>/dev/null || true)
  if [[ -n "${id:-}" ]]; then
    if docker inspect --format '{{.State.Health.Status}} {{.State.Status}}' "$id" 2>/dev/null \
      | grep -Eqi 'starting|Created|Restarting'; then
      return 0
    fi
  fi
  if is_coolify_deploy_active; then
    return 0
  fi
  return 1
}

check_one() {
  local url="$1"
  local body code
  body=$(curl -fsS -m 12 -w '\n%{http_code}' "$url" 2>/dev/null) || return 1
  code=$(printf '%s\n' "$body" | tail -n1)
  body=$(printf '%s\n' "$body" | sed '$d')
  case "$code" in
    2??|3??) ;;
    *) return 1 ;;
  esac
  printf '%s' "$body" | health_body_ok
}

find_app_container() {
  # Coolify labels the container; the Docker *name* is usually the application UUID.
  # Older host copies grepped for a literal "socratic-app" name and missed ST entirely
  # (board ST-9 / 4ca246e5), then fell through to Coolify API restart loops.
  local id filter
  for filter in \
    "label=coolify.resourceName=${RESOURCE_NAME}" \
    "label=coolify.name=${RESOURCE_NAME}"; do
    id=$(docker ps -q --filter "$filter" 2>/dev/null | head -1 || true)
    if [[ -n "$id" ]]; then
      printf '%s\n' "$id"
      return 0
    fi
  done
  if [[ -n "${APP_UUID:-}" ]]; then
    id=$(docker ps -q --filter "name=${APP_UUID}" 2>/dev/null | head -1 || true)
    if [[ -n "$id" ]]; then
      log "warn: using running name=${APP_UUID} fallback id=${id:0:12} (prefer Coolify labels)"
      printf '%s\n' "$id"
      return 0
    fi
  fi
  for filter in \
    "label=coolify.resourceName=${RESOURCE_NAME}" \
    "label=coolify.name=${RESOURCE_NAME}"; do
    id=$(docker ps -aq --filter "$filter" --filter "status=exited" 2>/dev/null | head -1 || true)
    if [[ -n "$id" ]]; then
      printf '%s\n' "$id"
      return 0
    fi
  done
  return 1
}

check_health() {
  local id ip url
  id=$(find_app_container) || return 1
  ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$id" 2>/dev/null | awk '{print $1}')
  [[ -n "${ip:-}" ]] || return 1
  url="${INTERNAL_HEALTH_URL/127.0.0.1/$ip}"
  check_one "$url"
}

restart_timestamps_file() { printf '%s/restarts.log' "$STATE_DIR"; }
last_restart_file() { printf '%s/last_restart' "$STATE_DIR"; }

restarts_last_hour() {
  local cutoff now_ts
  now_ts=$(now)
  cutoff=$((now_ts - 3600))
  if [[ ! -f "$(restart_timestamps_file)" ]]; then
    echo 0
    return
  fi
  awk -v c="$cutoff" '$1 >= c { n++ } END { print n+0 }' "$(restart_timestamps_file)"
}

cooldown_ok() {
  local last now_ts
  now_ts=$(now)
  if [[ ! -f "$(last_restart_file)" ]]; then
    return 0
  fi
  last=$(cat "$(last_restart_file)" 2>/dev/null || echo 0)
  [[ $((now_ts - last)) -ge $RESTART_COOLDOWN_SEC ]]
}

restart_via_docker() {
  local id name status
  id=$(find_app_container) || {
    log "remediate: no $RESOURCE_NAME container found"
    return 1
  }
  name=$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')
  status=$(docker inspect --format '{{.State.Status}}' "$id" 2>/dev/null || echo unknown)
  log "remediate: docker $status -> restart id=${id:0:12} name=$name"
  if [[ "$status" == "running" ]]; then
    docker restart "$id" >/dev/null
  else
    docker start "$id" >/dev/null || docker restart "$id" >/dev/null
  fi
  return 0
}

restart_via_coolify_api() {
  if [[ -z "${COOLIFY_TOKEN:-}" ]]; then
    return 1
  fi
  local url="${COOLIFY_BASE_URL%/}/api/v1/applications/${APP_UUID}/restart"
  log "remediate: Coolify API restart $url"
  local code
  code=$(curl -sS -m 30 -o /tmp/fleet-coolify-restart-${APP_NAME}.out -w '%{http_code}' \
    -X GET \
    -H @<(printf 'Authorization: Bearer %s\n' "$COOLIFY_TOKEN") \
    -H "Accept: application/json" \
    "$url" 2>/dev/null || echo 000)
  if [[ "$code" == "405" || "$code" == "404" ]]; then
    code=$(curl -sS -m 30 -o /tmp/fleet-coolify-restart-${APP_NAME}.out -w '%{http_code}' \
      -X POST \
      -H @<(printf 'Authorization: Bearer %s\n' "$COOLIFY_TOKEN") \
      -H "Accept: application/json" \
      "$url" 2>/dev/null || echo 000)
  fi
  log "remediate: Coolify API HTTP $code"
  [[ "$code" == "2"* ]]
}

remediate() {
  if is_deploy_active; then
    log "remediate: skipped (Coolify build/deploy active)"
    return 0
  fi
  if ! cooldown_ok; then
    log "remediate: skipped (cooldown ${RESTART_COOLDOWN_SEC}s)"
    return 0
  fi
  local n
  n=$(restarts_last_hour)
  if [[ "$n" -ge "$MAX_RESTARTS_PER_HOUR" ]]; then
    log "remediate: skipped (already $n restarts in last hour; max $MAX_RESTARTS_PER_HOUR)"
    notify "Health failing but restart budget is spent ($n/$MAX_RESTARTS_PER_HOUR this hour). Not self-healing — needs a human." "$NOTIFY_TITLE DOWN"
    return 0
  fi

  local docker_ok=1 api_ok=0
  if restart_via_docker; then
    log "remediate: docker restart ok; Coolify API skipped (fallback only when the container is gone)"
  else
    docker_ok=0
  fi
  if coolify_fallback_needed "$docker_ok"; then
    restart_via_coolify_api && api_ok=1 || true
  fi

  if [[ "$docker_ok" -eq 0 && "$api_ok" -eq 0 ]]; then
    log "remediate: FAILED — no container and no Coolify API fallback"
    notify "Cannot self-heal $APP_NAME: no container and Coolify API restart unavailable." "$NOTIFY_TITLE DOWN"
    return 1
  fi

  local ts
  ts=$(now)
  echo "$ts" >> "$(restart_timestamps_file)"
  echo "$ts" > "$(last_restart_file)"
  log "remediate: recorded restart ts=$ts docker_ok=$docker_ok api_ok=$api_ok"

  sleep 45
  if check_health; then
    log "remediate: health restored after restart"
    FAILS=0
  else
    log "remediate: health still down after restart"
    notify "Restarted $APP_NAME but internal /api/health is still failing." "$NOTIFY_TITLE DOWN"
  fi
}

if [ "${1:-}" = "--find-container" ]; then
  find_app_container
  exit 0
fi

log "start app=$APP_NAME resource=$RESOURCE_NAME health=$INTERNAL_HEALTH_URL interval=${CHECK_INTERVAL_SEC}s"

while true; do
  if [[ $(( $(now) - STARTED_AT )) -lt $STARTUP_GRACE_SEC ]]; then
    sleep "$CHECK_INTERVAL_SEC"
    continue
  fi

  if check_health; then
    if [[ "$FAILS" -gt 0 ]]; then
      log "health ok (recovered after $FAILS fail(s))"
    fi
    FAILS=0
  else
    FAILS=$((FAILS + 1))
    log "health FAIL count=$FAILS/$FAIL_THRESHOLD"
    if [[ "$FAILS" -ge "$FAIL_THRESHOLD" ]]; then
      remediate || log "remediate: failed"
    fi
  fi
  sleep "$CHECK_INTERVAL_SEC"
done
