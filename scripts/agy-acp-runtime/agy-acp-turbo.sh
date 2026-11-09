#!/usr/bin/env bash
# Shellular ACP wrapper for Antigravity (auto-approve / turbo YOLO mode).
# Stdout must be clean JSON-RPC only.
set -euo pipefail
export AGY_EXTRA_ARGS="${AGY_EXTRA_ARGS:---dangerously-skip-permissions --mode accept-edits}"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/Users/jay/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
exec /usr/local/bin/agy-acp --skip-naration "$@"
