#!/usr/bin/env bash
# Writes the Sparkle appcast for one signed, notarized Muster archive.
#
#   muster/script/make_appcast.sh <archive.zip> <download-url-prefix> <output-dir> [release-notes.md]
#
# The appcast has exactly one item:  the archive, at <download-url-prefix><archive name>,
# with its EdDSA signature and length.  Sparkle only ever needs the newest item, so there is
# no history to merge.  generate_appcast reads the version and the minimum macOS from the app
# inside the archive, and refuses an archive whose SUPublicEDKey does not match the signing
# key, so a key mix-up fails here and not on the owner's Mac.
#
# The EdDSA private key is read from the file $MUSTER_SPARKLE_KEY_PATH (the workflow writes
# it there, mode 600, and it never reaches an argument or the log).  generate_appcast comes
# from the Sparkle distribution;  $SPARKLE_TOOLS_DIR names its bin directory.
# Copied from Hog Hunter's scripts/make_appcast.sh.  See muster/docs/AUTO-UPDATE.md.
set -euo pipefail

APP_LINK="${SPARKLE_APP_LINK:-https://github.com/Simple-With-Us/AI-Fleet-Coordinator/tree/main/muster}"

usage() {
  echo "usage: muster/script/make_appcast.sh <archive.zip> <download-url-prefix> <output-dir> [release-notes.md]" >&2
  exit 2
}

[[ $# -ge 3 && $# -le 4 ]] || usage
ARCHIVE="$1"
PREFIX="$2"
OUT_DIR="$3"
NOTES="${4:-}"
[[ -f "$ARCHIVE" ]] || { echo "error: no archive at $ARCHIVE" >&2; exit 1; }
[[ "$PREFIX" == */ ]] || PREFIX="$PREFIX/"
[[ -n "${SPARKLE_TOOLS_DIR:-}" ]] || { echo "error: SPARKLE_TOOLS_DIR is not set" >&2; exit 1; }
[[ -s "${MUSTER_SPARKLE_KEY_PATH:-}" ]] || { echo "error: MUSTER_SPARKLE_KEY_PATH names no key file" >&2; exit 1; }

GENERATE_APPCAST="$SPARKLE_TOOLS_DIR/generate_appcast"
[[ -x "$GENERATE_APPCAST" ]] || { echo "error: generate_appcast not found under $SPARKLE_TOOLS_DIR" >&2; exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/muster-appcast.XXXXXX")"
trap 'rm -rf "$work"' EXIT
cp "$ARCHIVE" "$work/"
archive_name="$(basename "$ARCHIVE")"
# generate_appcast pairs release notes with an archive by file name.
if [[ -n "$NOTES" ]]; then
  cp "$NOTES" "$work/${archive_name%.*}.md"
fi

"$GENERATE_APPCAST" --ed-key-file "$MUSTER_SPARKLE_KEY_PATH" \
  --download-url-prefix "$PREFIX" --link "$APP_LINK" --embed-release-notes \
  -o "$work/appcast.xml" "$work"

feed="$work/appcast.xml"
items="$(/usr/bin/grep -c '<item>' "$feed" || true)"
[[ "$items" == "1" ]] || { echo "error: expected one appcast item, found $items" >&2; exit 1; }
/usr/bin/grep -q 'sparkle:edSignature=' "$feed" || { echo "error: the appcast item carries no EdDSA signature" >&2; exit 1; }
/usr/bin/grep -q "url=\"$PREFIX$archive_name\"" "$feed" || { echo "error: the appcast does not point at $PREFIX$archive_name" >&2; exit 1; }

mkdir -p "$OUT_DIR"
cp "$feed" "$OUT_DIR/appcast.xml"
version="$(/usr/bin/sed -n 's:.*<sparkle\:version>\(.*\)</sparkle\:version>.*:\1:p' "$feed" | /usr/bin/head -n 1)"
echo "appcast: $OUT_DIR/appcast.xml"
echo "  item:  build $version"
echo "  url:   $PREFIX$archive_name"
