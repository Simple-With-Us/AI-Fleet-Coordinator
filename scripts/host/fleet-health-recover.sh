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
# Run guard (opt-in, per instance; unset RUN_GUARD_DB = old behaviour):
#           RUN_GUARD_DB                 host path to the app's SQLite DB
#           RUN_GUARD_MAX_DEFER_SEC      cap from the first deferral (default 900)
#           RUN_GUARD_RUNNING_MAX_AGE_SEC ignore older status='running' rows (default 10800)
#           RUN_GUARD_QUERY_TIMEOUT_SEC  hard limit on the sqlite3 read (default 10)
#           RUN_GUARD_BUSY_MS            sqlite busy timeout (default 2000)
#  When a restart is due and the DB shows a strategy run in progress (a
#  strategy_runs row with status='running', or an unexpired
#  settings.strategy_run_lock:* lease), the restart is deferred and logged,
#  up to RUN_GUARD_MAX_DEFER_SEC from the first deferral; then it restarts as
#  before.  An unreadable DB counts as "unknown" and also defers (same cap).
#  The guard fails closed: any decision status other than the documented
#  defer (0) and proceed (1) is logged and also defers, under the same cap.
#
# Probe contract: a URL whose path is /api/live (or /live) is a process-only
# liveness probe (ST app/api/live/route.ts returns {ok:true,probe:"live"}).
# Any 2xx from it is healthy; the body is not parsed, so a future shape
# change cannot restart a serving container.  Every other URL keeps the
# top-level-ok body check below (usage-monitor /api/health unchanged).

set -euo pipefail

# Top-level health only.  ST /api/health is {ok:true, checks.dependencies.*.ok}
# and a nested "ok":false (congress.trade dependency) must not count as down —
# that substring used to docker-restart production during RTH.
# Build a curl -K config for Pushover creds.  Values are escaped for curl's
# quoted config syntax (backslash, then double quote); values with CR/LF are
# rejected because they cannot be represented safely on one config line.
pushover_curl_config() {
  local t="${PUSHOVER_APP_TOKEN:-}" u="${PUSHOVER_USER_KEY:-}"
  case "$t$u" in
    *$'\n'*|*$'\r'*) return 1 ;;
  esac
  t=${t//\\/\\\\}; t=${t//\"/\\\"}
  u=${u//\\/\\\\}; u=${u//\"/\\\"}
  printf 'form-string = "token=%s"\nform-string = "user=%s"\n' "$t" "$u"
}

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

# 0 when the URL's path is a process-only liveness endpoint (/api/live, /live).
health_url_is_liveness() {
  local url="$1" path
  path="${url#*://}"
  path="/${path#*/}"
  [[ "$path" == "/${url#*://}" ]] && path="/"
  path="${path%%\?*}"
  path="${path%%#*}"
  path="${path%/}"
  [[ "$path" == "/api/live" || "$path" == "/live" ]]
}

# Judge one probe response.  0 = healthy.
#   health_response_ok <url> <http-code> < body
health_response_ok() {
  local url="$1" code="$2"
  if health_url_is_liveness "$url"; then
    case "$code" in
      2??) cat >/dev/null; return 0 ;;
      *) cat >/dev/null; return 1 ;;
    esac
  fi
  case "$code" in
    2??|3??) ;;
    *) cat >/dev/null; return 1 ;;
  esac
  health_body_ok
}

# Coolify GET/POST /applications/{uuid}/restart queues a deployment.
# After a successful docker bounce that is a second production outage.
coolify_fallback_needed() {
  local docker_ok="$1"
  [[ "$docker_ok" -eq 0 ]]
}

# ── Run guard ────────────────────────────────────────────────────────────────
# Read-only probe of the app DB: prints "running <detail>", "idle" or
# "unknown <reason>".  Never writes; -readonly plus a short busy timeout, and
# a hard `timeout` so a starved disk cannot wedge the watchdog loop.
run_guard_state() {
  local db="$1"
  local max_age="${RUN_GUARD_RUNNING_MAX_AGE_SEC:-10800}"
  local busy="${RUN_GUARD_BUSY_MS:-2000}"
  local qto="${RUN_GUARD_QUERY_TIMEOUT_SEC:-10}"
  [[ "$max_age" =~ ^[0-9]+$ ]] || max_age=10800
  [[ "$busy" =~ ^[0-9]+$ ]] || busy=2000
  [[ "$qto" =~ ^[0-9]+$ ]] || qto=10
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "unknown sqlite3-missing"; return 0
  fi
  if [[ ! -f "$db" ]]; then
    echo "unknown db-missing"; return 0
  fi
  local sql out
  sql="SELECT
  (SELECT count(*) FROM strategy_runs
     WHERE status = 'running'
       AND started_at >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-${max_age} seconds'))
  || ' ' ||
  (SELECT count(*) FROM settings
     WHERE key >= 'strategy_run_lock:' AND key < 'strategy_run_lock;'
       AND CASE WHEN json_valid(value)
                THEN json_extract(value, '\$.expiresAt') > strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                ELSE 0 END);"
  out=$(timeout "$qto" sqlite3 -readonly -batch -noheader \
        -cmd ".timeout ${busy}" "$db" "$sql" 2>/dev/null) || {
    echo "unknown query-failed"; return 0
  }
  local runs locks
  read -r runs locks <<<"$out"
  if ! [[ "${runs:-}" =~ ^[0-9]+$ && "${locks:-}" =~ ^[0-9]+$ ]]; then
    echo "unknown bad-output"; return 0
  fi
  if [[ "$runs" -gt 0 || "$locks" -gt 0 ]]; then
    echo "running runs=${runs} locks=${locks}"
  else
    echo "idle"
  fi
}

# Decide whether to defer a due restart.  Uses/updates RUN_GUARD_DEFER_SINCE.
# Returns 0 = defer now, 1 = proceed with the restart.
#   run_guard_should_defer <state-line> <now-epoch>
run_guard_should_defer() {
  local state="$1" now_ts="$2"
  local cap="${RUN_GUARD_MAX_DEFER_SEC:-900}"
  [[ "$cap" =~ ^[0-9]+$ ]] || cap=900
  case "$state" in
    idle)
      if [[ "$RUN_GUARD_DEFER_SINCE" -gt 0 ]]; then
        log "remediate: run guard idle after $(( now_ts - RUN_GUARD_DEFER_SINCE ))s of deferral; restarting"
      fi
      return 1 ;;
  esac
  if [[ "$RUN_GUARD_DEFER_SINCE" -eq 0 ]]; then
    RUN_GUARD_DEFER_SINCE="$now_ts"
  fi
  local waited=$(( now_ts - RUN_GUARD_DEFER_SINCE ))
  if [[ "$waited" -lt "$cap" ]]; then
    log "remediate: deferred (strategy run guard: ${state}; waited ${waited}s of ${cap}s)"
    return 0
  fi
  log "remediate: run guard cap reached (${state}; waited ${waited}s >= ${cap}s); restarting anyway"
  return 1
}

# Fail-closed wrapper around run_guard_should_defer.  Returns 0 = defer,
# 1 = proceed.  Only the documented proceed status (1) proceeds.  The
# documented defer status (0) defers.  Any other status (a crash, a bad
# arithmetic expansion, a signal) is logged and defers too, bounded by the
# same RUN_GUARD_MAX_DEFER_SEC cap from the first deferral so a broken guard
# cannot hold a down app forever.
#   run_guard_decide <state-line> <now-epoch>
run_guard_decide() {
  local state="$1" now_ts="$2" rc=0
  run_guard_should_defer "$state" "$now_ts" || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) return 1 ;;
  esac
  local cap="${RUN_GUARD_MAX_DEFER_SEC:-900}"
  [[ "$cap" =~ ^[0-9]+$ ]] || cap=900
  [[ "$RUN_GUARD_DEFER_SINCE" =~ ^[0-9]+$ ]] || RUN_GUARD_DEFER_SINCE=0
  if [[ "$RUN_GUARD_DEFER_SINCE" -eq 0 ]]; then
    RUN_GUARD_DEFER_SINCE="$now_ts"
  fi
  local waited=$(( now_ts - RUN_GUARD_DEFER_SINCE ))
  if [[ "$waited" -lt "$cap" ]]; then
    log "remediate: deferred (run guard decision failed rc=${rc}, failing closed; state=${state}; waited ${waited}s of ${cap}s)"
    return 0
  fi
  log "remediate: run guard decision failed rc=${rc} and cap reached (waited ${waited}s >= ${cap}s); restarting anyway"
  return 1
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
#   bash fleet-health-recover.sh --run-guard-state <app.db>   # prints running|idle|unknown
#   bash fleet-health-recover.sh --run-guard-defer <state> <since> <now>  # 0 = defer
if [ "${1:-}" = "--run-guard-state" ]; then
  log() { :; }
  run_guard_state "${2:-}"
  exit 0
fi
if [ "${1:-}" = "--run-guard-defer" ]; then
  log() { printf '%s\n' "$*" >&2; }
  RUN_GUARD_DEFER_SINCE="${3:-0}"
  rc=0
  run_guard_decide "${2:-}" "${4:-0}" || rc=$?
  exit "$rc"
fi
#   bash fleet-health-recover.sh --run-guard-decide-rc <inner-rc> <since> <now>
#   Stubs run_guard_should_defer to exit <inner-rc>; tests the fail-closed path.
if [ "${1:-}" = "--run-guard-decide-rc" ]; then
  log() { printf '%s\n' "$*" >&2; }
  inner_rc="${2:-0}"
  run_guard_should_defer() { return "$inner_rc"; }
  RUN_GUARD_DEFER_SINCE="${3:-0}"
  rc=0
  run_guard_decide "unknown stub" "${4:-0}" || rc=$?
  exit "$rc"
fi
#   bash fleet-health-recover.sh --check-response <url> <http-code> <body-file>
if [ "${1:-}" = "--check-response" ]; then
  [ -f "${4:-}" ] || exit 1
  health_response_ok "${2:-}" "${3:-}" < "$4"
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
RUN_GUARD_DB="${RUN_GUARD_DB:-}"
RUN_GUARD_DEFER_SINCE=0
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
  local _po_cfg
  if ! _po_cfg=$(pushover_curl_config); then
    log "warn: pushover config rejected (token/user contains CR/LF); notify skipped"
    return 0
  fi
  curl -sS -m 15 -o /dev/null \
    -K <(printf '%s\n' "$_po_cfg") \
    --form-string "title=${title}" \
    --form-string "message=${msg}" \
    --form-string "priority=1" \
    https://api.pushover.net/1/messages.json >/dev/null 2>&1 \
    || log "warn: pushover notify failed (curl exit $?; bad config or network)"
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
  printf '%s' "$body" | health_response_ok "$url" "$code"
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

  if [[ -n "$RUN_GUARD_DB" ]]; then
    local guard_state guard_rc=0
    guard_state=$(run_guard_state "$RUN_GUARD_DB") || guard_state="unknown state-probe-failed"
    [[ -n "$guard_state" ]] || guard_state="unknown empty-state"
    run_guard_decide "$guard_state" "$(now)" || guard_rc=$?
    if [[ "$guard_rc" -eq 0 ]]; then
      return 0
    fi
  fi
  RUN_GUARD_DEFER_SINCE=0

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

log "start app=$APP_NAME resource=$RESOURCE_NAME health=$INTERNAL_HEALTH_URL interval=${CHECK_INTERVAL_SEC}s run_guard=${RUN_GUARD_DB:-off}"

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
    RUN_GUARD_DEFER_SINCE=0
  else
    FAILS=$((FAILS + 1))
    log "health FAIL count=$FAILS/$FAIL_THRESHOLD"
    if [[ "$FAILS" -ge "$FAIL_THRESHOLD" ]]; then
      remediate || log "remediate: failed"
    fi
  fi
  sleep "$CHECK_INTERVAL_SEC"
done
