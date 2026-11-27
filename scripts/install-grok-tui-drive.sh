#!/usr/bin/env bash
# Copy tracked Grok TUI drive helpers from this repo into live ~/apps/.
#
# On-demand.  Run after merging AI-Fleet-Coordinator so agents do not keep
# using a stale ~/apps/grok-acp-runtime.  Standalone pm2 grok-acp (:12419) and
# seat-mcp (:8793) are retired (AFC-926); this installer now copies only the
# grok-leader / grok-drive.py helpers and shared Infisical reader.
#
# Usage:
#   bash scripts/install-grok-tui-drive.sh
#   bash scripts/install-grok-tui-drive.sh --dry-run
set -euo pipefail

DRY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ACP_SRC="$REPO_ROOT/scripts/grok-acp-runtime"
ACP_DST="${HOME}/apps/grok-acp-runtime"

copy_file() {
  local src="$1" dst="$2"
  if [[ ! -f "$src" ]]; then
    echo "missing $src" >&2
    exit 1
  fi
  if [[ "$DRY" -eq 1 ]]; then
    echo "dry-run: $src -> $dst"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  cp -p "$src" "$dst"
  echo "installed $dst"
}

if [[ ! -d "$ACP_SRC" ]]; then
  echo "run this from an AI-Fleet-Coordinator checkout that has scripts/grok-acp-runtime" >&2
  exit 1
fi

mkdir -p "$ACP_DST" 2>/dev/null || true

# Shared source closure for the copied runtime consumers.
copy_file "$REPO_ROOT/scripts/fleet_infisical.py" "${HOME}/apps/fleet_infisical.py"
copy_file "$REPO_ROOT/scripts/fleet_infisical_cli.py" "${HOME}/apps/fleet_infisical_cli.py"

# The scoped reader the runtime reads its secret through:  the shared client and
# the one reader command beside it,  at the stable path every consumer outside this
# repository uses.  The account is named by the caller,  so the read uses that
# consumer's own scoped service token and never an ambient environment token.
SCOPED_DST="${HOME}/apps/infisical-scoped"
copy_file "$REPO_ROOT/scripts/fleet_infisical.py" "${SCOPED_DST}/fleet_infisical.py"
copy_file "$REPO_ROOT/scripts/infisical-scoped.py" "${SCOPED_DST}/infisical-scoped.py"
chmod 0755 "${SCOPED_DST}/infisical-scoped.py" 2>/dev/null || true

for f in grok-drive.py leader-client.py session_disk.py grok-idle-unload.py README.md; do
  copy_file "$ACP_SRC/$f" "$ACP_DST/$f"
done
chmod +x "$ACP_DST/grok-drive.py" "$ACP_DST/leader-client.py" "$ACP_DST/grok-idle-unload.py" 2>/dev/null || true

PLIST_SRC="$REPO_ROOT/scripts/launchd/com.jay.grok-idle-unload.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.jay.grok-idle-unload.plist"
if [[ -f "$PLIST_SRC" ]]; then
  copy_file "$PLIST_SRC" "$PLIST_DST"
fi

echo "ok live helpers are in $ACP_DST"
