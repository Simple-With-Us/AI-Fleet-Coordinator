#!/usr/bin/env bash
# Builds, signs, notarizes and packages Muster for Sparkle 2 self-update.  This is the
# release route:  .github/workflows/muster-release.yml runs it on a GitHub-hosted Mac for
# every merge to main that changes Muster.  muster/docs/AUTO-UPDATE.md describes it, and
# Hog Hunter's scripts/mac-release-build.sh is the copy it follows.
#
#   muster/script/release_build.sh --build-only   stage muster/dist/Muster.app, sign it
#                                                  and verify the signature
#   muster/script/release_build.sh --release      --build-only, then notarize and staple
#                                                  the app, zip the stapled bundle, and
#                                                  build, sign, notarize and staple the dmg
#
# Signing:  $MUSTER_CODESIGN_IDENTITY when set, otherwise the one "Developer ID
# Application:" identity in the codesigning keychains.  The workflow imports the certificate
# into a throwaway keychain first.  MUSTER_ALLOW_ADHOC_VALIDATION=1 is allowed only with
# --build-only:  an ad-hoc signature proves the bundle's structure and nothing about the team.
#
# The stamp:  the build number is the commit's time in UTC (YYYYMMDDHHMM), and the commit,
# its time and the channel go into the Info.plist through Support/Info.plist.  The workflow
# sets MUSTER_BUILD_CHANNEL=release on main.
#
# Notarization signs in with an App Store Connect API key when NOTARY_KEY_PATH,
# NOTARY_KEY_ID and NOTARY_ISSUER_ID are all set.  No value is ever printed.
set -euo pipefail

MODE="${1:---build-only}"
MUSTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$MUSTER_DIR/dist"
DERIVED="$MUSTER_DIR/.build/release"
APP_BUNDLE="$DIST_DIR/Muster.app"
APP_CONTENTS="$APP_BUNDLE/Contents"
APP_FRAMEWORKS="$APP_CONTENTS/Frameworks"
INFO_PLIST="$APP_CONTENTS/Info.plist"
ZIP_FILE="$DIST_DIR/Muster.zip"
DMG_FILE="$DIST_DIR/Muster.dmg"

NOTARY_AUTH=()
if [[ -n "${NOTARY_KEY_PATH:-}" && -n "${NOTARY_KEY_ID:-}" && -n "${NOTARY_ISSUER_ID:-}" ]]; then
  NOTARY_AUTH=(--key "$NOTARY_KEY_PATH" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER_ID")
fi

usage() {
  echo "usage: muster/script/release_build.sh [--build-only | --release]" >&2
}

# codesign blocks on a keychain access panel when this shell was never allowed the key, and
# a build must never hang behind a panel nobody is watching.  Thirty seconds, then it fails.
WATCHDOG_STATUS=142
codesign_bounded() {
  /usr/bin/perl -e 'alarm 30; exec @ARGV' /usr/bin/codesign "$@"
}

resolve_codesign_identity() {
  if [[ -n "${MUSTER_CODESIGN_IDENTITY:-}" ]]; then
    printf '%s\n' "$MUSTER_CODESIGN_IDENTITY"
    return 0
  fi
  local found
  local listing
  listing="$(/usr/bin/security find-identity -v -p codesigning 2>/dev/null || true)"
  found="$(/usr/bin/sed -n 's/^.*"\(Developer ID Application:[^"]*\)".*$/\1/p' <<< "$listing" | /usr/bin/sort -u)"
  if [[ -n "$found" && "$(printf '%s\n' "$found" | /usr/bin/wc -l | /usr/bin/tr -d ' ')" != "1" ]]; then
    echo "error: more than one Developer ID Application identity; set MUSTER_CODESIGN_IDENTITY." >&2
    return 1
  fi
  printf '%s\n' "$found"
}

read_identity() {
  local identity key value
  identity="$(python3 "$MUSTER_DIR/script/build_support.py" build-identity Release)"
  while IFS='=' read -r key value; do
    case "$key" in
      BUILD_NUMBER) BUILD_NUMBER="$value" ;;
      COMMIT) COMMIT="$value" ;;
      COMMIT_TIME) COMMIT_TIME="$value" ;;
      CHANNEL) CHANNEL="$value" ;;
    esac
  done <<< "$identity"
  [[ "${BUILD_NUMBER:-}" =~ ^[0-9]{12}$ && "${COMMIT:-}" =~ ^[0-9a-f]{40}$ ]] || {
    echo "error: the build identity could not be read." >&2
    exit 1
  }
}

plist_value() {
  /usr/libexec/PlistBuddy -c "Print :$1" "$INFO_PLIST" 2>/dev/null || true
}

# The bundle carries what Settings shows and what Sparkle needs, or it is not shipped.
check_stamp() {
  [[ "$(plist_value CFBundleVersion)" == "$BUILD_NUMBER" ]] || { echo "error: CFBundleVersion is not $BUILD_NUMBER." >&2; exit 1; }
  [[ "$(plist_value MusterCommit)" == "$COMMIT" ]] || { echo "error: MusterCommit is not $COMMIT." >&2; exit 1; }
  [[ "$(plist_value MusterBuildChannel)" == "$CHANNEL" ]] || { echo "error: MusterBuildChannel is not $CHANNEL." >&2; exit 1; }
  [[ -n "$(plist_value SUFeedURL)" ]] || { echo "error: the Release build carries no SUFeedURL." >&2; exit 1; }
  if [[ -z "$(plist_value SUPublicEDKey)" ]]; then
    if [[ "$MODE" == "--release" ]]; then
      echo "error: the Release build carries no SUPublicEDKey; set MUSTER_SPARKLE_PUBLIC_KEY in muster/project.yml." >&2
      exit 1
    fi
    echo "note: no SUPublicEDKey yet, so this build would read Automatic Updates: Off."
  fi
  echo "stamp: build $BUILD_NUMBER, commit ${COMMIT:0:7}, channel $CHANNEL"
}

# Xcode embeds the package's framework itself, but a release must never ship without it:
# the app would crash at launch looking for it.
assert_sparkle_embedded() {
  [[ -d "$APP_FRAMEWORKS/Sparkle.framework" ]] || { echo "error: Sparkle.framework is not embedded." >&2; exit 1; }
  local load_commands
  load_commands="$(/usr/bin/otool -l "$APP_CONTENTS/MacOS/Muster")"
  if ! /usr/bin/grep -q '@executable_path/../Frameworks' <<< "$load_commands"; then
    /usr/bin/install_name_tool -add_rpath "@executable_path/../Frameworks" "$APP_CONTENTS/MacOS/Muster"
  fi
  echo "Sparkle.framework embedded"
}

build_and_stage() {
  command -v xcodegen >/dev/null 2>&1 || { echo "error: xcodegen is required" >&2; exit 1; }
  read_identity
  (cd "$MUSTER_DIR" && xcodegen generate --spec project.yml)
  rm -rf "$APP_BUNDLE" "$DERIVED"
  /usr/bin/xcodebuild -project "$MUSTER_DIR/Muster.xcodeproj" \
    -scheme Muster -configuration Release \
    -destination 'generic/platform=macOS' \
    -derivedDataPath "$DERIVED" \
    ARCHS="arm64 x86_64" ONLY_ACTIVE_ARCH=NO \
    CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY= \
    "CURRENT_PROJECT_VERSION=$BUILD_NUMBER" \
    "MUSTER_COMMIT=$COMMIT" \
    "MUSTER_COMMIT_TIME=$COMMIT_TIME" \
    "MUSTER_BUILD_CHANNEL=$CHANNEL" \
    build
  local product="$DERIVED/Build/Products/Release/Muster.app"
  [[ -d "$product" ]] || { echo "error: built app not found at $product" >&2; exit 1; }
  mkdir -p "$DIST_DIR"
  /usr/bin/ditto "$product" "$APP_BUNDLE"
  local archs
  archs="$(/usr/bin/lipo -archs "$APP_CONTENTS/MacOS/Muster")"
  for wanted in arm64 x86_64; do
    [[ " $archs " == *" $wanted "* ]] || { echo "error: the universal build is missing $wanted: $archs" >&2; exit 1; }
  done
  assert_sparkle_embedded
  check_stamp
}

# Sparkle's helpers are signed before the framework that holds them, in the order Sparkle's
# code-signing guide gives, and the framework before the app.  No --deep on the signing path.
framework_helper_paths() {
  local framework name
  for framework in "$APP_FRAMEWORKS"/*.framework; do
    [[ -d "$framework" ]] || continue
    name="$(basename "$framework" .framework)"
    /usr/bin/find "$framework/Versions" -mindepth 3 -maxdepth 3 -path '*/XPCServices/*.xpc' -type d 2>/dev/null
    /usr/bin/find "$framework/Versions" -mindepth 2 -maxdepth 2 -name '*.app' -type d 2>/dev/null
    /usr/bin/find "$framework/Versions" -mindepth 2 -maxdepth 2 -type f -perm -u+x ! -name "$name" 2>/dev/null
  done
}

nested_code_paths() {
  /usr/bin/find "$APP_CONTENTS" \
    \( -name '*.framework' -o -name '*.bundle' -o -name '*.dylib' \) \
    -prune -print 2>/dev/null | /usr/bin/sort -r
}

sign_with_identity() {
  local identity="$1" target status
  local options=(--options runtime --timestamp)
  [[ "$identity" == "-" ]] && options=(--options runtime)
  while IFS= read -r target; do
    [[ -n "$target" ]] || continue
    codesign_bounded --force "${options[@]}" --preserve-metadata=entitlements --sign "$identity" "$target" || return 1
  done < <(framework_helper_paths)
  while IFS= read -r target; do
    [[ -n "$target" ]] || continue
    codesign_bounded --force "${options[@]}" --sign "$identity" "$target" && continue
    status=$?
    [[ "$status" != "$WATCHDOG_STATUS" ]] || return 1
    echo "note: not separately signable, sealed as a resource instead: $target"
  done < <(nested_code_paths)
  codesign_bounded --force "${options[@]}" --sign "$identity" "$APP_BUNDLE" || return 1
}

verify_app_signature() {
  /usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE" || {
    echo "error: codesign --verify --deep --strict failed for $APP_BUNDLE" >&2
    exit 1
  }
}

sign_app_bundle() {
  local identity
  identity="$(resolve_codesign_identity)"
  if [[ -n "$identity" ]]; then
    sign_with_identity "$identity" || { echo "error: signing failed or timed out." >&2; exit 1; }
    verify_app_signature
    local details
    details="$(/usr/bin/codesign -d --verbose=4 "$APP_BUNDLE" 2>&1)"
    /usr/bin/grep -q 'flags=.*runtime' <<< "$details" || {
      echo "error: the app does not have hardened runtime enabled." >&2
      exit 1
    }
    echo "signed with $identity"
    return 0
  fi
  if [[ "$MODE" == "--build-only" && "${MUSTER_ALLOW_ADHOC_VALIDATION:-}" == "1" ]]; then
    sign_with_identity "-" || { echo "error: ad-hoc structural signing failed." >&2; exit 1; }
    verify_app_signature
    echo "signed ad-hoc for structural validation only; the Developer ID team is not verified."
    return 0
  fi
  echo "error: no Developer ID Application identity is available." >&2
  exit 1
}

write_sha_file() {
  local sha
  sha="$(/usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}')"
  echo "$sha  $(basename "$1")" > "$1.sha256"
}

write_zip() {
  rm -f "$ZIP_FILE"
  /usr/bin/ditto -c -k --keepParent "$APP_BUNDLE" "$ZIP_FILE"
  write_sha_file "$ZIP_FILE"
}

notarize_file() {
  local file="$1" response submission status
  [[ "${#NOTARY_AUTH[@]}" -gt 0 ]] || { echo "error: no notarization credentials (NOTARY_KEY_PATH, NOTARY_KEY_ID, NOTARY_ISSUER_ID)." >&2; exit 1; }
  echo "Submitting $(basename "$file") for notarization.  This takes a few minutes."
  if ! response="$(/usr/bin/xcrun notarytool submit "$file" "${NOTARY_AUTH[@]}" --wait --output-format json 2>/dev/null)"; then
    echo "error: notarization could not be submitted for $(basename "$file")." >&2
    exit 1
  fi
  submission="$(printf '%s' "$response" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
  status="$(printf '%s' "$response" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')"
  echo "notarization status: $status ($submission)"
  if [[ "$status" != "Accepted" ]]; then
    /usr/bin/xcrun notarytool log "$submission" "${NOTARY_AUTH[@]}" >&2 || true
    exit 1
  fi
}

# Stapling fetches Apple's ticket over the network and is flaky on CI runners, and it is
# idempotent, so it is tried five times, thirty seconds apart (Hog Hunter's staple_with_retry).
staple_with_retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    /usr/bin/xcrun stapler staple "$1" && return 0
    [[ "$attempt" -lt 5 ]] && { echo "warning: stapling $1 failed (attempt $attempt of 5); retrying in 30s." >&2; sleep 30; }
  done
  echo "error: stapling $1 failed after 5 attempts." >&2
  return 1
}

build_dmg() {
  local staging="$DIST_DIR/dmg-staging" identity
  rm -rf "$staging" "$DMG_FILE"
  mkdir -p "$staging"
  cp -R "$APP_BUNDLE" "$staging/Muster.app"
  ln -s /Applications "$staging/Applications"
  /usr/bin/hdiutil create -volname "Muster" -srcfolder "$staging" -ov -format UDZO "$DMG_FILE"
  rm -rf "$staging"
  identity="$(resolve_codesign_identity)"
  [[ -n "$identity" ]] || { echo "error: no Developer ID Application identity to sign the disk image." >&2; exit 1; }
  codesign_bounded --force --timestamp --sign "$identity" "$DMG_FILE"
}

release_dist() {
  build_and_stage
  sign_app_bundle
  write_zip
  notarize_file "$ZIP_FILE"
  staple_with_retry "$APP_BUNDLE"
  /usr/sbin/spctl --assess --type execute -vv "$APP_BUNDLE"
  # The zip that was submitted holds the unstapled bundle, so it is made again from the
  # stapled one.
  write_zip
  build_dmg
  notarize_file "$DMG_FILE"
  staple_with_retry "$DMG_FILE"
  /usr/sbin/spctl --assess --type open --context context:primary-signature -vv "$DMG_FILE"
  write_sha_file "$DMG_FILE"
  echo "Release artifacts in $DIST_DIR:  Muster.zip, Muster.dmg (build $BUILD_NUMBER, commit ${COMMIT:0:7})"
}

case "$MODE" in
  --build-only)
    build_and_stage
    sign_app_bundle
    ;;
  --release)
    release_dist
    ;;
  --help|-h)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
