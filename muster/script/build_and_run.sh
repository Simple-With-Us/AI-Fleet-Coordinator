#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Muster"
INSTALL_DIR="$HOME/Applications"
INSTALLED_APP="$INSTALL_DIR/$APP_NAME.app"
INSTALLED_BINARY="$INSTALLED_APP/Contents/MacOS/$APP_NAME"
BUILD_DIR="$ROOT_DIR/.build"
DIST_DIR="$ROOT_DIR/dist"
STAGED_APP="$DIST_DIR/$APP_NAME.app"
LOCK_DIR="$BUILD_DIR/run.lock"
MODE="run"
CONFIGURATION="Release"
RELAUNCH=0

usage() {
    echo "Usage: $0 [--verify|--logs|--telemetry|--debug] [--development] [--relaunch]"
    echo "Set MUSTER_SIGNING_IDENTITY to an exact Developer ID Application name or SHA-1 to select it."
}

for argument in "$@"; do
    case "$argument" in
        --verify|--logs|--telemetry|--debug) MODE="$argument" ;;
        --development) CONFIGURATION="Debug" ;;
        --relaunch) RELAUNCH=1 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

for command in xcodebuild xcodegen python3; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command" >&2
        exit 1
    fi
done

installed_pids() {
    /bin/ps -axo pid=,comm= | /usr/bin/awk -v binary="$INSTALLED_BINARY" '
        { pid = $1; sub(/^[[:space:]]*[0-9]+[[:space:]]+/, ""); if ($0 == binary) print pid }
    '
}

refuse_running_replacement() {
    if [ -n "$(installed_pids)" ] && [ "$RELAUNCH" -ne 1 ]; then
        echo "Muster is running at $INSTALLED_APP." >&2
        echo "Quit it first, or use --relaunch to explicitly stop that installed copy."
        exit 1
    fi
}

refuse_running_replacement
mkdir -p "$BUILD_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Another Muster build is active, or $LOCK_DIR needs review after an interrupted build." >&2
    exit 1
fi

cleanup() {
    rm -rf "$DIST_DIR"
    rmdir "$LOCK_DIR"
}
trap cleanup EXIT

# Selection uses public certificate metadata; no private key material is exported.
SIGNING_SELECTION="$(python3 - <<'PY'
import os
import re
import subprocess
import sys

result = subprocess.run(
    ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
    check=True, capture_output=True, text=True,
)
identities = re.findall(
    r'\b([A-Fa-f0-9]{40}) "(Developer ID Application: [^"\n]+ \(([A-Z0-9]{10})\))"',
    result.stdout,
)
requested = os.environ.get("MUSTER_SIGNING_IDENTITY")
if requested:
    identities = [item for item in identities if requested in (item[0], item[1])]
if len(identities) != 1:
    sys.exit("Expected exactly one matching valid Developer ID Application identity; "
             "select one with MUSTER_SIGNING_IDENTITY.")
fingerprint, _, team = identities[0]
print(fingerprint, team)
PY
)"
read -r SIGNING_IDENTITY TEAM_ID <<< "$SIGNING_SELECTION"

cd "$ROOT_DIR"
xcodegen generate --spec project.yml
mkdir -p "$DIST_DIR"
xcodebuild \
    -project Muster.xcodeproj \
    -scheme Muster \
    -configuration "$CONFIGURATION" \
    -destination 'platform=macOS' \
    -derivedDataPath "$BUILD_DIR/DerivedData" \
    "CONFIGURATION_BUILD_DIR=$DIST_DIR" \
    "CODE_SIGN_IDENTITY=$SIGNING_IDENTITY" \
    "DEVELOPMENT_TEAM=$TEAM_ID" \
    build

/usr/bin/codesign --verify --strict --deep "$STAGED_APP"
SIGNATURE_DETAILS="$(/usr/bin/codesign -d --verbose=4 "$STAGED_APP" 2>&1)"
if ! /usr/bin/grep -q 'Authority=Developer ID Application:' <<< "$SIGNATURE_DETAILS"; then
    echo "The app does not have the required Developer ID Application signature." >&2
    exit 1
fi
if ! /usr/bin/grep -q 'flags=.*runtime' <<< "$SIGNATURE_DETAILS"; then
    echo "The app does not have hardened runtime enabled." >&2
    exit 1
fi

refuse_running_replacement
if [ "$RELAUNCH" -eq 1 ]; then
    PIDS="$(installed_pids)"
    if [ -n "$PIDS" ]; then
        while IFS= read -r pid; do
            /bin/kill -TERM "$pid"
        done <<< "$PIDS"
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
            [ -z "$(installed_pids)" ] && break
            sleep 0.5
        done
        if [ -n "$(installed_pids)" ]; then
            echo "Muster has not exited; refusing to replace the running app." >&2
            exit 1
        fi
    fi
fi

mkdir -p "$INSTALL_DIR"
if [ -L "$INSTALLED_APP" ]; then
    echo "Refusing to replace a symlink at $INSTALLED_APP." >&2
    exit 1
fi
rm -rf "$INSTALLED_APP"
mv "$STAGED_APP" "$INSTALLED_APP"
/usr/bin/codesign --verify --strict --deep "$INSTALLED_APP"
/usr/bin/open "$INSTALLED_APP"

case "$MODE" in
    --verify|--debug)
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
            [ -n "$(installed_pids)" ] && break
            sleep 0.5
        done
        PIDS="$(installed_pids)"
        if [ -z "$PIDS" ]; then
            echo "Muster did not remain running after launch." >&2
            exit 1
        fi
        echo "Verified Muster is running from $INSTALLED_APP."
        if [ "$MODE" = "--debug" ]; then
            xcrun lldb -p "${PIDS%%$'\n'*}"
        fi
        ;;
    --logs)
        /usr/bin/log stream --info --style compact --predicate 'process == "Muster"'
        ;;
    --telemetry)
        if [ "$CONFIGURATION" = "Debug" ]; then
            BUNDLE_ID="services.jays.Muster.dev"
        else
            BUNDLE_ID="services.jays.Muster"
        fi
        /usr/bin/log stream --info --style compact --predicate "subsystem == \"$BUNDLE_ID\""
        ;;
esac
