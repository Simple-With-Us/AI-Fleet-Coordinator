#!/usr/bin/env bash
# Hermetic test of scripts/infisical-secrets-sync.sh:  a stub CLI stands in for infisical, a
# temp dir stands in for the handoff file and log.  No network, no real secret, no keychain.
#
#   bash scripts/test-infisical-secrets-sync.sh
set -u
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/infisical-secrets-sync.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fail=0
check() { # name, command...
  local name="$1"; shift
  if "$@"; then echo "ok   $name"; else echo "FAIL $name"; fail=1; fi
}

# Stub CLI.  With --plain it prints only the value (or STUB_PLAIN);  without it prints a
# box-drawn table whose last field is a border, the shape that broke the old parsing.
cat > "$tmp/infisical" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_ARGS"
plain=0; for a in "$@"; do [ "$a" = "--plain" ] && plain=1; done
if [ "$plain" = 1 ]; then
  printf '%s\n' "${STUB_PLAIN-stub-token-0123456789abcdefghij}"
else
  printf '┌────┬────┐\n│ CLOUDFLARE_API_TOKEN │ stub-token-0123456789abcdefghij │\n└────┴────┘\n'
fi
EOF
chmod +x "$tmp/infisical"

export SYNC_INFISICAL_BIN="$tmp/infisical" SYNC_FILE="$tmp/handoff" SYNC_LOG="$tmp/log" STUB_ARGS="$tmp/args"
printf 'OTHER_KEY=keep-me\n' > "$SYNC_FILE"; chmod 600 "$SYNC_FILE"

check "map names prod and never dev or staging" \
  bash -c "grep -q '|prod|CLOUDFLARE_API_TOKEN\"' '$script' && ! grep -E '^ +\"[A-Z_]+\\|[^|]+\\|(dev|staging)\\|' '$script'"

check "map carries the fleet-work-backup keys, all prod" bash -c \
  "for k in RESTIC_FLEET_WORK_PASSWORD B2_FLEET_WORK_KEY_ID B2_FLEET_WORK_APPLICATION_KEY; do grep -q \"|prod|\$k\\\"\" '$script' || exit 1; done"

bash "$script"
check "reads with --env=prod --plain --silent" grep -q -- '--env=prod --plain --silent' "$STUB_ARGS"
check "never asks for dev" bash -c "! grep -q -- '--env=dev' '$STUB_ARGS'"
check "writes the value from the plain output" grep -q '^CLOUDFLARE_API_TOKEN=stub-token-0123456789abcdefghij$' "$SYNC_FILE"
check "writes the fleet-work-backup keys too" bash -c \
  "grep -q '^RESTIC_FLEET_WORK_PASSWORD=stub-token-0123456789abcdefghij$' '$SYNC_FILE' && grep -q '^B2_FLEET_WORK_KEY_ID=stub-token-0123456789abcdefghij$' '$SYNC_FILE' && grep -q '^B2_FLEET_WORK_APPLICATION_KEY=stub-token-0123456789abcdefghij$' '$SYNC_FILE'"
check "keeps other keys" grep -q '^OTHER_KEY=keep-me$' "$SYNC_FILE"
check "logs the update by length only" grep -q 'updated CLOUDFLARE_API_TOKEN (new_len=31)' "$SYNC_LOG"
check "handoff file stays mode 600" test "$(stat -c %a "$SYNC_FILE" 2>/dev/null || stat -f %Lp "$SYNC_FILE")" = 600
check "the log never holds the value" bash -c "! grep -q 'stub-token' '$SYNC_LOG'"

: > "$SYNC_LOG"
bash "$script"
check "second run is a no-op" grep -q 'no changes' "$SYNC_LOG"

: > "$SYNC_LOG"
STUB_PLAIN='short' bash "$script"
check "implausible value is rejected, file untouched" bash -c \
  "grep -q 'WARN: fetch failed or implausible for CLOUDFLARE_API_TOKEN (got_len=5)' '$SYNC_LOG' && grep -q '^CLOUDFLARE_API_TOKEN=stub-token-0123456789abcdefghij$' '$SYNC_FILE'"

: > "$SYNC_LOG"
STUB_PLAIN='two words that are long enough' bash "$script"
check "value with spaces is rejected" grep -q 'WARN: fetch failed or implausible' "$SYNC_LOG"

[ "$fail" -eq 0 ] && echo "all passed" || echo "FAILED"
exit "$fail"
