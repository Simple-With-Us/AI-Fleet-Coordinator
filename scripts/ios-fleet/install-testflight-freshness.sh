#!/usr/bin/env bash
# Install the TestFlight freshness check on this Mac (AFC-341).
#
#   bash scripts/ios-fleet/install-testflight-freshness.sh --dry-run   # show the plan
#   bash scripts/ios-fleet/install-testflight-freshness.sh             # install or update
#   bash scripts/ios-fleet/install-testflight-freshness.sh --no-launchd   # files only
#
# Run it from an AI-Fleet-Coordinator lane at origin/main, never from a stale tree.
# It copies the tracked check and its registry into ~/apps/ios-fleet (the install
# target the fleet's TestFlight scripts already live in; asc-api.mjs and
# build-window.sh stay there and are not touched), and installs the LaunchAgent
# com.jay.ios-testflight-freshness.  A replaced file is kept as
# <file>.bak-freshness-<stamp>.  The LaunchAgent is not RunAtLoad, so loading it
# never dispatches a ship by itself.  Register changes in MAC-LOCAL-PROCESSES.md.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="${IOS_FLEET_DIR:-$HOME/apps/ios-fleet}"
LABEL="com.jay.ios-testflight-freshness"
PLIST_SRC="$REPO/scripts/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
STAMP="$(date +%Y%m%d-%H%M%S)"
DRY=0
LAUNCHD=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --no-launchd) LAUNCHD=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

say() { printf 'install-testflight-freshness: %s\n' "$*"; }

[[ -d "$DEST" ]] || { say "missing $DEST (the ios-fleet install target)"; exit 1; }
[[ -f "$DEST/asc-api.mjs" ]] || say "warning: $DEST/asc-api.mjs is missing; the check will fail until it is installed"

changed_plist=0
install_one() {
  local src="$1" dst="$2" mode="$3"
  if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
    say "unchanged $dst"
    return 1
  fi
  if [[ "$DRY" -eq 1 ]]; then
    say "would install $dst"
    return 0
  fi
  [[ -f "$dst" ]] && cp -p "$dst" "$dst.bak-freshness-$STAMP"
  install -m "$mode" "$src" "$dst"
  say "installed $dst"
  return 0
}

install_one "$HERE/ios_testflight_freshness.mjs" "$DEST/ios_testflight_freshness.mjs" 755 || true
install_one "$HERE/testflight-apps.json" "$DEST/testflight-apps.json" 644 || true

if [[ "$LAUNCHD" -eq 1 ]]; then
  plutil -lint "$PLIST_SRC" >/dev/null
  if install_one "$PLIST_SRC" "$PLIST_DST" 644; then changed_plist=1; fi
  loaded=0
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && loaded=1
  if [[ "$changed_plist" -eq 1 || "$loaded" -eq 0 ]]; then
    if [[ "$DRY" -eq 1 ]]; then
      say "would bootout and bootstrap gui/$(id -u)/$LABEL"
    else
      launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
      say "bootstrapped $LABEL (next run at :20 of an even hour)"
    fi
  else
    say "$LABEL already loaded with this plist"
  fi
fi
say "done.  Check by hand:  node $DEST/ios_testflight_freshness.mjs"
