#!/usr/bin/env bash
# ios-debug.sh - Capture the Xcode-console equivalent without opening Xcode.
#
# Live copy:  /Users/jay/apps/ios-fleet/ios-debug.sh
# Tracked:    AI-Fleet-Coordinator/scripts/ios-debug.sh
#
# Usage:
#   bash /Users/jay/apps/ios-fleet/ios-debug.sh <socratic|congress|usage|usage-local|dealdex|autorotate> [options]
#
# Options:
#   --target auto|simulator|device   Default auto (= simulator; never the phone unless --target device)
#   --install-debug                  Build Debug and install. On a physical device this
#                                    REPLACES the TestFlight/App Store copy of that bundle.
#   --logs-only                      Do not build/install. Stream or collect logs only.
#   --no-launch                      Do not launch; only collect logs after --seconds.
#   --seconds N                      Log window (default 45)
#   --screenshot                     Capture a PNG after launch (sim or device)
#   --repo-root PATH                 App checkout (default: worktreeHint, then cwd)
#   --device-name NAME               Prefer this paired device
#   --skip-xcodegen
#   --list                           Print simulator + paired devices and exit
#   --dry-run                        Print plan and exit
#
# When to run this (owner 2026-08-21). Canonical: FLEET-PROTOCOL.md § iOS agent build loop.
#   1. Simulator Debug + this script is the default for UI and print()/os_log.
#   2. Device --logs-only when the bug is phone-only and TestFlight is the truth.
#   3. Device --install-debug only when we need a DEBUG binary or unreleased code.
#   4. Ask the owner to press Run in Xcode only when LLDB / the IDE console is required.
#
# ASCII-only (Apple bash 3.2). Team: CC8UTF7ATG.

set -euo pipefail

export PATH="/Applications/Xcode.app/Contents/Developer/usr/bin:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:${PATH}"

FLEET_DIR="$(cd "$(dirname "$0")" && pwd)"
# Tracked copy lives in the coordinator repo; the live helper is ios-fleet.
if [[ "$(basename "$FLEET_DIR")" == "scripts" && -f "${FLEET_DIR}/../docs/ONBOARDING-NEW-AGENT.md" ]]; then
  if [[ -f "/Users/jay/apps/ios-fleet/apps.json" ]]; then
    FLEET_DIR="/Users/jay/apps/ios-fleet"
  fi
fi
APPS_JSON="${FLEET_DIR}/apps.json"
TEAM_ID="CC8UTF7ATG"
LOG_BIN="/usr/bin/log"

APP_KEY=""
TARGET="auto"
INSTALL_DEBUG=0
LOGS_ONLY=0
NO_LAUNCH=0
SECONDS_WIN=45
SCREENSHOT=0
REPO_ROOT=""
DEVICE_NAME=""
SKIP_XCODEGEN=0
LIST_ONLY=0
DRY_RUN=0

die() { echo "error: $*" >&2; exit 1; }
log() { echo "[ios-debug] $*"; }
need_owner() { echo "NEED OWNER: $*"; }

usage() {
  sed -n '2,${/^[^#]/q;p;}' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

json_get() {
  /usr/bin/python3 - "$APPS_JSON" "$1" "$2" <<'PY'
import json, sys
path, app, field = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path))
apps = data["apps"]
if app not in apps:
    sys.exit(0)
val = apps[app].get(field)
if val is None:
    pass
elif isinstance(val, list):
    print(",".join(val), end="")
else:
    print(val, end="")
PY
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage ;;
    --target) TARGET="${2:-}"; shift 2 ;;
    --install-debug) INSTALL_DEBUG=1; shift ;;
    --logs-only) LOGS_ONLY=1; shift ;;
    --no-launch) NO_LAUNCH=1; shift ;;
    --seconds) SECONDS_WIN="${2:-}"; shift 2 ;;
    --screenshot) SCREENSHOT=1; shift ;;
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    --device-name) DEVICE_NAME="${2:-}"; shift 2 ;;
    --skip-xcodegen) SKIP_XCODEGEN=1; shift ;;
    --list) LIST_ONLY=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; break ;;
    -*) die "unknown option: $1" ;;
    *)
      if [[ -z "$APP_KEY" ]]; then
        APP_KEY="$1"
        shift
      else
        die "unexpected argument: $1"
      fi
      ;;
  esac
done

[[ "$SECONDS_WIN" =~ ^[0-9]+$ ]] || die "--seconds must be an integer"
[[ "$TARGET" == "auto" || "$TARGET" == "simulator" || "$TARGET" == "device" ]] || die "--target must be auto|simulator|device"

pick_device() {
  local json_path="$1" want_name="$2"
  /usr/bin/python3 - "$json_path" "$want_name" <<'PY'
import json, sys
path, want = sys.argv[1], sys.argv[2]
data = json.load(open(path))
devices = data.get("result", {}).get("devices") or []
physical = []
for it in devices:
    hw = it.get("hardwareProperties") or {}
    dp = it.get("deviceProperties") or {}
    cp = it.get("connectionProperties") or {}
    if hw.get("reality") != "physical":
        continue
    name = dp.get("name") or ""
    tunnel = (cp.get("tunnelState") or "").lower()
    transport = (cp.get("transportType") or "").lower()
    devmode = (dp.get("developerModeStatus") or "").lower()
    boot = (dp.get("bootState") or "").lower()
    score = 0
    if want and want.lower() in name.lower():
        score += 100
    if "jay" in name.lower() and "iphone" in name.lower():
        score += 50
    if "iphone" in name.lower():
        score += 10
    if tunnel in ("connected", "connecting"):
        score += 20
    if transport in ("wired", "usb"):
        score += 15
    if boot == "booted":
        score += 5
    if devmode == "enabled":
        score += 5
    physical.append((score, {
        "name": name,
        "id": it.get("identifier") or "",
        "udid": hw.get("udid") or "",
        "tunnel": tunnel or "unknown",
        "transport": transport or "unknown",
        "devmode": devmode or "unknown",
        "boot": boot or "unknown",
        "product": hw.get("marketingName") or hw.get("productType") or "",
    }))
physical.sort(key=lambda x: x[0], reverse=True)
if not physical:
    sys.exit(3)
best = physical[0][1]
need = []
if best["devmode"] != "enabled":
    need.append("enable Developer Mode on the phone (Settings > Privacy & Security)")
if best["tunnel"] not in ("connected", "connecting") and best["transport"] not in ("wired", "usb"):
    need.append("plug the phone into this Mac (USB) or keep it unlocked on the same network so CoreDevice can connect")
if best["boot"] and best["boot"] not in ("booted", "unknown"):
    need.append("unlock / power on the phone")
print("NAME=" + best["name"])
print("ID=" + best["id"])
print("UDID=" + best["udid"])
print("TUNNEL=" + best["tunnel"])
print("TRANSPORT=" + best["transport"])
print("DEVMODE=" + best["devmode"])
print("BOOT=" + best["boot"])
print("PRODUCT=" + best["product"])
print("NEED=" + " | ".join(need))
PY
}

list_all() {
  log "simulators (available):"
  xcrun simctl list devices available
  echo
  log "CoreDevice:"
  xcrun devicectl list devices
}

if [[ "$LIST_ONLY" -eq 1 ]]; then
  list_all
  exit 0
fi

[[ -n "$APP_KEY" ]] || usage
[[ -f "$APPS_JSON" ]] || die "missing $APPS_JSON"

DISPLAY_NAME="$(json_get "$APP_KEY" displayName)"
[[ -n "$DISPLAY_NAME" ]] || die "unknown app key: $APP_KEY"
BUNDLE_ID="$(json_get "$APP_KEY" bundleId)"
SCHEME="$(json_get "$APP_KEY" scheme)"
PROJECT_REL="$(json_get "$APP_KEY" projectRel)"
PROJECT_ALT="$(json_get "$APP_KEY" projectRelAlt)"
XCODEGEN_DIR="$(json_get "$APP_KEY" xcodegenDir)"
WORKTREE_HINT="$(json_get "$APP_KEY" worktreeHint)"

if [[ -z "$REPO_ROOT" ]]; then
  hint="${WORKTREE_HINT/#\~/$HOME}"
  if [[ -n "$hint" && -d "$hint" ]]; then
    REPO_ROOT="$hint"
  else
    REPO_ROOT="$(pwd)"
  fi
fi

resolve_project() {
  local root="$1" rel="$2" alt="$3"
  if [[ -n "$rel" && -e "${root}/${rel}" ]]; then
    echo "${root}/${rel}"
    return 0
  fi
  if [[ -n "$alt" && -e "${root}/${alt}" ]]; then
    echo "${root}/${alt}"
    return 0
  fi
  return 1
}

PROJECT=""
if PROJECT="$(resolve_project "$REPO_ROOT" "$PROJECT_REL" "$PROJECT_ALT")"; then
  :
else
  PROJECT=""
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${FLEET_DIR}/artifacts/${APP_KEY}/debug/${STAMP}"
mkdir -p "$OUT_DIR"

PRED="subsystem CONTAINS[c] \"${BUNDLE_ID}\" OR processImagePath CONTAINS[c] \"${SCHEME}\" OR process CONTAINS[c] \"${SCHEME}\""

if [[ "$TARGET" == "auto" ]]; then
  TARGET="simulator"
fi

log "app=${APP_KEY} (${DISPLAY_NAME})"
log "bundle=${BUNDLE_ID} scheme=${SCHEME}"
log "target=${TARGET} install_debug=${INSTALL_DEBUG} logs_only=${LOGS_ONLY} seconds=${SECONDS_WIN}"
log "repo=${REPO_ROOT}"
log "out=${OUT_DIR}"

if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ "$LOGS_ONLY" -eq 1 ]]; then
    log "dry-run: would collect logs on ${TARGET}"
  else
    log "dry-run: would build/launch on ${TARGET}"
  fi
  [[ -n "$PROJECT" ]] && log "project=${PROJECT}" || log "project=UNRESOLVED"
  if [[ "$TARGET" == "device" ]]; then
    tmp="$(mktemp -t ios-debug-devices)"
    xcrun devicectl list devices --json-output "$tmp" >/dev/null
    pick_device "$tmp" "$DEVICE_NAME" || true
    rm -f "$tmp"
  fi
  exit 0
fi

run_xcodegen() {
  if [[ "$SKIP_XCODEGEN" -eq 1 ]]; then
    return 0
  fi
  if [[ -z "$XCODEGEN_DIR" || "$XCODEGEN_DIR" == "null" ]]; then
    return 0
  fi
  local dir="${REPO_ROOT}/${XCODEGEN_DIR}"
  [[ -f "${dir}/project.yml" ]] || return 0
  command -v xcodegen >/dev/null 2>&1 || die "xcodegen not on PATH (needed for ${dir}/project.yml)"
  log "xcodegen generate in ${dir}"
  (cd "$dir" && xcodegen generate)
}

build_debug() {
  local dest="$1"
  local dd="${OUT_DIR}/DerivedData"
  [[ -n "$PROJECT" ]] || die "could not resolve .xcodeproj under ${REPO_ROOT}"
  run_xcodegen
  mkdir -p "$dd"
  log "xcodebuild Debug destination=${dest}"
  xcodebuild \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -configuration Debug \
    -destination "$dest" \
    -derivedDataPath "$dd" \
    DEVELOPMENT_TEAM="$TEAM_ID" \
    -allowProvisioningUpdates \
    -quiet \
    build
}

find_app() {
  local dd="${OUT_DIR}/DerivedData"
  local suffix="$1"
  /usr/bin/python3 - "$dd" "$suffix" <<'PY'
import os, sys
root, suffix = sys.argv[1], sys.argv[2]
products = os.path.join(root, "Build", "Products")
if not os.path.isdir(products):
    sys.exit(4)
hits = []
for dirpath, dirnames, filenames in os.walk(products):
    for name in dirnames:
        if name.endswith(".app") and suffix in dirpath:
            hits.append(os.path.join(dirpath, name))
    dirnames[:] = [d for d in dirnames if not d.endswith(".app")]
if not hits:
    sys.exit(4)
print(sorted(hits, key=len)[0])
PY
}

screenshot_sim() {
  local png="${OUT_DIR}/screenshot.png"
  xcrun simctl io booted screenshot "$png"
  log "screenshot ${png}"
}

do_simulator() {
  local booted
  booted="$(xcrun simctl list devices | awk -F '[()]' '/Booted/{print $2; exit}')"
  if [[ -z "$booted" ]]; then
    log "no booted simulator; listing available"
    xcrun simctl list devices available
    die "boot a simulator first (xcrun simctl boot <udid>)"
  fi
  log "booted simulator ${booted}"

  if [[ "$LOGS_ONLY" -eq 0 && "$NO_LAUNCH" -eq 0 ]]; then
    build_debug "platform=iOS Simulator,id=${booted}"
    APP_PATH="$(find_app iphonesimulator)" || die "Debug .app not found after simulator build"
    log "install ${APP_PATH}"
    xcrun simctl install booted "$APP_PATH"
  fi

  local stream_log="${OUT_DIR}/sim-log-stream.txt"
  local console_log="${OUT_DIR}/sim-console.txt"
  log "streaming os_log for ${SECONDS_WIN}s -> ${stream_log}"
  "$LOG_BIN" stream \
    --style compact \
    --level debug \
    --timeout "${SECONDS_WIN}s" \
    --predicate "$PRED" \
    >"$stream_log" 2>&1 &
  local stream_pid=$!

  if [[ "$NO_LAUNCH" -eq 0 ]]; then
    log "launch --console ${BUNDLE_ID} (print/NSLog; ${SECONDS_WIN}s cap)"
    # --console is the Xcode debug-area equivalent on the simulator.
    perl -e 'alarm shift; exec @ARGV' "$SECONDS_WIN" \
      xcrun simctl launch --console --terminate-running-process booted "$BUNDLE_ID" \
      >"$console_log" 2>&1 || true
  else
    sleep "$SECONDS_WIN"
  fi

  wait "$stream_pid" 2>/dev/null || true
  if [[ "$SCREENSHOT" -eq 1 ]]; then
    screenshot_sim
  fi
  log "os_log ${stream_log}"
  if [[ -s "$console_log" ]]; then
    log "console ${console_log}"
    log "---- last 40 console lines ----"
    tail -n 40 "$console_log"
  fi
  if [[ -s "$stream_log" ]]; then
    log "---- last 40 os_log lines ----"
    tail -n 40 "$stream_log"
  fi
}

do_device() {
  local tmp need_line
  tmp="$(mktemp -t ios-debug-devices)"
  xcrun devicectl list devices --json-output "$tmp" >/dev/null
  if ! pick_device "$tmp" "$DEVICE_NAME" >"${OUT_DIR}/device-pick.txt"; then
    rm -f "$tmp"
    need_owner "No physical iPhone/iPad is paired. Plug Jay's iPhone into this Mac, unlock it, and trust the computer."
    die "no physical device"
  fi
  rm -f "$tmp"
  DEV_NAME="" DEV_ID="" DEV_UDID="" DEV_TUNNEL="" DEV_TRANSPORT="" NEED_LINE=""
  while IFS= read -r line; do
    case "$line" in
      NAME=*) DEV_NAME="${line#NAME=}" ;;
      ID=*) DEV_ID="${line#ID=}" ;;
      UDID=*) DEV_UDID="${line#UDID=}" ;;
      TUNNEL=*) DEV_TUNNEL="${line#TUNNEL=}" ;;
      TRANSPORT=*) DEV_TRANSPORT="${line#TRANSPORT=}" ;;
      NEED=*) NEED_LINE="${line#NEED=}" ;;
    esac
  done < "${OUT_DIR}/device-pick.txt"
  log "device ${DEV_NAME} id=${DEV_ID} udid=${DEV_UDID} tunnel=${DEV_TUNNEL} transport=${DEV_TRANSPORT}"
  if [[ -n "$NEED_LINE" ]]; then
    need_owner "$NEED_LINE"
    die "device not reachable for CoreDevice (tunnel=${DEV_TUNNEL})"
  fi

  local lock_json="${OUT_DIR}/lock.json"
  if xcrun devicectl device info lockState --device "$DEV_ID" --json-output "$lock_json" >/dev/null 2>&1; then
    if /usr/bin/python3 - "$lock_json" <<'PY'
import json, sys
d=json.load(open(sys.argv[1]))
# pass if we cannot tell
passed = True
try:
    res = d.get("result") or d
    locked = res.get("locked")
    if locked is True:
        passed = False
except Exception:
    passed = True
sys.exit(0 if passed else 5)
PY
    then
      :
    else
      need_owner "Unlock ${DEV_NAME} (passcode / Face ID) and leave it awake."
      die "device locked"
    fi
  fi

  if [[ "$INSTALL_DEBUG" -eq 1 && "$LOGS_ONLY" -eq 0 ]]; then
    log "WARNING: Debug install replaces TestFlight/App Store for ${BUNDLE_ID} on this device until you reinstall from TestFlight."
    build_debug "id=${DEV_UDID}"
    APP_PATH="$(find_app iphoneos)" || die "Debug .app not found after device build"
    log "install ${APP_PATH} -> ${DEV_NAME}"
    xcrun devicectl device install app --device "$DEV_ID" "$APP_PATH"
  fi

  if [[ "$NO_LAUNCH" -eq 0 ]]; then
    log "launch ${BUNDLE_ID} on ${DEV_NAME}"
    xcrun devicectl device process launch --device "$DEV_ID" --terminate-existing --activate "$BUNDLE_ID" \
      || need_owner "Launch failed. Unlock the phone, open the app once, or reinstall from TestFlight if the bundle is missing."
  fi

  if [[ "$SCREENSHOT" -eq 1 ]]; then
    local png="${OUT_DIR}/screenshot.png"
    xcrun devicectl device capture screenshot --device "$DEV_ID" --destination "$png" \
      && log "screenshot ${png}" \
      || log "screenshot failed (device may be locked or wireless tunnel dropped)"
  fi

  need_owner "Reproduce the bug on ${DEV_NAME} now. I am collecting ${SECONDS_WIN}s of unified logs (no Xcode). Leave the phone unlocked."
  sleep "$SECONDS_WIN"

  local archive="${OUT_DIR}/device.logarchive"
  log "log collect --device-udid ${DEV_UDID} --last ${SECONDS_WIN}s"
  if "$LOG_BIN" collect --device-udid "$DEV_UDID" --last "${SECONDS_WIN}s" --output "$archive" --predicate "$PRED"; then
    local shown="${OUT_DIR}/device-log.txt"
    "$LOG_BIN" show "$archive" --style compact --info --debug --predicate "$PRED" >"$shown" || true
    log "device log ${shown}"
    if [[ -s "$shown" ]]; then
      log "---- last 60 device log lines ----"
      tail -n 60 "$shown"
    else
      log "no matching os_log lines. print()/NSLog from a TestFlight build often never appear here; Debug install or an OSLog Logger(subsystem:) is required."
    fi
  else
    need_owner "log collect failed. Plug the phone in over USB, unlock it, and I will retry. Or paste the Xcode / Console.app pane."
    die "log collect failed"
  fi
}

case "$TARGET" in
  simulator) do_simulator ;;
  device) do_device ;;
  *) die "internal: bad target $TARGET" ;;
esac

log "done. artifacts in ${OUT_DIR}"
echo "$OUT_DIR"
