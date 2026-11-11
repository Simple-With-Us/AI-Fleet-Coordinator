#!/usr/bin/env bash
set -euo pipefail

# Renders every Muster panel to a PNG (AFC-707):  each MusterDestination, in light and dark,
# at the window's default size and at its minimum.  It runs PanelSnapshotTests in the
# MusterPanels scheme, unsigned, and leaves the PNGs in the output folder, one subfolder per
# size.  Report only:  nothing is compared with a baseline.
#
# Usage:  script/render_panels.sh [output-folder]
# The folder defaults to muster/.build/panel-screenshots.  CI uploads it as the
# panel-screenshots artifact.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/.build/panel-screenshots}"
DERIVED_DATA="${MUSTER_PANELS_DERIVED_DATA:-$ROOT_DIR/.build/Panels}"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

cd "$ROOT_DIR"
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
