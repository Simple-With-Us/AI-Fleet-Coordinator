#!/usr/bin/env bash
# Shellular ACP spawn for DeepSeek Harness. Stdout is JSON-RPC only.
# Tracked copy: AI-Fleet-Coordinator/scripts/dsh-acp.sh
# Live install: ~/apps/dsh-runtime/dsh-acp.sh
set -euo pipefail

export DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
export DSH_RUNTIME_ROOT="${DSH_RUNTIME_ROOT:-/Users/jay/apps/dsh-runtime}"
export DSH_PERMISSION_MODE="${DSH_PERMISSION_MODE:-danger-full-access}"

ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /opt/homebrew/bin/python3 "$ROOT/dsh-acp.py" "$@"
