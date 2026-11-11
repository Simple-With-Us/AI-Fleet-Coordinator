#!/usr/bin/env bash
# Exercise scripts/recall-tunnel's `up` retry loop against a fake `ssh` and `curl` on PATH --
# no real network, no real SSH control master.  Covers: retry-then-succeed, give-up-after-N,
# and idempotency (an already-up tunnel never re-attempts SSH).
#
#   cd scripts && bash fleet_rag/tests/test_recall_tunnel.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$(cd "$HERE/../.." && pwd)"
TUNNEL="$SCRIPTS/recall-tunnel"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/fleet-recall-tunnel-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT   # replaced below (adds reap_supervisors) once that helper exists

FAILS=0
pass() { printf 'ok   - %s\n' "$1"; }
fail() { printf 'FAIL - %s\n' "$1"; FAILS=$((FAILS + 1)); }
assert() {  # assert <desc> <command...>
  local desc="$1"; shift
  if "$@"; then pass "$desc"; else fail "$desc"; fi
}

# -- fake `ssh`: distinguishes the tunnel-open attempt (`-M`, the control master) from a
# status probe (`-O check` / `-O exit`, what master_alive()/cmd_down use).  A "MARKER" file
# stands in for a real, persistent SSH control master: an open attempt that is allowed to
# succeed creates it; `-O check` reports alive only while it exists.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/ssh" <<'SSH'
#!/usr/bin/env bash
MARKER="${FAKE_SSH_MARKER:?}"
FAIL_UNTIL="${FAKE_SSH_FAIL_UNTIL:-0}"   # open attempts before #FAIL_UNTIL fail; that one succeeds
is_open_attempt=0
for a in "$@"; do [[ "$a" == "-M" ]] && is_open_attempt=1; done
if [[ "$is_open_attempt" == "1" ]]; then
  # OPEN_LOG is only needed for an actual open attempt (-M) -- a status probe (-O check / -O
  # exit) never touches it, so it stays unrequired for those, and callers that only probe
  # status (this file's `status`/`down` tests) don't need to set FAKE_SSH_OPEN_LOG at all.
  OPEN_LOG="${FAKE_SSH_OPEN_LOG:?}"
  n=0
  [[ -f "$OPEN_LOG" ]] && n=$(wc -l < "$OPEN_LOG")
  n=$((n + 1))
  echo "$n" >> "$OPEN_LOG"
  if (( FAIL_UNTIL > 0 && n < FAIL_UNTIL )); then
    echo "fake ssh: simulated open failure (attempt $n)" >&2
    exit 255
  fi
  : > "$MARKER"
  exit 0
fi
# -O check / -O exit: report/clear the marker like a real control master would
if [[ "$*" == *"-O exit"* ]]; then
  rm -f "$MARKER"
  exit 0
fi
[[ -f "$MARKER" ]] && exit 0 || exit 1
SSH
chmod +x "$TMP/bin/ssh"

# -- fake `curl`: check_port always reports FAIL, harmlessly, no network
cat > "$TMP/bin/curl" <<'CURL'
#!/usr/bin/env bash
exit 1
CURL
chmod +x "$TMP/bin/curl"

# Any supervisor a test starts for real (a background `nohup bash recall-tunnel __supervise__`)
# must not outlive this script -- it would keep polling paths under $TMP after `trap ... EXIT`
# removes it.  Tests that care about the supervisor stop it explicitly; this is the safety net.
SUP_PIDS_TO_REAP=()
reap_supervisors() {
  local pid
  for pid in "${SUP_PIDS_TO_REAP[@]:-}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
}
trap 'reap_supervisors; rm -rf "$TMP"' EXIT

run_up() {  # run_up <fail_until> <attempts> <retry_sleep> -- prints "RC=<n>" as the last stdout line
  local fail_until="$1" attempts="$2" retry_sleep="$3"
  rm -f "$TMP/marker" "$TMP/open.log"
  mkdir -p "$TMP/home"
  local rc=0
  PATH="$TMP/bin:$PATH" \
    FAKE_SSH_MARKER="$TMP/marker" FAKE_SSH_OPEN_LOG="$TMP/open.log" FAKE_SSH_FAIL_UNTIL="$fail_until" \
    RECALL_TUNNEL_ATTEMPTS="$attempts" RECALL_TUNNEL_RETRY_SLEEP="$retry_sleep" \
    RECALL_TUNNEL_SUPERVISE=0 \
    FLEET_RAG_HOME="$TMP/home" \
    bash "$TUNNEL" up > "$TMP/up.out" 2> "$TMP/up.err" || rc=$?
  echo "RC=$rc"
}

open_attempts() {
  if [[ -f "$TMP/open.log" ]]; then wc -l < "$TMP/open.log" | tr -d '[:space:]'; else echo 0; fi
}

echo "== retry then succeed (fails twice, succeeds on the 3rd open attempt)"
rc="$(run_up 3 5 0 | sed -n 's/^RC=//p')"
assert "exits 0 once ssh eventually succeeds" test "$rc" = "0"
assert "stdout reports tunnel opened" grep -q "tunnel opened" "$TMP/up.out"
assert "made exactly 3 open attempts" test "$(open_attempts)" = "3"
assert "stderr logs attempt 1/5 failed" grep -q "attempt 1/5 failed" "$TMP/up.err"
assert "stderr logs attempt 2/5 failed" grep -q "attempt 2/5 failed" "$TMP/up.err"
assert "stderr logs a retry message" grep -q "retrying in 0s" "$TMP/up.err"

echo "== give up after the last attempt (never succeeds)"
rc="$(run_up 999 3 0 | sed -n 's/^RC=//p')"   # FAIL_UNTIL far beyond `attempts`: every attempt fails
assert "exits non-zero after the last attempt" test "$rc" != "0"
assert "made exactly 3 open attempts, no more" test "$(open_attempts)" = "3"
assert "stderr logs giving up after 3 attempt(s)" grep -q "giving up after 3 attempt(s)" "$TMP/up.err"
assert "stdout never claims the tunnel opened" bash -c "! grep -q 'tunnel opened' '$TMP/up.out'"

echo "== idempotent: an already-up tunnel never re-attempts SSH"
rm -f "$TMP/open.log"
: > "$TMP/marker"          # simulate a control master already alive from a prior `up`
mkdir -p "$TMP/home"
rc=0
PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FAKE_SSH_OPEN_LOG="$TMP/open.log" \
  FAKE_SSH_FAIL_UNTIL=0 RECALL_TUNNEL_ATTEMPTS=5 RECALL_TUNNEL_RETRY_SLEEP=0 RECALL_TUNNEL_SUPERVISE=0 \
  FLEET_RAG_HOME="$TMP/home" \
  bash "$TUNNEL" up > "$TMP/up2.out" 2> "$TMP/up2.err" || rc=$?
assert "exits 0 when already up" test "$rc" = "0"
assert "stdout says tunnel already up" grep -q "tunnel already up" "$TMP/up2.out"
assert "made zero open attempts" test "$(open_attempts)" = "0"

echo "== supervisor: starts on \`up\` by default, \`status\` reports it, \`down\` stops it first"
rm -f "$TMP/marker" "$TMP/open.log"
SUP_HOME="$TMP/sup-home"
mkdir -p "$SUP_HOME"
SUP_SOCK="$SUP_HOME/state/tunnel.sock"
rc=0
PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FAKE_SSH_OPEN_LOG="$TMP/open.log" \
  FAKE_SSH_FAIL_UNTIL=0 RECALL_TUNNEL_ATTEMPTS=5 RECALL_TUNNEL_RETRY_SLEEP=0 \
  FLEET_RAG_HOME="$SUP_HOME" \
  bash "$TUNNEL" up > "$TMP/sup_up.out" 2> "$TMP/sup_up.err" || rc=$?
assert "up (default supervise) exits 0" test "$rc" = "0"
assert "supervisor pid file exists right after \`up\` returns" test -f "${SUP_SOCK}.supervisor.pid"
SUP_PID="$(cat "${SUP_SOCK}.supervisor.pid" 2>/dev/null || echo)"
SUP_PIDS_TO_REAP+=("$SUP_PID")
assert "supervisor process is actually running" bash -c "[[ -n '$SUP_PID' ]] && kill -0 '$SUP_PID' 2>/dev/null"

rc=0
PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FLEET_RAG_HOME="$SUP_HOME" \
  bash "$TUNNEL" status > "$TMP/sup_status.out" 2>&1 || rc=$?
assert "status (tunnel up) exits 0" test "$rc" = "0"
assert "status reports the supervisor running" grep -q "supervisor: running" "$TMP/sup_status.out"

rc=0
PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FLEET_RAG_HOME="$SUP_HOME" \
  bash "$TUNNEL" down > "$TMP/sup_down.out" 2>&1 || rc=$?
assert "down stops the supervisor (pid file gone)" bash -c "[[ ! -f '${SUP_SOCK}.supervisor.pid' ]]"
assert "down kills the supervisor process, not just the pid file" \
  bash -c "[[ -n '$SUP_PID' ]] && ! kill -0 '$SUP_PID' 2>/dev/null"

echo "== supervisor: RECALL_TUNNEL_SUPERVISE=0 disables it"
rm -f "$TMP/marker" "$TMP/open.log"
NOSUP_HOME="$TMP/nosup-home"
mkdir -p "$NOSUP_HOME"
NOSUP_SOCK="$NOSUP_HOME/state/tunnel.sock"
rc=0
PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FAKE_SSH_OPEN_LOG="$TMP/open.log" \
  FAKE_SSH_FAIL_UNTIL=0 RECALL_TUNNEL_ATTEMPTS=5 RECALL_TUNNEL_RETRY_SLEEP=0 RECALL_TUNNEL_SUPERVISE=0 \
  FLEET_RAG_HOME="$NOSUP_HOME" \
  bash "$TUNNEL" up > "$TMP/nosup_up.out" 2> "$TMP/nosup_up.err" || rc=$?
assert "up (supervise=0) exits 0" test "$rc" = "0"
assert "no supervisor pid file is created" bash -c "[[ ! -f '${NOSUP_SOCK}.supervisor.pid' ]]"

echo "== supervisor self-check: an orphaned supervisor (pid file no longer its own) exits within one poll interval"
# Bypasses start_supervisor's lock entirely to simulate exactly the race it normally prevents:
# two processes sharing one SUP_PID_FILE, the second's write clobbering the first's.  Covers
# fix (a) -- the loop's own self-termination -- independently of fix (b), the lock.
rm -f "$TMP/marker" "$TMP/open.log"
SELFCHECK_HOME="$TMP/selfcheck-home"
mkdir -p "$SELFCHECK_HOME"
SELFCHECK_SOCK="$SELFCHECK_HOME/state/tunnel.sock"
mkdir -p "$(dirname "$SELFCHECK_SOCK")"
: > "$TMP/marker"   # tunnel already "up" so the loop never tries to reopen it

# Process A: a real `__supervise__` process, told (via a poll interval of 1s, RECALL_TUNNEL_
# SUPERVISE_INTERVAL) to check ownership often so this test stays fast.  Its own pid is written
# to SUP_PID_FILE right after backgrounding, exactly like start_supervisor does.
PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FLEET_RAG_HOME="$SELFCHECK_HOME" \
  RECALL_TUNNEL_SUPERVISE_INTERVAL=1 \
  bash "$TUNNEL" __supervise__ > "$TMP/selfcheck_a.log" 2>&1 &
PID_A=$!
SUP_PIDS_TO_REAP+=("$PID_A")
echo "$PID_A" > "${SELFCHECK_SOCK}.supervisor.pid"
sleep 0.3   # let A clear its startup grace-wait and enter the main loop
assert "process A is running once it owns the pid file" kill -0 "$PID_A"

# Simulate a concurrent second start_supervisor clobbering the pid file with a different pid
# (a plain `sleep`, standing in for "process B" -- only its liveness matters here).
sleep 5 &
PID_B=$!
SUP_PIDS_TO_REAP+=("$PID_B")
echo "$PID_B" > "${SELFCHECK_SOCK}.supervisor.pid"

sleep 2   # >= 2 poll intervals at RECALL_TUNNEL_SUPERVISE_INTERVAL=1
assert "orphaned process A has exited on its own" bash -c "! kill -0 '$PID_A' 2>/dev/null"
SELFCHECK_PID_FILE_CONTENT="$(cat "${SELFCHECK_SOCK}.supervisor.pid" 2>/dev/null || echo)"
assert "pid file still names B -- A did not delete state it no longer owns" \
  test "$SELFCHECK_PID_FILE_CONTENT" = "$PID_B"
kill "$PID_B" 2>/dev/null || true

echo "== supervisor lock: two concurrent \`up\`s -- exactly one supervisor survives a poll interval, \`down\` leaves zero"
rm -f "$TMP/marker" "$TMP/open.log"
CONC_HOME="$TMP/conc-home"
mkdir -p "$CONC_HOME"
CONC_SOCK="$CONC_HOME/state/tunnel.sock"

run_conc_up() {
  PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FAKE_SSH_OPEN_LOG="$TMP/open.log" \
    FAKE_SSH_FAIL_UNTIL=0 RECALL_TUNNEL_ATTEMPTS=5 RECALL_TUNNEL_RETRY_SLEEP=0 \
    RECALL_TUNNEL_SUPERVISE_INTERVAL=1 \
    FLEET_RAG_HOME="$CONC_HOME" \
    bash "$TUNNEL" up
}
run_conc_up > "$TMP/conc_up_1.out" 2> "$TMP/conc_up_1.err" &
CONC_PID_1=$!
run_conc_up > "$TMP/conc_up_2.out" 2> "$TMP/conc_up_2.err" &
CONC_PID_2=$!
rc1=0; rc2=0
wait "$CONC_PID_1" || rc1=$?
wait "$CONC_PID_2" || rc2=$?
assert "first concurrent up exits 0" test "$rc1" = "0"
assert "second concurrent up exits 0" test "$rc2" = "0"

assert "pid file exists after both ups return" test -f "${CONC_SOCK}.supervisor.pid"
CONC_SUP_PID="$(cat "${CONC_SOCK}.supervisor.pid" 2>/dev/null || echo)"
SUP_PIDS_TO_REAP+=("$CONC_SUP_PID")
assert "supervisor named in the pid file is alive" \
  bash -c "[[ -n '$CONC_SUP_PID' ]] && kill -0 '$CONC_SUP_PID' 2>/dev/null"

sleep 2   # >= 2 poll intervals: an orphan the old, unlocked start_supervisor could have left
          # behind would already have exited by now (belt-and-suspenders check); the lock
          # should mean there never was one.
SUP_COUNT="$(pgrep -f "$TUNNEL __supervise__" 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
assert "exactly one supervisor process exists for this pid file" test "$SUP_COUNT" = "1"

rc=0
PATH="$TMP/bin:$PATH" FAKE_SSH_MARKER="$TMP/marker" FLEET_RAG_HOME="$CONC_HOME" \
  bash "$TUNNEL" down > "$TMP/conc_down.out" 2>&1 || rc=$?
assert "down exits 0" test "$rc" = "0"
assert "down stops the surviving supervisor" bash -c "! kill -0 '$CONC_SUP_PID' 2>/dev/null"
assert "down removes the pid file" bash -c "[[ ! -f '${CONC_SOCK}.supervisor.pid' ]]"
SUP_COUNT_AFTER_DOWN="$(pgrep -f "$TUNNEL __supervise__" 2>/dev/null | wc -l | tr -d '[:space:]' || true)"
assert "down leaves zero supervisor processes" test "$SUP_COUNT_AFTER_DOWN" = "0"

echo "== env: exports the wider HTTP retry budget for the tunnel path"
bash "$TUNNEL" env > "$TMP/env.out"
assert "exports QDRANT_URL" grep -q "^export QDRANT_URL=http://127.0.0.1:16333$" "$TMP/env.out"
assert "exports RECALL_HTTP_RETRIES=8" grep -q "^export RECALL_HTTP_RETRIES=8$" "$TMP/env.out"
assert "exports RECALL_HTTP_BACKOFF_MAX=60" grep -q "^export RECALL_HTTP_BACKOFF_MAX=60$" "$TMP/env.out"

echo
if [[ "$FAILS" -eq 0 ]]; then
  echo "ALL PASS (test_recall_tunnel.sh)"
else
  echo "$FAILS FAILURE(S) (test_recall_tunnel.sh)"
  exit 1
fi
