#!/usr/bin/env bash
# Shellular ACP entry for Antigravity session/list.
# Stdout must stay JSON-RPC only.  Diagnostics go to the node wrapper stderr.
set -euo pipefail
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/Users/jay/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec node "$ROOT/agy-acp-list-wrapper.cjs" "$@"
