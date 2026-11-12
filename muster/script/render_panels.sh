#!/usr/bin/env bash
set -euo pipefail

# Renders every Muster panel to a PNG (AFC-707):  each MusterDestination, in light and dark,
# at the window's default size and at its minimum.  It runs PanelSnapshotTests in the
# MusterPanels scheme, unsigned, and leaves the PNGs in the output folder, one subfolder per
# size.  No baselines, but it is a layout gate:  a blank PNG, a wrong size or a split view
# outside the window fails it.  It is inert:  the test bundle stops every process launch and
# every URLSession.shared request a panel makes (MusterPanelTests/InertProcess.swift), so no
# fleet-secrets, ssh or recall read runs, on this Mac or in CI.
#
# Usage:  script/render_panels.sh [--any-folder] [output-folder]
# The folder defaults to muster/.build/panel-screenshots, which git ignores, and CI uploads it
# as the panel-screenshots artifact.  A folder outside muster/.build is refused unless
# --any-folder is given, so the PNGs do not land somewhere they could be committed by mistake.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANY_FOLDER=0
if [ "${1:-}" = "--any-folder" ]; then
    ANY_FOLDER=1
    shift
fi
if [ "$#" -gt 1 ]; then
    echo "usage:  script/render_panels.sh [--any-folder] [output-folder]" >&2
    exit 2
fi
OUTPUT_DIR="${1:-$ROOT_DIR/.build/panel-screenshots}"
DERIVED_DATA="${MUSTER_PANELS_DERIVED_DATA:-$ROOT_DIR/.build/Panels}"

# Check the folder before anything is created:  resolve its nearest existing parent, so a
# refused path leaves no folder behind, and refuse a not-yet-made tail that holds "..".
case "$OUTPUT_DIR" in
    /*) WANTED="$OUTPUT_DIR" ;;
    *) WANTED="$PWD/$OUTPUT_DIR" ;;
esac
EXISTING="$WANTED"
TAIL=""
while [ ! -d "$EXISTING" ]; do
    TAIL="/$(basename -- "$EXISTING")$TAIL"
    EXISTING="$(dirname -- "$EXISTING")"
done
RESOLVED="$(cd -- "$EXISTING" && pwd -P)$TAIL"
BUILD_DIR="$(mkdir -p -- "$ROOT_DIR/.build" && cd -- "$ROOT_DIR/.build" && pwd -P)"
INSIDE=0
case "$RESOLVED/" in
    "$BUILD_DIR"/*) INSIDE=1 ;;
esac
case "$TAIL/" in
    */../*|*/./*) INSIDE=0 ;;
esac
if [ "$INSIDE" -ne 1 ] && [ "$ANY_FOLDER" -ne 1 ]; then
    echo "render_panels:  $RESOLVED is outside $BUILD_DIR;  pass --any-folder to use it" >&2
    exit 2
fi
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR="$(cd -- "$OUTPUT_DIR" && pwd -P)"

cd -- "$ROOT_DIR"
if [ ! -d Muster.xcodeproj ] || [ project.yml -nt Muster.xcodeproj/project.pbxproj ]; then
    xcodegen generate --spec project.yml
fi

# TEST_RUNNER_ variables reach the test process without the prefix.
TEST_RUNNER_MUSTER_PANEL_SNAPSHOT_DIR="$OUTPUT_DIR" \
    xcodebuild -project Muster.xcodeproj -scheme MusterPanels -configuration Debug \
        -destination 'platform=macOS' -derivedDataPath "$DERIVED_DATA" \
        CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= \
        test -only-testing:MusterPanelTests/PanelSnapshotTests

count="$(find "$OUTPUT_DIR" -name '*.png' | wc -l | tr -d ' ')"
echo "render_panels:  $count PNGs in $OUTPUT_DIR"
if [ "$count" -eq 0 ]; then
    echo "render_panels:  no PNGs were written" >&2
    exit 1
fi
