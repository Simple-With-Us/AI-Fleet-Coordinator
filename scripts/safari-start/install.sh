#!/usr/bin/env bash
# Install Jay's Safari start page on this Mac.  Copies index.html to ~/Sites/safari-start
# and points Safari's homepage, new tabs, and new windows at it.
# Writes both the global Safari domain and the sandboxed container plist
# (macOS 13+ Safari ignores ~/Library/Preferences/com.apple.Safari for these keys).
#   scripts/safari-start/install.sh           install or refresh
#   scripts/safari-start/install.sh --revert  restore Safari's built-in Start Page
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/Sites/safari-start"
CONTAINER="$HOME/Library/Containers/com.apple.Safari/Data/Library/Preferences/com.apple.Safari"

write_safari() {
  local key="$1"
  shift
  defaults write com.apple.Safari "$key" "$@"
  if [[ -d "$(dirname "$CONTAINER")" ]]; then
    defaults write "$CONTAINER" "$key" "$@"
  fi
}

delete_safari() {
  local key="$1"
  defaults delete com.apple.Safari "$key" 2>/dev/null || true
  if [[ -d "$(dirname "$CONTAINER")" ]]; then
    defaults delete "$CONTAINER" "$key" 2>/dev/null || true
  fi
}

quit_safari() {
  if pgrep -x Safari >/dev/null 2>&1; then
    osascript -e 'tell application "Safari" to quit' >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8; do
      pgrep -x Safari >/dev/null 2>&1 || break
      sleep 0.25
    done
  fi
}

if [[ "${1:-}" == "--revert" ]]; then
  quit_safari
  write_safari HomePage -string "https://www.apple.com/startpage/"
  delete_safari NewTabBehavior
  delete_safari NewWindowBehavior
  echo "Safari is back on its built-in Start Page.  The page itself is still at $DEST/index.html."
  exit 0
fi

mkdir -p "$DEST"
cp "$HERE/public/index.html" "$DEST/index.html"
URL="file://$DEST/index.html"
quit_safari
write_safari HomePage -string "$URL"
write_safari NewTabBehavior -int 0
write_safari NewWindowBehavior -int 0
echo "Installed $DEST/index.html and pointed Safari homepage, new tabs, and new windows at it."
echo "Phone copy (after Personal-Site deploy): https://jays.services/start/"
