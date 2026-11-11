#!/bin/sh
# Install com.jay.operator-archive-sweep on this Mac.
#
#   scripts/launchd/install-operator-archive-sweep.sh            install in DRY-RUN mode (closes nothing)
#   scripts/launchd/install-operator-archive-sweep.sh --apply    install with the apply pass on
#   scripts/launchd/install-operator-archive-sweep.sh --print    show the plist it would write
#   scripts/launchd/install-operator-archive-sweep.sh --remove   boot it out and delete the plist
#
# Safe to run again:  it rewrites the plist and re-bootstraps.  It touches only this one label.
set -eu

HERE="$(cd -P -- "$(dirname -- "$0")" && pwd)"
LABEL=com.jay.operator-archive-sweep
SRC="$HERE/$LABEL.plist"
DEST="${HOME}/Library/LaunchAgents/$LABEL.plist"
MODE=install
APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --print) MODE=print ;;
    --remove) MODE=remove ;;
    *) echo "usage: $0 [--apply|--print|--remove]" >&2; exit 2 ;;
  esac
done

render() {
  # The template's APPLY value is the string right after its key.
  awk -v apply="$APPLY" '
    prev ~ /OPERATOR_ARCHIVE_SWEEP_APPLY/ && /<string>/ { sub(/<string>[^<]*<\/string>/, "<string>" apply "</string>") }
    { print; prev = $0 }
  ' "$SRC"
}

case "$MODE" in
  print) render ;;
  remove)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$DEST"
    echo "removed $LABEL" ;;
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.local/state/operator"
    render >"$DEST.new"
    plutil -lint "$DEST.new" >/dev/null
    mv "$DEST.new" "$DEST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    # launchd can still hold the label for a moment after bootout, so wait, and retry once.
    sleep 1
    launchctl bootstrap "gui/$(id -u)" "$DEST" || { sleep 2; launchctl bootstrap "gui/$(id -u)" "$DEST"; }
    echo "installed $LABEL (apply=$APPLY);  check:  launchctl print gui/$(id -u)/$LABEL" ;;
esac
