#!/usr/bin/env bash
# Copy tracked Grok TUI drive helpers from this repo into live ~/apps/.
#
# On-demand.  Run after merging AI-Fleet-Coordinator so agents do not keep
# using a stale ~/apps/grok-acp-runtime or ~/apps/seat-mcp.  Does not bind
# :2419.  Does not start a second seat-mcp unless --restart-seat-mcp and
# pm2 already owns the job.
#
# Usage:
#   bash scripts/install-grok-tui-drive.sh
#   bash scripts/install-grok-tui-drive.sh --dry-run
#   bash scripts/install-grok-tui-drive.sh --restart-seat-mcp
set -euo pipefail

DRY=0
RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --restart-seat-mcp) RESTART=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"
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
SEAT_SRC="$REPO_ROOT/scripts/seat-mcp"
MCP_SRC="$REPO_ROOT/scripts/mcp-servers"
ACP_DST="${HOME}/apps/grok-acp-runtime"
SEAT_DST="${HOME}/apps/seat-mcp"
MCP_DST="${HOME}/apps/mcp-servers"

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

if [[ ! -d "$ACP_SRC" || ! -d "$SEAT_SRC" ]]; then
  echo "run this from an AI-Fleet-Coordinator checkout that has scripts/grok-acp-runtime" >&2
  exit 1
fi

mkdir -p "$ACP_DST" "$SEAT_DST" "$MCP_DST" 2>/dev/null || true

for f in grok-drive.py leader-client.py session_disk.py grok-idle-unload.py README.md acp-client.py mcp_catalog.py start.sh acp-home-config.toml; do
  copy_file "$ACP_SRC/$f" "$ACP_DST/$f"
done
chmod +x "$ACP_DST/acp-client.py" "$ACP_DST/start.sh" "$ACP_DST/grok-idle-unload.py" 2>/dev/null || true

PLIST_SRC="$REPO_ROOT/scripts/launchd/com.jay.grok-idle-unload.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/com.jay.grok-idle-unload.plist"
if [[ -f "$PLIST_SRC" ]]; then
  copy_file "$PLIST_SRC" "$PLIST_DST"
fi

copy_file "$SEAT_SRC/start.sh" "$SEAT_DST/start.sh"
copy_file "$SEAT_SRC/README.md" "$SEAT_DST/README.md"
copy_file "$SEAT_SRC/mcp.example.json" "$SEAT_DST/mcp.example.json"
copy_file "$SEAT_SRC/ecosystem.snippet.cjs" "$SEAT_DST/ecosystem.snippet.cjs"
copy_file "$SEAT_SRC/prove.py" "$SEAT_DST/prove.py"
mkdir -p "$SEAT_DST/seat_mcp" 2>/dev/null || true
for f in "$SEAT_SRC"/seat_mcp/*.py; do
  copy_file "$f" "$SEAT_DST/seat_mcp/$(basename "$f")"
done

if [[ -d "$MCP_SRC" ]]; then
  copy_file "$MCP_SRC/seat-mcp-launch.sh" "$MCP_DST/seat-mcp-launch.sh"
  copy_file "$MCP_SRC/seat-mcp-stdio-proxy.py" "$MCP_DST/seat-mcp-stdio-proxy.py"
  chmod +x "$MCP_DST/seat-mcp-launch.sh" 2>/dev/null || true
fi

chmod +x "$ACP_DST/grok-drive.py" "$ACP_DST/leader-client.py" "$SEAT_DST/start.sh" 2>/dev/null || true

if [[ "$RESTART" -eq 1 ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    echo "dry-run: would recycle pm2 seat-mcp only if it owns :8793 (never a second bind)"
  else
    holder="$(/usr/sbin/lsof -nP -iTCP:8793 -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $2}')"
    if ! command -v pm2 >/dev/null 2>&1 || ! pm2 describe seat-mcp >/dev/null 2>&1; then
      echo "seat-mcp is not a pm2 job; leave the existing 127.0.0.1:8793 listener alone" >&2
      echo "If you started it by hand, restart THAT process.  Do not bind a second :8793." >&2
    elif [[ -n "$holder" ]]; then
      holder_cmd="$(ps -p "$holder" -o command= 2>/dev/null || true)"
      pm2_pid="$(pm2 pid seat-mcp 2>/dev/null | head -1 | tr -d '[:space:]')"
      if [[ -n "$pm2_pid" && "$pm2_pid" != "0" && "$holder" == "$pm2_pid" ]]; then
        pm2 restart seat-mcp --update-env
        echo "restarted pm2 seat-mcp (it owned :8793)"
      else
        echo "refusing pm2 restart: :8793 is pid $holder ($holder_cmd), not pm2 seat-mcp ($pm2_pid)" >&2
        echo "Stop the crash-loop (pm2 stop seat-mcp), SIGTERM the seat_mcp orphan, then pm2 start seat-mcp." >&2
        exit 3
      fi
    else
      pm2 start seat-mcp --update-env
      echo "started pm2 seat-mcp on free :8793"
    fi
  fi
fi

echo "ok live helpers are $ACP_DST and $SEAT_DST"
