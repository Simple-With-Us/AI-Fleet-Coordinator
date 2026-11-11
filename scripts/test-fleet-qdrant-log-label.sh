#!/usr/bin/env bash
# Offline tests for the Central-time log labels in fleet-qdrant-snapshot.sh.
# No Docker, no Qdrant, no network.
#   bash scripts/test-fleet-qdrant-log-label.sh
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="${ROOT_DIR}/scripts/hetzner/fleet-qdrant-snapshot.sh"
fail=0
assert_eq() {
  if [ "$1" != "$2" ]; then echo "FAIL $3: got '$1' want '$2'"; fail=1; else echo "ok   $3"; fi
}
bash -n "$SCRIPT"
QDRANT_SNAPSHOT_LIB_ONLY=1
# shellcheck source=/dev/null
. "$SCRIPT"
assert_eq "$(qdrant_ct_label 20261030T032000Z)" "20261030T032000Z (10:20pm CDT, -05:00)" "label CDT"
assert_eq "$(qdrant_ct_label 20261103T042000Z)" "20261103T042000Z (10:20pm CST, -06:00)" "label CST after DST"
assert_eq "$(qdrant_ct_label 20261101T063000Z)" "20261101T063000Z (1:30am CDT, -05:00)" "first 1:30am on fall-back night"
assert_eq "$(qdrant_ct_label 20261101T073000Z)" "20261101T073000Z (1:30am CST, -06:00)" "second 1:30am on fall-back night"
assert_eq "$(qdrant_ct_label not-a-stamp)" "not-a-stamp" "unparseable falls back to raw"
case "$(qdrant_ct_label)" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z\ \(*\ C[DS]T,\ -0[56]:00\)) r=ok ;;
  *) r=bad ;;
esac
assert_eq "$r" ok "default label is now"
grep -q 'STAMP="$(date -u +%Y%m%dT%H%M%SZ)"' "$SCRIPT" && r=yes || r=no
assert_eq "$r" yes "file/object stamp stays UTC Z"
grep -q 'start $(qdrant_ct_label "$STAMP")' "$SCRIPT" && r=yes || r=no
assert_eq "$r" yes "start line carries Central label"
grep -q 'done $(qdrant_ct_label "$STAMP")' "$SCRIPT" && r=yes || r=no
assert_eq "$r" yes "done line carries Central label"
[ "$fail" -eq 0 ] || { echo FAILED; exit 1; }
echo "all tests passed"
