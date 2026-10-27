#!/usr/bin/env bash
# publish-ios-versions.sh — open a pull request that updates the public fleet
# iOS version manifest.
#
# Called by ship-testflight.sh after a successful upload.  Also safe to run by
# hand.  Never prints secret values.  Failure is non-fatal for ships.
#
# Usage:
#   bash scripts/ios-fleet/publish-ios-versions.sh \
#     --bundle-id com.socratictrade.ios \
#     --version 1.0.68 \
#     [--build 202608211800] \
#     [--apple-id 1111111111] \
#     [--display-name "Socratic-Trade"]
#     [--base-json path] [--out-json path] [--skip-push] [--skip-local-write]
#
# Writes:
#   <this-dir>/ios-app-versions.json  (local cache; may be stale)
#   a pull request against Simple-With-Us/AI-Fleet-Coordinator :
#     site/ios-versions.json
#
# WHY A PULL REQUEST AND NOT A DIRECT WRITE:  AI-Fleet-Coordinator's ruleset
# requires `test` and `gitleaks` on main, and a ruleset with required status
# checks refuses a direct write to the protected branch.  A Contents API PUT
# to the default branch is therefore rejected, the ship treats the publish as
# best effort, and the only trace anywhere is one warning line in the ship
# log.  A pull request merge is exempt from that gate, so the manifest is
# published as a branch plus a pull request.  The script never writes to the
# default branch.
#
# GATES: before it publishes, the script runs the fleet icon audit
# (fleet-icon-audit.mjs).  FLEET_ICON_AUDIT=block (default) refuses to publish
# when the audit reports an error or cannot run; warn reports and continues;
# off skips it.  After a successful publish it runs the guarded sheet sync
# shim (~/apps/fleet-apps-sheet-sync.mjs, or FLEET_SHEET_SYNC) so the Fleet
# Reference Sheet follows the manifest; a missing shim is skipped, a failing
# one is reported and fails the run.
#
# THE TOKEN:  the publish needs a credential with write access to
# AI-Fleet-Coordinator.  The ship workflow passes the fleet's GH_PAT secret
# as GH_TOKEN; a hand run reads GH_PAT through fleet-secrets.  A bare gh login
# is never used.  The
# workflow's own GITHUB_TOKEN is read-only by declaration and cannot write to
# another repository, so it is never used for this and never arms auto-merge.
#
# SAFETY: the vendored ios-app-versions.json in this repo is a stale snapshot
# (2026-08-21).  Starting from that file and publishing it would drop
# net.dealdex 1.0.2 and roll Usage Client 1.0.11→1.0.8, Usage Local
# 1.0.9→1.0.7, and Autorotate 1.0.4→1.0.1.  Always seed from the live remote
# (or an explicit --base-json).  Refuse to publish a single-app / empty-apps
# document over a multi-app remote.  Same class as DealDex #173.

set -euo pipefail

FLEET_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_JSON="${FLEET_DIR}/ios-app-versions.json"
REPO="${AFC_MANIFEST_REPO:-Simple-With-Us/AI-Fleet-Coordinator}"
REMOTE_PATH="site/ios-versions.json"
BASE_BRANCH="${AFC_MANIFEST_BRANCH:-main}"
PR_BRANCH_PREFIX="${AFC_MANIFEST_BRANCH_PREFIX:-ios-versions-publish}"
AUTO_MERGE="${AFC_MANIFEST_AUTO_MERGE:-1}"

BUNDLE_ID=""
VERSION=""
BUILD=""
APPLE_ID=""
DISPLAY_NAME=""
BASE_JSON=""
OUT_JSON=""
SKIP_PUSH=0
SKIP_LOCAL_WRITE=0

need_arg() {
  [[ "$2" -ge 2 ]] || { echo "error: $1 needs a value" >&2; exit 2; }
}

usage() {
  sed -n '1,45p' "$0"
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle-id) need_arg "$1" "$#"; BUNDLE_ID="$2"; shift 2 ;;
    --version) need_arg "$1" "$#"; VERSION="$2"; shift 2 ;;
    --build) need_arg "$1" "$#"; BUILD="$2"; shift 2 ;;
    --apple-id) need_arg "$1" "$#"; APPLE_ID="$2"; shift 2 ;;
    --display-name) need_arg "$1" "$#"; DISPLAY_NAME="$2"; shift 2 ;;
    --base-json) need_arg "$1" "$#"; BASE_JSON="$2"; shift 2 ;;
    --out-json) need_arg "$1" "$#"; OUT_JSON="$2"; shift 2 ;;
    --skip-push) SKIP_PUSH=1; shift ;;
    --skip-local-write) SKIP_LOCAL_WRITE=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$BUNDLE_ID" && -n "$VERSION" ]] || { echo "need --bundle-id and --version" >&2; exit 2; }

# The token the publish uses.  GITHUB_TOKEN is the workflow's own token:  it is
# read-only by declaration and it cannot write to another repository, so it is
# refused here rather than failing later with an authorization error that reads
# like a branch rule.
TOKEN=""
TOKEN_SOURCE=""
resolve_token() {
  if [[ -n "${GH_TOKEN:-}" ]]; then
    TOKEN="$GH_TOKEN"
    TOKEN_SOURCE="env"
    return 0
  fi
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    echo "error: GITHUB_TOKEN cannot write to ${REPO}" >&2
    echo "error: pass the fleet GH_PAT secret as GH_TOKEN (the ship workflow does)" >&2
    return 1
  fi
  # No token in the environment:  read the fleet GH_PAT.  A bare gh login is
  # not used, because it may be a personal or read-only credential.
  local pat=""
  if command -v fleet-secrets >/dev/null 2>&1; then
    pat="$(fleet-secrets get GH_PAT 2>/dev/null || true)"
  fi
  if [[ -z "$pat" ]]; then
    echo "error: no GH_TOKEN and fleet-secrets could not return GH_PAT; cannot publish the manifest" >&2
    return 1
  fi
  TOKEN="$pat"
  TOKEN_SOURCE="fleet-secrets"
}

fetch_remote_json() {
  local dest="$1"
  if ! command -v gh >/dev/null 2>&1; then
    return 1
  fi
  local b64 meta
  meta="$(gh api "repos/${REPO}/contents/${REMOTE_PATH}?ref=${BASE_BRANCH}" --jq '[.sha, .content] | @tsv' 2>/dev/null || true)"
  [[ -n "$meta" ]] || return 1
  REMOTE_BLOB_SHA="${meta%%$'\t'*}"
  b64="${meta#*$'\t'}"
  [[ -n "$b64" ]] || return 1
  python3 -c 'import base64,sys; sys.stdout.write(base64.b64decode("".join(sys.argv[1].split())).decode())' "$b64" >"$dest"
}

TMP_REMOTE=""
BASE_SNAPSHOT=""
LIVE_JSON=""
REMOTE_BLOB_SHA=""
BASE_FROM_REMOTE=0
cleanup() {
  rm -f "${TMP_REMOTE:-}" "${BASE_SNAPSHOT:-}" "${LIVE_JSON:-}"
}
trap cleanup EXIT

if [[ -z "$BASE_JSON" ]]; then
  TMP_REMOTE="$(mktemp)"
  if fetch_remote_json "$TMP_REMOTE"; then
    BASE_JSON="$TMP_REMOTE"
    BASE_FROM_REMOTE=1
    SEEN_BLOB_SHA="$REMOTE_BLOB_SHA"
  else
    echo "error: could not read the live manifest ${REPO}/${REMOTE_PATH}; pass --base-json to seed explicitly" >&2
    echo "error: refusing to seed from the local snapshot (stale; would roll apps back)" >&2
    exit 1
  fi
fi

if [[ ! -f "$BASE_JSON" ]]; then
  echo "error: base json not found: ${BASE_JSON}" >&2
  exit 1
fi

# Work from a private copy of the base, so a later write to LOCAL_JSON or
# OUT_JSON (either may be the same path) cannot change what the no-op check
# compares against.
BASE_SNAPSHOT="$(mktemp)"
cp "$BASE_JSON" "$BASE_SNAPSHOT"
BASE_JSON="$BASE_SNAPSHOT"

MERGED="$(python3 - "$BASE_JSON" "$BUNDLE_ID" "$VERSION" "$BUILD" "$APPLE_ID" "$DISPLAY_NAME" <<'PY'
import json, sys, datetime
path, bundle, version, build, apple_id, display = sys.argv[1:7]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
if not isinstance(data, dict):
    print("error: base json is not an object", file=sys.stderr)
    sys.exit(1)
apps = data.get("apps")
if not isinstance(apps, dict) or not apps:
    print("error: base json has no apps; refusing to publish a one-app replacement", file=sys.stderr)
    sys.exit(1)
before = set(apps)
entry = dict(apps.get(bundle) or {})
entry["marketingVersion"] = version
if build:
    entry["build"] = build
if apple_id:
    entry["appleId"] = int(apple_id)
if display:
    entry["displayName"] = display
apps[bundle] = entry
after = set(apps)
lost = sorted(before - after)
if lost:
    print("error: merge dropped apps: " + ", ".join(lost), file=sys.stderr)
    sys.exit(1)
data["apps"] = apps
data["schemaVersion"] = 1
data["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump(data, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
)"

if [[ "$SKIP_LOCAL_WRITE" -eq 0 ]]; then
  printf '%s\n' "$MERGED" >"$LOCAL_JSON"
  echo "local-manifest-updated ${BUNDLE_ID} ${VERSION}"
fi
if [[ -n "$OUT_JSON" ]]; then
  printf '%s\n' "$MERGED" >"$OUT_JSON"
fi

if [[ "$SKIP_PUSH" -eq 1 ]]; then
  echo "skip-push ${BUNDLE_ID} ${VERSION} apps=$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("apps") or {}))' <<<"$MERGED")"
  exit 0
fi

# Nothing to publish:  the remote already carries this version.  A pull
# request for an unchanged manifest is noise, and the fleet's merge queue is
# small.  The build is part of the comparison when one was passed, because a
# ship that re-records the same marketing version with a NEW build is a real
# change:  the version a tester installs is the build, not the label.
#
# This check runs BEFORE the credential is resolved.  A ship that has nothing
# to publish must not need a token, and it must not reach the network at all;
# a run that only re-records the version already on main leaves no pull
# request behind and says so.
# The comparison is the whole document minus updatedAt, so a change to the
# Apple ID or display name is a real change too.
if printf '%s\n' "$MERGED" | python3 -c '
import json, sys
def strip(d):
    d = dict(d); d.pop("updatedAt", None); return d
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    base = json.load(fh)
merged = json.loads(sys.stdin.read())
sys.exit(0 if strip(base) == strip(merged) else 1)
' "$BASE_JSON"; then
  echo "version-manifest: ${BUNDLE_ID} ${VERSION}${BUILD:+ build ${BUILD}} is already recorded"
  exit 0
fi

# Release gate: the fleet icon audit.  block (default) refuses to publish when
# the audit errors or cannot run; warn reports and continues; off skips.
ICON_MODE="${FLEET_ICON_AUDIT:-block}"
ICON_SCRIPT="${FLEET_ICON_AUDIT_SCRIPT:-${FLEET_DIR}/fleet-icon-audit.mjs}"
case "$ICON_MODE" in
  off) ;;
  block|warn)
    if node "$ICON_SCRIPT" --no-lanes >&2; then
      echo "icon-audit: clean"
    elif [[ "$ICON_MODE" == "block" ]]; then
      echo "error: icon audit failed or could not run; refusing to publish (FLEET_ICON_AUDIT=warn reports and continues, off skips)" >&2
      exit 1
    else
      echo "warning: icon audit reported errors; publishing anyway (FLEET_ICON_AUDIT=warn)" >&2
    fi
    ;;
  *) echo "error: FLEET_ICON_AUDIT must be block, warn or off (got: ${ICON_MODE})" >&2; exit 2 ;;
esac

# The credential is resolved before any API call, so a run that cannot
# possibly publish says why, instead of failing later on an authorization
# error that reads like a branch rule.
resolve_token
if [[ -n "$TOKEN" ]]; then
  export GH_TOKEN="$TOKEN"
  unset GITHUB_TOKEN || true
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh not installed; local manifest only" >&2
  exit 1
fi

PR_BRANCH="${PR_BRANCH_PREFIX}/$(printf '%s' "$BUNDLE_ID" | tr -c 'A-Za-z0-9._-' '-')"
PR_TITLE="chore: record ${BUNDLE_ID} ${VERSION} in the fleet iOS manifest"
# The gap between the sentences is a real no-break space plus a normal space,
# which is what GitHub renders.  The entity would show up literally.
SENTENCE_GAP=$'\xc2\xa0 '
PR_BODY="Automated fleet iOS version manifest update for ${BUNDLE_ID} ${VERSION}.${SENTENCE_GAP}Published by scripts/ios-fleet/publish-ios-versions.sh as a pull request, because AI-Fleet-Coordinator's ruleset refuses a direct write to main."

# 0.  Re-read the live manifest right before writing.  The merged document must
#     keep every app the live remote has (the real shrink guard), and when the
#     base was read from the remote, the remote must not have moved since.
LIVE_JSON="$(mktemp)"
if ! fetch_remote_json "$LIVE_JSON"; then
  echo "error: could not re-read the live manifest before publishing" >&2
  exit 1
fi
if [[ "$BASE_FROM_REMOTE" -eq 1 && "$REMOTE_BLOB_SHA" != "$SEEN_BLOB_SHA" ]]; then
  echo "error: ${REMOTE_PATH} changed on ${BASE_BRANCH} since it was read (concurrent publish); re-run to merge onto the new manifest" >&2
  exit 1
fi
MERGED_JSON="$MERGED" python3 - "$LIVE_JSON" <<'PY' || exit 1
import json, os, sys
live = json.load(open(sys.argv[1], encoding="utf-8")).get("apps") or {}
merged = json.loads(os.environ["MERGED_JSON"]).get("apps") or {}
gone = sorted(set(live) - set(merged))
if gone:
    print("error: the document would drop apps the live remote has: " + ", ".join(gone), file=sys.stderr)
    sys.exit(1)
if len(merged) < len(live):
    print("error: refusing to publish fewer apps than the live remote", file=sys.stderr)
    sys.exit(1)
PY

# 1.  The branch starts at the current default branch, so the pull request
#     never carries a stale manifest forward.
BASE_SHA="$(gh api "repos/${REPO}/git/ref/heads/${BASE_BRANCH}" --jq .object.sha)" || {
  echo "error: could not read ${REPO} ${BASE_BRANCH}" >&2
  exit 1
}
if gh api "repos/${REPO}/git/ref/heads/${PR_BRANCH}" >/dev/null 2>&1; then
  gh api --method PATCH "repos/${REPO}/git/refs/heads/${PR_BRANCH}" -f sha="$BASE_SHA" -F force=true >/dev/null
else
  gh api --method POST "repos/${REPO}/git/refs" -f ref="refs/heads/${PR_BRANCH}" -f sha="$BASE_SHA" >/dev/null
fi

# 2.  The document lands on that branch, never on the default branch.
CONTENT_B64="$(printf '%s\n' "$MERGED" | python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())')"
FILE_SHA="$(gh api "repos/${REPO}/contents/${REMOTE_PATH}?ref=${PR_BRANCH}" --jq .sha 2>/dev/null || true)"

PUT_ARGS=(
  --method PUT
  "repos/${REPO}/contents/${REMOTE_PATH}"
  -f message="chore: ${BUNDLE_ID} ${VERSION}"
  -f content="${CONTENT_B64}"
  -f branch="${PR_BRANCH}"
)
if [[ -n "$FILE_SHA" ]]; then
  PUT_ARGS+=(-f sha="$FILE_SHA")
fi
gh api "${PUT_ARGS[@]}" >/dev/null || {
  echo "error: version-manifest publish failed (branch ${PR_BRANCH})" >&2
  exit 1
}

# 3.  The pull request.  An open one for this branch is reused, so a fleet
#     that ships twice a day leaves one reviewable pull request, not a pile.
PR_NUMBER="$(gh api "repos/${REPO}/pulls?head=${REPO%%/*}:${PR_BRANCH}&state=open" --jq '.[0].number' 2>/dev/null || true)"
if [[ -n "$PR_NUMBER" && "$PR_NUMBER" != "null" ]]; then
  gh api --method PATCH "repos/${REPO}/pulls/${PR_NUMBER}" -f title="$PR_TITLE" -f body="$PR_BODY" >/dev/null
  PR_URL="https://github.com/${REPO}/pull/${PR_NUMBER}"
else
  PR_URL="$(gh api --method POST "repos/${REPO}/pulls" -f title="$PR_TITLE" -f body="$PR_BODY" -f head="$PR_BRANCH" -f base="$BASE_BRANCH" --jq .html_url)" || {
    echo "error: version-manifest publish failed (could not open the pull request)" >&2
    exit 1
  }
fi

# 4.  Arm the merge with the same elevated credential.  GitHub raises no
#     workflow events for a merge made with the repository's own
#     GITHUB_TOKEN, so the merge is armed here with the fleet token and
#     lands once `test` and `gitleaks` are green.  The default branch's
#     ruleset still decides whether the merge is allowed.
RC=0
if [[ "$AUTO_MERGE" == "1" ]]; then
  PR_NUM="${PR_URL##*/}"
  if gh pr merge "$PR_NUM" --repo "$REPO" --squash --auto >/dev/null 2>&1; then
    echo "version-manifest: auto-merge armed on ${PR_URL}"
  else
    echo "error: version-manifest auto-merge could not be armed on ${PR_URL}" >&2
    RC=1
  fi
fi

echo "version-manifest-pr ${PR_URL}"

# Sheet sync: the Fleet Reference Sheet follows the manifest.  The shim runs
# the registry sync only from a clean checkout at origin/main; a host without
# the shim skips it, a failing shim fails the run.
SHEET_SYNC="${FLEET_SHEET_SYNC:-$HOME/apps/fleet-apps-sheet-sync.mjs}"
if [[ -f "$SHEET_SYNC" ]]; then
  if node "$SHEET_SYNC" >&2; then
    echo "sheet-sync: done"
  else
    echo "error: fleet apps sheet sync failed (run it from an AFC lane: node scripts/fleet-apps-sheet-sync.mjs)" >&2
    RC=1
  fi
else
  echo "sheet-sync: shim ${SHEET_SYNC} not present, skipped"
fi
exit "$RC"
