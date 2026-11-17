#!/bin/bash
# Contract tests for fleet-health-recover.sh --check-body.
# A live ST payload (top-level ok:true, nested dependency ok:false) must
# pass.  Substring-grepping "ok":false used to bounce production.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RECOVER="${ROOT}/fleet-health-recover.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

check() {
  bash "$RECOVER" --check-body "$@"
}

assert_ok() {
  if ! check "$1"; then
    echo "FAIL expected pass: $1" >&2
    exit 1
  fi
}

assert_no() {
  if check "$1"; then
    echo "FAIL expected reject: $1" >&2
    exit 1
  fi
}

# Live 2026-08-21 ST shape (trimmed): process healthy, one dependency down.
printf '%s\n' '{"ok":true,"checks":{"db":"ok","dependencies":{"congress.trade":{"ok":false},"usage-monitor":{"ok":true}}}}' \
  >"${tmp}/st-nested.json"
assert_ok "${tmp}/st-nested.json"

# UM / CT liveness
printf '%s\n' '{"ok":true,"status":"live"}' >"${tmp}/um.json"
assert_ok "${tmp}/um.json"
printf '%s\n' '{"ok":true,"db":true}' >"${tmp}/ct.json"
assert_ok "${tmp}/ct.json"

# Real downs
printf '%s\n' '{"ok":false,"checks":{"dependencies":{"x":{"ok":true}}}}' >"${tmp}/top-false.json"
assert_no "${tmp}/top-false.json"
printf '%s\n' '{"ok":true,"db":false}' >"${tmp}/db-false.json"
assert_no "${tmp}/db-false.json"
printf '%s\n' '{"status":"live"}' >"${tmp}/no-ok.json"
assert_no "${tmp}/no-ok.json"
printf '%s\n' 'not-json' >"${tmp}/bad.json"
assert_no "${tmp}/bad.json"
printf '%s\n' '[]' >"${tmp}/arr.json"
assert_no "${tmp}/arr.json"

# Spaces around colon still parse as JSON
printf '%s\n' '{ "ok" : true, "nested": { "ok" : false } }' >"${tmp}/spaces.json"
assert_ok "${tmp}/spaces.json"

# Coolify /restart queues a deployment.  Only when docker could not bounce.
if bash "$RECOVER" --coolify-fallback 1; then
  echo "FAIL expected no Coolify fallback after docker ok" >&2
  exit 1
fi
if ! bash "$RECOVER" --coolify-fallback 0; then
  echo "FAIL expected Coolify fallback when docker failed" >&2
  exit 1
fi

# Run guard (ST-4635): defer a due restart while a strategy run is in progress.
if command -v sqlite3 >/dev/null 2>&1; then
  db="${tmp}/app.db"
  sqlite3 "$db" "CREATE TABLE strategy_runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL);
                 CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
  guard() { bash "$RECOVER" --run-guard-state "$db"; }
  expect_state() {
    local got; got="$(guard)"
    case "$got" in
      "$1"*) ;;
      *) echo "FAIL run guard expected '$1', got '$got' ($2)" >&2; exit 1 ;;
    esac
  }
  expect_state idle "empty tables"
  sqlite3 "$db" "INSERT INTO strategy_runs VALUES ('r1', strftime('%Y-%m-%dT%H:%M:%fZ','now','-60 seconds'), NULL, 'running');"
  expect_state "running runs=1 locks=0" "fresh running row"
  sqlite3 "$db" "UPDATE strategy_runs SET started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now','-5 hours');"
  expect_state idle "orphaned running row older than RUN_GUARD_RUNNING_MAX_AGE_SEC"
  sqlite3 "$db" "UPDATE strategy_runs SET status='completed';
                 INSERT INTO settings VALUES ('strategy_run_lock:u1:a1', json_object('owner','x','expiresAt', strftime('%Y-%m-%dT%H:%M:%fZ','now','+240 seconds')), '');
                 INSERT INTO settings VALUES ('strategy_run_lock:u2', 'not json', '');
                 INSERT INTO settings VALUES ('strategy_run_lockout', json_object('expiresAt','9999-01-01T00:00:00.000Z'), '');"
  expect_state "running runs=0 locks=1" "live lease; malformed and look-alike keys ignored"
  sqlite3 "$db" "UPDATE settings SET value = json_object('owner','x','expiresAt', strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 seconds')) WHERE key='strategy_run_lock:u1:a1';"
  expect_state idle "expired lease"
  got="$(bash "$RECOVER" --run-guard-state "${tmp}/missing.db")"
  [[ "$got" == "unknown db-missing" ]] || { echo "FAIL missing db -> '$got'" >&2; exit 1; }
  printf 'garbage' >"${tmp}/bad.db"
  got="$(bash "$RECOVER" --run-guard-state "${tmp}/bad.db")"
  [[ "$got" == unknown* ]] || { echo "FAIL corrupt db -> '$got'" >&2; exit 1; }
  echo "ok  run-guard-state (sqlite3)"
else
  echo "skip run-guard-state (no sqlite3)"
fi

defer() { RUN_GUARD_MAX_DEFER_SEC=900 bash "$RECOVER" --run-guard-defer "$@" 2>/dev/null; }
defer "running runs=1 locks=0" 0 1000 || { echo "FAIL first deferral should defer" >&2; exit 1; }
defer "running runs=1 locks=0" 1000 1899 || { echo "FAIL 899s should still defer" >&2; exit 1; }
if defer "running runs=1 locks=0" 1000 1900; then echo "FAIL cap reached should restart" >&2; exit 1; fi
defer "unknown query-failed" 0 1000 || { echo "FAIL unknown should defer" >&2; exit 1; }
if defer "idle" 1000 1200; then echo "FAIL idle should restart" >&2; exit 1; fi
if defer "idle" 0 1200; then echo "FAIL idle with no deferral should restart" >&2; exit 1; fi

echo "ok  health-body $(bash -n "$RECOVER" && echo 'bash -n clean')"
