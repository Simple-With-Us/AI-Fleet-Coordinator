#!/bin/sh
# Build the Touch ID gate helper:  generate the project with XcodeGen,  build it with
# xcodebuild,  and sign it.
#
# The generated project and the build directory are machine specific and are not
# committed;  project.yml and the Swift source are.
#
# Usage:  scripts/fleet_config/touchid_helper/build.sh [Release|Debug]
set -eu

here="$(cd "$(dirname "$0")" && pwd)"
config="${1:-Release}"
xcodegen="${XCODEGEN:-/opt/homebrew/bin/xcodegen}"

if [ ! -x "$xcodegen" ]; then
    echo "build.sh: xcodegen is not at $xcodegen;  install it with 'brew install xcodegen'" >&2
    exit 1
fi

cd "$here"
"$xcodegen" generate --spec project.yml

xcodebuild \
    -project TouchIDGate.xcodeproj \
    -scheme TouchIDGate \
    -configuration "$config" \
    -derivedDataPath build \
    build

binary="build/Build/Products/$config/TouchIDGate"
if [ ! -x "$binary" ]; then
    echo "build.sh: the helper was not built at $binary" >&2
    exit 1
fi

# An ad-hoc signature is enough for LocalAuthentication on this Mac,  and the gate
# refuses a helper that codesign does not accept.
codesign --force --sign - "$binary"
codesign --verify --strict "$binary"
echo "build.sh: built and signed $binary"
