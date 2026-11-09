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

echo "ok  health-body $(bash -n "$RECOVER" && echo 'bash -n clean')"
