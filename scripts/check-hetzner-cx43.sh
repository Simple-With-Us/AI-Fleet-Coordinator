#!/bin/bash
# Watch for a cheaper 8-vCPU Hetzner type than the live fleet box.
#
# Live host (see private fleet-ops:ATTACK-MAP.md): cx43 in nbg1.
#
# HIT only when an 8-vCPU type cheaper than the current monthly price is
# available for migration in nbg1.  cx43 itself being listed is not a HIT
# -- we already run that.  Never echoes the token.
# Live cron runs ~/apps/check-hetzner-cx43.sh -- keep that copy in sync.

set -uo pipefail

# Cron's PATH is minimal, so the absolute path;  override with FLEET_SECRETS_BIN.
FLEET_SECRETS="${FLEET_SECRETS_BIN:-/Users/jay/.local/bin/fleet-secrets}"
LOG="/Users/jay/apps/hetzner-upgrade-watch.log"
SERVER_ID="${HETZNER_SERVER_ID:-hetzner-box}"
DC="nbg1-dc3"
CURRENT_TYPE="cx43"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

# Environment first, then `fleet-secrets get` (OPS-818;  the old flat secrets file is retired, AFC-359).
TOKEN="${HCLOUD_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -x "$FLEET_SECRETS" ]; then
  TOKEN="$("$FLEET_SECRETS" get HCLOUD_TOKEN 2>/dev/null || true)"
fi
if [ -z "$TOKEN" ]; then
  echo "$STAMP  ERROR: HCLOUD_TOKEN missing/empty" >> "$LOG"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -sS --max-time 20 -H "Authorization: Bearer $TOKEN" \
  'https://api.hetzner.cloud/v1/server_types?per_page=50' \
  -o "$TMP/st.json" 2>/dev/null
curl -sS --max-time 20 -H "Authorization: Bearer $TOKEN" \
  'https://api.hetzner.cloud/v1/datacenters' \
  -o "$TMP/dc.json" 2>/dev/null

RESULT="$(/usr/bin/python3 - "$TMP/st.json" "$TMP/dc.json" "$DC" "$CURRENT_TYPE" <<'PY'
import json, sys
try:
    st = json.load(open(sys.argv[1]))['server_types']
    dcs = json.load(open(sys.argv[2]))['datacenters']
except Exception as e:
    print("ERROR|could not parse Hetzner API response: %s" % type(e).__name__)
    raise SystemExit(0)

dc_name = sys.argv[3]
current_name = sys.argv[4]
loc = dc_name.split("-")[0]
byname = {t['name']: t for t in st}
target_dc = next((D for D in dcs if D['name'] == dc_name), None)
if target_dc is None:
    print("ERROR|datacenter %s not found" % dc_name); raise SystemExit(0)

def monthly(t):
    prices = t.get('prices') or []
    loc_hit = [p for p in prices if p.get('location') == loc]
    pick = loc_hit or prices
    if not pick:
        return None
    try:
        return float(pick[0]['price_monthly']['gross'])
    except Exception:
        return None

cur = byname.get(current_name)
cur_price = monthly(cur) if cur else None
mig = set(target_dc['server_types'].get('available_for_migration', []))
hits = []
for t in st:
    if t.get('cores') != 8:
        continue
    if t['id'] not in mig:
        continue
    p = monthly(t)
    if p is None:
        continue
    if cur_price is not None and p >= cur_price:
        continue
    hits.append("%s (%d vCPU / %.0f GB, EUR %.2f/mo)" % (
        t['name'], t['cores'], t['memory'], p))
if hits:
    print("HIT|" + "; ".join(sorted(hits)))
else:
    extra = " current=%s EUR %.2f/mo" % (current_name, cur_price) if cur_price is not None else ""
    print("MISS|no cheaper 8-vCPU than %s in %s%s" % (current_name, dc_name, extra))
PY
)"

STATUS="${RESULT%%|*}"
DETAIL="${RESULT#*|}"
echo "$STAMP  $STATUS  $DETAIL" >> "$LOG"

if [ "$STATUS" = "HIT" ]; then
  BANNER="HETZNER CHEAPER 8-vCPU AVAILABLE: $DETAIL -- live host id $SERVER_ID is $CURRENT_TYPE in $DC.  Review before any change_type."
  echo "$STAMP  *** $BANNER" >> "$LOG"
  osascript -e "display notification \"$DETAIL\" with title \"Hetzner cx43/cpx41 now migratable\"" 2>/dev/null || true
  # No effort-log line any more:  the effort logs are archived (Linear cutover).  The log above and
  # the notification are the record;  a person files the Linear issue (`board file --app OPS`) if the
  # migration is worth doing.
fi
