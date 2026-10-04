#!/bin/bash
# Contract: the disk janitor's KEEP_RE must protect every always-on runtime that
# owns a node_modules tree or a state home.
#
# Why this is its own file instead of an addition to test-disk-janitor-match.sh:
# that suite has been red on AFC main since PR #318 (it still asserts the
# pre-#318 pressure-gate defaults and the removed `cleanmymac clean dev` call),
# so anything added there would never actually run.  Fixing that suite belongs
# to whoever owns the janitor tuning, not to a rename PR.
#
# The failure mode this guards against is silent.  A reaped node_modules does not
# take a running service down immediately — the process keeps serving from
# memory and reports healthy while its own restart is what fails.  On
# 2026-10-03 that is exactly how ~/apps/agent-sync-push lost `ws` and killed
# Slack event fan-out for every seat on this Mac for a day and a half.  The
# tracked KEEP_RE had silently diverged from the live one, which is the other
# half of the bug: a fix that only lands on the Mac is one re-sync away from
# being lost.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
JANITOR="${ROOT}/disk-janitor.sh"

fail() { echo "FAIL $*" >&2; exit 1; }

[ -f "$JANITOR" ] || fail "missing $JANITOR"

KEEP_LINE=$(grep -n '^KEEP_RE=' "$JANITOR" | head -1 | cut -d: -f1)
[ -n "$KEEP_LINE" ] || fail "KEEP_RE not found in $JANITOR"

# Exact-token membership.  Splitting the list into whole alternatives means
# `agent-sync` can never be satisfied by `agent-sync-push`, and a prefix match
# cannot stand in for a real entry.  The leading `^"^(` and trailing `))$"` are
# regex syntax, not part of any path.
KEEP_TOKENS=$(sed -n "${KEEP_LINE}p" "$JANITOR" \
  | tr '()"|' '\n\n\n\n' | sed 's/^KEEP_RE=//; s/\^$//' | grep -v '^$')

for keep in agent-sync agent-sync-push clutch-runtime botfleet-server mac-collab; do
  printf '%s\n' "$KEEP_TOKENS" | grep -qx "$keep" \
    || fail "KEEP_RE must protect $keep (line $KEEP_LINE)"
done

# A runtime that has been renamed away must not linger in the keep-list.  It
# matches nothing, so it costs nothing, but it reads as if the successor were
# covered — which is how harness-runtime sat in this list for weeks after the
# live directory became clutch-runtime and the protection was in fact gone.
if printf '%s\n' "$KEEP_TOKENS" | grep -qx "harness-runtime"; then
  fail "KEEP_RE still lists the retired harness-runtime (line $KEEP_LINE)"
fi

echo OK
