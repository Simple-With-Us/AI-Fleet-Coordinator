#!/bin/sh
# Shell prefilter for sim-session-hook.py (Claude Code PreToolUse, PostToolUse,
# PostToolUseFailure, Stop, SubagentStop, SessionEnd).  An ordinary tool call is
# answered here without starting Python:  only a payload that names a way to boot
# a simulator, or a turn or session end while some session owns a simulator, goes
# on to the Python hook.  The words below are a superset of BOOTISH in the .py.
# Always exits 0 and never writes stdout, so it can never block a tool call.
payload=$(cat) || exit 0
case "$payload" in
  *xcodebuild*|*boot*|*Simulator*|*ios-debug*|*run-ios*|*run:ios*|*fastlane*|*xctest*) ;;
  *'"Stop"'*|*'"SubagentStop"'*|*'"SessionEnd"'*)
    set -- "${SIM_REAPER_STATE_DIR:-$HOME/Library/Application Support/sim-idle-reaper}/sessions/"*.json
    [ -e "$1" ] || exit 0
    ;;
  *) exit 0 ;;
esac
PY=$(command -v python3 2>/dev/null) || PY=/usr/bin/python3
printf '%s' "$payload" | "$PY" "$(dirname "$0")/sim-session-hook.py" >/dev/null 2>&1
exit 0
